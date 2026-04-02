"""Stream session, proxy, and session-settings API handlers extracted from web_api."""

import queue
import time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from flask import Response, jsonify, make_response, send_from_directory

from apps.api.schemas import (
    GroupStreamSessionsCreateSchema,
    SessionIdsPayloadSchema,
    SessionSettingsUpdateSchema,
    StreamSessionCreateSchema,
)
from apps.core.api_responses import error_response
from apps.core.exceptions import ValidationError
from apps.core.logging_config import setup_logging

logger = setup_logging(__name__)


def get_stream_sessions_response(*, status: str, get_session_manager: Callable[[], Any]):
    """Get all stream monitoring sessions or filter by status."""
    try:
        session_manager = get_session_manager()

        if status == "active":
            sessions = session_manager.get_active_sessions()
        else:
            sessions = session_manager.get_all_sessions()

        sessions_data = []
        for session in sessions:
            session_dict = {
                "session_id": session.session_id,
                "channel_id": session.channel_id,
                "channel_name": session.channel_name,
                "regex_filter": session.regex_filter,
                "created_at": session.created_at,
                "is_active": session.is_active,
                "pre_event_minutes": session.pre_event_minutes,
                "stagger_ms": session.stagger_ms,
                "timeout_ms": session.timeout_ms,
                "probe_interval_ms": session.probe_interval_ms,
                "screenshot_interval_seconds": session.screenshot_interval_seconds,
                "window_size": session.window_size,
                "stream_count": len(session.streams) if session.streams else 0,
                "active_streams": (
                    sum(1 for s in session.streams.values() if s.status in ["stable", "review"])
                    if session.streams
                    else 0
                ),
                "stable_count": sum(1 for s in session.streams.values() if s.status == "stable") if session.streams else 0,
                "review_count": sum(1 for s in session.streams.values() if s.status == "review") if session.streams else 0,
                "quarantined_count": (
                    sum(1 for s in session.streams.values() if s.status == "quarantined" or s.is_quarantined)
                    if session.streams
                    else 0
                ),
                "epg_event_id": session.epg_event_id,
                "epg_event_title": session.epg_event_title,
                "epg_event_start": session.epg_event_start,
                "epg_event_end": session.epg_event_end,
                "epg_event_description": session.epg_event_description,
                "channel_logo_url": session.channel_logo_url,
                "channel_tvg_id": session.channel_tvg_id,
                "auto_created": session.auto_created,
                "auto_create_rule_id": session.auto_create_rule_id,
            }
            sessions_data.append(session_dict)

        return jsonify(sessions_data), 200
    except Exception as exc:
        logger.error(f"Error getting stream sessions: {exc}", exc_info=True)
        return jsonify({"error": "Internal Server Error"}), 500


def create_stream_session_response(
    *,
    payload: Optional[Dict[str, Any]],
    get_session_manager: Callable[[], Any],
    get_regex_matcher: Callable[[], Any],
):
    """Create a new stream monitoring session."""
    try:
        data = StreamSessionCreateSchema.from_payload(payload or {})

        regex_filter = data.regex_filter
        match_by_tvg_id = False

        if not regex_filter:
            try:
                regex_matcher = get_regex_matcher()
                group_id = None
                match_config = regex_matcher.get_channel_match_config(data.channel_id, group_id)
                match_by_tvg_id = match_config.get("match_by_tvg_id", False)
                default_regex = None
                regex_filter = regex_matcher.get_channel_regex_filter(
                    data.channel_id,
                    default=default_regex,
                    group_id=group_id,
                )
                logger.info(
                    f"Using channel config for manual session: regex='{regex_filter}', match_by_tvg_id={match_by_tvg_id}"
                )
            except Exception as exc:
                logger.debug(f"Could not get channel match config: {exc}")
                regex_filter = ".*"

        session_manager = get_session_manager()
        session_id = session_manager.create_session(
            channel_id=data.channel_id,
            regex_filter=regex_filter,
            pre_event_minutes=data.pre_event_minutes,
            stagger_ms=data.stagger_ms,
            timeout_ms=data.timeout_ms,
            epg_event=data.epg_event,
            auto_created=data.auto_created,
            auto_create_rule_id=data.auto_create_rule_id,
            match_by_tvg_id=match_by_tvg_id,
            enable_looping_detection=data.enable_looping_detection,
            enable_logo_detection=data.enable_logo_detection,
        )

        return jsonify({"session_id": session_id, "message": "Session created successfully"}), 201
    except ValidationError as exc:
        return error_response(
            exc.message,
            status_code=exc.status_code,
            code=exc.error_code,
            details=exc.details,
        )
    except ValueError:
        return error_response("Invalid value or request parameters", code="validation_error")
    except Exception as exc:
        logger.error(f"Error creating stream session: {exc}", exc_info=True)
        return error_response("Internal Server Error", status_code=500, code="internal_error")


