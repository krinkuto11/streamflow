"""Visibility and operator-confirmed resolution of ambiguous refresh starts."""

from apps.api.schemas import M3URefreshResolutionSchema
from apps.core.api_responses import error_response, success_response
from apps.core.api_utils import get_m3u_refresh_guard
from apps.core.exceptions import ValidationError
from apps.core.logging_config import setup_logging
from apps.core.m3u_refresh_guard import RefreshStartBlocked

logger = setup_logging(__name__)


def m3u_refresh_attempts_response(*, method, payload=None, get_guard=None):
    try:
        if method == 'POST':
            resolution = M3URefreshResolutionSchema.from_payload(payload)
            guard = (get_guard or get_m3u_refresh_guard)()
            guard.resolve(resolution.account_id, resolution.attempt_id, resolution.confirmed_finished)
            return success_response(message='Protection resolved. Future refresh starts are allowed; no refresh was started by this action.')
        guard = (get_guard or get_m3u_refresh_guard)()
        return success_response(data={'attempts': guard.pending()})
    except ValidationError as exc:
        return error_response(str(exc), status_code=400, code='validation_error')
    except RefreshStartBlocked as exc:
        return error_response(str(exc), status_code=409, code='refresh_start_unresolved')
    except Exception:
        logger.exception('Could not access playlist refresh protection')
        return error_response('Could not access playlist refresh protection', status_code=500, code='internal_error')
