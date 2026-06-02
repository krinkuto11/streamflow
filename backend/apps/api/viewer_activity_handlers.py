"""API helpers for current viewer activity."""

from __future__ import annotations

from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from flask import jsonify

from apps.core.logging_config import setup_logging

logger = setup_logging(__name__)


def _client_text(client: Any) -> str:
    if isinstance(client, dict):
        values = [
            client.get("user_agent"),
            client.get("client_id"),
            client.get("username"),
            client.get("user"),
            client.get("ip"),
        ]
        return " ".join(str(value) for value in values if value is not None)
    return str(client)


def _clients_from_status(status: Dict[str, Any]) -> Optional[List[Any]]:
    clients = status.get("clients")
    if isinstance(clients, dict):
        return list(clients.values())
    if isinstance(clients, list):
        return clients
    return None


def _is_status_active(status: Dict[str, Any]) -> bool:
    if not isinstance(status, dict):
        return False
    return bool(
        status.get("state") == "active"
        or status.get("active")
        or status.get("current_stream")
        or status.get("stream_id")
        or status.get("clients")
    )


def _extract_stream_id(status: Dict[str, Any]) -> Optional[int]:
    value = status.get("stream_id") or status.get("current_stream_id")
    current = status.get("current_stream")
    if value is None and isinstance(current, dict):
        value = current.get("id") or current.get("stream_id")
    elif value is None:
        value = current
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _extract_channel_id(channel: Optional[Dict[str, Any]], status: Dict[str, Any]) -> Optional[int]:
    value = (channel or {}).get("id") or status.get("numeric_channel_id") or status.get("id")
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _fallback_client_count(status: Dict[str, Any]) -> int:
    for key in ("real_client_count", "client_count", "current_viewers", "viewer_count"):
        try:
            count = int(status.get(key))
            if count > 0:
                return count
        except (TypeError, ValueError):
            continue
    return 1 if _is_status_active(status) else 0


def _count_clients(status: Dict[str, Any], watcher_marker: str) -> Tuple[int, int, int]:
    clients = _clients_from_status(status)
    marker = watcher_marker.lower().strip()
    if clients is None:
        total = _fallback_client_count(status)
        return total, 0, total

    real_clients = 0
    watcher_clients = 0
    for client in clients:
        text = _client_text(client).lower()
        if marker and marker in text:
            watcher_clients += 1
        else:
            real_clients += 1
    return real_clients, watcher_clients, real_clients + watcher_clients


def _index_channels(channels: Iterable[Dict[str, Any]]) -> Tuple[Dict[str, Dict[str, Any]], Dict[int, Dict[str, Any]]]:
    by_uuid: Dict[str, Dict[str, Any]] = {}
    by_id: Dict[int, Dict[str, Any]] = {}
    for channel in channels or []:
        if not isinstance(channel, dict):
            continue
        if channel.get("uuid"):
            by_uuid[str(channel["uuid"])] = channel
        if channel.get("id") is not None:
            try:
                by_id[int(channel["id"])] = channel
            except (TypeError, ValueError):
                pass
    return by_uuid, by_id


def build_viewer_activity_status(
    *,
    proxy_status: Dict[str, Any],
    channels: Iterable[Dict[str, Any]],
    watcher_user_agent: str = "",
) -> Dict[str, Any]:
    """Build a UI-safe active viewer summary from Dispatcharr proxy status."""
    by_uuid, by_id = _index_channels(channels)
    active_channels: List[Dict[str, Any]] = []

    for key, raw_status in (proxy_status or {}).items():
        if not isinstance(raw_status, dict) or not _is_status_active(raw_status):
            continue

        channel_uuid = str(raw_status.get("channel_id") or raw_status.get("channel_uuid") or key)
        channel = by_uuid.get(channel_uuid)
        if channel is None and str(key).isdigit():
            channel = by_id.get(int(key))
            if channel and channel.get("uuid"):
                channel_uuid = str(channel["uuid"])

        numeric_id = _extract_channel_id(channel, raw_status)
        real_clients, watcher_clients, total_clients = _count_clients(raw_status, watcher_user_agent)
        if total_clients <= 0:
            continue

        active_channels.append({
            "channel_id": numeric_id,
            "channel_uuid": channel_uuid,
            "channel_name": (channel or {}).get("name") or raw_status.get("channel_name") or f"Channel {numeric_id or channel_uuid}",
            "stream_id": _extract_stream_id(raw_status),
            "state": raw_status.get("state") or ("active" if _is_status_active(raw_status) else "idle"),
            "real_client_count": real_clients,
            "watcher_client_count": watcher_clients,
            "total_client_count": total_clients,
            "has_real_clients": real_clients > 0,
            "watcher_only": watcher_clients > 0 and real_clients == 0,
        })

    active_channels.sort(
        key=lambda item: (
            0 if item["has_real_clients"] else 1,
            str(item.get("channel_name") or "").lower(),
        )
    )

    real_watched = [channel for channel in active_channels if channel["has_real_clients"]]
    watcher_only = [channel for channel in active_channels if channel["watcher_only"]]
    total_real_clients = sum(channel["real_client_count"] for channel in active_channels)
    total_watcher_clients = sum(channel["watcher_client_count"] for channel in active_channels)

    return {
        "active_channel_count": len(active_channels),
        "real_watched_count": len(real_watched),
        "watcher_only_count": len(watcher_only),
        "total_real_clients": total_real_clients,
        "total_watcher_clients": total_watcher_clients,
        "total_clients": total_real_clients + total_watcher_clients,
        "channels": active_channels,
    }


def get_viewer_activity_status_response(
    *,
    get_udi_manager: Callable[[], Any],
    get_shadow_monitor_service: Callable[[], Any],
):
    """Return current active viewer state for the Dashboard."""
    try:
        udi = get_udi_manager()
        shadow_monitor = get_shadow_monitor_service()
        watcher_user_agent = ""
        try:
            watcher_user_agent = (shadow_monitor.get_config() or {}).get("watcher_user_agent") or ""
        except Exception as exc:
            logger.debug(f"Could not read shadow monitor config for viewer activity: {exc}")

        status = build_viewer_activity_status(
            proxy_status=udi.get_proxy_status() if hasattr(udi, "get_proxy_status") else {},
            channels=udi.get_channels() if hasattr(udi, "get_channels") else [],
            watcher_user_agent=watcher_user_agent,
        )
        shadow_status = shadow_monitor.get_status() if hasattr(shadow_monitor, "get_status") else {}
        status["shadow_monitor_running"] = bool(shadow_status.get("running"))
        status["shadow_monitor_enabled"] = bool(shadow_status.get("enabled"))
        return jsonify(status), 200
    except Exception as exc:
        logger.error(f"Error getting viewer activity status: {exc}", exc_info=True)
        return jsonify({"error": "Internal Server Error"}), 500
