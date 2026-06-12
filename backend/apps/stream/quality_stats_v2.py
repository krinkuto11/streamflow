"""V2 quality statistics helpers for streams and providers."""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Optional, Sequence

from apps.core.stream_stats_utils import extract_stream_stats


UHD_NAME_HINT_RE = re.compile(r"\b(4k|uhd|2160p|3840p|3840x2160)\b", re.IGNORECASE)


def _coerce_int(value: Any) -> Optional[int]:
    try:
        if value is None or isinstance(value, bool):
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _resolution_dimensions(resolution: Any) -> Dict[str, Optional[int]]:
    text = str(resolution or "").strip().lower()
    if "x" not in text:
        return {"width": None, "height": None}

    left, right = text.split("x", 1)
    width = _coerce_int(left)
    height = _coerce_int(right)
    return {"width": width, "height": height}


def _quality_bucket(width: Optional[int], height: Optional[int]) -> str:
    if width == 0 or height == 0:
        return "offline"
    if width is None or height is None:
        return "unknown"
    if width >= 3840 or height >= 2160:
        return "uhd"
    if width >= 1920 or height >= 1080:
        return "fhd"
    if width >= 1280 or height >= 720:
        return "hd"
    return "sd"


def build_quality_markers(stream: Dict[str, Any]) -> Dict[str, Any]:
    stats = extract_stream_stats(stream)
    dimensions = _resolution_dimensions(stats.get("resolution"))
    width = dimensions["width"]
    height = dimensions["height"]
    measured_uhd = bool(width is not None and height is not None and (width >= 3840 or height >= 2160))
    name_uhd_hint = bool(UHD_NAME_HINT_RE.search(str(stream.get("name") or "")))
    hdr_format = stats.get("hdr_format")

    return {
        "quality_bucket": _quality_bucket(width, height),
        "resolution_width": width,
        "resolution_height": height,
        "measured_uhd": measured_uhd,
        "name_uhd_hint": name_uhd_hint,
        "uhd_confidence": "measured" if measured_uhd else "name_hint" if name_uhd_hint else "none",
        "hdr": bool(hdr_format),
        "hdr_format": hdr_format,
        "zero_resolution": bool(width == 0 or height == 0),
    }


def _group_id(stream: Dict[str, Any]) -> Optional[int]:
    for key in ("group_id", "channel_group_id", "m3u_group_id"):
        value = _coerce_int(stream.get(key))
        if value is not None:
            return value
    return None


def build_uhd_identity_selectors(stream: Dict[str, Any], markers: Dict[str, Any]) -> List[Dict[str, Any]]:
    stream_id = _coerce_int(stream.get("id"))
    provider_id = _provider_id(stream)
    group_id = _group_id(stream)
    raw_stats = stream.get("stream_stats")
    resolution = stream.get("resolution")
    if not resolution and isinstance(raw_stats, dict):
        resolution = raw_stats.get("resolution")
    measured_uhd = bool(markers.get("measured_uhd"))

    if not measured_uhd:
        if markers.get("name_uhd_hint"):
            return [
                {
                    "scope": "name_hint",
                    "selector": f"name_hint:stream:{stream_id}",
                    "safe": False,
                    "reason": "name_hint_without_measured_uhd",
                }
            ]
        return []

    selectors = []
    if stream_id is not None:
        selectors.append({
            "scope": "stream",
            "selector": f"stream:{stream_id}:measured_uhd",
            "safe": True,
        })
    if provider_id is not None:
        selectors.append({
            "scope": "provider",
            "selector": f"provider:{provider_id}:measured_uhd",
            "safe": True,
        })
    if group_id is not None:
        selectors.append({
            "scope": "group",
            "selector": f"group:{group_id}:measured_uhd",
            "safe": True,
        })
    selectors.append({
        "scope": "quality",
        "selector": f"quality:measured_uhd:{resolution or 'unknown'}",
        "safe": True,
    })
    return selectors


