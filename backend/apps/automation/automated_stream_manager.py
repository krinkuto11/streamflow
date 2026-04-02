#!/usr/bin/env python3
"""
Automated Stream Manager for Dispatcharr

This module handles the automated process of:
1. Updating M3U playlists
2. Discovering new streams and assigning them to channels via regex
3. Maintaining changelog of updates

Uses the Universal Data Index (UDI) as the single source of truth for data access.
"""

import json
import logging
import os
import re
import time
import threading
from functools import lru_cache
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any, Union
import concurrent.futures
from collections import defaultdict

# Pre-compiled regex pattern for whitespace conversion (performance optimization)
# This pattern matches one or more spaces that are NOT preceded by a backslash
# Used to convert literal spaces to flexible whitespace while preserving escaped spaces
_WHITESPACE_PATTERN = re.compile(r'(?<!\\) +')

# Placeholder for CHANNEL_NAME variable during regex validation
# Used to substitute CHANNEL_NAME in patterns before compiling for validation
_CHANNEL_NAME_PLACEHOLDER = 'PLACEHOLDER'


@lru_cache(maxsize=50000)
def _compile_stream_search_regex(pattern: str, channel_name: str, case_sensitive: bool) -> re.Pattern:
    """Compile and cache stream-matching regex patterns for reuse."""
    substituted_pattern = pattern.replace('CHANNEL_NAME', re.escape(channel_name))
    search_pattern = substituted_pattern if case_sensitive else substituted_pattern.lower()
    search_pattern = _WHITESPACE_PATTERN.sub(r'\\s+', search_pattern)
    return re.compile(search_pattern)

# Import croniter for cron expression support
try:
    from croniter import croniter
    CRONITER_AVAILABLE = True
except ImportError:
    CRONITER_AVAILABLE = False

from apps.core.api_utils import (
    refresh_m3u_playlists,
    get_m3u_accounts,
    get_streams,
    add_streams_to_channel,
    _get_base_url
)

# Import UDI for direct data access
from apps.udi import get_udi_manager
from apps.automation.automation_config_manager import get_automation_config_manager

# Import channel settings manager
# Import channel settings manager - DEPRECATED/REMOVED
# from channel_settings_manager import get_channel_settings_manager

# Import profile config
# Import profile config - DEPRECATED/REMOVED
# from profile_config import get_profile_config

# Setup centralized logging
from apps.core.logging_config import setup_logging, log_function_call, log_function_return, log_exception, log_state_change

logger = setup_logging(__name__)

# Import DeadStreamsTracker
try:
    from apps.stream.dead_streams_tracker import DeadStreamsTracker
    DEAD_STREAMS_TRACKER_AVAILABLE = True
except ImportError:
    DEAD_STREAMS_TRACKER_AVAILABLE = False
    logger.warning("DeadStreamsTracker not available. Dead stream filtering will be disabled.")

# Configuration directory - persisted via Docker volume
CONFIG_DIR = Path(os.environ.get('CONFIG_DIR', '/app/data'))

class ChangelogManager:
    """Manages changelog entries for stream updates."""
    
    def __init__(self, changelog_file=None):
        if changelog_file is None:
            changelog_file = CONFIG_DIR / "changelog.json"
        self.changelog_file = Path(changelog_file)
        self.changelog = [] # deprecated but kept for backwards comp
        
        pass
    
    def _load_changelog(self) -> List[Dict]:
        """Deprecated."""
        return []
    
    def add_entry(self, action: str, details: Dict, timestamp: Optional[str] = None, subentries: Optional[List[Dict[str, Any]]] = None):
        """Add a new changelog entry."""
        if timestamp is None:
            timestamp = datetime.now().isoformat()
        
        # New telemetry DB logic
        try:
            from apps.telemetry.telemetry_db import save_automation_run_telemetry, save_generic_telemetry
            if action == 'automation_run':
                save_automation_run_telemetry(action, details, subentries, timestamp)
            else:
                save_generic_telemetry(action, details, subentries, timestamp)
            logger.info(f"Telemetry entry added: {action}")
        except Exception as e:
            logger.error(f"Failed to process telemetry: {e}")
    
    def _save_changelog(self):
        """Deprecated."""
        pass
    
    def get_recent_entries(self, days: int = 7) -> List[Dict]:
        """Deprecated: The UI will update to use the new Telemetry API."""
        return []
    
    def add_playlist_update_entry(self, channels_updated: Dict[int, Dict], global_stats: Dict):
        """Add a playlist update & match entry with subentries.
        
        Args:
            channels_updated: Dict mapping channel_id to update info (streams added, stats, logo_url)
            global_stats: Global statistics (total streams added, dead streams, avg resolution, avg bitrate)
        """
        # Create subentries for streams matched per channel
        update_subentries = []
        for channel_id, info in channels_updated.items():
            if info.get('streams_added'):
                update_subentries.append({
                    'type': 'update_match',
                    'channel_id': channel_id,
                    'channel_name': info.get('channel_name', f'Channel {channel_id}'),
                    'logo_url': info.get('logo_url'),
                    'streams': info.get('streams_added', [])
                })
        
        # Create subentries for channel checks
        check_subentries = []
        for channel_id, info in channels_updated.items():
            if info.get('check_stats'):
                check_subentries.append({
                    'type': 'check',
                    'channel_id': channel_id,
                    'channel_name': info.get('channel_name', f'Channel {channel_id}'),
                    'logo_url': info.get('logo_url'),
                    'stats': info.get('check_stats', {})
                })
        
        subentries = []
        if update_subentries:
            subentries.append({'group': 'update_match', 'items': update_subentries})
        if check_subentries:
            subentries.append({'group': 'check', 'items': check_subentries})
        
        self.add_entry(
            action='playlist_update_match',
            details=global_stats,
            subentries=subentries
        )
    
    def add_global_check_entry(self, channels_checked: Dict[int, Dict], global_stats: Dict):
        """Add a global check entry with subentries.
        
        Args:
            channels_checked: Dict mapping channel_id to check stats (including logo_url)
            global_stats: Global statistics across all channels
        """
        check_subentries = []
        for channel_id, stats in channels_checked.items():
            check_subentries.append({
                'type': 'check',
                'channel_id': channel_id,
                'channel_name': stats.get('channel_name', f'Channel {channel_id}'),
                'logo_url': stats.get('logo_url'),
                'stats': stats
            })
        
        subentries = [{'group': 'check', 'items': check_subentries}] if check_subentries else []
        
        self.add_entry(
            action='global_check',
            details=global_stats,
            subentries=subentries
        )
    
    def add_single_channel_check_entry(self, channel_id: int, channel_name: str, check_stats: Dict, logo_url: Optional[str] = None, program_name: Optional[str] = None):
        """Add a single channel check entry.
        
        Args:
            channel_id: ID of the channel checked
            channel_name: Name of the channel
            check_stats: Statistics from the channel check
            logo_url: Optional URL for the channel logo
            program_name: Optional program name if this was a scheduled EPG check
        """
        check_subentries = [{
            'type': 'check',
            'channel_id': channel_id,
            'channel_name': channel_name,
            'logo_url': logo_url,
            'stats': check_stats
        }]
        
        subentries = [{'group': 'check', 'items': check_subentries}]
        
        # Build details dict
        details = {
            'channel_id': channel_id,
            'channel_name': channel_name,
            'total_streams': check_stats.get('total_streams', 0),
            'dead_streams': check_stats.get('dead_streams', 0),
            'avg_resolution': check_stats.get('avg_resolution', 'N/A'),
            'avg_bitrate': check_stats.get('avg_bitrate', 'N/A')
        }
        
        # Add program name if provided (for scheduled EPG checks)
        if program_name:
            details['program_name'] = program_name
        
        self.add_entry(
            action='single_channel_check',
            details=details,
            subentries=subentries
        )
    
    def add_automation_run_entry(self, run_results: Dict[str, Any]):
        """Add a consolidated automation run entry.
        
        Args:
            run_results: Dictionary containing periods, channels, and their step results.
        """
        self.add_entry(
            action='automation_run',
            details=run_results
        )

    def _has_channel_updates(self, entry: Dict) -> bool:
        """Check if a changelog entry contains meaningful channel/stream updates."""
        details = entry.get('details', {})
        action = entry.get('action', '')
        
        # For automation_run, always include
        if action == 'automation_run':
            return True
            
        # For new action types, check if they have subentries
        if action in ['playlist_update_match', 'global_check', 'single_channel_check']:
            subentries = entry.get('subentries', [])
            # Include if there are subentries with items
            return any(group.get('items') for group in subentries)
        
        # For playlist_refresh, only include if there were actual changes
        if action == 'playlist_refresh':
            added = details.get('added_streams', [])
            removed = details.get('removed_streams', [])
            return len(added) > 0 or len(removed) > 0
        
        # For streams_assigned, only include if streams were actually assigned
        if action == 'streams_assigned':
            total_assigned = details.get('total_assigned', 0)
            return total_assigned > 0
        
        # For other actions, include if success is True or not specified
        # (exclude failed operations without updates)
        if 'success' in details:
            return details['success'] is True
        
        return True  # Include entries without explicit success flag


