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


def _get_stream_m3u_account_id(stream: Dict[str, Any]) -> Optional[Any]:
    """Return a stream's M3U account id across legacy and SQL payloads."""
    if not isinstance(stream, dict):
        return None
    account_id = stream.get('m3u_account_id')
    if account_id in (None, ''):
        account_id = stream.get('m3u_account')
    try:
        return int(account_id) if account_id not in (None, '') else None
    except (TypeError, ValueError):
        return account_id


def _safe_int(value: Any) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, parsed)


def _mapping_value(mapping: Dict[Any, Any], key: Any) -> Any:
    candidates = [key, str(key)]
    try:
        candidates.append(int(key))
    except (TypeError, ValueError):
        pass

    for candidate in candidates:
        if candidate in mapping:
            return mapping.get(candidate)
    return 0


def _usage_context(mapping: Dict[Any, Any], key: Any) -> Dict[str, int]:
    value = _mapping_value(mapping, key)
    if isinstance(value, dict):
        active_streams = _safe_int(
            value.get('active_streams', value.get('active_viewers', value.get('count', 0)))
        )
        real_viewers = _safe_int(value.get('real_viewers', value.get('real_clients', 0)))
        shadow_watchers = _safe_int(value.get('shadow_watchers', value.get('watcher_clients', 0)))
        real_viewer_streams = _safe_int(value.get('real_viewer_streams'))
        if 'real_viewer_streams' not in value:
            # Legacy contexts expose client count but not the number of provider
            # slots carrying those clients. Never count multiple clients on one
            # proxied channel as multiple upstream streams.
            real_viewer_streams = min(active_streams, real_viewers)
        return {
            'active_streams': active_streams,
            'real_viewers': real_viewers,
            'real_viewer_streams': real_viewer_streams,
            'shadow_watchers': shadow_watchers,
        }
    active_streams = _safe_int(value)
    return {
        'active_streams': active_streams,
        'real_viewers': active_streams,
        'real_viewer_streams': active_streams,
        'shadow_watchers': 0,
    }


