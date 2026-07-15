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

import hashlib
import inspect
import re
import threading
import time
import uuid
from collections import defaultdict
from typing import Dict, List, Optional, Any, Callable
from concurrent.futures import ThreadPoolExecutor, Future
from apps.core.logging_config import setup_logging

logger = setup_logging(__name__)


_RESERVATION_TOKEN_KEY = '__streamflow_internal_reservation_token__'


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


def _profile_route_key(profile: Dict[str, Any]) -> Optional[str]:
    """Return a secret-safe identity for one configured credential route.

    Profiles with no search/replace pair all represent the stored/default URL.
    Rewrites with the same normalized target template represent the same
    credential route and must share capacity instead of multiplying it. The
    source regex is deliberately excluded: two syntactically different searches
    can still select the same provider login. Incomplete or invalid pairs are
    not usable routes.
    """
    if not isinstance(profile, dict):
        return None

    raw_search = profile.get('search_pattern')
    raw_replace = profile.get('replace_pattern')
    search_present = raw_search not in (None, '')
    replace_present = raw_replace not in (None, '')

    if search_present or replace_present:
        # An explicit rewrite is part of the route contract even for a default
        # profile.  Validate it before assigning any capacity: the strict URL
        # resolver rejects incomplete, empty, non-string, and invalid-regex
        # pairs, so the aggregate route map must reject them too.
        if not search_present or not replace_present:
            return None
        if not isinstance(raw_search, str) or not isinstance(raw_replace, str):
            return None
        search_pattern = raw_search.strip()
        replace_pattern = raw_replace.strip()
        if not search_pattern or not replace_pattern:
            return None
        try:
            re.compile(search_pattern)
        except re.error:
            return None

        if profile.get('is_default') is True:
            route_material = b'default-profile-route'
        else:
            route_material = (
                'profile-transform-target\0' + replace_pattern
            ).encode('utf-8')
    else:
        if profile.get('is_default') is False:
            return None
        route_material = b'default-profile-route'

    return hashlib.sha256(route_material).hexdigest()


def _profile_resolution_key(profile: Dict[str, Any]) -> Optional[str]:
    """Return a secret-safe fingerprint of URL-resolution semantics.

    The route key intentionally groups profiles by credential target. This
    separate key preserves the exact normalized inputs that decide whether and
    how one profile can resolve a stream, so a search-only refresh cannot commit
    a URL that was resolved from a stale profile payload.
    """
    if not isinstance(profile, dict):
        return None

    is_default = profile.get('is_default')
    if is_default is True:
        default_state = 'true'
    elif is_default is False:
        default_state = 'false'
    else:
        default_state = 'unset'

    def normalized_pattern(value: Any) -> str:
        if value in (None, ''):
            return 'absent'
        if isinstance(value, str):
            # Leading/trailing whitespace is discarded by the resolver too.
            # A whitespace-only string remains distinct from an absent value:
            # it is configured but invalid rather than a default-route noop.
            return f'string:{value.strip()}'
        return f'{type(value).__name__}:{str(value).strip()}'

    resolution_material = '\0'.join((
        'profile-resolution-v1',
        default_state,
        normalized_pattern(profile.get('search_pattern')),
        normalized_pattern(profile.get('replace_pattern')),
    )).encode('utf-8')
    return hashlib.sha256(resolution_material).hexdigest()


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


