"""Scheduling endpoint handlers extracted from web_api."""

import logging
from typing import Any, Callable

from flask import jsonify

from apps.api.schemas import (
    AutoCreateRuleCreateSchema,
    AutoCreateRulesImportSchema,
    AutoCreateRuleTestSchema,
    AutoCreateRuleUpdateSchema,
    ScheduledEventCreateSchema,
    SchedulingConfigUpdateSchema,
)
from apps.core.api_responses import error_response
from apps.core.exceptions import ValidationError


logger = logging.getLogger(__name__)


def get_scheduling_config_response(*, get_scheduling_service: Callable[[], Any]):
    """Handle retrieval of scheduling configuration."""
    try:
        service = get_scheduling_service()
        config = service.get_config()
        return jsonify(config)
    except Exception as exc:
        logger.error(f"Error getting scheduling config: {exc}")
        return jsonify({"error": "Internal Server Error"}), 500


def update_scheduling_config_response(*, payload: Any, get_scheduling_service: Callable[[], Any]):
    """Handle update of scheduling configuration."""
    try:
        schema = SchedulingConfigUpdateSchema.from_payload(payload)

        service = get_scheduling_service()
        success = service.update_config(schema.config)

        if success:
            return jsonify({"message": "Configuration updated", "config": service.get_config()})
        return error_response("Failed to save configuration", status_code=500, code="internal_error")
    except ValidationError as exc:
        return error_response(
            exc.message,
            status_code=exc.status_code,
            code=exc.error_code,
            details=exc.details,
        )
    except Exception as exc:
        logger.error(f"Error updating scheduling config: {exc}")
        return error_response("Internal Server Error", status_code=500, code="internal_error")


def get_epg_grid_response(*, force_refresh: bool, get_scheduling_service: Callable[[], Any]):
    """Handle retrieval of EPG grid data."""
    try:
        service = get_scheduling_service()
        programs = service.fetch_epg_grid(force_refresh=force_refresh)
        return jsonify(programs)
    except Exception as exc:
        logger.error(f"Error fetching EPG grid: {exc}")
        return jsonify({"error": "Internal Server Error"}), 500


def get_channel_programs_response(*, channel_id: int, get_scheduling_service: Callable[[], Any]):
    """Handle retrieval of EPG programs for one channel."""
    from apps.automation.scheduling_service import NoTvgIdError
    try:
        service = get_scheduling_service()
        programs = service.get_programs_by_channel(channel_id)
        return jsonify(programs)
    except NoTvgIdError as exc:
        # SCH-002: surface missing TVG-ID as a structured response
        return jsonify({
            "programs": [],
            "no_tvg_id": True,
            "message": str(exc),
        }), 200
    except Exception as exc:
        logger.error(f"Error fetching programs for channel {channel_id}: {exc}")
        return jsonify({"error": "Internal Server Error"}), 500


def get_scheduled_events_response(*, get_scheduling_service: Callable[[], Any]):
    """Handle retrieval of all scheduled events."""
    try:
        service = get_scheduling_service()
        events = service.get_scheduled_events()
        return jsonify(events)
    except Exception as exc:
        logger.error(f"Error getting scheduled events: {exc}")
        return jsonify({"error": "Internal Server Error"}), 500


def create_scheduled_event_response(
    *,
    payload: Any,
    get_scheduling_service: Callable[[], Any],
    scheduled_event_processor_wake: Any = None,
):
    """Handle creation of a new scheduled event."""
    try:
        schema = ScheduledEventCreateSchema.from_payload(payload)

        service = get_scheduling_service()
        event = service.create_scheduled_event(schema.event_data)

        if scheduled_event_processor_wake is not None and hasattr(scheduled_event_processor_wake, "set"):
            scheduled_event_processor_wake.set()

        return jsonify(event), 201
    except ValidationError as exc:
        return error_response(
            exc.message,
            status_code=exc.status_code,
            code=exc.error_code,
            details=exc.details,
        )
    except ValueError:
        return error_response("Invalid value or request parameters", code="validation_error")
    except Exception as exc:
        logger.error(f"Error creating scheduled event: {exc}")
        return error_response("Internal Server Error", status_code=500, code="internal_error")


