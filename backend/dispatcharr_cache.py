#!/usr/bin/env python3
"""
Dispatcharr API Cache Module

This module provides a centralized caching layer for Dispatcharr API responses
to significantly reduce redundant API calls during playlist refresh cycles.

ARCHITECTURE CHANGE (v2.0):
This module now acts as a facade over the Unified Data Index (UDI).
Instead of caching in memory, it delegates to the SQLite-based UDI which
provides persistent caching and is rebuilt on every M3U refresh.

The old in-memory cache behavior is preserved for backward compatibility,
but the primary data source is now the UDI when available.

The cache is designed to be:
- Thread-safe for concurrent access
- Scoped to individual refresh cycles
- Automatically invalidated when needed
- Transparent to calling code (via context manager)
- Backed by the Unified Data Index for persistent storage

Usage:
    with DispatcharrCache() as cache:
        # All API calls within this context will use the cache
        streams = cache.get_streams()
        channel_streams = cache.get_channel_streams(channel_id)
"""

import threading
import time
from typing import Dict, List, Optional, Any, Set
from datetime import datetime, timedelta

from logging_config import setup_logging

logger = setup_logging(__name__)

# Try to import UDI - if not available, fall back to old behavior
try:
    from unified_data_index import get_unified_data_index
    UDI_AVAILABLE = True
except ImportError:
    UDI_AVAILABLE = False
    logger.debug("Unified Data Index not available, using legacy cache only")


