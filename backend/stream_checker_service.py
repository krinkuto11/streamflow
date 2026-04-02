"""Compatibility alias for legacy imports expecting top-level stream_checker_service module."""

import sys

from apps.stream import stream_checker_service as _impl

# Alias this module name to the real implementation so patching attributes
# on "stream_checker_service" affects symbols in apps.stream.stream_checker_service.
sys.modules[__name__] = _impl
