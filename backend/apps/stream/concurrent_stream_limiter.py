#!/usr/bin/env python3
"""
Concurrent Stream Limiter for StreamFlow.

This module provides intelligent concurrent stream limiting based on M3U account
stream limits. It ensures that when checking multiple streams in parallel, the
system respects each account's maximum concurrent stream limit.

Example:
    Account A has max_streams=1, Account B has max_streams=2
    Channel has streams: A1, A2, B1, B2, B3
    
    The limiter will ensure:
    - Only 1 stream from Account A is checked at a time
    - Up to 2 streams from Account B can be checked concurrently
    - Overall: A1, B1, B2 can run in parallel (3 total)
    - When A1 completes, A2 can start
    - When B1 or B2 completes, B3 can start
"""

import threading
import time
from collections import defaultdict
from typing import Dict, List, Optional, Any, Callable
from concurrent.futures import ThreadPoolExecutor, Future
from apps.core.logging_config import setup_logging

logger = setup_logging(__name__)


class AccountStreamLimiter:
    """
    Manages concurrent stream limits per M3U account.
    
    Uses tracking counters to enforce per-account concurrency limits while allowing
    maximum parallelism across different accounts. Also considers active viewers
    from the UDI when determining available slots.
    
    The limiter ensures: active_viewers + checking_streams <= max_streams
    """
    
    def __init__(self, udi_manager=None):
        """Initialize the account stream limiter.
        
        Args:
            udi_manager: Optional UDI manager instance for checking active viewers
        """
        self.account_limits: Dict[int, int] = {}
        self.account_checking_counts: Dict[int, int] = {}  # Track streams currently being checked
        self.lock = threading.Lock()
        self.udi_manager = udi_manager
        logger.info("AccountStreamLimiter initialized")
    
    def set_account_limit(self, account_id: int, max_streams: int, profiles: List[Dict[str, Any]] = None):
        """
        Set the maximum concurrent streams for an account.
        
        This now supports M3U account profiles. If profiles are provided, the total
        limit is calculated by summing the max_streams of all active profiles.
        
        Args:
            account_id: M3U account ID
            max_streams: Maximum concurrent streams at account level (0 = unlimited)
            profiles: Optional list of profile dictionaries with 'max_streams' and 'is_active' fields
        """
        with self.lock:
            # Calculate total limit by summing active profile limits if profiles exist
            total_limit = max_streams
            if profiles:
                # Sum up limits from all active profiles using generator expression
                profile_total = sum(
                    p.get('max_streams', 0) 
                    for p in profiles 
                    if p.get('is_active', True)
                )
                
                # Use profile total if it's greater than account-level limit
                # This handles the case where account has multiple profiles
                if profile_total > 0:
                    total_limit = profile_total
                    active_profile_count = sum(1 for p in profiles if p.get('is_active', True))
                    logger.debug(
                        f"Account {account_id} has {active_profile_count} "
                        f"active profile(s) with total limit: {total_limit}"
                    )
            
            # Store the calculated limit
            self.account_limits[account_id] = total_limit
            
            # Initialize checking count for this account
            if account_id not in self.account_checking_counts:
                self.account_checking_counts[account_id] = 0
            
            logger.debug(
                f"Set limit for account {account_id}: {total_limit} concurrent streams" 
                if total_limit > 0 
                else f"Set limit for account {account_id}: unlimited concurrent streams"
            )
    
    def get_account_limit(self, account_id: int) -> int:
        """
        Get the maximum concurrent streams for an account.
        
        Args:
            account_id: M3U account ID
            
        Returns:
            Maximum concurrent streams (0 = unlimited)
        """
        return self.account_limits.get(account_id, 0)
    
    def get_available_slots(self, account_id: int) -> int:
        """
        Get the number of available stream slots for an account.
        
        Considers both active viewers (from UDI) and currently checking streams.
        
        Args:
            account_id: M3U account ID
            
        Returns:
            Number of available slots (0 if at limit, -1 if unlimited)
        """
        limit = self.get_account_limit(account_id)
        
        if limit == 0:
            # Unlimited
            return -1
        
        # Get active streams from UDI if available
        active_count = 0
        if self.udi_manager:
            try:
                active_count = self.udi_manager.get_active_streams_for_account(account_id)
            except Exception as e:
                logger.warning(f"Could not get active streams for account {account_id}: {e}")
        
        # Get currently checking streams
        with self.lock:
            checking_count = self.account_checking_counts.get(account_id, 0)
        
        # Available slots = limit - active streams - checking streams
        available = limit - active_count - checking_count
        return max(0, available)
    
    def acquire(self, account_id: Optional[int], timeout: float = None) -> tuple[bool, str]:
        """
        Acquire permission to check a stream from the given account.
        
        Considers active viewers (from UDI) when determining if a slot is available.
        This ensures that: active_viewers + checking_streams <= max_streams
        
        Blocks/waits until a slot becomes available or timeout expires.
        
        Args:
            account_id: M3U account ID (None for custom streams)
            timeout: Maximum time to wait in seconds (None = wait forever)
            
        Returns:
            Tuple of (acquired: bool, reason: str)
            - (True, 'acquired') if slot was acquired
            - (False, 'active_viewers') if limit reached due to active viewers and timeout
            - (False, 'timeout') if timed out waiting for slot
        """
        if account_id is None:
            # Custom stream with no account - always allow
            return (True, 'acquired')
        
        limit = self.get_account_limit(account_id)
        if limit == 0:
            # Unlimited - always allow
            return (True, 'acquired')
        
        # Poll for available slot with exponential backoff
        start_time = time.time()
        wait_time = 0.1  # Start with 100ms
        max_wait = 2.0  # Max 2 seconds between checks
        
        while True:
            # Get active streams from UDI if available
            active_count = 0
            if self.udi_manager:
                try:
                    active_count = self.udi_manager.get_active_streams_for_account(account_id)
                except Exception as e:
                    logger.warning(f"Could not get active streams for account {account_id}: {e}")
            
            # Check if we have available slots: active_viewers + checking_streams < max_streams
            # We need to check this atomically with acquiring the semaphore
            with self.lock:
                checking_count = self.account_checking_counts.get(account_id, 0)
                total_in_use = active_count + checking_count
                
                if total_in_use < limit:
                    # We have a slot available, increment checking count
                    self.account_checking_counts[account_id] = checking_count + 1
                    logger.debug(
                        f"Acquired stream slot for account {account_id} "
                        f"({active_count} active + {checking_count + 1} checking = "
                        f"{total_in_use + 1}/{limit})"
                    )
                    return (True, 'acquired')
            
            # No slot available, check timeout
            if timeout is not None:
                elapsed = time.time() - start_time
                if elapsed >= timeout:
                    logger.warning(
                        f"Timeout acquiring slot for account {account_id} after {elapsed:.1f}s "
                        f"({active_count} active + {checking_count} checking = {total_in_use}/{limit})"
                    )
                    return (False, 'timeout')
            
            # Wait before retrying (exponential backoff)
            time.sleep(wait_time)
            wait_time = min(wait_time * 1.5, max_wait)
    
    def release(self, account_id: Optional[int]):
        """
        Release a stream slot for the given account.
        
        Args:
            account_id: M3U account ID (None for custom streams)
        """
        if account_id is None:
            # Custom stream with no account - nothing to release
            return
        
        limit = self.get_account_limit(account_id)
        if limit == 0:
            # Unlimited account - nothing to track or release
            return
        
        with self.lock:
            checking_count = self.account_checking_counts.get(account_id, 0)
            if checking_count > 0:
                self.account_checking_counts[account_id] = checking_count - 1
                logger.debug(
                    f"Released stream slot for account {account_id} "
                    f"(now {self.account_checking_counts[account_id]} checking)"
                )
            else:
                logger.warning(
                    f"Attempted to release slot for account {account_id} "
                    f"but checking count is already 0"
                )
    
    def clear(self):
        """Clear all account limits and checking counts."""
        with self.lock:
            self.account_limits.clear()
            self.account_checking_counts.clear()
        logger.info("Cleared all account limits")