class DispatcharrCache:
    """
    Thread-safe cache for Dispatcharr API responses.
    
    This cache is designed to be used within a specific context (like a playlist refresh
    or stream assignment cycle) and stores frequently accessed data to avoid redundant
    API calls.
    
    Cache Invalidation:
    - Manual: call invalidate()
    - Automatic: on context exit if auto_invalidate=True
    - Time-based: entries expire after max_age seconds
    
    Thread Safety:
    - All operations are protected by locks
    - Safe for concurrent access from multiple threads
    """
    
    # Class-level shared cache - singleton pattern
    _instance = None
    _lock = threading.Lock()
    _cache_data: Dict[str, Any] = {}
    _cache_timestamps: Dict[str, datetime] = {}
    _enabled = False
    
    def __new__(cls, *args, **kwargs):
        """Ensure singleton instance."""
        with cls._lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
        return cls._instance
    
    def __init__(self, max_age: int = 3600, auto_invalidate: bool = True):
        """
        Initialize the cache.
        
        Args:
            max_age: Maximum age of cache entries in seconds (default: 1 hour)
            auto_invalidate: Whether to invalidate cache on context exit
        """
        self.max_age = max_age
        self.auto_invalidate = auto_invalidate
        self._context_depth = 0
    
    def __enter__(self):
        """Enter cache context - enables caching."""
        with self._lock:
            self._context_depth += 1
            if self._context_depth == 1:
                self._enabled = True
                logger.debug("Dispatcharr cache enabled")
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        """Exit cache context - optionally invalidates cache."""
        with self._lock:
            self._context_depth -= 1
            if self._context_depth == 0:
                if self.auto_invalidate:
                    self._invalidate_internal()
                self._enabled = False
                logger.debug("Dispatcharr cache disabled")
        return False
    
    @classmethod
    def is_enabled(cls) -> bool:
        """Check if caching is currently enabled."""
        with cls._lock:
            return cls._enabled
    
    def _is_expired(self, key: str) -> bool:
        """Check if a cache entry has expired."""
        if key not in self._cache_timestamps:
            return True
        age = (datetime.now() - self._cache_timestamps[key]).total_seconds()
        return age > self.max_age
    
    def _get_from_cache(self, key: str) -> Optional[Any]:
        """
        Get value from cache if it exists and hasn't expired.
        
        Args:
            key: Cache key
            
        Returns:
            Cached value or None if not found/expired
        """
        if not self._enabled:
            return None
        
        with self._lock:
            if key in self._cache_data and not self._is_expired(key):
                logger.debug(f"Cache HIT: {key}")
                return self._cache_data[key]
            else:
                if key in self._cache_data:
                    logger.debug(f"Cache EXPIRED: {key}")
                else:
                    logger.debug(f"Cache MISS: {key}")
                return None
    
    def _set_in_cache(self, key: str, value: Any) -> None:
        """
        Store value in cache.
        
        Args:
            key: Cache key
            value: Value to cache
        """
        if not self._enabled:
            return
        
        with self._lock:
            self._cache_data[key] = value
            self._cache_timestamps[key] = datetime.now()
            logger.debug(f"Cache SET: {key} (size: {len(str(value))} chars)")
    
    def _invalidate_internal(self) -> None:
        """Internal method to clear all cache data."""
        cache_size = len(self._cache_data)
        self._cache_data.clear()
        self._cache_timestamps.clear()
        if cache_size > 0:
            logger.info(f"Cache invalidated ({cache_size} entries cleared)")
    
    def invalidate(self) -> None:
        """Public method to manually invalidate the cache."""
        with self._lock:
            self._invalidate_internal()
    
    def get_streams(self, fetch_func) -> List[Dict[str, Any]]:
        """
        Get all streams from cache or fetch if not cached.
        
        In v2.0, this method first tries to get data from the Unified Data Index.
        If UDI is not available or empty, falls back to the legacy cache behavior.
        
        Args:
            fetch_func: Function to call to fetch streams if not cached
                       Should return List[Dict[str, Any]]
        
        Returns:
            List of stream dictionaries
        """
        # Try Unified Data Index first
        if UDI_AVAILABLE:
            try:
                udi = get_unified_data_index()
                udi_stats = udi.get_stats()
                if udi_stats.get('streams', 0) > 0:
                    streams = udi.get_all_streams()
                    if streams:
                        logger.debug(f"Retrieved {len(streams)} streams from Unified Data Index")
                        return streams
            except Exception as e:
                logger.debug(f"UDI read failed, falling back to legacy cache: {e}")
        
        # Legacy cache behavior
        key = "all_streams"
        cached = self._get_from_cache(key)
        
        if cached is not None:
            return cached
        
        # Cache miss - fetch from API
        logger.info("Fetching streams from Dispatcharr API (not in cache)...")
        streams = fetch_func()
        
        if streams is not None:
            self._set_in_cache(key, streams)
        
        return streams
    
    def get_channel_streams(self, channel_id: int, fetch_func) -> Optional[List[Dict[str, Any]]]:
        """
        Get streams for a specific channel from cache or fetch if not cached.
        
        In v2.0, this method first tries to get data from the Unified Data Index.
        
        Args:
            channel_id: Channel ID
            fetch_func: Function to call to fetch channel streams if not cached
                       Should accept channel_id and return List[Dict[str, Any]]
        
        Returns:
            List of stream dictionaries or None if fetch fails
        """
        # Try Unified Data Index first
        if UDI_AVAILABLE:
            try:
                udi = get_unified_data_index()
                udi_stats = udi.get_stats()
                if udi_stats.get('channel_streams', 0) > 0:
                    streams = udi.get_channel_streams(channel_id)
                    if streams is not None:
                        logger.debug(f"Retrieved {len(streams)} streams for channel {channel_id} from UDI")
                        return streams
            except Exception as e:
                logger.debug(f"UDI read failed for channel {channel_id}, falling back: {e}")
        
        # Legacy cache behavior
        key = f"channel_{channel_id}_streams"
        cached = self._get_from_cache(key)
        
        if cached is not None:
            return cached
        
        # Cache miss - fetch from API
        logger.debug(f"Fetching streams for channel {channel_id} from API (not in cache)...")
        streams = fetch_func(channel_id)
        
        if streams is not None:
            self._set_in_cache(key, streams)
        
        return streams
    
    def get_valid_stream_ids(self, fetch_func) -> Set[int]:
        """
        Get set of valid stream IDs from cache or compute if not cached.
        
        In v2.0, this method first tries to get data from the Unified Data Index.
        
        Args:
            fetch_func: Function to call to get all streams if not cached
                       Should return List[Dict[str, Any]]
        
        Returns:
            Set of valid stream IDs
        """
        # Try Unified Data Index first
        if UDI_AVAILABLE:
            try:
                udi = get_unified_data_index()
                udi_stats = udi.get_stats()
                if udi_stats.get('streams', 0) > 0:
                    valid_ids = udi.get_valid_stream_ids()
                    if valid_ids:
                        logger.debug(f"Retrieved {len(valid_ids)} valid stream IDs from UDI")
                        return valid_ids
            except Exception as e:
                logger.debug(f"UDI read failed for valid stream IDs, falling back: {e}")
        
        # Legacy cache behavior
        key = "valid_stream_ids"
        cached = self._get_from_cache(key)
        
        if cached is not None:
            return cached
        
        # Cache miss - compute from streams
        all_streams = self.get_streams(fetch_func)
        valid_ids = {stream['id'] for stream in all_streams if isinstance(stream, dict) and 'id' in stream}
        
        self._set_in_cache(key, valid_ids)
        
        return valid_ids
    
    def get_stream_id_to_url_mapping(self, fetch_func) -> Dict[int, str]:
        """
        Get mapping of stream IDs to URLs from cache or compute if not cached.
        
        In v2.0, this method first tries to get data from the Unified Data Index.
        
        Args:
            fetch_func: Function to call to get all streams if not cached
                       Should return List[Dict[str, Any]]
        
        Returns:
            Dictionary mapping stream IDs to URLs
        """
        # Try Unified Data Index first
        if UDI_AVAILABLE:
            try:
                udi = get_unified_data_index()
                udi_stats = udi.get_stats()
                if udi_stats.get('streams', 0) > 0:
                    mapping = udi.get_stream_url_mapping()
                    if mapping:
                        logger.debug(f"Retrieved stream URL mapping ({len(mapping)} entries) from UDI")
                        return mapping
            except Exception as e:
                logger.debug(f"UDI read failed for stream URL mapping, falling back: {e}")
        
        # Legacy cache behavior
        key = "stream_id_to_url"
        cached = self._get_from_cache(key)
        
        if cached is not None:
            return cached
        
        # Cache miss - compute from streams
        all_streams = self.get_streams(fetch_func)
        mapping = {s['id']: s.get('url') for s in all_streams if isinstance(s, dict) and 'id' in s}
        
        self._set_in_cache(key, mapping)
        
        return mapping
    
    def get_stats(self) -> Dict[str, Any]:
        """
        Get cache statistics.
        
        In v2.0, includes statistics from both the legacy cache and UDI.
        
        Returns:
            Dictionary with cache statistics
        """
        with self._lock:
            stats = {
                "enabled": self._enabled,
                "entries": len(self._cache_data),
                "keys": list(self._cache_data.keys()),
                "max_age": self.max_age,
                "context_depth": self._context_depth,
                "udi_available": UDI_AVAILABLE
            }
            
            # Add UDI stats if available
            if UDI_AVAILABLE:
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
            Dict with counts of synced entities, or None if UDI not available
        """
        if not UDI_AVAILABLE:
            logger.warning("Cannot rebuild index: Unified Data Index not available")
            return None
        
        try:
            from dispatcharr_sync_service import get_dispatcharr_sync_service
            sync_service = get_dispatcharr_sync_service()
            counts = sync_service.rebuild_index()
            
            # Invalidate legacy cache after rebuild
            self.invalidate()
            
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
