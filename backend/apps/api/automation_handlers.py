"""Automation API handler functions extracted from web_api."""

from typing import Any, Callable, Dict, Optional

from flask import jsonify

from apps.api.schemas import (
    AutomationPeriodCreateSchema,
    AutomationPeriodUpdateSchema,
    AutomationProfileCreateSchema,
    AutomationProfileUpdateSchema,
    BatchPeriodAssignmentsSchema,
    BatchPeriodUsageSchema,
    MultiEntityProfileAssignmentSchema,
    PeriodAssignmentSchema,
    PeriodRemovalSchema,
    ProfileIdsBulkDeleteSchema,
    SingleEntityProfileAssignmentSchema,
)
from apps.core.api_responses import error_response
from apps.core.exceptions import ValidationError
from apps.core.logging_config import setup_logging

logger = setup_logging(__name__)


def _validation_error(exc: ValidationError):
    return error_response(
        exc.message,
        status_code=exc.status_code,
        code=exc.error_code,
        details=exc.details,
    )


def handle_global_automation_settings_response(
    *,
    method: str,
    updates: Optional[Dict[str, Any]],
    get_automation_config_manager: Callable[[], Any],
    check_wizard_complete: Callable[[], bool],
    get_automation_manager: Callable[[], Any],
):
    """Get or update global automation settings and lifecycle state."""
    config_manager = get_automation_config_manager()
    global_settings_keys = {
        "regular_automation_enabled",
        "validate_existing_streams",
        "playlist_update_interval_minutes",
    }

    if method == "GET":
        try:
            settings = config_manager.get_global_settings()
            manager = get_automation_manager()
            settings["enabled_m3u_accounts"] = manager.config.get("enabled_m3u_accounts", [])
            return jsonify(settings), 200
        except Exception as exc:
            logger.error(f"Error getting global automation settings: {exc}")
            return jsonify({"error": "Internal Server Error"}), 500

    if method == "PUT":
        try:
            if not updates:
                return jsonify({"error": "No data provided"}), 400

            old_settings = config_manager.get_global_settings()
            old_regular_enabled = old_settings.get("regular_automation_enabled", False)
            manager = get_automation_manager()

            global_updates = {key: updates[key] for key in global_settings_keys if key in updates}

            manager_updates = {}
            if "enabled_m3u_accounts" in updates:
                raw_accounts = updates.get("enabled_m3u_accounts")
                if raw_accounts is None:
                    raw_accounts = []
                if not isinstance(raw_accounts, list):
                    return jsonify({"error": "enabled_m3u_accounts must be a list"}), 400

                normalized_accounts = []
                for account_id in raw_accounts:
                    try:
                        normalized_accounts.append(int(account_id))
                    except (TypeError, ValueError):
                        return jsonify(
                            {"error": "enabled_m3u_accounts must contain numeric account IDs"}
                        ), 400

                manager_updates["enabled_m3u_accounts"] = normalized_accounts

            if not global_updates and not manager_updates:
                return jsonify({"error": "No valid settings provided"}), 400

            global_update_success = True
            if global_updates:
                global_update_success = config_manager.update_global_settings(settings=global_updates)

            if manager_updates:
                manager.update_config(manager_updates)

            if global_update_success:
                new_settings = config_manager.get_global_settings()
                new_settings["enabled_m3u_accounts"] = manager.config.get("enabled_m3u_accounts", [])
                new_regular_enabled = new_settings.get("regular_automation_enabled", False)

                if old_regular_enabled != new_regular_enabled and check_wizard_complete():
                    if new_regular_enabled:
                        if not manager.automation_running:
                            manager.start_automation()
                            logger.info(
                                "Automation service started (Enable Regular Automation toggled ON via /settings)"
                            )
                    else:
                        if manager.automation_running:
                            manager.stop_automation()
                            logger.info(
                                "Automation service stopped (Enable Regular Automation toggled OFF via /settings)"
                            )

                return jsonify({"message": "Global automation settings updated", "settings": new_settings}), 200

            return jsonify({"error": "Failed to update global settings"}), 500
        except Exception as exc:
            logger.error(f"Error updating global automation settings: {exc}")
            return jsonify({"error": "Internal Server Error"}), 500

    return jsonify({"error": "Method not allowed"}), 405


