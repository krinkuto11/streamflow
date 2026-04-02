"""
Configuration Migration Script

Automatically detects and migrates legacy configuration format to new profile-based system.
Safe, idempotent, and preserves all user settings with automatic backup.

Author: StreamFlow
Version: 2.0.0
"""

import json
import shutil
import logging
from pathlib import Path
from datetime import datetime
from typing import Dict, Any, Optional, Tuple
import uuid

# Setup logging
logger = logging.getLogger(__name__)

# Paths
CONFIG_DIR = Path(__file__).parent.parent / 'data'
LEGACY_DIR = Path(__file__).parent.parent / 'legacy_files'

# Configuration files
AUTOMATION_CONFIG = CONFIG_DIR / 'automation_config.json'
M3U_PRIORITY_CONFIG = CONFIG_DIR / 'm3u_priority_config.json'
PROFILE_CONFIG = CONFIG_DIR / 'profile_config.json'


def is_legacy_format(config: Dict[str, Any]) -> bool:
    """
    Detect if automation config is in legacy format.
    
    Legacy format has:
    - playlist_update_interval_minutes as integer (not dict)
    - enabled_features dict
    - No 'profiles' key
    
    Args:
        config: Automation configuration dictionary
        
    Returns:
        True if legacy format detected
    """
    # Check for legacy indicators
    has_enabled_features = 'enabled_features' in config
    has_old_interval_format = (
        'playlist_update_interval_minutes' in config and 
        isinstance(config['playlist_update_interval_minutes'], (int, float))
    )
    lacks_profiles = 'profiles' not in config
    
    return has_enabled_features or (has_old_interval_format and lacks_profiles)


def create_backup(timestamp: str) -> Path:
    """
    Create backup directory and copy all current config files.
    
    Args:
        timestamp: Timestamp string for backup directory name
        
    Returns:
        Path to backup directory
    """
    backup_dir = CONFIG_DIR / f'migration_backup_{timestamp}'
    backup_dir.mkdir(parents=True, exist_ok=True)
    
    # Files to backup
    files_to_backup = [
        'automation_config.json',
        'm3u_priority_config.json',
        'profile_config.json',
        'stream_checker_config.json',
        'channel_regex_config.json',
        'dispatcharr_config.json'
    ]
    
    for filename in files_to_backup:
        source = CONFIG_DIR / filename
        if source.exists():
            shutil.copy2(source, backup_dir / filename)
            logger.info(f"Backed up {filename}")
    
    logger.info(f"Created backup at {backup_dir}")
    return backup_dir


def load_legacy_configs() -> Tuple[Dict, Optional[Dict], Optional[Dict]]:
    """
    Load legacy configuration files.
    
    Returns:
        Tuple of (automation_config, m3u_priority_config, profile_config)
    """
    # Load automation config (required)
    with open(AUTOMATION_CONFIG, 'r') as f:
        automation_config = json.load(f)
    
    # Load M3U priority config (optional)
    m3u_priority_config = None
    if M3U_PRIORITY_CONFIG.exists():
        with open(M3U_PRIORITY_CONFIG, 'r') as f:
            m3u_priority_config = json.load(f)
    
    # Load profile config (optional, deprecated)
    profile_config = None
    if PROFILE_CONFIG.exists():
        with open(PROFILE_CONFIG, 'r') as f:
            profile_config = json.load(f)
    
    return automation_config, m3u_priority_config, profile_config


