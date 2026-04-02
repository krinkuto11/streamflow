"""AceStream API handler functions extracted from web_api."""

import os
from typing import Any, Callable, Dict, Optional

import requests
from flask import jsonify

from apps.core.logging_config import setup_logging
from apps.stream.acestream_session_service import stop_ace_loop_detector

logger = setup_logging(__name__)


def _requests_error_response(exc: requests.RequestException, *, message: str):
    status_code = getattr(getattr(exc, "response", None), "status_code", 502)
    detail = None
    if getattr(exc, "response", None) is not None:
        try:
            detail = exc.response.json()
        except Exception:
            detail = exc.response.text
    return jsonify({"error": message, "detail": detail}), status_code


def get_acestream_orchestrator_config_response(*, get_acestream_orchestrator_config: Callable[[], Any]):
    """Get AceStream orchestrator configuration without exposing sensitive fields."""
    try:
        cfg = get_acestream_orchestrator_config().get_config()
        return jsonify(cfg), 200
    except Exception as exc:
        logger.error(f"Error getting AceStream orchestrator config: {exc}")
        return jsonify({"error": "Internal Server Error"}), 500


def update_acestream_orchestrator_config_response(
    *,
    payload: Optional[Dict[str, Any]],
    get_acestream_orchestrator_config: Callable[[], Any],
):
    """Update AceStream orchestrator host, port, and API key settings."""
    try:
        data = payload or {}
        if not isinstance(data, dict):
            return jsonify({"error": "No configuration data provided"}), 400

        host = data.get("host")
        port = data.get("port")
        api_key = data.get("api_key")

        cfg = get_acestream_orchestrator_config()
        success = cfg.update_config(host=host, port=port, api_key=api_key)
        if not success:
            return jsonify({"error": "Failed to save configuration"}), 500

        base_url = cfg.get_base_url() or ""
        if base_url:
            os.environ["ACESTREAM_ORCHESTRATOR_BASE_URL"] = base_url
            os.environ["ORCHESTRATOR_BASE_URL"] = base_url
        if api_key is not None:
            os.environ["ACESTREAM_ORCHESTRATOR_API_KEY"] = str(api_key)
            os.environ["ORCHESTRATOR_API_KEY"] = str(api_key)

        return jsonify({"message": "AceStream orchestrator configuration updated successfully"}), 200
    except Exception as exc:
        logger.error(f"Error updating AceStream orchestrator config: {exc}")
        return jsonify({"error": "Internal Server Error"}), 500


def check_acestream_orchestrator_ready_response(
    *,
    make_client: Callable[[], Any],
    ping_orchestrator_ready: Callable[[Any], Any],
):
    """Check if AceStream orchestrator is configured and reachable."""
    try:
        client = make_client()
        if not client.is_configured():
            return (
                jsonify(
                    {
                        "ready": False,
                        "error": "AceStream orchestrator is not configured",
                    }
                ),
                200,
            )

        ready, detail = ping_orchestrator_ready(client)
        return jsonify({"ready": ready, "detail": detail}), 200
    except Exception as exc:
        logger.error(f"Error checking AceStream orchestrator readiness: {exc}")
        return jsonify({"ready": False, "error": "Internal Server Error"}), 500


def start_acestream_monitor_session_response(
    *,
    payload: Optional[Dict[str, Any]],
    get_client_or_error: Callable[[], Any],
    normalize_content_id: Callable[[Any], Any],
):
    """Start AceStream monitoring session via orchestrator contract."""
    client, error_payload = get_client_or_error()
    if error_payload:
        return error_payload

    data = payload or {}
    normalized = normalize_content_id(data.get("content_id"))
    if not normalized:
        return jsonify({"error": "content_id is required and must contain a valid 40-hex AceStream ID"}), 400

    data["content_id"] = normalized
    try:
        response_data = client.start_session(data)
        return jsonify(response_data), 200
    except requests.RequestException as exc:
        return _requests_error_response(exc, message="Failed to start AceStream monitoring session")