def get_automation_status_response(
    *,
    get_automation_manager: Callable[[], Any],
    get_automation_config_manager: Callable[[], Any],
):
    """Handle retrieval of automation runtime status and dashboard summary metrics."""
    try:
        manager = get_automation_manager()

        thread_alive = manager.automation_thread is not None and manager.automation_thread.is_alive()
        is_running = thread_alive and manager.automation_running

        config_manager = get_automation_config_manager()
        profiles = config_manager.get_all_profiles()

        profiles_count = len(profiles)
        stream_checking_enabled = any(
            profile.get("stream_checking", {}).get("enabled", False) for profile in profiles
        )

        return (
            jsonify(
                {
                    "running": is_running,
                    "thread_alive": thread_alive,
                    "last_playlist_update": (
                        manager.last_playlist_update.isoformat() if manager.last_playlist_update else None
                    ),
                    "profiles_count": profiles_count,
                    "stream_checking_enabled": stream_checking_enabled,
                }
            ),
            200,
        )
    except Exception as exc:
        logger.error(f"Error getting automation status: {exc}")
        return jsonify({"error": "Internal Server Error"}), 500


def start_automation_service_api_response(*, get_automation_manager: Callable[[], Any]):
    """Handle start request for automation background service."""
    try:
        manager = get_automation_manager()

        if manager.automation_thread and manager.automation_thread.is_alive():
            return jsonify({"message": "Automation service is already running"}), 200

        manager.start_automation()
        return jsonify({"message": "Automation service started"}), 200
    except Exception as exc:
        logger.error(f"Error starting automation service: {exc}")
        return jsonify({"error": "Internal Server Error"}), 500


def stop_automation_service_api_response(*, get_automation_manager: Callable[[], Any]):
    """Handle stop request for automation background service."""
    try:
        manager = get_automation_manager()
        manager.stop_automation()
        return jsonify({"message": "Automation service stopped"}), 200
    except Exception as exc:
        logger.error(f"Error stopping automation service: {exc}")
        return jsonify({"error": "Internal Server Error"}), 500


def trigger_automation_cycle_response(*, payload: Optional[Dict[str, Any]], get_automation_manager: Callable[[], Any]):
    """Handle manual trigger of an automation cycle, optionally for a single period."""
    try:
        data = payload or {}
        period_id = data.get("period_id")
        force = data.get("force", True)

        manager = get_automation_manager()

        if manager.automation_running:
            manager.trigger_automation(period_id=period_id, force=force)
            return (
                jsonify(
                    {
                        "message": (
                            f"Automation cycle triggered{' for period ' + period_id if period_id else ''}"
                        )
                    }
                ),
                200,
            )

        if force:
            import threading

            def run_forced():
                try:
                    manager.run_automation_cycle(forced=True, forced_period_id=period_id)
                except Exception as exc:
                    logger.error(f"Error in forced automation cycle: {exc}")

            threading.Thread(target=run_forced).start()
            return (
                jsonify(
                    {
                        "message": (
                            f"Forced automation cycle started{' for period ' + period_id if period_id else ''}"
                        )
                    }
                ),
                200,
            )

        return jsonify({"error": "Automation service is not running"}), 400
    except Exception as exc:
        logger.error(f"Error triggering automation cycle: {exc}")
        return jsonify({"error": "Internal Server Error"}), 500


def handle_automation_profiles_response(
    *,
    method: str,
    args: Any,
    payload: Optional[Dict[str, Any]],
    get_automation_config_manager: Callable[[], Any],
):
    """Get all automation profiles or create a new profile."""
    try:
        automation_config = get_automation_config_manager()

        if method == "GET":
            search = args.get("search", "").strip()
            page_param = args.get("page", None)
            per_page_param = args.get("per_page", "50")

            page: Optional[int] = None
            if page_param is not None:
                try:
                    page = max(1, int(page_param))
                except (ValueError, TypeError):
                    return error_response(
                        "Invalid page parameter: must be an integer",
                        status_code=400,
                        code="validation_error",
                    )

            try:
                per_page = min(max(int(per_page_param), 1), 200)
            except (ValueError, TypeError):
                per_page = 50

            profiles = automation_config.get_all_profiles(search=search, page=page, per_page=per_page)
            return jsonify(profiles), 200

        if method == "POST":
            data = AutomationProfileCreateSchema.from_payload(payload or {}).profile_data

            profile = automation_config.create_profile(data)
            if profile:
                return jsonify(profile), 201
            return error_response("Failed to create profile", status_code=500, code="internal_error")

        return error_response("Method not allowed", status_code=405, code="method_not_allowed")
    except ValidationError as exc:
        return _validation_error(exc)
    except Exception as exc:
        logger.error(f"Error handling automation profiles: {exc}")
        return error_response("Internal Server Error", status_code=500, code="internal_error")


