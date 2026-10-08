"""Playback Stability settings and sanitized passive history."""

import logging
from flask import jsonify
from apps.core.api_responses import error_response

logger = logging.getLogger(__name__)


def playback_stability_config_response(*, method, payload=None, get_service):
    try:
        service = get_service()
        return jsonify(service.get_config() if method == 'GET' else service.update_config(payload)), 200
    except ValueError as exc:
        return error_response(str(exc), status_code=400, code='invalid_playback_stability_config')
    except Exception:
        logger.exception('Playback Stability settings unavailable')
        return error_response('Playback Stability settings unavailable', status_code=500, code='internal_error')


def playback_stability_status_response(*, get_service):
    try:
        return jsonify(get_service().get_status()), 200
    except Exception:
        logger.exception('Playback Stability history unavailable')
        return error_response('Playback Stability history unavailable', status_code=500, code='internal_error')
