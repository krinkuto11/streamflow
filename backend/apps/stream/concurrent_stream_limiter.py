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
from contextlib import ExitStack, nullcontext
from typing import Dict, List, Optional, Any, Callable
from concurrent.futures import ThreadPoolExecutor, Future
from apps.core.logging_config import setup_logging

logger = setup_logging(__name__)


_RESERVATION_TOKEN_KEY = '__streamflow_internal_reservation_token__'
_INVALID_PROVIDER_ACCOUNT_ID = object()


def _strict_positive_id(value: Any) -> Optional[int]:
    """Normalize provider authority IDs without bool/zero/alias collisions."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, str):
        stripped = value.strip()
        if re.fullmatch(r'[1-9][0-9]*', stripped):
            return int(stripped)
    return None


def _get_stream_m3u_account_id(stream: Dict[str, Any]) -> Optional[Any]:
    """Return a stream's M3U account id across legacy and SQL payloads."""
    if not isinstance(stream, dict):
        return None
    account_id = stream.get('m3u_account_id')
    if account_id in (None, ''):
        account_id = stream.get('m3u_account')
    if account_id in (None, ''):
        return (
            None
            if stream.get('is_custom') is True
            else _INVALID_PROVIDER_ACCOUNT_ID
        )
    normalized = _strict_positive_id(account_id)
    return normalized if normalized is not None else _INVALID_PROVIDER_ACCOUNT_ID


def _safe_int(value: Any) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, parsed)


def _strict_nonnegative_count(value: Any) -> Optional[int]:
    """Return a trustworthy provider-usage count, or ``None`` if malformed."""
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        return None
    return value


def _strict_configured_limit(
    payload: Dict[str, Any],
    field: str = 'max_streams',
) -> Optional[int]:
    """Validate a configured limit while preserving omitted/null = unlimited."""
    if field not in payload or payload.get(field) is None:
        return 0
    return _strict_nonnegative_count(payload.get(field))


def _usage_mapping_is_trusted(
    mapping: Any,
    *,
    contextual: bool,
) -> bool:
    """Validate one authoritative UDI profile-usage response without coercion."""
    if not isinstance(mapping, dict):
        return False
    normalized_profile_ids = set()
    for profile_id, value in mapping.items():
        normalized_profile_id = _strict_positive_id(profile_id)
        if (
            normalized_profile_id is None
            or normalized_profile_id in normalized_profile_ids
        ):
            return False
        normalized_profile_ids.add(normalized_profile_id)
        if contextual:
            if not isinstance(value, dict):
                return False
            required_counts = ('active_streams', 'real_viewers', 'shadow_watchers')
            if any(
                _strict_nonnegative_count(value.get(field)) is None
                for field in required_counts
            ):
                return False
            if (
                'real_viewer_streams' in value
                and _strict_nonnegative_count(value.get('real_viewer_streams')) is None
            ):
                return False
        elif _strict_nonnegative_count(value) is None:
            return False
    return True


def _active_profile_snapshot_is_trusted(profiles: Any) -> bool:
    """Reject ambiguous active-profile authority before any raw/provider probe."""
    if not isinstance(profiles, list):
        return False
    profile_ids = set()
    for profile in profiles:
        if not isinstance(profile, dict):
            return False
        active_value = profile.get('is_active', True)
        if not isinstance(active_value, bool):
            return False
        default_value = profile.get('is_default')
        if default_value is not None and not isinstance(default_value, bool):
            return False
        profile_id = _strict_positive_id(profile.get('id'))
        if profile_id is None:
            return False
        if profile_id in profile_ids:
            return False
        profile_ids.add(profile_id)
        if _strict_configured_limit(profile) is None:
            return False
    return True


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


_DEFAULT_PROFILE_ROUTE_KEY = hashlib.sha256(b'default-profile-route').hexdigest()


def _canonical_profile_replace_pattern(replace_pattern: str) -> str:
    """Apply the same ``$n`` back-reference normalization as UDI."""
    normalized = replace_pattern
    for index in range(99, 0, -1):
        normalized = normalized.replace(f'${index}', f'\\{index}')
    return normalized


def _profile_route_alias_keys(profile: Dict[str, Any]) -> Optional[set[str]]:
    """Return every credential route an active profile can represent.

    An explicit default rewrite bridges the stored/default route and its
    canonical rewrite target.  That conservative bridge is required because a
    no-op rewrite can resolve to the exact stored URL while another profile
    reaches the same credentials through an explicit target.  Treating either
    side as independent would multiply provider capacity.
    """
    if not isinstance(profile, dict):
        return None

    raw_search = profile.get('search_pattern')
    raw_replace = profile.get('replace_pattern')
    search_present = raw_search not in (None, '')
    replace_present = raw_replace not in (None, '')

    if search_present or replace_present:
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

        canonical_target = _canonical_profile_replace_pattern(replace_pattern)
        target_key = hashlib.sha256(
            ('profile-transform-target\0' + canonical_target).encode('utf-8')
        ).hexdigest()
        alias_keys = {target_key}
        if profile.get('is_default') is True:
            alias_keys.add(_DEFAULT_PROFILE_ROUTE_KEY)
        return alias_keys

    if profile.get('is_default') is False:
        return None
    return {_DEFAULT_PROFILE_ROUTE_KEY}


def _profile_route_key(profile: Dict[str, Any]) -> Optional[str]:
    """Return the primary secret-safe route identity for one profile.

    Profiles with no search/replace pair all represent the stored/default URL.
    Rewrites with the same normalized target template represent the same
    credential route and must share capacity instead of multiplying it. The
    source regex is deliberately excluded: two syntactically different searches
    can still select the same provider login. Incomplete or invalid pairs are
    not usable routes.
    """
    alias_keys = _profile_route_alias_keys(profile)
    if not alias_keys:
        return None
    if len(alias_keys) == 1:
        return next(iter(alias_keys))
    # For explicit default rewrites the rewrite target is the primary route;
    # component construction below connects it to the stored/default route.
    return next(
        route_key
        for route_key in alias_keys
        if route_key != _DEFAULT_PROFILE_ROUTE_KEY
    )