def delete_scheduled_event_response(*, event_id: str, get_scheduling_service: Callable[[], Any]):
    """Handle deletion of a scheduled event."""
    try:
        service = get_scheduling_service()
        success = service.delete_scheduled_event(event_id)

        if success:
            return jsonify({"message": "Event deleted"}), 200
        return jsonify({"error": "Event not found"}), 404
    except Exception as exc:
        logger.error(f"Error deleting scheduled event: {exc}")
        return jsonify({"error": "Internal Server Error"}), 500


def get_auto_create_rules_response(*, get_scheduling_service: Callable[[], Any]):
    """Handle retrieval of all auto-create scheduling rules."""
    try:
        service = get_scheduling_service()
        rules = service.get_auto_create_rules()
        return jsonify(rules)
    except Exception as exc:
        logger.error(f"Error getting auto-create rules: {exc}")
        return jsonify({"error": "Internal Server Error"}), 500


def create_auto_create_rule_response(
    *,
    payload: Any,
    get_scheduling_service: Callable[[], Any],
    scheduled_event_processor_wake: Any = None,
):
    """Handle creation of a new auto-create scheduling rule."""
    try:
        schema = AutoCreateRuleCreateSchema.from_payload(payload)

        service = get_scheduling_service()
        rule = service.create_auto_create_rule(schema.rule_data)

        try:
            service.match_programs_to_rules()
            logger.info("Triggered immediate program matching after creating auto-create rule")
        except Exception as exc:
            logger.warning(f"Failed to immediately match programs to new rule: {exc}")

        if scheduled_event_processor_wake is not None and hasattr(scheduled_event_processor_wake, "set"):
            scheduled_event_processor_wake.set()

        return jsonify(rule), 201
    except ValidationError as exc:
        return error_response(
            exc.message,
            status_code=exc.status_code,
            code=exc.error_code,
            details=exc.details,
        )
    except ValueError:
        return error_response("Invalid value or request parameters", code="validation_error")
    except Exception as exc:
        logger.error(f"Error creating auto-create rule: {exc}")
        return error_response("Internal Server Error", status_code=500, code="internal_error")


def delete_auto_create_rule_response(*, rule_id: str, get_scheduling_service: Callable[[], Any]):
    """Handle deletion of one auto-create scheduling rule."""
    try:
        service = get_scheduling_service()
        success = service.delete_auto_create_rule(rule_id)

        if success:
            return jsonify({"message": "Rule deleted"}), 200
        return jsonify({"error": "Rule not found"}), 404
    except Exception as exc:
        logger.error(f"Error deleting auto-create rule: {exc}")
        return jsonify({"error": "Internal Server Error"}), 500


def update_auto_create_rule_response(
    *,
    rule_id: str,
    payload: Any,
    get_scheduling_service: Callable[[], Any],
    scheduled_event_processor_wake: Any = None,
):
    """Handle update of one auto-create scheduling rule."""
    try:
        schema = AutoCreateRuleUpdateSchema.from_payload(payload)

        service = get_scheduling_service()
        updated_rule = service.update_auto_create_rule(rule_id, schema.rule_data)

        if updated_rule:
            try:
                service.match_programs_to_rules()
                logger.info("Triggered immediate program matching after updating auto-create rule")
            except Exception as exc:
                logger.warning(f"Failed to immediately match programs to updated rule: {exc}")

            if scheduled_event_processor_wake is not None and hasattr(scheduled_event_processor_wake, "set"):
                scheduled_event_processor_wake.set()

            return jsonify(updated_rule), 200

        return error_response("Rule not found", status_code=404, code="not_found")
    except ValidationError as exc:
        return error_response(
            exc.message,
            status_code=exc.status_code,
            code=exc.error_code,
            details=exc.details,
        )
    except ValueError:
        return error_response("Invalid value or request parameters", code="validation_error")
    except Exception as exc:
        logger.error(f"Error updating auto-create rule: {exc}")
        return error_response("Internal Server Error", status_code=500, code="internal_error")


