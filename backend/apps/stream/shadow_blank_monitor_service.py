"""Active-viewer shadow blank monitor.

This service watches only channels that already have real active clients.  It
probes the channel proxy URL, never a provider stream URL, so an already-active
channel should receive only one extra local downstream client during a probe.
"""

from __future__ import annotations

import hashlib
import json
import os
import queue
import subprocess
import threading
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional

from apps.config.dispatcharr_config import get_dispatcharr_config
from apps.core.api_utils import change_channel_stream
from apps.core.logging_config import setup_logging
from apps.stream.stream_check_utils import (
    BLACK_DURATION_RE,
    BLACK_END_RE,
    BLACK_START_RE,
    FREEZE_DURATION_RE,
    FREEZE_END_RE,
    FREEZE_START_RE,
    _parse_blank_detection,
    _parse_ffmpeg_progress_time,
    _parse_freeze_detection,
)
from apps.udi import get_udi_manager

logger = setup_logging(__name__)

CONFIG_DIR = Path(os.environ.get("CONFIG_DIR", "/app/data"))
CONFIG_FILE = CONFIG_DIR / "shadow_blank_monitor_config.json"
MAX_EVENTS = 100
WATCHER_API_KEY_REQUIRED_CODE = "watcher_api_key_required"
WATCHER_API_KEY_REQUIRED_MESSAGE = "Watcher API Key is required before Shadow Monitor can start."
SHADOW_MONITOR_LOOP_ERROR_MESSAGE = "Shadow monitor loop failed; see server logs."
SHADOW_MONITOR_SCAN_ERROR_MESSAGE = "Shadow monitor scan failed; see server logs."

