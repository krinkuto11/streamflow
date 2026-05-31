#!/usr/bin/env python3
"""
Dispatcharr Configuration Manager

Manages Dispatcharr connection credentials with priority:
1. JSON configuration file (dispatcharr_config.json)
2. Environment variables (as override)
"""

import json
import os
import threading
from pathlib import Path
from typing import Any, Dict, Optional

from apps.core.logging_config import setup_logging

logger = setup_logging(__name__)

# Configuration directory
CONFIG_DIR = Path(os.environ.get('CONFIG_DIR', '/app/data'))
DISPATCHARR_CONFIG_FILE = CONFIG_DIR / 'dispatcharr_config.json'
DEFAULT_STREAM_FETCH_PAGE_SIZE = 1000
DEFAULT_STREAM_FETCH_MAX_WORKERS = 10
MIN_STREAM_FETCH_PAGE_SIZE = 100
MAX_STREAM_FETCH_PAGE_SIZE = 10000
MIN_STREAM_FETCH_MAX_WORKERS = 1
MAX_STREAM_FETCH_MAX_WORKERS = 20
DEFAULT_AUTH_MODE = 'credentials'
AUTH_MODES = {'credentials', 'api_key'}


def _coerce_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    """Return a bounded integer for user-tunable fetch pressure settings."""
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return min(max(parsed, minimum), maximum)


def _normalize_auth_mode(value: Any) -> str:
    mode = str(value or DEFAULT_AUTH_MODE).strip().lower()
    return mode if mode in AUTH_MODES else DEFAULT_AUTH_MODE


