"""Read-only access to the last reusable stream quality measurement."""

from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta
from typing import Any, Callable, Dict, Iterable, Optional, Tuple

from apps.core.stream_stats_utils import (
    extract_stream_stats,
    normalize_resolution,
    parse_bitrate_value,
    parse_fps_value,
)
from apps.database import connection
from apps.database.models import Run, Stream, StreamTelemetry


UNREUSABLE_STATUSES = {
    "dead",
    "blank",
    "freeze",
    "frozen",
    "probe_failed",
    "failed",
    "error",
    "offline",
    "no_decodable_frames",
}

EMPTY_VALUES = {"", "n/a", "none", "null", "unknown"}


def _clean_string(value: Any) -> Optional[str]:
    if value is None:
        return None
    cleaned = str(value).strip()
    if cleaned.lower() in EMPTY_VALUES:
        return None
    return cleaned


def _coerce_int(value: Any) -> Optional[int]:
    try:
        if value is None or value == "":
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _load_json_blob(blob: Any) -> Any:
    if not blob:
        return None
    if isinstance(blob, (dict, list)):
        return blob
    if isinstance(blob, str):
        try:
            return json.loads(blob)
        except json.JSONDecodeError:
            return None
    return None


def _iter_dicts(value: Any) -> Iterable[Dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _iter_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from _iter_dicts(child)


def _stream_payload_matches(payload: Dict[str, Any], stream_id: int) -> bool:
    for key in ("stream_id", "id"):
        if _coerce_int(payload.get(key)) == stream_id:
            return True
    return False


def _find_stream_payload(run: Run, stream_id: int) -> Dict[str, Any]:
    best_match: Dict[str, Any] = {}
    for root in (_load_json_blob(run.raw_details), _load_json_blob(run.raw_subentries)):
        for payload in _iter_dicts(root):
            if not _stream_payload_matches(payload, stream_id):
                continue
            if not best_match:
                best_match = payload
            if any(
                key in payload
                for key in (
                    "status",
                    "dead_reason",
                    "quality_reason",
                    "quality_reason_detail",
                    "resolution",
                    "stream_stats",
                )
            ):
                return payload
    return best_match


def _extract_resolution(payload: Dict[str, Any]) -> Tuple[Optional[int], Optional[int]]:
    resolution = normalize_resolution(payload.get("resolution"))
    if resolution == "N/A":
        stats = extract_stream_stats(payload)
        resolution = normalize_resolution(stats.get("resolution"))
    if resolution == "N/A" or "x" not in resolution:
        return None, None
    try:
        width_raw, height_raw = resolution.lower().split("x", 1)
        return int(width_raw), int(height_raw)
    except (TypeError, ValueError):
        return None, None


def _row_or_payload_stats(row: StreamTelemetry, payload: Dict[str, Any]) -> Dict[str, Any]:
    extracted = extract_stream_stats(payload) if payload else {}
    stream_stats = payload.get("stream_stats") if isinstance(payload.get("stream_stats"), dict) else {}
    width = row.resolution_width
    height = row.resolution_height
    if width is None or height is None:
        width, height = _extract_resolution(payload)

    bitrate = row.bitrate_kbps
    if bitrate is None:
        bitrate = parse_bitrate_value(
            payload.get("bitrate")
            or payload.get("ffmpeg_output_bitrate")
            or extracted.get("bitrate_kbps")
        )
    bitrate_int = int(bitrate) if bitrate is not None else None

    fps = row.fps if row.fps is not None else parse_fps_value(payload.get("fps") or extracted.get("fps"))
    codec = (
        _clean_string(row.codec)
        or _clean_string(payload.get("video_codec"))
        or _clean_string(extracted.get("video_codec"))
    )
    audio_codec = (
        _clean_string(row.audio_codec)
        or _clean_string(payload.get("audio_codec"))
        or _clean_string(extracted.get("audio_codec"))
    )

    return {
        "width": width,
        "height": height,
        "fps": fps,
        "codec": codec.lower() if codec else None,
        "audio_codec": audio_codec.lower() if audio_codec else None,
        "bitrate_kbps": bitrate_int,
        "hdr": bool(row.is_hdr or payload.get("is_hdr") or payload.get("hdr_format")),
        "measurement_incomplete": bool(
            payload.get("measurement_incomplete")
            or stream_stats.get("measurement_incomplete")
            or payload.get("bitrate_recheck_required")
            or stream_stats.get("bitrate_recheck_required")
        ),
        "measurement_incomplete_reason": (
            _clean_string(payload.get("measurement_incomplete_reason"))
            or _clean_string(stream_stats.get("measurement_incomplete_reason"))
        ),
        "bitrate_recheck_required": bool(
            payload.get("bitrate_recheck_required")
            or stream_stats.get("bitrate_recheck_required")
        ),
    }


def _quality_status(row: StreamTelemetry, payload: Dict[str, Any]) -> Tuple[str, str]:
    status = _clean_string(payload.get("status"))
    reason = (
        _clean_string(payload.get("quality_reason_detail"))
        or _clean_string(payload.get("quality_reason"))
        or _clean_string(payload.get("dead_reason"))
    )

    if payload.get("blank_detected") is True:
        status = "blank"
        reason = reason or "blank_detected"
    elif payload.get("freeze_detected") is True:
        status = "freeze"
        reason = reason or "freeze_detected"
    elif payload.get("no_decodable_frames") is True:
        status = "no_decodable_frames"
        reason = reason or "no_decodable_frames"

    if row.is_dead and (not status or status == "completed"):
        status = "dead"
        reason = reason or "dead"

    return (status or "completed").lower(), (reason or "none").lower()


def _format_timestamp(value: Optional[datetime]) -> Optional[str]:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat().replace("+00:00", "Z")


def _is_stale(run: Run, stream: Optional[Stream], stale_after_hours: Optional[float]) -> bool:
    if stream is not None and bool(stream.is_stale):
        return True
    if stale_after_hours is None or stale_after_hours <= 0:
        return False
    measured_at = run.timestamp
    if measured_at is None:
        return False
    if measured_at.tzinfo is None:
        measured_at = measured_at.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - measured_at > timedelta(hours=stale_after_hours)


def _not_reusable(
    *,
    stream_id: int,
    reason: str,
    status: str,
    quality_reason: str,
    row: StreamTelemetry,
    run: Run,
) -> Dict[str, Any]:
    return {
        "stream_id": stream_id,
        "measured": False,
        "recheck_required": True,
        "reason": reason,
        "last_run_id": run.id,
        "run_type": run.run_type,
        "channel_id": row.channel_id,
        "provider_id": row.provider_id,
        "last_status": status,
        "quality_reason": quality_reason,
        "measured_at": _format_timestamp(run.timestamp),
        "source": "stream_telemetry",
    }


def get_last_quality_stats(
    stream_id: int,
    *,
    session_factory: Optional[Callable[[], Any]] = None,
    stale_after_hours: Optional[float] = None,
) -> Dict[str, Any]:
    """Return the latest stored quality measurement for one stream.

    This is intentionally read-only. It only queries persisted run telemetry and
    never touches StreamChecker, queues, Dispatcharr, ffmpeg, or any probe path.
    """
    session_factory = session_factory or connection.get_session
    session = session_factory()
    try:
        result = (
            session.query(StreamTelemetry, Run, Stream)
            .join(Run, StreamTelemetry.run_id == Run.id)
            .outerjoin(Stream, StreamTelemetry.stream_id == Stream.id)
            .filter(StreamTelemetry.stream_id == stream_id)
            .order_by(Run.timestamp.desc(), StreamTelemetry.id.desc())
            .first()
        )
        if result is None:
            return {
                "stream_id": stream_id,
                "measured": False,
                "recheck_required": True,
                "reason": "never_measured",
            }

        row, run, stream = result
        raw_payload = _find_stream_payload(run, stream_id)
        stats = _row_or_payload_stats(row, raw_payload)
        status, quality_reason = _quality_status(row, raw_payload)

        if status in UNREUSABLE_STATUSES or row.is_dead:
            return _not_reusable(
                stream_id=stream_id,
                reason="last_result_not_reusable",
                status=status,
                quality_reason=quality_reason,
                row=row,
                run=run,
            )

        width = _coerce_int(stats["width"])
        height = _coerce_int(stats["height"])
        if not width or not height or width <= 0 or height <= 0:
            return _not_reusable(
                stream_id=stream_id,
                reason="last_result_not_reusable",
                status="0x0" if width == 0 or height == 0 else "incomplete_stats",
                quality_reason=quality_reason,
                row=row,
                run=run,
            )

        fps = parse_fps_value(stats["fps"])
        codec = _clean_string(stats["codec"])
        if fps is None or codec is None:
            return _not_reusable(
                stream_id=stream_id,
                reason="last_result_not_reusable",
                status="incomplete_stats",
                quality_reason=quality_reason,
                row=row,
                run=run,
            )

        if stats["bitrate_kbps"] is None:
            return _not_reusable(
                stream_id=stream_id,
                reason=stats["measurement_incomplete_reason"] or "missing_bitrate",
                status="incomplete_bitrate",
                quality_reason=quality_reason,
                row=row,
                run=run,
            )

        return {
            "stream_id": stream_id,
            "measured": True,
            "recheck_required": False,
            "last_run_id": run.id,
            "run_type": run.run_type,
            "channel_id": row.channel_id,
            "provider_id": row.provider_id,
            "resolution": f"{width}x{height}",
            "width": width,
            "height": height,
            "fps": fps,
            "codec": codec.lower(),
            "audio_codec": stats["audio_codec"],
            "bitrate_kbps": stats["bitrate_kbps"],
            "hdr": bool(stats["hdr"]),
            "status": status,
            "quality_reason": quality_reason,
            "measured_at": _format_timestamp(run.timestamp),
            "source": "stream_telemetry",
            "stale": _is_stale(run, stream, stale_after_hours),
        }
    finally:
        session.close()
