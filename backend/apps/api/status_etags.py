"""Conditional GETs for StreamFlow's existing public status responses.

The digest covers the complete serialized response, including changing queue
state and time-sensitive fields. It does not skip service refreshes, and cannot
incorrectly label a newly computed status as unchanged.
"""

import hashlib
import re

from flask import request

_STATUS_PATH = re.compile(
    r"^/api/(?:automation/status|stream-checker/(?:status|progress)|"
    r"teamarr-preflight/status|shadow-blank-monitor/status|"
    r"stream-sessions(?:/[^/]+)?|viewer-activity/status|job-arbiter/status|"
    r"scheduling/(?:processor|epg-refresh|udi-refresh)/status|"
    r"dispatcharr/initialization-status|udi/(?:status|stats))$"
)


def install_status_etags(app):
    @app.after_request
    def conditional_status(response):
        if (
            request.method not in {"GET", "HEAD"}
            or not _STATUS_PATH.fullmatch(request.path)
            or response.status_code != 200
            or not response.is_json
            or response.is_streamed
        ):
            return response
        digest = hashlib.sha256(response.get_data()).hexdigest()
        response.set_etag(digest)
        response.headers["Cache-Control"] = "private, no-cache"
        response.vary.add("Cookie")
        response.vary.add("Authorization")
        if request.if_none_match.contains_weak(digest):
            response.status_code = 304
            response.set_data(b"")
        return response
