"""Telemetry and dead-stream API handler functions extracted from web_api."""

import csv
import io
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from flask import Response, jsonify

from apps.core.logging_config import setup_logging

logger = setup_logging(__name__)

DEAD_STREAM_EXPORT_FIELDS = [
    "stream_id",
    "channel_id",
    "provider_id",
    "provider_name",
    "reason",
    "marked_dead_at",
    "stream_name",
    "url",
]
RUN_STREAM_EXPORT_FIELDS = [
    "run_id",
    "run_timestamp",
    "action",
    "job_category",
    "job_outcome",
    "profile_id",
    "profile_name",
    "channel_id",
    "channel_name",
    "bucket",
    "stream_id",
    "stream_name",
    "provider_id",
    "provider_name",
    "status",
    "reason",
    "reason_detail",
    "quality_reason",
    "quality_reason_detail",
    "resolution",
    "fps",
    "bitrate",
    "video_codec",
    "audio_codec",
    "score",
]
RUN_SNAPSHOT_EXPORT_FIELDS = [
    "schema_version",
    "run_id",
    "run_mode",
    "start_source",
    "started_at",
    "completed_at",
    "duration_seconds",
    "streamflow_version",
    "streamflow_commit",
    "channel_id",
    "channel_name",
    "forced",
    "forced_profile_id",
    "forced_period_id",
    "force_check",
    "provider_limit_override",
    "is_epg_scheduled",
    "effective_profile_count",
    "channel_count",
    "effective_profiles",
    "quality_rules",
    "capacity_profile_context",
    "feature_flags",
    "dispatcharr_status",
    "stale_warnings",
    "teamarr_status",
    "m3u_refresh",
    "result_summary",
    "limits",
    "snapshot_size_bytes",
    "snapshot_truncated",
    "snapshot_omitted_reason",
]
DEAD_STREAM_EXPORT_FORMATS = {
    "txt": ("|", "txt", "text/plain; charset=utf-8"),
    "pipe": ("|", "txt", "text/plain; charset=utf-8"),
    "csv": (",", "csv", "text/csv; charset=utf-8"),
    "tsv": ("\t", "tsv", "text/tab-separated-values; charset=utf-8"),
    "json": (None, "json", "application/json; charset=utf-8"),
}
_FALSE_VALUES = {"0", "false", "no", "off"}
_RUN_DEAD_STATUSES = {
    "dead",
    "blank",
    "freeze",
    "low_quality",
    "offline",
    "error",
    "failed",
    "timeout",
}
_RUN_DEAD_REASONS = _RUN_DEAD_STATUSES | {
    "no_streams",
    "all_failed",
    "quality_failed",
    "probe_failed",
    "connection_failed",
    "black_screen",
    "frozen_video",
}
_RUN_EMPTY_VALUES = {"", "none", "null", "n/a", "na", "unknown", "-"}
_RUN_SNAPSHOT_PRIVATE_KEY_FRAGMENTS = (
    "url",
    "credential",
    "password",
    "passwd",
    "token",
    "secret",
    "apikey",
    "authorization",
    "authheader",
    "cookie",
    "header",
    "streamdetails",
    "streamstats",
    "raw",
    "log",
    "screenshot",
)
_SNAPSHOT_EXPORT_OMIT = object()


