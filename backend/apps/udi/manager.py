"""
Universal Data Index (UDI) Manager - Single source of truth for all Dispatcharr data.

The UDIManager is a singleton class that:
- Manages all data access for channels, streams, groups, logos, and M3U accounts
- Provides cached data with configurable TTL
- Supports background refresh
- Handles data persistence via JSON storage

Usage:
    from apps.udi import get_udi_manager
    
    udi = get_udi_manager()
    
    # Initialize on startup (fetches all data)
    udi.initialize()
    
    # Get data (from cache)
    channels = udi.get_channels()
    streams = udi.get_streams()
    
    # Force refresh
    udi.refresh_all()
"""

import threading
import time
import requests
from datetime import datetime
from typing import Dict, List, Optional, Any, Set, Tuple

from apps.udi.storage import UDIStorage
from apps.udi.fetcher import UDIFetcher, FetchResult, _get_auth_headers
from apps.udi.cache import UDICache

from apps.core.logging_config import setup_logging

# Import at module level for better performance
from apps.config.dispatcharr_config import get_dispatcharr_config

logger = setup_logging(__name__)

# Constants for channel status
CHANNEL_STATE_ACTIVE = 'active'

# Minimum fraction of expected records that must be received before a fetch is
# considered potentially truncated.  0.9 means "warn if we received less than
# 90 % of what Dispatcharr told us to expect."  None / 0 disables the check.
UDI_INTEGRITY_THRESHOLD = 0.9


def _check_fetch_integrity(entity: str, result: FetchResult) -> bool:
    """Evaluate whether a FetchResult looks complete.

    Logs a warning when the received count falls below the expected count by
    more than UDI_INTEGRITY_THRESHOLD.  Returns True when the result appears
    complete (or when expected_count is unknown).

    Args:
        entity:  Human-readable name used in log messages (e.g. "streams").
        result:  The FetchResult to evaluate.

    Returns:
        True if integrity check passes or cannot be determined, False if the
        result is detectably incomplete.
    """
    if result.expected_count is None:
        # Endpoint did not report a total — cannot check.
        return True

    received = len(result)
    expected = result.expected_count

    if expected == 0:
        # Dispatcharr says zero records exist — that is valid.
        return True

    ratio = received / expected
    if ratio < UDI_INTEGRITY_THRESHOLD:
        logger.warning(
            f"UDI integrity check FAILED for {entity}: "
            f"received {received} of {expected} expected records "
            f"({ratio * 100:.1f}% — threshold {UDI_INTEGRITY_THRESHOLD * 100:.0f}%)"
        )
        return False

    if received != expected:
        # Within threshold but not an exact match — log at debug level only.
        logger.debug(
            f"UDI integrity minor variance for {entity}: "
            f"received {received}, expected {expected}"
        )

    return True