class RegexChannelMatcher:
    """Handles regex-based channel matching for stream assignment.

    Patterns are stored in the ``channel_regex_configs`` and
    ``channel_regex_patterns`` SQL tables via the DAL.  The legacy
    in-memory ``channel_patterns`` dict is kept populated from the DB so
    that no callers need to change (hot-path matching code reads the dict).
    """
    
    def __init__(self, config_file=None):
        # config_file kept for backward compatibility (tests, one-time migration).
        # If a path is given and the file exists, the patterns are seeded into the
        # SQL database before loading.  In production the parameter is unused.
        self.lock = threading.RLock()
        self._config_file: Optional[Path] = None
        if config_file is not None:
            config_file = Path(config_file)
            self._config_file = config_file
            if config_file.exists():
                self._seed_from_config_file(config_file)
        self.group_patterns_key = 'group_regex_patterns'
        self.channel_patterns = self._load_patterns()
        self.group_patterns = self._load_group_patterns()

    def _seed_from_config_file(self, config_file: Path):
        """Read a JSON config file and import the patterns into SQL.

        Used for backward-compat test setup and one-time migration paths.
        Any import errors are logged and silently swallowed so that the
        matcher still initialises with whatever state is already in the DB.
        """
        import json as _json
        try:
            with open(config_file, 'r') as fh:
                data = _json.load(fh)
            from apps.database.manager import get_db_manager
            db = get_db_manager()
            db.import_channel_regex_configs_from_json(data, merge=False)
            if isinstance(data.get('global_settings'), dict):
                db.set_system_setting('channel_regex_global_settings', data['global_settings'])
        except Exception as exc:
            logger.warning(
                f"Could not seed regex config from {config_file}: "
                f"{type(exc).__name__}: {exc}"
            )
    
    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_in_memory(self, configs: Dict[str, Any], global_settings: Dict[str, Any]) -> Dict:
        """Build the canonical in-memory dict from DAL data."""
        return {
            'patterns': configs,
            'global_settings': global_settings,
        }

    def _load_patterns(self) -> Dict:
        """Load regex patterns from SQL and build the in-memory cache.

        **Side-effects on the database**: If any stored patterns fail regex
        compilation they are permanently removed from the database during this
        call.  Channels whose *entire* pattern list is invalid are deleted.
        Callers that need explicit control over clean-up should call a
        dedicated validate method instead.

        Falls back to the legacy ``channel_regex_config`` SystemSetting JSON
        blob if the new tables are empty, migrating the data transparently.
        """
        from apps.database.manager import get_db_manager
        db = get_db_manager()

        configs = db.get_all_channel_regex_configs()
        global_settings = db.get_system_setting(
            'channel_regex_global_settings',
            {'case_sensitive': True, 'require_exact_match': False},
        )

        # --- One-time migration from legacy SystemSetting JSON blob ---
        if not configs:
            legacy = db.get_system_setting('channel_regex_config')
            if legacy and isinstance(legacy, dict) and 'patterns' in legacy:
                logger.info("Migrating regex patterns from SystemSetting JSON blob to dedicated SQL tables")
                imported, errors = db.import_channel_regex_configs_from_json(legacy)
                if errors:
                    logger.warning(f"Migration errors: {errors}")
                # Preserve global_settings from legacy blob
                if 'global_settings' in legacy:
                    global_settings = legacy['global_settings']
                    db.set_system_setting('channel_regex_global_settings', global_settings)
                # Clear the old blob to avoid re-migration on next start
                db.set_system_setting('channel_regex_config', None)
                configs = db.get_all_channel_regex_configs()

        # Validate and clean up invalid regex patterns
        configs_to_remove = []
        for channel_id, cfg in list(configs.items()):
            valid_patterns = []
            has_invalid = False
            for pat_obj in cfg.get('regex_patterns', []):
                pattern = pat_obj.get('pattern', '')
                if not pattern:
                    has_invalid = True
                    continue
                try:
                    validation_pattern = pattern.replace('CHANNEL_NAME', _CHANNEL_NAME_PLACEHOLDER)
                    re.compile(validation_pattern)
                    valid_patterns.append(pat_obj)
                except re.error as e:
                    logger.warning(f"Removing invalid regex '{pattern}' for channel {channel_id}: {e}")
                    has_invalid = True

            if has_invalid:
                if valid_patterns:
                    cfg['regex_patterns'] = valid_patterns
                    db.upsert_channel_regex_config(
                        channel_id=str(channel_id),
                        name=cfg.get('name', ''),
                        enabled=cfg.get('enabled', True),
                        match_by_tvg_id=cfg.get('match_by_tvg_id', False),
                        regex_patterns=valid_patterns,
                    )
                else:
                    configs_to_remove.append(channel_id)

        for cid in configs_to_remove:
            del configs[cid]
            db.delete_channel_regex_config(str(cid))

        return self._build_in_memory(configs, global_settings)

    def _save_patterns(self, patterns: Dict):
        """Persist patterns dict to SQL (used by import and legacy callers)."""
        from apps.database.manager import get_db_manager
        db = get_db_manager()
        patterns_dict = patterns.get('patterns', {})
        global_settings = patterns.get('global_settings', {})
        imported, errors = db.import_channel_regex_configs_from_json(
            {'patterns': patterns_dict}, merge=False
        )
        if errors:
            logger.error(f"Error saving patterns to SQL: {errors}")
        if global_settings:
            db.set_system_setting('channel_regex_global_settings', global_settings)
        # Keep in-memory cache in sync
        self.channel_patterns = self._build_in_memory(
            db.get_all_channel_regex_configs(), global_settings
        )
        self._clear_runtime_caches()

    def _clear_runtime_caches(self):
        """Clear hot-path caches used during stream matching."""
        _compile_stream_search_regex.cache_clear()

    def _load_group_patterns(self) -> Dict[str, Any]:
        """Load group-level regex pattern config from system settings."""
        from apps.database.manager import get_db_manager
        db = get_db_manager()
        data = db.get_system_setting(self.group_patterns_key, {}) or {}
        if not isinstance(data, dict):
            return {}
        return data

    def _get_group_patterns(self) -> Dict[str, Any]:
        """Get group-level regex pattern config from in-memory cache."""
        with self.lock:
            return dict(self.group_patterns)

    def _save_group_patterns(self, data: Dict[str, Any]) -> bool:
        """Persist group-level regex pattern config to system settings."""
        from apps.database.manager import get_db_manager
        db = get_db_manager()
        saved = db.set_system_setting(self.group_patterns_key, data)
        if saved:
            with self.lock:
                self.group_patterns = dict(data)
            self._clear_runtime_caches()
        return saved

    def _normalize_regex_patterns(self, regex_patterns: 'Union[List[str], List[Dict]]', m3u_accounts: Optional[List[int]] = None) -> List[Dict[str, Any]]:
        """Normalize regex patterns to the canonical object format."""
        normalized_patterns: List[Dict[str, Any]] = []

        if isinstance(regex_patterns, list) and len(regex_patterns) > 0:
            if isinstance(regex_patterns[0], dict):
                for item in regex_patterns:
                    if not isinstance(item, dict) or "pattern" not in item:
                        raise ValueError("Each pattern object must have a 'pattern' field")
                    normalized_patterns.append({
                        "pattern": item["pattern"],
                        "m3u_accounts": item.get("m3u_accounts")
                    })
            else:
                for pattern in regex_patterns:
                    normalized_patterns.append({
                        "pattern": pattern,
                        "m3u_accounts": m3u_accounts
                    })
        return normalized_patterns

    def _get_effective_channel_config(self, channel_id: Union[str, int], group_id: Optional[Union[str, int]] = None) -> Dict[str, Any]:
        """Return effective channel matching config with channel-over-group precedence."""
        channel_id_str = str(channel_id)
        channel_config = self.channel_patterns.get("patterns", {}).get(channel_id_str)
        if isinstance(channel_config, dict):
            return channel_config

        if group_id is None:
            return {}

        group_config = self.get_group_pattern(str(group_id))
        if not isinstance(group_config, dict):
            return {}

        # Use group-level settings as channel fallback; name is informational only.
        return {
            "name": group_config.get("name", ""),
            "enabled": group_config.get("enabled", True),
            "match_by_tvg_id": group_config.get("match_by_tvg_id", False),
            "regex_patterns": group_config.get("regex_patterns", [])
        }

    def get_group_pattern(self, group_id: Union[str, int]) -> Optional[Dict[str, Any]]:
        """Get regex config for a group."""
        patterns = self._get_group_patterns()
        cfg = patterns.get(str(group_id))
        return cfg if isinstance(cfg, dict) else None

    def add_group_pattern(self, group_id: Union[str, int], name: str, regex_patterns: 'Union[List[str], List[Dict]]', enabled: bool = True, match_by_tvg_id: bool = False, m3u_accounts: Optional[List[int]] = None):
        """Add or update regex pattern config for a group."""
        normalized_patterns = self._normalize_regex_patterns(regex_patterns, m3u_accounts)
        pattern_strings = [p["pattern"] for p in normalized_patterns]
        if pattern_strings:
            is_valid, error_msg = self.validate_regex_patterns(pattern_strings)
            if not is_valid:
                raise ValueError(error_msg)

        patterns = self._get_group_patterns()
        patterns[str(group_id)] = {
            "name": name,
            "enabled": enabled,
            "match_by_tvg_id": bool(match_by_tvg_id),
            "regex_patterns": normalized_patterns
        }
        self._save_group_patterns(patterns)

    def delete_group_pattern(self, group_id: Union[str, int]):
        """Delete regex config for a group."""
        patterns = self._get_group_patterns()
        gid = str(group_id)
        if gid in patterns:
            del patterns[gid]
            self._save_group_patterns(patterns)

    def set_group_match_by_tvg_id(self, group_id: Union[str, int], enabled: bool):
        """Enable or disable TVG-ID matching for a group."""
        patterns = self._get_group_patterns()
        gid = str(group_id)
        existing = patterns.get(gid, {}) if isinstance(patterns.get(gid), dict) else {}
        patterns[gid] = {
            "name": existing.get("name", ""),
            "enabled": existing.get("enabled", True),
            "match_by_tvg_id": bool(enabled),
            "regex_patterns": existing.get("regex_patterns", [])
        }
        self._save_group_patterns(patterns)

    def get_group_match_config(self, group_id: Union[str, int]) -> Dict[str, Any]:
        """Get matching config for a group."""
        cfg = self.get_group_pattern(group_id) or {}
        return {
            "match_by_tvg_id": cfg.get("match_by_tvg_id", False),
            "enabled": cfg.get("enabled", True),
            "name": cfg.get("name", ""),
            "regex_patterns": cfg.get("regex_patterns", [])
        }
    
    def validate_regex_patterns(self, patterns: List[str]) -> Tuple[bool, Optional[str]]:
        """Validate a list of regex patterns.
        
        Args:
            patterns: List of regex pattern strings to validate
            
        Returns:
            Tuple of (is_valid, error_message). If valid, error_message is None.
        """
        if not patterns:
            return False, "At least one regex pattern is required"
        
        for pattern in patterns:
            if not pattern or not isinstance(pattern, str):
                return False, f"Pattern must be a non-empty string"
            
            try:
                # Temporarily substitute CHANNEL_NAME with a placeholder for validation
                # Use a simple placeholder that won't interfere with regex syntax
                validation_pattern = pattern.replace('CHANNEL_NAME', _CHANNEL_NAME_PLACEHOLDER)
                re.compile(validation_pattern)
            except re.error as e:
                return False, f"Invalid regex pattern '{pattern}': {str(e)}"
        
        return True, None
    
    def add_channel_pattern(self, channel_id: str, name: str, regex_patterns: 'Union[List[str], List[Dict]]', enabled: bool = True, m3u_accounts: Optional[List[int]] = None, silent: bool = False):
        """Add or update a channel pattern.
        
        Args:
            channel_id: Channel ID
            name: Channel name
            regex_patterns: Can be either:
                          - List[str]: Legacy format, list of regex pattern strings
                          - List[Dict]: New format with per-pattern m3u_accounts
                              [{"pattern": str, "m3u_accounts": List[int] | None}, ...]
            enabled: Whether the pattern is enabled
            m3u_accounts: Optional list of M3U account IDs (legacy, channel-level).
                         Only used when regex_patterns is List[str].
                         Examples:
                         - None: Field not stored, applies to all M3U accounts (backward compatible)
                         - []: Empty list stored, explicitly means "all M3U accounts"
                         - [1, 2, 3]: Only match streams from M3U accounts with these IDs
            silent: If True, log at DEBUG level instead of INFO (useful for batch operations)
            
        Raises:
            ValueError: If any regex pattern is invalid
        """
        from apps.database.manager import get_db_manager
        db = get_db_manager()

        # Normalize regex_patterns to new format
        normalized_patterns = []
        
        if isinstance(regex_patterns, list) and len(regex_patterns) > 0:
            if isinstance(regex_patterns[0], dict):
                # New format: List[Dict]
                for item in regex_patterns:
                    if not isinstance(item, dict) or "pattern" not in item:
                        raise ValueError("Each pattern object must have a 'pattern' field")
                    normalized_patterns.append({
                        "pattern": item["pattern"],
                        "m3u_accounts": item.get("m3u_accounts")
                    })
            else:
                # Legacy format: List[str] - convert to new format
                for pattern in regex_patterns:
                    normalized_patterns.append({
                        "pattern": pattern,
                        "m3u_accounts": m3u_accounts  # Use channel-level m3u_accounts for all patterns
                    })
        else:
            raise ValueError("At least one regex pattern is required")
        
        # Validate patterns
        pattern_strings = [p["pattern"] for p in normalized_patterns]
        is_valid, error_msg = self.validate_regex_patterns(pattern_strings)
        if not is_valid:
            raise ValueError(error_msg)

        # Preserve existing match_by_tvg_id flag if already set
        existing_cfg = self.channel_patterns.get('patterns', {}).get(str(channel_id), {})
        match_by_tvg_id = existing_cfg.get('match_by_tvg_id', False)
        
        db.upsert_channel_regex_config(
            channel_id=str(channel_id),
            name=name,
            enabled=enabled,
            match_by_tvg_id=match_by_tvg_id,
            regex_patterns=normalized_patterns,
        )

        # Update in-memory cache
        with self.lock:
            self.channel_patterns.setdefault('patterns', {})[str(channel_id)] = {
                'name': name,
                'enabled': enabled,
                'match_by_tvg_id': match_by_tvg_id,
                'regex_patterns': normalized_patterns,
            }
        self._clear_runtime_caches()
        
        if silent:
            logger.debug(f"Added/updated {len(normalized_patterns)} pattern(s) for channel {channel_id}: {name}")
        else:
            logger.info(f"Added/updated {len(normalized_patterns)} pattern(s) for channel {channel_id}: {name}")
    
    def delete_channel_pattern(self, channel_id: str):
        """Delete all regex patterns for a channel."""
        from apps.database.manager import get_db_manager
        db = get_db_manager()

        channel_id = str(channel_id)
        if channel_id in self.channel_patterns.get("patterns", {}):
            with self.lock:
                del self.channel_patterns["patterns"][channel_id]
            self._clear_runtime_caches()
            db.delete_channel_regex_config(str(channel_id))
            logger.info(f"Deleted all patterns for channel {channel_id}")
        else:
            logger.warning(f"No patterns found for channel {channel_id}")
    
    def reload_patterns(self):
        """Reload patterns from SQL (refreshes the in-memory cache)."""
        # Legacy/test compatibility: when initialized from a config file,
        # refresh SQL from that file before rebuilding in-memory caches.
        if self._config_file is not None and self._config_file.exists():
            self._seed_from_config_file(self._config_file)

        with self.lock:
            self.channel_patterns = self._load_patterns()
            self.group_patterns = self._load_group_patterns()
        self._clear_runtime_caches()
        logger.debug("Reloaded regex patterns from SQL")
    
    def _substitute_channel_variables(self, pattern: str, channel_name: str) -> str:
        """Substitute channel name variables in a regex pattern.
        
        Args:
            pattern: Regex pattern that may contain CHANNEL_NAME
            channel_name: Name of the channel to substitute
            
        Returns:
            Pattern with variables substituted
        """
        # Replace CHANNEL_NAME with the actual channel name
        # Escape special regex characters in channel name to avoid issues
        escaped_channel_name = re.escape(channel_name)
        return pattern.replace('CHANNEL_NAME', escaped_channel_name)
    
    def match_stream_to_channels(self, stream_name: str, stream_m3u_account: Optional[int] = None,
                               stream_tvg_id: Optional[str] = None, channel_tvg_ids: Optional[Dict[str, str]] = None,
                               channel_match_priorities: Optional[Dict[str, List[str]]] = None,
                               channel_to_group_map: Optional[Dict[str, Any]] = None,
                               channel_name_map: Optional[Dict[str, str]] = None) -> List[str]:
        """
        Match a stream name and optionally TVG-ID to channels using regex patterns and TVG-ID matching.
        
        Args:
            stream_name: The name of the stream to match
            stream_m3u_account: The ID of the M3U account the stream belongs to (optional)
            stream_tvg_id: The TVG-ID of the stream (optional)
            channel_tvg_ids: Dictionary mapping channel_id -> tvg_id (optional, for optimization)
            channel_match_priorities: Dictionary mapping channel_id -> ['tvg', 'regex'] or ['regex', 'tvg'] (optional)
            
        Returns:
            List of channel IDs that match the stream
        """
        matches = []
        case_sensitive = self.channel_patterns.get("global_settings", {}).get("case_sensitive", True)
        
        search_name = stream_name if case_sensitive else stream_name.lower()
        
        channel_to_group_map = channel_to_group_map or {}
        channel_name_map = channel_name_map or {}

        # Iterate all target channels when available so group-only configs are considered.
        explicit_channel_ids = set(self.channel_patterns.get("patterns", {}).keys())
        if channel_tvg_ids:
            explicit_channel_ids.update(str(cid) for cid in channel_tvg_ids.keys())
        if channel_to_group_map:
            explicit_channel_ids.update(str(cid) for cid in channel_to_group_map.keys())

        for channel_id in explicit_channel_ids:
            group_id = channel_to_group_map.get(str(channel_id))
            config = self._get_effective_channel_config(channel_id, group_id)
            if not isinstance(config, dict) or not config:
                continue
            
            # Determine priority order
            # Default: TVG first (if enabled)
            priority_order = ['tvg', 'regex']
            if channel_match_priorities:
                # Look up by string ID since config keys are stringified
                mapped_order = channel_match_priorities.get(str(channel_id))
                if mapped_order:
                    priority_order = mapped_order
            
            matched_channel = False
            
            # Check based on priority order
            # We stop checking a channel if one method matches (optimization and precedence)
            for match_type in priority_order:
                if match_type == 'tvg':
                    # Check for TVG-ID match if enabled and not already matched
                    if not matched_channel and stream_tvg_id and channel_tvg_ids and config.get("match_by_tvg_id", False):
                        channel_tvg_id = channel_tvg_ids.get(str(channel_id))
                        if channel_tvg_id and stream_tvg_id == channel_tvg_id:
                            matches.append(channel_id)
                            matched_channel = True
                            break # Skip other match types for this channel
                            
                elif match_type == 'regex':
                    if matched_channel:
                        continue
                    
                    if not config.get("enabled", True):
                        continue
                    
                    channel_name = channel_name_map.get(str(channel_id)) or config.get("name", "")
                    match_by_tvg = config.get("match_by_tvg_id", False)
                    
                    # Support both new format (regex_patterns) and old format (regex) for backward compatibility
                    regex_patterns = config.get("regex_patterns")
                    if regex_patterns is None:
                        # Fallback to old format
                        old_regex = config.get("regex", [])
                        old_m3u_accounts = config.get("m3u_accounts")
                        regex_patterns = [{"pattern": p, "m3u_accounts": old_m3u_accounts} for p in old_regex]
                    
                    regex_matched = False
                    for pattern_obj in regex_patterns:
                        # Handle both dict and string patterns for flexibility
                        if isinstance(pattern_obj, dict):
                            pattern = pattern_obj.get("pattern", "")
                            pattern_m3u_accounts = pattern_obj.get("m3u_accounts")
                        else:
                            # Legacy string format
                            pattern = pattern_obj
                            pattern_m3u_accounts = None
                        
                        if not pattern:
                            continue
                        
                        # Check if this regex pattern applies to the stream's M3U account
                        if pattern_m3u_accounts is not None and len(pattern_m3u_accounts) > 0:
                            # Pattern is limited to specific M3U accounts
                            if stream_m3u_account is None or stream_m3u_account not in pattern_m3u_accounts:
                                # Stream's M3U account is not in the allowed list, skip this pattern
                                continue
                        
                        # SAFETY CHECK: If match_by_tvg_id is enabled, IGNORE catch-all regexes
                        # This prevents the issue where a lingering ".*" causes unwanted matches
                        # despite the user enabling TVG matching.
                        if match_by_tvg:
                            is_catch_all = pattern == ".*" or pattern == "^.*$" or pattern == ".+" or pattern == "^.+$"
                            if is_catch_all:
                                # logger.debug(f"Ignoring catch-all regex '{pattern}' for channel {channel_id} because match_by_tvg_id is enabled")
                                continue
                        
                        try:
                            compiled_pattern = _compile_stream_search_regex(pattern, channel_name, case_sensitive)
                            if compiled_pattern.search(search_name):
                                matches.append(channel_id)
                                matched_channel = True
                                regex_matched = True
                                # logger.debug(f"Stream '{stream_name}' matched channel {channel_id} with pattern '{pattern}'")
                                break  # Only match once per channel
                        except re.error as e:
                            logger.error(f"Invalid regex pattern '{pattern}' for channel {channel_id}: {e}")
                    
                    if regex_matched:
                        break  # Skip other match types for this channel
        
        return matches
    
    def match_stream_to_channels_with_priority(self, stream_name: str, stream_m3u_account: Optional[int] = None,
                                             stream_tvg_id: Optional[str] = None, channel_tvg_ids: Optional[Dict[str, str]] = None,
                                             channel_match_priorities: Optional[Dict[str, List[str]]] = None,
                                             channel_to_group_map: Optional[Dict[str, Any]] = None,
                                             channel_name_map: Optional[Dict[str, str]] = None) -> List[Dict[str, Any]]:
        """Match a stream name and optionally TVG-ID to channels using regex patterns with priority.
        
        Args:
            stream_name: The name of the stream to match
            stream_m3u_account: The ID of the M3U account the stream belongs to (optional)
            stream_tvg_id: The TVG-ID of the stream (optional)
            channel_tvg_ids: Dictionary mapping channel_id -> tvg_id (optional)
            channel_match_priorities: Dictionary mapping channel_id -> priority order list (optional)
            
        Returns:
            List of dictionaries containing channel_id and priority
        """
        matches = []
        case_sensitive = self.channel_patterns.get("global_settings", {}).get("case_sensitive", True)
        
        search_name = stream_name if case_sensitive else stream_name.lower()
        
        channel_to_group_map = channel_to_group_map or {}
        channel_name_map = channel_name_map or {}

        with self.lock:
            explicit_channel_ids = set(self.channel_patterns.get("patterns", {}).keys())
            if channel_tvg_ids:
                explicit_channel_ids.update(str(cid) for cid in channel_tvg_ids.keys())
            if channel_to_group_map:
                explicit_channel_ids.update(str(cid) for cid in channel_to_group_map.keys())

            for channel_id in explicit_channel_ids:
                group_id = channel_to_group_map.get(str(channel_id))
                config = self._get_effective_channel_config(channel_id, group_id)
                if not isinstance(config, dict) or not config:
                    continue
                
                matched = False
                match_source = "regex"
                
                # Determine priority order
                priority_order = ['tvg', 'regex']
                if channel_match_priorities:
                    mapped_order = channel_match_priorities.get(str(channel_id))
                    if mapped_order:
                        priority_order = mapped_order
                        
                for match_type in priority_order:
                    if match_type == 'tvg':
                        # Check for TVG-ID match if enabled and not already matched
                        if not matched and stream_tvg_id and channel_tvg_ids and config.get("match_by_tvg_id", False):
                            channel_tvg_id = channel_tvg_ids.get(str(channel_id))
                            if channel_tvg_id and stream_tvg_id == channel_tvg_id:
                                matched = True
                                match_source = "tvg_id"
                                priority = 0
                                break
                                
                    elif match_type == 'regex':
                        if matched:
                            continue
                            
                        if not config.get("enabled", True):
                            continue
                        
                        channel_name = channel_name_map.get(str(channel_id)) or config.get("name", "")
                        match_by_tvg = config.get("match_by_tvg_id", False)
                        
                        # Support both new format (regex_patterns) and old format (regex) for backward compatibility
                        regex_patterns = config.get("regex_patterns")
                        if regex_patterns is None:
                            # Fallback to old format
                            old_regex = config.get("regex", [])
                            old_m3u_accounts = config.get("m3u_accounts")
                            regex_patterns = [{"pattern": p, "m3u_accounts": old_m3u_accounts} for p in old_regex]
                        
                        regex_matched = False
                        best_regex_priority = 0
                        
                        for pattern_obj in regex_patterns:
                            # Handle both dict and string patterns for flexibility
                            if isinstance(pattern_obj, dict):
                                pattern = pattern_obj.get("pattern", "")
                                pattern_m3u_accounts = pattern_obj.get("m3u_accounts")
                            else:
                                # Legacy string format
                                pattern = pattern_obj
                                pattern_m3u_accounts = None
                            
                            if not pattern:
                                continue
                            
                            # Check if this regex pattern applies to the stream's M3U account
                            if pattern_m3u_accounts is not None and len(pattern_m3u_accounts) > 0:
                                # Pattern is limited to specific M3U accounts
                                if stream_m3u_account is None or stream_m3u_account not in pattern_m3u_accounts:
                                    # Stream's M3U account is not in the allowed list, skip this pattern
                                    continue
                            
                            # SAFETY CHECK: If match_by_tvg_id is enabled, IGNORE catch-all regexes
                            if match_by_tvg:
                                is_catch_all = pattern == ".*" or pattern == "^.*$" or pattern == ".+" or pattern == "^.+$"
                                if is_catch_all:
                                    continue

                            try:
                                compiled_pattern = _compile_stream_search_regex(pattern, channel_name, case_sensitive)
                            except re.error:
                                continue

                            if compiled_pattern.search(search_name):
                                regex_matched = True
                                # Only match once per channel
                                break
                                
                        if regex_matched:
                            matched = True
                            match_source = "regex"
                            priority = 0
                            break

            
                if matched:
                    matches.append({
                        "channel_id": channel_id,
                        "priority": priority,
                        "source": match_source
                    })
    
        return matches

    def set_match_by_tvg_id(self, channel_id: Union[str, int], enabled: bool) -> bool:
        """Enable or disable matching by TVG-ID for a channel."""
        from apps.database.manager import get_db_manager
        db = get_db_manager()

        with self.lock:
            channel_id = str(channel_id)
            if "patterns" not in self.channel_patterns:
                self.channel_patterns["patterns"] = {}
                
            if channel_id not in self.channel_patterns["patterns"]:
                self.channel_patterns["patterns"][channel_id] = {
                    "regex_patterns": [],
                    "match_by_tvg_id": enabled,
                    "name": "",
                    "enabled": True,
                }
            else:
                self.channel_patterns["patterns"][channel_id]["match_by_tvg_id"] = enabled

        db.update_channel_regex_tvg_id(str(channel_id), enabled)
        return True

    def get_match_by_tvg_id(self, channel_id: Union[str, int], group_id: Optional[Union[str, int]] = None) -> bool:
        """Check if matching by TVG-ID is enabled for a channel."""
        with self.lock:
            effective = self._get_effective_channel_config(channel_id, group_id)
            if isinstance(effective, dict):
                return effective.get("match_by_tvg_id", False)
            return False
    
    def get_patterns(self) -> Dict:
        """Get current patterns configuration (in-memory snapshot)."""
        return self.channel_patterns
    
    def has_regex_patterns(self, channel_id: str, group_id: Optional[Union[str, int]] = None) -> bool:
        """Check if a channel has regex patterns configured and enabled.
        
        A channel is considered to have regex patterns if:
        1. The channel exists in the patterns configuration
        2. The pattern configuration is enabled (enabled=True)
        3. The regex list is non-empty
        
        Args:
            channel_id: Channel ID to check
            
        Returns:
            True if the channel has at least one enabled regex pattern, False otherwise
        """
        channel_config = self._get_effective_channel_config(channel_id, group_id)
        if not channel_config:
            return False
        
        # Check if the pattern is enabled
        if not channel_config.get("enabled", True):
            return False
        
        # Check if there are any regex patterns (support both old and new format)
        regex_patterns = channel_config.get("regex_patterns")
        if regex_patterns is None:
            # Fallback to old format
            regex_patterns = channel_config.get("regex", [])
        
        return isinstance(regex_patterns, list) and len(regex_patterns) > 0
    
    def get_channel_regex_filter(self, channel_id: str, default: str = ".*", group_id: Optional[Union[str, int]] = None) -> Optional[str]:
        """Get the combined regex filter for a channel for stream name matching.
        
        Combines all enabled regex patterns for the channel into a single OR pattern.
        Returns `default` (standard ".*") if no patterns are configured or channel is disabled.
        
        Args:
            channel_id: Channel ID to get regex filter for
            default: Default value to return if no patterns found (default: ".*")
            
        Returns:
            Combined regex pattern string (e.g., '(pattern1|pattern2|pattern3)')
        """
        channel_config = self._get_effective_channel_config(channel_id, group_id)
        if not channel_config:
            return default
        
        # Check if the pattern is enabled
        if not channel_config.get("enabled", True):
            return default
        
        # Get regex patterns (support both old and new format)
        regex_patterns = channel_config.get("regex_patterns")
        if regex_patterns is None:
            # Fallback to old format
            regex_patterns = channel_config.get("regex", [])
        
        if not isinstance(regex_patterns, list) or len(regex_patterns) == 0:
            return default
        
        # Extract pattern strings from objects (new format) or use directly (old format)
        pattern_strings = []
        for pattern_obj in regex_patterns:
            if isinstance(pattern_obj, dict):
                pattern = pattern_obj.get('pattern', '')
            else:
                # Legacy format
                pattern = pattern_obj
            
            if pattern and isinstance(pattern, str):
                pattern_strings.append(pattern)
        
        if not pattern_strings:
            return default
        
        # If only one pattern, return it directly
        if len(pattern_strings) == 1:
            return pattern_strings[0]
        
        # Combine multiple patterns with OR
        # Each pattern is wrapped in a non-capturing group for safety
        combined = '|'.join(f'(?:{p})' for p in pattern_strings)
        return f'({combined})'

    def get_channel_match_config(self, channel_id: str, group_id: Optional[Union[str, int]] = None) -> Dict[str, Any]:
        """Get the matching configuration for a channel.
        
        Args:
            channel_id: Channel ID
            
        Returns:
            Dictionary with matching configuration (match_by_tvg_id, enabled, etc.)
        """
        channel_config = self._get_effective_channel_config(channel_id, group_id) or {}
        return {
            "match_by_tvg_id": channel_config.get("match_by_tvg_id", False),
            "enabled": channel_config.get("enabled", True),
            "name": channel_config.get("name", "")
        }


