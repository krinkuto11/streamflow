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
MAX_UPCOMING_EVENTS = 1000
TEAMARR_PREFLIGHT_QUEUE_PRIORITY = 100

READY_SYNC_STATES = {"in_sync", "synced", "ready"}
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
CONTROLLED_CHECK_DEFERRAL_REASONS = {
    "active_viewers",
    "max_streams_reached",
    "connectivity_guard",
}
MANUAL_FORCE_ALLOWED_STATES = {"due", "scheduled", "already_attempted", "past"}
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
    "post_start_offsets_minutes": [2, 4],
    "post_start_grace_minutes": 5,
    "max_concurrent_checks": 1,
    "event_cooldown_minutes": 720,
    "skip_during_quality_check": True,
    "provider_limit_override": False,
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
    config["provider_limit_override"] = bool(config.get("provider_limit_override"))
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
        self._filter_options: Dict[str, Any] = {"sports": [], "leagues": [], "source": "events"}
        self._attempted_buckets: Dict[str, float] = {}
        self._active_checks: Dict[str, Dict[str, Any]] = {}
        self._last_scan_at: Optional[float] = None
        self._last_error: Optional[str] = None
        self._last_events_seen = 0
        self._last_candidates_count = 0
        self._upcoming_truncated = False

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
            recent_events = list(self._events)[:25]
            upcoming_events = self._attach_recent_events_to_upcoming(self._upcoming, recent_events)
            return {
                "enabled": bool(self._config.get("enabled")),
                "running": bool(self._thread and self._thread.is_alive()),
                "last_scan_at": self._last_scan_at,
                "last_error": self._last_error,
                "active_checks": list(self._active_checks.values()),
                "upcoming_events": upcoming_events,
                "managed_events_seen": self._last_events_seen,
                "managed_candidates": self._last_candidates_count,
                "managed_events_returned": len(upcoming_events),
                "managed_events_truncated": self._upcoming_truncated,
                "managed_events_limit": MAX_UPCOMING_EVENTS,
                "recent_events": recent_events,
                "filter_options": dict(self._filter_options),
                "config": public_config(self._config, self._default_profile_metadata()),
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

    def run_once(self, *, force: bool = False) -> Dict[str, Any]:
        config = self.get_config(include_secret=True)
        if not config.get("enabled") and not force:
            return {"success": True, "skipped": True, "reason": "disabled"}

        try:
            raw_events = self._fetch_managed_events(config)
            now = datetime.fromtimestamp(self.clock(), tz=timezone.utc)
            candidates = self._build_candidates(raw_events, config, now)
            filter_options = self._build_filter_options(config, raw_events)
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
                self._upcoming = candidates[:MAX_UPCOMING_EVENTS]
                self._last_events_seen = len(raw_events)
                self._last_candidates_count = len(candidates)
                self._upcoming_truncated = len(candidates) > len(self._upcoming)
                self._filter_options = filter_options

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
            filter_options = self._build_filter_options(config, raw_events)
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
                self._upcoming = candidates[:MAX_UPCOMING_EVENTS]
                self._last_events_seen = len(raw_events)
                self._last_candidates_count = len(candidates)
                self._upcoming_truncated = len(candidates) > len(self._upcoming)
                self._filter_options = filter_options

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
            candidate["next_automatic_check"] = self._next_automatic_check(
                candidate,
                config,
                state,
                bucket,
            )
            candidates.append(candidate)

        candidates.sort(key=lambda item: item.get("seconds_to_start", 10**12))
        return candidates

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
        normalized = sorted(
            {int(offset) for offset in offsets if int(offset) > 0},
            reverse=(direction == "pre"),
        )
        attempted_bucket: Optional[str] = None
        for offset in normalized:
            threshold_seconds = offset * 60
            if direction == "pre":
                is_due = seconds <= threshold_seconds
            else:
                is_due = seconds >= threshold_seconds
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
        if self._automation_active(config):
            self._record_event("deferred_automation_active", event, {"bucket": event.get("trigger_bucket")})
            return False
        if not self._channel_has_streams(channel_id):
            self._mark_attempted(event)
            self._record_event("no_streams_yet", event, {"bucket": event.get("trigger_bucket")})
            return False
        if self._stream_checker_active():
            return self._queue_check(event, config)

        queue_due_to_capacity = False
        with self._lock:
            if len(self._active_checks) >= int(config["max_concurrent_checks"]):
                queue_due_to_capacity = True
            else:
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
        self._record_event("preflight_started", event, {"bucket": event.get("trigger_bucket")})
        thread.start()
        return True

    def _queue_check(self, event: Dict[str, Any], config: Dict[str, Any]) -> bool:
        channel_id = event.get("dispatcharr_channel_id")
        if not channel_id:
            return False

        forced_profile_id = self._resolve_profile_id(config.get("forced_profile_id"))
        checker = self.stream_checker_provider()
        bucket = event.get("trigger_bucket") or "manual"
        attempted_key = f"{event['identity']}:{bucket}"
        metadata = {
            "source": "teamarr_preflight",
            "program_name": event.get("event_name"),
            "is_epg_scheduled": True,
            "forced_profile_id": forced_profile_id,
            "trigger_bucket": event.get("trigger_bucket"),
            "attempted_key": attempted_key,
            "event": {
                "identity": event.get("identity"),
                "teamarr_id": event.get("teamarr_id"),
                "event_id": event.get("event_id"),
                "event_name": event.get("event_name"),
                "event_date": event.get("event_date"),
                "channel_name": event.get("channel_name"),
                "dispatcharr_channel_id": event.get("dispatcharr_channel_id"),
                "sport": event.get("sport"),
                "league": event.get("league"),
                "seconds_to_start": event.get("seconds_to_start"),
                "trigger_bucket": event.get("trigger_bucket"),
            },
        }
        if config.get("provider_limit_override"):
            metadata["provider_limit_override"] = True
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
                },
            )
        return queued

    def record_queued_check_result(self, metadata: Dict[str, Any], result: Any) -> None:
        """Record the eventual result of a Teamarr preflight check run through the queue."""
        event = dict(metadata.get("event") or {})
        if not event:
            event = {
                "identity": metadata.get("identity"),
                "teamarr_id": metadata.get("teamarr_id"),
                "event_id": metadata.get("event_id"),
                "event_name": metadata.get("program_name"),
                "event_date": metadata.get("event_date"),
                "channel_name": metadata.get("channel_name"),
                "dispatcharr_channel_id": metadata.get("dispatcharr_channel_id"),
                "sport": metadata.get("sport"),
                "league": metadata.get("league"),
                "seconds_to_start": metadata.get("seconds_to_start"),
            }
        event["trigger_bucket"] = metadata.get("trigger_bucket")

        result = result if isinstance(result, dict) else {}
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
            },
        )

    def _run_check(self, key: str, event: Dict[str, Any], config: Dict[str, Any]) -> None:
        try:
            checker = self.stream_checker_provider()
            forced_profile_id = self._resolve_profile_id(config.get("forced_profile_id"))
            result = checker.check_single_channel(
                int(event["dispatcharr_channel_id"]),
                program_name=event.get("event_name"),
                is_epg_scheduled=True,
                forced_profile_id=forced_profile_id,
                force_check=True,
                **({"provider_limit_override": True} if config.get("provider_limit_override") else {}),
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

    def _automation_active(self, config: Dict[str, Any]) -> bool:
        if not config.get("skip_during_quality_check", True):
            return False
        try:
            automation_status = self.automation_status_provider() or {}
        except Exception as exc:
            logger.warning(f"Teamarr preflight could not read automation status: {exc}")
            return True

        if automation_status.get("active") or automation_status.get("state") == "running":
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
            "teamarr_id": event.get("teamarr_id"),
            "event_id": event.get("event_id"),
            "event_name": event.get("event_name"),
            "event_date": event.get("event_date"),
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
