"""Compatibility alias for legacy imports expecting top-level automated_stream_manager module."""

import sys

from apps.automation import automated_stream_manager as _impl

sys.modules[__name__] = _impl