def bulk_delete_automation_profiles_response(
    *,
    payload: Optional[Dict[str, Any]],
    get_automation_config_manager: Callable[[], Any],
):
    """Delete multiple automation profiles at once."""
    try:
        automation_config = get_automation_config_manager()
        data = ProfileIdsBulkDeleteSchema.from_payload(payload or {})
        profile_ids = data.profile_ids

        deleted_count = 0
        failed_ids = []

        for pid in profile_ids:
            if pid == "default":
                continue
            if automation_config.delete_profile(pid):
                deleted_count += 1
            else:
                failed_ids.append(pid)

        return (
            jsonify(
                {
                    "message": f"Deleted {deleted_count} profiles",
                    "deleted_count": deleted_count,
                    "failed_ids": failed_ids,
                }
            ),
            200,
        )
    except ValidationError as exc:
        return _validation_error(exc)
    except Exception as exc:
        logger.error(f"Error bulk deleting automation profiles: {exc}")
        return error_response("Internal Server Error", status_code=500, code="internal_error")


def handle_automation_profile_response(
    *,
    method: str,
    profile_id: str,
    payload: Optional[Dict[str, Any]],
    get_automation_config_manager: Callable[[], Any],
):
    """Get, update, or delete a specific automation profile."""
    try:
        automation_config = get_automation_config_manager()

        if method == "GET":
            profile = automation_config.get_profile(profile_id)
            if profile:
                return jsonify(profile), 200
            return error_response("Profile not found", status_code=404, code="not_found")

        if method == "PUT":
            data = AutomationProfileUpdateSchema.from_payload(payload or {}).profile_data

            updated_profile = automation_config.update_profile(profile_id, data)
            if updated_profile:
                return jsonify(updated_profile), 200
            return error_response("Profile not found or update failed", status_code=404, code="not_found")

        if method == "DELETE":
            if automation_config.delete_profile(profile_id):
                return jsonify({"message": "Profile deleted"}), 200
            return error_response("Profile not found or delete failed", status_code=404, code="not_found")

        return error_response("Method not allowed", status_code=405, code="method_not_allowed")
    except ValidationError as exc:
        return _validation_error(exc)
    except Exception as exc:
        logger.error(f"Error handling automation profile {profile_id}: {exc}")
        return error_response("Internal Server Error", status_code=500, code="internal_error")


def assign_automation_profile_channel_response(
    *,
    payload: Optional[Dict[str, Any]],
    get_automation_config_manager: Callable[[], Any],
):
    """Assign an automation profile to a channel."""
    try:
        automation_config = get_automation_config_manager()
        data = SingleEntityProfileAssignmentSchema.from_payload(payload or {}, entity_field="channel_id")
        channel_id = data.entity_id
        profile_id = data.profile_id

        if automation_config.assign_profile_to_channel(channel_id, profile_id):
            return jsonify({"message": f"Profile {profile_id} assigned to channel {channel_id}"}), 200
        return error_response("Failed to assign profile", status_code=500, code="internal_error")
    except ValidationError as exc:
        return _validation_error(exc)
    except Exception as exc:
        logger.error(f"Error assigning profile to channel: {exc}")
        return error_response("Internal Server Error", status_code=500, code="internal_error")


def assign_automation_profile_channels_response(
    *,
    payload: Optional[Dict[str, Any]],
    get_automation_config_manager: Callable[[], Any],
):
    """Assign an automation profile to multiple channels."""
    try:
        automation_config = get_automation_config_manager()
        data = MultiEntityProfileAssignmentSchema.from_payload(payload or {}, entity_field="channel_ids")
        channel_ids = data.entity_ids
        profile_id = data.profile_id

        if automation_config.assign_profile_to_channels(channel_ids, profile_id):
            return jsonify({"message": f"Profile {profile_id} assigned to {len(channel_ids)} channels"}), 200
        return error_response("Failed to assign profile to channels", status_code=500, code="internal_error")
    except ValidationError as exc:
        return _validation_error(exc)
    except Exception as exc:
        logger.error(f"Error assigning profile to channels: {exc}")
        return error_response("Internal Server Error", status_code=500, code="internal_error")


