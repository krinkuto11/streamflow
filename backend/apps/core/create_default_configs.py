#!/usr/bin/env python3
"""
Create default configuration files if they don't exist.

This script is run during Docker build to ensure proper defaults.
It creates necessary configuration files for automation, webhooks,
regex patterns, and changelog tracking.
"""

import os
from pathlib import Path
from typing import Dict, Any

try:
    from apps.core.atomic_json import atomic_write_json
except ModuleNotFoundError:  # Direct Docker build script execution.
    from atomic_json import atomic_write_json

# Configuration directory - persisted via Docker volume
CONFIG_DIR = Path(os.environ.get('CONFIG_DIR', '/app/data'))


def create_default_configs() -> None:
    """
    Create default configuration files.
    
    Creates default JSON configuration files for the application if they
    don't already exist. This includes automation config, regex patterns,
    changelog, and webhook configuration.
    """
    # Ensure config directory exists
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    
    # Create default automation config
    config_path = CONFIG_DIR / 'automation_config.json'
    if not config_path.exists():
        config: Dict[str, Any] = {
            'enabled': False,
            'playlist_refresh_interval': 300,
            'stream_check_interval': 3600,
            'global_check_interval': 86400,
            'max_concurrent_streams': 2,
            'stream_timeout': 30,
            'webhook_enabled': False
        }
        atomic_write_json(config_path, config)
        print(f"Created {config_path}")

    # Create default regex config
    regex_path = CONFIG_DIR / 'channel_regex_config.json'
    if not regex_path.exists():
        config = {'patterns': {}}
        atomic_write_json(regex_path, config)
        print(f"Created {regex_path}")

    # Create empty changelog
    changelog_path = CONFIG_DIR / 'changelog.json'
    if not changelog_path.exists():
        atomic_write_json(changelog_path, [])
        print(f"Created {changelog_path}")

    # Create default webhook config
    webhook_path = CONFIG_DIR / 'webhook_config.json'
    if not webhook_path.exists():
        config = {
            'webhooks': [],  # Empty list, not dict
            'enabled': False,
            'retry_attempts': 3,
            'retry_delay_seconds': 5,
            'timeout_seconds': 10
        }
        atomic_write_json(webhook_path, config)
        print(f"Created {webhook_path}")

if __name__ == '__main__':
    create_default_configs()
