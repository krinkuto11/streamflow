"""Compatibility alias for legacy imports expecting top-level stream_session_manager."""

import sys

from apps.stream import stream_session_manager as _impl

sys.modules[__name__] = _impl