def assign_automation_profile_group_response(
    *,
    method: str,
    payload: Optional[Dict[str, Any]],
    get_automation_config_manager: Callable[[], Any],
):
    """Get or assign/remove automation profile mappings for channel groups."""
    try:
        automation_config = get_automation_config_manager()

        if method == "GET":
            assignments = automation_config.get_all_group_assignments()
            return jsonify(assignments), 200

        data = SingleEntityProfileAssignmentSchema.from_payload(payload or {}, entity_field="group_id")
        group_id = data.entity_id
        profile_id = data.profile_id

        if automation_config.assign_profile_to_group(group_id, profile_id):
            return jsonify({"message": f"Profile {profile_id} assigned to group {group_id}"}), 200
        return error_response("Failed to assign profile", status_code=500, code="internal_error")
    except ValidationError as exc:
        return _validation_error(exc)
    except Exception as exc:
        logger.error(f"Error assigning profile to group: {exc}")
        return error_response("Internal Server Error", status_code=500, code="internal_error")


def assign_epg_scheduled_profile_channel_response(
    *,
    payload: Optional[Dict[str, Any]],
    get_automation_config_manager: Callable[[], Any],
):
    """Assign an EPG scheduled automation profile to a channel."""
    try:
        automation_config = get_automation_config_manager()
        data = SingleEntityProfileAssignmentSchema.from_payload(payload or {}, entity_field="channel_id")
        channel_id = data.entity_id
        profile_id = data.profile_id

        if automation_config.assign_epg_scheduled_profile_to_channel(channel_id, profile_id):
            return jsonify({"message": f"EPG scheduled profile {profile_id} assigned to channel {channel_id}"}), 200
        return error_response("Failed to assign EPG scheduled profile", status_code=500, code="internal_error")
    except ValidationError as exc:
        return _validation_error(exc)
    except Exception as exc:
        logger.error(f"Error assigning EPG scheduled profile to channel: {exc}")
        return error_response("Internal Server Error", status_code=500, code="internal_error")


def assign_epg_scheduled_profile_channels_response(
    *,
    payload: Optional[Dict[str, Any]],
    get_automation_config_manager: Callable[[], Any],
):
    """Assign an EPG scheduled automation profile to multiple channels."""
    try:
        automation_config = get_automation_config_manager()
        data = MultiEntityProfileAssignmentSchema.from_payload(payload or {}, entity_field="channel_ids")
        channel_ids = data.entity_ids
        profile_id = data.profile_id

        if automation_config.assign_epg_scheduled_profile_to_channels(channel_ids, profile_id):
            return (
                jsonify(
                    {"message": f"EPG scheduled profile {profile_id} assigned to {len(channel_ids)} channels"}
                ),
                200,
            )
        return error_response(
            "Failed to assign EPG scheduled profile to channels",
            status_code=500,
            code="internal_error",
        )
    except ValidationError as exc:
        return _validation_error(exc)
    except Exception as exc:
        logger.error(f"Error assigning EPG scheduled profile to channels: {exc}")
        return error_response("Internal Server Error", status_code=500, code="internal_error")


def assign_epg_scheduled_profile_group_response(
    *,
    method: str,
    payload: Optional[Dict[str, Any]],
    get_automation_config_manager: Callable[[], Any],
):
    """Get or assign/remove EPG scheduled profile mappings for channel groups."""
    try:
        automation_config = get_automation_config_manager()

        if method == "GET":
            assignments = automation_config.get_all_group_epg_scheduled_assignments()
            return jsonify(assignments), 200

        data = SingleEntityProfileAssignmentSchema.from_payload(payload or {}, entity_field="group_id")
        group_id = data.entity_id
        profile_id = data.profile_id

        if automation_config.assign_epg_scheduled_profile_to_group(group_id, profile_id):
            return jsonify({"message": f"EPG scheduled profile {profile_id} assigned to group {group_id}"}), 200
        return error_response("Failed to assign EPG scheduled profile", status_code=500, code="internal_error")
    except ValidationError as exc:
        return _validation_error(exc)
    except Exception as exc:
        logger.error(f"Error assigning EPG scheduled profile to group: {exc}")
        return error_response("Internal Server Error", status_code=500, code="internal_error")