def test_auto_create_rule_response(*, payload: Any, get_scheduling_service: Callable[[], Any]):
    """Handle regex testing against EPG data for a channel."""
    from apps.automation.scheduling_service import NoTvgIdError
    try:
        schema = AutoCreateRuleTestSchema.from_payload(payload)

        service = get_scheduling_service()
        matching_programs = service.test_regex_against_epg(schema.channel_id, schema.regex_pattern)

        return jsonify({"matches": len(matching_programs), "programs": matching_programs})

    except NoTvgIdError as exc:
        # SCH-002: channel has no TVG-ID — return structured 200 so the frontend
        # shows a specific actionable message instead of generic "No Matches".
        return jsonify({
            "matches": 0,
            "programs": [],
            "no_tvg_id": True,
            "message": str(exc),
        }), 200

    except ValidationError as exc:
        return error_response(
            exc.message,
            status_code=exc.status_code,
            code=exc.error_code,
            details=exc.details,
        )
    except ValueError:
        return error_response("Invalid value or request parameters", code="validation_error")
    except Exception as exc:
        logger.error(f"Error testing auto-create rule: {exc}")
        return error_response("Internal Server Error", status_code=500, code="internal_error")


def export_auto_create_rules_response(*, get_scheduling_service: Callable[[], Any]):
    """Handle export of all auto-create rules."""
    try:
        service = get_scheduling_service()
        rules = service.export_auto_create_rules()
        return jsonify(rules), 200
    except Exception as exc:
        logger.error(f"Error exporting auto-create rules: {exc}")
        return jsonify({"error": "Internal Server Error"}), 500


def import_auto_create_rules_response(
    *,
    payload: Any,
    get_scheduling_service: Callable[[], Any],
    scheduled_event_processor_wake: Any = None,
):
    """Handle import of auto-create rules from JSON array payload."""
    try:
        schema = AutoCreateRulesImportSchema.from_payload(payload)

        service = get_scheduling_service()
        result = service.import_auto_create_rules(schema.rules_data)

        if scheduled_event_processor_wake is not None and hasattr(scheduled_event_processor_wake, "set"):
            scheduled_event_processor_wake.set()

        return jsonify(result), 200
    except ValidationError as exc:
        return error_response(
            exc.message,
            status_code=exc.status_code,
            code=exc.error_code,
            details=exc.details,
        )
    except ValueError:
        return error_response("Invalid value or request parameters", code="validation_error")
    except Exception as exc:
        logger.error(f"Error importing auto-create rules: {exc}")
        return error_response("Internal Server Error", status_code=500, code="internal_error")


def process_due_scheduled_events_response(
    *,
    get_scheduling_service: Callable[[], Any],
    get_stream_checker_service: Callable[[], Any],
):
    """Handle processing and execution of due scheduled events."""
    try:
        service = get_scheduling_service()
        stream_checker = get_stream_checker_service()

        due_events = service.get_due_events()
        if not due_events:
            return jsonify({"message": "No events due for execution", "processed": 0}), 200

        results = []
        for event in due_events:
            event_id = event.get("id")
            channel_name = event.get("channel_name", "Unknown")
            program_title = event.get("program_title", "Unknown")

            logger.info(f"Processing due event {event_id} for {channel_name} (program: {program_title})")

            success = service.execute_scheduled_check(event_id, stream_checker)
            results.append(
                {
                    "event_id": event_id,
                    "channel_name": channel_name,
                    "program_title": program_title,
                    "success": success,
                }
            )

        successful = sum(1 for result in results if result["success"])

        return (
            jsonify(
                {
                    "message": f"Processed {len(results)} event(s), {successful} successful",
                    "processed": len(results),
                    "successful": successful,
                    "results": results,
                }
            ),
            200,
        )
    except Exception as exc:
        logger.error(f"Error processing due scheduled events: {exc}", exc_info=True)
        return jsonify({"error": "Internal Server Error"}), 500


