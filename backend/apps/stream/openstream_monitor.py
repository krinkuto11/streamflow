"""
OpenStream stream monitor.

A drop-in alternative to :class:`FFmpegStreamMonitor` that sources a stream's
health from an OpenStream server's continuous swarm-health API instead of running
an ffmpeg process. It duck-types the ffmpeg monitor (``.stats``/``start``/``stop``/
``get_stats``/``get_transport_health``/``is_buffering``) so the existing monitoring
service — reliability scoring (``add_measurement``), ranking and Dispatcharr
reordering — works against it unchanged.

Used only by monitoring sessions of type ``openstream`` (AceStream channels whose
Dispatcharr stream URLs point at an OpenStream/AceStream gateway). Both the API
base URL and the 40-hex content id are derived from the stream URL, so no separate
server configuration is needed.

Mapping OpenStream snapshot -> ffmpeg-style stats:
  * ``speed``       <- ``health.keepUpMargin``  (cursor advance / live-edge advance; ~1.0 = keeping up)
  * ``is_alive``    <- ``health.state != "dead"``
  * ``is_buffering``<- ``health.state in {warming, draining, stalled}``
  * ``bitrate``     <- ``health.trueBitrateKbps``

The snapshot also carries swarm telemetry that has no ffmpeg equivalent, surfaced
verbatim for the UI (these are the *reliable* signals on a warm live pull, where
raw keep-up margin is noisy — see the OpenStream MONITORING docs):
  * ``download_kbps``    <- top-level ``kbps``     (smoothed swarm download rate)
  * ``peers`` / ``seeders`` <- top-level ``peers`` / ``seeders``
  * ``reliability_score``<- ``health.reliabilityScore`` (0..1 EWMA, the primary rank key)
  * ``latency_secs``     <- ``health.latencySecs``
  * ``swarm_state``      <- ``health.state``

Only a reported ``state == "dead"`` marks the stream fatally dead; transient poll
failures never do (a down OpenStream server must not evict every source).

Authentication: OpenStream's ``/api`` needs an API key (OpenStream → Settings → API
Keys), configured in Streamflow under Settings → Connection or via
``OPENSTREAM_API_KEY`` and sent as ``X-API-Key``. A refused key is reported
on the stream (``error_message``) but, like an unreachable server, it is
never fatal: a missing key must not evict every source.
"""

import re
import time
from collections import deque
from typing import Callable, Optional, Tuple
from urllib.parse import urlsplit, parse_qs

from apps.core.logging_config import setup_logging
from apps.stream.openstream_hub import get_hub
from apps.stream.ffmpeg_stream_monitor import FFmpegStats

logger = setup_logging(__name__)

_CID_RE = re.compile(r"[0-9a-fA-F]{40}")


# health.state values that mean "connected but not cleanly keeping up".
_BUFFERING_STATES = {"warming", "draining", "stalled"}

# Ranking. A source's score is the share of the live stream it actually
# received over the last RANK_WINDOW_SECS: the mean of min(1, kbps / true
# edge bitrate), x100. Delivery is the causal quantity: below the source's own
# rate the viewer's buffer drains. Chosen against what a viewer of each pull
# saw (playsim; 2/5/10 s player caches) in two 80-min runs on 25 different live
# sources during live events, picked on run 1 and checked on run 2:
#   agreement with which source stalls next (pooled, 5 s cache, 5 min ahead)
#     this score 0.84 · healthy-poll window used before 0.69 · OpenStream's
#     reliabilityScore 0.69; best of the 7 candidates in all 6 cache/horizon
#     conditions
#   viewer stall through Streamflow's ordering and 10-point switch hysteresis
#     run 1: 0.00 min/h, 0 switches (before: 15.1 min/h) · run 2: 0.07 min/h
#   score >= 70 (pass review) -> stall-free for the next 5 min: 87% / 91% of
#     the time (base rates 66% / 90%)
# The healthy-poll window failed because a source that stalls briefly and
# often reads healthy most of the time: one read healthy in 77% of polls and
# stalled a viewer 137 times.
RANK_WINDOW_SECS = 300.0

