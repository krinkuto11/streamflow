"""Compatibility alias for legacy imports expecting apps.udimanager."""

import sys

from apps import udi as _impl

manager = _impl.manager
cache = _impl.cache
storage = _impl.storage
UDIManager = _impl.UDIManager
UDIStorage = _impl.UDIStorage
get_udi_manager = _impl.get_udi_manager

sys.modules[__name__] = _impl
