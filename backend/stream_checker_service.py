#!/usr/bin/env python3
"""
Stream Checker Service for Dispatcharr.

This service manages stream quality checking, rating, and ordering for
Dispatcharr channels. It implements a comprehensive system for maintaining
optimal stream quality across all channels.

Features:
    - Queue-based channel checking with priority support
    - Tracking of M3U playlist update events
    - Scheduled global checks during configurable off-peak hours
    - Progressive stream rating and automatic ordering
    - Real-time progress reporting via web API
    - Thread-safe operations with proper synchronization

The service runs continuously in the background, monitoring for channel
updates and maintaining a queue of channels that need checking. It
integrates with the stream_check_utils.py module for stream analysis.
"""

import json
import logging
import os
import threading
import time
from collections import defaultdict, deque, Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple, Any
import queue

from api_utils import (
    fetch_channel_streams,
    update_channel_streams,
    _get_base_url,
    patch_request,
    batch_update_stream_stats
)

# Import UDI for direct data access
from udi import get_udi_manager

# Import dead streams tracker
from dead_streams_tracker import DeadStreamsTracker

# Import channel settings manager
from channel_settings_manager import get_channel_settings_manager

# Import profile config
from profile_config import get_profile_config

# Import centralized stream stats utilities
from stream_stats_utils import (
    parse_bitrate_value,
    format_bitrate,
    parse_fps_value,
    format_fps,
    extract_stream_stats,
    format_stream_stats_for_display,
    calculate_channel_averages,
    is_stream_dead as utils_is_stream_dead
)

# Import changelog manager
try:
    from automated_stream_manager import ChangelogManager
    CHANGELOG_AVAILABLE = True
except ImportError:
    CHANGELOG_AVAILABLE = False

# Setup centralized logging
from logging_config import setup_logging, log_function_call, log_function_return, log_exception, log_state_change

logger = setup_logging(__name__)

# Configuration directory
CONFIG_DIR = Path(os.environ.get('CONFIG_DIR', '/app/data'))


class StreamCheckConfig:
    """Configuration for stream checking service."""
    
    DEFAULT_CONFIG = {
        'enabled': True,
        'check_interval': 300,  # DEPRECATED - checks now only triggered by M3U refresh
        # Individual automation controls
        'automation_controls': {
            'auto_m3u_updates': True,  # Automatically refresh M3U playlists
            'auto_stream_matching': True,  # Automatically match streams to channels via regex
            'auto_quality_checking': True,  # Automatically check stream quality
            'scheduled_global_action': False,  # Run scheduled global actions (update + match + check all)
            'remove_non_matching_streams': False  # Remove streams from channels if they no longer match regex
        },
        'global_check_schedule': {
            'enabled': True,
            'cron_expression': '0 3 * * *',  # Cron expression: default is daily at 3:00 AM
            'frequency': 'daily',  # DEPRECATED: kept for backward compatibility - 'daily' or 'monthly'
            'hour': 3,  # DEPRECATED: kept for backward compatibility - 3 AM for off-peak checking
            'minute': 0,  # DEPRECATED: kept for backward compatibility
            'day_of_month': 1  # DEPRECATED: kept for backward compatibility - Day of month for monthly checks (1-31)
        },
        'stream_analysis': {
            'ffmpeg_duration': 30,  # seconds to analyze each stream
            'timeout': 30,  # timeout for operations
            'stream_startup_buffer': 10,  # seconds buffer for stream startup (max time before stream starts)
            'retries': 1,  # retry attempts
            'retry_delay': 10,  # seconds between retries
            'user_agent': 'VLC/3.0.14'  # user agent for ffmpeg/ffprobe
        },
        'scoring': {
            'weights': {
                'bitrate': 0.40,
                'resolution': 0.35,
                'fps': 0.15,
                'codec': 0.10
            },
            'min_score': 0.0,  # minimum score to keep stream
            'prefer_h265': True  # prefer h265 over h264
        },
        'queue': {
            'max_size': 1000,
            'check_on_update': True,  # check channels when they receive M3U updates
            'max_channels_per_run': 50  # limit channels per check cycle
        },
        'concurrent_streams': {
            'global_limit': 10,  # Maximum concurrent stream checks globally (0 = unlimited)
            'enabled': True,  # Enable concurrent checking via Celery
            'stagger_delay': 1.0  # Delay in seconds between dispatching tasks to prevent simultaneous starts
        },
        'dead_stream_handling': {
            'enabled': True,  # Enable dead stream removal
            'min_resolution_width': 0,  # Minimum width in pixels (0 = no minimum, e.g., 1280 for 720p)
            'min_resolution_height': 0,  # Minimum height in pixels (0 = no minimum, e.g., 720 for 720p)
            'min_bitrate_kbps': 0,  # Minimum bitrate in kbps (0 = no minimum)
            'min_score': 0  # Minimum score (0-100, 0 = no minimum)
        },
        'batch_operations': {
            'enabled': True,  # Enable batch stats updates to reduce API calls
            'batch_size': 10,  # Number of streams to update per batch
            'verify_updates': False  # Verify channel updates by refreshing UDI (adds API overhead)
        }
    }
    
    def __init__(self, config_file: Optional[str] = None) -> None:
        """
        Initialize the StreamCheckConfig.
        
        Parameters:
            config_file (Optional[str]): Path to config file. Defaults
                to CONFIG_DIR/stream_checker_config.json.
        """
        if config_file is None:
            config_file = CONFIG_DIR / 'stream_checker_config.json'
        self.config_file = Path(config_file)
        self.config = self._load_config()
    
    def _load_config(self) -> Dict[str, Any]:
        """
        Load configuration from file or create default.
        
        Merges loaded config with DEFAULT_CONFIG to ensure all
        required keys exist even if config file is incomplete.
        
        Returns:
            Dict[str, Any]: The configuration dictionary.
        """
        import copy
        log_function_call(logger, "_load_config", config_file=str(self.config_file))
        
        if self.config_file.exists():
            logger.debug(f"Config file exists: {self.config_file}")
            try:
                with open(self.config_file, 'r', encoding='utf-8') as f:
                    loaded = json.load(f)
                    logger.debug(f"Loaded config with {len(loaded)} top-level keys")
                    # Deep copy defaults to avoid mutating DEFAULT_CONFIG
                    config = copy.deepcopy(self.DEFAULT_CONFIG)
                    config.update(loaded)
                    
                    # Auto-migrate legacy pipeline mode to automation_controls
                    pipeline_mode = config.get('pipeline_mode', '')
                    if pipeline_mode and pipeline_mode != 'disabled':
                        logger.info(f"Migrating legacy pipeline mode '{pipeline_mode}' to automation_controls")
                        
                        # Map pipeline modes to automation controls
                        if pipeline_mode == 'pipeline_1':
                            config['automation_controls'] = {
                                'auto_m3u_updates': True,
                                'auto_stream_matching': True,
                                'auto_quality_checking': True,
                                'scheduled_global_action': False
                            }
                        elif pipeline_mode == 'pipeline_1_5':
                            config['automation_controls'] = {
                                'auto_m3u_updates': True,
                                'auto_stream_matching': True,
                                'auto_quality_checking': True,
                                'scheduled_global_action': True
                            }
                        elif pipeline_mode == 'pipeline_2':
                            config['automation_controls'] = {
                                'auto_m3u_updates': True,
                                'auto_stream_matching': True,
                                'auto_quality_checking': False,
                                'scheduled_global_action': False
                            }
                        elif pipeline_mode == 'pipeline_2_5':
                            config['automation_controls'] = {
                                'auto_m3u_updates': True,
                                'auto_stream_matching': True,
                                'auto_quality_checking': False,
                                'scheduled_global_action': True
                            }
                        elif pipeline_mode == 'pipeline_3':
                            config['automation_controls'] = {
                                'auto_m3u_updates': False,
                                'auto_stream_matching': False,
                                'auto_quality_checking': False,
                                'scheduled_global_action': True
                            }
                        
                        # Remove pipeline_mode key
                        config.pop('pipeline_mode', None)
                        
                        # Save migrated config
                        self._save_config(config)
                        logger.info(f"Successfully migrated to automation_controls: {config['automation_controls']}")
                    
                    logger.debug(f"Merged config: automation_controls={config.get('automation_controls')}, enabled={config.get('enabled')}")
                    log_function_return(logger, "_load_config", f"<config with {len(config)} keys>")
                    return config
            except (json.JSONDecodeError, FileNotFoundError) as e:
                log_exception(logger, e, f"loading config from {self.config_file}")
                logger.warning(
                    f"Could not load config from "
                    f"{self.config_file}: {e}, using defaults"
                )
        else:
            logger.debug(f"Config file does not exist: {self.config_file}")
        
        # Create default config - use deep copy to avoid mutation
        logger.debug("Creating default config")
        self._save_config(copy.deepcopy(self.DEFAULT_CONFIG))
        log_function_return(logger, "_load_config", "<default config>")
        return copy.deepcopy(self.DEFAULT_CONFIG)
    
    def _save_config(
        self, config: Optional[Dict[str, Any]] = None
    ) -> None:
        """
        Save configuration to file.
        
        Parameters:
            config (Optional[Dict[str, Any]]): Config to save.
                Defaults to self.config.
        """
        if config is None:
            config = self.config
        
        self.config_file.parent.mkdir(parents=True, exist_ok=True)
        with open(self.config_file, 'w', encoding='utf-8') as f:
            json.dump(config, f, indent=2)
    
    def update(self, updates: Dict[str, Any]) -> None:
        """
        Update configuration with new values.
        
        Performs deep update to handle nested dictionaries.
        
        Parameters:
            updates (Dict[str, Any]): Configuration updates to apply.
        """
        def deep_update(
            base: Dict[str, Any], updates: Dict[str, Any]
        ) -> None:
            """Recursively update nested dictionaries."""
            for key, value in updates.items():
                if (isinstance(value, dict) and key in base and
                        isinstance(base[key], dict)):
                    deep_update(base[key], value)
                else:
                    base[key] = value
        
        deep_update(self.config, updates)
        self._save_config()
        logger.info("Stream checker configuration updated")
    
    def get(self, key: str, default: Any = None) -> Any:
        """
        Get configuration value using dot notation.
        
        Supports nested keys like 'queue.max_size'.
        
        Parameters:
            key (str): Configuration key (supports dot notation).
            default (Any): Default value if key not found.
            
        Returns:
            Any: The configuration value or default.
        """
        keys = key.split('.')
        value = self.config
        for k in keys:
            if isinstance(value, dict):
                value = value.get(k)
            else:
                return default
        return value if value is not None else default
    
    def is_auto_m3u_updates_enabled(self) -> bool:
        """Check if automatic M3U updates are enabled."""
        return self.config.get('automation_controls', {}).get('auto_m3u_updates', True)
    
    def is_auto_stream_matching_enabled(self) -> bool:
        """Check if automatic stream matching is enabled."""
        return self.config.get('automation_controls', {}).get('auto_stream_matching', True)
    
    def is_auto_quality_checking_enabled(self) -> bool:
        """Check if automatic quality checking is enabled."""
        return self.config.get('automation_controls', {}).get('auto_quality_checking', True)
    
    def is_scheduled_global_action_enabled(self) -> bool:
        """Check if scheduled global action is enabled."""
        return self.config.get('automation_controls', {}).get('scheduled_global_action', False)


class ChannelUpdateTracker:
    """Tracks which channels have received M3U updates."""
    
    def __init__(self, tracker_file=None):
        if tracker_file is None:
            tracker_file = CONFIG_DIR / 'channel_updates.json'
        self.tracker_file = Path(tracker_file)
        self.updates = self._load_updates()
        self.lock = threading.Lock()
        # Ensure the file is created on initialization
        self._save_updates()
    
    def _load_updates(self) -> Dict:
        """Load update tracking data."""
        if self.tracker_file.exists():
            try:
                with open(self.tracker_file, 'r') as f:
                    return json.load(f)
            except (json.JSONDecodeError, FileNotFoundError):
                logger.warning(f"Could not load updates from {self.tracker_file}, creating new")
        return {'channels': {}, 'last_global_check': None}
    
    def _save_updates(self):
        """Save update tracking data."""
        try:
            self.tracker_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self.tracker_file, 'w') as f:
                json.dump(self.updates, f, indent=2)
        except Exception as e:
            logger.error(f"Failed to save channel updates: {e}")
    
    def mark_channel_updated(self, channel_id: int, timestamp: str = None, stream_count: int = None):
        """Mark a channel as having received an update.
        
        Args:
            channel_id: The channel ID to mark as updated
            timestamp: When the update occurred (defaults to now)
            stream_count: Number of streams in the channel after update
        """
        if timestamp is None:
            timestamp = datetime.now().isoformat()
        
        with self.lock:
            if 'channels' not in self.updates:
                self.updates['channels'] = {}
            
            channel_key = str(channel_id)
            
            # Always mark channel as needing check if stream count changed
            # This ensures new streams are analyzed even during invulnerability period
            if channel_key in self.updates['channels']:
                channel_info = self.updates['channels'][channel_key]
                # Preserve checked_stream_ids if they exist
                checked_stream_ids = channel_info.get('checked_stream_ids', [])
                
                self.updates['channels'][channel_key] = {
                    'last_update': timestamp,
                    'needs_check': True,
                    'stream_count': stream_count,
                    'checked_stream_ids': checked_stream_ids
                }
            else:
                self.updates['channels'][channel_key] = {
                    'last_update': timestamp,
                    'needs_check': True,
                    'stream_count': stream_count,
                    'checked_stream_ids': []
                }
            self._save_updates()
    
    def mark_channels_updated(self, channel_ids: List[int], timestamp: str = None, stream_counts: Dict[int, int] = None):
        """Mark multiple channels as updated.
        
        Args:
            channel_ids: List of channel IDs to mark
            timestamp: When the update occurred (defaults to now)
            stream_counts: Optional dict mapping channel_id to stream count
        """
        if timestamp is None:
            timestamp = datetime.now().isoformat()
        
        if stream_counts is None:
            stream_counts = {}
        
        marked_count = 0
        
        with self.lock:
            if 'channels' not in self.updates:
                self.updates['channels'] = {}
            
            for channel_id in channel_ids:
                channel_key = str(channel_id)
                stream_count = stream_counts.get(channel_id)
                
                # Always mark channel if stream count changed (new streams added)
                # Preserve checked_stream_ids if they exist
                if channel_key in self.updates['channels']:
                    channel_info = self.updates['channels'][channel_key]
                    checked_stream_ids = channel_info.get('checked_stream_ids', [])
                    
                    self.updates['channels'][channel_key] = {
                        'last_update': timestamp,
                        'needs_check': True,
                        'stream_count': stream_count,
                        'checked_stream_ids': checked_stream_ids
                    }
                else:
                    self.updates['channels'][channel_key] = {
                        'last_update': timestamp,
                        'needs_check': True,
                        'stream_count': stream_count,
                        'checked_stream_ids': []
                    }
                marked_count += 1
            
            if marked_count > 0:
                self._save_updates()
        
        logger.info(f"Marked {marked_count} channels as updated")
    
    def get_channels_needing_check(self) -> List[int]:
        """Get list of channel IDs that need checking (read-only, doesn't clear flag).
        
        For actual queueing operations, use get_and_clear_channels_needing_check() instead
        to prevent race conditions.
        """
        with self.lock:
            channels = []
            for channel_id, info in self.updates.get('channels', {}).items():
                if info.get('needs_check', False):
                    channels.append(int(channel_id))
            return channels
    
    def get_and_clear_channels_needing_check(self, max_channels: int = None) -> List[int]:
        """Get list of channel IDs that need checking and atomically clear their needs_check flag.
        
        This atomic operation prevents race conditions where M3U refresh could
        re-mark channels while they're being queued.
        
        Args:
            max_channels: Maximum number of channels to return (None = all)
            
        Returns:
            List of channel IDs that were marked as needing check
        """
        with self.lock:
            channels = []
            timestamp = datetime.now().isoformat()
            
            for channel_id, info in self.updates.get('channels', {}).items():
                if info.get('needs_check', False):
                    channels.append(int(channel_id))
                    # Clear the flag immediately
                    info['needs_check'] = False
                    info['queued_at'] = timestamp
                    
                    if max_channels and len(channels) >= max_channels:
                        break
            
            # Filter channels by checking_mode setting (channel-level overrides group-level)
            # Need to get full channel data to access channel_group_id
            channel_settings = get_channel_settings_manager()
            udi = get_udi_manager()
            
            filtered_channels = []
            for cid in channels:
                # Get channel data to access group_id
                channel_data = None
                for ch in udi.get_channels():
                    if ch.get('id') == cid:
                        channel_data = ch
                        break
                
                if channel_data:
                    channel_group_id = channel_data.get('channel_group_id')
                    
                    # Check if channel has an explicit setting (not default)
                    channel_explicit_settings = channel_settings._settings.get(cid, {})
                    has_explicit_checking = 'checking_mode' in channel_explicit_settings
                    
                    if has_explicit_checking:
                        # Channel has explicit override - use it
                        if channel_settings.is_checking_enabled(cid):
                            filtered_channels.append(cid)
                    else:
                        # No channel override - use group setting (or default to enabled if no group)
                        if channel_settings.is_channel_enabled_by_group(channel_group_id, mode='checking'):
                            filtered_channels.append(cid)
                else:
                    # If we can't find channel data, use channel-level setting only
                    if channel_settings.is_checking_enabled(cid):
                        filtered_channels.append(cid)
            
            excluded_count = len(channels) - len(filtered_channels)
            
            if excluded_count > 0:
                logger.info(f"Excluding {excluded_count} channel(s) with checking disabled (channel or group level)")
            
            if filtered_channels:
                self._save_updates()
                logger.debug(f"Atomically retrieved and cleared {len(filtered_channels)} channels needing check")
            
            return filtered_channels
    
    def mark_channel_checked(self, channel_id: int, timestamp: str = None, stream_count: int = None, checked_stream_ids: List[int] = None):
        """Mark a channel as checked (completed).
        
        Args:
            channel_id: The channel ID to mark as checked
            timestamp: When the check was completed (defaults to now)
            stream_count: Number of streams in the channel
            checked_stream_ids: List of stream IDs that were checked
        """
        if timestamp is None:
            timestamp = datetime.now().isoformat()
        
        with self.lock:
            if 'channels' not in self.updates:
                self.updates['channels'] = {}
            
            channel_key = str(channel_id)
            if channel_key in self.updates['channels']:
                # Update existing entry
                self.updates['channels'][channel_key]['needs_check'] = False
                self.updates['channels'][channel_key]['last_check'] = timestamp
                if stream_count is not None:
                    self.updates['channels'][channel_key]['stream_count'] = stream_count
                if checked_stream_ids is not None:
                    self.updates['channels'][channel_key]['checked_stream_ids'] = checked_stream_ids
            else:
                # Create new entry
                self.updates['channels'][channel_key] = {
                    'needs_check': False,
                    'last_check': timestamp,
                    'stream_count': stream_count,
                    'checked_stream_ids': checked_stream_ids if checked_stream_ids is not None else []
                }
            self._save_updates()
    
    def get_checked_stream_ids(self, channel_id: int) -> List[int]:
        """Get the list of stream IDs that have been checked for a channel.
        
        Args:
            channel_id: The channel ID to query
            
        Returns:
            List of stream IDs that have been checked (empty list if none or channel not tracked)
        """
        with self.lock:
            channel_key = str(channel_id)
            if channel_key in self.updates.get('channels', {}):
                return self.updates['channels'][channel_key].get('checked_stream_ids', [])
            return []
    
    def mark_channel_for_force_check(self, channel_id: int):
        """Mark a channel for force checking (bypasses 2-hour immunity).
        
        Args:
            channel_id: The channel ID to mark for force check
        """
        with self.lock:
            if 'channels' not in self.updates:
                self.updates['channels'] = {}
            
            channel_key = str(channel_id)
            if channel_key not in self.updates['channels']:
                self.updates['channels'][channel_key] = {}
            
            self.updates['channels'][channel_key]['force_check'] = True
            self._save_updates()
    
    def should_force_check(self, channel_id: int) -> bool:
        """Check if a channel should be force checked (bypassing immunity).
        
        Args:
            channel_id: The channel ID to check
            
        Returns:
            True if force check is enabled for this channel
        """
        with self.lock:
            channel_key = str(channel_id)
            if channel_key in self.updates.get('channels', {}):
                return self.updates['channels'][channel_key].get('force_check', False)
            return False
    
    def clear_force_check(self, channel_id: int):
        """Clear the force check flag for a channel.
        
        Args:
            channel_id: The channel ID to clear force check for
        """
        with self.lock:
            channel_key = str(channel_id)
            if channel_key in self.updates.get('channels', {}):
                self.updates['channels'][channel_key]['force_check'] = False
                self._save_updates()
    
    def mark_global_check(self, timestamp: str = None):
        """Mark that a global check was initiated.
        
        This only updates the timestamp to prevent duplicate global checks.
        It does NOT clear needs_check flags - those should only be cleared
        when channels are actually checked via mark_channel_checked().
        """
        if timestamp is None:
            timestamp = datetime.now().isoformat()
        
        with self.lock:
            self.updates['last_global_check'] = timestamp
            self._save_updates()
    
    def get_last_global_check(self) -> Optional[str]:
        """Get timestamp of last global check."""
        return self.updates.get('last_global_check')