# "dead" is only fatal once it lasts this long. Dead sources come back: in the
# same runs 59 of 59 dead episodes recovered, the slowest after 182 s (5 of
# them under OpenStream's current 20-s-without-life rule, up to 176 s).
# Quarantine (15 min, removed from the Dispatcharr channel) for a blip costs a
# good source mid-event, while a dead source already scores 0 (it delivers
# nothing) and drops in rank at once without being evicted.
DEAD_FATAL_SECS = 300.0

# OpenStream's answers when the control plane refuses a request: 401 = the API
# key is wrong or revoked, 403 = no key was sent, 428 = OpenStream has no
# account yet (first-run setup not done), so no key can exist either.
_AUTH_ERRORS = {
    401: "OpenStream rejected the API key (wrong or revoked). Update it in Settings → Connection.",
    403: "OpenStream requires an API key. Create one in OpenStream (Settings → API Keys) and add it in Settings → Connection.",
    428: "OpenStream has no account yet. Finish its first-run setup, then create an API key.",
}


def parse_openstream_url(url: str) -> Tuple[Optional[str], Optional[str]]:
    """Extract ``(api_base, content_id)`` from an AceStream gateway stream URL.

    Handles both ``http://host:6878/ace/getstream?id=<40hex>`` and the short
    ``http://host:6878/<40hex>`` form. Returns ``(None, None)`` if no 40-hex
    content id or usable base can be found.
    """
    if not url:
        return None, None
    try:
        parts = urlsplit(url)
    except ValueError:
        return None, None
    if not parts.scheme or not parts.netloc:
        return None, None
    # Prefer the ?id= query param; fall back to the first 40-hex run in the path.
    cid = None
    qs = parse_qs(parts.query)
    if "id" in qs and qs["id"]:
        m = _CID_RE.search(qs["id"][0])
        if m:
            cid = m.group(0)
    if cid is None:
        m = _CID_RE.search(parts.path)
        if m:
            cid = m.group(0)
    if cid is None:
        return None, None
    base = f"{parts.scheme}://{parts.netloc}"
    return base, cid.lower()