def get_changelog_response(*, request_args: Any):
    """Handle changelog listing with in-memory pagination over telemetry runs."""
    try:
        days = request_args.get("days", 7, type=int)
        page = request_args.get("page", 1, type=int)
        limit = request_args.get("limit", 10, type=int)
        job_category = (request_args.get("job_category") or request_args.get("category") or "").strip()
        job_outcome = (request_args.get("job_outcome") or request_args.get("outcome") or "").strip()
        action_filter = (request_args.get("action") or "").strip()

        from apps.telemetry.telemetry_db import Run, get_session

        cutoff = datetime.utcnow() - timedelta(days=days)
        session = get_session()

        try:
            query = session.query(Run).filter(Run.timestamp >= cutoff)
            if job_category:
                query = query.filter(Run.job_category == job_category)
            if job_outcome:
                query = query.filter(Run.job_outcome == job_outcome)
            if action_filter:
                query = query.filter(Run.run_type == action_filter)

            runs = query.order_by(Run.timestamp.desc()).all()

            merged_changelog = []
            for run in runs:
                details = {}
                raw_details = getattr(run, "raw_details", None)
                if raw_details:
                    details = json.loads(raw_details)

                subentries = []
                raw_subentries = getattr(run, "raw_subentries", None)
                if raw_subentries:
                    subentries = json.loads(raw_subentries)

                merged_changelog.append(
                    {
                        "id": run.id,
                        "timestamp": run.timestamp.isoformat(),
                        "action": run.run_type,
                        "job_category": getattr(run, "job_category", None),
                        "job_outcome": getattr(run, "job_outcome", None),
                        "job_subject_ref": getattr(run, "job_subject_ref", None),
                        "job_correlation_id": getattr(run, "job_correlation_id", None),
                        "details": details,
                        "subentries": subentries,
                    }
                )

            total = len(merged_changelog)
            total_pages = (total + limit - 1) // limit if limit > 0 else 0
            start_idx = (page - 1) * limit
            end_idx = start_idx + limit
            paginated_data = merged_changelog[start_idx:end_idx] if limit > 0 else merged_changelog

            return jsonify(
                {
                    "data": paginated_data,
                    "page": page,
                    "limit": limit,
                    "total": total,
                    "total_pages": total_pages,
                }
            )
        finally:
            session.close()
    except Exception as exc:
        logger.error(f"Error getting changelog: {exc}")
        return jsonify({"error": "Internal Server Error"}), 500


def get_dead_streams_response(
    *,
    request_args: Any,
    parse_pagination_params: Callable[..., Any],
    default_per_page: int,
    max_per_page: int,
):
    """Handle dead-stream listing with SQL-native pagination and sorting."""
    try:
        page_param = request_args.get("page", "1")
        per_page_param = request_args.get("per_page", str(default_per_page))
        sort_by = request_args.get("sort_by", "marked_dead_at")
        sort_dir = request_args.get("sort_dir", "desc")
        search = request_args.get("search", "").strip()

        page, per_page, err = parse_pagination_params(
            page_param,
            per_page_param,
            default_per_page=default_per_page,
            max_per_page=max_per_page,
        )
        if err:
            return err

        if sort_dir not in ("asc", "desc"):
            sort_dir = "desc"

        from apps.database.manager import get_db_manager

        db = get_db_manager()
        result = db.get_dead_streams_paginated(
            page=page or 1,
            per_page=per_page,
            sort_by=sort_by,
            sort_dir=sort_dir,
            search=search,
        )

        return jsonify(
            {
                "total_dead_streams": result["total"],
                "dead_streams": result["items"],
                "pagination": {
                    "page": result["page"],
                    "per_page": result["per_page"],
                    "total_pages": result["total_pages"],
                    "has_next": result["has_next"],
                    "has_prev": result["has_prev"],
                },
            }
        )
    except Exception as exc:
        logger.error(f"Error getting dead streams: {exc}")
        return jsonify({"error": "Internal Server Error"}), 500


def _arg(request_args: Any, name: str, default: Any = None) -> Any:
    try:
        return request_args.get(name, default)
    except TypeError:
        return request_args.get(name) or default


def _as_bool(value: Any, default: bool = True) -> bool:
    if value is None:
        return default
    return str(value).strip().lower() not in _FALSE_VALUES


def _as_int(value: Any) -> Any:
    try:
        return int(value)
    except (TypeError, ValueError):
        return value


def _coerce_dead_stream_rows(raw: Any) -> List[Dict[str, Any]]:
    if isinstance(raw, dict):
        rows = []
        for url, item in raw.items():
            if not isinstance(item, dict):
                continue
            row = dict(item)
            row.setdefault("url", url)
            rows.append(row)
        return rows
    if isinstance(raw, list):
        return [dict(item) for item in raw if isinstance(item, dict)]
    return []


def _provider_value_missing(value: Any) -> bool:
    return value is None or (isinstance(value, str) and value.strip() == "")


def _provider_is_numeric(value: Any) -> bool:
    return str(value or "").strip().isdigit()