def list_acestream_monitor_sessions_response(
    *,
    args: Any,
    get_client_or_error: Callable[[], Any],
):
    """List AceStream monitoring sessions with optional playback correlation."""
    client, error_payload = get_client_or_error()
    if error_payload:
        return error_payload

    try:
        sessions_data = client.list_sessions()
        include_correlation = args.get("include_correlation", "true").lower() != "false"
        if include_correlation:
            started_streams = client.list_started_streams()
            sessions_data["items"] = client.annotate_many_with_playback(sessions_data.get("items", []), started_streams)
        return jsonify(sessions_data), 200
    except requests.RequestException as exc:
        return _requests_error_response(exc, message="Failed to list AceStream monitoring sessions")


def get_acestream_monitor_session_response(
    *,
    monitor_id: str,
    args: Any,
    get_client_or_error: Callable[[], Any],
):
    """Get one AceStream monitoring session with detailed history."""
    client, error_payload = get_client_or_error()
    if error_payload:
        return error_payload

    try:
        session_data = client.get_session(monitor_id)
        include_correlation = args.get("include_correlation", "true").lower() != "false"
        if include_correlation:
            started_streams = client.list_started_streams()
            session_data = client.annotate_with_playback(session_data, started_streams)
        return jsonify(session_data), 200
    except requests.RequestException as exc:
        return _requests_error_response(exc, message="Failed to get AceStream monitoring session")


def stop_acestream_monitor_session_response(*, monitor_id: str, get_client_or_error: Callable[[], Any]):
    """Stop AceStream monitoring session lifecycle."""
    client, error_payload = get_client_or_error()
    if error_payload:
        return error_payload

    try:
        response_data = client.stop_session(monitor_id)
        return jsonify(response_data if response_data is not None else {"ok": True}), 200
    except requests.RequestException as exc:
        return _requests_error_response(exc, message="Failed to stop AceStream monitoring session")


def delete_acestream_monitor_entry_response(*, monitor_id: str, get_client_or_error: Callable[[], Any]):
    """Delete AceStream monitoring entry and ensure it is stopped."""
    client, error_payload = get_client_or_error()
    if error_payload:
        return error_payload

    stop_ace_loop_detector(monitor_id)

    try:
        response_data = client.delete_entry(monitor_id)
        return jsonify(response_data if response_data is not None else {"ok": True}), 200
    except requests.RequestException as exc:
        return _requests_error_response(exc, message="Failed to delete AceStream monitoring entry")


def parse_acestream_m3u_response(
    *,
    payload: Optional[Dict[str, Any]],
    get_client_or_error: Callable[[], Any],
    parse_m3u_fallback: Callable[[str], Any],
    normalize_content_id: Callable[[Any], Any],
):
    """Parse M3U and extract AceStream IDs and names."""
    client, error_payload = get_client_or_error()
    if error_payload:
        return error_payload

    data = payload or {}
    m3u_content = data.get("m3u_content", "")
    if not isinstance(m3u_content, str):
        return jsonify({"error": "m3u_content must be a string"}), 400

    try:
        response_data = client.parse_m3u(m3u_content)
        parsed_items = response_data.get("items", []) if isinstance(response_data, dict) else []

        fallback_items = parse_m3u_fallback(m3u_content)
        merged = {}
        for item in parsed_items:
            cid = normalize_content_id(item.get("content_id"))
            if not cid:
                continue
            merged[cid] = {
                "content_id": cid,
                "name": item.get("name"),
                "line_number": str(item.get("line_number")) if item.get("line_number") is not None else None,
            }

        for item in fallback_items:
            cid = item["content_id"]
            if cid not in merged:
                merged[cid] = item
            elif not merged[cid].get("name") and item.get("name"):
                merged[cid]["name"] = item["name"]

        return jsonify({"count": len(merged), "items": list(merged.values())}), 200
    except requests.RequestException as exc:
        return _requests_error_response(exc, message="Failed to parse M3U for AceStream entries")


def list_acestream_started_streams_response(*, get_client_or_error: Callable[[], Any]):
    """List currently started streams from orchestrator proxy endpoint."""
    client, error_payload = get_client_or_error()
    if error_payload:
        return error_payload

    try:
        response_data = client.list_started_streams()
        return jsonify(response_data), 200
    except requests.RequestException as exc:
        return _requests_error_response(exc, message="Failed to list started streams")


