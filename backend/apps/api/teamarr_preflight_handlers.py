"""API handlers for Teamarr managed-event preflight checks."""

from typing import Any, Callable, Dict, Optional

from flask import jsonify

from apps.core.api_responses import error_response
from apps.core.logging_config import setup_logging

logger = setup_logging(__name__)


def get_teamarr_preflight_config_response(*, get_service: Callable[[], Any]):
    try:
        return jsonify(get_service().get_config()), 200
    except Exception as exc:
        logger.error(f"Error loading Teamarr preflight config: {exc}", exc_info=True)
        return error_response("Internal Server Error", status_code=500, code="internal_error")


def update_teamarr_preflight_config_response(
    *,
    payload: Optional[Dict[str, Any]],
    get_service: Callable[[], Any],
):
    try:
        return jsonify(get_service().update_config(payload or {})), 200
    except Exception as exc:
        logger.error(f"Error updating Teamarr preflight config: {exc}", exc_info=True)
        return error_response("Internal Server Error", status_code=500, code="internal_error")


def get_teamarr_preflight_status_response(*, get_service: Callable[[], Any]):
    try:
        return jsonify(get_service().get_status()), 200
    except Exception as exc:
        logger.error(f"Error loading Teamarr preflight status: {exc}", exc_info=True)
        return error_response("Internal Server Error", status_code=500, code="internal_error")


def start_teamarr_preflight_response(*, get_service: Callable[[], Any]):
    try:
        get_service().start()
        return jsonify({"success": True, "status": get_service().get_status()}), 200
    except Exception as exc:
        logger.error(f"Error starting Teamarr preflight: {exc}", exc_info=True)
        return error_response("Internal Server Error", status_code=500, code="internal_error")


def stop_teamarr_preflight_response(*, get_service: Callable[[], Any]):
    try:
        get_service().stop()
        return jsonify({"success": True, "status": get_service().get_status()}), 200
    except Exception as exc:
        logger.error(f"Error stopping Teamarr preflight: {exc}", exc_info=True)
        return error_response("Internal Server Error", status_code=500, code="internal_error")


def run_teamarr_preflight_once_response(*, get_service: Callable[[], Any]):
    try:
        return jsonify(get_service().run_once(force=True)), 200
    except Exception as exc:
        logger.error(f"Error running Teamarr preflight scan: {exc}", exc_info=True)
        return error_response("Internal Server Error", status_code=500, code="internal_error")
