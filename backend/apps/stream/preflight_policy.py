"""Pure timing/expiry policy shared by scans and queued preflight execution."""

from datetime import datetime, timezone


def parse_time(value):
    if isinstance(value, datetime):
        result = value
    else:
        try:
            result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
    return result.replace(tzinfo=timezone.utc) if result.tzinfo is None else result.astimezone(timezone.utc)


def latest_due_bucket(seconds, offsets, direction):
    """Catch up to the newest crossed checkpoint, without replaying old ones."""
    offsets = sorted({int(value) for value in offsets if int(value) > 0})
    due = [value for value in offsets if (
        seconds <= value * 60 if direction == "pre" else seconds >= value * 60
    )]
    if not due:
        return None
    offset = min(due) if direction == "pre" else max(due)
    return f"{offset}m" if direction == "pre" else f"post+{offset}m"


def preflight_deadline(event, config):
    if event.get("trigger_bucket") == "manual":
        return None
    event_at = parse_time(event.get("event_date"))
    if event_at is None:
        return None
    grace = float(config.get("post_start_grace_minutes", 5)) * 60
    checkpoint_offsets = {-int(config.get("preflight_offset_minutes", 20)) * 60}
    checkpoint_offsets.update(-int(value) * 60 for value in config.get("retry_offsets_minutes", [])
                              if 0 < int(value) <= int(config.get("preflight_offset_minutes", 20)))
    checkpoint_offsets.update(int(value) * 60 for value in config.get("post_start_offsets_minutes", [])
                              if 0 < int(value) * 60 <= grace)
    bucket = str(event.get("trigger_bucket") or "")
    try:
        current_offset = int(bucket[5:-1]) * 60 if bucket.startswith("post+") else -int(bucket[:-1]) * 60
    except (TypeError, ValueError):
        return event_at.timestamp() + grace
    next_offsets = [value for value in checkpoint_offsets if value > current_offset]
    return event_at.timestamp() + min(next_offsets + [grace])
