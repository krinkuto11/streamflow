"""Stream checker API handler functions extracted from web_api."""

from typing import Any, Callable

from flask import jsonify

from apps.core.logging_config import setup_logging

logger = setup_logging(__name__)


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
        service.clear_queue()
        return jsonify({"message": "Queue cleared successfully"})
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


def get_stream_checker_progress_response(*, get_stream_checker_service: Callable[[], Any]):
    """Handle retrieval of current stream checker progress."""
    try:
        service = get_stream_checker_service()
        status = service.get_status()
        return jsonify(status.get("progress", {}))
    except Exception as exc:
        logger.error(f"Error getting stream checker progress: {exc}")
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

                if regular_automation_enabled and not manager.automation_running:
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
        service = get_stream_checker_service()
        result = service.check_single_channel(channel_id, forced_profile_id=forced_profile_id)

        if result.get("success"):
            return jsonify(result), 200

        # no_profile: user configuration error — channel has no automation profile.
        # Return 400 so the frontend can distinguish this from a generic failure
        # and surface a precise, actionable message rather than "Check Failed".
        if result.get("error") == "no_profile":
            return jsonify(result), 400

        return jsonify(result), 500

    except Exception as exc:
        logger.error(f"Error checking single channel: {exc}")
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
    get_stream_checker_service: Callable[[], Any],
    get_udi_manager: Callable[[], Any],
):
    """Handle queueing all channels for a full stream check run."""
    try:
        service = get_stream_checker_service()

        udi = get_udi_manager()
        channels = udi.get_channels()

        if not channels:
            return jsonify({"error": "Could not fetch channels"}), 500

        channel_ids = [channel["id"] for channel in channels if isinstance(channel, dict) and "id" in channel]
        if not channel_ids:
            return jsonify({"message": "No channels found to queue", "count": 0})

        service.update_tracker.mark_channels_updated(channel_ids)
        added = service.check_queue.add_channels(channel_ids, priority=10)

        return jsonify(
            {
                "message": f"Queued {added} channels for checking",
                "total_channels": len(channel_ids),
                "queued": added,
            }
        )
    except Exception as exc:
        logger.error(f"Error queueing all channels: {exc}")
        return jsonify({"error": "Internal Server Error"}), 500