class StreamCheckQueue:
    """Queue manager for channel stream checking."""
    
    def __init__(self, max_size=1000):
        self.queue = queue.Queue(maxsize=max_size)
        self.queued = set()  # Track channels already in queue
        self.in_progress = set()
        self.completed = set()
        self.failed = {}
        self.lock = threading.Lock()
        self.stats = {
            'total_queued': 0,
            'total_completed': 0,
            'total_failed': 0,
            'current_channel': None,
            'queue_size': 0
        }
    
    def add_channel(self, channel_id: int, priority: int = 0):
        """Add a channel to the checking queue."""
        with self.lock:
            # Check if channel is already queued, in progress, or completed
            if channel_id not in self.queued and channel_id not in self.in_progress and channel_id not in self.completed:
                try:
                    self.queue.put((priority, channel_id), block=False)
                    self.queued.add(channel_id)
                    self.stats['total_queued'] += 1
                    self.stats['queue_size'] = self.queue.qsize()
                    logger.debug(f"Added channel {channel_id} to queue (priority: {priority})")
                    return True
                except queue.Full:
                    logger.warning(f"Queue is full, cannot add channel {channel_id}")
                    return False
        return False
    
    def add_channels(self, channel_ids: List[int], priority: int = 0):
        """Add multiple channels to the queue."""
        added = 0
        for channel_id in channel_ids:
            if self.add_channel(channel_id, priority):
                added += 1
        logger.info(f"Added {added}/{len(channel_ids)} channels to checking queue")
        return added
    
    def remove_from_completed(self, channel_id: int):
        """Remove a channel from the completed set to allow re-queueing.
        
        This is used when a channel receives new streams and needs to be
        checked again, even if it was previously completed.
        """
        with self.lock:
            if channel_id in self.completed:
                self.completed.discard(channel_id)
                logger.debug(f"Removed channel {channel_id} from completed set")
                return True
        return False
    
    def get_next_channel(self, timeout: float = 1.0) -> Optional[int]:
        """Get the next channel to check."""
        try:
            priority, channel_id = self.queue.get(timeout=timeout)
            with self.lock:
                self.queued.discard(channel_id)  # Remove from queued set
                self.in_progress.add(channel_id)
                self.stats['current_channel'] = channel_id
                self.stats['queue_size'] = self.queue.qsize()
            return channel_id
        except queue.Empty:
            return None
    
    def mark_completed(self, channel_id: int):
        """Mark a channel check as completed."""
        with self.lock:
            if channel_id in self.in_progress:
                self.in_progress.remove(channel_id)
            self.completed.add(channel_id)
            self.stats['total_completed'] += 1
            if self.stats['current_channel'] == channel_id:
                self.stats['current_channel'] = None
            logger.debug(f"Marked channel {channel_id} as completed")
    
    def mark_failed(self, channel_id: int, error: str):
        """Mark a channel check as failed."""
        with self.lock:
            if channel_id in self.in_progress:
                self.in_progress.remove(channel_id)
            self.failed[channel_id] = {
                'error': error,
                'timestamp': datetime.now().isoformat()
            }
            self.stats['total_failed'] += 1
            if self.stats['current_channel'] == channel_id:
                self.stats['current_channel'] = None
            logger.warning(f"Marked channel {channel_id} as failed: {error}")
    
    def get_status(self) -> Dict:
        """Get current queue status."""
        with self.lock:
            return {
                'queue_size': self.queue.qsize(),
                'queued': len(self.queued),
                'in_progress': len(self.in_progress),
                'completed': len(self.completed),
                'failed': len(self.failed),
                'current_channel': self.stats['current_channel'],
                'total_queued': self.stats['total_queued'],
                'total_completed': self.stats['total_completed'],
                'total_failed': self.stats['total_failed']
            }
    
    def clear(self):
        """Clear the queue and reset stats."""
        with self.lock:
            while not self.queue.empty():
                try:
                    self.queue.get_nowait()
                except queue.Empty:
                    break
            self.queued.clear()
            self.in_progress.clear()
            self.completed.clear()
            self.failed.clear()
            self.stats = {
                'total_queued': 0,
                'total_completed': 0,
                'total_failed': 0,
                'current_channel': None,
                'queue_size': 0
            }
        logger.info("Queue cleared")


class StreamCheckerProgress:
    """Manages progress tracking for stream checker operations."""
    
    def __init__(self, progress_file=None):
        if progress_file is None:
            progress_file = CONFIG_DIR / 'stream_checker_progress.json'
        self.progress_file = Path(progress_file)
        self.lock = threading.Lock()
    
    def update(self, channel_id: int, channel_name: str, current: int, total: int,
               current_stream: str = '', status: str = 'checking', step: str = '', step_detail: str = ''):
        """Update progress information.
        
        Args:
            channel_id: The ID of the channel being checked
            channel_name: The name of the channel
            current: Current stream number being processed
            total: Total number of streams
            current_stream: Name of the current stream
            status: Overall status (checking, analyzing, updating, etc.)
            step: Current step in the process (e.g., "Fetching streams", "Analyzing", "Scoring", "Reordering")
            step_detail: Additional detail about the current step
        """
        with self.lock:
            progress_data = {
                'channel_id': channel_id,
                'channel_name': channel_name,
                'current_stream': current,
                'total_streams': total,
                'percentage': round((current / total * 100) if total > 0 else 0, 1),
                'current_stream_name': current_stream,
                'status': status,
                'step': step,
                'step_detail': step_detail,
                'timestamp': datetime.now().isoformat()
            }
            
            self.progress_file.parent.mkdir(parents=True, exist_ok=True)
            try:
                with open(self.progress_file, 'w') as f:
                    json.dump(progress_data, f)
                    f.flush()
                    os.fsync(f.fileno())
            except Exception as e:
                logger.warning(f"Failed to write progress file: {e}")
    
    def clear(self):
        """Clear progress tracking."""
        with self.lock:
            if self.progress_file.exists():
                try:
                    self.progress_file.unlink()
                except Exception as e:
                    logger.warning(f"Failed to delete progress file: {e}")
    
    def get(self) -> Optional[Dict]:
        """Get current progress."""
        with self.lock:
            if self.progress_file.exists():
                try:
                    with open(self.progress_file, 'r') as f:
                        return json.load(f)
                except (json.JSONDecodeError, FileNotFoundError):
                    pass
        return None


