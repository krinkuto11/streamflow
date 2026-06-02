"""Teamarr managed-event preflight checks.

This service watches Teamarr-managed event channels and starts a targeted
StreamFlow single-channel check shortly before an event begins. Teamarr remains
the source of truth for event/channel identity; StreamFlow only scores the
already-created channel.
"""

from __future__ import annotations

import json
import os
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

READY_SYNC_STATES = {"in_sync", "synced", "ready"}
DEFAULT_TEAMARR_PREFLIGHT_PROFILE_NAME = "Teamarr Event Preflight"
CONTROLLED_CHECK_DEFERRAL_REASONS = {
    "active_viewers",
    "max_streams_reached",
    "connectivity_guard",
}
MANUAL_FORCE_ALLOWED_STATES = {"due", "scheduled", "already_attempted"}
MANUAL_FORCE_ERROR_MESSAGES = {
    "event_not_found": "Managed event was not found",
    "filtered": "Managed event is filtered by the current preflight configuration",
    "no_dispatcharr_channel": "Managed event has no Dispatcharr channel yet",
    "past": "Managed event is outside the post-start grace window",
    "waiting_for_channel_sync": "Managed event channel is still syncing",
    "unavailable": "Managed event is not available for manual preflight",
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
}

DEFAULT_CONFIG: Dict[str, Any] = {
    "enabled": False,
    "teamarr_base_url": "",
    "api_key": "",
    "api_key_header": "X-API-Key",
    "poll_interval_seconds": 60,
    "preflight_offset_minutes": 20,
    "retry_offsets_minutes": [10, 3],
    "post_start_grace_minutes": 5,
    "max_concurrent_checks": 1,
    "event_cooldown_minutes": 720,
    "skip_during_quality_check": True,
    "forced_profile_id": "",
    "include_sports": [],
    "exclude_sports": [],
    "include_leagues": [],
    "exclude_leagues": [],
}
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


def _normalize_retry_offsets(value: Any) -> List[int]:
    offsets: List[int] = []
    for item in _coerce_list(value):
        try:
            parsed = int(item)
        except (TypeError, ValueError):
            continue
        if 1 <= parsed <= 360:
            offsets.append(parsed)
    return sorted(set(offsets), reverse=True)