class DispatcharrConfig:
    """
    Manages Dispatcharr connection configuration.
    
    Priority order:
    1. Environment variables (override)
    2. JSON configuration file
    """
    
    def __init__(self):
        """Initialize the configuration manager."""
        self._lock = threading.RLock()
        self._config: Dict[str, Any] = {}
        self._load_config()
        logger.info("Dispatcharr configuration manager initialized")
    
    def _load_config(self) -> None:
        """Load configuration from database with fallback to file for migration."""
        from apps.database.manager import get_db_manager
        try:
            db = get_db_manager()
            # Try to load from DB first
            db_config = db.get_system_setting('dispatcharr_config')
            if db_config:
                with self._lock:
                    self._config = db_config
                logger.info("Loaded Dispatcharr configuration from database")
                return

            # Auto-migration: If not in DB, check for legacy file
            config_file = Path(CONFIG_DIR) / 'dispatcharr_config.json'
            if config_file.exists():
                logger.info(f"Found legacy config file: {config_file}. Migrating to SQL...")
                with open(config_file, 'r') as f:
                    file_config = json.load(f)
                    with self._lock:
                        self._config = file_config
                
                # Save to DB
                db.set_system_setting('dispatcharr_config', self._config)
                logger.info("Migrated Dispatcharr configuration to database")
                
                # Delete file
                try:
                    config_file.unlink()
                    logger.info(f"Deleted legacy config file: {config_file.name}")
                except Exception as e:
                    logger.warning(f"Could not delete legacy config file: {e}")
            else:
                with self._lock:
                    self._config = {}
                logger.info("No Dispatcharr configuration found in DB or file")
                
        except Exception as e:
            logger.error(f"Error loading Dispatcharr configuration: {e}", exc_info=True)
            with self._lock:
                self._config = {}
    
    def _save_config(self) -> bool:
        """Save configuration to database.
        
        Returns:
            True if successful, False otherwise
        """
        from apps.database.manager import get_db_manager
        try:
            with self._lock:
                config_to_save = self._config.copy()
            
            db = get_db_manager()
            success = db.set_system_setting('dispatcharr_config', config_to_save)
            if success:
                logger.info("Dispatcharr configuration saved to database")
            else:
                logger.error("Failed to save Dispatcharr configuration to database")
            return success
        except Exception as e:
            logger.error(f"Error saving Dispatcharr configuration: {e}", exc_info=True)
            return False
    
    def get_base_url(self) -> Optional[str]:
        """Get Dispatcharr base URL from database.
        
        Returns:
            Base URL or None if not configured
        """
        from apps.database.manager import get_db_manager
        db_config = get_db_manager().get_system_setting('dispatcharr_config', {})
        if not db_config:
            self._load_config()
            db_config = self._config
        return db_config.get('base_url') or os.getenv('DISPATCHARR_BASE_URL')
    
    def get_username(self) -> Optional[str]:
        """Get Dispatcharr username from database.
        
        Returns:
            Username or None if not configured
        """
        from apps.database.manager import get_db_manager
        db_config = get_db_manager().get_system_setting('dispatcharr_config', {})
        if not db_config:
            self._load_config()
            db_config = self._config
        return db_config.get('username') or os.getenv('DISPATCHARR_USER')
    
    def get_password(self) -> Optional[str]:
        """Get Dispatcharr password from database.
        
        Returns:
            Password or None if not configured
        """
        from apps.database.manager import get_db_manager
        db_config = get_db_manager().get_system_setting('dispatcharr_config', {})
        if not db_config:
            self._load_config()
            db_config = self._config
        return db_config.get('password') or os.getenv('DISPATCHARR_PASS')

    def get_api_key(self) -> Optional[str]:
        """Get Dispatcharr API key from database."""
        from apps.database.manager import get_db_manager
        db_config = get_db_manager().get_system_setting('dispatcharr_config', {})
        if not db_config:
            self._load_config()
            db_config = self._config
        return db_config.get('api_key') or os.getenv('DISPATCHARR_API_KEY') or os.getenv('DISPATCHARR_TOKEN')

    def get_auth_mode(self) -> str:
        """Get configured Dispatcharr authentication mode."""
        from apps.database.manager import get_db_manager
        db_config = get_db_manager().get_system_setting('dispatcharr_config', {})
        if not db_config and (os.getenv('DISPATCHARR_API_KEY') or os.getenv('DISPATCHARR_TOKEN')):
            return 'api_key'
        mode = _normalize_auth_mode(db_config.get('auth_mode'))
        if (
            'auth_mode' not in db_config
            and db_config.get('api_key')
            and not (db_config.get('username') and db_config.get('password'))
        ):
            return 'api_key'
        return mode

    def get_stream_fetch_page_size(self) -> int:
        """Get Dispatcharr stream page size for UDI stream refreshes."""
        from apps.database.manager import get_db_manager
        db_config = get_db_manager().get_system_setting('dispatcharr_config', {})
        return _coerce_int(
            db_config.get('stream_fetch_page_size'),
            DEFAULT_STREAM_FETCH_PAGE_SIZE,
            MIN_STREAM_FETCH_PAGE_SIZE,
            MAX_STREAM_FETCH_PAGE_SIZE,
        )

    def get_stream_fetch_max_workers(self) -> int:
        """Get Dispatcharr stream page concurrency for UDI stream refreshes."""
        from apps.database.manager import get_db_manager
        db_config = get_db_manager().get_system_setting('dispatcharr_config', {})
        return _coerce_int(
            db_config.get('stream_fetch_max_workers'),
            DEFAULT_STREAM_FETCH_MAX_WORKERS,
            MIN_STREAM_FETCH_MAX_WORKERS,
            MAX_STREAM_FETCH_MAX_WORKERS,
        )
    
    def get_config(self) -> Dict[str, Any]:
        """Get complete configuration (without password for security).
        
        Returns:
            Dictionary with base_url, username, password state, and fetch tuning.
        """
        return {
            'base_url': self.get_base_url() or '',
            'auth_mode': self.get_auth_mode(),
            'username': self.get_username() or '',
            'has_password': bool(self.get_password()),
            'has_api_key': bool(self.get_api_key()),
            'stream_fetch_page_size': self.get_stream_fetch_page_size(),
            'stream_fetch_max_workers': self.get_stream_fetch_max_workers(),
        }
    
    def update_config(self, base_url: Optional[str] = None, 
                     auth_mode: Optional[str] = None,
                     username: Optional[str] = None,
                     password: Optional[str] = None,
                     api_key: Optional[str] = None,
                     stream_fetch_page_size: Optional[Any] = None,
                     stream_fetch_max_workers: Optional[Any] = None) -> bool:
        """Update configuration and save to database.
        
        Args:
            base_url: Dispatcharr base URL
            auth_mode: Authentication mode, either credentials or api_key
            username: Dispatcharr username
            password: Dispatcharr password
            api_key: Dispatcharr API key
            stream_fetch_page_size: Items per stream page during UDI stream refreshes
            stream_fetch_max_workers: Concurrent stream page requests during UDI stream refreshes
            
        Returns:
            True if successful, False otherwise
        """
        from apps.database.manager import get_db_manager
        db = get_db_manager()
        
        with self._lock:
            # Fetch current from DB to update only specified fields
            current_config = db.get_system_setting('dispatcharr_config', {})
            self._config = current_config if isinstance(current_config, dict) else {}
            
            if base_url is not None:
                self._config['base_url'] = base_url.strip()
            if auth_mode is not None:
                self._config['auth_mode'] = _normalize_auth_mode(auth_mode)
            if username is not None:
                self._config['username'] = username.strip()
            if password is not None:
                self._config['password'] = password
            if api_key is not None:
                self._config['api_key'] = api_key.strip()
            if stream_fetch_page_size is not None:
                self._config['stream_fetch_page_size'] = _coerce_int(
                    stream_fetch_page_size,
                    DEFAULT_STREAM_FETCH_PAGE_SIZE,
                    MIN_STREAM_FETCH_PAGE_SIZE,
                    MAX_STREAM_FETCH_PAGE_SIZE,
                )
            if stream_fetch_max_workers is not None:
                self._config['stream_fetch_max_workers'] = _coerce_int(
                    stream_fetch_max_workers,
                    DEFAULT_STREAM_FETCH_MAX_WORKERS,
                    MIN_STREAM_FETCH_MAX_WORKERS,
                    MAX_STREAM_FETCH_MAX_WORKERS,
                )
            
            return self._save_config()
    
    def is_configured(self) -> bool:
        """Check if all required configuration is present.
        
        Returns:
            True if base_url and the selected authentication method are configured.
        """
        if not self.get_base_url():
            return False
        if self.get_auth_mode() == 'api_key':
            return bool(self.get_api_key())
        return bool(self.get_username() and self.get_password())


# Global singleton instance
_dispatcharr_config: Optional[DispatcharrConfig] = None
_config_lock = threading.Lock()


def get_dispatcharr_config() -> DispatcharrConfig:
    """Get the global Dispatcharr configuration singleton instance.
    
    Returns:
        The Dispatcharr configuration instance
    """
    global _dispatcharr_config
    with _config_lock:
        if _dispatcharr_config is None:
            _dispatcharr_config = DispatcharrConfig()
        return _dispatcharr_config
