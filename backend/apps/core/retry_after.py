"""Server-directed retry delays for Dispatcharr control-plane requests."""

from datetime import timezone
from email.utils import parsedate_to_datetime
import math
import re
import time
from typing import Optional


_THROTTLE_DETAIL = re.compile(r"expected\s+available\s+in\s+(\d+(?:\.\d+)?)\s+seconds?", re.IGNORECASE)


def retry_after_seconds(response, *, now: Optional[float] = None) -> Optional[float]:
    """Return a conservative delay from Retry-After or DRF's throttle detail.

    None means no usable server hint. Zero is a valid hint. Do not cap a valid
    server-directed wait below the requested interval.
    """
    delays = []
    headers = getattr(response, 'headers', {}) or {}
    hint = headers.get('Retry-After') or headers.get('retry-after')
    if isinstance(hint, str):
        hint = hint.strip()
        if re.fullmatch(r'\d+(?:\.\d+)?', hint):
            delay = float(hint)
            if math.isfinite(delay):
                delays.append(delay)
        else:
            try:
                date = parsedate_to_datetime(hint)
                if date.tzinfo is None:
                    date = date.replace(tzinfo=timezone.utc)
                delay = max(0.0, date.timestamp() - (time.time() if now is None else now))
                if math.isfinite(delay):
                    delays.append(delay)
            except (TypeError, ValueError, OverflowError):
                pass

    try:
        payload = response.json()
    except (ValueError, TypeError, AttributeError):
        payload = None
    detail = payload.get('detail') if isinstance(payload, dict) else None
    if isinstance(detail, str):
        match = _THROTTLE_DETAIL.search(detail)
        if match:
            delay = float(match.group(1))
            if math.isfinite(delay):
                delays.append(delay)
    return float(math.ceil(max(delays))) if delays else None
