"""Compatibility alias for legacy imports expecting top-level stream_check_utils module."""

import sys

from apps.stream import stream_check_utils as _impl

sys.modules[__name__] = _impl