def _provider_reference_context() -> Dict[str, Any]:
    try:
        from apps.udi import get_udi_manager

        udi = get_udi_manager()
        if not udi:
            return {"streams": {}, "account_names_by_id": {}, "account_ids_by_name": {}}
        accounts = udi.get_m3u_accounts() if hasattr(udi, "get_m3u_accounts") else []
        account_names_by_id: Dict[str, Any] = {}
        account_name_candidates: Dict[str, List[Any]] = {}
        for account in accounts or []:
            if not isinstance(account, dict):
                continue
            account_id = account.get("id")
            account_name = account.get("name")
            if not _provider_value_missing(account_id):
                account_names_by_id[str(account_id)] = account_name
            if not _provider_value_missing(account_id) and account_name:
                account_name_candidates.setdefault(_text(account_name), []).append(account_id)

        account_ids_by_name = {
            name: ids[0]
            for name, ids in account_name_candidates.items()
            if len({str(item) for item in ids}) == 1
        }

        context = {}
        streams = udi.get_streams(log_result=False) if hasattr(udi, "get_streams") else []
        for stream in streams or []:
            if not isinstance(stream, dict):
                continue
            stream_id = stream.get("id")
            if stream_id is None:
                continue
            m3u_account = stream.get("m3u_account")
            provider_id = _first_present(
                stream.get("provider_id"),
                stream.get("m3u_account_id"),
                stream.get("m3u_account_id_id"),
            )
            if _provider_value_missing(provider_id) and _provider_is_numeric(m3u_account):
                provider_id = m3u_account
            provider_name = (
                stream.get("provider_name")
                or stream.get("m3u_account_name")
                or account_names_by_id.get(str(provider_id))
                or (m3u_account if not _provider_is_numeric(m3u_account) else None)
            )
            if _provider_value_missing(provider_id) and not _provider_value_missing(provider_name):
                provider_id = account_ids_by_name.get(_text(provider_name))
            context[int(stream_id)] = {
                "provider_id": provider_id,
                "provider_name": provider_name,
            }
        return {
            "streams": context,
            "account_names_by_id": account_names_by_id,
            "account_ids_by_name": account_ids_by_name,
        }
    except Exception as exc:
        logger.debug(f"Provider reference enrichment unavailable: {exc}")
        return {"streams": {}, "account_names_by_id": {}, "account_ids_by_name": {}}


def _provider_context_by_stream_id() -> Dict[int, Dict[str, Any]]:
    return _provider_reference_context().get("streams", {})


def _enrich_run_stream_provider_fields(
    row: Dict[str, Any],
    provider_refs: Dict[str, Any],
) -> Dict[str, Any]:
    enriched = dict(row)
    provider_id = enriched.get("provider_id")
    provider_name = enriched.get("provider_name")
    stream_context = (provider_refs.get("streams") or {}).get(_as_int(enriched.get("stream_id")), {})
    account_names_by_id = provider_refs.get("account_names_by_id") or {}
    account_ids_by_name = provider_refs.get("account_ids_by_name") or {}

    if _provider_value_missing(provider_id):
        provider_id = stream_context.get("provider_id")
    if _provider_value_missing(provider_name):
        provider_name = stream_context.get("provider_name")
    if _provider_value_missing(provider_name) and not _provider_value_missing(provider_id):
        provider_name = account_names_by_id.get(str(provider_id))
    if _provider_value_missing(provider_id) and not _provider_value_missing(provider_name):
        provider_id = account_ids_by_name.get(_text(provider_name))

    enriched["provider_id"] = None if _provider_value_missing(provider_id) else provider_id
    enriched["provider_name"] = None if _provider_value_missing(provider_name) else provider_name
    return enriched