def create_group_stream_sessions_response(
    *,
    payload: Optional[Dict[str, Any]],
    get_udi_manager: Callable[[], Any],
    get_session_manager: Callable[[], Any],
    get_monitoring_service: Callable[[], Any],
    get_regex_matcher: Callable[[], Any],
):
    """Create and start monitoring sessions for all channels in a group."""
    try:
        data = GroupStreamSessionsCreateSchema.from_payload(payload or {})

        udi = get_udi_manager()
        channels = udi.get_channels_by_group(data.group_id)

        if not channels:
            return error_response("Group not found or has no channels", status_code=404, code="not_found")

        if udi.refresh_streams():
            logger.info(f"Refreshed stream list before batch session creation for group {data.group_id}")

        session_manager = get_session_manager()
        monitoring_service = get_monitoring_service()

        created_sessions = []
        errors = []

        for channel in channels:
            try:
                channel_id = channel.get("id")

                channel_regex = data.regex_filter
                match_by_tvg_id = False

                if not channel_regex:
                    try:
                        regex_matcher = get_regex_matcher()
                        group_id_for_channel = (
                            channel.get("group_id")
                            if channel.get("group_id") is not None
                            else channel.get("channel_group_id")
                        )
                        match_config = regex_matcher.get_channel_match_config(str(channel_id), group_id_for_channel)
                        match_by_tvg_id = match_config.get("match_by_tvg_id", False)
                        default_regex = None
                        channel_regex = regex_matcher.get_channel_regex_filter(
                            str(channel_id),
                            default=default_regex,
                            group_id=group_id_for_channel,
                        )
                    except Exception:
                        channel_regex = None

                session_id = session_manager.create_session(
                    channel_id=channel_id,
                    regex_filter=channel_regex,
                    pre_event_minutes=data.pre_event_minutes,
                    stagger_ms=data.stagger_ms,
                    timeout_ms=data.timeout_ms,
                    skip_stream_refresh=True,
                    match_by_tvg_id=match_by_tvg_id,
                    enable_looping_detection=data.enable_looping_detection,
                    enable_logo_detection=data.enable_logo_detection,
                )

                if session_manager.start_session(session_id):
                    created_sessions.append(
                        {
                            "session_id": session_id,
                            "channel_id": channel_id,
                            "channel_name": channel.get("name"),
                        }
                    )
                else:
                    errors.append(f"Failed to start session for channel {channel.get('name')} ({channel_id})")

            except Exception as exc:
                errors.append(f"Error creating session for channel {channel.get('name')} ({channel_id}): {exc}")

        if created_sessions and not monitoring_service._running:
            monitoring_service.start()

        return (
            jsonify(
                {
                    "message": f"Started {len(created_sessions)} sessions from group {data.group_id}",
                    "sessions": created_sessions,
                    "errors": errors,
                }
            ),
            200 if created_sessions else 400,
        )

    except ValidationError as exc:
        return error_response(
            exc.message,
            status_code=exc.status_code,
            code=exc.error_code,
            details=exc.details,
        )
    except Exception as exc:
        logger.error(f"Error creating group stream sessions: {exc}", exc_info=True)
        return error_response("Internal Server Error", status_code=500, code="internal_error")


