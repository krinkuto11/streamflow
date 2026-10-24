"""Bounded Dispatcharr EPG search, independent of retained guide history."""

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit, urlunsplit

MAX_EPG_SNAPSHOT_PAGES = 500


class EpgSnapshotIncompleteError(RuntimeError):
    def __init__(self):
        super().__init__(
            "The EPG snapshot is incomplete. Auto-Create did not use the partial guide. "
            "Retry the EPG refresh and check its snapshot status."
        )


@dataclass
class EpgProgramSnapshot:
    programs: list = field(default_factory=list)
    pages: int = 0
    complete: bool = False
    reason: str = "fetch_failed"
    start: str = ""
    end: str = ""

    def status(self):
        return {
            "complete": self.complete, "reason": self.reason,
            "pages": self.pages, "program_count": len(self.programs),
            "end_after": self.start, "start_before": self.end,
        }


def fetch_program_window(base_url, fetch, *, hours_ahead=24, now=None, max_pages=None):
    """Use /search/ (the list endpoint ignores time filters), never scan history.

    Keep the initial window on subsequent pages and reject cross-origin or
    unexpected next links before the authenticated fetch follows them.
    """
    now = now or datetime.now(timezone.utc)
    hours_ahead = max(1, min(24 * 365, float(hours_ahead)))
    result = EpgProgramSnapshot(
        start=now.isoformat(), end=(now + timedelta(hours=hours_ahead)).isoformat(),
    )
    endpoint = urlsplit(f"{base_url.rstrip('/')}/api/epg/programs/search/")
    filters = {"end_after": result.start, "start_before": result.end, "page_size": "500"}
    next_url = urlunsplit(endpoint._replace(query=urlencode(filters)))
    seen = set()
    limit = MAX_EPG_SNAPSHOT_PAGES if max_pages is None else max_pages
    while next_url:
        parsed = urlsplit(next_url)
        if (parsed.scheme, parsed.netloc, parsed.path) != (endpoint.scheme, endpoint.netloc, endpoint.path):
            result.reason = "unsafe_next_link"
            return result
        params = parse_qs(parsed.query)
        params.update({key: [value] for key, value in filters.items()})
        next_url = urlunsplit(parsed._replace(query=urlencode(params, doseq=True), fragment=""))
        if next_url in seen:
            result.reason = "pagination_loop"
            return result
        if result.pages >= limit:
            result.reason = "page_limit"
            return result
        seen.add(next_url)
        data = fetch(next_url)
        if data is None:
            return result
        rows = data if isinstance(data, list) else data.get("results") if isinstance(data, dict) else None
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            result.reason = "invalid_page"
            return result
        result.programs.extend(rows)
        result.pages += 1
        next_value = data.get("next") if isinstance(data, dict) else None
        if next_value is not None and not isinstance(next_value, str):
            result.reason = "invalid_next_link"
            return result
        next_url = urljoin(next_url, next_value) if next_value else None
    result.complete = True
    result.reason = "ok"
    return result