def list_acestream_channel_sessions_response(
    *,
    args: Any,
    get_client_or_error: Callable[[], Any],
    load_store: Callable[[], Any],
    get_management_settings: Callable[[], Any],
    save_store: Callable[[Any], Any],
    annotate_playback: Callable[[Any, Dict[str, Any]], None],
    evaluate_management: Callable[[Any, Dict[str, Any], Any], bool],
    save_telemetry_snapshot: Callable[[Any, Dict[str, Any]], bool],
    check_epg_auto_stop: Callable[[Any, Any], bool],
    refresh_session_streams: Callable[[Any, Any], bool],
    apply_dispatcharr_sync: Callable[[Any], bool],
    build_summary: Callable[[Any, Dict[str, Any]], Dict[str, Any]],
):
    """List channel-scoped AceStream monitoring sessions."""
    client, error_payload = get_client_or_error()
    if error_payload:
        return error_payload

    try:
        status_filter = (args.get("status") or "").lower()
        store = load_store()
        mgmt_settings = get_management_settings()
        monitor_payload = client.list_sessions()
        monitor_items = monitor_payload.get("items", []) if isinstance(monitor_payload, dict) else []
        monitors_by_id = {
            item.get("monitor_id"): item
            for item in monitor_items
            if isinstance(item, dict) and item.get("monitor_id")
        }
        annotate_playback(client, monitors_by_id)

        sessions = []
        store_changed = False
        for session_id, raw in store.items():
            if not isinstance(raw, dict):
                continue
            raw["session_id"] = session_id
            if evaluate_management(raw, monitors_by_id, mgmt_settings):
                store_changed = True
            if save_telemetry_snapshot(raw, monitors_by_id):
                store_changed = True
            if check_epg_auto_stop(raw, client):
                store_changed = True
            if not raw.get("epg_auto_stopped"):
                if refresh_session_streams(raw, client):
                    store_changed = True
                    evaluate_management(raw, monitors_by_id, mgmt_settings)
            apply_dispatcharr_sync(raw)
            summary = build_summary(raw, monitors_by_id)
            if status_filter == "active" and not summary.get("is_active"):
                continue
            sessions.append(summary)

        if store_changed:
            save_store(store)

        sessions.sort(key=lambda s: s.get("created_at") or 0, reverse=True)
        return jsonify(sessions), 200
    except requests.RequestException as exc:
        return _requests_error_response(exc, message="Failed to list AceStream channel sessions")
    except Exception as exc:
        logger.error(f"Error listing AceStream channel sessions: {exc}", exc_info=True)
        return jsonify({"error": "Internal Server Error"}), 500


def create_acestream_channel_session_response(
    *,
    payload: Optional[Dict[str, Any]],
    create_session_impl: Callable[..., Any],
):
    """Create and start AceStream monitoring for all AceStream streams in a channel."""
    data = payload or {}
    channel_id = data.get("channel_id")
    if not channel_id:
        return jsonify({"error": "channel_id is required"}), 400

    result, status_code = create_session_impl(
        channel_id=channel_id,
        interval_s=data.get("interval_s", 1.0),
        run_seconds=data.get("run_seconds", 0),
        per_sample_timeout_s=data.get("per_sample_timeout_s", 1.0),
        engine_container_id=data.get("engine_container_id"),
        epg_event_title=data.get("epg_event_title"),
        epg_event_description=data.get("epg_event_description"),
        epg_event_start=data.get("epg_event_start"),
        epg_event_end=data.get("epg_event_end"),
        epg_event_id=data.get("epg_event_id"),
    )
    return jsonify(result), status_code