def get_stream_session_response(
    *,
    session_id: str,
    since_timestamp: Optional[float],
    get_session_manager: Callable[[], Any],
    get_udi_manager: Callable[[], Any],
):
    """Get detailed information about a specific session including streams and metrics."""
    try:
        session_manager = get_session_manager()
        session = session_manager.get_session(session_id)

        if not session:
            return jsonify({"error": "Session not found"}), 404

        from apps.database.connection import get_session as get_db_session
        from apps.database.models import Run, StreamTelemetry
        from apps.stream.stream_session_manager import StreamMetrics

        db_metrics_by_stream: Dict[int, list] = {}
        db_session = get_db_session()
        try:
            telemetry_rows = (
                db_session.query(StreamTelemetry, Run.timestamp)
                .join(Run)
                .filter(
                    StreamTelemetry.channel_id == session.channel_id,
                    Run.timestamp >= datetime.fromtimestamp(session.created_at),
                )
                .order_by(Run.timestamp.asc())
                .all()
            )

            for row, ts in telemetry_rows:
                metric = StreamMetrics(
                    timestamp=ts.timestamp(),
                    speed=0.0 if row.is_dead else 1.0,
                    bitrate=row.bitrate_kbps or 0,
                    fps=row.fps or 0.0,
                    is_alive=not row.is_dead,
                    buffering=False,
                    reliability_score=row.quality_score or 50.0,
                    status="stable" if not row.is_dead else "quarantined",
                    status_reason=None,
                    rank=None,
                    loop_duration=None,
                    display_logo_status="SUCCESS" if not row.is_dead else "PENDING",
                )
                if row.stream_id not in db_metrics_by_stream:
                    db_metrics_by_stream[row.stream_id] = []
                db_metrics_by_stream[row.stream_id].append(metric)
        except Exception as exc:
            logger.error(f"Error loading historical telemetry for session {session_id}: {exc}")
        finally:
            db_session.close()

        streams_data = []
        if session.streams:
            for stream_id, stream_info in session.streams.items():
                db_metrics = db_metrics_by_stream.get(stream_id, [])
                stream_metrics_history = stream_info.metrics_history if stream_info.metrics_history else db_metrics

                stream_dict = {
                    "stream_id": stream_info.stream_id,
                    "url": stream_info.url,
                    "name": stream_info.name,
                    "channel_id": stream_info.channel_id,
                    "width": stream_info.width,
                    "height": stream_info.height,
                    "fps": stream_info.fps,
                    "bitrate": stream_info.bitrate,
                    "m3u_account": stream_info.m3u_account,
                    "hdr_format": stream_info.hdr_format,
                    "status": stream_info.status,
                    "status_reason": getattr(stream_info, "status_reason", None),
                    "transport_health": getattr(stream_info, "transport_health", "Healthy"),
                    "transport_health_summary": getattr(stream_info, "transport_health_summary", ""),
                    "transport_error_density": getattr(stream_info, "transport_error_density", 0.0),
                    "last_loop_time": getattr(stream_info, "last_loop_time", None),
                    "loop_duration": getattr(stream_info, "loop_duration", None),
                    "is_quarantined": stream_info.is_quarantined,
                    "reliability_score": stream_info.reliability_score,
                    "current_speed": stream_metrics_history[-1].speed if stream_metrics_history else 0.0,
                    "rank": stream_info.rank,
                    "last_logo_status": getattr(stream_info, "last_logo_status", "PENDING"),
                    "display_logo_status": getattr(stream_info, "display_logo_status", "PENDING"),
                    "consecutive_logo_misses": getattr(stream_info, "consecutive_logo_misses", 0),
                    "screenshot_path": stream_info.screenshot_path,
                    "screenshot_url": (
                        f"/api/data/screenshots/{Path(stream_info.screenshot_path).name}?t={int(stream_info.last_screenshot_time)}"
                        if stream_info.screenshot_path
                        else None
                    ),
                    "last_screenshot_time": stream_info.last_screenshot_time,
                    "metrics_count": len(stream_metrics_history) if stream_metrics_history else 0,
                    "metrics_history": (
                        [
                            asdict(m)
                            for m in stream_metrics_history
                            if since_timestamp is None or m.timestamp > since_timestamp
                        ][-3600:]
                        if stream_metrics_history
                        else []
                    ),
                }

                if stream_info.status == "review":
                    time_in_review = time.time() - stream_info.last_status_change
                    review_limit = session_manager.get_review_duration()
                    remaining = max(0, review_limit - time_in_review)
                    stream_dict["review_time_remaining"] = remaining

                streams_data.append(stream_dict)

        try:
            udi = get_udi_manager()
            channel = udi.get_channel_by_id(session.channel_id)

            if channel and "streams" in channel:
                order_map = {sid: i for i, sid in enumerate(channel["streams"])}

                def get_sort_key(stream_data):
                    sid = stream_data["stream_id"]
                    if sid in order_map:
                        return (0, order_map[sid])
                    return (1, -stream_data["reliability_score"])

                streams_data.sort(key=get_sort_key)

                for idx, sdata in enumerate(streams_data, start=1):
                    sdata["rank"] = idx
            else:
                streams_data.sort(key=lambda x: x["reliability_score"], reverse=True)

        except Exception as exc:
            logger.warning(f"Failed to sort streams by channel order: {exc}")
            streams_data.sort(key=lambda x: x["reliability_score"], reverse=True)

        session_dict = {
            "session_id": session.session_id,
            "channel_id": session.channel_id,
            "channel_name": session.channel_name,
            "regex_filter": session.regex_filter,
            "created_at": session.created_at,
            "is_active": session.is_active,
            "pre_event_minutes": session.pre_event_minutes,
            "stagger_ms": session.stagger_ms,
            "timeout_ms": session.timeout_ms,
            "probe_interval_ms": session.probe_interval_ms,
            "screenshot_interval_seconds": session.screenshot_interval_seconds,
            "window_size": session.window_size,
            "streams": streams_data,
            "ad_periods": session.ad_periods,
            "epg_event_id": session.epg_event_id,
            "epg_event_title": session.epg_event_title,
            "epg_event_start": session.epg_event_start,
            "epg_event_end": session.epg_event_end,
            "epg_event_description": session.epg_event_description,
            "channel_logo_url": session.channel_logo_url,
            "channel_tvg_id": session.channel_tvg_id,
            "auto_created": session.auto_created,
            "auto_create_rule_id": session.auto_create_rule_id,
        }

        return jsonify(session_dict), 200

    except Exception as exc:
        logger.error(f"Error getting stream session {session_id}: {exc}", exc_info=True)
        return jsonify({"error": "Internal Server Error"}), 500


