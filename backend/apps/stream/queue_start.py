"""Shared queue start ordering helpers for stream quality checks."""

from typing import Any, Dict, List, Optional, Tuple


QUEUE_START_MODES = {"first", "last", "channel"}


def coerce_channel_id(value: Any) -> Optional[int]:
    """Return an integer channel ID when the payload value is usable."""
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _channel_display_name(channel: Dict[str, Any]) -> str:
    return str(channel.get("name") or f"Channel {channel.get('id')}")


def order_channels_for_queue_start(
    channels: List[Dict[str, Any]],
    *,
    start_mode: str = "first",
    start_channel_id: Any = None,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Order channels so the requested full-check start point is first."""
    usable_channels = [
        channel for channel in channels
        if isinstance(channel, dict) and channel.get("id") is not None
    ]
    mode = (start_mode or "first").strip().lower()
    if mode not in QUEUE_START_MODES:
        raise ValueError("Invalid start_mode")

    if not usable_channels:
        return [], {"mode": mode}

    selected_id = coerce_channel_id(start_channel_id)
    if mode == "first":
        ordered = usable_channels
    elif mode == "last":
        ordered = list(reversed(usable_channels))
    else:
        if selected_id is None:
            raise ValueError("start_channel_id is required when start_mode is channel")
        selected_index = next(
            (idx for idx, channel in enumerate(usable_channels) if int(channel["id"]) == selected_id),
            None,
        )
        if selected_index is None:
            raise ValueError("Selected start channel was not found")
        ordered = usable_channels[selected_index:] + usable_channels[:selected_index]

    first_channel = ordered[0]
    meta = {
        "mode": mode,
        "start_channel_id": first_channel.get("id"),
        "start_channel_name": _channel_display_name(first_channel),
    }
    if mode == "channel":
        meta["requested_channel_id"] = selected_id
    return ordered, meta