class SmartStreamScheduler:
    """
    Smart scheduler for parallel stream checking with per-account limits.
    
    This scheduler organizes streams by account and ensures that:
    1. Account limits are respected (max concurrent streams per account)
    2. Overall parallelism is maximized across different accounts
    3. Streams are scheduled efficiently to minimize total checking time
    """
    
    def __init__(self, account_limiter: AccountStreamLimiter, global_limit: int = 10):
        """
        Initialize the smart stream scheduler.
        
        Args:
            account_limiter: AccountStreamLimiter instance
            global_limit: Global maximum concurrent streams (default: 10)
        """
        self.account_limiter = account_limiter
        self.global_limit = global_limit
        logger.info(f"SmartStreamScheduler initialized with global_limit={global_limit}")
    
    def check_streams_with_limits(
        self,
        streams: List[Dict[str, Any]],
        check_function: Callable,
        progress_callback: Optional[Callable] = None,
        start_callback: Optional[Callable] = None,
        stagger_delay: float = 0.0,
        abort_event: Optional[threading.Event] = None,
        **check_params
    ) -> List[Dict[str, Any]]:
        """
        Check multiple streams in parallel with per-account concurrent limits.
        
        This method intelligently schedules stream checks to respect both:
        - Per-account concurrent stream limits
        - Global concurrent stream limit
        
        Args:
            streams: List of stream dictionaries to check (must include 'm3u_account')
            check_function: Function to call for each stream
            progress_callback: Optional callback after each stream completes
            start_callback: Optional callback right before a stream starts checking
            stagger_delay: Delay between starting tasks (default: 0.0)
            **check_params: Additional parameters for check_function
            
        Returns:
            List of stream analysis results
        """
        if not streams:
            logger.info("No streams to check")
            return []
        
        total_streams = len(streams)
        logger.info(f"Starting smart parallel check of {total_streams} streams")
        
        # Group streams by account for better logging
        account_groups = defaultdict(list)
        for stream in streams:
            account_id = stream.get('m3u_account')
            account_groups[account_id].append(stream)
        
        logger.info(f"Streams grouped by account: {dict((k, len(v)) for k, v in account_groups.items())}")
        for account_id, account_streams in account_groups.items():
            limit = self.account_limiter.get_account_limit(account_id) if account_id else 0
            limit_str = "unlimited" if limit == 0 else str(limit)
            logger.info(f"  Account {account_id}: {len(account_streams)} streams, limit={limit_str}")
        
        results = []
        completed_count = 0
        lock = threading.Lock()
        
        # Use ThreadPoolExecutor with global limit
        with ThreadPoolExecutor(max_workers=self.global_limit) as executor:
            futures: Dict[Future, Dict[str, Any]] = {}
            
            def submit_stream_check(stream: Dict[str, Any]):
                """Submit a stream check with profile-aware limit enforcement."""
                account_id = stream.get('m3u_account')
                
                # Check if stream can run using profile-aware checking
                # This replaces the old account-level acquire/release with per-profile awareness
                if account_id and self.account_limiter.udi_manager:
                    can_run, reason = self.account_limiter.udi_manager.check_stream_can_run(stream)
                    
                    if not can_run:
                        logger.info(f"Skipping check for stream {stream['id']}: {reason}, using cached stats")
                        
                        # Get cached stream stats from UDI
                        try:
                            cached_stream = self.account_limiter.udi_manager.get_stream_by_id(stream['id'])
                            if cached_stream and cached_stream.get('stream_stats'):
                                # Return a result with cached stats
                                return {
                                    'stream_id': stream['id'],
                                    'stream_name': stream.get('name', 'Unknown'),
                                    'stream_url': stream.get('url', ''),
                                    'cached': True,
                                    'skipped_reason': 'no_available_profile',
                                    'reason_detail': reason,
                                    **cached_stream.get('stream_stats', {})
                                }
                            else:
                                logger.warning(f"No cached stats available for stream {stream['id']}, skipping")
                        except Exception as e:
                            logger.error(f"Error retrieving cached stats for stream {stream['id']}: {e}")
                        return None
                
                # Acquire account slot before submitting to executor
                # This ensures we don't exceed per-account limits at a global level
                acquired, reason = self.account_limiter.acquire(account_id, timeout=300)
                
                if not acquired:
                    if reason == 'active_viewers':
                        # Quota fully consumed by active viewers - use cached stats
                        logger.info(f"Skipping check for stream {stream['id']} - quota consumed by active viewers, using cached stats")
                        
                        # Get cached stream stats from UDI
                        if self.account_limiter.udi_manager:
                            try:
                                cached_stream = self.account_limiter.udi_manager.get_stream_by_id(stream['id'])
                                if cached_stream and cached_stream.get('stream_stats'):
                                    # Return a result with cached stats
                                    return {
                                        'stream_id': stream['id'],
                                        'stream_name': stream.get('name', 'Unknown'),
                                        'stream_url': stream.get('url', ''),
                                        'cached': True,
                                        'skipped_reason': 'quota_consumed_by_active_viewers',
                                        **cached_stream.get('stream_stats', {})
                                    }
                                else:
                                    logger.warning(f"No cached stats available for stream {stream['id']}, skipping")
                            except Exception as e:
                                logger.error(f"Error retrieving cached stats for stream {stream['id']}: {e}")
                        return None
                    else:
                        # Timeout - skip stream
                        logger.error(f"Timeout acquiring slot for account {account_id}, skipping stream {stream['id']}")
                        return None
                
                def wrapped_check():
                    """Wrapper that ensures semaphore is released and progress fires immediately."""
                    result = None
                    try:
                        # Apply URL transformation if using M3U profile with search/replace patterns
                        stream_url = stream.get('url', '')
                        if self.account_limiter.udi_manager:
                            stream_url = self.account_limiter.udi_manager.apply_profile_url_transformation(stream)

                        # Notify that this stream has acquired a slot and is starting.
                        # Lock protects stream_statuses which is shared across worker threads.
                        if start_callback:
                            try:
                                with lock:
                                    start_callback(stream)
                            except Exception as e:
                                logger.error(f"Error in start_callback for stream {stream['id']}: {e}")

                        result = check_function(
                            stream_url=stream_url,
                            stream_id=stream['id'],
                            stream_name=stream.get('name', 'Unknown'),
                            **check_params
                        )
                        return result
                    finally:
                        # Release account slot immediately when stream finishes
                        self.account_limiter.release(account_id)

                        # Fire progress callback from the worker thread the instant the
                        # stream completes — before the submission loop has finished
                        # stagger-sleeping through remaining streams. This is what makes
                        # the live grid update in real time for large stream counts.
                        with lock:
                            if result is not None:
                                results.append(result)
                            else:
                                results.append({
                                    'stream_id': stream['id'],
                                    'stream_name': stream.get('name', 'Unknown'),
                                    'stream_url': stream.get('url', ''),
                                    'status': 'ERROR',
                                    'resolution': '0x0',
                                    'bitrate_kbps': 0,
                                    'fps': 0,
                                    'video_codec': 'N/A',
                                    'audio_codec': 'N/A'
                                })
                            nonlocal completed_count
                            completed_count += 1
                            current_completed = completed_count

                        logger.debug(
                            f"Completed {current_completed}/{total_streams}: "
                            f"Stream {stream['id']} - {stream.get('name', 'Unknown')}"
                        )

                        if progress_callback and result is not None:
                            try:
                                progress_callback(current_completed, total_streams, result)
                            except Exception as e:
                                logger.error(f"Error in progress_callback for stream {stream['id']}: {e}")

                # Submit to executor
                future = executor.submit(wrapped_check)
                return future

            # Submit all streams with stagger delay — runs concurrently with
            # wrapped_check completions since progress now fires from worker threads
            for stream in streams:
                if abort_event and abort_event.is_set():
                    logger.info("Abort requested, stopping stream submission")
                    break

                if stagger_delay > 0 and futures:
                    time.sleep(stagger_delay)

                future_or_result = submit_stream_check(stream)
                if future_or_result is not None:
                    if isinstance(future_or_result, dict):
                        # Cached result — no future, handle inline
                        with lock:
                            results.append(future_or_result)
                            completed_count += 1
                        logger.debug(f"Using cached stats for stream {stream['id']}")
                    else:
                        futures[future_or_result] = stream
                        logger.debug(f"Submitted stream {stream['id']} for checking")

            # Wait for all submitted futures to complete.
            # Progress callbacks already fired from worker threads — this loop
            # only needs to catch exceptions from futures that errored.
            from concurrent.futures import as_completed
            for future in as_completed(futures):
                if abort_event and abort_event.is_set():
                    logger.info("Abort requested, breaking completion wait loop")
                    break

                stream = futures[future]
                try:
                    future.result()  # re-raises any exception from wrapped_check
                except Exception as e:
                    logger.error(
                        f"Error checking stream {stream['id']} ({stream.get('name', 'Unknown')}): {e}",
                        exc_info=True
                    )
        
        logger.info(f"Completed smart parallel check of {completed_count}/{total_streams} streams")
        return results


