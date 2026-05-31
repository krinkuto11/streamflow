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

import json
import os
import threading
import time
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Any, Set, Tuple

from apps.udi.fetcher import UDIFetcher, FetchResult
from apps.udi.cache import UDICache
from apps.udi.storage import UDIStorage
from apps.core.auth import _get_auth_headers

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

# When more than this fraction of stream or channel IDs have changed between
# the cached state and Dispatcharr, refresh_delta() falls back to refresh_all().
# 20 % covers large M3U imports while still catching single-item add/delete quickly.
UDI_DELTA_FALLBACK_THRESHOLD = 0.20


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
        logger.warning(
            f"UDI integrity check FAILED for {entity}: "
            f"received {received} of {expected} expected records"
        )
        return False

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
        """Initialize the UDI Manager (pure in-memory, no local persistence)."""
        self.fetcher = UDIFetcher()
        self.cache = UDICache()
        
        self._initialized = False
        self._network_ready = False  # True only after a successful refresh_all() from Dispatcharr network
        self._last_refresh_duration_seconds: float = 0.0  # Duration of last successful refresh_all()
        self._last_refresh_time: Optional[datetime] = None  # Wall-clock time of last successful refresh_all()

        # Automation busy flag — set while a cycle or single-channel check is running.
        # The scheduled UDI refresh worker checks this before firing to avoid
        # replacing the cache mid-pipeline. Cleared before any background sync fires.
        self._automation_busy: bool = False
        self._automation_busy_count: int = 0
        self._automation_busy_lock = threading.Lock()
        self._automation_busy_since: Optional[datetime] = None
        # If busy flag is not cleared within this window (e.g. due to a crash),
        # is_automation_busy() auto-clears it to prevent permanent deadlock.
        self._automation_busy_timeout_seconds: int = 3600

        # Last run timestamp for the scheduled UDI refresh worker.
        # In-memory only — resets on restart, which is correct behaviour.
        self._udi_refresh_last_run: Optional[datetime] = None

        self._lock = threading.Lock()
        self._refresh_thread = None
        self._refresh_running = False       # controls background refresh loop thread
        self._init_in_progress = False      # re-entry guard for initialize()
        
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
        self._channel_groups_by_id: Dict[int, Dict[str, Any]] = {}
        self._logos_by_id: Dict[int, Dict[str, Any]] = {}
        self._m3u_accounts_by_id: Dict[int, Dict[str, Any]] = {}
        # group_id -> [channel, ...] for O(1) get_channels_by_group()
        self._channels_by_group_id: Dict[int, List[Dict[str, Any]]] = {}
        # stream_id -> account_id (derived from stream['m3u_account'])
        self._stream_account_id: Dict[int, int] = {}
        # profile_id -> account_id (derived from M3UAccount.profiles nesting)
        self._profile_to_account_id: Dict[int, int] = {}
        # account_id -> list of stream dicts
        self._streams_by_account_id: Dict[int, List[Dict[str, Any]]] = {}
        # maintained flag so has_custom_streams() is O(1)
        self._has_custom_streams: bool = False
        
        # Initialization progress tracking
        self._init_progress = {
            'status': 'idle',       # idle, in_progress, completed, failed
            'percentage': 0,
            'message': '',
            'current_step': '',
            'total_steps': 6,       # channels, streams, groups, logos, m3u_accounts, profiles
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
        wait_for_existing_init = False
        with self._lock:
            if self._initialized and not force_refresh:
                logger.debug("UDI Manager already initialized")
                return True

            if self._init_in_progress:
                logger.info("UDI Manager initialization already in progress; waiting for completion")
                wait_for_existing_init = True
            else:
                config = get_dispatcharr_config()
                if not config.is_configured():
                    logger.warning("Cannot initialize UDI: Dispatcharr credentials not configured")
                    self._initialized = True
                    return False

                self._init_in_progress = True

        if wait_for_existing_init:
            return self._wait_for_initialization_completion(force_refresh=force_refresh)

        # 2. Fetch fresh data from API (OUTSIDE Lock)
        logger.debug("Fetching fresh data from API...")
        try:
            success = self.refresh_all()
            with self._lock:
                if success:
                    self._initialized = True
                return success
        except Exception as e:
            logger.error(f"Error initializing UDI Manager: {e}")
            return False
        finally:
            with self._lock:
                self._init_in_progress = False

    def _wait_for_initialization_completion(self, *, force_refresh: bool, timeout_seconds: int = 300) -> bool:
        """Wait for a concurrent initialization instead of reporting success early."""
        deadline = time.time() + timeout_seconds
        while time.time() < deadline:
            with self._lock:
                if not self._init_in_progress:
                    if force_refresh:
                        return bool(
                            self._initialized
                            and self._network_ready
                            and self._init_progress.get("status") == "completed"
                        )
                    return bool(self._initialized)
            time.sleep(0.25)

        logger.warning("Timed out waiting for concurrent UDI initialization to complete")
        return False

    def _load_legacy_storage_snapshot(self) -> bool:
        """Load old single-file UDI storage for compatibility tests/migrations.

        Runtime V2 data is intentionally in-memory and refreshed from Dispatcharr.
        This fallback only activates for an overridden storage directory, or when
        explicitly enabled, so a normal container does not silently prefer stale
        `/app/data/udi_data.json` over the live API.
        """
        try:
            from apps.udi import storage as udi_storage

            storage_dir = Path(getattr(udi_storage, "CONFIG_DIR", Path("/app/data")))
            if (
                storage_dir == Path("/app/data")
                and os.environ.get("STREAMFLOW_ENABLE_LEGACY_UDI_STORAGE") != "1"
            ):
                return False

            snapshot_path = storage_dir / "udi_data.json"
            if not snapshot_path.exists():
                return False

            data = json.loads(snapshot_path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return False

            self._channels_cache = data.get("channels", []) if isinstance(data.get("channels"), list) else []
            self._streams_cache = data.get("streams", []) if isinstance(data.get("streams"), list) else []
            self._channel_groups_cache = (
                data.get("channel_groups", []) if isinstance(data.get("channel_groups"), list) else []
            )
            self._logos_cache = data.get("logos", []) if isinstance(data.get("logos"), list) else []
            self._m3u_accounts_cache = (
                data.get("m3u_accounts", []) if isinstance(data.get("m3u_accounts"), list) else []
            )
            channel_profiles = data.get("channel_profiles", data.get("profiles", []))
            self._channel_profiles_cache = channel_profiles if isinstance(channel_profiles, list) else []
            profile_channels = data.get("profile_channels", {})
            self._profile_channels_cache = profile_channels if isinstance(profile_channels, dict) else {}

            self._build_indexes()
            self._initialized = True
            self._network_ready = False
            self._last_refresh_time = None
            self._init_progress.update(
                {
                    "status": "completed",
                    "percentage": 100,
                    "message": "Loaded legacy UDI storage snapshot",
                    "current_step": "legacy_storage",
                    "entity_counts": {
                        "channels": {"received": len(self._channels_cache), "expected": None},
                        "streams": {"received": len(self._streams_cache), "expected": None},
                        "groups": {"received": len(self._channel_groups_cache), "expected": None},
                        "logos": {"received": len(self._logos_cache), "expected": None},
                        "m3u_accounts": {"received": len(self._m3u_accounts_cache), "expected": None},
                        "profiles": {"received": len(self._channel_profiles_cache), "expected": None},
                    },
                }
            )
            logger.info("Loaded legacy UDI storage snapshot from %s", snapshot_path)
            return True
        except Exception as exc:
            logger.warning("Failed to load legacy UDI storage snapshot: %s", exc)
            return False
    
    def _build_indexes(self) -> None:
        """Build index caches for fast lookups."""
        self._channels_by_id = {ch.get('id'): ch for ch in self._channels_cache if ch.get('id') is not None}
        self._streams_by_id = {st.get('id'): st for st in self._streams_cache if st.get('id') is not None}
        self._streams_by_url = {st.get('url'): st for st in self._streams_cache if st.get('url')}
        self._valid_stream_ids = set(self._streams_by_id.keys())
        self._profiles_by_id = {p.get('id'): p for p in self._channel_profiles_cache if p.get('id') is not None}
        self._channel_groups_by_id = {g.get('id'): g for g in self._channel_groups_cache if g.get('id') is not None}
        self._logos_by_id = {lo.get('id'): lo for lo in self._logos_cache if lo.get('id') is not None}

        # account_id -> [stream, ...] and stream_id -> account_id
        self._streams_by_account_id = {}
        self._stream_account_id = {}
        for st in self._streams_cache:
            sid = st.get('id')
            aid = st.get('m3u_account')
            if sid is not None and aid is not None:
                self._stream_account_id[sid] = aid
                self._streams_by_account_id.setdefault(aid, []).append(st)

        # profile_id -> account_id (profiles are nested inside m3u_accounts)
        self._profile_to_account_id = {}
        for account in self._m3u_accounts_cache:
            aid = account.get('id')
            if aid is None:
                continue
            for profile in account.get('profiles', []):
                if isinstance(profile, dict):
                    pid = profile.get('id')
                    if pid is not None:
                        self._profile_to_account_id[pid] = aid

        self._has_custom_streams = any(st.get('is_custom', False) for st in self._streams_cache)

        self._m3u_accounts_by_id = {
            a.get('id'): a for a in self._m3u_accounts_cache if a.get('id') is not None
        }

        self._channels_by_group_id = {}
        for ch in self._channels_cache:
            gid = ch.get('channel_group_id')
            if gid is not None:
                self._channels_by_group_id.setdefault(gid, []).append(ch)

    # === Data Access Methods ===
    
    def get_init_progress(self) -> Dict[str, Any]:
        """Get the current initialization progress.
        
        Returns:
            Dictionary with status, percentage, message, and entity_counts.
        """
        progress = self._init_progress.copy()
        progress['entity_counts'] = self._init_progress.get('entity_counts', {}).copy()
        progress['api_timing'] = self.get_api_timing_summary()
        progress['last_refresh_duration_seconds'] = round(self._last_refresh_duration_seconds, 3)
        progress['last_refresh_time'] = self._last_refresh_time.isoformat() if self._last_refresh_time else None
        return progress
    
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

    def is_network_ready(self) -> bool:
        """Check if the UDI Manager has completed a successful live refresh from Dispatcharr.

        Distinct from is_initialized() which returns True even when data was loaded
        from SQL storage at startup (potentially stale — zero streams, zero M3U accounts).

        Background workers, scheduled event processors, automation loops, and queue
        workers must use this guard before operating against the stream pool to avoid
        acting on empty or stale startup cache state.

        Returns:
            True only after refresh_all() completed successfully from the Dispatcharr
            network. False during startup before the network refresh finishes.
        """
        return self._network_ready

    def get_last_refresh_duration(self) -> float:
        """Return seconds taken by the last successful refresh_all().

        Used to calibrate the post-provider-fetch poll timeout in
        _wait_for_udi_stream_count_stabilise(). Returns 0.0 if refresh_all()
        has never completed successfully (e.g. before startup init finishes).
        """
        return self._last_refresh_duration_seconds

    def get_api_timing_summary(self) -> Dict[str, Any]:
        """Return recent Dispatcharr API latency metrics from the fetcher."""
        getter = getattr(self.fetcher, "get_api_timing_summary", None)
        if callable(getter):
            return getter()
        return {
            "sample_count": 0,
            "failure_count": 0,
            "p95_seconds": None,
            "p99_seconds": None,
            "slowest": [],
        }

    def get_observability_status(self) -> Dict[str, Any]:
        """Return UI-safe UDI timing and cache status details."""
        now = datetime.now()
        age_seconds = None
        if self._last_refresh_time:
            age_seconds = round((now - self._last_refresh_time).total_seconds(), 3)
        return {
            "network_ready": self._network_ready,
            "refresh_running": self._refresh_running,
            "init_in_progress": self._init_in_progress,
            "last_refresh_time": self._last_refresh_time.isoformat() if self._last_refresh_time else None,
            "last_refresh_age_seconds": age_seconds,
            "last_refresh_duration_seconds": round(self._last_refresh_duration_seconds, 3),
            "cache_age_description": self.get_cache_age_description(),
            "api_timing": self.get_api_timing_summary(),
            "init_progress": self.get_init_progress(),
        }

    def get_cache_age_description(self) -> str:
        """Return a human-readable description of UDI cache age and last sync time.

        Format: 'last synced Xm Ys ago (HH:MM:SS) — N streams'
        Returns 'not yet synced from network' if refresh_all() has never completed.
        Used for diagnostic log lines at the start of automation cycles and
        single-channel health checks.
        """
        if self._last_refresh_time is None:
            return "not yet synced from network"

        now = datetime.now()
        elapsed = int((now - self._last_refresh_time).total_seconds())
        hours = elapsed // 3600
        minutes = (elapsed % 3600) // 60
        seconds = elapsed % 60

        if hours > 0:
            age_str = f"{hours}h {minutes}m ago"
        elif minutes > 0:
            age_str = f"{minutes}m {seconds}s ago"
        else:
            age_str = f"{seconds}s ago"

        time_str = self._last_refresh_time.strftime("%H:%M:%S")
        stream_count = len(self._streams_cache)

        return f"last synced {age_str} ({time_str}) — {stream_count:,} streams"

    def set_automation_busy(self) -> None:
        """Mark automation as busy — a cycle or single-channel check is running.

        The scheduled UDI refresh worker checks this flag before firing.
        If set, the worker skips the current slot and waits for the next
        scheduled time rather than replacing the cache mid-pipeline.

        Must be called at the start of run_automation_cycle() and
        check_single_channel(). Clear with clear_automation_busy() before
        any post-completion background sync fires.
        """
        with self._automation_busy_lock:
            self._automation_busy_count += 1
            self._automation_busy = True
            if self._automation_busy_since is None:
                self._automation_busy_since = datetime.now()

    def clear_automation_busy(self) -> None:
        """Clear the automation busy flag.

        Call at the natural completion point of run_automation_cycle() and
        check_single_channel() — before starting any background UDI sync
        thread, so the sync does not hold the busy flag.
        """
        with self._automation_busy_lock:
            self._automation_busy_count = max(0, self._automation_busy_count - 1)
            if self._automation_busy_count == 0:
                self._automation_busy = False
                self._automation_busy_since = None

    def is_automation_busy(self) -> bool:
        """Return True if a cycle or single-channel check is currently running.

        Auto-clears the flag if it has been held longer than
        _automation_busy_timeout_seconds (default 3600s) to recover from
        crashes that prevented clear_automation_busy() from executing.
        Does not block — caller decides what to do.
        """
        with self._automation_busy_lock:
            if not self._automation_busy:
                return False
            if self._automation_busy_since is not None:
                elapsed = (datetime.now() - self._automation_busy_since).total_seconds()
                if elapsed > self._automation_busy_timeout_seconds:
                    logger.warning(
                        f"Automation busy flag timed out after {elapsed:.0f}s — "
                        "auto-clearing to recover from stale lock. "
                        "This usually means automation crashed without calling clear_automation_busy()."
                    )
                    self._automation_busy = False
                    self._automation_busy_count = 0
                    self._automation_busy_since = None
                    return False
            return True

    def get_udi_refresh_last_run(self) -> Optional[datetime]:
        """Return the timestamp of the last scheduled UDI refresh run.

        In-memory only — resets to None on restart. Used by the UDI refresh
        worker to determine if the schedule is due.
        """
        return self._udi_refresh_last_run

    def set_udi_refresh_last_run(self, ts: Optional[datetime] = None) -> None:
        """Record that the scheduled UDI refresh just ran.

        Args:
            ts: Timestamp to record. Defaults to datetime.now().
        """
        self._udi_refresh_last_run = ts if ts is not None else datetime.now()

    def get_channels(self) -> List[Dict[str, Any]]:
        """Get all channels.

        Returns:
            List of channel dictionaries.
        """
        self._ensure_initialized()
        result: List[Dict[str, Any]] = []
        positions_by_id: Dict[Any, int] = {}
        duplicate_count = 0

        for channel in self._channels_cache:
            channel_id = channel.get('id')
            if channel_id is None:
                result.append(channel)
                continue

            if channel_id in positions_by_id:
                result[positions_by_id[channel_id]] = channel
                duplicate_count += 1
            else:
                positions_by_id[channel_id] = len(result)
                result.append(channel)

        if duplicate_count:
            logger.warning(
                "UDI channels cache deduplicated %s duplicate channel entries",
                duplicate_count,
            )

        return result
    
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
        # Build the result in a single pass; track any IDs absent from the cache
        # so they can be logged without iterating stream_ids a second time.
        result = []
        missing = []
        for sid in stream_ids:
            stream = self._streams_by_id.get(sid)
            if stream is not None:
                result.append(stream)
            else:
                missing.append(sid)
        if missing:
            logger.warning(
                f"Channel {channel_id}: {len(missing)} stream ID(s) not in UDI stream cache "
                f"(stale cache? IDs: {missing[:10]}{'...' if len(missing) > 10 else ''}). "
                "Consider refreshing streams."
            )
        return result
    
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
        return self._streams_cache
    
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
        return self._channel_groups_by_id.get(group_id)
    
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

        return list(self._channels_by_group_id.get(group_id, []))
    
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
        return self._logos_by_id.get(logo_id)
    
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
        account = self._m3u_accounts_by_id.get(account_id)
        if account is not None:
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
        if not self._has_custom_streams and self._streams_cache:
            self._has_custom_streams = any(st.get('is_custom', False) for st in self._streams_cache)
        return self._has_custom_streams

    def get_stream_count(self) -> int:
        """Return the number of cached streams without copying the list."""
        self._ensure_initialized()
        return len(self._streams_cache)

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
        self.fetcher.refresh_config()
        
        try:
            _refresh_start = datetime.now()

            # Fetch all entity types concurrently — none depend on each other,
            # and pre_counts (the integrity oracle) is only consumed after all
            # fetches complete, so it can run in the same wave.
            # Progress advances as each future lands via as_completed so the
            # frontend bar still moves steadily.
            _fetch_tasks = {
                'pre_counts':   self.fetcher.fetch_entity_counts,
                'channels':     self.fetcher.fetch_channels,
                'streams':      self.fetcher.fetch_streams,
                'groups':       self.fetcher.fetch_channel_groups,
                'logos':        self.fetcher.fetch_logos,
                'm3u_accounts': self.fetcher.fetch_m3u_accounts,
                'profiles':     self.fetcher.fetch_channel_profiles,
            }
            _fetch_results: Dict[str, Any] = {}
            self._update_init_progress(percentage=5, message='Fetching all data...', current_step='fetch')
            with ThreadPoolExecutor(max_workers=len(_fetch_tasks)) as _executor:
                _future_to_name = {_executor.submit(fn): name for name, fn in _fetch_tasks.items()}
                for _i, _future in enumerate(as_completed(_future_to_name), 1):
                    _name = _future_to_name[_future]
                    try:
                        _fetch_results[_name] = _future.result()
                    except Exception as _exc:
                        logger.error(f"Entity fetch failed for '{_name}': {_exc}")
                        _fetch_results[_name] = None
                    self._update_init_progress(
                        percentage=5 + int(_i / len(_fetch_tasks) * 65),
                        message=f'Fetching data ({_i}/{len(_fetch_tasks)} done)...',
                        current_step='fetch',
                    )

            pre_counts      = _fetch_results.get('pre_counts') or {}
            channels_result = _fetch_results.get('channels') or FetchResult()
            streams_result  = _fetch_results.get('streams')  or FetchResult()
            channel_groups  = _fetch_results.get('groups')   or []
            logos_result    = _fetch_results.get('logos')    or FetchResult()
            m3u_accounts    = _fetch_results.get('m3u_accounts') or []
            channel_profiles = _fetch_results.get('profiles') or []

            logger.info(
                f"UDI pre-fetch oracle: channels={pre_counts.get('channels')}, "
                f"streams={pre_counts.get('streams')} (from Dispatcharr /ids/ endpoints)"
            )

            # Apply /ids/ oracle when the paginated envelope omitted 'count'
            # (can happen with older Dispatcharr builds / custom pagination).
            if channels_result.expected_count is None and pre_counts.get('channels') is not None:
                channels_result.expected_count = pre_counts['channels']
                logger.info(f"UDI integrity: using /ids/ oracle for channels — expected {channels_result.expected_count}")
            if streams_result.expected_count is None and pre_counts.get('streams') is not None:
                streams_result.expected_count = pre_counts['streams']
                logger.info(f"UDI integrity: using /ids/ oracle for streams — expected {streams_result.expected_count}")

            channels_ok = _check_fetch_integrity('channels', channels_result)
            streams_ok  = _check_fetch_integrity('streams',  streams_result)
            _check_fetch_integrity('logos', logos_result)

            # Profile channels are embedded in the profiles list response — no
            # separate per-profile fetch needed.
            profile_channels = {
                p['id']: {
                    'profile': p,
                    'channels': p.get('channels') if isinstance(p.get('channels'), list) else [],
                }
                for p in channel_profiles
                if p.get('id') is not None
            }

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

            integrity_ok = channels_ok and streams_ok
            if not integrity_ok:
                self._update_init_progress(
                    status='failed',
                    percentage=100,
                    message='Initialization failed integrity check — retry the data refresh',
                    current_step='failed',
                    entity_counts=entity_counts,
                )
                return False

            self._update_init_progress(percentage=85, message='Building indexes...', current_step='index')

            # ----------------------------------------------------------------
            # Swap-pointer pattern: build everything in local variables so
            # the lock is held only for the pointer swap (microseconds), not
            # for index construction or storage writes (potentially seconds).
            # ----------------------------------------------------------------
            new_channels    = channels_result.items
            new_streams     = streams_result.items
            new_groups      = channel_groups
            new_logos       = logos_result.items
            new_accounts    = m3u_accounts
            new_profiles    = channel_profiles
            new_prof_chans  = profile_channels

            # Build every index in local scope — no readers can see these yet.
            new_channels_by_id = {
                ch.get('id'): ch for ch in new_channels if ch.get('id') is not None
            }
            new_streams_by_id = {
                st.get('id'): st for st in new_streams if st.get('id') is not None
            }
            new_streams_by_url = {
                st.get('url'): st for st in new_streams if st.get('url')
            }
            new_valid_stream_ids: Set[int] = set(new_streams_by_id.keys())
            new_profiles_by_id = {
                p.get('id'): p for p in new_profiles if p.get('id') is not None
            }
            new_groups_by_id = {
                g.get('id'): g for g in new_groups if g.get('id') is not None
            }
            new_logos_by_id = {
                lo.get('id'): lo for lo in new_logos if lo.get('id') is not None
            }
            new_accounts_by_id = {
                a.get('id'): a for a in new_accounts if a.get('id') is not None
            }
            new_channels_by_group: Dict[int, List[Dict[str, Any]]] = {}
            for ch in new_channels:
                gid = ch.get('channel_group_id')
                if gid is not None:
                    new_channels_by_group.setdefault(gid, []).append(ch)
            new_streams_by_account: Dict[int, List[Dict[str, Any]]] = {}
            new_stream_account_id: Dict[int, int] = {}
            for st in new_streams:
                sid = st.get('id')
                aid = st.get('m3u_account')
                if sid is not None and aid is not None:
                    new_stream_account_id[sid] = aid
                    new_streams_by_account.setdefault(aid, []).append(st)
            new_profile_to_account: Dict[int, int] = {}
            for account in new_accounts:
                aid = account.get('id')
                if aid is None:
                    continue
                for profile in account.get('profiles', []):
                    if isinstance(profile, dict):
                        pid = profile.get('id')
                        if pid is not None:
                            new_profile_to_account[pid] = aid
            new_has_custom = any(st.get('is_custom', False) for st in new_streams)

            # Automation is blocked only for the pointer swap — O(1) assignments.
            self.set_automation_busy()

            with self._lock:
                self._channels_cache          = new_channels
                self._streams_cache           = new_streams
                self._channel_groups_cache    = new_groups
                self._logos_cache             = new_logos
                self._m3u_accounts_cache      = new_accounts
                self._channel_profiles_cache  = new_profiles
                self._profile_channels_cache  = new_prof_chans

                self._channels_by_id          = new_channels_by_id
                self._streams_by_id           = new_streams_by_id
                self._streams_by_url          = new_streams_by_url
                self._valid_stream_ids        = new_valid_stream_ids
                self._profiles_by_id          = new_profiles_by_id
                self._channel_groups_by_id    = new_groups_by_id
                self._logos_by_id             = new_logos_by_id
                self._m3u_accounts_by_id      = new_accounts_by_id
                self._channels_by_group_id    = new_channels_by_group
                self._streams_by_account_id   = new_streams_by_account
                self._stream_account_id       = new_stream_account_id
                self._profile_to_account_id   = new_profile_to_account
                self._has_custom_streams      = new_has_custom

            now = datetime.now()

            # Mark all caches as refreshed
            for entity_type in ['channels', 'streams', 'channel_groups', 'logos', 'm3u_accounts', 'channel_profiles', 'profile_channels']:
                self.cache.mark_refreshed(entity_type, now)

            
            logger.info(
                f"UDI initialized: {len(self._channels_cache)} channels, {len(self._streams_cache)} streams, "
                f"{len(self._channel_groups_cache)} groups, {len(self._m3u_accounts_cache)} M3U accounts, "
                f"{len(self._channel_profiles_cache)} profiles"
            )

            self._update_init_progress(
                status='completed',
                percentage=100,
                message='Initialization complete',
                current_step='done',
                entity_counts=entity_counts,
            )
            # Record duration and completion time for poll timeout calibration
            # and cache age visibility. In-memory only — resets on restart.
            _refresh_end = datetime.now()
            self._last_refresh_duration_seconds = (_refresh_end - _refresh_start).total_seconds()
            self._last_refresh_time = _refresh_end
            # Seed the scheduled UDI refresh last-run timestamp so the first
            # scheduled slot waits the full interval from startup rather than
            # firing immediately (startup refresh counts as the first run).
            self._udi_refresh_last_run = _refresh_end
            # Mark network as ready — distinguishes a live Dispatcharr fetch from
            # a load from SQL storage at startup (which may have stale/empty data).
            self._network_ready = True
            return True
            
        except Exception as e:
            logger.error(f"Error refreshing UDI data: {e}")
            self._update_init_progress(status='failed', message=f'Refresh failed: {str(e)}')
            return False

        finally:
            self.clear_automation_busy()

    def refresh_delta(self) -> bool:
        """Incremental refresh: detect and apply stream/channel structural changes.

        Uses the /ids/ endpoints to diff Dispatcharr's current ID set against
        the UDI cache.  Only add/delete events are handled — field-level updates
        (e.g. a stream's URL changing) require a full refresh_all().

        Falls back to refresh_all() when:
        - The UDI is not yet network-ready (no prior live fetch)
        - Either /ids/ endpoint is unreachable
        - The change ratio exceeds UDI_DELTA_FALLBACK_THRESHOLD (bulk import, etc.)

        Newly added streams are inserted into all stream indexes.  The embedded
        channel.streams ID lists are NOT updated for new streams (they reflect the
        last full refresh); use refresh_all() periodically to re-sync assignments.
        Deleted streams ARE stripped from channel.streams lists immediately.

        Returns:
            True if the refresh succeeded (delta applied or full fallback).
        """
        if not self._initialized or not self._network_ready:
            logger.info("Delta refresh: not network-ready, falling back to full refresh")
            return self.refresh_all()

        config = get_dispatcharr_config()
        if not config.is_configured():
            logger.warning("Delta refresh: Dispatcharr not configured")
            return False

        logger.info("Starting delta refresh...")

        # ---- Step 1: fetch current ID sets (two concurrent /ids/ calls) ----
        current_ids = self.fetcher.fetch_all_ids()
        if not current_ids:
            logger.warning("Delta refresh: /ids/ endpoints unreachable, falling back to full refresh")
            return self.refresh_all()

        dispatcharr_stream_ids:  Set[int] = current_ids.get('streams',  set())
        dispatcharr_channel_ids: Set[int] = current_ids.get('channels', set())

        # ---- Step 2: compute delta ----
        cached_stream_ids:  Set[int] = self._valid_stream_ids.copy()
        cached_channel_ids: Set[int] = set(self._channels_by_id.keys())

        added_stream_ids    = dispatcharr_stream_ids  - cached_stream_ids
        deleted_stream_ids  = cached_stream_ids       - dispatcharr_stream_ids
        added_channel_ids   = dispatcharr_channel_ids - cached_channel_ids
        deleted_channel_ids = cached_channel_ids      - dispatcharr_channel_ids

        stream_change_ratio  = (len(added_stream_ids)  + len(deleted_stream_ids))  / max(len(dispatcharr_stream_ids),  1)
        channel_change_ratio = (len(added_channel_ids) + len(deleted_channel_ids)) / max(len(dispatcharr_channel_ids), 1)

        logger.info(
            f"Delta: streams +{len(added_stream_ids)} -{len(deleted_stream_ids)} "
            f"({stream_change_ratio:.1%}), "
            f"channels +{len(added_channel_ids)} -{len(deleted_channel_ids)} "
            f"({channel_change_ratio:.1%})"
        )

        # ---- Step 3: fall back if the delta is too large ----
        if (stream_change_ratio  > UDI_DELTA_FALLBACK_THRESHOLD or
                channel_change_ratio > UDI_DELTA_FALLBACK_THRESHOLD):
            logger.info(
                f"Delta too large (streams {stream_change_ratio:.1%}, "
                f"channels {channel_change_ratio:.1%}) — falling back to full refresh"
            )
            return self.refresh_all()

        if not any([added_stream_ids, deleted_stream_ids, added_channel_ids, deleted_channel_ids]):
            logger.info("Delta refresh: no structural changes detected")
            return True

        # ---- Step 4: fetch new items — both concurrently (outside the lock) ----
        new_streams:  List[Dict[str, Any]] = []
        new_channels: List[Dict[str, Any]] = []

        if added_stream_ids or added_channel_ids:
            with ThreadPoolExecutor(max_workers=2) as _ex:
                _st_future = (
                    _ex.submit(self.fetcher.fetch_streams_by_ids, list(added_stream_ids))
                    if added_stream_ids else None
                )
                _ch_future = (
                    _ex.submit(self.fetcher.fetch_channels_by_ids, list(added_channel_ids))
                    if added_channel_ids else None
                )
                if _st_future is not None:
                    new_streams = _st_future.result()
                    logger.info(f"Fetched {len(new_streams)}/{len(added_stream_ids)} new streams")
                if _ch_future is not None:
                    new_channels = _ch_future.result()
                    logger.info(f"Fetched {len(new_channels)}/{len(added_channel_ids)} new channels")

        # ---- Step 5: apply delta atomically under lock ----
        with self._lock:
            # Stream additions
            for st in new_streams:
                sid = st.get('id')
                if sid is None:
                    continue
                self._streams_cache.append(st)
                self._streams_by_id[sid] = st
                if st.get('url'):
                    self._streams_by_url[st['url']] = st
                self._valid_stream_ids.add(sid)
                aid = st.get('m3u_account')
                if aid is not None:
                    self._stream_account_id[sid] = aid
                    self._streams_by_account_id.setdefault(aid, []).append(st)
                if st.get('is_custom'):
                    self._has_custom_streams = True

            # Stream deletions
            if deleted_stream_ids:
                self._streams_cache = [
                    st for st in self._streams_cache
                    if st.get('id') not in deleted_stream_ids
                ]
                for sid in deleted_stream_ids:
                    st = self._streams_by_id.pop(sid, None)
                    if st and st.get('url'):
                        self._streams_by_url.pop(st['url'], None)
                    self._valid_stream_ids.discard(sid)
                    aid = self._stream_account_id.pop(sid, None)
                    if aid is not None and aid in self._streams_by_account_id:
                        self._streams_by_account_id[aid] = [
                            s for s in self._streams_by_account_id[aid]
                            if s.get('id') != sid
                        ]
                # Strip deleted IDs from channel.streams lists for cache consistency.
                # Mutating ch['streams'] in-place is reflected in _channels_by_id
                # automatically (both hold references to the same dict objects).
                for ch in self._channels_cache:
                    ch_streams = ch.get('streams')
                    if isinstance(ch_streams, list):
                        ch['streams'] = [s for s in ch_streams if s not in deleted_stream_ids]
                self._has_custom_streams = any(
                    st.get('is_custom', False) for st in self._streams_cache
                )

            # Channel additions
            for ch in new_channels:
                cid = ch.get('id')
                if cid is None:
                    continue
                self._channels_cache.append(ch)
                self._channels_by_id[cid] = ch
                gid = ch.get('channel_group_id')
                if gid is not None:
                    self._channels_by_group_id.setdefault(gid, []).append(ch)

            # Channel deletions
            if deleted_channel_ids:
                for cid in deleted_channel_ids:
                    ch = self._channels_by_id.pop(cid, None)
                    if ch:
                        gid = ch.get('channel_group_id')
                        if gid is not None and gid in self._channels_by_group_id:
                            self._channels_by_group_id[gid] = [
                                c for c in self._channels_by_group_id[gid]
                                if c.get('id') != cid
                            ]
                self._channels_cache = [
                    ch for ch in self._channels_cache
                    if ch.get('id') not in deleted_channel_ids
                ]

        logger.info(
            f"Delta refresh complete — "
            f"streams +{len(new_streams)} -{len(deleted_stream_ids)}, "
            f"channels +{len(new_channels)} -{len(deleted_channel_ids)}"
        )
        return True

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
            with self._lock:
                self._channels_cache = result.items
                self._channels_by_id = {
                    ch.get('id'): ch for ch in result.items if ch.get('id') is not None
                }
                self._channels_by_group_id = {}
                for ch in result.items:
                    gid = ch.get('channel_group_id')
                    if gid is not None:
                        self._channels_by_group_id.setdefault(gid, []).append(ch)
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
                with self._lock:
                    is_new = channel_id not in self._channels_by_id
                    self._channels_by_id[channel_id] = channel
                    if is_new:
                        self._channels_cache.append(channel)
                    else:
                        for i, ch in enumerate(self._channels_cache):
                            if ch.get('id') == channel_id:
                                self._channels_cache[i] = channel
                                break
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
            with self._lock:
                self._streams_cache = result.items
                self._streams_by_id = {
                    st.get('id'): st for st in result.items if st.get('id') is not None
                }
                self._streams_by_url = {st.get('url'): st for st in result.items if st.get('url')}
                self._valid_stream_ids = set(self._streams_by_id.keys())
                self._streams_by_account_id = {}
                self._stream_account_id = {}
                for st in result.items:
                    sid = st.get('id')
                    aid = st.get('m3u_account')
                    if sid is not None and aid is not None:
                        self._stream_account_id[sid] = aid
                        self._streams_by_account_id.setdefault(aid, []).append(st)
                self._has_custom_streams = any(st.get('is_custom', False) for st in result.items)
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
            with self._lock:
                self._channel_groups_cache = groups
                self._channel_groups_by_id = {
                    g.get('id'): g for g in groups if g.get('id') is not None
                }
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
            with self._lock:
                self._m3u_accounts_cache = accounts
                self._m3u_accounts_by_id = {
                    a.get('id'): a for a in accounts if a.get('id') is not None
                }
                # Rebuild profile->account mapping since profiles nest inside accounts
                self._profile_to_account_id = {}
                for account in accounts:
                    aid = account.get('id')
                    if aid is None:
                        continue
                    for profile in account.get('profiles', []):
                        if isinstance(profile, dict):
                            pid = profile.get('id')
                            if pid is not None:
                                self._profile_to_account_id[pid] = aid
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
            with self._lock:
                self._channel_profiles_cache = profiles
                self._profiles_by_id = {
                    p.get('id'): p for p in profiles if p.get('id') is not None
                }
            self.cache.mark_refreshed('channel_profiles')

            profile_channels = {
                p['id']: {
                    'profile': p,
                    'channels': p.get('channels') if isinstance(p.get('channels'), list) else [],
                }
                for p in profiles
                if p.get('id') is not None
            }
            with self._lock:
                self._profile_channels_cache = profile_channels
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
            is_new = channel_id not in self._channels_by_id
            self._channels_by_id[channel_id] = channel_data
            if is_new:
                self._channels_cache.append(channel_data)
            else:
                for i, ch in enumerate(self._channels_cache):
                    if ch.get('id') == channel_id:
                        self._channels_cache[i] = channel_data
                        break
            return True
    
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
            is_new = stream_id not in self._streams_by_id
            self._streams_by_id[stream_id] = stream_data
            if stream_data.get('url'):
                self._streams_by_url[stream_data['url']] = stream_data
            if is_new:
                self._streams_cache.append(stream_data)
                self._valid_stream_ids.add(stream_id)
            else:
                for i, st in enumerate(self._streams_cache):
                    if st.get('id') == stream_id:
                        self._streams_cache[i] = stream_data
                        break
            return True
    
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
            self._profile_channels_cache[profile_id] = profile_channels_data
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
        }
    
    def get_cache_last_refresh(self, entity_type: str) -> Optional[Any]:
        """Get the last refresh time for a specific entity type from cache.
        
        Args:
            entity_type: The entity type to query (e.g., 'channel_profiles')
            
        Returns:
            The last refresh datetime or None if never refreshed
        """
        return self.cache.get_last_refresh(entity_type)
    
    def _find_account_for_profile(self, profile_id: int) -> Optional[int]:
        """Find the M3U account ID that contains a specific profile.

        Args:
            profile_id: M3U account profile ID

        Returns:
            M3U account ID or None if profile not found
        """
        return self._profile_to_account_id.get(profile_id)
    
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
        return sum(
            st.get('current_viewers', 0)
            for st in self._streams_by_account_id.get(account_id, [])
        )
    
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
        
        # Dispatcharr proxy status has existed in both numeric-ID keyed and
        # UUID-keyed shapes. Match both so active viewers reliably protect the
        # channel from quality checks.
        channel_id_str = str(channel_id)
        channel = self._channels_by_id.get(channel_id)
        channel_uuid = None
        if isinstance(channel, dict):
            channel_uuid = channel.get('uuid') or channel.get('channel_uuid')
            if channel_uuid is not None:
                channel_uuid = str(channel_uuid)

        candidate_keys = {channel_id_str}
        if channel_uuid:
            candidate_keys.add(channel_uuid)

        for key in candidate_keys:
            if key not in proxy_status:
                continue
            status = proxy_status[key]
            is_active = self._is_channel_status_active(status)
            logger.debug(
                f"Channel {channel_id} is {'active' if is_active else 'inactive'} "
                f"(from proxy status key {key})"
            )
            return is_active

        for key, status in proxy_status.items():
            if not isinstance(status, dict):
                continue
            status_identifiers = {
                str(value)
                for value in (
                    status.get('channel_id'),
                    status.get('channel_uuid'),
                    status.get('uuid'),
                    status.get('id'),
                )
                if value not in (None, '')
            }
            if channel_id_str in status_identifiers or (channel_uuid and channel_uuid in status_identifiers):
                is_active = self._is_channel_status_active(status)
                logger.debug(
                    f"Channel {channel_id} is {'active' if is_active else 'inactive'} "
                    f"(from proxy status entry {key})"
                )
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
            config = get_dispatcharr_config()
            if not config.is_configured():
                logger.warning("UDI Manager not initialized and Dispatcharr not configured — skipping auto-init.")
                return
            if self._load_legacy_storage_snapshot():
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
                self._invalidate_streams_cache(deleted_stream_ids=set(stream_ids))
                return True
            else:
                logger.error(f"Failed to delete streams: HTTP {response.status_code}")
                return False

        except Exception as e:
            logger.error(f"Error deleting streams from Dispatcharr: {e}", exc_info=True)
            return False

    def _invalidate_streams_cache(self, deleted_stream_ids: Optional[set] = None) -> None:
        """Invalidate streams cache after modification operations.

        If deleted_stream_ids is provided, also strips those IDs from the
        embedded stream lists in _channels_cache so subsequent get_channel_streams()
        calls don't emit stale-ID warnings.
        """
        with self._lock:
            self._streams_cache = []
            self._streams_by_id = {}
            self._streams_by_url = {}
            self._valid_stream_ids = set()
            self._streams_by_account_id = {}
            self._stream_account_id = {}
            self._has_custom_streams = False

            if deleted_stream_ids:
                for ch in self._channels_cache:
                    streams_list = ch.get('streams')
                    if isinstance(streams_list, list):
                        ch['streams'] = [sid for sid in streams_list if sid not in deleted_stream_ids]
                # Rebuild id→channel index to reflect updated stream lists
                self._channels_by_id = {
                    ch.get('id'): ch
                    for ch in self._channels_cache
                    if ch.get('id') is not None
                }


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