def quarantine_stream_response(*, session_id: str, stream_id: int, get_session_manager: Callable[[], Any]):
    """Quarantine a stream in a session."""
    try:
        session_manager = get_session_manager()

        if not session_manager.quarantine_stream(session_id, stream_id):
            return (
                jsonify(
                    {
                        "error": "Failed to quarantine stream. It may already be quarantined or session/stream not found."
                    }
                ),
                400,
            )

        return jsonify({"success": True}), 200
    except Exception as exc:
        logger.error(f"Error quarantining stream {stream_id} in {session_id}: {exc}", exc_info=True)
        return jsonify({"error": "Internal Server Error"}), 500


def revive_stream_response(*, session_id: str, stream_id: int, get_session_manager: Callable[[], Any]):
    """Revive a quarantined stream in a session."""
    try:
        session_manager = get_session_manager()

        if not session_manager.revive_stream(session_id, stream_id):
            return (
                jsonify(
                    {
                        "error": "Failed to revive stream. It may not be quarantined or session not found."
                    }
                ),
                400,
            )

        return jsonify({"success": True}), 200
    except Exception as exc:
        logger.error(f"Error reviving stream {stream_id} in {session_id}: {exc}", exc_info=True)
        return jsonify({"error": "Internal Server Error"}), 500


