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


def _coerce_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    """Return a bounded integer for user-tunable fetch pressure settings."""
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return min(max(parsed, minimum), maximum)


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
            if DISPATCHARR_CONFIG_FILE.exists():
                logger.info(f"Found legacy config file: {DISPATCHARR_CONFIG_FILE}. Migrating to SQL...")
                with open(DISPATCHARR_CONFIG_FILE, 'r') as f:
                    file_config = json.load(f)
                    with self._lock:
                        self._config = file_config
                
                # Save to DB
                db.set_system_setting('dispatcharr_config', self._config)
                logger.info("Migrated Dispatcharr configuration to database")
                
                # Delete file
                try:
                    DISPATCHARR_CONFIG_FILE.unlink()
                    logger.info(f"Deleted legacy config file: {DISPATCHARR_CONFIG_FILE.name}")
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
        return db_config.get('base_url')
    
    def get_username(self) -> Optional[str]:
        """Get Dispatcharr username from database.
        
        Returns:
            Username or None if not configured
        """
        from apps.database.manager import get_db_manager
        db_config = get_db_manager().get_system_setting('dispatcharr_config', {})
        return db_config.get('username')
    
    def get_password(self) -> Optional[str]:
        """Get Dispatcharr password from database.
        
        Returns:
            Password or None if not configured
        """
        from apps.database.manager import get_db_manager
        db_config = get_db_manager().get_system_setting('dispatcharr_config', {})
        return db_config.get('password')

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
            'username': self.get_username() or '',
            'has_password': bool(self.get_password()),
            'stream_fetch_page_size': self.get_stream_fetch_page_size(),
            'stream_fetch_max_workers': self.get_stream_fetch_max_workers(),
        }
    
    def update_config(self, base_url: Optional[str] = None, 
                     username: Optional[str] = None,
                     password: Optional[str] = None,
                     stream_fetch_page_size: Optional[Any] = None,
                     stream_fetch_max_workers: Optional[Any] = None) -> bool:
        """Update configuration and save to database.
        
        Args:
            base_url: Dispatcharr base URL
            username: Dispatcharr username
            password: Dispatcharr password
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
            if username is not None:
                self._config['username'] = username.strip()
            if password is not None:
                self._config['password'] = password
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
            True if base_url, username, and password are all configured
        """
        return all([
            self.get_base_url(),
            self.get_username(),
            self.get_password()
        ])


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
