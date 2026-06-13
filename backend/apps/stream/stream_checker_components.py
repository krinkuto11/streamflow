"""Support components used by the stream checker service."""

import copy
import json
import queue
import threading
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from apps.udi import get_udi_manager
from apps.core.logging_config import setup_logging, log_function_call

logger = setup_logging(__name__)


class StreamCheckConfig:
    """Configuration for stream checking service."""

    LEGACY_RECOVERY_WAIT_SECONDS = 120
    DEFAULT_RECOVERY_WAIT_SECONDS = 240
    
    DEFAULT_CONFIG = {
        'enabled': True,
        'check_interval': 300,  # DEPRECATED - checks now only triggered by M3U refresh
        # Individual automation controls
        'automation_controls': {
            'auto_m3u_updates': True,  # Automatically refresh M3U playlists
            'auto_stream_matching': True,  # Automatically match streams to channels via regex
            'auto_quality_checking': True,  # Automatically check stream quality
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
            'ffmpeg_duration': 30,      # seconds to analyze each stream
            'timeout': 30,              # timeout for operations
            'stream_startup_buffer': 10, # seconds buffer for stream startup (max time before stream starts)
            'retries': 1,               # retry attempts
            'retry_delay': 10,          # seconds between retries
            'max_loop_duration': 120,   # maximum loop period to detect (seconds); probe runs for 3× this value
            'blank_check_min_duration': 2.0,  # seconds of continuous black before blackdetect logs a segment
            'blank_check_pixel_threshold': 0.10,  # blackdetect pix_th threshold
            'blank_check_ratio_threshold': 0.80,  # probe-window ratio that marks a stream blank
            'freeze_check_min_duration': 5.0,  # seconds of frozen video before freezedetect logs a segment
            'freeze_check_noise_threshold': 0.001,  # freezedetect noise threshold
            'freeze_check_ratio_threshold': 0.80,  # probe-window ratio that marks a stream frozen
            'hardware_acceleration': {
                'enabled': False,
                'mode': 'auto',
                'device': '',
                'allow_fallback': True
            },
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
            'max_channels_per_run': 50,  # limit channels per check cycle
            'start_mode': 'first',  # first, last, or channel for manual full checks
            'start_channel_id': None
        },
        'concurrent_streams': {
            'global_limit': 10,  # Maximum concurrent stream checks globally (0 = unlimited)
            'enabled': True,  # Enable concurrent checking via Celery
            'stagger_delay': 1.0,  # Delay in seconds between dispatching tasks to prevent simultaneous starts
            # Max wait for externally unavailable capacity (for example active viewers).
            # Checker-owned provider slots wait until their current probes finish.
            'provider_wait_timeout': 180
        },
        'dead_stream_handling': {
            'enabled': True,  # Enable dead stream removal
            'min_resolution_width': 0,  # Minimum width in pixels (0 = no minimum, e.g., 1280 for 720p)
            'min_resolution_height': 0,  # Minimum height in pixels (0 = no minimum, e.g., 720 for 720p)
            'min_bitrate_kbps': 0,  # Minimum bitrate in kbps (0 = no minimum)
            'min_score': 0  # Minimum score (0-100, 0 = no minimum)
        },
        'channel_visibility_automation': {
            'enabled': False,
            'hide_on_no_regex': False,
            'hide_on_no_streams': False,
            'hide_on_all_failed': False,
            'unhide_on_recovered': True
        },
        'batch_operations': {
            'enabled': True,  # Enable batch stats updates to reduce API calls
            'batch_size': 10,  # Number of streams to update per batch
            'verify_updates': False  # Verify channel updates by refreshing UDI (adds API overhead)
        },
        'connectivity_guard': {
            'enabled': True,
            'require_internet': True,
            'require_dispatcharr_api': True,
            'timeout_seconds': 3.0,
            'retry_attempts': 2,
            'retry_backoff_seconds': 1.0,
            'recovery_wait_seconds': DEFAULT_RECOVERY_WAIT_SECONDS,
            'recovery_poll_seconds': 10,
            'stale_recheck_interval_seconds': 60,
            'internet_probe_urls': [
                'https://www.google.com/generate_204',
                'https://cloudflare.com/cdn-cgi/trace',
            ],
        }
    }
    
    def __init__(self, config_file: Optional[str] = None) -> None:
        """
        Initialize the StreamCheckConfig.
        """
        from apps.database.manager import get_db_manager
        self.db = get_db_manager()
        self.config_file = Path(config_file) if config_file is not None else None
        self.config = self._load_config()
    
    def _load_config(self) -> Dict[str, Any]:
        """
        Load configuration from database or create default.
        
        Merges loaded config with DEFAULT_CONFIG to ensure all
        required keys exist even if config in DB is incomplete.
        
        Returns:
            Dict[str, Any]: The configuration dictionary.
        """
        import copy
        log_function_call(logger, "_load_config")
        
        def deep_merge(defaults: Dict[str, Any], overrides: Dict[str, Any]) -> Dict[str, Any]:
            for key, value in overrides.items():
                if (
                    isinstance(value, dict)
                    and isinstance(defaults.get(key), dict)
                ):
                    defaults[key] = deep_merge(defaults[key], value)
                else:
                    defaults[key] = value
            return defaults

        def migrate_loaded_config(
            config: Dict[str, Any],
            loaded_config: Dict[str, Any],
        ) -> bool:
            loaded_guard = loaded_config.get('connectivity_guard')
            if not isinstance(loaded_guard, dict):
                return False

            loaded_wait = loaded_guard.get('recovery_wait_seconds')
            try:
                loaded_wait_seconds = float(loaded_wait)
            except (TypeError, ValueError):
                return False

            if loaded_wait_seconds != self.LEGACY_RECOVERY_WAIT_SECONDS:
                return False

            config.setdefault('connectivity_guard', {})['recovery_wait_seconds'] = (
                self.DEFAULT_RECOVERY_WAIT_SECONDS
            )
            return True

        if self.config_file is not None and self.config_file.exists():
            try:
                with open(self.config_file, 'r', encoding='utf-8') as fh:
                    loaded_file = json.load(fh) or {}
                config = copy.deepcopy(self.DEFAULT_CONFIG)
                config = deep_merge(config, loaded_file)
                if migrate_loaded_config(config, loaded_file):
                    self._save_config(config)
                return config
            except Exception as exc:
                logger.warning(f"Could not load stream checker config file {self.config_file}: {exc}")

        loaded = self.db.get_system_setting('stream_checker_config', {})
        if loaded:
            logger.debug(f"Loaded config from DB with {len(loaded)} top-level keys")
            # Deep copy defaults to avoid mutating DEFAULT_CONFIG
            config = copy.deepcopy(self.DEFAULT_CONFIG)
            config = deep_merge(config, loaded)
            if migrate_loaded_config(config, loaded):
                logger.info(
                    "Migrated connectivity recovery wait from %.0fs to %.0fs",
                    self.LEGACY_RECOVERY_WAIT_SECONDS,
                    self.DEFAULT_RECOVERY_WAIT_SECONDS,
                )
                self._save_config(config)
            return config
        
        logger.debug("No config in DB, creating default")
        config = copy.deepcopy(self.DEFAULT_CONFIG)
        self._save_config(config)
        return config
    
    def _save_config(
        self, config: Optional[Dict[str, Any]] = None
    ) -> None:
        """
        Save configuration to database.
        
        Parameters:
            config (Optional[Dict[str, Any]]): Config to save.
                Defaults to self.config.
        """
        if config is None:
            config = self.config
        if self.config_file is not None:
            self.config_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self.config_file, 'w', encoding='utf-8') as fh:
                json.dump(config, fh, indent=2)
            return
        
        self.db.set_system_setting('stream_checker_config', config)
    
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
    