def start_stream_session_response(
    *,
    session_id: str,
    get_session_manager: Callable[[], Any],
    get_monitoring_service: Callable[[], Any],
):
    """Start monitoring for a session."""
    try:
        session_manager = get_session_manager()

        if not session_manager.start_session(session_id):
            return jsonify({"error": "Failed to start session"}), 400

        monitoring_service = get_monitoring_service()
        if not monitoring_service._running:
            monitoring_service.start()

        return jsonify({"message": "Session started successfully"}), 200
    except Exception as exc:
        logger.error(f"Error starting stream session {session_id}: {exc}", exc_info=True)
        return jsonify({"error": "Internal Server Error"}), 500


def stop_stream_session_response(*, session_id: str, get_session_manager: Callable[[], Any]):
    """Stop monitoring for a session."""
    try:
        session_manager = get_session_manager()

        if not session_manager.stop_session(session_id):
            return jsonify({"error": "Failed to stop session"}), 400

        return jsonify({"message": "Session stopped successfully"}), 200
    except Exception as exc:
        logger.error(f"Error stopping stream session {session_id}: {exc}", exc_info=True)
        return jsonify({"error": "Internal Server Error"}), 500


def delete_stream_session_response(*, session_id: str, get_session_manager: Callable[[], Any]):
    """Delete a session."""
    try:
        session_manager = get_session_manager()

        if not session_manager.delete_session(session_id):
            return jsonify({"error": "Session not found"}), 404

        return jsonify({"message": "Session deleted successfully"}), 200
    except Exception as exc:
        logger.error(f"Error deleting stream session {session_id}: {exc}", exc_info=True)
        return jsonify({"error": "Internal Server Error"}), 500


def batch_stop_sessions_response(*, payload: Optional[Dict[str, Any]], get_session_manager: Callable[[], Any]):
    """Stop multiple monitoring sessions."""
    try:
        data = SessionIdsPayloadSchema.from_payload(payload or {})

        session_manager = get_session_manager()
        success_count = 0
        failed_count = 0
        errors = []

        for session_id in data.session_ids:
            if session_manager.stop_session(session_id):
                success_count += 1
            else:
                failed_count += 1
                errors.append(f"Failed to stop session {session_id}")

        return (
            jsonify(
                {
                    "message": f"Processed {len(data.session_ids)} sessions",
                    "success_count": success_count,
                    "failed_count": failed_count,
                    "errors": errors,
                }
            ),
            200,
        )
    except ValidationError as exc:
        return error_response(
            exc.message,
            status_code=exc.status_code,
            code=exc.error_code,
            details=exc.details,
        )
    except Exception as exc:
        logger.error(f"Error in batch stop sessions: {exc}", exc_info=True)
        return error_response("Internal Server Error", status_code=500, code="internal_error")


def batch_delete_sessions_response(*, payload: Optional[Dict[str, Any]], get_session_manager: Callable[[], Any]):
    """Delete multiple monitoring sessions."""
    try:
        data = SessionIdsPayloadSchema.from_payload(payload or {})

        session_manager = get_session_manager()
        success_count = 0
        failed_count = 0
        errors = []

        for session_id in data.session_ids:
            if session_manager.delete_session(session_id):
                success_count += 1
            else:
                failed_count += 1
                errors.append(f"Failed to delete session {session_id}")

        return (
            jsonify(
                {
                    "message": f"Processed {len(data.session_ids)} sessions",
                    "success_count": success_count,
                    "failed_count": failed_count,
                    "errors": errors,
                }
            ),
            200,
        )
    except ValidationError as exc:
        return error_response(
            exc.message,
            status_code=exc.status_code,
            code=exc.error_code,
            details=exc.details,
        )
    except Exception as exc:
        logger.error(f"Error in batch delete sessions: {exc}", exc_info=True)
        return error_response("Internal Server Error", status_code=500, code="internal_error")


