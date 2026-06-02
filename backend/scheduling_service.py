"""Compatibility alias for legacy imports expecting top-level scheduling_service."""

import sys

from apps.automation import scheduling_service as _impl

sys.modules[__name__] = _impl
