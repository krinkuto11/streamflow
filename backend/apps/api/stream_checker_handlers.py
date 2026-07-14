"""Stream checker API handler functions extracted from web_api."""

import math
import re
from typing import Any, Callable, Dict, Optional

from flask import jsonify

from apps.core.logging_config import setup_logging
from apps.stream.queue_start import (
    QUEUE_START_MODES,
    coerce_channel_id as _coerce_channel_id,
    order_channels_for_queue_start,
)
from apps.telemetry.last_quality_stats import get_last_quality_stats

logger = setup_logging(__name__)

_QUEUE_REQUEST_METADATA_KEYS = {'source', 'run_label'}
_QUEUE_REQUEST_METADATA_VALUE_RE = re.compile(
    r'^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$'
)
_RESERVED_SPECIALIZED_QUEUE_SOURCES = {'teamarr_preflight', 'auto_create'}
_QUEUE_GUARD_MAX_ENTRIES = 2048
_QUEUE_GUARD_MAX_METADATA_NODES = 512
_QUEUE_GUARD_MAX_METADATA_DEPTH = 8
_QUEUE_GUARD_MAX_METADATA_STRING_BYTES = 16384


def _coerce_bool(value: Any, default: bool = False) -> bool:
    """Coerce common JSON/form boolean values without treating "false" as True."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on", "enabled"}:
            return True
        if normalized in {"0", "false", "no", "off", "disabled"}:
            return False
    return bool(value)


def _validate_queue_request_metadata(payload: Dict[str, Any]):
    """Return safe public queue metadata or a client-facing validation error."""
    raw_metadata = payload.get('metadata')
    if raw_metadata is None:
        return None, None
    if not isinstance(raw_metadata, dict):
        return None, 'metadata must be an object'
    unexpected = set(raw_metadata) - _QUEUE_REQUEST_METADATA_KEYS
    if unexpected:
        return None, (
            'metadata contains unsupported fields: '
            + ', '.join(sorted(str(key) for key in unexpected))
        )

    metadata = {}
    for key in _QUEUE_REQUEST_METADATA_KEYS:
        if key not in raw_metadata:
            continue
        value = raw_metadata.get(key)
        if not isinstance(value, str) or not _QUEUE_REQUEST_METADATA_VALUE_RE.fullmatch(
            value.strip()
        ):
            return None, f'metadata.{key} has an invalid value'
        metadata[key] = value.strip()
    source = metadata.get('source')
    if source in _RESERVED_SPECIALIZED_QUEUE_SOURCES:
        return None, 'metadata.source is reserved for internal queue producers'
    if not metadata:
        return None, 'metadata must include source or run_label'
    return metadata, None


def _validate_guard_metadata_complexity(metadata: Dict[str, Any]):
    """Reject deeply nested or oversized metadata before service locking."""
    stack = [(metadata, 1)]
    node_count = 0
    while stack:
        value, depth = stack.pop()
        node_count += 1
        if node_count > _QUEUE_GUARD_MAX_METADATA_NODES:
            return 'metadata contains too many values'
        if depth > _QUEUE_GUARD_MAX_METADATA_DEPTH:
            return 'metadata is nested too deeply'
        if isinstance(value, dict):
            if (
                node_count + len(stack) + len(value)
                > _QUEUE_GUARD_MAX_METADATA_NODES
            ):
                return 'metadata contains too many values'
            for key, child in value.items():
                if not isinstance(key, str):
                    return 'metadata object keys must be strings'
                try:
                    key_size = len(key.encode('utf-8'))
                except UnicodeEncodeError:
                    return 'metadata contains invalid Unicode'
                if key_size > _QUEUE_GUARD_MAX_METADATA_STRING_BYTES:
                    return 'metadata contains an oversized key'
                stack.append((child, depth + 1))
        elif isinstance(value, list):
            if (
                node_count + len(stack) + len(value)
                > _QUEUE_GUARD_MAX_METADATA_NODES
            ):
                return 'metadata contains too many values'
            stack.extend((child, depth + 1) for child in value)
        elif isinstance(value, str):
            try:
                value_size = len(value.encode('utf-8'))
            except UnicodeEncodeError:
                return 'metadata contains invalid Unicode'
            if value_size > _QUEUE_GUARD_MAX_METADATA_STRING_BYTES:
                return 'metadata contains an oversized string'
        elif isinstance(value, float) and not math.isfinite(value):
            return 'metadata contains a non-finite number'
        elif value is not None and not isinstance(value, (bool, int, float)):
            return 'metadata contains a non-JSON value'
    return None


def _validate_expected_queue_snapshot(payload: Any):
    """Normalize the exact queue snapshot accepted by guarded clear."""
    if payload is None or payload == {}:
        return None, None
    if not isinstance(payload, dict):
        return None, 'clear request body must be an object'
    if 'expected_queue_snapshot' not in payload:
        return None, 'expected_queue_snapshot is required for a non-empty clear request'

    raw_snapshot = payload.get('expected_queue_snapshot')
    if not isinstance(raw_snapshot, dict):
        return None, 'expected_queue_snapshot must be an object'

    required_fields = {
        'entries_complete',
        'admission_epoch',
        'admission_revision',
        'paused',
        'queued_entries',
        'in_progress_entries',
        'completed_entries',
        'failed_entries',
        'completed_channel_ids',
        'failed_channel_ids',
    }
    missing_fields = required_fields - set(raw_snapshot)
    if missing_fields:
        return None, (
            'expected_queue_snapshot is missing fields: '
            + ', '.join(sorted(missing_fields))
        )
    if raw_snapshot.get('entries_complete') is not True:
        return None, 'expected_queue_snapshot.entries_complete must be true'

    paused = raw_snapshot.get('paused')
    if not isinstance(paused, bool):
        return None, 'expected_queue_snapshot.paused must be a boolean'

    admission_epoch = raw_snapshot.get('admission_epoch')
    if (
        not isinstance(admission_epoch, str)
        or re.fullmatch(r'[0-9a-f]{32}', admission_epoch) is None
    ):
        return None, (
            'expected_queue_snapshot.admission_epoch must be a 32-character '
            'lowercase hexadecimal string'
        )

    revision = raw_snapshot.get('admission_revision')
    if (
        not isinstance(revision, int)
        or isinstance(revision, bool)
        or revision < 0
    ):
        return None, 'expected_queue_snapshot.admission_revision must be a non-negative integer'

    normalized_snapshot = {
        'entries_complete': True,
        'admission_epoch': admission_epoch,
        'admission_revision': revision,
        'paused': paused,
    }
    lifecycle_channel_ids = set()
    entry_channel_ids = {}
    entry_field_names = (
        'queued_entries',
        'in_progress_entries',
        'completed_entries',
        'failed_entries',
    )
    raw_entry_lists = {
        field_name: raw_snapshot.get(field_name)
        for field_name in entry_field_names
    }
    for field_name, raw_entries in raw_entry_lists.items():
        if not isinstance(raw_entries, list):
            return None, f'expected_queue_snapshot.{field_name} must be an array'
    if sum(len(entries) for entries in raw_entry_lists.values()) > _QUEUE_GUARD_MAX_ENTRIES:
        return None, 'expected_queue_snapshot contains too many lifecycle entries'

    terminal_fields = (
        ('completed_channel_ids', 'completed_entries'),
        ('failed_channel_ids', 'failed_entries'),
    )
    raw_terminal_id_lists = {
        field_name: raw_snapshot.get(field_name)
        for field_name, _entries_field_name in terminal_fields
    }
    for field_name, entries_field_name in terminal_fields:
        raw_channel_ids = raw_terminal_id_lists[field_name]
        if not isinstance(raw_channel_ids, list):
            return None, f'expected_queue_snapshot.{field_name} must be an array'
        # Reject redundant terminal-id payload amplification in O(1) before
        # iterating it. Each id must have exactly one bounded terminal entry.
        if len(raw_channel_ids) != len(raw_entry_lists[entries_field_name]):
            return None, (
                f'expected_queue_snapshot.{field_name} must exactly match '
                f'{entries_field_name} channel ids'
            )

    for field_name in entry_field_names:
        raw_entries = raw_snapshot.get(field_name)
        normalized_entries = []
        entry_identities = set()
        for index, raw_entry in enumerate(raw_entries):
            if not isinstance(raw_entry, dict):
                return None, (
                    f'expected_queue_snapshot.{field_name}[{index}] must be an object'
                )
            channel_id = raw_entry.get('channel_id')
            entry_token = raw_entry.get('entry_token')
            metadata = raw_entry.get('metadata')
            if (
                not isinstance(channel_id, int)
                or isinstance(channel_id, bool)
                or channel_id <= 0
            ):
                return None, (
                    f'expected_queue_snapshot.{field_name}[{index}].channel_id '
                    'must be a positive integer'
                )
            if (
                not isinstance(entry_token, int)
                or isinstance(entry_token, bool)
                or entry_token < 0
            ):
                return None, (
                    f'expected_queue_snapshot.{field_name}[{index}].entry_token '
                    'must be a non-negative integer'
                )
            if not isinstance(metadata, dict):
                return None, (
                    f'expected_queue_snapshot.{field_name}[{index}].metadata '
                    'must be an object'
                )
            metadata_error = _validate_guard_metadata_complexity(metadata)
            if metadata_error:
                return None, (
                    f'expected_queue_snapshot.{field_name}[{index}].'
                    + metadata_error
                )
            identity = (channel_id, entry_token)
            if identity in entry_identities or channel_id in lifecycle_channel_ids:
                return None, (
                    f'expected_queue_snapshot.{field_name} contains duplicate '
                    f'channel identity {channel_id}'
                )
            entry_identities.add(identity)
            lifecycle_channel_ids.add(channel_id)
            normalized_entries.append({
                'channel_id': channel_id,
                'entry_token': entry_token,
                'metadata': dict(metadata),
            })
        normalized_snapshot[field_name] = normalized_entries
        entry_channel_ids[field_name] = {
            entry['channel_id'] for entry in normalized_entries
        }

    for field_name, entries_field_name in terminal_fields:
        raw_channel_ids = raw_terminal_id_lists[field_name]
        normalized_channel_ids = []
        seen_channel_ids = set()
        for index, channel_id in enumerate(raw_channel_ids):
            if (
                not isinstance(channel_id, int)
                or isinstance(channel_id, bool)
                or channel_id <= 0
            ):
                return None, (
                    f'expected_queue_snapshot.{field_name}[{index}] '
                    'must be a positive integer'
                )
            if channel_id in seen_channel_ids:
                return None, (
                    f'expected_queue_snapshot.{field_name} contains duplicate '
                    f'channel id {channel_id}'
                )
            seen_channel_ids.add(channel_id)
            normalized_channel_ids.append(channel_id)
        if set(normalized_channel_ids) != entry_channel_ids[entries_field_name]:
            return None, (
                f'expected_queue_snapshot.{field_name} must exactly match '
                f'{entries_field_name} channel ids'
            )
        normalized_snapshot[field_name] = normalized_channel_ids

    return normalized_snapshot, None


def _coerce_stream_id_from_payload(payload: Any) -> Optional[int]:
    """Resolve a Dispatcharr stream id from supported payload keys/references."""
    if not isinstance(payload, dict):
        return None

    for key in ("stream_id", "id", "stream_reference", "stream_ref"):
        candidate = payload.get(key)
        if isinstance(candidate, int):
            return candidate
        if isinstance(candidate, str):
            value = candidate.strip()
            if value.isdigit():
                return int(value)
            match = re.fullmatch(
                r"(?:stream|stream_id|dispatcharr[-_ ]?stream)[-_: ]+(\d+)",
                value,
                flags=re.IGNORECASE,
            )
            if match:
                return int(match.group(1))
    return None


def _automation_run_is_active(manager: Any) -> bool:
    """Return True only while an automation cycle is actively executing."""
    if manager is None:
        return False

    inspected_run_status = False
    try:
        run_status = manager.get_run_status() if hasattr(manager, "get_run_status") else None
    except Exception as exc:
        logger.debug("Could not inspect automation run status before single-channel check: %s", exc)
        run_status = None

    if isinstance(run_status, dict):
        inspected_run_status = True
        state = str(run_status.get("state") or run_status.get("status") or "").lower()
        if run_status.get("active") is True or state == "running":
            return True
        return False
    if inspected_run_status or hasattr(manager, "get_run_status"):
        return False

    thread = getattr(manager, "automation_thread", None)
    try:
        thread_alive = bool(thread and thread.is_alive())
    except Exception:
        thread_alive = False
    return bool(thread_alive and getattr(manager, "automation_running", False))


def _stream_checker_work_is_active(service: Any) -> bool:
    """Return True when Stream Checker is already doing active work."""
    try:
        status = service.get_status() if hasattr(service, "get_status") else None
    except Exception as exc:
        logger.debug("Could not inspect stream checker status before single-channel check: %s", exc)
        return False

    if not isinstance(status, dict):
        return False
    progress = status.get("progress") if isinstance(status.get("progress"), dict) else {}
    if (
        progress.get("is_single_channel_check")
        and not progress.get("stale")
        and not status.get("progress_stale")
    ):
        return True
    queue = status.get("queue") if isinstance(status.get("queue"), dict) else {}
    queue_active = (
        int(queue.get("queue_size") or 0) > 0
        or int(queue.get("in_progress") or 0) > 0
        or queue.get("current_channel") is not None
    )
    return bool(status.get("checking") or queue_active)


def _sanitize_hardware_acceleration_status(status: Any) -> Dict[str, Any]:
    """Return hardware diagnostics without host-specific error or device inventory details."""
    if not isinstance(status, dict):
        return {}

    safe: Dict[str, Any] = {}

    if isinstance(status.get("config"), dict):
        safe["config"] = dict(status["config"])

    for key in (
        "ffmpeg_available",
        "mode_supported",
        "nvidia_checked",
        "nvidia_visible_devices_set",
        "nvidia_smi_available",
        "nvidia_smi_ok",
        "dri_device_configured",
        "dri_available",
    ):
        safe[key] = bool(status.get(key))

    hardware_backend = status.get("hardware_backend")
    if isinstance(hardware_backend, str) and hardware_backend.strip():
        safe["hardware_backend"] = hardware_backend.strip()[:40]
    else:
        safe["hardware_backend"] = "unknown"

    ffmpeg_hwaccels = status.get("ffmpeg_hwaccels")
    if isinstance(ffmpeg_hwaccels, list):
        safe["ffmpeg_hwaccels"] = [
            str(method)
            for method in ffmpeg_hwaccels
            if isinstance(method, (str, int, float)) and str(method).strip()
        ]
    else:
        safe["ffmpeg_hwaccels"] = []

    dri_hwaccels = status.get("dri_hwaccels")
    if isinstance(dri_hwaccels, list):
        safe["dri_hwaccels"] = [
            str(method)
            for method in dri_hwaccels
            if isinstance(method, (str, int, float)) and str(method).strip()
        ]
    else:
        safe["dri_hwaccels"] = []

    nvidia_gpus = status.get("nvidia_gpus")
    safe["nvidia_gpu_count"] = len(nvidia_gpus) if isinstance(nvidia_gpus, list) else 0

    safe["diagnostics_available"] = not bool(status.get("error"))
    safe["ffmpeg_diagnostics_available"] = not bool(status.get("ffmpeg_error"))
    safe["nvidia_diagnostics_available"] = not bool(status.get("nvidia_error"))

    return safe


def start_stream_checker_response(*, get_stream_checker_service: Callable[[], Any]):
    """Handle starting the stream checker service."""
    try:
        service = get_stream_checker_service()
        service.start()
        return jsonify({"message": "Stream checker started successfully", "status": "running"})
    except Exception as exc:
        logger.error(f"Error starting stream checker: {exc}")
        return jsonify({"error": "Internal Server Error"}), 500


def stop_stream_checker_response(*, get_stream_checker_service: Callable[[], Any]):
    """Handle stopping the stream checker service."""
    try:
        service = get_stream_checker_service()
        service.stop()
        return jsonify({"message": "Stream checker stopped successfully", "status": "stopped"})
    except Exception as exc:
        logger.error(f"Error stopping stream checker: {exc}")
        return jsonify({"error": "Internal Server Error"}), 500


def get_stream_checker_queue_response(*, get_stream_checker_service: Callable[[], Any]):
    """Handle retrieval of stream checker queue status."""
    try:
        service = get_stream_checker_service()
        status = service.get_status()
        return jsonify(status.get("queue", {}))
    except Exception as exc:
        logger.error(f"Error getting stream checker queue: {exc}")
        return jsonify({"error": "Internal Server Error"}), 500


def add_to_stream_checker_queue_response(
    *,
    payload: Any,
    get_stream_checker_service: Callable[[], Any],
):
    """Handle enqueueing one or more channels for stream checking."""
    try:
        data = payload
        if not data:
            return jsonify({"error": "No data provided"}), 400

        service = get_stream_checker_service()
        force_check = _coerce_bool(data.get("force_check"), False)
        metadata, metadata_error = _validate_queue_request_metadata(data)
        if metadata_error:
            return jsonify({"error": metadata_error}), 400
        queue_kwargs = {"force_check": force_check}
        if metadata is not None:
            queue_kwargs["metadata"] = metadata
            queue_kwargs["immutable_metadata_keys"] = set(metadata)

        if "channel_id" in data:
            channel_id = data["channel_id"]
            priority = data.get("priority", 10)
            success = service.queue_channel(
                channel_id,
                priority,
                **queue_kwargs,
            )
            if success:
                return jsonify({"message": f"Channel {channel_id} queued successfully"})
            return jsonify({"error": "Failed to queue channel"}), 500

        if "channel_ids" in data:
            channel_ids = data["channel_ids"]
            priority = data.get("priority", 10)
            added = service.queue_channels(
                channel_ids,
                priority,
                **queue_kwargs,
            )
            return jsonify({"message": f"Queued {added} channels successfully", "added": added})

        return jsonify({"error": "Must provide channel_id or channel_ids"}), 400
    except Exception as exc:
        logger.error(f"Error adding to stream checker queue: {exc}")
        return jsonify({"error": "Internal Server Error"}), 500


def clear_stream_checker_queue_response(
    *,
    get_stream_checker_service: Callable[[], Any],
    payload: Any = None,
):
    """Handle clearing stream checker queue."""
    try:
        expected_snapshot, validation_error = _validate_expected_queue_snapshot(payload)
        if validation_error:
            return jsonify({'error': validation_error}), 400

        service = get_stream_checker_service()
        if expected_snapshot is None:
            result = service.clear_queue()
        else:
            result = service.clear_queue(expected_queue_snapshot=expected_snapshot)
        if isinstance(result, dict) and result.get('guard_matched') is False:
            return jsonify({
                'error': 'queue_snapshot_mismatch',
                'message': 'Queue changed after the expected snapshot was read',
                **result,
            }), 409
        return jsonify({"message": "Queue cleared successfully", **(result or {})})
    except Exception as exc:
        logger.error(f"Error clearing stream checker queue: {exc}")
        return jsonify({"error": "Internal Server Error"}), 500


def get_stream_checker_config_response(*, get_stream_checker_service: Callable[[], Any]):
    """Handle retrieval of stream checker configuration."""
    try:
        service = get_stream_checker_service()
        return jsonify(service.config.config)
    except Exception as exc:
        logger.error(f"Error getting stream checker config: {exc}")
        return jsonify({"error": "Internal Server Error"}), 500


def get_stream_checker_hardware_status_response(*, get_stream_checker_service: Callable[[], Any]):
    """Handle retrieval of optional hardware acceleration runtime status."""
    try:
        service = get_stream_checker_service()
        status = service.get_hardware_acceleration_status()
        return jsonify(_sanitize_hardware_acceleration_status(status))
    except Exception as exc:
        logger.error("Error getting stream checker hardware status", exc_info=True)
        return jsonify({"error": "Internal Server Error"}), 500


def get_stream_checker_progress_response(*, get_stream_checker_service: Callable[[], Any]):
    """Handle retrieval of current stream checker progress."""
    try:
        service = get_stream_checker_service()
        status = service.get_status()
        return jsonify(status.get("progress", {}))
    except Exception as exc:
        logger.error(f"Error getting stream checker progress: {exc}")
        return jsonify({"error": "Internal Server Error"}), 500


def get_stream_last_quality_stats_response(*, stream_id: int, stale_after_hours: Optional[float] = None):
    """Return the latest persisted quality stats for one stream without probing it."""
    try:
        return jsonify(
            get_last_quality_stats(
                stream_id,
                stale_after_hours=stale_after_hours,
            )
        )
    except Exception as exc:
        logger.error("Error getting last quality stats for stream %s: %s", stream_id, exc, exc_info=True)
        return jsonify({"error": "Internal Server Error"}), 500


def check_specific_channel_response(
    *,
    payload: Any,
    get_stream_checker_service: Callable[[], Any],
):
    """Handle immediate high-priority queueing of one channel."""
    try:
        data = payload
        if not data or "channel_id" not in data:
            return jsonify({"error": "channel_id required"}), 400

        channel_id = data["channel_id"]
        service = get_stream_checker_service()

        success = service.queue_channel(channel_id, priority=100)
        if success:
            return jsonify({"message": f"Channel {channel_id} queued for immediate checking"})
        return jsonify({"error": "Failed to queue channel"}), 500
    except Exception as exc:
        logger.error(f"Error checking specific channel: {exc}")
        return jsonify({"error": "Internal Server Error"}), 500


def get_stream_checker_status_response(
    *,
    get_stream_checker_service: Callable[[], Any],
    concurrent_streams_enabled_key: str,
    concurrent_streams_global_limit_key: str,
):
    """Handle retrieval of stream checker status with parallel metadata."""
    try:
        service = get_stream_checker_service()
        status = service.get_status()

        concurrent_enabled = service.config.get(concurrent_streams_enabled_key, True)
        global_limit = service.config.get(concurrent_streams_global_limit_key, 10)

        status["parallel"] = {
            "enabled": concurrent_enabled,
            "max_workers": global_limit if concurrent_enabled else 1,
            "configured_max_workers": global_limit,
            "mode": "parallel" if concurrent_enabled else "sequential",
        }

        return jsonify(status)
    except Exception as exc:
        logger.error(f"Error getting stream checker status: {exc}")
        return jsonify({"error": "Internal Server Error"}), 500


def update_stream_checker_config_response(
    *,
    payload: Any,
    croniter_available: bool,
    croniter_module: Any,
    get_stream_checker_service: Callable[[], Any],
    get_automation_manager: Callable[[], Any],
    get_automation_config_manager: Callable[[], Any],
    check_wizard_complete: Callable[[], bool],
    stop_scheduled_event_processor: Callable[[], Any],
    stop_epg_refresh_processor: Callable[[], Any],
    start_scheduled_event_processor: Callable[[], Any],
    start_epg_refresh_processor: Callable[[], Any],
    scheduled_event_processor_running: bool,
    epg_refresh_running: bool,
):
    """Handle stream checker configuration update and dependent lifecycle transitions."""
    try:
        data = payload
        if not data:
            return jsonify({"error": "No configuration data provided"}), 400

        if isinstance(data.get("queue"), dict):
            queue_config = data["queue"]
            start_mode = queue_config.get("start_mode")
            if start_mode is not None:
                normalized_mode = str(start_mode).strip().lower()
                if normalized_mode not in QUEUE_START_MODES:
                    return jsonify({"error": "Invalid queue start mode"}), 400
                queue_config["start_mode"] = normalized_mode
            if "start_channel_id" in queue_config:
                queue_config["start_channel_id"] = _coerce_channel_id(queue_config.get("start_channel_id"))
            if queue_config.get("start_mode") == "channel" and queue_config.get("start_channel_id") is None:
                return jsonify({"error": "Queue start channel is required for selected-channel mode"}), 400

        if "global_check_schedule" in data and "cron_expression" in data["global_check_schedule"]:
            cron_expr = data["global_check_schedule"]["cron_expression"]
            if cron_expr:
                if croniter_available:
                    try:
                        if not croniter_module.is_valid(cron_expr):
                            return jsonify({"error": f"Invalid cron expression: {cron_expr}"}), 400
                    except Exception as exc:
                        logger.error(f"Cron expression validation error: {exc}")
                        return jsonify({"error": "Invalid cron expression format"}), 400
                else:
                    logger.warning("croniter not available - cron expression validation skipped")

        service = get_stream_checker_service()
        service.update_config(data)

        if "automation_controls" in data and check_wizard_complete():
            automation_controls = data["automation_controls"]
            manager = get_automation_manager()
            automation_config = get_automation_config_manager()

            global_settings = automation_config.get_global_settings()
            regular_automation_enabled = global_settings.get("regular_automation_enabled", False)

            any_automation_enabled = (
                automation_controls.get("auto_m3u_updates", False)
                or automation_controls.get("auto_stream_matching", False)
                or automation_controls.get("auto_quality_checking", False)
                or automation_controls.get("scheduled_global_action", False)
            )

            if not any_automation_enabled:
                if service.running:
                    service.stop()
                    logger.info("Stream checker service stopped (all automation disabled)")
                if manager.automation_running:
                    manager.stop_automation()
                    logger.info("Automation service stopped (all automation disabled)")

                stop_scheduled_event_processor()
                stop_epg_refresh_processor()
            else:
                if not service.running:
                    service.start()
                    logger.info("Stream checker service auto-started after config update")

                if not manager.automation_running:
                    manager.start_automation()
                    logger.info("Automation service auto-started after config update")

                if not scheduled_event_processor_running:
                    start_scheduled_event_processor()
                    logger.info("Scheduled event processor auto-started after config update")
                if not epg_refresh_running:
                    start_epg_refresh_processor()
                    logger.info("EPG refresh processor auto-started after config update")

        return jsonify({"message": "Configuration updated successfully", "config": service.config.config})
    except Exception as exc:
        logger.error(f"Error updating stream checker config: {exc}")
        return jsonify({"error": "Internal Server Error"}), 500


def check_single_channel_now_response(
    *,
    payload: Any,
    get_stream_checker_service: Callable[[], Any],
    get_automation_manager: Optional[Callable[[], Any]] = None,
):
    """Handle immediate synchronous check for one channel.

    The backend enforces the opt-in model: a channel must have an automation
    profile assigned (via an automation period or, for EPG checks, an EPG
    scheduled profile override) before a health check can run. When neither
    resolves, the service returns error='no_profile'.

    Returns:
        200  — check ran successfully
        400  — channel_id missing, OR channel has no automation profile assigned
               (no_profile). 400 is used for no_profile rather than 500 because
               this is a user configuration error, not an internal server fault.
               The frontend pre-flight catches this before reaching the API in
               the normal path; the backend guard fires as a safety net (e.g.
               periods exist but none are currently active at execution time).
        500  — unexpected internal error
    """
    try:
        data = payload
        if not data or "channel_id" not in data:
            return jsonify({"error": "channel_id required"}), 400

        channel_id = data["channel_id"]
        # profile_id is optionally supplied when the user explicitly chose a profile
        # via the ProfilePickerDialog (multi-period channel). When present it is
        # forwarded to the service so the correct profile governs the check.
        forced_profile_id = data.get("profile_id")
        force_check = bool(data.get("force_check", False))
        service = get_stream_checker_service()
        if get_automation_manager is not None:
            try:
                if _automation_run_is_active(get_automation_manager()):
                    return jsonify(
                        {
                            "success": False,
                            "error": "automation_run_active",
                            "message": (
                                "A single-channel full check cannot run while an automation run is active. "
                                "Queue the channel check instead or wait until the automation run finishes."
                            ),
                        }
                    ), 409
            except Exception as exc:
                logger.warning("Could not enforce automation-run guard for single-channel check: %s", exc)

        if _stream_checker_work_is_active(service):
            return jsonify(
                {
                    "success": False,
                    "error": "stream_checker_active",
                    "message": (
                        "A single-channel full check cannot run while Stream Checker is already active. "
                        "Queue the channel check instead or wait for the active run to finish."
                    ),
                }
            ), 409

        result = service.check_single_channel(
            channel_id,
            forced_profile_id=forced_profile_id,
            force_check=force_check,
        )

        if result.get("error") == "aborted":
            return jsonify(result), 409
        if result.get("success") or result.get("skipped"):
            return jsonify(result), 200

        # no_profile: user configuration error — channel has no automation profile.
        # Return 400 so the frontend can distinguish this from a generic failure
        # and surface a precise, actionable message rather than "Check Failed".
        if result.get("error") == "no_profile":
            return jsonify(result), 400
        if result.get("error") == "stream_checker_active":
            return jsonify(result), 409

        logger.warning(
            "Single-channel check failed; returning sanitized error response for channel %s",
            channel_id,
        )
        return jsonify({"success": False, "error": "Internal Server Error"}), 500

    except Exception as exc:
        logger.error(f"Error checking single channel: {exc}")
        return jsonify({"error": "Internal Server Error"}), 500


def check_single_stream_now_response(
    *,
    payload: Any,
    get_stream_checker_service: Callable[[], Any],
    get_automation_manager: Optional[Callable[[], Any]] = None,
    stream_id: Optional[int] = None,
):
    """Handle an immediate synchronous check for one Dispatcharr stream.

    Unlike check_single_channel_now_response, this path does not require the
    stream to be assigned to any channel and never mutates channel membership.
    It only probes the stream and can optionally persist its stream_stats.
    """
    try:
        data = payload if isinstance(payload, dict) else {}
        resolved_stream_id = stream_id if stream_id is not None else _coerce_stream_id_from_payload(data)
        if resolved_stream_id is None:
            return jsonify({"success": False, "error": "stream_id required"}), 400

        service = get_stream_checker_service()
        if get_automation_manager is not None:
            try:
                if _automation_run_is_active(get_automation_manager()):
                    return jsonify(
                        {
                            "success": False,
                            "error": "automation_run_active",
                            "message": (
                                "A single-stream check cannot run while an automation run is active. "
                                "Wait until the automation run finishes."
                            ),
                        }
                    ), 409
            except Exception as exc:
                logger.warning("Could not enforce automation-run guard for single-stream check: %s", exc)

        if _stream_checker_work_is_active(service):
            return jsonify(
                {
                    "success": False,
                    "error": "stream_checker_active",
                    "message": (
                        "A single-stream check cannot run while Stream Checker is already active. "
                        "Wait for the active run to finish."
                    ),
                }
            ), 409

        result = service.check_single_stream(
            resolved_stream_id,
            persist=_coerce_bool(data.get("persist"), True),
            blank_check_enabled=_coerce_bool(
                data.get("blank_check_enabled", data.get("detect_blank")),
                False,
            ),
            freeze_check_enabled=_coerce_bool(
                data.get("freeze_check_enabled", data.get("detect_freeze")),
                False,
            ),
            loop_check_enabled=_coerce_bool(
                data.get("loop_check_enabled", data.get("detect_loop")),
                False,
            ),
        )

        if result.get("success"):
            return jsonify(result), 200

        error_code = result.get("error")
        if error_code == "stream_not_found":
            return jsonify(result), 404
        if error_code in {"invalid_stream_id", "stream_missing_url"}:
            return jsonify(result), 400
        if error_code == "connectivity_guard":
            return jsonify(result), 503
        if error_code == "provider_capacity_unavailable":
            # The internal provider-wait analysis contains the resolved stream
            # URL, which may embed provider credentials. Capacity conflicts only
            # need stable public metadata and must never echo that analysis.
            public_keys = {
                "success",
                "error",
                "reason_detail",
                "stream_id",
                "stream_name",
                "run_mode",
                "persisted",
                "message",
            }
            public_result = {
                key: value
                for key, value in result.items()
                if key in public_keys
            }
            return jsonify(public_result), 409
        if error_code in {
            "aborted",
            "stream_checker_active",
        }:
            return jsonify(result), 409

        logger.warning(
            "Single-stream check failed; returning sanitized error response for stream %s",
            resolved_stream_id,
        )
        return jsonify({"success": False, "error": "Internal Server Error"}), 500

    except Exception as exc:
        logger.error("Error checking single stream: %s", exc, exc_info=True)
        return jsonify({"error": "Internal Server Error"}), 500


def mark_channels_updated_response(
    *,
    payload: Any,
    get_stream_checker_service: Callable[[], Any],
):
    """Handle marking channels as updated in the stream-check update tracker."""
    try:
        data = payload
        if not data:
            return jsonify({"error": "No data provided"}), 400

        service = get_stream_checker_service()

        if "channel_id" in data:
            channel_id = data["channel_id"]
            service.update_tracker.mark_channel_updated(channel_id)
            return jsonify({"message": f"Channel {channel_id} marked as updated"})

        if "channel_ids" in data:
            channel_ids = data["channel_ids"]
            service.update_tracker.mark_channels_updated(channel_ids)
            return jsonify({"message": f"Marked {len(channel_ids)} channels as updated"})

        return jsonify({"error": "Must provide channel_id or channel_ids"}), 400
    except Exception as exc:
        logger.error(f"Error marking channels updated: {exc}")
        return jsonify({"error": "Internal Server Error"}), 500


def queue_all_channels_response(
    *,
    payload: Any = None,
    get_stream_checker_service: Callable[[], Any],
    get_udi_manager: Callable[[], Any],
):
    """Handle queueing all channels for a full stream check run."""
    try:
        service = get_stream_checker_service()
        payload_data = payload if isinstance(payload, dict) else {}
        service_config = getattr(service, "config", {}) or {}
        default_mode = service_config.get("queue.start_mode", "first")
        default_channel_id = service_config.get("queue.start_channel_id", None)
        start_mode = payload_data.get("start_mode", default_mode)
        start_channel_id = payload_data.get("start_channel_id", default_channel_id)

        udi = get_udi_manager()
        channels = udi.get_channels()

        if not channels:
            return jsonify({"error": "Could not fetch channels"}), 500

        try:
            ordered_channels, start_meta = order_channels_for_queue_start(
                channels,
                start_mode=start_mode,
                start_channel_id=start_channel_id,
            )
        except ValueError as exc:
            logger.info("Invalid queue start selection: %s", exc)
            return jsonify({"error": "Invalid queue start selection"}), 400

        channel_ids = [channel["id"] for channel in ordered_channels]
        if not channel_ids:
            return jsonify({"message": "No channels found to queue", "count": 0})

        service.update_tracker.mark_channels_updated(channel_ids)
        added = service.queue_channels(channel_ids, priority=10)

        return jsonify(
            {
                "message": f"Queued {added} channels for checking",
                "total_channels": len(channel_ids),
                "queued": added,
                "start": start_meta,
            }
        )
    except Exception as exc:
        logger.error(f"Error queueing all channels: {exc}")
        return jsonify({"error": "Internal Server Error"}), 500