def get_group_automation_periods_response(
    *,
    group_id: int,
    get_automation_config_manager: Callable[[], Any],
):
    """Get all automation periods assigned to a group."""
    try:
        automation_config = get_automation_config_manager()
        period_assignments = automation_config.get_group_periods(group_id)

        periods = []
        for pid, profile_id in period_assignments.items():
            period = automation_config.get_period(pid)
            if period:
                period_copy = period.copy()
                period_copy["profile_id"] = profile_id
                profile = automation_config.get_profile(profile_id)
                if profile:
                    period_copy["profile_name"] = profile.get("name")
                periods.append(period_copy)

        return jsonify(periods), 200
    except Exception as exc:
        logger.error(f"Error getting automation periods for group {group_id}: {exc}")
        return jsonify({"error": "Internal Server Error"}), 500


def assign_period_to_groups_response(
    *,
    period_id: str,
    payload: Optional[Dict[str, Any]],
    get_automation_config_manager: Callable[[], Any],
):
    """Assign an automation period to one or more groups with a profile."""
    try:
        automation_config = get_automation_config_manager()
        data = PeriodAssignmentSchema.from_payload(payload or {}, entity_field="group_ids")
        group_ids = data.entity_ids
        profile_id = data.profile_id
        replace = data.replace

        if automation_config.assign_period_to_groups(period_id, group_ids, profile_id, replace):
            return (
                jsonify(
                    {
                        "message": f"Period {period_id} with profile {profile_id} assigned to {len(group_ids)} groups",
                        "group_ids": group_ids,
                    }
                ),
                200,
            )
        return error_response("Failed to assign period to groups", status_code=500, code="internal_error")
    except ValidationError as exc:
        return _validation_error(exc)
    except Exception as exc:
        logger.error(f"Error assigning period to groups: {exc}")
        return error_response("Internal Server Error", status_code=500, code="internal_error")


def remove_period_from_groups_response(
    *,
    period_id: str,
    payload: Optional[Dict[str, Any]],
    get_automation_config_manager: Callable[[], Any],
):
    """Remove an automation period from specific groups."""
    try:
        automation_config = get_automation_config_manager()
        data = PeriodRemovalSchema.from_payload(payload or {}, entity_field="group_ids")
        group_ids = data.entity_ids

        if automation_config.remove_period_from_groups(period_id, group_ids):
            return jsonify({"message": f"Period {period_id} removed from {len(group_ids)} groups"}), 200
        return error_response("Failed to remove period from groups", status_code=500, code="internal_error")
    except ValidationError as exc:
        return _validation_error(exc)
    except Exception as exc:
        logger.error(f"Error removing period from groups: {exc}")
        return error_response("Internal Server Error", status_code=500, code="internal_error")


def batch_assign_periods_to_groups_response(
    *,
    payload: Optional[Dict[str, Any]],
    get_automation_config_manager: Callable[[], Any],
):
    """Batch assign automation periods to multiple groups with profiles."""
    try:
        automation_config = get_automation_config_manager()
        data = BatchPeriodAssignmentsSchema.from_payload(payload or {}, entity_field="group_ids")

        group_ids = data.entity_ids
        period_assignments = data.period_assignments
        replace = data.replace

        is_first = True
        for assignment in period_assignments:
            pid = assignment["period_id"]
            profile_id = assignment["profile_id"]
            automation_config.assign_period_to_groups(pid, group_ids, profile_id, replace and is_first)
            is_first = False

        return (
            jsonify(
                {
                    "message": (
                        f"Assigned {len(period_assignments)} period-profile pairs to {len(group_ids)} groups"
                    )
                }
            ),
            200,
        )
    except ValidationError as exc:
        return _validation_error(exc)
    except Exception as exc:
        logger.error(f"Error batch assigning periods to groups: {exc}")
        return error_response("Internal Server Error", status_code=500, code="internal_error")


