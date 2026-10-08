"""Reusable, thread-owned control-plane HTTP connections.

No retry adapter is installed here: callers decide whether an operation can
be retried. Authentication remains per request so rotated credentials cannot
leak into another connector or an existing session's default headers.
"""

import threading
from urllib.parse import urlsplit

import requests
from requests.adapters import HTTPAdapter
from apps.core.operation_timing import STREAM_OPERATION_TIMINGS

_local = threading.local()


def get_session(url: str) -> requests.Session:
    origin = urlsplit(url)
    key = (origin.scheme, origin.netloc)
    sessions = getattr(_local, "sessions", None)
    if sessions is None:
        sessions = _local.sessions = {}
    if key not in sessions:
        # Bound idle origins in a long-lived worker after connector edits.
        if len(sessions) >= 8:
            sessions.pop(next(iter(sessions))).close()
        session = requests.Session()
        adapter = HTTPAdapter(pool_connections=4, pool_maxsize=4, max_retries=0)
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        sessions[key] = session
    sessions[key].cookies.clear()
    return sessions[key]


def close_thread_sessions() -> None:
    for session in getattr(_local, "sessions", {}).values():
        session.close()
    _local.sessions = {}


def get(url: str, **kwargs):
    with STREAM_OPERATION_TIMINGS.measure("api_read"):
        return get_session(url).get(url, **kwargs)


def post(url: str, **kwargs):
    return get_session(url).post(url, **kwargs)


def patch(url: str, **kwargs):
    return get_session(url).patch(url, **kwargs)


def delete(url: str, **kwargs):
    return get_session(url).delete(url, **kwargs)