def _build_profile_route_snapshot(
    profiles: List[Dict[str, Any]],
) -> tuple[
    Dict[Any, str],
    Dict[str, int],
    Dict[Any, set[str]],
    Dict[str, set[str]],
]:
    """Build connected credential-route components for one account snapshot."""
    active_records: List[tuple[Any, int, set[str]]] = []
    parent: Dict[str, str] = {}

    def find(route_key: str) -> str:
        root = route_key
        while parent[root] != root:
            root = parent[root]
        while parent[route_key] != route_key:
            next_key = parent[route_key]
            parent[route_key] = root
            route_key = next_key
        return root

    def union(left: str, right: str) -> None:
        left_root = find(left)
        right_root = find(right)
        if left_root != right_root:
            parent[right_root] = left_root

    for profile in profiles or []:
        if not isinstance(profile, dict) or not profile.get('is_active', True):
            continue
        profile_id = _strict_positive_id(profile.get('id'))
        alias_keys = _profile_route_alias_keys(profile)
        if profile_id is None or not alias_keys:
            continue
        for route_key in alias_keys:
            parent.setdefault(route_key, route_key)
        first_key = next(iter(alias_keys))
        for route_key in alias_keys:
            union(first_key, route_key)
        active_records.append((
            profile_id,
            _safe_int(profile.get('max_streams', 0)),
            set(alias_keys),
        ))

    component_members: Dict[str, set[str]] = defaultdict(set)
    for route_key in parent:
        component_members[find(route_key)].add(route_key)

    component_keys: Dict[str, str] = {}
    for root, members in component_members.items():
        if len(members) == 1:
            component_keys[root] = next(iter(members))
        else:
            component_keys[root] = hashlib.sha256(
                ('credential-route-component\0' + '\0'.join(sorted(members))).encode(
                    'utf-8'
                )
            ).hexdigest()

    routes_by_profile: Dict[Any, str] = {}
    route_limits: Dict[str, int] = {}
    component_aliases: Dict[str, set[str]] = {}
    for root, members in component_members.items():
        component_aliases[component_keys[root]] = set(members)

    for profile_id, profile_limit, alias_keys in active_records:
        component_key = component_keys[find(next(iter(alias_keys)))]
        routes_by_profile[profile_id] = component_key
        previous_limit = route_limits.get(component_key)
        if previous_limit is None:
            route_limits[component_key] = profile_limit
        elif previous_limit == 0:
            route_limits[component_key] = profile_limit
        elif profile_limit != 0:
            route_limits[component_key] = min(previous_limit, profile_limit)

    usage_aliases_by_profile: Dict[Any, set[str]] = {}
    for profile in profiles or []:
        if not isinstance(profile, dict):
            continue
        profile_id = _strict_positive_id(profile.get('id'))
        alias_keys = _profile_route_alias_keys(profile)
        if profile_id is None or not alias_keys:
            continue
        usage_aliases_by_profile[profile_id] = set(alias_keys)

    return (
        routes_by_profile,
        route_limits,
        usage_aliases_by_profile,
        component_aliases,
    )


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


