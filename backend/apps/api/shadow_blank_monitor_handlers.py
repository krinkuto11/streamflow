"""API handlers for the active-viewer shadow blank monitor."""

from typing import Any, Callable, Dict, Optional

from flask import jsonify

from apps.core.api_responses import error_response
from apps.core.logging_config import setup_logging

logger = setup_logging(__name__)


def _configuration_status_details(status: Dict[str, Any]) -> Dict[str, Any]:
    """Return only safe configuration fields for client-facing error details."""
    allowed_keys = {
        "enabled",
        "running",
        "configuration_required",
        "configuration_issue",
        "configuration_message",
        "watched_count",
    }
    return {key: status.get(key) for key in allowed_keys if key in status}


def get_shadow_blank_monitor_config_response(*, get_service: Callable[[], Any]):
    try:
        return jsonify(get_service().get_config()), 200
    except Exception as exc:
        logger.error(f"Error loading shadow blank monitor config: {exc}", exc_info=True)
        return error_response("Internal Server Error", status_code=500, code="internal_error")


def update_shadow_blank_monitor_config_response(
    *,
    payload: Optional[Dict[str, Any]],
    get_service: Callable[[], Any],
):
    try:
        return jsonify(get_service().update_config(payload or {})), 200
    except Exception as exc:
        logger.error(f"Error updating shadow blank monitor config: {exc}", exc_info=True)
        return error_response("Internal Server Error", status_code=500, code="internal_error")


def get_shadow_blank_monitor_status_response(*, get_service: Callable[[], Any]):
    try:
        return jsonify(get_service().get_status()), 200
    except Exception as exc:
        logger.error(f"Error loading shadow blank monitor status: {exc}", exc_info=True)
        return error_response("Internal Server Error", status_code=500, code="internal_error")


def start_shadow_blank_monitor_response(*, get_service: Callable[[], Any]):
    try:
        service = get_service()
        started = service.start()
        status = service.get_status()
        if not started:
            return error_response(
                status.get("configuration_message") or "Shadow monitor could not start",
                status_code=400,
                code=status.get("configuration_issue") or "shadow_monitor_not_started",
                details={"status": _configuration_status_details(status)},
            )
        return jsonify({"success": True, "status": status}), 200
    except Exception as exc:
        logger.error(f"Error starting shadow blank monitor: {exc}", exc_info=True)
        return error_response("Internal Server Error", status_code=500, code="internal_error")


def stop_shadow_blank_monitor_response(*, get_service: Callable[[], Any]):
    try:
        get_service().stop()
        return jsonify({"success": True, "status": get_service().get_status()}), 200
    except Exception as exc:
        logger.error(f"Error stopping shadow blank monitor: {exc}", exc_info=True)
        return error_response("Internal Server Error", status_code=500, code="internal_error")


def run_shadow_blank_monitor_once_response(*, get_service: Callable[[], Any]):
    try:
        status = get_service().run_once(force=True)
        if status.get("configuration_required"):
            return error_response(
                status.get("configuration_message") or "Shadow monitor scan could not start",
                status_code=400,
                code=status.get("configuration_issue") or "shadow_monitor_scan_not_started",
                details={"status": _configuration_status_details(status)},
            )
        return jsonify(status), 200
    except Exception as exc:
        logger.error(f"Error running shadow blank monitor scan: {exc}", exc_info=True)
        return error_response("Internal Server Error", status_code=500, code="internal_error")


def learn_shadow_offline_image_response(
    *,
    payload: Optional[Dict[str, Any]],
    get_service: Callable[[], Any],
):
    try:
        payload = payload or {}
        result = get_service().learn_offline_image_from_current_frame(
            channel_ref=payload.get("channel_ref"),
            enable_detection=bool(payload.get("enable_detection")),
        )
        if not result.get("success"):
            return error_response(
                result.get("message") or "Could not learn offline image from current frame",
                status_code=400,
                code=result.get("reason") or "offline_image_learn_failed",
                details={"result": result},
            )
        return jsonify(result), 200
    except Exception as exc:
        logger.error(f"Error learning shadow offline image: {exc}", exc_info=True)
        return error_response("Internal Server Error", status_code=500, code="internal_error")