class ReservedProfile(dict):
    """Dictionary-compatible profile carrying one exact reservation identity."""

    def __init__(
        self,
        profile: Dict[str, Any],
        account_id: int,
        route_key: str,
        reservation_token: str,
    ):
        super().__init__(profile)
        # This UUID contains no profile or credential material and intentionally
        # survives ``dict(reserved_profile)`` so copied releases stay exact.
        self[_RESERVATION_TOKEN_KEY] = reservation_token
        self.account_id = account_id
        self.route_key = route_key


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
        self.profile_route_keys: Dict[int, str] = {}
        self.profile_resolution_keys: Dict[int, str] = {}
        self.account_route_keys: Dict[int, set[str]] = {}
        self.route_limits: Dict[tuple[int, str], int] = {}
        # Capacity comes only from active profiles, but a proxy session can
        # outlive a profile being disabled, removed, or rewritten.  Retain the
        # secret-safe route history needed to charge such observed usage to an
        # identical still-active credential route.  Account scoping avoids any
        # dependency on profile IDs being globally unique.
        self.account_profile_usage_route_keys: Dict[
            int,
            Dict[Any, set[str]],
        ] = {}
        self.account_checking_counts: Dict[int, int] = {}  # Track streams currently being checked
        self.profile_checking_counts: Dict[int, int] = {}  # Track checker-owned profile slots
        self.route_checking_counts: Dict[tuple[int, str], int] = {}
        self.profile_reservation_routes: Dict[Any, List[tuple[Any, str]]] = defaultdict(list)
        self.profile_reservations_by_token: Dict[
            str,
            tuple[Any, tuple[Any, str]],
        ] = {}
        self.viewer_preemption_claims: Dict[Any, tuple[Optional[int], Optional[int]]] = {}
        self.lock = threading.Lock()
        self.udi_manager = udi_manager
        logger.info("AccountStreamLimiter initialized")
    
    def set_account_limit(self, account_id: int, max_streams: int, profiles: List[Dict[str, Any]] = None):
        """
        Set the maximum concurrent streams for an account.
        
        Distinct active profile routes represent independent provider
        credentials. Their limits therefore define the aggregate account
        capacity: finite route limits are summed, while any unlimited route makes
        the aggregate unlimited. Profiles with the same target rewrite,
        including default aliases, share one route and use the strictest finite
        configured limit instead of multiplying capacity. The account-level
        value supplies aggregate fallback capacity only when there are no usable
        active profile routes. Reservation still fails closed when active
        profiles exist but none can resolve the current stream; it never falls
        back to the stored URL in that case.
        
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

            if profiles_provided:
                active_profile_limits_by_id: Dict[int, int] = {}
                active_profile_routes_by_id: Dict[int, str] = {}
                active_profile_resolution_keys_by_id: Dict[int, str] = {}
                route_limits_by_key: Dict[str, int] = {}
                current_profile_usage_routes_by_id: Dict[Any, set[str]] = {}
                for profile in profiles or []:
                    if not isinstance(profile, dict):
                        continue
                    profile_id = profile.get('id')
                    route_key = _profile_route_key(profile)
                    if profile_id is not None and route_key is not None:
                        current_profile_usage_routes_by_id.setdefault(
                            profile_id,
                            set(),
                        ).add(route_key)

                    if not profile.get('is_active', True):
                        # Inactive profiles add no capacity.  Their route is kept
                        # above solely so an already-running proxy session can
                        # still consume a matching active credential route.
                        continue
                    try:
                        profile_limit = max(0, int(profile.get('max_streams', 0) or 0))
                    except (TypeError, ValueError):
                        profile_limit = 0
                    if profile_id is not None:
                        active_profile_limits_by_id[profile_id] = profile_limit
                        resolution_key = _profile_resolution_key(profile)
                        if resolution_key is not None:
                            active_profile_resolution_keys_by_id[profile_id] = resolution_key
                        if route_key is not None:
                            active_profile_routes_by_id[profile_id] = route_key
                            previous_route_limit = route_limits_by_key.get(route_key)
                            if previous_route_limit is None:
                                route_limits_by_key[route_key] = profile_limit
                            elif previous_route_limit == 0:
                                route_limits_by_key[route_key] = profile_limit
                            elif profile_limit == 0:
                                route_limits_by_key[route_key] = previous_route_limit
                            else:
                                route_limits_by_key[route_key] = min(
                                    previous_route_limit,
                                    profile_limit,
                                )
            else:
                # Omitting profiles updates only the account fallback. Existing
                # imported profile routes remain authoritative and internally
                # consistent instead of leaving stale maps behind a new cap.
                existing_profile_ids = self.account_profile_ids.get(account_id, set())
                active_profile_limits_by_id = {
                    profile_id: self.profile_limits.get(profile_id, 0)
                    for profile_id in existing_profile_ids
                }
                active_profile_routes_by_id = {
                    profile_id: route_key
                    for profile_id in existing_profile_ids
                    if (route_key := self.profile_route_keys.get(profile_id)) is not None
                }
                active_profile_resolution_keys_by_id = {
                    profile_id: resolution_key
                    for profile_id in existing_profile_ids
                    if (
                        resolution_key := self.profile_resolution_keys.get(profile_id)
                    ) is not None
                }
                route_limits_by_key = {
                    route_key: self.route_limits.get((account_id, route_key), 0)
                    for route_key in self.account_route_keys.get(account_id, set())
                }

            route_limits = list(route_limits_by_key.values())
            profile_total = sum(route_limits)
            has_unlimited_profile = any(limit == 0 for limit in route_limits)

            if route_limits and has_unlimited_profile:
                total_limit = 0
                capacity_source = 'unlimited active credential-route aggregate'
            elif profile_total > 0:
                total_limit = profile_total
                capacity_source = 'finite active credential-route aggregate'
            elif account_limit > 0:
                total_limit = account_limit
                capacity_source = 'account fallback'
            else:
                total_limit = 0
                capacity_source = 'unlimited account fallback'

            if route_limits:
                logger.debug(
                    "Account %s capacity uses %s: account=%s, active profiles=%s, "
                    "distinct routes=%s, finite route sum=%s",
                    account_id,
                    capacity_source,
                    account_limit,
                    len(active_profile_limits_by_id),
                    len(route_limits_by_key),
                    profile_total,
                )
            
            # Store the calculated limit
            self.account_limits[account_id] = total_limit
            if profiles_provided:
                previous_profile_ids = self.account_profile_ids.get(account_id, set())
                current_profile_ids = set(active_profile_limits_by_id)
                for removed_profile_id in previous_profile_ids - current_profile_ids:
                    self.profile_limits.pop(removed_profile_id, None)
                    self.profile_route_keys.pop(removed_profile_id, None)
                    self.profile_resolution_keys.pop(removed_profile_id, None)
                self.profile_limits.update(active_profile_limits_by_id)
                self.profile_route_keys.update(active_profile_routes_by_id)
                self.profile_resolution_keys.update(active_profile_resolution_keys_by_id)
                for invalid_profile_id in current_profile_ids - set(active_profile_routes_by_id):
                    self.profile_route_keys.pop(invalid_profile_id, None)
                for invalid_profile_id in (
                    current_profile_ids - set(active_profile_resolution_keys_by_id)
                ):
                    self.profile_resolution_keys.pop(invalid_profile_id, None)
                self.account_profile_ids[account_id] = current_profile_ids

                previous_route_keys = self.account_route_keys.get(account_id, set())
                current_route_keys = set(route_limits_by_key)
                for removed_route_key in previous_route_keys - current_route_keys:
                    self.route_limits.pop((account_id, removed_route_key), None)
                for route_key, route_limit in route_limits_by_key.items():
                    self.route_limits[(account_id, route_key)] = route_limit
                self.account_route_keys[account_id] = current_route_keys

                retained_usage_routes = {
                    profile_id: set(route_keys)
                    for profile_id, route_keys in self.account_profile_usage_route_keys.get(
                        account_id,
                        {},
                    ).items()
                }
                for profile_id, route_keys in current_profile_usage_routes_by_id.items():
                    retained_usage_routes.setdefault(profile_id, set()).update(route_keys)
                self.account_profile_usage_route_keys[account_id] = retained_usage_routes
            
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

    def _resolve_profile_stream_url(
        self,
        stream: Dict[str, Any],
        profile: Dict[str, Any],
    ) -> tuple[bool, str, str]:
        """Resolve an explicit profile without permitting implicit reselection."""
        original_url = stream.get('url', '') if isinstance(stream, dict) else ''
        try:
            inspect.getattr_static(self.udi_manager, 'resolve_profile_stream_url')
        except AttributeError:
            resolver = None
        else:
            resolver = getattr(self.udi_manager, 'resolve_profile_stream_url', None)
        if callable(resolver):
            try:
                resolved = resolver(stream, profile)
            except Exception as exc:
                logger.warning(
                    "Profile URL resolution failed for profile %s: %s",
                    profile.get('id'),
                    type(exc).__name__,
                )
                return (False, '', 'profile_url_resolution_failed')
            if isinstance(resolved, tuple) and len(resolved) >= 3:
                eligible, resolved_url, reason = resolved[:3]
                if eligible and isinstance(resolved_url, str) and resolved_url:
                    return (True, resolved_url, str(reason or 'profile_url_resolved'))
                return (False, '', str(reason or 'profile_url_incompatible'))
            return (False, '', 'invalid_profile_url_resolution')

        # Compatibility for UDI doubles and older embedders. The explicit
        # profile is always passed; a transformer must never choose a sibling.
        raw_search = profile.get('search_pattern') if isinstance(profile, dict) else None
        raw_replace = profile.get('replace_pattern') if isinstance(profile, dict) else None
        search_present = raw_search not in (None, '')
        replace_present = raw_replace not in (None, '')
        if search_present != replace_present:
            return (False, '', 'incomplete_profile_url_transformation')
        if not search_present and not replace_present:
            if profile.get('is_default') is False:
                return (False, '', 'nondefault_profile_missing_url_transformation')
            return (
                (True, original_url, 'default_profile_url')
                if isinstance(original_url, str) and original_url
                else (False, '', 'missing_stream_url')
            )

        transformer = getattr(self.udi_manager, 'apply_profile_url_transformation', None)
        if callable(transformer):
            try:
                resolved_url = transformer(stream, profile=profile)
            except Exception as exc:
                logger.warning(
                    "Profile URL transformation failed for profile %s: %s",
                    profile.get('id'),
                    type(exc).__name__,
                )
                return (False, '', 'profile_url_transformation_failed')
        else:
            return (False, '', 'profile_url_transformer_unavailable')

        if not isinstance(resolved_url, str) or not resolved_url:
            return (False, '', 'invalid_profile_url')
        if search_present and resolved_url == original_url:
            if profile.get('is_default') is True:
                return (True, original_url, 'default_profile_url')
            return (False, '', 'profile_url_unchanged')
        return (True, resolved_url, 'profile_url_resolved')

    def reserve_profile_for_stream_with_url(
        self,
        stream: Dict[str, Any],
    ) -> tuple[bool, str, Optional[Dict[str, Any]], str]:
        """Reserve one profile and return the URL resolved for that reservation."""
        account_id = _get_stream_m3u_account_id(stream)
        if not account_id or not self.udi_manager:
            return (True, 'acquired', None, stream.get('url', ''))

        try:
            account_getter = getattr(self.udi_manager, 'get_m3u_account_by_id', None)
            account = account_getter(account_id) if callable(account_getter) else None
        except Exception as e:
            logger.warning(f"Could not get account {account_id} while reserving profile: {e}")
            return (False, 'provider_profile_unavailable', None, '')

        with self.lock:
            configured_profile_ids = self.account_profile_ids.get(account_id)

        if callable(account_getter) and not isinstance(account, dict):
            if configured_profile_ids is None:
                # Legacy/no-profile callers only configured an account limit.
                return (True, 'acquired', None, stream.get('url', ''))
            logger.warning("Account %s unavailable while reserving a profile", account_id)
            return (False, 'provider_profile_unavailable', None, '')

        profiles = account.get('profiles', []) if isinstance(account, dict) else []
        if not isinstance(profiles, list):
            profiles = []
        current_active_profiles = [
            profile
            for profile in profiles
            if isinstance(profile, dict)
            and profile.get('id') is not None
            and profile.get('is_active', True)
        ]
        current_active_profile_ids = {
            profile.get('id') for profile in current_active_profiles
        }
        current_route_keys = {
            profile.get('id'): _profile_route_key(profile)
            for profile in current_active_profiles
        }
        current_resolution_keys = {
            profile.get('id'): _profile_resolution_key(profile)
            for profile in current_active_profiles
        }

        def authoritative_snapshot_matches_locked(
            authoritative_profile_ids: Optional[set[Any]],
        ) -> bool:
            if authoritative_profile_ids is None:
                return True
            if set(authoritative_profile_ids) != current_active_profile_ids:
                return False
            return all(
                self.profile_route_keys.get(profile_id)
                == current_route_keys.get(profile_id)
                and self.profile_resolution_keys.get(profile_id)
                == current_resolution_keys.get(profile_id)
                for profile_id in current_active_profile_ids
            )

        # The raw account URL is a legitimate fallback only when the imported
        # limiter snapshot and the current UDI account snapshot agree that no
        # active provider profiles exist.  Empty/non-empty transitions are a
        # refresh race, not permission to probe with the main credentials.
        with self.lock:
            configured_profile_ids = self.account_profile_ids.get(account_id)
            snapshots_match = authoritative_snapshot_matches_locked(
                configured_profile_ids
            )
        if not snapshots_match:
            logger.warning(
                "Provider profile snapshot changed while reserving account %s",
                account_id,
            )
            return (False, 'provider_profile_unavailable', None, '')
        if not current_active_profile_ids:
            return (True, 'acquired', None, stream.get('url', ''))

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
        compatible_profile_seen = False
        authoritative_candidate_seen = False

        candidates = []
        for profile in current_active_profiles:
            profile_id = profile.get('id')
            # Resolve against the account payload first. The authoritative map is
            # checked again under the reservation lock below so a concurrent
            # profile refresh cannot commit a stale credential route.
            route_key = _profile_route_key(profile)
            if route_key is None:
                continue
            route_eligible, resolved_url, _route_reason = self._resolve_profile_stream_url(
                stream,
                profile,
            )
            if not route_eligible:
                continue
            compatible_profile_seen = True
            resolution_key = _profile_resolution_key(profile)
            candidates.append((
                profile,
                profile_id,
                route_key,
                resolution_key,
                resolved_url,
            ))

        with self.lock:
            authoritative_profile_ids = self.account_profile_ids.get(account_id)
            if not authoritative_snapshot_matches_locked(authoritative_profile_ids):
                return (False, 'provider_profile_unavailable', None, '')

            authoritative_route_profile_ids: Dict[str, set[Any]] = defaultdict(set)
            if authoritative_profile_ids is not None:
                for member_profile_id in authoritative_profile_ids:
                    member_route_key = self.profile_route_keys.get(member_profile_id)
                    if member_route_key is not None:
                        authoritative_route_profile_ids[member_route_key].add(
                            member_profile_id
                        )
            else:
                for member_profile in profiles:
                    if (
                        not isinstance(member_profile, dict)
                        or not member_profile.get('is_active', True)
                    ):
                        continue
                    member_profile_id = member_profile.get('id')
                    member_route_key = _profile_route_key(member_profile)
                    if member_profile_id is not None and member_route_key is not None:
                        authoritative_route_profile_ids[member_route_key].add(
                            member_profile_id
                        )

            usage_route_keys_by_profile = {
                profile_id: set(route_keys)
                for profile_id, route_keys in self.account_profile_usage_route_keys.get(
                    account_id,
                    {},
                ).items()
            }
            # Legacy callers may not have imported a profile snapshot.  The
            # current account payload still supplies enough route identity to
            # attribute inactive-profile usage conservatively.
            for member_profile in profiles:
                if not isinstance(member_profile, dict):
                    continue
                member_profile_id = member_profile.get('id')
                member_route_key = _profile_route_key(member_profile)
                if member_profile_id is not None and member_route_key is not None:
                    usage_route_keys_by_profile.setdefault(
                        member_profile_id,
                        set(),
                    ).add(member_route_key)

            # Inactive or just-removed profiles add no route capacity, but an
            # observed live session on one must consume an identical active
            # credential route until that upstream session disappears.
            active_route_keys = set(authoritative_route_profile_ids)
            for usage_profile_id, usage_route_keys in usage_route_keys_by_profile.items():
                if _usage_context(active_usage, usage_profile_id)['active_streams'] <= 0:
                    continue
                for usage_route_key in usage_route_keys & active_route_keys:
                    authoritative_route_profile_ids[usage_route_key].add(
                        usage_profile_id
                    )

            for (
                profile,
                profile_id,
                route_key,
                resolution_key,
                resolved_url,
            ) in candidates:
                if (
                    authoritative_profile_ids is not None
                    and profile_id not in authoritative_profile_ids
                ):
                    continue
                if (
                    authoritative_profile_ids is not None
                    and self.profile_route_keys.get(profile_id) != route_key
                ):
                    continue
                if (
                    authoritative_profile_ids is not None
                    and self.profile_resolution_keys.get(profile_id) != resolution_key
                ):
                    continue
                authoritative_candidate_seen = True

                max_streams = _safe_int(
                    self.profile_limits.get(
                        profile_id,
                        profile.get('max_streams', 0),
                    )
                )
                checking_count = self.profile_checking_counts.get(profile_id, 0)
                usage_context = _usage_context(active_usage, profile_id)
                active_count = usage_context['active_streams']
                profile_available = (
                    max_streams == 0
                    or active_count + checking_count < max_streams
                )

                route_identity = (account_id, route_key)
                route_limit = _safe_int(
                    self.route_limits.get(route_identity, max_streams)
                )
                route_checking_count = self.route_checking_counts.get(route_identity, 0)
                route_usage_contexts = [
                    _usage_context(active_usage, route_profile_id)
                    for route_profile_id in authoritative_route_profile_ids.get(
                        route_key,
                        {profile_id},
                    )
                ]
                route_active_count = sum(
                    context['active_streams'] for context in route_usage_contexts
                )
                route_available = (
                    route_limit == 0
                    or route_active_count + route_checking_count < route_limit
                )

                if profile_available and route_available:
                    reservation_token = uuid.uuid4().hex
                    while reservation_token in self.profile_reservations_by_token:
                        reservation_token = uuid.uuid4().hex
                    self.profile_checking_counts[profile_id] = checking_count + 1
                    self.route_checking_counts[route_identity] = route_checking_count + 1
                    self.profile_reservation_routes[profile_id].append(route_identity)
                    self.profile_reservations_by_token[reservation_token] = (
                        profile_id,
                        route_identity,
                    )
                    logger.debug(
                        f"Reserved profile {profile_id} for stream {stream.get('id')} "
                        f"({active_count} active + {checking_count + 1} checking = "
                        f"{active_count + checking_count + 1}/"
                        f"{'unlimited' if max_streams == 0 else max_streams})"
                    )
                    reserved_profile = ReservedProfile(
                        profile,
                        account_id,
                        route_key,
                        reservation_token,
                    )
                    return (True, 'acquired', reserved_profile, resolved_url)

                if checking_count > 0 or route_checking_count > 0:
                    checker_blocked = True
                profile_external_blocked = max_streams > 0 and active_count >= max_streams
                route_external_blocked = (
                    route_limit > 0 and route_active_count >= route_limit
                )
                if profile_external_blocked or route_external_blocked:
                    external_blocked = True
                    if any(context.get('real_viewers', 0) > 0 for context in route_usage_contexts):
                        external_block_reason = 'active_viewers'
                    elif (
                        any(
                            context.get('shadow_watchers', 0) > 0
                            for context in route_usage_contexts
                        )
                        and external_block_reason != 'active_viewers'
                    ):
                        external_block_reason = 'shadow_watchers'

        if checker_blocked:
            return (False, 'checking_capacity', None, '')
        if external_blocked:
            return (False, external_block_reason or 'active_viewers', None, '')
        if compatible_profile_seen:
            if not authoritative_candidate_seen:
                return (False, 'provider_profile_unavailable', None, '')
            return (False, 'provider_capacity', None, '')
        return (False, 'profile_url_incompatible', None, '')

    def reserve_profile_for_stream(self, stream: Dict[str, Any]) -> tuple[bool, str, Optional[Dict[str, Any]]]:
        """Reserve a concrete profile, preserving the legacy three-tuple API."""
        acquired, reason, profile, _resolved_url = self.reserve_profile_for_stream_with_url(stream)
        return (acquired, reason, profile)

    def release_profile(self, profile: Optional[Dict[str, Any]]):
        """Release a checker-owned profile reservation."""
        if not profile:
            return

        profile_id = profile.get('id') if isinstance(profile, dict) else None
        if profile_id is None:
            return

        token_present = _RESERVATION_TOKEN_KEY in profile
        reservation_token = profile.get(_RESERVATION_TOKEN_KEY)
        route_key = getattr(profile, 'route_key', None)
        route_account_id = getattr(profile, 'account_id', None)

        with self.lock:
            reservation_routes = self.profile_reservation_routes.get(profile_id, [])
            token_to_release = None
            if token_present:
                tracked_reservation = (
                    self.profile_reservations_by_token.get(reservation_token)
                    if isinstance(reservation_token, str)
                    else None
                )
                if tracked_reservation is None:
                    logger.warning(
                        "Refusing unknown or stale profile reservation token for "
                        "profile %s",
                        profile_id,
                    )
                    return
                tracked_profile_id, route_identity = tracked_reservation
                if (
                    tracked_profile_id != profile_id
                    or route_identity not in reservation_routes
                ):
                    logger.warning(
                        "Refusing mismatched profile reservation token for profile %s",
                        profile_id,
                    )
                    return
                token_to_release = reservation_token
            else:
                route_identity = (
                    (route_account_id, route_key)
                    if route_key is not None and route_account_id is not None
                    else None
                )
                if route_identity is not None and (
                    route_identity not in reservation_routes and reservation_routes
                ):
                    logger.warning(
                        "Refusing profile slot release for profile %s because its "
                        "reservation route is no longer pending",
                        profile_id,
                    )
                    return
                if route_identity is None and reservation_routes:
                    distinct_routes = set(reservation_routes)
                    if len(distinct_routes) > 1:
                        # A legacy copied profile without a token cannot identify
                        # which credential route it owns. Guessing could release a
                        # newer route and allow it to exceed provider capacity.
                        logger.warning(
                            "Refusing ambiguous copied profile release for profile %s "
                            "across multiple pending credential routes",
                            profile_id,
                        )
                        return
                    route_identity = next(iter(distinct_routes))

                # Preserve legacy unambiguous releases while retiring one token
                # that owns the selected route. Otherwise that token could later
                # be mistaken for a live reservation.
                if route_identity is not None:
                    token_to_release = next((
                        token
                        for token, tracked in self.profile_reservations_by_token.items()
                        if tracked == (profile_id, route_identity)
                    ), None)

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

            if route_identity is not None:
                if route_identity in reservation_routes:
                    reservation_routes.remove(route_identity)
                else:
                    route_identity = None
            if token_to_release is not None:
                self.profile_reservations_by_token.pop(token_to_release, None)
            if not reservation_routes:
                self.profile_reservation_routes.pop(profile_id, None)

            if route_identity is not None:
                route_checking_count = self.route_checking_counts.get(route_identity, 0)
                if route_checking_count > 0:
                    self.route_checking_counts[route_identity] = route_checking_count - 1
                else:
                    logger.warning(
                        "Attempted to release credential-route slot for profile %s "
                        "but its checking count is already 0",
                        profile_id,
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
            configured_profile_ids_value = self.account_profile_ids.get(account_id)
            configured_profile_ids = (
                set(configured_profile_ids_value)
                if configured_profile_ids_value is not None
                else None
            )
            configured_route_keys = dict(self.profile_route_keys)
            configured_route_limits = dict(self.route_limits)
            route_checking_counts = dict(self.route_checking_counts)
            usage_route_keys_by_profile = {
                profile_id: set(route_keys)
                for profile_id, route_keys in self.account_profile_usage_route_keys.get(
                    account_id,
                    {},
                ).items()
            }

        profile_records: List[tuple[Dict[str, Any], Any, Optional[str]]] = []
        route_profile_ids: Dict[str, set[Any]] = defaultdict(set)
        for profile in profiles:
            if not isinstance(profile, dict):
                continue
            profile_id = profile.get('id')
            if profile_id is None or not profile.get('is_active', True):
                continue
            if configured_profile_ids is not None and profile_id not in configured_profile_ids:
                continue
            route_key = (
                configured_route_keys.get(profile_id)
                if configured_profile_ids is not None
                else _profile_route_key(profile)
            )
            profile_records.append((profile, profile_id, route_key))
            if route_key is not None:
                route_profile_ids[route_key].add(profile_id)

        route_usage: Dict[str, Dict[str, int]] = {}
        route_capacity_representatives = {
            route_key: min(
                profile_ids,
                key=lambda profile_id: (str(profile_id), type(profile_id).__name__),
            )
            for route_key, profile_ids in route_profile_ids.items()
            if profile_ids
        }

        for profile in profiles:
            if not isinstance(profile, dict):
                continue
            profile_id = profile.get('id')
            route_key = _profile_route_key(profile)
            if profile_id is not None and route_key is not None:
                usage_route_keys_by_profile.setdefault(profile_id, set()).add(route_key)

        active_route_keys = set(route_profile_ids)
        for usage_profile_id, usage_route_keys in usage_route_keys_by_profile.items():
            if _usage_context(active_usage, usage_profile_id)['active_streams'] <= 0:
                continue
            for usage_route_key in usage_route_keys & active_route_keys:
                route_profile_ids[usage_route_key].add(usage_profile_id)

        for route_key, profile_ids in route_profile_ids.items():
            contexts = [_usage_context(active_usage, profile_id) for profile_id in profile_ids]
            route_identity = (account_id, route_key)
            route_checking = route_checking_counts.get(route_identity)
            if route_checking is None:
                route_checking = sum(
                    _safe_int(_mapping_value(checking_counts, profile_id))
                    for profile_id in profile_ids
                )
            route_usage[route_key] = {
                'active_streams': sum(context['active_streams'] for context in contexts),
                'real_viewers': sum(context['real_viewers'] for context in contexts),
                'shadow_watchers': sum(context['shadow_watchers'] for context in contexts),
                'checking': _safe_int(route_checking),
            }

        snapshots: List[Dict[str, Any]] = []
        for profile, profile_id, route_key in profile_records:
            max_streams = _safe_int(
                configured_profile_limits.get(
                    profile_id,
                    profile.get('max_streams', 0),
                )
            )

            if route_key is not None:
                usage_context = route_usage[route_key]
                active_count = usage_context['active_streams']
                checking_count = usage_context['checking']
                effective_limit = _safe_int(
                    configured_route_limits.get(
                        (account_id, route_key),
                        max_streams,
                    )
                )
            else:
                usage_context = _usage_context(active_usage, profile_id)
                active_count = usage_context['active_streams']
                checking_count = _safe_int(_mapping_value(checking_counts, profile_id))
                effective_limit = max_streams

            used = active_count + checking_count
            unlimited = effective_limit == 0
            available = None if unlimited else max(0, effective_limit - used)

            snapshot = {
                'id': profile_id,
                'name': profile.get('name') or f'Profile {profile_id}',
                'limit': effective_limit,
                'unlimited': unlimited,
                'active_viewers': active_count,
                'real_viewers': usage_context['real_viewers'],
                'shadow_watchers': usage_context['shadow_watchers'],
                'checking': checking_count,
                'used': used,
                'available': available,
                'full': False if unlimited else used >= effective_limit,
                'capacity_counted': (
                    route_key is not None
                    and route_capacity_representatives.get(route_key) == profile_id
                ),
            }
            if route_key is None:
                snapshot['route_usable'] = False
            if route_key is not None and len(route_profile_ids[route_key]) > 1:
                snapshot['shared_route'] = True
            snapshots.append(snapshot)

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
        reservation_route_key = getattr(profile, 'route_key', None)

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
            if reservation_route_key is None and profile_id is not None:
                reservation_route_key = self.profile_route_keys.get(profile_id)
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

            def route_excess_for(route_key: Optional[str]) -> int:
                if (
                    route_key is None
                    or resolved_account_id is None
                    or context is None
                ):
                    return 0
                route_identity = (resolved_account_id, route_key)
                route_limit = _safe_int(self.route_limits.get(route_identity, 0))
                if route_limit == 0:
                    return 0
                route_profile_ids = {
                    candidate.get('id')
                    for candidate in current_profiles
                    if isinstance(candidate, dict)
                    and self.profile_route_keys.get(candidate.get('id')) == route_key
                }
                route_profile_ids.discard(None)
                route_real_streams = sum(
                    _usage_context(context, route_profile_id)['real_viewer_streams']
                    for route_profile_id in route_profile_ids
                )
                if route_real_streams <= 0:
                    return 0
                route_active_streams = sum(
                    _usage_context(context, route_profile_id)['active_streams']
                    for route_profile_id in route_profile_ids
                )
                route_checking = self.route_checking_counts.get(route_identity, 0)
                route_claims = sum(
                    1
                    for claim_account_id, claim_profile_id in self.viewer_preemption_claims.values()
                    if claim_account_id == resolved_account_id
                    and self.profile_route_keys.get(claim_profile_id) == route_key
                )
                unclaimed_route_checks = max(0, route_checking - route_claims)
                return min(
                    unclaimed_route_checks,
                    max(
                        0,
                        route_active_streams
                        + unclaimed_route_checks
                        - route_limit,
                    ),
                )

            reservation_route_excess = route_excess_for(reservation_route_key)

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

            local_route_excess = sum(
                route_excess_for(route_key)
                for route_key in self.account_route_keys.get(resolved_account_id, set())
            )
            local_capacity_excess = max(local_profile_excess, local_route_excess)
            account_only_excess = max(0, account_excess - local_capacity_excess)
            should_preempt = (
                profile_excess > 0
                or reservation_route_excess > 0
                or account_only_excess > 0
            )
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
            self.profile_route_keys.clear()
            self.profile_resolution_keys.clear()
            self.account_route_keys.clear()
            self.route_limits.clear()
            self.account_profile_usage_route_keys.clear()
            self.account_checking_counts.clear()
            self.profile_checking_counts.clear()
            self.route_checking_counts.clear()
            self.profile_reservation_routes.clear()
            self.profile_reservations_by_token.clear()
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
                        'profile_url_incompatible': 'profile_url_incompatible',
                        'provider_profile_unavailable': 'provider_profile_unavailable',
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
                    acquired_stream_url = None
                    acquired_global = False
                    wait_started = time.time()
                    wait_reason = None
                    retrying_after_preempt = False
                    preemption_token = object()
                    preemption_claimed = False

                    def release_current_reservation():
                        nonlocal acquired_global, acquired_profile, acquired_stream_url, acquired_account
                        if acquired_global:
                            global_probe_slots.release()
                            acquired_global = False
                        if acquired_profile:
                            self.account_limiter.release_profile(acquired_profile)
                            acquired_profile = None
                        acquired_stream_url = None
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

                            # reserve_profile_for_stream_with_url is the
                            # authoritative capacity and route check. Calling the
                            # older UDI pre-check first can hide an incompatible
                            # rewrite behind a generic capacity response.
                            can_run, reason = (True, None)

                            if can_run:
                                acquired_account, reason = self.account_limiter.acquire(account_id, timeout=0)
                                if acquired_account:
                                    profile_acquired, reason, acquired_profile, acquired_stream_url = (
                                        self.account_limiter.reserve_profile_for_stream_with_url(stream)
                                    )
                                    if not profile_acquired:
                                        self.account_limiter.release(account_id)
                                        acquired_account = False
                                        acquired_profile = None
                                        acquired_stream_url = None
                                        wait_reason = self._normalize_wait_reason(reason)
                                        if defer_callback:
                                            try:
                                                with lock:
                                                    defer_callback(stream, wait_reason)
                                            except Exception as e:
                                                logger.error(f"Error in defer_callback for stream {stream['id']}: {e}")

                                        if wait_reason in {
                                            'profile_url_incompatible',
                                            'provider_profile_unavailable',
                                        }:
                                            result = provider_wait_result(wait_reason)
                                            return result

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
                                    acquired_stream_url = None
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

                        # The URL was resolved while reserving this exact profile.
                        # Never transform again here: doing so could reselect or
                        # fall back to another credential after capacity was taken.
                        stream_url = acquired_stream_url
                        if not isinstance(stream_url, str) or not stream_url:
                            logger.error(
                                "Reserved profile for stream %s without a usable URL",
                                stream.get('id'),
                            )
                            result = provider_wait_result('profile_url_incompatible')
                            return result

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
                        if isinstance(result, dict):
                            # Credential-rewritten URLs are probe-only. Persisted,
                            # returned, and callback-visible results must retain
                            # the canonical raw URL associated with the stream.
                            result['stream_url'] = stream.get('url', '')
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
    
    Distinct usable profile routes represent independent provider credentials,
    so their limits form the aggregate account capacity and remain individually
    enforced. Profiles with the same credential target, including default
    aliases, do not multiply capacity. The account-level max_streams value is
    the aggregate fallback only when no usable active profile routes are
    available. A stream with active profiles but no resolvable route still fails
    closed instead of using the stored URL.
    
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