def handle_automation_periods_response(
    *,
    method: str,
    args: Any,
    payload: Optional[Dict[str, Any]],
    get_automation_config_manager: Callable[[], Any],
    croniter_available: bool,
    croniter_module: Any,
):
    """Get all automation periods or create a new period."""
    try:
        automation_config = get_automation_config_manager()

        if method == "GET":
            search = args.get("search", "").strip()
            page_param = args.get("page", None)
            per_page_param = args.get("per_page", "50")

            page: Optional[int] = None
            if page_param is not None:
                try:
                    page = max(1, int(page_param))
                except (ValueError, TypeError):
                    return error_response(
                        "Invalid page parameter: must be an integer",
                        status_code=400,
                        code="validation_error",
                    )

            try:
                per_page = min(max(int(per_page_param), 1), 200)
            except (ValueError, TypeError):
                per_page = 50

            result = automation_config.get_all_periods(search=search, page=page, per_page=per_page)

            if page is None:
                for period in result:
                    channels = automation_config.get_period_channels(period["id"])
                    period["channel_count"] = len(channels)
                return jsonify(result), 200

            for period in result["items"]:
                channels = automation_config.get_period_channels(period["id"])
                period["channel_count"] = len(channels)
            return jsonify(result), 200

        if method == "POST":
            data = AutomationPeriodCreateSchema.from_payload(payload or {}).period_data

            schedule = data["schedule"]
            if schedule.get("type") == "cron":
                if not croniter_available or croniter_module is None:
                    return error_response(
                        "Cron scheduling is not available because the 'croniter' package is missing",
                        status_code=400,
                        code="validation_error",
                    )
                if not croniter_module.is_valid(schedule.get("value", "")):
                    return error_response("Invalid cron expression", status_code=400, code="validation_error")

            period_id = automation_config.create_period(data)
            if period_id:
                period = automation_config.get_period(period_id)
                return jsonify(period), 201
            return error_response("Failed to create period", status_code=500, code="internal_error")

        return error_response("Method not allowed", status_code=405, code="method_not_allowed")
    except ValidationError as exc:
        return _validation_error(exc)
    except Exception as exc:
        logger.error(f"Error handling automation periods: {exc}")
        return error_response("Internal Server Error", status_code=500, code="internal_error")


def handle_automation_period_response(
    *,
    method: str,
    period_id: str,
    payload: Optional[Dict[str, Any]],
    get_automation_config_manager: Callable[[], Any],
    croniter_available: bool,
    croniter_module: Any,
):
    """Get, update, or delete a specific automation period."""
    try:
        automation_config = get_automation_config_manager()

        if method == "GET":
            period = automation_config.get_period(period_id)
            if period:
                channels = automation_config.get_period_channels(period_id)
                period["channels"] = channels
                return jsonify(period), 200
            return error_response("Period not found", status_code=404, code="not_found")

        if method == "PUT":
            data = AutomationPeriodUpdateSchema.from_payload(payload or {}).period_data

            if "schedule" in data:
                schedule = data["schedule"]
                if schedule.get("type") == "cron":
                    if not croniter_available or croniter_module is None:
                        return error_response(
                            "Cron scheduling is not available because the 'croniter' package is missing",
                            status_code=400,
                            code="validation_error",
                        )
                    if not croniter_module.is_valid(schedule.get("value", "")):
                        return error_response("Invalid cron expression", status_code=400, code="validation_error")

            if automation_config.update_period(period_id, data):
                period = automation_config.get_period(period_id)
                return jsonify(period), 200
            return error_response("Period not found or update failed", status_code=404, code="not_found")

        if method == "DELETE":
            if automation_config.delete_period(period_id):
                return jsonify({"message": "Period deleted"}), 200
            return error_response("Period not found or delete failed", status_code=404, code="not_found")

        return error_response("Method not allowed", status_code=405, code="method_not_allowed")
    except ValidationError as exc:
        return _validation_error(exc)
    except Exception as exc:
        logger.error(f"Error handling automation period {period_id}: {exc}")
        return error_response("Internal Server Error", status_code=500, code="internal_error")