def create_acestream_group_sessions_response(
    *,
    payload: Optional[Dict[str, Any]],
    get_client_or_error: Callable[[], Any],
    ping_orchestrator_ready: Callable[[Any], Any],
    get_udi_manager: Callable[[], Any],
    create_session_impl: Callable[..., Any],
):
    """Create AceStream channel sessions for all channels in a group."""
    client, error_payload = get_client_or_error()
    if error_payload:
        return error_payload

    ready, ready_detail = ping_orchestrator_ready(client)
    if not ready:
        return jsonify({"error": "AceStream orchestrator is not ready", "detail": ready_detail}), 503

    try:
        data = payload or {}
        group_id = data.get("group_id")
        if not group_id:
            return jsonify({"error": "group_id is required"}), 400

        udi = get_udi_manager()
        channels = udi.get_channels_by_group(int(group_id)) or []
        if not channels:
            return jsonify({"error": "Group not found or has no channels"}), 404

        created_sessions = []
        errors = []
        for channel in channels:
            result, status_code = create_session_impl(
                channel_id=channel.get("id"),
                interval_s=data.get("interval_s", 1.0),
                run_seconds=data.get("run_seconds", 0),
                per_sample_timeout_s=data.get("per_sample_timeout_s", 1.0),
                engine_container_id=data.get("engine_container_id"),
                epg_event_title=data.get("epg_event_title"),
                epg_event_description=data.get("epg_event_description"),
                epg_event_start=data.get("epg_event_start"),
                epg_event_end=data.get("epg_event_end"),
                epg_event_id=data.get("epg_event_id"),
            )

            if 200 <= status_code < 300:
                created_sessions.append(result)
            else:
                msg = result.get("error") if isinstance(result, dict) else "Unknown error"
                errors.append(f"Channel {channel.get('name')} ({channel.get('id')}): {msg}")

        return (
            jsonify(
                {
                    "message": f"Started {len(created_sessions)} AceStream channel sessions from group {group_id}",
                    "sessions": created_sessions,
                    "errors": errors,
                }
            ),
            200 if created_sessions else 400,
        )
    except Exception as exc:
        logger.error(f"Error creating AceStream group sessions: {exc}", exc_info=True)
        return jsonify({"error": "Internal Server Error"}), 500


def get_acestream_channel_session_response(
    *,
    session_id: str,
    get_client_or_error: Callable[[], Any],
    load_store: Callable[[], Any],
    get_management_settings: Callable[[], Any],
    save_store: Callable[[Any], Any],
    annotate_playback: Callable[[Any, Dict[str, Any]], None],
    evaluate_management: Callable[[Any, Dict[str, Any], Any], bool],
    save_telemetry_snapshot: Callable[[Any, Dict[str, Any]], bool],
    check_epg_auto_stop: Callable[[Any, Any], bool],
    apply_dispatcharr_sync: Callable[[Any], bool],
    build_summary: Callable[[Any, Dict[str, Any]], Dict[str, Any]],
    compact_monitor_payload: Callable[[Any], Any],
):
    """Get detailed channel-scoped AceStream monitoring session."""
    client, error_payload = get_client_or_error()
    if error_payload:
        return error_payload

    try:
        store = load_store()
        raw = store.get(session_id)
        if not isinstance(raw, dict):
            return jsonify({"error": "Session not found"}), 404

        mgmt_settings = get_management_settings()
        monitor_payload = client.list_sessions()
        monitor_items = monitor_payload.get("items", []) if isinstance(monitor_payload, dict) else []
        monitors_by_id = {
            item.get("monitor_id"): item
            for item in monitor_items
            if isinstance(item, dict) and item.get("monitor_id")
        }
        annotate_playback(client, monitors_by_id)

        store_changed = False
        if evaluate_management(raw, monitors_by_id, mgmt_settings):
            store_changed = True
        if save_telemetry_snapshot(raw, monitors_by_id):
            store_changed = True
        if check_epg_auto_stop(raw, client):
            store_changed = True
        apply_dispatcharr_sync(raw)
        if store_changed:
            store[session_id] = raw
            save_store(store)

        summary = build_summary(raw, monitors_by_id)
        entries = []
        for entry in raw.get("entries", []):
            monitor = monitors_by_id.get(entry.get("monitor_id"))
            if monitor is None and entry.get("monitor_id"):
                try:
                    monitor = client.get_session(entry["monitor_id"])
                except Exception:
                    monitor = None
            entries.append({**entry, "monitor": compact_monitor_payload(monitor)})

        detail = {
            **summary,
            "entries": entries,
            "channel_id": raw.get("channel_id"),
            "channel_name": raw.get("channel_name"),
            "created_at": raw.get("created_at"),
        }
        return jsonify(detail), 200
    except requests.RequestException as exc:
        return _requests_error_response(exc, message="Failed to get AceStream channel session")
    except Exception as exc:
        logger.error(f"Error getting AceStream channel session {session_id}: {exc}", exc_info=True)
        return jsonify({"error": "Internal Server Error"}), 500


