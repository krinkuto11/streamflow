"""Compatibility alias for legacy imports expecting top-level udi module."""

import sys

from apps import udi as _impl

sys.modules[__name__] = _impl
