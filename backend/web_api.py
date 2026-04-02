"""Compatibility alias for legacy imports expecting top-level web_api module."""

import sys

from apps.api import web_api as _impl

# Alias this module name to the real implementation so patching attributes
# on "web_api" affects route functions in apps.api.web_api.
sys.modules[__name__] = _impl