def get_stream_metrics_response(
    *,
    session_id: str,
    stream_id: int,
    since_timestamp: Optional[float],
    get_session_manager: Callable[[], Any],
):
    """Get historical metrics for a stream in a session."""
    try:
        session_manager = get_session_manager()
        session = session_manager.get_session(session_id)

        if not session:
            return jsonify({"error": "Session not found"}), 404

        stream_info = session.streams.get(stream_id)
        if not stream_info:
            return jsonify({"error": "Stream not found in session"}), 404

        metrics_data = []
        if stream_info.metrics_history:
            for metric in stream_info.metrics_history:
                if since_timestamp is not None and metric.timestamp <= since_timestamp:
                    continue
                metrics_data.append(
                    {
                        "timestamp": metric.timestamp,
                        "speed": metric.speed,
                        "bitrate": metric.bitrate,
                        "fps": metric.fps,
                        "is_alive": metric.is_alive,
                        "buffering": metric.buffering,
                        "reliability_score": getattr(metric, "reliability_score", 50.0),
                        "status": getattr(metric, "status", "review"),
                        "rank": getattr(metric, "rank", None),
                    }
                )

        metrics_data = metrics_data[-3600:]

        return jsonify({"stream_id": stream_id, "metrics": metrics_data}), 200

    except Exception as exc:
        logger.error(f"Error getting stream metrics: {exc}", exc_info=True)
        return jsonify({"error": "Internal Server Error"}), 500


def serve_screenshot_response(*, filename: str, config_dir: Path):
    """Serve screenshot files with no-cache headers."""
    try:
        screenshots_dir = config_dir / "screenshots"
        response = make_response(send_from_directory(screenshots_dir, filename))
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        return response
    except Exception as exc:
        logger.error(f"Error serving screenshot {filename}: {exc}")
        return jsonify({"error": "Screenshot not found"}), 404


def get_alive_screenshots_response(
    *,
    session_id: str,
    get_session_manager: Callable[[], Any],
):
    """Get screenshots and info for all alive streams in a session."""
    try:
        session_manager = get_session_manager()
        session = session_manager.get_session(session_id)

        if not session:
            return jsonify({"error": "Session not found"}), 404

        screenshots_data = []
        if session.streams:
            for _, stream_info in session.streams.items():
                if not stream_info.is_quarantined and stream_info.screenshot_path:
                    latest_speed = 0.0
                    if stream_info.metrics_history:
                        latest_speed = stream_info.metrics_history[-1].speed

                    if latest_speed >= 0.9:
                        screenshots_data.append(
                            {
                                "stream_id": stream_info.stream_id,
                                "stream_name": stream_info.name,
                                "screenshot_url": (
                                    f"/api/data/screenshots/{Path(stream_info.screenshot_path).name}"
                                    f"?t={int(stream_info.last_screenshot_time)}"
                                ),
                                "reliability_score": stream_info.reliability_score,
                                "m3u_account": stream_info.m3u_account,
                            }
                        )

        screenshots_data.sort(key=lambda x: x["reliability_score"], reverse=True)

        return jsonify({"session_id": session_id, "screenshots": screenshots_data}), 200

    except Exception as exc:
        logger.error(f"Error getting alive screenshots for session {session_id}: {exc}", exc_info=True)
        return jsonify({"error": "Internal Server Error"}), 500


def get_proxy_status_response(*, get_udi_manager: Callable[[], Any]):
    """Get current proxy status."""
    try:
        udi = get_udi_manager()
        proxy_status = udi.get_proxy_status()
        return jsonify(proxy_status), 200
    except Exception as exc:
        logger.error(f"Error getting proxy status: {exc}", exc_info=True)
        return jsonify({"error": "Internal Server Error"}), 500