def _account_authority_fingerprint(
    account: Any,
    expected_account_id: int,
) -> Optional[tuple[Any, ...]]:
    """Return a secret-free, equality-safe capacity/route snapshot."""
    if not isinstance(account, dict):
        return None
    if (
        'id' in account
        and _strict_positive_id(account.get('id')) != expected_account_id
    ):
        return None
    account_limit = _strict_configured_limit(account)
    profiles = account.get('profiles', [])
    if account_limit is None or not _active_profile_snapshot_is_trusted(profiles):
        return None

    (
        routes_by_profile,
        route_limits,
        usage_aliases_by_profile,
        component_aliases,
    ) = _build_profile_route_snapshot(profiles)
    active_profile_records = []
    for profile in profiles:
        if not profile.get('is_active', True):
            continue
        profile_id = _strict_positive_id(profile.get('id'))
        active_profile_records.append((
            profile_id,
            _strict_configured_limit(profile),
            routes_by_profile.get(profile_id),
            _profile_resolution_key(profile),
        ))

    sort_key = lambda item: (type(item[0]).__name__, str(item[0]))
    return (
        account_limit,
        tuple(sorted(active_profile_records, key=sort_key)),
        tuple(sorted(route_limits.items())),
        tuple(sorted(
            (
                profile_id,
                tuple(sorted(alias_keys)),
            )
            for profile_id, alias_keys in usage_aliases_by_profile.items()
        )),
        tuple(sorted(
            (
                component_key,
                tuple(sorted(alias_keys)),
            )
            for component_key, alias_keys in component_aliases.items()
        )),
    )


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
        effective_limit: int,
        authority_fingerprint: Optional[tuple[Any, ...]] = None,
    ):
        super().__init__(profile)
        # This UUID contains no profile or credential material and intentionally
        # survives ``dict(reserved_profile)`` so copied releases stay exact.
        self[_RESERVATION_TOKEN_KEY] = reservation_token
        self.account_id = account_id
        self.route_key = route_key
        # The enforced limit can be stricter than the profile's raw setting
        # when multiple profile aliases share one credential route. Keep the
        # normalized, secret-free value on the reservation object so callbacks
        # report the capacity that was actually acquired.
        self.effective_limit = effective_limit
        self.authority_fingerprint = authority_fingerprint


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
        self.account_fallback_limits: Dict[int, int] = {}
        self.account_fallback_limits_trusted: Dict[int, bool] = {}
        self.account_inventory_initialized = False
        self.account_inventory_trusted = False
        self.account_inventory_ids: set[int] = set()
        self.profile_limits: Dict[int, int] = {}
        self.account_profile_ids: Dict[int, set[int]] = {}
        self.account_profile_snapshots_trusted: Dict[int, bool] = {}
        self.profile_route_keys: Dict[int, str] = {}
        self.profile_resolution_keys: Dict[int, str] = {}
        self.account_route_keys: Dict[int, set[str]] = {}
        self.route_limits: Dict[tuple[int, str], int] = {}
        # Capacity comes only from active profiles, but a proxy session can
        # outlive a profile being disabled, removed, or rewritten.  Retain the
        # secret-safe primitive-alias history needed to charge such observed
        # usage into the current connected credential-route component even when
        # that component expands or shrinks. Account scoping avoids any
        # dependency on historical component hashes.
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
        self.account_reservation_authority_by_thread: Dict[
            tuple[int, int],
            tuple[Any, ...],
        ] = {}
        # Snapshot imports take this lock across validation/publication and call
        # the single-account setter recursively.  An RLock keeps the inventory
        # update atomic to all reservation readers.
        self.lock = threading.RLock()
        self.udi_manager = udi_manager
        logger.info("AccountStreamLimiter initialized")

    def invalidate_account_inventory(self) -> None:
        """Revoke provider authority until a complete snapshot is published."""
        with self.lock:
            self.account_inventory_initialized = True
            self.account_inventory_trusted = False
            self.account_inventory_ids.clear()

    def _account_authority_is_trusted_locked(self, account_id: Any) -> bool:
        return bool(
            self.account_inventory_initialized
            and self.account_inventory_trusted
            and account_id in self.account_inventory_ids
        )

    def _acquire_account_authority_context(
        self,
        account_id: int,
    ) -> tuple[Optional[ExitStack], Any]:
        """Acquire UDI authority before any limiter commit lock is taken."""
        authority_stack = ExitStack()
        try:
            lease_capable = (
                getattr(
                    self.udi_manager,
                    'supports_account_authority_lease',
                    False,
                ) is True
            )
            if lease_capable:
                lease_factory = getattr(
                    self.udi_manager,
                    'account_authority_lease',
                    None,
                )
                if not callable(lease_factory):
                    raise RuntimeError('account authority lease unavailable')
                account = authority_stack.enter_context(
                    lease_factory(account_id)
                )
            else:
                account_getter = getattr(
                    self.udi_manager,
                    'get_m3u_account_by_id',
                    None,
                )
                if not callable(account_getter):
                    raise RuntimeError('account authority getter unavailable')
                # Compatibility for older embedders/test doubles. Current
                # UDIManager always advertises and supplies the atomic lease.
                account = authority_stack.enter_context(
                    nullcontext(account_getter(account_id))
                )
            return authority_stack, account
        except Exception as exc:
            authority_stack.close()
            logger.warning(
                "Could not lease account %s authority: %s",
                account_id,
                type(exc).__name__,
            )
            return None, None
    
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
        normalized_account_id = _strict_positive_id(account_id)
        if normalized_account_id is None:
            self.invalidate_account_inventory()
            raise ValueError('M3U account id must be a positive integer')
        account_id = normalized_account_id
        profiles_provided = profiles is not None
        with self.lock:
            strict_account_limit = _strict_nonnegative_count(max_streams)
            account_limit_trusted = strict_account_limit is not None
            try:
                account_limit = max(0, int(max_streams or 0))
            except (TypeError, ValueError):
                account_limit = 0

            if profiles_provided:
                profile_snapshot_trusted = _active_profile_snapshot_is_trusted(
                    profiles
                )
                active_profile_limits_by_id: Dict[int, int] = {}
                active_profile_resolution_keys_by_id: Dict[int, str] = {}
                for profile in profiles or []:
                    if not isinstance(profile, dict):
                        continue
                    profile_id = _strict_positive_id(profile.get('id'))
                    if not profile.get('is_active', True):
                        continue
                    strict_profile_limit = _strict_configured_limit(profile)
                    profile_limit = (
                        strict_profile_limit
                        if strict_profile_limit is not None
                        else 0
                    )
                    if profile_id is not None:
                        active_profile_limits_by_id[profile_id] = profile_limit
                        resolution_key = _profile_resolution_key(profile)
                        if resolution_key is not None:
                            active_profile_resolution_keys_by_id[profile_id] = resolution_key
                (
                    active_profile_routes_by_id,
                    route_limits_by_key,
                    current_profile_usage_aliases_by_id,
                    _component_aliases,
                ) = _build_profile_route_snapshot(profiles or [])
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
            self.account_fallback_limits[account_id] = account_limit
            self.account_fallback_limits_trusted[account_id] = (
                account_limit_trusted
            )
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
                self.account_profile_snapshots_trusted[account_id] = (
                    profile_snapshot_trusted
                )

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
                for profile_id, alias_keys in current_profile_usage_aliases_by_id.items():
                    retained_usage_routes.setdefault(profile_id, set()).update(alias_keys)
                self.account_profile_usage_route_keys[account_id] = retained_usage_routes
            
            # Initialize checking count for this account
            if account_id not in self.account_checking_counts:
                self.account_checking_counts[account_id] = 0

            self.account_inventory_initialized = True
            self.account_inventory_ids.add(account_id)
            # A malformed profile snapshot remains blocked by its dedicated
            # trust flag.  The account list itself was still published through
            # a structurally valid single-account update.
            self.account_inventory_trusted = True
            
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
        normalized_account_id = _strict_positive_id(account_id)
        if normalized_account_id is None:
            return 0
        with self.lock:
            if not self._account_authority_is_trusted_locked(
                normalized_account_id
            ):
                return 0
            return self.account_limits.get(normalized_account_id, 0)

    def _get_trusted_account_usage(
        self,
        account_id: int,
    ) -> tuple[bool, int, str]:
        """Return aggregate usage plus an exact external-capacity reason."""
        if not self.udi_manager:
            return False, 0, 'provider_usage_unavailable'

        try:
            inspect.getattr_static(
                self.udi_manager,
                'get_active_stream_context_per_profile',
            )
        except AttributeError:
            context_getter = None
        else:
            context_getter = getattr(
                self.udi_manager,
                'get_active_stream_context_per_profile',
                None,
            )

        if callable(context_getter):
            try:
                context = context_getter(account_id)
            except Exception as exc:
                logger.warning(
                    "Could not get active profile context for account %s: %s",
                    account_id,
                    type(exc).__name__,
                )
                return False, 0, 'provider_usage_unavailable'
            if not _usage_mapping_is_trusted(context, contextual=True):
                return False, 0, 'provider_usage_unavailable'
            contexts = [
                _usage_context(context, profile_id)
                for profile_id in context
            ]
            active_count = sum(item['active_streams'] for item in contexts)
            if any(item['real_viewer_streams'] > 0 for item in contexts):
                reason = 'active_viewers'
            elif any(item['shadow_watchers'] > 0 for item in contexts):
                reason = 'shadow_watchers'
            else:
                reason = 'provider_capacity'
            return True, active_count, reason

        usage_getter = getattr(
            self.udi_manager,
            'get_active_streams_for_account',
            None,
        )
        if not callable(usage_getter):
            return False, 0, 'provider_usage_unavailable'
        try:
            parsed_active_count = _strict_nonnegative_count(
                usage_getter(account_id)
            )
        except Exception as exc:
            logger.warning(
                "Could not get active streams for account %s: %s",
                account_id,
                type(exc).__name__,
            )
            return False, 0, 'provider_usage_unavailable'
        if parsed_active_count is None:
            return False, 0, 'provider_usage_unavailable'
        # Legacy aggregate APIs cannot distinguish real viewers from watchers.
        return True, parsed_active_count, 'active_viewers'
    
    def get_available_slots(self, account_id: int) -> int:
        """
        Get the number of available stream slots for an account.
        
        Considers both active viewers (from UDI) and currently checking streams.
        
        Args:
            account_id: M3U account ID
            
        Returns:
            Number of available slots (0 if at limit, -1 if unlimited)
        """
        normalized_account_id = _strict_positive_id(account_id)
        if normalized_account_id is None:
            return 0
        account_id = normalized_account_id
        with self.lock:
            if not self._account_authority_is_trusted_locked(account_id):
                return 0
            limit = self.account_limits.get(account_id, 0)
        
        if limit == 0:
            # Unlimited
            return -1
        
        usage_available, active_count, _usage_reason = (
            self._get_trusted_account_usage(account_id)
        )

        if not usage_available:
            # A finite provider limit cannot be evaluated safely without its
            # live usage. Advertise no slot instead of assuming zero viewers.
            return 0
        
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

        normalized_account_id = _strict_positive_id(account_id)
        if normalized_account_id is None:
            return AcquireResult(False, 'provider_profile_unavailable')
        account_id = normalized_account_id
        with self.lock:
            if not self._account_authority_is_trusted_locked(account_id):
                return AcquireResult(False, 'provider_profile_unavailable')
        
        # Poll for available slot with exponential backoff
        start_time = time.time()
        wait_time = 0.1  # Start with 100ms
        max_wait = 2.0  # Max 2 seconds between checks
        
        last_wait_reason = 'timeout'

        while True:
            usage_available, active_count, usage_reason = (
                self._get_trusted_account_usage(account_id)
            )
            
            # Check if we have available slots: active_viewers + checking_streams < max_streams
            # Read the current limit and reserve atomically. This linearizes live
            # reconfiguration with acquisition instead of using a stale limit
            # captured before the wait loop.
            with self.lock:
                if not self._account_authority_is_trusted_locked(account_id):
                    return AcquireResult(False, 'provider_profile_unavailable')
                limit = _safe_int(self.account_limits.get(account_id, 0))
                checking_count = self.account_checking_counts.get(account_id, 0)
                total_in_use = active_count + checking_count

                if not usage_available and limit != 0:
                    last_wait_reason = 'provider_usage_unavailable'
                elif limit == 0 or total_in_use < limit:
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

                elif active_count >= limit:
                    last_wait_reason = usage_reason
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
            normalized_account_id = _strict_positive_id(account_id)
            if normalized_account_id is not None:
                account_id = normalized_account_id
                self.account_reservation_authority_by_thread.pop(
                    (normalized_account_id, threading.get_ident()),
                    None,
                )
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
        if account_id is None:
            return (True, 'acquired', None, stream.get('url', ''))
        if account_id is _INVALID_PROVIDER_ACCOUNT_ID or not self.udi_manager:
            return (False, 'provider_profile_unavailable', None, '')

        with self.lock:
            if not self._account_authority_is_trusted_locked(account_id):
                return (False, 'provider_profile_unavailable', None, '')

        try:
            account_getter = getattr(self.udi_manager, 'get_m3u_account_by_id', None)
            account = account_getter(account_id) if callable(account_getter) else None
        except Exception as e:
            logger.warning(f"Could not get account {account_id} while reserving profile: {e}")
            return (False, 'provider_profile_unavailable', None, '')

        with self.lock:
            configured_profile_ids = self.account_profile_ids.get(account_id)

        if not callable(account_getter) or not isinstance(account, dict):
            logger.warning("Account %s unavailable while reserving a profile", account_id)
            return (False, 'provider_profile_unavailable', None, '')
        if (
            'id' in account
            and _strict_positive_id(account.get('id')) != account_id
        ):
            logger.warning("Account %s returned mismatched provider authority", account_id)
            return (False, 'provider_profile_unavailable', None, '')
        initial_authority_fingerprint = _account_authority_fingerprint(
            account,
            account_id,
        )
        if initial_authority_fingerprint is None:
            return (False, 'provider_profile_unavailable', None, '')

        raw_profiles = (
            account.get('profiles', [])
            if isinstance(account, dict)
            else []
        )
        current_snapshot_trusted = _active_profile_snapshot_is_trusted(
            raw_profiles
        )
        profiles = raw_profiles if isinstance(raw_profiles, list) else []
        current_active_profiles = [
            profile
            for profile in profiles
            if isinstance(profile, dict)
            and _strict_positive_id(profile.get('id')) is not None
            and profile.get('is_active', True)
        ]
        current_active_profile_ids = {
            _strict_positive_id(profile.get('id'))
            for profile in current_active_profiles
        }
        (
            current_route_keys,
            current_route_limits,
            current_usage_alias_keys,
            current_component_aliases,
        ) = _build_profile_route_snapshot(profiles)
        current_resolution_keys = {
            _strict_positive_id(profile.get('id')): _profile_resolution_key(profile)
            for profile in current_active_profiles
        }
        current_profile_limits = {
            _strict_positive_id(profile.get('id')): _safe_int(profile.get('max_streams', 0))
            for profile in current_active_profiles
        }
        current_account_fallback_limit = _strict_configured_limit(account)

        def authoritative_snapshot_matches_locked(
            authoritative_profile_ids: Optional[set[Any]],
        ) -> bool:
            if not current_snapshot_trusted:
                return False
            if not current_active_profile_ids:
                if current_account_fallback_limit is None:
                    return False
                if self.account_fallback_limits_trusted.get(account_id) is not True:
                    return False
                if (
                    self.account_fallback_limits.get(account_id)
                    != current_account_fallback_limit
                ):
                    return False
            if authoritative_profile_ids is None:
                return True
            if self.account_profile_snapshots_trusted.get(account_id) is not True:
                return False
            if set(authoritative_profile_ids) != current_active_profile_ids:
                return False
            if not all(
                self.profile_route_keys.get(profile_id)
                == current_route_keys.get(profile_id)
                and self.profile_resolution_keys.get(profile_id)
                == current_resolution_keys.get(profile_id)
                and self.profile_limits.get(profile_id)
                == current_profile_limits.get(profile_id)
                for profile_id in current_active_profile_ids
            ):
                return False
            configured_route_keys = self.account_route_keys.get(account_id, set())
            if set(configured_route_keys) != set(current_route_limits):
                return False
            return all(
                self.route_limits.get((account_id, route_key))
                == route_limit
                for route_key, route_limit in current_route_limits.items()
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
            authority_stack, latest_account = (
                self._acquire_account_authority_context(account_id)
            )
            if authority_stack is None:
                return (False, 'provider_profile_unavailable', None, '')
            if (
                _account_authority_fingerprint(latest_account, account_id)
                != initial_authority_fingerprint
            ):
                authority_stack.close()
                return (False, 'provider_profile_unavailable', None, '')
            with authority_stack, self.lock:
                authoritative_profile_ids = self.account_profile_ids.get(account_id)
                if not authoritative_snapshot_matches_locked(
                    authoritative_profile_ids
                ):
                    return (False, 'provider_profile_unavailable', None, '')
                self.account_reservation_authority_by_thread[
                    (account_id, threading.get_ident())
                ] = initial_authority_fingerprint
            return (True, 'acquired', None, stream.get('url', ''))

        active_usage = {}
        usage_available = True
        usage_source_available = False
        try:
            context_getter = getattr(
                self.udi_manager,
                'get_active_stream_context_per_profile',
                None,
            )
            if callable(context_getter):
                usage_source_available = True
                context_usage = context_getter(account_id)
                if not _usage_mapping_is_trusted(
                    context_usage,
                    contextual=True,
                ):
                    usage_available = False
                else:
                    active_usage = context_usage
            if usage_available and not usage_source_available:
                usage_getter = getattr(
                    self.udi_manager,
                    'get_active_streams_count_per_profile',
                    None,
                )
                if callable(usage_getter):
                    usage_source_available = True
                    count_usage = usage_getter(account_id)
                    if not _usage_mapping_is_trusted(
                        count_usage,
                        contextual=False,
                    ):
                        usage_available = False
                    else:
                        active_usage = count_usage
            if not usage_source_available:
                usage_available = False
        except Exception as e:
            logger.warning(
                "Could not get active profile usage for account %s: %s",
                account_id,
                e,
            )
            usage_available = False

        if not usage_available:
            return (False, 'provider_profile_unavailable', None, '')

        checker_blocked = False
        external_blocked = False
        external_block_reason = None
        compatible_profile_seen = False
        authoritative_candidate_seen = False

        candidates = []
        for profile in current_active_profiles:
            profile_id = _strict_positive_id(profile.get('id'))
            # Resolve against the account payload first. The authoritative map is
            # checked again under the reservation lock below so a concurrent
            # profile refresh cannot commit a stale credential route.
            route_key = current_route_keys.get(profile_id)
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

        authority_stack, latest_account = (
            self._acquire_account_authority_context(account_id)
        )
        if authority_stack is None:
            return (False, 'provider_profile_unavailable', None, '')
        if (
            _account_authority_fingerprint(latest_account, account_id)
            != initial_authority_fingerprint
        ):
            authority_stack.close()
            return (False, 'provider_profile_unavailable', None, '')

        with authority_stack, self.lock:
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
                    member_profile_id = _strict_positive_id(
                        member_profile.get('id')
                    )
                    member_route_key = current_route_keys.get(member_profile_id)
                    if member_profile_id is not None and member_route_key is not None:
                        authoritative_route_profile_ids[member_route_key].add(
                            member_profile_id
                        )

            usage_alias_keys_by_profile = {
                profile_id: set(alias_keys)
                for profile_id, alias_keys in self.account_profile_usage_route_keys.get(
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
                member_profile_id = _strict_positive_id(member_profile.get('id'))
                for member_alias_key in current_usage_alias_keys.get(
                    member_profile_id,
                    set(),
                ):
                    usage_alias_keys_by_profile.setdefault(
                        member_profile_id,
                        set(),
                    ).add(member_alias_key)

            # Inactive or just-removed profiles add no route capacity, but an
            # observed live session on one must consume an identical active
            # credential route until that upstream session disappears.
            active_route_keys = set(authoritative_route_profile_ids)
            for usage_profile_id, usage_alias_keys in usage_alias_keys_by_profile.items():
                if _usage_context(active_usage, usage_profile_id)['active_streams'] <= 0:
                    continue
                for active_route_key in active_route_keys:
                    if usage_alias_keys & current_component_aliases.get(
                        active_route_key,
                        set(),
                    ):
                        authoritative_route_profile_ids[active_route_key].add(
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
                    reservation_profile = dict(profile)
                    reservation_profile['id'] = profile_id
                    reserved_profile = ReservedProfile(
                        reservation_profile,
                        account_id,
                        route_key,
                        reservation_token,
                        route_limit,
                        initial_authority_fingerprint,
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

        profile_id = (
            _strict_positive_id(profile.get('id'))
            if isinstance(profile, dict)
            else None
        )
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

        account_id = _strict_positive_id(account_id)
        if account_id is None:
            return []

        with self.lock:
            if not self._account_authority_is_trusted_locked(account_id):
                return []

        try:
            account_getter = getattr(self.udi_manager, 'get_m3u_account_by_id', None)
            account = account_getter(account_id) if callable(account_getter) else None
        except Exception as e:
            logger.warning(f"Could not get account {account_id} while building profile slot snapshot: {e}")
            return []

        if (
            not isinstance(account, dict)
            or (
                'id' in account
                and _strict_positive_id(account.get('id')) != account_id
            )
        ):
            return []
        initial_authority_fingerprint = _account_authority_fingerprint(
            account,
            account_id,
        )
        if initial_authority_fingerprint is None:
            return []

        profiles = account.get('profiles', [])
        if (
            not profiles
            or not _active_profile_snapshot_is_trusted(profiles)
        ):
            return []

        current_active_profiles = [
            profile
            for profile in profiles
            if profile.get('is_active', True)
        ]
        current_profile_ids = {
            _strict_positive_id(profile.get('id'))
            for profile in current_active_profiles
        }
        (
            current_route_keys,
            current_route_limits,
            current_usage_alias_keys,
            current_component_aliases,
        ) = _build_profile_route_snapshot(profiles)
        current_resolution_keys = {
            _strict_positive_id(profile.get('id')): _profile_resolution_key(profile)
            for profile in current_active_profiles
        }
        current_profile_limits = {
            _strict_positive_id(profile.get('id')): _safe_int(profile.get('max_streams', 0))
            for profile in current_active_profiles
        }
        active_usage = {}
        usage_available = True
        usage_source_available = False
        try:
            context_getter = getattr(self.udi_manager, 'get_active_stream_context_per_profile', None)
            if callable(context_getter):
                usage_source_available = True
                context_usage = context_getter(account_id)
                if _usage_mapping_is_trusted(
                    context_usage,
                    contextual=True,
                ):
                    active_usage = context_usage
                else:
                    usage_available = False
            if usage_available and not usage_source_available:
                usage_getter = getattr(self.udi_manager, 'get_active_streams_count_per_profile', None)
                if callable(usage_getter):
                    usage_source_available = True
                    count_usage = usage_getter(account_id)
                    if _usage_mapping_is_trusted(
                        count_usage,
                        contextual=False,
                    ):
                        active_usage = count_usage
                    else:
                        usage_available = False
            if not usage_source_available:
                usage_available = False
        except Exception as e:
            logger.warning(f"Could not get active profile usage for account {account_id}: {e}")
            usage_available = False

        if not usage_available:
            return []

        with self.lock:
            checking_counts = dict(self.profile_checking_counts)
            configured_profile_limits = dict(self.profile_limits)
            configured_profile_ids_value = self.account_profile_ids.get(account_id)
            configured_profile_ids = (
                set(configured_profile_ids_value)
                if configured_profile_ids_value is not None
                else None
            )
            configured_snapshot_trusted = (
                self.account_profile_snapshots_trusted.get(account_id)
            )
            configured_route_keys = dict(self.profile_route_keys)
            configured_resolution_keys = dict(self.profile_resolution_keys)
            configured_route_limits = dict(self.route_limits)
            configured_account_route_keys = set(
                self.account_route_keys.get(account_id, set())
            )
            route_checking_counts = dict(self.route_checking_counts)
            usage_route_keys_by_profile = {
                profile_id: set(route_keys)
                for profile_id, route_keys in self.account_profile_usage_route_keys.get(
                    account_id,
                    {},
                ).items()
            }

        if configured_profile_ids is not None:
            if configured_snapshot_trusted is not True:
                return []
            if configured_profile_ids != current_profile_ids:
                return []
            if not all(
                configured_route_keys.get(profile_id)
                == current_route_keys.get(profile_id)
                and configured_resolution_keys.get(profile_id)
                == current_resolution_keys.get(profile_id)
                and configured_profile_limits.get(profile_id)
                == current_profile_limits.get(profile_id)
                for profile_id in current_profile_ids
            ):
                return []
            if configured_account_route_keys != set(current_route_limits):
                return []
            if any(
                configured_route_limits.get((account_id, route_key))
                != route_limit
                for route_key, route_limit in current_route_limits.items()
            ):
                return []

        profile_records: List[tuple[Dict[str, Any], Any, Optional[str]]] = []
        route_profile_ids: Dict[str, set[Any]] = defaultdict(set)
        for profile in profiles:
            if not isinstance(profile, dict):
                continue
            profile_id = _strict_positive_id(profile.get('id'))
            if profile_id is None or not profile.get('is_active', True):
                continue
            if configured_profile_ids is not None and profile_id not in configured_profile_ids:
                continue
            route_key = (
                configured_route_keys.get(profile_id)
                if configured_profile_ids is not None
                else current_route_keys.get(profile_id)
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

        for profile_id, alias_keys in current_usage_alias_keys.items():
            usage_route_keys_by_profile.setdefault(profile_id, set()).update(
                alias_keys
            )

        active_route_keys = set(route_profile_ids)
        for usage_profile_id, usage_alias_keys in usage_route_keys_by_profile.items():
            if _usage_context(active_usage, usage_profile_id)['active_streams'] <= 0:
                continue
            for active_route_key in active_route_keys:
                if usage_alias_keys & current_component_aliases.get(
                    active_route_key,
                    set(),
                ):
                    route_profile_ids[active_route_key].add(usage_profile_id)

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

        authority_stack, latest_account = (
            self._acquire_account_authority_context(account_id)
        )
        if authority_stack is None:
            return []
        if (
            _account_authority_fingerprint(latest_account, account_id)
            != initial_authority_fingerprint
        ):
            authority_stack.close()
            return []
        with authority_stack, self.lock:
            if (
                not self._account_authority_is_trusted_locked(account_id)
                or self.account_profile_snapshots_trusted.get(account_id) is not True
                or self.account_profile_ids.get(account_id) != current_profile_ids
                or any(
                    self.profile_route_keys.get(profile_id)
                    != current_route_keys.get(profile_id)
                    or self.profile_resolution_keys.get(profile_id)
                    != current_resolution_keys.get(profile_id)
                    or self.profile_limits.get(profile_id)
                    != current_profile_limits.get(profile_id)
                    for profile_id in current_profile_ids
                )
                or self.account_route_keys.get(account_id, set())
                != set(current_route_limits)
                or any(
                    self.route_limits.get((account_id, route_key))
                    != route_limit
                    for route_key, route_limit in current_route_limits.items()
                )
                or any(
                    self.profile_checking_counts.get(profile_id, 0)
                    != checking_counts.get(profile_id, 0)
                    for profile_id in current_profile_ids
                )
                or any(
                    self.route_checking_counts.get((account_id, route_key), 0)
                    != route_checking_counts.get((account_id, route_key), 0)
                    for route_key in current_route_limits
                )
            ):
                return []
            return sorted(
                snapshots,
                key=lambda item: str(item.get('name', '')).lower(),
            )

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
            return account_id not in (None, '') or profile is not None

        profile_id = (
            _strict_positive_id(profile.get('id'))
            if isinstance(profile, dict)
            else None
        )
        profile_limit = _safe_int(profile.get('max_streams', 0)) if isinstance(profile, dict) else 0
        reservation_route_key = getattr(profile, 'route_key', None)
        reservation_authority_fingerprint = getattr(
            profile,
            'authority_fingerprint',
            None,
        )

        resolved_account_id = (
            _strict_positive_id(account_id)
            if account_id not in (None, '')
            else None
        )
        if account_id not in (None, '') and resolved_account_id is None:
            return True
        if resolved_account_id is None and profile is None:
            # Explicit custom streams consume no provider credential capacity.
            # Their long probes still honor manual abort, but there is no
            # provider viewer slot to preempt or usage authority to query.
            return False
        if resolved_account_id is None and isinstance(profile, dict):
            raw_profile_account_id = profile.get(
                'm3u_account_id',
                profile.get('m3u_account'),
            )
            if raw_profile_account_id not in (None, ''):
                resolved_account_id = _strict_positive_id(
                    raw_profile_account_id
                )
                if resolved_account_id is None:
                    return True
        if resolved_account_id is None and profile_id is not None:
            finder = getattr(self.udi_manager, '_find_account_for_profile', None)
            try:
                resolved_account_id = finder(profile_id) if callable(finder) else None
            except Exception as e:
                logger.warning(f"Could not resolve account for profile {profile_id}: {e}")
                resolved_account_id = None
        if resolved_account_id is None and profile_id is not None:
            return True

        if profile_id is None and resolved_account_id is not None:
            with self.lock:
                reservation_authority_fingerprint = (
                    self.account_reservation_authority_by_thread.get((
                        resolved_account_id,
                        threading.get_ident(),
                    ))
                )
            if reservation_authority_fingerprint is None:
                return True

        current_profiles: List[Dict[str, Any]] = []
        current_component_aliases: Dict[str, set[str]] = {}
        current_usage_aliases: Dict[Any, set[str]] = {}
        if resolved_account_id is not None:
            account_getter = getattr(self.udi_manager, 'get_m3u_account_by_id', None)
            try:
                current_account = account_getter(resolved_account_id) if callable(account_getter) else None
                if (
                    not isinstance(current_account, dict)
                    or (
                        'id' in current_account
                        and _strict_positive_id(current_account.get('id'))
                        != resolved_account_id
                    )
                ):
                    return True
                current_authority_fingerprint = _account_authority_fingerprint(
                    current_account,
                    resolved_account_id,
                )
                if (
                    reservation_authority_fingerprint is not None
                    and current_authority_fingerprint
                    != reservation_authority_fingerprint
                ):
                    return True
                current_profiles = current_account.get('profiles', [])
                if not _active_profile_snapshot_is_trusted(current_profiles):
                    return True
                (
                    current_route_keys,
                    current_route_limits,
                    current_usage_aliases,
                    current_component_aliases,
                ) = _build_profile_route_snapshot(current_profiles)
                current_profile_ids = {
                    _strict_positive_id(candidate.get('id'))
                    for candidate in current_profiles
                    if candidate.get('is_active', True)
                }
                with self.lock:
                    if (
                        not self._account_authority_is_trusted_locked(
                            resolved_account_id
                        )
                        or self.account_profile_snapshots_trusted.get(
                            resolved_account_id
                        ) is not True
                        or self.account_profile_ids.get(resolved_account_id)
                        != current_profile_ids
                        or any(
                            self.profile_route_keys.get(current_profile_id)
                            != current_route_keys.get(current_profile_id)
                            or self.profile_resolution_keys.get(current_profile_id)
                            != _profile_resolution_key(current_profile)
                            or self.profile_limits.get(current_profile_id)
                            != _strict_configured_limit(current_profile)
                            for current_profile in current_profiles
                            if current_profile.get('is_active', True)
                            for current_profile_id in [
                                _strict_positive_id(current_profile.get('id'))
                            ]
                        )
                        or self.account_route_keys.get(
                            resolved_account_id,
                            set(),
                        ) != set(current_route_limits)
                        or any(
                            self.route_limits.get((resolved_account_id, route_key))
                            != route_limit
                            for route_key, route_limit in current_route_limits.items()
                        )
                        or (
                            not current_profile_ids
                            and (
                                self.account_fallback_limits_trusted.get(
                                    resolved_account_id
                                ) is not True
                                or self.account_fallback_limits.get(
                                    resolved_account_id
                                ) != _strict_configured_limit(current_account)
                            )
                        )
                    ):
                        return True
                current_profile = next(
                    (
                        candidate
                        for candidate in current_profiles
                        if isinstance(candidate, dict)
                        and _strict_positive_id(candidate.get('id')) == profile_id
                    ),
                    None,
                )
                if current_profile is not None and profile_id is not None:
                    profile_limit = _safe_int(current_profile.get('max_streams', 0))
            except Exception as e:
                logger.warning(f"Could not refresh limit for profile {profile_id}: {e}")
                return True

        try:
            context_getter = getattr(self.udi_manager, 'get_active_stream_context_per_profile', None)
            context = None
            usage_source_available = False
            if callable(context_getter) and resolved_account_id is not None:
                usage_source_available = True
                candidate_context = context_getter(resolved_account_id)
                if not _usage_mapping_is_trusted(
                    candidate_context,
                    contextual=True,
                ):
                    raise ValueError('malformed active profile usage context')
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
                    usage_source_available = True
                    profile_real_stream_count = _strict_nonnegative_count(
                        active_getter(profile_id)
                    )
                    if profile_real_stream_count is None:
                        raise ValueError('malformed active profile viewer count')
                    profile_active_stream_count = profile_real_stream_count
            if not usage_source_available:
                raise ValueError('active profile usage API unavailable')
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
            # A running probe must yield when provider usage becomes unknown.
            # Returning False would allow it to keep occupying a potentially
            # overcommitted credential slot.
            return True

        authority_stack, latest_account = (
            self._acquire_account_authority_context(resolved_account_id)
            if resolved_account_id is not None
            else (None, None)
        )
        if authority_stack is None:
            return True
        if (
            _account_authority_fingerprint(
                latest_account,
                resolved_account_id,
            )
            != current_authority_fingerprint
        ):
            authority_stack.close()
            return True

        with authority_stack, self.lock:
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
                route_aliases = current_component_aliases.get(route_key)
                if not route_aliases:
                    # A component changed after this reservation was made.
                    # Unknown current attribution must preempt, not continue.
                    return max(1, self.route_checking_counts.get(route_identity, 0))
                usage_aliases_by_profile = {
                    usage_profile_id: set(alias_keys)
                    for usage_profile_id, alias_keys in self.account_profile_usage_route_keys.get(
                        resolved_account_id,
                        {},
                    ).items()
                }
                for usage_profile_id, alias_keys in current_usage_aliases.items():
                    usage_aliases_by_profile.setdefault(
                        usage_profile_id,
                        set(),
                    ).update(alias_keys)
                route_profile_ids = {
                    usage_profile_id
                    for usage_profile_id, alias_keys in usage_aliases_by_profile.items()
                    if alias_keys & route_aliases
                }
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
                    current_profile_id = _strict_positive_id(
                        current_profile.get('id')
                    )
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
            self.account_fallback_limits.clear()
            self.account_fallback_limits_trusted.clear()
            self.account_inventory_initialized = False
            self.account_inventory_trusted = False
            self.account_inventory_ids.clear()
            self.profile_limits.clear()
            self.account_profile_ids.clear()
            self.account_profile_snapshots_trusted.clear()
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
            self.account_reservation_authority_by_thread.clear()
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

    normalized_accounts: List[Dict[str, Any]] = []
    account_ids: set[int] = set()
    profile_ids: set[int] = set()
    inventory_valid = isinstance(accounts, list)

    if inventory_valid:
        for account in accounts:
            if not isinstance(account, dict):
                inventory_valid = False
                break
            account_id = _strict_positive_id(account.get('id'))
            account_limit = _strict_configured_limit(account)
            if (
                account_id is None
                or account_id in account_ids
                or account_limit is None
            ):
                inventory_valid = False
                break

            raw_profiles = account.get('profiles', [])
            if not _active_profile_snapshot_is_trusted(raw_profiles):
                inventory_valid = False
                break

            normalized_profiles: List[Dict[str, Any]] = []
            for profile in raw_profiles:
                profile_id = _strict_positive_id(profile.get('id'))
                profile_limit = _strict_configured_limit(profile)
                if (
                    profile_id is None
                    or profile_id in profile_ids
                    or profile_limit is None
                ):
                    inventory_valid = False
                    break
                profile_ids.add(profile_id)
                normalized_profile = dict(profile)
                normalized_profile['id'] = profile_id
                normalized_profile['max_streams'] = profile_limit
                normalized_profiles.append(normalized_profile)
            if not inventory_valid:
                break

            account_ids.add(account_id)
            normalized_account = dict(account)
            normalized_account['id'] = account_id
            normalized_account['max_streams'] = account_limit
            normalized_account['profiles'] = normalized_profiles
            normalized_accounts.append(normalized_account)

    if not inventory_valid:
        limiter.invalidate_account_inventory()
        logger.error(
            "Rejected malformed M3U account inventory; provider probes remain blocked"
        )
        return False

    try:
        with limiter.lock:
            try:
                # Revoke the previous snapshot before publishing any member.
                # The re-entrant lock keeps readers from observing a partial
                # inventory, including when a later member raises.
                limiter.account_inventory_initialized = True
                limiter.account_inventory_trusted = False
                limiter.account_inventory_ids.clear()
                for account in normalized_accounts:
                    limiter.set_account_limit(
                        account['id'],
                        account['max_streams'],
                        account['profiles'],
                    )
                limiter.account_inventory_ids = set(account_ids)
                limiter.account_inventory_trusted = True
            except Exception:
                # Revoke the partial publication before releasing the same
                # outer lock. The logging/cleanup path below may reacquire it,
                # but no admission can observe a trusted prefix in between.
                limiter.account_inventory_trusted = False
                limiter.account_inventory_ids.clear()
                raise
    except Exception:
        limiter.invalidate_account_inventory()
        logger.exception(
            "Could not publish M3U account inventory; provider probes remain blocked"
        )
        return False

    logger.info(
        "Initialized limits for %s accounts (profile-aware checking enabled)",
        len(normalized_accounts),
    )
    return True