class AutomatedStreamManager:
    """Main automated stream management system."""
    
    def __init__(self, config_file=None):
        if config_file is None:
            config_file = CONFIG_DIR / "automation_config.json"
        self.config_file = Path(config_file)
        self.config = self._load_config()
        self.changelog = ChangelogManager()
        self.regex_matcher = RegexChannelMatcher()
        
        # Initialize dead streams tracker
        self.dead_streams_tracker = None
        if DEAD_STREAMS_TRACKER_AVAILABLE:
            try:
                self.dead_streams_tracker = DeadStreamsTracker()
                logger.info("Dead streams tracker initialized")
            except Exception as e:
                logger.warning(f"Failed to initialize dead streams tracker: {e}")
        
        self.running = False
        self.state_file = CONFIG_DIR / "automation_state.json"
        self.last_playlist_update = None
        self.period_last_run = self._load_state()  # Tracks last run time per period ID
        self.automation_start_time = None
        
        # Cache for M3U accounts to avoid redundant API calls within a single automation cycle
        # This is cleared after each cycle completes
        self._m3u_accounts_cache = None
        
        # Cache for dead stream removal setting to avoid repeated file I/O
        self._dead_stream_removal_enabled_cache = None
        
        # Background thread management
        self.automation_thread = None
        self.automation_running = False
        self.automation_wake_event = threading.Event()
        self.force_next_run = False
        self.forced_period_id = None
        self._dead_stream_removal_cache_time = None
        
        # Lock to prevent concurrent execution of heavy batch processes
        self._lock = threading.Lock()
    
    def _load_state(self) -> Dict[str, datetime]:
        """Load persisted automation state from file."""
        from apps.database.connection import get_session
        from apps.database.models import SystemSetting
        state = None
        try:
            session = get_session()
            setting = session.query(SystemSetting).filter(SystemSetting.key == 'automation_state').first()
            if setting and setting.value:
                state = setting.value
            else:
                raise FileNotFoundError("No automation state found in DB")
            
            # Convert stored ISO strings back to datetime objects
            loaded_runs = state.get('period_last_run', {})
            parsed_runs = {}
            for pid, iso_str in loaded_runs.items():
                try:
                    parsed_runs[pid] = datetime.fromisoformat(iso_str)
                except (ValueError, TypeError):
                    pass
            
            if parsed_runs:
                logger.info(f"Loaded {len(parsed_runs)} period last-run timestamps from state file")
            return parsed_runs
        except (Exception):
            logger.warning(f"Could not load state from DB, starting fresh")
        finally:
            try: session.close()
            except: pass
        return {}

    def _save_state(self):
        """Save current automation state to SQL."""
        from apps.database.connection import get_session
        from apps.database.models import SystemSetting
        try:
            # Convert datetime objects to ISO strings for JSON serialization
            serializable_runs = {
                pid: dt.isoformat() 
                for pid, dt in self.period_last_run.items() 
                if isinstance(dt, datetime)
            }
            state = {'period_last_run': serializable_runs}
            
            session = get_session()
            setting = session.query(SystemSetting).filter(SystemSetting.key == 'automation_state').first()
            if not setting:
                setting = SystemSetting(key='automation_state', value=state)
                session.add(setting)
            else:
                from sqlalchemy.orm.attributes import flag_modified
                setting.value = state
                flag_modified(setting, "value")
            session.commit()
            session.close()
        except Exception as e:
            logger.error(f"Failed to save automation state: {e}")

    
    def _load_config(self) -> Dict:
        """Load automation configuration from SQL."""
        from apps.database.connection import get_session
        from apps.database.models import SystemSetting
        try:
            session = get_session()
            setting = session.query(SystemSetting).filter(SystemSetting.key == 'automation_config').first()
            if setting and setting.value:
                return setting.value
        except Exception as e:
            logger.error(f"Failed to load automation config: {e}")
        finally:
            try: session.close()
            except: pass
        
        # Default configuration
        default_config = {
            "playlist_update_interval_minutes": 5,
            "playlist_update_cron": "",
            "enabled_m3u_accounts": [],
            "autostart_automation": False,
            "enabled_features": {
                "auto_playlist_update": True,
                "auto_stream_discovery": True,
                "changelog_tracking": True
            },
            "validate_existing_streams": False,
            "verify_stream_assignments": False
        }
        
        self._save_config(default_config)
        return default_config
    
    def _save_config(self, config: Dict):
        """Save configuration to SQL."""
        from apps.database.connection import get_session
        from apps.database.models import SystemSetting
        try:
            session = get_session()
            setting = session.query(SystemSetting).filter(SystemSetting.key == 'automation_config').first()
            if not setting:
                setting = SystemSetting(key='automation_config', value=config)
                session.add(setting)
            else:
                from sqlalchemy.orm.attributes import flag_modified
                setting.value = config
                flag_modified(setting, "value")
            session.commit()
        except Exception as e:
            logger.error(f"Failed to save automation config: {e}")
            if 'session' in locals():
                session.rollback()
        finally:
            if 'session' in locals():
                session.close()
    
    def update_config(self, updates: Dict):
        """Update configuration with new values and apply immediately."""
        # Log what's being updated
        config_changes = []
        
        if 'playlist_update_interval_minutes' in updates:
            old_interval = self.config.get('playlist_update_interval_minutes', 5)
            new_interval = updates['playlist_update_interval_minutes']
            if old_interval != new_interval:
                config_changes.append(f"Playlist update interval: {old_interval} → {new_interval} minutes")
        
        if 'enabled_features' in updates:
            old_features = self.config.get('enabled_features', {})
            new_features = updates['enabled_features']
            for feature, enabled in new_features.items():
                old_value = old_features.get(feature, True)
                if old_value != enabled:
                    status = "enabled" if enabled else "disabled"
                    config_changes.append(f"{feature}: {status}")
        
        if 'enabled_m3u_accounts' in updates:
            old_accounts = self.config.get('enabled_m3u_accounts', [])
            new_accounts = updates['enabled_m3u_accounts']
            if old_accounts != new_accounts:
                if not new_accounts:
                    config_changes.append("M3U accounts: all enabled")
                else:
                    config_changes.append(f"M3U accounts: {len(new_accounts)} selected")
        
        # Apply the configuration update
        self.config.update(updates)
        self._save_config(self.config)
        
        # Log the changes
        if config_changes:
            logger.info(f"Automation configuration updated: {'; '.join(config_changes)}")
            logger.info("Changes will take effect on next scheduled operation")
        else:
            logger.info("Automation configuration updated")
    
    def _is_dead_stream_removal_enabled(self) -> bool:
        """Check if dead stream removal is enabled in stream checker config.
        
        Uses a 60-second cache to avoid repeated DB queries.
        
        Returns:
            True if dead stream removal is enabled, False otherwise
        """
        import time
        current_time = time.time()
        
        # Check if cache is still valid (60 seconds)
        if (self._dead_stream_removal_cache_time is not None and 
            current_time - self._dead_stream_removal_cache_time < 60 and
            self._dead_stream_removal_enabled_cache is not None):
            return self._dead_stream_removal_enabled_cache
        
        try:
            from apps.database.manager import get_db_manager
            config = get_db_manager().get_system_setting('stream_checker_config', {})
            if config:
                enabled = config.get('dead_stream_handling', {}).get('enabled', True)
            else:
                enabled = True
            
            # Update cache
            self._dead_stream_removal_enabled_cache = enabled
            self._dead_stream_removal_cache_time = current_time
            return enabled
        except Exception as e:
            logger.error(f"Error reading stream checker config from DB: {e}")
            return True
    
    def _filter_channels_by_profile(self, all_channels: List[Dict], action_description: str) -> List[Dict]:
        """Filter channels by selected profile if one is configured.
        
        Args:
            all_channels: List of all channels from UDI
            action_description: Description of the action (e.g., "stream assignment", "stream validation")
                               Used in log messages to provide context
        
        Returns:
            Filtered list of channels. If no profile is selected or an error occurs,
            returns the original list.
        """
        # Legacy profile filtering is deprecated in favor of granular per-channel automation profiles.
        # This method is kept for backward compatibility but now returns all channels,
        # letting the specific logic (assignment/validation) handle per-channel profiles.
        return all_channels
    
    def refresh_playlists(self, force: bool = False, account_id: Optional[int] = None, skip_changelog: bool = False) -> Tuple[bool, List[Dict]]:
        """Refresh M3U playlists and track changes.
        
        Args:
            force: If True, bypass the auto_playlist_update feature flag check.
                   Used for manual/quick action triggers from the UI.
            account_id: Optional ID of specific account to refresh. If None, refreshes all enabled accounts.
            
        Returns:
            Tuple of (success_bool, refreshed_accounts_list)
        """
        refreshed_accounts = []
        refresh_failed = False
        try:
            if not force and not self.config.get("enabled_features", {}).get("auto_playlist_update", True):
                if not force:  # Allow force to override feature flag
                    logger.info("Playlist update is disabled in configuration")
                    return False, []
            
            logger.info("Starting M3U playlist refresh...")
            
            # Get streams before refresh
            from apps.core.api_utils import get_streams
            streams_before = get_streams(log_result=False) if self.config.get("enabled_features", {}).get("changelog_tracking", True) else []
            before_stream_ids = {s.get('id'): s.get('name', '') for s in streams_before if isinstance(s, dict) and s.get('id')}
            
            # Get all M3U accounts
            all_accounts = get_m3u_accounts()
            self._m3u_accounts_cache = all_accounts
            logger.debug(f"M3U accounts fetched from UDI cache: {len(all_accounts) if all_accounts else 0} accounts")
            
            if all_accounts:
                # Filter out "custom" account and non-active accounts
                non_custom_accounts = [
                    acc for acc in all_accounts
                    if acc.get('name', '').lower() != 'custom' and acc.get('is_active', True)
                ]
                
                # Determine which accounts to refresh
                accounts_to_process = []
                
                if account_id is not None:
                    # Refresh specific account
                    target_account = next((a for a in non_custom_accounts if a.get('id') == account_id), None)
                    if target_account:
                        accounts_to_process = [target_account]
                    else:
                        logger.warning(f"Requested refresh for account {account_id}, but it was not found or is inactive/custom.")
                else:
                    # Refresh all (or filtered by enabled_m3u_accounts config)
                    enabled_accounts = self.config.get("enabled_m3u_accounts", [])
                    if enabled_accounts:
                         # Filter to only enabled accounts
                         accounts_to_process = [a for a in non_custom_accounts if a.get('id') in enabled_accounts]
                    else:
                         # All non-custom active accounts
                         accounts_to_process = non_custom_accounts

                # Execute refresh
                for account in accounts_to_process:
                    acc_id = account.get('id')
                    if acc_id is not None:
                        logger.info(f"Refreshing M3U account {acc_id}: {account.get('name')}")
                        response = refresh_m3u_playlists(account_id=acc_id)
                        if self._is_m3u_refresh_response_success(response):
                            refreshed_accounts.append({
                                "id": acc_id,
                                "name": account.get('name', f"Account {acc_id}")
                            })
                        else:
                            refresh_failed = True
                            status = getattr(response, 'status_code', None)
                            logger.error(
                                f"M3U refresh failed for account {acc_id} ({account.get('name')}), "
                                f"status={status}"
                            )
                
                if not accounts_to_process:
                    logger.info("No accounts matched criteria for refresh.")
            else:
                # Fallback: if we can't get accounts, refresh all (legacy behavior)
                logger.warning("Could not fetch M3U accounts, refreshing all as fallback")
                response = refresh_m3u_playlists()
                if not self._is_m3u_refresh_response_success(response):
                    refresh_failed = True
                    status = getattr(response, 'status_code', None)
                    logger.error(f"Fallback M3U refresh failed, status={status}")

            if refresh_failed:
                logger.error("Playlist refresh encountered one or more failed account refreshes")
                return False, refreshed_accounts
            
            # NOTE: UDI refresh is intentionally decoupled from playlist refresh.
            # Automation cycles perform a single dedicated UDI refresh step to avoid
            # duplicate refreshes when playlist updates are part of the cycle.
            
            # Trigger EPG matching to pick up any EPG/tvg-id changes made in Dispatcharr
            # This ensures that if a channel's EPG assignment was changed in Dispatcharr,
            # the new program data will be available in StreamFlow
            try:
                logger.info("Triggering auto-create rule matching after playlist update...")
                from apps.automation.scheduling_service import get_scheduling_service
                scheduling_service = get_scheduling_service()
                # Force matching to bypass cache and get fresh EPG data
                scheduling_service.match_programs_to_rules()
                logger.info("Rule matching completed successfully")
            except Exception as e:
                logger.error(f"Error triggering rule matching after playlist update: {e}")
                # Continue even if EPG refresh fails
            
            # Get streams after refresh - log this one since it shows the final result
            streams_after = get_streams(log_result=True) if self.config.get("enabled_features", {}).get("changelog_tracking", True) else []
            after_stream_ids = {s.get('id'): s.get('name', '') for s in streams_after if isinstance(s, dict) and s.get('id')}
            
            self.last_playlist_update = datetime.now()
            
            # Calculate differences
            added_stream_ids = set(after_stream_ids.keys()) - set(before_stream_ids.keys())
            removed_stream_ids = set(before_stream_ids.keys()) - set(after_stream_ids.keys())
            
            added_streams = [{"id": sid, "name": after_stream_ids[sid]} for sid in added_stream_ids]
            removed_streams = [{"id": sid, "name": before_stream_ids[sid]} for sid in removed_stream_ids]
            
            
            if not skip_changelog and self.config.get("enabled_features", {}).get("changelog_tracking", True):
                self.changelog.add_entry("playlist_refresh", {
                    "success": True,
                    "timestamp": self.last_playlist_update.isoformat(),
                    "total_streams": len(after_stream_ids),
                    "added_streams": added_streams[:50],  # Limit to first 50 for changelog size
                    "removed_streams": removed_streams[:50],  # Limit to first 50 for changelog size
                    "added_count": len(added_streams),
                    "removed_count": len(removed_streams)
                })
            
            logger.info(f"M3U playlist refresh completed successfully. Added: {len(added_streams)}, Removed: {len(removed_streams)}")
            
            # Clean up dead streams that are no longer in the playlist
            if self.dead_streams_tracker:
                try:
                    current_stream_urls = {s.get('url', '') for s in streams_after if isinstance(s, dict) and s.get('url')}
                    # Remove empty URLs from the set
                    current_stream_urls.discard('')
                    cleaned_count = self.dead_streams_tracker.cleanup_removed_streams(current_stream_urls)
                    if cleaned_count > 0:
                        logger.info(f"Dead streams cleanup: removed {cleaned_count} stream(s) no longer in playlist")
                except Exception as cleanup_error:
                    logger.error(f"Error during dead streams cleanup: {cleanup_error}")
            
            # Note: Channel marking for stream quality checking is handled in discover_and_assign_streams()
            # after streams are actually assigned to specific channels. This prevents marking all channels
            # when we only know that *some* streams changed in the playlist, not which channels are affected.
            
            return True, refreshed_accounts
            
        except Exception as e:
            logger.error(f"Failed to refresh M3U playlists: {e}")
            
            
            if not skip_changelog and self.config.get("enabled_features", {}).get("changelog_tracking", True):
                self.changelog.add_entry("playlist_refresh", {
                    "success": False,
                    "error": str(e),
                    "timestamp": datetime.now().isoformat()
                })
            
            return False, []

    def _is_m3u_refresh_response_success(self, response: Any) -> bool:
        """Validate M3U refresh API responses.

        Accepts mock responses used in tests. If no status code is present,
        assume success to preserve compatibility with simplified mocks.
        """
        if response is None:
            return False

        status_code = getattr(response, 'status_code', None)
        if status_code is None:
            return True

        try:
            code = int(status_code)
        except (TypeError, ValueError):
            return False

        return 200 <= code < 300

    def _should_abort_for_suspicious_stream_pool(self, before_count: int, after_count: int, playlists_refreshed: bool) -> bool:
        """Return True when the post-refresh stream pool looks unsafe.

        Safety checks apply only when playlists were refreshed in this cycle.
        """
        if not playlists_refreshed:
            return False

        if before_count <= 0:
            return False

        if after_count <= 0:
            logger.error(
                "Aborting matching: stream pool is empty after playlist refresh "
                f"(before={before_count}, after={after_count})"
            )
            return True

        safety_cfg = self.config.get('automation_safety', {}) if isinstance(self.config, dict) else {}
        min_ratio = safety_cfg.get('min_stream_pool_ratio_after_refresh', 0.5)
        min_drop = safety_cfg.get('min_stream_pool_drop_after_refresh', 100)

        try:
            min_ratio = max(0.0, min(1.0, float(min_ratio)))
        except (TypeError, ValueError):
            min_ratio = 0.5

        try:
            min_drop = max(1, int(min_drop))
        except (TypeError, ValueError):
            min_drop = 100

        drop = before_count - after_count
        ratio = after_count / before_count

        if drop >= min_drop and ratio < min_ratio:
            logger.error(
                "Aborting matching: suspicious stream pool drop after playlist refresh "
                f"(before={before_count}, after={after_count}, drop={drop}, ratio={ratio:.3f}, "
                f"threshold_ratio={min_ratio}, threshold_drop={min_drop})"
            )
            return True

        return False
    def _match_streams_batch(self, streams: List[Dict], channel_streams: Dict[str, set],
                             dead_stream_removal_enabled: bool,
                             channel_to_revive_enabled: Dict[str, bool] = None,
                             channel_tvg_map: Dict[str, str] = None,
                             channel_to_match_priorities: Dict[str, List[str]] = None,
                             channel_to_group_map: Dict[str, Any] = None,
                             channel_name_map: Dict[str, str] = None) -> Tuple[Dict[str, List[str]], Dict[str, List[Dict]]]:
        """
        Process a batch of streams for regex matching.
        This method is designed to be run in a separate thread.
        
        Args:
            streams: List of stream dictionaries to process
            channel_streams: Dict of existing channel streams {channel_id: {stream_ids}}
            dead_stream_removal_enabled: Whether to skip dead streams
            channel_to_revive_enabled: Mapping of channel IDs to their Stream Revival setting
            channel_tvg_map: Mapping of channel IDs to their TVG-ID (optional)
            channel_to_match_priorities: Mapping of channel IDs to priority order (optional)
            
        Returns:
            Tuple of (assignments, assignment_details)
        """
        assignments = defaultdict(list)
        assignment_details = defaultdict(list)
        channel_to_revive_enabled = channel_to_revive_enabled or {}
        channel_tvg_map = channel_tvg_map or {}
        channel_to_match_priorities = channel_to_match_priorities or {}
        channel_to_group_map = channel_to_group_map or {}
        channel_name_map = channel_name_map or {}
        match_stream_to_channels = self.regex_matcher.match_stream_to_channels

        # Cache match outcomes for repeated stream signatures inside this batch.
        stream_match_cache: Dict[Tuple[str, Any, Optional[str]], Tuple[str, ...]] = {}
        
        for stream in streams:
            # Validate that stream is a dictionary before accessing attributes
            if not isinstance(stream, dict):
                continue
                
            stream_name = stream.get('name', '')
            stream_id = stream.get('id')
            
            if not stream_name or not stream_id:
                continue
            
            # Get stream's url and m3u_account
            stream_url = stream.get('url', '')
            stream_m3u_account = stream.get('m3u_account')
            stream_tvg_id = stream.get('tvg_id')
            match_cache_key = (stream_name, stream_m3u_account, stream_tvg_id)
            
            matching_channels = stream_match_cache.get(match_cache_key)
            if matching_channels is None:
                matching_channels = tuple(match_stream_to_channels(
                    stream_name,
                    stream_m3u_account,
                    stream_tvg_id,
                    channel_tvg_map,
                    channel_to_match_priorities,
                    channel_to_group_map,
                    channel_name_map,
                ))
                stream_match_cache[match_cache_key] = matching_channels

            if not matching_channels:
                continue

            # Check if any matching channel allows reviving dead streams
            any_revive_enabled = False
            for ch_id in matching_channels:
                if channel_to_revive_enabled.get(str(ch_id), False):
                    any_revive_enabled = True
                    break

            # Skip dead streams if removal is enabled globally
            if self.dead_streams_tracker and self.dead_streams_tracker.is_dead(stream_url):
                if dead_stream_removal_enabled:
                    # If any matched channel has Stream Revival enabled, we DON'T skip it, 
                    # even if it's offline. We want it added so the checker can re-evaluate it.
                    if any_revive_enabled:
                        # logger.debug(f"Allowing dead stream {stream_id} for potential revival")
                        pass
                    else:
                        # Revival is disabled for all matching channels - skip ALL dead streams
                        # This prevents the continuous re-addition loop for low quality/failed streams
                        # logger.debug(f"Skipping dead stream {stream_id} (revival disabled)")
                        continue
            
            for channel_id in matching_channels:
                # Check if stream is already in this channel
                if channel_id in channel_streams and stream_id not in channel_streams[channel_id]:
                    assignments[channel_id].append(stream_id)
                    assignment_details[channel_id].append({
                        "stream_id": stream_id,
                        "stream_name": stream_name
                    })
                    
        return assignments, assignment_details

    def _validate_channels_batch(self, channels: List[Dict], stream_lookup: Dict[int, Dict], 
                               matching_enabled_channel_ids: List[str],
                               channel_validation_settings: Dict[str, Dict] = None,
                               full_channel_tvg_map: Dict[str, str] = None) -> Dict[str, Any]:
        """
        Process a batch of channels for stream validation.
        This method is designed to be run in a separate thread.
        
        Args:
            channels: List of channel dictionaries to process
            stream_lookup: Dict of all streams {stream_id: stream_data}
            matching_enabled_channel_ids: List of channel IDs where matching is enabled
            channel_validation_settings: Dict of channel ID -> validation settings
            full_channel_tvg_map: Dict mapping channel_id -> tvg_id for ALL channels (not just the batch)
            
        Returns:
            Dict containing partial validation results
        """
        results = {
            "channels_checked": 0,
            "streams_removed": 0,
            "channels_modified": 0,
            "details": []
        }
        
        udi = get_udi_manager() # Singleton, thread-safe access
        
        if channel_validation_settings is None:
            channel_validation_settings = {}
        
        
        # Use the full channel TVG-ID map passed in from the impl, which covers ALL channels.
        # A batch-scoped map causes false negatives for cross-batch TVG lookups (Bug 2 fix).
        channel_tvg_map = full_channel_tvg_map if full_channel_tvg_map is not None else {}
        channel_to_group_map = {}
        channel_name_map = {}
        for ch in channels:
            if not isinstance(ch, dict) or ch.get('id') is None:
                continue
            cid = str(ch.get('id'))
            channel_name_map[cid] = ch.get('name', '')
            gid = ch.get('group_id') if ch.get('group_id') is not None else ch.get('channel_group_id')
            if gid is not None:
                channel_to_group_map[cid] = gid

        if not channel_tvg_map:
            # Defensive fallback: build from the batch if no global map was provided
            for ch in channels:
                if isinstance(ch, dict) and ch.get('tvg_id'):
                    channel_tvg_map[str(ch.get('id'))] = ch.get('tvg_id')
                
        for channel in channels:
            channel_id = channel.get('id')
            channel_name = channel.get('name', f'Channel {channel_id}')
            
            # Skip channels with matching disabled
            if channel_id not in matching_enabled_channel_ids:
                continue
            
            # Skip channels without matching criteria (no regex AND no TVG-ID matching)
            group_id = channel.get('group_id') if channel.get('group_id') is not None else channel.get('channel_group_id')
            if not self.regex_matcher.has_regex_patterns(str(channel_id), group_id) and not self.regex_matcher.get_match_by_tvg_id(str(channel_id), group_id):
                continue
            
            results["channels_checked"] += 1
            
            # Get settings for this channel
            settings = channel_validation_settings.get(channel_id, {})
            validate_enabled = settings.get("validate_enabled", False)
            
            # Get streams for this channel
            channel_streams = udi.get_channel_streams(channel_id)
            if not channel_streams:
                continue
            
            streams_to_keep = []
            streams_to_remove = []
            
            for stream in channel_streams:
                if not isinstance(stream, dict) or 'id' not in stream:
                    continue
                
                stream_id = stream['id']
                stream_name_in_channel = stream.get('name', '')
                
                # Look up full stream data to get m3u_account and up-to-date name
                full_stream = stream_lookup.get(stream_id)
                if not full_stream:
                    # Stream not found in UDI, keep it safe
                    streams_to_keep.append(stream_id)
                    continue
                
                stream_name = full_stream.get('name', stream_name_in_channel)
                stream_m3u_account = full_stream.get('m3u_account')
                
                # Check Regex/TVG-ID Validity
                # Check if stream still matches this channel
                stream_tvg_id = full_stream.get('tvg_id')
                matching_channels = self.regex_matcher.match_stream_to_channels(
                    stream_name,
                    stream_m3u_account,
                    stream_tvg_id,
                    channel_tvg_map,
                    None,
                    channel_to_group_map,
                    channel_name_map,
                )
                
                if str(channel_id) in matching_channels:
                    streams_to_keep.append(stream_id)
                else:
                    streams_to_remove.append({
                        "id": stream_id, 
                        "name": stream_name,
                        "reason": "regex_mismatch"
                    })
            
            if streams_to_remove:
                results["streams_removed"] += len(streams_to_remove)
                results["channels_modified"] += 1
                results["details"].append({
                    "channel_id": channel_id,
                    "channel_name": channel_name,
                    "removed_count": len(streams_to_remove),
                    "removed_streams": streams_to_remove,
                    "kept_ids": streams_to_keep,
                    "validate_enabled": validate_enabled
                })
                
        return results

    def discover_and_assign_streams(self, force: bool = False, skip_check_trigger: bool = False, forced_period_id: Optional[str] = None, skip_changelog: bool = False, channel_id: Optional[int] = None) -> Dict[str, int]:
        """Wrapper for stream discovery to ensure single execution."""
        if not self._lock.acquire(blocking=False):
            logger.warning("Stream discovery already active - skipping concurrent request")
            return {}
        
        try:
            return self._discover_and_assign_streams_impl(force, skip_check_trigger, forced_period_id, skip_changelog, channel_id)
        finally:
            self._lock.release()

    def _discover_and_assign_streams_impl(self, force: bool = False, skip_check_trigger: bool = False, forced_period_id: Optional[str] = None, skip_changelog: bool = False, channel_id: Optional[int] = None) -> Dict[str, int]:
        """Discover new streams and assign them to channels based on regex patterns.
        
        Args:
            force: If True, bypass the auto_stream_discovery feature flag check.
                   Used for manual/quick action triggers from the UI.
            skip_check_trigger: If True, don't trigger immediate stream quality check.
                   Used when the caller will handle the check itself (e.g., check_single_channel).
            forced_period_id: Optional period ID to filter channels.
            channel_id: Optional channel ID to scope discovery to a single channel.
                        When provided, only that channel receives stream assignments and
                        all_streams is pre-filtered to globally-enabled M3U accounts
                        (same boundary as full discovery) so that streams from new or
                        previously-unassigned providers are still considered.
                        All other callers pass None (default) for full discovery.
        """
        if not force and not self.config.get("enabled_features", {}).get("auto_stream_discovery", True):
            logger.info("Stream discovery is disabled in configuration")
            return {}
        
        try:
            # Reload patterns to ensure we have the latest changes
            self.regex_matcher.reload_patterns()
            
            logger.info("Starting stream discovery and assignment...")
            
            # Get all available streams (don't log, we already logged during refresh)
            from apps.core.api_utils import get_streams
            all_streams = get_streams(log_result=False)
            if not all_streams:
                logger.warning("No streams found")
                return {}
            
            # Validate that all_streams is a list
            if not isinstance(all_streams, list):
                logger.error(f"Invalid streams response format: expected list, got {type(all_streams).__name__}")
                return {}
            
            # Filter streams by enabled M3U accounts
            # Use cached M3U accounts if available (from refresh_playlists), otherwise fetch
            # This optimization ensures M3U accounts are only queried once per playlist refresh cycle
            if self._m3u_accounts_cache is not None:
                all_accounts = self._m3u_accounts_cache
                logger.debug(f"Using cached M3U accounts from playlist refresh (no UDI/API call - {len(all_accounts) if all_accounts else 0} accounts)")
            else:
                all_accounts = get_m3u_accounts()
                logger.debug(f"Fetched M3U accounts from UDI cache (cache was empty - {len(all_accounts) if all_accounts else 0} accounts)")
            enabled_account_ids = set()
            
            if all_accounts:
                # Filter specific accounts
                non_custom_accounts = [
                    acc for acc in all_accounts
                    if acc.get('is_active', True)
                ]
                
                # Get enabled accounts from config
                enabled_accounts_config = self.config.get("enabled_m3u_accounts", [])
                
                if enabled_accounts_config:
                    # Only include accounts that are in the enabled list
                    enabled_account_ids = set(
                        acc.get('id') for acc in non_custom_accounts 
                        if acc.get('id') in enabled_accounts_config and acc.get('id') is not None
                    )
                else:
                    # If no specific accounts are enabled in config, use all non-custom active accounts
                    enabled_account_ids = set(
                        acc.get('id') for acc in non_custom_accounts 
                        if acc.get('id') is not None
                    )
                
                # Filter streams to only include those from enabled accounts
                # Also include custom streams (is_custom=True) as they don't belong to an M3U account
                filtered_streams = [
                    stream for stream in all_streams
                    if stream.get('is_custom', False) or stream.get('m3u_account') in enabled_account_ids
                ]
                
                streams_filtered_count = len(all_streams) - len(filtered_streams)
                if streams_filtered_count > 0:
                    logger.info(f"Filtered out {streams_filtered_count} streams from disabled/inactive M3U accounts")
                
                all_streams = filtered_streams
                
                if not all_streams:
                    logger.info("No streams found after filtering by enabled M3U accounts")
                    return {}
            else:
                logger.warning("Could not fetch M3U accounts, using all streams")
            
            # Get all channels from UDI
            udi = get_udi_manager()
            all_channels = udi.get_channels()
            if not all_channels:
                logger.warning("No channels found")
                return {}
            
            # Filter by profile if one is selected
            all_channels = self._filter_channels_by_profile(all_channels, "stream assignment")

            # Scope to a single channel when called from check_single_channel.
            # This avoids iterating and assigning streams for every channel in
            # the system when only one channel needs updating.
            if channel_id is not None:
                all_channels = [ch for ch in all_channels if ch.get('id') == channel_id]
                if not all_channels:
                    logger.info(f"[single-channel] Channel {channel_id} not found or not eligible for stream assignment")
                    return {}
                logger.info(f"[single-channel] Scoping stream discovery to channel {channel_id}")

                # Pre-filter all_streams to only streams from globally-enabled M3U accounts.
                # This mirrors what the full-discovery path already applied above, so the
                # stream catalogue here is identical to a normal full-discovery run —
                # it just skips iterating/assigning every other channel.
                #
                # Previously this block narrowed the catalogue to accounts the channel
                # *already* had assigned streams from, which prevented newly added M3U
                # providers (or existing providers that added the event later) from ever
                # being matched to this channel during a single-channel check.
                if enabled_account_ids:
                    pre_filter_count = len(all_streams)
                    all_streams = [
                        s for s in all_streams
                        if s.get('is_custom', False)
                        or (s.get('m3u_account_id') or s.get('m3u_account')) in enabled_account_ids
                    ]
                    logger.info(
                        f"[single-channel] Pre-filtered streams from {pre_filter_count} "
                        f"to {len(all_streams)} (enabled accounts: {enabled_account_ids})"
                    )

            # Filter channels by automation profile settings
            from apps.automation.automation_config_manager import get_automation_config_manager
            automation_config = get_automation_config_manager()
            matching_enabled_channel_ids = []
            channel_to_revive_enabled = {}
            channel_tvg_map = {}
            channel_to_match_priorities = {}
            channel_to_group_map = {}
            channel_name_map = {}
            
            
            for channel in all_channels:
                if not isinstance(channel, dict) or 'id' not in channel:
                    continue
                channel_id = channel['id']
                channel_name_map[str(channel_id)] = channel.get('name', f'Channel {channel_id}')
                channel_tvg_id = channel.get('tvg_id')
                if channel_tvg_id:
                    channel_tvg_map[str(channel_id)] = channel_tvg_id
                group_id = channel.get('group_id') if channel.get('group_id') is not None else channel.get('channel_group_id')
                if group_id is not None:
                    channel_to_group_map[str(channel_id)] = group_id
                
                # Resolve group key from either UDI shape to include group-only period assignments.
                effective_group_id = channel.get('group_id') if channel.get('group_id') is not None else channel.get('channel_group_id')
                # Get effective configuration - only channels with automation periods participate
                config = automation_config.get_effective_configuration(channel_id, effective_group_id)
                
                # Skip channels without automation periods assigned
                if not config:
                    continue
                
                # Filter by forced_period_id if provided
                selected_period_profile = None
                if forced_period_id:
                    selected_period = next(
                        (p for p in config.get('periods', []) if p.get('id') == forced_period_id),
                        None,
                    )
                    if not selected_period:
                        continue
                    selected_period_profile = selected_period.get('profile')
                    
                profile = selected_period_profile or config.get('profile')
                
                # Check if stream matching is enabled
                matching_enabled = profile and profile.get('stream_matching', {}).get('enabled', False)
                
                # Global Action Override: Include if global action affected is True and force is True
                if force and profile and profile.get('global_action', {}).get('affected', False):
                    matching_enabled = True
                
                if matching_enabled:
                    matching_enabled_channel_ids.append(channel_id)
                    # Store match priority order
                    channel_to_match_priorities[str(channel_id)] = profile.get('stream_matching', {}).get('match_priority_order', ['tvg', 'regex'])
                    
                # Check if revive is enabled
                if profile and profile.get('stream_checking', {}).get('allow_revive', False):
                    channel_to_revive_enabled[str(channel_id)] = True

            # Filter channels to only those with matching enabled
            filtered_channels = [ch for ch in all_channels if ch.get('id') in matching_enabled_channel_ids]
            
            excluded_count = len(all_channels) - len(filtered_channels)
            if excluded_count > 0:
                logger.info(f"Excluding {excluded_count} channel(s) without automation periods or with matching disabled from stream assignment")
            
            # Use filtered channels for the rest of the logic
            all_channels = filtered_channels
            
            # Exclude channels in active monitoring sessions (coordination with monitoring system)
            from apps.stream.stream_session_manager import get_session_manager
            session_manager = get_session_manager()
            channels_in_monitoring = session_manager.get_channels_in_active_sessions()
            
            if channels_in_monitoring:
                pre_filter_count = len(all_channels)
                all_channels = [ch for ch in all_channels if ch.get('id') not in channels_in_monitoring]
                monitoring_excluded = pre_filter_count - len(all_channels)
                if monitoring_excluded > 0:
                    logger.info(f"⏸ Excluding {monitoring_excluded} channel(s) in active monitoring sessions from stream discovery/assignment")
            
            if not all_channels:
                logger.info("No channels available for stream assignment (all filtered or in monitoring)")
                return {}
            


            
            # Create a map of existing channel streams
            channel_streams = {}
            channel_names = {}  # Store channel names for changelog
            channel_logo_urls = {}  # Store channel logo URLs for changelog
            for channel in all_channels:
                # Validate that channel is a dictionary
                if not isinstance(channel, dict) or 'id' not in channel:
                    logger.warning(f"Invalid channel format encountered: {type(channel).__name__} - {channel}")
                    continue
                    
                channel_id = str(channel['id'])
                channel_names[channel_id] = channel.get('name', f'Channel {channel_id}')
                
                # Get logo URL for this channel
                logo_id = channel.get('logo_id')
                if logo_id:
                    channel_logo_urls[channel_id] = f"/api/logos/{logo_id}"
                
                # Get streams for this channel from UDI
                streams = udi.get_channel_streams(int(channel_id))
                if streams:
                    valid_stream_ids = set()
                    for s in streams:
                        if isinstance(s, dict) and 'id' in s:
                            valid_stream_ids.add(s['id'])
                        else:
                            logger.warning(f"Invalid stream format in channel {channel_id}: {type(s).__name__} - {s}")
                    channel_streams[channel_id] = valid_stream_ids
                else:
                    channel_streams[channel_id] = set()
            
            assignments = defaultdict(list)
            assignment_details = defaultdict(list)  # Track stream details for changelog
            assignment_count = {}
            
            # Log progress info
            total_streams = len(all_streams)
            
            # Use parallel processing for faster matching
            # Limit workers to avoid system thrashing, but scale with streams
            # For < 1000 streams: 2-4 workers is plenty
            # For 1000-5000: 4-6 workers
            # For 5000-20000: 8 workers
            # For > 20000: Up to 16 workers (if CPU permits) for massive playlists
            base_workers = os.cpu_count() or 4
            if total_streams < 1000:
                max_workers = min(4, base_workers)
            elif total_streams < 5000:
                max_workers = min(6, base_workers)
            elif total_streams < 20000:
                max_workers = min(8, base_workers)
            else:
                max_workers = min(16, base_workers)
                
            # Batch size for streams - use more work units for large playlists
            # to improve balancing across workers and reduce long-tail batches.
            if total_streams < 1000:
                batches_per_worker = 4
            elif total_streams < 5000:
                batches_per_worker = 6
            elif total_streams < 20000:
                batches_per_worker = 10
            else:
                batches_per_worker = 16

            batch_size = max(50, total_streams // max(1, (max_workers * batches_per_worker)))
            if total_streams >= 20000:
                batch_size = min(batch_size, 400)
            
            logger.info(f"Processing {total_streams} streams for pattern matching (Parallel, {max_workers} workers, {batch_size} streams per batch)...")
            
            # Get dead stream removal config once
            dead_stream_removal_enabled = self._is_dead_stream_removal_enabled()
            
            # Create batches
            batches = [all_streams[i:i + batch_size] for i in range(0, total_streams, batch_size)]
            
            completed_count = 0
            last_log_pct = -1
            
            # Process batches in parallel
            with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
                future_to_batch = {
                    executor.submit(self._match_streams_batch, batch, channel_streams, 
                                   dead_stream_removal_enabled,
                                   channel_to_revive_enabled, channel_tvg_map, channel_to_match_priorities, channel_to_group_map, channel_name_map): batch 
                    for batch in batches
                }
                
                for future in concurrent.futures.as_completed(future_to_batch):
                    try:
                        batch_assignments, batch_details = future.result()
                        
                        # Merge results
                        for channel_id, stream_ids in batch_assignments.items():
                            assignments[channel_id].extend(stream_ids)
                            
                        for channel_id, details in batch_details.items():
                            assignment_details[channel_id].extend(details)
                            
                        completed_count += len(future_to_batch[future])
                        
                        # Log progress monotonically
                        current_pct = int((completed_count / total_streams) * 100)
                        # Log every 5% for better visibility as requested
                        if current_pct >= last_log_pct + 5 or completed_count == total_streams:
                             logger.info(f"  Progress: {completed_count}/{total_streams} streams matched ({current_pct}%)")
                             last_log_pct = current_pct
                             
                    except Exception as e:
                        logger.error(f"Error in stream matching batch: {e}")
            
            logger.info(f"✓ Completed processing {total_streams} streams. Found {sum(len(s) for s in assignments.values())} new stream assignments across {len(assignments)} channels")
            
            # Get channels in active monitoring sessions to handle them separately
            from apps.stream.stream_session_manager import get_session_manager
            session_manager = get_session_manager()
            channels_in_sessions = session_manager.get_channels_in_active_sessions()
            
            if channels_in_sessions:
                logger.info(f"Found {len(channels_in_sessions)} channel(s) in active monitoring sessions - will add streams to sessions instead of direct assignment")
            
            # Prepare detailed changelog data
            detailed_assignments = []
            
            # Get dead stream removal config once for this discovery run
            dead_stream_removal_enabled = self._is_dead_stream_removal_enabled()
            
            # Assign streams to channels
            for channel_id, stream_ids in assignments.items():
                if stream_ids:
                    try:
                        channel_id_int = int(channel_id)
                        
                        # Check if channel is in an active monitoring session
                        if channel_id_int in channels_in_sessions:
                            # Add streams to the active session(s) instead of direct channel assignment
                            logger.info(f"Channel {channel_id} is in an active monitoring session - adding {len(stream_ids)} streams to session(s)")
                            added_to_session = 0
                            
                            # Find all active sessions for this channel
                            active_sessions = [s for s in session_manager.get_active_sessions() if s.channel_id == channel_id_int]
                            
                            for session in active_sessions:
                                for stream_id in stream_ids:
                                    if session_manager.add_stream_to_session(session.session_id, stream_id):
                                        added_to_session += 1
                            
                            if added_to_session > 0:
                                logger.info(f"Added {added_to_session} new streams to monitoring session(s) for channel {channel_id}")
                                assignment_count[channel_id] = added_to_session
                                
                                # Prepare detailed assignment info
                                channel_assignment = {
                                    "channel_id": channel_id,
                                    "channel_name": channel_names.get(channel_id, f'Channel {channel_id}'),
                                    "logo_url": channel_logo_urls.get(channel_id),
                                    "stream_count": added_to_session,
                                    "streams": assignment_details[channel_id][:20],  # Limit to first 20 for changelog
                                    "added_to_session": True
                                }
                                detailed_assignments.append(channel_assignment)
                            continue
                        
                        # Normal channel assignment (not in session)
                        added_count = add_streams_to_channel(channel_id_int, stream_ids, allow_dead_streams=(not dead_stream_removal_enabled))
                        assignment_count[channel_id] = added_count
                        
                        # Verify streams were added correctly (if enabled in config)
                        verify_enabled = self.config.get('verify_stream_assignments', False)
                        if added_count > 0 and verify_enabled:
                            try:
                                time.sleep(0.5)  # Brief delay for API processing
                                # Refresh this specific channel in UDI to get updated data after write
                                udi.refresh_channel_by_id(int(channel_id))
                                updated_channel = udi.get_channel_by_id(int(channel_id))
                                
                                if updated_channel:
                                    updated_stream_ids = set(updated_channel.get('streams', []))
                                    expected_stream_ids = set(stream_ids)
                                    added_stream_ids = expected_stream_ids & updated_stream_ids
                                    
                                    if len(added_stream_ids) == added_count:
                                        logger.info(f"✓ Verified: {added_count} streams successfully added to channel {channel_id} ({channel_names.get(channel_id, f'Channel {channel_id}')})")
                                    else:
                                        logger.warning(f"⚠ Verification mismatch for channel {channel_id}: expected {added_count} streams, found {len(added_stream_ids)} in channel")
                                else:
                                    logger.warning(f"⚠ Could not verify stream addition for channel {channel_id}: channel not found")
                            except Exception as verify_error:
                                logger.warning(f"⚠ Could not verify stream addition for channel {channel_id}: {verify_error}")
                        elif added_count > 0:
                            logger.debug(f"Skipped verification for channel {channel_id} (disabled in config)")
                        
                        # Prepare detailed assignment info
                        channel_assignment = {
                            "channel_id": channel_id,
                            "channel_name": channel_names.get(channel_id, f'Channel {channel_id}'),
                            "logo_url": channel_logo_urls.get(channel_id),
                            "stream_count": added_count,
                            "streams": assignment_details[channel_id][:20],  # Limit to first 20 for changelog
                            "added_to_session": False
                        }
                        detailed_assignments.append(channel_assignment)
                        
                        
                    except Exception as e:
                        logger.error(f"Failed to assign streams to channel {channel_id}: {e}")
            
            # Add comprehensive changelog entry
            total_assigned = sum(assignment_count.values())
            # Add comprehensive changelog entry
            total_assigned = sum(assignment_count.values())
            if self.config.get("enabled_features", {}).get("changelog_tracking", True) and not skip_changelog:
                # Limit detailed assignments to prevent oversized changelog entries
                # Sort by stream count (descending) to show the most significant updates
                sorted_assignments = sorted(detailed_assignments, key=lambda x: x['stream_count'], reverse=True)
                max_channels_in_changelog = 50  # Limit to 50 channels to prevent performance issues
                
                self.changelog.add_entry("streams_assigned", {
                    "total_assigned": total_assigned,
                    "channel_count": len(assignment_count),
                    "assignments": sorted_assignments[:max_channels_in_changelog],
                    "has_more_channels": len(sorted_assignments) > max_channels_in_changelog,
                    "timestamp": datetime.now().isoformat()
                })
            
            logger.info(f"Stream discovery completed. Assigned {total_assigned} new streams across {len(assignment_count)} channels")
            
            # Mark channels that received new streams for stream quality checking
            if total_assigned > 0 and assignment_count:
                try:
                    # Get updated stream counts for channels that received new streams
                    channel_ids_to_mark = []
                    stream_counts = {}
                    
                    for channel_id in assignment_count.keys():
                        if assignment_count[channel_id] > 0:
                            channel_ids_to_mark.append(int(channel_id))
                            # Get current stream count from UDI
                            try:
                                channel = udi.get_channel_by_id(int(channel_id))
                                if channel:
                                    streams_list = channel.get('streams', [])
                                    stream_counts[int(channel_id)] = len(streams_list) if isinstance(streams_list, list) else 0
                            except Exception:
                                pass  # If we can't get count, marking will still work
                    
                    # Try to get stream checker service and mark channels
                    if channel_ids_to_mark:
                        try:
                            from apps.stream.stream_checker_service import get_stream_checker_service
                            stream_checker = get_stream_checker_service()
                            stream_checker.update_tracker.mark_channels_updated(channel_ids_to_mark, stream_counts=stream_counts)
                            logger.info(f"Marked {len(channel_ids_to_mark)} channels with new streams for stream quality checking")
                            # Trigger immediate check instead of waiting for scheduled interval
                            # Skip if caller will handle the check (e.g., check_single_channel)
                            if not skip_check_trigger:
                                stream_checker.trigger_check_updated_channels()
                            else:
                                logger.debug("Skipping automatic check trigger (will be handled by caller)")
                        except Exception as sc_error:
                            logger.debug(f"Stream checker not available or error marking channels: {sc_error}")
                except Exception as mark_error:
                    logger.debug(f"Could not mark channels for stream checking after discovery: {mark_error}")
            
            return {
                "assignment_count": assignment_count,
                "assignment_details": detailed_assignments,
                "assigned_stream_ids": dict(assignments)
            }
            
        except Exception as e:
            logger.error(f"Stream discovery failed: {e}")
            return {
                "assignment_count": {},
                "assignment_details": [],
                "assigned_stream_ids": {},
                "success": False,
                "error": str(e)
            }
    
    def validate_and_remove_non_matching_streams(self, force: bool = False, forced_period_id: Optional[str] = None, skip_changelog: bool = False, channel_id: Optional[int] = None) -> Dict[str, Any]:
        """
        Validate existing streams in channels against regex patterns.
        Remove streams that no longer match their channel's patterns.
        
        This function respects the automation_controls.remove_non_matching_streams setting
        unless force=True is passed. This ensures consistent behavior across:
        - Automation cycles (step 1.5 in the pipeline)
        - Single channel checks
        - Global actions
        
        Args:
            force: If True, bypass the automation_controls config check.
                   Reserved for future use or special cases where removal must happen
                   regardless of user settings. Default is False to respect user config.
            forced_period_id: Optional period ID to filter channels.
        
        Returns:
            Dict containing validation statistics:
            - channels_checked: Number of channels checked
            - streams_removed: Total streams removed
            - channels_modified: Number of channels that had streams removed
            - details: List of channel details with removed streams
        """
        log_function_call(logger, "validate_and_remove_non_matching_streams")
        
        # Check if removal is enabled in stream checker config (unless forced)
        if not force:
            try:
                from apps.stream.stream_checker_service import get_stream_checker_service
                stream_checker = get_stream_checker_service()
                removal_enabled = stream_checker.config.get('automation_controls', {}).get('remove_non_matching_streams', False)
                
                if not removal_enabled:
                    logger.debug("Stream removal is disabled in automation_controls")
                    return {
                        "channels_checked": 0,
                        "streams_removed": 0,
                        "channels_modified": 0,
                        "details": []
                    }
            except Exception as e:
                logger.warning(f"Could not check stream checker config: {e}, skipping validation")
                return {
                    "channels_checked": 0,
                    "streams_removed": 0,
                    "channels_modified": 0,
                    "details": []
                }
        
        # Lock to prevent concurrent execution
        if not self._lock.acquire(blocking=False):
            logger.warning("Stream validation already active - skipping concurrent request")
            return {
                "channels_checked": 0,
                "streams_removed": 0,
                "channels_modified": 0,
                "details": []
            }
            
        try:
            return self._validate_and_remove_non_matching_streams_impl(force, forced_period_id, skip_changelog, channel_id)
        finally:
            self._lock.release()

    def _validate_and_remove_non_matching_streams_impl(self, force: bool = False, forced_period_id: Optional[str] = None, skip_changelog: bool = False, channel_id: Optional[int] = None) -> Dict[str, Any]:
        """Core implementation of stream validation."""
        log_function_call(logger, "validate_and_remove_non_matching_streams")
        try:
            logger.info("=" * 80)
            
            udi = get_udi_manager()
            all_channels = udi.get_channels()
            
            if not all_channels:
                logger.info("No channels found")
                return False, []
            
            # Filter by profile if one is selected
            all_channels = self._filter_channels_by_profile(all_channels, "stream validation")

            # Scope to a single channel when called from check_single_channel.
            if channel_id is not None:
                all_channels = [ch for ch in all_channels if ch.get('id') == channel_id]
                if not all_channels:
                    logger.info(f"[single-channel] Channel {channel_id} not found or not eligible for stream validation")
                    return {
                        "channels_checked": 0,
                        "streams_removed": 0,
                        "channels_modified": 0,
                        "details": []
                    }
                logger.info(f"[single-channel] Scoping stream validation to channel {channel_id}")

            # Filter channels with matching enabled using Automation Profiles
            from apps.automation.automation_config_manager import get_automation_config_manager
            automation_config = get_automation_config_manager()
            
            matching_enabled_channel_ids = []
            channel_validation_settings = {}
            for channel in all_channels:
                channel_id = channel.get('id')
                channel_group_id = channel.get('group_id') if channel.get('group_id') is not None else channel.get('channel_group_id')
                
                # Get effective configuration - only channels with automation periods participate
                config = automation_config.get_effective_configuration(channel_id, channel_group_id)
                
                # Skip channels without automation periods assigned
                if not config:
                    continue
                
                # Filter by forced_period_id if provided
                selected_period_profile = None
                if forced_period_id:
                    selected_period = next(
                        (p for p in config.get('periods', []) if p.get('id') == forced_period_id),
                        None,
                    )
                    if not selected_period:
                        continue
                    selected_period_profile = selected_period.get('profile')
                        
                profile = selected_period_profile or config.get('profile')
                
                # Check if stream matching is enabled in the profile
                matching_enabled = profile and profile.get('stream_matching', {}).get('enabled', False)
                validate_enabled = profile and profile.get('stream_matching', {}).get('validate_existing_streams', False)
                
                # Global Action Override: Include if global action affected is True and force is True
                if force and profile and profile.get('global_action', {}).get('affected', False):
                    matching_enabled = True
                    validate_enabled = True # Force validation if global action dictates
                    
                if matching_enabled:
                    matching_enabled_channel_ids.append(channel_id)
                    channel_validation_settings[channel_id] = {
                        "validate_enabled": validate_enabled
                    }
            
            # Exclude channels in active monitoring sessions (coordination with monitoring system)
            from apps.stream.stream_session_manager import get_session_manager
            session_manager = get_session_manager()
            channels_in_monitoring = session_manager.get_channels_in_active_sessions()
            
            if channels_in_monitoring:
                # Filter out channels in monitoring from matching_enabled list
                pre_filter_count = len(matching_enabled_channel_ids)
                matching_enabled_channel_ids = [ch_id for ch_id in matching_enabled_channel_ids if ch_id not in channels_in_monitoring]
                monitoring_excluded = pre_filter_count - len(matching_enabled_channel_ids)
                if monitoring_excluded > 0:
                    logger.info(f"⏸ Excluding {monitoring_excluded} channel(s) in active monitoring sessions from stream validation")
            
            validation_results = {
                "channels_checked": 0,
                "streams_removed": 0,
                "channels_modified": 0,
                "details": []
            }
            
            # Get dead stream removal setting to pass to update_channel_streams
            # This ensures the setting is respected when validating streams
            dead_stream_removal_enabled = self._is_dead_stream_removal_enabled()
            
            # Get all streams from UDI for lookup
            all_streams = udi.get_streams(log_result=False)
            stream_lookup = {s['id']: s for s in all_streams if isinstance(s, dict) and 'id' in s}
            
            # Build the full TVG-ID map from ALL channels upfront so that cross-batch TVG
            # lookups inside _validate_channels_batch work correctly (Bug 2 fix).
            full_channel_tvg_map = {
                str(ch.get('id')): ch.get('tvg_id')
                for ch in all_channels
                if isinstance(ch, dict) and ch.get('tvg_id')
            }
            
            # Parallel validation for faster processing
            # Limit workers to avoid system thrashing.
            max_workers = min(8, os.cpu_count() or 4)
            # Batch size for channels - smaller than streams since per-channel work is heavier
            batch_size = max(20, len(all_channels) // (max_workers * 2))
            
            logger.info(f"Validating {len(all_channels)} channels (Parallel, {max_workers} workers)...")
            
            # Create batches
            batches = [all_channels[i:i + batch_size] for i in range(0, len(all_channels), batch_size)]
            
            completed_count = 0
            
            with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
                future_to_batch = {
                    executor.submit(self._validate_channels_batch, batch, stream_lookup,
                                    matching_enabled_channel_ids, channel_validation_settings,
                                    full_channel_tvg_map): batch 
                    for batch in batches
                }
                
                for future in concurrent.futures.as_completed(future_to_batch):
                    try:
                        batch_results = future.result()
                        
                        validation_results["channels_checked"] += batch_results["channels_checked"]
                        
                        # Process results and update UDI if needed
                        for detail in batch_results.get("details", []):
                            channel_id = detail["channel_id"]
                            channel_name = detail["channel_name"]
                            kept_ids = detail["kept_ids"]
                            removed_streams = detail["removed_streams"]
                            
                            # Only apply updates if this channel's profile has validate_existing_streams
                            # enabled, OR if this is a forced run (e.g. global action).
                            # Bug 3 fix: previously gated on dead_stream_removal_enabled which ignored
                            # the per-profile validate_enabled setting entirely.
                            channel_validate_enabled = detail.get("validate_enabled", False)
                            if channel_validate_enabled or force:
                                try:
                                    from apps.core.api_utils import update_channel_streams
                                    # Update channel with kept streams
                                    success = update_channel_streams(channel_id, kept_ids, allow_dead_streams=(not dead_stream_removal_enabled))
                                    
                                    if success:
                                        validation_results["streams_removed"] += len(removed_streams)
                                        validation_results["channels_modified"] += 1
                                        validation_results["details"].append(detail)
                                        
                                        logger.info(f"✓ Removed {len(removed_streams)} non-matching stream(s) from {channel_name}")
                                    else:
                                        logger.error(f"Failed to update channel {channel_name} after validation")
                                        
                                except Exception as update_err:
                                    logger.error(f"Failed to update channel {channel_id}: {update_err}")
                            else:
                                if len(removed_streams) > 0:
                                    # Log but don't apply — validate_existing_streams is disabled for this channel
                                    logger.debug(f"Found {len(removed_streams)} non-matching streams in channel {channel_id}, but validate_existing_streams is disabled for its profile")
                        
                        completed_count += len(future_to_batch[future])
                        # Log less frequently
                        if completed_count == len(all_channels):
                             logger.info(f"  Validation progress: 100%")
                        elif completed_count % 100 == 0:
                             logger.info(f"  Validation progress: {int(completed_count/len(all_channels)*100)}%")

                    except Exception as e:
                        logger.error(f"Error in channel validation batch: {e}")
            
            logger.info(f"Stream validation completed: Checked {validation_results['channels_checked']} channels, " +
                       f"removed {validation_results['streams_removed']} streams from {validation_results['channels_modified']} channels")
            
            # Add changelog entry if there were changes
            if validation_results['streams_removed'] > 0 and self.config.get("enabled_features", {}).get("changelog_tracking", True) and not skip_changelog:
                self.changelog.add_entry("stream_validation", {
                    "channels_checked": validation_results['channels_checked'],
                    "streams_removed": validation_results['streams_removed'],
                    "channels_modified": validation_results['channels_modified'],
                    "timestamp": datetime.now().isoformat()
                })
            
            return validation_results
            
        except Exception as e:
            logger.error(f"Stream validation failed: {e}", exc_info=True)
            return {
                "channels_checked": 0,
                "streams_removed": 0,
                "channels_modified": 0,
                "details": [],
                "error": str(e)
            }
    
    
    def _is_period_due(self, period_id: str, period_info: dict) -> bool:
        """Check if a specific period is due to run based on its schedule."""
        last_run = self.period_last_run.get(period_id)
        if not last_run:
            # If no previous run, initialize to now() so it waits for the first interval/cron schedule block
            self.period_last_run[period_id] = datetime.now()
            logger.info(f"Initialized last_run for period {period_id} to now() to wait for next schedule run")
            return False
            
        schedule = period_info.get("schedule", {})
        schedule_type = schedule.get("type", "interval")
        
        if schedule_type == "interval":
            try:
                interval_mins = int(schedule.get("value", 60))
            except ValueError:
                logger.warning(f"Invalid interval value for period {period_id}, using default 60")
                interval_mins = 60
            return datetime.now() - last_run >= timedelta(minutes=interval_mins)
        elif schedule_type == "cron" and CRONITER_AVAILABLE:
            try:
                cron = croniter(schedule.get("value"), last_run)
                next_run = cron.get_next(datetime)
                return datetime.now() >= next_run
            except Exception as e:
                logger.warning(f"Invalid cron expression for period {period_id}, falling back to default interval: {e}")
                interval_mins = 60
                return datetime.now() - last_run >= timedelta(minutes=interval_mins)
        elif schedule_type == "cron":
             logger.warning(f"croniter not available, falling back to 60m interval for period {period_id}")
             return datetime.now() - last_run >= timedelta(minutes=60)
             
        return False

    def _refresh_udi_cache_for_automation_cycle(self) -> bool:
        """Refresh all UDI entities once per automation cycle.

        This is intentionally separate from playlist refresh calls so that:
        - Automation cycles always refresh UDI even without M3U updates.
        - Cycles with M3U updates still perform only one UDI refresh.
        """
        try:
            logger.info("Refreshing UDI cache for automation cycle...")
            udi = get_udi_manager()
            udi.refresh_m3u_accounts()
            udi.refresh_streams()
            udi.refresh_channels()
            udi.refresh_channel_groups()
            udi.refresh_channel_profiles()

            logger.info("UDI cache refresh for automation cycle completed")
            return True
        except Exception as e:
            logger.error(f"Failed to refresh UDI cache for automation cycle: {e}")
            return False

    def _get_post_refresh_delay_seconds(self) -> float:
        """Return optional delay after playlist refresh before matching starts.

        Priority order:
        1) Environment variable `STREAMFLOW_POST_REFRESH_DELAY_SECONDS`
        2) Config key `post_refresh_delay_seconds`
        3) Config key `automation_tuning.post_refresh_delay_seconds`
        4) Default `0.0` (no artificial delay)
        """
        env_value = os.environ.get("STREAMFLOW_POST_REFRESH_DELAY_SECONDS")
        if env_value is not None:
            try:
                return max(0.0, float(env_value))
            except (TypeError, ValueError):
                logger.warning(
                    f"Invalid STREAMFLOW_POST_REFRESH_DELAY_SECONDS='{env_value}', using config/default"
                )

        configured_value = self.config.get("post_refresh_delay_seconds")
        if configured_value is None:
            configured_value = self.config.get("automation_tuning", {}).get("post_refresh_delay_seconds")

        if configured_value is None:
            return 0.0

        try:
            return max(0.0, float(configured_value))
        except (TypeError, ValueError):
            logger.warning(
                f"Invalid post_refresh_delay_seconds value '{configured_value}', defaulting to 0"
            )
            return 0.0
    
    def run_automation_cycle(self, forced: bool = False, forced_period_id: str = None):
        """Run one complete automation cycle with profile support."""
        # Determine if this is a forced run (manual trigger)
        # forced and forced_period_id are now passed as arguments
        if forced:
            logger.info(f"Forcing automation cycle{' for period ' + forced_period_id if forced_period_id else ''}")
            
        # 1. Check Global Automation Switch
        from apps.automation.automation_config_manager import get_automation_config_manager
        automation_config = get_automation_config_manager()
        global_settings = automation_config.get_global_settings()
        
        current_enabled = global_settings.get('regular_automation_enabled', False)
        
        # Initialize flag if missing
        if not hasattr(self, '_was_automation_enabled'):
            self._was_automation_enabled = current_enabled
            
        # If just enabled, reset period timers to wait for the next scheduled block
        if current_enabled and not self._was_automation_enabled:
            logger.info("Automation system just enabled, resetting period last run states to wait")
            self.period_last_run.clear()
            
        self._was_automation_enabled = current_enabled
        
        if not forced and not current_enabled:
            logger.debug("Regular automation is disabled globally. Skipping cycle.")
            return

        # Check if stream checking mode is active - if so, skip this cycle
        try:
            from apps.stream.stream_checker_service import get_stream_checker_service
            stream_checker = get_stream_checker_service()
            status = stream_checker.get_status()
            if status.get('stream_checking_mode', False) and not forced:
                logger.debug("Stream checking is active. Skipping automation cycle.")
                return
        except Exception as e:
            logger.debug(f"Could not check stream checking mode status: {e}")
        # Global setting for playlist updates is now period-driven
        # We don't early return here, we let the individual periods be checked below
        
        logger.debug("Starting automation cycle...")
        
        try:
            # 2. Determine which playlists to update and group channels by period
            udi = get_udi_manager()
            channels = udi.get_channels()
            active_periods = {} # {(period_id, period_name): {profile_id, profile_name, channels: []}}
            active_profile_ids = set()
            
            for channel in channels:
                channel_id = channel.get('id')
                # Resolve group key from either UDI shape to include group-only period assignments.
                effective_group_id = channel.get('group_id') if channel.get('group_id') is not None else channel.get('channel_group_id')
                # Get effective configuration - only channels with automation periods participate
                config = automation_config.get_effective_configuration(channel_id, effective_group_id)
                if config and config.get('periods'):
                    for period_info in config['periods']:
                        p_id = period_info.get('id')
                        if forced_period_id and p_id != forced_period_id:
                            continue
                            
                        # Check if the period is actually due
                        if not forced and not forced_period_id and not self._is_period_due(p_id, period_info):
                            continue
                            
                        p_name = period_info.get('name')
                        profile = period_info.get('profile')
                        profile_id = period_info.get('profile_id')
                        if profile_id is not None:
                            profile_id = str(profile_id)
                        
                        if p_id and p_name:
                            key = (p_id, p_name)
                            if key not in active_periods:
                                active_periods[key] = {
                                    'profile_id': profile_id,
                                    'profile_name': profile.get('name') if profile else "Default",
                                    'channels': []
                                }
                            active_periods[key]['channels'].append(channel)
                            if profile_id:
                                active_profile_ids.add(profile_id)
            
            if not active_periods:
                logger.debug("No channels with active automation periods found. Skipping cycle.")
                self.last_playlist_update = datetime.now()
                return
            
            channels_with_periods = sum(len(p['channels']) for p in active_periods.values())
            logger.info(f"Processing {channels_with_periods} channel assignments across {len(active_periods)} active period(s)")
            
            # Determine playlists to update
            playlists_to_update = set()
            update_all_playlists = False
            channels_to_quality_check = []
            channel_check_all_streams = {}
            
            for p_id in active_profile_ids:
                profile = automation_config.get_profile(p_id)
                if not profile: continue
                
                if profile.get('stream_checking', {}).get('enabled', False):
                    # Collect all channels for this profile that are in the active periods
                    for entry in active_periods.values():
                        if entry.get('profile_id') == p_id:
                            check_all_streams = profile.get('stream_checking', {}).get('check_all_streams', False)
                            for ch in entry['channels']:
                                ch_id = ch.get('id')
                                if ch_id is None:
                                    continue
                                channels_to_quality_check.append(ch_id)
                                if check_all_streams:
                                    channel_check_all_streams[ch_id] = True
                                elif ch_id not in channel_check_all_streams:
                                    channel_check_all_streams[ch_id] = False

                m3u_config = profile.get('m3u_update', {})
                if m3u_config.get('enabled', False):
                    pf_playlists = m3u_config.get('playlists', [])
                    if not pf_playlists:
                        update_all_playlists = True
                    else:
                        playlists_to_update.update(pf_playlists)
            
            # 3. Update Playlists
            refresh_success = False
            refreshed_accounts = []
            pre_refresh_stream_count = 0
            post_refresh_stream_count = 0
            check_results = {}
            
            start_time = datetime.now()

            try:
                pre_refresh_stream_count = len(get_streams(log_result=False) or [])
            except Exception as e:
                logger.warning(f"Could not read pre-refresh stream pool size: {e}")
                pre_refresh_stream_count = 0
            
            if update_all_playlists:
                logger.info("Updating ALL playlists (requested by one or more profiles)")
                refresh_success, refreshed_accounts = self.refresh_playlists(account_id=None, skip_changelog=True)
            elif playlists_to_update:
                logger.info(f"Updating {len(playlists_to_update)} specific playlists: {playlists_to_update}")
                refresh_success = True
                for acc_id in playlists_to_update:
                    success, accs = self.refresh_playlists(account_id=int(acc_id), skip_changelog=True)
                    if not success:
                        refresh_success = False
                    if accs:
                        refreshed_accounts.extend(accs)
            else:
                logger.info("No playlists to update based on active profile settings.")
                self.last_playlist_update = datetime.now()
                refresh_success = True
            
            validation_details = []
            assignment_details = []

            # Deduplicate while preserving order (channels may appear in multiple active period groups).
            channels_to_quality_check = list(dict.fromkeys(channels_to_quality_check))

            # Refresh UDI once per cycle, independent of playlist refresh selection.
            # This keeps channel/stream/group/profile state current without coupling
            # cache sync to the M3U refresh call path.
            self._refresh_udi_cache_for_automation_cycle()

            try:
                post_refresh_stream_count = len(get_streams(log_result=False) or [])
            except Exception as e:
                logger.warning(f"Could not read post-refresh stream pool size: {e}")
                post_refresh_stream_count = 0

            playlists_refreshed = bool(update_all_playlists or playlists_to_update)

            if refresh_success and self._should_abort_for_suspicious_stream_pool(
                before_count=pre_refresh_stream_count,
                after_count=post_refresh_stream_count,
                playlists_refreshed=playlists_refreshed,
            ):
                logger.error(
                    "Automation safety gate triggered. Skipping validation and assignment "
                    "to preserve existing channel streams."
                )
                refresh_success = False
            
            if refresh_success:
                # Optional post-refresh delay for environments where provider updates
                # are eventually consistent. Defaults to 0 to avoid fixed latency.
                post_refresh_delay = self._get_post_refresh_delay_seconds()
                if playlists_refreshed and post_refresh_delay > 0:
                    logger.info(
                        f"Waiting {post_refresh_delay:.2f}s after playlist refresh before stream matching"
                    )
                    time.sleep(post_refresh_delay)
                
                # 4. Stream Matching (Validation & Assignment)
                # Group results by channel for easier joining later
                
                # Validate existing streams
                try:
                    val_res = self.validate_and_remove_non_matching_streams(force=forced, forced_period_id=forced_period_id, skip_changelog=True)
                    validation_details = val_res.get("details", [])
                except Exception as e:
                    logger.error(f"✗ Failed to validate streams: {e}")
                
                # Discover and assign new streams
                try:
                    assign_res = self.discover_and_assign_streams(force=forced, skip_check_trigger=True, forced_period_id=forced_period_id, skip_changelog=True)
                    assignment_details = assign_res.get("assignment_details", [])
                    assigned_stream_ids = assign_res.get("assigned_stream_ids", {})
                except Exception as e:
                    logger.error(f"✗ Failed to assign streams: {e}")
                    assigned_stream_ids = {}

                # 4.5. Trigger Quality Checks for all channels in the period(s)
                if channels_to_quality_check:
                    try:
                        from apps.stream.stream_checker_service import get_stream_checker_service
                        stream_checker = get_stream_checker_service()
                        logger.info(f"Running synchronous quality checks for {len(channels_to_quality_check)} channels...")
                        
                        target_stream_ids = None
                        _target_stream_ids = {}
                        
                        for ch_id in channels_to_quality_check:
                            check_all_streams = channel_check_all_streams.get(ch_id, False)
                            if not check_all_streams:
                                # Strict validation mapping: Evaluate newly assigned streams only.
                                if str(ch_id) in assigned_stream_ids:
                                    _target_stream_ids[ch_id] = assigned_stream_ids[str(ch_id)]
                                else:
                                    _target_stream_ids[ch_id] = []
                        
                        if _target_stream_ids:
                            target_stream_ids = _target_stream_ids
                            
                        # Run checks synchronously and collect results
                        check_results = stream_checker.check_channels_synchronously(
                            channel_ids=channels_to_quality_check, 
                            force_check=forced,
                            target_stream_ids=target_stream_ids
                        )
                        logger.info(f"Synchronous quality checks completed for {len(check_results)} channels")
                    except Exception as e:
                        logger.error(f"✗ Failed to run quality checks: {e}")
                        check_results = {}
            
            # 5. Consolidate Results by Period for Changelog
            end_time = datetime.now()
            duration_sec = (end_time - start_time).total_seconds()
            duration_str = f"{int(duration_sec)}s"
            
            run_results = {
                'duration': duration_str,
                'total_channels': channels_with_periods,
                'periods': [],
                'total_streams': 0,
                'streams_analyzed': 0,
                'dead_streams': 0,
                'streams_revived': 0,
                'added_streams': 0,
                'removed_streams': 0,
                'avg_bitrate': 'N/A',
                'avg_resolution': 'N/A',
                'avg_fps': 'N/A'
            }
            
            # Aggregate stats counters
            agg_bitrates = []
            agg_fps = []
            agg_resolutions = []
            total_streams_count = 0
            streams_analyzed_count = 0
            dead_streams_count = 0
            revived_streams_count = 0
            added_streams_count = 0
            removed_streams_count = 0
            
            # Map channel IDs to their results
            val_map = {str(d['channel_id']): d for d in validation_details}
            assign_map = {str(d['channel_id']): d for d in assignment_details}
            
            for (p_id, p_name), p_data in active_periods.items():
                period_entry = {
                    'period_id': p_id,
                    'period_name': p_name,
                    'channels': []
                }
                
                for channel in p_data['channels']:
                    c_id = str(channel.get('id'))
                    c_name = channel.get('name', f'Channel {c_id}')
                    
                    # Fetch logo URL
                    logo_url = None
                    logo_id = channel.get('logo_id')
                    if logo_id:
                        logo_url = f"/api/channels/logos/{logo_id}/cache"
                    
                    steps = []
                    
                    # Step 1: Playlist Refresh (if relevant for this period's profile)
                    profile_id = p_data['profile_id']
                    profile = automation_config.get_profile(profile_id) if profile_id else None
                    m3u_enabled = profile.get('m3u_update', {}).get('enabled', False) if profile else False
                    
                    if m3u_enabled:
                        steps.append({
                            'step': 'Playlist Refresh',
                            'status': 'success' if refresh_success else ('skipped' if not refreshed_accounts else 'failed'),
                            'details': {
                                'accounts': refreshed_accounts
                            }
                        })
                    
                    if c_id in val_map:
                        v_detail = val_map[c_id]
                        rem_count = v_detail.get('removed_count', 0)
                        removed_streams_count += rem_count
                        steps.append({
                            'step': 'Validation',
                            'status': 'success',
                            'details': {
                                'removed_count': rem_count,
                                'streams': v_detail.get('removed_streams', [])
                            }
                        })
                    
                    if c_id in assign_map:
                        a_detail = assign_map[c_id]
                        add_count = a_detail.get('stream_count', 0)
                        added_streams_count += add_count
                        steps.append({
                            'step': 'Assignment',
                            'status': 'success',
                            'details': {
                                'added_count': add_count,
                                'streams': a_detail.get('streams', [])
                            }
                        })
                    
                    if int(c_id) in check_results:
                        c_result = check_results[int(c_id)]
                        ch_dead = c_result.get('dead_streams_count', 0)
                        ch_revived = c_result.get('revived_streams_count', 0)
                        ch_analyzed = len(c_result.get('checked_streams', []))
                        
                        dead_streams_count += ch_dead
                        revived_streams_count += ch_revived
                        streams_analyzed_count += ch_analyzed
                        
                        # Collect metrics for global averages
                        from apps.core.stream_stats_utils import parse_bitrate_value, parse_fps_value
                        for s in c_result.get('checked_streams', []):
                            br = parse_bitrate_value(s.get('bitrate'))
                            if br: agg_bitrates.append(br)
                            
                            f = parse_fps_value(s.get('fps'))
                            if f: agg_fps.append(f)
                            
                            res = s.get('resolution')
                            if res and res != 'N/A': agg_resolutions.append(res)

                        steps.append({
                            'step': 'Quality Check',
                            'status': 'success' if c_result.get('error') is None else 'failed',
                            'details': {
                                'dead_streams_count': ch_dead,
                                'revived_streams_count': ch_revived,
                                'skipped_streams_count': len(c_result.get('skipped_streams', [])),
                                'dead_streams': c_result.get('dead_streams', []),
                                'revived_streams': c_result.get('revived_streams', []),
                                'skipped_streams': c_result.get('skipped_streams', []),
                                'checked_streams': c_result.get('checked_streams', []),
                                'error': c_result.get('error')
                            }
                        })
                        
                        # Total streams count for this channel
                        total_streams_count += len(channel.get('streams', []))
                    
                    # Filter out channels with zero impact
                    # A channel has impact if:
                    # 1. Validation removed streams
                    # 2. Assignment added streams
                    # 3. Quality check found dead, revived, skipped, or actively checked streams
                    # 4. Playlist Refresh was explicitly triggered for this channel's accounts and was successful
                    has_impact = False
                    for step in steps:
                        if step['step'] == 'Validation' and step['details'].get('removed_count', 0) > 0:
                            has_impact = True
                        elif step['step'] == 'Assignment' and step['details'].get('added_count', 0) > 0:
                            has_impact = True
                        elif step['step'] == 'Quality Check':
                            d = step['details']
                            # Count streams that were actively checked (not from cache)
                            active_checks = 0
                            for cs in d.get('checked_streams', []):
                                if not cs.get('from_cache', False):
                                    active_checks += 1
                                    
                            if d.get('dead_streams_count', 0) > 0 or d.get('revived_streams_count', 0) > 0 \
                               or active_checks > 0:
                                has_impact = True

                    if steps and has_impact:
                        period_entry['channels'].append({
                            'channel_id': int(c_id),
                            'channel_name': c_name,
                            'logo_url': logo_url,
                            'profile_id': profile_id,
                            'profile_name': p_data['profile_name'],
                            'steps': steps
                        })
                
                if period_entry['channels']:
                    run_results['periods'].append(period_entry)

            # Finalize aggregate stats
            run_results['total_streams'] = total_streams_count
            run_results['streams_analyzed'] = streams_analyzed_count
            run_results['dead_streams'] = dead_streams_count
            run_results['streams_revived'] = revived_streams_count
            run_results['added_streams'] = added_streams_count
            run_results['removed_streams'] = removed_streams_count
            
            from apps.core.stream_stats_utils import format_bitrate, format_fps
            from collections import Counter
            
            if agg_bitrates:
                run_results['avg_bitrate'] = format_bitrate(sum(agg_bitrates) / len(agg_bitrates))
            if agg_fps:
                run_results['avg_fps'] = format_fps(sum(agg_fps) / len(agg_fps))
            if agg_resolutions:
                run_results['avg_resolution'] = Counter(agg_resolutions).most_common(1)[0][0]
            
            # Add to changelog if there's any work done
            has_work = any(len(p['channels']) > 0 for p in run_results['periods'])
            if has_work and self.config.get("enabled_features", {}).get("changelog_tracking", True):
                self.changelog.add_automation_run_entry(run_results)
            
            # Update last run times ONLY for periods that actually had work / were due
            for p_id_tuple in active_periods.keys():
                # active_periods keys are (p_id, p_name)
                pid = p_id_tuple[0]
                self.period_last_run[pid] = datetime.now()
                
            # Keep legacy last_playlist_update synced for legacy backward compatibility if any
            if active_periods:
                self.last_playlist_update = datetime.now()
                self._save_state()
            
            logger.info("Automation cycle completed")
            
        finally:
            self._m3u_accounts_cache = None

    def _filter_channels_by_profile(self, channels: List[Dict], operation: str = "") -> List[Dict]:
        """
        Filter channels based on their effective profile settings.
        Deprecated: Logic moved into specific methods (validation/assignment) to handle per-channel profiles.
        This helper returns all channels to avoid breaking legacy callers if any.
        """
        return channels
        
    def trigger_automation(self, period_id=None, force=True):
        """Manually trigger an automation cycle.
        
        Args:
            period_id: Optional ID of a specific automation period to run
            force: If True (default), forces check bypassing grace periods. 
                   If False, respects grace periods (simulates scheduled run).
        """
        logger.info(f"Triggering manual automation cycle{' for period ' + period_id if period_id else ''} (force={force})")
        self.force_next_run = force
        self.forced_period_id = period_id
        if self.automation_thread and self.automation_thread.is_alive():
            self.automation_wake_event.set()
        else:
            # If not running, we could potentially run it synchronously or just log warning.
            # But the requirement is likely to trigger the *service*.
            logger.warning("Automation service not running, manual trigger queueing for next run or ignored")
            
    def start_automation(self):
        """Start the automation background thread."""
        if self.automation_thread and self.automation_thread.is_alive():
            logger.info("Automation service already running")
            return
            
        logger.info("Starting automation service...")
        self.automation_running = True
        self.automation_wake_event.clear()
        self.automation_thread = threading.Thread(target=self._automation_loop, daemon=True)
        self.automation_thread.start()
        logger.info("Automation service started")
        
    def stop_automation(self):
        """Stop the automation background thread."""
        logger.info("Stopping automation service...")
        self.automation_running = False
        self.automation_wake_event.set()  # Wake up thread to exit
        
        if self.automation_thread:
            self.automation_thread.join(timeout=5)
            logger.info("Automation service stopped")
            
    def _automation_loop(self):
        """Main loop for automation service."""
        logger.info("Automation background loop started")
        while self.automation_running:
            try:
                # Run automation cycle
                # Pass forced period info to cycle
                forced = self.force_next_run
                period_id = self.forced_period_id
                
                # Reset forced flags before running
                self.force_next_run = False
                self.forced_period_id = None
                
                self.run_automation_cycle(forced=forced, forced_period_id=period_id)
                
            except Exception as e:
                logger.error(f"Error in automation loop: {e}", exc_info=True)
            
            # Sleep interval (e.g. 60 seconds)
            # We use wait() on the event to allow early wake-up/exit
            if self.automation_running:
                # Loop interval can be configured, default to 60s for responsiveness to schedule changes
                self.automation_wake_event.wait(timeout=60)
                if self.automation_wake_event.is_set() and not self.automation_running:
                    break
                self.automation_wake_event.clear()
        
        logger.info("Automation background loop stopped")
