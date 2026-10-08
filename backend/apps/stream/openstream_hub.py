"""
One poller per OpenStream server, shared by every monitored source on it.

A thread and a fresh HTTP connection per source measured 47 requests and 47 new
TCP connections per second for 100 sources (10 sessions x 10), leaving ~2,800
sockets in TIME_WAIT. The hub asks the server once per interval for the whole
set (``GET /api/streams?ids=...``) over one keep-alive session and hands each
monitor its snapshot, so the load no longer grows with the number of sources.

It also owns the server-side lifecycle of the sources, through leases
(``POST /api/streams {ids, lease: {owner, ttlSecs}}``, renewed every
``LEASE_RENEW_SECS``; ``POST /api/actions {op: release}`` on stop). A lease,
unlike a pin, never stops a stream someone is watching: releasing it only lets
OpenStream's idle reaper take the session once it is unwatched, and an
operator-pinned stream is never touched. If Streamflow dies, the leases expire
after ``LEASE_TTL_SECS`` and the pulls end on their own.

Servers from before leases answer a lease with ``{"added": n}`` (a plain pin).
For those the hub falls back to the old remove-on-stop, which is what they
supported.
"""

import threading
import time
from collections import defaultdict
from typing import Dict, List, Optional, Set

import requests

from apps.config.openstream_config import openstream_headers
from apps.core.logging_config import setup_logging

logger = setup_logging(__name__)

# OpenStream recomputes health once a second; Streamflow evaluates once a
# second. Two seconds keeps every evaluation within one fresh sample while
# halving the requests.
POLL_INTERVAL = 2.0
HTTP_TIMEOUT = 5.0
# Renew well inside the TTL so a missed renewal or two never drops a pull; the
# TTL bounds how long a crashed Streamflow keeps streams running.
LEASE_TTL_SECS = 300
LEASE_RENEW_SECS = 60

AUTH_STATUSES = (401, 403, 428)

_hubs: Dict[str, "OpenStreamHub"] = {}
_hubs_lock = threading.Lock()


def get_hub(base: str) -> "OpenStreamHub":
    with _hubs_lock:
        hub = _hubs.get(base)
        if hub is None:
            hub = OpenStreamHub(base)
            _hubs[base] = hub
        return hub


def reset_hubs():
    """Stop every hub (tests, shutdown)."""
    with _hubs_lock:
        hubs = list(_hubs.values())
        _hubs.clear()
    for hub in hubs:
        hub.close()


