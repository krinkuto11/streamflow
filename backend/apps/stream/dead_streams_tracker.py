#!/usr/bin/env python3
"""
Dead Streams Tracker for StreamFlow.

This module tracks dead streams in a JSON file using stream URLs as unique keys.
Stream URLs are used instead of names because multiple streams can have the same name.
"""

import json
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional, Any, List, Tuple

from apps.core.logging_config import setup_logging, log_function_call, log_function_return, log_exception
from apps.core.log_sanitizer import channel_ref, scrub_urls, stream_context, stream_ref

# Setup logging for this module
logger = setup_logging(__name__)

# Configuration directory
CONFIG_DIR = Path(os.environ.get('CONFIG_DIR', '/app/data'))


from apps.database.manager import get_db_manager

class DeadStreamsTracker:
    """Tracks dead streams utilizing a unified SQL Database backend."""
    
    def __init__(self, tracker_file=None):
        """Initialize the dead streams tracker using the DatabaseManager."""
        self.db = get_db_manager()
    
    def _load_dead_streams(self) -> Dict[str, Dict]:
        """Deprecated."""
        return self.db.get_dead_streams(as_dict=True)
    
    def _save_dead_streams(self):
        """Deprecated."""
        pass
    
    def mark_as_dead(self, stream_url: str, stream_id: int, stream_name: str, channel_id: int = None, reason: str = 'unknown') -> bool:
        """Mark a stream as dead in the database."""
        try:
            res = self.db.mark_stream_dead(stream_url, stream_id, stream_name, channel_id, reason)
            if res:
                logger.info(
                    f"Marked stream as dead: "
                    f"{stream_context(stream_id=stream_id, stream_url=stream_url, channel_id=channel_id, reason=reason)}"
                )
            return res
        except Exception as e:
            logger.error(f"Error marking stream as dead: {scrub_urls(e)}")
            return False
    
    def update_dead_reason(self, stream_url: str, reason: str, channel_id: int = None) -> bool:
        """Update the reason for an already-dead stream without logging sensitive stream details."""
        try:
            return self.db.update_dead_stream_reason(stream_url, reason, channel_id)
        except Exception as e:
            logger.error(f"Error updating dead stream reason: {e}")
            return False

    def mark_as_alive(self, stream_url: str) -> bool:
        """Mark a stream as alive (remove from dead streams)."""
        try:
            # We first fetch it from the total list to get its name for logging
            dead = self.db.get_dead_streams(as_dict=True)
            if stream_url in dead:
                stream_info = dead[stream_url]
                self.db.remove_dead_stream(stream_url)
                logger.info(f"Revived stream: {stream_ref(stream_info.get('stream_id'), stream_url)}")
            return True
        except Exception as e:
            logger.error(f"Error marking stream as alive: {scrub_urls(e)}")
            return False
    
    def is_dead(self, stream_url: str) -> bool:
        """Check if a stream is marked as dead."""
        return self.db.is_stream_dead(stream_url)
    
    def get_dead_reason(self, stream_url: str) -> Optional[str]:
        """Get the reason why a stream was marked as dead."""
        dead_streams = self.db.get_dead_streams(as_dict=True)
        info = dead_streams.get(stream_url)
        return info.get('reason') if info else None

    def is_offline(self, stream_url: str) -> bool:
        """Check if a stream is specifically 'offline'."""
        return self.get_dead_reason(stream_url) == 'offline'
    
    def get_dead_streams(self) -> Dict[str, Dict]:
        """Get all dead streams as dictionary."""
        return self.db.get_dead_streams(as_dict=True)
    
    def get_dead_streams_count_for_channel(self, channel_id: int) -> int:
        """Count dead streams for a channel."""
        return self.db.count_dead_streams_for_channel(channel_id)
    
    def get_dead_streams_for_channel(self, channel_id: int) -> Dict[str, Dict]:
        """Get dead streams for a channel."""
        return self.db.get_dead_streams_for_channel(channel_id, as_dict=True)
    
    def remove_dead_streams_by_channel_id(self, channel_id: int) -> int:
        removed_count = 0
        try:
            dead_streams = self.get_dead_streams_for_channel(channel_id)
            for url, stream_info in dead_streams.items():
                self.db.remove_dead_stream(url)
                removed_count += 1
                
            if removed_count > 0:
                logger.info(
                    f"Removed {removed_count} dead stream(s) from "
                    f"{channel_ref(channel_id)} before refresh"
                )
            return removed_count
        except Exception as e:
            logger.error(f"Error removing dead streams for {channel_ref(channel_id)}: {scrub_urls(e)}")
            return 0
    
    def remove_dead_streams_for_channel(self, channel_stream_urls: set) -> int:
        removed_count = 0
        try:
            dead_streams = self.get_dead_streams() # Uses DAL cache/fetch
            for url, stream_info in dead_streams.items():
                if url in channel_stream_urls:
                    self.db.remove_dead_stream(url)
                    removed_count += 1
                    logger.info(
                        f"Removed dead stream from channel tracking: "
                        f"{stream_ref(stream_info.get('stream_id'), url)}"
                    )
            return removed_count
        except Exception as e:
            logger.error(f"Error removing dead streams for channel: {scrub_urls(e)}")
            return 0
    
    def cleanup_removed_streams(self, current_stream_urls: set, channel_id: Optional[int] = None) -> int:
        """Remove tracker entries for streams no longer in the provider's playlist.

        When ``channel_id`` is supplied, cleanup is scoped to that channel's
        tracker entries. Global playlist refreshes intentionally omit it so
        entries for streams missing from the full refreshed playlist are cleaned.

        Only 'offline' and 'unstable' entries are eligible for cleanup.
        'low_quality' streams are intentionally absent from the channel because
        they were culled by the profile's quality thresholds — the URL still
        exists in the M3U source.  Clearing their tracker entry would cause the
        channel to appear empty on the next run: the checker removes them from
        the channel, then cleanup_removed_streams clears the tracker, then the
        next run sees no streams to check.
        """
        removed_count = 0
        skipped_low_quality = 0
        try:
            dead_streams = (
                self.get_dead_streams_for_channel(channel_id)
                if channel_id is not None
                else self.get_dead_streams()
            )
            for url, stream_info in dead_streams.items():
                if url not in current_stream_urls:
                    reason = stream_info.get('reason', 'unknown')
                    if reason == 'low_quality':
                        # Stream was removed by the checker for failing quality
                        # thresholds, not because the provider dropped it.
                        # Keep the tracker entry so subsequent runs know to
                        # continue excluding it without re-checking.
                        skipped_low_quality += 1
                        continue
                    self.db.remove_dead_stream(url)
                    removed_count += 1
                    if channel_id is not None:
                        logger.info(
                            f"Removed dead stream no longer in channel playlist: "
                            f"{stream_context(stream_id=stream_info.get('stream_id'), stream_url=url, channel_id=channel_id)}"
                        )
                    else:
                        logger.info(
                            f"Removed dead stream no longer in playlist: "
                            f"{stream_ref(stream_info.get('stream_id'), url)}"
                        )
            if skipped_low_quality:
                scope = f" for {channel_ref(channel_id)}" if channel_id is not None else ""
                logger.debug(
                    f"Skipped cleanup of {skipped_low_quality} low_quality stream(s){scope} "
                    f"— still present in M3U source, excluded from channel by quality threshold"
                )
            return removed_count
        except Exception as e:
            logger.error(f"Error cleaning up removed streams: {scrub_urls(e)}")
            return 0
    
    def clear_all_dead_streams(self) -> int:
        try:
            count = self.db.clear_all_dead_streams()
            if count > 0:
                logger.info(f"🔄 Clearing ALL {count} dead stream(s) from tracker")
            return count
        except Exception as e:
            logger.error(f"❌ Error clearing all dead streams: {e}")
            return 0
