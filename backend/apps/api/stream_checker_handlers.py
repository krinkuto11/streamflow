"""Stream checker API handler functions extracted from web_api."""

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
        force_check = data.get("force_check", False)

        if "channel_id" in data:
            channel_id = data["channel_id"]
            priority = data.get("priority", 10)
            success = service.queue_channel(channel_id, priority, force_check=force_check)
            if success:
                return jsonify({"message": f"Channel {channel_id} queued successfully"})
            return jsonify({"error": "Failed to queue channel"}), 500

        if "channel_ids" in data:
            channel_ids = data["channel_ids"]
            priority = data.get("priority", 10)
            added = service.queue_channels(channel_ids, priority, force_check=force_check)
            return jsonify({"message": f"Queued {added} channels successfully", "added": added})

        return jsonify({"error": "Must provide channel_id or channel_ids"}), 400
    except Exception as exc:
        logger.error(f"Error adding to stream checker queue: {exc}")
        return jsonify({"error": "Internal Server Error"}), 500


def clear_stream_checker_queue_response(*, get_stream_checker_service: Callable[[], Any]):
    """Handle clearing stream checker queue."""
    try:
        service = get_stream_checker_service()
        result = service.clear_queue()
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
            "max_workers": global_limit,
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

        if result.get("success") or result.get("skipped"):
            return jsonify(result), 200

        # no_profile: user configuration error — channel has no automation profile.
        # Return 400 so the frontend can distinguish this from a generic failure
        # and surface a precise, actionable message rather than "Check Failed".
        if result.get("error") == "no_profile":
            return jsonify(result), 400

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