def stop_acestream_channel_session_response(
    *,
    session_id: str,
    get_client_or_error: Callable[[], Any],
    load_store: Callable[[], Any],
):
    """Stop all orchestrator monitor sessions attached to a channel session."""
    client, error_payload = get_client_or_error()
    if error_payload:
        return error_payload

    try:
        store = load_store()
        raw = store.get(session_id)
        if not isinstance(raw, dict):
            return jsonify({"error": "Session not found"}), 404

        for entry in raw.get("entries", []):
            monitor_id = entry.get("monitor_id")
            if not monitor_id:
                continue
            
            stop_ace_loop_detector(monitor_id)
            
            try:
                client.stop_session(monitor_id)
            except Exception:
                pass

        return jsonify({"message": "AceStream channel session stopped"}), 200
    except Exception as exc:
        logger.error(f"Error stopping AceStream channel session {session_id}: {exc}", exc_info=True)
        return jsonify({"error": "Internal Server Error"}), 500


def delete_acestream_channel_session_response(
    *,
    session_id: str,
    get_client_or_error: Callable[[], Any],
    load_store: Callable[[], Any],
    save_store: Callable[[Any], Any],
):
    """Delete channel session and all orchestrator monitor entries."""
    client, error_payload = get_client_or_error()
    if error_payload:
        return error_payload

    try:
        store = load_store()
        raw = store.get(session_id)
        if not isinstance(raw, dict):
            return jsonify({"error": "Session not found"}), 404

        for entry in raw.get("entries", []):
            monitor_id = entry.get("monitor_id")
            if not monitor_id:
                continue
            try:
                client.delete_entry(monitor_id)
            except Exception:
                try:
                    client.stop_session(monitor_id)
                except Exception:
                    pass

        del store[session_id]
        save_store(store)
        return jsonify({"message": "AceStream channel session deleted"}), 200
    except Exception as exc:
        logger.error(f"Error deleting AceStream channel session {session_id}: {exc}", exc_info=True)
        return jsonify({"error": "Internal Server Error"}), 500


def quarantine_acestream_channel_stream_response(
    *,
    session_id: str,
    stream_id: int,
    load_store: Callable[[], Any],
    save_store: Callable[[Any], Any],
    now_ts: float,
):
    """Manually quarantine one Ace stream entry within a channel session."""
    try:
        store = load_store()
        raw = store.get(session_id)
        if not isinstance(raw, dict):
            return jsonify({"error": "Session not found"}), 404

        entries = raw.get("entries") or []
        target = None
        for entry in entries:
            if int(entry.get("stream_id") or -1) == int(stream_id):
                target = entry
                break

        if not isinstance(target, dict):
            return jsonify({"error": "Stream not found in session"}), 404

        target["manual_quarantine"] = True
        target["management_state"] = "quarantined"
        target["management_reason"] = "manual"
        target["management_since"] = now_ts

        save_store(store)
        return jsonify({"message": "Ace stream quarantined"}), 200
    except Exception as exc:
        logger.error(
            f"Error quarantining Ace stream {stream_id} in session {session_id}: {exc}",
            exc_info=True,
        )
        return jsonify({"error": "Internal Server Error"}), 500


def revive_acestream_channel_stream_response(
    *,
    session_id: str,
    stream_id: int,
    load_store: Callable[[], Any],
    save_store: Callable[[Any], Any],
    now_ts: float,
):
    """Revive one manually quarantined Ace stream entry back to review."""
    try:
        store = load_store()
        raw = store.get(session_id)
        if not isinstance(raw, dict):
            return jsonify({"error": "Session not found"}), 404

        entries = raw.get("entries") or []
        target = None
        for entry in entries:
            if int(entry.get("stream_id") or -1) == int(stream_id):
                target = entry
                break

        if not isinstance(target, dict):
            return jsonify({"error": "Stream not found in session"}), 404

        target["manual_quarantine"] = False
        target["management_state"] = "review"
        target["management_reason"] = "manual-revive"
        target["management_since"] = now_ts

        save_store(store)
        return jsonify({"message": "Ace stream moved to review"}), 200
    except Exception as exc:
        logger.error(
            f"Error reviving Ace stream {stream_id} in session {session_id}: {exc}",
            exc_info=True,
        )
        return jsonify({"error": "Internal Server Error"}), 500