DEFAULT_CONFIG: Dict[str, Any] = {
    "enabled": False,
    "dry_run": False,
    "watch_mode": "continuous",
    "poll_interval_seconds": 5,
    "watch_gap_seconds": 1,
    "probe_duration_seconds": 60,
    "blank_min_duration_seconds": 2.0,
    "blank_pixel_threshold": 0.10,
    "blank_ratio_threshold": 0.80,
    "freeze_detection_enabled": True,
    "freeze_min_duration_seconds": 5.0,
    "freeze_noise_threshold": 0.001,
    "freeze_ratio_threshold": 0.80,
    "confirmation_count": 2,
    "channel_cooldown_seconds": 300,
    "max_switches_per_hour": 3,
    "max_concurrent_watchers": 2,
    "skip_during_quality_check": False,
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
    "freeze_min_duration_seconds": (1.0, 120.0),
    "freeze_noise_threshold": (0.0, 1.0),
    "freeze_ratio_threshold": (0.1, 1.0),
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
    config["freeze_detection_enabled"] = bool(config.get("freeze_detection_enabled"))
    config["watch_mode"] = str(config.get("watch_mode") or DEFAULT_CONFIG["watch_mode"]).strip().lower()
    if config["watch_mode"] not in WATCH_MODES:
        config["watch_mode"] = DEFAULT_CONFIG["watch_mode"]
    # The shadow monitor protects active viewers through Dispatcharr's local
    # channel proxy. It should not pause just because quality checks are active.
    config["skip_during_quality_check"] = False
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


def _watcher_configuration_issue(config: Dict[str, Any]) -> Optional[Dict[str, str]]:
    if not str(config.get("watcher_api_key") or "").strip():
        return {
            "code": WATCHER_API_KEY_REQUIRED_CODE,
            "message": WATCHER_API_KEY_REQUIRED_MESSAGE,
        }
    return None


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
        self._uses_default_blank_probe = blank_probe is None
        self.stream_checker_provider = stream_checker_provider or self._default_stream_checker_provider
        self.clock = clock

        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._config = self._load_config()
        self._events: deque[Dict[str, Any]] = deque(maxlen=MAX_EVENTS)
        self._watched: Dict[str, Dict[str, Any]] = {}
        self._watcher_absences: Dict[str, Dict[str, Any]] = {}
        self._blank_counts: Dict[str, int] = defaultdict(int)
        self._cooldowns: Dict[str, float] = {}
        self._switch_attempts: Dict[str, Dict[str, Any]] = {}
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
            issue = _watcher_configuration_issue(self._config)
            if issue and self._config["enabled"]:
                self._config["enabled"] = False
                self._last_error = issue["message"]
            elif not issue and self._last_error == WATCHER_API_KEY_REQUIRED_MESSAGE:
                self._last_error = None
            self._save_config()
            enabled = self._config["enabled"]

        if enabled:
            self.start(persist=False)
        else:
            self.stop(persist=False)
        return self.get_config()

    def start(self, *, persist: bool = True) -> bool:
        with self._lock:
            issue = _watcher_configuration_issue(self._config)
            if issue:
                if persist:
                    self._config["enabled"] = False
                    self._save_config()
                self._last_error = issue["message"]
                return False
            if persist:
                self._config["enabled"] = True
                self._save_config()
            if self._last_error == WATCHER_API_KEY_REQUIRED_MESSAGE:
                self._last_error = None
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
            self._watcher_absences = {}
            thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=5)
        return True

    def get_status(self) -> Dict[str, Any]:
        with self._lock:
            now = self.clock()
            issue = _watcher_configuration_issue(self._config)
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
                "configuration_required": bool(issue),
                "configuration_issue": issue["code"] if issue else None,
                "configuration_message": issue["message"] if issue else None,
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
                self._last_error = SHADOW_MONITOR_LOOP_ERROR_MESSAGE
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
        issue = _watcher_configuration_issue(config)
        if issue:
            with self._lock:
                self._last_error = issue["message"]
            return self.get_status()
        if not config.get("enabled") and not force:
            return self.get_status()

        self._last_scan_at = self.clock()
        try:
            udi = self.udi_provider()
            targets = self.discover_active_targets(udi, config)
            self._probe_targets(udi, targets[: config["max_concurrent_watchers"]], config)
            self._last_error = None
        except Exception as exc:
            self._last_error = SHADOW_MONITOR_SCAN_ERROR_MESSAGE
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
        continuity_events: List[tuple[str, Dict[str, Any], Dict[str, Any]]] = []
        continuous_mode = config.get("watch_mode") == "continuous"
        now = self.clock()
        with self._lock:
            previous_watched = {
                channel_uuid: dict(target)
                for channel_uuid, target in self._watched.items()
            }
            previous_absences = {
                channel_uuid: dict(absence)
                for channel_uuid, absence in self._watcher_absences.items()
            }
        next_absences: Dict[str, Dict[str, Any]] = {}

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
            watcher_details = self._watcher_client_details(raw_status, config)
            watcher_clients = int(watcher_details.get("watcher_client_count") or 0)

            stream_id = self._extract_stream_id(raw_status)
            current_program = self._current_epg_program(channel, raw_status, numeric_id)
            target = {
                "channel_uuid": channel_uuid,
                "channel_id": numeric_id,
                "channel_ref": _ref("channel", numeric_id or channel_uuid),
                "stream_id": stream_id,
                "stream_ref": _ref("stream", stream_id),
                "real_client_count": real_clients,
                "watcher_client_count": watcher_clients,
                "state": raw_status.get("state") or "active",
                "cooldown_seconds": self._cooldown_remaining(channel_uuid),
            }
            if current_program:
                target["current_program"] = current_program
            target.update(watcher_details)
            if continuous_mode:
                previous_target = previous_watched.get(channel_uuid) or {}
                previous_absence = previous_absences.get(channel_uuid)
                previous_watcher_count = int(previous_target.get("watcher_client_count") or 0)
                previous_watcher_ref = previous_target.get("watcher_client_ref")
                watcher_ref = target.get("watcher_client_ref")
                if watcher_clients > 0:
                    target["watcher_state"] = "watching"
                    if previous_absence:
                        since = float(previous_absence.get("since") or now)
                        recovered_after = max(0, int(now - since))
                        target["watcher_recovered_after_seconds"] = recovered_after
                        continuity_events.append((
                            "watcher_recovered",
                            dict(target),
                            {
                                "downtime_seconds": recovered_after,
                                "last_watcher_client_ref": previous_absence.get("last_watcher_client_ref"),
                                "watcher_client_ref": target.get("watcher_client_ref"),
                            },
                        ))
                    elif (
                        previous_watcher_count > 0
                        and previous_watcher_ref
                        and watcher_ref
                        and watcher_ref != previous_watcher_ref
                    ):
                        target["watcher_recovered_after_seconds"] = 0
                        continuity_events.append((
                            "watcher_recovered",
                            dict(target),
                            {
                                "downtime_seconds": 0,
                                "last_watcher_client_ref": previous_watcher_ref,
                                "watcher_client_ref": watcher_ref,
                            },
                        ))
                elif previous_watcher_count > 0 or previous_absence:
                    absence = previous_absence or {
                        "since": now,
                        "last_watcher_client_ref": previous_target.get("watcher_client_ref"),
                    }
                    since = float(absence.get("since") or now)
                    target["watcher_state"] = "reconnecting"
                    target["watcher_absent_since"] = since
                    target["watcher_absent_seconds"] = max(0, int(now - since))
                    if absence.get("last_watcher_client_ref"):
                        target["last_watcher_client_ref"] = absence.get("last_watcher_client_ref")
                    next_absences[channel_uuid] = dict(absence)
                    if not previous_absence:
                        continuity_events.append((
                            "watcher_reconnecting",
                            dict(target),
                            {
                                "last_watcher_client_ref": absence.get("last_watcher_client_ref"),
                                "watcher_absent_seconds": target["watcher_absent_seconds"],
                            },
                        ))
                else:
                    target["watcher_state"] = "waiting"
            targets.append(target)
            watched[channel_uuid] = dict(target)

        with self._lock:
            self._watched = watched
            self._watcher_absences = next_absences if continuous_mode else {}
        for event_type, event_target, details in continuity_events:
            self._record_event(event_type, event_target, details)
        return targets

    def _probe_targets(self, udi: Any, targets: Iterable[Dict[str, Any]], config: Dict[str, Any]) -> None:
        targets = list(targets)
        threads: List[threading.Thread] = []
        wait_for_probes = not (
            config.get("watch_mode") == "continuous" and self._uses_default_blank_probe
        )
        for target in targets:
            channel_uuid = target["channel_uuid"]
            if self._cooldown_remaining(channel_uuid) > 0:
                self._record_event("cooldown", target, {"cooldown_seconds": self._cooldown_remaining(channel_uuid)})
                continue
            if self._quality_checker_conflicts(target, config):
                self._record_event("quality_check_active", target, {})
                continue
            if int(target.get("watcher_client_count") or 0) > 0:
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

        if wait_for_probes:
            for thread in threads:
                thread.join()

    def _probe_target(self, udi: Any, target: Dict[str, Any], config: Dict[str, Any]) -> None:
        channel_uuid = target["channel_uuid"]
        try:
            first_probe = True
            while first_probe or not self._stop_event.is_set():
                first_probe = False
                should_continue = self._probe_target_once(udi, target, config)
                if not (
                    should_continue
                    and config.get("watch_mode") == "continuous"
                    and self._uses_default_blank_probe
                ):
                    break
                if self._stop_event.is_set():
                    break

                fresh_status = self._find_status_for_target(udi.get_proxy_status() or {}, target)
                if self._real_client_count(fresh_status, config) <= 0:
                    self._reset_blank_count(channel_uuid)
                    self._clear_switch_attempts(channel_uuid)
                    self._record_event("viewer_left", target, {})
                    with self._lock:
                        self._watched.pop(channel_uuid, None)
                    break

                target = dict(target)
                target["real_client_count"] = self._real_client_count(fresh_status, config)
                target.update(self._watcher_client_details(fresh_status, config))
                watcher_count = int(target.get("watcher_client_count") or 0)
                target["watcher_state"] = "watching" if watcher_count > 0 else "reconnecting"
                stream_id = self._extract_stream_id(fresh_status) or target.get("stream_id")
                target["stream_id"] = stream_id
                target["stream_ref"] = _ref("stream", stream_id)
                with self._lock:
                    if watcher_count > 0:
                        self._watcher_absences.pop(channel_uuid, None)
                    self._watched[channel_uuid] = dict(target)
        finally:
            with self._lock:
                self._active_probes.discard(channel_uuid)

    def _probe_target_once(self, udi: Any, target: Dict[str, Any], config: Dict[str, Any]) -> bool:
        channel_uuid = target["channel_uuid"]
        try:
            proxy_url = self._channel_proxy_url(channel_uuid)
            if config.get("watch_mode") == "continuous" and self._uses_default_blank_probe:
                result = self._run_blank_probe_until_viewer_left(proxy_url, config, udi, target)
            else:
                result = self.blank_probe(proxy_url, config)
            blank = bool(result.get("blank_detected"))
            freeze = bool(result.get("freeze_detected"))
            detection_reason = "blank" if blank else ("freeze" if freeze else "")
            target["last_probe"] = {
                "blank_detected": blank,
                "blank_ratio": result.get("blank_ratio"),
                "blank_duration_secs": result.get("blank_duration_secs"),
                "freeze_detected": freeze,
                "freeze_ratio": result.get("freeze_ratio"),
                "freeze_duration_secs": result.get("freeze_duration_secs"),
            }

            if result.get("viewer_left"):
                fresh_status = {}
            else:
                fresh_status = self._find_status_for_target(udi.get_proxy_status() or {}, target)

            if result.get("viewer_left") or self._real_client_count(fresh_status, config) <= 0:
                self._reset_blank_count(channel_uuid)
                self._clear_switch_attempts(channel_uuid)
                self._record_event("viewer_left", target, {})
                with self._lock:
                    self._watched.pop(channel_uuid, None)
                return False

            if not detection_reason:
                self._reset_blank_count(channel_uuid)
                self._clear_switch_attempts(channel_uuid)
                self._record_event("probe_ok", target, target["last_probe"])
                return True

            blank_count = self._increment_blank_count(channel_uuid, detection_reason)
            confirmations = int(config.get("confirmation_count", 2))
            if blank_count < confirmations:
                self._record_event(
                    f"{detection_reason}_pending",
                    target,
                    {"confirmations": blank_count, "required": confirmations, "reason": detection_reason},
                )
                return True

            self._handle_confirmed_blank(udi, target, config, reason=detection_reason)
            return False
        except Exception:
            logger.error(
                f"Shadow blank monitor probe failed for {_ref('channel', channel_uuid)}",
                exc_info=True,
            )
            return False

    def _handle_confirmed_blank(self, udi: Any, target: Dict[str, Any], config: Dict[str, Any], *, reason: str = "blank") -> None:
        channel_uuid = target["channel_uuid"]
        fresh_status = self._find_status_for_target(udi.get_proxy_status() or {}, target)
        if self._real_client_count(fresh_status, config) <= 0:
            self._reset_blank_count(channel_uuid)
            self._clear_switch_attempts(channel_uuid)
            self._record_event("viewer_left", target, {})
            return

        fresh_stream_id = self._extract_stream_id(fresh_status) or target.get("stream_id")
        if target.get("stream_id") and fresh_stream_id != target.get("stream_id"):
            self._reset_blank_count(channel_uuid)
            self._clear_switch_attempts(channel_uuid)
            self._record_event(
                "stale_stream_guard",
                target,
                {"current_stream_ref": _ref("stream", fresh_stream_id)},
            )
            return

        if not self._switch_allowed(channel_uuid, config):
            self._record_event("switch_rate_limited", target, {"reason": reason})
            return

        attempted_targets = self._attempted_switch_targets(channel_uuid, fresh_stream_id)
        alternative = self._choose_alternative_stream(
            udi,
            target.get("channel_id"),
            fresh_stream_id,
            excluded_stream_ids=attempted_targets,
        )
        if not alternative:
            self._set_cooldown(channel_uuid, config)
            self._reset_blank_count(channel_uuid)
            self._record_event("no_alternative", target, {"reason": reason})
            return

        if config.get("dry_run"):
            self._set_cooldown(channel_uuid, config)
            self._record_event(
                "dry_run_switch",
                target,
                {"target_stream_ref": _ref("stream", alternative), "reason": reason},
            )
            return

        self._record_switch_attempt(channel_uuid, fresh_stream_id, alternative)
        success = bool(self.switch_stream(channel_uuid, stream_id=alternative))
        self._reset_blank_count(channel_uuid)
        if success:
            with self._lock:
                self._switch_history[channel_uuid].append(self.clock())
        else:
            self._set_cooldown(channel_uuid, config)
        self._record_event(
            "switch_success" if success else "switch_failed",
            target,
            {
                "target_stream_ref": _ref("stream", alternative),
                "reason": reason,
                "post_switch_verification": bool(success),
            },
        )

    def _choose_alternative_stream(
        self,
        udi: Any,
        channel_id: Optional[int],
        current_stream_id: Optional[int],
        *,
        excluded_stream_ids: Optional[Iterable[int]] = None,
    ) -> Optional[int]:
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
        excluded = {
            int(item)
            for item in (excluded_stream_ids or [])
            if item is not None and str(item).isdigit()
        }
        if current_stream_id in stream_ids:
            index = stream_ids.index(current_stream_id)
            ordered = stream_ids[index + 1 :] + stream_ids[:index]
        else:
            ordered = stream_ids
        return next((sid for sid in ordered if sid != current_stream_id and sid not in excluded), None)

    @staticmethod
    def _detection_count_key(channel_uuid: str, reason: str) -> str:
        return f"{channel_uuid}:{reason or 'blank'}"

    def _reset_blank_count(self, channel_uuid: str) -> None:
        with self._lock:
            self._blank_counts.pop(channel_uuid, None)
            self._blank_counts.pop(self._detection_count_key(channel_uuid, "blank"), None)
            self._blank_counts.pop(self._detection_count_key(channel_uuid, "freeze"), None)

    def _increment_blank_count(self, channel_uuid: str, reason: str = "blank") -> int:
        key = self._detection_count_key(channel_uuid, reason)
        with self._lock:
            self._blank_counts[key] += 1
            return self._blank_counts[key]

    def _channel_proxy_url(self, channel_uuid: str) -> str:
        base_url = (self.base_url_provider() or "").rstrip("/")
        if not base_url:
            raise RuntimeError("Dispatcharr base URL is not configured")
        return f"{base_url}/proxy/ts/stream/{channel_uuid}"

    @staticmethod
    def _blank_probe_command(url: str, config: Dict[str, Any], *, continuous: bool = False) -> tuple[List[str], int]:
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
        video_filters = [
            (
                f"blackdetect=d={float(config['blank_min_duration_seconds'])}:"
                f"pix_th={float(config['blank_pixel_threshold'])}"
            )
        ]
        if config.get("freeze_detection_enabled"):
            video_filters.append(
                f"freezedetect=n={float(config['freeze_noise_threshold'])}:"
                f"d={float(config['freeze_min_duration_seconds'])}"
            )
        command.extend(
            [
                "-i",
                url,
                "-vf",
                ",".join(video_filters),
                "-an",
                "-f",
                "null",
                "-",
            ]
        )
        if not continuous:
            input_index = command.index("-vf")
            command[input_index:input_index] = ["-t", str(duration)]
        return command, duration

    def _run_blank_probe(self, url: str, config: Dict[str, Any]) -> Dict[str, Any]:
        command, duration = self._blank_probe_command(url, config)
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
            if config.get("freeze_detection_enabled"):
                parsed.update(_parse_freeze_detection(
                    output,
                    duration,
                    freeze_ratio_threshold=float(config["freeze_ratio_threshold"]),
                ))
            parsed["returncode"] = completed.returncode
            return parsed
        except subprocess.TimeoutExpired as exc:
            output = exc.stderr if isinstance(exc.stderr, str) else ""
            parsed = _parse_blank_detection(
                output,
                duration,
                blank_ratio_threshold=float(config["blank_ratio_threshold"]),
            )
            if config.get("freeze_detection_enabled"):
                parsed.update(_parse_freeze_detection(
                    output,
                    duration,
                    freeze_ratio_threshold=float(config["freeze_ratio_threshold"]),
                ))
            parsed["timeout"] = True
            return parsed

    def _run_blank_probe_until_viewer_left(
        self,
        url: str,
        config: Dict[str, Any],
        udi: Any,
        target: Dict[str, Any],
    ) -> Dict[str, Any]:
        command, duration = self._blank_probe_command(url, config, continuous=True)
        viewer_left = False
        stopped = False
        detected_reason = ""
        detected_duration = 0.0
        lines: List[str] = []
        line_queue: queue.Queue[str] = queue.Queue()
        process = subprocess.Popen(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )

        def read_stderr() -> None:
            if process.stderr is None:
                return
            try:
                for line in iter(process.stderr.readline, ""):
                    line_queue.put(line)
            except Exception as exc:
                logger.debug(f"Shadow blank monitor stderr reader stopped: {exc}")

        reader = threading.Thread(
            target=read_stderr,
            name=f"ShadowBlankProbeReader-{str(target.get('channel_uuid') or '')[:8]}",
            daemon=True,
        )
        reader.start()

        try:
            # Continuous probes are protecting a real viewer that is already on
            # the channel. Waiting for probe_duration * ratio turns a 60s
            # watcher into a 48s blank wait before even the first confirmation.
            # For open segments, use the configured minimum duration as the
            # real-time trigger and let confirmation_count provide the safety
            # against one-off black frames.
            blank_required = float(
                config.get("blank_min_duration_seconds", DEFAULT_CONFIG["blank_min_duration_seconds"])
            )
            freeze_required = float(
                config.get("freeze_min_duration_seconds", DEFAULT_CONFIG["freeze_min_duration_seconds"])
            )
            last_media_time = 0.0
            active_blank_start: Optional[float] = None
            active_blank_wall: Optional[float] = None
            active_freeze_start: Optional[float] = None
            active_freeze_wall: Optional[float] = None
            last_viewer_poll = 0.0

            def observed_duration(media_start: Optional[float], wall_start: Optional[float]) -> float:
                media_duration = 0.0
                if media_start is not None:
                    media_duration = max(0.0, last_media_time - media_start)
                wall_duration = 0.0
                if wall_start is not None:
                    wall_duration = max(0.0, time.monotonic() - wall_start)
                return max(media_duration, wall_duration)

            def mark_detection(reason: str, event_duration: float) -> bool:
                nonlocal detected_reason, detected_duration
                detected_reason = reason
                detected_duration = max(0.0, event_duration)
                if process.poll() is None:
                    process.terminate()
                return True

            while process.poll() is None and not self._stop_event.is_set():
                now = time.monotonic()
                while True:
                    try:
                        line = line_queue.get_nowait()
                    except queue.Empty:
                        break
                    lines.append(line)

                    progress_time = _parse_ffmpeg_progress_time(line)
                    if progress_time is not None:
                        last_media_time = max(last_media_time, progress_time)

                    blank_start_match = BLACK_START_RE.search(line)
                    if blank_start_match:
                        try:
                            active_blank_start = max(0.0, float(blank_start_match.group("start")))
                            active_blank_wall = now
                        except (TypeError, ValueError):
                            active_blank_start = None
                            active_blank_wall = None

                    blank_end_match = BLACK_END_RE.search(line)
                    if blank_end_match:
                        segment_duration = None
                        duration_match = BLACK_DURATION_RE.search(line)
                        if duration_match:
                            try:
                                segment_duration = max(0.0, float(duration_match.group("duration")))
                            except (TypeError, ValueError):
                                segment_duration = None
                        if segment_duration is None and active_blank_start is not None:
                            try:
                                segment_duration = max(0.0, float(blank_end_match.group("end")) - active_blank_start)
                            except (TypeError, ValueError):
                                segment_duration = 0.0
                        active_blank_start = None
                        active_blank_wall = None
                        if segment_duration is not None and segment_duration >= blank_required:
                            if mark_detection("blank", segment_duration):
                                break

                    freeze_start_match = FREEZE_START_RE.search(line)
                    if freeze_start_match:
                        try:
                            active_freeze_start = max(0.0, float(freeze_start_match.group("start")))
                            active_freeze_wall = now
                        except (TypeError, ValueError):
                            active_freeze_start = None
                            active_freeze_wall = None

                    freeze_end_match = FREEZE_END_RE.search(line)
                    if freeze_end_match:
                        segment_duration = None
                        duration_match = FREEZE_DURATION_RE.search(line)
                        if duration_match:
                            try:
                                segment_duration = max(0.0, float(duration_match.group("duration")))
                            except (TypeError, ValueError):
                                segment_duration = None
                        if segment_duration is None and active_freeze_start is not None:
                            try:
                                segment_duration = max(0.0, float(freeze_end_match.group("end")) - active_freeze_start)
                            except (TypeError, ValueError):
                                segment_duration = 0.0
                        active_freeze_start = None
                        active_freeze_wall = None
                        if segment_duration is not None and segment_duration >= freeze_required:
                            if mark_detection("freeze", segment_duration):
                                break

                if detected_reason:
                    break

                if (
                    active_blank_start is not None
                    and observed_duration(active_blank_start, active_blank_wall) >= blank_required
                ):
                    mark_detection("blank", observed_duration(active_blank_start, active_blank_wall))
                    break

                if (
                    active_freeze_start is not None
                    and observed_duration(active_freeze_start, active_freeze_wall) >= freeze_required
                ):
                    mark_detection("freeze", observed_duration(active_freeze_start, active_freeze_wall))
                    break

                try:
                    if now - last_viewer_poll >= 1.0:
                        last_viewer_poll = now
                        fresh_status = self._find_status_for_target(udi.get_proxy_status() or {}, target)
                        if self._real_client_count(fresh_status, config) <= 0:
                            viewer_left = True
                            process.terminate()
                            break
                except Exception as exc:
                    logger.warning(f"Shadow blank monitor viewer poll failed: {exc}")

                time.sleep(0.2)

            if self._stop_event.is_set() and process.poll() is None:
                stopped = True
                process.terminate()

            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
                stopped = True

            while True:
                try:
                    lines.append(line_queue.get_nowait())
                except queue.Empty:
                    break

            output = "".join(lines)
            observed_probe_duration = max(
                duration,
                int(last_media_time) if last_media_time else 0,
                int(detected_duration) if detected_duration else 0,
            )
            parsed = _parse_blank_detection(
                output,
                observed_probe_duration,
                blank_ratio_threshold=float(config["blank_ratio_threshold"]),
            )
            if config.get("freeze_detection_enabled"):
                parsed.update(_parse_freeze_detection(
                    output,
                    observed_probe_duration,
                    freeze_ratio_threshold=float(config["freeze_ratio_threshold"]),
                ))
            parsed["returncode"] = process.returncode
            if detected_reason == "blank":
                parsed["blank_detected"] = True
                parsed["blank_duration_secs"] = round(detected_duration, 3)
                parsed["blank_ratio"] = round(min(1.0, detected_duration / float(duration or 1)), 4)
            elif detected_reason == "freeze":
                parsed.setdefault("blank_detected", False)
                parsed["freeze_detected"] = True
                parsed["freeze_duration_secs"] = round(detected_duration, 3)
                parsed["freeze_ratio"] = round(min(1.0, detected_duration / float(duration or 1)), 4)
            if viewer_left:
                parsed["viewer_left"] = True
            if stopped:
                parsed["stopped"] = True
            return parsed
        finally:
            if process.poll() is None:
                process.kill()

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

    def _watcher_client_count(self, status: Dict[str, Any], config: Dict[str, Any]) -> int:
        return int(self._watcher_client_details(status, config).get("watcher_client_count") or 0)

    def _watcher_client_details(self, status: Dict[str, Any], config: Dict[str, Any]) -> Dict[str, Any]:
        marker = str(config.get("watcher_user_agent") or "").lower()
        if not marker:
            return {"watcher_client_count": 0}
        clients = status.get("clients")
        if isinstance(clients, dict):
            clients = list(clients.values())
        if not isinstance(clients, list):
            return {"watcher_client_count": 0}

        watcher_clients = [
            (index, client)
            for index, client in enumerate(clients)
            if marker in self._client_text(client).lower()
        ]
        details: Dict[str, Any] = {"watcher_client_count": len(watcher_clients)}
        if not watcher_clients:
            return details

        index, client = watcher_clients[0]
        raw_client_id: Any = None
        connected_at: Any = None
        if isinstance(client, dict):
            raw_client_id = client.get("client_id") or client.get("id") or client.get("session_id")
            connected_at = client.get("connected_at") or client.get("started_at")
        if raw_client_id is None:
            raw_client_id = f"{index}:{self._client_text(client)}"

        details["watcher_client_ref"] = _ref("client", raw_client_id)
        try:
            connected_at_float = float(connected_at)
        except (TypeError, ValueError):
            connected_at_float = None
        if connected_at_float is not None:
            details["watcher_connected_at"] = connected_at_float
            details["watcher_uptime_seconds"] = max(0, int(self.clock() - connected_at_float))
        return details

    def _cooldown_remaining(self, channel_uuid: str) -> int:
        with self._lock:
            return max(0, int(self._cooldowns.get(channel_uuid, 0) - self.clock()))

    def _set_cooldown(self, channel_uuid: str, config: Dict[str, Any]) -> None:
        with self._lock:
            self._cooldowns[channel_uuid] = self.clock() + int(config["channel_cooldown_seconds"])

    def _attempted_switch_targets(
        self,
        channel_uuid: str,
        origin_stream_id: Optional[int],
    ) -> set[int]:
        if origin_stream_id is None:
            return set()
        try:
            origin = int(origin_stream_id)
        except (TypeError, ValueError):
            return set()
        with self._lock:
            entry = self._switch_attempts.get(channel_uuid) or {}
            if entry.get("origin_stream_id") != origin:
                return set()
            return {
                int(item)
                for item in entry.get("target_stream_ids", [])
                if item is not None and str(item).isdigit()
            }

    def _record_switch_attempt(
        self,
        channel_uuid: str,
        origin_stream_id: Optional[int],
        target_stream_id: Optional[int],
    ) -> None:
        if origin_stream_id is None or target_stream_id is None:
            return
        try:
            origin = int(origin_stream_id)
            target = int(target_stream_id)
        except (TypeError, ValueError):
            return
        with self._lock:
            entry = self._switch_attempts.get(channel_uuid)
            if not entry or entry.get("origin_stream_id") != origin:
                entry = {"origin_stream_id": origin, "target_stream_ids": []}
                self._switch_attempts[channel_uuid] = entry
            targets = entry.setdefault("target_stream_ids", [])
            if target not in targets:
                targets.append(target)

    def _clear_switch_attempts(self, channel_uuid: str) -> None:
        with self._lock:
            self._switch_attempts.pop(channel_uuid, None)

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
        if not config.get("skip_during_quality_check", False):
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
    def _parse_program_time(value: Any) -> Optional[datetime]:
        if not value:
            return None
        if isinstance(value, datetime):
            parsed = value
        else:
            text = str(value).strip()
            if not text:
                return None
            try:
                parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            except ValueError:
                return None
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    @classmethod
    def _normalize_program_payload(cls, program: Dict[str, Any], *, state: str) -> Optional[Dict[str, Any]]:
        title = (
            program.get("title")
            or program.get("program_title")
            or program.get("name")
        )
        if not title:
            return None
        start = program.get("start_time") or program.get("start") or program.get("starts_at")
        end = program.get("end_time") or program.get("end") or program.get("ends_at")
        payload = {
            "title": str(title),
            "state": state,
        }
        if start:
            payload["start_time"] = str(start)
        if end:
            payload["end_time"] = str(end)
        return payload

    def _current_epg_program(
        self,
        channel: Optional[Dict[str, Any]],
        raw_status: Dict[str, Any],
        numeric_channel_id: Optional[int],
    ) -> Optional[Dict[str, Any]]:
        for key in ("current_program", "epg_program", "program"):
            program = raw_status.get(key)
            if isinstance(program, dict):
                normalized = self._normalize_program_payload(program, state="current")
                if normalized:
                    return normalized

        status_title = (
            raw_status.get("program_title")
            or raw_status.get("epg_title")
            or raw_status.get("current_program_title")
        )
        if status_title:
            return {"title": str(status_title), "state": "current"}

        tvg_id = (channel or {}).get("tvg_id") or raw_status.get("tvg_id")
        if numeric_channel_id is None and not tvg_id:
            return None

        try:
            from apps.automation.scheduling_service import get_scheduling_service

            service = get_scheduling_service()
            programs = service.get_programs_by_channel(numeric_channel_id, tvg_id=tvg_id)
        except Exception as exc:
            logger.debug("Shadow monitor could not read EPG program: %s", exc)
            return None

        if not isinstance(programs, list) or not programs:
            return None

        now = datetime.now(timezone.utc)
        upcoming: List[tuple[datetime, Dict[str, Any]]] = []
        for program in programs:
            if not isinstance(program, dict):
                continue
            start = self._parse_program_time(program.get("start_time") or program.get("start"))
            end = self._parse_program_time(program.get("end_time") or program.get("end"))
            if start and end and start <= now <= end:
                return self._normalize_program_payload(program, state="current")
            if start and start > now:
                upcoming.append((start, program))

        if upcoming:
            upcoming.sort(key=lambda item: item[0])
            return self._normalize_program_payload(upcoming[0][1], state="upcoming")
        return None

    @staticmethod
    def _public_target(target: Dict[str, Any]) -> Dict[str, Any]:
        allowed = {
            "channel_ref",
            "stream_ref",
            "real_client_count",
            "watcher_client_count",
            "watcher_client_ref",
            "watcher_connected_at",
            "watcher_uptime_seconds",
            "watcher_state",
            "watcher_absent_since",
            "watcher_absent_seconds",
            "watcher_recovered_after_seconds",
            "last_watcher_client_ref",
            "state",
            "current_program",
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
