"""API handlers for the active-viewer shadow blank monitor."""

from typing import Any, Callable, Dict, Optional

from flask import jsonify

from apps.core.api_responses import error_response
from apps.core.logging_config import setup_logging

logger = setup_logging(__name__)


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
        get_service().start()
        return jsonify({"success": True, "status": get_service().get_status()}), 200
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
        return jsonify(get_service().run_once(force=True)), 200
    except Exception as exc:
        logger.error(f"Error running shadow blank monitor scan: {exc}", exc_info=True)
        return error_response("Internal Server Error", status_code=500, code="internal_error")
