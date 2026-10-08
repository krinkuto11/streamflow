#!/usr/bin/env python3
"""
OpenStream Configuration Manager

OpenStream servers put their control plane (``/api/...``) behind a login. The
monitor authenticates with an API key created in OpenStream under
*Settings → API Keys*, sent as the ``X-API-Key`` header. Playback URLs
(``/ace/getstream``) never need it.

The key comes from, in priority order:
1. ``OPENSTREAM_API_KEY_FILE`` / ``OPENSTREAM_API_KEY`` (Docker/Unraid secrets)
2. The ``openstream_config`` system setting (editable in Settings → Connection)

Server addresses are not configured here: each monitored stream's OpenStream
server is derived from its stream URL. ``test_url`` is only remembered for the
Settings page's "Test Connection" button.
"""

import threading
import time
from typing import Any, Dict, Optional

from apps.core.logging_config import setup_logging
from apps.core.secret_sources import external_secret_source, read_external_secret

logger = setup_logging(__name__)

SETTING_KEY = 'openstream_config'
API_KEY_ENV = 'OPENSTREAM_API_KEY'
API_KEY_FILE_ENV = 'OPENSTREAM_API_KEY_FILE'

# The monitor asks for the key on every poll (every 2 s per stream), so the
# stored value is cached briefly instead of hitting the database each time.
_CACHE_TTL = 30.0


class OpenStreamConfig:
    """Manages the OpenStream API key (and the Settings page's test URL)."""

    def __init__(self):
        self._lock = threading.RLock()
        self._cached: Optional[Dict[str, Any]] = None
        self._cached_at = 0.0

    def _stored(self) -> Dict[str, Any]:
        with self._lock:
            if self._cached is not None and time.monotonic() - self._cached_at < _CACHE_TTL:
                return self._cached
        from apps.database.manager import get_db_manager
        try:
            value = get_db_manager().get_system_setting(SETTING_KEY, {})
        except Exception as e:  # never let a settings read break monitoring
            logger.debug(f"Could not read OpenStream config: {e}")
            value = {}
        value = value if isinstance(value, dict) else {}
        with self._lock:
            self._cached, self._cached_at = value, time.monotonic()
        return value

    def api_key_managed_externally(self) -> bool:
        return bool(external_secret_source(API_KEY_ENV, API_KEY_FILE_ENV))

    def get_api_key(self) -> Optional[str]:
        if self.api_key_managed_externally():
            return read_external_secret(API_KEY_ENV, API_KEY_FILE_ENV)
        key = self._stored().get('api_key')
        return key.strip() if isinstance(key, str) and key.strip() else None

    def get_test_url(self) -> str:
        return str(self._stored().get('test_url') or '')

    def get_config(self) -> Dict[str, Any]:
        """Configuration for the UI, without the key itself."""
        return {
            'has_api_key': bool(self.get_api_key()),
            'api_key_managed_externally': self.api_key_managed_externally(),
            'test_url': self.get_test_url(),
        }

    def update_config(self, api_key: Optional[str] = None, test_url: Optional[str] = None) -> bool:
        """Save changed fields. ``api_key=""`` clears the stored key; None keeps it."""
        from apps.database.manager import get_db_manager
        db = get_db_manager()
        with self._lock:
            current = db.get_system_setting(SETTING_KEY, {})
            config = dict(current) if isinstance(current, dict) else {}
            if api_key is not None and not self.api_key_managed_externally():
                config['api_key'] = api_key.strip()
            if test_url is not None:
                config['test_url'] = test_url.strip().rstrip('/')
            ok = bool(db.set_system_setting(SETTING_KEY, config))
            if ok:
                self._cached, self._cached_at = config, time.monotonic()
            return ok


_openstream_config: Optional[OpenStreamConfig] = None
_config_lock = threading.Lock()


def get_openstream_config() -> OpenStreamConfig:
    """Get the global OpenStream configuration singleton."""
    global _openstream_config
    with _config_lock:
        if _openstream_config is None:
            _openstream_config = OpenStreamConfig()
        return _openstream_config


def openstream_headers() -> Dict[str, str]:
    """Request headers for OpenStream's control plane (empty without a key)."""
    try:
        key = get_openstream_config().get_api_key()
    except Exception:
        key = None
    return {'X-API-Key': key} if key else {}