class ChannelUpdateTracker:
    """Tracks which channels have received M3U updates."""
    
    def __init__(self, tracker_file=None):
        """Initialize tracker using Database backend."""
        from apps.database.manager import get_db_manager
        self.db = get_db_manager()
        self.updates = self._load_updates()
        self.lock = threading.Lock()
        self._save_updates()
    
    def _load_updates(self) -> Dict:
        """Load update tracking data from Database."""
        loaded = self.db.get_system_setting('channel_updates', {})
        if loaded:
            return loaded
        return {'channels': {}, 'last_global_check': None}
    
    def _save_updates(self):
        """Save update tracking data to Database."""
        try:
            self.db.set_system_setting('channel_updates', self.updates)
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
            
            if not channels:
                return []

            # Filter channels by checking_mode setting (channel-level overrides group-level)
            # using only already-available cache/config state. This path can run during
            # startup while UDI or automation storage is not ready yet.
            channel_by_id: Dict[int, Dict[str, Any]] = {}
            try:
                udi = get_udi_manager()
                if hasattr(udi, 'is_initialized') and not udi.is_initialized():
                    udi = None
            except Exception as exc:
                logger.debug(f"Skipping channel metadata lookup while queueing updates: {exc}")
                udi = None

            if udi is not None:
                try:
                    channel_by_id = {
                        ch.get('id'): ch
                        for ch in udi.get_channels()
                        if isinstance(ch, dict) and ch.get('id') is not None
                    }
                except Exception as exc:
                    logger.debug(f"Unable to read channel metadata while queueing updates: {exc}")
                    channel_by_id = {}

            try:
                from apps.automation.automation_config_manager import get_automation_config_manager
                automation_config = get_automation_config_manager()
            except Exception as exc:
                logger.debug(f"Skipping checking-mode filtering while queueing updates: {exc}")
                automation_config = None
            
            filtered_channels = []
            for cid in channels:
                channel_data = channel_by_id.get(cid)
                group_id = channel_data.get('channel_group_id') if channel_data else None

                if automation_config is None:
                    filtered_channels.append(cid)
                    continue

                try:
                    config = automation_config.get_effective_configuration(cid, group_id)
                except Exception as exc:
                    logger.debug(f"Unable to resolve checking profile for channel {cid}: {exc}")
                    filtered_channels.append(cid)
                    continue

                profile = config.get('profile') if config else None
                
                if profile and profile.get('stream_checking', {}).get('enabled', False):
                    filtered_channels.append(cid)
                elif profile is None:
                    try:
                        try:
                            existing_profiles = automation_config.get_all_profiles(include_inactive=True)
                        except TypeError:
                            existing_profiles = automation_config.get_all_profiles()
                    except Exception:
                        existing_profiles = []
                    if not existing_profiles:
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
        self.queue = queue.PriorityQueue(maxsize=max_size)
        self._queue_sequence = 0
        self.queued = {}  # Track channels already in queue dict(channel_id -> stream_count)
        self.queued_priorities = {}
        self.queued_metadata = {}  # Optional channel_id -> metadata for specialized queue entries
        self.in_progress = {} # dict(channel_id -> stream_count)
        self.in_progress_metadata = {}
        self.deferred_metadata_sources = set()
        self.paused = False
        self.completed = set()
        self.failed = {}
        self.lock = threading.Lock()
        
        # ETA Tracking variables
        import collections
        self.stream_processing_times = collections.deque(maxlen=100)
        self.channel_processing_times = collections.deque(maxlen=100)
        self.channel_start_times = {}
        self.batch_started_at = None
        self.last_cleared_at = None
        self.last_clear_reason = None
        self.stats = {
            'total_queued': 0,
            'total_completed': 0,
            'total_failed': 0,
            'current_channel': None,
            'queue_size': 0
        }
    
    def add_channel(self, channel_id: int, priority: int = 0, stream_count: int = 1, metadata: Optional[Dict[str, Any]] = None):
        """Add a channel to the checking queue."""
        with self.lock:
            try:
                normalized_priority = int(priority)
            except (TypeError, ValueError):
                normalized_priority = 0

            # Queued channels can be promoted by a higher-priority entry.  The
            # old heap item stays in PriorityQueue and is ignored as stale after
            # the promoted item is consumed.  Active/completed channels stay
            # protected until an explicit re-queue path removes them.
            if channel_id in self.queued:
                existing_priority = self.queued_priorities.get(channel_id, 0)
                if normalized_priority <= existing_priority:
                    return False

                try:
                    sequence = self._queue_sequence
                    self._queue_sequence += 1
                    self.queue.put((-normalized_priority, sequence, channel_id), block=False)
                except queue.Full:
                    logger.warning(
                        f"Queue is full, cannot promote channel {channel_id} "
                        f"to priority {normalized_priority}"
                    )
                    return False
                self.queued[channel_id] = stream_count
                self.queued_priorities[channel_id] = normalized_priority
                if metadata:
                    merged_metadata = dict(self.queued_metadata.get(channel_id, {}))
                    merged_metadata.update(dict(metadata))
                    self.queued_metadata[channel_id] = merged_metadata
                self.stats['queue_size'] = len(self.queued)
                logger.debug(
                    f"Promoted queued channel {channel_id} "
                    f"from priority {existing_priority} to {normalized_priority}"
                )
                return True

            if channel_id in self.in_progress or channel_id in self.completed:
                return False

            # Check if this is a new "batch" starting (queue is completely empty and no workers are active)
            if self.queue.empty() and len(self.in_progress) == 0:
                self.stats['total_queued'] = 0
                self.stats['total_completed'] = 0
                self.stats['total_failed'] = 0
                self.queued.clear()
                self.queued_priorities.clear()
                self.queued_metadata.clear()
                self.failed.clear()
                self.stream_processing_times.clear()
                self.channel_processing_times.clear()
                self.batch_started_at = datetime.now()
                self.last_cleared_at = None
                self.last_clear_reason = None

            try:
                sequence = self._queue_sequence
                self._queue_sequence += 1
                self.queue.put((-normalized_priority, sequence, channel_id), block=False)
                # We default to 1 stream roughly if unknown, but add_channels will pass precise length
                self.queued[channel_id] = stream_count
                self.queued_priorities[channel_id] = normalized_priority
                if metadata:
                    self.queued_metadata[channel_id] = dict(metadata)
                self.stats['total_queued'] += 1
                self.stats['queue_size'] = len(self.queued)
                logger.debug(f"Added channel {channel_id} to queue (priority: {priority})")
                return True
            except queue.Full:
                logger.warning(f"Queue is full, cannot add channel {channel_id}")
                return False
        return False
    
    def add_channels(self, channel_ids: List[int], priority: int = 0):
        """Add multiple channels to the queue."""
        added = 0
        udi = None
        try:
            from apps.udi import get_udi_manager
            udi = get_udi_manager()
        except Exception as exc:
            logger.debug(
                "Could not access UDI manager while estimating queued stream counts: %s",
                exc,
            )
        
        for channel_id in channel_ids:
            channel = None
            if udi is not None:
                try:
                    is_initialized = getattr(udi, "is_initialized", None)
                    if callable(is_initialized) and not is_initialized():
                        channel = None
                    else:
                        try:
                            channel = udi.get_channel_by_id(channel_id, fetch_if_missing=False)
                        except TypeError:
                            channel = udi.get_channel_by_id(channel_id)
                except Exception as exc:
                    logger.debug(
                        "Could not read cached channel %s for queued stream count: %s",
                        channel_id,
                        exc,
                    )
            stream_count = len(channel.get('streams', [])) if channel else 1
            if self.add_channel(channel_id, priority, stream_count=stream_count):
                added += 1
        logger.info(f"Added {added}/{len(channel_ids)} channels to checking queue")
        return added

    def defer_metadata_sources(self, sources: Optional[set]):
        """Temporarily keep matching specialized entries queued.

        Synchronous automation quality runs bypass the normal worker queue.  When
        those runs are active, event-triggered queue items must remain available
        for the synchronous runner to drain serially between normal channels
        rather than being started in parallel by the background worker.
        """
        with self.lock:
            self.deferred_metadata_sources = set(sources or set())

    def set_paused(self, paused: bool):
        """Pause or resume normal background-worker queue consumption."""
        with self.lock:
            self.paused = bool(paused)

    @staticmethod
    def _wait_after_deferred_entry(timeout: float) -> None:
        try:
            delay = float(timeout or 0)
        except (TypeError, ValueError):
            delay = 0.1
        time.sleep(min(max(delay, 0.05), 1.0))

    def _entry_channel_id(self, item) -> Optional[int]:
        if not item:
            return None
        if len(item) == 3:
            return item[2]
        return item[1]

    def _activate_queued_entry_locked(self, channel_id: int) -> Dict[str, Any]:
        stream_count = self.queued.pop(channel_id)
        self.queued_priorities.pop(channel_id, None)
        metadata = self.queued_metadata.pop(channel_id, {})
        self.in_progress[channel_id] = stream_count
        if metadata:
            self.in_progress_metadata[channel_id] = metadata
        self.channel_start_times[channel_id] = datetime.now()
        self.stats['current_channel'] = channel_id
        self.stats['queue_size'] = len(self.queued)
        return {
            'channel_id': channel_id,
            'metadata': metadata,
        }

    def get_next_entry_for_metadata_sources(self, sources: set) -> Optional[Dict[str, Any]]:
        """Pop the highest-priority queued entry whose metadata source matches."""
        wanted_sources = set(sources or set())
        if not wanted_sources:
            return None

        deferred_items = []
        with self.lock:
            try:
                while True:
                    try:
                        item = self.queue.get_nowait()
                    except queue.Empty:
                        return None

                    channel_id = self._entry_channel_id(item)
                    if channel_id not in self.queued:
                        logger.debug(
                            f"Ignoring stale queued channel {channel_id}; queue entry was cleared"
                        )
                        self.stats['queue_size'] = len(self.queued)
                        continue

                    metadata = self.queued_metadata.get(channel_id, {}) or {}
                    if metadata.get("source") in wanted_sources:
                        return self._activate_queued_entry_locked(channel_id)

                    deferred_items.append(item)
            finally:
                for item in deferred_items:
                    try:
                        self.queue.put_nowait(item)
                    except queue.Full:
                        logger.warning("Queue unexpectedly full while restoring deferred entry")
    
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
    
    def get_next_entry(self, timeout: float = 1.0) -> Optional[Dict[str, Any]]:
        """Get the next queue entry to check."""
        try:
            item = self.queue.get(timeout=timeout)
            if len(item) == 3:
                _, _, channel_id = item
            else:
                _, channel_id = item
            with self.lock:
                if self.paused:
                    try:
                        self.queue.put_nowait(item)
                    except queue.Full:
                        logger.warning("Queue unexpectedly full while paused entry was restored")
                    self.stats['queue_size'] = len(self.queued)
                    self._wait_after_deferred_entry(timeout)
                    return None

                if channel_id not in self.queued:
                    logger.debug(
                        f"Ignoring stale queued channel {channel_id}; queue entry was cleared"
                    )
                    self.stats['queue_size'] = len(self.queued)
                    return None

                metadata = self.queued_metadata.get(channel_id, {}) or {}
                if metadata.get("source") in self.deferred_metadata_sources:
                    try:
                        self.queue.put_nowait(item)
                    except queue.Full:
                        logger.warning(
                            f"Queue unexpectedly full while deferring channel {channel_id}"
                        )
                    self.stats['queue_size'] = len(self.queued)
                    self._wait_after_deferred_entry(timeout)
                    return None

                return self._activate_queued_entry_locked(channel_id)
        except queue.Empty:
            return None

    def get_next_channel(self, timeout: float = 1.0) -> Optional[int]:
        """Get the next channel to check."""
        entry = self.get_next_entry(timeout=timeout)
        return entry.get('channel_id') if entry else None
    
    def mark_completed(self, channel_id: int) -> bool:
        """Mark a channel check as completed.

        Returns False when the channel is no longer registered as active. This
        can happen when a queue clear aborts an in-flight check and the worker
        exits slightly later.
        """
        with self.lock:
            if channel_id not in self.in_progress and channel_id not in self.channel_start_times:
                logger.debug(
                    f"Ignoring completion for channel {channel_id}; no active queue entry exists"
                )
                return False

            # Calculate stream processing duration
            if channel_id in self.channel_start_times:
                duration_sec = (datetime.now() - self.channel_start_times[channel_id]).total_seconds()
                stream_count = self.in_progress.get(channel_id, 1)
                self.channel_processing_times.append(duration_sec)
                if stream_count > 0:
                    time_per_stream = duration_sec / stream_count
                    self.stream_processing_times.append(time_per_stream)
                del self.channel_start_times[channel_id]

            if channel_id in self.in_progress:
                del self.in_progress[channel_id]
            self.in_progress_metadata.pop(channel_id, None)
            self.completed.add(channel_id)
            self.stats['total_completed'] += 1
            if self.stats['current_channel'] == channel_id:
                self.stats['current_channel'] = None
            logger.debug(f"Marked channel {channel_id} as completed")
            return True
    
    def mark_failed(self, channel_id: int, error: str) -> bool:
        """Mark a channel check as failed.

        Returns False when the channel is no longer registered as active.
        """
        with self.lock:
            if channel_id not in self.in_progress and channel_id not in self.channel_start_times:
                logger.debug(
                    f"Ignoring failure for channel {channel_id}; no active queue entry exists"
                )
                return False

            if channel_id in self.channel_start_times:
                duration_sec = (datetime.now() - self.channel_start_times[channel_id]).total_seconds()
                self.channel_processing_times.append(duration_sec)
                del self.channel_start_times[channel_id]
                
            if channel_id in self.in_progress:
                del self.in_progress[channel_id]
            self.in_progress_metadata.pop(channel_id, None)
            self.failed[channel_id] = {
                'error': error,
                'timestamp': datetime.now().isoformat()
            }
            self.stats['total_failed'] += 1
            if self.stats['current_channel'] == channel_id:
                self.stats['current_channel'] = None
            logger.warning(f"Marked channel {channel_id} as failed: {error}")
            return True
    
    def get_status(self) -> Dict:
        """Get current queue status."""
        with self.lock:
            return {
                'queue_size': len(self.queued),
                'queued': len(self.queued),
                'in_progress': len(self.in_progress),
                'completed': len(self.completed),
                'failed': len(self.failed),
                
                # Expose stream ETA calculations to API Response payload
                'queued_streams_count': sum(self.queued.values()),
                'in_progress_streams_count': sum(self.in_progress.values()),
                'avg_stream_process_time_sec': sum(self.stream_processing_times) / len(self.stream_processing_times) if self.stream_processing_times else 0,
                'avg_channel_process_time_sec': sum(self.channel_processing_times) / len(self.channel_processing_times) if self.channel_processing_times else 0,
                
                'current_channel': self.stats['current_channel'],
                'total_queued': self.stats['total_queued'],
                'total_completed': self.stats['total_completed'],
                'total_failed': self.stats['total_failed'],
                'started_at': self.batch_started_at.isoformat() if self.batch_started_at else None,
                'state': self._state_locked(),
                'paused': self.paused,
                'last_cleared_at': self.last_cleared_at,
                'last_clear_reason': self.last_clear_reason
            }

    def _state_locked(self) -> str:
        """Return the queue lifecycle state while self.lock is held."""
        if self.in_progress:
            return 'checking'
        if self.queued:
            return 'queued'
        if self.completed or self.failed:
            return 'completed'
        if self.last_cleared_at:
            return 'cleared'
        return 'idle'

    def clear(self, reason: str = 'manual') -> Dict:
        """Clear the queue and reset stats."""
        with self.lock:
            cleared = {
                'queued': len(self.queued),
                'in_progress': len(self.in_progress),
                'completed': len(self.completed),
                'failed': len(self.failed),
                'queue_size': self.queue.qsize()
            }
            while not self.queue.empty():
                try:
                    self.queue.get_nowait()
                except queue.Empty:
                    break
            self.queued.clear()
            self.queued_priorities.clear()
            self.queued_metadata.clear()
            self.in_progress.clear()
            self.in_progress_metadata.clear()
            self.completed.clear()
            self.failed.clear()
            self.paused = False
            self.channel_start_times.clear()
            self.stream_processing_times.clear()
            self.channel_processing_times.clear()
            self.batch_started_at = None
            self.stats = {
                'total_queued': 0,
                'total_completed': 0,
                'total_failed': 0,
                'current_channel': None,
                'queue_size': 0
            }
            self.last_cleared_at = datetime.now().isoformat()
            self.last_clear_reason = reason
        logger.info("Queue cleared")
        return cleared

    def is_empty(self) -> bool:
        """Check if the queue is empty."""
        with self.lock:
            return not self.queued


