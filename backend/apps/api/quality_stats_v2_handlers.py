"""API handlers for V2 quality statistics."""

from __future__ import annotations

from typing import Any, Callable, List

from flask import jsonify

from apps.core.api_responses import error_response
from apps.core.logging_config import setup_logging
from apps.stream.quality_stats_v2 import (
    build_bulk_quality_stats,
    build_provider_quality_stats,
    build_stream_quality_stats,
)

logger = setup_logging(__name__)


def _coerce_positive_int(value: Any, field_name: str):
    try:
        result = int(value)
    except (TypeError, ValueError):
        return None, error_response(
            f"{field_name} must be a valid integer",
            status_code=400,
            code="invalid_integer",
        )
    if result <= 0:
        return None, error_response(
            f"{field_name} must be greater than zero",
            status_code=400,
            code="invalid_integer",
        )
    return result, None


def _payload_int_list(payload: Any, key: str) -> List[int]:
    values = payload.get(key, []) if isinstance(payload, dict) else []
    if not isinstance(values, list):
        return []
    result = []
    for value in values:
        try:
            result.append(int(value))
        except (TypeError, ValueError):
            continue
    return result


def get_quality_stats_v2_stream_response(
    *,
    stream_id: int,
    get_udi_manager: Callable[[], Any],
):
    try:
        stream_id_int, err = _coerce_positive_int(stream_id, "stream_id")
        if err:
            return err

        udi = get_udi_manager()
        stream = udi.get_stream_by_id(stream_id_int)
        if not stream:
            return error_response("Stream not found", status_code=404, code="stream_not_found")

        return jsonify(build_stream_quality_stats(stream))
    except Exception as exc:
        logger.error("Error building V2 stream quality stats: %s", exc, exc_info=True)
        return error_response("Internal Server Error", status_code=500, code="internal_error")


def get_quality_stats_v2_provider_response(
    *,
    provider_id: int,
    request_args: Any,
    get_udi_manager: Callable[[], Any],
):
    try:
        provider_id_int, err = _coerce_positive_int(provider_id, "provider_id")
        if err:
            return err

        limit = request_args.get("limit", default=100, type=int)
        include_streams = str(request_args.get("include_streams", "true")).lower() != "false"
        udi = get_udi_manager()
        streams = udi.get_streams(log_result=False)
        return jsonify(
            build_provider_quality_stats(
                streams,
                provider_id_int,
                include_streams=include_streams,
                limit=max(0, min(int(limit or 0), 1000)),
            )
        )
    except Exception as exc:
        logger.error("Error building V2 provider quality stats: %s", exc, exc_info=True)
        return error_response("Internal Server Error", status_code=500, code="internal_error")


def post_quality_stats_v2_bulk_response(
    *,
    payload: Any,
    get_udi_manager: Callable[[], Any],
):
    try:
        payload = payload if isinstance(payload, dict) else {}
        stream_ids = _payload_int_list(payload, "stream_ids")[:500]
        provider_ids = _payload_int_list(payload, "provider_ids")[:100]
        provider_limit = payload.get("provider_limit", 100)
        try:
            provider_limit = max(0, min(int(provider_limit), 1000))
        except (TypeError, ValueError):
            provider_limit = 100

        udi = get_udi_manager()
        streams = udi.get_streams(log_result=False)
        return jsonify(
            build_bulk_quality_stats(
                streams,
                stream_ids=stream_ids,
                provider_ids=provider_ids,
                provider_limit=provider_limit,
            )
        )
    except Exception as exc:
        logger.error("Error building V2 bulk quality stats: %s", exc, exc_info=True)
        return error_response("Internal Server Error", status_code=500, code="internal_error")