class AcquireResult(tuple):
    """Tuple-compatible acquire result that remains truthy/falsey by success."""

    def __new__(cls, acquired: bool, reason: str):
        return super().__new__(cls, (acquired, reason))

    @property
    def acquired(self) -> bool:
        return self[0]

    @property
    def reason(self) -> str:
        return self[1]

    def __bool__(self) -> bool:
        return bool(self[0])


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
        self.profile_limits: Dict[int, int] = {}
        self.account_profile_ids: Dict[int, set[int]] = {}
        self.account_checking_counts: Dict[int, int] = {}  # Track streams currently being checked
        self.profile_checking_counts: Dict[int, int] = {}  # Track checker-owned profile slots
        self.viewer_preemption_claims: Dict[Any, tuple[Optional[int], Optional[int]]] = {}
        self.lock = threading.Lock()
        self.udi_manager = udi_manager
        logger.info("AccountStreamLimiter initialized")
    
    def set_account_limit(self, account_id: int, max_streams: int, profiles: List[Dict[str, Any]] = None):
        """
        Set the maximum concurrent streams for an account.
        
        A positive account limit is the authoritative aggregate hard cap. Active
        profile limits remain additional per-profile sublimits and must never add
        capacity above that account cap. When the account limit is unset/unlimited,
        finite active profile limits provide the aggregate fallback. If any active
        profile is unlimited, that fallback is unlimited as well.
        
        Args:
            account_id: M3U account ID
            max_streams: Maximum concurrent streams at account level (0 = unlimited)
            profiles: Optional list of profile dictionaries with 'max_streams' and 'is_active' fields
        """
        profiles_provided = profiles is not None
        with self.lock:
            try:
                account_limit = max(0, int(max_streams or 0))
            except (TypeError, ValueError):
                account_limit = 0

            active_profile_limits = []
            active_profile_limits_by_id: Dict[int, int] = {}
            for profile in profiles or []:
                if not isinstance(profile, dict) or not profile.get('is_active', True):
                    continue
                try:
                    profile_limit = max(0, int(profile.get('max_streams', 0) or 0))
                except (TypeError, ValueError):
                    profile_limit = 0
                active_profile_limits.append(profile_limit)
                profile_id = profile.get('id')
                if profile_id is not None:
                    active_profile_limits_by_id[profile_id] = profile_limit

            profile_total = sum(active_profile_limits)
            has_unlimited_profile = any(limit == 0 for limit in active_profile_limits)

            if account_limit > 0:
                total_limit = account_limit
                capacity_source = 'account hard cap'
            elif active_profile_limits and has_unlimited_profile:
                total_limit = 0
                capacity_source = 'unlimited active profile fallback'
            elif profile_total > 0:
                total_limit = profile_total
                capacity_source = 'finite active profile fallback'
            else:
                total_limit = 0
                capacity_source = 'unlimited account fallback'

            if active_profile_limits:
                logger.debug(
                    "Account %s capacity uses %s: account=%s, active profiles=%s, "
                    "finite profile sum=%s",
                    account_id,
                    capacity_source,
                    account_limit,
                    len(active_profile_limits),
                    profile_total,
                )
            
            # Store the calculated limit
            self.account_limits[account_id] = total_limit
            if profiles_provided:
                previous_profile_ids = self.account_profile_ids.get(account_id, set())
                current_profile_ids = set(active_profile_limits_by_id)
                for removed_profile_id in previous_profile_ids - current_profile_ids:
                    self.profile_limits.pop(removed_profile_id, None)
                self.profile_limits.update(active_profile_limits_by_id)
                self.account_profile_ids[account_id] = current_profile_ids
            
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
                if not isinstance(active_count, (int, float)):
                    active_count = 0
                else:
                    active_count = _safe_int(active_count)
            except Exception as e:
                logger.warning(f"Could not get active streams for account {account_id}: {e}")
                active_count = 0
        
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
            return AcquireResult(True, 'acquired')
        
        # Poll for available slot with exponential backoff
        start_time = time.time()
        wait_time = 0.1  # Start with 100ms
        max_wait = 2.0  # Max 2 seconds between checks
        
        last_wait_reason = 'timeout'

        while True:
            # Get active streams from UDI if available
            active_count = 0
            if self.udi_manager:
                try:
                    active_count = self.udi_manager.get_active_streams_for_account(account_id)
                    if not isinstance(active_count, (int, float)):
                        active_count = 0
                    else:
                        active_count = _safe_int(active_count)
                except Exception as e:
                    logger.warning(f"Could not get active streams for account {account_id}: {e}")
                    active_count = 0
            
            # Check if we have available slots: active_viewers + checking_streams < max_streams
            # Read the current limit and reserve atomically. This linearizes live
            # reconfiguration with acquisition instead of using a stale limit
            # captured before the wait loop.
            with self.lock:
                limit = _safe_int(self.account_limits.get(account_id, 0))
                checking_count = self.account_checking_counts.get(account_id, 0)
                total_in_use = active_count + checking_count

                if limit == 0 or total_in_use < limit:
                    # Track unlimited reservations too. A later 0 -> finite
                    # reconfiguration can then release exactly the reservation
                    # that was acquired without decrementing another checker.
                    self.account_checking_counts[account_id] = checking_count + 1
                    if limit == 0:
                        logger.debug(
                            f"Acquired unlimited stream slot for account {account_id} "
                            f"(now {checking_count + 1} checking)"
                        )
                    else:
                        logger.debug(
                            f"Acquired stream slot for account {account_id} "
                            f"({active_count} active + {checking_count + 1} checking = "
                            f"{total_in_use + 1}/{limit})"
                        )
                    return AcquireResult(True, 'acquired')

                if active_count >= limit:
                    last_wait_reason = 'active_viewers'
                else:
                    last_wait_reason = 'timeout'
            
            # No slot available, check timeout
            if timeout is not None:
                elapsed = time.time() - start_time
                if elapsed >= timeout:
                    log_method = logger.debug if timeout <= 0 else logger.warning
                    log_method(
                        f"Timeout acquiring slot for account {account_id} after {elapsed:.1f}s "
                        f"({active_count} active + {checking_count} checking = {total_in_use}/{limit})"
                    )
                    return AcquireResult(False, last_wait_reason)
            
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

    def reserve_profile_for_stream(self, stream: Dict[str, Any]) -> tuple[bool, str, Optional[Dict[str, Any]]]:
        """
        Reserve a concrete M3U account profile for a checker probe.

        Account limits protect the provider as a whole. Profile reservations
        protect individual credentials so concurrent checks do not all reuse the
        first free profile while another profile is available.
        """
        account_id = _get_stream_m3u_account_id(stream)
        if not account_id or not self.udi_manager:
            return (True, 'acquired', None)

        try:
            account_getter = getattr(self.udi_manager, 'get_m3u_account_by_id', None)
            account = account_getter(account_id) if callable(account_getter) else None
        except Exception as e:
            logger.warning(f"Could not get account {account_id} while reserving profile: {e}")
            return (True, 'acquired', None)

        profiles = account.get('profiles', []) if isinstance(account, dict) else []
        if not profiles:
            return (True, 'acquired', None)

        try:
            active_usage = {}
            context_getter = getattr(self.udi_manager, 'get_active_stream_context_per_profile', None)
            if callable(context_getter):
                context_usage = context_getter(account_id)
                if isinstance(context_usage, dict):
                    active_usage = context_usage
            if not active_usage:
                usage_getter = getattr(self.udi_manager, 'get_active_streams_count_per_profile', None)
                count_usage = usage_getter(account_id) if callable(usage_getter) else {}
                active_usage = count_usage if isinstance(count_usage, dict) else {}
        except Exception as e:
            logger.warning(f"Could not get active profile usage for account {account_id}: {e}")
            active_usage = {}

        checker_blocked = False
        external_blocked = False
        external_block_reason = None

        with self.lock:
            authoritative_profile_ids = self.account_profile_ids.get(account_id)
            for profile in profiles:
                if not isinstance(profile, dict):
                    continue

                profile_id = profile.get('id')
                if profile_id is None or not profile.get('is_active', True):
                    continue
                if (
                    authoritative_profile_ids is not None
                    and profile_id not in authoritative_profile_ids
                ):
                    continue

                max_streams = _safe_int(
                    self.profile_limits.get(
                        profile_id,
                        profile.get('max_streams', 0),
                    )
                )
                checking_count = self.profile_checking_counts.get(profile_id, 0)

                if max_streams == 0:
                    self.profile_checking_counts[profile_id] = checking_count + 1
                    logger.debug(
                        f"Reserved unlimited profile {profile_id} for stream {stream.get('id')} "
                        f"(now {checking_count + 1} checking)"
                    )
                    return (True, 'acquired', profile)

                usage_context = _usage_context(active_usage, profile_id)
                active_count = usage_context['active_streams']

                if active_count + checking_count < max_streams:
                    self.profile_checking_counts[profile_id] = checking_count + 1
                    logger.debug(
                        f"Reserved profile {profile_id} for stream {stream.get('id')} "
                        f"({active_count} active + {checking_count + 1} checking = "
                        f"{active_count + checking_count + 1}/{max_streams})"
                    )
                    return (True, 'acquired', profile)

                if checking_count > 0:
                    checker_blocked = True
                if active_count >= max_streams:
                    external_blocked = True
                    if usage_context.get('real_viewers', 0) > 0:
                        external_block_reason = 'active_viewers'
                    elif (
                        usage_context.get('shadow_watchers', 0) > 0
                        and external_block_reason != 'active_viewers'
                    ):
                        external_block_reason = 'shadow_watchers'

        if checker_blocked:
            return (False, 'checking_capacity', None)
        if external_blocked:
            return (False, external_block_reason or 'active_viewers', None)
        return (False, 'provider_capacity', None)

    def release_profile(self, profile: Optional[Dict[str, Any]]):
        """Release a checker-owned profile reservation."""
        if not profile:
            return

        profile_id = profile.get('id') if isinstance(profile, dict) else None
        if profile_id is None:
            return

        with self.lock:
            checking_count = self.profile_checking_counts.get(profile_id, 0)
            if checking_count > 0:
                self.profile_checking_counts[profile_id] = checking_count - 1
                logger.debug(
                    f"Released profile slot {profile_id} "
                    f"(now {self.profile_checking_counts[profile_id]} checking)"
                )
            else:
                logger.warning(
                    f"Attempted to release profile slot {profile_id} "
                    f"but checking count is already 0"
                )

    def get_profile_slot_snapshot(self, account_id: Optional[int]) -> List[Dict[str, Any]]:
        """Return a compact slot snapshot for active profiles on an account."""
        if account_id is None or not self.udi_manager:
            return []

        try:
            account_id = int(account_id)
        except (TypeError, ValueError):
            return []

        try:
            account_getter = getattr(self.udi_manager, 'get_m3u_account_by_id', None)
            account = account_getter(account_id) if callable(account_getter) else None
        except Exception as e:
            logger.warning(f"Could not get account {account_id} while building profile slot snapshot: {e}")
            return []

        profiles = account.get('profiles', []) if isinstance(account, dict) else []
        if not profiles:
            return []

        try:
            active_usage = {}
            context_getter = getattr(self.udi_manager, 'get_active_stream_context_per_profile', None)
            if callable(context_getter):
                context_usage = context_getter(account_id)
                if isinstance(context_usage, dict):
                    active_usage = context_usage
            if not active_usage:
                usage_getter = getattr(self.udi_manager, 'get_active_streams_count_per_profile', None)
                count_usage = usage_getter(account_id) if callable(usage_getter) else {}
                active_usage = count_usage if isinstance(count_usage, dict) else {}
        except Exception as e:
            logger.warning(f"Could not get active profile usage for account {account_id}: {e}")
            active_usage = {}

        with self.lock:
            checking_counts = dict(self.profile_checking_counts)
            configured_profile_limits = dict(self.profile_limits)
            configured_profile_ids = self.account_profile_ids.get(account_id)

        snapshots: List[Dict[str, Any]] = []
        for profile in profiles:
            if not isinstance(profile, dict):
                continue

            profile_id = profile.get('id')
            if profile_id is None or not profile.get('is_active', True):
                continue
            if configured_profile_ids is not None and profile_id not in configured_profile_ids:
                continue

            max_streams = _safe_int(
                configured_profile_limits.get(
                    profile_id,
                    profile.get('max_streams', 0),
                )
            )

            usage_context = _usage_context(active_usage, profile_id)
            active_count = usage_context['active_streams']
            checking_count = _safe_int(_mapping_value(checking_counts, profile_id))
            used = active_count + checking_count
            unlimited = max_streams == 0
            available = None if unlimited else max(0, max_streams - used)

            snapshots.append({
                'id': profile_id,
                'name': profile.get('name') or f'Profile {profile_id}',
                'limit': max_streams,
                'unlimited': unlimited,
                'active_viewers': active_count,
                'real_viewers': usage_context['real_viewers'],
                'shadow_watchers': usage_context['shadow_watchers'],
                'checking': checking_count,
                'used': used,
                'available': available,
                'full': False if unlimited else used >= max_streams,
            })

        return sorted(snapshots, key=lambda item: str(item.get('name', '')).lower())

    def should_preempt_profile_for_viewer(
        self,
        profile: Optional[Dict[str, Any]],
        account_id: Optional[int] = None,
        reservation_token: Optional[Any] = None,
    ) -> bool:
        """Return True when real viewers overcommit a reserved profile/account slot.

        ``account_id`` and ``reservation_token`` are optional for API
        compatibility. Supplying the account protects its aggregate cap even for
        unlimited/no-profile reservations. A token atomically claims at most one
        required preemption so simultaneous callbacks do not all abort when one
        released reservation is sufficient.
        """
        if not self.udi_manager:
            return False

        profile_id = profile.get('id') if isinstance(profile, dict) else None
        profile_limit = _safe_int(profile.get('max_streams', 0)) if isinstance(profile, dict) else 0

        try:
            resolved_account_id = int(account_id) if account_id not in (None, '') else None
        except (TypeError, ValueError):
            resolved_account_id = account_id
        if resolved_account_id is None and isinstance(profile, dict):
            resolved_account_id = _get_stream_m3u_account_id(
                {'m3u_account_id': profile.get('m3u_account_id')}
            )
        if resolved_account_id is None and profile_id is not None:
            finder = getattr(self.udi_manager, '_find_account_for_profile', None)
            try:
                resolved_account_id = finder(profile_id) if callable(finder) else None
            except Exception as e:
                logger.warning(f"Could not resolve account for profile {profile_id}: {e}")
                resolved_account_id = None

        current_profiles: List[Dict[str, Any]] = []
        if resolved_account_id is not None:
            account_getter = getattr(self.udi_manager, 'get_m3u_account_by_id', None)
            try:
                current_account = account_getter(resolved_account_id) if callable(account_getter) else None
                current_profiles = (
                    current_account.get('profiles', [])
                    if isinstance(current_account, dict)
                    else []
                )
                current_profile = next(
                    (
                        candidate
                        for candidate in current_profiles
                        if isinstance(candidate, dict)
                        and str(candidate.get('id')) == str(profile_id)
                    ),
                    None,
                )
                if current_profile is not None and profile_id is not None:
                    profile_limit = _safe_int(current_profile.get('max_streams', 0))
            except Exception as e:
                logger.warning(f"Could not refresh limit for profile {profile_id}: {e}")

        try:
            context_getter = getattr(self.udi_manager, 'get_active_stream_context_per_profile', None)
            context = None
            if callable(context_getter) and resolved_account_id is not None:
                candidate_context = context_getter(resolved_account_id)
                if isinstance(candidate_context, dict):
                    context = candidate_context

            profile_real_stream_count = None
            account_real_stream_count = None
            profile_active_stream_count = None
            account_active_stream_count = None
            if context is not None:
                if profile_id is not None:
                    profile_usage_context = _usage_context(context, profile_id)
                    profile_real_stream_count = profile_usage_context['real_viewer_streams']
                    profile_active_stream_count = profile_usage_context['active_streams']
                account_real_stream_count = sum(
                    _usage_context(context, context_profile_id)['real_viewer_streams']
                    for context_profile_id in context
                )
                account_active_stream_count = sum(
                    _usage_context(context, context_profile_id)['active_streams']
                    for context_profile_id in context
                )

            if profile_real_stream_count is None and profile_id is not None:
                active_getter = getattr(self.udi_manager, 'get_active_streams_for_profile', None)
                if callable(active_getter):
                    profile_real_stream_count = _safe_int(active_getter(profile_id))
                    profile_active_stream_count = profile_real_stream_count
            if profile_real_stream_count is None:
                profile_real_stream_count = 0
            if profile_active_stream_count is None:
                profile_active_stream_count = profile_real_stream_count
            if account_real_stream_count is None:
                # Legacy UDI implementations expose only current-profile usage.
                # This preserves existing behavior without classifying shadow
                # watchers from the aggregate active-stream API as real viewers.
                account_real_stream_count = profile_real_stream_count
            if account_active_stream_count is None:
                account_active_stream_count = profile_active_stream_count
        except Exception as e:
            logger.warning(f"Could not check active viewers for profile {profile_id}: {e}")
            return False

        with self.lock:
            if reservation_token is not None and reservation_token in self.viewer_preemption_claims:
                return True

            profile_checking_count = self.profile_checking_counts.get(profile_id, 0)
            account_checking_count = self.account_checking_counts.get(resolved_account_id, 0)
            account_limit = _safe_int(self.account_limits.get(resolved_account_id, 0))
            profile_limit = _safe_int(self.profile_limits.get(profile_id, profile_limit))
            claimed_profile_count = sum(
                1
                for _claimed_account_id, claimed_profile_id in self.viewer_preemption_claims.values()
                if claimed_profile_id == profile_id and profile_id is not None
            )
            claimed_account_count = sum(
                1
                for claimed_account_id, _claimed_profile_id in self.viewer_preemption_claims.values()
                if claimed_account_id == resolved_account_id and resolved_account_id is not None
            )

            unclaimed_profile_checks = max(
                0,
                profile_checking_count - claimed_profile_count,
            )
            unclaimed_account_checks = max(
                0,
                account_checking_count - claimed_account_count,
            )
            profile_excess = (
                min(
                    unclaimed_profile_checks,
                    max(
                        0,
                        profile_active_stream_count
                        + unclaimed_profile_checks
                        - profile_limit,
                    ),
                )
                if profile_id is not None
                and profile_limit > 0
                and profile_real_stream_count > 0
                else 0
            )
            account_excess = (
                max(
                    0,
                    account_active_stream_count
                    + unclaimed_account_checks
                    - account_limit,
                )
                if resolved_account_id is not None
                and account_limit > 0
                and account_real_stream_count > 0
                else 0
            )

            # Prefer reservations on profiles that are locally overcommitted.
            # Those releases also satisfy the aggregate account excess. Without
            # this priority, a sibling callback could claim the account slot first
            # and force an unnecessary second profile-local preemption.
            local_profile_excess = 0
            if context is not None:
                for current_profile in current_profiles:
                    if not isinstance(current_profile, dict):
                        continue
                    current_profile_id = current_profile.get('id')
                    current_limit = _safe_int(
                        self.profile_limits.get(
                            current_profile_id,
                            current_profile.get('max_streams', 0),
                        )
                    )
                    if current_profile_id is None or current_limit == 0:
                        continue
                    current_usage = _usage_context(context, current_profile_id)
                    current_real_streams = current_usage['real_viewer_streams']
                    current_active_streams = current_usage['active_streams']
                    if current_real_streams <= 0:
                        continue
                    current_checking = self.profile_checking_counts.get(current_profile_id, 0)
                    current_claims = sum(
                        1
                        for _claim_account_id, claim_profile_id in self.viewer_preemption_claims.values()
                        if claim_profile_id == current_profile_id
                    )
                    current_unclaimed_checks = max(0, current_checking - current_claims)
                    local_profile_excess += min(
                        current_unclaimed_checks,
                        max(
                            0,
                            current_active_streams
                            + current_unclaimed_checks
                            - current_limit,
                        ),
                    )

            account_only_excess = max(0, account_excess - local_profile_excess)
            should_preempt = profile_excess > 0 or account_only_excess > 0
            if should_preempt and reservation_token is not None:
                self.viewer_preemption_claims[reservation_token] = (
                    resolved_account_id,
                    profile_id,
                )
            return should_preempt

    def release_viewer_preemption_claim(self, reservation_token: Optional[Any]) -> None:
        """Release a token claimed by ``should_preempt_profile_for_viewer``."""
        if reservation_token is None:
            return
        with self.lock:
            self.viewer_preemption_claims.pop(reservation_token, None)
    
    def clear(self):
        """Clear all account limits and checking counts."""
        with self.lock:
            self.account_limits.clear()
            self.profile_limits.clear()
            self.account_profile_ids.clear()
            self.account_checking_counts.clear()
            self.profile_checking_counts.clear()
            self.viewer_preemption_claims.clear()
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

    @staticmethod
    def _normalize_wait_reason(reason: Optional[str]) -> str:
        """Return a UI/log friendly wait reason."""
        if reason == 'timeout':
            # AccountStreamLimiter uses "timeout" when checker-owned slots are full.
            return 'checking_capacity'
        if reason == 'shadow_watchers':
            return 'shadow_watchers'
        if isinstance(reason, str):
            reason_lower = reason.lower()
            if 'shadow' in reason_lower and 'watcher' in reason_lower:
                return 'shadow_watchers'
            if 'profile' in reason_lower and 'capacity' in reason_lower:
                # Legacy UDI profile capacity strings do not carry client class
                # context. StreamFlow-owned checker reservations are classified
                # by AccountStreamLimiter before this fallback is used.
                return 'active_viewers'
        return reason or 'provider_capacity'

    @staticmethod
    def _is_internal_capacity_wait(reason: str) -> bool:
        """Reasons caused by StreamFlow's own scheduler should wait for completion."""
        return reason in {'checking_capacity', 'global_worker_limit', 'provider_capacity'}
    
    def check_streams_with_limits(
        self,
        streams: List[Dict[str, Any]],
        check_function: Callable,
        progress_callback: Optional[Callable] = None,
        start_callback: Optional[Callable] = None,
        defer_callback: Optional[Callable] = None,
        stagger_delay: float = 0.0,
        abort_event: Optional[threading.Event] = None,
        provider_wait_timeout: Optional[float] = 300.0,
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
            defer_callback: Optional callback when a stream is waiting for provider capacity
            stagger_delay: Delay between starting tasks (default: 0.0)
            provider_wait_timeout: Maximum time a stream may wait when capacity is
                externally unavailable (for example active viewers/profile capacity).
                StreamFlow-owned checker saturation waits until a probe slot frees
                instead of skipping streams from the same provider.
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
            account_id = _get_stream_m3u_account_id(stream)
            account_groups[account_id].append(stream)
        
        logger.info(f"Streams grouped by account: {dict((k, len(v)) for k, v in account_groups.items())}")
        for account_id, account_streams in account_groups.items():
            limit = self.account_limiter.get_account_limit(account_id) if account_id else 0
            limit_str = "unlimited" if limit == 0 else str(limit)
            logger.info(f"  Account {account_id}: {len(account_streams)} streams, limit={limit_str}")

        # Account-wise round-robin keeps the scheduler from filling every
        # coordination worker with the same saturated provider before reaching
        # streams from providers that still have free capacity.
        scheduled_streams = []
        round_robin_groups = {account_id: list(account_streams) for account_id, account_streams in account_groups.items()}
        while round_robin_groups:
            for account_id in list(round_robin_groups.keys()):
                account_streams = round_robin_groups[account_id]
                if account_streams:
                    scheduled_streams.append(account_streams.pop(0))
                if not account_streams:
                    del round_robin_groups[account_id]
        
        results = []
        completed_count = 0
        lock = threading.Lock()
        
        # Submit all stream coordination tasks so a saturated account cannot block
        # the scheduling thread from reaching later streams on providers with free
        # capacity. The semaphore below enforces the real global FFmpeg probe limit.
        worker_count = min(
            total_streams,
            max(self.global_limit * 2, self.global_limit + len(account_groups), 16),
            64,
        )
        global_probe_slots = threading.Semaphore(max(1, self.global_limit))
        logger.info(
            "Using %s coordination workers for %s streams with %s global probe slots",
            worker_count,
            total_streams,
            self.global_limit,
        )

        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            futures: Dict[Future, Dict[str, Any]] = {}
            
            def submit_stream_check(stream: Dict[str, Any]):
                """Submit a stream check with provider-aware limit enforcement."""
                account_id = _get_stream_m3u_account_id(stream)

                def provider_wait_result(reason_detail: str) -> Dict[str, Any]:
                    cached_stats = {}
                    if self.account_limiter.udi_manager:
                        try:
                            cached_stream = self.account_limiter.udi_manager.get_stream_by_id(stream['id'])
                            if cached_stream and isinstance(cached_stream.get('stream_stats'), dict):
                                cached_stats = cached_stream.get('stream_stats') or {}
                        except Exception as e:
                            logger.error(f"Error retrieving cached stats for stream {stream['id']}: {e}")

                    skipped_reason = {
                        'active_viewers': 'quota_consumed_by_active_viewers',
                        'shadow_watchers': 'shadow_watcher_capacity',
                        'viewer_preempted': 'viewer_preempted',
                    }.get(reason_detail, 'provider_capacity_unavailable')
                    return {
                        'stream_id': stream['id'],
                        'stream_name': stream.get('name', 'Unknown'),
                        'stream_url': stream.get('url', ''),
                        'status': 'SKIPPED_PROVIDER_LIMIT',
                        'cached': bool(cached_stats),
                        'provider_limit_skipped': True,
                        'skipped_reason': skipped_reason,
                        'reason_detail': reason_detail,
                        **cached_stats,
                    }

                def check_stream_can_run() -> tuple[bool, Optional[str]]:
                    if not account_id or not self.account_limiter.udi_manager:
                        return (True, None)

                    checker = getattr(self.account_limiter.udi_manager, 'check_stream_can_run', None)
                    if not callable(checker):
                        return (True, None)

                    try:
                        result = checker(stream)
                    except Exception as e:
                        logger.warning(f"Could not check provider profile capacity for stream {stream['id']}: {e}")
                        return (True, None)

                    if isinstance(result, tuple) and len(result) >= 2:
                        return (bool(result[0]), result[1])

                    return (True, None)

                def wrapped_check(preempted_for_viewer: bool = False):
                    """Wrapper that waits for provider capacity without consuming probe slots."""
                    result = None
                    acquired_account = False
                    acquired_profile = None
                    acquired_global = False
                    wait_started = time.time()
                    wait_reason = None
                    retrying_after_preempt = False
                    preemption_token = object()
                    preemption_claimed = False

                    def release_current_reservation():
                        nonlocal acquired_global, acquired_profile, acquired_account
                        if acquired_global:
                            global_probe_slots.release()
                            acquired_global = False
                        if acquired_profile:
                            self.account_limiter.release_profile(acquired_profile)
                            acquired_profile = None
                        if acquired_account:
                            self.account_limiter.release(account_id)
                            acquired_account = False

                    def final_wait_reason(reason: str) -> str:
                        if preempted_for_viewer and reason == 'active_viewers':
                            return 'viewer_preempted'
                        return reason

                    def release_current_preemption_claim():
                        nonlocal preemption_claimed
                        if preemption_claimed:
                            self.account_limiter.release_viewer_preemption_claim(preemption_token)
                            preemption_claimed = False

                    try:
                        while True:
                            if abort_event and abort_event.is_set():
                                logger.info("Abort requested while waiting for provider capacity")
                                return None

                            can_run, reason = check_stream_can_run()

                            if can_run:
                                acquired_account, reason = self.account_limiter.acquire(account_id, timeout=0)
                                if acquired_account:
                                    profile_acquired, reason, acquired_profile = (
                                        self.account_limiter.reserve_profile_for_stream(stream)
                                    )
                                    if not profile_acquired:
                                        self.account_limiter.release(account_id)
                                        acquired_account = False
                                        acquired_profile = None
                                        wait_reason = self._normalize_wait_reason(reason)
                                        if defer_callback:
                                            try:
                                                with lock:
                                                    defer_callback(stream, wait_reason)
                                            except Exception as e:
                                                logger.error(f"Error in defer_callback for stream {stream['id']}: {e}")

                                        if (
                                            provider_wait_timeout is not None
                                            and not self._is_internal_capacity_wait(wait_reason)
                                        ):
                                            elapsed = time.time() - wait_started
                                            if elapsed >= provider_wait_timeout:
                                                logger.warning(
                                                    f"Provider capacity wait timed out for stream {stream['id']} "
                                                    f"after {elapsed:.1f}s: {wait_reason}"
                                                )
                                                result = provider_wait_result(final_wait_reason(wait_reason))
                                                return result

                                        time.sleep(0.5)
                                        continue

                                    acquired_global = global_probe_slots.acquire(blocking=False)
                                    if acquired_global:
                                        break
                                    self.account_limiter.release_profile(acquired_profile)
                                    acquired_profile = None
                                    self.account_limiter.release(account_id)
                                    acquired_account = False
                                    reason = 'global_worker_limit'

                            wait_reason = self._normalize_wait_reason(reason)
                            if defer_callback:
                                try:
                                    with lock:
                                        defer_callback(stream, wait_reason)
                                except Exception as e:
                                    logger.error(f"Error in defer_callback for stream {stream['id']}: {e}")

                            if (
                                provider_wait_timeout is not None
                                and not self._is_internal_capacity_wait(wait_reason)
                            ):
                                elapsed = time.time() - wait_started
                                if elapsed >= provider_wait_timeout:
                                    logger.warning(
                                        f"Provider capacity wait timed out for stream {stream['id']} "
                                        f"after {elapsed:.1f}s: {wait_reason}"
                                    )
                                    result = provider_wait_result(final_wait_reason(wait_reason))
                                    return result

                            time.sleep(0.5)

                        # Apply URL transformation if using M3U profile with search/replace patterns
                        stream_url = stream.get('url', '')
                        if self.account_limiter.udi_manager:
                            transformed_url = self.account_limiter.udi_manager.apply_profile_url_transformation(
                                stream,
                                profile=acquired_profile,
                            )
                            if isinstance(transformed_url, str):
                                stream_url = transformed_url

                        # Notify that this stream has acquired a slot and is starting.
                        # Lock protects stream_statuses which is shared across worker threads.
                        if start_callback:
                            try:
                                with lock:
                                    start_callback(stream, acquired_profile)
                            except Exception as e:
                                logger.error(f"Error in start_callback for stream {stream['id']}: {e}")

                        preempt_logged = False

                        def preempt_check() -> bool:
                            nonlocal preempt_logged, preemption_claimed
                            should_preempt = self.account_limiter.should_preempt_profile_for_viewer(
                                acquired_profile,
                                account_id=account_id,
                                reservation_token=preemption_token,
                            )
                            if should_preempt:
                                preemption_claimed = True
                            if should_preempt and not preempt_logged:
                                preempt_logged = True
                                logger.info(
                                    "Preempting stream check for stream %s because active viewer capacity is needed",
                                    stream.get('id'),
                                )
                            return should_preempt

                        runtime_params = dict(check_params)
                        if acquired_profile or account_id is not None:
                            runtime_params['preempt_check'] = preempt_check

                        result = check_function(
                            stream_url=stream_url,
                            stream_id=stream['id'],
                            stream_name=stream.get('name', 'Unknown'),
                            **runtime_params
                        )
                        if isinstance(result, dict) and result.get('preempted'):
                            logger.info(
                                "Retrying stream check for stream %s after viewer preemption",
                                stream.get('id'),
                            )
                            result = None
                            release_current_reservation()
                            release_current_preemption_claim()
                            wait_reason = 'viewer_preempted'
                            if defer_callback:
                                try:
                                    with lock:
                                        defer_callback(stream, wait_reason)
                                except Exception as e:
                                    logger.error(f"Error in defer_callback for stream {stream['id']}: {e}")
                            retrying_after_preempt = True
                            return wrapped_check(preempted_for_viewer=True)
                        return result
                    finally:
                        # Release account slot immediately when stream finishes
                        release_current_reservation()
                        release_current_preemption_claim()

                        # Fire progress callback from the worker thread the instant the
                        # stream completes — before the submission loop has finished
                        # stagger-sleeping through remaining streams. This is what makes
                        # the live grid update in real time for large stream counts.
                        with lock:
                            if result is not None:
                                results.append(result)
                            elif not retrying_after_preempt and not (abort_event and abort_event.is_set()):
                                results.append({
                                    'stream_id': stream['id'],
                                    'stream_name': stream.get('name', 'Unknown'),
                                    'stream_url': stream.get('url', ''),
                                    'status': 'ERROR',
                                    'resolution': '0x0',
                                    'bitrate_kbps': 0,
                                    'fps': 0,
                                    'video_codec': 'N/A',
                                    'audio_codec': 'N/A',
                                    'quality_reason': 'offline',
                                    'quality_reason_detail': 'error',
                                    'quality_reason_context': {
                                        'stage': 'stream analysis',
                                        'message': 'Stream analysis worker returned no result',
                                    },
                                })
                            if result is not None or (
                                not retrying_after_preempt
                                and not (abort_event and abort_event.is_set())
                            ):
                                nonlocal completed_count
                                completed_count += 1
                                current_completed = completed_count
                            else:
                                current_completed = completed_count

                        if not retrying_after_preempt:
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
            for stream in scheduled_streams:
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
    
    A positive account max_streams value remains the aggregate hard cap. Profile
    limits are enforced as additional sublimits. Only an unset/unlimited account
    limit falls back to the active profile aggregate; an unlimited active profile
    makes that fallback unlimited.
    
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