class OpenStreamStreamMonitor:
    """One source's health, fed by its server's shared hub (openstream_hub).
    See module docstring."""

    def __init__(
        self,
        url: str,
        stream_id: Optional[int] = None,
        on_stats_update: Optional[Callable[[FFmpegStats], None]] = None,
        owner: str = "streamflow",
    ):
        self.url = url
        self.stream_id = stream_id
        self.on_stats_update = on_stats_update
        # Lease owner on the OpenStream server. One per Streamflow session, so
        # two sessions sharing a source hold separate claims and stopping one
        # never takes the source from the other.
        self.owner = owner
        self.stats = FFmpegStats(url=url)
        self.base, self.content_id = parse_openstream_url(url)
        self.stopped = False
        self._hub = None
        self._buffering = False
        self._state = "warming"
        self._error_density = 0.0
        self._auth_error: Optional[int] = None  # last refusal status, to log once
        self._delivery = deque()  # (t, min(1, kbps / true bitrate)) over RANK_WINDOW_SECS
        self._dead_since: Optional[float] = None
        # No UDP sidecar pipes (screenshots/CV don't apply to health monitoring);
        # present as attributes so any incidental access stays safe.
        self.port_a = None
        self.port_b = None

    # ---- lifecycle (mirrors FFmpegStreamMonitor) ----

    def start(self) -> bool:
        """Registers with the server's hub. Never blocks on the network: the
        lease and the first poll happen on the hub's thread, so a slow or
        unreachable server cannot stall Streamflow's monitoring loop."""
        if not self.base or not self.content_id:
            self.stats.is_alive = False
            self.stats.error_message = "Not an OpenStream/AceStream URL (no content id)"
            return False
        self.stopped = False
        self.stats.is_alive = True
        self.stats.last_updated = time.time()
        self._hub = get_hub(self.base)
        self._hub.register(self)
        return True

    def stop(self):
        # Safe from inside the hub thread (the service stops a dead source from
        # the stats callback): unregistering only queues the lease release.
        if self.stopped:
            return
        self.stopped = True
        if self._hub is not None:
            self._hub.unregister(self)

    def is_running(self) -> bool:
        return not self.stopped and self._hub is not None and self._hub.is_running()

    def is_alive(self) -> bool:
        return bool(self.stats.is_alive)

    def is_buffering(self) -> bool:
        return self._buffering

    def get_stats(self) -> FFmpegStats:
        return self.stats

    def get_transport_health(self):
        status = "healthy"
        if self._state == "dead":
            status = "dead"
        elif self._buffering:
            status = "degraded"
        peers = self.stats.peers if self.stats.peers is not None else 0
        seeders = self.stats.seeders if self.stats.seeders is not None else 0
        summary = f"openstream:{self._state} · {seeders}/{peers} seeders/peers"
        return {"status": status, "summary": summary, "error_density": self._error_density}

    def rank_score(self) -> Optional[float]:
        """0-100 ranking score (see RANK_WINDOW_SECS), or None before any
        sample (the service then keeps its neutral starting score)."""
        if not self._delivery:
            return None
        return 100.0 * sum(v for _, v in self._delivery) / len(self._delivery)

    def _note_delivery(self, now: float, value: float):
        self._delivery.append((now, value))
        while self._delivery and self._delivery[0][0] < now - RANK_WINDOW_SECS:
            self._delivery.popleft()

    def _notify(self):
        if self.on_stats_update and not self.stopped:
            try:
                self.on_stats_update(self.stats)
            except Exception:  # never let a callback break the hub
                logger.exception("on_stats_update raised for stream %s", self.stream_id)

    # ---- applied by the hub, once per poll ----
    #
    # Every applied result, success or handled failure, refreshes
    # last_updated: the service restarts a monitor whose last_updated is older
    # than the session timeout, and a restart cannot fix an unreachable server
    # or a refused key. The timeout is for a hub that stopped polling.

    def _apply_unreachable(self, now: Optional[float] = None):
        """Server unreachable or answered garbage: unknown health, never fatal
        (a down OpenStream server must not evict every source)."""
        self.stats.last_updated = now if now is not None else time.time()
        self._buffering = True
        self.stats.speed = 0.0
        self.stats.is_alive = True
        self.stats.is_fatal = False
        self.stats.error_message = None

    def _apply_auth_error(self, status: int, now: Optional[float] = None):
        """OpenStream refused the request. Like an unreachable server this is
        unknown health, not a dead source: report it and keep polling, so the
        stream recovers on its own once the key is fixed."""
        message = _AUTH_ERRORS[status]
        if self._auth_error != status:
            logger.warning("OpenStream at %s refused stream %s (HTTP %s): %s",
                           self.base, self.content_id, status, message)
            self._auth_error = status
        self.stats.last_updated = now if now is not None else time.time()
        self._buffering = True
        self.stats.speed = 0.0
        self.stats.is_alive = True
        self.stats.is_fatal = False
        self.stats.error_message = message

    def _apply_swarm_telemetry(self, snap: dict, health: dict):
        """Copy the swarm signals (peers/seeders/download/score/latency) that have
        no ffmpeg equivalent onto stats, so the API and UI can show them, and
        the source's video format from OpenStream's one-time media probe."""
        self.stats.peers = _as_int(snap.get("peers"))
        self.stats.seeders = _as_int(snap.get("seeders"))
        self.stats.download_kbps = _as_float(snap.get("kbps"))
        self.stats.swarm_state = self._state
        # Resolution and frame rate feed Streamflow's ranking tie-break and the
        # stats it syncs to Dispatcharr; without them every OpenStream source
        # read 0x0 at 0 fps. OpenStream's fps is the real frame rate (field-coded
        # interlace counted as frames), matching ffprobe's avg_frame_rate.
        media = snap.get("media") if isinstance(snap.get("media"), dict) else {}
        if _as_int(media.get("width")) > 0 and _as_int(media.get("height")) > 0:
            self.stats.width = _as_int(media.get("width"))
            self.stats.height = _as_int(media.get("height"))
        if _as_float(media.get("fps")) > 0:
            self.stats.fps = _as_float(media.get("fps"))
        if not health and _as_float(media.get("bitrateKbps")) > 0:
            self.stats.bitrate = _as_float(media.get("bitrateKbps"))
        if health:
            self.stats.reliability_score = _as_float(health.get("reliabilityScore"))
            self.stats.keepup_margin = _as_float(health.get("keepUpMargin"))
        # Delay behind live: behindLiveSecs (media seconds per piece, validated
        # against the player-side delay) over health.latencySecs (pieces over
        # the edge's wall rate, whole seconds, 0 while the edge is frozen).
        # They agree within 0.7 s median when steady and diverge by up to 46 s
        # while warming or stalled (2,433 live samples).
        behind = snap.get("behindLiveSecs")
        if isinstance(behind, (int, float)) and behind >= 0:
            self.stats.latency_secs = int(round(behind))
        elif health:
            self.stats.latency_secs = _as_int(health.get("latencySecs"))

    def _apply_warming(self, snap: Optional[dict] = None, now: Optional[float] = None):
        self._clear_auth_error()
        self._dead_since = None
        self._state = "warming"
        self._buffering = True
        self.stats.is_alive = True
        self.stats.speed = 0.0
        self.stats.last_updated = now if now is not None else time.time()
        self._apply_swarm_telemetry(snap or {}, {})

    def _clear_auth_error(self):
        if self._auth_error is not None:
            logger.info("OpenStream at %s accepted the API key again", self.base)
            self._auth_error = None
            self.stats.error_message = None

    def _apply_snapshot(self, snap: dict, now: Optional[float] = None):
        self._clear_auth_error()
        health = snap.get("health") if isinstance(snap.get("health"), dict) else None
        if not health:
            # Session exists but no health yet (just started) -> warming. Peers and
            # download rate are already meaningful, so keep them.
            self._apply_warming(snap, now)
            return
        state = str(health.get("state") or "warming")
        margin = _as_float(health.get("keepUpMargin"))
        true_bitrate = _as_float(health.get("trueBitrateKbps"))
        now = now if now is not None else time.time()
        kbps = _as_float(snap.get("kbps"))
        # Warming snapshots never reach here (no health yet), so the window
        # holds only measured seconds. A frozen edge (true bitrate 0) is a
        # second of nothing delivered.
        self._note_delivery(now, min(1.0, kbps / true_bitrate) if true_bitrate > 0 else 0.0)
        self._state = state
        self._error_density = max(0.0, min(1.0, 1.0 - margin))
        self.stats.last_updated = now if now is not None else time.time()
        self.stats.bitrate = true_bitrate
        self._apply_swarm_telemetry(snap, health)

        if state == "dead":
            if self._dead_since is None:
                self._dead_since = now
            dead_for = now - self._dead_since
            if dead_for >= DEAD_FATAL_SECS:
                self.stats.is_alive = False
                self.stats.is_fatal = True
                self.stats.error_message = f"OpenStream: no live source for {dead_for:.0f}s (dead)"
                self._buffering = False
                return
            # Not yet fatal: report it and let the score (no delivery) rank it down.
            self.stats.is_alive = True
            self.stats.is_fatal = False
            self.stats.error_message = None
            self.stats.speed = 0.0
            self._buffering = True
            return
        self._dead_since = None

        self.stats.is_alive = True
        self.stats.is_fatal = False
        self.stats.error_message = None
        self.stats.speed = margin
        # Buffering when the source is connected but not cleanly keeping up, so the
        # reliability window (is_healthy = is_alive and not is_buffering) credits
        # only genuinely-healthy measurements.
        self._buffering = state in _BUFFERING_STATES or margin < 0.9


def _as_float(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _as_int(v) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0
