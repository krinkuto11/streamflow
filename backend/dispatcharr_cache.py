#!/usr/bin/env python3
"""
Dispatcharr Cache Module - Facade over Unified Data Index

This module provides a caching facade that delegates entirely to the
Unified Data Index (UDI) SQLite-based persistent storage.

ARCHITECTURE (v3.0):
All caching is now handled exclusively by the Unified Data Index.
The legacy in-memory cache has been removed. This ensures:
- Single source of truth for all cached data
- Persistent storage across application restarts
- Consistent data across all components

The cache facade is kept for API compatibility with existing code.

Usage:
    with DispatcharrCache() as cache:
        # All API calls within this context will use the UDI
        streams = cache.get_streams()
        channel_streams = cache.get_channel_streams(channel_id)
"""

import threading
from typing import Dict, List, Optional, Any, Set

from logging_config import setup_logging
from unified_data_index import get_unified_data_index

logger = setup_logging(__name__)


class DispatcharrCache:
    """
    Thread-safe cache facade for Dispatcharr data using Unified Data Index.
    
    This cache delegates all operations to the Unified Data Index (UDI).
    The context manager interface is maintained for API compatibility.
    
    Thread Safety:
    - All operations are protected by locks
    - Safe for concurrent access from multiple threads
    """
    
    # Class-level shared cache - singleton pattern
    _instance = None
    _lock = threading.Lock()
    _enabled = False
    
    def __new__(cls, *args, **kwargs):
        """Ensure singleton instance."""
        with cls._lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
        return cls._instance
    
    def __init__(self, max_age: int = 3600, auto_invalidate: bool = True):
        """
        Initialize the cache facade.
        
        Args:
            max_age: Ignored (kept for API compatibility)
            auto_invalidate: Ignored (kept for API compatibility)
        """
        self._context_depth = 0
    
    def __enter__(self):
        """Enter cache context - enables caching."""
        with self._lock:
            self._context_depth += 1
            if self._context_depth == 1:
                self._enabled = True
                logger.debug("Cache context enabled (using Unified Data Index)")
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        """Exit cache context."""
        with self._lock:
            self._context_depth -= 1
            if self._context_depth == 0:
                self._enabled = False
                logger.debug("Cache context disabled")
        return False
    
    @classmethod
    def is_enabled(cls) -> bool:
        """Check if caching context is currently active."""
        with cls._lock:
            return cls._enabled
    
    def invalidate(self) -> None:
        """
        Invalidate cached data.
        
        Note: With UDI, this is a no-op since data is persisted.
        The index should be rebuilt instead of invalidated.
        """
        logger.debug("Cache invalidate called (no-op with UDI)")
    
    def get_streams(self, fetch_func) -> List[Dict[str, Any]]:
        """
        Get all streams from the Unified Data Index.
        
        Falls back to fetch_func if UDI is empty.
        
        Args:
            fetch_func: Function to call to fetch streams if UDI is empty
                       Should return List[Dict[str, Any]]
        
        Returns:
            List of stream dictionaries
        """
        try:
            udi = get_unified_data_index()
            udi_stats = udi.get_stats()
            if udi_stats.get('streams', 0) > 0:
                streams = udi.get_all_streams()
                if streams:
                    logger.debug(f"Retrieved {len(streams)} streams from Unified Data Index")
                    return streams
        except Exception as e:
            logger.warning(f"UDI read failed: {e}")
        
        # UDI is empty or failed - fetch from API
        logger.info("Fetching streams from Dispatcharr API (UDI empty)...")
        streams = fetch_func()
        return streams if streams is not None else []
    
    def get_channel_streams(self, channel_id: int, fetch_func) -> Optional[List[Dict[str, Any]]]:
        """
        Get streams for a specific channel from the Unified Data Index.
        
        Falls back to fetch_func if UDI is empty.
        
        Args:
            channel_id: Channel ID
            fetch_func: Function to call to fetch channel streams if UDI is empty
                       Should accept channel_id and return List[Dict[str, Any]]
        
        Returns:
            List of stream dictionaries or None if fetch fails
        """
        try:
            udi = get_unified_data_index()
            udi_stats = udi.get_stats()
            if udi_stats.get('channel_streams', 0) > 0:
                streams = udi.get_channel_streams(channel_id)
                if streams is not None:
                    logger.debug(f"Retrieved {len(streams)} streams for channel {channel_id} from UDI")
                    return streams
        except Exception as e:
            logger.warning(f"UDI read failed for channel {channel_id}: {e}")
        
        # UDI is empty or failed - fetch from API
        logger.debug(f"Fetching streams for channel {channel_id} from API (UDI empty)...")
        return fetch_func(channel_id)
    
    def get_valid_stream_ids(self, fetch_func) -> Set[int]:
        """
        Get set of valid stream IDs from the Unified Data Index.
        
        Falls back to computing from fetch_func if UDI is empty.
        
        Args:
            fetch_func: Function to call to get all streams if UDI is empty
                       Should return List[Dict[str, Any]]
        
        Returns:
            Set of valid stream IDs
        """
        try:
            udi = get_unified_data_index()
            udi_stats = udi.get_stats()
            if udi_stats.get('streams', 0) > 0:
                valid_ids = udi.get_valid_stream_ids()
                if valid_ids:
                    logger.debug(f"Retrieved {len(valid_ids)} valid stream IDs from UDI")
                    return valid_ids
        except Exception as e:
            logger.warning(f"UDI read failed for valid stream IDs: {e}")
        
        # UDI is empty or failed - compute from API
        all_streams = self.get_streams(fetch_func)
        return {stream['id'] for stream in all_streams if isinstance(stream, dict) and 'id' in stream}
    
    def get_stream_id_to_url_mapping(self, fetch_func) -> Dict[int, str]:
        """
        Get mapping of stream IDs to URLs from the Unified Data Index.
        
        Falls back to computing from fetch_func if UDI is empty.
        
        Args:
            fetch_func: Function to call to get all streams if UDI is empty
                       Should return List[Dict[str, Any]]
        
        Returns:
            Dictionary mapping stream IDs to URLs
        """
        try:
            udi = get_unified_data_index()
            udi_stats = udi.get_stats()
            if udi_stats.get('streams', 0) > 0:
                mapping = udi.get_stream_url_mapping()
                if mapping:
                    logger.debug(f"Retrieved stream URL mapping ({len(mapping)} entries) from UDI")
                    return mapping
        except Exception as e:
            logger.warning(f"UDI read failed for stream URL mapping: {e}")
        
        # UDI is empty or failed - compute from API
        all_streams = self.get_streams(fetch_func)
        return {s['id']: s.get('url') for s in all_streams if isinstance(s, dict) and 'id' in s}
    
    def get_stats(self) -> Dict[str, Any]:
        """
        Get cache statistics from the Unified Data Index.
        
        Returns:
            Dictionary with UDI statistics
        """
        with self._lock:
            stats = {
                "enabled": self._enabled,
                "context_depth": self._context_depth,
                "backend": "unified_data_index"
            }
            
            try:
                udi = get_unified_data_index()
                stats["udi_stats"] = udi.get_stats()
            except Exception as e:
                stats["udi_error"] = str(e)
            
            return stats
    
    def rebuild_index(self) -> Optional[Dict[str, int]]:
        """
        Rebuild the Unified Data Index from Dispatcharr.
        
        This should be called after M3U refresh to ensure the index
        is synchronized with Dispatcharr.
        
        Returns:
            Dict with counts of synced entities, or None on failure
        """
        try:
            from dispatcharr_sync_service import get_dispatcharr_sync_service
            sync_service = get_dispatcharr_sync_service()
            counts = sync_service.rebuild_index()
            
            logger.info(f"Index rebuilt: {counts}")
            return counts
        except Exception as e:
            logger.error(f"Failed to rebuild index: {e}")
            return None


# Global cache instance for convenience
_global_cache = DispatcharrCache()


def get_cache() -> DispatcharrCache:
    """
    Get the global cache instance.
    
    Returns:
        Global DispatcharrCache instance
    """
    return _global_cache
