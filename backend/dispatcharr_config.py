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
from typing import Dict, Optional, Any

from logging_config import setup_logging

logger = setup_logging(__name__)

# Configuration directory
CONFIG_DIR = Path(os.environ.get('CONFIG_DIR', '/app/data'))
DISPATCHARR_CONFIG_FILE = CONFIG_DIR / 'dispatcharr_config.json'


class DispatcharrConfig:
    """
    Manages Dispatcharr connection configuration.
    
    Priority order:
    1. Environment variables (override)
    2. JSON configuration file
    """
    
    def __init__(self):
        """Initialize the configuration manager."""
        self._lock = threading.Lock()
        self._config: Dict[str, str] = {}
        self._load_config()
        logger.info("Dispatcharr configuration manager initialized")
    
    def _load_config(self) -> None:
        """Load configuration from file."""
        try:
            if DISPATCHARR_CONFIG_FILE.exists():
                with open(DISPATCHARR_CONFIG_FILE, 'r') as f:
                    self._config = json.load(f)
                    logger.info("Loaded Dispatcharr configuration from file")
            else:
                self._config = {}
                logger.info("No existing Dispatcharr configuration file")
        except Exception as e:
            logger.error(f"Error loading Dispatcharr configuration: {e}", exc_info=True)
            self._config = {}
    
    def _save_config(self) -> bool:
        """Save configuration to file.
        
        Returns:
            True if successful, False otherwise
        """
        try:
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            with open(DISPATCHARR_CONFIG_FILE, 'w') as f:
                json.dump(self._config, f, indent=2)
            logger.info("Dispatcharr configuration saved to file")
            return True
        except Exception as e:
            logger.error(f"Error saving Dispatcharr configuration: {e}", exc_info=True)
            return False
    
    def get_base_url(self) -> Optional[str]:
        """Get Dispatcharr base URL.
        
        Priority: Environment variable > Config file
        
        Returns:
            Base URL or None if not configured
        """
        # Environment variable takes priority
        env_url = os.getenv("DISPATCHARR_BASE_URL")
        if env_url:
            return env_url
        
        # Fall back to config file
        with self._lock:
            return self._config.get('base_url')
    
    def get_username(self) -> Optional[str]:
        """Get Dispatcharr username.
        
        Priority: Environment variable > Config file
        
        Returns:
            Username or None if not configured
        """
        # Environment variable takes priority
        env_user = os.getenv("DISPATCHARR_USER")
        if env_user:
            return env_user
        
        # Fall back to config file
        with self._lock:
            return self._config.get('username')
    
    def get_password(self) -> Optional[str]:
        """Get Dispatcharr password.
        
        Priority: Environment variable > Config file
        
        Returns:
            Password or None if not configured
        """
        # Environment variable takes priority
        env_pass = os.getenv("DISPATCHARR_PASS")
        if env_pass:
            return env_pass
        
        # Fall back to config file
        with self._lock:
            return self._config.get('password')
    
    def get_config(self) -> Dict[str, str]:
        """Get complete configuration (without password for security).
        
        Returns:
            Dictionary with base_url, username, and has_password
        """
        return {
            'base_url': self.get_base_url() or '',
            'username': self.get_username() or '',
            'has_password': bool(self.get_password())
        }
    
    def update_config(self, base_url: Optional[str] = None, 
                     username: Optional[str] = None,
                     password: Optional[str] = None) -> bool:
        """Update configuration and save to file.
        
        Args:
            base_url: Dispatcharr base URL
            username: Dispatcharr username
            password: Dispatcharr password
            
        Returns:
            True if successful, False otherwise
        """
        with self._lock:
            if base_url is not None:
                self._config['base_url'] = base_url.strip()
            if username is not None:
                self._config['username'] = username.strip()
            if password is not None:
                self._config['password'] = password
            
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
    
    def get(self, key: str, default: Any = None) -> Any:
        """Get a configuration value by key.
        
        Args:
            key: Configuration key
            default: Default value if key not found
            
        Returns:
            Configuration value or default
        """
        with self._lock:
            return self._config.get(key, default)
    
    def __getitem__(self, key: str) -> Any:
        """Get a configuration value using dictionary syntax.
        
        Args:
            key: Configuration key
            
        Returns:
            Configuration value
            
        Raises:
            KeyError: If key not found
        """
        with self._lock:
            return self._config[key]
    
    def __setitem__(self, key: str, value: Any) -> None:
        """Set a configuration value using dictionary syntax.
        
        Args:
            key: Configuration key
            value: Configuration value
        """
        with self._lock:
            self._config[key] = value
    
    def save(self) -> bool:
        """Save configuration to file.
        
        Returns:
            True if successful, False otherwise
        """
        return self._save_config()


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