def normalize_config(payload: Optional[Dict[str, Any]], current: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    config = dict(DEFAULT_CONFIG)
    if current:
        config.update({key: value for key, value in current.items() if key in CONFIG_KEYS})
    if payload:
        config.update({key: value for key, value in payload.items() if key in CONFIG_KEYS})

    for key, bounds in INT_BOUNDS.items():
        config[key] = _coerce_int(config.get(key), DEFAULT_CONFIG[key], bounds)

    config["enabled"] = bool(config.get("enabled"))
    config["skip_during_quality_check"] = bool(config.get("skip_during_quality_check"))
    config["teamarr_base_url"] = str(config.get("teamarr_base_url") or "").strip().rstrip("/")
    config["api_key"] = str(config.get("api_key") or "").strip()
    config["api_key_header"] = str(config.get("api_key_header") or DEFAULT_CONFIG["api_key_header"]).strip()[:80]
    if not config["api_key_header"]:
        config["api_key_header"] = DEFAULT_CONFIG["api_key_header"]
    config["forced_profile_id"] = str(config.get("forced_profile_id") or "").strip()
    config["retry_offsets_minutes"] = _normalize_retry_offsets(config.get("retry_offsets_minutes"))
    for key in ("include_sports", "exclude_sports", "include_leagues", "exclude_leagues"):
        config[key] = _normalize_terms(config.get(key))
    return config


def public_config(config: Dict[str, Any], metadata: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    visible = dict(config)
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


def _event_identity(event: Dict[str, Any]) -> str:
    for key in ("id", "event_id"):
        value = event.get(key)
        if value not in (None, ""):
            return f"{key}:{value}:{event.get('event_date') or ''}"
    return f"channel:{event.get('dispatcharr_channel_id')}:{event.get('event_date') or ''}:{event.get('event_name') or ''}"


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
        self._attempted_buckets: Dict[str, float] = {}
        self._active_checks: Dict[str, Dict[str, Any]] = {}
        self._last_scan_at: Optional[float] = None
        self._last_error: Optional[str] = None

    def _load_config(self) -> Dict[str, Any]:
        try:
            if self.config_file.exists():
                with open(self.config_file, "r", encoding="utf-8") as handle:
                    return normalize_config(json.load(handle))
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
        if thread and thread.is_alive():
            thread.join(timeout=5)
        return True

    def get_status(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "enabled": bool(self._config.get("enabled")),
                "running": bool(self._thread and self._thread.is_alive()),
                "last_scan_at": self._last_scan_at,
                "last_error": self._last_error,
                "active_checks": list(self._active_checks.values()),
                "upcoming_events": list(self._upcoming),
                "recent_events": list(self._events)[:25],
                "config": public_config(self._config, self._default_profile_metadata()),
            }

    def run_once(self, *, force: bool = False) -> Dict[str, Any]:
        config = self.get_config(include_secret=True)
        if not config.get("enabled") and not force:
            return {"success": True, "skipped": True, "reason": "disabled"}

        try:
            raw_events = self._fetch_managed_events(config)
            now = datetime.fromtimestamp(self.clock(), tz=timezone.utc)
            candidates = self._build_candidates(raw_events, config, now)
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

            with self._lock:
                self._last_scan_at = self.clock()
                self._last_error = None
                self._upcoming = candidates[:50]

            return {
                "success": True,
                "events_seen": len(raw_events),
                "candidates": len(candidates),
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
            raw_events = self._fetch_managed_events(config)
            now = datetime.fromtimestamp(self.clock(), tz=timezone.utc)
            candidates = self._build_candidates(raw_events, config, now)
            target = next(
                (
                    event
                    for event in candidates
                    if str(event.get("identity") or "") == requested_identity
                ),
                None,
            )

            with self._lock:
                self._last_scan_at = self.clock()
                self._last_error = None
                self._upcoming = candidates[:50]

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
        base_url = str(config.get("teamarr_base_url") or "").rstrip("/")
        if not base_url:
            raise ValueError("Teamarr base URL is required")

        headers: Dict[str, str] = {}
        api_key = str(config.get("api_key") or "")
        if api_key:
            headers[str(config.get("api_key_header") or DEFAULT_CONFIG["api_key_header"])] = api_key

        response = self.http_get(
            f"{base_url}/api/v1/channels/managed",
            headers=headers,
            timeout=20,
        )
        response.raise_for_status()
        payload = response.json()
        if isinstance(payload, dict):
            for key in ("items", "channels", "data", "results"):
                value = payload.get(key)
                if isinstance(value, list):
                    return [item for item in value if isinstance(item, dict)]
            return []
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]
        return []

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
            candidates.append(candidate)

        candidates.sort(key=lambda item: item.get("seconds_to_start", 10**12))
        return candidates

    def _public_event(self, event: Dict[str, Any], now: datetime) -> Optional[Dict[str, Any]]:
        event_at = _parse_event_datetime(event.get("event_date"))
        if event_at is None:
            return None
        channel_id = event.get("dispatcharr_channel_id")
        try:
            dispatcharr_channel_id = int(channel_id)
        except (TypeError, ValueError):
            dispatcharr_channel_id = None

        seconds_to_start = int((event_at - now).total_seconds())
        return {
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
        if seconds < -int(config["post_start_grace_minutes"]) * 60:
            return "past", None

        offsets = [int(config["preflight_offset_minutes"])] + list(config.get("retry_offsets_minutes") or [])
        offsets = sorted(set(offsets), reverse=True)
        for offset in offsets:
            if seconds <= offset * 60:
                bucket = f"{offset}m"
                attempted_key = f"{event['identity']}:{bucket}"
                with self._lock:
                    if attempted_key in self._attempted_buckets:
                        return "already_attempted", bucket
                return "due", bucket

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
        if self._automation_or_quality_checker_active(config):
            self._record_event("deferred_automation_active", event, {"bucket": event.get("trigger_bucket")})
            return False
        if not self._channel_has_streams(channel_id):
            self._mark_attempted(event)
            self._record_event("no_streams_yet", event, {"bucket": event.get("trigger_bucket")})
            return False

        with self._lock:
            if len(self._active_checks) >= int(config["max_concurrent_checks"]):
                self._record_event("concurrency_limit", event, {"bucket": event.get("trigger_bucket")})
                return False
            key = f"{event['identity']}:{event.get('trigger_bucket') or 'manual'}"
            if key in self._active_checks:
                return False
            self._mark_attempted(event, key=key)
            self._active_checks[key] = {
                "identity": event["identity"],
                "event_name": event.get("event_name"),
                "channel_name": event.get("channel_name"),
                "dispatcharr_channel_id": channel_id,
                "bucket": event.get("trigger_bucket"),
                "started_at": self.clock(),
            }

        thread = threading.Thread(
            target=self._run_check,
            args=(key, dict(event), dict(config)),
            daemon=True,
            name=f"TeamarrPreflight-{channel_id}",
        )
        self._record_event("preflight_started", event, {"bucket": event.get("trigger_bucket")})
        thread.start()
        return True

    def _run_check(self, key: str, event: Dict[str, Any], config: Dict[str, Any]) -> None:
        try:
            checker = self.stream_checker_provider()
            forced_profile_id = self._resolve_profile_id(config.get("forced_profile_id"))
            result = checker.check_single_channel(
                int(event["dispatcharr_channel_id"]),
                program_name=event.get("event_name"),
                is_epg_scheduled=True,
                forced_profile_id=forced_profile_id,
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
            self._purge_old_attempts()

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

    def _automation_or_quality_checker_active(self, config: Dict[str, Any]) -> bool:
        if not config.get("skip_during_quality_check", True):
            return False
        try:
            automation_status = self.automation_status_provider() or {}
        except Exception as exc:
            logger.warning(f"Teamarr preflight could not read automation status: {exc}")
            return True

        if automation_status.get("active") or automation_status.get("state") == "running":
            return True

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
            streams = self.udi_provider().get_channel_streams(int(channel_id)) or []
            return len(streams) > 0
        except Exception as exc:
            logger.warning(f"Teamarr preflight could not read streams for channel {channel_id}: {exc}")
            return False

    def _mark_attempted(self, event: Dict[str, Any], *, key: Optional[str] = None) -> None:
        attempted_key = key or f"{event['identity']}:{event.get('trigger_bucket') or 'manual'}"
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
        payload = {
            "timestamp": self.clock(),
            "type": event_type,
            "identity": event.get("identity"),
            "event_name": event.get("event_name"),
            "channel_name": event.get("channel_name"),
            "dispatcharr_channel_id": event.get("dispatcharr_channel_id"),
            "sport": event.get("sport"),
            "league": event.get("league"),
            "seconds_to_start": event.get("seconds_to_start"),
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
        getter = getattr(manager, "get_run_status", None)
        if callable(getter):
            return getter() or {}
        return None

    @staticmethod
    def _default_automation_status_provider() -> Dict[str, Any]:
        try:
            for module_name in ("apps.api.web_api", "web_api", "__main__"):
                status = TeamarrPreflightService._automation_status_from_module(sys.modules.get(module_name))
                if status is not None:
                    return status

            from apps.api import web_api

            status = TeamarrPreflightService._automation_status_from_module(web_api)
            if status is not None:
                return status
        except Exception as exc:
            logger.debug("Could not read global automation run status: %s", exc)
        return {}


_teamarr_preflight_instance: Optional[TeamarrPreflightService] = None
_teamarr_preflight_lock = threading.Lock()


def get_teamarr_preflight_service() -> TeamarrPreflightService:
    global _teamarr_preflight_instance
    with _teamarr_preflight_lock:
        if _teamarr_preflight_instance is None:
            _teamarr_preflight_instance = TeamarrPreflightService()
        return _teamarr_preflight_instance