def _prepare_dead_stream_export_rows(
    raw: Any,
    *,
    search: str = "",
    reason: str = "",
    channel_id: str = "",
    provider_id: str = "",
    provider_name: str = "",
    sort_by: str = "marked_dead_at",
    sort_dir: str = "desc",
    enrich_providers: bool = True,
) -> List[Dict[str, Any]]:
    rows = _coerce_dead_stream_rows(raw)
    provider_context = _provider_context_by_stream_id() if enrich_providers else {}
    for row in rows:
        context = provider_context.get(_as_int(row.get("stream_id")), {})
        if _provider_value_missing(row.get("provider_id")):
            row["provider_id"] = context.get("provider_id")
        if _provider_value_missing(row.get("provider_name")):
            row["provider_name"] = context.get("provider_name")

    search_text = str(search or "").strip().casefold()
    reason_text = str(reason or "").strip().casefold()
    channel_text = str(channel_id or "").strip()
    provider_id_text = str(provider_id or "").strip()
    provider_name_text = str(provider_name or "").strip().casefold()

    if search_text:
        rows = [
            row for row in rows
            if search_text in str(row.get("stream_name") or "").casefold()
            or search_text in str(row.get("url") or "").casefold()
        ]
    if reason_text:
        rows = [row for row in rows if str(row.get("reason") or "").casefold() == reason_text]
    if channel_text:
        rows = [row for row in rows if str(row.get("channel_id") or "") == channel_text]
    if provider_id_text:
        rows = [row for row in rows if str(row.get("provider_id") or "") == provider_id_text]
    if provider_name_text:
        rows = [
            row for row in rows
            if provider_name_text in str(row.get("provider_name") or "").casefold()
        ]

    if sort_by not in DEAD_STREAM_EXPORT_FIELDS:
        sort_by = "marked_dead_at"
    reverse = str(sort_dir or "desc").strip().lower() != "asc"

    def sort_key(row: Dict[str, Any]) -> Tuple[bool, Any]:
        value = row.get(sort_by)
        if sort_by in {"stream_id", "channel_id", "provider_id"}:
            value = _as_int(value)
        if isinstance(value, str):
            value = value.casefold()
        return value in (None, ""), value

    return sorted(rows, key=sort_key, reverse=reverse)


def _clean_delimited_value(value: Any, delimiter: str) -> str:
    text = "" if value is None else str(value)
    text = text.replace("\r", " ").replace("\n", " ").strip()
    if delimiter == "\t":
        return text.replace("\t", " ")
    return text.replace(delimiter, f"\\{delimiter}")


def _render_dead_stream_export(
    rows: List[Dict[str, Any]],
    *,
    export_format: str,
    include_url: bool = True,
    generated_at: Optional[str] = None,
) -> Tuple[str, str, str]:
    delimiter, extension, mimetype = DEAD_STREAM_EXPORT_FORMATS[export_format]
    fields = list(DEAD_STREAM_EXPORT_FIELDS)
    if not include_url:
        fields.remove("url")
    generated_at = generated_at or datetime.now(timezone.utc).isoformat()

    if export_format == "json":
        content = json.dumps(
            {
                "generated_at": generated_at,
                "format": "json",
                "total_dead_streams": len(rows),
                "fields": fields,
                "dead_streams": [{field: row.get(field) for field in fields} for row in rows],
            },
            indent=2,
        )
        return content + "\n", extension, mimetype

    output = io.StringIO()
    if export_format == "csv":
        writer = csv.DictWriter(output, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field) for field in fields})
    else:
        output.write(delimiter.join(fields) + "\n")
        for row in rows:
            output.write(
                delimiter.join(_clean_delimited_value(row.get(field), delimiter) for field in fields)
                + "\n"
            )
    return output.getvalue(), extension, mimetype


def _first_present(*values: Any) -> Any:
    for value in values:
        if value not in (None, ""):
            return value
    return None


def _text(value: Any) -> str:
    return str(value or "").strip().casefold()


def _normalise_snapshot_key(key: Any) -> str:
    return "".join(ch for ch in str(key or "").strip().casefold() if ch.isalnum())


def _is_private_snapshot_export_key(key: Any) -> bool:
    normalised = _normalise_snapshot_key(key)
    if not normalised:
        return False
    return any(fragment in normalised for fragment in _RUN_SNAPSHOT_PRIVATE_KEY_FRAGMENTS)