class StreamCheckerProgress:
    """Manages progress tracking for stream checker operations."""
    
    def __init__(self, progress_file: Optional[Any] = None):
        self.lock = threading.Lock()
        self.progress_file = progress_file

    @staticmethod
    def _build_provider_progress(
        streams_detail: Optional[List[Dict[str, Any]]],
        provider_profile_slots: Optional[Dict[str, List[Dict[str, Any]]]] = None,
    ) -> List[Dict[str, Any]]:
        """Build compact per-account progress counters from per-stream rows."""
        if not streams_detail:
            return []

        failed_statuses = {'error', 'dead', 'blank', 'freeze', 'low_quality', 'loop_detected'}
        grouped: Dict[str, Dict[str, Any]] = {}

        for stream in streams_detail:
            account_name = stream.get('m3u_account') or 'Unknown'
            account_id = stream.get('m3u_account_id')
            account_key = str(account_id) if account_id not in (None, '') else account_name
            status = stream.get('status') or 'pending'
            provider = grouped.setdefault(
                account_key,
                {
                    'account_id': account_id,
                    'name': account_name,
                    'total': 0,
                    'status_counts': Counter(),
                    'wait_reason_counts': Counter(),
                },
            )
            provider['total'] += 1
            provider['status_counts'][status] += 1
            if status in {'waiting_provider_limit', 'provider_limit_wait_timeout', 'viewer_preempted'}:
                reason = (
                    stream.get('reason_detail')
                    or stream.get('quality_reason_detail')
                    or stream.get('skipped_reason')
                )
                if reason and reason != 'none':
                    provider['wait_reason_counts'][str(reason)] += 1

        provider_progress: List[Dict[str, Any]] = []
        for provider in grouped.values():
            counts = provider['status_counts']
            wait_reason_counts = provider['wait_reason_counts']
            checking = counts.get('checking', 0) + counts.get('probing', 0)
            waiting = counts.get('waiting_provider_limit', 0)
            pending = counts.get('pending', 0)
            completed = counts.get('completed', 0)
            skipped = counts.get('provider_limit_wait_timeout', 0) + counts.get('viewer_preempted', 0)
            failed = sum(counts.get(status, 0) for status in failed_statuses)
            finished = completed + skipped + failed
            dominant_wait_reason = None
            if wait_reason_counts:
                dominant_wait_reason = sorted(
                    wait_reason_counts.items(),
                    key=lambda item: (-item[1], item[0]),
                )[0][0]

            if waiting:
                state = 'waiting_provider_limit'
            elif checking:
                state = 'checking'
            elif pending:
                state = 'pending'
            elif failed:
                state = 'attention'
            elif finished >= provider['total']:
                state = 'complete'
            else:
                state = 'idle'

            profile_slots = []
            if provider_profile_slots:
                account_id = provider.get('account_id')
                profile_slots = provider_profile_slots.get(str(account_id)) if account_id not in (None, '') else None
                if not profile_slots:
                    profile_slots = provider_profile_slots.get(provider['name'])
                if not isinstance(profile_slots, list):
                    profile_slots = []

            provider_row = {
                'account_id': provider.get('account_id'),
                'name': provider['name'],
                'total': provider['total'],
                'checking': checking,
                'waiting': waiting,
                'pending': pending,
                'completed': completed,
                'skipped': skipped,
                'failed': failed,
                'finished': finished,
                'state': state,
                'status_counts': dict(sorted(counts.items())),
                'wait_reason_counts': dict(sorted(wait_reason_counts.items())),
                'dominant_wait_reason': dominant_wait_reason,
            }
            if profile_slots:
                provider_row['profile_slots'] = profile_slots
            provider_row['capacity_explanation'] = StreamCheckerProgress._build_capacity_explanation(
                provider_row,
                profile_slots=profile_slots,
                dominant_wait_reason=dominant_wait_reason,
                wait_reason_counts=wait_reason_counts,
            )
            provider_progress.append(provider_row)

        return sorted(
            provider_progress,
            key=lambda item: (
                0 if item['checking'] or item['waiting'] else 1,
                -item['waiting'],
                -item['checking'],
                item['name'].lower(),
            ),
        )

    @staticmethod
    def _build_provider_summary(provider_progress: List[Dict[str, Any]]) -> Dict[str, int]:
        """Build aggregate provider scheduling counters for the progress API."""
        capacity_summaries = [
            item.get('capacity_explanation', {}).get('profile_slot_summary', {})
            for item in provider_progress
            if isinstance(item.get('capacity_explanation'), dict)
        ]
        return {
            'total_providers': len(provider_progress),
            'active_providers': sum(1 for item in provider_progress if item.get('checking', 0) > 0),
            'waiting_providers': sum(1 for item in provider_progress if item.get('waiting', 0) > 0),
            'checking_streams': sum(item.get('checking', 0) for item in provider_progress),
            'waiting_streams': sum(item.get('waiting', 0) for item in provider_progress),
            'pending_streams': sum(item.get('pending', 0) for item in provider_progress),
            'completed_streams': sum(item.get('completed', 0) for item in provider_progress),
            'skipped_streams': sum(item.get('skipped', 0) for item in provider_progress),
            'failed_streams': sum(item.get('failed', 0) for item in provider_progress),
            'profile_slots_total': sum(item.get('total', 0) for item in capacity_summaries),
            'profile_slots_full': sum(item.get('full', 0) for item in capacity_summaries),
            'profile_slots_open': sum(item.get('open', 0) for item in capacity_summaries),
            'profile_slots_with_real_viewers': sum(item.get('with_real_viewers', 0) for item in capacity_summaries),
            'profile_slots_with_shadow_watchers': sum(item.get('with_shadow_watchers', 0) for item in capacity_summaries),
            'profile_slots_with_streamflow_workers': sum(item.get('with_streamflow_workers', 0) for item in capacity_summaries),
            'profile_slots_with_teamarr_preflight': sum(item.get('with_teamarr_preflight', 0) for item in capacity_summaries),
            'profile_slots_with_quality_checks': sum(item.get('with_quality_checks', 0) for item in capacity_summaries),
        }

    @staticmethod
    def _build_capacity_explanation(
        provider: Dict[str, Any],
        *,
        profile_slots: Optional[List[Dict[str, Any]]] = None,
        dominant_wait_reason: Optional[str] = None,
        wait_reason_counts: Optional[Counter] = None,
    ) -> Dict[str, Any]:
        """Build a compact operator-facing capacity explanation without private stream data."""
        slots = profile_slots or []

        def safe_count(value: Any) -> int:
            try:
                return max(0, int(value or 0))
            except (TypeError, ValueError):
                return 0

        explicit_viewer_context = any(
            isinstance(slot, dict)
            and ('real_viewers' in slot or 'shadow_watchers' in slot)
            for slot in slots
        )

        def slot_real_viewer_count(slot: Dict[str, Any]) -> int:
            if 'real_viewers' in slot or 'shadow_watchers' in slot:
                return safe_count(slot.get('real_viewers'))
            return safe_count(slot.get('active_viewers'))

        total_slots = len(slots)
        full_slots = sum(1 for slot in slots if slot.get('full'))
        checking_slots = sum(1 for slot in slots if safe_count(slot.get('checking')) > 0)
        real_viewer_slots = sum(
            1
            for slot in slots
            if slot_real_viewer_count(slot) > 0
        )
        shadow_watcher_slots = sum(1 for slot in slots if safe_count(slot.get('shadow_watchers')) > 0)
        teamarr_preflight_slots = sum(1 for slot in slots if safe_count(slot.get('teamarr_preflight')) > 0)
        quality_check_slots = sum(
            1
            for slot in slots
            if safe_count(slot.get('quality_checks', slot.get('quality_checking'))) > 0
        )
        unlimited_slots = sum(1 for slot in slots if slot.get('unlimited'))
        open_slots = sum(
            1
            for slot in slots
            if slot.get('unlimited') or int(slot.get('available') or 0) > 0
        )
        limited_slots = max(0, total_slots - unlimited_slots)
        counts = dict(sorted((wait_reason_counts or Counter()).items()))
        sources = []

        account_capacity_reasons = {'provider_capacity', 'provider_capacity_unavailable', 'max_streams_reached'}
        worker_capacity_reasons = {'checking_capacity', 'global_worker_limit'}
        viewer_capacity_reasons = {'active_viewers', 'quota_consumed_by_active_viewers', 'viewer_preempted'}
        shadow_capacity_reasons = {'shadow_watchers', 'shadow_watcher_capacity'}
        capacity_reasons = (
            account_capacity_reasons
            | worker_capacity_reasons
            | viewer_capacity_reasons
            | shadow_capacity_reasons
        )

        if dominant_wait_reason in viewer_capacity_reasons and (
            real_viewer_slots > 0 or not explicit_viewer_context
        ):
            sources.append('real_viewers')
        if dominant_wait_reason in shadow_capacity_reasons:
            sources.append('shadow_watchers')
        if dominant_wait_reason in worker_capacity_reasons:
            sources.append('streamflow_workers')
        if dominant_wait_reason in account_capacity_reasons:
            sources.append('provider_account')
        if total_slots:
            sources.append('provider_profile')
        if shadow_watcher_slots:
            sources.append('shadow_watchers')
        if checking_slots:
            sources.append('streamflow_workers')
        if teamarr_preflight_slots:
            sources.append('teamarr_preflight')
        if quality_check_slots:
            sources.append('quality_checks')
        if real_viewer_slots:
            sources.append('real_viewers')
        if full_slots:
            sources.append('profile_limit')

        deduped_sources = []
        for source in sources:
            if source not in deduped_sources:
                deduped_sources.append(source)

        if dominant_wait_reason == 'viewer_preempted':
            state = 'viewer_preempted'
            message = 'A live viewer needed the slot; the probe yielded and can be retried later.'
            action = 'retry_later'
        elif dominant_wait_reason in shadow_capacity_reasons:
            state = 'shadow_watcher_capacity'
            message = 'A Shadow Monitor watcher is using the provider profile slot without being counted as a real viewer.'
            action = 'wait_for_shadow_watcher'
        elif dominant_wait_reason in {'active_viewers', 'quota_consumed_by_active_viewers'} or real_viewer_slots:
            state = 'viewer_protected'
            message = 'Real viewer capacity is protected before StreamFlow probes use the slot.'
            action = 'wait_for_viewer_capacity'
        elif provider.get('waiting', 0) > 0:
            state = 'waiting_for_capacity'
            if full_slots:
                message = 'Waiting for a provider profile slot to free up.'
            elif provider.get('checking', 0) > 0:
                message = 'Waiting behind active StreamFlow probes for this provider.'
            else:
                message = 'Waiting for provider account capacity.'
            action = 'wait_for_slot'
        elif provider.get('skipped', 0) > 0 and (full_slots or dominant_wait_reason in capacity_reasons):
            state = 'capacity_timeout'
            if full_slots:
                message = 'Provider profile capacity did not free up before the wait timeout.'
            elif dominant_wait_reason in worker_capacity_reasons:
                message = 'StreamFlow worker capacity did not free up before the wait timeout.'
            else:
                message = 'Provider account capacity did not free up before the wait timeout.'
            action = 'review_capacity_or_retry'
        elif provider.get('checking', 0) > 0:
            state = 'checking'
            message = 'StreamFlow has active probes using provider capacity.'
            action = 'watch_progress'
        elif open_slots > 0:
            state = 'available'
            message = 'At least one provider profile slot is available.'
            action = 'none'
        else:
            state = 'idle'
            message = 'No provider capacity wait is active.'
            action = 'none'

        return {
            'state': state,
            'message': message,
            'operator_action': action,
            'primary_reason': dominant_wait_reason,
            'wait_reason_counts': counts,
            'capacity_sources': deduped_sources,
            'has_free_profile_slot': open_slots > 0,
            'has_full_profile_slot': full_slots > 0,
            'has_real_viewer_usage': (
                real_viewer_slots > 0
                or dominant_wait_reason in {
                    'active_viewers',
                    'quota_consumed_by_active_viewers',
                    'viewer_preempted',
                }
                and not (explicit_viewer_context and shadow_watcher_slots > 0 and real_viewer_slots == 0)
            ),
            'has_shadow_watcher_usage': (
                shadow_watcher_slots > 0
                or dominant_wait_reason in shadow_capacity_reasons
            ),
            'has_streamflow_worker_usage': (
                checking_slots > 0
                or provider.get('checking', 0) > 0
                or dominant_wait_reason in {'checking_capacity', 'global_worker_limit'}
            ),
            'has_teamarr_preflight_usage': teamarr_preflight_slots > 0,
            'has_quality_check_usage': quality_check_slots > 0,
            'profile_slot_summary': {
                'total': total_slots,
                'limited': limited_slots,
                'unlimited': unlimited_slots,
                'full': full_slots,
                'open': open_slots,
                'with_real_viewers': real_viewer_slots,
                'with_shadow_watchers': shadow_watcher_slots,
                'with_streamflow_workers': checking_slots,
                'with_teamarr_preflight': teamarr_preflight_slots,
                'with_quality_checks': quality_check_slots,
            },
        }
    
    def update(self, channel_id: int, channel_name: str, current: int, total: int,
               current_stream: str = '', status: str = 'checking', step: str = '', step_detail: str = '',
               streams_detail: Optional[List[Dict[str, Any]]] = None, stream_duration: Optional[int] = None,
               is_single_channel_check: bool = False,
               provider_profile_slots: Optional[Dict[str, List[Dict[str, Any]]]] = None,
               automation_profile_id: Optional[str] = None,
               automation_profile_name: Optional[str] = None,
               automation_profile_source: Optional[str] = None,
               run_mode: Optional[str] = None,
               run_profile_id: Optional[str] = None,
               run_profile_name: Optional[str] = None,
               run_profile_source: Optional[str] = None,
               quality_profile_id: Optional[str] = None,
               quality_profile_name: Optional[str] = None,
               quality_profile_source: Optional[str] = None,
               capacity_profile_name: Optional[str] = None,
               capacity_profile_source: Optional[str] = None):
        """Update progress information."""
        from apps.database.manager import get_db_manager
        with self.lock:
            resolved_run_mode = run_mode or ("single_channel_check" if is_single_channel_check else "stream_checker")
            resolved_run_profile_id = run_profile_id if run_profile_id not in (None, '') else automation_profile_id
            resolved_run_profile_name = run_profile_name if run_profile_name not in (None, '') else automation_profile_name
            resolved_run_profile_source = run_profile_source if run_profile_source not in (None, '') else automation_profile_source
            resolved_quality_profile_id = quality_profile_id if quality_profile_id not in (None, '') else automation_profile_id
            resolved_quality_profile_name = quality_profile_name if quality_profile_name not in (None, '') else automation_profile_name
            resolved_quality_profile_source = quality_profile_source if quality_profile_source not in (None, '') else automation_profile_source
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
                'stream_duration': stream_duration,
                'is_single_channel_check': is_single_channel_check,
                'run_mode': resolved_run_mode,
                'timestamp': datetime.now().isoformat()
            }
            for key, value in {
                'automation_profile_id': automation_profile_id,
                'automation_profile_name': automation_profile_name,
                'automation_profile_source': automation_profile_source,
                'run_profile_id': resolved_run_profile_id,
                'run_profile_name': resolved_run_profile_name,
                'run_profile_source': resolved_run_profile_source,
                'quality_profile_id': resolved_quality_profile_id,
                'quality_profile_name': resolved_quality_profile_name,
                'quality_profile_source': resolved_quality_profile_source,
                'capacity_profile_name': capacity_profile_name,
                'capacity_profile_source': capacity_profile_source,
            }.items():
                if value not in (None, ''):
                    progress_data[key] = value
            if streams_detail is not None:
                progress_data['streams_detail'] = streams_detail
                provider_progress = self._build_provider_progress(streams_detail, provider_profile_slots)
                if provider_progress:
                    progress_data['provider_progress'] = provider_progress
                    progress_data['provider_summary'] = self._build_provider_summary(provider_progress)
            
            try:
                db = get_db_manager()
                existing = db.get_system_setting('stream_checker_progress', {}) or {}
                same_channel = str(existing.get('channel_id')) == str(channel_id)
                if same_channel:
                    for key in (
                        'automation_profile_id',
                        'automation_profile_name',
                        'automation_profile_source',
                        'run_mode',
                        'run_profile_id',
                        'run_profile_name',
                        'run_profile_source',
                        'quality_profile_id',
                        'quality_profile_name',
                        'quality_profile_source',
                        'capacity_profile_name',
                        'capacity_profile_source',
                    ):
                        if key not in progress_data and existing.get(key) not in (None, ''):
                            progress_data[key] = existing.get(key)
                db.set_system_setting('stream_checker_progress', progress_data)
            except Exception as e:
                logger.warning(f"Failed to write progress to database: {e}")
            if self.progress_file:
                try:
                    import json
                    from pathlib import Path

                    path = Path(self.progress_file)
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(json.dumps(progress_data), encoding='utf-8')
                except Exception as e:
                    logger.warning(f"Failed to write progress to file: {e}")
    
    def clear(self):
        """Clear progress tracking."""
        from apps.database.manager import get_db_manager
        with self.lock:
            try:
                db = get_db_manager()
                db.set_system_setting('stream_checker_progress', {})
            except Exception as e:
                logger.warning(f"Failed to clear progress in database: {e}")
            if self.progress_file:
                try:
                    from pathlib import Path

                    Path(self.progress_file).write_text("{}", encoding='utf-8')
                except Exception as e:
                    logger.warning(f"Failed to clear progress file: {e}")
    
    def get(self) -> Optional[Dict]:
        """Get current progress."""
        from apps.database.manager import get_db_manager
        with self.lock:
            try:
                db = get_db_manager()
                data = db.get_system_setting('stream_checker_progress', {})
                if data:
                    return data
            except Exception:
                pass
            if self.progress_file:
                try:
                    import json
                    from pathlib import Path

                    path = Path(self.progress_file)
                    if path.exists():
                        data = json.loads(path.read_text(encoding='utf-8'))
                        return data if data else None
                except Exception:
                    return None
            return None