class OpenStreamHub:
    def __init__(self, base: str, poll_interval: float = POLL_INTERVAL):
        self.base = base
        self.poll_interval = poll_interval
        self._http = requests.Session()
        self._lock = threading.Lock()
        self._monitors: Dict[str, Set] = defaultdict(set)  # cid -> monitors
        self._need_lease: Set[tuple] = set()  # (cid, owner) to (re)lease now
        self._leased_at: Dict[tuple, float] = {}  # (cid, owner) -> last lease time
        self._release: Set[tuple] = set()  # (cid, owner) to release
        self._legacy = False  # server predates leases
        self._wake = threading.Event()
        self._closed = False
        self._thread: Optional[threading.Thread] = None

    # ---- registration ----

    def register(self, monitor):
        key = (monitor.content_id, monitor.owner)
        with self._lock:
            self._monitors[monitor.content_id].add(monitor)
            self._release.discard(key)
            if key not in self._leased_at:
                self._need_lease.add(key)
            self._ensure_thread()
        self._wake.set()

    def unregister(self, monitor):
        key = (monitor.content_id, monitor.owner)
        with self._lock:
            mons = self._monitors.get(monitor.content_id)
            if mons is not None:
                mons.discard(monitor)
                if not mons:
                    del self._monitors[monitor.content_id]
            # Release only when no other monitor of the same owner still wants it.
            still = any(m.owner == monitor.owner for m in self._monitors.get(monitor.content_id, ()))
            if not still:
                self._need_lease.discard(key)
                self._leased_at.pop(key, None)
                self._release.add(key)
        self._wake.set()

    def is_running(self) -> bool:
        t = self._thread
        return t is not None and t.is_alive()

    def close(self):
        self._closed = True
        self._wake.set()

    def _ensure_thread(self):
        # Caller holds self._lock. A dead thread (unexpected exception) is
        # replaced on the next registration.
        if self._thread is None or not self._thread.is_alive():
            self._closed = False
            self._thread = threading.Thread(target=self._run, name=f"openstream-hub {self.base}", daemon=True)
            self._thread.start()

    # ---- loop ----

    def _run(self):
        while not self._closed:
            start = time.monotonic()
            try:
                self.cycle()
            except Exception:  # never let one bad cycle kill monitoring
                logger.exception("OpenStream hub cycle failed for %s", self.base)
            self._wake.wait(max(0.0, self.poll_interval - (time.monotonic() - start)))
            self._wake.clear()
            with self._lock:
                if not self._monitors and not self._release:
                    # Nothing left to watch or release: let the thread end; the
                    # next registration starts a new one.
                    self._thread = None
                    return

    def cycle(self):
        """One round: release, lease/renew, poll, deliver. Public for tests."""
        self._send_releases()
        self._send_leases()
        with self._lock:
            cids = sorted(self._monitors)
        if not cids:
            return
        now = time.time()
        try:
            resp = self._http.get(
                f"{self.base}/api/streams",
                params={"ids": ",".join(cids)},
                headers=openstream_headers(),
                timeout=HTTP_TIMEOUT,
            )
        except requests.RequestException as e:
            logger.debug("OpenStream poll failed for %s: %s", self.base, e)
            self._deliver(cids, lambda m: m._apply_unreachable(now))
            return
        if resp.status_code in AUTH_STATUSES:
            self._deliver(cids, lambda m: m._apply_auth_error(resp.status_code, now))
            return
        try:
            body = resp.json()
        except ValueError:
            body = None
        if resp.status_code != 200 or not isinstance(body, list):
            self._deliver(cids, lambda m: m._apply_unreachable(now))
            return
        by_cid = {s.get("contentID"): s for s in body if isinstance(s, dict)}
        missing = [c for c in cids if c not in by_cid]
        if missing:
            # Not on the server (never leased, reaped, or the server restarted):
            # lease again, report warming meanwhile.
            with self._lock:
                for cid in missing:
                    for m in self._monitors.get(cid, ()):
                        self._need_lease.add((cid, m.owner))
        self._deliver(cids, lambda m: m._apply_snapshot(by_cid[m.content_id], now)
                      if m.content_id in by_cid else m._apply_warming(None, now))

    def _deliver(self, cids: List[str], apply):
        with self._lock:
            monitors = [m for c in cids for m in self._monitors.get(c, ())]
        for m in monitors:
            if m.stopped:
                continue
            apply(m)
            m._notify()

    # ---- leases ----

    def _send_leases(self):
        now = time.monotonic()
        with self._lock:
            due = set(self._need_lease)
            for key, at in self._leased_at.items():
                if now - at >= LEASE_RENEW_SECS:
                    due.add(key)
            self._need_lease.clear()
        by_owner: Dict[str, List[str]] = defaultdict(list)
        for cid, owner in due:
            by_owner[owner].append(cid)
        for owner, ids in by_owner.items():
            try:
                resp = self._http.post(
                    f"{self.base}/api/streams",
                    json={"ids": sorted(ids), "lease": {"owner": owner, "ttlSecs": LEASE_TTL_SECS}},
                    headers=openstream_headers(),
                    timeout=HTTP_TIMEOUT,
                )
                ok = resp.status_code == 200
                if ok:
                    try:
                        self._legacy = "leased" not in resp.json()
                    except ValueError:
                        pass
            except requests.RequestException as e:
                logger.debug("OpenStream lease failed for %s: %s", self.base, e)
                ok = False
            with self._lock:
                for cid in ids:
                    key = (cid, owner)
                    if not any(m.owner == owner for m in self._monitors.get(cid, ())):
                        continue  # unregistered meanwhile
                    if ok:
                        self._leased_at[key] = now
                    else:
                        self._need_lease.add(key)  # retry next cycle

    def _send_releases(self):
        with self._lock:
            todo = set(self._release)
            self._release.clear()
        by_owner: Dict[str, List[str]] = defaultdict(list)
        for cid, owner in todo:
            by_owner[owner].append(cid)
        for owner, ids in by_owner.items():
            body = ({"op": "remove", "ids": sorted(ids)} if self._legacy
                    else {"op": "release", "ids": sorted(ids), "owner": owner})
            try:
                self._http.post(f"{self.base}/api/actions", json=body,
                                headers=openstream_headers(), timeout=HTTP_TIMEOUT)
            except requests.RequestException as e:
                # Not retried: an unreleased lease expires on its own.
                logger.debug("OpenStream release failed for %s: %s", self.base, e)