def assign_period_to_channels_response(
    *,
    period_id: str,
    payload: Optional[Dict[str, Any]],
    get_automation_config_manager: Callable[[], Any],
):
    """Assign an automation period to multiple channels with a profile."""
    try:
        automation_config = get_automation_config_manager()
        data = PeriodAssignmentSchema.from_payload(payload or {}, entity_field="channel_ids")
        channel_ids = data.entity_ids
        profile_id = data.profile_id
        replace = data.replace

        if automation_config.assign_period_to_channels(period_id, channel_ids, profile_id, replace):
            return (
                jsonify(
                    {
                        "message": f"Period {period_id} with profile {profile_id} assigned to {len(channel_ids)} channels",
                        "channel_ids": channel_ids,
                    }
                ),
                200,
            )
        return error_response("Failed to assign period to channels", status_code=500, code="internal_error")
    except ValidationError as exc:
        return _validation_error(exc)
    except Exception as exc:
        logger.error(f"Error assigning period to channels: {exc}")
        return error_response("Internal Server Error", status_code=500, code="internal_error")


def remove_period_from_channels_response(
    *,
    period_id: str,
    payload: Optional[Dict[str, Any]],
    get_automation_config_manager: Callable[[], Any],
):
    """Remove an automation period from specific channels."""
    try:
        automation_config = get_automation_config_manager()
        data = PeriodRemovalSchema.from_payload(payload or {}, entity_field="channel_ids")
        channel_ids = data.entity_ids

        if automation_config.remove_period_from_channels(period_id, channel_ids):
            return jsonify({"message": f"Period {period_id} removed from {len(channel_ids)} channels"}), 200
        return error_response("Failed to remove period from channels", status_code=500, code="internal_error")
    except ValidationError as exc:
        return _validation_error(exc)
    except Exception as exc:
        logger.error(f"Error removing period from channels: {exc}")
        return error_response("Internal Server Error", status_code=500, code="internal_error")


def get_period_channels_response(
    *,
    period_id: str,
    get_automation_config_manager: Callable[[], Any],
    get_udi_manager: Callable[[], Any],
):
    """Get all channels assigned to a period."""
    try:
        automation_config = get_automation_config_manager()
        period = automation_config.get_period(period_id)

        if not period:
            return jsonify({"error": "Period not found"}), 404

        channel_ids = automation_config.get_period_channels(period_id)

        try:
            udi = get_udi_manager()
            channels = []
            for cid in channel_ids:
                channel = udi.get_channel(cid)
                if channel:
                    channels.append({"id": cid, "number": channel.get("number"), "name": channel.get("name")})
                else:
                    channels.append({"id": cid})
            return jsonify(channels), 200
        except Exception:
            return jsonify([{"id": cid} for cid in channel_ids]), 200
    except Exception as exc:
        logger.error(f"Error getting channels for period {period_id}: {exc}")
        return jsonify({"error": "Internal Server Error"}), 500


def get_batch_period_usage_response(
    *,
    payload: Optional[Dict[str, Any]],
    get_automation_config_manager: Callable[[], Any],
):
    """Analyze automation period usage across multiple channels."""
    try:
        data = BatchPeriodUsageSchema.from_payload(payload or {})
        channel_ids = data.channel_ids

        automation_config = get_automation_config_manager()
        period_usage: Dict[str, Dict[str, Any]] = {}

        for ch_id in channel_ids:
            assignments = automation_config.get_channel_periods(ch_id)
            for pid, prof_id in assignments.items():
                if pid not in period_usage:
                    period = automation_config.get_period(pid)
                    if not period:
                        continue
                    period_usage[pid] = {
                        "id": pid,
                        "name": period.get("name", "Unknown"),
                        "count": 0,
                        "profile_breakdown": {},
                    }

                usage = period_usage[pid]
                usage["count"] += 1

                if prof_id not in usage["profile_breakdown"]:
                    profile = automation_config.get_profile(prof_id)
                    usage["profile_breakdown"][prof_id] = {
                        "id": prof_id,
                        "name": profile.get("name", "Unknown") if profile else "Unknown",
                        "channel_ids": [],
                    }

                usage["profile_breakdown"][prof_id]["channel_ids"].append(ch_id)

        results = []
        for pid, usage in period_usage.items():
            results.append(
                {
                    "id": pid,
                    "name": usage["name"],
                    "count": usage["count"],
                    "percentage": round((usage["count"] / len(channel_ids)) * 100, 1),
                    "profiles": list(usage["profile_breakdown"].values()),
                }
            )

        results.sort(key=lambda x: x["count"], reverse=True)

        return jsonify({"periods": results, "total_channels": len(channel_ids)}), 200
    except ValidationError as exc:
        return _validation_error(exc)
    except Exception as exc:
        logger.error(f"Error getting batch period usage: {exc}")
        return error_response("Internal Server Error", status_code=500, code="internal_error")


