"""Teamarr managed-event preflight checks.

This service watches Teamarr-managed event channels and starts a targeted
StreamFlow single-channel check shortly before an event begins. Teamarr remains
the source of truth for event/channel identity; StreamFlow only scores the
already-created channel.
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
from copy import deepcopy
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional

import requests

from apps.core.logging_config import setup_logging
from apps.udi import get_udi_manager

logger = setup_logging(__name__)

CONFIG_DIR = Path(os.environ.get("CONFIG_DIR", "/app/data"))
CONFIG_FILE = CONFIG_DIR / "teamarr_preflight_config.json"
MAX_EVENTS = 120
MAX_UPCOMING_EVENTS = 1000
TEAMARR_PREFLIGHT_QUEUE_PRIORITY = 100

READY_SYNC_STATES = {"in_sync", "synced", "ready"}
STATIC_TEAM_MATCHUP_RE = re.compile(
    r"(^|\s)(?:@|at|vs\.?|v\.?|versus|bei|gegen)(?=\s|$)",
    re.IGNORECASE,
)
STATIC_TEAM_NON_EVENT_WINDOW_RE = re.compile(
    r"^\s*(?:coming\s+up|upcoming|pre[-\s]?game)\b",
    re.IGNORECASE,
)
EVENT_DATETIME_KEYS = (
    "event_date",
    "start_time",
    "start_time_utc",
    "starts_at",
    "scheduled_start",
    "scheduled_at",
    "game_time",
    "datetime",
)
DISPATCHARR_CHANNEL_ID_KEYS = (
    "dispatcharr_channel_id",
    "dispatcharr_id",
    "channel_id",
    "channelId",
)
DEFAULT_TEAMARR_PREFLIGHT_PROFILE_NAME = "Teamarr Event Preflight"
DEFAULT_TEAMARR_PREFLIGHT_VISIBILITY_POLICY: Dict[str, Any] = {
    "enabled": False,
    "hide_on_no_regex": False,
    "hide_on_no_streams": False,
    "hide_on_all_failed": False,
    "unhide_on_recovered": False,
}
CONTROLLED_CHECK_DEFERRAL_REASONS = {
    "active_viewers",
    "max_streams_reached",
    "connectivity_guard",
}
MANUAL_FORCE_ALLOWED_STATES = {"due", "scheduled", "already_attempted", "past"}
MANUAL_FORCE_ERROR_MESSAGES = {
    "event_not_found": "Preflight item was not found",
    "filtered": "Managed event is filtered by the current preflight configuration",
    "no_dispatcharr_channel": "Managed event has no Dispatcharr channel yet",
    "no_live_window": "Static team has no live window yet",
    "no_event_window": "Static team live window has no event evidence",
    "no_streams_yet": "Static team channel has no streams yet",
    "incomplete_team": "Static team channel status is incomplete",
    "past": "Managed event is outside the post-start grace window",
    "waiting_for_channel_sync": "Managed event channel is still syncing",
    "unavailable": "Preflight item is not available for manual preflight",
}
DEFAULT_TEAMARR_PREFLIGHT_PROFILE: Dict[str, Any] = {
    "name": DEFAULT_TEAMARR_PREFLIGHT_PROFILE_NAME,
    "description": (
        "Safe default for Teamarr-managed event preflights. Scores existing "
        "channel streams without playlist refresh, regex matching, or automatic "
        "dead-stream removal."
    ),
    "enabled": True,
    "parallel_checks": 1,
    "m3u_update": {
        "enabled": False,
        "playlists": [],
    },
    "stream_matching": {
        "enabled": False,
        "validate_existing_streams": False,
    },
    "stream_checking": {
        "enabled": True,
        "allow_revive": False,
        "remove_dead_streams": False,
        "check_all_streams": True,
        "loop_check_enabled": False,
        "blank_check_enabled": False,
        "treat_blank_as_dead": False,
        "freeze_check_enabled": False,
        "treat_freeze_as_dead": False,
        "stream_limit": 0,
        "min_resolution": "any",
        "min_fps": 0,
        "min_bitrate": 0,
        "m3u_priority": [],
        "m3u_priority_mode": "quality",
    },
    "scoring_weights": {
        "bitrate": 0.40,
        "resolution": 0.35,
        "fps": 0.15,
        "codec": 0.10,
        "prefer_h265": True,
        "loop_penalty": 0,
    },
    "channel_visibility_automation": deepcopy(DEFAULT_TEAMARR_PREFLIGHT_VISIBILITY_POLICY),
}

DEFAULT_CONFIG: Dict[str, Any] = {
    "enabled": False,
    "managed_event_preflight_enabled": True,
    "static_team_preflight_enabled": False,
    "teamarr_base_url": "",
    "api_key": "",
    "api_key_header": "X-API-Key",
    "poll_interval_seconds": 60,
    "preflight_offset_minutes": 20,
    "retry_offsets_minutes": [10, 3],
    "post_start_offsets_minutes": [2, 4],
    "post_start_grace_minutes": 5,
    "max_concurrent_checks": 1,
    "event_cooldown_minutes": 720,
    "queue_during_active_checks": True,
    "forced_profile_id": "",
    "include_sports": [],
    "exclude_sports": [],
    "include_leagues": [],
    "exclude_leagues": [],
}
LEGACY_CONFIG_KEYS = {"defer_during_active_checks", "skip_during_quality_check"}
CONFIG_KEYS = set(DEFAULT_CONFIG)

INT_BOUNDS = {
    "poll_interval_seconds": (15, 3600),
    "preflight_offset_minutes": (1, 360),
    "post_start_grace_minutes": (0, 120),
    "max_concurrent_checks": (1, 10),
    "event_cooldown_minutes": (1, 10080),
}


def _coerce_int(value: Any, default: int, bounds: tuple[int, int]) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(bounds[0], min(bounds[1], parsed))


def _coerce_list(value: Any) -> List[Any]:
    if isinstance(value, list):
        return value
    if value in (None, ""):
        return []
    return [value]


def _normalize_terms(value: Any) -> List[str]:
    terms = []
    for item in _coerce_list(value):
        text = str(item).strip().lower()
        if text:
            terms.append(text)
    return terms


def _normalize_minute_offsets(value: Any, *, min_value: int = 1, max_value: int = 360, reverse: bool = True) -> List[int]:
    offsets: List[int] = []
    for item in _coerce_list(value):
        try:
            parsed = int(item)
        except (TypeError, ValueError):
            continue
        if min_value <= parsed <= max_value:
            offsets.append(parsed)
    return sorted(set(offsets), reverse=reverse)


def _legacy_queue_during_active_checks(payload: Optional[Dict[str, Any]]) -> Optional[bool]:
    if not isinstance(payload, dict):
        return None
    if "queue_during_active_checks" in payload:
        return bool(payload.get("queue_during_active_checks"))
    if "defer_during_active_checks" in payload:
        return not bool(payload.get("defer_during_active_checks"))
    if "skip_during_quality_check" in payload:
        return not bool(payload.get("skip_during_quality_check"))
    return None


def normalize_config(payload: Optional[Dict[str, Any]], current: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    config = dict(DEFAULT_CONFIG)
    if current:
        config.update({key: value for key, value in current.items() if key in DEFAULT_CONFIG})
        legacy_queue_setting = _legacy_queue_during_active_checks(current)
        if legacy_queue_setting is not None:
            config["queue_during_active_checks"] = legacy_queue_setting
    if payload:
        config.update({key: value for key, value in payload.items() if key in CONFIG_KEYS})
        legacy_queue_setting = _legacy_queue_during_active_checks(payload)
        if legacy_queue_setting is not None:
            config["queue_during_active_checks"] = legacy_queue_setting

    for key, bounds in INT_BOUNDS.items():
        config[key] = _coerce_int(config.get(key), DEFAULT_CONFIG[key], bounds)

    config["enabled"] = bool(config.get("enabled"))
    config["managed_event_preflight_enabled"] = bool(config.get("managed_event_preflight_enabled", True))
    config["static_team_preflight_enabled"] = bool(config.get("static_team_preflight_enabled"))
    config["queue_during_active_checks"] = bool(config.get("queue_during_active_checks"))
    config["teamarr_base_url"] = str(config.get("teamarr_base_url") or "").strip().rstrip("/")
    config["api_key"] = str(config.get("api_key") or "").strip()
    config["api_key_header"] = str(config.get("api_key_header") or DEFAULT_CONFIG["api_key_header"]).strip()[:80]
    if not config["api_key_header"]:
        config["api_key_header"] = DEFAULT_CONFIG["api_key_header"]
    config["forced_profile_id"] = str(config.get("forced_profile_id") or "").strip()
    config["retry_offsets_minutes"] = _normalize_minute_offsets(config.get("retry_offsets_minutes"))
    config["post_start_offsets_minutes"] = _normalize_minute_offsets(
        config.get("post_start_offsets_minutes"),
        max_value=120,
        reverse=False,
    )
    for key in ("include_sports", "exclude_sports", "include_leagues", "exclude_leagues"):
        config[key] = _normalize_terms(config.get(key))
    return config


def public_config(config: Dict[str, Any], metadata: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    visible = dict(config)
    visible["defer_during_active_checks"] = not bool(visible.get("queue_during_active_checks", True))
    visible["skip_during_quality_check"] = not bool(visible.get("queue_during_active_checks", True))
    visible["has_api_key"] = bool(visible.get("api_key"))
    visible["api_key"] = ""
    if metadata:
        visible.update(metadata)
    return visible


def _parse_event_datetime(value: Any) -> Optional[datetime]:
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _first_present(event: Dict[str, Any], keys: Iterable[str]) -> Any:
    for key in keys:
        value = event.get(key)
        if value not in (None, ""):
            return value
    return None


def _event_datetime_value(event: Dict[str, Any]) -> Any:
    return _first_present(event, EVENT_DATETIME_KEYS)


def _event_dispatcharr_channel_id(event: Dict[str, Any]) -> Any:
    value = _first_present(event, DISPATCHARR_CHANNEL_ID_KEYS)
    if value not in (None, ""):
        return value
    dispatcharr_channel = event.get("dispatcharr_channel")
    if isinstance(dispatcharr_channel, dict):
        return _first_present(dispatcharr_channel, ("id", "channel_id", "dispatcharr_channel_id"))
    return None


def _event_identity(event: Dict[str, Any]) -> str:
    date_value = _event_datetime_value(event) or ""
    managed_id = event.get("id")
    if managed_id not in (None, ""):
        return f"id:{managed_id}:{date_value}"

    channel_id = _event_dispatcharr_channel_id(event) or ""
    group_id = event.get("event_epg_group_id") or event.get("group_id") or ""
    event_id = event.get("event_id")
    if event_id not in (None, ""):
        return f"event:{event_id}:channel:{channel_id}:group:{group_id}:{date_value}"
    return f"channel:{channel_id}:{date_value}:{event.get('event_name') or event.get('channel_name') or ''}"


class TeamarrPreflightService:
    def __init__(
        self,
        *,
        config_file: Path = CONFIG_FILE,
        http_get: Callable[..., Any] = requests.get,
        udi_provider: Callable[[], Any] = get_udi_manager,
        stream_checker_provider: Optional[Callable[[], Any]] = None,
        automation_config_provider: Optional[Callable[[], Any]] = None,
        automation_status_provider: Optional[Callable[[], Dict[str, Any]]] = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.config_file = config_file
        self.http_get = http_get
        self.udi_provider = udi_provider
        self.stream_checker_provider = stream_checker_provider or self._default_stream_checker_provider
        self.automation_config_provider = automation_config_provider or self._default_automation_config_provider
        self.automation_status_provider = automation_status_provider or self._default_automation_status_provider
        self.clock = clock

        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._config = self._load_config()
        self._default_profile_id: str = ""
        self._default_profile_error: Optional[str] = None
        self._ensure_default_profile()
        self._events: deque[Dict[str, Any]] = deque(maxlen=MAX_EVENTS)
        self._upcoming: List[Dict[str, Any]] = []
        self._upcoming_teams: List[Dict[str, Any]] = []
        self._preflight_items: List[Dict[str, Any]] = []
        self._filter_options: Dict[str, Any] = {"sports": [], "leagues": [], "source": "events"}
        self._attempted_buckets: Dict[str, float] = {}
        self._active_checks: Dict[str, Dict[str, Any]] = {}
        self._last_scan_at: Optional[float] = None
        self._last_error: Optional[str] = None
        self._last_team_error: Optional[str] = None
        self._last_events_seen = 0
        self._last_candidates_count = 0
        self._last_teams_seen = 0
        self._last_team_candidates_count = 0
        self._last_team_status: Dict[str, Any] = {
            "enabled": False,
            "seen": 0,
            "ready": 0,
            "incomplete": 0,
            "queueable": 0,
            "last_error": None,
        }
        self._upcoming_truncated = False

    def _load_config(self) -> Dict[str, Any]:
        try:
            if self.config_file.exists():
                with open(self.config_file, "r", encoding="utf-8") as handle:
                    raw_config = json.load(handle)
                config = normalize_config(raw_config)
                if any(key in raw_config for key in LEGACY_CONFIG_KEYS):
                    self.config_file.parent.mkdir(parents=True, exist_ok=True)
                    with open(self.config_file, "w", encoding="utf-8") as handle:
                        json.dump(config, handle, indent=2, sort_keys=True)
                        handle.write("\n")
                return config
        except Exception as exc:
            logger.warning(f"Failed to load Teamarr preflight config: {exc}")
        return normalize_config({})

    def _save_config(self) -> None:
        self.config_file.parent.mkdir(parents=True, exist_ok=True)
        with open(self.config_file, "w", encoding="utf-8") as handle:
            json.dump(self._config, handle, indent=2, sort_keys=True)
            handle.write("\n")

    @staticmethod
    def _profile_items(payload: Any) -> List[Dict[str, Any]]:
        if isinstance(payload, dict):
            payload = payload.get("items") or payload.get("profiles") or []
        if not isinstance(payload, list):
            return []
        return [item for item in payload if isinstance(item, dict)]

    def _default_profile_metadata(self) -> Dict[str, Any]:
        return {
            "default_profile_id": self._default_profile_id,
            "default_profile_name": DEFAULT_TEAMARR_PREFLIGHT_PROFILE_NAME,
            "default_profile_available": bool(self._default_profile_id),
            "default_profile_error": self._default_profile_error,
        }

    @staticmethod
    def _default_profile_visibility_needs_update(profile: Optional[Dict[str, Any]]) -> bool:
        if not isinstance(profile, dict):
            return False
        current = profile.get("channel_visibility_automation")
        if not isinstance(current, dict):
            return True
        if "inherit_global" in current:
            return True
        for key, value in DEFAULT_TEAMARR_PREFLIGHT_VISIBILITY_POLICY.items():
            if current.get(key) != value:
                return True
        return False

    def _quality_profile_details(self, profile_id: Any) -> Dict[str, Any]:
        resolved_id = self._resolve_profile_id(profile_id)
        if not resolved_id:
            return {}
        details: Dict[str, Any] = {"quality_profile_id": str(resolved_id)}
        try:
            automation_config = self.automation_config_provider()
            profile = automation_config.get_profile(resolved_id)
            if isinstance(profile, dict) and profile.get("name"):
                details["quality_profile_name"] = str(profile.get("name"))
        except Exception as exc:
            logger.debug("Could not resolve Teamarr preflight quality profile details: %s", exc)
        if not details.get("quality_profile_name") and str(resolved_id) == str(self._default_profile_id):
            details["quality_profile_name"] = DEFAULT_TEAMARR_PREFLIGHT_PROFILE_NAME
        return details

    def _ensure_default_profile(self) -> None:
        try:
            automation_config = self.automation_config_provider()
            profiles = self._profile_items(automation_config.get_all_profiles())
            default_profile = next(
                (
                    profile
                    for profile in profiles
                    if str(profile.get("name") or "").strip() == DEFAULT_TEAMARR_PREFLIGHT_PROFILE_NAME
                ),
                None,
            )

            profile_id = str(default_profile.get("id")) if default_profile and default_profile.get("id") else ""
            if not profile_id:
                profile_id = str(automation_config.create_profile(deepcopy(DEFAULT_TEAMARR_PREFLIGHT_PROFILE)) or "")
                if not profile_id:
                    raise RuntimeError("default profile could not be created")
                logger.info("Created default Teamarr preflight automation profile id=%s", profile_id)
            elif self._default_profile_visibility_needs_update(default_profile):
                update_profile = getattr(automation_config, "update_profile", None)
                if callable(update_profile):
                    update_profile(
                        profile_id,
                        {
                            "channel_visibility_automation": deepcopy(
                                DEFAULT_TEAMARR_PREFLIGHT_VISIBILITY_POLICY
                            )
                        },
                    )
                    logger.info(
                        "Updated Teamarr preflight profile id=%s with profile-level channel visibility policy",
                        profile_id,
                    )

            with self._lock:
                self._default_profile_id = profile_id
                self._default_profile_error = None
                if not str(self._config.get("forced_profile_id") or "").strip():
                    self._config["forced_profile_id"] = profile_id
                    self._save_config()
        except Exception as exc:
            with self._lock:
                self._default_profile_error = "Default profile is unavailable"
            logger.warning("Could not ensure Teamarr preflight default profile: %s", exc)

    def get_config(self, *, include_secret: bool = False) -> Dict[str, Any]:
        self._ensure_default_profile()
        with self._lock:
            if include_secret:
                config = dict(self._config)
                config.update(self._default_profile_metadata())
                return config
            return public_config(self._config, self._default_profile_metadata())

    def update_config(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock:
            payload = dict(payload or {})
            if "queue_during_active_checks" not in payload:
                legacy_queue_setting = _legacy_queue_during_active_checks(payload)
                if legacy_queue_setting is not None:
                    payload["queue_during_active_checks"] = legacy_queue_setting
            current = dict(self._config)
            if payload.get("clear_api_key"):
                current["api_key"] = ""
                payload.pop("api_key", None)
            elif payload.get("api_key", "") == "":
                payload["api_key"] = current.get("api_key", "")

            self._config = normalize_config(payload, current)
            self._save_config()
            enabled = self._config["enabled"]

        self._ensure_default_profile()

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
                name="TeamarrPreflight",
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
            thread = self._thread
        self._set_stream_checker_event_gate(False, gate_name="teamarr_preflight_automation")
        self._set_stream_checker_event_gate(False, gate_name="teamarr_preflight_direct")
        if thread and thread.is_alive():
            thread.join(timeout=5)
        return True

    def get_status(self) -> Dict[str, Any]:
        queue_snapshot = self._teamarr_queue_snapshot()
        with self._lock:
            all_events = list(self._events)
            recent_events = all_events[:25]
            upcoming_events = self._attach_recent_events_to_upcoming(self._upcoming, all_events)
            upcoming_teams = self._attach_recent_events_to_upcoming(self._upcoming_teams, all_events)
            preflight_items = self._attach_recent_events_to_upcoming(self._preflight_items, all_events)
            return {
                "enabled": bool(self._config.get("enabled")),
                "running": bool(self._thread and self._thread.is_alive()),
                "last_scan_at": self._last_scan_at,
                "last_error": self._last_error,
                "last_team_error": self._last_team_error,
                "active_checks": list(self._active_checks.values()),
                "queued_checks": queue_snapshot["queued_checks"],
                "queued_checks_count": len(queue_snapshot["queued_checks"]),
                "queue_active_checks": queue_snapshot["queue_active_checks"],
                "queue_active_checks_count": len(queue_snapshot["queue_active_checks"]),
                "upcoming_events": upcoming_events,
                "upcoming_teams": upcoming_teams,
                "preflight_items": preflight_items,
                "team_status": dict(self._last_team_status),
                "managed_events_seen": self._last_events_seen,
                "managed_candidates": self._last_candidates_count,
                "managed_events_returned": len(upcoming_events),
                "static_teams_seen": self._last_teams_seen,
                "static_team_candidates": self._last_team_candidates_count,
                "preflight_candidates": len(preflight_items),
                "managed_events_truncated": self._upcoming_truncated,
                "managed_events_limit": MAX_UPCOMING_EVENTS,
                "recent_events": recent_events,
                "filter_options": dict(self._filter_options),
                "teamarr_connector": self._teamarr_connector_status(),
                "config": public_config(self._config, self._default_profile_metadata()),
            }

    def _teamarr_queue_snapshot(self) -> Dict[str, List[Dict[str, Any]]]:
        snapshot: Dict[str, List[Dict[str, Any]]] = {
            "queued_checks": [],
            "queue_active_checks": [],
        }
        try:
            checker = self.stream_checker_provider()
        except Exception as exc:
            logger.debug("Unable to read Stream Checker queue for Teamarr preflight status: %s", exc)
            return snapshot

        check_queue = getattr(checker, "check_queue", None)
        if check_queue is None:
            return snapshot

        def collect(mapping_name: str) -> List[Dict[str, Any]]:
            mapping = getattr(check_queue, mapping_name, {}) or {}
            priorities = getattr(check_queue, "queued_priorities", {}) or {}
            items: List[Dict[str, Any]] = []
            for raw_channel_id, metadata in list(mapping.items()):
                if not isinstance(metadata, dict) or metadata.get("source") != "teamarr_preflight":
                    continue
                event = metadata.get("event") or {}
                if not isinstance(event, dict):
                    event = {}
                dispatcharr_channel_id = event.get("dispatcharr_channel_id") or raw_channel_id
                item = {
                    "identity": event.get("identity"),
                    "preflight_kind": event.get("preflight_kind") or metadata.get("preflight_kind") or "event",
                    "teamarr_id": event.get("teamarr_id"),
                    "teamarr_team_id": event.get("teamarr_team_id"),
                    "event_id": event.get("event_id"),
                    "event_name": event.get("event_name") or metadata.get("program_name"),
                    "team_name": event.get("team_name"),
                    "team_abbrev": event.get("team_abbrev"),
                    "event_date": event.get("event_date"),
                    "channel_name": event.get("channel_name"),
                    "dispatcharr_channel_id": dispatcharr_channel_id,
                    "dispatcharr_uuid": event.get("dispatcharr_uuid"),
                    "sport": event.get("sport"),
                    "league": event.get("league"),
                    "seconds_to_start": event.get("seconds_to_start"),
                    "bucket": event.get("trigger_bucket") or metadata.get("trigger_bucket"),
                    "forced_profile_id": metadata.get("forced_profile_id"),
                    "quality_profile_id": event.get("quality_profile_id") or metadata.get("quality_profile_id"),
                    "quality_profile_name": event.get("quality_profile_name") or metadata.get("quality_profile_name"),
                }
                if mapping_name == "queued_metadata":
                    item["priority"] = priorities.get(raw_channel_id)
                items.append(item)
            items.sort(key=lambda item: (
                str(item.get("event_date") or ""),
                str(item.get("channel_name") or ""),
                str(item.get("event_name") or ""),
            ))
            return items

        lock = getattr(check_queue, "lock", None)
        if lock is None:
            snapshot["queued_checks"] = collect("queued_metadata")
            snapshot["queue_active_checks"] = collect("in_progress_metadata")
            return snapshot

        with lock:
            snapshot["queued_checks"] = collect("queued_metadata")
            snapshot["queue_active_checks"] = collect("in_progress_metadata")
        return snapshot

    def _teamarr_connector_status(self) -> Dict[str, Any]:
        base_url = str(self._config.get("teamarr_base_url") or "").strip()
        if not base_url:
            state = "not_configured"
            label = "Not configured"
            detail = "Set the Teamarr base URL to read managed event channels."
        elif self._last_error:
            state = "error"
            label = "Scan error"
            detail = "Teamarr managed event endpoint did not complete the last scan."
        elif not self._last_scan_at:
            state = "pending"
            label = "Waiting for scan"
            detail = "StreamFlow has not scanned Teamarr managed event channels yet."
        elif self._last_events_seen <= 0:
            state = "empty"
            label = "No managed channels"
            detail = "Teamarr was reached but returned no managed event channels."
        elif self._last_candidates_count <= 0:
            state = "filtered"
            label = "No matching events"
            detail = "Teamarr returned managed channels, but filters or event data left no check candidates."
        else:
            state = "connected"
            label = "Connected"
            detail = "Teamarr managed event channels are available."

        return {
            "state": state,
            "label": label,
            "detail": detail,
            "base_url_configured": bool(base_url),
            "endpoint": "/api/v1/channels/managed",
            "official_api_key_required": False,
            "last_scan_at": self._last_scan_at,
            "last_error": self._last_error,
            "managed_events_seen": self._last_events_seen,
            "managed_candidates": self._last_candidates_count,
        }

    @staticmethod
    def _attach_recent_events_to_upcoming(
        upcoming_events: Iterable[Dict[str, Any]],
        recent_events: Iterable[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        latest_by_key: Dict[str, Dict[str, Any]] = {}
        for recent in recent_events:
            for key in TeamarrPreflightService._event_match_keys(recent):
                if key not in latest_by_key:
                    latest_by_key[key] = recent

        enriched = []
        for event in upcoming_events:
            public_event = dict(event)
            latest = None
            for key in TeamarrPreflightService._event_match_keys(public_event):
                latest = latest_by_key.get(key)
                if latest:
                    break
            if latest:
                public_event["last_preflight_event"] = deepcopy(latest)
            enriched.append(public_event)
        return enriched

    @staticmethod
    def _event_match_keys(event: Dict[str, Any]) -> List[str]:
        keys = []
        identity = str(event.get("identity") or "").strip()
        if identity:
            keys.append(f"identity:{identity}")

        event_date = str(event.get("event_date") or "").strip()
        teamarr_id = str(event.get("teamarr_id") or "").strip()
        event_id = str(event.get("event_id") or "").strip()
        channel_id = str(event.get("dispatcharr_channel_id") or "").strip()
        event_name = str(event.get("event_name") or "").strip().casefold()

        if teamarr_id and event_date:
            keys.append(f"teamarr:{teamarr_id}:{event_date}")
        if event_id and event_date:
            keys.append(f"event:{event_id}:{event_date}")
        if channel_id and event_date:
            keys.append(f"channel-date:{channel_id}:{event_date}")
        if channel_id and event_name:
            keys.append(f"channel-name:{channel_id}:{event_name}")

        return keys

    def _collect_preflight_scan(self, config: Dict[str, Any], now: datetime) -> Dict[str, Any]:
        raw_events: List[Dict[str, Any]] = []
        event_candidates: List[Dict[str, Any]] = []
        team_statuses: List[Dict[str, Any]] = []
        team_candidates: List[Dict[str, Any]] = []
        team_error: Optional[str] = None

        if config.get("managed_event_preflight_enabled", True):
            raw_events = self._fetch_managed_events(config)
            event_candidates = self._build_candidates(raw_events, config, now)

        if config.get("static_team_preflight_enabled"):
            try:
                team_statuses = self._fetch_static_team_statuses(config)
                team_candidates = self._build_team_candidates(team_statuses, config, now)
            except Exception as exc:
                logger.warning("Teamarr static team preflight source degraded: %s", exc)
                team_error = "Teamarr static team endpoint did not complete the last scan"

        combined_items = sorted(
            [*event_candidates, *team_candidates],
            key=self._candidate_sort_key,
        )
        filter_options = self._build_filter_options(config, [*raw_events, *team_candidates])
        team_status = self._team_status_summary(
            enabled=bool(config.get("static_team_preflight_enabled")),
            seen=len(team_statuses),
            candidates=team_candidates,
            error=team_error,
        )
        return {
            "raw_events": raw_events,
            "event_candidates": event_candidates,
            "team_statuses": team_statuses,
            "team_candidates": team_candidates,
            "combined_items": combined_items,
            "filter_options": filter_options,
            "team_status": team_status,
            "team_error": team_error,
        }

    def _store_preflight_scan(self, scan: Dict[str, Any], *, scan_error: Optional[str] = None) -> None:
        event_candidates = list(scan.get("event_candidates") or [])
        team_candidates = list(scan.get("team_candidates") or [])
        combined_items = list(scan.get("combined_items") or [])
        with self._lock:
            self._last_scan_at = self.clock()
            self._last_error = scan_error
            self._last_team_error = scan.get("team_error")
            self._upcoming = event_candidates[:MAX_UPCOMING_EVENTS]
            self._upcoming_teams = team_candidates[:MAX_UPCOMING_EVENTS]
            self._preflight_items = combined_items[:MAX_UPCOMING_EVENTS]
            self._last_events_seen = len(scan.get("raw_events") or [])
            self._last_candidates_count = len(event_candidates)
            self._last_teams_seen = len(scan.get("team_statuses") or [])
            self._last_team_candidates_count = len(team_candidates)
            self._last_team_status = dict(scan.get("team_status") or {})
            self._upcoming_truncated = len(event_candidates) > len(self._upcoming)
            self._filter_options = dict(scan.get("filter_options") or {})

    def run_once(self, *, force: bool = False) -> Dict[str, Any]:
        config = self.get_config(include_secret=True)
        if not config.get("enabled") and not force:
            self._set_stream_checker_event_gate(False, gate_name="teamarr_preflight_automation")
            return {"success": True, "skipped": True, "reason": "disabled"}

        try:
            self._sync_automation_queue_gate(config)
            now = datetime.fromtimestamp(self.clock(), tz=timezone.utc)
            scan = self._collect_preflight_scan(config, now)
            candidates = list(scan["combined_items"])
            launched = 0
            skipped = 0
            for event in candidates:
                if event.get("state") != "due":
                    skipped += 1
                    continue
                if self._launch_check(event, config, force=force):
                    launched += 1
                else:
                    skipped += 1

            self._store_preflight_scan(scan, scan_error=None)

            return {
                "success": True,
                "events_seen": len(scan["raw_events"]),
                "teams_seen": len(scan["team_statuses"]),
                "candidates": len(candidates),
                "event_candidates": len(scan["event_candidates"]),
                "team_candidates": len(scan["team_candidates"]),
                "team_error": scan.get("team_error"),
                "launched": launched,
                "skipped": skipped,
            }
        except Exception as exc:
            logger.error(f"Teamarr preflight scan failed: {exc}", exc_info=True)
            safe_error = "Teamarr preflight scan failed"
            with self._lock:
                self._last_scan_at = self.clock()
                self._last_error = safe_error
            self._record_event("scan_failed", {}, {"error": safe_error})
            return {"success": False, "error": safe_error}

    def force_check_event(self, identity: Any) -> Dict[str, Any]:
        requested_identity = str(identity or "").strip()
        if not requested_identity:
            return {
                "success": False,
                "error": "Managed event identity is required",
                "code": "missing_identity",
            }

        config = self.get_config(include_secret=True)
        try:
            now = datetime.fromtimestamp(self.clock(), tz=timezone.utc)
            scan = self._collect_preflight_scan(config, now)
            candidates = list(scan["combined_items"])
            target = next(
                (
                    event
                    for event in candidates
                    if str(event.get("identity") or "") == requested_identity
                ),
                None,
            )

            self._store_preflight_scan(scan, scan_error=None)

            if target is None:
                return {
                    "success": False,
                    "error": MANUAL_FORCE_ERROR_MESSAGES["event_not_found"],
                    "code": "event_not_found",
                    "identity": requested_identity,
                }

            blocked_reason = self._manual_force_block_reason(target)
            if blocked_reason:
                self._record_event(
                    "manual_preflight_rejected",
                    target,
                    {"reason": blocked_reason, "state": target.get("state")},
                )
                return {
                    "success": False,
                    "error": MANUAL_FORCE_ERROR_MESSAGES.get(
                        blocked_reason,
                        MANUAL_FORCE_ERROR_MESSAGES["unavailable"],
                    ),
                    "code": blocked_reason,
                    "event": target,
                }

            manual_event = dict(target)
            manual_event["state"] = "due"
            manual_event["trigger_bucket"] = "manual"
            launched = self._launch_check(manual_event, config, force=True)
            return {
                "success": True,
                "launched": launched,
                "event": manual_event,
                "reason": None if launched else "not_launched",
            }
        except Exception as exc:
            logger.error(f"Teamarr manual preflight failed: {exc}", exc_info=True)
            safe_error = "Teamarr manual preflight failed"
            with self._lock:
                self._last_scan_at = self.clock()
                self._last_error = safe_error
            self._record_event("scan_failed", {}, {"error": safe_error})
            return {"success": False, "error": safe_error, "code": "scan_failed"}

    def _worker(self) -> None:
        while not self._stop_event.is_set():
            with self._lock:
                config = dict(self._config)
                enabled = bool(config.get("enabled"))
                interval = int(config.get("poll_interval_seconds", 60))

            if enabled:
                self.run_once(force=False)

            self._stop_event.wait(interval)

    def _fetch_managed_events(self, config: Dict[str, Any]) -> List[Dict[str, Any]]:
        payload = self._fetch_teamarr_json(config, "/api/v1/channels/managed")
        if isinstance(payload, dict):
            for key in ("items", "channels", "data", "results"):
                value = payload.get(key)
                if isinstance(value, list):
                    return [item for item in value if isinstance(item, dict)]
            return []
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]
        return []

    @staticmethod
    def _extract_teamarr_items(payload: Any) -> List[Dict[str, Any]]:
        if isinstance(payload, dict):
            for key in ("items", "teams", "data", "results"):
                value = payload.get(key)
                if isinstance(value, list):
                    return [item for item in value if isinstance(item, dict)]
            return []
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]
        return []

    def _fetch_static_team_statuses(self, config: Dict[str, Any]) -> List[Dict[str, Any]]:
        teams = self._extract_teamarr_items(
            self._fetch_teamarr_json(config, "/api/v1/teams?active_only=true")
        )
        statuses: List[Dict[str, Any]] = []
        for team in teams:
            team_id = team.get("id")
            if team_id in (None, ""):
                continue
            try:
                status = self._fetch_teamarr_json(config, f"/api/v1/teams/{team_id}/channel-status")
                if isinstance(status, dict):
                    statuses.append(status)
                    continue
            except Exception as exc:
                logger.warning("Teamarr team channel status degraded for team id=%s: %s", team_id, exc)
            statuses.append({
                "team": dict(team),
                "dispatcharr_channel": {"found": False, "error": "Team channel status unavailable"},
                "next_live_window": {"found": False, "is_live": False, "source": "team_epg_xmltv"},
                "status": "incomplete",
                "missing": ["channel_status"],
                "error": "Team channel status unavailable",
            })
        return statuses

    def _fetch_teamarr_json(self, config: Dict[str, Any], path: str) -> Any:
        base_url = str(config.get("teamarr_base_url") or "").rstrip("/")
        if not base_url:
            raise ValueError("Teamarr base URL is required")

        headers: Dict[str, str] = {}
        api_key = str(config.get("api_key") or "")
        if api_key:
            headers[str(config.get("api_key_header") or DEFAULT_CONFIG["api_key_header"])] = api_key

        response = self.http_get(
            f"{base_url}{path}",
            headers=headers,
            timeout=20,
        )
        response.raise_for_status()
        return response.json()

    def _build_filter_options(self, config: Dict[str, Any], events: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
        fallback = self._event_filter_options(events)
        try:
            subscription = self._fetch_teamarr_json(config, "/api/v1/sports-subscription")
            sports_catalog = self._fetch_teamarr_json(config, "/api/v1/cache/sports")
            leagues_catalog = self._fetch_teamarr_json(config, "/api/v1/cache/leagues")
            selected_leagues = self._selected_league_slugs(subscription)
            if not selected_leagues:
                return fallback

            sports_by_slug = self._sports_catalog_by_slug(sports_catalog)
            leagues_by_slug = self._leagues_catalog_by_slug(leagues_catalog)
            league_options = []
            selected_sports = set()

            for league_slug in selected_leagues:
                league = leagues_by_slug.get(league_slug, {})
                sport_slug = str(league.get("sport") or "").strip().lower()
                if sport_slug:
                    selected_sports.add(sport_slug)
                elif league_slug in sports_by_slug:
                    selected_sports.add(league_slug)
                league_options.append({
                    "value": league_slug,
                    "label": str(league.get("name") or league.get("league_alias") or league_slug).strip(),
                    "sport": sport_slug,
                })

            for event_sport in fallback.get("sports", []):
                value = event_sport.get("value") if isinstance(event_sport, dict) else event_sport
                if value:
                    selected_sports.add(str(value).strip().lower())

            sport_options = [
                {
                    "value": sport_slug,
                    "label": sports_by_slug.get(sport_slug) or self._titleize_slug(sport_slug),
                }
                for sport_slug in sorted(selected_sports)
                if sport_slug
            ]
            league_options.sort(key=lambda item: item.get("label") or item.get("value") or "")

            return {
                "sports": sport_options,
                "leagues": league_options,
                "source": "teamarr_subscription",
            }
        except Exception as exc:
            logger.debug("Teamarr preflight filter options fell back to managed events: %s", exc)
            fallback["error"] = "Teamarr subscription options unavailable"
            return fallback

    @staticmethod
    def _event_filter_options(events: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
        sports = set()
        leagues = set()
        for event in events:
            sport = str(event.get("sport") or "").strip().lower()
            league = str(event.get("league") or "").strip().lower()
            if sport:
                sports.add(sport)
            if league:
                leagues.add(league)
        return {
            "sports": [{"value": value, "label": TeamarrPreflightService._titleize_slug(value)} for value in sorted(sports)],
            "leagues": [{"value": value, "label": value.upper()} for value in sorted(leagues)],
            "source": "events",
        }

    @staticmethod
    def _selected_league_slugs(subscription: Any) -> List[str]:
        if isinstance(subscription, dict):
            values = subscription.get("leagues") or subscription.get("selected_leagues") or []
        elif isinstance(subscription, list):
            values = subscription
        else:
            return []
        if not isinstance(values, list):
            return []

        selected = []
        def add_slug(raw_value: Any) -> None:
            slug = str(raw_value or "").strip().lower()
            if slug and slug not in selected:
                selected.append(slug)

        for value in values:
            if isinstance(value, dict):
                nested = value.get("leagues") or value.get("selected_leagues")
                if isinstance(nested, list):
                    for nested_value in nested:
                        add_slug(nested_value)
                    continue
                for key in ("slug", "league_slug", "league", "value", "id", "name"):
                    if value.get(key):
                        add_slug(value.get(key))
                        break
                continue
            add_slug(value)
        return selected

    @staticmethod
    def _sports_catalog_by_slug(payload: Any) -> Dict[str, str]:
        sports = payload.get("sports") if isinstance(payload, dict) else {}
        if isinstance(sports, dict):
            return {
                str(slug).strip().lower(): str(label).strip()
                for slug, label in sports.items()
                if str(slug).strip() and str(label).strip()
            }
        if isinstance(sports, list):
            by_slug = {}
            for sport in sports:
                if not isinstance(sport, dict):
                    continue
                slug = str(sport.get("slug") or sport.get("value") or sport.get("id") or "").strip().lower()
                label = str(sport.get("name") or sport.get("label") or slug).strip()
                if slug and label:
                    by_slug[slug] = label
            return by_slug
        return {}

    @staticmethod
    def _leagues_catalog_by_slug(payload: Any) -> Dict[str, Dict[str, Any]]:
        leagues = payload.get("leagues") if isinstance(payload, dict) else []
        if not isinstance(leagues, list):
            return {}
        by_slug = {}
        for league in leagues:
            if not isinstance(league, dict):
                continue
            slug = str(league.get("slug") or "").strip().lower()
            if slug:
                by_slug[slug] = league
        return by_slug

    @staticmethod
    def _titleize_slug(value: Any) -> str:
        text = str(value or "").replace("_", " ").replace("-", " ").replace(".", " ").strip()
        return " ".join(part.capitalize() for part in text.split()) if text else ""

    def _build_candidates(
        self,
        events: Iterable[Dict[str, Any]],
        config: Dict[str, Any],
        now: datetime,
    ) -> List[Dict[str, Any]]:
        candidates = []
        for event in events:
            candidate = self._public_event(event, now)
            if not candidate:
                continue
            state, bucket = self._classify_event(candidate, config, now)
            candidate["state"] = state
            candidate["trigger_bucket"] = bucket
            candidate["match_evidence"] = self._match_evidence(candidate, state=state, bucket=bucket)
            candidate["may_start_full_run"] = False
            candidate["next_automatic_check"] = self._next_automatic_check(
                candidate,
                config,
                state,
                bucket,
            )
            candidates.append(candidate)

        candidates.sort(key=self._candidate_sort_key)
        return candidates

    def _build_team_candidates(
        self,
        team_statuses: Iterable[Dict[str, Any]],
        config: Dict[str, Any],
        now: datetime,
    ) -> List[Dict[str, Any]]:
        candidates: List[Dict[str, Any]] = []
        for status in team_statuses:
            candidate = self._public_team(status, now)
            if not candidate:
                continue

            if not self._passes_filters(candidate, config):
                state, bucket = "filtered", None
            elif not candidate.get("dispatcharr_channel_id"):
                state, bucket = "no_dispatcharr_channel", None
            elif int(candidate.get("stream_count") or 0) <= 0:
                state, bucket = "no_streams_yet", None
            elif not candidate.get("event_date"):
                state, bucket = "no_live_window", None
            elif str(candidate.get("team_status") or "") != "ready":
                state, bucket = "incomplete_team", None
            elif not candidate.get("live_window_event_evidence"):
                state, bucket = "no_event_window", None
                missing = list(candidate.get("missing") or [])
                if "team_event_evidence" not in missing:
                    missing.append("team_event_evidence")
                candidate["missing"] = missing
            else:
                state, bucket = self._classify_event(candidate, config, now)

            candidate["state"] = state
            candidate["trigger_bucket"] = bucket
            candidate["match_evidence"] = self._match_evidence(candidate, state=state, bucket=bucket)
            candidate["may_start_full_run"] = False
            candidate["next_automatic_check"] = self._next_automatic_check(
                candidate,
                config,
                state,
                bucket,
            )
            candidates.append(candidate)

        candidates.sort(key=self._candidate_sort_key)
        return candidates

    @staticmethod
    def _team_status_summary(
        *,
        enabled: bool,
        seen: int,
        candidates: Iterable[Dict[str, Any]],
        error: Optional[str],
    ) -> Dict[str, Any]:
        candidate_list = list(candidates)
        return {
            "enabled": bool(enabled),
            "seen": int(seen),
            "ready": sum(1 for item in candidate_list if item.get("team_status") == "ready"),
            "incomplete": sum(1 for item in candidate_list if item.get("team_status") != "ready"),
            "queueable": sum(1 for item in candidate_list if item.get("state") in MANUAL_FORCE_ALLOWED_STATES),
            "last_error": error,
        }

    @staticmethod
    def _candidate_sort_key(event: Dict[str, Any]) -> tuple[int, int, str]:
        try:
            seconds = int(event.get("seconds_to_start"))
        except (TypeError, ValueError):
            seconds = 10**12
        state = str(event.get("state") or "")
        if state == "past":
            return (2, -seconds, str(event.get("event_name") or ""))
        if seconds < 0 and state not in {"due", "already_attempted"}:
            return (1, abs(seconds), str(event.get("event_name") or ""))
        return (0, seconds, str(event.get("event_name") or ""))

    def _public_event(self, event: Dict[str, Any], now: datetime) -> Optional[Dict[str, Any]]:
        event_at = _parse_event_datetime(_event_datetime_value(event))
        if event_at is None:
            return None
        channel_id = _event_dispatcharr_channel_id(event)
        try:
            dispatcharr_channel_id = int(channel_id)
        except (TypeError, ValueError):
            dispatcharr_channel_id = None

        seconds_to_start = int((event_at - now).total_seconds())
        return {
            "preflight_kind": "event",
            "identity": _event_identity(event),
            "teamarr_id": event.get("id"),
            "event_id": event.get("event_id"),
            "event_name": event.get("event_name") or event.get("channel_name") or "Managed Event",
            "channel_name": event.get("channel_name"),
            "dispatcharr_channel_id": dispatcharr_channel_id,
            "dispatcharr_uuid": event.get("dispatcharr_uuid"),
            "sport": event.get("sport"),
            "league": event.get("league"),
            "sync_status": event.get("sync_status"),
            "event_date": event_at.isoformat(),
            "seconds_to_start": seconds_to_start,
        }

    @staticmethod
    def _team_league(team: Dict[str, Any]) -> Any:
        primary = team.get("primary_league")
        if primary not in (None, ""):
            return primary
        leagues = team.get("leagues")
        if isinstance(leagues, list) and leagues:
            return leagues[0]
        return None

    @staticmethod
    def _team_identity(team: Dict[str, Any], event_date: Any, dispatcharr_channel: Dict[str, Any]) -> str:
        team_id = team.get("id")
        channel_ref = (
            dispatcharr_channel.get("uuid")
            or dispatcharr_channel.get("id")
            or team.get("channel_id")
            or ""
        )
        return f"team:{team_id}:{event_date or 'no-window'}:{channel_ref}"

    @staticmethod
    def _normalize_team_window_text(value: Any) -> str:
        text = re.sub(r"[\._-]+", " ", str(value or ""))
        return re.sub(r"\s+", " ", text).strip().casefold()

    @staticmethod
    def _static_team_live_window_has_event_evidence(
        team: Dict[str, Any],
        next_live_window: Dict[str, Any],
    ) -> bool:
        if not next_live_window.get("found"):
            return False
        if not next_live_window.get("start"):
            return False
        if next_live_window.get("is_live") is not True:
            return False

        title = TeamarrPreflightService._normalize_team_window_text(next_live_window.get("title"))
        sub_title = TeamarrPreflightService._normalize_team_window_text(next_live_window.get("sub_title"))
        raw_title = str(next_live_window.get("title") or "")
        raw_sub_title = str(next_live_window.get("sub_title") or "")
        if STATIC_TEAM_NON_EVENT_WINDOW_RE.search(raw_title) or STATIC_TEAM_NON_EVENT_WINDOW_RE.search(raw_sub_title):
            return False

        text_values = [value for value in (title, sub_title) if value]
        if not text_values:
            return False

        team_terms = {
            TeamarrPreflightService._normalize_team_window_text(team.get("team_name")),
            TeamarrPreflightService._normalize_team_window_text(team.get("team_abbrev")),
            TeamarrPreflightService._normalize_team_window_text(team.get("channel_id")),
        }
        team_terms = {value for value in team_terms if value}
        if text_values and all(value in team_terms for value in text_values):
            return False

        return bool(STATIC_TEAM_MATCHUP_RE.search(" ".join(text_values)))

    def _public_team(self, status: Dict[str, Any], now: datetime) -> Optional[Dict[str, Any]]:
        team = status.get("team") if isinstance(status.get("team"), dict) else {}
        if not team:
            return None
        dispatcharr_channel = (
            status.get("dispatcharr_channel")
            if isinstance(status.get("dispatcharr_channel"), dict)
            else {}
        )
        next_live_window = (
            status.get("next_live_window")
            if isinstance(status.get("next_live_window"), dict)
            else {}
        )
        event_at = _parse_event_datetime(next_live_window.get("start"))
        event_date = event_at.isoformat() if event_at else None
        seconds_to_start = int((event_at - now).total_seconds()) if event_at else None
        channel_id = dispatcharr_channel.get("id")
        try:
            dispatcharr_channel_id = int(channel_id)
        except (TypeError, ValueError):
            dispatcharr_channel_id = None

        team_name = team.get("team_name") or team.get("name") or "Static Team"
        league = self._team_league(team)
        return {
            "preflight_kind": "team",
            "identity": self._team_identity(team, event_date, dispatcharr_channel),
            "teamarr_team_id": team.get("id"),
            "team_name": team_name,
            "team_abbrev": team.get("team_abbrev"),
            "team_channel_id": team.get("channel_id"),
            "event_name": team_name,
            "channel_name": dispatcharr_channel.get("name") or team.get("channel_id"),
            "dispatcharr_channel_id": dispatcharr_channel_id,
            "dispatcharr_uuid": dispatcharr_channel.get("uuid"),
            "dispatcharr_tvg_id": dispatcharr_channel.get("tvg_id"),
            "stream_count": int(dispatcharr_channel.get("stream_count") or 0),
            "sport": team.get("sport"),
            "league": league,
            "sync_status": "ready" if str(status.get("status") or "") == "ready" else None,
            "event_date": event_date,
            "seconds_to_start": seconds_to_start,
            "team_status": status.get("status") or "incomplete",
            "missing": list(status.get("missing") or []),
            "live_window_event_evidence": self._static_team_live_window_has_event_evidence(
                team,
                next_live_window,
            ),
            "next_live_window": {
                key: value
                for key, value in next_live_window.items()
                if key in {"found", "start", "stop", "title", "sub_title", "is_live", "source"}
            },
            "xmltv_updated_at": status.get("xmltv_updated_at"),
        }

    @staticmethod
    def _match_evidence(
        event: Dict[str, Any],
        *,
        state: Optional[str] = None,
        bucket: Optional[str] = None,
    ) -> Dict[str, Any]:
        preflight_kind = event.get("preflight_kind") or "event"
        evidence = {
            "source": "teamarr_static_team" if preflight_kind == "team" else "teamarr_managed_event",
            "preflight_kind": preflight_kind,
            "identity": event.get("identity"),
            "teamarr_id_present": event.get("teamarr_id") not in (None, ""),
            "teamarr_team_id_present": event.get("teamarr_team_id") not in (None, ""),
            "event_id_present": event.get("event_id") not in (None, ""),
            "event_date": event.get("event_date"),
            "dispatcharr_channel_id": event.get("dispatcharr_channel_id"),
            "dispatcharr_uuid": event.get("dispatcharr_uuid"),
            "sync_status": event.get("sync_status"),
            "sport": event.get("sport"),
            "league": event.get("league"),
            "team_status": event.get("team_status"),
            "live_window_event_evidence": event.get("live_window_event_evidence"),
            "state": state or event.get("state"),
            "trigger_bucket": bucket if bucket is not None else event.get("trigger_bucket"),
            "may_start_full_run": False,
        }
        return {
            key: value
            for key, value in evidence.items()
            if value not in (None, "")
        }

    def _classify_event(
        self,
        event: Dict[str, Any],
        config: Dict[str, Any],
        now: datetime,
    ) -> tuple[str, Optional[str]]:
        if not self._passes_filters(event, config):
            return "filtered", None
        if not event.get("dispatcharr_channel_id"):
            return "no_dispatcharr_channel", None
        sync_status = str(event.get("sync_status") or "").lower()
        if sync_status and sync_status not in READY_SYNC_STATES:
            return "waiting_for_channel_sync", None

        seconds = int(event.get("seconds_to_start") or 0)
        post_start_grace_seconds = int(config["post_start_grace_minutes"]) * 60
        if seconds < -post_start_grace_seconds:
            return "past", None

        if seconds >= 0:
            preflight_offset = int(config["preflight_offset_minutes"])
            retry_offsets = [
                int(offset)
                for offset in list(config.get("retry_offsets_minutes") or [])
                if int(offset) <= preflight_offset
            ]
            offsets = [preflight_offset] + retry_offsets
            return self._classify_bucket_offsets(event, seconds, offsets, direction="pre")

        elapsed_seconds = abs(seconds)
        post_offsets = list(config.get("post_start_offsets_minutes") or [])
        return self._classify_bucket_offsets(event, elapsed_seconds, post_offsets, direction="post")

    @staticmethod
    def _next_automatic_check(
        event: Dict[str, Any],
        config: Dict[str, Any],
        state: str,
        bucket: Optional[str],
    ) -> Optional[Dict[str, Any]]:
        if state == "due":
            return {
                "label": "Due now",
                "bucket": bucket,
                "timestamp": None,
            }
        if state not in {"scheduled", "already_attempted"}:
            return None

        event_at = _parse_event_datetime(event.get("event_date"))
        if event_at is None:
            return None

        seconds = int(event.get("seconds_to_start") or 0)
        if seconds >= 0:
            preflight_offset = int(config["preflight_offset_minutes"])
            retry_offsets = [
                int(offset)
                for offset in list(config.get("retry_offsets_minutes") or [])
                if int(offset) <= preflight_offset
            ]
            offsets = sorted({preflight_offset, *retry_offsets}, reverse=True)
            next_offset = next((offset for offset in offsets if seconds > offset * 60), None)
            if next_offset is None:
                return None
            check_at = event_at.timestamp() - next_offset * 60
            return {
                "label": "Next auto check",
                "bucket": f"-{next_offset}m",
                "timestamp": datetime.fromtimestamp(check_at, tz=timezone.utc).isoformat(),
            }

        elapsed_seconds = abs(seconds)
        post_offsets = sorted(
            {
                int(offset)
                for offset in list(config.get("post_start_offsets_minutes") or [])
                if int(offset) > 0
            }
        )
        next_offset = next((offset for offset in post_offsets if elapsed_seconds < offset * 60), None)
        if next_offset is None:
            return None
        check_at = event_at.timestamp() + next_offset * 60
        return {
            "label": "Next auto check",
            "bucket": f"+{next_offset}m",
            "timestamp": datetime.fromtimestamp(check_at, tz=timezone.utc).isoformat(),
        }

    def _classify_bucket_offsets(
        self,
        event: Dict[str, Any],
        seconds: int,
        offsets: Iterable[int],
        *,
        direction: str,
    ) -> tuple[str, Optional[str]]:
        poll_window_seconds = max(1, int(self._config.get("poll_interval_seconds", 60)))
        normalized = sorted(
            {int(offset) for offset in offsets if int(offset) > 0},
            reverse=(direction == "pre"),
        )
        attempted_bucket: Optional[str] = None
        for offset in normalized:
            threshold_seconds = offset * 60
            if direction == "pre":
                is_due = threshold_seconds - poll_window_seconds <= seconds <= threshold_seconds
            else:
                is_due = threshold_seconds <= seconds
            if not is_due:
                continue

            bucket = f"{offset}m" if direction == "pre" else f"post+{offset}m"
            attempted_key = f"{event['identity']}:{bucket}"
            with self._lock:
                if attempted_key in self._attempted_buckets:
                    attempted_bucket = bucket
                    continue
            return "due", bucket

        if attempted_bucket:
            return "already_attempted", attempted_bucket
        return "scheduled", None

    def _passes_filters(self, event: Dict[str, Any], config: Dict[str, Any]) -> bool:
        sport = str(event.get("sport") or "").strip().lower()
        league = str(event.get("league") or "").strip().lower()

        include_sports = set(config.get("include_sports") or [])
        exclude_sports = set(config.get("exclude_sports") or [])
        include_leagues = set(config.get("include_leagues") or [])
        exclude_leagues = set(config.get("exclude_leagues") or [])

        if include_sports and sport not in include_sports:
            return False
        if sport and sport in exclude_sports:
            return False
        if include_leagues and league not in include_leagues:
            return False
        if league and league in exclude_leagues:
            return False
        return True

    @staticmethod
    def _manual_force_block_reason(event: Dict[str, Any]) -> Optional[str]:
        if not event.get("dispatcharr_channel_id"):
            return "no_dispatcharr_channel"

        state = str(event.get("state") or "").strip()
        if state in {"no_live_window", "no_streams_yet", "incomplete_team"}:
            return state
        if state in MANUAL_FORCE_ALLOWED_STATES:
            return None
        if state in MANUAL_FORCE_ERROR_MESSAGES:
            return state
        return "unavailable"

    def _launch_check(self, event: Dict[str, Any], config: Dict[str, Any], *, force: bool = False) -> bool:
        channel_id = event.get("dispatcharr_channel_id")
        if not channel_id:
            self._record_event("no_dispatcharr_channel", event, {})
            return False
        attempted_key = self._attempted_key(event)
        allow_manual_retry = force and event.get("trigger_bucket") == "manual"
        if not allow_manual_retry and self._is_attempted(attempted_key):
            self._record_event(
                "duplicate_preflight_skipped",
                event,
                {"bucket": event.get("trigger_bucket"), "reason": "already_attempted"},
            )
            return False
        defer_reason = self._active_work_defer_reason(config)
        if defer_reason:
            self._record_event(
                "deferred_automation_active" if defer_reason == "automation_active" else "deferred_stream_checker_active",
                event,
                {"bucket": event.get("trigger_bucket"), "reason": defer_reason},
            )
            return False
        if not self._channel_has_streams(channel_id):
            self._mark_attempted(event)
            self._record_event("no_streams_yet", event, {"bucket": event.get("trigger_bucket")})
            return False
        forced_profile_id = self._resolve_profile_id(config.get("forced_profile_id"))
        quality_profile_details = self._quality_profile_details(forced_profile_id)
        if config.get("queue_during_active_checks", True) and self._automation_active():
            self._set_stream_checker_event_gate(True, gate_name="teamarr_preflight_automation")
            return self._queue_check(event, config)
        if self._stream_checker_active():
            return self._queue_check(event, config)

        queue_due_to_capacity = False
        with self._lock:
            if len(self._active_checks) >= int(config["max_concurrent_checks"]):
                queue_due_to_capacity = True
            else:
                key = attempted_key
                if key in self._active_checks:
                    return False
                self._mark_attempted(event, key=key)
                self._active_checks[key] = {
                    "identity": event["identity"],
                    "preflight_kind": event.get("preflight_kind") or "event",
                    "event_name": event.get("event_name"),
                    "team_name": event.get("team_name"),
                    "team_abbrev": event.get("team_abbrev"),
                    "channel_name": event.get("channel_name"),
                    "dispatcharr_channel_id": channel_id,
                    "dispatcharr_uuid": event.get("dispatcharr_uuid"),
                    "bucket": event.get("trigger_bucket"),
                    "match_evidence": event.get("match_evidence"),
                    "may_start_full_run": False,
                    "forced_profile_id": forced_profile_id,
                    **quality_profile_details,
                    "started_at": self.clock(),
                }
                self._set_stream_checker_event_gate(True)

        if queue_due_to_capacity:
            if self._queue_check(event, config):
                logger.info(
                    "Teamarr preflight direct capacity full; queued channel_id=%s identity=%s",
                    channel_id,
                    event.get("identity"),
                )
                return True
            self._record_event("concurrency_limit", event, {"bucket": event.get("trigger_bucket")})
            return False

        thread = threading.Thread(
            target=self._run_check,
            args=(key, dict(event), dict(config)),
            daemon=True,
            name=f"TeamarrPreflight-{channel_id}",
        )
        self._record_event(
            "preflight_started",
            event,
            {
                "bucket": event.get("trigger_bucket"),
                "forced_profile_id": forced_profile_id,
                **quality_profile_details,
            },
        )
        thread.start()
        return True

    def _queue_check(self, event: Dict[str, Any], config: Dict[str, Any]) -> bool:
        channel_id = event.get("dispatcharr_channel_id")
        if not channel_id:
            return False

        forced_profile_id = self._resolve_profile_id(config.get("forced_profile_id"))
        quality_profile_details = self._quality_profile_details(forced_profile_id)
        checker = self.stream_checker_provider()
        attempted_key = self._attempted_key(event)
        preflight_kind = event.get("preflight_kind") or "event"
        metadata = {
            "source": "teamarr_preflight",
            "preflight_kind": preflight_kind,
            "program_name": event.get("event_name"),
            "is_epg_scheduled": True,
            "forced_profile_id": forced_profile_id,
            "trigger_bucket": event.get("trigger_bucket"),
            "attempted_key": attempted_key,
            "may_start_full_run": False,
            **quality_profile_details,
            "match_evidence": event.get("match_evidence") or self._match_evidence(event),
            "event": {
                "identity": event.get("identity"),
                "preflight_kind": preflight_kind,
                "teamarr_id": event.get("teamarr_id"),
                "teamarr_team_id": event.get("teamarr_team_id"),
                "event_id": event.get("event_id"),
                "event_name": event.get("event_name"),
                "team_name": event.get("team_name"),
                "team_abbrev": event.get("team_abbrev"),
                "event_date": event.get("event_date"),
                "channel_name": event.get("channel_name"),
                "dispatcharr_channel_id": event.get("dispatcharr_channel_id"),
                "dispatcharr_uuid": event.get("dispatcharr_uuid"),
                "sport": event.get("sport"),
                "league": event.get("league"),
                "seconds_to_start": event.get("seconds_to_start"),
                "trigger_bucket": event.get("trigger_bucket"),
                "match_evidence": event.get("match_evidence") or self._match_evidence(event),
                "may_start_full_run": False,
                **quality_profile_details,
            },
        }
        queued = bool(checker.queue_channel(
            int(channel_id),
            priority=TEAMARR_PREFLIGHT_QUEUE_PRIORITY,
            force_check=True,
            metadata=metadata,
        ))
        if queued:
            self._mark_attempted(event)
            self._record_event(
                "preflight_queued",
                event,
                {
                    "bucket": event.get("trigger_bucket"),
                    "priority": TEAMARR_PREFLIGHT_QUEUE_PRIORITY,
                    **quality_profile_details,
                },
            )
        return queued

    def record_queued_check_result(self, metadata: Dict[str, Any], result: Any) -> None:
        """Record the eventual result of a Teamarr preflight check run through the queue."""
        event = dict(metadata.get("event") or {})
        if not event:
            event = {
                "identity": metadata.get("identity"),
                "preflight_kind": metadata.get("preflight_kind") or "event",
                "teamarr_id": metadata.get("teamarr_id"),
                "teamarr_team_id": metadata.get("teamarr_team_id"),
                "event_id": metadata.get("event_id"),
                "event_name": metadata.get("program_name"),
                "team_name": metadata.get("team_name"),
                "team_abbrev": metadata.get("team_abbrev"),
                "event_date": metadata.get("event_date"),
                "channel_name": metadata.get("channel_name"),
                "dispatcharr_channel_id": metadata.get("dispatcharr_channel_id"),
                "dispatcharr_uuid": metadata.get("dispatcharr_uuid"),
                "sport": metadata.get("sport"),
                "league": metadata.get("league"),
                "seconds_to_start": metadata.get("seconds_to_start"),
            }
        event.setdefault("preflight_kind", metadata.get("preflight_kind") or "event")
        event["trigger_bucket"] = metadata.get("trigger_bucket")
        if not event.get("match_evidence"):
            event["match_evidence"] = metadata.get("match_evidence") or self._match_evidence(event)
        event["may_start_full_run"] = False

        result = result if isinstance(result, dict) else {}
        quality_profile_details = {
            "quality_profile_id": (
                result.get("automation_profile_id")
                or metadata.get("quality_profile_id")
                or metadata.get("forced_profile_id")
            ),
            "quality_profile_name": (
                result.get("automation_profile_name")
                or metadata.get("quality_profile_name")
            ),
        }
        quality_profile_details = {
            key: value for key, value in quality_profile_details.items() if value not in (None, "")
        }
        deferral_reason = self._controlled_deferral_reason(result)
        if deferral_reason:
            attempted_key = str(metadata.get("attempted_key") or "").strip()
            if attempted_key:
                self._clear_attempted(attempted_key)
            self._record_event(
                "preflight_deferred",
                event,
                {
                    "bucket": metadata.get("trigger_bucket"),
                    "reason": deferral_reason,
                    "stats": self._public_check_stats(result.get("stats")),
                    **quality_profile_details,
                },
            )
            return

        event_type = "preflight_completed" if result.get("success") else "preflight_failed"
        self._record_event(
            event_type,
            event,
            {
                "bucket": metadata.get("trigger_bucket"),
                "error": "Preflight check failed" if result.get("error") else None,
                "reason": result.get("reason"),
                "stats": self._public_check_stats(result.get("stats")),
                **quality_profile_details,
            },
        )

    def _run_check(self, key: str, event: Dict[str, Any], config: Dict[str, Any]) -> None:
        try:
            checker = self.stream_checker_provider()
            forced_profile_id = self._resolve_profile_id(config.get("forced_profile_id"))
            quality_profile_details = self._quality_profile_details(forced_profile_id)
            result = checker.check_single_channel(
                int(event["dispatcharr_channel_id"]),
                program_name=event.get("event_name"),
                is_epg_scheduled=True,
                forced_profile_id=forced_profile_id,
                force_check=True,
            )
            deferral_reason = self._controlled_deferral_reason(result)
            if deferral_reason:
                self._clear_attempted(key)
                self._finish_active_check(key)
                self._record_event(
                    "preflight_deferred",
                    event,
                    {
                        "bucket": event.get("trigger_bucket"),
                        "reason": deferral_reason,
                        "stats": self._public_check_stats(result.get("stats")),
                        **quality_profile_details,
                    },
                )
                return

            event_type = "preflight_completed" if result.get("success") else "preflight_failed"
            self._finish_active_check(key)
            self._record_event(
                event_type,
                event,
                {
                    "bucket": event.get("trigger_bucket"),
                    "error": "Preflight check failed" if result.get("error") else None,
                    "reason": result.get("reason"),
                    "stats": self._public_check_stats(result.get("stats")),
                    **quality_profile_details,
                },
            )
        except Exception as exc:
            logger.error(f"Teamarr preflight check failed for channel {event.get('dispatcharr_channel_id')}: {exc}", exc_info=True)
            self._finish_active_check(key)
            self._record_event("preflight_failed", event, {
                "error": "Teamarr preflight check failed",
                "bucket": event.get("trigger_bucket"),
            })
        finally:
            self._finish_active_check(key)

    def _finish_active_check(self, key: str) -> None:
        with self._lock:
            self._active_checks.pop(key, None)
            has_active_checks = bool(self._active_checks)
        if not has_active_checks:
            self._set_stream_checker_event_gate(False, gate_name="teamarr_preflight_direct")
        self._purge_old_attempts()

    def _set_stream_checker_event_gate(self, active: bool, *, gate_name: str = "teamarr_preflight_direct") -> None:
        try:
            checker = self.stream_checker_provider()
            setter = getattr(checker, "set_specialized_queue_gate", None)
            if callable(setter):
                setter(gate_name, bool(active))
        except Exception as exc:
            logger.debug("Unable to update Stream Checker event queue gate: %s", exc)

    def _resolve_profile_id(self, profile_id: Any) -> Optional[str]:
        requested = str(profile_id or "").strip()
        if requested:
            try:
                automation_config = self.automation_config_provider()
                if automation_config.get_profile(requested):
                    return requested
                logger.warning(
                    "Teamarr preflight profile id=%s is unavailable; falling back to the default profile",
                    requested,
                )
            except Exception as exc:
                logger.debug("Could not verify Teamarr preflight profile id=%s: %s", requested, exc)
                return requested

        self._ensure_default_profile()
        with self._lock:
            return self._default_profile_id or None

    def _active_work_defer_reason(self, config: Dict[str, Any]) -> Optional[str]:
        if config.get("queue_during_active_checks", True):
            return None
        if self._automation_active():
            return "automation_active"
        if self._stream_checker_active():
            return "stream_checker_active"
        return None

    def _sync_automation_queue_gate(self, config: Dict[str, Any]) -> bool:
        automation_queue_active = bool(
            config.get("queue_during_active_checks", True)
            and self._automation_active()
        )
        self._set_stream_checker_event_gate(
            automation_queue_active,
            gate_name="teamarr_preflight_automation",
        )
        return automation_queue_active

    def _automation_active(self) -> bool:
        try:
            automation_status = self.automation_status_provider() or {}
        except Exception as exc:
            logger.warning(f"Teamarr preflight could not read automation status: {exc}")
            return True

        return self._automation_status_indicates_active_run(automation_status)

    @classmethod
    def _automation_status_indicates_active_run(cls, automation_status: Any) -> bool:
        if not isinstance(automation_status, dict):
            return False

        if automation_status.get("active") is True:
            return True

        state = str(automation_status.get("state") or automation_status.get("status") or "").lower()
        if state == "running":
            return True

        for key in ("run_status", "run_progress"):
            if cls._automation_status_indicates_active_run(automation_status.get(key)):
                return True

        return False

    def _stream_checker_active(self) -> bool:
        try:
            checker = self.stream_checker_provider()
            status = checker.get_status() if checker else {}
        except Exception as exc:
            logger.warning(f"Teamarr preflight could not read stream checker status: {exc}")
            return True

        queue_status = status.get("queue") or {}
        return bool(
            status.get("stream_checking_mode")
            or queue_status.get("queue_size", 0)
            or queue_status.get("in_progress", 0)
        )

    def _channel_has_streams(self, channel_id: int) -> bool:
        try:
            udi = self.udi_provider()
            channel_id = int(channel_id)
            streams = udi.get_channel_streams(channel_id) or []
            if streams:
                return True
            if hasattr(udi, "refresh_channel_by_id"):
                try:
                    udi.refresh_channel_by_id(channel_id)
                    streams = udi.get_channel_streams(channel_id) or []
                except Exception as exc:
                    logger.warning(f"Teamarr preflight could not refresh channel {channel_id}: {exc}")
            return len(streams) > 0
        except Exception as exc:
            logger.warning(f"Teamarr preflight could not read streams for channel {channel_id}: {exc}")
            return False

    @staticmethod
    def _attempted_key(event: Dict[str, Any]) -> str:
        return f"{event['identity']}:{event.get('trigger_bucket') or 'manual'}"

    def _is_attempted(self, key: str) -> bool:
        with self._lock:
            return key in self._attempted_buckets

    def _mark_attempted(self, event: Dict[str, Any], *, key: Optional[str] = None) -> None:
        attempted_key = key or self._attempted_key(event)
        with self._lock:
            self._attempted_buckets[attempted_key] = self.clock()

    def _clear_attempted(self, key: str) -> None:
        with self._lock:
            self._attempted_buckets.pop(key, None)

    def _purge_old_attempts(self) -> None:
        cutoff = self.clock() - int(self._config.get("event_cooldown_minutes", 720)) * 60
        for key, timestamp in list(self._attempted_buckets.items()):
            if timestamp < cutoff:
                self._attempted_buckets.pop(key, None)

    def _record_event(self, event_type: str, event: Dict[str, Any], details: Dict[str, Any]) -> None:
        details = dict(details or {})
        match_evidence = event.get("match_evidence") or details.get("match_evidence") or self._match_evidence(event)
        details.setdefault("match_evidence", match_evidence)
        details.setdefault("may_start_full_run", False)
        payload = {
            "timestamp": self.clock(),
            "type": event_type,
            "preflight_kind": event.get("preflight_kind") or "event",
            "identity": event.get("identity"),
            "teamarr_id": event.get("teamarr_id"),
            "teamarr_team_id": event.get("teamarr_team_id"),
            "event_id": event.get("event_id"),
            "event_name": event.get("event_name"),
            "team_name": event.get("team_name"),
            "team_abbrev": event.get("team_abbrev"),
            "event_date": event.get("event_date"),
            "channel_name": event.get("channel_name"),
            "dispatcharr_channel_id": event.get("dispatcharr_channel_id"),
            "dispatcharr_uuid": event.get("dispatcharr_uuid"),
            "sport": event.get("sport"),
            "league": event.get("league"),
            "seconds_to_start": event.get("seconds_to_start"),
            "match_evidence": match_evidence,
            "may_start_full_run": False,
            "details": details,
        }
        with self._lock:
            self._events.appendleft(payload)
        logger.info(
            "Teamarr preflight event=%s channel_id=%s identity=%s",
            event_type,
            payload.get("dispatcharr_channel_id"),
            payload.get("identity"),
        )

    @staticmethod
    def _controlled_deferral_reason(result: Any) -> Optional[str]:
        if not isinstance(result, dict):
            return None

        details = result.get("details") if isinstance(result.get("details"), dict) else {}
        reason = (
            result.get("reason")
            or result.get("skip_reason")
            or result.get("error")
            or details.get("skip_reason")
            or details.get("reason")
        )
        if reason in CONTROLLED_CHECK_DEFERRAL_REASONS:
            return str(reason)
        return None

    @staticmethod
    def _public_check_stats(stats: Any) -> Optional[Dict[str, Any]]:
        if not isinstance(stats, dict):
            return None

        allowed_keys = {
            "avg_bitrate",
            "avg_fps",
            "avg_resolution",
            "blank_streams",
            "dead_streams",
            "duration",
            "duration_seconds",
            "failed",
            "freeze_streams",
            "loop_streams",
            "successful",
            "streams_analyzed",
            "total_streams",
        }
        return {
            key: value
            for key, value in stats.items()
            if key in allowed_keys and isinstance(value, (str, int, float, bool, type(None)))
        }

    @staticmethod
    def _default_stream_checker_provider() -> Any:
        from apps.stream.stream_checker_service import get_stream_checker_service

        return get_stream_checker_service()

    @staticmethod
    def _default_automation_config_provider() -> Any:
        from apps.automation.automation_config_manager import get_automation_config_manager

        return get_automation_config_manager()

    @staticmethod
    def _automation_status_from_module(module: Any) -> Optional[Dict[str, Any]]:
        manager = getattr(module, "automation_manager", None) if module is not None else None
        if manager is None and module is not None:
            manager_factory = getattr(module, "get_automation_manager", None)
            if callable(manager_factory):
                manager = manager_factory()
        getter = getattr(manager, "get_run_status", None)
        if callable(getter):
            return getter() or {}
        return None

    @staticmethod
    def _default_automation_status_provider() -> Dict[str, Any]:
        fallback_status: Optional[Dict[str, Any]] = None
        seen_modules = set()

        def consider_module(module: Any) -> Optional[Dict[str, Any]]:
            nonlocal fallback_status
            if module is None:
                return None
            module_id = id(module)
            if module_id in seen_modules:
                return None
            seen_modules.add(module_id)

            status = TeamarrPreflightService._automation_status_from_module(module)
            if status is None:
                return None
            if fallback_status is None:
                fallback_status = status
            if TeamarrPreflightService._automation_status_indicates_active_run(status):
                return status
            return None

        try:
            for module_name in ("apps.api.web_api", "web_api", "__main__"):
                status = consider_module(sys.modules.get(module_name))
                if status is not None:
                    return status

            from apps.api import web_api

            status = consider_module(web_api)
            if status is not None:
                return status
        except Exception as exc:
            logger.debug("Could not read global automation run status: %s", exc)
        return fallback_status or {}


_teamarr_preflight_instance: Optional[TeamarrPreflightService] = None
_teamarr_preflight_lock = threading.Lock()


def get_teamarr_preflight_service() -> TeamarrPreflightService:
    global _teamarr_preflight_instance
    with _teamarr_preflight_lock:
        if _teamarr_preflight_instance is None:
            _teamarr_preflight_instance = TeamarrPreflightService()
        return _teamarr_preflight_instance
