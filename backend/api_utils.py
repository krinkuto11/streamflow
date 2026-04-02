"""Compatibility alias for legacy imports expecting top-level api_utils module."""

import sys

from apps.core import api_utils as _impl

sys.modules[__name__] = _impl
