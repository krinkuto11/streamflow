#!/usr/bin/env python3
"""
Automated Stream Manager for Dispatcharr

This module handles the automated process of:
1. Updating M3U playlists
2. Discovering new streams and assigning them to channels via regex
3. Maintaining changelog of updates

This module integrates with the caching layer to reduce API calls.
"""

import json
import logging
import os
import re
import time
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from collections import defaultdict

from api_utils import (
    refresh_m3u_playlists,
    get_m3u_accounts,
    get_streams,
    fetch_data_from_url,
    add_streams_to_channel,
    _get_base_url
)
from dispatcharr_cache import get_cache

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
    
    def add_entry(self, action: str, details: Dict, timestamp: Optional[str] = None):
        """Add a new changelog entry."""
        if timestamp is None:
            timestamp = datetime.now().isoformat()
        
        entry = {
            "timestamp": timestamp,
            "action": action,
            "details": details
        }
        
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
    
    def _has_channel_updates(self, entry: Dict) -> bool:
        """Check if a changelog entry contains meaningful channel/stream updates."""
        details = entry.get('details', {})
        action = entry.get('action', '')
        
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
                # Try to compile the pattern to check if it's valid
                re.compile(pattern)
            except re.error as e:
                return False, f"Invalid regex pattern '{pattern}': {str(e)}"
        
        return True, None
    
    def add_channel_pattern(self, channel_id: str, name: str, regex_patterns: List[str], enabled: bool = True):
        """Add or update a channel pattern.
        
        Args:
            channel_id: Channel ID
            name: Channel name
            regex_patterns: List of regex patterns
            enabled: Whether the pattern is enabled
            
        Raises:
            ValueError: If any regex pattern is invalid
        """
        # Validate patterns before saving
        is_valid, error_msg = self.validate_regex_patterns(regex_patterns)
        if not is_valid:
            raise ValueError(error_msg)
        
        self.channel_patterns["patterns"][str(channel_id)] = {
            "name": name,
            "regex": regex_patterns,
            "enabled": enabled
        }
        self._save_patterns(self.channel_patterns)
        logger.info(f"Added/updated pattern for channel {channel_id}: {name}")
    
    def reload_patterns(self):
        """Reload patterns from the config file.
        
        This is useful when patterns have been updated by another process
        and we need to ensure we're using the latest patterns.
        """
        self.channel_patterns = self._load_patterns()
        logger.debug("Reloaded regex patterns from config file")
    
    def match_stream_to_channels(self, stream_name: str) -> List[str]:
        """Match a stream name to channel IDs based on regex patterns."""
        matches = []
        case_sensitive = self.channel_patterns.get("global_settings", {}).get("case_sensitive", False)
        
        search_name = stream_name if case_sensitive else stream_name.lower()
        
        for channel_id, config in self.channel_patterns.get("patterns", {}).items():
            if not config.get("enabled", True):
                continue
            
            for pattern in config.get("regex", []):
                search_pattern = pattern if case_sensitive else pattern.lower()
                
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
            "enabled_m3u_accounts": [],  # Empty list means all accounts enabled
            "autostart_automation": False,  # Don't auto-start by default
            "enabled_features": {
                "auto_playlist_update": True,
                "auto_stream_discovery": True,
                "changelog_tracking": True
            }
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
    
    def refresh_playlists(self, force: bool = False) -> bool:
        """Refresh M3U playlists and track changes.
        
        Uses caching to reduce API calls during the refresh cycle.
        
        Args:
            force: If True, bypass the auto_playlist_update feature flag check.
                   Used for manual/quick action triggers from the UI.
        """
        # Use cache for the entire refresh cycle
        cache = get_cache()
        
        with cache:
            try:
                if not force and not self.config.get("enabled_features", {}).get("auto_playlist_update", True):
                    logger.info("Playlist update is disabled in configuration")
                    return False
                
                logger.info("Starting M3U playlist refresh (with caching enabled)...")
                
                # Get streams before refresh
                from api_utils import get_streams
                streams_before = get_streams(log_result=False) if self.config.get("enabled_features", {}).get("changelog_tracking", True) else []
                before_stream_ids = {s.get('id'): s.get('name', '') for s in streams_before if isinstance(s, dict) and s.get('id')}
                
                # Get all M3U accounts and filter out "custom" and non-active accounts
                all_accounts = get_m3u_accounts()
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
                
                # Invalidate cache after refresh to get fresh data
                cache.invalidate()
                logger.debug("Cache invalidated after M3U refresh")
                
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
                
                # Mark channels for stream quality checking ONLY if streams were added or removed
                # This prevents unnecessary marking of all channels on every refresh
                if len(added_streams) > 0 or len(removed_streams) > 0:
                    try:
                        # Get all channels that may have been affected
                        from api_utils import fetch_data_from_url, _get_base_url
                        base_url = _get_base_url()
                        channels_data = fetch_data_from_url(f"{base_url}/api/channels/channels/")
                        
                        if channels_data:
                            if isinstance(channels_data, dict) and 'results' in channels_data:
                                channels = channels_data['results']
                            else:
                                channels = channels_data
                            
                            # Mark all channels for checking with stream counts for 2-hour immunity
                            channel_ids = []
                            stream_counts = {}
                            for ch in channels:
                                if isinstance(ch, dict) and 'id' in ch:
                                    ch_id = ch['id']
                                    channel_ids.append(ch_id)
                                    # Get stream count if available
                                    if 'streams' in ch and isinstance(ch['streams'], list):
                                        stream_counts[ch_id] = len(ch['streams'])
                            
                            # Try to get stream checker service and mark channels
                            try:
                                from stream_checker_service import get_stream_checker_service
                                stream_checker = get_stream_checker_service()
                                stream_checker.update_tracker.mark_channels_updated(channel_ids, stream_counts=stream_counts)
                                logger.info(f"Marked {len(channel_ids)} channels for stream quality checking")
                                # Trigger immediate check instead of waiting for scheduled interval
                                stream_checker.trigger_check_updated_channels()
                            except Exception as sc_error:
                                logger.debug(f"Stream checker not available or error marking channels: {sc_error}")
                    except Exception as ch_error:
                        logger.debug(f"Could not mark channels for stream checking: {ch_error}")
                else:
                    logger.info("No stream changes detected, skipping channel marking")
                
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
    
    def discover_and_assign_streams(self, force: bool = False) -> Dict[str, int]:
        """Discover new streams and assign them to channels based on regex patterns.
        
        Uses caching to reduce API calls during discovery and assignment.
        
        Args:
            force: If True, bypass the auto_stream_discovery feature flag check.
                   Used for manual/quick action triggers from the UI.
        """
        if not force and not self.config.get("enabled_features", {}).get("auto_stream_discovery", True):
            logger.info("Stream discovery is disabled in configuration")
            return {}
        
        # Use cache for the entire discovery cycle
        cache = get_cache()
        
        with cache:
            try:
                # Reload patterns to ensure we have the latest changes
                self.regex_matcher.reload_patterns()
                
                logger.info("Starting stream discovery and assignment (with caching enabled)...")
                
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
                # Get all M3U accounts and filter by enabled and active status
                all_accounts = get_m3u_accounts()
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
                
                # Get all channels
                base_url = _get_base_url()
                all_channels = fetch_data_from_url(f"{base_url}/api/channels/channels/")
                if not all_channels:
                    logger.warning("No channels found")
                    return {}
                
                # Validate that all_channels is a list
                if not isinstance(all_channels, list):
                    logger.error(f"Invalid channels response format: expected list, got {type(all_channels).__name__}")
                    return {}
                
                # Create a map of existing channel streams
                channel_streams = {}
            channel_names = {}  # Store channel names for changelog
            for channel in all_channels:
                # Validate that channel is a dictionary
                if not isinstance(channel, dict) or 'id' not in channel:
                    logger.warning(f"Invalid channel format encountered: {type(channel).__name__} - {channel}")
                    continue
                    
                channel_id = str(channel['id'])
                channel_names[channel_id] = channel.get('name', f'Channel {channel_id}')
                streams = fetch_data_from_url(f"{base_url}/api/channels/channels/{channel_id}/streams/")
                if streams:
                    # Validate that streams is a list and contains dictionaries
                    if isinstance(streams, list):
                        valid_stream_ids = set()
                        for s in streams:
                            if isinstance(s, dict) and 'id' in s:
                                valid_stream_ids.add(s['id'])
                            else:
                                logger.warning(f"Invalid stream format in channel {channel_id}: {type(s).__name__} - {s}")
                        channel_streams[channel_id] = valid_stream_ids
                    else:
                        logger.warning(f"Invalid streams format for channel {channel_id}: expected list, got {type(streams).__name__}")
                        channel_streams[channel_id] = set()
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
                
                # Skip streams marked as dead in the tracker
                # Dead streams should not be added to channels during subsequent matches
                stream_url = stream.get('url', '')
                if self.dead_streams_tracker and self.dead_streams_tracker.is_dead(stream_url):
                    logger.debug(f"Skipping dead stream {stream_id}: {stream_name} (URL: {stream_url})")
                    continue
                
                # Find matching channels
                matching_channels = self.regex_matcher.match_stream_to_channels(stream_name)
                
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
            
            # Assign streams to channels
            for channel_id, stream_ids in assignments.items():
                if stream_ids:
                    try:
                        added_count = add_streams_to_channel(int(channel_id), stream_ids)
                        assignment_count[channel_id] = added_count
                        
                        # Verify streams were added correctly
                        if added_count > 0:
                            try:
                                time.sleep(0.5)  # Brief delay for API processing
                                base_url = _get_base_url()
                                updated_streams = fetch_data_from_url(f"{base_url}/api/channels/channels/{channel_id}/streams/")
                                
                                if updated_streams and isinstance(updated_streams, list):
                                    updated_stream_ids = set(s.get('id') for s in updated_streams if isinstance(s, dict) and 'id' in s)
                                    expected_stream_ids = set(stream_ids)
                                    added_stream_ids = expected_stream_ids & updated_stream_ids
                                    
                                    if len(added_stream_ids) == added_count:
                                        logger.info(f"✓ Verified: {added_count} streams successfully added to channel {channel_id} ({channel_names.get(channel_id, f'Channel {channel_id}')})")
                                    else:
                                        logger.warning(f"⚠ Verification mismatch for channel {channel_id}: expected {added_count} streams, found {len(added_stream_ids)} in channel")
                                else:
                                    logger.warning(f"⚠ Could not verify stream addition for channel {channel_id}: invalid response")
                            except Exception as verify_error:
                                logger.warning(f"⚠ Could not verify stream addition for channel {channel_id}: {verify_error}")
                        
                        # Prepare detailed assignment info
                        channel_assignment = {
                            "channel_id": channel_id,
                            "channel_name": channel_names.get(channel_id, f'Channel {channel_id}'),
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
                            # Fetch current stream count for this channel
                            try:
                                ch_streams = fetch_data_from_url(f"{base_url}/api/channels/channels/{channel_id}/streams/")
                                if ch_streams and isinstance(ch_streams, list):
                                    stream_counts[int(channel_id)] = len(ch_streams)
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
                                stream_checker.trigger_check_updated_channels()
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
    
    def should_run_playlist_update(self) -> bool:
        """Check if it's time to run playlist update."""
        if not self.last_playlist_update:
            return True
        
        interval = timedelta(minutes=self.config.get("playlist_update_interval_minutes", 5))
        return datetime.now() - self.last_playlist_update >= interval
    
    def run_automation_cycle(self):
        """Run one complete automation cycle."""
        # Check if a global action is in progress - if so, skip this cycle
        try:
            from stream_checker_service import get_stream_checker_service
            stream_checker = get_stream_checker_service()
            if stream_checker.global_action_in_progress:
                logger.debug("Skipping automation cycle - global action in progress")
                return
        except Exception as e:
            logger.debug(f"Could not check global action status: {e}")
        
        # Only log and run if it's actually time to update
        if not self.should_run_playlist_update():
            return  # Skip silently until it's time
        
        logger.info("Starting automation cycle...")
        
        # 1. Update playlists
        success = self.refresh_playlists()
        if success:
            # Small delay to allow playlist processing
            time.sleep(10)
            
            # 2. Discover and assign new streams
            assignments = self.discover_and_assign_streams()
        
        logger.info("Automation cycle completed")
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
                    logger.debug("Running automation cycle...")
                    self.run_automation_cycle()
                    logger.debug("Automation cycle completed, sleeping for 60 seconds")
                    
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