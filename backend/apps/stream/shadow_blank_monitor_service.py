"""Active-viewer shadow blank monitor.

This service watches only channels that already have real active clients.  It
probes the channel proxy URL, never a provider stream URL, so an already-active
channel should receive only one extra local downstream client during a probe.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import threading
import time
from collections import defaultdict, deque
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional

from apps.config.dispatcharr_config import get_dispatcharr_config
from apps.core.api_utils import change_channel_stream
from apps.core.logging_config import setup_logging
from apps.stream.stream_check_utils import _parse_blank_detection
from apps.udi import get_udi_manager

logger = setup_logging(__name__)

CONFIG_DIR = Path(os.environ.get("CONFIG_DIR", "/app/data"))
CONFIG_FILE = CONFIG_DIR / "shadow_blank_monitor_config.json"
MAX_EVENTS = 100

DEFAULT_CONFIG: Dict[str, Any] = {
    "enabled": False,
    "dry_run": True,
    "watch_mode": "periodic",
    "poll_interval_seconds": 30,
    "watch_gap_seconds": 1,
    "probe_duration_seconds": 8,
    "blank_min_duration_seconds": 2.0,
    "blank_pixel_threshold": 0.10,
    "blank_ratio_threshold": 0.80,
    "confirmation_count": 2,
    "channel_cooldown_seconds": 300,
    "max_switches_per_hour": 3,
    "max_concurrent_watchers": 2,
    "skip_during_quality_check": True,
    "watcher_user_agent": "StreamFlow-Shadow-Blank-Monitor/1.0",
    "watcher_api_key": "",
    "excluded_channel_ids": [],
    "excluded_channel_uuids": [],
}
CONFIG_KEYS = set(DEFAULT_CONFIG)
WATCH_MODES = {"periodic", "continuous"}

INT_BOUNDS = {
    "poll_interval_seconds": (5, 3600),
    "watch_gap_seconds": (1, 300),
    "probe_duration_seconds": (3, 120),
    "confirmation_count": (1, 5),
    "channel_cooldown_seconds": (30, 86400),
    "max_switches_per_hour": (1, 20),
    "max_concurrent_watchers": (1, 10),
}

FLOAT_BOUNDS = {
    "blank_min_duration_seconds": (0.5, 30.0),
    "blank_pixel_threshold": (0.0, 1.0),
    "blank_ratio_threshold": (0.1, 1.0),
}


def _ref(kind: str, value: Any) -> str:
    if value is None:
        return f"{kind}-unknown"
    digest = hashlib.sha256(f"{kind}:{value}".encode("utf-8")).hexdigest()[:12]
    return f"{kind}-{digest}"


def _coerce_int(value: Any, default: int, bounds: tuple[int, int]) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(bounds[0], min(bounds[1], parsed))


def _coerce_float(value: Any, default: float, bounds: tuple[float, float]) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = default
    return max(bounds[0], min(bounds[1], parsed))


def _coerce_list(value: Any) -> List[Any]:
    if isinstance(value, list):
        return value
    if value in (None, ""):
        return []
    return [value]


def normalize_config(payload: Optional[Dict[str, Any]], current: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    config = dict(DEFAULT_CONFIG)
    if current:
        config.update({key: value for key, value in current.items() if key in CONFIG_KEYS})
    if payload:
        config.update({key: value for key, value in payload.items() if key in CONFIG_KEYS})

    for key, bounds in INT_BOUNDS.items():
        config[key] = _coerce_int(config.get(key), DEFAULT_CONFIG[key], bounds)
    for key, bounds in FLOAT_BOUNDS.items():
        config[key] = _coerce_float(config.get(key), DEFAULT_CONFIG[key], bounds)

    config["enabled"] = bool(config.get("enabled"))
    config["dry_run"] = bool(config.get("dry_run"))
    config["watch_mode"] = str(config.get("watch_mode") or DEFAULT_CONFIG["watch_mode"]).strip().lower()
    if config["watch_mode"] not in WATCH_MODES:
        config["watch_mode"] = DEFAULT_CONFIG["watch_mode"]
    config["skip_during_quality_check"] = bool(config.get("skip_during_quality_check"))
    config["watcher_user_agent"] = str(config.get("watcher_user_agent") or DEFAULT_CONFIG["watcher_user_agent"]).strip()
    config["watcher_api_key"] = str(config.get("watcher_api_key") or "").strip()
    config["excluded_channel_ids"] = [
        int(item)
        for item in _coerce_list(config.get("excluded_channel_ids"))
        if str(item).strip().isdigit()
    ]
    config["excluded_channel_uuids"] = [
        str(item).strip()
        for item in _coerce_list(config.get("excluded_channel_uuids"))
        if str(item).strip()
    ]
    return config


def public_config(config: Dict[str, Any]) -> Dict[str, Any]:
    visible = dict(config)
    visible["has_watcher_api_key"] = bool(visible.get("watcher_api_key"))
    visible["watcher_api_key"] = ""
    return visible


class ShadowBlankMonitorService:
    def __init__(
        self,
        *,
        config_file: Path = CONFIG_FILE,
        udi_provider: Callable[[], Any] = get_udi_manager,
        switch_stream: Callable[..., bool] = change_channel_stream,
        base_url_provider: Optional[Callable[[], Optional[str]]] = None,
        blank_probe: Optional[Callable[[str, Dict[str, Any]], Dict[str, Any]]] = None,
        stream_checker_provider: Optional[Callable[[], Any]] = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.config_file = config_file
        self.udi_provider = udi_provider
        self.switch_stream = switch_stream
        self.base_url_provider = base_url_provider or (lambda: get_dispatcharr_config().get_base_url())
        self.blank_probe = blank_probe or self._run_blank_probe
        self.stream_checker_provider = stream_checker_provider or self._default_stream_checker_provider
        self.clock = clock

        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._config = self._load_config()
        self._events: deque[Dict[str, Any]] = deque(maxlen=MAX_EVENTS)
        self._watched: Dict[str, Dict[str, Any]] = {}
        self._blank_counts: Dict[str, int] = defaultdict(int)
        self._cooldowns: Dict[str, float] = {}
        self._switch_history: Dict[str, deque[float]] = defaultdict(deque)
        self._active_probes: set[str] = set()
        self._last_scan_at: Optional[float] = None
        self._last_error: Optional[str] = None

    def _load_config(self) -> Dict[str, Any]:
        try:
            if self.config_file.exists():
                with open(self.config_file, "r", encoding="utf-8") as handle:
                    return normalize_config(json.load(handle))
        except Exception as exc:
            logger.warning(f"Failed to load shadow blank monitor config: {exc}")
        return normalize_config({})

    def _save_config(self) -> None:
        self.config_file.parent.mkdir(parents=True, exist_ok=True)
        with open(self.config_file, "w", encoding="utf-8") as handle:
            json.dump(self._config, handle, indent=2, sort_keys=True)
            handle.write("\n")

    def get_config(self, *, include_secret: bool = False) -> Dict[str, Any]:
        with self._lock:
            return dict(self._config) if include_secret else public_config(self._config)

    def update_config(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock:
            payload = dict(payload or {})
            current = dict(self._config)
            if payload.get("clear_watcher_api_key"):
                current["watcher_api_key"] = ""
                payload.pop("watcher_api_key", None)
            elif payload.get("watcher_api_key", "") == "":
                payload["watcher_api_key"] = current.get("watcher_api_key", "")

            self._config = normalize_config(payload, current)
            self._save_config()
            enabled = self._config["enabled"]

        if enabled:
            self.start(persist=False)
        else:
            self.stop(persist=False)
        return self.get_config()

    def start(self, *, persist: bool = True) -> bool:
        with self._lock:
            if persist:
                self._config["enabled"] = True
                self._save_config()
            if self._thread and self._thread.is_alive():
                return True
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._worker,
                name="ShadowBlankMonitor",
                daemon=True,
            )
            self._thread.start()
            return True

    def stop(self, *, persist: bool = True) -> bool:
        with self._lock:
            if persist:
                self._config["enabled"] = False
                self._save_config()
            self._stop_event.set()
            self._watched = {}
            thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=5)
        return True

    def get_status(self) -> Dict[str, Any]:
        with self._lock:
            now = self.clock()
            watched_channels = [self._public_target(target) for target in self._watched.values()]
            cooldowns = []
            for channel_uuid, until in self._cooldowns.items():
                if until <= now:
                    continue
                target = self._watched.get(channel_uuid, {"channel_ref": _ref("channel", channel_uuid)})
                cooldowns.append({
                    "channel_ref": target.get("channel_ref"),
                    "cooldown_seconds": max(0, int(until - now)),
                })
            return {
                "enabled": bool(self._config.get("enabled")),
                "running": bool(self._thread and self._thread.is_alive()),
                "dry_run": bool(self._config.get("dry_run")),
                "last_scan_at": self._last_scan_at,
                "last_error": self._last_error,
                "watched_count": len(watched_channels),
                "watched_channels": watched_channels,
                "cooldowns": cooldowns,
                "recent_events": list(self._events),
            }

    def _worker(self) -> None:
        logger.info("Shadow blank monitor started")
        while not self._stop_event.is_set():
            try:
                with self._lock:
                    enabled = bool(self._config.get("enabled"))
                    config = dict(self._config)
                    interval = self._next_scan_delay(config)
                if enabled:
                    self.run_once()
                self._stop_event.wait(interval)
            except Exception as exc:
                self._last_error = str(exc)
                logger.error(f"Shadow blank monitor loop failed: {exc}", exc_info=True)
                self._stop_event.wait(30)
        logger.info("Shadow blank monitor stopped")

    @staticmethod
    def _next_scan_delay(config: Dict[str, Any]) -> int:
        if config.get("watch_mode") == "continuous":
            return int(config.get("watch_gap_seconds") or DEFAULT_CONFIG["watch_gap_seconds"])
        return int(config.get("poll_interval_seconds") or DEFAULT_CONFIG["poll_interval_seconds"])

    def run_once(self, *, force: bool = False) -> Dict[str, Any]:
        with self._lock:
            config = dict(self._config)
        if not config.get("enabled") and not force:
            return self.get_status()

        self._last_scan_at = self.clock()
        try:
            udi = self.udi_provider()
            targets = self.discover_active_targets(udi, config)
            self._probe_targets(udi, targets[: config["max_concurrent_watchers"]], config)
            self._last_error = None
        except Exception as exc:
            self._last_error = str(exc)
            logger.error(f"Shadow blank monitor scan failed: {exc}", exc_info=True)
        return self.get_status()

    def discover_active_targets(self, udi: Any, config: Dict[str, Any]) -> List[Dict[str, Any]]:
        proxy_status = udi.get_proxy_status() or {}
        channels = udi.get_channels() if hasattr(udi, "get_channels") else []
        by_uuid, by_id = self._index_channels(channels)
        excluded_ids = {int(item) for item in config.get("excluded_channel_ids", [])}
        excluded_uuids = {str(item) for item in config.get("excluded_channel_uuids", [])}

        targets: List[Dict[str, Any]] = []
        watched: Dict[str, Dict[str, Any]] = {}

        for key, raw_status in proxy_status.items():
            if not self._is_status_active(raw_status):
                continue

            channel_uuid = str(raw_status.get("channel_id") or raw_status.get("channel_uuid") or key)
            channel = by_uuid.get(channel_uuid)
            if channel is None and str(key).isdigit():
                channel = by_id.get(int(key))
                channel_uuid = str(channel.get("uuid") or key) if channel else channel_uuid
            if not channel and channel_uuid in by_uuid:
                channel = by_uuid[channel_uuid]

            numeric_id = self._extract_channel_id(channel, raw_status)
            if numeric_id in excluded_ids or channel_uuid in excluded_uuids:
                continue

            real_clients = self._real_client_count(raw_status, config)
            if real_clients <= 0:
                continue

            stream_id = self._extract_stream_id(raw_status)
            target = {
                "channel_uuid": channel_uuid,
                "channel_id": numeric_id,
                "channel_ref": _ref("channel", numeric_id or channel_uuid),
                "stream_id": stream_id,
                "stream_ref": _ref("stream", stream_id),
                "real_client_count": real_clients,
                "state": raw_status.get("state") or "active",
                "cooldown_seconds": self._cooldown_remaining(channel_uuid),
            }
            targets.append(target)
            watched[channel_uuid] = dict(target)

        with self._lock:
            self._watched = watched
        return targets

    def _probe_targets(self, udi: Any, targets: Iterable[Dict[str, Any]], config: Dict[str, Any]) -> None:
        threads: List[threading.Thread] = []
        for target in targets:
            channel_uuid = target["channel_uuid"]
            if self._cooldown_remaining(channel_uuid) > 0:
                self._record_event("cooldown", target, {"cooldown_seconds": self._cooldown_remaining(channel_uuid)})
                continue
            if self._quality_checker_conflicts(target, config):
                self._record_event("quality_check_active", target, {})
                continue

            with self._lock:
                if channel_uuid in self._active_probes:
                    continue
                self._active_probes.add(channel_uuid)

            thread = threading.Thread(
                target=self._probe_target,
                args=(udi, target, dict(config)),
                name=f"ShadowBlankProbe-{channel_uuid[:8]}",
                daemon=True,
            )
            thread.start()
            threads.append(thread)

        for thread in threads:
            thread.join()

    def _probe_target(self, udi: Any, target: Dict[str, Any], config: Dict[str, Any]) -> None:
        channel_uuid = target["channel_uuid"]
        try:
            proxy_url = self._channel_proxy_url(channel_uuid)
            result = self.blank_probe(proxy_url, config)
            blank = bool(result.get("blank_detected"))
            target["last_probe"] = {
                "blank_detected": blank,
                "blank_ratio": result.get("blank_ratio"),
                "blank_duration_secs": result.get("blank_duration_secs"),
            }

            fresh_status = self._find_status_for_target(udi.get_proxy_status() or {}, target)
            if self._real_client_count(fresh_status, config) <= 0:
                self._reset_blank_count(channel_uuid)
                self._record_event("viewer_left", target, {})
                with self._lock:
                    self._watched.pop(channel_uuid, None)
                return

            if not blank:
                self._reset_blank_count(channel_uuid)
                self._record_event("probe_ok", target, target["last_probe"])
                return

            blank_count = self._increment_blank_count(channel_uuid)
            confirmations = int(config.get("confirmation_count", 2))
            if blank_count < confirmations:
                self._record_event(
                    "blank_pending",
                    target,
                    {"confirmations": blank_count, "required": confirmations},
                )
                return

            self._handle_confirmed_blank(udi, target, config)
        finally:
            with self._lock:
                self._active_probes.discard(channel_uuid)

    def _handle_confirmed_blank(self, udi: Any, target: Dict[str, Any], config: Dict[str, Any]) -> None:
        channel_uuid = target["channel_uuid"]
        fresh_status = self._find_status_for_target(udi.get_proxy_status() or {}, target)
        if self._real_client_count(fresh_status, config) <= 0:
            self._reset_blank_count(channel_uuid)
            self._record_event("viewer_left", target, {})
            return

        fresh_stream_id = self._extract_stream_id(fresh_status) or target.get("stream_id")
        if target.get("stream_id") and fresh_stream_id != target.get("stream_id"):
            self._reset_blank_count(channel_uuid)
            self._record_event(
                "stale_stream_guard",
                target,
                {"current_stream_ref": _ref("stream", fresh_stream_id)},
            )
            return

        if not self._switch_allowed(channel_uuid, config):
            self._record_event("switch_rate_limited", target, {})
            return

        alternative = self._choose_alternative_stream(udi, target.get("channel_id"), fresh_stream_id)
        if not alternative:
            self._set_cooldown(channel_uuid, config)
            self._reset_blank_count(channel_uuid)
            self._record_event("no_alternative", target, {})
            return

        if config.get("dry_run"):
            self._set_cooldown(channel_uuid, config)
            self._record_event(
                "dry_run_switch",
                target,
                {"target_stream_ref": _ref("stream", alternative)},
            )
            return

        success = bool(self.switch_stream(channel_uuid, stream_id=alternative))
        self._set_cooldown(channel_uuid, config)
        self._reset_blank_count(channel_uuid)
        if success:
            with self._lock:
                self._switch_history[channel_uuid].append(self.clock())
        self._record_event(
            "switch_success" if success else "switch_failed",
            target,
            {"target_stream_ref": _ref("stream", alternative)},
        )

    def _choose_alternative_stream(self, udi: Any, channel_id: Optional[int], current_stream_id: Optional[int]) -> Optional[int]:
        if channel_id is None:
            return None
        channel = udi.get_channel_by_id(channel_id) if hasattr(udi, "get_channel_by_id") else None
        stream_ids = list((channel or {}).get("streams") or [])
        if not stream_ids and hasattr(udi, "get_channel_streams"):
            stream_ids = [
                stream.get("id")
                for stream in (udi.get_channel_streams(channel_id) or [])
                if isinstance(stream, dict) and stream.get("id") is not None
            ]
        stream_ids = [int(item) for item in stream_ids if str(item).isdigit()]
        if len(stream_ids) <= 1:
            return None
        if current_stream_id in stream_ids:
            index = stream_ids.index(current_stream_id)
            ordered = stream_ids[index + 1 :] + stream_ids[:index]
        else:
            ordered = stream_ids
        return next((sid for sid in ordered if sid != current_stream_id), None)

    def _reset_blank_count(self, channel_uuid: str) -> None:
        with self._lock:
            self._blank_counts[channel_uuid] = 0

    def _increment_blank_count(self, channel_uuid: str) -> int:
        with self._lock:
            self._blank_counts[channel_uuid] += 1
            return self._blank_counts[channel_uuid]

    def _channel_proxy_url(self, channel_uuid: str) -> str:
        base_url = (self.base_url_provider() or "").rstrip("/")
        if not base_url:
            raise RuntimeError("Dispatcharr base URL is not configured")
        return f"{base_url}/proxy/ts/stream/{channel_uuid}"

    def _run_blank_probe(self, url: str, config: Dict[str, Any]) -> Dict[str, Any]:
        duration = int(config["probe_duration_seconds"])
        headers = ""
        api_key = config.get("watcher_api_key")
        if api_key:
            headers = f"X-API-Key: {api_key}\r\nAuthorization: ApiKey {api_key}\r\n"

        command = [
            "ffmpeg",
            "-hide_banner",
            "-nostdin",
            "-loglevel",
            "info",
            "-user_agent",
            config.get("watcher_user_agent") or DEFAULT_CONFIG["watcher_user_agent"],
        ]
        if headers:
            command.extend(["-headers", headers])
        command.extend(
            [
                "-i",
                url,
                "-t",
                str(duration),
                "-vf",
                (
                    f"blackdetect=d={float(config['blank_min_duration_seconds'])}:"
                    f"pix_th={float(config['blank_pixel_threshold'])}"
                ),
                "-an",
                "-f",
                "null",
                "-",
            ]
        )

        try:
            completed = subprocess.run(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=duration + 15,
                text=True,
            )
            output = completed.stderr or ""
            parsed = _parse_blank_detection(
                output,
                duration,
                blank_ratio_threshold=float(config["blank_ratio_threshold"]),
            )
            parsed["returncode"] = completed.returncode
            return parsed
        except subprocess.TimeoutExpired as exc:
            output = exc.stderr if isinstance(exc.stderr, str) else ""
            parsed = _parse_blank_detection(
                output,
                duration,
                blank_ratio_threshold=float(config["blank_ratio_threshold"]),
            )
            parsed["timeout"] = True
            return parsed

    @staticmethod
    def _index_channels(channels: Iterable[Dict[str, Any]]) -> tuple[Dict[str, Dict[str, Any]], Dict[int, Dict[str, Any]]]:
        by_uuid: Dict[str, Dict[str, Any]] = {}
        by_id: Dict[int, Dict[str, Any]] = {}
        for channel in channels or []:
            if not isinstance(channel, dict):
                continue
            if channel.get("uuid"):
                by_uuid[str(channel["uuid"])] = channel
            if channel.get("id") is not None:
                try:
                    by_id[int(channel["id"])] = channel
                except (TypeError, ValueError):
                    pass
        return by_uuid, by_id

    @staticmethod
    def _is_status_active(status: Dict[str, Any]) -> bool:
        if not isinstance(status, dict):
            return False
        return bool(
            status.get("state") == "active"
            or status.get("active")
            or status.get("current_stream")
            or status.get("stream_id")
            or status.get("clients")
        )

    @staticmethod
    def _extract_stream_id(status: Dict[str, Any]) -> Optional[int]:
        value = status.get("stream_id") or status.get("current_stream_id")
        current = status.get("current_stream")
        if value is None and isinstance(current, dict):
            value = current.get("id") or current.get("stream_id")
        elif value is None:
            value = current
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _extract_channel_id(channel: Optional[Dict[str, Any]], status: Dict[str, Any]) -> Optional[int]:
        value = (channel or {}).get("id") or status.get("numeric_channel_id") or status.get("id")
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _client_text(client: Any) -> str:
        if isinstance(client, dict):
            values = [
                client.get("user_agent"),
                client.get("client_id"),
                client.get("username"),
                client.get("user"),
                client.get("ip"),
            ]
            return " ".join(str(value) for value in values if value is not None)
        return str(client)

    def _real_client_count(self, status: Dict[str, Any], config: Dict[str, Any]) -> int:
        marker = str(config.get("watcher_user_agent") or "").lower()
        clients = status.get("clients")
        if isinstance(clients, dict):
            clients = list(clients.values())
        if isinstance(clients, list):
            real = 0
            for client in clients:
                text = self._client_text(client).lower()
                if marker and marker in text:
                    continue
                real += 1
            return real

        for key in ("real_client_count", "client_count", "current_viewers", "viewer_count"):
            try:
                count = int(status.get(key))
                if count > 0:
                    return count
            except (TypeError, ValueError):
                continue
        return 1 if self._is_status_active(status) else 0

    def _cooldown_remaining(self, channel_uuid: str) -> int:
        with self._lock:
            return max(0, int(self._cooldowns.get(channel_uuid, 0) - self.clock()))

    def _set_cooldown(self, channel_uuid: str, config: Dict[str, Any]) -> None:
        with self._lock:
            self._cooldowns[channel_uuid] = self.clock() + int(config["channel_cooldown_seconds"])

    def _switch_allowed(self, channel_uuid: str, config: Dict[str, Any]) -> bool:
        now = self.clock()
        with self._lock:
            history = self._switch_history[channel_uuid]
            while history and now - history[0] > 3600:
                history.popleft()
            return len(history) < int(config["max_switches_per_hour"])

    def _record_event(self, event_type: str, target: Dict[str, Any], details: Dict[str, Any]) -> None:
        event = {
            "timestamp": self.clock(),
            "type": event_type,
            "channel_ref": target.get("channel_ref"),
            "stream_ref": target.get("stream_ref"),
            "real_client_count": target.get("real_client_count"),
            "details": details,
        }
        with self._lock:
            self._events.appendleft(event)
            watched = self._watched.get(target.get("channel_uuid"))
            if watched is not None:
                watched["last_event"] = event
                if target.get("last_probe"):
                    watched["last_probe"] = target["last_probe"]
        logger.info(
            f"Shadow blank monitor event={event_type} "
            f"channel_ref={event['channel_ref']} stream_ref={event['stream_ref']}"
        )

    @staticmethod
    def _default_stream_checker_provider() -> Any:
        from apps.stream.stream_checker_service import get_stream_checker_service

        return get_stream_checker_service()

    def _quality_checker_conflicts(self, target: Dict[str, Any], config: Dict[str, Any]) -> bool:
        if not config.get("skip_during_quality_check", True):
            return False

        channel_id = target.get("channel_id")
        if channel_id is None:
            return False

        try:
            checker = self.stream_checker_provider()
            status = checker.get_status() if checker else {}
        except Exception as exc:
            logger.warning(f"Shadow blank monitor could not read quality checker status: {exc}")
            return True

        queue_status = status.get("queue") or {}
        progress = status.get("progress") or {}
        current_channel = queue_status.get("current_channel") or progress.get("channel_id")

        try:
            if current_channel is not None and int(current_channel) == int(channel_id):
                return True
        except (TypeError, ValueError):
            pass

        if status.get("stream_checking_mode") and queue_status.get("in_progress", 0) and current_channel is None:
            return True

        return False

    @staticmethod
    def _public_target(target: Dict[str, Any]) -> Dict[str, Any]:
        allowed = {
            "channel_ref",
            "stream_ref",
            "real_client_count",
            "state",
            "cooldown_seconds",
            "last_probe",
            "last_event",
        }
        return {key: value for key, value in target.items() if key in allowed}

    @staticmethod
    def _find_status_for_target(proxy_status: Dict[str, Any], target: Dict[str, Any]) -> Dict[str, Any]:
        channel_uuid = str(target.get("channel_uuid") or "")
        if channel_uuid and channel_uuid in proxy_status:
            return proxy_status[channel_uuid] or {}

        channel_id = target.get("channel_id")
        channel_id_key = str(channel_id) if channel_id is not None else ""
        if channel_id_key and channel_id_key in proxy_status:
            return proxy_status[channel_id_key] or {}

        for status in proxy_status.values():
            if not isinstance(status, dict):
                continue
            status_uuid = str(status.get("channel_id") or status.get("channel_uuid") or "")
            if channel_uuid and status_uuid == channel_uuid:
                return status
            try:
                status_id = int(status.get("numeric_channel_id") or status.get("id"))
            except (TypeError, ValueError):
                continue
            if channel_id is not None and status_id == int(channel_id):
                return status

        return {}


_shadow_monitor_instance: Optional[ShadowBlankMonitorService] = None
_shadow_monitor_lock = threading.Lock()


def get_shadow_blank_monitor_service() -> ShadowBlankMonitorService:
    global _shadow_monitor_instance
    with _shadow_monitor_lock:
        if _shadow_monitor_instance is None:
            _shadow_monitor_instance = ShadowBlankMonitorService()
        return _shadow_monitor_instance