def get_channel_automation_periods_response(
    *,
    channel_id: int,
    get_automation_config_manager: Callable[[], Any],
    get_udi_manager: Callable[[], Any],
):
    """Get all automation periods assigned to a channel including inherited group assignments."""
    try:
        automation_config = get_automation_config_manager()
        udi = get_udi_manager()
        channel = udi.get_channel_by_id(channel_id)
        group_id = channel.get("channel_group_id") if channel else None

        period_assignments = automation_config.get_effective_channel_periods(channel_id, group_id)

        periods = []
        for pid, profile_id in period_assignments.items():
            period = automation_config.get_period(pid)
            if period:
                period_copy = period.copy()
                period_copy["profile_id"] = profile_id
                profile = automation_config.get_profile(profile_id)
                if profile:
                    period_copy["profile_name"] = profile.get("name")
                periods.append(period_copy)

        return jsonify(periods), 200
    except Exception as exc:
        logger.error(f"Error getting automation periods for channel {channel_id}: {exc}")
        return jsonify({"error": "Internal Server Error"}), 500


def batch_assign_periods_to_channels_response(
    *,
    payload: Optional[Dict[str, Any]],
    get_automation_config_manager: Callable[[], Any],
):
    """Batch assign automation periods to multiple channels with profiles."""
    try:
        automation_config = get_automation_config_manager()
        data = BatchPeriodAssignmentsSchema.from_payload(payload or {}, entity_field="channel_ids")

        channel_ids = data.entity_ids
        period_assignments = data.period_assignments
        replace = data.replace

        is_first = True
        for assignment in period_assignments:
            pid = assignment["period_id"]
            profile_id = assignment["profile_id"]
            automation_config.assign_period_to_channels(pid, channel_ids, profile_id, replace and is_first)
            is_first = False

        return (
            jsonify(
                {
                    "message": (
                        f"Assigned {len(period_assignments)} period-profile pairs to {len(channel_ids)} channels"
                    )
                }
            ),
            200,
        )
    except ValidationError as exc:
        return _validation_error(exc)
    except Exception as exc:
        logger.error(f"Error batch assigning periods: {exc}")
        return error_response("Internal Server Error", status_code=500, code="internal_error")


def get_upcoming_automation_events_response(
    *,
    args: Any,
    get_events_scheduler: Callable[[], Any],
    get_automation_config_manager: Callable[[], Any],
):
    """Get upcoming automation events based on configured periods."""
    try:
        events_scheduler = get_events_scheduler()

        hours_ahead = min(int(args.get("hours", 24)), 168)
        max_events = min(int(args.get("max_events", 100)), 500)
        period_id_filter = args.get("period_id")
        force_refresh = args.get("force_refresh", "").lower() == "true"

        result = events_scheduler.get_cached_events(hours_ahead, max_events, force_refresh)

        config_manager = get_automation_config_manager()
        global_settings = config_manager.get_global_settings()
        automation_enabled = global_settings.get("regular_automation_enabled", False)

        result["automation_enabled"] = automation_enabled

        if period_id_filter:
            result["events"] = [e for e in result["events"] if e.get("period_id") == period_id_filter]

        return jsonify(result), 200
    except Exception as exc:
        logger.error(f"Error getting upcoming automation events: {exc}")
        return jsonify({"error": "Internal Server Error"}), 500


def invalidate_automation_events_cache_response(*, get_events_scheduler: Callable[[], Any]):
    """Invalidate the automation events cache."""
    try:
        events_scheduler = get_events_scheduler()
        events_scheduler.invalidate_cache()

        return jsonify({"message": "Cache invalidated successfully"}), 200
    except Exception as exc:
        logger.error(f"Error invalidating cache: {exc}")
        return jsonify({"error": "Internal Server Error"}), 500