def get_scheduled_event_processor_status_response(
    *,
    scheduled_event_processor_thread: Any,
    scheduled_event_processor_running: bool,
):
    """Handle retrieval of scheduled-event processor status."""
    try:
        thread_alive = (
            scheduled_event_processor_thread is not None
            and hasattr(scheduled_event_processor_thread, "is_alive")
            and scheduled_event_processor_thread.is_alive()
        )
        is_running = thread_alive and scheduled_event_processor_running
        return jsonify({"running": is_running, "thread_alive": thread_alive}), 200
    except Exception as exc:
        logger.error(f"Error getting scheduled event processor status: {exc}")
        return jsonify({"error": "Internal Server Error"}), 500


def start_scheduled_event_processor_api_response(*, start_scheduled_event_processor: Callable[[], Any]):
    """Handle start request for scheduled-event processor."""
    try:
        success = start_scheduled_event_processor()
        if success:
            return jsonify({"message": "Scheduled event processor started"}), 200
        return jsonify({"message": "Scheduled event processor is already running"}), 200
    except Exception as exc:
        logger.error(f"Error starting scheduled event processor: {exc}")
        return jsonify({"error": "Internal Server Error"}), 500


def stop_scheduled_event_processor_api_response(*, stop_scheduled_event_processor: Callable[[], Any]):
    """Handle stop request for scheduled-event processor."""
    try:
        success = stop_scheduled_event_processor()
        if success:
            return jsonify({"message": "Scheduled event processor stopped"}), 200
        return jsonify({"message": "Scheduled event processor is not running"}), 200
    except Exception as exc:
        logger.error(f"Error stopping scheduled event processor: {exc}")
        return jsonify({"error": "Internal Server Error"}), 500


def get_epg_refresh_processor_status_response(*, epg_refresh_thread: Any, epg_refresh_running: bool):
    """Handle retrieval of EPG refresh processor status."""
    try:
        thread_alive = (
            epg_refresh_thread is not None
            and hasattr(epg_refresh_thread, "is_alive")
            and epg_refresh_thread.is_alive()
        )
        is_running = thread_alive and epg_refresh_running
        return jsonify({"running": is_running, "thread_alive": thread_alive}), 200
    except Exception as exc:
        logger.error(f"Error getting EPG refresh processor status: {exc}")
        return jsonify({"error": "Internal Server Error"}), 500


def start_epg_refresh_processor_api_response(*, start_epg_refresh_processor: Callable[[], Any]):
    """Handle start request for EPG refresh processor."""
    try:
        success = start_epg_refresh_processor()
        if success:
            return jsonify({"message": "EPG refresh processor started"}), 200
        return jsonify({"message": "EPG refresh processor is already running"}), 200
    except Exception as exc:
        logger.error(f"Error starting EPG refresh processor: {exc}")
        return jsonify({"error": "Internal Server Error"}), 500


def stop_epg_refresh_processor_api_response(*, stop_epg_refresh_processor: Callable[[], Any]):
    """Handle stop request for EPG refresh processor."""
    try:
        success = stop_epg_refresh_processor()
        if success:
            return jsonify({"message": "EPG refresh processor stopped"}), 200
        return jsonify({"message": "EPG refresh processor is not running"}), 200
    except Exception as exc:
        logger.error(f"Error stopping EPG refresh processor: {exc}")
        return jsonify({"error": "Internal Server Error"}), 500


def trigger_epg_refresh_response(*, epg_refresh_wake: Any, epg_refresh_running: bool, epg_refresh_thread: Any):
    """Handle manual EPG refresh trigger request."""
    try:
        if (
            epg_refresh_wake is not None
            and hasattr(epg_refresh_wake, "set")
            and epg_refresh_running
            and epg_refresh_thread is not None
            and hasattr(epg_refresh_thread, "is_alive")
            and epg_refresh_thread.is_alive()
        ):
            epg_refresh_wake.set()
            return jsonify({"message": "EPG refresh triggered"}), 200

        return jsonify({"error": "EPG refresh processor is not running"}), 400
    except Exception as exc:
        logger.error(f"Error triggering EPG refresh: {exc}")
        return jsonify({"error": "Internal Server Error"}), 500
