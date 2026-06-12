"""API handlers for the V6 job arbiter snapshot."""

from typing import Any, Callable, Dict

from flask import jsonify

from apps.core.api_responses import error_response
from apps.core.logging_config import setup_logging
from apps.orchestration.job_arbiter import build_job_arbiter_snapshot

logger = setup_logging(__name__)


def _safe_status(name: str, getter: Callable[[], Any]) -> Dict[str, Any]:
    try:
        service = getter()
        if service is None:
            return {}
        if hasattr(service, "get_status"):
            status = service.get_status()
            return status if isinstance(status, dict) else {}
    except Exception as exc:
        logger.debug("Could not collect %s status for job arbiter: %s", name, exc)
    return {}


def get_job_arbiter_status_response(
    *,
    get_automation_manager: Callable[[], Any],
    get_stream_checker_service: Callable[[], Any],
    get_shadow_monitor_service: Callable[[], Any],
    get_teamarr_preflight_service: Callable[[], Any],
):
    try:
        snapshot = build_job_arbiter_snapshot(
            automation_status=_safe_status("automation", get_automation_manager),
            stream_checker_status=_safe_status("stream_checker", get_stream_checker_service),
            shadow_status=_safe_status("shadow_monitor", get_shadow_monitor_service),
            teamarr_status=_safe_status("teamarr_preflight", get_teamarr_preflight_service),
        )
        return jsonify(snapshot), 200
    except Exception as exc:
        logger.error("Error building job arbiter status: %s", exc, exc_info=True)
        return error_response("Internal Server Error", status_code=500, code="internal_error")
