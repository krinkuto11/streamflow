"""Support components used by the stream checker service."""

import copy
import queue
import threading
from datetime import datetime
from typing import Any, Dict, List, Optional

from apps.udi import get_udi_manager
from apps.core.logging_config import setup_logging, log_function_call

logger = setup_logging(__name__)


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
        """
        from apps.database.manager import get_db_manager
        self.db = get_db_manager()
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
        
        loaded = self.db.get_system_setting('stream_checker_config', {})
        if loaded:
            logger.debug(f"Loaded config from DB with {len(loaded)} top-level keys")
            # Deep copy defaults to avoid mutating DEFAULT_CONFIG
            config = copy.deepcopy(self.DEFAULT_CONFIG)
            config.update(loaded)
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
            
            # Filter channels by checking_mode setting (channel-level overrides group-level)
            # Filter channels by checking_mode setting (channel-level overrides group-level)
            # Need to get full channel data to access channel_group_id
            udi = get_udi_manager()
            from apps.automation.automation_config_manager import get_automation_config_manager
            automation_config = get_automation_config_manager()
            
            filtered_channels = []
            for cid in channels:
                # Get channel data to access group_id
                channel_data = None
                for ch in udi.get_channels():
                    if ch.get('id') == cid:
                        channel_data = ch
                        break
                
                group_id = channel_data.get('channel_group_id') if channel_data else None
                
                # Get effective profile
                # Get effective profile via configuration
                config = automation_config.get_effective_configuration(cid, group_id)
                profile = config.get('profile') if config else None
                
                if profile and profile.get('stream_checking', {}).get('enabled', False):
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
        self.queued = {}  # Track channels already in queue dict(channel_id -> stream_count)
        self.in_progress = {} # dict(channel_id -> stream_count)
        self.completed = set()
        self.failed = {}
        self.lock = threading.Lock()
        
        # ETA Tracking variables
        import collections
        self.stream_processing_times = collections.deque(maxlen=100)
        self.channel_start_times = {}
        self.stats = {
            'total_queued': 0,
            'total_completed': 0,
            'total_failed': 0,
            'current_channel': None,
            'queue_size': 0
        }
    
    def add_channel(self, channel_id: int, priority: int = 0, stream_count: int = 1):
        """Add a channel to the checking queue."""
        with self.lock:
            # Check if this is a new "batch" starting (queue is completely empty and no workers are active)
            if self.queue.empty() and len(self.in_progress) == 0:
                self.stats['total_queued'] = 0
                self.stats['total_completed'] = 0
                self.stats['total_failed'] = 0
                self.queued.clear()
                self.completed.clear()
                self.failed.clear()

            # Check if channel is already queued, in progress, or completed
            if channel_id not in self.queued and channel_id not in self.in_progress and channel_id not in self.completed:
                try:
                    self.queue.put((priority, channel_id), block=False)
                    # We default to 1 stream roughly if unknown, but add_channels will pass precise length 
                    self.queued[channel_id] = stream_count
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
        from apps.udi import get_udi_manager
        udi = get_udi_manager()
        
        for channel_id in channel_ids:
            channel = udi.get_channel_by_id(channel_id)
            stream_count = len(channel.get('streams', [])) if channel else 1
            if self.add_channel(channel_id, priority, stream_count=stream_count):
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
                stream_count = self.queued.pop(channel_id, 1)  # Remove from queued dict
                self.in_progress[channel_id] = stream_count
                self.channel_start_times[channel_id] = datetime.now()
                self.stats['current_channel'] = channel_id
                self.stats['queue_size'] = self.queue.qsize()
            return channel_id
        except queue.Empty:
            return None
    
    def mark_completed(self, channel_id: int):
        """Mark a channel check as completed."""
        with self.lock:
            # Calculate stream processing duration
            if channel_id in self.channel_start_times:
                duration_sec = (datetime.now() - self.channel_start_times[channel_id]).total_seconds()
                stream_count = self.in_progress.get(channel_id, 1)
                if stream_count > 0:
                    time_per_stream = duration_sec / stream_count
                    self.stream_processing_times.append(time_per_stream)
                del self.channel_start_times[channel_id]

            if channel_id in self.in_progress:
                del self.in_progress[channel_id]
            self.completed.add(channel_id)
            self.stats['total_completed'] += 1
            if self.stats['current_channel'] == channel_id:
                self.stats['current_channel'] = None
            logger.debug(f"Marked channel {channel_id} as completed")
    
    def mark_failed(self, channel_id: int, error: str):
        """Mark a channel check as failed."""
        with self.lock:
            if channel_id in self.channel_start_times:
                del self.channel_start_times[channel_id]
                
            if channel_id in self.in_progress:
                del self.in_progress[channel_id]
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
                
                # Expose stream ETA calculations to API Response payload
                'queued_streams_count': sum(self.queued.values()),
                'in_progress_streams_count': sum(self.in_progress.values()),
                'avg_stream_process_time_sec': sum(self.stream_processing_times) / len(self.stream_processing_times) if self.stream_processing_times else 0,
                
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

    def is_empty(self) -> bool:
        """Check if the queue is empty."""
        return self.queue.empty()


class StreamCheckerProgress:
    """Manages progress tracking for stream checker operations."""
    
    def __init__(self):
        self.lock = threading.Lock()
    
    def update(self, channel_id: int, channel_name: str, current: int, total: int,
               current_stream: str = '', status: str = 'checking', step: str = '', step_detail: str = '',
               streams_detail: Optional[Dict] = None, stream_duration: Optional[int] = None,
               is_single_channel_check: bool = False):
        """Update progress information."""
        from apps.database.manager import get_db_manager
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
                'stream_duration': stream_duration,
                'is_single_channel_check': is_single_channel_check,
                'timestamp': datetime.now().isoformat()
            }
            if streams_detail is not None:
                progress_data['streams_detail'] = streams_detail
            
            try:
                db = get_db_manager()
                db.set_system_setting('stream_checker_progress', progress_data)
            except Exception as e:
                logger.warning(f"Failed to write progress to database: {e}")
    
    def clear(self):
        """Clear progress tracking."""
        from apps.database.manager import get_db_manager
        with self.lock:
            try:
                db = get_db_manager()
                db.set_system_setting('stream_checker_progress', {})
            except Exception as e:
                logger.warning(f"Failed to clear progress in database: {e}")
    
    def get(self) -> Optional[Dict]:
        """Get current progress."""
        from apps.database.manager import get_db_manager
        with self.lock:
            try:
                db = get_db_manager()
                data = db.get_system_setting('stream_checker_progress', {})
                return data if data else None
            except Exception:
                return None