class StreamCheckerService:
    """Main service for managing stream checking operations."""
    
    def __init__(self):
        log_function_call(logger, "__init__")
        logger.debug("Initializing StreamCheckerService components...")
        
        self.config = StreamCheckConfig()
        logger.debug(f"Config loaded: pipeline_mode={self.config.get('pipeline_mode')}")
        
        self.update_tracker = ChannelUpdateTracker()
        logger.debug("Update tracker initialized")
        
        self.check_queue = StreamCheckQueue(
            max_size=self.config.get('queue.max_size', 1000)
        )
        logger.debug(f"Check queue initialized with max_size={self.config.get('queue.max_size', 1000)}")
        
        self.progress = StreamCheckerProgress()
        logger.debug("Progress tracker initialized")
        
        self.dead_streams_tracker = DeadStreamsTracker()
        logger.debug("Dead streams tracker initialized")
        
        # Initialize changelog manager
        self.changelog = None
        if CHANGELOG_AVAILABLE:
            try:
                self.changelog = ChangelogManager(changelog_file=CONFIG_DIR / "stream_checker_changelog.json")
                logger.info("Stream checker changelog manager initialized")
            except Exception as e:
                log_exception(logger, e, "changelog initialization")
                logger.warning(f"Failed to initialize changelog manager: {e}")
        
        # Batch changelog tracking
        self.batch_changelog_entries = []
        self.batch_start_time = None
        self.batch_lock = threading.Lock()
        
        self.running = False
        self.checking = False
        self.global_action_in_progress = False
        self.worker_thread = None
        self.scheduler_thread = None
        self.lock = threading.Lock()
        
        # Event for immediate triggering of updated channels check
        self.check_trigger = threading.Event()
        logger.debug("Check trigger event created")
        
        # Event for immediate config change notification
        self.config_changed = threading.Event()
        logger.debug("Config changed event created")
        
        logger.info("Stream Checker Service initialized")
        log_function_return(logger, "__init__")
    
    def start(self):
        """Start the stream checker service."""
        log_function_call(logger, "start")
        with self.lock:
            if self.running:
                logger.warning("Stream checker service is already running")
                return
            
            log_state_change(logger, "stream_checker_service", "stopped", "starting")
            self.running = True
            
            # Start worker thread for processing queue
            self.worker_thread = threading.Thread(target=self._worker_loop, daemon=True)
            self.worker_thread.start()
            logger.debug(f"Worker thread started (id: {self.worker_thread.ident})")
            
            # Start scheduler thread for periodic checks
            self.scheduler_thread = threading.Thread(target=self._scheduler_loop, daemon=True)
            self.scheduler_thread.start()
            logger.debug(f"Scheduler thread started (id: {self.scheduler_thread.ident})")
            
            log_state_change(logger, "stream_checker_service", "starting", "running")
            logger.info("Stream checker service started")
            log_function_return(logger, "start")
    
    def stop(self):
        """Stop the stream checker service."""
        with self.lock:
            if not self.running:
                logger.warning("Stream checker service is not running")
                return
            
            self.running = False
            logger.info("Stream checker service stopping...")
        
        # Wait for threads to finish
        if self.worker_thread and self.worker_thread.is_alive():
            self.worker_thread.join(timeout=5)
        if self.scheduler_thread and self.scheduler_thread.is_alive():
            self.scheduler_thread.join(timeout=5)
        
        self.progress.clear()
        logger.info("Stream checker service stopped")
    
    def _worker_loop(self):
        """Main worker loop for processing the check queue."""
        log_function_call(logger, "_worker_loop")
        logger.info("Stream checker worker started")
        
        while self.running:
            try:
                logger.debug("Worker waiting for next channel from queue...")
                channel_id = self.check_queue.get_next_channel(timeout=1.0)
                if channel_id is None:
                    # No channel in queue - check if we should finalize a batch
                    if self.batch_start_time is not None:
                        # Queue is empty and we have an active batch - finalize it
                        self._finalize_batch_changelog()
                    logger.debug("No channel in queue (timeout)")
                    continue
                
                # Start a new batch if not already started
                if self.batch_start_time is None:
                    self._start_batch_changelog()
                
                logger.debug(f"Worker processing channel {channel_id}")
                # Check this channel
                self._check_channel(channel_id)
                logger.debug(f"Worker completed channel {channel_id}")
                
            except Exception as e:
                log_exception(logger, e, "worker loop")
                logger.error(f"Error in worker loop: {e}", exc_info=True)
        
        # Finalize any remaining batch before stopping
        if self.batch_start_time is not None:
            self._finalize_batch_changelog()
        
        logger.info("Stream checker worker stopped")
        log_function_return(logger, "_worker_loop")
    
    def _scheduler_loop(self):
        """Scheduler loop for M3U update-triggered and scheduled checks."""
        logger.info("Stream checker scheduler started")
        
        while self.running:
            try:
                # Wait for either a trigger event or timeout (60 seconds for global check monitoring)
                triggered = self.check_trigger.wait(timeout=60)
                
                # Handle trigger for M3U updates
                if triggered:
                    self.check_trigger.clear()
                    # Only process channel queueing if this was a real M3U update trigger
                    # (not a config change wake-up) AND no global action is in progress
                    if not self.config_changed.is_set():
                        if self.global_action_in_progress:
                            logger.info("Skipping channel queueing - global action in progress")
                        else:
                            # Call _queue_updated_channels() directly - it handles pipeline mode checking internally
                            self._queue_updated_channels()
                
                # Check if config was changed
                if self.config_changed.is_set():
                    self.config_changed.clear()
                    logger.info("Configuration change detected, applying new settings immediately")
                
                # Check if it's time for a global check (checked on every iteration)
                # This will set global_action_in_progress if a global action is triggered
                if not self.global_action_in_progress:
                    self._check_global_schedule()
                
            except Exception as e:
                logger.error(f"Error in scheduler loop: {e}", exc_info=True)
        
        logger.info("Stream checker scheduler stopped")
    
    def _queue_updated_channels(self):
        """Queue channels that have received M3U updates.
        
        This respects the pipeline mode:
        - Disabled: Skip all automation
        - Pipeline 1/1.5: Queue channels for checking
        - Pipeline 2/2.5: Skip checking (only update and match)
        - Pipeline 3: Skip checking (only scheduled global actions)
        """
        # Check if auto quality checking is enabled (considers both pipeline mode and individual controls)
        if not self.config.is_auto_quality_checking_enabled():
            logger.info("Skipping channel queueing - automatic quality checking is disabled")
            return
        
        max_channels = self.config.get('queue.max_channels_per_run', 50)
        
        # Atomically get channels and clear their needs_check flag
        # This prevents duplicate queueing if M3U refresh happens during check
        channels_to_queue = self.update_tracker.get_and_clear_channels_needing_check(max_channels)
        
        if channels_to_queue:
            # Remove channels from completed set to allow re-queueing
            # This is necessary when channels receive new streams after being checked
            for channel_id in channels_to_queue:
                self.check_queue.remove_from_completed(channel_id)
            
            added = self.check_queue.add_channels(channels_to_queue, priority=10)
            logger.info(f"Queued {added}/{len(channels_to_queue)} updated channels for checking")
        else:
            logger.debug("No channels need checking")
    
    def _check_global_schedule(self):
        """Check if it's time for a scheduled global action.
        
        Uses cron expression to determine when to run the global action.
        
        On fresh start (no previous check recorded):
        - Only runs if current time is within ±10 minutes of the next scheduled time
        - Otherwise waits for the scheduled time to arrive
        
        On subsequent checks (previous check exists):
        - Runs if the next scheduled time has passed since the last check
        - Prevents duplicate runs by tracking the last check time
        """
        if not self.config.get('global_check_schedule.enabled', True):
            logger.debug("Global check schedule is disabled")
            return
        
        # Check if scheduled global action is enabled (considers both pipeline mode and individual controls)
        if not self.config.is_scheduled_global_action_enabled():
            logger.debug("Skipping global schedule check - scheduled global actions are disabled")
            return
        
        now = datetime.now()
        
        # Get cron expression, with backward compatibility for old config format
        cron_expression = self.config.get('global_check_schedule.cron_expression')
        if not cron_expression:
            # Backward compatibility: convert old format to cron
            cron_expression = self._convert_legacy_schedule_to_cron()
        
        try:
            from croniter import croniter
        except ImportError:
            logger.error("croniter library not installed. Please install it with: pip install croniter")
            return
        
        # Validate cron expression
        if not croniter.is_valid(cron_expression):
            logger.error(f"Invalid cron expression: {cron_expression}")
            return
        
        last_global = self.update_tracker.get_last_global_check()
        
        # Calculate next scheduled time from now
        cron = croniter(cron_expression, now)
        next_scheduled_time = cron.get_next(datetime)
        
        # Calculate previous scheduled time (going back from now)
        cron_prev = croniter(cron_expression, now)
        prev_scheduled_time = cron_prev.get_prev(datetime)
        
        # On fresh start (no previous check), only run if within the scheduled time window (±10 minutes)
        # Otherwise, do nothing and wait for the scheduled time to arrive
        if last_global is None:
            time_diff_minutes = abs((now - prev_scheduled_time).total_seconds() / 60)
            if time_diff_minutes <= 10:
                # We're within the scheduled window on fresh start, run the check
                automation_controls = self.config.get('automation_controls', {})
                logger.info(f"Starting scheduled global action (automation_controls: {automation_controls}, cron: {cron_expression})")
                self._perform_global_action()
                self.update_tracker.mark_global_check()
            else:
                # Fresh start but not within scheduled window, do nothing and wait
                # The scheduler will check again later when the scheduled time arrives
                logger.debug(f"Fresh start outside scheduled window (±10 min of {prev_scheduled_time.strftime('%Y-%m-%d %H:%M')}), waiting for scheduled time")
            return
        
        # Parse last check time
        last_check_time = datetime.fromisoformat(last_global)
        
        # Check if we've passed the previous scheduled time since the last check
        # This prevents running multiple times between scheduled intervals
        if prev_scheduled_time > last_check_time:
            # We've passed a scheduled time since the last check, so we should run
            automation_controls = self.config.get('automation_controls', {})
            logger.info(f"Starting scheduled global action (automation_controls: {automation_controls}, cron: {cron_expression})")
            self._perform_global_action()
            # Mark that global check has been initiated to prevent duplicate queueing
            self.update_tracker.mark_global_check()
    
    def _convert_legacy_schedule_to_cron(self):
        """Convert legacy schedule format (hour/minute/frequency) to cron expression.
        
        This provides backward compatibility for existing configurations.
        """
        frequency = self.config.get('global_check_schedule.frequency', 'daily')
        hour = self.config.get('global_check_schedule.hour', 3)
        minute = self.config.get('global_check_schedule.minute', 0)
        
        if frequency == 'monthly':
            day_of_month = self.config.get('global_check_schedule.day_of_month', 1)
            # Monthly on specific day: minute hour day * *
            cron_expression = f"{minute} {hour} {day_of_month} * *"
        else:
            # Daily: minute hour * * *
            cron_expression = f"{minute} {hour} * * *"
        
        logger.info(f"Converted legacy schedule to cron: {cron_expression}")
        return cron_expression
    
    def _perform_global_action(self):
        """Perform a complete global action: Refresh UDI, Update M3U, Match streams, and Check all channels.
        
        This is the comprehensive global action that:
        1. Refreshes UDI cache to ensure current data from Dispatcharr
        2. Clears ALL dead streams from tracker to give them a second chance
        3. Reloads enabled M3U accounts
        4. Matches new streams with regex patterns (including previously dead ones)
        5. Checks every channel from every stream (bypassing 2-hour immunity)
        6. Disables empty channels if configured
        
        During this operation, regular automated updates, matching, and checking are paused.
        """
        try:
            # Set global action flag to prevent concurrent operations
            self.global_action_in_progress = True
            logger.info("=" * 80)
            logger.info("STARTING GLOBAL ACTION")
            logger.info("Regular automation paused during global action")
            logger.info("=" * 80)
            
            # Step 1: Refresh UDI cache to ensure we have current data from Dispatcharr
            logger.info("Step 1/6: Refreshing UDI cache...")
            try:
                from udi import get_udi_manager
                udi = get_udi_manager()
                refresh_success = udi.refresh_all()
                if refresh_success:
                    logger.info("✓ UDI cache refreshed successfully")
                else:
                    logger.warning("⚠ UDI cache refresh had issues")
            except Exception as e:
                logger.error(f"✗ Failed to refresh UDI cache: {e}")
            
            # Step 2: Clear ALL dead streams from tracker to give them a second chance
            logger.info("Step 2/6: Clearing dead stream tracker to give all streams a second chance...")
            try:
                dead_count = len(self.dead_streams_tracker.get_dead_streams())
                if dead_count > 0:
                    logger.info(f"Found {dead_count} dead stream(s) before clearing")
                    self.dead_streams_tracker.clear_all_dead_streams()
                    logger.info(f"✓ Cleared {dead_count} dead stream(s) from tracker - they will be given a second chance")
                    
                    # Verify the streams were actually cleared
                    remaining_dead = len(self.dead_streams_tracker.get_dead_streams())
                    if remaining_dead > 0:
                        logger.warning(f"⚠ {remaining_dead} dead stream(s) still remain after clearing - this may indicate an issue")
                    else:
                        logger.info("✓ Verified: All dead streams successfully removed from tracker")
                else:
                    logger.info("✓ No dead streams to clear from tracker")
            except Exception as e:
                logger.error(f"✗ Failed to clear dead streams: {e}", exc_info=True)
            
            automation_manager = None
            
            # Step 3: Update M3U playlists
            logger.info("Step 3/6: Updating M3U playlists...")
            try:
                from automated_stream_manager import AutomatedStreamManager
                automation_manager = AutomatedStreamManager()
                update_success = automation_manager.refresh_playlists()
                if update_success:
                    logger.info("✓ M3U playlists updated successfully")
                else:
                    logger.warning("⚠ M3U playlist update had issues")
            except Exception as e:
                logger.error(f"✗ Failed to update M3U playlists: {e}")
            
            # Step 4: Validate and remove non-matching streams
            logger.info("Step 4/6: Validating existing streams against regex patterns...")
            try:
                if automation_manager is not None:
                    # Respect automation_controls.remove_non_matching_streams setting
                    validation_results = automation_manager.validate_and_remove_non_matching_streams()
                    if validation_results.get("streams_removed", 0) > 0:
                        logger.info(f"✓ Removed {validation_results['streams_removed']} non-matching streams from {validation_results['channels_modified']} channels")
                    else:
                        logger.info("✓ No non-matching streams found to remove")
                else:
                    logger.warning("⚠ Skipping stream validation - automation manager not available")
            except Exception as e:
                logger.error(f"✗ Failed to validate streams: {e}")
            
            # Step 5: Match and assign streams (including previously dead ones since tracker was cleared)
            logger.info("Step 5/6: Matching and assigning streams...")
            try:
                if automation_manager is not None:
                    assignments = automation_manager.discover_and_assign_streams()
                    if assignments:
                        logger.info(f"✓ Assigned streams to {len(assignments)} channels")
                    else:
                        logger.info("✓ No new stream assignments")
                else:
                    logger.warning("⚠ Skipping stream matching - automation manager not available")
            except Exception as e:
                logger.error(f"✗ Failed to match streams: {e}")
            
            # Step 6: Check all channels (force check to bypass immunity)
            logger.info("Step 6/6: Queueing all channels for checking...")
            self._queue_all_channels(force_check=True)
            
            # Note: Empty channel disabling will be triggered after batch finalization
            
            logger.info("=" * 80)
            logger.info("GLOBAL ACTION INITIATED SUCCESSFULLY")
            logger.info("Regular automation will resume")
            logger.info("=" * 80)
            
        except Exception as e:
            logger.error(f"Error performing global action: {e}", exc_info=True)
        finally:
            # Always clear the flag, even if there was an error
            self.global_action_in_progress = False
    
    def _queue_all_channels(self, force_check: bool = False):
        """Queue all channels for checking (global check).
        
        Args:
            force_check: If True, marks channels for force checking which bypasses 2-hour immunity
        """
        try:
            udi = get_udi_manager()
            channels = udi.get_channels()
            
            if channels:
                channel_ids = [ch['id'] for ch in channels if isinstance(ch, dict) and 'id' in ch]
                
                # Filter by profile if one is selected
                profile_config = get_profile_config()
                
                if profile_config.is_using_profile():
                    selected_profile_id = profile_config.get_selected_profile()
                    if selected_profile_id:
                        try:
                            # Get channels that are enabled in this profile from UDI
                            profile_data = udi.get_profile_channels(selected_profile_id)
                            
                            # According to Dispatcharr API, profile_data.channels is a list of channel IDs
                            profile_channel_ids = {
                                ch_id for ch_id in profile_data.get('channels', []) 
                                if isinstance(ch_id, int)
                            }
                            
                            # Filter channels to only those in the profile
                            channels = [ch for ch in channels if ch.get('id') in profile_channel_ids]
                            channel_ids = [ch['id'] for ch in channels]
                            
                            profile_name = profile_config.get_config().get('selected_profile_name', 'Unknown')
                            logger.info(f"Profile filter active: Using {len(channel_ids)} channels from profile '{profile_name}'")
                        except Exception as e:
                            logger.error(f"Failed to load profile channels, using all channels: {e}")
                
                # Filter channels by checking_mode setting (channel-level overrides group-level)
                channel_settings = get_channel_settings_manager()
                filtered_channel_ids = []
                
                for ch in channels:
                    if not isinstance(ch, dict) or 'id' not in ch:
                        continue
                    
                    cid = ch['id']
                    channel_group_id = ch.get('channel_group_id')
                    
                    # Check if channel has an explicit setting (not default)
                    channel_explicit_settings = channel_settings._settings.get(cid, {})
                    has_explicit_checking = 'checking_mode' in channel_explicit_settings
                    
                    if has_explicit_checking:
                        # Channel has explicit override - use it
                        if channel_settings.is_checking_enabled(cid):
                            filtered_channel_ids.append(cid)
                    else:
                        # No channel override - use group setting (or default to enabled if no group)
                        if channel_settings.is_channel_enabled_by_group(channel_group_id, mode='checking'):
                            filtered_channel_ids.append(cid)
                
                excluded_count = len(channel_ids) - len(filtered_channel_ids)
                
                if excluded_count > 0:
                    logger.info(f"Excluding {excluded_count} channel(s) with checking disabled (channel or group level) from global action")
                
                if not filtered_channel_ids:
                    logger.info("No channels with checking enabled to queue for global check")
                    return
                
                if force_check:
                    # Mark all enabled channels for force check (bypasses immunity)
                    for channel_id in filtered_channel_ids:
                        self.update_tracker.mark_channel_for_force_check(channel_id)
                
                # Remove channels from completed set to allow re-queueing
                # This is necessary for global checks to re-check all channels
                for channel_id in filtered_channel_ids:
                    self.check_queue.remove_from_completed(channel_id)
                
                max_channels = self.config.get('queue.max_channels_per_run', 50)
                
                # Queue in batches with higher priority for global checks
                total_added = 0
                for i in range(0, len(filtered_channel_ids), max_channels):
                    batch = filtered_channel_ids[i:i+max_channels]
                    added = self.check_queue.add_channels(batch, priority=5)
                    total_added += added
                
                logger.info(f"Queued {total_added}/{len(filtered_channel_ids)} channels for global check (force_check={force_check})")
        except Exception as e:
            logger.error(f"Failed to queue all channels: {e}")
    
    def _is_stream_dead(self, stream_data: Dict) -> bool:
        """Check if a stream should be considered dead based on analysis results.
        
        Uses centralized utility function for consistent dead stream detection
        with configurable thresholds.
        
        Args:
            stream_data: Analyzed stream data dictionary
            
        Returns:
            bool: True if stream is dead, False otherwise
        """
        # Get dead stream handling configuration
        dead_stream_config = self.config.get('dead_stream_handling', {})
        
        # If dead stream handling is disabled, never consider streams dead
        # (except for the 0x0 resolution and 0 bitrate cases which are always dead)
        if not dead_stream_config.get('enabled', True):
            # Only check for absolute failures (0x0 resolution, 0 bitrate)
            basic_config = {
                'min_resolution_width': 0,
                'min_resolution_height': 0,
                'min_bitrate_kbps': 0,
                'min_score': 0
            }
            return utils_is_stream_dead(stream_data, basic_config)
        
        # Pass the configuration to the utility function
        return utils_is_stream_dead(stream_data, dead_stream_config)
    
    def _calculate_channel_averages(self, analyzed_streams: List[Dict], dead_stream_ids: set) -> Dict[str, str]:
        """Calculate channel-level average statistics from analyzed streams.
        
        Uses centralized utility function for consistent average calculation.
        
        Args:
            analyzed_streams: List of analyzed stream dictionaries
            dead_stream_ids: Set of stream IDs that are marked as dead
            
        Returns:
            Dictionary with avg_resolution, avg_bitrate, and avg_fps
        """
        return calculate_channel_averages(analyzed_streams, dead_stream_ids)
    
    def _get_m3u_account_name(self, stream_id: int, udi=None) -> Optional[str]:
        """Get the M3U account name for a stream.
        
        Args:
            stream_id: The stream ID to look up
            udi: Optional UDI manager instance (will fetch if not provided)
            
        Returns:
            M3U account name or None if not found
        """
        try:
            if udi is None:
                udi = get_udi_manager()
            
            stream_data = udi.get_stream_by_id(stream_id)
            if not stream_data:
                return None
            
            m3u_account_id = stream_data.get('m3u_account')
            if not m3u_account_id:
                return None
            
            m3u_account = udi.get_m3u_account_by_id(m3u_account_id)
            if not m3u_account:
                return None
            
            return m3u_account.get('name', 'Unknown')
        except Exception as e:
            logger.debug(f"Could not fetch M3U account for stream {stream_id}: {e}")
            return None
    
    
    def _update_stream_stats(self, stream_data: Dict) -> bool:
        """Update stream stats for a single stream on the server and sync with UDI cache.
        
        This method:
        1. Constructs the stats payload from analyzed stream data
        2. Merges with existing stats on Dispatcharr
        3. PATCHes the updated stats to Dispatcharr
        4. Updates the UDI cache to keep it in sync
        
        This ensures that the UDI cache always reflects the latest stats written to Dispatcharr,
        preventing inconsistencies between changelog data and actual Dispatcharr data.
        """
        base_url = _get_base_url()
        if not base_url:
            logger.error("DISPATCHARR_BASE_URL not set.")
            return False
        
        stream_id = stream_data.get("stream_id")
        if not stream_id:
            logger.warning("No stream_id in stream data. Skipping stats update.")
            return False
        
        # Construct the stream stats payload from the analyzed stream data
        stream_stats_payload = {
            "resolution": stream_data.get("resolution"),
            "source_fps": stream_data.get("fps"),
            "video_codec": stream_data.get("video_codec"),
            "audio_codec": stream_data.get("audio_codec"),
            "ffmpeg_output_bitrate": int(stream_data.get("bitrate_kbps")) if stream_data.get("bitrate_kbps") not in ["N/A", None] else None,
            # Extended stream metadata
            "video_profile": stream_data.get("video_profile"),
            "video_level": stream_data.get("video_level"),
            "video_bit_depth": stream_data.get("video_bit_depth"),
            "video_ref_frames": stream_data.get("video_ref_frames"),
            "audio_language": stream_data.get("audio_language"),
            "sample_rate": stream_data.get("sample_rate"),
            "audio_channels": stream_data.get("audio_channels"),
        }

        # Clean up the payload, removing any None values or N/A values
        stream_stats_payload = {k: v for k, v in stream_stats_payload.items() if v not in [None, "N/A"]}

        if not stream_stats_payload:
            logger.debug(f"No data to update for stream {stream_id}. Skipping.")
            return False

        # Construct the URL for the specific stream
        stream_url = f"{base_url}/api/channels/streams/{int(stream_id)}/"
        
        try:
            # Fetch the existing stream data from UDI
            udi = get_udi_manager()
            existing_stream_data = udi.get_stream_by_id(int(stream_id))
            if not existing_stream_data:
                logger.warning(f"Could not fetch existing data for stream {stream_id}. Skipping stats update.")
                return False
            
            # Get the existing stream_stats or an empty dict
            existing_stats = existing_stream_data.get("stream_stats") or {}
            if isinstance(existing_stats, str):
                try:
                    existing_stats = json.loads(existing_stats)
                except json.JSONDecodeError:
                    existing_stats = {}
            
            # Merge the existing stats with the new payload
            updated_stats = {**existing_stats, **stream_stats_payload}
            
            # Send the PATCH request with the updated stream_stats
            patch_payload = {"stream_stats": updated_stats}
            logger.info(f"Updating stream {stream_id} stats with: {stream_stats_payload}")
            patch_request(stream_url, patch_payload)
            
            # Update UDI cache with the new stats to keep it in sync with Dispatcharr
            # This ensures changelog and verification read the correct, up-to-date data
            updated_stream_data = existing_stream_data.copy()
            updated_stream_data['stream_stats'] = updated_stats
            udi.update_stream(int(stream_id), updated_stream_data)
            logger.debug(f"Updated UDI cache for stream {stream_id} with new stats")
            
            return True
        
        except Exception as e:
            logger.error(f"Error updating stats for stream {stream_id}: {e}")
            return False
    
    def _prepare_stream_stats_for_batch(self, stream_data: Dict) -> Optional[Dict[str, Any]]:
        """
        Prepare stream stats for batch update.
        
        This method extracts and formats stream stats from analyzed stream data
        for use in batch update operations.
        
        Parameters:
            stream_data (Dict): Analyzed stream data with resolution, fps, codecs, bitrate
            
        Returns:
            Optional[Dict[str, Any]]: Dict with 'stream_id' and 'stream_stats' keys,
                                     or None if no valid stats to update
        """
        stream_id = stream_data.get("stream_id")
        if not stream_id:
            logger.warning("No stream_id in stream data. Skipping stats preparation.")
            return None
        
        # Construct the stream stats payload from the analyzed stream data
        stream_stats_payload = {
            "resolution": stream_data.get("resolution"),
            "source_fps": stream_data.get("fps"),
            "video_codec": stream_data.get("video_codec"),
            "audio_codec": stream_data.get("audio_codec"),
            "ffmpeg_output_bitrate": int(stream_data.get("bitrate_kbps")) if stream_data.get("bitrate_kbps") not in ["N/A", None] else None,
            # Extended stream metadata
            "video_profile": stream_data.get("video_profile"),
            "video_level": stream_data.get("video_level"),
            "video_bit_depth": stream_data.get("video_bit_depth"),
            "video_ref_frames": stream_data.get("video_ref_frames"),
            "audio_language": stream_data.get("audio_language"),
            "sample_rate": stream_data.get("sample_rate"),
            "audio_channels": stream_data.get("audio_channels"),
        }

        # Clean up the payload, removing any None values or N/A values
        stream_stats_payload = {k: v for k, v in stream_stats_payload.items() if v not in [None, "N/A"]}

        if not stream_stats_payload:
            logger.debug(f"No data to update for stream {stream_id}. Skipping.")
            return None

        return {
            'stream_id': stream_id,
            'stream_stats': stream_stats_payload
        }
    
    def _start_batch_changelog(self):
        """Start a new batch for changelog entries."""
        with self.batch_lock:
            self.batch_start_time = datetime.now().isoformat()
            self.batch_changelog_entries = []
            logger.debug("Started new changelog batch")
    
    def _add_to_batch_changelog(self, channel_entry: Dict[str, Any]):
        """Add a channel check result to the current batch.
        
        Args:
            channel_entry: Dictionary containing channel check results
        """
        with self.batch_lock:
            if self.batch_start_time is not None:
                self.batch_changelog_entries.append(channel_entry)
                logger.debug(f"Added channel entry to batch (total: {len(self.batch_changelog_entries)})")
    
    def _finalize_batch_changelog(self):
        """Finalize the current batch and create a consolidated changelog entry."""
        with self.batch_lock:
            if self.batch_start_time is None or len(self.batch_changelog_entries) == 0:
                logger.debug("No batch to finalize")
                return
            
            if not self.changelog:
                logger.debug("Changelog not available, skipping batch finalization")
                self.batch_start_time = None
                self.batch_changelog_entries = []
                return
            
            try:
                # Calculate duration
                start_dt = datetime.fromisoformat(self.batch_start_time)
                end_dt = datetime.now()
                duration_seconds = int((end_dt - start_dt).total_seconds())
                
                # Format duration as human-readable string
                if duration_seconds < 60:
                    duration_str = f"{duration_seconds}s"
                elif duration_seconds < 3600:
                    minutes = duration_seconds // 60
                    seconds = duration_seconds % 60
                    duration_str = f"{minutes}m {seconds}s"
                else:
                    hours = duration_seconds // 3600
                    minutes = (duration_seconds % 3600) // 60
                    duration_str = f"{hours}h {minutes}m"
                
                # Calculate aggregate stats
                total_channels = len(self.batch_changelog_entries)
                total_streams = sum(entry.get('total_streams', 0) for entry in self.batch_changelog_entries)
                streams_analyzed = sum(entry.get('streams_analyzed', 0) for entry in self.batch_changelog_entries)
                dead_streams = sum(entry.get('dead_streams_detected', 0) for entry in self.batch_changelog_entries)
                streams_revived = sum(entry.get('streams_revived', 0) for entry in self.batch_changelog_entries)
                successful_checks = sum(1 for entry in self.batch_changelog_entries if entry.get('success', False))
                failed_checks = total_channels - successful_checks
                
                # Prepare subentries in the format expected by the UI
                subentries = [{
                    "group": "check",
                    "items": [
                        {
                            "channel_id": entry.get('channel_id'),
                            "channel_name": entry.get('channel_name'),
                            "logo_url": entry.get('logo_url'),
                            "stats": {
                                "total_streams": entry.get('total_streams', 0),
                                "streams_analyzed": entry.get('streams_analyzed', 0),
                                "dead_streams": entry.get('dead_streams_detected', 0),
                                "streams_revived": entry.get('streams_revived', 0),
                                "avg_resolution": entry.get('avg_resolution', 'N/A'),
                                "avg_bitrate": entry.get('avg_bitrate', 'N/A'),
                                "avg_fps": entry.get('avg_fps', 'N/A'),
                                "stream_details": entry.get('stream_stats', [])
                            }
                        }
                        for entry in self.batch_changelog_entries
                    ]
                }]
                
                # Create consolidated changelog entry
                self.changelog.add_entry(
                    action='batch_stream_check',
                    details={
                        'total_channels': total_channels,
                        'successful_checks': successful_checks,
                        'failed_checks': failed_checks,
                        'total_streams': total_streams,
                        'streams_analyzed': streams_analyzed,
                        'dead_streams': dead_streams,
                        'streams_revived': streams_revived,
                        'duration': duration_str,
                        'duration_seconds': duration_seconds
                    },
                    timestamp=self.batch_start_time,
                    subentries=subentries
                )
                
                logger.info(f"Finalized batch changelog: {total_channels} channels, {streams_analyzed} streams analyzed in {duration_str}")
                
                # After batch finalization, trigger channel re-enabling first to give channels a second chance
                self._trigger_channel_re_enabling()
                
                # Then trigger empty channel disabling if configured
                self._trigger_empty_channel_disabling()
                
            except Exception as e:
                logger.error(f"Failed to finalize batch changelog: {e}", exc_info=True)
            finally:
                # Reset batch tracking
                self.batch_start_time = None
                self.batch_changelog_entries = []
    
    def _trigger_empty_channel_disabling(self):
        """Trigger empty channel disabling if configured.
        
        This method checks if empty channel management is enabled in the profile
        configuration and triggers the disabling operation if so.
        """
        try:
            from empty_channel_manager import trigger_empty_channel_disabling
            
            result = trigger_empty_channel_disabling()
            if result:
                disabled_count, total_checked = result
                if disabled_count > 0:
                    logger.info(f"Empty channel management: Disabled {disabled_count} empty channels (checked {total_checked} channels)")
                else:
                    logger.debug(f"Empty channel management: No empty channels found (checked {total_checked} channels)")
        except Exception as e:
            logger.error(f"Error triggering empty channel disabling: {e}", exc_info=True)
    
    def _trigger_channel_re_enabling(self):
        """Trigger channel re-enabling if configured.
        
        This method checks if empty channel management with snapshot mode is enabled,
        and triggers the re-enabling operation to give previously disabled channels
        a second chance when their streams come back online.
        """
        try:
            from empty_channel_manager import trigger_channel_re_enabling
            
            result = trigger_channel_re_enabling()
            if result:
                enabled_count, total_checked = result
                if enabled_count > 0:
                    logger.info(f"Channel re-enabling: Re-enabled {enabled_count} channels with working streams (checked {total_checked} channels)")
                else:
                    logger.debug(f"Channel re-enabling: No disabled channels with working streams (checked {total_checked} channels)")
        except Exception as e:
            logger.error(f"Error triggering channel re-enabling: {e}", exc_info=True)
    
    def _check_channel_limits(self, channel_id: int, channel_name: str, streams: List[Dict]) -> Optional[Dict]:
        """Check if a channel can be checked based on viewer and playlist limits.
        
        This method now uses profile-aware checking. Instead of just checking account-level
        max_streams, it verifies that at least one stream has an available profile slot.
        
        Args:
            channel_id: ID of the channel
            channel_name: Name of the channel
            streams: List of streams for the channel
            
        Returns:
            None if check can proceed, or a result dict if check should be skipped
        """
        udi = get_udi_manager()
        
        # Check if channel has active viewers using real-time proxy status
        has_active_viewers = udi.is_channel_active(channel_id)
        if has_active_viewers:
            logger.warning(f"Channel {channel_name} has active viewers, skipping check to avoid disruption")
            return {
                'dead_streams_count': 0,
                'revived_streams_count': 0,
                'skipped': True,
                'skip_reason': 'active_viewers'
            }
        
        # Check if at least one stream can run (has an available profile)
        # This replaces the old account-level checking with profile-aware logic
        has_available_slot = False
        blocked_reasons = []
        
        for stream in streams:
            m3u_account = stream.get('m3u_account')
            if not m3u_account:
                # Custom stream without M3U account - can always check
                has_available_slot = True
                break
            
            # Check if this stream can run using profile-aware checking
            can_run, reason = udi.check_stream_can_run(stream)
            if can_run:
                has_available_slot = True
                break
            else:
                if reason and reason not in blocked_reasons:
                    blocked_reasons.append(reason)
        
        # If no stream has an available slot, skip the check
        if not has_available_slot:
            reason_str = "; ".join(blocked_reasons) if blocked_reasons else "All M3U account profiles are at capacity"
            logger.warning(f"Cannot check channel {channel_name}: {reason_str}")
            return {
                'dead_streams_count': 0,
                'revived_streams_count': 0,
                'skipped': True,
                'skip_reason': 'max_streams_reached',
                'reason_detail': reason_str
            }
        
        # At least one stream has an available slot, check can proceed
        return None
    
    def _get_resolution_product(self, stream_data: Dict) -> int:
        """Get resolution product (width * height) from stream data."""
        res = stream_data.get('resolution', '')
        if 'x' in str(res):
            try:
                width, height = map(int, str(res).split('x'))
                return width * height
            except: pass
        return 0

    def _refine_sorted_streams(self, streams: List[Dict]) -> List[Dict]:
        """Refine sorted streams to prioritize resolution when scores are close.
        
        This prevents lower resolution streams with high priority from displacing
        higher resolution streams, unless the quality difference is significant.
        """
        if not streams:
            return []
            
        # Tolerance for treating scores as "equal enough" to look at resolution
        # 0.15 allows a stream to be boosted by priority (up to ~7 levels) without jumping a resolution tier
        # unless the base quality difference is also large.
        SCORE_TOLERANCE = 0.15 
        
        refined = []
        remaining = streams.copy()
        
        while remaining:
            current = remaining.pop(0)
            tier = [current]
            
            i = 0
            while i < len(remaining):
                # Since list is sorted descending, diff is positive
                diff = current.get('score', 0) - remaining[i].get('score', 0)
                if diff <= SCORE_TOLERANCE:
                    tier.append(remaining.pop(i))
                else:
                    break
            
            # Sort tier by resolution product (descending)
            # This ensures higher resolution streams float to the top within the tolerance group
            tier.sort(key=self._get_resolution_product, reverse=True)
            refined.extend(tier)
            
        return refined
    
    def _check_channel(self, channel_id: int, skip_batch_changelog: bool = False):
        """Check and reorder streams for a specific channel.
        
        Routes to either concurrent or sequential checking based on configuration.
        
        Args:
            channel_id: ID of the channel to check
            skip_batch_changelog: If True, don't add this check to the batch changelog
        """
        concurrent_enabled = self.config.get('concurrent_streams.enabled', True)
        
        if concurrent_enabled:
            return self._check_channel_concurrent(channel_id, skip_batch_changelog=skip_batch_changelog)
        else:
            return self._check_channel_sequential(channel_id, skip_batch_changelog=skip_batch_changelog)
    
    def _check_channel_concurrent(self, channel_id: int, skip_batch_changelog: bool = False):
        """Check and reorder streams for a specific channel using parallel thread pool.
        
        Args:
            channel_id: ID of the channel to check
            skip_batch_changelog: If True, don't add this check to the batch changelog
        """
        import time as time_module
        from stream_check_utils import analyze_stream
        from concurrent_stream_limiter import get_smart_scheduler, get_account_limiter, initialize_account_limits
        
        start_time = time_module.time()
        log_function_call(logger, "_check_channel_concurrent", channel_id=channel_id)
        
        log_state_change(logger, f"channel_{channel_id}", "queued", "checking")
        self.checking = True
        logger.info(f"=" * 80)
        logger.info(f"Checking channel {channel_id} (parallel mode)")
        logger.info(f"=" * 80)
        
        # Get dead stream removal configuration early (used later in finally block)
        dead_stream_removal_enabled = self.config.get('dead_stream_handling', {}).get('enabled', True)
        
        try:
            # Get channel information from UDI
            logger.debug(f"Updating progress for channel {channel_id} initialization")
            self.progress.update(
                channel_id=channel_id,
                channel_name='Loading...',
                current=0,
                total=0,
                status='initializing',
                step='Fetching channel info',
                step_detail='Retrieving channel data from UDI'
            )
            
            udi = get_udi_manager()
            base_url = _get_base_url()
            logger.debug(f"Fetching channel data for channel {channel_id} from UDI")
            channel_data = udi.get_channel_by_id(channel_id)
            if not channel_data:
                logger.error(f"UDI returned None for channel {channel_id}")
                raise Exception(f"Could not fetch channel {channel_id}")
            
            channel_name = channel_data.get('name', f'Channel {channel_id}')
            
            # Get streams for this channel
            self.progress.update(
                channel_id=channel_id,
                channel_name=channel_name,
                current=0,
                total=0,
                status='initializing',
                step='Fetching streams',
                step_detail=f'Loading streams for {channel_name}'
            )
            
            streams = fetch_channel_streams(channel_id)
            if not streams or len(streams) == 0:
                logger.info(f"No streams found for channel {channel_name}")
                self.check_queue.mark_completed(channel_id)
                self.update_tracker.mark_channel_checked(channel_id)
                return {
                    'dead_streams_count': 0,
                    'revived_streams_count': 0
                }
            
            logger.info(f"Found {len(streams)} streams for channel {channel_name}")
            
            # Check if channel has active viewers or if its playlist has reached max concurrent streams
            limit_check_result = self._check_channel_limits(channel_id, channel_name, streams)
            if limit_check_result is not None:
                self.check_queue.mark_completed(channel_id)
                self.update_tracker.mark_channel_checked(channel_id)
                return limit_check_result
            
            # Check if this is a force check (bypasses 2-hour immunity)
            force_check = self.update_tracker.should_force_check(channel_id)
            
            # Get list of already checked streams to avoid re-analyzing
            checked_stream_ids = self.update_tracker.get_checked_stream_ids(channel_id)
            current_stream_ids = [s['id'] for s in streams]
            
            # Identify which streams need analysis (new or unchecked)
            if force_check:
                streams_to_check = streams
                streams_already_checked = []
                logger.info(f"Force check enabled: analyzing all {len(streams)} streams (bypassing 2-hour immunity)")
                self.update_tracker.clear_force_check(channel_id)
            else:
                streams_to_check = [s for s in streams if s['id'] not in checked_stream_ids]
                streams_already_checked = [s for s in streams if s['id'] in checked_stream_ids]
                
                if streams_to_check:
                    logger.info(f"Found {len(streams_to_check)} new/unchecked streams (out of {len(streams)} total)")
                else:
                    logger.info(f"All {len(streams)} streams have been recently checked, using cached scores")
                    
                    # Optimization: Skip check entirely if all conditions are met:
                    # 1. No new streams to analyze (all have been checked)
                    # 2. Stream count matches previous check (no additions/deletions)
                    # 3. Set of stream IDs is identical (no stream replacements)
                    previous_stream_count = len(checked_stream_ids)
                    current_stream_count = len(current_stream_ids)
                    
                    if (current_stream_count == previous_stream_count and 
                        set(current_stream_ids) == set(checked_stream_ids)):
                        logger.info(f"Channel {channel_name} unchanged since last check - skipping reorder")
                        self.check_queue.mark_completed(channel_id)
                        # Update timestamp but keep existing checked_stream_ids
                        self.update_tracker.mark_channel_checked(
                            channel_id,
                            stream_count=current_stream_count,
                            checked_stream_ids=checked_stream_ids
                        )
                        return
                    else:
                        logger.info(f"Channel composition changed (prev: {previous_stream_count}, curr: {current_stream_count}) - will reorder")
            
            # Get configuration for analysis
            analysis_params = self.config.get('stream_analysis', {})
            global_limit = self.config.get('concurrent_streams.global_limit', 10)
            stagger_delay = self.config.get('concurrent_streams.stagger_delay', 1.0)
            
            # Initialize account limits from UDI
            accounts = udi.get_m3u_accounts()
            if accounts:
                initialize_account_limits(accounts)
                logger.debug(f"Initialized concurrent stream limits for {len(accounts)} M3U accounts")
            
            # Initialize smart scheduler with account-aware limiting
            smart_scheduler = get_smart_scheduler(global_limit=global_limit)
            
            # Prepare for concurrent execution
            analyzed_streams = []
            dead_stream_ids = set()  # Use set for O(1) lookups
            revived_stream_ids = []
            total_streams = len(streams_to_check)
            completed_count = [0]  # Use list for mutable closure
            
            # Progress callback for parallel checker
            def progress_callback(completed, total, result):
                completed_count[0] = completed
                stream_name = result.get('stream_name', 'Unknown')
                
                # DO NOT update stream stats here - wait until all checks complete
                # This prevents race conditions with concurrent checks
                
                # Update progress
                self.progress.update(
                    channel_id=channel_id,
                    channel_name=channel_name,
                    current=completed,
                    total=total,
                    current_stream=stream_name,
                    status='analyzing',
                    step='Analyzing streams with account limits',
                    step_detail=f'Completed {completed}/{total}'
                )
            
            if streams_to_check:
                logger.info(f"Starting smart parallel analysis of {total_streams} streams with {global_limit} global workers")
                
                self.progress.update(
                    channel_id=channel_id,
                    channel_name=channel_name,
                    current=0,
                    total=total_streams,
                    status='analyzing',
                    step='Analyzing streams with account limits',
                    step_detail=f'Using smart scheduler with per-account limits'
                )
                
                # Check streams in parallel with account-aware limits
                results = smart_scheduler.check_streams_with_limits(
                    streams=streams_to_check,
                    check_function=analyze_stream,
                    progress_callback=progress_callback,
                    stagger_delay=stagger_delay,
                    ffmpeg_duration=analysis_params.get('ffmpeg_duration', 30),
                    timeout=analysis_params.get('timeout', 30),
                    retries=analysis_params.get('retries', 1),
                    retry_delay=analysis_params.get('retry_delay', 10),
                    user_agent=analysis_params.get('user_agent', 'VLC/3.0.14'),
                    stream_startup_buffer=analysis_params.get('stream_startup_buffer', 10)
                )
                
                # Process results - ALL checks are complete at this point
                # Collect stats for batch update to minimize API calls
                batch_stats_list = []
                batch_config = self.config.get('batch_operations', {})
                batch_enabled = batch_config.get('enabled', True)
                batch_size = batch_config.get('batch_size', 10)
                
                for analyzed in results:
                    # Prepare stats for batch update
                    if batch_enabled:
                        stats_item = self._prepare_stream_stats_for_batch(analyzed)
                        if stats_item:
                            batch_stats_list.append(stats_item)
                    else:
                        # Fall back to individual updates if batching is disabled
                        self._update_stream_stats(analyzed)
                    
                    # Check if stream is dead
                    is_dead = self._is_stream_dead(analyzed)
                    stream_id = analyzed.get('stream_id')
                    stream_url = analyzed.get('stream_url', '')
                    stream_name = analyzed.get('stream_name', 'Unknown')
                    was_dead = self.dead_streams_tracker.is_dead(stream_url)
                    
                    if is_dead and not was_dead:
                        if self.dead_streams_tracker.mark_as_dead(stream_url, stream_id, stream_name, channel_id):
                            dead_stream_ids.add(stream_id)
                            logger.warning(f"Stream {stream_id} detected as DEAD: {stream_name}")
                        else:
                            logger.error(f"Failed to mark stream {stream_id} as dead in tracker")
                    elif not is_dead and was_dead:
                        if self.dead_streams_tracker.mark_as_alive(stream_url):
                            revived_stream_ids.append(stream_id)
                            logger.info(f"Stream {stream_id} REVIVED: {stream_name}")
                    elif is_dead and was_dead:
                        logger.debug(f"Stream {stream_id} remains dead (already marked)")
                        # Add to dead_stream_ids so the stream removal logic (line 1455) will filter it out
                        dead_stream_ids.add(stream_id)
                    
                    # Calculate score
                    score = self._calculate_stream_score(analyzed)
                    analyzed['score'] = score
                    analyzed['channel_id'] = channel_id
                    analyzed['channel_name'] = channel_name
                    analyzed_streams.append(analyzed)
                
                # Perform batch stats update if enabled and we have stats to update
                if batch_enabled and batch_stats_list:
                    logger.info(f"Batch updating stats for {len(batch_stats_list)} streams (batch_size={batch_size})")
                    successful, failed = batch_update_stream_stats(batch_stats_list, batch_size=batch_size)
                    logger.info(f"Batch update complete: {successful} successful, {failed} failed")
                
                logger.info(f"Completed smart parallel analysis of {len(results)} streams with account-aware limits")
            
            # Process already-checked streams (use cached data)
            for stream in streams_already_checked:
                stream_data = udi.get_stream_by_id(stream['id'])
                if stream_data:
                    stream_stats = stream_data.get('stream_stats', {})
                    if stream_stats is None:
                        stream_stats = {}
                    if isinstance(stream_stats, str):
                        try:
                            stream_stats = json.loads(stream_stats)
                            if stream_stats is None:
                                stream_stats = {}
                        except json.JSONDecodeError:
                            stream_stats = {}
                    
                    analyzed = {
                        'channel_id': channel_id,
                        'channel_name': channel_name,
                        'stream_id': stream['id'],
                        'stream_name': stream.get('name', 'Unknown'),
                        'stream_url': stream.get('url', ''),
                        'resolution': stream_stats.get('resolution', '0x0'),
                        'fps': stream_stats.get('source_fps', 0),
                        'video_codec': stream_stats.get('video_codec', 'N/A'),
                        'audio_codec': stream_stats.get('audio_codec', 'N/A'),
                        'bitrate_kbps': stream_stats.get('ffmpeg_output_bitrate', 0),
                        'status': 'OK'
                    }
                    
                    # Check if cached stream is dead
                    stream_url = stream.get('url', '')
                    stream_name = stream.get('name', 'Unknown')
                    is_dead = self._is_stream_dead(analyzed)
                    was_dead = self.dead_streams_tracker.is_dead(stream_url)
                    
                    # Handle dead/alive state transitions (same logic as newly-checked streams)
                    if is_dead and not was_dead:
                        # Newly detected as dead
                        if self.dead_streams_tracker.mark_as_dead(stream_url, stream['id'], stream_name, channel_id):
                            dead_stream_ids.add(stream['id'])
                            logger.warning(f"Cached stream {stream['id']} detected as DEAD: {stream_name}")
                        else:
                            logger.error(f"Failed to mark cached stream {stream['id']} as dead in tracker")
                    elif not is_dead and was_dead:
                        # Stream was revived!
                        if self.dead_streams_tracker.mark_as_alive(stream_url):
                            revived_stream_ids.append(stream['id'])
                            logger.info(f"Cached stream {stream['id']} REVIVED: {stream_name}")
                        else:
                            logger.error(f"Failed to mark cached stream {stream['id']} as alive")
                    elif is_dead and was_dead:
                        # Stream remains dead (already marked)
                        logger.debug(f"Cached stream {stream['id']} remains dead (already marked)")
                        dead_stream_ids.add(stream['id'])
                    
                    score = self._calculate_stream_score(analyzed)
                    analyzed['score'] = score
                    analyzed_streams.append(analyzed)
            
            # Sort streams by score (highest first)
            self.progress.update(
                channel_id=channel_id,
                channel_name=channel_name,
                current=len(streams),
                total=len(streams),
                status='processing',
                step='Calculating scores',
                step_detail='Sorting streams by quality score'
            )
            analyzed_streams.sort(key=lambda x: x.get('score', 0), reverse=True)
            
            # Refine sort order to respect resolution for close scores
            # This handles priority boosts correctly without jumping resolution tiers
            analyzed_streams = self._refine_sorted_streams(analyzed_streams)
            
            # Remove dead streams from the channel (if enabled in config)
            # Dead streams are checked during all channel checks (normal and global)
            # If they're still dead, they're removed; if revived, they remain
            if dead_stream_ids:
                if dead_stream_removal_enabled:
                    logger.warning(f"🔴 Removing {len(dead_stream_ids)} dead streams from channel {channel_name}")
                    analyzed_streams = [s for s in analyzed_streams if s.get('stream_id') not in dead_stream_ids]
                else:
                    logger.info(f"⚠️ Found {len(dead_stream_ids)} dead streams in channel {channel_name}, but removal is disabled in config")
            
            if revived_stream_ids:
                logger.info(f"{len(revived_stream_ids)} streams were revived in channel {channel_name}")
            
            # Update channel with reordered streams
            self.progress.update(
                channel_id=channel_id,
                channel_name=channel_name,
                current=len(streams),
                total=len(streams),
                status='updating',
                step='Reordering streams',
                step_detail='Applying new stream order to channel'
            )
            reordered_ids = [s.get('stream_id') for s in analyzed_streams if s.get('stream_id') is not None]
            # Dead streams have already been filtered from analyzed_streams if removal is enabled
            # If removal is disabled, allow them to remain in the channel
            update_channel_streams(channel_id, reordered_ids, allow_dead_streams=(not dead_stream_removal_enabled))
            
            # Verify the update
            self.progress.update(
                channel_id=channel_id,
                channel_name=channel_name,
                current=len(streams),
                total=len(streams),
                status='verifying',
                step='Verifying update',
                step_detail='Confirming stream order was applied'
            )
            
            # Only verify if enabled in configuration
            batch_config = self.config.get('batch_operations', {})
            verify_updates = batch_config.get('verify_updates', False)
            
            if verify_updates:
                time_module.sleep(0.5)
                udi.refresh_channel_by_id(channel_id)
                logger.debug(f"Verified channel {channel_name} update via UDI refresh")
            else:
                logger.debug(f"Skipped verification for channel {channel_name} (disabled in config)")
            
            logger.info(f"✓ Channel {channel_name} checked and streams reordered (parallel mode)")
            
            # Add to batch changelog instead of creating individual entry
            if self.changelog:
                try:
                    # Get channel logo URL
                    logo_url = None
                    logo_id = channel_data.get('logo_id')
                    if logo_id:
                        logo = udi.get_logo_by_id(logo_id)
                        if logo:
                            logo_url = logo.get('cache_url') or logo.get('url')
                    
                    # Calculate channel-level averages from analyzed streams
                    averages = self._calculate_channel_averages(analyzed_streams, dead_stream_ids)
                    
                    stream_stats = []
                    for analyzed in analyzed_streams[:10]:  # Limit to first 10
                        stream_id = analyzed.get('stream_id')
                        is_dead = stream_id in dead_stream_ids
                        is_revived = stream_id in revived_stream_ids
                        
                        # Extract and format stats using centralized utilities
                        extracted_stats = extract_stream_stats(analyzed)
                        formatted_stats = format_stream_stats_for_display(extracted_stats)
                        
                        # Get M3U account name for this stream using helper method
                        m3u_account_name = self._get_m3u_account_name(stream_id, udi)
                        
                        stream_stat = {
                            'stream_id': stream_id,
                            'stream_name': analyzed.get('stream_name'),
                            'resolution': formatted_stats['resolution'],
                            'fps': formatted_stats['fps'],
                            'video_codec': formatted_stats['video_codec'],
                            'bitrate': formatted_stats['bitrate'],
                            'm3u_account': m3u_account_name
                        }
                        
                        # Mark dead streams as "dead" instead of showing score:0
                        if is_dead:
                            stream_stat['status'] = 'dead'
                        elif is_revived:
                            stream_stat['status'] = 'revived'
                            stream_stat['score'] = round(analyzed.get('score', 0), 2)
                        else:
                            stream_stat['score'] = round(analyzed.get('score', 0), 2)
                        
                        stream_stats.append({k: v for k, v in stream_stat.items() if v not in [None, "N/A"]})
                    
                    # Add to batch instead of creating individual changelog entry
                    # Only add to batch if not explicitly skipped (e.g., when called from check_single_channel)
                    if not skip_batch_changelog:
                        self._add_to_batch_changelog({
                            'channel_id': channel_id,
                            'channel_name': channel_name,
                            'logo_url': logo_url,
                            'total_streams': len(streams),
                            'streams_analyzed': len(analyzed_streams),
                            'dead_streams_detected': len(dead_stream_ids),
                            'streams_revived': len(revived_stream_ids),
                            'avg_resolution': averages['avg_resolution'],
                            'avg_bitrate': averages['avg_bitrate'],
                            'avg_fps': averages['avg_fps'],
                            'success': True,
                            'stream_stats': stream_stats
                        })
                except Exception as e:
                    logger.warning(f"Failed to add to batch changelog: {e}")
            
            # Mark as completed
            self.check_queue.mark_completed(channel_id)
            # Update current_stream_ids to exclude dead streams that were removed
            # This prevents dead stream IDs from being saved in checked_stream_ids
            # which would cause them to be skipped by 2-hour immunity even after revival
            # Note: Using list comprehension instead of set operations to preserve order
            # Only exclude dead streams if removal is enabled
            if dead_stream_removal_enabled:
                final_stream_ids = [sid for sid in current_stream_ids if sid not in dead_stream_ids]
            else:
                final_stream_ids = current_stream_ids  # Keep all streams if removal is disabled
            self.update_tracker.mark_channel_checked(
                channel_id, 
                stream_count=len(streams),
                checked_stream_ids=final_stream_ids
            )
            
            # Return statistics for callers that need them
            return {
                'dead_streams_count': len(dead_stream_ids),
                'revived_streams_count': len(revived_stream_ids)
            }
            
        except Exception as e:
            logger.error(f"Error checking channel {channel_id}: {e}", exc_info=True)
            self.check_queue.mark_failed(channel_id, str(e))
            
            # Only add to batch changelog if not explicitly skipped
            if self.changelog and not skip_batch_changelog:
                try:
                    try:
                        channel_name = channel_data.get('name', f'Channel {channel_id}')
                    except:
                        channel_name = f'Channel {channel_id}'
                    
                    # Add failed check to batch
                    self._add_to_batch_changelog({
                        'channel_id': channel_id,
                        'channel_name': channel_name,
                        'total_streams': 0,
                        'streams_analyzed': 0,
                        'dead_streams_detected': 0,
                        'streams_revived': 0,
                        'success': False,
                        'error': str(e),
                        'stream_stats': []
                    })
                except Exception as changelog_error:
                    logger.warning(f"Failed to add to batch changelog: {changelog_error}")
            
            # Return empty stats on error
            return {
                'dead_streams_count': 0,
                'revived_streams_count': 0
            }
        
        finally:
            self.checking = False
            self.progress.clear()
            log_function_return(logger, "_check_channel_concurrent")

    
    def _check_channel_sequential(self, channel_id: int, skip_batch_changelog: bool = False):
        """Check and reorder streams for a specific channel using sequential checking.
        
        Args:
            channel_id: ID of the channel to check
            skip_batch_changelog: If True, don't add this check to the batch changelog
        """
        import time as time_module
        start_time = time_module.time()
        log_function_call(logger, "_check_channel_sequential", channel_id=channel_id)
        
        log_state_change(logger, f"channel_{channel_id}", "queued", "checking")
        self.checking = True
        logger.info(f"=" * 80)
        logger.info(f"Checking channel {channel_id} (sequential mode)")
        logger.info(f"=" * 80)
        
        # Get dead stream removal configuration early (used later in finally block)
        dead_stream_removal_enabled = self.config.get('dead_stream_handling', {}).get('enabled', True)
        
        try:
            # Get channel information from UDI
            logger.debug(f"Updating progress for channel {channel_id} initialization")
            self.progress.update(
                channel_id=channel_id,
                channel_name='Loading...',
                current=0,
                total=0,
                status='initializing',
                step='Fetching channel info',
                step_detail='Retrieving channel data from UDI'
            )
            
            udi = get_udi_manager()
            base_url = _get_base_url()
            logger.debug(f"Fetching channel data for channel {channel_id} from UDI")
            channel_data = udi.get_channel_by_id(channel_id)
            if not channel_data:
                logger.error(f"UDI returned None for channel {channel_id}")
                raise Exception(f"Could not fetch channel {channel_id}")
            
            channel_name = channel_data.get('name', f'Channel {channel_id}')
            
            # Get streams for this channel
            self.progress.update(
                channel_id=channel_id,
                channel_name=channel_name,
                current=0,
                total=0,
                status='initializing',
                step='Fetching streams',
                step_detail=f'Loading streams for {channel_name}'
            )
            
            streams = fetch_channel_streams(channel_id)
            if not streams or len(streams) == 0:
                logger.info(f"No streams found for channel {channel_name}")
                self.check_queue.mark_completed(channel_id)
                self.update_tracker.mark_channel_checked(channel_id)
                return {
                    'dead_streams_count': 0,
                    'revived_streams_count': 0
                }
            
            logger.info(f"Found {len(streams)} streams for channel {channel_name}")
            
            # Check if channel has active viewers or if its playlist has reached max concurrent streams
            limit_check_result = self._check_channel_limits(channel_id, channel_name, streams)
            if limit_check_result is not None:
                self.check_queue.mark_completed(channel_id)
                self.update_tracker.mark_channel_checked(channel_id)
                return limit_check_result
            
            # Check if this is a force check (bypasses 2-hour immunity)
            force_check = self.update_tracker.should_force_check(channel_id)
            
            # Get list of already checked streams to avoid re-analyzing
            checked_stream_ids = self.update_tracker.get_checked_stream_ids(channel_id)
            current_stream_ids = [s['id'] for s in streams]
            
            # Identify which streams need analysis (new or unchecked)
            # If force_check is True, check ALL streams regardless of immunity
            if force_check:
                streams_to_check = streams
                streams_already_checked = []
                logger.info(f"Force check enabled: analyzing all {len(streams)} streams (bypassing 2-hour immunity)")
                # Clear the force check flag after acknowledging it
                self.update_tracker.clear_force_check(channel_id)
            else:
                streams_to_check = [s for s in streams if s['id'] not in checked_stream_ids]
                streams_already_checked = [s for s in streams if s['id'] in checked_stream_ids]
                
                if streams_to_check:
                    logger.info(f"Found {len(streams_to_check)} new/unchecked streams (out of {len(streams)} total)")
                else:
                    logger.info(f"All {len(streams)} streams have been recently checked, using cached scores")
                    
                    # Optimization: Skip check entirely if all conditions are met:
                    # 1. No new streams to analyze (all have been checked)
                    # 2. Stream count matches previous check (no additions/deletions)
                    # 3. Set of stream IDs is identical (no stream replacements)
                    previous_stream_count = len(checked_stream_ids)
                    current_stream_count = len(current_stream_ids)
                    
                    if (current_stream_count == previous_stream_count and 
                        set(current_stream_ids) == set(checked_stream_ids)):
                        logger.info(f"Channel {channel_name} unchanged since last check - skipping reorder")
                        self.check_queue.mark_completed(channel_id)
                        # Update timestamp but keep existing checked_stream_ids
                        self.update_tracker.mark_channel_checked(
                            channel_id,
                            stream_count=current_stream_count,
                            checked_stream_ids=checked_stream_ids
                        )
                        return
                    else:
                        logger.info(f"Channel composition changed (prev: {previous_stream_count}, curr: {current_stream_count}) - will reorder")
            
            # Import stream analysis functions from stream_check_utils
            from stream_check_utils import analyze_stream
            
            # Analyze new/unchecked streams
            analyzed_streams = []
            dead_stream_ids = set()  # Use set for O(1) lookups
            revived_stream_ids = []
            total_streams = len(streams_to_check)
            
            for idx, stream in enumerate(streams_to_check, 1):
                self.progress.update(
                    channel_id=channel_id,
                    channel_name=channel_name,
                    current=idx,
                    total=total_streams,
                    current_stream=stream.get('name', 'Unknown'),
                    status='analyzing',
                    step='Analyzing stream quality',
                    step_detail=f'Checking bitrate, resolution, codec ({idx}/{total_streams})'
                )
                
                # Analyze stream
                analysis_params = self.config.get('stream_analysis', {})
                
                # Apply URL transformation if using M3U profile with search/replace patterns
                stream_url = stream.get('url', '')
                if udi:
                    stream_url = udi.apply_profile_url_transformation(stream)
                
                analyzed = analyze_stream(
                    stream_url=stream_url,
                    stream_id=stream['id'],
                    stream_name=stream.get('name', 'Unknown'),
                    ffmpeg_duration=analysis_params.get('ffmpeg_duration', 20),
                    timeout=analysis_params.get('timeout', 30),
                    retries=analysis_params.get('retries', 1),
                    retry_delay=analysis_params.get('retry_delay', 10),
                    user_agent=analysis_params.get('user_agent', 'VLC/3.0.14'),
                    stream_startup_buffer=analysis_params.get('stream_startup_buffer', 10)
                )
                
                # Update stream stats on dispatcharr with ffmpeg-extracted data
                self._update_stream_stats(analyzed)
                
                # Check if stream is dead (resolution=0 or bitrate=0)
                is_dead = self._is_stream_dead(analyzed)
                stream_url = stream.get('url', '')
                stream_name = stream.get('name', 'Unknown')
                was_dead = self.dead_streams_tracker.is_dead(stream_url)
                
                if is_dead and not was_dead:
                    # Mark as dead in tracker
                    if self.dead_streams_tracker.mark_as_dead(stream_url, stream['id'], stream_name, channel_id):
                        dead_stream_ids.add(stream['id'])
                        logger.warning(f"Stream {stream['id']} detected as DEAD: {stream_name}")
                    else:
                        logger.error(f"Failed to mark stream {stream['id']} as DEAD, will not remove from channel")
                elif not is_dead and was_dead:
                    # Stream was revived!
                    if self.dead_streams_tracker.mark_as_alive(stream_url):
                        revived_stream_ids.append(stream['id'])
                        logger.info(f"Stream {stream['id']} REVIVED: {stream_name}")
                    else:
                        logger.error(f"Failed to mark stream {stream['id']} as alive")
                elif is_dead and was_dead:
                    # Stream remains dead
                    dead_stream_ids.add(stream['id'])
                
                # Calculate score
                score = self._calculate_stream_score(analyzed)
                analyzed['score'] = score
                analyzed_streams.append(analyzed)
                
                logger.info(f"Stream {idx}/{total_streams}: {stream.get('name')} - Score: {score:.2f}")
            
            # For already-checked streams, retrieve their cached data from UDI
            for stream in streams_already_checked:
                stream_data = udi.get_stream_by_id(stream['id'])
                if stream_data:
                    stream_stats = stream_data.get('stream_stats', {})
                    # Handle None case explicitly
                    if stream_stats is None:
                        stream_stats = {}
                    if isinstance(stream_stats, str):
                        try:
                            stream_stats = json.loads(stream_stats)
                            # Handle case where JSON string is "null"
                            if stream_stats is None:
                                stream_stats = {}
                        except json.JSONDecodeError:
                            stream_stats = {}
                    
                    # Reconstruct analyzed format from stored stats
                    # Use "0x0" for resolution, 0 for FPS and bitrate when not available
                    analyzed = {
                        'channel_id': channel_id,
                        'channel_name': channel_name,
                        'stream_id': stream['id'],
                        'stream_name': stream.get('name', 'Unknown'),
                        'stream_url': stream.get('url', ''),
                        'resolution': stream_stats.get('resolution', '0x0'),
                        'fps': stream_stats.get('source_fps', 0),
                        'video_codec': stream_stats.get('video_codec', 'N/A'),
                        'audio_codec': stream_stats.get('audio_codec', 'N/A'),
                        'bitrate_kbps': stream_stats.get('ffmpeg_output_bitrate', 0),
                        'status': 'OK'  # Assume OK for previously checked streams
                    }
                    
                    # Check if this cached stream is dead and handle state transitions
                    stream_url = stream.get('url', '')
                    stream_name = stream.get('name', 'Unknown')
                    is_dead = self._is_stream_dead(analyzed)
                    was_dead = self.dead_streams_tracker.is_dead(stream_url)
                    
                    # Handle dead/alive state transitions (same logic as newly-checked streams)
                    if is_dead and not was_dead:
                        # Newly detected as dead
                        if self.dead_streams_tracker.mark_as_dead(stream_url, stream['id'], stream_name, channel_id):
                            dead_stream_ids.add(stream['id'])
                            logger.warning(f"Cached stream {stream['id']} detected as DEAD: {stream_name}")
                        else:
                            logger.error(f"Failed to mark cached stream {stream['id']} as DEAD, will not remove from channel")
                    elif not is_dead and was_dead:
                        # Stream was revived!
                        if self.dead_streams_tracker.mark_as_alive(stream_url):
                            revived_stream_ids.append(stream['id'])
                            logger.info(f"Cached stream {stream['id']} REVIVED: {stream_name}")
                        else:
                            logger.error(f"Failed to mark cached stream {stream['id']} as alive")
                    elif is_dead and was_dead:
                        # Stream remains dead (already marked)
                        logger.debug(f"Cached stream {stream['id']} remains dead (already marked)")
                        dead_stream_ids.add(stream['id'])
                    
                    # Recalculate score from cached data
                    score = self._calculate_stream_score(analyzed)
                    analyzed['score'] = score
                    analyzed_streams.append(analyzed)
                    logger.debug(f"Using cached data for stream {stream['id']}: {stream.get('name')} - Score: {score:.2f}")
                else:
                    # If we can't fetch cached data, analyze this stream
                    logger.warning(f"Could not fetch cached data for stream {stream['id']}, will analyze")
                    analysis_params = self.config.get('stream_analysis', {})
                    
                    # Apply URL transformation if using M3U profile with search/replace patterns
                    stream_url = stream.get('url', '')
                    if udi:
                        stream_url = udi.apply_profile_url_transformation(stream)
                    
                    analyzed = analyze_stream(
                        stream_url=stream_url,
                        stream_id=stream['id'],
                        stream_name=stream.get('name', 'Unknown'),
                        ffmpeg_duration=analysis_params.get('ffmpeg_duration', 20),
                        timeout=analysis_params.get('timeout', 30),
                        retries=analysis_params.get('retries', 1),
                        retry_delay=analysis_params.get('retry_delay', 10),
                        user_agent=analysis_params.get('user_agent', 'VLC/3.0.14'),
                        stream_startup_buffer=analysis_params.get('stream_startup_buffer', 10)
                    )
                    self._update_stream_stats(analyzed)
                    score = self._calculate_stream_score(analyzed)
                    analyzed['score'] = score
                    analyzed_streams.append(analyzed)
            
            # Sort streams by score (highest first)
            self.progress.update(
                channel_id=channel_id,
                channel_name=channel_name,
                current=len(streams),
                total=len(streams),
                status='processing',
                step='Calculating scores',
                step_detail='Sorting streams by quality score'
            )
            analyzed_streams.sort(key=lambda x: x.get('score', 0), reverse=True)
            
            # Refine sort order to respect resolution for close scores
            # This handles priority boosts correctly without jumping resolution tiers
            analyzed_streams = self._refine_sorted_streams(analyzed_streams)
            
            # Remove dead streams from the channel (if enabled in config)
            # Dead streams are checked during all channel checks (normal and global)
            # If they're still dead, they're removed; if revived, they remain
            if dead_stream_ids:
                if dead_stream_removal_enabled:
                    logger.warning(f"🔴 Removing {len(dead_stream_ids)} dead streams from channel {channel_name}")
                    # Log which streams are being removed
                    for stream_id in dead_stream_ids:
                        dead_stream = next((s for s in analyzed_streams if s.get('stream_id') == stream_id), None)
                        if dead_stream:
                            logger.info(f"  - Removing dead stream {stream_id}: {dead_stream.get('stream_name', 'Unknown')}")
                    analyzed_streams = [s for s in analyzed_streams if s.get('stream_id') not in dead_stream_ids]
                else:
                    logger.info(f"⚠️ Found {len(dead_stream_ids)} dead streams in channel {channel_name}, but removal is disabled in config")
            
            if revived_stream_ids:
                logger.info(f"{len(revived_stream_ids)} streams were revived in channel {channel_name}")
            
            # Update channel with reordered streams
            self.progress.update(
                channel_id=channel_id,
                channel_name=channel_name,
                current=len(streams),
                total=len(streams),
                status='updating',
                step='Reordering streams',
                step_detail='Applying new stream order to channel'
            )
            reordered_ids = [s.get('stream_id') for s in analyzed_streams if s.get('stream_id') is not None]
            # Dead streams have already been filtered from analyzed_streams if removal is enabled
            # If removal is disabled, allow them to remain in the channel
            update_channel_streams(channel_id, reordered_ids, allow_dead_streams=(not dead_stream_removal_enabled))
            
            # Verify the update was applied correctly
            self.progress.update(
                channel_id=channel_id,
                channel_name=channel_name,
                current=len(streams),
                total=len(streams),
                status='verifying',
                step='Verifying update',
                step_detail='Confirming stream order was applied'
            )
            
            # Only verify if enabled in configuration
            batch_config = self.config.get('batch_operations', {})
            verify_updates = batch_config.get('verify_updates', False)
            
            if verify_updates:
                time.sleep(0.5)  # Brief delay to ensure API has processed the update
                # Refresh this specific channel in UDI to get updated data after write
                udi.refresh_channel_by_id(channel_id)
                updated_channel_data = udi.get_channel_by_id(channel_id)
                if updated_channel_data:
                    updated_stream_ids = updated_channel_data.get('streams', [])
                    if updated_stream_ids == reordered_ids:
                        logger.info(f"✓ Verified: Channel {channel_name} streams reordered correctly")
                    else:
                        logger.warning(f"⚠ Verification failed: Stream order mismatch for channel {channel_name}")
                        logger.warning(f"Expected: {reordered_ids[:5]}... Got: {updated_stream_ids[:5]}...")
                else:
                    logger.warning(f"⚠ Could not verify channel {channel_name}: channel data not found after refresh")
            else:
                logger.debug(f"Skipped verification for channel {channel_name} (disabled in config)")
            
            logger.info(f"✓ Channel {channel_name} checked and streams reordered")
            
            # Add changelog entry with stream stats
            if self.changelog:
                try:
                    # Calculate channel-level averages from analyzed streams
                    averages = self._calculate_channel_averages(analyzed_streams, dead_stream_ids)
                    
                    # Prepare stream stats summary for changelog
                    stream_stats = []
                    for analyzed in analyzed_streams[:10]:  # Limit to top 10
                        stream_id = analyzed.get('stream_id')
                        is_dead = stream_id in dead_stream_ids
                        is_revived = stream_id in revived_stream_ids
                        
                        # Extract and format stats using centralized utilities
                        extracted_stats = extract_stream_stats(analyzed)
                        formatted_stats = format_stream_stats_for_display(extracted_stats)
                        
                        # Get M3U account name for this stream using helper method
                        m3u_account_name = self._get_m3u_account_name(stream_id, udi)
                        
                        stream_stat = {
                            'stream_id': stream_id,
                            'stream_name': analyzed.get('stream_name'),
                            'resolution': formatted_stats['resolution'],
                            'fps': formatted_stats['fps'],
                            'video_codec': formatted_stats['video_codec'],
                            'audio_codec': formatted_stats['audio_codec'],
                            'bitrate': formatted_stats['bitrate'],
                            'm3u_account': m3u_account_name
                        }
                        
                        # Mark dead streams as "dead" instead of showing score:0
                        if is_dead:
                            stream_stat['status'] = 'dead'
                        elif is_revived:
                            stream_stat['status'] = 'revived'
                            stream_stat['score'] = round(analyzed.get('score', 0), 2)
                        else:
                            stream_stat['score'] = round(analyzed.get('score', 0), 2)
                            # Include original status from analysis if present
                            if 'status' in analyzed:
                                stream_stat['analysis_status'] = analyzed.get('status')
                        
                        # Clean up N/A values for cleaner output
                        stream_stat = {k: v for k, v in stream_stat.items() if v not in [None, "N/A"]}
                        stream_stats.append(stream_stat)
                    
                    # Get channel logo URL
                    logo_url = None
                    logo_id = channel_data.get('logo_id')
                    if logo_id:
                        logo = udi.get_logo_by_id(logo_id)
                        if logo:
                            logo_url = logo.get('cache_url') or logo.get('url')
                    
                    # Add to batch changelog instead of creating individual entry
                    # Only add to batch if not explicitly skipped (e.g., when called from check_single_channel)
                    if not skip_batch_changelog:
                        self._add_to_batch_changelog({
                            'channel_id': channel_id,
                            'channel_name': channel_name,
                            'logo_url': logo_url,
                            'total_streams': len(streams),
                            'streams_analyzed': len(analyzed_streams),
                            'dead_streams_detected': len(dead_stream_ids),
                            'streams_revived': len(revived_stream_ids),
                            'avg_resolution': averages['avg_resolution'],
                            'avg_bitrate': averages['avg_bitrate'],
                            'avg_fps': averages['avg_fps'],
                            'success': True,
                            'stream_stats': stream_stats[:10]  # Limit to top 10 for brevity
                        })
                        logger.info(f"Added channel {channel_name} to batch changelog")
                except Exception as e:
                    logger.warning(f"Failed to add to batch changelog: {e}")
            
            # Mark as completed with stream count and checked stream IDs
            self.check_queue.mark_completed(channel_id)
            # Update current_stream_ids to exclude dead streams that were removed
            # This prevents dead stream IDs from being saved in checked_stream_ids
            # which would cause them to be skipped by 2-hour immunity even after revival
            # Note: Using list comprehension instead of set operations to preserve order
            # Only exclude dead streams if removal is enabled
            if dead_stream_removal_enabled:
                final_stream_ids = [sid for sid in current_stream_ids if sid not in dead_stream_ids]
            else:
                final_stream_ids = current_stream_ids  # Keep all streams if removal is disabled
            self.update_tracker.mark_channel_checked(
                channel_id, 
                stream_count=len(streams),
                checked_stream_ids=final_stream_ids
            )
            
            # Return statistics for callers that need them
            return {
                'dead_streams_count': len(dead_stream_ids),
                'revived_streams_count': len(revived_stream_ids)
            }
            
        except Exception as e:
            logger.error(f"Error checking channel {channel_id}: {e}", exc_info=True)
            self.check_queue.mark_failed(channel_id, str(e))
            
            # Add failed check to batch changelog
            # Only add to batch if not explicitly skipped
            if self.changelog and not skip_batch_changelog:
                try:
                    # Try to get channel name if available
                    try:
                        channel_name = channel_data.get('name', f'Channel {channel_id}')
                    except:
                        channel_name = f'Channel {channel_id}'
                    
                    self._add_to_batch_changelog({
                        'channel_id': channel_id,
                        'channel_name': channel_name,
                        'total_streams': 0,
                        'streams_analyzed': 0,
                        'dead_streams_detected': 0,
                        'streams_revived': 0,
                        'success': False,
                        'error': str(e),
                        'stream_stats': []
                    })
                except Exception as changelog_error:
                    logger.warning(f"Failed to add to batch changelog: {changelog_error}")
            
            # Return empty stats on error
            return {
                'dead_streams_count': 0,
                'revived_streams_count': 0
            }
        
        finally:
            self.checking = False
            self.progress.clear()
    
    def _calculate_stream_score(self, stream_data: Dict) -> float:
        """Calculate a quality score for a stream based on analysis.
        
        Applies M3U account priority bonuses according to priority_mode:
        - "disabled": No priority bonus applied
        - "same_resolution": Priority bonus applied only to streams with same resolution
        - "all_streams": Priority bonus applied to all streams from higher priority accounts
        """
        # Dead streams always get a score of 0
        if self._is_stream_dead(stream_data):
            return 0.0
        
        weights = self.config.get('scoring.weights', {})
        score = 0.0
        
        # Bitrate score (0-1, normalized to typical range 1000-8000 kbps)
        bitrate = stream_data.get('bitrate_kbps', 0)
        if isinstance(bitrate, (int, float)) and bitrate > 0:
            bitrate_score = min(bitrate / 8000, 1.0)
            score += bitrate_score * weights.get('bitrate', 0.40)
        
        # Resolution score (0-1)
        resolution = stream_data.get('resolution', 'N/A')
        resolution_score = 0.0
        if 'x' in str(resolution):
            try:
                width, height = map(int, resolution.split('x'))
                # Score based on vertical resolution
                if height >= 1080:
                    resolution_score = 1.0
                elif height >= 720:
                    resolution_score = 0.7
                elif height >= 576:
                    resolution_score = 0.5
                else:
                    resolution_score = 0.3
            except (ValueError, AttributeError):
                pass
        score += resolution_score * weights.get('resolution', 0.35)
        
        # FPS score (0-1)
        fps = stream_data.get('fps', 0)
        if isinstance(fps, (int, float)) and fps > 0:
            fps_score = min(fps / 60, 1.0)
            score += fps_score * weights.get('fps', 0.15)
        
        # Codec score (0-1)
        codec = stream_data.get('video_codec', '').lower()
        codec_score = 0.0
        if codec:
            if 'h265' in codec or 'hevc' in codec:
                codec_score = 1.0 if self.config.get('scoring.prefer_h265', True) else 0.8
            elif 'h264' in codec or 'avc' in codec:
                codec_score = 0.8 if self.config.get('scoring.prefer_h265', True) else 1.0
            elif codec != 'n/a':
                codec_score = 0.5
        score += codec_score * weights.get('codec', 0.10)
        
        # Apply M3U account priority bonus if enabled
        stream_id = stream_data.get('stream_id')
        if stream_id:
            priority_boost = self._get_priority_boost(stream_id, stream_data)
            score += priority_boost
            
            # Apply regex pattern priority boost
            regex_priority_boost = self._get_regex_priority_boost(stream_id, stream_data)
            score += regex_priority_boost
        
        return round(score, 2)
    
    def _get_priority_boost(self, stream_id: int, stream_data: Dict) -> float:
        """Calculate priority boost for a stream based on its M3U account priority.
        
        Args:
            stream_id: The stream ID
            stream_data: Stream data dictionary containing resolution and other info
            
        Returns:
            Priority boost value (0.0 to 10.0+)
        """
        try:
            # Get stream from UDI to find its M3U account
            udi = get_udi_manager()
            stream = udi.get_stream_by_id(stream_id)
            if not stream:
                return 0.0
            
            m3u_account_id = stream.get('m3u_account')
            if not m3u_account_id:
                return 0.0
            
            # Get M3U account to check priority settings
            m3u_account = udi.get_m3u_account_by_id(m3u_account_id)
            if not m3u_account:
                return 0.0
            
            priority = m3u_account.get('priority', 0)
            priority_mode = m3u_account.get('priority_mode', 'disabled')
            
            # If priority is 0 or mode is disabled, no boost
            if priority == 0 or priority_mode == 'disabled':
                return 0.0
            
            # For "all_streams" mode, apply full priority boost to all streams
            if priority_mode == 'all_streams':
                # Priority boost: each priority point adds 0.5 to the score
                # This ensures higher priority accounts' streams rank higher
                boost = priority * 0.5
                logger.debug(f"Applying all_streams priority boost of {boost} to stream {stream_id} (priority: {priority})")
                return boost
            
            # For "same_resolution" mode, we need to group streams by resolution
            # and only apply priority within the same resolution group
            # This is more complex and requires comparing with other streams
            # For now, we apply a smaller boost that respects quality differences
            elif priority_mode == 'same_resolution':
                # Apply a smaller boost that won't override resolution differences
                # but will prioritize within same resolution
                boost = priority * 0.02
                logger.debug(f"Applying same_resolution priority boost of {boost} to stream {stream_id} (priority: {priority})")
                return boost
            
            return 0.0
        except Exception as e:
            logger.error(f"Error calculating priority boost for stream {stream_id}: {e}")
            return 0.0
    
    def _get_regex_priority_boost(self, stream_id: int, stream_data: Dict) -> float:
        """Calculate priority boost for a stream based on which regex pattern matched it.
        
        When multiple streams from the same playlist match a channel, those matched by
        higher-priority regex patterns should score higher.
        
        Args:
            stream_id: The stream ID
            stream_data: Stream data dictionary containing stream name and other info
            
        Returns:
            Regex priority boost value (0.0 to 5.0+)
        """
        try:
            # Get stream from UDI to find its name and M3U account
            udi = get_udi_manager()
            stream = udi.get_stream_by_id(stream_id)
            if not stream:
                return 0.0
            
            stream_name = stream.get('name', '')
            stream_m3u_account = stream.get('m3u_account')
            
            if not stream_name:
                return 0.0
            
            # Get the channel ID this stream belongs to
            channel_id = stream_data.get('channel_id')
            if not channel_id:
                return 0.0
            
            # Import regex matcher to check which pattern matched this stream
            from web_api import get_regex_matcher
            regex_matcher = get_regex_matcher()
            
            # Get match results with priority
            matches = regex_matcher.match_stream_to_channels_with_priority(stream_name, stream_m3u_account)
            
            # Find the match for this channel
            for match in matches:
                if str(match.get('channel_id')) == str(channel_id):
                    priority = match.get('priority', 0)
                    if priority > 0:
                        # Each priority point adds 0.1 to the score
                        # This is smaller than M3U priority boost (0.5) but still significant
                        boost = priority * 0.1
                        logger.debug(f"Applying regex priority boost of {boost} to stream {stream_id} in channel {channel_id} (priority: {priority})")
                        return boost
            
            return 0.0
        except Exception as e:
            logger.error(f"Error calculating regex priority boost for stream {stream_id}: {e}")
            return 0.0
    
    def get_status(self) -> Dict:
        """Get current service status."""
        queue_status = self.check_queue.get_status()
        progress = self.progress.get()
        
        # Stream checking mode is active when:
        # - A global action is in progress, OR
        # - An individual channel is being checked, OR
        # - There are channels in the queue waiting to be checked
        stream_checking_mode = (
            self.global_action_in_progress or 
            self.checking or 
            queue_status.get('queue_size', 0) > 0 or
            queue_status.get('in_progress', 0) > 0
        )
        
        return {
            'running': self.running,
            'checking': self.checking,
            'global_action_in_progress': self.global_action_in_progress,
            'stream_checking_mode': stream_checking_mode,
            'enabled': self.config.get('enabled', True),
            'queue': queue_status,
            'progress': progress,
            'last_global_check': self.update_tracker.get_last_global_check(),
            'config': {
                'automation_controls': self.config.get('automation_controls', {}),
                'check_interval': self.config.get('check_interval'),
                'global_check_schedule': self.config.get('global_check_schedule'),
                'queue_settings': self.config.get('queue')
            }
        }
    
    def queue_channel(self, channel_id: int, priority: int = 10, force_check: bool = False) -> bool:
        """Manually queue a channel for checking.
        
        Args:
            channel_id: ID of the channel to queue
            priority: Priority for queue ordering (higher = earlier)
            force_check: If True, marks channel for force checking (bypasses 2-hour immunity)
            
        Returns:
            True if channel was successfully queued, False otherwise
        """
        if force_check:
            self.update_tracker.mark_channel_for_force_check(channel_id)
            logger.info(f"Marked channel {channel_id} for force check (bypasses 2-hour immunity)")
        return self.check_queue.add_channel(channel_id, priority)
    
    def queue_channels(self, channel_ids: List[int], priority: int = 10, force_check: bool = False) -> int:
        """Manually queue multiple channels for checking.
        
        Args:
            channel_ids: List of channel IDs to queue
            priority: Priority for queue ordering (higher = earlier)
            force_check: If True, marks all channels for force checking (bypasses 2-hour immunity)
            
        Returns:
            Number of channels successfully queued
        """
        if force_check:
            for channel_id in channel_ids:
                self.update_tracker.mark_channel_for_force_check(channel_id)
            logger.info(f"Marked {len(channel_ids)} channels for force check (bypasses 2-hour immunity)")
        return self.check_queue.add_channels(channel_ids, priority)
    
    def check_single_channel(self, channel_id: int, program_name: Optional[str] = None) -> Dict:
        """Check a single channel immediately and return results.
        
        This performs a targeted channel refresh for a single channel:
        - Identifies M3U accounts used by the channel
        - Refreshes playlists for accounts associated with the channel
        - Clears dead streams for the specified channel to give them a second chance (like global action)
        - Re-matches and assigns streams (including previously dead ones) if matching_mode is enabled
        - Force checks all streams (bypasses 2-hour immunity) if checking_mode is enabled
        - Detects newly dead streams and marks them (if checking is enabled)
        - Detects revived streams and marks them as alive (if checking is enabled)
        - Removes dead streams from the channel (if checking is enabled)
        
        Note: This now works like Global Action but only for the specified channel.
        Dead streams for other channels are not affected.
        
        Channel settings (matching_mode and checking_mode) are respected:
        - If matching_mode is disabled, stream matching is skipped
        - If checking_mode is disabled, stream quality checking is skipped
        
        Args:
            channel_id: ID of the channel to check
            program_name: Optional program name if this is a scheduled EPG check
            
        Returns:
            Dict with check results and statistics
        """
        import time as time_module
        start_time = time_module.time()
        
        try:
            logger.info(f"Starting single channel check for channel {channel_id}")
            
            # Get channel info from UDI
            udi = get_udi_manager()
            channel = udi.get_channel_by_id(channel_id)
            if not channel:
                error_msg = f"Channel {channel_id} not found"
                logger.error(error_msg)
                return {'success': False, 'error': error_msg}
            
            channel_name = channel.get('name', f'Channel {channel_id}')
            
            # Check channel settings for matching and checking modes
            channel_settings = get_channel_settings_manager()
            settings = channel_settings.get_channel_settings(channel_id)
            matching_enabled = settings['matching_mode'] == 'enabled'
            checking_enabled = settings['checking_mode'] == 'enabled'
            
            logger.info(f"Channel {channel_name} settings: matching={matching_enabled}, checking={checking_enabled}")
            
            # Check if channel has active viewers or if its playlist has reached max concurrent streams
            current_streams = fetch_channel_streams(channel_id)
            if current_streams:
                limit_check_result = self._check_channel_limits(channel_id, channel_name, current_streams)
                if limit_check_result is not None:
                    # Convert the internal result format to the single channel check format
                    return {
                        'success': False,
                        'error': f"Channel check skipped: {limit_check_result.get('skip_reason', 'limits reached')}",
                        'reason': limit_check_result.get('skip_reason'),
                        'channel_id': channel_id,
                        'channel_name': channel_name,
                        'details': limit_check_result
                    }
            
            # Step 1: Identify M3U accounts for channel (reusing current_streams from limit check above)
            logger.info(f"Step 1/6: Identifying M3U accounts for channel {channel_name}...")
            account_ids = set()
            if current_streams:
                for stream in current_streams:
                    m3u_account = stream.get('m3u_account')
                    if m3u_account:
                        account_ids.add(m3u_account)
            
            # Also check dead streams for this channel to find M3U accounts
            # This fixes the bug where channels with all dead streams couldn't refresh their playlists
            dead_streams = self.dead_streams_tracker.get_dead_streams_for_channel(channel_id)
            for dead_url, dead_info in dead_streams.items():
                # Try to get the stream from UDI to find its m3u_account
                stream_id = dead_info.get('stream_id')
                if stream_id:
                    stream = udi.get_stream_by_id(stream_id)
                    if stream:
                        m3u_account = stream.get('m3u_account')
                        if m3u_account:
                            account_ids.add(m3u_account)
                            logger.info(f"Found M3U account {m3u_account} from dead stream {dead_info.get('stream_name', 'Unknown')}")
            
            # Step 2: Refresh playlists for those accounts
            if account_ids:
                logger.info(f"Step 2/6: Refreshing playlists for {len(account_ids)} M3U account(s)...")
                # Import here to allow better test mocking
                from api_utils import refresh_m3u_playlists
                for account_id in account_ids:
                    logger.info(f"Refreshing M3U account {account_id}")
                    refresh_m3u_playlists(account_id=account_id)
                
                # Refresh UDI cache to get updated streams
                # Also refresh M3U accounts to detect any new accounts
                # And refresh channel groups to detect any group changes
                udi.refresh_m3u_accounts()  # Check for new M3U accounts
                udi.refresh_streams()
                udi.refresh_channels()
                udi.refresh_channel_groups()  # Check for new/updated channel groups
                logger.info("✓ Playlists refreshed and UDI cache updated")
            else:
                logger.info("Step 2/6: No M3U accounts found for this channel, skipping playlist refresh")
            
            # Step 3: Clear dead streams for this channel to give them a second chance
            logger.info(f"Step 3/6: Clearing dead streams for channel {channel_name} to give them a second chance...")
            try:
                # First, check how many dead streams exist for this channel
                dead_streams_for_channel = self.dead_streams_tracker.get_dead_streams_for_channel(channel_id)
                initial_dead_count = len(dead_streams_for_channel)
                
                if initial_dead_count > 0:
                    logger.info(f"Found {initial_dead_count} dead stream(s) for channel {channel_id} before clearing")
                
                # Clear all dead streams that belong to this channel by channel_id
                # This handles cases where playlist refresh creates new streams with different URLs
                cleared_count = self.dead_streams_tracker.remove_dead_streams_by_channel_id(channel_id)
                
                if cleared_count > 0:
                    logger.info(f"✓ Cleared {cleared_count} dead stream(s) from tracker - they will be given a second chance")
                    
                    # Verify the streams were actually cleared
                    remaining_dead = self.dead_streams_tracker.get_dead_streams_for_channel(channel_id)
                    if len(remaining_dead) > 0:
                        logger.warning(f"⚠ {len(remaining_dead)} dead stream(s) still remain after clearing - this may indicate an issue")
                    else:
                        logger.info("✓ Verified: All dead streams successfully removed from tracker")
                else:
                    logger.info("✓ No dead streams to clear for this channel")
            except Exception as e:
                logger.error(f"✗ Failed to clear dead streams: {e}", exc_info=True)
            
            # Step 4: Validate existing streams against regex patterns (if matching is enabled)
            if matching_enabled:
                logger.info(f"Step 4/6: Validating existing streams for channel {channel_name}...")
                try:
                    from automated_stream_manager import AutomatedStreamManager
                    automation_manager = AutomatedStreamManager()
                    
                    # Run validation - respects automation_controls.remove_non_matching_streams setting
                    validation_results = automation_manager.validate_and_remove_non_matching_streams()
                    if validation_results.get("streams_removed", 0) > 0:
                        logger.info(f"✓ Removed {validation_results['streams_removed']} non-matching streams")
                    else:
                        logger.info("✓ No non-matching streams found to remove")
                except Exception as e:
                    logger.error(f"✗ Failed to validate streams: {e}")
            else:
                logger.info(f"Step 4/6: Skipping stream validation (matching is disabled for this channel)")
            
            # Step 5: Re-match and assign streams for this specific channel (if matching is enabled)
            # With dead streams cleared, previously dead streams can now be re-added
            if matching_enabled:
                logger.info(f"Step 5/6: Re-matching streams for channel {channel_name}...")
                try:
                    # Import here to allow better test mocking
                    from automated_stream_manager import AutomatedStreamManager
                    automation_manager = AutomatedStreamManager()
                    
                    # Run full discovery (this will add new matching streams but skip dead ones)
                    # Skip automatic check trigger since we'll perform the check explicitly in Step 6
                    assignments = automation_manager.discover_and_assign_streams(force=True, skip_check_trigger=True)
                    if assignments:
                        logger.info(f"✓ Stream matching completed")
                    else:
                        logger.info("✓ No new stream assignments")
                except Exception as e:
                    logger.error(f"✗ Failed to match streams: {e}")
            else:
                logger.info(f"Step 5/6: Skipping stream matching (matching is disabled for this channel)")
            
            # Refresh UDI cache again to ensure the check in Step 6 sees the newly assigned streams
            # This is critical because discover_and_assign_streams updates the DB but UDI cache might be stale
            if matching_enabled:
                logger.debug("Refreshing UDI cache for streams and channel to reflect new assignments...")
                udi.refresh_streams()
                udi.refresh_channel_by_id(channel_id)
                logger.info(f"✓ UDI cache refreshed with latest stream assignments")
            
            # Step 6: Mark channel for force check and perform the check (if checking is enabled)
            dead_count = 0
            if checking_enabled:
                logger.info(f"Step 6/6: Force checking all streams for channel {channel_name}...")
                self.update_tracker.mark_channel_for_force_check(channel_id)
                
                # Perform the check (this will now bypass immunity and check all streams)
                # Returns dict with dead_streams_count and revived_streams_count
                # Skip batch changelog since this is a single channel check
                check_result = self._check_channel(channel_id, skip_batch_changelog=True)
                if not check_result or not isinstance(check_result, dict):
                    # This should not happen with updated methods, but provide safe fallback
                    logger.warning(f"_check_channel did not return expected result dict, using defaults")
                    check_result = {'dead_streams_count': 0, 'revived_streams_count': 0}
                
                # Get the count of dead streams that were removed during the check
                dead_count = check_result.get('dead_streams_count', 0)
            else:
                logger.info(f"Step 6/6: Skipping stream checking (checking is disabled for this channel)")
            
            # Gather statistics after check using centralized utility
            streams = fetch_channel_streams(channel_id)
            total_streams = len(streams)
            
            # Calculate channel averages using centralized function
            channel_averages = calculate_channel_averages(streams, dead_stream_ids=set())
            
            check_stats = {
                'total_streams': total_streams,
                'dead_streams': dead_count,
                'avg_resolution': channel_averages['avg_resolution'],
                'avg_bitrate': channel_averages['avg_bitrate'],
                'avg_fps': channel_averages['avg_fps'],
                'stream_details': []
            }
            
            # Add top stream details using centralized extraction
            for stream in streams[:10]:  # Top 10 streams
                # Extract stats using centralized utility
                extracted_stats = extract_stream_stats(stream)
                formatted_stats = format_stream_stats_for_display(extracted_stats)
                
                # Calculate score for this stream using its stats
                # The score needs to be calculated from the stream_stats data stored in Dispatcharr
                stream_stats = stream.get('stream_stats', {})
                if stream_stats is None:
                    stream_stats = {}
                if isinstance(stream_stats, str):
                    try:
                        stream_stats = json.loads(stream_stats)
                        if stream_stats is None:
                            stream_stats = {}
                    except json.JSONDecodeError:
                        stream_stats = {}
                
                # Build stream data dict for score calculation
                score_data = {
                    'stream_id': stream.get('id'),
                    'stream_name': stream.get('name', 'Unknown'),
                    'stream_url': stream.get('url', ''),
                    'resolution': stream_stats.get('resolution', '0x0'),
                    'fps': stream_stats.get('source_fps', 0),
                    'video_codec': stream_stats.get('video_codec', 'N/A'),
                    'bitrate_kbps': stream_stats.get('ffmpeg_output_bitrate', 0)
                }
                
                # Calculate score
                score = self._calculate_stream_score(score_data)
                
                # Get M3U account name for this stream using helper method
                m3u_account_name = None
                m3u_account_id = stream.get('m3u_account')
                if m3u_account_id:
                    m3u_account_name = self._get_m3u_account_name(stream.get('id'), udi)
                
                check_stats['stream_details'].append({
                    'stream_id': stream.get('id'),
                    'stream_name': stream.get('name', 'Unknown'),
                    'resolution': formatted_stats['resolution'],
                    'bitrate': formatted_stats['bitrate'],
                    'video_codec': formatted_stats['video_codec'],
                    'fps': formatted_stats['fps'],
                    'score': score,
                    'm3u_account': m3u_account_name
                })
            
            # Calculate duration
            end_time = time_module.time()
            duration_seconds = int(end_time - start_time)
            
            # Format duration as human-readable string
            if duration_seconds < 60:
                duration_str = f"{duration_seconds}s"
            elif duration_seconds < 3600:
                minutes = duration_seconds // 60
                seconds = duration_seconds % 60
                duration_str = f"{minutes}m {seconds}s"
            else:
                hours = duration_seconds // 3600
                minutes = (duration_seconds % 3600) // 60
                duration_str = f"{hours}h {minutes}m"
            
            # Add duration to check stats
            check_stats['duration'] = duration_str
            check_stats['duration_seconds'] = duration_seconds
            
            # Add changelog entry
            if self.changelog:
                try:
                    # Get logo URL for the channel
                    logo_url = None
                    logo_id = channel.get('logo_id')
                    if logo_id:
                        try:
                            logo = udi.get_logo_by_id(logo_id)
                            if logo and logo.get('cache_url'):
                                logo_url = logo.get('cache_url')
                        except Exception as e:
                            logger.debug(f"Could not fetch logo for channel {channel_id}: {e}")
                    
                    self.changelog.add_single_channel_check_entry(
                        channel_id=channel_id,
                        channel_name=channel_name,
                        check_stats=check_stats,
                        logo_url=logo_url,
                        program_name=program_name
                    )
                except Exception as e:
                    logger.warning(f"Failed to add changelog entry: {e}")
            
            logger.info(f"✓ Single channel check completed for {channel_name} in {duration_str}")
            
            # Trigger channel re-enabling first to give channels a second chance
            self._trigger_channel_re_enabling()
            
            # Trigger empty channel disabling if configured
            # This ensures that if this channel became empty after checking, it gets disabled
            self._trigger_empty_channel_disabling()
            
            return {
                'success': True,
                'channel_id': channel_id,
                'channel_name': channel_name,
                'stats': check_stats
            }
            
        except Exception as e:
            logger.error(f"Error checking single channel {channel_id}: {e}", exc_info=True)
            return {'success': False, 'error': str(e)}
    
    def clear_queue(self):
        """Clear the checking queue."""
        self.check_queue.clear()
        logger.info("Checking queue cleared")
    
    def trigger_check_updated_channels(self):
        """Trigger immediate check of channels with M3U updates.
        
        This method signals the scheduler to immediately process any channels
        that have been marked as updated, instead of waiting for the next
        scheduled check interval.
        """
        if self.running:
            logger.info("Triggering immediate check for updated channels")
            self.check_trigger.set()
        else:
            logger.warning("Cannot trigger check - service is not running")
    
    def update_config(self, updates: Dict):
        """Update service configuration and apply changes immediately."""
        # Sanitize user_agent if present
        if 'stream_analysis' in updates and 'user_agent' in updates['stream_analysis']:
            user_agent = updates['stream_analysis']['user_agent']
            # Sanitize user agent: allow alphanumeric, spaces, dots, slashes, dashes, underscores, parentheses
            import re
            sanitized = re.sub(r'[^a-zA-Z0-9 ./_\-()]+', '', str(user_agent))
            # Limit length to 200 characters
            sanitized = sanitized[:200].strip()
            if not sanitized:
                sanitized = 'VLC/3.0.14'  # Default fallback
            updates['stream_analysis']['user_agent'] = sanitized
            if sanitized != user_agent:
                logger.warning(f"User agent sanitized from '{user_agent}' to '{sanitized}'")
        
        # Log what's being updated
        config_changes = []
        if 'automation_controls' in updates:
            old_controls = self.config.get('automation_controls', {})
            new_controls = updates['automation_controls']
            for key, value in new_controls.items():
                old_value = old_controls.get(key, False)
                if old_value != value:
                    config_changes.append(f"Automation control '{key}': {old_value} → {value}")
        
        if 'global_check_schedule' in updates:
            schedule_changes = []
            schedule = updates['global_check_schedule']
            if 'hour' in schedule or 'minute' in schedule:
                old_hour = self.config.get('global_check_schedule.hour', 3)
                old_minute = self.config.get('global_check_schedule.minute', 0)
                new_hour = schedule.get('hour', old_hour)
                new_minute = schedule.get('minute', old_minute)
                if old_hour != new_hour or old_minute != new_minute:
                    schedule_changes.append(f"Time: {old_hour:02d}:{old_minute:02d} → {new_hour:02d}:{new_minute:02d}")
            if 'frequency' in schedule:
                old_freq = self.config.get('global_check_schedule.frequency', 'daily')
                new_freq = schedule['frequency']
                if old_freq != new_freq:
                    schedule_changes.append(f"Frequency: {old_freq} → {new_freq}")
            if 'enabled' in schedule:
                old_enabled = self.config.get('global_check_schedule.enabled', True)
                new_enabled = schedule['enabled']
                if old_enabled != new_enabled:
                    schedule_changes.append(f"Enabled: {old_enabled} → {new_enabled}")
            if schedule_changes:
                config_changes.append(f"Global check schedule: {', '.join(schedule_changes)}")
        
        # Apply the configuration update
        self.config.update(updates)
        
        # Log the changes
        if config_changes:
            logger.info(f"Configuration updated: {'; '.join(config_changes)}")
        else:
            logger.info("Configuration updated")
        
        # Signal that config has changed for immediate application
        if self.running:
            self.config_changed.set()
            # Wake up the scheduler immediately by setting the trigger
            # The scheduler will check config_changed and skip channel queueing
            self.check_trigger.set()
            logger.info("Configuration changes will be applied immediately")
        
        # Reload queue max size if changed
        if 'queue' in updates and 'max_size' in updates['queue']:
            # Can't resize existing queue, but will apply on next restart
            logger.info("Queue max size updated, will apply on next restart")
    
    def trigger_global_action(self):
        """Manually trigger a global action (Update, Match, Check all channels).
        
        This can be called at any time to perform a complete global action,
        regardless of the scheduled time.
        """
        if not self.running:
            logger.warning("Cannot trigger global action - service is not running")
            return False
        
        logger.info("Manual global action triggered")
        try:
            self._perform_global_action()
            self.update_tracker.mark_global_check()
            return True
        except Exception as e:
            logger.error(f"Failed to trigger global action: {e}")
            return False


# Global service instance
_service_instance = None
_service_lock = threading.Lock()

def get_stream_checker_service() -> StreamCheckerService:
    """Get or create the global stream checker service instance."""
    global _service_instance
    with _service_lock:
        if _service_instance is None:
            _service_instance = StreamCheckerService()
        return _service_instance