def _scrub_snapshot_export_value(value: Any, *, depth: int = 0) -> Any:
    if depth > 6:
        return _SNAPSHOT_EXPORT_OMIT
    if isinstance(value, dict):
        scrubbed = {}
        for key, item in value.items():
            if _is_private_snapshot_export_key(key):
                continue
            safe_value = _scrub_snapshot_export_value(item, depth=depth + 1)
            if safe_value is not _SNAPSHOT_EXPORT_OMIT:
                scrubbed[str(key)] = safe_value
        return scrubbed
    if isinstance(value, list):
        scrubbed_items = []
        for item in value[:50]:
            safe_value = _scrub_snapshot_export_value(item, depth=depth + 1)
            if safe_value is not _SNAPSHOT_EXPORT_OMIT:
                scrubbed_items.append(safe_value)
        return scrubbed_items
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _safe_run_snapshot_for_export(details: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if not isinstance(details, dict):
        return None
    snapshot = details.get("run_snapshot")
    if not isinstance(snapshot, dict):
        return None

    exported = {}
    for key in RUN_SNAPSHOT_EXPORT_FIELDS:
        if key not in snapshot or _is_private_snapshot_export_key(key):
            continue
        safe_value = _scrub_snapshot_export_value(snapshot.get(key))
        if safe_value is not _SNAPSHOT_EXPORT_OMIT:
            exported[key] = safe_value
    return exported or None


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return _text(value) in {"1", "true", "yes", "on"}


def _zero_or_empty_metric(value: Any) -> bool:
    text = _text(value)
    if text in _RUN_EMPTY_VALUES:
        return True
    try:
        return float(text.replace(",", ".")) <= 0
    except (TypeError, ValueError):
        return False


def _zero_or_empty_resolution(value: Any) -> bool:
    text = _text(value).replace(" ", "")
    if text in _RUN_EMPTY_VALUES or text == "0x0":
        return True
    parts = text.split("x", 1)
    if len(parts) != 2:
        return False
    try:
        return int(float(parts[0])) <= 0 or int(float(parts[1])) <= 0
    except (TypeError, ValueError):
        return False


def _score_is_zero_or_worse(value: Any) -> bool:
    if value in (None, ""):
        return False
    try:
        return float(str(value).replace(",", ".")) <= 0
    except (TypeError, ValueError):
        return False


def _normalise_run_profile_fields(row: Dict[str, Any]) -> None:
    row["profile_id"] = _first_present(
        row.get("profile_id"),
        row.get("automation_profile_id"),
        row.get("quality_profile_id"),
        row.get("forced_profile_id"),
    )
    row["profile_name"] = _first_present(
        row.get("profile_name"),
        row.get("automation_profile_name"),
        row.get("quality_profile_name"),
    )


def _infer_run_stream_dead_reason(row: Dict[str, Any]) -> Optional[str]:
    for key in ("status", "reason", "quality_reason"):
        value = _text(row.get(key))
        if value in _RUN_DEAD_REASONS:
            return value

    if _truthy(row.get("blank_detected")):
        return "blank"
    if _truthy(row.get("freeze_detected")):
        return "freeze"
    if row.get("bucket") == "dead":
        reason = _text(row.get("reason"))
        return reason if reason and reason not in _RUN_EMPTY_VALUES else "dead"

    has_dead_metrics = (
        _zero_or_empty_resolution(row.get("resolution"))
        and _zero_or_empty_metric(row.get("fps"))
        and _zero_or_empty_metric(row.get("bitrate"))
    )
    if has_dead_metrics and _score_is_zero_or_worse(row.get("score")):
        return "low_quality"
    return None


def _enrich_run_stream_row(row: Dict[str, Any]) -> Dict[str, Any]:
    enriched = dict(row)
    _normalise_run_profile_fields(enriched)
    dead_reason = _infer_run_stream_dead_reason(enriched)
    if not dead_reason:
        return enriched

    current_status = _text(enriched.get("status"))
    if not current_status or current_status == "completed":
        enriched["status"] = dead_reason if dead_reason in {"blank", "freeze", "low_quality"} else "dead"
    if not enriched.get("reason") or _text(enriched.get("reason")) in {"completed", "none"}:
        enriched["reason"] = dead_reason
    if not enriched.get("reason_detail") or _text(enriched.get("reason_detail")) == "none":
        enriched["reason_detail"] = (
            "inferred_from_run_metrics"
            if dead_reason == "low_quality" and _has_zero_score_or_metrics(enriched)
            else dead_reason
        )
    if not enriched.get("quality_reason") or _text(enriched.get("quality_reason")) == "none":
        enriched["quality_reason"] = dead_reason
    if not enriched.get("quality_reason_detail") or _text(enriched.get("quality_reason_detail")) == "none":
        enriched["quality_reason_detail"] = enriched.get("reason_detail") or dead_reason
    return enriched


def _has_zero_score_or_metrics(row: Dict[str, Any]) -> bool:
    return (
        _score_is_zero_or_worse(row.get("score"))
        or _zero_or_empty_resolution(row.get("resolution"))
        or _zero_or_empty_metric(row.get("fps"))
        or _zero_or_empty_metric(row.get("bitrate"))
    )


def _is_dead_run_stream_row(row: Dict[str, Any]) -> bool:
    return _infer_run_stream_dead_reason(row) is not None


def _run_stream_collection_specs() -> Dict[str, str]:
    return {
        "dead_streams": "dead",
        "revived_streams": "revived",
        "skipped_streams": "skipped",
        "preempted_streams": "preempted",
        "checked_streams": "checked",
        "stream_details": "checked",
        "stream_stats": "checked",
    }


def _run_stream_row(
    item: Dict[str, Any],
    *,
    bucket: str,
    run_context: Dict[str, Any],
    channel_context: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    if not isinstance(item, dict):
        return None

    stream_id = item.get("stream_id", item.get("id"))
    stream_name = item.get("stream_name") or item.get("name")
    if stream_id in (None, "") and not stream_name:
        return None

    m3u_account = item.get("m3u_account")
    provider_id = _first_present(item.get("provider_id"), item.get("m3u_account_id"))
    if provider_id in (None, "") and _text(m3u_account).isdigit():
        provider_id = m3u_account
    provider_name = item.get("provider_name") or item.get("m3u_account_name")
    if provider_name in (None, "") and not _text(m3u_account).isdigit():
        provider_name = m3u_account
    reason = (
        item.get("reason")
        or item.get("skip_reason")
        or item.get("dead_reason")
        or item.get("quality_reason")
        or item.get("status")
    )
    profile_id = _first_present(
        item.get("profile_id"),
        item.get("automation_profile_id"),
        item.get("quality_profile_id"),
        item.get("forced_profile_id"),
        channel_context.get("profile_id"),
        channel_context.get("automation_profile_id"),
        channel_context.get("quality_profile_id"),
        channel_context.get("forced_profile_id"),
    )
    profile_name = _first_present(
        item.get("profile_name"),
        item.get("automation_profile_name"),
        item.get("quality_profile_name"),
        channel_context.get("profile_name"),
        channel_context.get("automation_profile_name"),
        channel_context.get("quality_profile_name"),
    )

    row = {
        **run_context,
        "profile_id": profile_id,
        "profile_name": profile_name,
        "channel_id": item.get("channel_id", channel_context.get("channel_id")),
        "channel_name": item.get("channel_name", channel_context.get("channel_name")),
        "bucket": bucket,
        "stream_id": stream_id,
        "stream_name": stream_name,
        "provider_id": provider_id,
        "provider_name": provider_name,
        "status": item.get("status") or item.get("analysis_status"),
        "reason": reason,
        "reason_detail": item.get("reason_detail") or item.get("quality_reason_detail"),
        "quality_reason": item.get("quality_reason"),
        "quality_reason_detail": item.get("quality_reason_detail"),
        "resolution": item.get("resolution"),
        "fps": item.get("fps"),
        "bitrate": item.get("bitrate") or item.get("bitrate_kbps"),
        "video_codec": item.get("video_codec"),
        "audio_codec": item.get("audio_codec"),
        "score": item.get("score"),
        "blank_detected": item.get("blank_detected"),
        "freeze_detected": item.get("freeze_detected"),
    }
    if item.get("url") or item.get("stream_url"):
        row["url"] = item.get("url") or item.get("stream_url")
    return row


def _extract_changelog_run_stream_rows(run: Any, details: Dict[str, Any], subentries: List[Any]) -> List[Dict[str, Any]]:
    specs = _run_stream_collection_specs()
    run_context = {
        "run_id": getattr(run, "id", None),
        "run_timestamp": getattr(run, "timestamp", None).isoformat() if getattr(run, "timestamp", None) else None,
        "action": getattr(run, "run_type", None),
        "job_category": getattr(run, "job_category", None),
        "job_outcome": getattr(run, "job_outcome", None),
    }
    rows: List[Dict[str, Any]] = []
    provider_refs = _provider_reference_context()

    def walk(node: Any, channel_context: Optional[Dict[str, Any]] = None) -> None:
        context = dict(channel_context or {})
        if isinstance(node, list):
            for item in node:
                walk(item, context)
            return
        if not isinstance(node, dict):
            return

        if node.get("channel_id") is not None:
            context["channel_id"] = node.get("channel_id")
        if node.get("channel_name"):
            context["channel_name"] = node.get("channel_name")
        for field in (
            "profile_id",
            "profile_name",
            "automation_profile_id",
            "automation_profile_name",
            "quality_profile_id",
            "quality_profile_name",
            "forced_profile_id",
        ):
            if node.get(field) not in (None, ""):
                context[field] = node.get(field)

        for key, bucket in specs.items():
            collection = node.get(key)
            if isinstance(collection, list):
                for item in collection:
                    row = _run_stream_row(
                        item,
                        bucket=bucket,
                        run_context=run_context,
                        channel_context=context,
                    )
                    if row:
                        rows.append(row)

        for key, value in node.items():
            if key in specs:
                continue
            if isinstance(value, (dict, list)):
                walk(value, context)

    walk(details or {})
    walk(subentries or [])

    deduped: List[Dict[str, Any]] = []
    seen = set()
    for raw_row in rows:
        row = _enrich_run_stream_provider_fields(_enrich_run_stream_row(raw_row), provider_refs)
        key = (
            row.get("bucket"),
            row.get("channel_id"),
            row.get("stream_id"),
            row.get("stream_name"),
            row.get("reason"),
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(row)
    return deduped


def _render_changelog_run_export(
    rows: List[Dict[str, Any]],
    *,
    export_format: str,
    scope: str = "all",
    include_url: bool = False,
    run_snapshot: Optional[Dict[str, Any]] = None,
    generated_at: Optional[str] = None,
) -> Tuple[str, str, str]:
    delimiter, extension, mimetype = DEAD_STREAM_EXPORT_FORMATS[export_format]
    fields = list(RUN_STREAM_EXPORT_FIELDS)
    if include_url:
        fields.append("url")
    generated_at = generated_at or datetime.now(timezone.utc).isoformat()

    if export_format == "json":
        payload = {
            "generated_at": generated_at,
            "format": "json",
            "scope": scope,
            "total_stream_rows": len(rows),
            "fields": fields,
            "streams": [{field: row.get(field) for field in fields} for row in rows],
        }
        if run_snapshot is not None:
            payload["run_snapshot"] = run_snapshot
        content = json.dumps(payload, indent=2)
        return content + "\n", extension, mimetype

    output = io.StringIO()
    if export_format == "csv":
        writer = csv.DictWriter(output, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field) for field in fields})
    else:
        output.write(delimiter.join(fields) + "\n")
        for row in rows:
            output.write(
                delimiter.join(_clean_delimited_value(row.get(field), delimiter) for field in fields)
                + "\n"
            )
    return output.getvalue(), extension, mimetype


def export_changelog_run_response(*, run_id: int, request_args: Any):
    """Export stream rows captured inside one changelog/telemetry run."""
    try:
        requested_format = str(_arg(request_args, "format", "json") or "json").strip().lower()
        if requested_format not in DEAD_STREAM_EXPORT_FORMATS:
            return jsonify({
                "error": "Unsupported changelog run export format",
                "supported_formats": sorted(DEAD_STREAM_EXPORT_FORMATS.keys()),
            }), 400
        requested_scope = str(
            _arg(request_args, "scope", None)
            or _arg(request_args, "stream_scope", None)
            or "all"
        ).strip().lower()
        if requested_scope not in {"all", "dead"}:
            return jsonify({
                "error": "Unsupported changelog run export scope",
                "supported_scopes": ["all", "dead"],
            }), 400

        from apps.telemetry.telemetry_db import Run, get_session

        session = get_session()
        try:
            run = session.query(Run).filter(Run.id == run_id).first()
            if not run:
                return jsonify({"error": "Run not found"}), 404

            details = json.loads(getattr(run, "raw_details", None) or "{}")
            subentries = json.loads(getattr(run, "raw_subentries", None) or "[]")
            rows = _extract_changelog_run_stream_rows(run, details, subentries)
            run_snapshot = _safe_run_snapshot_for_export(details)
            if requested_scope == "dead":
                rows = [row for row in rows if _is_dead_run_stream_row(row)]
        finally:
            session.close()

        content, extension, mimetype = _render_changelog_run_export(
            rows,
            export_format=requested_format,
            scope=requested_scope,
            include_url=_as_bool(_arg(request_args, "include_url", False), default=False),
            run_snapshot=run_snapshot,
        )
        suffix = "-dead" if requested_scope == "dead" else ""
        filename = f"changelog-run-{run_id}{suffix}-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}.{extension}"
        response = Response(content, content_type=mimetype)
        response.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
        response.headers["X-Changelog-Run-Id"] = str(run_id)
        response.headers["X-Changelog-Run-Scope"] = requested_scope
        response.headers["X-Changelog-Stream-Rows"] = str(len(rows))
        response.headers["X-Changelog-Run-Format"] = extension
        return response
    except Exception as exc:
        logger.error(f"Error exporting changelog run {run_id}: {exc}")
        return jsonify({"error": "Internal Server Error"}), 500


def export_dead_streams_response(*, request_args: Any):
    """Export dead-stream rows as pipe-delimited text, CSV, TSV or JSON."""
    try:
        requested_format = str(_arg(request_args, "format", "txt") or "txt").strip().lower()
        if requested_format not in DEAD_STREAM_EXPORT_FORMATS:
            return jsonify({
                "error": "Unsupported dead-stream export format",
                "supported_formats": sorted(DEAD_STREAM_EXPORT_FORMATS.keys()),
            }), 400

        from apps.database.manager import get_db_manager

        db = get_db_manager()
        rows = _prepare_dead_stream_export_rows(
            db.get_dead_streams(as_dict=True),
            search=str(_arg(request_args, "search", "") or ""),
            reason=str(_arg(request_args, "reason", "") or ""),
            channel_id=str(_arg(request_args, "channel_id", "") or ""),
            provider_id=str(_arg(request_args, "provider_id", "") or ""),
            provider_name=str(_arg(request_args, "provider_name", "") or ""),
            sort_by=str(_arg(request_args, "sort_by", "marked_dead_at") or "marked_dead_at"),
            sort_dir=str(_arg(request_args, "sort_dir", "desc") or "desc"),
            enrich_providers=_as_bool(_arg(request_args, "enrich_providers", True), default=True),
        )
        content, extension, mimetype = _render_dead_stream_export(
            rows,
            export_format=requested_format,
            include_url=_as_bool(_arg(request_args, "include_url", True), default=True),
        )
        filename = f"dead-streams-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}.{extension}"
        response = Response(content, content_type=mimetype)
        response.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
        response.headers["X-Dead-Streams-Count"] = str(len(rows))
        response.headers["X-Dead-Streams-Format"] = extension
        return response
    except Exception as exc:
        logger.error(f"Error exporting dead streams: {exc}")
        return jsonify({"error": "Internal Server Error"}), 500


def revive_dead_stream_response(
    *,
    payload: Any,
    get_stream_checker_service: Callable[[], Any],
):
    """Handle marking one dead stream as alive."""
    try:
        stream_url = (payload or {}).get("stream_url")
        if not stream_url:
            return jsonify({"error": "stream_url is required"}), 400

        from apps.database.manager import get_db_manager

        db = get_db_manager()
        checker = get_stream_checker_service()
        if checker and checker.dead_streams_tracker:
            success = checker.dead_streams_tracker.mark_as_alive(stream_url)
        else:
            success = db.remove_dead_stream(stream_url)

        if success:
            return jsonify({"success": True, "message": "Stream marked as alive"})
        return jsonify({"error": "Failed to mark stream as alive"}), 500
    except Exception as exc:
        logger.error(f"Error reviving dead stream: {exc}")
        return jsonify({"error": "Internal Server Error"}), 500


def clear_all_dead_streams_response(*, get_stream_checker_service: Callable[[], Any]):
    """Handle clearing all dead streams from storage/tracker."""
    try:
        from apps.database.manager import get_db_manager

        db = get_db_manager()
        dead_count = db.get_dead_streams_paginated(page=1, per_page=1)["total"]

        checker = get_stream_checker_service()
        if checker and checker.dead_streams_tracker:
            checker.dead_streams_tracker.clear_all_dead_streams()
        else:
            db.clear_all_dead_streams()

        return jsonify(
            {
                "success": True,
                "message": f"Cleared {dead_count} dead stream(s)",
                "cleared_count": dead_count,
            }
        )
    except Exception as exc:
        logger.error(f"Error clearing dead streams: {exc}")
        return jsonify({"error": "Internal Server Error"}), 500