class UDIManager:
    """
    Universal Data Index Manager - Singleton class for all Dispatcharr data access.
    
    This class provides:
    - Centralized data access for all Dispatcharr entities
    - Automatic cache management with configurable TTL
    - Background refresh capability
    - Thread-safe operations
    """
    
    def __init__(self):
        """Initialize the UDI Manager with SQL backend storage."""
        from apps.udi.storage import UDIStorage
        self.storage = UDIStorage()
        logger.info("Using SQL backend storage for UDI")
        
        self.fetcher = UDIFetcher()
        self.cache = UDICache()
        
        self._initialized = False
        self._lock = threading.Lock()
        self._refresh_thread = None
        self._refresh_running = False
        
        # In-memory caches for faster access
        self._channels_cache: List[Dict[str, Any]] = []
        self._streams_cache: List[Dict[str, Any]] = []
        self._channel_groups_cache: List[Dict[str, Any]] = []
        self._logos_cache: List[Dict[str, Any]] = []
        self._m3u_accounts_cache: List[Dict[str, Any]] = []
        self._channel_profiles_cache: List[Dict[str, Any]] = []
        self._profile_channels_cache: Dict[int, Dict[str, Any]] = {}
        
        # Index caches for fast lookups
        self._channels_by_id: Dict[int, Dict[str, Any]] = {}
        self._streams_by_id: Dict[int, Dict[str, Any]] = {}
        self._streams_by_url: Dict[str, Dict[str, Any]] = {}
        self._valid_stream_ids: Set[int] = set()
        self._profiles_by_id: Dict[int, Dict[str, Any]] = {}
        
        # Initialization progress tracking
        self._init_progress = {
            'status': 'idle',       # idle, in_progress, completed, failed
            'percentage': 0,
            'message': '',
            'current_step': '',
            'total_steps': 7,       # channels, streams, groups, logos, m3u_accounts, profiles, profile_channels
            # entity_counts is populated after each full refresh_all() run.
            # Each entry: { 'received': int, 'expected': int | None }
            # 'expected' is None when the Dispatcharr endpoint did not report a total.
            'entity_counts': {},
        }
        
        # Proxy status cache for real-time stream viewer information
        self._proxy_status_cache: Dict[str, Any] = {}
        self._proxy_status_last_fetch: float = 0
        self._proxy_status_ttl: float = 1.0  # Cache proxy status for 1 second to match FFmpeg stats update frequency
        
        logger.info("UDI Manager created")
    
    def initialize(self, force_refresh: bool = False) -> bool:
        """
        Initialize the UDI Manager by loading or fetching all data.
        
        This should be called on application startup. It will:
        1. Load existing data from storage if available
        2. Fetch fresh data from the API if storage is empty or force_refresh is True
        
        Args:
            force_refresh: If True, always fetch fresh data from API
            
        Returns:
            True if initialization successful
        """
        # 1. State check with lock
        with self._lock:
            if self._initialized and not force_refresh:
                logger.debug("UDI Manager already initialized")
                return True
                
            if getattr(self, '_refresh_running', False):
                logger.debug("UDI Manager refresh already running in another thread")
                return True
            
            logger.debug("Initializing UDI Manager...")
            
            # Check if we have existing data
            if not force_refresh and self.storage.is_initialized():
                logger.debug("Loading existing data from storage...")
                self._load_from_storage()
                
                # Check if we actually loaded complete data to prevent initializing with missing accounts/streams
                if len(self._channels_cache) > 0 and len(self._m3u_accounts_cache) > 0:
                    self._initialized = True
                    logger.info(f"UDI initialized from storage: {len(self._channels_cache)} channels, {len(self._m3u_accounts_cache)} accounts")
                    return True
                else:
                    logger.info(f"UDI storage loaded but incomplete ({len(self._channels_cache)} channels, {len(self._m3u_accounts_cache)} accounts). Falling back to API fetch...")
            
            # Check if Dispatcharr is configured before fetching from API
            config = get_dispatcharr_config()
            if not config.is_configured():
                logger.warning("Cannot fetch data from API: Dispatcharr credentials not configured")
                # Mark as initialized with empty data to prevent repeated attempts
                self._initialized = True
                return False
            
            # Mark as refreshing to prevent re-entrancy
            self._refresh_running = True
            
        # 2. Fetch fresh data from API (OUTSIDE Lock)
        logger.debug("Fetching fresh data from API...")
        try:
            success = self.refresh_all()
            with self._lock:
                if success:
                    self._initialized = True
                    # Success message logged in refresh_all()
                return success
        except Exception as e:
            logger.error(f"Error initializing UDI Manager: {e}")
            return False
        finally:
            with self._lock:
                self._refresh_running = False
    
    def _load_from_storage(self) -> None:
        """Load all data from storage into memory caches."""
        self._channels_cache = self.storage.load_channels()
        self._streams_cache = self.storage.load_streams()
        self._channel_groups_cache = self.storage.load_channel_groups()
        self._logos_cache = self.storage.load_logos()
        self._m3u_accounts_cache = self.storage.load_m3u_accounts()
        self._channel_profiles_cache = self.storage.load_channel_profiles()
        self._profile_channels_cache = self.storage.load_profile_channels()
        
        # Build index caches
        self._build_indexes()
        
        # Mark caches as refreshed based on storage metadata
        metadata = self.storage.load_metadata()
        for entity_type in ['channels', 'streams', 'channel_groups', 'logos', 'm3u_accounts', 'channel_profiles']:
            timestamp_str = metadata.get(f'{entity_type}_last_updated')
            if timestamp_str:
                try:
                    timestamp = datetime.fromisoformat(timestamp_str)
                    self.cache.mark_refreshed(entity_type, timestamp)
                except ValueError:
                    pass
        
        # Also mark profile_channels if present
        profile_channels_timestamp = metadata.get('profile_channels_last_updated')
        if profile_channels_timestamp:
            try:
                timestamp = datetime.fromisoformat(profile_channels_timestamp)
                self.cache.mark_refreshed('profile_channels', timestamp)
            except ValueError:
                pass
        
        logger.info(
            f"Loaded from storage: {len(self._channels_cache)} channels, "
            f"{len(self._streams_cache)} streams, {len(self._channel_groups_cache)} groups, "
            f"{len(self._logos_cache)} logos, {len(self._m3u_accounts_cache)} M3U accounts, "
            f"{len(self._channel_profiles_cache)} profiles, "
            f"{len(self._profile_channels_cache)} profile channels"
        )
    
    def _build_indexes(self) -> None:
        """Build index caches for fast lookups."""
        self._channels_by_id = {ch.get('id'): ch for ch in self._channels_cache if ch.get('id') is not None}
        self._streams_by_id = {st.get('id'): st for st in self._streams_cache if st.get('id') is not None}
        self._streams_by_url = {st.get('url'): st for st in self._streams_cache if st.get('url')}
        self._valid_stream_ids = set(self._streams_by_id.keys())
        self._profiles_by_id = {p.get('id'): p for p in self._channel_profiles_cache if p.get('id') is not None}
    
    # === Data Access Methods ===
    
    def get_init_progress(self) -> Dict[str, Any]:
        """Get the current initialization progress.
        
        Returns:
            Dictionary with status, percentage, message, and entity_counts.
        """
        return self._init_progress.copy()
    
    def _update_init_progress(
        self,
        status: str = None,
        percentage: int = None,
        message: str = None,
        current_step: str = None,
        entity_counts: Dict[str, Any] = None,
    ):
        """Update the initialization progress.
        
        Args:
            status:        New status (idle, in_progress, completed, failed)
            percentage:    Progress percentage (0-100)
            message:       Progress message
            current_step:  Name of the current step
            entity_counts: Dict of entity -> {received, expected} counts from
                           the most recent full refresh.  Merged into the
                           existing dict so partial updates are safe.
        """
        if status:
            self._init_progress['status'] = status
        if percentage is not None:
            self._init_progress['percentage'] = percentage
        if message:
            self._init_progress['message'] = message
        if current_step:
            self._init_progress['current_step'] = current_step
        if entity_counts is not None:
            self._init_progress['entity_counts'].update(entity_counts)
            
        logger.debug(f"UDI Init Progress: {self._init_progress['percentage']}% - {self._init_progress['message']}")

    def is_initialized(self) -> bool:
        """Check if the UDI Manager has been initialized.
        
        Returns:
            True if initialized, False otherwise
        """
        return self._initialized
    
    def get_channels(self) -> List[Dict[str, Any]]:
        """Get all channels.
        
        Returns:
            List of channel dictionaries
        """
        self._ensure_initialized()
        return self._channels_cache.copy()
    
    def get_channel_by_id(self, channel_id: int, fetch_if_missing: bool = True) -> Optional[Dict[str, Any]]:
        """Get a specific channel by ID.
        
        If the channel is not in the cache and fetch_if_missing is True,
        attempts to fetch it from the API and add it to the cache.
        
        Args:
            channel_id: The channel ID
            fetch_if_missing: If True, fetch from API when not in cache (default: True)
            
        Returns:
            Channel dictionary or None if not found
        """
        self._ensure_initialized()
        channel = self._channels_by_id.get(channel_id)
        
        if channel is None and fetch_if_missing:
            # Channel not in cache, try fetching from API
            logger.debug(f"Channel {channel_id} not in cache, fetching from API")
            try:
                # Fetch channel from Dispatcharr API (returns channel dict or None)
                channel = self.fetcher.fetch_channel_by_id(channel_id)
                if channel:
                    # Add to caches under lock to ensure thread safety
                    with self._lock:
                        # Only add if still not in cache (could have been added by another thread)
                        if channel_id not in self._channels_by_id:
                            self._channels_by_id[channel_id] = channel
                            self._channels_cache.append(channel)
                        else:
                            # Already in cache, use the cached version
                            channel = self._channels_by_id[channel_id]
                    logger.info(f"Fetched and cached channel {channel_id}")
            except Exception as e:
                logger.warning(f"Failed to fetch channel {channel_id} from API: {e}")
                channel = None
        
        return channel
    
    def get_channel_streams(self, channel_id: int) -> List[Dict[str, Any]]:
        """Get streams for a specific channel.
        
        Args:
            channel_id: The channel ID
            
        Returns:
            List of stream dictionaries for the channel
        """
        self._ensure_initialized()
        channel = self._channels_by_id.get(channel_id)
        if not channel:
            return []
        
        stream_ids = channel.get('streams', [])
        return [self._streams_by_id.get(sid) for sid in stream_ids if sid in self._streams_by_id]
    
    def get_streams(self, log_result: bool = True) -> List[Dict[str, Any]]:
        """Get all streams.
        
        Args:
            log_result: Whether to log the number of streams returned
            
        Returns:
            List of stream dictionaries
        """
        self._ensure_initialized()
        if log_result:
            logger.debug(f"Returning {len(self._streams_cache)} streams from UDI")
        return self._streams_cache.copy()
    
    def get_stream_by_id(self, stream_id: int) -> Optional[Dict[str, Any]]:
        """Get a specific stream by ID.
        
        Args:
            stream_id: The stream ID
            
        Returns:
            Stream dictionary or None if not found
        """
        self._ensure_initialized()
        return self._streams_by_id.get(stream_id)
    
    def get_stream_by_url(self, url: str) -> Optional[Dict[str, Any]]:
        """Get a specific stream by URL.
        
        Args:
            url: The stream URL
            
        Returns:
            Stream dictionary or None if not found
        """
        self._ensure_initialized()
        return self._streams_by_url.get(url)
    
    def get_valid_stream_ids(self) -> Set[int]:
        """Get a set of all valid stream IDs.
        
        Returns:
            Set of valid stream IDs
        """
        self._ensure_initialized()
        return self._valid_stream_ids.copy()
    
    def get_channel_groups(self) -> List[Dict[str, Any]]:
        """Get all channel groups that have associated channels.
        
        Only returns groups where channel_count > 0 to avoid cluttering
        the Group Management UI.
        
        Returns:
            List of channel group dictionaries with channels
        """
        self._ensure_initialized()
        # Filter out groups with no channels
        return [
            group for group in self._channel_groups_cache 
            if group.get('channel_count', 0) > 0
        ]
    
    def get_channel_group_by_id(self, group_id: int) -> Optional[Dict[str, Any]]:
        """Get a specific channel group by ID.
        
        Args:
            group_id: The channel group ID
            
        Returns:
            Channel group dictionary or None if not found
        """
        self._ensure_initialized()
        for group in self._channel_groups_cache:
            if group.get('id') == group_id:
                return group
        return None
    
    def get_channels_by_group(self, group_id: int) -> Optional[List[Dict[str, Any]]]:
        """Get all channels that belong to a specific channel group.
        
        Args:
            group_id: The channel group ID
            
        Returns:
            List of channel dictionaries or None if group not found
        """
        self._ensure_initialized()
        
        # Verify group exists
        group = self.get_channel_group_by_id(group_id)
        if not group:
            return None
        
        # Filter channels by group
        channels = [
            channel for channel in self._channels_cache
            if channel.get('channel_group_id') == group_id
        ]
        return channels
    
    def get_logos(self) -> List[Dict[str, Any]]:
        """Get all logos.
        
        Returns:
            List of logo dictionaries
        """
        self._ensure_initialized()
        return self._logos_cache.copy()
    
    def get_logo_by_id(self, logo_id: int) -> Optional[Dict[str, Any]]:
        """Get a specific logo by ID.
        
        Args:
            logo_id: The logo ID
            
        Returns:
            Logo dictionary or None if not found
        """
        self._ensure_initialized()
        for logo in self._logos_cache:
            if logo.get('id') == logo_id:
                return logo
        return None
    
    def get_m3u_accounts(self) -> List[Dict[str, Any]]:
        """Get all M3U accounts with priority_mode merged from local config.
        
        Returns:
            List of M3U account dictionaries with priority_mode included
        """
        self._ensure_initialized()
        logger.debug(f"Returning {len(self._m3u_accounts_cache)} M3U accounts from UDI cache")
        accounts = self._m3u_accounts_cache.copy()
        
        return accounts
    
    def get_m3u_account_by_id(self, account_id: int) -> Optional[Dict[str, Any]]:
        """Get a specific M3U account by ID with priority_mode merged.
        
        Args:
            account_id: M3U account ID
            
        Returns:
            M3U account dictionary or None if not found
        """
        self._ensure_initialized()
        
        # Try fast lookup from storage if using Redis
        if hasattr(self.storage, 'get_m3u_account_by_id'):
            account = self.storage.get_m3u_account_by_id(account_id)
            if account:
                return account
        
        # Fallback to in-memory cache
        for account in self._m3u_accounts_cache:
            if account.get('id') == account_id:
                return account.copy()
        
        logger.debug(f"M3U account {account_id} not found in UDI")
        return None
    
    def get_channel_profiles(self) -> List[Dict[str, Any]]:
        """Get all channel profiles.
        
        Returns:
            List of channel profile dictionaries
        """
        self._ensure_initialized()
        return self._channel_profiles_cache.copy()
    
    def get_channel_profile_by_id(self, profile_id: int) -> Optional[Dict[str, Any]]:
        """Get a specific channel profile by ID.
        
        Args:
            profile_id: The profile ID
            
        Returns:
            Profile dictionary or None if not found
        """
        self._ensure_initialized()
        return self._profiles_by_id.get(profile_id)
    
    def get_profile_channels(self, profile_id: int) -> Optional[Dict[str, Any]]:
        """Get channel associations for a specific profile.
        
        Args:
            profile_id: The profile ID
            
        Returns:
            Profile channel data or None if not cached
        """
        self._ensure_initialized()
        return self._profile_channels_cache.get(profile_id)
    
    def has_custom_streams(self) -> bool:
        """Check if any custom streams exist.
        
        Returns:
            True if at least one custom stream exists
        """
        self._ensure_initialized()
        return any(st.get('is_custom', False) for st in self._streams_cache)
    
    # === Refresh Methods ===
    
    def refresh_all(self) -> bool:
        """Refresh all data from the API.
        
        Returns:
            True if refresh successful
        """
        logger.debug("Refreshing all UDI data...")
        self._update_init_progress(status='in_progress', percentage=0, message='Starting refresh...', current_step='start')
        
        # Check if Dispatcharr is configured before attempting API calls
        config = get_dispatcharr_config()
        if not config.is_configured():
            logger.warning("Cannot refresh data: Dispatcharr credentials not configured")
            self._update_init_progress(status='failed', message='Dispatcharr not configured')
            return False
        
        try:
            # Pre-fetch step: get authoritative counts from lightweight /ids/
            # endpoints before pulling full objects.  These counts serve as the
            # integrity oracle when the pagination envelope does not include a
            # 'count' field (e.g. ChannelPagination in older Dispatcharr builds).
            self._update_init_progress(percentage=2, message='Fetching entity counts...', current_step='pre_count')
            pre_counts = self.fetcher.fetch_entity_counts()
            logger.info(
                f"UDI pre-fetch oracle: channels={pre_counts.get('channels')}, "
                f"streams={pre_counts.get('streams')} (from Dispatcharr /ids/ endpoints)"
            )

            # Step 1: Channels
            self._update_init_progress(percentage=5, message='Fetching channels...', current_step='channels')
            channels_result = self.fetcher.fetch_channels()
            # If pagination envelope omitted 'count', fall back to the /ids/ oracle
            if channels_result.expected_count is None and pre_counts.get('channels') is not None:
                channels_result.expected_count = pre_counts['channels']
                logger.info(f"UDI integrity: using /ids/ oracle for channels — expected {channels_result.expected_count}")
            channels_ok = _check_fetch_integrity('channels', channels_result)

            # Step 2: Streams
            self._update_init_progress(percentage=20, message='Fetching streams...', current_step='streams')
            streams_result = self.fetcher.fetch_streams()
            # If pagination envelope omitted 'count', fall back to the /ids/ oracle
            if streams_result.expected_count is None and pre_counts.get('streams') is not None:
                streams_result.expected_count = pre_counts['streams']
                logger.info(f"UDI integrity: using /ids/ oracle for streams — expected {streams_result.expected_count}")
            streams_ok = _check_fetch_integrity('streams', streams_result)

            # Step 3: Channel Groups (non-paginated — no integrity check)
            self._update_init_progress(percentage=40, message='Fetching groups...', current_step='groups')
            channel_groups = self.fetcher.fetch_channel_groups()

            # Step 4: Logos
            self._update_init_progress(percentage=50, message='Fetching logos...', current_step='logos')
            logos_result = self.fetcher.fetch_logos()
            _check_fetch_integrity('logos', logos_result)

            # Step 5: M3U Accounts (non-paginated — no integrity check)
            self._update_init_progress(percentage=60, message='Fetching M3U accounts...', current_step='m3u_accounts')
            m3u_accounts = self.fetcher.fetch_m3u_accounts()

            # Step 6: Channel Profiles (non-paginated — no integrity check)
            self._update_init_progress(percentage=70, message='Fetching profiles...', current_step='profiles')
            channel_profiles = self.fetcher.fetch_channel_profiles()

            # Step 7: Profile Channels
            profile_ids = [p.get('id') for p in channel_profiles if p.get('id')]
            if profile_ids:
                logger.debug(f"Fetching channel data for {len(profile_ids)} profiles...")
                
                def profile_progress(i, total, msg):
                    # Progress between 80% and 90%
                    percentage = 80 + int((i / total) * 10)
                    self._update_init_progress(percentage=percentage, message=msg)
                
                profile_channels = self.fetcher.fetch_profile_channels(profile_ids, progress_callback=profile_progress)
            else:
                profile_channels = {}

            # Build entity_counts for the progress dict — used by the frontend
            # stats panel and the future unstable-state check.
            # 'expected' reflects the best oracle available: pagination envelope
            # count if present, otherwise the pre-fetch /ids/ count.
            entity_counts = {
                'channels': {
                    'received': len(channels_result),
                    'expected': channels_result.expected_count,
                },
                'streams': {
                    'received': len(streams_result),
                    'expected': streams_result.expected_count,
                },
                'logos': {
                    'received': len(logos_result),
                    'expected': logos_result.expected_count,
                },
                'm3u_accounts': {
                    'received': len(m3u_accounts),
                    'expected': None,  # non-paginated, no oracle available
                },
            }

            self._update_init_progress(percentage=90, message='Building indexes and saving...', current_step='finalize')
            
            with self._lock:
                # Unpack .items from FetchResult objects for cache storage
                self._channels_cache = channels_result.items
                self._streams_cache = streams_result.items
                self._channel_groups_cache = channel_groups
                self._logos_cache = logos_result.items
                self._m3u_accounts_cache = m3u_accounts
                self._channel_profiles_cache = channel_profiles
                self._profile_channels_cache = profile_channels
                
                # Build index caches
                self._build_indexes()
            
            # Save to storage
            self.storage.save_channels(self._channels_cache)
            self.storage.save_streams(self._streams_cache)
            self.storage.save_channel_groups(self._channel_groups_cache)
            self.storage.save_logos(self._logos_cache)
            self.storage.save_m3u_accounts(self._m3u_accounts_cache)
            self.storage.save_channel_profiles(self._channel_profiles_cache)
            self.storage.save_profile_channels(self._profile_channels_cache)
            
            # Update metadata
            now = datetime.now()
            metadata = {
                'last_full_refresh': now.isoformat(),
                'channels_last_updated': now.isoformat(),
                'streams_last_updated': now.isoformat(),
                'channel_groups_last_updated': now.isoformat(),
                'logos_last_updated': now.isoformat(),
                'm3u_accounts_last_updated': now.isoformat(),
                'channel_profiles_last_updated': now.isoformat(),
                'profile_channels_last_updated': now.isoformat(),
                'version': '1.0.0'
            }
            self.storage.save_metadata(metadata)
            
            # Mark all caches as refreshed
            for entity_type in ['channels', 'streams', 'channel_groups', 'logos', 'm3u_accounts', 'channel_profiles', 'profile_channels']:
                self.cache.mark_refreshed(entity_type, now)
            
            logger.info(
                f"UDI initialized: {len(self._channels_cache)} channels, {len(self._streams_cache)} streams, "
                f"{len(self._channel_groups_cache)} groups, {len(self._m3u_accounts_cache)} M3U accounts, "
                f"{len(self._channel_profiles_cache)} profiles"
            )

            # Surface integrity warnings in the progress status if any check failed.
            # Phase 3 (unstable state) will act on these; for now we log and expose.
            integrity_ok = channels_ok and streams_ok
            completion_message = (
                'Initialization complete'
                if integrity_ok
                else 'Initialization complete (integrity warnings — check logs)'
            )

            self._update_init_progress(
                status='completed',
                percentage=100,
                message=completion_message,
                current_step='done',
                entity_counts=entity_counts,
            )
            return True
            
        except Exception as e:
            logger.error(f"Error refreshing UDI data: {e}")
            self._update_init_progress(status='failed', message=f'Refresh failed: {str(e)}')
            return False
    
    def refresh_channels(self) -> bool:
        """Refresh only channels data.
        
        Returns:
            True if refresh successful
        """
        # Check if Dispatcharr is configured
        config = get_dispatcharr_config()
        if not config.is_configured():
            logger.warning("Cannot refresh channels: Dispatcharr credentials not configured")
            return False
        
        logger.info("Refreshing channels...")
        try:
            result = self.fetcher.fetch_channels()
            # FetchResult supports len() and iteration — assign .items to cache
            self._channels_cache = result.items
            self._channels_by_id = {ch.get('id'): ch for ch in result.items if ch.get('id') is not None}
            self.storage.save_channels(result.items)
            self.cache.mark_refreshed('channels')
            return True
        except Exception as e:
            logger.error(f"Error refreshing channels: {e}")
            return False
    
    def refresh_channel_by_id(self, channel_id: int) -> bool:
        """Refresh a single channel by ID from the API.
        
        This is more efficient than refreshing all channels when only one channel
        needs to be updated (e.g., after modifying its stream list).
        
        Args:
            channel_id: The channel ID to refresh
            
        Returns:
            True if refresh successful
        """
        logger.debug(f"Refreshing channel {channel_id}...")
        try:
            channel = self.fetcher.fetch_channel_by_id(channel_id)
            if channel:
                # Update in-memory caches
                with self._lock:
                    self._channels_by_id[channel_id] = channel
                    
                    # Update list cache
                    found = False
                    for i, ch in enumerate(self._channels_cache):
                        if ch.get('id') == channel_id:
                            self._channels_cache[i] = channel
                            found = True
                            break
                    
                    if not found:
                        self._channels_cache.append(channel)
                    
                    # Update storage
                    self.storage.update_channel(channel_id, channel)
                
                logger.debug(f"Channel {channel_id} refreshed successfully")
                return True
            else:
                logger.warning(f"Failed to refresh channel {channel_id}: channel not found")
                return False
        except Exception as e:
            logger.error(f"Error refreshing channel {channel_id}: {e}")
            return False
    
    def refresh_streams(self) -> bool:
        """Refresh only streams data.
        
        Returns:
            True if refresh successful
        """
        logger.info("Refreshing streams...")
        try:
            result = self.fetcher.fetch_streams()
            self._streams_cache = result.items
            self._streams_by_id = {st.get('id'): st for st in result.items if st.get('id') is not None}
            self._streams_by_url = {st.get('url'): st for st in result.items if st.get('url')}
            self._valid_stream_ids = set(self._streams_by_id.keys())
            self.storage.save_streams(result.items)
            self.cache.mark_refreshed('streams')
            return True
        except Exception as e:
            logger.error(f"Error refreshing streams: {e}")
            return False
    
    def refresh_channel_groups(self) -> bool:
        """Refresh only channel groups data.
        
        Returns:
            True if refresh successful
        """
        logger.info("Refreshing channel groups...")
        try:
            groups = self.fetcher.fetch_channel_groups()
            self._channel_groups_cache = groups
            self.storage.save_channel_groups(groups)
            self.cache.mark_refreshed('channel_groups')
            return True
        except Exception as e:
            logger.error(f"Error refreshing channel groups: {e}")
            return False
    
    def refresh_m3u_accounts(self) -> bool:
        """Refresh only M3U accounts data.
        
        Returns:
            True if refresh successful
        """
        logger.info("Refreshing M3U accounts...")
        try:
            accounts = self.fetcher.fetch_m3u_accounts()
            self._m3u_accounts_cache = accounts
            self.storage.save_m3u_accounts(accounts)
            self.cache.mark_refreshed('m3u_accounts')
            return True
        except Exception as e:
            logger.error(f"Error refreshing M3U accounts: {e}")
            return False
    
    def refresh_channel_profiles(self) -> bool:
        """Refresh only channel profiles data and their channel associations.
        
        Returns:
            True if refresh successful
        """
        logger.info("Refreshing channel profiles...")
        try:
            profiles = self.fetcher.fetch_channel_profiles()
            self._channel_profiles_cache = profiles
            self._profiles_by_id = {p.get('id'): p for p in profiles if p.get('id') is not None}
            if hasattr(self.storage, 'save_channel_profiles'):
                self.storage.save_channel_profiles(profiles)
            self.cache.mark_refreshed('channel_profiles')
            
            # Also refresh profile channel associations
            profile_ids = [p.get('id') for p in profiles if p.get('id')]
            if profile_ids:
                logger.info(f"Fetching channel data for {len(profile_ids)} profiles...")
                self._profile_channels_cache = self.fetcher.fetch_profile_channels(profile_ids)
                if hasattr(self.storage, 'save_profile_channels'):
                    self.storage.save_profile_channels(self._profile_channels_cache)
                self.cache.mark_refreshed('profile_channels')
            
            return True
        except Exception as e:
            logger.error(f"Error refreshing channel profiles: {e}")
            return False
    
    def invalidate_cache(self, entity_type: Optional[str] = None) -> None:
        """Invalidate cache for entity type(s).
        
        Args:
            entity_type: Specific type to invalidate, or None for all
        """
        if entity_type:
            self.cache.invalidate(entity_type)
        else:
            self.cache.invalidate_all()
    
    # === Background Refresh ===
    
    def start_background_refresh(self, interval_seconds: int = 300) -> None:
        """Start background refresh thread.
        
        Args:
            interval_seconds: Seconds between refresh cycles
        """
        if self._refresh_running:
            logger.warning("Background refresh already running")
            return
        
        self._refresh_running = True
        
        def refresh_loop():
            logger.info(f"Starting background refresh (interval: {interval_seconds}s)")
            while self._refresh_running:
                time.sleep(interval_seconds)
                if self._refresh_running:
                    try:
                        # Refresh data that needs updating based on TTL
                        for entity_type in ['channels', 'streams', 'channel_groups', 'logos', 'm3u_accounts', 'channel_profiles']:
                            if self.cache.needs_refresh(entity_type):
                                getattr(self, f'refresh_{entity_type}')()
                    except Exception as e:
                        logger.error(f"Error in background refresh: {e}")
            logger.info("Background refresh stopped")
        
        self._refresh_thread = threading.Thread(target=refresh_loop, daemon=True)
        self._refresh_thread.start()
        logger.info("Background refresh thread started")
    
    def stop_background_refresh(self) -> None:
        """Stop background refresh thread."""
        if self._refresh_running:
            self._refresh_running = False
            if self._refresh_thread:
                self._refresh_thread.join(timeout=5)
            logger.info("Background refresh stopped")
    
    # === Update Methods (for write-through) ===
    
    def update_channel(self, channel_id: int, channel_data: Dict[str, Any]) -> bool:
        """Update a channel in the cache.
        
        This is called after a successful API update to keep the cache in sync.
        
        Args:
            channel_id: The channel ID
            channel_data: The updated channel data
            
        Returns:
            True if successful
        """
        with self._lock:
            # Update in-memory cache
            self._channels_by_id[channel_id] = channel_data
            
            # Update list cache
            for i, ch in enumerate(self._channels_cache):
                if ch.get('id') == channel_id:
                    self._channels_cache[i] = channel_data
                    break
            else:
                self._channels_cache.append(channel_data)
            
            # Save to storage
            return self.storage.update_channel(channel_id, channel_data)
    
    def update_stream(self, stream_id: int, stream_data: Dict[str, Any]) -> bool:
        """Update a stream in the cache.
        
        This is called after a successful API update to keep the cache in sync.
        
        Args:
            stream_id: The stream ID
            stream_data: The updated stream data
            
        Returns:
            True if successful
        """
        with self._lock:
            # Update in-memory caches
            self._streams_by_id[stream_id] = stream_data
            if stream_data.get('url'):
                self._streams_by_url[stream_data['url']] = stream_data
            
            # Update list cache
            for i, st in enumerate(self._streams_cache):
                if st.get('id') == stream_id:
                    self._streams_cache[i] = stream_data
                    break
            else:
                self._streams_cache.append(stream_data)
                self._valid_stream_ids.add(stream_id)
            
            # Save to storage
            return self.storage.update_stream(stream_id, stream_data)
    
    def update_profile_channels(self, profile_id: int, profile_channels_data: Dict[str, Any]) -> bool:
        """Update profile channels data in the cache.
        
        This is called after fetching profile channels to keep the cache in sync.
        
        Args:
            profile_id: The profile ID
            profile_channels_data: The profile channels data (dict with 'profile' and 'channels' keys)
            
        Returns:
            True if successful
        """
        with self._lock:
            # Update in-memory cache
            self._profile_channels_cache[profile_id] = profile_channels_data
            
            # Save to storage
            if hasattr(self.storage, 'save_profile_channels_by_id'):
                try:
                    return self.storage.save_profile_channels_by_id(profile_id, profile_channels_data)
                except Exception as e:
                    logger.error(f"Error saving profile channels to storage: {e}")
                    return False
            return True
    
    # === Status Methods ===
    
    def get_status(self) -> Dict[str, Any]:
        """Get the current UDI Manager status.
        
        Returns:
            Dictionary with status information
        """
        return {
            'initialized': self._initialized,
            'background_refresh_running': self._refresh_running,
            'data_counts': {
                'channels': len(self._channels_cache),
                'streams': len(self._streams_cache),
                'channel_groups': len(self._channel_groups_cache),
                'logos': len(self._logos_cache),
                'm3u_accounts': len(self._m3u_accounts_cache),
                'channel_profiles': len(self._channel_profiles_cache)
            },
            'cache_status': self.cache.get_status(),
            'storage_path': str(self.storage.storage_dir)
        }
    
    def get_cache_last_refresh(self, entity_type: str) -> Optional[Any]:
        """Get the last refresh time for a specific entity type from cache.
        
        Args:
            entity_type: The entity type to query (e.g., 'channel_profiles')
            
        Returns:
            The last refresh datetime or None if never refreshed
        """
        return self.cache.get_last_refresh(entity_type)
    
    def get_storage_count(self, entity_type: str) -> int:
        """Get the count of entities in storage for a specific type.
        
        Args:
            entity_type: The entity type to query (e.g., 'channel_profiles')
            
        Returns:
            Count of entities in storage, or 0 on error
        """
        # Mapping of entity types to storage loader methods
        entity_loaders = {
            'channels': self.storage.load_channels,
            'streams': self.storage.load_streams,
            'channel_groups': self.storage.load_channel_groups,
            'logos': self.storage.load_logos,
            'm3u_accounts': self.storage.load_m3u_accounts,
            'channel_profiles': self.storage.load_channel_profiles
        }
        
        loader = entity_loaders.get(entity_type)
        if not loader:
            logger.warning(f"Unknown entity type: {entity_type}")
            return 0
        
        try:
            data = loader()
            return len(data) if data else 0
        except Exception as e:
            logger.error(f"Error getting storage count for {entity_type}: {e}")
            return 0
    
    def is_initialized(self) -> bool:
        """Check if UDI Manager is initialized.
        
        Returns:
            True if initialized
        """
        return self._initialized
    
    def _find_account_for_profile(self, profile_id: int) -> Optional[int]:
        """Find the M3U account ID that contains a specific profile.
        
        Args:
            profile_id: M3U account profile ID
            
        Returns:
            M3U account ID or None if profile not found
        """
        accounts = self.get_m3u_accounts()
        
        for account in accounts:
            profiles = account.get('profiles', [])
            if isinstance(profiles, list):
                for profile in profiles:
                    if isinstance(profile, dict) and profile.get('id') == profile_id:
                        # Return account ID from profile's account_id field if available
                        # Use explicit None check to handle account_id=0 case
                        profile_account_id = profile.get('account_id')
                        if profile_account_id is not None:
                            return profile_account_id
                        # Fallback to parent account's ID
                        return account.get('id')
        
        return None
    
    def _is_channel_status_active(self, status: Dict[str, Any]) -> bool:
        """Check if a channel status indicates it's active.
        
        Args:
            status: Channel status dictionary from proxy
            
        Returns:
            True if channel is active, False otherwise
        """
        if not isinstance(status, dict):
            return False
            
        # Check the 'state' field (newer API format)
        state = status.get('state')
        if state == CHANNEL_STATE_ACTIVE:
            return True
            
        # Check various indicators of activity (legacy formats)
        if status.get('current_stream'):
            return True
        if status.get('active'):
            return True
            
        # Check if there are active clients
        clients = status.get('clients')
        if clients and len(clients) > 0:
            return True
            
        return False
    
    def _get_proxy_status(self, force_refresh: bool = False) -> Dict[str, Any]:
        """Get cached proxy status or fetch fresh if needed.
        
        Args:
            force_refresh: If True, always fetch fresh data
            
        Returns:
            Dictionary with proxy status information
        """
        current_time = time.time()
        
        # Check if cache is valid
        if not force_refresh and self._proxy_status_cache:
            age = current_time - self._proxy_status_last_fetch
            if age < self._proxy_status_ttl:
                logger.debug(f"Using cached proxy status (age: {age:.1f}s)")
                return self._proxy_status_cache
        
        # Fetch fresh data
        try:
            logger.debug("Fetching fresh proxy status")
            proxy_status = self.fetcher.fetch_proxy_status()
            self._proxy_status_cache = proxy_status
            self._proxy_status_last_fetch = current_time
            return proxy_status
        except Exception as e:
            logger.warning(f"Failed to fetch proxy status: {e}")
            # Return cached data even if expired, or empty dict
            return self._proxy_status_cache if self._proxy_status_cache else {}
    
    def _count_active_streams(self, account_id: int) -> int:
        """Count streams with active viewers for an account.
        
        This method uses real-time proxy status from /proxy/ts/status to determine 
        which streams are actually running. It correlates the m3u_profile_id from 
        active channels to find which profiles (and their parent accounts) are in use.
        
        Args:
            account_id: M3U account ID
            
        Returns:
            Number of active streams for this account
        """
        # Get real-time proxy status
        proxy_status = self._get_proxy_status()
        
        # Count active channels that are using profiles from this account
        active_count = 0
        active_profiles = set()
        
        for channel_id_str, status in proxy_status.items():
            if not self._is_channel_status_active(status):
                continue
            
            # Get the m3u_profile_id from the proxy status
            profile_id = status.get('m3u_profile_id')
            if not profile_id:
                logger.debug(f"Channel {channel_id_str} has no m3u_profile_id in proxy status")
                continue
            
            # Find which account owns this profile
            profile_account_id = self._find_account_for_profile(profile_id)
            if profile_account_id is None:
                logger.debug(f"Profile {profile_id} not found in any M3U account")
                continue
            
            # If this profile belongs to the account we're checking, count it
            if profile_account_id == account_id:
                active_count += 1
                active_profiles.add(profile_id)
                profile_name = status.get('m3u_profile_name', f'Profile {profile_id}')
                logger.debug(
                    f"Channel {channel_id_str} is using profile {profile_id} ({profile_name}) "
                    f"from account {account_id}"
                )
        
        logger.debug(
            f"Account {account_id} has {active_count} active streams across "
            f"{len(active_profiles)} profile(s): {sorted(active_profiles)}"
        )
        return active_count
    
    def _sum_total_viewers(self, account_id: int) -> int:
        """Sum all current_viewers for an account.
        
        Args:
            account_id: M3U account ID
            
        Returns:
            Total number of viewers
        """
        total_viewers = 0
        for stream in self._streams_cache:
            if stream.get('m3u_account') == account_id:
                current_viewers = stream.get('current_viewers', 0)
                total_viewers += current_viewers
        return total_viewers
    
    def get_active_streams_for_profile(self, profile_id: int) -> int:
        """Calculate the number of active streams for a specific M3U account profile.
        
        Uses real-time proxy status to count channels that are actively using this profile.
        
        Args:
            profile_id: M3U account profile ID
            
        Returns:
            Number of active streams using this profile
        """
        self._ensure_initialized()
        
        # Find the account that contains this profile
        account_id = self._find_account_for_profile(profile_id)
        
        if not account_id:
            logger.warning(f"Profile {profile_id} not found in any M3U account")
            return 0
        
        # Count active streams for this account
        active_count = self._count_active_streams(account_id)
        logger.debug(f"Profile {profile_id} has {active_count} active streams")
        return active_count
    
    def get_active_streams_for_account(self, account_id: int) -> int:
        """Calculate the number of active streams for an M3U account.
        
        Uses real-time proxy status to count channels that are actively using
        profiles from this account.
        
        Args:
            account_id: M3U account ID
            
        Returns:
            Number of active streams for this account
        """
        self._ensure_initialized()
        
        # Count active streams for this account
        active_count = self._count_active_streams(account_id)
        logger.debug(f"Account {account_id} has {active_count} active streams")
        return active_count
    
    def is_channel_active(self, channel_id: int) -> bool:
        """Check if a channel currently has active viewers.
        
        Uses real-time proxy status to determine if the channel is currently streaming.
        
        Args:
            channel_id: Channel ID to check
            
        Returns:
            True if channel has active viewers, False otherwise
        """
        self._ensure_initialized()
        
        # Get real-time proxy status
        proxy_status = self._get_proxy_status()
        
        # Check if this channel is in the proxy status
        channel_id_str = str(channel_id)
        if channel_id_str in proxy_status:
            status = proxy_status[channel_id_str]
            is_active = self._is_channel_status_active(status)
            logger.debug(f"Channel {channel_id} is {'active' if is_active else 'inactive'} (from proxy status)")
            return is_active
        
        logger.debug(f"Channel {channel_id} is not in proxy status, assuming inactive")
        return False
    
    def get_total_viewers_for_profile(self, profile_id: int) -> int:
        """Calculate the total number of viewers for a specific M3U account profile.
        
        This sums all current_viewers across all streams for the given profile.
        
        Args:
            profile_id: M3U account profile ID
            
        Returns:
            Total number of current viewers
        """
        self._ensure_initialized()
        
        # Find the account that contains this profile
        account_id = self._find_account_for_profile(profile_id)
        
        if not account_id:
            logger.warning(f"Profile {profile_id} not found in any M3U account")
            return 0
        
        # Sum viewers for this account
        total_viewers = self._sum_total_viewers(account_id)
        logger.debug(f"Profile {profile_id} has {total_viewers} total viewers")
        return total_viewers
    
    def get_total_viewers_for_account(self, account_id: int) -> int:
        """Calculate the total number of viewers for an M3U account.
        
        This sums all current_viewers across all streams for the given account.
        
        Args:
            account_id: M3U account ID
            
        Returns:
            Total number of current viewers
        """
        self._ensure_initialized()
        
        # Sum viewers for this account
        total_viewers = self._sum_total_viewers(account_id)
        logger.debug(f"Account {account_id} has {total_viewers} total viewers")
        return total_viewers
    
    def get_active_streams_count_per_profile(self, account_id: int) -> Dict[int, int]:
        """Get the count of active streams for each profile in an account.
        
        Args:
            account_id: M3U account ID
            
        Returns:
            Dictionary mapping profile_id to active stream count
        """
        self._ensure_initialized()
        
        # Get real-time proxy status
        proxy_status = self._get_proxy_status()
        
        # Count active streams per profile
        profile_counts: Dict[int, int] = {}
        
        for channel_id_str, status in proxy_status.items():
            if not self._is_channel_status_active(status):
                continue
            
            # Get the m3u_profile_id from the proxy status
            profile_id = status.get('m3u_profile_id')
            if not profile_id:
                continue
            
            # Find which account owns this profile
            profile_account_id = self._find_account_for_profile(profile_id)
            if profile_account_id != account_id:
                continue
            
            # Increment count for this profile
            profile_counts[profile_id] = profile_counts.get(profile_id, 0) + 1
        
        logger.debug(f"Account {account_id} profile usage: {profile_counts}")
        return profile_counts
    
    def find_available_profile_for_stream(self, stream: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Find an available profile that can serve this stream.
        
        Profiles use search_pattern/replace_pattern to transform stream URLs.
        This method finds a profile from the stream's M3U account that:
        1. Is active
        2. Has available slots (active_count < max_streams)
        3. Can serve this stream (URL pattern matching if needed)
        
        Args:
            stream: Stream dictionary with 'm3u_account' and 'url' fields
            
        Returns:
            Profile dictionary if available, None otherwise
        """
        self._ensure_initialized()
        
        account_id = stream.get('m3u_account')
        stream_id = stream.get('id')
        
        if not account_id:
            logger.debug(f"Stream {stream_id} has no m3u_account")
            return None
        
        # Get the account and its profiles
        account = self.get_m3u_account_by_id(account_id)
        if not account:
            logger.warning(f"Account {account_id} not found for stream {stream_id}")
            return None
        
        account_name = account.get('name', f'Account {account_id}')
        profiles = account.get('profiles', [])
        if not profiles:
            logger.debug(f"Account {account_id} ({account_name}) has no profiles")
            return None
        
        # Get current usage per profile
        profile_usage = self.get_active_streams_count_per_profile(account_id)
        
        logger.debug(
            f"Finding available profile for stream {stream_id} in account {account_id} ({account_name}): "
            f"{len(profiles)} profile(s), current usage: {profile_usage}"
        )
        
        # Find the first available profile
        for profile in profiles:
            if not isinstance(profile, dict):
                continue
            
            profile_id = profile.get('id')
            profile_name = profile.get('name', f'Profile {profile_id}')
            
            if not profile_id:
                continue
            
            # Skip inactive profiles
            if not profile.get('is_active', True):
                logger.debug(f"Profile {profile_id} ({profile_name}) is inactive, skipping")
                continue
            
            # Check if profile has available slots
            max_streams = profile.get('max_streams', 0)
            active_count = profile_usage.get(profile_id, 0)
            
            if max_streams == 0:
                # Unlimited streams
                logger.debug(
                    f"Profile {profile_id} ({profile_name}) has unlimited streams, selecting it for stream {stream_id}"
                )
                return profile
            
            if active_count < max_streams:
                logger.debug(
                    f"Profile {profile_id} ({profile_name}) has {active_count}/{max_streams} active streams, "
                    f"selecting it for stream {stream_id}"
                )
                return profile
            else:
                logger.debug(
                    f"Profile {profile_id} ({profile_name}) is at capacity ({active_count}/{max_streams} streams)"
                )
        
        logger.warning(
            f"No available profile found for stream {stream_id} in account {account_id} ({account_name}). "
            f"All {len(profiles)} profile(s) are either inactive or at capacity."
        )
        return None
    
    def check_stream_can_run(self, stream: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
        """Check if a stream can run based on its M3U account profile availability.
        
        Args:
            stream: Stream dictionary with 'm3u_account' and other fields
            
        Returns:
            Tuple of (can_run: bool, reason: Optional[str])
            - (True, None) if stream can run
            - (False, reason) if stream cannot run with explanation
        """
        self._ensure_initialized()
        
        account_id = stream.get('m3u_account')
        if not account_id:
            # Custom stream without M3U account - can always run
            return (True, None)
        
        # Try to find an available profile
        available_profile = self.find_available_profile_for_stream(stream)
        
        if available_profile:
            return (True, None)
        else:
            account = self.get_m3u_account_by_id(account_id)
            account_name = account.get('name', f'Account {account_id}') if account else f'Account {account_id}'
            return (False, f"All profiles in {account_name} are at capacity")
    
    def apply_profile_url_transformation(self, stream: Dict[str, Any], profile: Optional[Dict[str, Any]] = None) -> str:
        """Apply search/replace pattern transformation to a stream URL.
        
        When using M3U account profiles with search_pattern and replace_pattern,
        this method transforms the stream URL according to the profile configuration.
        This is essential for free profiles that need different URL formats than
        the main account URL.
        
        Args:
            stream: Stream dictionary with 'url' and optionally 'm3u_account'
            profile: Optional profile dictionary. If not provided, will find available profile for stream
            
        Returns:
            Transformed URL string. If no transformation is needed, returns original URL.
        """
        import re
        
        original_url = stream.get('url', '')
        if not original_url:
            return original_url
        
        # If no profile provided, try to find one
        if profile is None:
            profile = self.find_available_profile_for_stream(stream)
        
        # If still no profile, return original URL
        if not profile:
            return original_url
        
        # Get search and replace patterns
        search_pattern = profile.get('search_pattern')
        replace_pattern = profile.get('replace_pattern')
        
        # If patterns are not configured, return original URL
        # Check explicitly for None or empty strings (including whitespace-only strings)
        if not search_pattern or not replace_pattern:
            return original_url
        
        # Strip whitespace and check again
        search_pattern = search_pattern.strip()
        replace_pattern = replace_pattern.strip()
        
        if not search_pattern or not replace_pattern:
            logger.debug(f"Profile {profile.get('id')} has empty search_pattern or replace_pattern after stripping whitespace")
            return original_url
        
        try:
            # First, test if the pattern matches the URL
            # If it doesn't match, don't apply any transformation
            if not re.search(search_pattern, original_url):
                logger.debug(f"Search pattern '{search_pattern}' does not match URL for stream {stream.get('id')}, skipping transformation")
                return original_url
            
            # Convert $1, $2 style backreferences to \1, \2 for Python's re.sub()
            # This handles patterns from other regex engines (e.g., JavaScript, Perl)
            # Maximum supported backreference number (Python regex supports up to 99 groups)
            MAX_BACKREFERENCE_COUNT = 99
            python_replace_pattern = replace_pattern
            # Replace $1, $2, ... $99 with \1, \2, ... \99
            # Start from highest to avoid replacing $10 as $1 + 0
            for i in range(MAX_BACKREFERENCE_COUNT, 0, -1):
                python_replace_pattern = python_replace_pattern.replace(f'${i}', f'\\{i}')
            
            # Apply regex transformation
            transformed_url = re.sub(search_pattern, python_replace_pattern, original_url)
            
            # Validate the transformed URL has a valid protocol
            if not transformed_url.startswith(('http://', 'https://', 'rtmp://', 'rtmps://')):
                logger.error(f"Profile {profile.get('id')} transformation resulted in invalid URL protocol. "
                           f"Original URL preserved. Check search_pattern and replace_pattern configuration.")
                return original_url
            
            if transformed_url != original_url:
                # Log transformation without exposing sensitive URL details
                logger.debug(f"Applied URL transformation for stream {stream.get('id')} using profile {profile.get('id')}")
            
            return transformed_url
        except re.error as e:
            logger.error(f"Invalid regex pattern in profile {profile.get('id')}: {e}")
            return original_url
        except Exception as e:
            logger.error(f"Error applying URL transformation for stream {stream.get('id')}: {e}")
            return original_url
    
    def _ensure_initialized(self) -> None:
        """Ensure UDI Manager is initialized before data access.
        
        This will auto-initialize if not already done, loading from storage
        if available, or fetching from API if configured.
        """
        if not self._initialized:
            # Try to load from storage first if available
            if self.storage.is_initialized():
                logger.info("UDI Manager not initialized, loading from storage...")
                self._load_from_storage()
                self._initialized = True
                logger.info("UDI Manager initialized from storage")
                return
            
            # Check if Dispatcharr is configured before auto-initializing
            config = get_dispatcharr_config()
            
            if not config.is_configured():
                logger.warning("UDI Manager not initialized and Dispatcharr credentials not configured. Skipping auto-initialization.")
                return
            
            logger.info("UDI Manager not initialized, auto-initializing from API...")
            self.initialize()
    
    def get_proxy_status(self) -> Dict[str, Any]:
        """
        Get the current proxy status showing which streams are actively playing.
        
        Returns:
            Dictionary with proxy status information including:
            - channels: List of active channels with stream info
            - count: Number of active channels
        """
        self._ensure_initialized()
        return self._get_proxy_status()
    
    def get_playing_stream_ids(self) -> Set[int]:
        """
        Get the set of stream IDs that are currently being played.
        
        Returns:
            Set of stream IDs currently active in the proxy
        """
        self._ensure_initialized()
        proxy_status = self._get_proxy_status()
        
        playing_stream_ids = set()
        
        # proxy_status is a dict with channel_id -> status mapping
        for channel_id_str, channel_data in proxy_status.items():
            if self._is_channel_status_active(channel_data):
                stream_id = channel_data.get('stream_id')
                if stream_id:
                    playing_stream_ids.add(stream_id)
        
        return playing_stream_ids
    
    def bulk_delete_streams(self, stream_ids: List[int]) -> bool:
        """
        Delete multiple streams from Dispatcharr by their IDs.
        
        Args:
            stream_ids: List of stream IDs to delete
            
        Returns:
            True if deletion successful, False otherwise
        """
        if not stream_ids:
            logger.debug("No stream IDs provided for bulk delete")
            return True
        
        try:
            config = get_dispatcharr_config()
            base_url = config.get_base_url()
            
            if not base_url:
                logger.error("DISPATCHARR_BASE_URL not set, cannot delete streams")
                return False
            
            url = f"{base_url}/api/channels/streams/bulk-delete/"
            headers = _get_auth_headers()
            payload = {"stream_ids": stream_ids}
            
            logger.info(f"Bulk deleting {len(stream_ids)} streams from Dispatcharr")
            response = requests.delete(url, headers=headers, json=payload, timeout=30)
            
            if response.status_code == 204:
                logger.info(f"Successfully deleted {len(stream_ids)} streams from Dispatcharr")
                # Invalidate streams cache after deletion
                self._invalidate_streams_cache()
                return True
            else:
                logger.error(f"Failed to delete streams: HTTP {response.status_code}")
                return False
                
        except Exception as e:
            logger.error(f"Error deleting streams from Dispatcharr: {e}", exc_info=True)
            return False
    
    def _invalidate_streams_cache(self) -> None:
        """Invalidate streams cache after modification operations."""
        with self._lock:
            self._streams_cache = []
            self._streams_by_id = {}
            self._streams_by_url = {}
            self._valid_stream_ids = set()


# Global singleton instance
_udi_manager: Optional[UDIManager] = None
_udi_lock = threading.Lock()


def get_udi_manager() -> UDIManager:
    """Get the global UDI Manager singleton instance.
    
    Returns:
        The UDI Manager instance
    """
    global _udi_manager
    with _udi_lock:
        if _udi_manager is None:
            _udi_manager = UDIManager()
        return _udi_manager
