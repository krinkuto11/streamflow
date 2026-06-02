"""Compatibility alias for legacy imports expecting top-level automation_config_manager."""

import sys

from apps.automation import automation_config_manager as _impl

sys.modules[__name__] = _impl
