"""Active-viewer shadow blank monitor.

This service watches only channels that already have real active clients.  It
probes the channel proxy URL, never a provider stream URL, so an already-active
channel should receive only one extra local downstream client during a probe.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import queue
import re
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
    _probe_stream_for_loops,
)
from apps.udi import get_udi_manager

logger = setup_logging(__name__)

CONFIG_DIR = Path(os.environ.get("CONFIG_DIR", "/app/data"))
CONFIG_FILE = CONFIG_DIR / "shadow_blank_monitor_config.json"
MAX_EVENTS = 100
LOOP_SWITCH_REQUIRES_PRE_PROBE = True
LOOP_PENDING_PROBE_OK_MISS_TOLERANCE = 1
AGGREGATE_ONLY_VIEWER_GRACE_SECONDS = 2.0
WATCHER_API_KEY_REQUIRED_CODE = "watcher_api_key_required"
WATCHER_API_KEY_REQUIRED_MESSAGE = "Watcher API Key is required before Shadow Monitor can start."
SHADOW_MONITOR_LOOP_ERROR_MESSAGE = "Shadow monitor loop failed; see server logs."
SHADOW_MONITOR_SCAN_ERROR_MESSAGE = "Shadow monitor scan failed; see server logs."
NO_DECODABLE_FRAME_ERROR_PATTERNS = (
    "could not find codec parameters",
    "unspecified size",
    "output file does not contain any stream",
    "cannot determine format of input stream",
    "no decodable frames",
    "no frame could be decoded",
)
FFMPEG_FRAME_RE = re.compile(r"\bframe=\s*(?P<frames>\d+)")
OFFLINE_IMAGE_PHASH_RE = re.compile(r"^[0-9a-f]{16}$")
SILENCE_START_RE = re.compile(r"silence_start:\s*(?P<start>\d+(?:\.\d+)?)")
SILENCE_END_RE = re.compile(r"silence_end:\s*(?P<end>\d+(?:\.\d+)?)")
SILENCE_DURATION_RE = re.compile(r"silence_duration:\s*(?P<duration>\d+(?:\.\d+)?)")
AUDIO_MISSING_PATTERNS = (
    "matches no streams",
    "stream specifier",
    "no audio stream",
)
AUDIO_ERROR_PATTERNS = (
    "error while decoding stream",
    "audio decoding failed",
    "audio decode error",
    "invalid audio",
    "corrupt audio",
)
AUDIO_DECODER_RE = re.compile(r"^\[(?:aac|ac3|eac3|mp2|mp3float|opus|vorbis|flac|truehd|dca)\s+@")
AUDIO_DECODER_ERROR_PATTERNS = (
    "channel element",
    "audible artifact",
    "clipped noise gain",
    "input buffer exhausted",
    "error decoding",
    "decode error",
    "invalid",
    "not allocated",
)
FFMPEG_STREAM_AUDIO_RE = re.compile(r"^\s*Stream #\d+:\d+(?:\[[^\]]+\])?(?:\([^)]+\))?:\s*Audio:", re.IGNORECASE)
FFMPEG_STREAM_VIDEO_RE = re.compile(r"^\s*Stream #\d+:\d+(?:\[[^\]]+\])?(?:\([^)]+\))?:\s*Video:", re.IGNORECASE)
FFMPEG_STREAM_MAPPING_RE = re.compile(r"^\s*Stream mapping:", re.IGNORECASE)
FFMPEG_ZERO_AUDIO_SUMMARY_RE = re.compile(r"\baudio:\s*0KiB\b", re.IGNORECASE)
PROXY_OUTPUT_FORMAT_ALIASES = {
    "fmp4": "fmp4",
    "mp4": "fmp4",
    "mpegts": "mpegts",
    "ts": "mpegts",
}

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
    "no_decodable_frames_detection_enabled": True,
    "no_decodable_frames_min_duration_seconds": 10.0,
    "garbled_audio_detection_enabled": False,
    "garbled_audio_error_threshold": 3,
    "silent_audio_detection_enabled": False,
    "silent_audio_min_duration_seconds": 10.0,
    "silent_audio_noise_db": -50,
    "offline_image_detection_enabled": False,
    "offline_image_reference_hashes": [],
    "offline_image_hash_threshold": 4,
    "offline_image_capture_offset_seconds": 3,
    "loop_detection_enabled": False,
    "loop_probe_duration_seconds": 360,
    "next_stream_pre_probe_enabled": False,
    "next_stream_pre_probe_duration_seconds": 8,
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
MEDIA_FAULT_RECOVERY_GUARD_BYPASS_REASONS = {
    "blank",
    "freeze",
    "offline_image",
    "garbled_audio",
    "silent_audio",
    "loop",
}
DETECTION_REASONS = {
    "blank",
    "freeze",
    "no_decodable_frames",
    "garbled_audio",
    "silent_audio",
    "offline_image",
    "loop",
}
VIDEO_FAULT_CONFIRMATION_REASONS = {
    "blank",
    "freeze",
}
VIDEO_FAULT_CONFIRMATION_KEY = "video_fault"
PENDING_EVENT_REASONS = {
    f"{reason}_pending": reason
    for reason in DETECTION_REASONS
}
DETECTION_MEASUREMENT_KEYS = {
    "blank": ("blank_ratio", "blank_duration_secs"),
    "freeze": ("freeze_ratio", "freeze_duration_secs"),
    "no_decodable_frames": (
        "no_decodable_frames_duration_secs",
        "no_decodable_frames_error",
    ),
    "garbled_audio": ("garbled_audio_error_count", "garbled_audio_error"),
    "silent_audio": ("silent_audio_duration_secs", "silent_audio_noise_db", "audio_stream_present"),
    "offline_image": ("offline_image_distance", "offline_image_hash"),
    "loop": ("loop_duration_secs", "loop_frames_processed"),
}
DETECTION_THRESHOLD_KEYS = {
    "blank": (
        "blank_min_duration_seconds",
        "blank_ratio_threshold",
        "blank_pixel_threshold",
    ),
    "freeze": (
        "freeze_min_duration_seconds",
        "freeze_ratio_threshold",
        "freeze_noise_threshold",
    ),
    "no_decodable_frames": ("no_decodable_frames_min_duration_seconds",),
    "garbled_audio": ("garbled_audio_error_threshold",),
    "silent_audio": (
        "silent_audio_min_duration_seconds",
        "silent_audio_noise_db",
    ),
    "offline_image": ("offline_image_hash_threshold",),
    "loop": ("loop_probe_duration_seconds",),
}
SWITCH_EVENT_TYPES = {"dry_run_switch", "switch_success", "switch_failed", "external_stream_change"}
PRE_PROBE_EVENT_TYPES = {"pre_probe_unavailable", "pre_probe_rejected"}
PRE_PROBE_METRICS = {
    "preprobe_attempted",
    "preprobe_success",
    "preprobe_rejected_media_fault",
    "preprobe_skipped_provider_limit",
    "preprobe_skipped_profile_limit",
    "preprobe_skipped_missing_url",
    "preprobe_timeout",
    "switch_prevented_by_preprobe",
}
GUARD_EVENT_TYPES = {
    "cooldown",
    "loop_pre_probe_required",
    "stale_stream_guard",
    "switch_rate_limited",
    "quality_check_active",
    "watcher_recovery_guard",
    "watcher_recovery_observed",
}

INT_BOUNDS = {
    "poll_interval_seconds": (5, 3600),
    "watch_gap_seconds": (1, 300),
    "probe_duration_seconds": (3, 120),
    "confirmation_count": (1, 5),
    "channel_cooldown_seconds": (30, 86400),
    "max_switches_per_hour": (1, 20),
    "max_concurrent_watchers": (1, 10),
    "garbled_audio_error_threshold": (1, 20),
    "silent_audio_noise_db": (-90, -20),
    "offline_image_hash_threshold": (0, 20),
    "offline_image_capture_offset_seconds": (0, 30),
    "loop_probe_duration_seconds": (60, 720),
    "next_stream_pre_probe_duration_seconds": (3, 60),
}

FLOAT_BOUNDS = {
    "blank_min_duration_seconds": (0.5, 30.0),
    "blank_pixel_threshold": (0.0, 1.0),
    "blank_ratio_threshold": (0.1, 1.0),
    "freeze_min_duration_seconds": (1.0, 120.0),
    "freeze_noise_threshold": (0.0, 1.0),
    "freeze_ratio_threshold": (0.1, 1.0),
    "no_decodable_frames_min_duration_seconds": (3.0, 60.0),
    "silent_audio_min_duration_seconds": (2.0, 60.0),
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


def _coerce_int_set(value: Any) -> set[int]:
    items: set[int] = set()
    for item in _coerce_list(value):
        try:
            items.add(int(item))
        except (TypeError, ValueError):
            continue
    return items


def _coerce_text_set(value: Any) -> set[str]:
    items: set[str] = set()
    for item in _coerce_list(value):
        text = str(item).strip()
        if text:
            items.add(text)
    return items


def _offline_image_hashes(value: Any) -> List[str]:
    seen = set()
    hashes: List[str] = []
    for item in _coerce_list(value):
        text = str(item).strip().lower()
        if not text or text in seen:
            continue
        seen.add(text)
        hashes.append(text)
    return hashes


def _valid_offline_image_hashes(value: Any) -> List[str]:
    return [
        item
        for item in _offline_image_hashes(value)
        if OFFLINE_IMAGE_PHASH_RE.match(item)
    ]


def _invalid_offline_image_hashes(value: Any) -> List[str]:
    return [
        item
        for item in _offline_image_hashes(value)
        if not OFFLINE_IMAGE_PHASH_RE.match(item)
    ]


def _offline_image_config_status(config: Dict[str, Any]) -> Dict[str, Any]:
    reference_hashes = _offline_image_hashes(config.get("offline_image_reference_hashes"))
    invalid_count = len(_invalid_offline_image_hashes(reference_hashes))
    valid_count = len(reference_hashes) - invalid_count
    warnings = []
    if config.get("offline_image_detection_enabled") and not reference_hashes:
        warnings.append("missing_reference_hash")
    if invalid_count:
        warnings.append("invalid_reference_hash")
    if config.get("offline_image_detection_enabled") and reference_hashes and valid_count == 0:
        warnings.append("no_valid_reference_hash")
    return {
        "enabled": bool(config.get("offline_image_detection_enabled")),
        "reference_count": len(reference_hashes),
        "valid_reference_count": valid_count,
        "invalid_reference_count": invalid_count,
        "hash_threshold": int(config.get("offline_image_hash_threshold", DEFAULT_CONFIG["offline_image_hash_threshold"])),
        "capture_offset_seconds": int(config.get("offline_image_capture_offset_seconds", DEFAULT_CONFIG["offline_image_capture_offset_seconds"])),
        "warnings": warnings,
    }


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
    config["no_decodable_frames_detection_enabled"] = bool(config.get("no_decodable_frames_detection_enabled"))
    config["garbled_audio_detection_enabled"] = bool(config.get("garbled_audio_detection_enabled"))
    config["silent_audio_detection_enabled"] = bool(config.get("silent_audio_detection_enabled"))
    config["offline_image_detection_enabled"] = bool(config.get("offline_image_detection_enabled"))
    config["loop_detection_enabled"] = bool(config.get("loop_detection_enabled"))
    config["next_stream_pre_probe_enabled"] = bool(config.get("next_stream_pre_probe_enabled"))
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
    config["offline_image_reference_hashes"] = _offline_image_hashes(config.get("offline_image_reference_hashes"))
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
        loop_probe: Optional[Callable[[str, Dict[str, Any]], Dict[str, Any]]] = None,
        stream_checker_provider: Optional[Callable[[], Any]] = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.config_file = config_file
        self.udi_provider = udi_provider
        self.switch_stream = switch_stream
        self._uses_default_switch_stream = switch_stream is change_channel_stream
        self.base_url_provider = base_url_provider or (lambda: get_dispatcharr_config().get_base_url())
        self.blank_probe = blank_probe or self._run_blank_probe
        self.loop_probe = loop_probe or self._run_loop_probe
        self._uses_default_blank_probe = blank_probe is None
        self.stream_checker_provider = stream_checker_provider or self._default_stream_checker_provider
        self.clock = clock

        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._config = self._load_config()
        self._events: deque[Dict[str, Any]] = deque(maxlen=MAX_EVENTS)
        self._watched: Dict[str, Dict[str, Any]] = {}
        self._last_excluded_active_targets: List[Dict[str, Any]] = []
        self._watcher_absences: Dict[str, Dict[str, Any]] = {}
        self._blank_counts: Dict[str, int] = defaultdict(int)
        self._detection_misses: Dict[str, int] = defaultdict(int)
        self._cooldowns: Dict[str, float] = {}
        self._switch_attempts: Dict[str, Dict[str, Any]] = {}
        self._switch_history: Dict[str, deque[float]] = defaultdict(deque)
        self._known_channel_ids_by_uuid: Dict[str, int] = {}
        self._known_channel_ids_by_stream_id: Dict[int, int] = {}
        self._pre_probe_metrics: Dict[str, int] = defaultdict(int)
        self._last_pre_probe: Optional[Dict[str, Any]] = None
        self._active_probes: set[str] = set()
        self._persistent_watchers: Dict[str, Dict[str, Any]] = {}
        self._last_loop_probe_started_at: Dict[str, float] = {}
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

    def learn_offline_image_from_current_frame(
        self,
        *,
        channel_ref: Optional[str] = None,
        enable_detection: bool = False,
    ) -> Dict[str, Any]:
        with self._lock:
            targets = list(self._watched.values())
            if channel_ref:
                targets = [
                    target
                    for target in targets
                    if str(target.get("channel_ref")) == str(channel_ref)
                ]
            target = dict(targets[0]) if targets else {}
            config = dict(self._config)

        if not target:
            return {
                "success": False,
                "learned": False,
                "reason": "no_watched_channel",
                "message": "No active Shadow-watched channel is available.",
            }

        channel_uuid = target.get("channel_uuid")
        if not channel_uuid:
            return {
                "success": False,
                "learned": False,
                "reason": "missing_channel_uuid",
                "channel_ref": target.get("channel_ref"),
            }

        capture = self._capture_offline_image_hash(
            self._channel_proxy_url(str(channel_uuid), target.get("viewer_output_format")),
            config,
        )
        if not capture.get("success"):
            return {
                "success": False,
                "learned": False,
                "reason": capture.get("reason") or "frame_capture_failed",
                "channel_ref": target.get("channel_ref"),
                "stream_ref": target.get("stream_ref"),
            }

        phash = str(capture.get("offline_image_hash") or "").lower()
        if not OFFLINE_IMAGE_PHASH_RE.match(phash):
            return {
                "success": False,
                "learned": False,
                "reason": "invalid_captured_hash",
                "channel_ref": target.get("channel_ref"),
                "stream_ref": target.get("stream_ref"),
            }

        with self._lock:
            existing_hashes = _offline_image_hashes(self._config.get("offline_image_reference_hashes"))
            valid_existing = _valid_offline_image_hashes(existing_hashes)
            threshold = int(self._config.get("offline_image_hash_threshold", 4))
            distances = [
                distance
                for distance in (self._hash_distance(phash, candidate) for candidate in valid_existing)
                if distance is not None
            ]
            nearest_distance = min(distances) if distances else None
            already_covered = nearest_distance is not None and nearest_distance <= threshold
            if enable_detection:
                self._config["offline_image_detection_enabled"] = True
            if not already_covered:
                self._config["offline_image_reference_hashes"] = existing_hashes + [phash]
                self._save_config()
            elif enable_detection:
                self._save_config()
            reference_count = len(self._config.get("offline_image_reference_hashes") or [])

        details = {
            "reason": "offline_image",
            "offline_image_hash": phash,
            "offline_image_distance": nearest_distance,
            "reference_count": reference_count,
            "deduplicated": already_covered,
        }
        self._record_event("offline_image_learned", target, details)
        return {
            "success": True,
            "learned": not already_covered,
            "deduplicated": already_covered,
            "channel_ref": target.get("channel_ref"),
            "stream_ref": target.get("stream_ref"),
            "offline_image_hash": phash,
            "offline_image_distance": nearest_distance,
            "reference_count": reference_count,
            "config": self.get_config(),
            "status": self.get_status(),
        }

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
            watcher_keys = list(self._persistent_watchers)
        if thread and thread.is_alive():
            thread.join(timeout=5)
        for channel_uuid in watcher_keys:
            self._stop_persistent_watcher(channel_uuid)
        return True

    def get_status(self) -> Dict[str, Any]:
        with self._lock:
            now = self.clock()
            issue = _watcher_configuration_issue(self._config)
            watched_channels = [self._public_target(target) for target in self._watched.values()]
            excluded_active_channels = [
                self._public_excluded_target(target)
                for target in self._last_excluded_active_targets
            ]
            cooldowns = []
            for channel_uuid, until in self._cooldowns.items():
                if until <= now:
                    continue
                target = self._watched.get(channel_uuid, {"channel_ref": _ref("channel", channel_uuid)})
                cooldowns.append({
                    "channel_ref": target.get("channel_ref"),
                    "cooldown_seconds": max(0, int(until - now)),
                })
            recent_events = list(self._events)
            loop_gate_status = self._loop_detection_gate_status(self._config)
            return {
                "enabled": bool(self._config.get("enabled")),
                "running": bool(self._thread and self._thread.is_alive()),
                "dry_run": bool(self._config.get("dry_run")),
                "watch_mode": self._config.get("watch_mode"),
                "freeze_detection_enabled": bool(self._config.get("freeze_detection_enabled")),
                "garbled_audio_detection_enabled": bool(self._config.get("garbled_audio_detection_enabled")),
                "silent_audio_detection_enabled": bool(self._config.get("silent_audio_detection_enabled")),
                "offline_image_detection_enabled": bool(self._config.get("offline_image_detection_enabled")),
                "next_stream_pre_probe_enabled": bool(self._config.get("next_stream_pre_probe_enabled")),
                "loop_detection_enabled": bool(self._config.get("loop_detection_enabled")),
                "loop_probe_duration_seconds": int(self._config.get("loop_probe_duration_seconds") or 0),
                "loop_switch_requires_pre_probe": LOOP_SWITCH_REQUIRES_PRE_PROBE,
                "loop_switch_gate_satisfied": bool(loop_gate_status.get("switch_gate_satisfied")),
                "loop_detection_gates": loop_gate_status,
                "configuration_required": bool(issue),
                "configuration_issue": issue["code"] if issue else None,
                "configuration_message": issue["message"] if issue else None,
                "has_watcher_api_key": bool(str(self._config.get("watcher_api_key") or "").strip()),
                "last_scan_at": self._last_scan_at,
                "last_error": self._last_error,
                "watched_count": len(watched_channels),
                "watched_channels": watched_channels,
                "excluded_active_count": len(excluded_active_channels),
                "excluded_active_channels": excluded_active_channels,
                "cooldowns": cooldowns,
                "recent_events": recent_events,
                "decision_history": recent_events,
                "switch_summary": self._switch_summary(recent_events, active_cooldowns=len(cooldowns)),
                "pre_probe": {
                    "metrics": dict(self._pre_probe_metrics),
                    "last": dict(self._last_pre_probe) if self._last_pre_probe else None,
                },
                "offline_image": _offline_image_config_status(self._config),
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

    def run_once(
        self,
        *,
        force: bool = False,
        include_channel_ids: Any = None,
        include_channel_uuids: Any = None,
    ) -> Dict[str, Any]:
        with self._lock:
            config = dict(self._config)
        issue = _watcher_configuration_issue(config)
        if issue:
            with self._lock:
                self._last_error = issue["message"]
            return self.get_status()
        if not config.get("enabled") and not force:
            return self.get_status()

        restore_stop_event = False
        if force and self._stop_event.is_set():
            # Manual scans are allowed while the background monitor is disabled.
            # The worker stop event must not abort the one-off continuous probe.
            self._stop_event.clear()
            restore_stop_event = True

        self._last_scan_at = self.clock()
        try:
            udi = self.udi_provider()
            targets = self.discover_active_targets(
                udi,
                config,
                include_channel_ids=include_channel_ids,
                include_channel_uuids=include_channel_uuids,
            )
            if not force:
                self._sync_persistent_watchers(targets, config)
            self._probe_targets(
                udi,
                targets[: config["max_concurrent_watchers"]],
                config,
                single_pass=force,
                allow_orphaned_watcher_reprobe=force and bool(include_channel_ids or include_channel_uuids),
            )
            self._last_error = None
        except Exception as exc:
            self._last_error = SHADOW_MONITOR_SCAN_ERROR_MESSAGE
            logger.error(f"Shadow blank monitor scan failed: {exc}", exc_info=True)
        finally:
            if restore_stop_event:
                with self._lock:
                    if not self._config.get("enabled"):
                        self._stop_event.set()
        return self.get_status()

    def discover_active_targets(
        self,
        udi: Any,
        config: Dict[str, Any],
        *,
        include_channel_ids: Any = None,
        include_channel_uuids: Any = None,
    ) -> List[Dict[str, Any]]:
        proxy_status = udi.get_proxy_status() or {}
        channels = udi.get_channels() if hasattr(udi, "get_channels") else []
        by_uuid, by_id = self._index_channels(channels)
        self._remember_channel_identities(channels)
        excluded_ids = {int(item) for item in config.get("excluded_channel_ids", [])}
        excluded_uuids = {str(item) for item in config.get("excluded_channel_uuids", [])}
        included_ids = _coerce_int_set(include_channel_ids)
        included_uuids = _coerce_text_set(include_channel_uuids)
        if len(included_ids) == 1 and len(included_uuids) == 1:
            self._remember_channel_identity({
                "id": next(iter(included_ids)),
                "uuid": next(iter(included_uuids)),
            })
        limit_to_included = bool(included_ids or included_uuids)

        targets: List[Dict[str, Any]] = []
        watched: Dict[str, Dict[str, Any]] = {}
        excluded_active_targets: List[Dict[str, Any]] = []
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
            active_probe_channels = set(self._active_probes)
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

            real_clients = self._real_client_count(raw_status, config)
            if real_clients <= 0:
                continue
            watcher_details = self._watcher_client_details(raw_status, config)
            watcher_clients = int(watcher_details.get("watcher_client_count") or 0)
            viewer_output_format = self._real_viewer_output_format(raw_status, config)

            stream_id = self._extract_stream_id(raw_status)
            numeric_id = self._resolve_channel_id(
                udi,
                self._extract_channel_id(channel, raw_status),
                channel_uuid=channel_uuid,
                channel=channel,
                status=raw_status,
                current_stream_id=stream_id,
                included_ids=included_ids,
                included_uuids=included_uuids,
                previous_target=previous_watched.get(channel_uuid),
            )
            if limit_to_included and numeric_id not in included_ids and channel_uuid not in included_uuids:
                continue

            current_program = self._current_epg_program(channel, raw_status, numeric_id)
            if numeric_id in excluded_ids or channel_uuid in excluded_uuids:
                excluded_target = {
                    "channel_uuid": channel_uuid,
                    "channel_id": numeric_id,
                    "channel_ref": _ref("channel", numeric_id or channel_uuid),
                    "channel_name": self._channel_display_name(channel, raw_status),
                    "stream_id": stream_id,
                    "stream_ref": _ref("stream", stream_id),
                    "real_client_count": real_clients,
                    "watcher_client_count": watcher_clients,
                    "state": raw_status.get("state") or "active",
                    "exclude_reason": "channel_excluded",
                }
                if viewer_output_format:
                    excluded_target["viewer_output_format"] = viewer_output_format
                if current_program:
                    excluded_target["current_program"] = current_program
                excluded_active_targets.append(excluded_target)
                continue
            target = {
                "channel_uuid": channel_uuid,
                "channel_id": numeric_id,
                "channel_ref": _ref("channel", numeric_id or channel_uuid),
                "channel_name": self._channel_display_name(channel, raw_status),
                "stream_id": stream_id,
                "stream_ref": _ref("stream", stream_id),
                "real_client_count": real_clients,
                "watcher_client_count": watcher_clients,
                "state": raw_status.get("state") or "active",
                "cooldown_seconds": self._cooldown_remaining(channel_uuid),
            }
            if viewer_output_format:
                target["viewer_output_format"] = viewer_output_format
            if current_program:
                target["current_program"] = current_program
            target.update(watcher_details)
            if continuous_mode:
                previous_target = previous_watched.get(channel_uuid) or {}
                probe_running = channel_uuid in active_probe_channels
                previous_probe_started = previous_target.get("active_probe_started_at")
                if probe_running and previous_probe_started is not None:
                    target["active_probe_started_at"] = previous_probe_started
                previous_absence = previous_absences.get(channel_uuid)
                previous_watcher_count = int(previous_target.get("watcher_client_count") or 0)
                previous_watcher_ref = previous_target.get("watcher_client_ref")
                watcher_ref = target.get("watcher_client_ref")
                previous_stream_id = previous_target.get("stream_id")
                if (
                    previous_stream_id is not None
                    and stream_id is not None
                    and str(previous_stream_id) != str(stream_id)
                ):
                    expected_shadow_switch = self._switch_attempt_matches(
                        channel_uuid,
                        previous_stream_id,
                        stream_id,
                    )
                    self._reset_detection_state(channel_uuid)
                    self._clear_cooldown(channel_uuid)
                    if not expected_shadow_switch:
                        continuity_events.append((
                            "external_stream_change",
                            {
                                **dict(target),
                                "stream_id": previous_stream_id,
                                "stream_ref": _ref("stream", previous_stream_id),
                            },
                            {
                                "target_stream_ref": _ref("stream", stream_id),
                                "observed_stream_ref": _ref("stream", stream_id),
                                "switch_source": "external",
                                "switch_actor": "unknown",
                                "switch_actor_note": (
                                    "Dispatcharr proxy status does not expose who changed "
                                    "the active stream."
                                ),
                                "stream_change_source": "dispatcharr_proxy_status",
                                "operator_note": (
                                    "Active stream changed outside the Shadow Monitor switch path. "
                                    "Dispatcharr did not provide actor details."
                                ),
                            },
                        ))
                if watcher_clients > 0:
                    target["watcher_state"] = "watching"
                    if probe_running:
                        next_absences.pop(channel_uuid, None)
                    elif previous_absence:
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
            self._last_excluded_active_targets = excluded_active_targets
            self._watcher_absences = next_absences if continuous_mode else {}
        for event_type, event_target, details in continuity_events:
            self._record_event(event_type, event_target, details)
        return targets

    def _probe_targets(
        self,
        udi: Any,
        targets: Iterable[Dict[str, Any]],
        config: Dict[str, Any],
        *,
        single_pass: bool = False,
        allow_orphaned_watcher_reprobe: bool = False,
    ) -> None:
        targets = list(targets)
        threads: List[threading.Thread] = []
        wait_for_probes = single_pass or not (
            config.get("watch_mode") == "continuous" and self._uses_default_blank_probe
        )
        for target in targets:
            channel_uuid = target["channel_uuid"]
            watcher_count = int(target.get("watcher_client_count") or 0)
            blocking_watcher_count = self._blocking_watcher_count(target, watcher_count)
            if blocking_watcher_count > 0:
                with self._lock:
                    probe_is_running = channel_uuid in self._active_probes
                if probe_is_running or not allow_orphaned_watcher_reprobe:
                    continue
                target["watcher_state"] = "orphaned"
                self._record_event(
                    "watcher_orphaned",
                    target,
                    {
                        "reason": "watcher_without_active_probe",
                        "watcher_client_count": watcher_count,
                        "blocking_watcher_count": blocking_watcher_count,
                        "action": "starting_recovery_probe",
                    },
                )
            if self._quality_checker_conflicts(target, config):
                self._record_event("quality_check_active", target, {})
                continue

            with self._lock:
                if channel_uuid in self._active_probes:
                    continue
                self._active_probes.add(channel_uuid)

            thread = threading.Thread(
                target=self._probe_target,
                args=(udi, target, dict(config), single_pass),
                name=f"ShadowBlankProbe-{channel_uuid[:8]}",
                daemon=True,
            )
            thread.start()
            threads.append(thread)

        if wait_for_probes:
            for thread in threads:
                thread.join()

    def _probe_target(
        self,
        udi: Any,
        target: Dict[str, Any],
        config: Dict[str, Any],
        single_pass: bool = False,
    ) -> None:
        channel_uuid = target["channel_uuid"]
        try:
            first_probe = True
            while first_probe or not self._stop_event.is_set():
                first_probe = False
                should_continue = self._probe_target_once(udi, target, config)
                if single_pass:
                    break
                if not (
                    should_continue
                    and config.get("watch_mode") == "continuous"
                    and self._uses_default_blank_probe
                ):
                    break
                if self._stop_event.is_set():
                    break

                fresh_status = self._find_status_for_target(udi.get_proxy_status() or {}, target)
                if self._real_client_count(fresh_status, config, target) <= 0:
                    self._reset_blank_count(channel_uuid)
                    self._clear_switch_attempts(channel_uuid)
                    event_target = dict(target)
                    event_target["real_client_count"] = 0
                    self._record_event("viewer_left", event_target, {})
                    with self._lock:
                        self._watched.pop(channel_uuid, None)
                    break

                target = dict(target)
                target["real_client_count"] = self._real_client_count(fresh_status, config, target)
                viewer_output_format = self._real_viewer_output_format(fresh_status, config)
                if viewer_output_format:
                    target["viewer_output_format"] = viewer_output_format
                watcher_details = self._watcher_client_details(fresh_status, config)
                target.update(watcher_details)
                watcher_count = int(target.get("watcher_client_count") or 0)
                target["watcher_state"] = "watching" if watcher_count > 0 else "reconnecting"
                stream_id = self._extract_stream_id(fresh_status) or target.get("stream_id")
                target["stream_id"] = stream_id
                target["stream_ref"] = _ref("stream", stream_id)
                with self._lock:
                    if watcher_count > 0:
                        self._watcher_absences.pop(channel_uuid, None)
                    self._watched[channel_uuid] = dict(target)
                blocking_watcher_count = self._blocking_watcher_count(target, watcher_count)
                if blocking_watcher_count > 0:
                    if self._watcher_status_has_current_probe_client(target, fresh_status, config):
                        time.sleep(float(config.get("watch_gap_seconds", 1)))
                        continue
                    bypass_details = self._pending_media_recovery_guard_bypass_details(
                        channel_uuid,
                        target,
                        fresh_status,
                        config,
                    )
                    if bypass_details is not None:
                        target["media_recovery_guard_bypass"] = bypass_details
                        target["media_recovery_guard_observed"] = bypass_details
                        self._record_event("watcher_recovery_observed", target, bypass_details)
                        time.sleep(float(config.get("watch_gap_seconds", 1)))
                        continue
                    guard_details = {
                        "reason": "active_watcher_between_confirmations",
                        "watcher_client_count": watcher_count,
                        "blocking_watcher_count": blocking_watcher_count,
                    }
                    if target.get("watcher_client_ref"):
                        guard_details["watcher_client_ref"] = target.get("watcher_client_ref")
                    self._record_watcher_recovery_guard(channel_uuid, target, guard_details, config)
                    break
        finally:
            with self._lock:
                self._active_probes.discard(channel_uuid)

    def _probe_target_once(self, udi: Any, target: Dict[str, Any], config: Dict[str, Any]) -> bool:
        channel_uuid = target["channel_uuid"]
        try:
            target.pop("media_recovery_guard_reason", None)
            target.pop("media_recovery_guard_bypass", None)
            target["active_probe_started_at"] = self.clock()
            proxy_url = self._channel_proxy_url(channel_uuid, target.get("viewer_output_format"))
            media_probe_url, media_probe_source = self._media_probe_url(udi, target, proxy_url)
            media_probe_config = dict(config)
            if media_probe_source == "stream_url":
                media_probe_config["watcher_api_key"] = ""
            target["media_probe_source"] = media_probe_source
            use_continuous_blank_probe = (
                config.get("watch_mode") == "continuous"
                and self._uses_default_blank_probe
            )
            if use_continuous_blank_probe:
                result = self._run_blank_probe_until_viewer_left(
                    media_probe_url,
                    media_probe_config,
                    udi,
                    target,
                    continuous=not bool(media_probe_config.get("loop_detection_enabled")),
                )
            else:
                result = self.blank_probe(media_probe_url, media_probe_config)
            blank = bool(result.get("blank_detected"))
            freeze = bool(result.get("freeze_detected"))
            no_decodable_frames = bool(result.get("no_decodable_frames_detected"))
            garbled_audio = bool(
                config.get("garbled_audio_detection_enabled")
                and result.get("garbled_audio_detected")
            )
            silent_audio = bool(
                config.get("silent_audio_detection_enabled")
                and result.get("silent_audio_detected")
            )
            offline_image = bool(
                config.get("offline_image_detection_enabled")
                and result.get("offline_image_detected")
            )
            loop = False
            detection_reason = next(
                (
                    reason
                    for reason, detected in (
                        ("blank", blank),
                        ("offline_image", offline_image),
                        ("freeze", freeze),
                        ("no_decodable_frames", no_decodable_frames),
                        ("garbled_audio", garbled_audio),
                        ("silent_audio", silent_audio),
                    )
                    if detected
                ),
                "",
            )
            target["last_probe"] = {
                "blank_detected": blank,
                "blank_ratio": result.get("blank_ratio"),
                "blank_duration_secs": result.get("blank_duration_secs"),
                "freeze_detected": freeze,
                "freeze_ratio": result.get("freeze_ratio"),
                "freeze_duration_secs": result.get("freeze_duration_secs"),
                "no_decodable_frames_detected": no_decodable_frames,
                "no_decodable_frames_duration_secs": result.get("no_decodable_frames_duration_secs"),
                "no_decodable_frames_error": result.get("no_decodable_frames_error"),
                "garbled_audio_detected": garbled_audio,
                "garbled_audio_error_count": result.get("garbled_audio_error_count"),
                "garbled_audio_error": result.get("garbled_audio_error"),
                "silent_audio_detected": silent_audio,
                "silent_audio_duration_secs": result.get("silent_audio_duration_secs"),
                "silent_audio_noise_db": result.get("silent_audio_noise_db"),
                "audio_stream_present": result.get("audio_stream_present"),
                "offline_image_detected": offline_image,
                "offline_image_hash": result.get("offline_image_hash"),
                "offline_image_distance": result.get("offline_image_distance"),
                "media_probe_source": media_probe_source,
                "loop_probe_ran": bool(result.get("loop_probe_ran")),
                "loop_detected": loop,
                "loop_duration_secs": result.get("loop_duration_secs"),
                "loop_frames_processed": result.get("loop_frames_processed"),
                "loop_probe_error": result.get("loop_probe_error"),
                "loop_probe_sliced": bool(result.get("loop_probe_sliced")),
            }
            target["last_probe_thresholds"] = self._detection_thresholds(config)

            if result.get("viewer_left"):
                fresh_status = {}
            else:
                fresh_status = self._find_status_for_target(udi.get_proxy_status() or {}, target)

            if result.get("viewer_left") or self._real_client_count(fresh_status, config, target) <= 0:
                self._reset_blank_count(channel_uuid)
                self._clear_switch_attempts(channel_uuid)
                target.pop("media_recovery_guard_observed", None)
                event_target = dict(target)
                event_target["real_client_count"] = 0
                self._record_event("viewer_left", event_target, {})
                with self._lock:
                    self._watched.pop(channel_uuid, None)
                return False

            if not detection_reason:
                loop_result = self._run_loop_probe_if_enabled(media_probe_url, target, media_probe_config, udi=udi)
                if loop_result:
                    result.update(loop_result)
                    loop = bool(loop_result.get("loop_detected"))
                    target["last_probe"].update({
                        "loop_probe_ran": bool(loop_result.get("loop_probe_ran")),
                        "loop_detected": loop,
                        "loop_duration_secs": loop_result.get("loop_duration_secs"),
                        "loop_frames_processed": loop_result.get("loop_frames_processed"),
                        "loop_probe_error": loop_result.get("loop_probe_error"),
                        "loop_probe_sliced": bool(loop_result.get("loop_probe_sliced")),
                    })
                    if loop_result.get("viewer_left"):
                        self._reset_blank_count(channel_uuid)
                        self._clear_switch_attempts(channel_uuid)
                        target.pop("media_recovery_guard_observed", None)
                        event_target = dict(target)
                        event_target["real_client_count"] = 0
                        self._record_event("viewer_left", event_target, {})
                        with self._lock:
                            self._watched.pop(channel_uuid, None)
                        return False
                    if loop:
                        detection_reason = "loop"
                    fresh_status = self._find_status_for_target(udi.get_proxy_status() or {}, target)
                    if self._real_client_count(fresh_status, config, target) <= 0:
                        self._reset_blank_count(channel_uuid)
                        self._clear_switch_attempts(channel_uuid)
                        target.pop("media_recovery_guard_observed", None)
                        event_target = dict(target)
                        event_target["real_client_count"] = 0
                        self._record_event("viewer_left", event_target, {})
                        with self._lock:
                            self._watched.pop(channel_uuid, None)
                        return False

            if not detection_reason:
                preserved_pending = self._reset_blank_count_after_probe_ok(channel_uuid, config)
                self._clear_switch_attempts(channel_uuid)
                target.pop("media_recovery_guard_observed", None)
                probe_details = dict(target["last_probe"])
                if preserved_pending:
                    probe_details["preserved_pending_detection"] = preserved_pending
                self._record_event("probe_ok", target, probe_details)
                return True

            cooldown_remaining = self._cooldown_remaining(channel_uuid)
            if cooldown_remaining > 0:
                self._reset_blank_count(channel_uuid)
                self._record_event(
                    "cooldown",
                    target,
                    {
                        "cooldown_seconds": cooldown_remaining,
                        "reason": detection_reason,
                    },
                )
                return True

            guard_details = self._watcher_recovery_guard_details(
                channel_uuid,
                target,
                fresh_status,
                config,
                reason=detection_reason,
            )
            confirmations = self._required_confirmations(config, detection_reason, guard_details)
            recovery_guard_bypass: Optional[Dict[str, Any]] = None
            if guard_details is not None:
                next_count = self._current_detection_count(channel_uuid, detection_reason) + 1
                recovery_guard_bypass = self._media_recovery_guard_bypass_details(
                    target,
                    fresh_status,
                    config,
                    reason=detection_reason,
                    guard_details=guard_details,
                    confirmed_count=next_count,
                    required_confirmations=confirmations,
                    require_confirmed=False,
                )
                if recovery_guard_bypass is None:
                    self._record_watcher_recovery_guard(channel_uuid, target, guard_details, config)
                    return False
                target["media_recovery_guard_reason"] = detection_reason
                target["media_recovery_guard_bypass"] = recovery_guard_bypass

            blank_count = self._increment_blank_count(channel_uuid, detection_reason)
            if blank_count < confirmations:
                pending_details = {
                    "confirmations": blank_count,
                    "required": confirmations,
                    "reason": detection_reason,
                }
                if recovery_guard_bypass:
                    pending_details["recovery_guard"] = recovery_guard_bypass
                self._record_event(
                    f"{detection_reason}_pending",
                    target,
                    pending_details,
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
        if self._real_client_count(fresh_status, config, target) <= 0:
            self._reset_blank_count(channel_uuid)
            self._clear_switch_attempts(channel_uuid)
            self._record_event("viewer_left", target, {})
            return

        fresh_stream_id = self._extract_stream_id(fresh_status) or target.get("stream_id")
        guard_details = self._watcher_recovery_guard_details(
            channel_uuid,
            target,
            fresh_status,
            config,
            reason=reason,
        )
        recovery_guard_bypass: Optional[Dict[str, Any]] = None
        if guard_details is not None:
            current_count = self._current_detection_count(channel_uuid, reason)
            required_confirmations = self._required_confirmations(config, reason, guard_details)
            recovery_guard_bypass = self._media_recovery_guard_bypass_details(
                target,
                fresh_status,
                config,
                reason=reason,
                guard_details=guard_details,
                confirmed_count=current_count,
                required_confirmations=required_confirmations,
                require_confirmed=True,
            )
            if recovery_guard_bypass is None:
                self._record_watcher_recovery_guard(channel_uuid, target, guard_details, config)
                return
        else:
            observed_guard = target.get("media_recovery_guard_observed")
            if isinstance(observed_guard, dict) and observed_guard.get("reason") == reason:
                current_count = self._current_detection_count(channel_uuid, reason)
                required_confirmations = self._required_confirmations(config, reason, observed_guard)
                recovery_guard_bypass = self._media_recovery_guard_bypass_details(
                    target,
                    fresh_status,
                    config,
                    reason=reason,
                    guard_details=observed_guard,
                    confirmed_count=current_count,
                    required_confirmations=required_confirmations,
                    require_confirmed=True,
                )
                if recovery_guard_bypass is not None:
                    recovery_guard_bypass["observed_between_confirmations"] = True

        if target.get("stream_id") and fresh_stream_id != target.get("stream_id"):
            self._reset_blank_count(channel_uuid)
            self._clear_switch_attempts(channel_uuid)
            self._record_event(
                "stale_stream_guard",
                target,
                {
                    "reason": reason,
                    "current_stream_ref": _ref("stream", fresh_stream_id),
                },
            )
            return

        if self._loop_switch_pre_probe_required(config, reason):
            self._set_cooldown(channel_uuid, config)
            self._reset_blank_count(channel_uuid)
            self._record_event(
                "loop_pre_probe_required",
                target,
                {
                    "reason": reason,
                    "next_stream_pre_probe_enabled": False,
                    "operator_action": "enable_next_stream_pre_probe",
                },
            )
            return

        if not self._switch_allowed(channel_uuid, config):
            self._record_event("switch_rate_limited", target, {"reason": reason})
            return

        attempted_targets = self._attempted_switch_targets(channel_uuid, fresh_stream_id)
        if config.get("next_stream_pre_probe_enabled"):
            alternative, pre_probe_details = self._choose_preprobed_alternative_stream(
                udi,
                target,
                config,
                fresh_stream_id,
                excluded_stream_ids=attempted_targets,
                reason=reason,
            )
        else:
            resolved_channel_id = self._resolve_channel_id(
                udi,
                target.get("channel_id"),
                channel_uuid=target.get("channel_uuid"),
                current_stream_id=fresh_stream_id,
                previous_target=target,
            )
            if resolved_channel_id is not None and target.get("channel_id") != resolved_channel_id:
                target["channel_id"] = resolved_channel_id
                target["channel_ref"] = _ref("channel", resolved_channel_id)
            alternative = self._choose_alternative_stream(
                udi,
                resolved_channel_id,
                fresh_stream_id,
                excluded_stream_ids=attempted_targets,
            )
            pre_probe_details = None
        if not alternative:
            self._set_cooldown(channel_uuid, config)
            self._reset_blank_count(channel_uuid)
            details = {"reason": reason}
            if pre_probe_details:
                details["pre_probe"] = pre_probe_details
                self._record_pre_probe_metric(
                    "switch_prevented_by_preprobe",
                    target,
                    pre_probe_details,
                )
            self._record_event("no_alternative", target, details)
            return

        switch_details = {"target_stream_ref": _ref("stream", alternative), "reason": reason}
        if pre_probe_details:
            switch_details["pre_probe"] = pre_probe_details
        if recovery_guard_bypass:
            switch_details["recovery_guard"] = recovery_guard_bypass

        if config.get("dry_run"):
            self._set_cooldown(channel_uuid, config)
            self._record_event(
                "dry_run_switch",
                target,
                switch_details,
            )
            return

        self._record_switch_attempt(channel_uuid, fresh_stream_id, alternative)
        switch_details["switch_channel_ref"] = _ref("channel", channel_uuid)
        # Dispatcharr's change_stream route is UUID-based even though proxy
        # status also carries StreamFlow's numeric channel id.
        switch_channel_id = channel_uuid or target.get("channel_id")
        api_success = bool(self.switch_stream(switch_channel_id, stream_id=alternative))
        observed_stream_id: Optional[int] = None
        verification_details: Dict[str, Any] = {}
        success = api_success
        if api_success and self._uses_default_switch_stream:
            success, observed_stream_id, verification_details = self._verify_active_stream_after_switch(
                udi,
                target,
                alternative,
                config,
            )
        self._reset_blank_count(channel_uuid)
        if success:
            with self._lock:
                self._switch_history[channel_uuid].append(self.clock())
        else:
            self._set_cooldown(channel_uuid, config)
        switch_details["switch_api_success"] = api_success
        if observed_stream_id is not None:
            switch_details["observed_stream_ref"] = _ref("stream", observed_stream_id)
        if verification_details:
            verification_details = dict(verification_details)
            verification_details.pop("accepted", None)
            switch_details.update(verification_details)
        self._record_event(
            "switch_success" if success else "switch_failed",
            target,
            {
                **switch_details,
                "post_switch_verification": bool(success),
            },
        )

    def _verify_active_stream_after_switch(
        self,
        udi: Any,
        target: Dict[str, Any],
        expected_stream_id: int,
        config: Dict[str, Any],
    ) -> tuple[bool, Optional[int], Dict[str, Any]]:
        deadline = time.monotonic() + 20.0
        observed_stream_id: Optional[int] = None
        while time.monotonic() < deadline and not self._stop_event.is_set():
            try:
                status = self._find_status_for_target(udi.get_proxy_status() or {}, target)
            except Exception as exc:
                logger.warning(f"Shadow blank monitor switch verification status fetch failed: {exc}")
                status = {}
            stream_id = self._extract_stream_id(status)
            if stream_id is not None:
                observed_stream_id = stream_id
            try:
                if stream_id is not None and int(stream_id) == int(expected_stream_id):
                    return True, observed_stream_id, {
                        "post_switch_verification_mode": "status_stream_id",
                    }
            except (TypeError, ValueError):
                pass
            time.sleep(0.5)

        if observed_stream_id is not None:
            details: Dict[str, Any] = {
                "post_switch_verification_mode": "status_stream_id",
                "post_switch_status_mismatch": True,
                "expected_stream_ref": _ref("stream", expected_stream_id),
            }
            probe_details = self._verify_proxy_output_after_switch(target, config)
            details.update(probe_details)
            if probe_details.get("accepted"):
                details["post_switch_verification_mode"] = "status_stream_id+proxy_probe"
                return True, observed_stream_id, details
            return False, observed_stream_id, details

        probe_details = self._verify_proxy_output_after_switch(target, config)
        if probe_details.get("accepted"):
            return True, observed_stream_id, probe_details
        return False, observed_stream_id, probe_details

    def _verify_proxy_output_after_switch(
        self,
        target: Dict[str, Any],
        config: Dict[str, Any],
    ) -> Dict[str, Any]:
        channel_uuid = target.get("channel_uuid")
        if not channel_uuid:
            return {
                "post_switch_verification_mode": "proxy_probe",
                "post_switch_proxy_probe": {"error": "missing_channel_uuid"},
            }
        try:
            url = self._channel_proxy_url(str(channel_uuid), target.get("viewer_output_format"))
        except Exception as exc:
            return {
                "post_switch_verification_mode": "proxy_probe",
                "post_switch_proxy_probe": {"error": str(exc)},
            }

        verify_config = dict(config)
        verify_config["probe_duration_seconds"] = max(
            3,
            min(8, int(config.get("next_stream_pre_probe_duration_seconds", 8))),
        )
        verify_config["loop_detection_enabled"] = False
        try:
            result = self.blank_probe(url, verify_config)
        except Exception as exc:
            return {
                "post_switch_verification_mode": "proxy_probe",
                "post_switch_proxy_probe": {"error": str(exc)},
            }

        rejection_reason = self._pre_probe_rejection_reason(result)
        summary = {
            key: result.get(key)
            for key in (
                "blank_detected",
                "freeze_detected",
                "no_decodable_frames_detected",
                "garbled_audio_detected",
                "silent_audio_detected",
                "offline_image_detected",
                "timeout",
            )
            if key in result
        }
        if rejection_reason:
            summary["rejection_reason"] = rejection_reason
        return {
            "post_switch_verification_mode": "proxy_probe",
            "post_switch_proxy_probe": summary,
            "post_switch_proxy_probe_accepted": rejection_reason is None,
            "accepted": rejection_reason is None,
        }

    def _ordered_alternative_streams(
        self,
        udi: Any,
        channel_id: Optional[int],
        current_stream_id: Optional[int],
        *,
        excluded_stream_ids: Optional[Iterable[int]] = None,
    ) -> List[int]:
        if channel_id is None:
            return []
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
            return []
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
        return [
            sid
            for sid in ordered
            if sid != current_stream_id and sid not in excluded
        ]

    def _choose_alternative_stream(
        self,
        udi: Any,
        channel_id: Optional[int],
        current_stream_id: Optional[int],
        *,
        excluded_stream_ids: Optional[Iterable[int]] = None,
    ) -> Optional[int]:
        return next(
            iter(
                self._ordered_alternative_streams(
                    udi,
                    channel_id,
                    current_stream_id,
                    excluded_stream_ids=excluded_stream_ids,
                )
            ),
            None,
        )

    @staticmethod
    def _loop_switch_pre_probe_required(config: Dict[str, Any], reason: str) -> bool:
        return (
            str(reason or "") == "loop"
            and LOOP_SWITCH_REQUIRES_PRE_PROBE
            and not bool(config.get("next_stream_pre_probe_enabled"))
        )

    @staticmethod
    def _loop_detection_gate_status(config: Dict[str, Any]) -> Dict[str, Any]:
        loop_enabled = bool(config.get("loop_detection_enabled"))
        pre_probe_enabled = bool(config.get("next_stream_pre_probe_enabled"))
        return {
            "enabled": loop_enabled,
            "active_real_viewer_required": True,
            "confirmation_required": True,
            "cooldown_required": True,
            "switch_rate_limit_required": True,
            "stale_stream_guard_required": True,
            "watcher_recovery_guard_required": True,
            "next_stream_pre_probe_required": LOOP_SWITCH_REQUIRES_PRE_PROBE,
            "next_stream_pre_probe_enabled": pre_probe_enabled,
            "switch_gate_satisfied": not (
                loop_enabled
                and LOOP_SWITCH_REQUIRES_PRE_PROBE
                and not pre_probe_enabled
            ),
        }

    def _choose_preprobed_alternative_stream(
        self,
        udi: Any,
        target: Dict[str, Any],
        config: Dict[str, Any],
        current_stream_id: Optional[int],
        *,
        excluded_stream_ids: Optional[Iterable[int]] = None,
        reason: str,
    ) -> tuple[Optional[int], Optional[Dict[str, Any]]]:
        last_details: Optional[Dict[str, Any]] = None
        channel_id = self._resolve_channel_id(
            udi,
            target.get("channel_id"),
            channel_uuid=target.get("channel_uuid"),
            current_stream_id=current_stream_id,
            previous_target=target,
        )
        if channel_id is not None and target.get("channel_id") != channel_id:
            target["channel_id"] = channel_id
            target["channel_ref"] = _ref("channel", channel_id)
        for candidate_id in self._ordered_alternative_streams(
            udi,
            channel_id,
            current_stream_id,
            excluded_stream_ids=excluded_stream_ids,
        ):
            candidate_ref = _ref("stream", candidate_id)
            candidate_stream = self._stream_for_id(udi, channel_id, candidate_id)
            candidate_url = self._stream_url(candidate_stream)
            details: Dict[str, Any] = {
                "enabled": True,
                "target_stream_ref": candidate_ref,
                "duration_seconds": int(config.get("next_stream_pre_probe_duration_seconds", 8)),
            }
            if not candidate_url:
                details["result"] = "missing_url"
                self._record_pre_probe_metric("preprobe_skipped_missing_url", target, details)
                last_details = details
                self._record_event("pre_probe_unavailable", target, {**details, "reason": reason})
                continue

            provider_slot = self._acquire_pre_probe_provider_slot(udi, candidate_stream)
            details.update(provider_slot.get("details") or {})
            if not provider_slot.get("acquired"):
                details["result"] = "rejected"
                details["rejection_reason"] = provider_slot.get("reason") or "provider_capacity"
                details["slot_scope"] = self._pre_probe_slot_scope(details["rejection_reason"])
                self._record_pre_probe_metric(
                    self._pre_probe_slot_metric(details["rejection_reason"]),
                    target,
                    details,
                )
                last_details = details
                self._record_event("pre_probe_rejected", target, {**details, "reason": reason})
                continue

            self._record_pre_probe_metric("preprobe_attempted", target, details)
            probe_started = time.monotonic()
            try:
                probe_result = self._run_next_stream_pre_probe(
                    str(provider_slot.get("url") or candidate_url),
                    config,
                    reason=reason,
                )
            finally:
                self._release_pre_probe_provider_slot(provider_slot)
            rejection_reason = self._pre_probe_rejection_reason(probe_result)
            details["elapsed_ms"] = max(0, int((time.monotonic() - probe_started) * 1000))
            details.update({
                "result": "rejected" if rejection_reason else "ok",
                "rejection_reason": rejection_reason,
                "blank_detected": bool(probe_result.get("blank_detected")),
                "freeze_detected": bool(probe_result.get("freeze_detected")),
                "no_decodable_frames_detected": bool(probe_result.get("no_decodable_frames_detected")),
                "garbled_audio_detected": bool(probe_result.get("garbled_audio_detected")),
                "silent_audio_detected": bool(probe_result.get("silent_audio_detected")),
                "audio_stream_present": probe_result.get("audio_stream_present"),
                "offline_image_detected": bool(probe_result.get("offline_image_detected")),
                "loop_probe_ran": bool(probe_result.get("loop_probe_ran")),
                "loop_detected": bool(probe_result.get("loop_detected")),
                "loop_duration_secs": probe_result.get("loop_duration_secs"),
            })
            last_details = details
            if rejection_reason:
                metric = "preprobe_timeout" if rejection_reason == "timeout" else "preprobe_rejected_media_fault"
                self._record_pre_probe_metric(metric, target, details)
                self._record_event("pre_probe_rejected", target, {**details, "reason": reason})
                continue
            self._record_pre_probe_metric("preprobe_success", target, details)
            return candidate_id, details
        return None, last_details

    def _stream_for_id(self, udi: Any, channel_id: Optional[int], stream_id: int) -> Optional[Dict[str, Any]]:
        stream: Optional[Dict[str, Any]] = None
        if hasattr(udi, "get_stream_by_id"):
            stream = udi.get_stream_by_id(stream_id)
        if not stream and channel_id is not None and hasattr(udi, "get_channel_streams"):
            stream_ref = str(stream_id)
            stream = next(
                (
                    candidate
                    for candidate in (udi.get_channel_streams(channel_id) or [])
                    if isinstance(candidate, dict) and str(candidate.get("id") or "") == stream_ref
                ),
                None,
            )
        return stream if isinstance(stream, dict) else None

    @staticmethod
    def _stream_url(stream: Optional[Dict[str, Any]]) -> Optional[str]:
        if not isinstance(stream, dict):
            return None
        url = str(stream.get("url") or "").strip()
        return url or None

    @staticmethod
    def _stream_m3u_account_id(stream: Optional[Dict[str, Any]]) -> Optional[int]:
        if not isinstance(stream, dict):
            return None
        account_id = stream.get("m3u_account_id")
        if account_id in (None, ""):
            account_id = stream.get("m3u_account")
        try:
            return int(account_id) if account_id not in (None, "") else None
        except (TypeError, ValueError):
            return None

    def _acquire_pre_probe_provider_slot(
        self,
        udi: Any,
        stream: Optional[Dict[str, Any]],
    ) -> Dict[str, Any]:
        account_id = self._stream_m3u_account_id(stream)
        details: Dict[str, Any] = {"provider_limited": account_id is not None}
        if account_id is not None:
            details["m3u_account_ref"] = _ref("m3u_account", account_id)

        if account_id is None:
            return {
                "acquired": True,
                "reason": "custom_stream",
                "url": self._stream_url(stream),
                "details": details,
            }

        try:
            from apps.stream.concurrent_stream_limiter import (
                get_account_limiter,
                initialize_account_limits,
            )

            limiter = get_account_limiter()
            limiter.udi_manager = udi
            if hasattr(udi, "get_m3u_accounts"):
                accounts = udi.get_m3u_accounts() or []
                if accounts:
                    initialize_account_limits(accounts)

            acquired, limit_reason = limiter.acquire(account_id, timeout=0)
            if not acquired:
                details["provider_limit_reason"] = limit_reason
                return {
                    "acquired": False,
                    "reason": limit_reason or "provider_capacity",
                    "details": details,
                }

            profile_acquired = False
            profile = None
            try:
                profile_acquired, profile_reason, profile = limiter.reserve_profile_for_stream(stream or {})
                if not profile_acquired:
                    details["provider_limit_reason"] = profile_reason
                    limiter.release(account_id)
                    return {
                        "acquired": False,
                        "reason": profile_reason or "provider_capacity",
                        "details": details,
                    }

                url = self._stream_url(stream)
                if profile and hasattr(udi, "apply_profile_url_transformation"):
                    transformed_url = udi.apply_profile_url_transformation(stream or {}, profile=profile)
                    if transformed_url:
                        url = str(transformed_url)
                        details["profile_url_transformed"] = True
                if profile:
                    details["m3u_profile_ref"] = _ref("m3u_profile", profile.get("id"))
                details["provider_slot_acquired"] = True
                return {
                    "acquired": True,
                    "reason": "acquired",
                    "limiter": limiter,
                    "account_id": account_id,
                    "profile": profile,
                    "url": url,
                    "details": details,
                }
            except Exception:
                if profile_acquired and profile is not None:
                    limiter.release_profile(profile)
                limiter.release(account_id)
                raise
        except Exception as exc:
            logger.warning("Shadow next-stream pre-probe provider limit check failed: %s", exc)
            details["provider_limit_reason"] = type(exc).__name__
            return {
                "acquired": False,
                "reason": "provider_limit_unavailable",
                "details": details,
            }

    @staticmethod
    def _release_pre_probe_provider_slot(slot: Dict[str, Any]) -> None:
        limiter = slot.get("limiter")
        if limiter is None:
            return
        try:
            limiter.release_profile(slot.get("profile"))
        finally:
            limiter.release(slot.get("account_id"))

    @staticmethod
    def _pre_probe_slot_scope(reason: Any) -> str:
        reason_text = str(reason or "")
        if reason_text in {"checking_capacity"}:
            return "profile"
        return "provider"

    @classmethod
    def _pre_probe_slot_metric(cls, reason: Any) -> str:
        if cls._pre_probe_slot_scope(reason) == "profile":
            return "preprobe_skipped_profile_limit"
        return "preprobe_skipped_provider_limit"

    def _record_pre_probe_metric(
        self,
        metric: str,
        target: Dict[str, Any],
        details: Optional[Dict[str, Any]],
    ) -> None:
        if metric not in PRE_PROBE_METRICS:
            return
        if isinstance(details, dict):
            details["pre_probe_metric"] = metric
        payload = dict(details or {})
        payload["metric"] = metric
        summary = {
            "timestamp": self.clock(),
            "metric": metric,
            "channel_ref": target.get("channel_ref"),
            "origin_stream_ref": target.get("stream_ref"),
            "target_stream_ref": payload.get("target_stream_ref"),
            "result": payload.get("result"),
            "rejection_reason": payload.get("rejection_reason"),
            "slot_scope": payload.get("slot_scope"),
            "elapsed_ms": payload.get("elapsed_ms"),
            "provider_limited": payload.get("provider_limited"),
            "provider_slot_acquired": payload.get("provider_slot_acquired"),
            "trigger_reason": payload.get("trigger_reason") or payload.get("reason"),
        }
        summary = {key: value for key, value in summary.items() if value is not None}
        with self._lock:
            self._pre_probe_metrics[metric] += 1
            self._last_pre_probe = summary

    def _run_next_stream_pre_probe(
        self,
        url: str,
        config: Dict[str, Any],
        *,
        reason: str = "",
    ) -> Dict[str, Any]:
        pre_probe_config = dict(config)
        pre_probe_config["probe_duration_seconds"] = int(
            config.get("next_stream_pre_probe_duration_seconds", 8)
        )
        pre_probe_config["watcher_api_key"] = ""
        if str(reason or "") != "loop":
            pre_probe_config["loop_detection_enabled"] = False
        result = self._run_blank_probe(url, pre_probe_config)
        if self._pre_probe_rejection_reason(result):
            return result
        result.update(self._run_loop_probe_if_enabled(url, {"channel_ref": "pre_probe"}, pre_probe_config))
        return result

    @staticmethod
    def _pre_probe_rejection_reason(result: Dict[str, Any]) -> Optional[str]:
        if result.get("timeout"):
            return "timeout"
        for reason in (
            "blank",
            "offline_image",
            "freeze",
            "no_decodable_frames",
            "garbled_audio",
            "silent_audio",
            "loop",
        ):
            if result.get(f"{reason}_detected"):
                return reason
        return None

    @staticmethod
    def _detection_count_key(channel_uuid: str, reason: str) -> str:
        return f"{channel_uuid}:{reason or 'blank'}"

    @staticmethod
    def _confirmation_count_reason(reason: str) -> str:
        if reason in VIDEO_FAULT_CONFIRMATION_REASONS:
            return VIDEO_FAULT_CONFIRMATION_KEY
        return reason or "blank"

    def _clear_detection_counts_locked(self, channel_uuid: str) -> None:
        self._blank_counts.pop(channel_uuid, None)
        self._blank_counts.pop(
            self._detection_count_key(channel_uuid, VIDEO_FAULT_CONFIRMATION_KEY),
            None,
        )
        self._blank_counts.pop(self._detection_count_key(channel_uuid, "blank"), None)
        self._blank_counts.pop(self._detection_count_key(channel_uuid, "freeze"), None)
        self._blank_counts.pop(self._detection_count_key(channel_uuid, "no_decodable_frames"), None)
        self._blank_counts.pop(self._detection_count_key(channel_uuid, "garbled_audio"), None)
        self._blank_counts.pop(self._detection_count_key(channel_uuid, "silent_audio"), None)
        self._blank_counts.pop(self._detection_count_key(channel_uuid, "offline_image"), None)
        self._blank_counts.pop(self._detection_count_key(channel_uuid, "loop"), None)
        self._detection_misses.pop(channel_uuid, None)
        self._detection_misses.pop(
            self._detection_count_key(channel_uuid, VIDEO_FAULT_CONFIRMATION_KEY),
            None,
        )
        self._detection_misses.pop(self._detection_count_key(channel_uuid, "blank"), None)
        self._detection_misses.pop(self._detection_count_key(channel_uuid, "freeze"), None)
        self._detection_misses.pop(self._detection_count_key(channel_uuid, "no_decodable_frames"), None)
        self._detection_misses.pop(self._detection_count_key(channel_uuid, "garbled_audio"), None)
        self._detection_misses.pop(self._detection_count_key(channel_uuid, "silent_audio"), None)
        self._detection_misses.pop(self._detection_count_key(channel_uuid, "offline_image"), None)
        self._detection_misses.pop(self._detection_count_key(channel_uuid, "loop"), None)

    def _reset_blank_count(self, channel_uuid: str) -> None:
        with self._lock:
            self._clear_detection_counts_locked(channel_uuid)

    def _reset_blank_count_after_probe_ok(
        self,
        channel_uuid: str,
        config: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        loop_key = self._detection_count_key(channel_uuid, "loop")
        with self._lock:
            loop_count = int(self._blank_counts.get(loop_key) or 0)
            loop_misses = int(self._detection_misses.get(loop_key) or 0)
            preserve_loop = (
                bool(config.get("loop_detection_enabled"))
                and loop_count > 0
                and loop_misses < LOOP_PENDING_PROBE_OK_MISS_TOLERANCE
            )
            self._clear_detection_counts_locked(channel_uuid)
            if not preserve_loop:
                return None
            self._blank_counts[loop_key] = loop_count
            self._detection_misses[loop_key] = loop_misses + 1
            return {
                "reason": "loop",
                "confirmations": loop_count,
                "misses": loop_misses + 1,
                "miss_tolerance": LOOP_PENDING_PROBE_OK_MISS_TOLERANCE,
            }

    def _reset_detection_state(self, channel_uuid: str) -> None:
        self._reset_blank_count(channel_uuid)
        self._clear_switch_attempts(channel_uuid)

    def _increment_blank_count(self, channel_uuid: str, reason: str = "blank") -> int:
        count_reason = self._confirmation_count_reason(reason)
        key = self._detection_count_key(channel_uuid, count_reason)
        with self._lock:
            if count_reason == VIDEO_FAULT_CONFIRMATION_KEY and not self._blank_counts.get(key):
                self._blank_counts[key] = max(
                    int(
                        self._blank_counts.get(self._detection_count_key(channel_uuid, "blank"))
                        or 0
                    ),
                    int(
                        self._blank_counts.get(self._detection_count_key(channel_uuid, "freeze"))
                        or 0
                    ),
                )
            self._blank_counts[key] += 1
            self._detection_misses[key] = 0
            return self._blank_counts[key]

    def _current_detection_count(self, channel_uuid: str, reason: str = "blank") -> int:
        count_reason = self._confirmation_count_reason(reason)
        key = self._detection_count_key(channel_uuid, count_reason)
        with self._lock:
            current_count = int(self._blank_counts.get(key) or 0)
            if current_count or count_reason != VIDEO_FAULT_CONFIRMATION_KEY:
                return current_count
            return max(
                int(
                    self._blank_counts.get(self._detection_count_key(channel_uuid, "blank"))
                    or 0
                ),
                int(
                    self._blank_counts.get(self._detection_count_key(channel_uuid, "freeze"))
                    or 0
                ),
            )

    @staticmethod
    def _required_confirmations(
        config: Dict[str, Any],
        reason: str,
        guard_details: Optional[Dict[str, Any]] = None,
    ) -> int:
        confirmations = int(config.get("confirmation_count", 2))
        if guard_details is not None and reason in MEDIA_FAULT_RECOVERY_GUARD_BYPASS_REASONS:
            return max(confirmations, 2)
        return confirmations

    def _watcher_recovery_guard_details(
        self,
        channel_uuid: str,
        target: Dict[str, Any],
        fresh_status: Dict[str, Any],
        config: Dict[str, Any],
        *,
        reason: str,
    ) -> Optional[Dict[str, Any]]:
        if config.get("watch_mode") != "continuous":
            return None

        with self._lock:
            watched = dict(self._watched.get(channel_uuid) or {})
        recovery_age = watched.get("watcher_recovered_after_seconds")
        guard_details: Optional[Dict[str, Any]] = None
        if recovery_age is not None:
            if self._watcher_recovery_matches_current_probe_window(
                target,
                watched,
                config,
                reason=reason,
                recovery_age=recovery_age,
            ):
                return None
            guard_details = {
                "reason": reason,
                "watcher_recovered_after_seconds": recovery_age,
            }
        else:
            fresh_details = self._watcher_client_details(fresh_status, config)
            if (
                self._watcher_client_started_with_current_probe(target, fresh_details)
                or self._watcher_status_has_current_probe_client(target, fresh_status, config)
            ):
                return None
            target_watcher_ref = target.get("watcher_client_ref")
            fresh_watcher_ref = fresh_details.get("watcher_client_ref")
            if target_watcher_ref and fresh_watcher_ref and fresh_watcher_ref != target_watcher_ref:
                guard_details = {
                    "reason": reason,
                    "last_watcher_client_ref": target_watcher_ref,
                    "watcher_client_ref": fresh_watcher_ref,
                }
            elif not target_watcher_ref and fresh_watcher_ref:
                guard_details = {
                    "reason": reason,
                    "watcher_client_ref": fresh_watcher_ref,
                    "watcher_client_count": fresh_details.get("watcher_client_count"),
                }

        return guard_details

    @staticmethod
    def _watcher_client_started_with_current_probe(
        target: Dict[str, Any],
        watcher_details: Dict[str, Any],
    ) -> bool:
        try:
            probe_started = float(target.get("active_probe_started_at"))
            watcher_connected = float(watcher_details.get("watcher_connected_at"))
        except (TypeError, ValueError):
            return False
        return watcher_connected >= probe_started - 0.5

    def _watcher_status_has_current_probe_client(
        self,
        target: Dict[str, Any],
        status: Dict[str, Any],
        config: Dict[str, Any],
    ) -> bool:
        try:
            probe_started = float(target.get("active_probe_started_at"))
        except (TypeError, ValueError):
            return False

        marker = str(config.get("watcher_user_agent") or "").lower()
        if not marker:
            return False
        clients = status.get("clients")
        if isinstance(clients, dict):
            clients = list(clients.values())
        if not isinstance(clients, list):
            return False

        for client in clients:
            if marker not in self._client_text(client).lower():
                continue
            connected_at = None
            if isinstance(client, dict):
                connected_at = client.get("connected_at") or client.get("started_at")
            try:
                watcher_connected = float(connected_at)
            except (TypeError, ValueError):
                continue
            if watcher_connected >= probe_started - 0.5:
                return True
        return False

    def _watcher_recovery_matches_current_probe_window(
        self,
        target: Dict[str, Any],
        watched: Dict[str, Any],
        config: Dict[str, Any],
        *,
        reason: str,
        recovery_age: Any,
    ) -> bool:
        if reason != "blank" and reason not in MEDIA_FAULT_RECOVERY_GUARD_BYPASS_REASONS:
            return False
        try:
            probe_started = float(target.get("active_probe_started_at"))
            watched_probe_started = float(watched.get("active_probe_started_at"))
            age = float(recovery_age)
        except (TypeError, ValueError):
            return False
        if abs(watched_probe_started - probe_started) > 1.0:
            return False
        # Bounded Shadow probes intentionally close and reopen the watcher
        # connection so ffmpeg can flush media-fault lines. Dispatcharr does
        # not always expose watcher connected_at. Only recoveries attached to
        # the same active probe window are treated as our own probe rollover;
        # stale or external watcher recoveries still guard the switch.
        window = self._probe_analysis_window_seconds(config) + float(config.get("watch_gap_seconds", 1)) + 1.0
        return 0.0 <= age <= window

    def _media_recovery_guard_bypass_details(
        self,
        target: Dict[str, Any],
        fresh_status: Dict[str, Any],
        config: Dict[str, Any],
        *,
        reason: str,
        guard_details: Dict[str, Any],
        confirmed_count: int,
        required_confirmations: int,
        require_confirmed: bool,
    ) -> Optional[Dict[str, Any]]:
        if reason not in MEDIA_FAULT_RECOVERY_GUARD_BYPASS_REASONS:
            return None
        if config.get("watch_mode") != "continuous":
            return None
        if not config.get("next_stream_pre_probe_enabled"):
            return None
        if require_confirmed and confirmed_count < required_confirmations:
            return None
        if self._real_client_count(fresh_status, config, target) <= 0:
            return None

        target_stream_id = target.get("stream_id")
        fresh_stream_id = self._extract_stream_id(fresh_status) or target_stream_id
        if target_stream_id and fresh_stream_id != target_stream_id:
            return None

        details = dict(guard_details)
        guard_reason = guard_details.get("reason")
        details.update({
            "bypassed": True,
            "bypass_scope": "confirmed_media_fault",
            "guard_reason": guard_reason,
            "reason": reason,
            "pre_probe_required": True,
            "confirmations": confirmed_count,
            "required": required_confirmations,
            "current_stream_ref": _ref("stream", fresh_stream_id),
        })
        return details

    def _pending_media_recovery_guard_bypass_details(
        self,
        channel_uuid: str,
        target: Dict[str, Any],
        fresh_status: Dict[str, Any],
        config: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        reason = (
            target.get("media_recovery_guard_reason")
            or (target.get("media_recovery_guard_bypass") or {}).get("reason")
        )
        if not reason:
            return None

        current_count = self._current_detection_count(channel_uuid, str(reason))
        guard_details = {
            "reason": "active_watcher_between_confirmations",
            "watcher_client_count": self._watcher_client_count(fresh_status, config),
        }
        fresh_details = self._watcher_client_details(fresh_status, config)
        if fresh_details.get("watcher_client_ref"):
            guard_details["watcher_client_ref"] = fresh_details.get("watcher_client_ref")
        required_confirmations = self._required_confirmations(config, str(reason), guard_details)
        if current_count <= 0 or current_count >= required_confirmations:
            return None

        return self._media_recovery_guard_bypass_details(
            target,
            fresh_status,
            config,
            reason=str(reason),
            guard_details=guard_details,
            confirmed_count=current_count,
            required_confirmations=required_confirmations,
            require_confirmed=False,
        )

    def _guard_recent_watcher_recovery(
        self,
        channel_uuid: str,
        target: Dict[str, Any],
        fresh_status: Dict[str, Any],
        config: Dict[str, Any],
        *,
        reason: str,
    ) -> bool:
        guard_details = self._watcher_recovery_guard_details(
            channel_uuid,
            target,
            fresh_status,
            config,
            reason=reason,
        )
        if guard_details is None:
            return False

        self._record_watcher_recovery_guard(channel_uuid, target, guard_details, config)
        return True

    def _record_watcher_recovery_guard(
        self,
        channel_uuid: str,
        target: Dict[str, Any],
        guard_details: Dict[str, Any],
        config: Optional[Dict[str, Any]] = None,
    ) -> None:
        self._reset_detection_state(channel_uuid)
        self._record_event("watcher_recovery_guard", target, guard_details)

    @staticmethod
    def _persistent_watcher_command(url: str, config: Dict[str, Any]) -> List[str]:
        command = [
            "ffmpeg",
            "-hide_banner",
            "-nostdin",
            "-loglevel",
            "error",
            "-user_agent",
            config.get("watcher_user_agent") or DEFAULT_CONFIG["watcher_user_agent"],
        ]
        headers = ShadowBlankMonitorService._watcher_probe_headers(config)
        if headers:
            command.extend(["-headers", headers])
        command.extend([
            "-i",
            url,
            "-map",
            "0:v:0?",
            "-map",
            "0:a:0?",
            "-c",
            "copy",
            "-f",
            "null",
            "-",
        ])
        return command

    def _persistent_watcher_snapshot(self, channel_uuid: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            session = self._persistent_watchers.get(channel_uuid)
            if not session:
                return None
            process = session.get("process")
            if process is not None and process.poll() is not None:
                return None
            started_at = float(session.get("started_at") or self.clock())
            return {
                "persistent_watcher_state": "running",
                "persistent_watcher_started_at": started_at,
                "persistent_watcher_uptime_seconds": max(0, int(self.clock() - started_at)),
                "persistent_watcher_pid": getattr(process, "pid", None),
            }

    def _apply_persistent_watcher_snapshot(self, channel_uuid: str, target: Dict[str, Any]) -> None:
        snapshot = self._persistent_watcher_snapshot(channel_uuid)
        if not snapshot:
            return
        target.update(snapshot)
        with self._lock:
            watched = self._watched.get(channel_uuid)
            if watched is not None:
                watched.update(snapshot)

    @staticmethod
    def _blocking_watcher_count(target: Dict[str, Any], watcher_count: int) -> int:
        """Return watcher clients that should block starting another probe.

        A persistent watcher is intentionally kept open to stabilize Dispatcharr
        playback visibility. It is not an analysis probe and must not suppress
        future media checks. Any additional watcher client still blocks broad
        probing to avoid duplicate proxy probes.
        """
        count = max(0, int(watcher_count or 0))
        if target.get("persistent_watcher_state") == "running" and count > 0:
            return count - 1
        return count

    def _start_persistent_watcher(
        self,
        channel_uuid: str,
        target: Dict[str, Any],
        config: Dict[str, Any],
        url: str,
    ) -> None:
        command = self._persistent_watcher_command(url, config)
        try:
            process = subprocess.Popen(
                command,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
            )
        except Exception as exc:
            logger.warning(
                "Shadow persistent watcher failed to start for %s: %s",
                target.get("channel_ref") or _ref("channel", channel_uuid),
                exc,
            )
            return

        session = {
            "process": process,
            "url": url,
            "stream_id": target.get("stream_id"),
            "output_format": target.get("viewer_output_format"),
            "started_at": self.clock(),
        }
        with self._lock:
            self._persistent_watchers[channel_uuid] = session
        self._apply_persistent_watcher_snapshot(channel_uuid, target)

    def _stop_persistent_watcher(self, channel_uuid: str) -> None:
        with self._lock:
            session = self._persistent_watchers.pop(channel_uuid, None)
        if not session:
            return
        process = session.get("process")
        if process is None or process.poll() is not None:
            return
        try:
            process.terminate()
            process.wait(timeout=3)
        except Exception:
            try:
                process.kill()
                process.wait(timeout=3)
            except Exception:
                pass

    def _sync_persistent_watchers(self, targets: Iterable[Dict[str, Any]], config: Dict[str, Any]) -> None:
        targets_by_uuid = {
            str(target.get("channel_uuid")): target
            for target in targets
            if target.get("channel_uuid") and int(target.get("real_client_count") or 0) > 0
        }
        should_keep_watchers = (
            config.get("watch_mode") == "continuous"
            and self._uses_default_blank_probe
            and bool(str(config.get("watcher_api_key") or "").strip())
        )
        if not should_keep_watchers:
            targets_by_uuid = {}

        with self._lock:
            existing_uuids = list(self._persistent_watchers)

        for channel_uuid in existing_uuids:
            target = targets_by_uuid.get(channel_uuid)
            if not target:
                self._stop_persistent_watcher(channel_uuid)
                continue
            try:
                url = self._channel_proxy_url(channel_uuid, target.get("viewer_output_format"))
            except Exception:
                self._stop_persistent_watcher(channel_uuid)
                continue
            with self._lock:
                session = self._persistent_watchers.get(channel_uuid)
            process = session.get("process") if session else None
            stale = (
                not session
                or process is None
                or process.poll() is not None
                or session.get("url") != url
                or str(session.get("stream_id")) != str(target.get("stream_id"))
            )
            if stale:
                self._stop_persistent_watcher(channel_uuid)
                self._start_persistent_watcher(channel_uuid, target, config, url)
            else:
                self._apply_persistent_watcher_snapshot(channel_uuid, target)

        for channel_uuid, target in targets_by_uuid.items():
            with self._lock:
                exists = channel_uuid in self._persistent_watchers
            if exists:
                continue
            try:
                url = self._channel_proxy_url(channel_uuid, target.get("viewer_output_format"))
            except Exception as exc:
                logger.warning(
                    "Shadow persistent watcher could not build proxy URL for %s: %s",
                    target.get("channel_ref") or _ref("channel", channel_uuid),
                    exc,
                )
                continue
            self._start_persistent_watcher(channel_uuid, target, config, url)

    def _channel_proxy_url(self, channel_uuid: str, output_format: Optional[str] = None) -> str:
        base_url = (self.base_url_provider() or "").rstrip("/")
        if not base_url:
            raise RuntimeError("Dispatcharr base URL is not configured")
        url = f"{base_url}/proxy/ts/stream/{channel_uuid}"
        canonical_format = self._normalize_proxy_output_format(output_format)
        if canonical_format:
            return f"{url}?output_format={canonical_format}"
        return url

    def _media_probe_url(self, udi: Any, target: Dict[str, Any], fallback_url: str) -> tuple[str, str]:
        """Prefer the current source URL for short media probes.

        The persistent watcher intentionally uses the Dispatcharr proxy so the
        channel stays open like a real viewer. The short ffmpeg analysis probes
        do not need to appear as additional proxy clients; when UDI can resolve
        the active stream URL, analyze that source directly and keep the proxy
        URL as a conservative fallback.
        """
        stream_id = self._coerce_int(target.get("stream_id"))
        if stream_id is None or not hasattr(udi, "get_stream_by_id"):
            return fallback_url, "channel_proxy"
        try:
            stream = udi.get_stream_by_id(stream_id)
        except Exception as exc:
            logger.debug("Shadow media probe stream lookup failed for %s: %s", target.get("stream_ref"), exc)
            return fallback_url, "channel_proxy"
        if not isinstance(stream, dict):
            return fallback_url, "channel_proxy"
        source_url = str(stream.get("url") or "").strip()
        if not source_url:
            return fallback_url, "channel_proxy"
        return source_url, "stream_url"

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
        command.extend(["-i", url, "-vf", ",".join(video_filters)])
        if config.get("garbled_audio_detection_enabled") or config.get("silent_audio_detection_enabled"):
            audio_filters: List[str] = []
            if config.get("silent_audio_detection_enabled"):
                audio_filters.append(
                    "silencedetect="
                    f"n={int(config['silent_audio_noise_db'])}dB:"
                    f"d={float(config['silent_audio_min_duration_seconds'])}"
                )
            audio_filters.append("astats=metadata=1:reset=1")
            command.extend(["-af", ",".join(audio_filters)])
        else:
            command.append("-an")
        command.extend(["-f", "null", "-"])
        if not continuous:
            input_index = command.index("-vf")
            command[input_index:input_index] = ["-t", str(duration)]
        return command, duration

    @staticmethod
    def _probe_analysis_window_seconds(config: Dict[str, Any]) -> float:
        """Bound one shadow media analysis window to the enabled fault timers.

        Some Dispatcharr proxy formats only flush blackdetect/freezedetect
        segment summaries when ffmpeg exits. Keeping the downstream probe open
        forever makes a real black channel look healthy until the viewer leaves.
        This cap lets one confirmation window close quickly while preserving the
        configured per-fault minimum durations.
        """
        duration = float(config.get("probe_duration_seconds") or DEFAULT_CONFIG["probe_duration_seconds"])
        required_windows = [
            float(config.get("blank_min_duration_seconds", DEFAULT_CONFIG["blank_min_duration_seconds"])),
        ]
        if config.get("freeze_detection_enabled"):
            required_windows.append(float(
                config.get("freeze_min_duration_seconds", DEFAULT_CONFIG["freeze_min_duration_seconds"])
            ))
        if config.get("no_decodable_frames_detection_enabled", True):
            required_windows.append(float(
                config.get(
                    "no_decodable_frames_min_duration_seconds",
                    DEFAULT_CONFIG["no_decodable_frames_min_duration_seconds"],
                )
            ))
        if config.get("silent_audio_detection_enabled"):
            required_windows.append(float(
                config.get(
                    "silent_audio_min_duration_seconds",
                    DEFAULT_CONFIG["silent_audio_min_duration_seconds"],
                )
            ))
        # Add a small settle window so ffmpeg can flush final filter lines.
        analysis_window = max(required_windows or [duration]) + 2.0
        return max(1.0, min(duration, analysis_window))

    @staticmethod
    def _line_is_missing_audio(line: str) -> bool:
        lowered = (line or "").lower()
        return any(pattern in lowered for pattern in AUDIO_MISSING_PATTERNS) and (
            "audio" in lowered or ":a" in lowered
        )

    @staticmethod
    def _line_has_audio_stream(line: str) -> bool:
        return FFMPEG_STREAM_AUDIO_RE.search(line or "") is not None

    @staticmethod
    def _line_has_video_stream(line: str) -> bool:
        return FFMPEG_STREAM_VIDEO_RE.search(line or "") is not None

    @staticmethod
    def _line_marks_stream_mapping(line: str) -> bool:
        return FFMPEG_STREAM_MAPPING_RE.search(line or "") is not None

    @staticmethod
    def _output_has_video_without_audio(output: str) -> bool:
        saw_video_stream = False
        saw_audio_stream = False
        saw_stream_mapping = False
        saw_frame_progress = False
        saw_zero_audio_summary = False
        for line in (output or "").splitlines():
            if ShadowBlankMonitorService._line_has_audio_stream(line):
                saw_audio_stream = True
            if ShadowBlankMonitorService._line_has_video_stream(line):
                saw_video_stream = True
            if ShadowBlankMonitorService._line_marks_stream_mapping(line):
                saw_stream_mapping = True
            if FFMPEG_FRAME_RE.search(line):
                saw_frame_progress = True
            if FFMPEG_ZERO_AUDIO_SUMMARY_RE.search(line):
                saw_zero_audio_summary = True
        return saw_video_stream and not saw_audio_stream and (
            saw_stream_mapping or saw_frame_progress or saw_zero_audio_summary
        )

    @staticmethod
    def _line_is_garbled_audio(line: str) -> bool:
        lowered = (line or "").lower()
        if not lowered or ShadowBlankMonitorService._line_is_missing_audio(lowered):
            return False
        if any(pattern in lowered for pattern in AUDIO_ERROR_PATTERNS):
            return "audio" in lowered or ":a" in lowered or AUDIO_DECODER_RE.search(lowered) is not None
        if AUDIO_DECODER_RE.search(lowered) is None:
            return False
        return any(pattern in lowered for pattern in AUDIO_DECODER_ERROR_PATTERNS)

    @staticmethod
    def _parse_audio_detection(
        output: str,
        config: Dict[str, Any],
        *,
        observed_duration: float,
    ) -> Dict[str, Any]:
        result = {
            "garbled_audio_detected": False,
            "garbled_audio_error_count": 0,
            "garbled_audio_error": None,
            "silent_audio_detected": False,
            "silent_audio_duration_secs": None,
            "silent_audio_noise_db": None,
            "audio_stream_present": None,
        }
        if not (
            config.get("garbled_audio_detection_enabled")
            or config.get("silent_audio_detection_enabled")
        ):
            return result

        output = output or ""
        audio_missing = False
        garbled_count = 0
        first_garbled_line: Optional[str] = None
        active_silence_start: Optional[float] = None
        longest_silence = 0.0

        for line in output.splitlines():
            if ShadowBlankMonitorService._line_is_missing_audio(line):
                audio_missing = True
                continue
            if ShadowBlankMonitorService._line_is_garbled_audio(line):
                garbled_count += 1
                first_garbled_line = first_garbled_line or line.strip()[:160]

            silence_start = SILENCE_START_RE.search(line)
            if silence_start:
                try:
                    active_silence_start = max(0.0, float(silence_start.group("start")))
                except (TypeError, ValueError):
                    active_silence_start = None

            silence_duration = SILENCE_DURATION_RE.search(line)
            if silence_duration:
                try:
                    longest_silence = max(longest_silence, float(silence_duration.group("duration")))
                except (TypeError, ValueError):
                    pass
                active_silence_start = None
                continue

            silence_end = SILENCE_END_RE.search(line)
            if silence_end and active_silence_start is not None:
                try:
                    longest_silence = max(
                        longest_silence,
                        max(0.0, float(silence_end.group("end")) - active_silence_start),
                    )
                except (TypeError, ValueError):
                    pass
                active_silence_start = None

        if not audio_missing and ShadowBlankMonitorService._output_has_video_without_audio(output):
            audio_missing = True

        if active_silence_start is not None:
            longest_silence = max(longest_silence, max(0.0, float(observed_duration or 0.0) - active_silence_start))

        result["audio_stream_present"] = not audio_missing if (audio_missing or garbled_count or longest_silence) else None
        if audio_missing:
            if config.get("silent_audio_detection_enabled"):
                result["silent_audio_detected"] = True
                result["silent_audio_duration_secs"] = round(max(0.0, float(observed_duration or 0.0)), 3)
                result["silent_audio_noise_db"] = int(config.get("silent_audio_noise_db", DEFAULT_CONFIG["silent_audio_noise_db"]))
            return result

        if config.get("garbled_audio_detection_enabled"):
            threshold = int(config.get("garbled_audio_error_threshold", DEFAULT_CONFIG["garbled_audio_error_threshold"]))
            result["garbled_audio_error_count"] = garbled_count
            if garbled_count >= threshold:
                result["garbled_audio_detected"] = True
                result["garbled_audio_error"] = first_garbled_line or "audio_decode_errors"

        if config.get("silent_audio_detection_enabled"):
            min_duration = float(
                config.get(
                    "silent_audio_min_duration_seconds",
                    DEFAULT_CONFIG["silent_audio_min_duration_seconds"],
                )
            )
            result["silent_audio_duration_secs"] = round(longest_silence, 3) if longest_silence else None
            result["silent_audio_noise_db"] = int(config.get("silent_audio_noise_db", DEFAULT_CONFIG["silent_audio_noise_db"]))
            if longest_silence >= min_duration:
                result["silent_audio_detected"] = True

        return result

    @staticmethod
    def _offline_image_probe_command(url: str, config: Dict[str, Any]) -> List[str]:
        headers = ""
        api_key = config.get("watcher_api_key")
        if api_key:
            headers = f"X-API-Key: {api_key}\r\nAuthorization: ApiKey {api_key}\r\n"
        command = [
            "ffmpeg",
            "-hide_banner",
            "-nostdin",
            "-loglevel",
            "error",
            "-user_agent",
            config.get("watcher_user_agent") or DEFAULT_CONFIG["watcher_user_agent"],
        ]
        if headers:
            command.extend(["-headers", headers])
        offset = int(config.get("offline_image_capture_offset_seconds", 3))
        if offset > 0:
            command.extend(["-ss", str(offset)])
        command.extend(["-i", url, "-frames:v", "1", "-f", "image2pipe", "-vcodec", "png", "-"])
        return command

    @staticmethod
    def _hash_distance(left: str, right: str) -> Optional[int]:
        try:
            return bin(int(left, 16) ^ int(right, 16)).count("1")
        except (TypeError, ValueError):
            return None

    def _capture_offline_image_hash(self, url: str, config: Dict[str, Any]) -> Dict[str, Any]:
        try:
            from PIL import Image
            import imagehash
        except Exception as exc:
            return {
                "success": False,
                "reason": f"image_hash_unavailable:{type(exc).__name__}",
            }

        try:
            completed = subprocess.run(
                self._offline_image_probe_command(url, config),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=15,
            )
            if completed.returncode != 0 or not completed.stdout:
                return {
                    "success": False,
                    "reason": "frame_capture_failed",
                }

            image = Image.open(io.BytesIO(completed.stdout))
            return {
                "success": True,
                "offline_image_hash": str(imagehash.phash(image)).lower(),
            }
        except subprocess.TimeoutExpired:
            return {
                "success": False,
                "reason": "frame_capture_timeout",
            }
        except Exception as exc:
            logger.debug("Shadow offline image hash capture failed: %s", exc)
            return {
                "success": False,
                "reason": type(exc).__name__,
            }

    def _run_offline_image_probe(self, url: str, config: Dict[str, Any]) -> Dict[str, Any]:
        result = {
            "offline_image_detected": False,
            "offline_image_hash": None,
            "offline_image_distance": None,
            "offline_image_reference_count": len(config.get("offline_image_reference_hashes") or []),
        }
        if not config.get("offline_image_detection_enabled"):
            return result
        invalid_hashes = _invalid_offline_image_hashes(config.get("offline_image_reference_hashes"))
        reference_hashes = _valid_offline_image_hashes(config.get("offline_image_reference_hashes"))
        result["offline_image_invalid_reference_count"] = len(invalid_hashes)
        if not reference_hashes:
            if invalid_hashes:
                result["offline_image_error"] = "no_valid_reference_hashes"
            return result

        capture = self._capture_offline_image_hash(url, config)
        if not capture.get("success"):
            result["offline_image_error"] = capture.get("reason")
            return result

        phash = str(capture.get("offline_image_hash") or "").lower()
        result["offline_image_hash"] = phash
        distances = [
            distance
            for distance in (self._hash_distance(phash, candidate) for candidate in reference_hashes)
            if distance is not None
        ]
        if not distances:
            return result
        best_distance = min(distances)
        result["offline_image_distance"] = best_distance
        if best_distance <= int(config.get("offline_image_hash_threshold", 4)):
            result["offline_image_detected"] = True
        return result

    @staticmethod
    def _watcher_probe_headers(config: Dict[str, Any]) -> str:
        api_key = config.get("watcher_api_key")
        if not api_key:
            return ""
        return f"X-API-Key: {api_key}\r\nAuthorization: ApiKey {api_key}\r\n"

    def _run_loop_probe(self, url: str, config: Dict[str, Any]) -> Dict[str, Any]:
        stream_tag = _ref("shadow-loop-url", url)
        loop_detected, loop_duration, frames = _probe_stream_for_loops(
            url=url,
            stream_tag=stream_tag,
            probe_duration=int(config.get("loop_probe_duration_seconds", 120)),
            user_agent=config.get("watcher_user_agent") or DEFAULT_CONFIG["watcher_user_agent"],
            headers=self._watcher_probe_headers(config) or None,
            should_abort=config.get("_shadow_loop_abort_check"),
        )
        return {
            "loop_probe_ran": True,
            "loop_detected": bool(loop_detected),
            "loop_duration_secs": loop_duration,
            "loop_frames_processed": frames,
        }

    @staticmethod
    def _continuous_loop_probe_slice_seconds(config: Dict[str, Any]) -> float:
        """Keep continuous Shadow loop probes from blocking media-fault checks.

        Loop detection still gets enough wall time to satisfy the 10s sidecar
        loop threshold, but it must yield back to the watcher so black, freeze,
        silent-audio, and decode-fault checks can run again while a viewer is
        still active.
        """
        configured = float(
            config.get("loop_probe_duration_seconds")
            or DEFAULT_CONFIG["loop_probe_duration_seconds"]
        )
        analysis_window = ShadowBlankMonitorService._probe_analysis_window_seconds(config)
        return max(1.0, min(configured, max(15.0, analysis_window + 3.0)))

    def _run_loop_probe_if_enabled(
        self,
        url: str,
        target: Dict[str, Any],
        config: Dict[str, Any],
        *,
        udi: Any = None,
    ) -> Dict[str, Any]:
        if not config.get("loop_detection_enabled"):
            return {}
        loop_config = dict(config)
        channel_uuid = str(target.get("channel_uuid") or "")
        abort_state = {"viewer_left": False}
        loop_slice_seconds: Optional[float] = None
        loop_slice_deadline: Optional[float] = None
        if udi is not None and config.get("watch_mode") == "continuous":
            pending_loop_confirmation = (
                bool(channel_uuid)
                and self._current_detection_count(channel_uuid, "loop") > 0
            )
            interval_seconds = float(
                max(
                    self._continuous_loop_probe_slice_seconds(config),
                    float(config.get("loop_probe_duration_seconds") or DEFAULT_CONFIG["loop_probe_duration_seconds"]),
                )
            )
            now = self.clock()
            last_started = None
            if not pending_loop_confirmation:
                with self._lock:
                    last_started = self._last_loop_probe_started_at.get(channel_uuid)
            if last_started is not None:
                elapsed = now - float(last_started)
                if elapsed < interval_seconds:
                    return {
                        "loop_probe_ran": False,
                        "loop_detected": False,
                        "loop_probe_skipped": True,
                        "loop_probe_skip_reason": "loop_probe_interval",
                        "loop_probe_next_due_seconds": max(0, int(interval_seconds - elapsed)),
                    }
            if channel_uuid:
                with self._lock:
                    self._last_loop_probe_started_at[channel_uuid] = now
            loop_slice_seconds = self._continuous_loop_probe_slice_seconds(config)
            loop_slice_deadline = time.monotonic() + loop_slice_seconds

            def _abort_when_viewer_left() -> bool:
                try:
                    fresh_status = self._find_status_for_target(udi.get_proxy_status() or {}, target)
                    viewer_left = self._real_client_count(fresh_status, config, target) <= 0
                except Exception as exc:
                    logger.warning(f"Shadow loop probe viewer poll failed: {exc}")
                    viewer_left = False
                if viewer_left:
                    abort_state["viewer_left"] = True
                    return True
                if loop_slice_deadline is not None and time.monotonic() >= loop_slice_deadline:
                    abort_state["time_sliced"] = True
                    return True
                return self._stop_event.is_set()

            loop_config["_shadow_loop_abort_check"] = _abort_when_viewer_left
        try:
            result = dict(self.loop_probe(url, loop_config) or {})
            result.setdefault("loop_probe_ran", True)
            result["loop_detected"] = bool(result.get("loop_detected"))
            if abort_state.get("viewer_left"):
                result["viewer_left"] = True
            if abort_state.get("time_sliced") and not result.get("loop_detected"):
                result["loop_probe_sliced"] = True
                if loop_slice_seconds is not None:
                    result["loop_probe_slice_seconds"] = round(loop_slice_seconds, 3)
            return result
        except Exception as exc:
            logger.warning(
                "Shadow loop probe failed for %s: %s",
                target.get("channel_ref") or _ref("channel", target.get("channel_uuid")),
                exc,
            )
            return {
                "loop_probe_ran": False,
                "loop_detected": False,
                "loop_probe_error": type(exc).__name__,
            }

    @staticmethod
    def _parse_no_decodable_frames_detection(
        output: str,
        config: Dict[str, Any],
        *,
        observed_duration: float,
        returncode: Optional[int],
    ) -> Dict[str, Any]:
        result = {
            "no_decodable_frames_detected": False,
            "no_decodable_frames_duration_secs": None,
            "no_decodable_frames_error": None,
        }
        if not config.get("no_decodable_frames_detection_enabled", True):
            return result

        output = output or ""
        lowered = output.lower()
        matched_pattern = next(
            (pattern for pattern in NO_DECODABLE_FRAME_ERROR_PATTERNS if pattern in lowered),
            None,
        )
        if not matched_pattern:
            return result

        decoded_frames = 0
        for match in FFMPEG_FRAME_RE.finditer(output):
            try:
                decoded_frames = max(decoded_frames, int(match.group("frames")))
            except (TypeError, ValueError):
                continue
        if decoded_frames > 0:
            return result

        min_duration = float(
            config.get(
                "no_decodable_frames_min_duration_seconds",
                DEFAULT_CONFIG["no_decodable_frames_min_duration_seconds"],
            )
        )
        strong_terminal_error = (
            returncode not in (None, 0)
            and (
                "output file does not contain any stream" in lowered
                or (
                    "could not find codec parameters" in lowered
                    and "unspecified size" in lowered
                )
            )
        )
        if float(observed_duration or 0.0) < min_duration and not strong_terminal_error:
            return result

        result["no_decodable_frames_detected"] = True
        result["no_decodable_frames_duration_secs"] = round(
            max(float(observed_duration or 0.0), min_duration if strong_terminal_error else 0.0),
            3,
        )
        result["no_decodable_frames_error"] = matched_pattern
        return result

    def _run_blank_probe(self, url: str, config: Dict[str, Any]) -> Dict[str, Any]:
        command, duration = self._blank_probe_command(url, config)
        try:
            start = time.time()
            completed = subprocess.run(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=duration + 15,
                text=True,
            )
            elapsed = time.time() - start
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
            parsed.update(self._parse_audio_detection(
                output,
                config,
                observed_duration=elapsed,
            ))
            parsed.update(self._parse_no_decodable_frames_detection(
                output,
                config,
                observed_duration=elapsed,
                returncode=completed.returncode,
            ))
            parsed.update(self._run_offline_image_probe(url, config))
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
            parsed.update(self._parse_audio_detection(
                output,
                config,
                observed_duration=duration,
            ))
            parsed.update(self._parse_no_decodable_frames_detection(
                output,
                config,
                observed_duration=duration,
                returncode=None,
            ))
            parsed.update(self._run_offline_image_probe(url, config))
            parsed["timeout"] = True
            return parsed

    def _run_blank_probe_until_viewer_left(
        self,
        url: str,
        config: Dict[str, Any],
        udi: Any,
        target: Dict[str, Any],
        *,
        continuous: bool = True,
    ) -> Dict[str, Any]:
        command, duration = self._blank_probe_command(url, config, continuous=continuous)
        offline_image_probe = self._run_offline_image_probe(url, config)
        if offline_image_probe.get("offline_image_detected"):
            return {
                "blank_detected": False,
                "freeze_detected": False,
                "no_decodable_frames_detected": False,
                "garbled_audio_detected": False,
                "silent_audio_detected": False,
                **offline_image_probe,
            }
        viewer_left = False
        stopped = False
        detected_reason = ""
        detected_duration = 0.0
        no_decodable_error: Optional[str] = None
        no_decodable_first_seen_wall: Optional[float] = None
        garbled_audio_errors = 0
        first_garbled_audio_error: Optional[str] = None
        active_silence_start: Optional[float] = None
        active_silence_wall: Optional[float] = None
        decoded_frames = 0
        lines: List[str] = []
        line_queue: queue.Queue[str] = queue.Queue()
        probe_started_wall = time.monotonic()
        analysis_window_seconds = self._probe_analysis_window_seconds(config)
        analysis_window_elapsed = False
        analysis_window_duration: Optional[float] = None
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
            no_decodable_required = float(
                config.get(
                    "no_decodable_frames_min_duration_seconds",
                    DEFAULT_CONFIG["no_decodable_frames_min_duration_seconds"],
                )
            )
            silent_audio_required = float(
                config.get(
                    "silent_audio_min_duration_seconds",
                    DEFAULT_CONFIG["silent_audio_min_duration_seconds"],
                )
            )
            garbled_audio_threshold = int(
                config.get(
                    "garbled_audio_error_threshold",
                    DEFAULT_CONFIG["garbled_audio_error_threshold"],
                )
            )
            last_media_time = 0.0
            active_blank_start: Optional[float] = None
            active_blank_wall: Optional[float] = None
            active_freeze_start: Optional[float] = None
            active_freeze_wall: Optional[float] = None
            last_viewer_poll = 0.0
            saw_audio_stream = False
            saw_video_stream = False
            saw_stream_mapping = False

            def observed_duration(media_start: Optional[float], wall_start: Optional[float]) -> float:
                media_duration = 0.0
                if media_start is not None:
                    media_duration = max(0.0, last_media_time - media_start)
                wall_duration = 0.0
                if wall_start is not None:
                    wall_duration = max(0.0, time.monotonic() - wall_start)
                return max(media_duration, wall_duration)

            def no_decodable_observed_duration() -> float:
                start = no_decodable_first_seen_wall if no_decodable_first_seen_wall is not None else probe_started_wall
                return max(0.0, time.monotonic() - start)

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

                    if self._line_has_audio_stream(line):
                        saw_audio_stream = True
                    if self._line_has_video_stream(line):
                        saw_video_stream = True
                    if self._line_marks_stream_mapping(line):
                        saw_stream_mapping = True

                    progress_time = _parse_ffmpeg_progress_time(line)
                    if progress_time is not None:
                        last_media_time = max(last_media_time, progress_time)

                    for frame_match in FFMPEG_FRAME_RE.finditer(line):
                        try:
                            decoded_frames = max(decoded_frames, int(frame_match.group("frames")))
                        except (TypeError, ValueError):
                            continue

                    if config.get("garbled_audio_detection_enabled") and self._line_is_garbled_audio(line):
                        garbled_audio_errors += 1
                        first_garbled_audio_error = first_garbled_audio_error or line.strip()[:160]
                        if garbled_audio_errors >= garbled_audio_threshold:
                            if mark_detection("garbled_audio", 0.0):
                                break

                    if config.get("silent_audio_detection_enabled"):
                        if saw_video_stream and not saw_audio_stream and (
                            saw_stream_mapping or FFMPEG_FRAME_RE.search(line)
                        ):
                            if mark_detection("silent_audio", max(0.0, now - probe_started_wall)):
                                break

                        silence_start = SILENCE_START_RE.search(line)
                        if silence_start:
                            try:
                                active_silence_start = max(0.0, float(silence_start.group("start")))
                                active_silence_wall = now
                            except (TypeError, ValueError):
                                active_silence_start = None
                                active_silence_wall = None

                        silence_duration = SILENCE_DURATION_RE.search(line)
                        if silence_duration:
                            try:
                                silence_secs = max(0.0, float(silence_duration.group("duration")))
                            except (TypeError, ValueError):
                                silence_secs = 0.0
                            active_silence_start = None
                            active_silence_wall = None
                            if silence_secs >= silent_audio_required:
                                if mark_detection("silent_audio", silence_secs):
                                    break

                        silence_end = SILENCE_END_RE.search(line)
                        if silence_end and active_silence_start is not None:
                            try:
                                silence_secs = max(0.0, float(silence_end.group("end")) - active_silence_start)
                            except (TypeError, ValueError):
                                silence_secs = 0.0
                            active_silence_start = None
                            active_silence_wall = None
                            if silence_secs >= silent_audio_required:
                                if mark_detection("silent_audio", silence_secs):
                                    break

                    if (
                        config.get("no_decodable_frames_detection_enabled", True)
                        and decoded_frames <= 0
                    ):
                        lowered_line = line.lower()
                        matched_no_decodable = next(
                            (
                                pattern
                                for pattern in NO_DECODABLE_FRAME_ERROR_PATTERNS
                                if pattern in lowered_line
                            ),
                            None,
                        )
                        if matched_no_decodable and no_decodable_error is None:
                            no_decodable_error = matched_no_decodable
                            no_decodable_first_seen_wall = now

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
                    config.get("silent_audio_detection_enabled")
                    and active_silence_start is not None
                    and observed_duration(active_silence_start, active_silence_wall) >= silent_audio_required
                ):
                    mark_detection("silent_audio", observed_duration(active_silence_start, active_silence_wall))
                    break

                if (
                    no_decodable_error
                    and decoded_frames <= 0
                    and no_decodable_observed_duration() >= no_decodable_required
                ):
                    mark_detection("no_decodable_frames", no_decodable_observed_duration())
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
                        if self._real_client_count(fresh_status, config, target) <= 0:
                            viewer_left = True
                            process.terminate()
                            break
                except Exception as exc:
                    logger.warning(f"Shadow blank monitor viewer poll failed: {exc}")

                if now - probe_started_wall >= analysis_window_seconds:
                    analysis_window_elapsed = True
                    analysis_window_duration = max(0.0, now - probe_started_wall)
                    if process.poll() is None:
                        process.terminate()
                    break

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

            reader.join(timeout=1.0)
            while True:
                try:
                    lines.append(line_queue.get_nowait())
                except queue.Empty:
                    break

            output = "".join(lines)
            no_decodable_probe_duration = no_decodable_observed_duration()
            no_decodable_parsed = self._parse_no_decodable_frames_detection(
                output,
                config,
                observed_duration=no_decodable_probe_duration,
                # Continuous mode intentionally does not treat a fast terminal
                # ffmpeg error as enough by itself. It waits for the configured
                # stall duration so confirmation_count still controls switch speed.
                returncode=0,
            )
            if (
                not detected_reason
                and no_decodable_parsed.get("no_decodable_frames_detected")
                and not viewer_left
                and not stopped
            ):
                detected_reason = "no_decodable_frames"
                detected_duration = float(no_decodable_parsed.get("no_decodable_frames_duration_secs") or 0.0)
            elif (
                not detected_reason
                and no_decodable_error
                and decoded_frames <= 0
                and not viewer_left
                and not stopped
            ):
                while no_decodable_probe_duration < no_decodable_required and not self._stop_event.is_set():
                    try:
                        fresh_status = self._find_status_for_target(udi.get_proxy_status() or {}, target)
                        if self._real_client_count(fresh_status, config, target) <= 0:
                            viewer_left = True
                            break
                    except Exception as exc:
                        logger.warning(f"Shadow blank monitor viewer poll failed: {exc}")
                    time.sleep(0.2)
                    no_decodable_probe_duration = no_decodable_observed_duration()

                no_decodable_parsed = self._parse_no_decodable_frames_detection(
                    output,
                    config,
                    observed_duration=no_decodable_probe_duration,
                    returncode=0,
                )
                if no_decodable_parsed.get("no_decodable_frames_detected") and not viewer_left:
                    detected_reason = "no_decodable_frames"
                    detected_duration = float(no_decodable_parsed.get("no_decodable_frames_duration_secs") or 0.0)

            base_probe_duration = (
                analysis_window_duration
                if analysis_window_elapsed and analysis_window_duration is not None
                else float(duration)
            )
            observed_probe_duration = max(
                base_probe_duration,
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
            if not detected_reason and not viewer_left and not stopped:
                flushed_blank_duration = max(
                    (
                        float(segment.get("duration") or 0.0)
                        for segment in parsed.get("blank_segments") or []
                        if isinstance(segment, dict)
                    ),
                    default=0.0,
                )
                if flushed_blank_duration >= blank_required:
                    detected_reason = "blank"
                    detected_duration = flushed_blank_duration
                elif config.get("freeze_detection_enabled"):
                    flushed_freeze_duration = max(
                        (
                            float(segment.get("duration") or 0.0)
                            for segment in parsed.get("freeze_segments") or []
                            if isinstance(segment, dict)
                        ),
                        default=0.0,
                    )
                    if flushed_freeze_duration >= freeze_required:
                        detected_reason = "freeze"
                        detected_duration = flushed_freeze_duration
            parsed.update(self._parse_audio_detection(
                output,
                config,
                observed_duration=max(detected_duration, time.monotonic() - probe_started_wall),
            ))
            parsed.update(no_decodable_parsed)
            parsed.update(offline_image_probe)
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
            elif detected_reason == "no_decodable_frames":
                parsed.setdefault("blank_detected", False)
                parsed.setdefault("freeze_detected", False)
                parsed["no_decodable_frames_detected"] = True
                parsed["no_decodable_frames_duration_secs"] = round(detected_duration, 3)
                parsed["no_decodable_frames_error"] = (
                    no_decodable_parsed.get("no_decodable_frames_error")
                    or no_decodable_error
                )
            elif detected_reason == "garbled_audio":
                parsed.setdefault("blank_detected", False)
                parsed.setdefault("freeze_detected", False)
                parsed["garbled_audio_detected"] = True
                parsed["garbled_audio_error_count"] = garbled_audio_errors
                parsed["garbled_audio_error"] = first_garbled_audio_error or "audio_decode_errors"
            elif detected_reason == "silent_audio":
                parsed.setdefault("blank_detected", False)
                parsed.setdefault("freeze_detected", False)
                parsed["silent_audio_detected"] = True
                parsed["silent_audio_duration_secs"] = round(detected_duration, 3)
                parsed["silent_audio_noise_db"] = int(config.get("silent_audio_noise_db", DEFAULT_CONFIG["silent_audio_noise_db"]))
            if viewer_left:
                parsed["viewer_left"] = True
            if stopped:
                parsed["stopped"] = True
            return parsed
        finally:
            if process.poll() is None:
                process.kill()

    def _remember_channel_identities(self, channels: Iterable[Dict[str, Any]]) -> None:
        for channel in channels or []:
            self._remember_channel_identity(channel)

    def _remember_channel_identity(self, channel: Optional[Dict[str, Any]]) -> Optional[int]:
        if not isinstance(channel, dict):
            return None
        channel_id = self._coerce_int((channel or {}).get("id"))
        if channel_id is None:
            return None
        channel_uuid = str(channel.get("uuid") or channel.get("channel_uuid") or "").strip()
        stream_ids = [
            stream_id
            for stream_id in (
                self._coerce_int(item.get("id") if isinstance(item, dict) else item)
                for item in (channel.get("streams") or [])
            )
            if stream_id is not None
        ]
        with self._lock:
            if channel_uuid:
                self._known_channel_ids_by_uuid[channel_uuid] = channel_id
            for stream_id in stream_ids:
                self._known_channel_ids_by_stream_id[stream_id] = channel_id
        return channel_id

    def _resolve_channel_id(
        self,
        udi: Any,
        candidate_id: Any,
        *,
        channel_uuid: Any = None,
        channel: Optional[Dict[str, Any]] = None,
        status: Optional[Dict[str, Any]] = None,
        current_stream_id: Any = None,
        included_ids: Optional[Iterable[int]] = None,
        included_uuids: Optional[Iterable[str]] = None,
        previous_target: Optional[Dict[str, Any]] = None,
    ) -> Optional[int]:
        channel_uuid_text = str(channel_uuid or "").strip()
        current_stream_id_int = self._coerce_int(current_stream_id)

        if isinstance(channel, dict):
            if not channel_uuid_text:
                channel_uuid_text = str(channel.get("uuid") or channel.get("channel_uuid") or "").strip()
            resolved = self._remember_channel_identity(channel)
            if resolved is not None:
                return resolved

        for value in (
            candidate_id,
            (status or {}).get("numeric_channel_id") if isinstance(status, dict) else None,
            (status or {}).get("id") if isinstance(status, dict) else None,
            (previous_target or {}).get("channel_id") if isinstance(previous_target, dict) else None,
        ):
            resolved = self._coerce_int(value)
            if resolved is not None:
                self._remember_channel_identity({
                    "id": resolved,
                    "uuid": channel_uuid_text,
                    "streams": [current_stream_id_int] if current_stream_id_int is not None else [],
                })
                return resolved

        if channel_uuid_text:
            with self._lock:
                resolved = self._known_channel_ids_by_uuid.get(channel_uuid_text)
            if resolved is not None:
                return resolved

        if current_stream_id_int is not None:
            with self._lock:
                resolved = self._known_channel_ids_by_stream_id.get(current_stream_id_int)
            if resolved is not None:
                return resolved

        resolved_channel = self._find_channel_for_identity(
            udi,
            channel_uuid=channel_uuid_text,
            stream_id=current_stream_id_int,
        )
        resolved = self._remember_channel_identity(resolved_channel)
        if resolved is not None:
            return resolved

        included_id_set = set(included_ids or [])
        included_uuid_set = {str(item) for item in (included_uuids or [])}
        if len(included_id_set) == 1 and channel_uuid_text and channel_uuid_text in included_uuid_set:
            resolved = next(iter(included_id_set))
            self._remember_channel_identity({
                "id": resolved,
                "uuid": channel_uuid_text,
                "streams": [current_stream_id_int] if current_stream_id_int is not None else [],
            })
            return resolved

        return None

    def _find_channel_for_identity(
        self,
        udi: Any,
        *,
        channel_uuid: str = "",
        stream_id: Optional[int] = None,
    ) -> Optional[Dict[str, Any]]:
        if not hasattr(udi, "get_channels"):
            return None
        try:
            channels = udi.get_channels() or []
        except Exception:
            return None
        for channel in channels:
            if not isinstance(channel, dict):
                continue
            if channel_uuid and str(channel.get("uuid") or channel.get("channel_uuid") or "") == channel_uuid:
                return channel
            if stream_id is not None and self._channel_has_stream(channel, stream_id):
                return channel
        return None

    @staticmethod
    def _channel_has_stream(channel: Dict[str, Any], stream_id: int) -> bool:
        stream_id_text = str(stream_id)
        for item in (channel or {}).get("streams") or []:
            value = item.get("id") if isinstance(item, dict) else item
            if str(value) == stream_id_text:
                return True
        return False

    @staticmethod
    def _coerce_int(value: Any) -> Optional[int]:
        try:
            return int(value) if value is not None and str(value) != "" else None
        except (TypeError, ValueError):
            return None

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
    def _channel_display_name(channel: Optional[Dict[str, Any]], status: Dict[str, Any]) -> Optional[str]:
        for value in (
            (channel or {}).get("name"),
            (channel or {}).get("channel_name"),
            status.get("channel_name"),
            status.get("name"),
        ):
            text = str(value or "").strip()
            if text:
                return text
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

    @staticmethod
    def _normalize_proxy_output_format(value: Any) -> Optional[str]:
        text = str(value or "").strip().lower()
        if not text:
            return None
        # Dispatcharr can expose resolved format keys such as fmp4:p21.
        text = text.split(":", 1)[0].strip()
        return PROXY_OUTPUT_FORMAT_ALIASES.get(text)

    def _real_viewer_output_format(self, status: Dict[str, Any], config: Dict[str, Any]) -> Optional[str]:
        marker = str(config.get("watcher_user_agent") or "").lower()
        clients = status.get("clients")
        if isinstance(clients, dict):
            clients = list(clients.values())
        if isinstance(clients, list):
            for client in clients:
                if marker and marker in self._client_text(client).lower():
                    continue
                if not isinstance(client, dict):
                    continue
                for key in ("output_format", "resolved_output_format", "container", "format"):
                    normalized = self._normalize_proxy_output_format(client.get(key))
                    if normalized:
                        return normalized

        for key in ("output_format", "resolved_output_format", "container", "format"):
            normalized = self._normalize_proxy_output_format(status.get(key))
            if normalized:
                return normalized
        return None

    def _real_client_count(
        self,
        status: Dict[str, Any],
        config: Dict[str, Any],
        target: Optional[Dict[str, Any]] = None,
    ) -> int:
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

        aggregate_count: Optional[int] = None
        for key in ("real_client_count", "client_count", "current_viewers", "viewer_count"):
            try:
                count = int(status.get(key))
            except (TypeError, ValueError):
                continue
            if count >= 0:
                aggregate_count = count
                break

        if target is None:
            if aggregate_count is not None:
                return aggregate_count
            return 1 if self._is_status_active(status) else 0

        probe_started = target.get("active_probe_started_at")
        try:
            probe_age = self.clock() - float(probe_started)
        except (TypeError, ValueError):
            probe_age = 0.0
        probe_is_established = probe_started is not None and probe_age >= AGGREGATE_ONLY_VIEWER_GRACE_SECONDS

        if aggregate_count is not None:
            if not probe_is_established:
                return aggregate_count
            # When Dispatcharr does not expose per-client details, a continuous
            # Shadow probe can be the only remaining downstream client. In that
            # ambiguous state we fail closed: one aggregate client means watcher
            # only, two or more means at least one real viewer remains.
            return max(0, aggregate_count - 1)

        if probe_is_established and self._is_status_active(status):
            return 0
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

        def _watcher_sort_key(item: tuple[int, Any]) -> tuple[int, float, int]:
            index, client = item
            connected_at: Any = None
            if isinstance(client, dict):
                connected_at = client.get("connected_at") or client.get("started_at")
            try:
                parsed = float(connected_at)
            except (TypeError, ValueError):
                return (1, float(index), index)
            return (0, parsed, index)

        index, client = sorted(watcher_clients, key=_watcher_sort_key)[0]
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

    def _clear_cooldown(self, channel_uuid: str) -> None:
        with self._lock:
            self._cooldowns.pop(channel_uuid, None)

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

    def _switch_attempt_matches(
        self,
        channel_uuid: str,
        origin_stream_id: Optional[int],
        target_stream_id: Optional[int],
    ) -> bool:
        try:
            target = int(target_stream_id) if target_stream_id is not None else None
        except (TypeError, ValueError):
            return False
        return target in self._attempted_switch_targets(channel_uuid, origin_stream_id)

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

    @staticmethod
    def _detection_thresholds(config: Dict[str, Any]) -> Dict[str, Any]:
        return {
            key: config.get(key)
            for keys in DETECTION_THRESHOLD_KEYS.values()
            for key in keys
            if key in config
        }

    @staticmethod
    def _event_trigger_reason(event_type: str, details: Dict[str, Any]) -> Optional[str]:
        explicit = (
            details.get("trigger_reason")
            or details.get("reason")
            or PENDING_EVENT_REASONS.get(event_type)
        )
        if explicit:
            return str(explicit)
        if event_type == "probe_ok":
            return "probe_ok"
        if event_type in PRE_PROBE_EVENT_TYPES:
            return "pre_probe"
        if event_type in GUARD_EVENT_TYPES:
            return event_type
        return None

    @staticmethod
    def _event_decision_group(event_type: str) -> str:
        if event_type in SWITCH_EVENT_TYPES:
            return "switch"
        if event_type in PRE_PROBE_EVENT_TYPES:
            return "pre_probe"
        if event_type in GUARD_EVENT_TYPES:
            return "guard"
        if event_type.endswith("_pending") or event_type == "probe_ok":
            return "probe"
        if event_type == "offline_image_learned":
            return "learn"
        if event_type in {"viewer_left", "watcher_reconnecting", "watcher_recovered", "watcher_orphaned"}:
            return "watcher"
        if event_type == "no_alternative":
            return "skip"
        return "other"

    @staticmethod
    def _switch_summary(events: Iterable[Dict[str, Any]], *, active_cooldowns: int = 0) -> Dict[str, Any]:
        summary: Dict[str, Any] = {
            "window_events": 0,
            "successful_switches": 0,
            "failed_switches": 0,
            "dry_run_switches": 0,
            "external_stream_changes": 0,
            "skipped_switches": 0,
            "pre_probe_prevented_switches": 0,
            "loop_pre_probe_required_skips": 0,
            "recovery_guard_prevented_switches": 0,
            "stale_stream_guard_skips": 0,
            "cooldown_skips": 0,
            "rate_limited_skips": 0,
            "quality_guard_skips": 0,
            "active_cooldowns": int(active_cooldowns),
            "prevented_false_switches": 0,
            "last_switch_reason": None,
            "last_switch_at": None,
            "last_switch_result": None,
        }
        for event in events or []:
            if not isinstance(event, dict):
                continue
            summary["window_events"] += 1
            event_type = event.get("type")
            details = event.get("details") if isinstance(event.get("details"), dict) else {}
            reason = (
                details.get("trigger_reason")
                or event.get("trigger_reason")
                or details.get("reason")
            )

            if event_type == "switch_success":
                summary["successful_switches"] += 1
            elif event_type == "switch_failed":
                summary["failed_switches"] += 1
            elif event_type == "dry_run_switch":
                summary["dry_run_switches"] += 1
            elif event_type == "external_stream_change":
                summary["external_stream_changes"] = int(summary.get("external_stream_changes", 0)) + 1
            elif event_type == "no_alternative":
                summary["skipped_switches"] += 1
                if isinstance(details.get("pre_probe"), dict):
                    summary["pre_probe_prevented_switches"] += 1
            elif event_type == "watcher_recovery_guard":
                summary["skipped_switches"] += 1
                summary["recovery_guard_prevented_switches"] += 1
            elif event_type == "loop_pre_probe_required":
                summary["skipped_switches"] += 1
                summary["loop_pre_probe_required_skips"] += 1
            elif event_type == "stale_stream_guard":
                summary["skipped_switches"] += 1
                summary["stale_stream_guard_skips"] += 1
            elif event_type == "cooldown":
                summary["cooldown_skips"] += 1
            elif event_type == "switch_rate_limited":
                summary["skipped_switches"] += 1
                summary["rate_limited_skips"] += 1
            elif event_type == "quality_check_active":
                summary["quality_guard_skips"] += 1

            if event_type in SWITCH_EVENT_TYPES and summary["last_switch_result"] is None:
                summary["last_switch_result"] = event_type
                summary["last_switch_reason"] = reason
                summary["last_switch_at"] = event.get("timestamp")

        summary["prevented_false_switches"] = (
            summary["pre_probe_prevented_switches"]
            + summary["loop_pre_probe_required_skips"]
            + summary["recovery_guard_prevented_switches"]
            + summary["stale_stream_guard_skips"]
            + summary["rate_limited_skips"]
        )
        return summary

    @staticmethod
    def _compact_probe_context(
        reason: Optional[str],
        probe: Optional[Dict[str, Any]],
        thresholds: Optional[Dict[str, Any]],
    ) -> Optional[Dict[str, Any]]:
        if not reason or reason not in DETECTION_REASONS or not isinstance(probe, dict):
            return None

        measurements = {
            key: probe.get(key)
            for key in DETECTION_MEASUREMENT_KEYS.get(reason, ())
            if probe.get(key) is not None
        }
        threshold_values = {
            key: (thresholds or {}).get(key)
            for key in DETECTION_THRESHOLD_KEYS.get(reason, ())
            if (thresholds or {}).get(key) is not None
        }
        if not measurements and not threshold_values:
            return {"reason": reason}
        context: Dict[str, Any] = {"reason": reason}
        if measurements:
            context["measurements"] = measurements
        if threshold_values:
            context["thresholds"] = threshold_values
        return context

    @staticmethod
    def _viewer_context(target: Dict[str, Any]) -> Dict[str, Any]:
        allowed = (
            "real_client_count",
            "watcher_client_count",
            "watcher_state",
            "watcher_client_ref",
            "watcher_uptime_seconds",
            "watcher_absent_seconds",
            "watcher_recovered_after_seconds",
            "persistent_watcher_state",
            "persistent_watcher_uptime_seconds",
        )
        return {
            key: target.get(key)
            for key in allowed
            if target.get(key) is not None
        }

    def _event_details_with_context(
        self,
        event_type: str,
        target: Dict[str, Any],
        details: Optional[Dict[str, Any]],
    ) -> Dict[str, Any]:
        enriched = dict(details or {})
        reason = self._event_trigger_reason(event_type, enriched)
        if reason:
            enriched.setdefault("trigger_reason", reason)
        if target.get("stream_ref"):
            enriched.setdefault("origin_stream_ref", target.get("stream_ref"))

        viewer_context = self._viewer_context(target)
        if viewer_context:
            enriched.setdefault("viewer_context", viewer_context)

        detection_context = self._compact_probe_context(
            reason,
            target.get("last_probe"),
            target.get("last_probe_thresholds"),
        )
        if detection_context:
            if "confirmations" in enriched:
                detection_context["confirmations"] = enriched.get("confirmations")
            if "required" in enriched:
                detection_context["required"] = enriched.get("required")
            enriched.setdefault("detection", detection_context)

        if event_type in SWITCH_EVENT_TYPES:
            enriched.setdefault("switch_result", event_type)
        return enriched

    def _record_event(self, event_type: str, target: Dict[str, Any], details: Dict[str, Any]) -> None:
        details = self._event_details_with_context(event_type, target, details)
        event = {
            "timestamp": self.clock(),
            "type": event_type,
            "decision_group": self._event_decision_group(event_type),
            "trigger_reason": details.get("trigger_reason"),
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
        if event_type == "viewer_left" and target.get("channel_uuid"):
            channel_uuid = str(target.get("channel_uuid"))
            self._stop_persistent_watcher(channel_uuid)
            with self._lock:
                self._last_loop_probe_started_at.pop(channel_uuid, None)

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
            "channel_id",
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
            "persistent_watcher_state",
            "persistent_watcher_started_at",
            "persistent_watcher_uptime_seconds",
            "persistent_watcher_pid",
            "state",
            "current_program",
            "channel_name",
            "cooldown_seconds",
            "viewer_output_format",
            "last_probe",
            "last_event",
        }
        return {key: value for key, value in target.items() if key in allowed}

    @staticmethod
    def _public_excluded_target(target: Dict[str, Any]) -> Dict[str, Any]:
        allowed = {
            "channel_ref",
            "channel_id",
            "channel_name",
            "stream_ref",
            "real_client_count",
            "watcher_client_count",
            "state",
            "current_program",
            "viewer_output_format",
            "exclude_reason",
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
