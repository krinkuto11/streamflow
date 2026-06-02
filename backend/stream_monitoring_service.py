"""Compatibility alias for legacy imports expecting top-level stream_monitoring_service."""

import sys

from apps.stream import stream_monitoring_service as _impl

sys.modules[__name__] = _impl
