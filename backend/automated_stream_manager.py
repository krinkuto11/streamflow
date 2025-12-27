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
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any
from collections import defaultdict

# Import croniter for cron expression support
try:
    from croniter import croniter
    CRONITER_AVAILABLE = True
except ImportError:
    CRONITER_AVAILABLE = False

from api_utils import (
    refresh_m3u_playlists,
    get_m3u_accounts,
    get_streams,
    add_streams_to_channel,
    _get_base_url
)

# Import UDI for direct data access
from udi import get_udi_manager

# Import channel settings manager
from channel_settings_manager import get_channel_settings_manager

# Setup centralized logging
from logging_config import setup_logging, log_function_call, log_function_return, log_exception, log_state_change

logger = setup_logging(__name__)

# Import DeadStreamsTracker
try:
    from dead_streams_tracker import DeadStreamsTracker
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
        self.changelog = self._load_changelog()
    
    def _load_changelog(self) -> List[Dict]:
        """Load existing changelog or create empty one."""
        if self.changelog_file.exists():
            try:
                with open(self.changelog_file, 'r') as f:
                    return json.load(f)
            except (json.JSONDecodeError, FileNotFoundError):
                logger.warning(f"Could not load {self.changelog_file}, creating new changelog")
        return []
    
    def add_entry(self, action: str, details: Dict, timestamp: Optional[str] = None, subentries: Optional[List[Dict[str, Any]]] = None):
        """Add a new changelog entry.
        
        Args:
            action: Type of action (e.g., 'playlist_update_match', 'global_check', 'single_channel_check')
            details: Main details of the action including global stats
            timestamp: Optional timestamp (defaults to now)
            subentries: Optional list of subentry groups (e.g., updates, checks)
        """
        if timestamp is None:
            timestamp = datetime.now().isoformat()
        
        entry = {
            "timestamp": timestamp,
            "action": action,
            "details": details
        }
        
        # Add subentries if provided
        if subentries:
            entry["subentries"] = subentries
        
        self.changelog.append(entry)
        self._save_changelog()
        logger.info(f"Changelog entry added: {action}")
    
    def _save_changelog(self):
        """Save changelog to file."""
        # Ensure parent directory exists
        self.changelog_file.parent.mkdir(parents=True, exist_ok=True)
        with open(self.changelog_file, 'w') as f:
            json.dump(self.changelog, f, indent=2)
    
    def get_recent_entries(self, days: int = 7) -> List[Dict]:
        """Get changelog entries from the last N days, filtered and sorted."""
        cutoff = datetime.now() - timedelta(days=days)
        recent = []
        
        for entry in self.changelog:
            try:
                entry_time = datetime.fromisoformat(entry['timestamp'])
                if entry_time >= cutoff:
                    # Filter out entries without meaningful channel updates
                    if self._has_channel_updates(entry):
                        recent.append(entry)
            except (ValueError, KeyError):
                continue
        
        # Sort by timestamp in reverse chronological order (newest first)
        recent.sort(key=lambda x: x.get('timestamp', ''), reverse=True)
        
        return recent
    
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
    
    def _has_channel_updates(self, entry: Dict) -> bool:
        """Check if a changelog entry contains meaningful channel/stream updates."""
        details = entry.get('details', {})
        action = entry.get('action', '')
        
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
    """Handles regex-based channel matching for stream assignment."""
    
    def __init__(self, config_file=None):
        if config_file is None:
            config_file = CONFIG_DIR / "channel_regex_config.json"
        self.config_file = Path(config_file)
        self.channel_patterns = self._load_patterns()
    
    def _load_patterns(self) -> Dict:
        """Load regex patterns for channel matching."""
        if self.config_file.exists():
            try:
                with open(self.config_file, 'r') as f:
                    return json.load(f)
            except (json.JSONDecodeError, FileNotFoundError):
                logger.warning(f"Could not load {self.config_file}, creating default config")
        
        # Create default configuration
        default_config = {
            "patterns": {
                # Example patterns - these should be configured by the user
                # "1": {"name": "CNN", "regex": [".*CNN.*", ".*Cable News.*"], "enabled": True},
                # "2": {"name": "ESPN", "regex": [".*ESPN.*", ".*Sports.*"], "enabled": True}
            },
            "global_settings": {
                "case_sensitive": False,
                "require_exact_match": False
            }
        }
        
        self._save_patterns(default_config)
        return default_config
    
    def _save_patterns(self, patterns: Dict):
        """Save patterns to file."""
        # Ensure parent directory exists
        self.config_file.parent.mkdir(parents=True, exist_ok=True)
        with open(self.config_file, 'w') as f:
            json.dump(patterns, f, indent=2)
    
    def validate_regex_patterns(self, patterns: List[str]) -> Tuple[bool, Optional[str]]:
        """Validate a list of regex patterns.
        
        Supports both {CHANNEL_NAME} and CHANNEL_NAME variable formats.
        
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
                # Temporarily substitute both variable formats with a placeholder for validation
                # Use a simple placeholder that won't interfere with regex syntax
                validation_pattern = pattern.replace('{CHANNEL_NAME}', 'PLACEHOLDER')
                validation_pattern = validation_pattern.replace('CHANNEL_NAME', 'PLACEHOLDER')
                re.compile(validation_pattern)
            except re.error as e:
                return False, f"Invalid regex pattern '{pattern}': {str(e)}"
        
        return True, None
    
    def add_channel_pattern(self, channel_id: str, name: str, regex_patterns: List[str], enabled: bool = True, m3u_accounts: Optional[List[int]] = None):
        """Add or update a channel pattern.
        
        Args:
            channel_id: Channel ID
            name: Channel name
            regex_patterns: List of regex patterns
            enabled: Whether the pattern is enabled
            m3u_accounts: Optional list of M3U account IDs that this regex should apply to.
                         Examples:
                         - None: Field not stored, applies to all M3U accounts (backward compatible)
                         - []: Empty list stored, explicitly means "all M3U accounts"
                         - [1, 2, 3]: Only match streams from M3U accounts with these IDs
            
        Raises:
            ValueError: If any regex pattern is invalid
        """
        # Validate patterns before saving
        is_valid, error_msg = self.validate_regex_patterns(regex_patterns)
        if not is_valid:
            raise ValueError(error_msg)
        
        pattern_data = {
            "name": name,
            "regex": regex_patterns,
            "enabled": enabled
        }
        
        # Store m3u_accounts field when explicitly provided
        # - Not provided (None): field not stored, backward compatible (applies to all)
        # - Empty list []: stored as [], explicitly means "all M3U accounts"
        # - Specific IDs [1,2,3]: stored as-is, only those M3U accounts
        if m3u_accounts is not None:
            pattern_data["m3u_accounts"] = m3u_accounts
        
        self.channel_patterns["patterns"][str(channel_id)] = pattern_data
        self._save_patterns(self.channel_patterns)
        logger.info(f"Added/updated pattern for channel {channel_id}: {name} (M3U accounts: {m3u_accounts or 'all'})")
    
    
    def reload_patterns(self):
        """Reload patterns from the config file.
        
        This is useful when patterns have been updated by another process
        and we need to ensure we're using the latest patterns.
        """
        self.channel_patterns = self._load_patterns()
        logger.debug("Reloaded regex patterns from config file")
    
    def _substitute_channel_variables(self, pattern: str, channel_name: str) -> str:
        """Substitute channel name variables in a regex pattern.
        
        Supports multiple variable formats for user convenience:
        - {CHANNEL_NAME} - Primary format (intuitive, common in templating)
        - CHANNEL_NAME - Legacy format (maintained for backward compatibility)
        
        Args:
            pattern: Regex pattern that may contain CHANNEL_NAME variable
            channel_name: Name of the channel to substitute
            
        Returns:
            Pattern with variables substituted
            
        Examples:
            >>> _substitute_channel_variables(".*{CHANNEL_NAME}.*", "HBO")
            ".*HBO.*"
            >>> _substitute_channel_variables(".*CHANNEL_NAME.*", "ESPN+")
            ".*ESPN\\+.*"
        """
        # Escape special regex characters in channel name to avoid issues
        escaped_channel_name = re.escape(channel_name)
        
        # Replace both {CHANNEL_NAME} and CHANNEL_NAME formats
        # Process {CHANNEL_NAME} first to avoid double replacement
        result = pattern.replace('{CHANNEL_NAME}', escaped_channel_name)
        result = result.replace('CHANNEL_NAME', escaped_channel_name)
        
        return result
    
    def match_stream_to_channels(self, stream_name: str, stream_m3u_account: Optional[int] = None) -> List[str]:
        """Match a stream name to channel IDs based on regex patterns.
        
        Args:
            stream_name: Name of the stream to match
            stream_m3u_account: Optional M3U account ID of the stream.
                               If provided, only matches patterns that apply to this M3U account.
                               
        Examples:
            - stream_m3u_account=None: Stream source unknown, matches all patterns (no filtering)
            - stream_m3u_account=5: Stream is from M3U account 5, only matches patterns where:
              * pattern has no m3u_accounts field (old configs, apply to all)
              * pattern has m3u_accounts=[] (explicitly applies to all)
              * pattern has m3u_accounts=[5] or m3u_accounts=[5, 6, ...] (includes account 5)
        
        Returns:
            List of channel IDs that match the stream
        """
        matches = []
        case_sensitive = self.channel_patterns.get("global_settings", {}).get("case_sensitive", False)
        
        search_name = stream_name if case_sensitive else stream_name.lower()
        
        for channel_id, config in self.channel_patterns.get("patterns", {}).items():
            if not config.get("enabled", True):
                continue
            
            # Check if this regex pattern applies to the stream's M3U account
            # Backward compatible behavior:
            # - m3u_accounts not present (None) = old config, applies to all M3U accounts
            # - m3u_accounts = [] (empty) = new config, explicitly applies to all M3U accounts
            # - m3u_accounts = [1,2,3] = only applies to those specific M3U accounts
            pattern_m3u_accounts = config.get("m3u_accounts")
            if pattern_m3u_accounts is not None and len(pattern_m3u_accounts) > 0:
                # Pattern is limited to specific M3U accounts
                if stream_m3u_account is None or stream_m3u_account not in pattern_m3u_accounts:
                    # Stream's M3U account is not in the allowed list, skip this pattern
                    continue
            # If m3u_accounts is None (old config) or empty list (new, all), pattern applies to all M3U accounts
            
            channel_name = config.get("name", "")
            
            for pattern in config.get("regex", []):
                # Substitute channel name variable if present
                substituted_pattern = self._substitute_channel_variables(pattern, channel_name)
                
                search_pattern = substituted_pattern if case_sensitive else substituted_pattern.lower()
                
                # Convert literal spaces in pattern to flexible whitespace regex (\s+)
                # This allows matching streams with different whitespace characters
                # (non-breaking spaces, tabs, double spaces, etc.)
                search_pattern = re.sub(r' +', r'\\s+', search_pattern)
                
                try:
                    if re.search(search_pattern, search_name):
                        matches.append(channel_id)
                        logger.debug(f"Stream '{stream_name}' matched channel {channel_id} with pattern '{pattern}'")
                        break  # Only match once per channel
                except re.error as e:
                    logger.error(f"Invalid regex pattern '{pattern}' for channel {channel_id}: {e}")
        
        return matches
    
    def get_patterns(self) -> Dict:
        """Get current patterns configuration."""
        return self.channel_patterns


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
        self.last_playlist_update = None
        self.automation_start_time = None
        
        # Cache for M3U accounts to avoid redundant API calls within a single automation cycle
        # This is cleared after each cycle completes
        self._m3u_accounts_cache = None
        
        # Cache for dead stream removal setting to avoid repeated file I/O
        self._dead_stream_removal_enabled_cache = None
        self._dead_stream_removal_cache_time = None
    
    def _load_config(self) -> Dict:
        """Load automation configuration."""
        if self.config_file.exists():
            try:
                with open(self.config_file, 'r') as f:
                    return json.load(f)
            except (json.JSONDecodeError, FileNotFoundError):
                logger.warning(f"Could not load {self.config_file}, creating default config")
        
        # Default configuration
        default_config = {
            "playlist_update_interval_minutes": 5,
            "playlist_update_cron": "",  # Empty means use interval. When both are set, cron takes precedence.
            "enabled_m3u_accounts": [],  # Empty list means all accounts enabled
            "autostart_automation": False,  # Don't auto-start by default
            "enabled_features": {
                "auto_playlist_update": True,
                "auto_stream_discovery": True,
                "changelog_tracking": True
            },
            "validate_existing_streams": False  # Validate existing streams in channels against regex patterns
        }
        
        self._save_config(default_config)
        return default_config
    
    def _save_config(self, config: Dict):
        """Save configuration to file."""
        # Ensure parent directory exists
        self.config_file.parent.mkdir(parents=True, exist_ok=True)
        with open(self.config_file, 'w') as f:
            json.dump(config, f, indent=2)
    
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
        
        Uses a 60-second cache to avoid repeated file I/O operations.
        
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
        
        # Cache expired or not set, read from file
        try:
            stream_checker_config_file = CONFIG_DIR / 'stream_checker_config.json'
            if stream_checker_config_file.exists():
                with open(stream_checker_config_file, 'r') as f:
                    config = json.load(f)
                    enabled = config.get('dead_stream_handling', {}).get('enabled', True)
            else:
                # Default to True if config doesn't exist
                enabled = True
            
            # Update cache
            self._dead_stream_removal_enabled_cache = enabled
            self._dead_stream_removal_cache_time = current_time
            return enabled
        except Exception as e:
            logger.error(f"Error reading stream checker config: {e}")
            # Default to True on error (conservative approach)
            # Don't cache errors, try again next time
            return True
    
    def refresh_playlists(self, force: bool = False) -> bool:
        """Refresh M3U playlists and track changes.
        
        Args:
            force: If True, bypass the auto_playlist_update feature flag check.
                   Used for manual/quick action triggers from the UI.
        """
        try:
            if not force and not self.config.get("enabled_features", {}).get("auto_playlist_update", True):
                logger.info("Playlist update is disabled in configuration")
                return False
            
            logger.info("Starting M3U playlist refresh...")
            
            # Get streams before refresh
            from api_utils import get_streams
            streams_before = get_streams(log_result=False) if self.config.get("enabled_features", {}).get("changelog_tracking", True) else []
            before_stream_ids = {s.get('id'): s.get('name', '') for s in streams_before if isinstance(s, dict) and s.get('id')}
            
            # Get all M3U accounts and filter out "custom" and non-active accounts
            # Cache the result to avoid redundant API calls in discover_and_assign_streams
            all_accounts = get_m3u_accounts()
            self._m3u_accounts_cache = all_accounts  # Cache for use in discover_and_assign_streams
            logger.debug(f"M3U accounts fetched from UDI cache and stored in local cache ({len(all_accounts) if all_accounts else 0} accounts)")
            if all_accounts:
                # Filter out "custom" account (it doesn't need refresh as it's for locally added streams)
                # and non-active accounts (per Dispatcharr API spec)
                # Only filter by name, not by null URLs, as legitimate accounts may have these
                non_custom_accounts = [
                    acc for acc in all_accounts
                    if acc.get('name', '').lower() != 'custom' and acc.get('is_active', True)
                ]
                
                # Perform refresh - check if we need to filter by enabled accounts
                enabled_accounts = self.config.get("enabled_m3u_accounts", [])
                if enabled_accounts:
                    # Refresh only enabled accounts (and exclude custom)
                    non_custom_ids = [acc.get('id') for acc in non_custom_accounts if acc.get('id') is not None]
                    accounts_to_refresh = [acc_id for acc_id in enabled_accounts if acc_id in non_custom_ids]
                    for account_id in accounts_to_refresh:
                        logger.info(f"Refreshing M3U account {account_id}")
                        refresh_m3u_playlists(account_id=account_id)
                    if len(enabled_accounts) != len(accounts_to_refresh):
                        logger.info(f"Skipped {len(enabled_accounts) - len(accounts_to_refresh)} account(s) (custom or invalid)")
                else:
                    # Refresh all non-custom accounts
                    for account in non_custom_accounts:
                        account_id = account.get('id')
                        if account_id is not None:
                            logger.info(f"Refreshing M3U account {account_id}")
                            refresh_m3u_playlists(account_id=account_id)
                    if len(all_accounts) != len(non_custom_accounts):
                        logger.info(f"Skipped {len(all_accounts) - len(non_custom_accounts)} 'custom' account(s)")
            else:
                # Fallback: if we can't get accounts, refresh all (legacy behavior)
                logger.warning("Could not fetch M3U accounts, refreshing all as fallback")
                refresh_m3u_playlists()
            
            # Refresh UDI cache to get updated streams and channels after playlist update
            # This ensures deleted/added streams are reflected in the cache
            # Also refresh M3U accounts to detect any new accounts added in Dispatcharr
            # And refresh channel groups to detect any group changes (splits, merges, etc.)
            # Profile refresh is critical: ensures channel profiles stay synced with Dispatcharr
            # (deletions, modifications, new profiles) to prevent orphaned profile references
            logger.info("Refreshing UDI cache after playlist update...")
            udi = get_udi_manager()
            udi.refresh_m3u_accounts()  # Check for new M3U accounts
            udi.refresh_streams()
            udi.refresh_channels()
            udi.refresh_channel_groups()  # Check for new/updated channel groups
            udi.refresh_channel_profiles()  # Sync profiles with Dispatcharr to prevent orphaned references
            logger.info("UDI cache refreshed successfully")
            
            # Trigger EPG refresh to pick up any EPG/tvg-id changes made in Dispatcharr
            # This ensures that if a channel's EPG assignment was changed in Dispatcharr,
            # the new program data will be available in StreamFlow
            try:
                logger.info("Triggering EPG refresh after playlist update...")
                from scheduling_service import get_scheduling_service
                scheduling_service = get_scheduling_service()
                # Force refresh to bypass cache and get fresh EPG data
                scheduling_service.fetch_epg_grid(force_refresh=True)
                logger.info("EPG refresh completed successfully")
            except Exception as e:
                logger.error(f"Error triggering EPG refresh after playlist update: {e}")
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
            
            
            if self.config.get("enabled_features", {}).get("changelog_tracking", True):
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
            
            return True
            
        except Exception as e:
            logger.error(f"Failed to refresh M3U playlists: {e}")
            
            
            if self.config.get("enabled_features", {}).get("changelog_tracking", True):
                self.changelog.add_entry("playlist_refresh", {
                    "success": False,
                    "error": str(e),
                    "timestamp": datetime.now().isoformat()
                })
            return False
    
    def discover_and_assign_streams(self, force: bool = False, skip_check_trigger: bool = False) -> Dict[str, int]:
        """Discover new streams and assign them to channels based on regex patterns.
        
        Args:
            force: If True, bypass the auto_stream_discovery feature flag check.
                   Used for manual/quick action triggers from the UI.
            skip_check_trigger: If True, don't trigger immediate stream quality check.
                   Used when the caller will handle the check itself (e.g., check_single_channel).
        """
        if not force and not self.config.get("enabled_features", {}).get("auto_stream_discovery", True):
            logger.info("Stream discovery is disabled in configuration")
            return {}
        
        try:
            # Reload patterns to ensure we have the latest changes
            self.regex_matcher.reload_patterns()
            
            logger.info("Starting stream discovery and assignment...")
            
            # Get all available streams (don't log, we already logged during refresh)
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
                # Filter out "custom" account and non-active accounts
                non_custom_accounts = [
                    acc for acc in all_accounts
                    if acc.get('name', '').lower() != 'custom' and acc.get('is_active', True)
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
            
            # Filter channels by matching_mode setting (channel-level overrides group-level)
            channel_settings = get_channel_settings_manager()
            matching_enabled_channel_ids = []
            
            for channel in all_channels:
                if not isinstance(channel, dict) or 'id' not in channel:
                    continue
                channel_id = channel['id']
                channel_group_id = channel.get('channel_group_id')
                
                # Check if channel has an explicit setting (not default)
                channel_explicit_settings = channel_settings._settings.get(channel_id, {})
                has_explicit_matching = 'matching_mode' in channel_explicit_settings
                
                if has_explicit_matching:
                    # Channel has explicit override - use it
                    if channel_settings.is_matching_enabled(channel_id):
                        matching_enabled_channel_ids.append(channel_id)
                else:
                    # No channel override - use group setting (or default to enabled if no group)
                    if channel_settings.is_channel_enabled_by_group(channel_group_id, mode='matching'):
                        matching_enabled_channel_ids.append(channel_id)
            
            # Filter channels to only those with matching enabled
            filtered_channels = [ch for ch in all_channels if ch.get('id') in matching_enabled_channel_ids]
            
            excluded_count = len(all_channels) - len(filtered_channels)
            if excluded_count > 0:
                logger.info(f"Excluding {excluded_count} channel(s) with matching disabled (channel or group level) from stream assignment")
            
            # Use filtered channels for the rest of the logic
            all_channels = filtered_channels
            
            if not all_channels:
                logger.info("No channels with matching enabled found")
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
                    try:
                        logo = udi.get_logo_by_id(logo_id)
                        if logo and logo.get('cache_url'):
                            channel_logo_urls[channel_id] = logo.get('cache_url')
                    except Exception as e:
                        logger.debug(f"Could not fetch logo for channel {channel_id}: {e}")
                
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
            
            # Process each stream
            for stream in all_streams:
                # Validate that stream is a dictionary before accessing attributes
                if not isinstance(stream, dict):
                    logger.warning(f"Invalid stream format encountered: {type(stream).__name__} - {stream}")
                    continue
                    
                stream_name = stream.get('name', '')
                stream_id = stream.get('id')
                
                if not stream_name or not stream_id:
                    continue
                
                # Skip streams marked as dead in the tracker (if dead stream removal is enabled)
                # Dead streams should not be added to channels during subsequent matches
                stream_url = stream.get('url', '')
                if self.dead_streams_tracker and self.dead_streams_tracker.is_dead(stream_url):
                    # Check if dead stream removal is enabled
                    dead_stream_removal_enabled = self._is_dead_stream_removal_enabled()
                    if dead_stream_removal_enabled:
                        logger.debug(f"Skipping dead stream {stream_id}: {stream_name} (URL: {stream_url})")
                        continue
                    else:
                        logger.debug(f"Including dead stream {stream_id}: {stream_name} (dead stream removal is disabled)")
                
                # Get stream's m3u_account for M3U account filtering
                stream_m3u_account = stream.get('m3u_account')
                
                # Find matching channels (with M3U account filtering if applicable)
                matching_channels = self.regex_matcher.match_stream_to_channels(stream_name, stream_m3u_account)
                
                for channel_id in matching_channels:
                    # Check if stream is already in this channel
                    if channel_id in channel_streams and stream_id not in channel_streams[channel_id]:
                        assignments[channel_id].append(stream_id)
                        assignment_details[channel_id].append({
                            "stream_id": stream_id,
                            "stream_name": stream_name
                        })
            
            # Prepare detailed changelog data
            detailed_assignments = []
            
            # Get dead stream removal config once for this discovery run
            dead_stream_removal_enabled = self._is_dead_stream_removal_enabled()
            
            # Assign streams to channels
            for channel_id, stream_ids in assignments.items():
                if stream_ids:
                    try:
                        added_count = add_streams_to_channel(int(channel_id), stream_ids, allow_dead_streams=(not dead_stream_removal_enabled))
                        assignment_count[channel_id] = added_count
                        
                        # Verify streams were added correctly
                        if added_count > 0:
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
                        
                        # Prepare detailed assignment info
                        channel_assignment = {
                            "channel_id": channel_id,
                            "channel_name": channel_names.get(channel_id, f'Channel {channel_id}'),
                            "logo_url": channel_logo_urls.get(channel_id),
                            "stream_count": added_count,
                            "streams": assignment_details[channel_id][:20]  # Limit to first 20 for changelog
                        }
                        detailed_assignments.append(channel_assignment)
                        
                        
                    except Exception as e:
                        logger.error(f"Failed to assign streams to channel {channel_id}: {e}")
            
            # Add comprehensive changelog entry
            total_assigned = sum(assignment_count.values())
            if self.config.get("enabled_features", {}).get("changelog_tracking", True):
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
                            from stream_checker_service import get_stream_checker_service
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
            
            return assignment_count
            
        except Exception as e:
            logger.error(f"Stream discovery failed: {e}")
            if self.config.get("enabled_features", {}).get("changelog_tracking", True):
                self.changelog.add_entry("stream_discovery", {
                    "success": False,
                    "error": str(e)
                })
            return {}
    
    def validate_and_remove_non_matching_streams(self) -> Dict[str, Any]:
        """
        Validate existing streams in channels against regex patterns.
        Remove streams that no longer match their channel's patterns.
        
        Returns:
            Dict containing validation statistics:
            - channels_checked: Number of channels checked
            - streams_removed: Total streams removed
            - channels_modified: Number of channels that had streams removed
            - details: List of channel details with removed streams
        """
        log_function_call(logger, "validate_and_remove_non_matching_streams")
        
        # Check if removal is enabled in stream checker config
        try:
            from stream_checker_service import get_stream_checker_service
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
        
        try:
            logger.info("=" * 80)
            logger.info("Starting validation of existing streams against regex patterns")
            logger.info("=" * 80)
            
            udi = get_udi_manager()
            all_channels = udi.get_channels()
            
            if not all_channels:
                logger.info("No channels found")
                return {
                    "channels_checked": 0,
                    "streams_removed": 0,
                    "channels_modified": 0,
                    "details": []
                }
            
            # Get channel settings manager to respect matching mode settings
            channel_settings = get_channel_settings_manager()
            
            # Filter channels with matching enabled (similar logic to match_streams_to_channels)
            matching_enabled_channel_ids = []
            for channel in all_channels:
                channel_id = channel.get('id')
                channel_group_id = channel.get('group')
                
                # Get effective settings
                channel_explicit_settings = channel_settings.get_channel_settings(channel_id)
                has_explicit_matching = 'matching_mode' in channel_explicit_settings
                
                if has_explicit_matching:
                    if channel_settings.is_matching_enabled(channel_id):
                        matching_enabled_channel_ids.append(channel_id)
                else:
                    if channel_settings.is_channel_enabled_by_group(channel_group_id, mode='matching'):
                        matching_enabled_channel_ids.append(channel_id)
            
            validation_results = {
                "channels_checked": 0,
                "streams_removed": 0,
                "channels_modified": 0,
                "details": []
            }
            
            # Get all streams from UDI for lookup
            all_streams = udi.get_streams(log_result=False)
            stream_lookup = {s['id']: s for s in all_streams if isinstance(s, dict) and 'id' in s}
            
            # Validate each channel's streams
            for channel in all_channels:
                channel_id = channel.get('id')
                
                # Skip channels with matching disabled
                if channel_id not in matching_enabled_channel_ids:
                    continue
                
                validation_results["channels_checked"] += 1
                channel_name = channel.get('name', f'Channel {channel_id}')
                
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
                    stream_name = stream.get('name', '')
                    
                    # Look up full stream data
                    full_stream = stream_lookup.get(stream_id)
                    if not full_stream:
                        logger.warning(f"Stream {stream_id} not found in UDI for channel {channel_name}")
                        streams_to_keep.append(stream_id)
                        continue
                    
                    # Get stream's m3u_account for M3U account filtering
                    stream_m3u_account = full_stream.get('m3u_account')
                    
                    # Check if stream matches any pattern for this channel (with M3U account filtering)
                    matching_channels = self.regex_matcher.match_stream_to_channels(stream_name, stream_m3u_account)
                    
                    if str(channel_id) in matching_channels:
                        # Stream still matches, keep it
                        streams_to_keep.append(stream_id)
                    else:
                        # Stream no longer matches, remove it
                        streams_to_remove.append({
                            "stream_id": stream_id,
                            "stream_name": stream_name
                        })
                        logger.info(f"  Removing non-matching stream from {channel_name}: {stream_name}")
                
                # Update channel if streams need to be removed
                if streams_to_remove:
                    try:
                        from api_utils import update_channel_streams
                        success = update_channel_streams(channel_id, streams_to_keep)
                        
                        if success:
                            validation_results["streams_removed"] += len(streams_to_remove)
                            validation_results["channels_modified"] += 1
                            validation_results["details"].append({
                                "channel_id": channel_id,
                                "channel_name": channel_name,
                                "removed_count": len(streams_to_remove),
                                "removed_streams": streams_to_remove[:10]  # Limit to first 10 for logging
                            })
                            
                            # Refresh channel in UDI after update
                            time.sleep(0.3)
                            udi.refresh_channel_by_id(channel_id)
                            
                            logger.info(f"✓ Removed {len(streams_to_remove)} non-matching stream(s) from {channel_name}")
                        else:
                            logger.error(f"Failed to update channel {channel_name} after validation")
                    except Exception as e:
                        logger.error(f"Error removing streams from channel {channel_name}: {e}")
            
            logger.info(f"Stream validation completed: Checked {validation_results['channels_checked']} channels, " +
                       f"removed {validation_results['streams_removed']} streams from {validation_results['channels_modified']} channels")
            
            # Add changelog entry if there were changes
            if validation_results['streams_removed'] > 0 and self.config.get("enabled_features", {}).get("changelog_tracking", True):
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
    
    def should_run_playlist_update(self) -> bool:
        """Check if it's time to run playlist update."""
        if not self.last_playlist_update:
            return True
        
        # Check if cron expression is configured
        cron_expr = self.config.get("playlist_update_cron", "")
        if cron_expr and CRONITER_AVAILABLE:
            try:
                # Use croniter to check if it's time to run
                cron = croniter(cron_expr, self.last_playlist_update)
                next_run = cron.get_next(datetime)
                return datetime.now() >= next_run
            except Exception as e:
                logger.warning(f"Invalid cron expression '{cron_expr}', falling back to interval: {e}")
        
        # Fall back to interval-based scheduling
        interval = timedelta(minutes=self.config.get("playlist_update_interval_minutes", 5))
        return datetime.now() - self.last_playlist_update >= interval
    
    def run_automation_cycle(self):
        """Run one complete automation cycle."""
        # Check if stream checking mode is active - if so, skip this cycle
        # Stream checking mode includes: global actions, individual checks, and queued checks
        try:
            from stream_checker_service import get_stream_checker_service
            stream_checker = get_stream_checker_service()
            status = stream_checker.get_status()
            if status.get('stream_checking_mode', False):
                # Skip silently when in stream checking mode to avoid log spam
                return
        except Exception as e:
            logger.debug(f"Could not check stream checking mode status: {e}")
        
        # Only log and run if it's actually time to update
        if not self.should_run_playlist_update():
            return  # Skip silently until it's time
        
        logger.info("Starting automation cycle...")
        
        try:
            # 1. Update playlists (also caches M3U accounts for use in discover_and_assign_streams)
            success = self.refresh_playlists()
            if success:
                # Small delay to allow playlist processing
                time.sleep(10)
                
                # 2. Validate existing streams against regex patterns (remove non-matching)
                # This should happen during matching periods, not during stream checks
                try:
                    validation_results = self.validate_and_remove_non_matching_streams()
                    if validation_results.get("streams_removed", 0) > 0:
                        logger.info(f"✓ Removed {validation_results['streams_removed']} non-matching streams from {validation_results['channels_modified']} channels")
                    else:
                        logger.debug("No non-matching streams found to remove")
                except Exception as e:
                    logger.error(f"✗ Failed to validate streams against regex: {e}")
                
                # 3. Discover and assign new streams (uses cached M3U accounts)
                assignments = self.discover_and_assign_streams()
            
            logger.info("Automation cycle completed")
        finally:
            # Clear the M3U accounts cache after each cycle to ensure fresh data on next cycle
            self._m3u_accounts_cache = None
        
    def start_automation(self):
        """Start the automated stream management process."""
        log_function_call(logger, "start_automation")
        if self.running:
            logger.warning("Automation is already running")
            return
        
        log_state_change(logger, "automation_manager", "stopped", "starting")
        self.running = True
        self.automation_start_time = datetime.now()
        logger.info("Starting automated stream management...")
        
        def automation_loop():
            logger.debug("Automation loop thread started")
            while self.running:
                try:
                    # run_automation_cycle handles its own logging based on what actually runs
                    self.run_automation_cycle()
                    
                    # Sleep for a minute before checking again
                    time.sleep(60)
                    
                except Exception as e:
                    log_exception(logger, e, "automation loop")
                    logger.error(f"Error in automation loop: {e}")
                    time.sleep(60)  # Continue after error
            logger.debug("Automation loop thread exiting")
        
        self.automation_thread = threading.Thread(target=automation_loop, daemon=True)
        self.automation_thread.start()
        logger.debug(f"Automation thread started (id: {self.automation_thread.ident})")
        log_state_change(logger, "automation_manager", "starting", "running")
        log_function_return(logger, "start_automation")
    
    def stop_automation(self):
        """Stop the automated stream management process."""
        if not self.running:
            logger.warning("Automation is not running")
            return
        
        self.running = False
        self.automation_start_time = None
        logger.info("Stopping automated stream management...")
        
        if hasattr(self, 'automation_thread'):
            self.automation_thread.join(timeout=5)
        
        logger.info("Automated stream management stopped")
    
    def get_status(self) -> Dict:
        """Get current status of the automation system."""
        # Calculate next update time properly
        next_update = None
        if self.running:
            cron_expr = self.config.get("playlist_update_cron", "")
            if cron_expr and CRONITER_AVAILABLE:
                try:
                    # Use croniter to calculate next run time
                    base_time = self.last_playlist_update or self.automation_start_time or datetime.now()
                    cron = croniter(cron_expr, base_time)
                    next_update = cron.get_next(datetime)
                except Exception as e:
                    logger.warning(f"Invalid cron expression '{cron_expr}': {e}")
            
            if not next_update:
                # Fall back to interval-based calculation
                if self.last_playlist_update:
                    # Calculate when the next update should occur based on last update + interval
                    next_update = self.last_playlist_update + timedelta(minutes=self.config.get("playlist_update_interval_minutes", 5))
                elif self.automation_start_time:
                    # If automation is running but no last update, calculate from start time
                    next_update = self.automation_start_time + timedelta(minutes=self.config.get("playlist_update_interval_minutes", 5))
        
        return {
            "running": self.running,
            "last_playlist_update": self.last_playlist_update.isoformat() if self.last_playlist_update else None,
            "next_playlist_update": next_update.isoformat() if next_update else None,
            "config": self.config,
            "recent_changelog": self.changelog.get_recent_entries(7)
        }