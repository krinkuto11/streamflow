"""Telemetry and dead-stream API handler functions extracted from web_api."""

import json
from datetime import datetime, timedelta
from typing import Any, Callable

from flask import jsonify

from apps.core.logging_config import setup_logging

logger = setup_logging(__name__)


def get_changelog_response(*, request_args: Any):
    """Handle changelog listing with in-memory pagination over telemetry runs."""
    try:
        days = request_args.get("days", 7, type=int)
        page = request_args.get("page", 1, type=int)
        limit = request_args.get("limit", 10, type=int)

        from apps.telemetry.telemetry_db import Run, get_session

        cutoff = datetime.utcnow() - timedelta(days=days)
        session = get_session()

        try:
            runs = (
                session.query(Run)
                .filter(Run.timestamp >= cutoff, Run.run_type != "acestream_monitor")
                .order_by(Run.timestamp.desc())
                .all()
            )

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
                        "timestamp": run.timestamp.isoformat(),
                        "action": run.run_type,
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