def convert_to_new_format(
    legacy_automation: Dict[str, Any],
    legacy_m3u_priority: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """
    Convert legacy configuration to new profile-based format.
    
    Args:
        legacy_automation: Legacy automation config
        legacy_m3u_priority: Legacy M3U priority config (optional)
        
    Returns:
        New format automation configuration
    """
    # Generate UUID for default profile
    profile_id = str(uuid.uuid4())
    
    # Extract legacy settings with defaults
    enabled_features = legacy_automation.get('enabled_features', {})
    enabled_m3u_accounts = legacy_automation.get('enabled_m3u_accounts', [])
    validate_existing = legacy_automation.get('validate_existing_streams', False)
    
    # Get M3U priority settings if available
    m3u_priority = []
    m3u_priority_mode = 'equal'
    if legacy_m3u_priority and 'accounts' in legacy_m3u_priority:
        # Extract account priorities
        for account_id, priority_data in legacy_m3u_priority.get('accounts', {}).items():
            if isinstance(priority_data, dict) and priority_data.get('priority', 0) > 0:
                m3u_priority.append(account_id)
        m3u_priority_mode = legacy_m3u_priority.get('global_priority_mode', 'equal')
        # Map old mode names to new
        if m3u_priority_mode == 'disabled':
            m3u_priority_mode = 'equal'
    
    # Extract interval settings - handle both old (int) and transitional (dict) formats
    playlist_interval = legacy_automation.get('playlist_update_interval_minutes', 5)
    if isinstance(playlist_interval, dict):
        # Already in new format
        interval_value = playlist_interval.get('value', 5)
    else:
        # Old format - convert to new
        interval_value = playlist_interval
    
    # Determine global check interval
    global_check_hours = legacy_automation.get('global_check_interval_hours', 24)
    global_schedule_minutes = global_check_hours * 60
    
    # Create new configuration structure
    new_config = {
        "regular_automation_enabled": legacy_automation.get('autostart_automation', True),
        "global_action_enabled": False,
        "playlist_update_interval_minutes": {
            "type": "interval",
            "value": interval_value
        },
        "global_schedule": {
            "type": "interval",
            "value": global_schedule_minutes
        },
        "profiles": {
            profile_id: {
                "id": profile_id,
                "name": "Migrated Default Profile",
                "description": "Automatically migrated from legacy configuration",
                "m3u_update": {
                    "enabled": enabled_features.get('auto_playlist_update', True),
                    "playlists": enabled_m3u_accounts if enabled_m3u_accounts else []
                },
                "stream_matching": {
                    "enabled": enabled_features.get('auto_stream_discovery', True),
                    "playlists": [],
                    "validate_existing_streams": validate_existing,
                    "match_priority_order": ["regex", "tvg"]
                },
                "stream_checking": {
                    "enabled": enabled_features.get('auto_quality_reordering', True),
                    "stream_limit": 0,
                    "allow_revive": False,
                    "remove_dead_streams": True,
                    "grace_period": False,
                    "check_all_streams": False,
                    "loop_check_enabled": False,
                    "m3u_priority": m3u_priority,
                    "m3u_priority_mode": m3u_priority_mode,
                    "min_resolution": "any",
                    "min_bitrate": 0,
                    "min_fps": 0
                },
                "global_action": {
                    "affected": False
                }
            }
        },
        "channel_assignments": {},
        "group_assignments": {},
        "validate_existing_streams": validate_existing
    }
    
    return new_config


def migrate_legacy_config() -> bool:
    """
    Main migration function. Detects legacy config and migrates to new format.
    
    Returns:
        True if migration was performed, False if not needed
    """
    # Check if automation config exists
    if not AUTOMATION_CONFIG.exists():
        logger.info("No automation config found - skipping migration")
        return False
    
    # Load current config
    try:
        with open(AUTOMATION_CONFIG, 'r') as f:
            current_config = json.load(f)
    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse automation config: {e}")
        return False
    
    # Check if migration needed
    if not is_legacy_format(current_config):
        logger.info("Configuration already in new format - skipping migration")
        return False
    
    logger.info("=" * 80)
    logger.info("LEGACY CONFIGURATION DETECTED - STARTING MIGRATION")
    logger.info("=" * 80)
    
    # Create timestamp for backup
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    
    try:
        # Step 1: Create backup
        logger.info("Step 1/5: Creating backup...")
        backup_dir = create_backup(timestamp)
        
        # Step 2: Load legacy configs
        logger.info("Step 2/5: Loading legacy configurations...")
        legacy_automation, legacy_m3u_priority, legacy_profile = load_legacy_configs()
        
        # Step 3: Convert to new format
        logger.info("Step 3/5: Converting to new profile-based format...")
        new_config = convert_to_new_format(legacy_automation, legacy_m3u_priority)
        
        # Step 4: Write new configuration
        logger.info("Step 4/5: Writing new configuration...")
        with open(AUTOMATION_CONFIG, 'w') as f:
            json.dump(new_config, f, indent=2)
        logger.info(f"Created new automation config with profile: {list(new_config['profiles'].keys())[0]}")
        
        # Step 5: Move deprecated files to legacy directory
        logger.info("Step 5/5: Moving deprecated files to legacy directory...")
        LEGACY_DIR.mkdir(parents=True, exist_ok=True)
        
        # Move m3u_priority_config.json if exists
        if M3U_PRIORITY_CONFIG.exists():
            legacy_dest = LEGACY_DIR / 'm3u_priority_config.json'
            if legacy_dest.exists():
                # If file already exists in legacy, append timestamp
                legacy_dest = LEGACY_DIR / f'm3u_priority_config_{timestamp}.json'
            shutil.move(str(M3U_PRIORITY_CONFIG), str(legacy_dest))
            logger.info(f"Moved m3u_priority_config.json to {legacy_dest}")
        
        # Move profile_config.json if exists
        if PROFILE_CONFIG.exists():
            legacy_dest = LEGACY_DIR / 'profile_config.json'
            if legacy_dest.exists():
                # If file already exists in legacy, append timestamp
                legacy_dest = LEGACY_DIR / f'profile_config_{timestamp}.json'
            shutil.move(str(PROFILE_CONFIG), str(legacy_dest))
            logger.info(f"Moved profile_config.json to {legacy_dest}")
        
        logger.info("=" * 80)
        logger.info("MIGRATION COMPLETED SUCCESSFULLY")
        logger.info(f"Backup created at: {backup_dir}")
        logger.info(f"New profile created: '{new_config['profiles'][list(new_config['profiles'].keys())[0]]['name']}'")
        logger.info("=" * 80)
        
        return True
        
    except Exception as e:
        logger.error(f"Migration failed: {e}", exc_info=True)
        logger.error("Configuration backup preserved - manual intervention may be required")
        return False


def check_and_migrate_legacy_config() -> bool:
    """
    Safe wrapper for migration check. Can be called during application startup.
    
    Returns:
        True if migration was performed, False otherwise
    """
    try:
        return migrate_legacy_config()
    except Exception as e:
        logger.error(f"Unexpected error during migration check: {e}", exc_info=True)
        return False


# Allow running as standalone script for testing
if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    
    result = check_and_migrate_legacy_config()
    if result:
        print("\n✅ Migration completed successfully")
    else:
        print("\n ℹ️  No migration needed or migration skipped")
