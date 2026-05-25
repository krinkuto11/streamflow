"""Small helpers for URL/name-safe operational logging."""

import hashlib
import re
from typing import Any, Optional


_URL_RE = re.compile(r"(?i)\b(?:https?|rtmps?|rtsp|rtp|udp|tcp|acestream)://[^\s'\"<>]+")


def audit_ref(kind: str, value: Any) -> str:
    """Return a stable, non-reversible reference for a logged object."""
    if value is None or value == "":
        return f"{kind}-unknown"
    digest = hashlib.sha256(f"{kind}:{value}".encode("utf-8")).hexdigest()[:12]
    return f"{kind}-{digest}"


def channel_ref(channel_id: Any) -> str:
    return audit_ref("channel", channel_id)


def stream_ref(stream_id: Any = None, stream_url: Optional[str] = None) -> str:
    if stream_id is not None and stream_id != "":
        return audit_ref("stream", stream_id)
    if stream_url:
        return audit_ref("stream-url", stream_url)
    return "stream-unknown"


def url_ref(url: Optional[str]) -> str:
    return audit_ref("url", url)


def scrub_urls(message: Any) -> str:
    """Replace raw stream/API URLs inside a log message with stable URL refs."""
    text = str(message)

    def _replace(match: re.Match) -> str:
        return f"<{url_ref(match.group(0))}>"

    return _URL_RE.sub(_replace, text)


def stream_context(
    *,
    stream_id: Any = None,
    stream_url: Optional[str] = None,
    channel_id: Any = None,
    reason: Optional[str] = None,
) -> str:
    parts = [f"stream_ref={stream_ref(stream_id, stream_url)}"]
    if channel_id is not None:
        parts.append(f"channel_ref={channel_ref(channel_id)}")
    if reason:
        parts.append(f"reason={reason}")
    return ", ".join(parts)
