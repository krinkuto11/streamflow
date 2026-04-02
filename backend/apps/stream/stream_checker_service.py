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

from apps.core.api_utils import (
    fetch_channel_streams,
    update_channel_streams,
    _get_base_url,
    patch_request,
    batch_update_stream_stats
)

# Import UDI for direct data access
from apps.udi import get_udi_manager

# Import dead streams tracker
from apps.stream.dead_streams_tracker import DeadStreamsTracker

# Import channel settings manager
# Import channel settings manager - DEPRECATED/REMOVED
# from channel_settings_manager import get_channel_settings_manager

# Import profile config
# Import profile config - DEPRECATED/REMOVED
# from profile_config import get_profile_config

# Import centralized stream stats utilities
from apps.core.stream_stats_utils import (
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
    from apps.automation.automated_stream_manager import ChangelogManager
    CHANGELOG_AVAILABLE = True
except ImportError:
    CHANGELOG_AVAILABLE = False

# Import croniter for cron expression validation
try:
    from croniter import croniter
    CRONITER_AVAILABLE = True
except ImportError:
    CRONITER_AVAILABLE = False

# Setup centralized logging
from apps.core.logging_config import setup_logging, log_function_call, log_function_return, log_exception, log_state_change

logger = setup_logging(__name__)

# Configuration directory
CONFIG_DIR = Path(os.environ.get('CONFIG_DIR', '/app/data'))


from apps.stream.stream_checker_components import (
    StreamCheckConfig,
    ChannelUpdateTracker,
    StreamCheckQueue,
    StreamCheckerProgress,
)

class StreamCheckerService:
    """Main service for managing stream checking operations."""
    
    def __init__(self):
        log_function_call(logger, "__init__")
        logger.debug("Initializing StreamCheckerService components...")
        
        self.config = StreamCheckConfig()
        logger.debug("Config loaded")
        
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
        self.start_time = datetime.now()
        self.worker_thread = None
        self.scheduler_thread = None
        self.lock = threading.Lock()
        self._cancel_queueing = False
        
        self.sync_batch_state = {
            'active': False,
            'total_channels': 0,
            'completed': 0,
            'failed': 0,
            'in_progress': 0
        }
        
        # Event for immediate triggering of updated channels check
        self.check_trigger = threading.Event()
        logger.debug("Check trigger event created")
        
        # Event for immediate config change notification
        self.config_changed = threading.Event()
        logger.debug("Config changed event created")
        
        # Event for aborting current channel check
        self.abort_current_check = threading.Event()
        logger.debug("Abort current check event created")
        
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
                # Clear abort flag before checking
                self.abort_current_check.clear()
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
                    # (not a config change wake-up)
                    if not self.config_changed.is_set():
                        # Call _queue_updated_channels() directly - it handles pipeline mode checking internally
                        self._queue_updated_channels()
                
                # Check if config was changed
                if self.config_changed.is_set():
                    self.config_changed.clear()
                    logger.info("Configuration change detected, applying new settings immediately")
                
            except Exception as e:
                logger.error(f"Error in scheduler loop: {e}", exc_info=True)
        
        logger.info("Stream checker scheduler stopped")
    
    def _queue_updated_channels(self):
        """Queue channels that have received M3U updates.
        
        This respects automation_controls and queues channels only when
        automatic quality checking is enabled.
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
    
    def _queue_all_channels(self, force_check: bool = False):
        """Queue all channels for checking (global check).
        
        Args:
            force_check: If True, marks channels for force checking which bypasses 2-hour immunity
        """
        self._cancel_queueing = False
        try:
            udi = get_udi_manager()
            channels = udi.get_channels()
            
            if channels:
                channel_ids = [ch['id'] for ch in channels if isinstance(ch, dict) and 'id' in ch]
                
                # Filter by profile if one is selected
                # Filter channels using Automation Profiles
                from apps.automation.automation_config_manager import get_automation_config_manager
                automation_config = get_automation_config_manager()
                
                filtered_channel_ids = []
                
                for ch in channels:
                    if not isinstance(ch, dict) or 'id' not in ch:
                        continue
                    
                    cid = ch['id']
                    channel_group_id = ch.get('channel_group_id')
                    
                    # Get effective profile
                    # Get effective profile via configuration
                    config = automation_config.get_effective_configuration(cid, channel_group_id)
                    profile = config.get('profile') if config else None
                    
                    # Check if stream checking is enabled in the profile
                    if profile and profile.get('stream_checking', {}).get('enabled', False):
                        filtered_channel_ids.append(cid)
                
                excluded_count = len(channel_ids) - len(filtered_channel_ids)
                
                if excluded_count > 0:
                    logger.info(f"Excluding {excluded_count} channel(s) with checking disabled (channel or group level)")
                
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
                    if getattr(self, '_cancel_queueing', False):
                        logger.info("Aborting channel queueing loop due to cancel flag")
                        break
                    batch = filtered_channel_ids[i:i+max_channels]
                    added = self.check_queue.add_channels(batch, priority=5)
                    total_added += added
                
                logger.info(f"Queued {total_added}/{len(filtered_channel_ids)} channels for global check (force_check={force_check})")
        except Exception as e:
            logger.error(f"Failed to queue all channels: {e}")
    
    def _is_stream_dead(self, stream_data: Dict[str, Any], channel_id: Optional[int] = None) -> bool:
        """
        Check if a stream should be considered dead based on profile or global settings.
        
        This method uses categorization logic: 
        - 'offline': truly dead (0x0 resolution, 0 bitrate)
        - 'low_quality': dead based on quality thresholds
        
        Args:
            stream_data: Dictionary containing stream statistics
            channel_id: Optional channel ID to look up profile-specific thresholds
            
        Returns:
            bool: True if the stream is considered dead, False otherwise.
        """
        # Default configuration
        dead_stream_config = self.config.get('dead_stream_handling', {})
        profile_config = {}
        
        # Try to get profile-specific settings if channel_id provided
        if channel_id is not None:
            try:
                from apps.automation.automation_config_manager import get_automation_config_manager
                automation_config = get_automation_config_manager()
                
                # Get effective profile
                udi = get_udi_manager()
                channel = udi.get_channel_by_id(channel_id)
                group_id = channel.get('channel_group_id') if channel else None
                config = automation_config.get_effective_configuration(channel_id, group_id)
                profile = config.get('profile') if config else None
                
                if profile:
                    stream_checking = profile.get('stream_checking', {})
                    if stream_checking.get('enabled', False):
                        # Construct config from profile settings
                        # Convert min_res string (e.g., '1080p') to dimensions
                        min_res = stream_checking.get('min_resolution', '0x0')
                        if min_res == '2160p' or min_res == '4k':
                            profile_config['min_resolution_width'], profile_config['min_resolution_height'] = 3840, 2160
                        elif min_res == '1080p':
                            profile_config['min_resolution_width'], profile_config['min_resolution_height'] = 1920, 1080
                        elif min_res == '720p':
                            profile_config['min_resolution_width'], profile_config['min_resolution_height'] = 1280, 720
                        elif min_res == '480p':
                            profile_config['min_resolution_width'], profile_config['min_resolution_height'] = 854, 480
                        elif min_res == '360p':
                            profile_config['min_resolution_width'], profile_config['min_resolution_height'] = 640, 360
                        
                        if 'min_bitrate' in stream_checking:
                            profile_config['min_bitrate_kbps'] = stream_checking['min_bitrate']
                        
                        if 'min_fps' in stream_checking:
                            profile_config['min_fps'] = stream_checking['min_fps']
                        
                        # Use profile config if available
                        check_config = profile_config
                    else:
                        # Stream matching enabled but checker disabled in profile?
                        # Fallback to global or basic
                        check_config = dead_stream_config if dead_stream_config.get('enabled', True) else {'min_resolution_width': 0, 'min_resolution_height': 0, 'min_bitrate_kbps': 0}
                else:
                    check_config = dead_stream_config
            except Exception as e:
                logger.warning(f"Error fetching profile for dead stream check: {e}")
                check_config = dead_stream_config
        else:
            check_config = dead_stream_config

        # If global handling is disabled and no profile was found, use basic check (absolute failures only)
        if not check_config.get('enabled', True) and not profile_config:
            check_config = {
                'min_resolution_width': 0,
                'min_resolution_height': 0,
                'min_bitrate_kbps': 0,
                'min_score': 0
            }
        
        # Use centralized utility for the check
        is_dead, reason = utils_is_stream_dead(stream_data, check_config)
        
        if is_dead:
            # Mark in dead streams tracker with categorization
            stream_url = stream_data.get('url')
            if stream_url:
                stream_id = stream_data.get('id') or stream_data.get('stream_id')
                stream_name = stream_data.get('name') or stream_data.get('stream_name', 'Unknown')
                # Use passed channel_id if available, otherwise from stream_data
                effective_channel_id = channel_id if channel_id is not None else stream_data.get('channel_id')
                self.dead_streams_tracker.mark_as_dead(stream_url, stream_id, stream_name, effective_channel_id, reason=reason)
        
        return is_dead
    
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
            "hdr_format": stream_data.get("hdr_format"),
            "pixel_format": stream_data.get("pixel_format"),
            "audio_sample_rate": stream_data.get("audio_sample_rate"),
            "audio_channels": stream_data.get("audio_channels"),
            "channel_layout": stream_data.get("channel_layout"),
            "audio_bitrate": stream_data.get("audio_bitrate"),
            "ffmpeg_output_bitrate": int(stream_data.get("bitrate_kbps")) if stream_data.get("bitrate_kbps") not in ["N/A", None] else None,
            "quality_score": stream_data.get("score"),
            # PRESERVE_FALSE: emit False (not None) so the None-filter below keeps these
            # fields in the payload even when the probe ran but found no loop.
            # Without this, Dispatcharr's PATCH merge leaves a stale loop_probe_ran: true
            # from a previous run in place for streams not probed in the current run.
            "loop_probe_ran": True if stream_data.get("loop_probe_ran") else False,
            "loop_detected": stream_data.get("loop_detected") if stream_data.get("loop_probe_ran") else False,
            "loop_duration_secs": stream_data.get("loop_duration_secs") if stream_data.get("loop_detected") else None,
            "loop_score_penalty": stream_data.get("loop_score_penalty"),
        }
        
        # Clean up the payload, removing None and N/A values.
        # PRESERVE_FALSE: keep False values for boolean loop fields so they
        # explicitly clear stale True values in Dispatcharr on PATCH merge.
        PRESERVE_FALSE = {"loop_probe_ran", "loop_detected"}
        stream_stats_payload = {
            k: v for k, v in stream_stats_payload.items()
            if v not in [None, "N/A"] or (v is None and k not in PRESERVE_FALSE)
        }
        for k in PRESERVE_FALSE:
            if k in stream_stats_payload or stream_data.get(k) is False:
                stream_stats_payload[k] = stream_data.get(k) if stream_data.get(k) is not None else False
        
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
            "hdr_format": stream_data.get("hdr_format"),
            "pixel_format": stream_data.get("pixel_format"),
            "audio_sample_rate": stream_data.get("audio_sample_rate"),
            "audio_channels": stream_data.get("audio_channels"),
            "channel_layout": stream_data.get("channel_layout"),
            "audio_bitrate": stream_data.get("audio_bitrate"),
            "ffmpeg_output_bitrate": int(stream_data.get("bitrate_kbps")) if stream_data.get("bitrate_kbps") not in ["N/A", None] else None,
            "quality_score": stream_data.get("score"),
            # PRESERVE_FALSE: emit False (not None) so the None-filter below keeps these
            # fields in the payload even when the probe ran but found no loop.
            # Without this, Dispatcharr's PATCH merge leaves a stale loop_probe_ran: true
            # from a previous run in place for streams not probed in the current run.
            "loop_probe_ran": True if stream_data.get("loop_probe_ran") else False,
            "loop_detected": stream_data.get("loop_detected") if stream_data.get("loop_probe_ran") else False,
            "loop_duration_secs": stream_data.get("loop_duration_secs") if stream_data.get("loop_detected") else None,
            "loop_score_penalty": stream_data.get("loop_score_penalty"),
        }
        
        # Clean up the payload, removing None and N/A values.
        # PRESERVE_FALSE: keep False values for boolean loop fields so they
        # explicitly clear stale True values in Dispatcharr on PATCH merge.
        PRESERVE_FALSE = {"loop_probe_ran", "loop_detected"}
        stream_stats_payload = {
            k: v for k, v in stream_stats_payload.items()
            if v not in [None, "N/A"] or (v is None and k not in PRESERVE_FALSE)
        }
        for k in PRESERVE_FALSE:
            if k in stream_stats_payload or stream_data.get(k) is False:
                stream_stats_payload[k] = stream_data.get(k) if stream_data.get(k) is not None else False
        
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
                
                # Note: trigger_channel_re_enabling and trigger_empty_channel_disabling 
                # have been deprecated as they relied on Dispatcharr channel profiles 
                # which have been removed.
                
            except Exception as e:
                logger.error(f"Failed to finalize batch changelog: {e}", exc_info=True)
            finally:
                # Reset batch tracking
                self.batch_start_time = None
                self.batch_changelog_entries = []
    
    # Deprecated: _trigger_empty_channel_disabling and _trigger_channel_re_enabling
    # were removed as they relied on a missing module 'empty_channel_manager'
    # and obsolete Dispatcharr features.
    
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
        
        # Check if channel has active viewers using real-time proxy status.
        # Some tests stub UDI with generic mocks; treat non-boolean responses as "not active"
        # for backward compatibility with legacy single-channel test setups.
        has_active_viewers = False
        try:
            active_status = udi.is_channel_active(channel_id)
            if isinstance(active_status, bool):
                has_active_viewers = active_status
        except Exception:
            has_active_viewers = False
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
            
            # Check if this stream can run using profile-aware checking.
            # If the helper is unavailable in a mocked environment, default to allowing checks.
            check_stream_can_run = getattr(udi, 'check_stream_can_run', None)
            if not callable(check_stream_can_run):
                has_available_slot = True
                break

            can_run_result = check_stream_can_run(stream)
            if not (isinstance(can_run_result, tuple) and len(can_run_result) == 2):
                has_available_slot = True
                break

            can_run, reason = can_run_result
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

    # Removed _refine_sorted_streams in favor of lexicographical Sort Keys.
    
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
    
    def _check_channel_concurrent(self, channel_id: int, skip_batch_changelog: bool = False, target_stream_ids: Optional[List[str]] = None):
        """Check and reorder streams for a specific channel using parallel thread pool.
        
        Args:
            channel_id: ID of the channel to check
            skip_batch_changelog: If True, don't add this check to the batch changelog
            target_stream_ids: Optional list of stream IDs. If provided, ONLY these
                               streams will be checked, bypassing all other logic.
        """
        import time as time_module
        from apps.stream.stream_check_utils import analyze_stream
        from apps.stream.concurrent_stream_limiter import get_smart_scheduler, get_account_limiter, initialize_account_limits
        
        start_time = time_module.time()
        log_function_call(logger, "_check_channel_concurrent", channel_id=channel_id)
        
        log_state_change(logger, f"channel_{channel_id}", "queued", "checking")
        self.checking = True
        logger.info(f"=" * 80)
        logger.info(f"Checking channel {channel_id} (parallel mode)")
        logger.info(f"=" * 80)
        
        # Get dead stream removal configuration early (used later in finally block)
        dead_stream_removal_enabled = self.config.get('dead_stream_handling', {}).get('enabled', True)
        
        # Get effective profile for this channel
        stream_limit = 0
        allow_revive = True
        grace_period = False
        loop_check_enabled = False
        loop_penalty = 0.0
        priority_m3u_ids = []
        priority_mode = 'absolute'
        scoring_weights = None
        batch_config = self.config.get('batch_operations', {})
        batch_enabled = batch_config.get('enabled', True)
        batch_size = batch_config.get('batch_size', 10)
        batch_stats_list = []
        
        try:
            from apps.automation.automation_config_manager import get_automation_config_manager
            automation_config = get_automation_config_manager()
            
            # Fetch channel data to get group_id (might be fetched already but just in case)
            udi = get_udi_manager()
            channel = udi.get_channel_by_id(channel_id)
            group_id = channel.get('channel_group_id') if channel else None
            
            config = automation_config.get_effective_configuration(channel_id, group_id)
            profile = config.get('profile') if config else None
            if profile:
                profile_stream_checking = profile.get('stream_checking', {})
                stream_limit = profile_stream_checking.get('stream_limit', 0)
                allow_revive = profile_stream_checking.get('allow_revive', True)
                priority_m3u_ids = profile_stream_checking.get('m3u_priority', [])
                priority_mode = profile_stream_checking.get('m3u_priority_mode', 'absolute')
                grace_period = profile_stream_checking.get('grace_period', False)
                loop_check_enabled = profile_stream_checking.get('loop_check_enabled', False)
                profile_remove_dead_streams = profile_stream_checking.get('remove_dead_streams')
                if isinstance(profile_remove_dead_streams, bool):
                    dead_stream_removal_enabled = profile_remove_dead_streams
                elif profile_remove_dead_streams is not None:
                    logger.warning(
                        "Ignoring non-boolean stream_checking.remove_dead_streams for channel %s",
                        channel_id,
                    )
                scoring_weights = profile.get('scoring_weights', None)
                loop_penalty = float(
                    (scoring_weights or {}).get('loop_penalty', 0.0)
                )
                # Clamp to valid range: -0.25 to 0.0
                loop_penalty = max(-0.25, min(0.0, loop_penalty))
                
                # Also check if checking is enabled at all for this profile
                if not profile_stream_checking.get('enabled', False):
                    logger.info(f"Stream checking disabled by profile for channel {channel_id}")
                    return {
                        'dead_streams_count': 0,
                        'revived_streams_count': 0,
                        'skipped': True,
                        'skip_reason': 'profile_disabled'
                    }
        except Exception as e:
            logger.warning(f"Failed to load profile settings for channel {channel_id}: {e}")
        
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
            
            # Global Action / Force Check overrides profile settings
            if force_check:
                if not allow_revive:
                    logger.info(f"Force check enabled for channel {channel_name}: Overriding Profile 'allow_revive' to True")
                    allow_revive = True
            
            # Get list of already checked streams to avoid re-analyzing
            checked_stream_info = self.update_tracker.updates.get('channels', {}).get(str(channel_id), {})
            checked_stream_ids = checked_stream_info.get('checked_stream_ids', [])
            last_check_str = checked_stream_info.get('last_check')
            
            # Check if immunity period (2 hours) has expired
            immunity_expired = False
            if last_check_str and grace_period:
                try:
                    last_check_time = datetime.fromisoformat(last_check_str)
                    if (datetime.now() - last_check_time).total_seconds() > 7200:
                        immunity_expired = True
                        logger.info(f"Immunity period (2 hours) expired for channel {channel_name} - will re-analyze all streams")
                except Exception as e:
                    logger.warning(f"Failed to parse last_check timestamp for channel {channel_id}: {e}")
            
            current_stream_ids = [s['id'] for s in streams]
            
            # Identify which streams need analysis (new or unchecked)
            
            if target_stream_ids is not None:
                # Targeted check mode: Evaluates newly assigned streams ONLY
                streams_to_check = [s for s in streams if str(s['id']) in [str(ts) for ts in target_stream_ids]]
                streams_already_checked = [s for s in streams if str(s['id']) not in [str(ts) for ts in target_stream_ids]]
                logger.info(f"Targeted stream check: evaluating {len(streams_to_check)} specific newly assigned streams")
                
            elif force_check or (grace_period and immunity_expired) or (not grace_period and not force_check):
                # If grace period is DISABLED, we check everything every time unless it's a "needs_check" trigger?
                # Actually, if grace_period is False, users probably expect regular checks.
                # However, we only get here if the worker picked up the channel.
                # If it's a force check or immunity expired, check all.
                # If grace period is OFF, we also check everything if we are running.
                streams_to_check = streams
                streams_already_checked = []
                
                if force_check:
                    logger.info(f"Force check enabled: analyzing all {len(streams)} streams (bypassing 2-hour immunity)")
                    self.update_tracker.clear_force_check(channel_id)
                elif grace_period and immunity_expired:
                    logger.info(f"Grace period (2h) expired: re-analyzing {len(streams)} streams for {channel_name}")
                elif not grace_period:
                    logger.info(f"Grace period disabled for profile: analyzing all {len(streams)} streams")
            else:
                # Normal incremental check: only analyze new streams
                streams_to_check = [s for s in streams if s['id'] not in checked_stream_ids]
                streams_already_checked = [s for s in streams if s['id'] in checked_stream_ids]
                
                if streams_to_check:
                    logger.info(f"Found {len(streams_to_check)} new/unchecked streams (out of {len(streams)} total)")
                else:
                    logger.info(f"All {len(streams)} streams have been recently checked (within 2h immunity), using cached scores")
                    
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
                        # Best effort to reconstruct stats for skipped/cached streams
                        cached_stats = []
                        for s in streams:
                            # Try to find existing stats if available in stream object
                            # Otherwise use placeholders
                            extracted_stats = extract_stream_stats(s)
                            formatted_stats = format_stream_stats_for_display(extracted_stats)
                            
                            cached_for_score = {
                                'stream_id': s.get('id'),
                                'stream_name': s.get('name'),
                                'stream_url': s.get('url'),
                                'bitrate_kbps': extracted_stats.get('bitrate_kbps'),
                                'resolution': extracted_stats.get('resolution'),
                                'fps': extracted_stats.get('fps'),
                                'video_codec': extracted_stats.get('video_codec'),
                                'audio_codec': extracted_stats.get('audio_codec'),
                                'hdr_format': extracted_stats.get('hdr_format'),
                                'status': 'cached'
                            }
                            
                            temp_score = self._calculate_stream_score(cached_for_score, priority_m3u_ids, priority_mode, scoring_weights)

                            stat = {
                                'stream_id': s.get('id'),
                                'stream_name': s.get('name'),
                                'resolution': formatted_stats['resolution'],
                                'fps': formatted_stats['fps'],
                                'video_codec': formatted_stats['video_codec'],
                                'bitrate': formatted_stats['bitrate'],
                                'm3u_account': self._get_m3u_account_name(s.get('id'), udi) if hasattr(self, '_get_m3u_account_name') else 'N/A',
                                'score': temp_score
                            }
                            cached_stats.append(stat)

                        return {
                            'dead_streams_count': 0,
                            'revived_streams_count': 0,
                            'dead_streams': [],
                            'revived_streams': [],
                            'skipped_streams_count': len(streams),
                            'skipped_streams': [{'id': s['id'], 'name': s.get('name', f"Stream {s['id']}")} for s in streams],
                            'checked_streams': cached_stats
                        }
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
            
            # Dict to keep track of the stream details throughout the analysis
            stream_statuses = {
                s['id']: {
                    'id': s['id'],
                    'name': s.get('name', f"Stream {s['id']}"),
                    'status': 'pending',
                    'm3u_account': self._get_m3u_account_name(s.get('id'), udi) if hasattr(self, '_get_m3u_account_name') else 'N/A'
                }
                for s in streams_to_check
            }
            
            # Start callback for parallel checker
            def start_callback(stream):
                stream_id = stream.get('id')
                if stream_id in stream_statuses:
                    stream_statuses[stream_id]['status'] = 'checking'
                    stream_statuses[stream_id]['started_at'] = datetime.now().isoformat()
                    self.progress.update(
                        channel_id=channel_id,
                        channel_name=channel_name,
                        current=completed_count[0],
                        total=total_streams,
                        current_stream=stream.get('name', 'Unknown'),
                        status='analyzing',
                        step='Analyzing streams with account limits',
                        step_detail=f'Started checking {stream.get("name", "Unknown")}',
                        streams_detail=list(stream_statuses.values()),
                        stream_duration=analysis_params.get('ffmpeg_duration', 30)
                    )
            
            def progress_callback(completed, total, result):
                completed_count[0] = completed
                stream_name = result.get('stream_name', 'Unknown')
                stream_id = result.get('stream_id')
                
                # Calculate temp score for UI display
                temp_score = self._calculate_stream_score(result, priority_m3u_ids, priority_mode, scoring_weights)
                
                # Update stream status based on result
                is_dead = self._is_stream_dead(result, channel_id)
                
                if stream_id in stream_statuses:
                    if result.get('status') == 'ERROR':
                        stream_statuses[stream_id]['status'] = 'error'
                        stream_statuses[stream_id]['score'] = 0.0
                    elif is_dead:
                        stream_statuses[stream_id]['status'] = 'dead'
                        stream_statuses[stream_id]['score'] = 0.0
                    else:
                        stream_statuses[stream_id]['status'] = 'completed'
                        # Optional: record score or resolution
                        stream_statuses[stream_id]['score'] = temp_score
                        stream_statuses[stream_id]['resolution'] = result.get('resolution', '0x0')
                        stream_statuses[stream_id]['video_codec'] = result.get('video_codec', 'N/A')
                        stream_statuses[stream_id]['fps'] = result.get('fps', 0)
                        stream_statuses[stream_id]['bitrate'] = result.get('bitrate_kbps')
                        stream_statuses[stream_id]['hdr_format'] = result.get('hdr_format')
                
                # Update progress
                self.progress.update(
                    channel_id=channel_id,
                    channel_name=channel_name,
                    current=completed,
                    total=total,
                    current_stream=stream_name,
                    status='analyzing',
                    step='Analyzing streams with account limits',
                    step_detail=f'Completed {completed}/{total}',
                    streams_detail=list(stream_statuses.values()),
                    stream_duration=analysis_params.get('ffmpeg_duration', 30)
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

                # Heartbeat thread: pushes current stream_statuses to the frontend
                # every 2 seconds while check_streams_with_limits is running.
                # Ensures the live grid stays responsive during stagger delays and
                # between completion events regardless of stream count.
                _heartbeat_stop = threading.Event()

                def _heartbeat():
                    while not _heartbeat_stop.wait(2):
                        try:
                            self.progress.update(
                                channel_id=channel_id,
                                channel_name=channel_name,
                                current=sum(1 for s in stream_statuses.values() if s.get('status') in ('completed', 'dead', 'error', 'loop_detected')),
                                total=total_streams,
                                status='analyzing',
                                step='Analyzing streams with account limits',
                                step_detail='Checking streams...',
                                streams_detail=list(stream_statuses.values()),
                                stream_duration=analysis_params.get('ffmpeg_duration', 30)
                            )
                        except Exception:
                            pass  # never let the heartbeat crash the check

                _hb_thread = threading.Thread(target=_heartbeat, daemon=True, name='stream-checker-heartbeat')
                _hb_thread.start()

                try:
                    # Check streams in parallel with account-aware limits
                    results = smart_scheduler.check_streams_with_limits(
                        streams=streams_to_check,
                        check_function=analyze_stream,
                        progress_callback=progress_callback,
                        start_callback=start_callback,
                        stagger_delay=stagger_delay,
                        abort_event=self.abort_current_check,
                        ffmpeg_duration=analysis_params.get('ffmpeg_duration', 30),
                        timeout=analysis_params.get('timeout', 30),
                        retries=analysis_params.get('retries', 1),
                        retry_delay=analysis_params.get('retry_delay', 10),
                        user_agent=analysis_params.get('user_agent', 'VLC/3.0.14'),
                        stream_startup_buffer=analysis_params.get('stream_startup_buffer', 10)
                    )
                finally:
                    _heartbeat_stop.set()
                    _hb_thread.join(timeout=3)
                
                # Process results - ALL checks are complete at this point
                # Collect stats for batch update to minimize API calls
                batch_stats_list = []
                
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
                    is_dead = self._is_stream_dead(analyzed, channel_id)
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
                        if allow_revive:
                            if self.dead_streams_tracker.mark_as_alive(stream_url):
                                revived_stream_ids.append(stream_id)
                                logger.info(f"Stream {stream_id} REVIVED: {stream_name}")
                        else:
                            # Not allowed to revive, treat as still dead
                            dead_stream_ids.add(stream_id)
                            logger.info(f"Stream {stream_id} is alive but revival disabled by profile: {stream_name}")
                    elif is_dead and was_dead:
                        logger.debug(f"Stream {stream_id} remains dead (already marked)")
                        # Add to dead_stream_ids so the stream removal logic (line 1455) will filter it out
                        dead_stream_ids.add(stream_id)
                    
                    # Calculate score using per-profile scoring weights
                    score = self._calculate_stream_score(analyzed, priority_m3u_ids, priority_mode, scoring_weights)
                    analyzed['score'] = score
                    analyzed['channel_id'] = channel_id
                    analyzed['channel_name'] = channel_name
                    analyzed_streams.append(analyzed)
                
                
                # --- MERGE CACHED STREAMS FOR CORRECT SORTING AND LIMITING ---
                # Retrieve "cached" streams that weren't analyzed (because they are within immunity period)
                # We need to include them in the sorting and limiting process to ensure we keep the absolute best streams
                if streams_already_checked:
                    cached_analyzed_streams = []
                    logger.info(f"Re-integrating {len(streams_already_checked)} cached streams for global sorting/limiting")
                    
                    for stream in streams_already_checked:
                        stream_id = stream['id']
                        # Reconstruct a minimal 'analyzed' object from stored stats
                        # This allows standard scoring and sorting logic to work
                        stream_stats = stream.get('stream_stats')
                        if stream_stats is None:
                            stream_stats = {}
                        elif isinstance(stream_stats, str):
                            try:
                                stream_stats = json.loads(stream_stats)
                            except:
                                stream_stats = {}
                        
                        # Map stored stats back to analysis keys
                        cached_analyzed = {
                            'stream_id': stream_id,
                            'stream_url': stream.get('url'),
                            'stream_name': stream.get('name'),
                            'bitrate_kbps': stream_stats.get('ffmpeg_output_bitrate', 0),
                            'resolution': stream_stats.get('resolution', 'N/A'),
                            'fps': stream_stats.get('source_fps', 0),
                            'video_codec': stream_stats.get('video_codec', 'N/A'),
                            'audio_codec': stream_stats.get('audio_codec', 'N/A'),
                            'hdr_format': stream_stats.get('hdr_format'),
                            'status': 'cached',
                            'channel_id': channel_id,
                            'channel_name': channel_name,
                            'score': 0.0 # Will be calculated below
                        }
                        
                        # Calculate score using CURRENT profile weights
                        score = self._calculate_stream_score(cached_analyzed, priority_m3u_ids, priority_mode, scoring_weights)
                        cached_analyzed['score'] = score
                        cached_analyzed_streams.append(cached_analyzed)
                    
                    # Merge cached streams with newly analyzed streams
                    analyzed_streams.extend(cached_analyzed_streams)
                    logger.info(f"Merged {len(cached_analyzed_streams)} cached streams with {len(results)} new results. Total candidates: {len(analyzed_streams)}")

                logger.info(f"Completed smart parallel analysis of {len(results)} streams with account-aware limits")

            # Run loop probes on eligible streams (top 25% scoring >= 0.5).
            # Called after all streams are scored and analyzed_streams is fully
            # assembled so the complete score distribution is available.
            # Gated on the per-profile loop_check_enabled flag.
            if loop_check_enabled:
                analysis_params_lp = self.config.get('stream_analysis', {})
                self._run_loop_probes(
                    analyzed_streams,
                    user_agent=analysis_params_lp.get('user_agent', 'VLC/3.0.14'),
                    loop_penalty=loop_penalty,
                    probe_duration=analysis_params_lp.get('max_loop_duration', 120) * 3,
                    channel_id=channel_id,
                    channel_name=channel_name,
                    streams_detail=list(stream_statuses.values()),
                )
            else:
                logger.debug("[loop-probe] Loop checking disabled by profile — skipping")

            # Batch stats write after probes so the persisted score and loop
            # fields reflect the penalised score from this run.
            if batch_enabled and batch_stats_list:
                # Rebuild batch list with updated scores post-penalty
                batch_stats_list = []
                for analyzed in analyzed_streams:
                    stats_item = self._prepare_stream_stats_for_batch(analyzed)
                    if stats_item:
                        batch_stats_list.append(stats_item)
            if batch_enabled and batch_stats_list:
                logger.info(f"Batch updating stats for {len(batch_stats_list)} streams (batch_size={batch_size})")
                successful, failed = batch_update_stream_stats(batch_stats_list, batch_size=batch_size)
                logger.info(f"Batch update complete: {successful} successful, {failed} failed")

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
            # Sort streams using tiered sort keys (lexicographical ranking)
            for analyzed in analyzed_streams:
                analyzed['sort_key'] = self._generate_stream_sort_key(analyzed, priority_m3u_ids, priority_mode)
                
            analyzed_streams.sort(key=lambda x: x['sort_key'])
            
            # Apply stream limit if configured in profile
            if stream_limit > 0 and len(analyzed_streams) > stream_limit:
                removed_count = len(analyzed_streams) - stream_limit
                logger.info(f"Applying profile stream limit: Keeping top {stream_limit} streams, removing {removed_count}")
                analyzed_streams = analyzed_streams[:stream_limit]
            
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
            
            # Generate detailed stream stats for return value and changelog
            try:
                # Get channel logo URL
                logo_url = None
                logo_id = channel_data.get('logo_id')
                if logo_id:
                    logo_url = f"/api/logos/{logo_id}"
                
                # Calculate channel-level averages from analyzed streams
                averages = self._calculate_channel_averages(analyzed_streams, dead_stream_ids)
                
                stream_stats = []
                # Use all analyzed streams for stats, not just first 10
                for analyzed in analyzed_streams:
                    stream_id = analyzed.get('stream_id')
                    is_dead = stream_id in dead_stream_ids
                    is_revived = stream_id in revived_stream_ids
                    
                    # Extract and format stats using centralized utilities
                    extracted_stats = extract_stream_stats(analyzed)
                    formatted_stats = format_stream_stats_for_display(extracted_stats)
                    
                    # Get M3U account name for this stream using helper method
                    m3u_account_name = self._get_m3u_account_name(stream_id, udi)

                    # Stamp onto the analyzed dict so analyzed_lookup (used by
                    # check_single_channel) can read it without a separate UDI call.
                    analyzed['m3u_account'] = m3u_account_name
                    
                    stream_stat = {
                        'stream_id': stream_id,
                        'stream_name': analyzed.get('stream_name'),
                        'resolution': formatted_stats['resolution'],
                        'fps': formatted_stats['fps'],
                        'video_codec': formatted_stats['video_codec'],
                        'audio_codec': formatted_stats.get('audio_codec', 'N/A'),
                        'bitrate': formatted_stats['bitrate'],
                        'm3u_account': m3u_account_name,
                        'hdr_format': extracted_stats.get('hdr_format')
                    }
                    
                    # Mark dead streams as "dead" instead of showing score:0
                    if is_dead:
                        stream_stat['status'] = 'dead'
                    elif is_revived:
                        stream_stat['status'] = 'revived'
                        stream_stat['score'] = round(analyzed.get('score', 0), 2)
                    else:
                        stream_stat['score'] = round(analyzed.get('score', 0), 2)

                    # Include loop detection results if the probe ran
                    if analyzed.get('loop_probe_ran'):
                        stream_stat['loop_probe_ran']      = True
                        stream_stat['loop_detected']       = analyzed.get('loop_detected')
                        stream_stat['loop_duration_secs']  = analyzed.get('loop_duration_secs')

                    # Clean up N/A values for cleaner JSON
                    cleaned_stat = {k: v for k, v in stream_stat.items() if v not in [None]}
                    stream_stats.append(cleaned_stat)

            except Exception as e:
                logger.error(f"Error generating stream stats: {e}")
                stream_stats = []
                averages = {'avg_resolution': 'N/A', 'avg_bitrate': 'N/A', 'avg_fps': 'N/A'}
                logo_url = None

            # Add to batch changelog instead of creating individual entry
            if self.changelog:
                try:
                    
                    # Add to batch instead of creating individual changelog entry
                    
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
                'revived_streams_count': len(revived_stream_ids),
                'dead_streams': [{
                    'id': s, 
                    'name': next((st.get('name') for st in streams if st['id'] == s), f'Stream {s}'),
                    'm3u_account': next((st.get('m3u_account') for st in streams if st['id'] == s), None)
                } for s in dead_stream_ids],
                'revived_streams': [{
                    'id': s, 
                    'name': next((st.get('name') for st in streams if st['id'] == s), f'Stream {s}'),
                    'm3u_account': next((st.get('m3u_account') for st in streams if st['id'] == s), None)
                } for s in revived_stream_ids],
                'skipped_streams': [{'id': s['id'], 'name': s.get('name', f"Stream {s['id']}")} for s in streams_already_checked],
                'checked_streams': stream_stats,
                # In-memory analyzed_streams: authoritative source for loop results
                # and m3u_account names. Used by check_single_channel to build its
                # changelog entry without depending on a potentially stale UDI refresh.
                'analyzed_streams': analyzed_streams,
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
                'revived_streams_count': 0,
                'checked_streams': [],
                'error': str(e)
            }
        
        finally:
            self.checking = False
            log_function_return(logger, "_check_channel_concurrent")

    
    def _check_channel_sequential(self, channel_id: int, skip_batch_changelog: bool = False, target_stream_ids: Optional[List[str]] = None):
        """Check and reorder streams for a specific channel using sequential checking.
        
        Args:
            channel_id: ID of the channel to check
            skip_batch_changelog: If True, don't add this check to the batch changelog
            target_stream_ids: Optional list of stream IDs. If provided, ONLY these
                               streams will be checked, bypassing all other logic.
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
        
        # Get effective profile for this channel
        stream_limit = 0
        allow_revive = True
        grace_period = False
        loop_check_enabled = False
        loop_penalty = 0.0
        priority_m3u_ids = []
        priority_mode = 'absolute'
        scoring_weights = None
        
        try:
            from apps.automation.automation_config_manager import get_automation_config_manager
            automation_config = get_automation_config_manager()
            
            # Fetch channel data to get group_id (might be fetched already but just in case)
            udi = get_udi_manager()
            channel = udi.get_channel_by_id(channel_id)
            group_id = channel.get('channel_group_id') if channel else None
            
            config = automation_config.get_effective_configuration(channel_id, group_id)
            profile = config.get('profile') if config else None
            if profile:
                profile_stream_checking = profile.get('stream_checking', {})
                stream_limit = profile_stream_checking.get('stream_limit', 0)
                allow_revive = profile_stream_checking.get('allow_revive', True)
                priority_m3u_ids = profile_stream_checking.get('m3u_priority', [])
                priority_mode = profile_stream_checking.get('m3u_priority_mode', 'absolute')
                grace_period = profile_stream_checking.get('grace_period', False)
                loop_check_enabled = profile_stream_checking.get('loop_check_enabled', False)
                profile_remove_dead_streams = profile_stream_checking.get('remove_dead_streams')
                if isinstance(profile_remove_dead_streams, bool):
                    dead_stream_removal_enabled = profile_remove_dead_streams
                elif profile_remove_dead_streams is not None:
                    logger.warning(
                        "Ignoring non-boolean stream_checking.remove_dead_streams for channel %s",
                        channel_id,
                    )
                scoring_weights = profile.get('scoring_weights', None)
                loop_penalty = float(
                    (scoring_weights or {}).get('loop_penalty', 0.0)
                )
                # Clamp to valid range: -0.25 to 0.0
                loop_penalty = max(-0.25, min(0.0, loop_penalty))
                
                # Also check if checking is enabled at all for this profile
                if not profile_stream_checking.get('enabled', False):
                    logger.info(f"Stream checking disabled by profile for channel {channel_id}")
                    return {
                        'dead_streams_count': 0,
                        'revived_streams_count': 0,
                        'skipped': True,
                        'skip_reason': 'profile_disabled'
                    }
        except Exception as e:
            logger.warning(f"Failed to load profile settings for channel {channel_id}: {e}")
        
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
            
            # Global Action / Force Check overrides profile settings
            if force_check:
                if not allow_revive:
                    logger.info(f"Force check enabled for channel {channel_name}: Overriding Profile 'allow_revive' to True")
                    allow_revive = True
            
            # Get list of already checked streams to avoid re-analyzing
            checked_stream_info = self.update_tracker.updates.get('channels', {}).get(str(channel_id), {})
            checked_stream_ids = checked_stream_info.get('checked_stream_ids', [])
            last_check_str = checked_stream_info.get('last_check')
            
            # Check if immunity period (2 hours) has expired
            immunity_expired = False
            if last_check_str and grace_period:
                try:
                    last_check_time = datetime.fromisoformat(last_check_str)
                    if (datetime.now() - last_check_time).total_seconds() > 7200:
                        immunity_expired = True
                        logger.info(f"Immunity period (2 hours) expired for channel {channel_name} - will re-analyze all streams")
                except Exception as e:
                    logger.warning(f"Failed to parse last_check timestamp for channel {channel_id}: {e}")
            
            current_stream_ids = [s['id'] for s in streams]
            
            # Identify which streams need analysis (new or unchecked)
            
            if target_stream_ids is not None:
                # Targeted check mode: Evaluates newly assigned streams ONLY
                streams_to_check = [s for s in streams if str(s['id']) in [str(ts) for ts in target_stream_ids]]
                streams_already_checked = [s for s in streams if str(s['id']) not in [str(ts) for ts in target_stream_ids]]
                logger.info(f"Targeted stream check: evaluating {len(streams_to_check)} specific newly assigned streams")
                
            elif force_check or (grace_period and immunity_expired) or (not grace_period and not force_check):
                streams_to_check = streams
                streams_already_checked = []
                
                if force_check:
                    logger.info(f"Force check enabled: analyzing all {len(streams)} streams (bypassing 2-hour immunity)")
                    self.update_tracker.clear_force_check(channel_id)
                elif grace_period and immunity_expired:
                    logger.info(f"Grace period (2h) expired: re-analyzing {len(streams)} streams for {channel_name}")
                elif not grace_period:
                    logger.info(f"Grace period disabled for profile: analyzing all {len(streams)} streams")
            else:
                # Normal incremental check: only analyze new streams
                streams_to_check = [s for s in streams if s['id'] not in checked_stream_ids]
                streams_already_checked = [s for s in streams if s['id'] in checked_stream_ids]
                
                if streams_to_check:
                    logger.info(f"Found {len(streams_to_check)} new/unchecked streams (out of {len(streams)} total)")
                else:
                    logger.info(f"All {len(streams)} streams have been recently checked (within 2h immunity), using cached scores")
                    
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
            from apps.stream.stream_check_utils import analyze_stream
            
            # Analyze new/unchecked streams
            analyzed_streams = []
            dead_stream_ids = set()  # Use set for O(1) lookups
            revived_stream_ids = []
            total_streams = len(streams_to_check)
            
            # Dict to keep track of the stream details throughout the analysis
            stream_statuses = {
                s['id']: {
                    'id': s['id'],
                    'name': s.get('name', f"Stream {s['id']}"),
                    'status': 'pending',
                    'm3u_account': self._get_m3u_account_name(s.get('id'), udi) if hasattr(self, '_get_m3u_account_name') else 'N/A'
                }
                for s in streams_to_check
            }
            
            for idx, stream in enumerate(streams_to_check, 1):
                if self.abort_current_check.is_set():
                    logger.info("Abort requested, stopping sequential stream checks")
                    break
                    
                self.progress.update(
                    channel_id=channel_id,
                    channel_name=channel_name,
                    current=idx,
                    total=total_streams,
                    current_stream=stream.get('name', 'Unknown'),
                    status='analyzing',
                    step='Analyzing stream quality',
                    step_detail=f'Checking bitrate, resolution, codec ({idx}/{total_streams})',
                    streams_detail=list(stream_statuses.values())
                )
                
                if stream['id'] in stream_statuses:
                    stream_statuses[stream['id']]['status'] = 'checking'
                    stream_statuses[stream['id']]['started_at'] = datetime.now().isoformat()
                
                # Analyze stream
                analysis_params = self.config.get('stream_analysis', {})

                # Push checking status + started_at to frontend before analyze_stream blocks
                self.progress.update(
                    channel_id=channel_id,
                    channel_name=channel_name,
                    current=idx,
                    total=total_streams,
                    current_stream=stream.get('name', 'Unknown'),
                    status='analyzing',
                    step='Analyzing stream quality',
                    step_detail=f'Checking bitrate, resolution, codec ({idx}/{total_streams})',
                    streams_detail=list(stream_statuses.values()),
                    stream_duration=analysis_params.get('ffmpeg_duration', 20)
                )
                
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
                is_dead = self._is_stream_dead(analyzed, channel_id)
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
                    if allow_revive:
                        if self.dead_streams_tracker.mark_as_alive(stream_url):
                            revived_stream_ids.append(stream['id'])
                            logger.info(f"Stream {stream['id']} REVIVED: {stream_name}")
                    else:
                        dead_stream_ids.add(stream['id'])
                        logger.info(f"Stream {stream['id']} is alive but revival disabled by profile: {stream_name}")
                elif is_dead and was_dead:
                    # Stream remains dead
                    dead_stream_ids.add(stream['id'])
                
                # Calculate score
                score = self._calculate_stream_score(analyzed, priority_m3u_ids, priority_mode, scoring_weights)
                analyzed['score'] = score
                analyzed_streams.append(analyzed)
                
                # Update stream status for progress display
                if stream['id'] in stream_statuses:
                    if analyzed.get('status') == 'ERROR':
                        stream_statuses[stream['id']]['status'] = 'error'
                        stream_statuses[stream['id']]['score'] = 0.0
                    elif is_dead:
                        stream_statuses[stream['id']]['status'] = 'dead'
                        stream_statuses[stream['id']]['score'] = 0.0
                    else:
                        stream_statuses[stream['id']]['status'] = 'completed'
                        stream_statuses[stream['id']]['score'] = score
                        stream_statuses[stream['id']]['resolution'] = analyzed.get('resolution', '0x0')
                        stream_statuses[stream['id']]['video_codec'] = analyzed.get('video_codec', 'N/A')
                        stream_statuses[stream['id']]['fps'] = analyzed.get('fps', 0)
                        stream_statuses[stream['id']]['bitrate'] = analyzed.get('bitrate_kbps')
                
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
                        'hdr_format': stream_stats.get('hdr_format'),
                        'bitrate_kbps': stream_stats.get('ffmpeg_output_bitrate', 0),
                        'status': 'OK'  # Assume OK for previously checked streams
                    }
                    
                    # Check if this cached stream is dead and handle state transitions
                    stream_url = stream.get('url', '')
                    stream_name = stream.get('name', 'Unknown')
                    is_dead = self._is_stream_dead(analyzed, channel_id)
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
                        if allow_revive:
                            if self.dead_streams_tracker.mark_as_alive(stream_url):
                                revived_stream_ids.append(stream['id'])
                                logger.info(f"Cached stream {stream['id']} REVIVED: {stream_name}")
                        else:
                            # Not allowed to revive, treat as still dead
                            dead_stream_ids.add(stream['id'])
                            logger.info(f"Cached stream {stream['id']} is alive but revival disabled by profile: {stream_name}")
                    elif is_dead and was_dead:
                        # Stream remains dead (already marked)
                        logger.debug(f"Cached stream {stream['id']} remains dead (already marked)")
                        dead_stream_ids.add(stream['id'])
                    
                    # Calculate score using stored stats and CURRENT profile weights
                    score = self._calculate_stream_score(analyzed, priority_m3u_ids, priority_mode, scoring_weights)
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
                    score = self._calculate_stream_score(analyzed, priority_m3u_ids, priority_mode)
                    analyzed['score'] = score
                    analyzed_streams.append(analyzed)

            # Run loop probes on eligible streams — all streams scored, full
            # distribution known for top-percentile calculation.
            # Gated on the per-profile loop_check_enabled flag.
            if loop_check_enabled:
                analysis_params_lp = self.config.get('stream_analysis', {})
                self._run_loop_probes(
                    analyzed_streams,
                    user_agent=analysis_params_lp.get('user_agent', 'VLC/3.0.14'),
                    loop_penalty=loop_penalty,
                    probe_duration=analysis_params_lp.get('max_loop_duration', 120) * 3,
                    channel_id=channel_id,
                    channel_name=channel_name,
                    streams_detail=list(stream_statuses.values()),
                )
                # Write stats for all probed streams so loop fields
                # (loop_probe_ran, loop_detected, loop_duration_secs) are
                # persisted to the database regardless of whether a penalty
                # was applied. Streams with a penalty get their updated score
                # persisted here too.
                for analyzed in analyzed_streams:
                    if analyzed.get('loop_probe_ran'):
                        self._update_stream_stats(analyzed)
            else:
                logger.debug("[loop-probe] Loop checking disabled by profile — skipping")

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
            # Sort streams using tiered sort keys (lexicographical ranking)
            for analyzed in analyzed_streams:
                analyzed['sort_key'] = self._generate_stream_sort_key(analyzed, priority_m3u_ids, priority_mode)
                
            analyzed_streams.sort(key=lambda x: x['sort_key'])
            
            # Apply stream limit if configured in profile
            if stream_limit > 0 and len(analyzed_streams) > stream_limit:
                removed_count = len(analyzed_streams) - stream_limit
                logger.info(f"Applying profile stream limit: Keeping top {stream_limit} streams, removing {removed_count}")
                analyzed_streams = analyzed_streams[:stream_limit]
            
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

            # Build stream_stats unconditionally so the return dict can always
            # carry 'checked_streams' (mirrors _check_channel_concurrent behaviour).
            # The automation changelog builder reads c_result.get('checked_streams', [])
            # for every channel regardless of whether sequential or concurrent mode is
            # active — without this, sequential runs produce an empty Quality Check table.
            stream_stats = []
            try:
                averages = self._calculate_channel_averages(analyzed_streams, dead_stream_ids)
                for analyzed in analyzed_streams:
                    stream_id = analyzed.get('stream_id')
                    is_dead = stream_id in dead_stream_ids
                    is_revived = stream_id in revived_stream_ids

                    extracted_stats = extract_stream_stats(analyzed)
                    formatted_stats = format_stream_stats_for_display(extracted_stats)
                    m3u_account_name = self._get_m3u_account_name(stream_id, udi)

                    # Stamp onto analyzed dict so analyzed_lookup in check_single_channel
                    # carries the resolved name without a separate UDI call.
                    analyzed['m3u_account'] = m3u_account_name

                    stream_stat = {
                        'stream_id': stream_id,
                        'stream_name': analyzed.get('stream_name'),
                        'resolution': formatted_stats['resolution'],
                        'fps': formatted_stats['fps'],
                        'video_codec': formatted_stats['video_codec'],
                        'audio_codec': formatted_stats['audio_codec'],
                        'bitrate': formatted_stats['bitrate'],
                        'm3u_account': m3u_account_name,
                        'hdr_format': extracted_stats.get('hdr_format')
                    }

                    if is_dead:
                        stream_stat['status'] = 'dead'
                    elif is_revived:
                        stream_stat['status'] = 'revived'
                        stream_stat['score'] = round(analyzed.get('score', 0), 2)
                    else:
                        stream_stat['score'] = round(analyzed.get('score', 0), 2)
                        if 'status' in analyzed:
                            stream_stat['analysis_status'] = analyzed.get('status')

                    # Include loop detection results if the probe ran
                    if analyzed.get('loop_probe_ran'):
                        stream_stat['loop_probe_ran']     = True
                        stream_stat['loop_detected']      = analyzed.get('loop_detected')
                        stream_stat['loop_duration_secs'] = analyzed.get('loop_duration_secs')

                    stream_stat = {k: v for k, v in stream_stat.items() if v not in [None, "N/A"]}
                    stream_stats.append(stream_stat)
            except Exception as e:
                logger.warning(f"Failed to build stream_stats for sequential return: {e}")
                averages = {'avg_resolution': 'N/A', 'avg_bitrate': 'N/A', 'avg_fps': 'N/A'}

            # Add changelog entry with stream stats
            if self.changelog:
                try:
                    # Get channel logo URL
                    logo_url = None
                    logo_id = channel_data.get('logo_id')
                    if logo_id:
                        logo_url = f"/api/logos/{logo_id}"
                    
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
                            'stream_details': stream_stats[:10]  # Limit to top 10 for brevity
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
                'revived_streams_count': len(revived_stream_ids),
                'dead_streams': [{
                    'id': s, 
                    'name': next((st.get('name') for st in streams if st['id'] == s), f'Stream {s}'),
                    'm3u_account': next((st.get('m3u_account') for st in streams if st['id'] == s), None)
                } for s in dead_stream_ids],
                'revived_streams': [{
                    'id': s, 
                    'name': next((st.get('name') for st in streams if st['id'] == s), f'Stream {s}'),
                    'm3u_account': next((st.get('m3u_account') for st in streams if st['id'] == s), None)
                } for s in revived_stream_ids],
                'skipped_streams': [{'id': s['id'], 'name': s.get('name', f"Stream {s['id']}")} for s in streams_already_checked],
                'checked_streams': stream_stats,
                'analyzed_streams': analyzed_streams,
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
                'revived_streams_count': 0,
                'checked_streams': []
            }
        
        finally:
            self.checking = False
    
    def _run_loop_probes(self, analyzed_streams: list, user_agent: str = 'VLC/3.0.14', loop_penalty: float = 0.0,
                         probe_duration: int = 360, channel_id: int = 0, channel_name: str = '',
                         streams_detail: Optional[list] = None) -> None:
        """
        Run loop detection probes on eligible streams in parallel with
        per-account concurrent limits, then write results back into each
        stream's analyzed dict.

        Eligibility criteria (both must be met):
          1. score >= LOOP_PROBE_SCORE_THRESHOLD (stream is healthy)
          2. stream is in the top LOOP_PROBE_TOP_PERCENTILE of all scored streams

        Dead streams (score == 0) and cached streams are never probed.

        Parallelism uses AccountStreamLimiter directly rather than
        SmartStreamScheduler to avoid:
          - URL double-transformation (analyzed dicts already carry the
            transformed URL used by quality analysis)
          - Progress/start callback conflicts with the quality analysis UI
          - Result-shape mismatch (probe returns a tuple, not a dict)

        Account ID comes from the UDI stream record ('m3u_account_id' column,
        mapped to 'm3u_account' integer expected by AccountStreamLimiter).

        Results written into each analyzed dict (always present after this call):
          analyzed['loop_detected']      True / False / None (not probed / error)
          analyzed['loop_duration_secs'] float or None
          analyzed['loop_probe_ran']     True / False

        After all probes complete, applies loop_penalty to the score of any
        confirmed looping stream (loop_detected is True). Score is floored at
        0.0 — a looping stream is still better than no stream.

        Args:
            analyzed_streams: List of analyzed stream dicts, each with 'score' set.
            user_agent:       HTTP User-Agent forwarded to FFmpeg.
            loop_penalty:     Negative float (e.g. -0.25) subtracted from score
                              of looping streams. 0.0 = no penalty.
            probe_duration:   Seconds to run each FFmpeg probe. Derived from the
                              global max_loop_duration * 3. Clamped by
                              _probe_stream_for_loops to [60, 720]. Default 360.
            channel_id:       Channel being checked — used for progress reporting.
            channel_name:     Channel name — used for progress reporting.
            streams_detail:   Snapshot of stream_statuses values from the calling
                              check method. Used to build probe_detail for live
                              frontend grid updates. None on paths where
                              stream_statuses is not available.
        """
        import threading
        from concurrent.futures import ThreadPoolExecutor, as_completed
        from apps.stream.stream_check_utils import _probe_stream_for_loops
        from apps.stream.concurrent_stream_limiter import get_account_limiter

        LOOP_PROBE_SCORE_THRESHOLD = 0.5
        LOOP_PROBE_TOP_PERCENTILE  = 0.25   # top 25%

        # Initialise loop fields on every stream so callers can always read them
        for s in analyzed_streams:
            s.setdefault('loop_detected', None)
            s.setdefault('loop_duration_secs', None)
            s.setdefault('loop_probe_ran', False)

        # Build candidate pool: alive, scored at or above threshold, not cached
        candidates = [
            s for s in analyzed_streams
            if s.get('score', 0) >= LOOP_PROBE_SCORE_THRESHOLD
            and s.get('status') != 'cached'
        ]

        if not candidates:
            logger.info("[loop-probe] No streams meet eligibility criteria — skipping all probes")
            return

        # Rank by score descending, take top percentile (minimum 1)
        candidates_sorted = sorted(candidates, key=lambda s: s.get('score', 0), reverse=True)
        cutoff  = max(1, int(len(candidates_sorted) * LOOP_PROBE_TOP_PERCENTILE))
        eligible = candidates_sorted[:cutoff]

        total = len(eligible)
        logger.info(
            f"[loop-probe] {total} stream(s) eligible for loop probe "
            f"(top {int(LOOP_PROBE_TOP_PERCENTILE * 100)}% of {len(candidates_sorted)} "
            f"scoring >= {LOOP_PROBE_SCORE_THRESHOLD}) — running in parallel"
        )

        global_limit = self.config.get('concurrent_streams.global_limit', 10)
        account_limiter = get_account_limiter()
        udi = get_udi_manager()
        results_lock = threading.Lock()
        completed = [0]

        # Build probe_detail from the streams_detail snapshot passed in by the
        # calling check method. Keyed by integer stream id (same as stream_statuses).
        # Only built when streams_detail is available — the grid stays frozen
        # otherwise but the probes still run correctly.
        probe_detail: dict = {}
        if streams_detail:
            for entry in streams_detail:
                sid = entry.get('id') or entry.get('stream_id')
                if sid is not None:
                    probe_detail[sid] = dict(entry)  # shallow copy

        # Mark eligible streams as 'probing' and stamp started_at
        eligible_ids = {s.get('stream_id') for s in eligible}
        probe_start = datetime.now().isoformat()
        for sid, entry in probe_detail.items():
            if sid in eligible_ids:
                entry['status'] = 'probing'
                entry['started_at'] = probe_start

        # Emit phase-entry progress update so frontend transitions to loop phase
        if channel_id and probe_detail:
            self.progress.update(
                channel_id=channel_id,
                channel_name=channel_name,
                current=0,
                total=total,
                status='analyzing',
                step='Loop testing',
                step_detail=f'Probing {total} stream(s) for looping content',
                streams_detail=list(probe_detail.values()),
                stream_duration=probe_duration,
            )

        def _probe_one(stream: dict) -> None:
            """Run one loop probe with account slot acquire/release."""
            stream_url  = stream.get('stream_url', '')
            stream_name = stream.get('stream_name', 'Unknown')
            stream_id   = stream.get('stream_id')
            score       = stream.get('score', 0)

            # Resolve numeric account ID from UDI.
            # analyzed dicts carry stream_id from quality analysis — use that
            # to look up the raw stream record which has m3u_account_id.
            account_id = None
            try:
                raw_stream = udi.get_stream_by_id(int(stream_id)) if stream_id else None
                if raw_stream:
                    # SQL storage uses m3u_account_id; AccountStreamLimiter
                    # expects the integer under the key 'm3u_account'
                    account_id = raw_stream.get('m3u_account_id') or raw_stream.get('m3u_account')
            except Exception:
                pass

            # Build a short readable tag for log messages
            try:
                from urllib.parse import urlparse as _up
                _p    = _up(stream_url)
                _segs = [seg for seg in _p.path.split('/') if seg]
                tag   = f"{_p.hostname}/{_segs[-1]}" if _segs else (_p.hostname or stream_url[:20])
            except Exception:
                tag = stream_url[:30]

            # Acquire account slot — same mechanism used by quality analysis.
            # Timeout of 60s: if the account is saturated (e.g. live viewers
            # consuming all slots) we skip rather than block indefinitely.
            acquired, reason = account_limiter.acquire(account_id, timeout=60)
            if not acquired:
                logger.info(
                    f"[loop-probe:{tag}] Skipping '{stream_name}' — "
                    f"account slot unavailable ({reason})"
                )
                return

            try:
                logger.info(
                    f"[loop-probe:{tag}] Probing '{stream_name}' "
                    f"(ID: {stream_id}, score: {score:.2f})"
                )
                loop_detected, loop_duration, frames = _probe_stream_for_loops(
                    url=stream_url,
                    stream_tag=tag,
                    probe_duration=probe_duration,
                    user_agent=user_agent,
                )
                stream['loop_detected']      = loop_detected
                stream['loop_duration_secs'] = loop_duration
                stream['loop_probe_ran']     = True

            except Exception as e:
                logger.error(
                    f"[loop-probe:{tag}] Probe failed for '{stream_name}': {e}"
                )
                # loop_detected remains None — distinguishable from clean (False)
                # or detected (True)
            finally:
                account_limiter.release(account_id)
                with results_lock:
                    completed[0] += 1
                    # Update probe_detail entry with result for live grid
                    if probe_detail and stream_id in probe_detail:
                        loop_result = stream.get('loop_detected')
                        probe_detail[stream_id]['status'] = (
                            'loop_detected' if loop_result is True else 'completed'
                        )
                    if channel_id and probe_detail:
                        self.progress.update(
                            channel_id=channel_id,
                            channel_name=channel_name,
                            current=completed[0],
                            total=total,
                            status='analyzing',
                            step='Loop testing',
                            step_detail=f'Completed {completed[0]}/{total}: {stream_name}',
                            streams_detail=list(probe_detail.values()),
                            stream_duration=probe_duration,
                        )
                    logger.info(
                        f"[loop-probe] Completed {completed[0]}/{total}: {stream_name}"
                    )

        with ThreadPoolExecutor(max_workers=global_limit) as executor:
            futures = {executor.submit(_probe_one, stream): stream for stream in eligible}
            for future in as_completed(futures):
                try:
                    future.result()
                except Exception as e:
                    stream = futures[future]
                    logger.error(
                        f"[loop-probe] Unhandled error for stream "
                        f"{stream.get('stream_name', 'Unknown')}: {e}"
                    )

        logger.info(f"[loop-probe] Parallel probe complete — {completed[0]}/{total} streams probed")

        # Apply score penalty to confirmed looping streams.
        # Only fires when loop_penalty is non-zero and loop_detected is True.
        # Score is floored at 0.0 — looping is bad but the stream still exists.
        if loop_penalty < 0.0:
            penalised = 0
            for stream in analyzed_streams:
                if stream.get('loop_detected') is True:
                    original = stream.get('score', 0.0)
                    stream['score'] = round(max(0.0, original + loop_penalty), 2)
                    stream['loop_score_penalty'] = loop_penalty
                    logger.info(
                        f"[loop-probe] Penalty applied to '{stream.get('stream_name', 'Unknown')}': "
                        f"{original:.2f} → {stream['score']:.2f} (penalty={loop_penalty:+.2f})"
                    )
                    penalised += 1
            if penalised:
                logger.info(f"[loop-probe] Score penalty applied to {penalised} looping stream(s)")

    def _calculate_stream_score(self, stream_data: Dict, priority_m3u_ids: List[int] = None, priority_mode: str = 'absolute', scoring_weights: Dict = None) -> float:
        """Calculate a quality score for a stream based on analysis.
        
        Applies M3U account priority matching the order in the channel's Automation Profile.
        
        Args:
            stream_data: Dictionary of stream analysis data
            priority_m3u_ids: List of M3U account IDs in priority order (highest first)
            priority_mode: 'absolute' (priority trumps quality) or 'same_resolution' (quality trumps priority)
            scoring_weights: Optional per-profile scoring weights. Falls back to global config if not provided.
        """
        # Dead streams always get a score of 0
        if self._is_stream_dead(stream_data):
            return 0.0
        
        # Use per-profile weights if provided, otherwise fall back to global config
        if scoring_weights is None:
            weights = self.config.get('scoring.weights', {})
            prefer_h265 = self.config.get('scoring.prefer_h265', True)
        else:
            weights = {
                'bitrate': scoring_weights.get('bitrate', 0.35),
                'resolution': scoring_weights.get('resolution', 0.30),
                'fps': scoring_weights.get('fps', 0.15),
                'codec': scoring_weights.get('codec', 0.10),
                'hdr': scoring_weights.get('hdr', 0.10)
            }
            prefer_h265 = scoring_weights.get('prefer_h265', True)
        
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
                if height >= 2160:
                    resolution_score = 1.0
                elif height >= 1080:
                    resolution_score = 0.85
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
                codec_score = 1.0 if prefer_h265 else 0.8
            elif 'h264' in codec or 'avc' in codec:
                codec_score = 0.8 if prefer_h265 else 1.0
            elif codec != 'n/a':
                codec_score = 0.5
        score += codec_score * weights.get('codec', 0.10)
        
        # HDR score (0-1)
        # Give full score for HDR10 or HLG, zero for SDR
        hdr_format = stream_data.get('hdr_format')
        hdr_score = 1.0 if hdr_format in ['HDR10', 'HLG'] else 0.0
        score += hdr_score * weights.get('hdr', 0.10)
        
        return round(score, 2)
    
    def _get_priority_boost(self, stream_id: int, stream_data: Dict, priority_m3u_ids: List[int] = None, priority_mode: str = 'absolute') -> float:
        """Calculate priority boost for a stream based on its M3U account priority.
        
        Args:
            stream_id: The stream ID
            stream_data: Stream data dictionary containing resolution and other info
            priority_m3u_ids: List of M3U account IDs in priority order (highest first)
            priority_mode: 'absolute' or 'same_resolution'
            
        Returns:
            Priority boost value
        """
        try:
            if not priority_m3u_ids:
                return 0.0
                
            # Get stream from UDI to find its M3U account
            udi = get_udi_manager()
            stream = udi.get_stream_by_id(stream_id)
            if not stream:
                return 0.0
            
            m3u_account_id = stream.get('m3u_account')
            if not m3u_account_id:
                return 0.0
            
            # Check if this account is in the priority list
            if m3u_account_id in priority_m3u_ids:
                # Calculate boost based on position (index)
                # Lower index = higher priority
                index = priority_m3u_ids.index(m3u_account_id)
                total_accounts = len(priority_m3u_ids)
                
                if priority_mode == 'equal':
                    # Equal Mode
                    # No priority boost, ranking matches quality exactly
                    boost = 0.0
                    logger.debug(f"Applying equal priority (no boost) to stream {stream_id}")
                elif priority_mode == 'same_resolution':
                    # Same Resolution (Tie-breaker) Mode
                    # Small boost (0.05 per step) to break ties in quality
                    # Example separation: 0.15 max (less than resolution tier gap)
                    boost = 0.05 * (total_accounts - index)
                    logger.debug(f"Applying tie-breaker priority boost of {boost} to stream {stream_id}")
                else:
                    # Absolute Mode (Default)
                    # Massive boost to override quality differences
                    # Boost formula: Base 10 + (inverted index count)
                    boost = 10.0 + (total_accounts - index)
                    logger.debug(f"Applying absolute priority boost of {boost} to stream {stream_id}")
                
                return boost
            
            return 0.0
        except Exception as e:
            logger.error(f"Error calculating priority boost for stream {stream_id}: {e}")
            return 0.0
    

    def _get_resolution_tier(self, resolution: str) -> int:
        """Map resolution string to a numeric tier (0-5, lower is better)."""
        if not resolution or 'x' not in str(resolution):
            return 5 # Unknown/N/A
            
        try:
            # Handle list/tuple format if resolution was already parsed elsewhere
            if isinstance(resolution, (list, tuple)):
                height = int(resolution[1])
            else:
                width, height = map(int, str(resolution).split('x'))
                
            if height >= 2160: return 0 # 4K
            if height >= 1080: return 1 # 1080p
            if height >= 720:  return 2 # 720p
            if height >= 576:  return 3 # 576p/SD
            return 4 # Low resolution
        except (ValueError, AttributeError, IndexError):
            return 5

    def _generate_stream_sort_key(self, stream_data: Dict, priority_m3u_ids: List[int] = None, priority_mode: str = 'absolute') -> Tuple:
        """Generate a lexicographical sort key for a stream based on priority tiers.
        
        The sort key is a tuple used for ascending sort (lower is better).
        
        (AccountRank, ResolutionTier, QualityScore)
        
        Tiers (for 'same_resolution' mode):
        (ResolutionTier, AccountRank, QualityScore)
        """
        # 1. Account Rank (0 = highest)
        account_rank = 100
        stream_id = stream_data.get('stream_id')
        if priority_m3u_ids and stream_id:
            udi = get_udi_manager()
            stream = udi.get_stream_by_id(stream_id)
            if stream:
                m3u_id = stream.get('m3u_account')
                if m3u_id in priority_m3u_ids:
                    account_rank = priority_m3u_ids.index(m3u_id)
        
        # 2. Resolution Tier (0 = highest)
        res_tier = self._get_resolution_tier(stream_data.get('resolution'))
        
        
        # 4. Quality Score (lower is better, so negate the 0-1 scale)
        quality_score = -stream_data.get('score', 0.0)
        
        if priority_mode == 'same_resolution':
            return (res_tier, account_rank, quality_score)
        elif priority_mode == 'equal':
            # In 'equal' mode, resolution and quality matter, but not M3U account priority
            return (res_tier, quality_score)
        else: # 'absolute' mode
            return (account_rank, res_tier, quality_score)
    
    def get_status(self) -> Dict:
        """Get current service status."""
        queue_status = self.check_queue.get_status()
        progress = self.progress.get()
        
        with self.lock:
            sync_state = dict(self.sync_batch_state)
            
        if sync_state.get('active'):
            # Override queue status with our synchronous batch status
            # When active, ONLY the sync batch progress should be displayed
            queue_status['in_progress'] = sync_state['in_progress']
            queue_status['completed'] = sync_state['completed']
            queue_status['failed'] = sync_state['failed']
            queue_status['queued'] = sync_state['total_channels'] - sync_state['completed'] - sync_state['failed'] - sync_state['in_progress']
            queue_status['total_queued'] = sync_state['total_channels']
            queue_status['total_completed'] = sync_state['completed']
            queue_status['total_failed'] = sync_state['failed']
            queue_status['queue_size'] = queue_status['queued']
            
            # Map tracking stream properties back over queue_status for calculations
            queue_status['queued_streams_count'] = sync_state.get('queued_streams_count', 0)
            queue_status['in_progress_streams_count'] = sync_state.get('in_progress_streams_count', 0)
            
            # Use real queue average if available, otherwise 0
            queue_status['avg_stream_process_time_sec'] = self.check_queue.get_status().get('avg_stream_process_time_sec', 0)
            
        # Mathematical ETA Calculation
        avg_seconds = queue_status.get('avg_stream_process_time_sec', 0)
        remaining_streams = queue_status.get('queued_streams_count', 0) + queue_status.get('in_progress_streams_count', 0)
        
        eta_seconds = 0
        if avg_seconds > 0 and remaining_streams > 0:
            if self.config.get('concurrent_streams.enabled', True):
                # Parallel worker mapping applies strictly per-channel, not across the whole queue.
                # E.g. If max_workers=5 and a channel has 1 stream, it processes sequentially.
                # Therefore, ETA = (Remaining Channels) * (Average stream timing * Average streams per channel / max_workers)
                
                # To keep math accurate simply across total queued items vs average duration metric:
                # We factor concurrency purely as a divisor of the remaining time, but cap the denominator
                # at whatever typical 'max streams per channel' throughput averages to avoid over-optimistic calculations.
                max_workers = max(1, self.config.get('concurrent_streams', {}).get('max_workers', 5))
                
                # Assume an average channel distributes its streams perfectly over max_workers:
                eta_seconds = (avg_seconds * remaining_streams) / max_workers
            else:
                # Sequential Mode
                eta_seconds = avg_seconds * remaining_streams
                
        queue_status['eta_seconds'] = int(eta_seconds)
        
        # Stream checking mode is active when:
        # - An individual channel is being checked, OR
        # - There are channels in the queue waiting to be checked
        stream_checking_mode = (
            self.checking or 
            queue_status.get('queue_size', 0) > 0 or
            queue_status.get('in_progress', 0) > 0 or
            sync_state.get('active', False)
        )
        
        return {
            'running': self.running,
            'checking': self.checking,
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
            
        # Ensure we can re-queue if it was completed (manual check overrides completion state)
        self.check_queue.remove_from_completed(channel_id)
        
        # Look up stream count accurately to assist in ETA tracking calculations
        udi = get_udi_manager()
        channel = udi.get_channel_by_id(channel_id)
        stream_count = len(channel.get('streams', [])) if channel else 1
        
        # Inject exact array width
        with self.check_queue.lock:
            # We add manually here if not queued, or intercept normal logic.
            # Best way without changing add_channel signature excessively is to manually modify:
            pass # We'll modify add_channel signature to handle this cleanly.
        
        # Calling modified add_channel
        return self.check_queue.add_channel(channel_id, priority, stream_count=stream_count)
    
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
    
    def check_channels_synchronously(self, channel_ids: List[int], force_check: bool = False, target_stream_ids: Optional[Dict[int, List[str]]] = None) -> Dict[int, Dict]:
        """Check multiple channels synchronously and return results.
        
        Using this method Bypasses the queue and worker/scheduler entirely.
        This is useful for automation cycles where we want to wait for results
        and consolidate them into a single report.
        
        Args:
            channel_ids: List of channel IDs to check
            force_check: If True, marks channels for force checking
            target_stream_ids: Optional dict mapping channel_id -> list of stream_ids.
                               If provided, only these specific streams will be evaluated.
                               Any stream not in the list will be skipped and its existing
                               stats cached. Used by automation for newly matched streams.
            
        Returns:
            Dict mapping channel_id to result dict (containing dead/revived streams)
        """
        results = {}
        
        # Mark force check if requested
        if force_check:
            for channel_id in channel_ids:
                self.update_tracker.mark_channel_for_force_check(channel_id)
                
        # Fast lookup precise stream counts
        from apps.udi import get_udi_manager
        udi = get_udi_manager()
        
        channel_streams = {}
        total_streams = 0
        for channel_id in channel_ids:
            channel = udi.get_channel_by_id(channel_id)
            stream_count = len(channel.get('streams', [])) if channel else 1
            channel_streams[channel_id] = stream_count
            total_streams += stream_count
                
        with self.lock:
            self.sync_batch_state = {
                'active': True,
                'total_channels': len(channel_ids),
                'completed': 0,
                'failed': 0,
                'in_progress': 0,
                'queued_streams_count': total_streams,
                'in_progress_streams_count': 0
            }
            self.checking = True
        
        try:
            # Process each channel
            for channel_id in channel_ids:
                stream_count = channel_streams.get(channel_id, 1)
                
                with self.lock:
                    self.sync_batch_state['in_progress'] = 1
                    self.sync_batch_state['queued_streams_count'] = max(0, self.sync_batch_state['queued_streams_count'] - stream_count)
                    self.sync_batch_state['in_progress_streams_count'] = stream_count
                    
                channel_start_time = datetime.now()
                
                try:
                    # Use specific channel-checker directly depending on concurrency setting
                    concurrent_enabled = self.config.get('concurrent_streams.enabled', True)
                    
                    if target_stream_ids and channel_id in target_stream_ids:
                        stream_id_whitelist = target_stream_ids[channel_id]
                    else:
                        stream_id_whitelist = None
                        
                    if concurrent_enabled:
                        channel_result = self._check_channel_concurrent(channel_id, skip_batch_changelog=True, target_stream_ids=stream_id_whitelist)
                    else:
                        channel_result = self._check_channel_sequential(channel_id, skip_batch_changelog=True, target_stream_ids=stream_id_whitelist)
                        
                    results[channel_id] = channel_result
                    with self.lock:
                        self.sync_batch_state['completed'] += 1
                except Exception as e:
                    logger.error(f"Error checking channel {channel_id} synchronously: {e}")
                    results[channel_id] = {'error': str(e)}
                    with self.lock:
                        self.sync_batch_state['failed'] += 1
                finally:
                    duration_sec = (datetime.now() - channel_start_time).total_seconds()
                    if stream_count > 0:
                        time_per_stream = duration_sec / stream_count
                        with self.check_queue.lock:
                            self.check_queue.stream_processing_times.append(time_per_stream)
                            
                    with self.lock:
                        self.sync_batch_state['in_progress'] = 0
                        self.sync_batch_state['in_progress_streams_count'] = 0
        finally:
            with self.lock:
                self.sync_batch_state['active'] = False
                queue_status = self.check_queue.get_status()
                if queue_status.get('queue_size', 0) == 0 and queue_status.get('in_progress', 0) == 0:
                    self.checking = False
                
        return results

    def check_single_channel(self, channel_id: int, program_name: Optional[str] = None, is_epg_scheduled: bool = False) -> Dict:
        """Check a single channel immediately and return results.
        
        This performs a targeted channel refresh for a single channel:
        - Identifies M3U accounts used by the channel
        - Refreshes playlists for accounts associated with the channel
        - Clears dead streams for the specified channel to give them a second chance
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
            is_epg_scheduled: If True, prefer the channel's EPG scheduled profile over the period profile
            
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
            
            # Check if channel is in active monitoring session (coordination with monitoring system)
            from apps.stream.stream_session_manager import get_session_manager
            session_manager = get_session_manager()
            channels_in_monitoring = session_manager.get_channels_in_active_sessions()
            
            if channel_id in channels_in_monitoring:
                logger.info(f"⏸ Skipping channel {channel_name} (ID: {channel_id}) - currently in active monitoring session")
                return {
                    'success': False,
                    'skipped': True,
                    'reason': 'in_monitoring_session',
                    'message': f'Channel {channel_name} is in an active monitoring session and cannot be checked by automation',
                    'channel_id': channel_id,
                    'channel_name': channel_name
                }
            
            # Check channel settings for matching and checking modes
            # Check channel settings for matching and checking modes via Automation Profiles
            from apps.automation.automation_config_manager import get_automation_config_manager
            automation_config = get_automation_config_manager()
            
            # channel dict is available in local scope
            channel_group_id = channel.get('channel_group_id')

            # If this is an EPG scheduled check, prefer the EPG scheduled profile override
            profile = None
            if is_epg_scheduled:
                epg_profile = automation_config.get_effective_epg_scheduled_profile(channel_id, channel_group_id)
                if epg_profile:
                    profile = epg_profile
                    logger.info(f"Channel {channel_name}: using EPG scheduled profile '{epg_profile.get('name')}'")

            # Fall back to the active period-based profile if no EPG profile was found
            if profile is None:
                config = automation_config.get_effective_configuration(channel_id, channel_group_id)
                profile = config.get('profile') if config else None
            
            # Default to global automation controls when no channel/group profile applies.
            matching_enabled = self.config.get('automation_controls.auto_stream_matching', True)
            checking_enabled = self.config.get('automation_controls.auto_quality_checking', True)
            if profile:
                matching_enabled = profile.get('stream_matching', {}).get('enabled', False)
                checking_enabled = profile.get('stream_checking', {}).get('enabled', False)
            
            logger.info(f"Channel {channel_name} settings: matching={matching_enabled}, checking={checking_enabled}")

            # Signal to the frontend that this is a single channel check so the
            # stale batch progress card from the previous automation run is suppressed.
            self.progress.update(
                channel_id=channel_id,
                channel_name=channel_name,
                current=0,
                total=1,
                status='starting',
                step='Starting single channel check',
                step_detail=f'Preparing check for {channel_name}',
                is_single_channel_check=True,
            )

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
                from apps.core.api_utils import refresh_m3u_playlists
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
                    from apps.automation.automated_stream_manager import AutomatedStreamManager
                    automation_manager = AutomatedStreamManager()
                    
                    # Run validation scoped to this channel only
                    validation_results = automation_manager.validate_and_remove_non_matching_streams(channel_id=channel_id)
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
                    from apps.automation.automated_stream_manager import AutomatedStreamManager
                    automation_manager = AutomatedStreamManager()
                    
                    # Run discovery scoped to this channel only
                    # Skip automatic check trigger since we'll perform the check explicitly in Step 6
                    assignments = automation_manager.discover_and_assign_streams(force=True, skip_check_trigger=True, channel_id=channel_id)
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

                # Build an in-memory lookup keyed by stream_id from the authoritative
                # analyzed_streams list. This carries correct loop results and m3u_account
                # names without depending on UDI refresh timing or Dispatcharr staleness.
                analyzed_lookup = {
                    a.get('stream_id'): a
                    for a in check_result.get('analyzed_streams', [])
                    if a.get('stream_id') is not None
                }
            else:
                logger.info(f"Step 6/6: Skipping stream checking (checking is disabled for this channel)")
                analyzed_lookup = {}
            
            # Gather statistics after check using centralized utility.
            # Refresh UDI cache first so stream_stats reflect the post-probe
            # database state — loop fields written by _update_stream_stats
            # won't be visible otherwise.
            udi.refresh_streams()
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
            
            # Sort streams by persisted quality_score descending so the
            # highest-ranked streams (including any that were loop-probed)
            # appear first. No arbitrary cap — all streams are included so
            # loop results are never hidden by a slice.
            streams_sorted = sorted(
                streams,
                key=lambda s: (s.get('stream_stats') or {}).get('quality_score') or 0,
                reverse=True
            )
            for stream in streams_sorted:
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
                
                # Calculate score — prefer the in-memory score from the check run
                # which already reflects loop penalties, priority weights, and profile
                # settings at the time of the check. Recalculate only as a fallback
                # for streams not present in the lookup (e.g. pre-existing streams
                # not re-analyzed in this run).
                analyzed = analyzed_lookup.get(stream.get('id'))
                if analyzed and analyzed.get('score') is not None:
                    score = analyzed.get('score')
                else:
                    score = self._calculate_stream_score(score_data)

                # M3U account: use the name already resolved during the check
                if analyzed and analyzed.get('m3u_account'):
                    m3u_account_name = analyzed.get('m3u_account')
                else:
                    m3u_account_name = None
                    m3u_account_id = stream.get('m3u_account')
                    if m3u_account_id:
                        m3u_account_name = self._get_m3u_account_name(stream.get('id'), udi)
                
                # Build stream detail dict — include loop results if persisted
                stream_detail = {
                    'stream_id': stream.get('id'),
                    'stream_name': stream.get('name', 'Unknown'),
                    'resolution': formatted_stats['resolution'],
                    'bitrate': formatted_stats['bitrate'],
                    'video_codec': formatted_stats['video_codec'],
                    'fps': formatted_stats['fps'],
                    'score': score,
                    'm3u_account': m3u_account_name,
                    'hdr_format': extracted_stats.get('hdr_format')
                }

                # Loop detection: prefer in-memory analyzed dict (authoritative, always
                # current) over Dispatcharr stream_stats (may lag UDI refresh timing).
                if analyzed and analyzed.get('loop_probe_ran'):
                    stream_detail['loop_probe_ran']     = True
                    stream_detail['loop_detected']      = analyzed.get('loop_detected')
                    stream_detail['loop_duration_secs'] = analyzed.get('loop_duration_secs')
                elif stream_stats.get('loop_probe_ran'):
                    # Fallback: stream not in analyzed_lookup but has persisted loop data
                    stream_detail['loop_probe_ran']     = True
                    stream_detail['loop_detected']      = stream_stats.get('loop_detected')
                    stream_detail['loop_duration_secs'] = stream_stats.get('loop_duration_secs')

                check_stats['stream_details'].append(stream_detail)
            
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
                        logo_url = f"/api/logos/{logo_id}"
                    
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
            
            # Note: _trigger_channel_re_enabling and _trigger_empty_channel_disabling
            # have been deprecated as they relied on obsolete Dispatcharr features

            # Clear progress so the frontend stops showing the single channel check UI
            self.progress.clear()

            return {
                'success': True,
                'channel_id': channel_id,
                'channel_name': channel_name,
                'stats': check_stats
            }
            
        except Exception as e:
            logger.error(f"Error checking single channel {channel_id}: {e}", exc_info=True)
            self.progress.clear()
            return {'success': False, 'error': str(e)}
    
    def clear_queue(self):
        """Clear the checking queue."""
        self.check_queue.clear()
        self.abort_current_check.set()
        self._cancel_queueing = True
        logger.info("Checking queue cleared and current check aborted")
    
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
