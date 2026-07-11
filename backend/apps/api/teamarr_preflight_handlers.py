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
        service = get_service()
        started = bool(service.start())
        status = service.get_status()
        if not started:
            return jsonify({
                "success": False,
                "error": "Teamarr preflight is still stopping",
                "code": "preflight_stopping",
                "status": status,
            }), 409
        return jsonify({"success": True, "status": status}), 200
    except Exception as exc:
        logger.error(f"Error starting Teamarr preflight: {exc}", exc_info=True)
        return error_response("Internal Server Error", status_code=500, code="internal_error")


def stop_teamarr_preflight_response(*, get_service: Callable[[], Any]):
    try:
        service = get_service()
        stopped = bool(service.stop())
        status = service.get_status()
        if not stopped:
            return jsonify({
                "success": False,
                "stopping": True,
                "message": "Waiting for active Teamarr requests to finish",
                "status": status,
            }), 202
        return jsonify({"success": True, "stopping": False, "status": status}), 200
    except Exception as exc:
        logger.error(f"Error stopping Teamarr preflight: {exc}", exc_info=True)
        return error_response("Internal Server Error", status_code=500, code="internal_error")


def run_teamarr_preflight_once_response(*, get_service: Callable[[], Any]):
    try:
        result = get_service().run_once(force=True)
        status_code = 409 if result.get("code") == "scan_in_progress" else 200
        return jsonify(result), status_code
    except Exception as exc:
        logger.error(f"Error running Teamarr preflight scan: {exc}", exc_info=True)
        return error_response("Internal Server Error", status_code=500, code="internal_error")


def force_teamarr_preflight_event_response(
    *,
    payload: Optional[Dict[str, Any]],
    get_service: Callable[[], Any],
):
    try:
        identity = (payload or {}).get("identity")
        result = get_service().force_check_event(identity)
        if result.get("success"):
            return jsonify(result), 200

        code = str(result.get("code") or "manual_preflight_failed")
        status_code = 404 if code == "event_not_found" else 400
        return error_response(
            str(result.get("error") or "Manual preflight failed"),
            status_code=status_code,
            code=code,
            details={
                key: value
                for key, value in result.items()
                if key not in {"success", "error", "code"}
            },
        )
    except Exception as exc:
        logger.error(f"Error forcing Teamarr preflight event: {exc}", exc_info=True)
        return error_response("Internal Server Error", status_code=500, code="internal_error")