def build_stream_quality_stats(stream: Dict[str, Any]) -> Dict[str, Any]:
    stats = extract_stream_stats(stream)
    markers = build_quality_markers(stream)
    return {
        "version": 2,
        "stream_id": stream.get("id"),
        "stream_name": stream.get("name"),
        "provider_id": stream.get("m3u_account") or stream.get("m3u_account_id"),
        "stats": {
            "resolution": stats.get("resolution"),
            "fps": stats.get("fps"),
            "bitrate_kbps": stats.get("bitrate_kbps"),
            "video_codec": stats.get("video_codec"),
            "audio_codec": stats.get("audio_codec"),
            "pixel_format": stats.get("pixel_format"),
            "audio_sample_rate": stats.get("audio_sample_rate"),
            "audio_channels": stats.get("audio_channels"),
            "audio_bitrate": stats.get("audio_bitrate"),
        },
        "markers": markers,
        "uhd_identity_selectors": build_uhd_identity_selectors(stream, markers),
    }


def _provider_id(stream: Dict[str, Any]) -> Optional[int]:
    return _coerce_int(stream.get("m3u_account") or stream.get("m3u_account_id"))


def _summarize_stream_stats(items: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    summary = {
        "total_streams": len(items),
        "uhd_streams": 0,
        "hdr_streams": 0,
        "offline_streams": 0,
        "unknown_streams": 0,
        "buckets": {},
    }
    for item in items:
        markers = item.get("markers") or {}
        bucket = markers.get("quality_bucket") or "unknown"
        summary["buckets"][bucket] = summary["buckets"].get(bucket, 0) + 1
        if markers.get("measured_uhd"):
            summary["uhd_streams"] += 1
        if markers.get("hdr"):
            summary["hdr_streams"] += 1
        if bucket == "offline":
            summary["offline_streams"] += 1
        if bucket == "unknown":
            summary["unknown_streams"] += 1
    return summary


def build_provider_quality_stats(
    streams: Iterable[Dict[str, Any]],
    provider_id: int,
    *,
    include_streams: bool = True,
    limit: int = 100,
) -> Dict[str, Any]:
    provider_streams = [
        build_stream_quality_stats(stream)
        for stream in streams
        if isinstance(stream, dict) and _provider_id(stream) == provider_id
    ]
    provider_streams.sort(key=lambda item: str(item.get("stream_name") or ""))
    summary = _summarize_stream_stats(provider_streams)

    return {
        "version": 2,
        "provider_id": provider_id,
        "summary": summary,
        "streams": provider_streams[: max(0, int(limit or 0))] if include_streams else [],
        "truncated": include_streams and len(provider_streams) > max(0, int(limit or 0)),
    }


def build_bulk_quality_stats(
    streams: Iterable[Dict[str, Any]],
    *,
    stream_ids: Optional[Sequence[int]] = None,
    provider_ids: Optional[Sequence[int]] = None,
    provider_limit: int = 100,
) -> Dict[str, Any]:
    stream_list = [stream for stream in streams if isinstance(stream, dict)]
    stream_id_set = {_coerce_int(stream_id) for stream_id in (stream_ids or [])}
    stream_id_set.discard(None)
    provider_id_list = [
        provider_id for provider_id in (_coerce_int(value) for value in (provider_ids or [])) if provider_id is not None
    ]

    selected_streams = [
        build_stream_quality_stats(stream)
        for stream in stream_list
        if _coerce_int(stream.get("id")) in stream_id_set
    ]
    providers = {
        str(provider_id): build_provider_quality_stats(
            stream_list,
            provider_id,
            include_streams=True,
            limit=provider_limit,
        )
        for provider_id in provider_id_list
    }

    return {
        "version": 2,
        "streams": selected_streams,
        "providers": providers,
        "summary": _summarize_stream_stats(selected_streams),
    }