def get_playing_streams_response(*, get_udi_manager: Callable[[], Any]):
    """Get list of stream IDs that are currently being played."""
    try:
        udi = get_udi_manager()
        playing_stream_ids = udi.get_playing_stream_ids()

        return jsonify({"playing_stream_ids": list(playing_stream_ids), "count": len(playing_stream_ids)}), 200
    except Exception as exc:
        logger.error(f"Error getting playing streams: {exc}", exc_info=True)
        return jsonify({"error": "Internal Server Error"}), 500


def get_stream_viewer_url_response(*, stream_id: int, get_udi_manager: Callable[[], Any]):
    """Get stream direct URL for live browser viewing."""
    try:
        udi = get_udi_manager()
        stream = udi.get_stream_by_id(stream_id)

        if not stream:
            return jsonify({"success": False, "error": f"Stream {stream_id} not found"}), 404

        stream_url = stream.get("url")

        return jsonify(
            {
                "success": True,
                "stream_url": stream_url,
                "stream_id": stream_id,
                "stream_name": stream.get("name", "Unknown"),
            }
        )
    except Exception as exc:
        logger.error(f"Error getting stream viewer URL: {exc}", exc_info=True)
        return jsonify({"error": "Internal Server Error"}), 500


def stream_proxy_url_response(*, stream_id: int, udp_proxy_manager: Any):
    """Proxy local UDP stream via HTTP using shared listener."""
    proxy = udp_proxy_manager.get_proxy(stream_id)
    client_queue = proxy.add_client()

    def generate():
        try:
            while True:
                try:
                    data = client_queue.get(timeout=10.0)
                    yield data
                except queue.Empty:
                    if not proxy.running:
                        logger.warning(f"Shared proxy for {stream_id} stopped, closing HTTP client")
                        break
                    continue
        except Exception as exc:
            logger.debug(f"HTTP streaming client for {stream_id} disconnected: {exc}")
        finally:
            proxy.remove_client(client_queue)

    return Response(generate(), mimetype="video/mp2t")


def create_session_from_event_response(*, event_id: str, get_scheduling_service: Callable[[], Any]):
    """Create a monitoring session from a scheduled event."""
    try:
        scheduling_service = get_scheduling_service()
        session_id = scheduling_service.create_session_from_event(event_id)

        if not session_id:
            return jsonify({"error": "Failed to create session from event"}), 400

        return jsonify({"session_id": session_id, "message": "Session created successfully from event"}), 201

    except Exception as exc:
        logger.error(f"Error creating session from event {event_id}: {exc}", exc_info=True)
        return jsonify({"error": "Internal Server Error"}), 500


def handle_session_settings_response(
    *,
    method: str,
    payload: Optional[Dict[str, Any]],
    get_session_manager: Callable[[], Any],
):
    """Get or update session settings."""
    try:
        session_manager = get_session_manager()

        if method == "GET":
            return jsonify(
                {
                    "review_duration": session_manager.get_review_duration(),
                    "loop_review_duration": session_manager.get_loop_review_duration(),
                }
            )

        if method == "POST":
            data = SessionSettingsUpdateSchema.from_payload(payload or {})

            updated = {}
            if data.review_duration is not None:
                session_manager.set_review_duration(data.review_duration)
                updated["review_duration"] = data.review_duration

            if data.loop_review_duration is not None:
                session_manager.set_loop_review_duration(data.loop_review_duration)
                updated["loop_review_duration"] = data.loop_review_duration

            return jsonify({"message": "Settings updated", **updated})

        return error_response("Method not allowed", status_code=405, code="method_not_allowed")

    except ValidationError as exc:
        return error_response(
            exc.message,
            status_code=exc.status_code,
            code=exc.error_code,
            details=exc.details,
        )

    except Exception as exc:
        logger.error(f"Error handling session settings: {exc}", exc_info=True)
        return error_response("Internal Server Error", status_code=500, code="internal_error")