# Global instance
_account_limiter = None
_smart_scheduler = None
_limiter_lock = threading.RLock()  # Use RLock to allow recursive locking


def get_account_limiter() -> AccountStreamLimiter:
    """
    Get or create the global account limiter instance.
    
    Returns:
        AccountStreamLimiter instance
    """
    global _account_limiter
    with _limiter_lock:
        if _account_limiter is None:
            # Import UDI manager here to avoid circular imports
            from apps.udi import get_udi_manager
            udi_manager = get_udi_manager()
            _account_limiter = AccountStreamLimiter(udi_manager=udi_manager)
        return _account_limiter


def get_smart_scheduler(global_limit: int = 10) -> SmartStreamScheduler:
    """
    Get or create the global smart scheduler instance.
    
    Args:
        global_limit: Global maximum concurrent streams
        
    Returns:
        SmartStreamScheduler instance
    """
    global _smart_scheduler
    with _limiter_lock:
        account_limiter = get_account_limiter()
        if _smart_scheduler is None or _smart_scheduler.global_limit != global_limit:
            _smart_scheduler = SmartStreamScheduler(account_limiter, global_limit=global_limit)
        return _smart_scheduler


def initialize_account_limits(accounts: List[Dict[str, Any]]):
    """
    Initialize account limits from M3U account data.
    
    NOTE: This function now works in conjunction with profile-aware checking.
    The limits set here are used as a fallback/global cap, but the primary
    limit enforcement is done per-profile via UDI's check_stream_can_run().
    
    When profiles are present, the total limit is calculated by summing max_streams
    from all active profiles to provide a global upper bound for the account.
    However, actual stream checking uses profile-specific availability.
    
    Args:
        accounts: List of M3U account dictionaries with 'id', 'max_streams', and optionally 'profiles' fields
    """
    limiter = get_account_limiter()
    
    for account in accounts:
        account_id = account.get('id')
        max_streams = account.get('max_streams', 0)
        profiles = account.get('profiles', [])
        
        if account_id is not None:
            limiter.set_account_limit(account_id, max_streams, profiles)
    
    logger.info(f"Initialized limits for {len(accounts)} accounts (profile-aware checking enabled)")
