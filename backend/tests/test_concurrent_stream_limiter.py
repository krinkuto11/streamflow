#!/usr/bin/env python3
"""
Test suite for concurrent stream limiter.

Tests the AccountStreamLimiter and SmartStreamScheduler to ensure:
1. Per-account concurrent stream limits are enforced
2. Multiple accounts can check streams in parallel
3. The smart scheduler maximizes concurrency while respecting limits
"""

import unittest
import time
import threading
from contextlib import contextmanager
from unittest.mock import Mock, patch
import sys
import os

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from apps.stream.concurrent_stream_limiter import (
    AccountStreamLimiter,
    SmartStreamScheduler,
    _RESERVATION_TOKEN_KEY,
    _profile_resolution_key,
    get_account_limiter,
    get_smart_scheduler,
    initialize_account_limits
)
from apps.stream.stream_checker_components import StreamCheckConfig
from apps.udi.manager import UDIManager


def _credential_profile(profile_id, name=None, max_streams=1, is_active=True):
    """Build one valid, distinct synthetic credential route for scheduler tests."""
    return {
        'id': profile_id,
        'name': name or f'Credential {profile_id}',
        'max_streams': max_streams,
        'is_active': is_active,
        'search_pattern': r'^(https?://.+)$',
        'replace_pattern': f'$1?profile={profile_id}',
    }


class _RealProfileRouteResolverMixin:
    """Resolve synthetic profiles with the same strict API used in production."""

    def resolve_profile_stream_url(self, stream, profile):
        return UDIManager.resolve_profile_stream_url(self, stream, profile)


class TestAccountStreamLimiter(unittest.TestCase):
    """Test cases for AccountStreamLimiter."""
    
    def setUp(self):
        """Set up test fixtures."""
        class IdleUsageUDI:
            def get_active_streams_for_account(self, _account_id):
                return 0

        self.limiter = AccountStreamLimiter(udi_manager=IdleUsageUDI())
    
    def _acquire(self, account_id, timeout=None):
        """Helper to acquire and return just the boolean result."""
        acquired, _ = self.limiter.acquire(account_id, timeout=timeout)
        return acquired
    
    def test_set_account_limit(self):
        """Test setting account limits."""
        self.limiter.set_account_limit(1, 2)
        self.assertEqual(self.limiter.get_account_limit(1), 2)
        
        self.limiter.set_account_limit(2, 1)
        self.assertEqual(self.limiter.get_account_limit(2), 1)
    
    def test_unlimited_account(self):
        """Test that accounts with 0 limit are unlimited."""
        self.limiter.set_account_limit(1, 0)
        
        # Should be able to acquire many times
        for _ in range(100):
            self.assertTrue(self._acquire(1))
        
        # Releases should not fail
        for _ in range(100):
            self.limiter.release(1)

        self.assertEqual(self.limiter.account_checking_counts[1], 0)

    def test_account_reservations_release_across_finite_unlimited_reconfiguration(self):
        """Finite <-> unlimited changes must not leak or release another reservation."""
        self.limiter.set_account_limit(1, 1)
        self.assertTrue(self._acquire(1, timeout=0))
        self.assertEqual(self.limiter.account_checking_counts[1], 1)

        self.limiter.set_account_limit(1, 0)
        self.limiter.release(1)
        self.assertEqual(self.limiter.account_checking_counts[1], 0)

        self.assertTrue(self._acquire(1, timeout=0))
        self.assertEqual(self.limiter.account_checking_counts[1], 1)
        self.limiter.set_account_limit(1, 1)
        self.limiter.release(1)
        self.assertEqual(self.limiter.account_checking_counts[1], 0)

    def test_acquire_uses_reconfigured_limit_after_waiting_for_lock(self):
        """A 2 -> 1 update must win over an acquire that has not entered its lock."""
        self.limiter.set_account_limit(1, 2)
        self.assertTrue(self._acquire(1, timeout=0))

        acquire_at_lock = threading.Event()
        allow_acquire = threading.Event()

        class GateLock:
            def __init__(self):
                self._lock = threading.Lock()
                self._gated = False

            def acquire(self, *args, **kwargs):
                if threading.current_thread().name == 'reconfiguration-acquirer' and not self._gated:
                    self._gated = True
                    acquire_at_lock.set()
                    if not allow_acquire.wait(2):
                        raise AssertionError('Timed out waiting to linearize account reconfiguration')
                return self._lock.acquire(*args, **kwargs)

            def release(self):
                self._lock.release()

            def __enter__(self):
                self.acquire()
                return self

            def __exit__(self, _exc_type, _exc, _tb):
                self.release()

        self.limiter.lock = GateLock()
        result = []

        worker = threading.Thread(
            name='reconfiguration-acquirer',
            target=lambda: result.append(self.limiter.acquire(1, timeout=0)),
        )
        worker.start()
        self.assertTrue(acquire_at_lock.wait(2))
        self.limiter.set_account_limit(1, 1)
        allow_acquire.set()
        worker.join(2)

        self.assertFalse(worker.is_alive())
        self.assertEqual(len(result), 1)
        self.assertFalse(result[0])
        self.assertEqual(self.limiter.account_checking_counts[1], 1)
        self.limiter.release(1)

    def test_profile_reservations_release_across_finite_unlimited_reconfiguration(self):
        """Profile reservations are counted and released regardless of current limit."""
        profile = {'id': 10, 'name': 'Primary', 'max_streams': 1, 'is_active': True}

        class FakeUDI:
            def get_m3u_account_by_id(self, account_id):
                return {'id': 1, 'profiles': [profile]} if account_id == 1 else None

            def get_active_streams_count_per_profile(self, _account_id):
                return {}

        limiter = AccountStreamLimiter(udi_manager=FakeUDI())
        stream = {'id': 100, 'url': 'http://example.test/stream', 'm3u_account_id': 1}

        limiter.set_account_limit(1, 0, profiles=[profile])

        acquired, _reason, reserved_profile = limiter.reserve_profile_for_stream(stream)
        self.assertTrue(acquired)
        self.assertEqual(limiter.profile_checking_counts[10], 1)
        profile['max_streams'] = 0
        limiter.set_account_limit(1, 0, profiles=[profile])
        limiter.release_profile(reserved_profile)
        self.assertEqual(limiter.profile_checking_counts[10], 0)

        acquired, _reason, reserved_profile = limiter.reserve_profile_for_stream(stream)
        self.assertTrue(acquired)
        self.assertEqual(limiter.profile_checking_counts[10], 1)
        profile['max_streams'] = 1
        limiter.set_account_limit(1, 0, profiles=[profile])
        limiter.release_profile(reserved_profile)
        self.assertEqual(limiter.profile_checking_counts[10], 0)

    def test_plain_dict_profile_release_clears_shared_route_reservation(self):
        """Legacy callers may copy the returned dict before releasing it."""
        profiles = [
            {
                'id': 10,
                'name': 'Default',
                'max_streams': 1,
                'is_active': True,
                'is_default': True,
            },
            {
                'id': 11,
                'name': 'Default alias',
                'max_streams': 1,
                'is_active': True,
            },
        ]

        class FakeUDI:
            def get_m3u_account_by_id(self, account_id):
                return {'id': 1, 'profiles': profiles} if account_id == 1 else None

            def get_active_streams_count_per_profile(self, _account_id):
                return {}

        limiter = AccountStreamLimiter(udi_manager=FakeUDI())
        limiter.set_account_limit(1, 1, profiles=profiles)
        acquired, _reason, reserved_profile = limiter.reserve_profile_for_stream({
            'id': 100,
            'url': 'http://example.test/stream',
            'm3u_account_id': 1,
        })

        self.assertTrue(acquired)
        route_identity = next(iter(limiter.route_checking_counts))
        self.assertEqual(limiter.route_checking_counts[route_identity], 1)
        full_snapshot = limiter.get_profile_slot_snapshot(1)
        self.assertTrue(all(item['full'] for item in full_snapshot))
        self.assertTrue(all(item['shared_route'] for item in full_snapshot))
        self.assertTrue(all(item['used'] == 1 for item in full_snapshot))
        self.assertEqual(
            [item['id'] for item in full_snapshot if item['capacity_counted']],
            [10],
        )

        limiter.release_profile(dict(reserved_profile))

        self.assertEqual(limiter.profile_checking_counts[10], 0)
        self.assertEqual(limiter.route_checking_counts[route_identity], 0)
        self.assertNotIn(10, limiter.profile_reservation_routes)
        open_snapshot = limiter.get_profile_slot_snapshot(1)
        self.assertTrue(all(not item['full'] for item in open_snapshot))
        self.assertTrue(all(item['available'] == 1 for item in open_snapshot))

    def test_legacy_plain_dict_profile_release_refuses_ambiguous_routes_after_refresh(self):
        """A tokenless legacy copy must not guess between old and new routes."""
        old_profile = {
            'id': 10,
            'name': 'Credential',
            'max_streams': 2,
            'is_active': True,
            'is_default': False,
            'search_pattern': r'^http://provider[.]test/old/(.+)$',
            'replace_pattern': r'http://old-user:old-pass@provider.test/live/$1',
        }
        new_profile = {
            **old_profile,
            'search_pattern': r'^http://provider[.]test/new/(.+)$',
            'replace_pattern': r'http://new-user:new-pass@provider.test/live/$1',
        }

        class FakeUDI(_RealProfileRouteResolverMixin):
            def __init__(self):
                self.account = {'id': 1, 'profiles': [old_profile]}

            def get_m3u_account_by_id(self, account_id):
                return self.account if account_id == 1 else None

            def get_active_streams_count_per_profile(self, _account_id):
                return {}

        udi = FakeUDI()
        limiter = AccountStreamLimiter(udi_manager=udi)
        limiter.set_account_limit(1, 1, profiles=[old_profile])
        first_acquired, _reason, first, _url = (
            limiter.reserve_profile_for_stream_with_url({
                'id': 100,
                'url': 'http://provider.test/old/100',
                'm3u_account_id': 1,
            })
        )
        self.assertTrue(first_acquired)

        udi.account = {'id': 1, 'profiles': [new_profile]}
        limiter.set_account_limit(1, 1, profiles=[new_profile])
        second_acquired, _reason, second, _url = (
            limiter.reserve_profile_for_stream_with_url({
                'id': 101,
                'url': 'http://provider.test/new/101',
                'm3u_account_id': 1,
            })
        )
        self.assertTrue(second_acquired)

        old_route = (1, first.route_key)
        new_route = (1, second.route_key)
        self.assertNotEqual(old_route, new_route)
        self.assertEqual(limiter.profile_checking_counts[10], 2)
        self.assertEqual(limiter.route_checking_counts[old_route], 1)
        self.assertEqual(limiter.route_checking_counts[new_route], 1)

        legacy_copy = dict(first)
        legacy_copy.pop(_RESERVATION_TOKEN_KEY)
        limiter.release_profile(legacy_copy)

        self.assertEqual(limiter.profile_checking_counts[10], 2)
        self.assertEqual(limiter.route_checking_counts[old_route], 1)
        self.assertEqual(limiter.route_checking_counts[new_route], 1)
        self.assertEqual(
            limiter.profile_reservation_routes[10],
            [old_route, new_route],
        )

        limiter.release_profile(first)
        self.assertEqual(limiter.profile_checking_counts[10], 1)
        self.assertEqual(limiter.route_checking_counts[old_route], 0)
        self.assertEqual(limiter.route_checking_counts[new_route], 1)
        limiter.release_profile(second)
        self.assertEqual(limiter.profile_checking_counts[10], 0)
        self.assertEqual(limiter.route_checking_counts[new_route], 0)
        self.assertNotIn(10, limiter.profile_reservation_routes)

    def test_stale_copied_reservation_token_cannot_release_reconfigured_route(self):
        """A released A token must never release a later B reservation."""
        old_profile = {
            'id': 10,
            'name': 'Credential',
            'max_streams': 1,
            'is_active': True,
            'is_default': False,
            'search_pattern': r'^http://provider[.]test/old/(.+)$',
            'replace_pattern': r'http://old-user:old-pass@provider.test/live/$1',
        }
        new_profile = {
            **old_profile,
            'search_pattern': r'^http://provider[.]test/new/(.+)$',
            'replace_pattern': r'http://new-user:new-pass@provider.test/live/$1',
        }

        class FakeUDI(_RealProfileRouteResolverMixin):
            def __init__(self):
                self.account = {'id': 1, 'profiles': [old_profile]}

            def get_m3u_account_by_id(self, account_id):
                return self.account if account_id == 1 else None

            def get_active_streams_count_per_profile(self, _account_id):
                return {}

        udi = FakeUDI()
        limiter = AccountStreamLimiter(udi_manager=udi)
        limiter.set_account_limit(1, 1, profiles=[old_profile])
        acquired, _reason, first, _url = (
            limiter.reserve_profile_for_stream_with_url({
                'id': 100,
                'url': 'http://provider.test/old/100',
                'm3u_account_id': 1,
            })
        )
        self.assertTrue(acquired)
        old_copy = dict(first)
        old_token = old_copy[_RESERVATION_TOKEN_KEY]
        old_route = (1, first.route_key)
        self.assertIsInstance(old_token, str)
        self.assertEqual(len(old_token), 32)
        self.assertNotIn('old-user', old_token)
        self.assertIn(old_token, limiter.profile_reservations_by_token)

        limiter.release_profile(first)
        self.assertEqual(limiter.profile_checking_counts[10], 0)
        self.assertEqual(limiter.route_checking_counts[old_route], 0)
        self.assertNotIn(old_token, limiter.profile_reservations_by_token)

        udi.account = {'id': 1, 'profiles': [new_profile]}
        limiter.set_account_limit(1, 1, profiles=[new_profile])
        acquired, _reason, second, _url = (
            limiter.reserve_profile_for_stream_with_url({
                'id': 101,
                'url': 'http://provider.test/new/101',
                'm3u_account_id': 1,
            })
        )
        self.assertTrue(acquired)
        new_token = second[_RESERVATION_TOKEN_KEY]
        new_route = (1, second.route_key)
        self.assertNotEqual(new_token, old_token)

        limiter.release_profile(old_copy)

        self.assertEqual(limiter.profile_checking_counts[10], 1)
        self.assertEqual(limiter.route_checking_counts[new_route], 1)
        self.assertEqual(limiter.profile_reservation_routes[10], [new_route])
        self.assertIn(new_token, limiter.profile_reservations_by_token)

        limiter.clear()
        self.assertEqual(limiter.profile_reservations_by_token, {})
        self.assertEqual(limiter.profile_reservation_routes, {})
        self.assertEqual(limiter.profile_checking_counts, {})
        self.assertEqual(limiter.route_checking_counts, {})

    def test_profile_snapshot_empty_transitions_fail_closed_before_raw_fallback(self):
        """A refresh race must never turn an active profile into a raw-URL probe."""
        alternate = {
            'id': 10,
            'name': 'Alternate',
            'max_streams': 1,
            'is_active': True,
            'is_default': False,
            'search_pattern': r'/main/',
            'replace_pattern': '/alternate/',
        }

        class FakeUDI(_RealProfileRouteResolverMixin):
            def __init__(self):
                self.account = {
                    'id': 1,
                    'max_streams': 1,
                    'profiles': [alternate],
                }

            def get_m3u_account_by_id(self, account_id):
                return self.account if account_id == 1 else None

            def get_active_streams_count_per_profile(self, _account_id):
                return {}

        stream = {
            'id': 100,
            'url': 'http://provider.test/main/100',
            'm3u_account_id': 1,
        }
        udi = FakeUDI()
        limiter = AccountStreamLimiter(udi_manager=udi)

        limiter.set_account_limit(1, 1, profiles=[])
        acquired, reason, profile, resolved_url = (
            limiter.reserve_profile_for_stream_with_url(stream)
        )
        self.assertFalse(acquired)
        self.assertEqual(reason, 'provider_profile_unavailable')
        self.assertIsNone(profile)
        self.assertEqual(resolved_url, '')

        udi.account = {'id': 1, 'max_streams': 1, 'profiles': []}
        acquired, reason, profile, resolved_url = (
            limiter.reserve_profile_for_stream_with_url(stream)
        )
        self.assertTrue(acquired)
        self.assertEqual(reason, 'acquired')
        self.assertIsNone(profile)
        self.assertEqual(resolved_url, stream['url'])

        limiter.set_account_limit(1, 1, profiles=[alternate])
        acquired, reason, profile, resolved_url = (
            limiter.reserve_profile_for_stream_with_url(stream)
        )
        self.assertFalse(acquired)
        self.assertEqual(reason, 'provider_profile_unavailable')
        self.assertIsNone(profile)
        self.assertEqual(resolved_url, '')

    def test_active_profile_without_id_never_falls_back_to_raw_url(self):
        """Malformed active profile authority must block the probe entirely."""
        malformed_profile = {
            'name': 'Missing authority id',
            'max_streams': 1,
            'is_active': True,
            'is_default': True,
        }
        raw_url = 'http://provider.test/main-user/main-token/100.ts'

        class FakeUDI:
            def get_m3u_account_by_id(self, account_id):
                if account_id == 1:
                    return {'id': 1, 'profiles': [malformed_profile]}
                return None

            def get_active_streams_for_account(self, _account_id):
                return 0

            def get_active_stream_context_per_profile(self, _account_id):
                return {}

            def get_active_streams_count_per_profile(self, _account_id):
                return {}

            def get_stream_by_id(self, _stream_id):
                return None

        limiter = AccountStreamLimiter(udi_manager=FakeUDI())
        limiter.set_account_limit(1, 1, profiles=[malformed_profile])
        scheduler = SmartStreamScheduler(limiter, global_limit=1)
        check_function = Mock()

        results = scheduler.check_streams_with_limits(
            streams=[{
                'id': 100,
                'name': 'Credential-bound stream',
                'url': raw_url,
                'm3u_account_id': 1,
            }],
            check_function=check_function,
            provider_wait_timeout=0,
        )

        check_function.assert_not_called()
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]['status'], 'SKIPPED_PROVIDER_LIMIT')
        self.assertEqual(
            results[0]['reason_detail'],
            'provider_profile_unavailable',
        )
        self.assertEqual(results[0]['stream_url'], raw_url)
        self.assertEqual(limiter.account_checking_counts.get(1, 0), 0)

    def test_profile_limit_drift_blocks_stale_capacity_and_effective_limit(self):
        """A UDI limit refresh must match the locked capacity snapshot exactly."""
        configured_profile = {
            'id': 10,
            'name': 'Default credential',
            'max_streams': 5,
            'is_active': True,
            'is_default': True,
        }

        class FakeUDI(_RealProfileRouteResolverMixin):
            def __init__(self):
                self.account = {'id': 1, 'profiles': [dict(configured_profile)]}

            def get_m3u_account_by_id(self, account_id):
                return self.account if account_id == 1 else None

            def get_active_stream_context_per_profile(self, _account_id):
                return {}

            def get_active_streams_count_per_profile(self, _account_id):
                return {}

        udi = FakeUDI()
        limiter = AccountStreamLimiter(udi_manager=udi)
        limiter.set_account_limit(1, 5, profiles=[configured_profile])
        udi.account = {
            'id': 1,
            'profiles': [{**configured_profile, 'max_streams': 1}],
        }

        acquired, reason, profile, resolved_url = (
            limiter.reserve_profile_for_stream_with_url({
                'id': 100,
                'url': 'http://provider.test/main/100',
                'm3u_account_id': 1,
            })
        )

        self.assertFalse(acquired)
        self.assertEqual(reason, 'provider_profile_unavailable')
        self.assertIsNone(profile)
        self.assertEqual(resolved_url, '')
        self.assertEqual(limiter.profile_checking_counts.get(10, 0), 0)

    def test_account_fallback_limit_drift_never_releases_raw_url(self):
        """No-profile capacity must match the fresh account limit exactly."""
        stream = {
            'id': 100,
            'url': 'http://provider.test/main/100',
            'm3u_account_id': 1,
        }

        for live_limit in (1, '1'):
            with self.subTest(live_limit=live_limit):
                class FakeUDI:
                    def get_m3u_account_by_id(self, account_id):
                        if account_id == 1:
                            return {
                                'id': 1,
                                'max_streams': live_limit,
                                'profiles': [],
                            }
                        return None

                limiter = AccountStreamLimiter(udi_manager=FakeUDI())
                limiter.set_account_limit(1, 5, profiles=[])

                acquired, reason, reserved, resolved_url = (
                    limiter.reserve_profile_for_stream_with_url(stream)
                )

                self.assertFalse(acquired)
                self.assertEqual(reason, 'provider_profile_unavailable')
                self.assertIsNone(reserved)
                self.assertEqual(resolved_url, '')

    def test_profile_slot_snapshot_is_empty_when_authority_is_unknown(self):
        """Telemetry must never advertise open slots from stale/invalid authority."""
        configured_profile = _credential_profile(10, max_streams=5)

        for failure in ('limit_drift', 'usage_exception', 'usage_malformed'):
            with self.subTest(failure=failure):
                class FakeUDI:
                    def get_m3u_account_by_id(self, account_id):
                        if account_id != 1:
                            return None
                        live_profile = dict(configured_profile)
                        if failure == 'limit_drift':
                            live_profile['max_streams'] = 1
                        return {'id': 1, 'profiles': [live_profile]}

                    def get_active_stream_context_per_profile(self, _account_id):
                        if failure == 'usage_exception':
                            raise RuntimeError('usage unavailable')
                        if failure == 'usage_malformed':
                            return {
                                10: {
                                    'active_streams': 0,
                                    'real_viewers': False,
                                    'shadow_watchers': 0,
                                },
                            }
                        return {}

                limiter = AccountStreamLimiter(udi_manager=FakeUDI())
                limiter.set_account_limit(1, 5, profiles=[configured_profile])

                self.assertEqual(limiter.get_profile_slot_snapshot(1), [])

    def test_unknown_account_or_profile_usage_never_starts_probe(self):
        """Raised and malformed UDI usage is capacity-unknown, never zero usage."""
        profile = _credential_profile(10)
        raw_url = 'http://provider.test/main/100'

        for usage_failure in (
            'account_exception',
            'profile_malformed',
            'usage_methods_missing',
        ):
            with self.subTest(usage_failure=usage_failure):
                class FakeUDI(_RealProfileRouteResolverMixin):
                    def get_m3u_account_by_id(self, account_id):
                        if account_id == 1:
                            return {'id': 1, 'profiles': [profile]}
                        return None

                    def get_active_streams_for_account(self, _account_id):
                        if usage_failure == 'account_exception':
                            raise RuntimeError('aggregate usage unavailable')
                        return 0

                    def get_active_stream_context_per_profile(self, _account_id):
                        if usage_failure == 'profile_malformed':
                            return {
                                10: {
                                    'active_streams': '0',
                                    'real_viewers': 0,
                                    'shadow_watchers': 0,
                                },
                            }
                        return {}

                    def get_active_streams_count_per_profile(self, _account_id):
                        return {}

                    def get_stream_by_id(self, _stream_id):
                        return None

                udi = FakeUDI()
                if usage_failure == 'account_exception':
                    udi.get_active_stream_context_per_profile = None
                if usage_failure == 'usage_methods_missing':
                    udi.get_active_streams_for_account = None
                    udi.get_active_stream_context_per_profile = None
                    udi.get_active_streams_count_per_profile = None
                limiter = AccountStreamLimiter(udi_manager=udi)
                limiter.set_account_limit(1, 1, profiles=[profile])
                scheduler = SmartStreamScheduler(limiter, global_limit=1)
                check_function = Mock()

                results = scheduler.check_streams_with_limits(
                    streams=[{
                        'id': 100,
                        'name': 'Usage-bound stream',
                        'url': raw_url,
                        'm3u_account_id': 1,
                    }],
                    check_function=check_function,
                    provider_wait_timeout=0,
                )

                check_function.assert_not_called()
                self.assertEqual(len(results), 1)
                self.assertEqual(results[0]['status'], 'SKIPPED_PROVIDER_LIMIT')
                self.assertIn(
                    results[0]['reason_detail'],
                    {
                        'provider_usage_unavailable',
                        'provider_profile_unavailable',
                    },
                )
                self.assertEqual(limiter.account_checking_counts.get(1, 0), 0)

    def test_usage_failure_conservatively_preempts_running_probe(self):
        """An in-flight reservation yields when live usage authority disappears."""
        profile = _credential_profile(10)

        class FakeUDI:
            def get_m3u_account_by_id(self, account_id):
                if account_id == 1:
                    return {'id': 1, 'profiles': [profile]}
                return None

            def get_active_stream_context_per_profile(self, _account_id):
                raise RuntimeError('profile usage unavailable')

        limiter = AccountStreamLimiter(udi_manager=FakeUDI())
        limiter.set_account_limit(1, 1, profiles=[profile])
        limiter.account_checking_counts[1] = 1
        limiter.profile_checking_counts[10] = 1

        self.assertTrue(
            limiter.should_preempt_profile_for_viewer(profile, account_id=1)
        )

    def test_custom_reservation_has_no_provider_viewer_capacity_to_preempt(self):
        """A profile-free custom probe never consults provider usage authority."""
        class ProviderUsageMustNotBeRead:
            def __getattr__(self, name):
                raise AssertionError(f'custom preemption queried provider API {name}')

        limiter = AccountStreamLimiter(udi_manager=ProviderUsageMustNotBeRead())

        self.assertFalse(
            limiter.should_preempt_profile_for_viewer(
                None,
                account_id=None,
                reservation_token=object(),
            )
        )

    def test_provider_reservation_token_uses_noncolliding_internal_dict_key(self):
        """A future/provider reservation_token field must survive untouched."""
        profile = {
            'id': 10,
            'name': 'Default',
            'max_streams': 1,
            'is_active': True,
            'is_default': True,
            'reservation_token': 'provider-owned-value',
        }

        class FakeUDI(_RealProfileRouteResolverMixin):
            def get_m3u_account_by_id(self, account_id):
                return {'id': 1, 'profiles': [profile]} if account_id == 1 else None

            def get_active_streams_count_per_profile(self, _account_id):
                return {}

        limiter = AccountStreamLimiter(udi_manager=FakeUDI())
        limiter.set_account_limit(1, 1, profiles=[profile])

        acquired, _reason, reserved = limiter.reserve_profile_for_stream({
            'id': 100,
            'url': 'http://provider.test/live/100',
            'm3u_account_id': 1,
        })

        self.assertTrue(acquired)
        self.assertEqual(reserved['reservation_token'], 'provider-owned-value')
        self.assertIn(_RESERVATION_TOKEN_KEY, reserved)
        self.assertNotEqual(
            reserved[_RESERVATION_TOKEN_KEY],
            reserved['reservation_token'],
        )
        limiter.release_profile(dict(reserved))
        self.assertEqual(limiter.profile_checking_counts[10], 0)

    def test_account_only_update_preserves_existing_profile_route_aggregate(self):
        """profiles=None changes fallback metadata without corrupting route maps."""
        profiles = [
            _credential_profile(10, max_streams=1),
            _credential_profile(11, max_streams=1),
        ]
        limiter = AccountStreamLimiter()
        limiter.set_account_limit(1, 1, profiles=profiles)
        route_keys_before = dict(limiter.profile_route_keys)
        resolution_keys_before = dict(limiter.profile_resolution_keys)
        route_limits_before = dict(limiter.route_limits)

        limiter.set_account_limit(1, 99)

        self.assertEqual(limiter.get_account_limit(1), 2)
        self.assertEqual(limiter.profile_route_keys, route_keys_before)
        self.assertEqual(limiter.profile_resolution_keys, resolution_keys_before)
        self.assertEqual(limiter.route_limits, route_limits_before)

    def test_profile_resolution_keys_are_normalized_secret_safe_and_refreshed(self):
        """Resolution fingerprints track exact semantics without retaining secrets."""
        original = {
            'id': 10,
            'name': 'Credential',
            'max_streams': 1,
            'is_active': True,
            'is_default': False,
            'search_pattern': r'^http://provider[.]test/old/(.+)$',
            'replace_pattern': r'http://secret-user:secret-pass@provider.test/live/$1',
        }
        whitespace_variant = {
            **original,
            'search_pattern': f"  {original['search_pattern']}  ",
            'replace_pattern': f"  {original['replace_pattern']}  ",
        }

        original_key = _profile_resolution_key(original)
        self.assertEqual(original_key, _profile_resolution_key(whitespace_variant))
        self.assertEqual(len(original_key), 64)
        self.assertNotIn('secret-user', original_key)
        self.assertNotIn('secret-pass', original_key)
        self.assertNotEqual(
            original_key,
            _profile_resolution_key({**original, 'is_default': None}),
        )

        limiter = AccountStreamLimiter()
        limiter.set_account_limit(1, 1, profiles=[original])
        self.assertEqual(limiter.profile_resolution_keys[10], original_key)
        original_route_key = limiter.profile_route_keys[10]

        search_only_refresh = {
            **original,
            'search_pattern': r'^http://provider[.]test/new/(.+)$',
        }
        limiter.set_account_limit(1, 1, profiles=[search_only_refresh])
        refreshed_key = limiter.profile_resolution_keys[10]
        self.assertNotEqual(refreshed_key, original_key)
        self.assertEqual(limiter.profile_route_keys[10], original_route_key)

        replacement_profile = {
            **original,
            'id': 11,
            'name': 'Replacement credential',
        }
        limiter.set_account_limit(1, 1, profiles=[replacement_profile])
        self.assertNotIn(10, limiter.profile_resolution_keys)
        self.assertIn(11, limiter.profile_resolution_keys)

        limiter.clear()
        self.assertEqual(limiter.profile_resolution_keys, {})

    def test_snapshot_counts_each_shared_route_once_and_non_routes_individually(self):
        """Snapshot capacity rows must not multiply aliases of one route."""
        profiles = [
            {
                'id': 20,
                'name': 'Alias B',
                'max_streams': 1,
                'is_active': True,
                'is_default': True,
            },
            {
                'id': 10,
                'name': 'Alias A',
                'max_streams': 1,
                'is_active': True,
            },
            {
                'id': 30,
                'name': 'Invalid non-route',
                'max_streams': 1,
                'is_active': True,
                'is_default': False,
            },
        ]

        class FakeUDI:
            def get_m3u_account_by_id(self, account_id):
                return {'id': 1, 'profiles': profiles} if account_id == 1 else None

            def get_active_streams_count_per_profile(self, _account_id):
                return {}

        limiter = AccountStreamLimiter(udi_manager=FakeUDI())
        limiter.set_account_limit(1, 1, profiles=profiles)

        snapshot_by_id = {
            item['id']: item for item in limiter.get_profile_slot_snapshot(1)
        }

        self.assertTrue(snapshot_by_id[10]['capacity_counted'])
        self.assertFalse(snapshot_by_id[20]['capacity_counted'])
        self.assertTrue(snapshot_by_id[10]['shared_route'])
        self.assertTrue(snapshot_by_id[20]['shared_route'])
        self.assertFalse(snapshot_by_id[30]['capacity_counted'])
        self.assertFalse(snapshot_by_id[30]['route_usable'])
        self.assertNotIn('shared_route', snapshot_by_id[30])

    def test_snapshot_keeps_only_invalid_profile_visible_without_counting_capacity(self):
        """An unusable-only profile set must expose no synthetic route capacity."""
        profiles = [{
            'id': 30,
            'name': 'Invalid only',
            'max_streams': 4,
            'is_active': True,
            'is_default': False,
        }]

        class FakeUDI:
            def get_m3u_account_by_id(self, account_id):
                return {'id': 1, 'profiles': profiles} if account_id == 1 else None

            def get_active_streams_count_per_profile(self, _account_id):
                return {}

        limiter = AccountStreamLimiter(udi_manager=FakeUDI())
        limiter.set_account_limit(1, 2, profiles=profiles)

        snapshot = limiter.get_profile_slot_snapshot(1)

        self.assertEqual(len(snapshot), 1)
        self.assertEqual(snapshot[0]['id'], 30)
        self.assertFalse(snapshot[0]['capacity_counted'])
        self.assertFalse(snapshot[0]['route_usable'])
        self.assertEqual(
            [item for item in snapshot if item['capacity_counted']],
            [],
        )

    def test_explicit_malformed_default_rewrites_add_no_route_capacity(self):
        """Default status cannot make an unusable explicit rewrite a route."""
        malformed_pairs = {
            'incomplete': (r'/main/', None),
            'non_string': (123, '/alternate/'),
            'whitespace': ('   ', '   '),
            'invalid_regex': ('(', '/alternate/'),
        }

        for case_name, (search_pattern, replace_pattern) in malformed_pairs.items():
            with self.subTest(case=case_name):
                profile = {
                    'id': 30,
                    'name': f'Malformed default {case_name}',
                    'max_streams': 4,
                    'is_active': True,
                    'is_default': True,
                    'search_pattern': search_pattern,
                    'replace_pattern': replace_pattern,
                }

                class FakeUDI(_RealProfileRouteResolverMixin):
                    def get_m3u_account_by_id(self, account_id):
                        return (
                            {'id': 1, 'profiles': [profile]}
                            if account_id == 1
                            else None
                        )

                    def get_active_streams_count_per_profile(self, _account_id):
                        return {}

                limiter = AccountStreamLimiter(udi_manager=FakeUDI())
                limiter.set_account_limit(1, 1, profiles=[profile])

                self.assertEqual(limiter.get_account_limit(1), 1)
                snapshot = limiter.get_profile_slot_snapshot(1)
                self.assertEqual(len(snapshot), 1)
                self.assertFalse(snapshot[0]['route_usable'])
                self.assertFalse(snapshot[0]['capacity_counted'])

                acquired, reason, reserved, resolved_url = (
                    limiter.reserve_profile_for_stream_with_url({
                        'id': 100,
                        'url': 'http://provider.test/main/100',
                        'm3u_account_id': 1,
                    })
                )
                self.assertFalse(acquired)
                self.assertEqual(reason, 'profile_url_incompatible')
                self.assertIsNone(reserved)
                self.assertEqual(resolved_url, '')

    def test_inactive_shared_profile_usage_consumes_active_route_without_capacity(self):
        """A disabled alias viewer still fills its identical active credential."""
        profiles = [
            {
                'id': 10,
                'name': 'Active shared',
                'max_streams': 1,
                'is_active': True,
                'is_default': False,
                'search_pattern': r'^http://provider[.]test/a/(.+)$',
                'replace_pattern': r'http://shared.test/live/$1',
            },
            {
                'id': 11,
                'name': 'Inactive shared',
                'max_streams': 1,
                'is_active': False,
                'is_default': False,
                'search_pattern': r'^http://provider[.]test/b/(.+)$',
                'replace_pattern': r'http://shared.test/live/$1',
            },
            {
                'id': 12,
                'name': 'Independent',
                'max_streams': 1,
                'is_active': True,
                'is_default': False,
                'search_pattern': r'^http://provider[.]test/c/(.+)$',
                'replace_pattern': r'http://other.test/live/$1',
            },
        ]

        class FakeUDI(_RealProfileRouteResolverMixin):
            def get_m3u_account_by_id(self, account_id):
                return {'id': 1, 'profiles': profiles} if account_id == 1 else None

            def get_active_stream_context_per_profile(self, _account_id):
                return {
                    11: {
                        'active_streams': 1,
                        'real_viewers': 1,
                        'real_viewer_streams': 1,
                        'shadow_watchers': 0,
                    },
                }

        limiter = AccountStreamLimiter(udi_manager=FakeUDI())
        limiter.set_account_limit(1, 1, profiles=profiles)

        self.assertEqual(limiter.get_account_limit(1), 2)
        acquired, reason, profile, resolved_url = (
            limiter.reserve_profile_for_stream_with_url({
                'id': 100,
                'url': 'http://provider.test/a/100',
                'm3u_account_id': 1,
            })
        )
        self.assertFalse(acquired)
        self.assertEqual(reason, 'active_viewers')
        self.assertIsNone(profile)
        self.assertEqual(resolved_url, '')

        snapshot_by_id = {
            item['id']: item for item in limiter.get_profile_slot_snapshot(1)
        }
        self.assertEqual(set(snapshot_by_id), {10, 12})
        self.assertEqual(snapshot_by_id[10]['active_viewers'], 1)
        self.assertTrue(snapshot_by_id[10]['full'])
        self.assertEqual(snapshot_by_id[12]['active_viewers'], 0)
        self.assertFalse(snapshot_by_id[12]['full'])

    def test_removed_shared_profile_usage_retains_route_capacity_charge(self):
        """A removed profile's observed session remains charged to its old route."""
        shared_active = {
            'id': 10,
            'name': 'Active shared',
            'max_streams': 1,
            'is_active': True,
            'is_default': False,
            'search_pattern': r'^http://provider[.]test/a/(.+)$',
            'replace_pattern': r'http://shared.test/live/$1',
        }
        removed_shared = {
            'id': 11,
            'name': 'Removed shared',
            'max_streams': 1,
            'is_active': True,
            'is_default': False,
            'search_pattern': r'^http://provider[.]test/b/(.+)$',
            'replace_pattern': r'http://shared.test/live/$1',
        }
        independent = {
            'id': 12,
            'name': 'Independent',
            'max_streams': 1,
            'is_active': True,
            'is_default': False,
            'search_pattern': r'^http://provider[.]test/c/(.+)$',
            'replace_pattern': r'http://other.test/live/$1',
        }

        class FakeUDI(_RealProfileRouteResolverMixin):
            def __init__(self):
                self.account = {
                    'id': 1,
                    'profiles': [shared_active, removed_shared, independent],
                }

            def get_m3u_account_by_id(self, account_id):
                return self.account if account_id == 1 else None

            def get_active_stream_context_per_profile(self, _account_id):
                return {
                    11: {
                        'active_streams': 1,
                        'real_viewers': 1,
                        'real_viewer_streams': 1,
                        'shadow_watchers': 0,
                    },
                }

        udi = FakeUDI()
        limiter = AccountStreamLimiter(udi_manager=udi)
        limiter.set_account_limit(1, 1, profiles=udi.account['profiles'])

        udi.account = {'id': 1, 'profiles': [shared_active, independent]}
        limiter.set_account_limit(1, 1, profiles=udi.account['profiles'])

        acquired, reason, profile, resolved_url = (
            limiter.reserve_profile_for_stream_with_url({
                'id': 100,
                'url': 'http://provider.test/a/100',
                'm3u_account_id': 1,
            })
        )
        self.assertFalse(acquired)
        self.assertEqual(reason, 'active_viewers')
        self.assertIsNone(profile)
        self.assertEqual(resolved_url, '')

        snapshot_by_id = {
            item['id']: item for item in limiter.get_profile_slot_snapshot(1)
        }
        self.assertEqual(set(snapshot_by_id), {10, 12})
        self.assertEqual(snapshot_by_id[10]['active_viewers'], 1)
        self.assertTrue(snapshot_by_id[10]['full'])
        self.assertEqual(snapshot_by_id[12]['active_viewers'], 0)

    def test_shared_route_usage_includes_all_authoritative_profile_members(self):
        """An incompatible alias viewer still consumes its shared credential route."""
        profiles = [
            {
                'id': 10,
                'name': 'Shared A',
                'max_streams': 1,
                'is_active': True,
                'is_default': False,
                'search_pattern': r'^http://provider[.]test/a/(.+)$',
                'replace_pattern': r'http://shared-user:shared-pass@provider.test/live/$1',
            },
            {
                'id': 11,
                'name': 'Shared B',
                'max_streams': 1,
                'is_active': True,
                'is_default': False,
                'search_pattern': r'^http://provider[.]test/b/(.+)$',
                'replace_pattern': r'http://shared-user:shared-pass@provider.test/live/$1',
            },
            {
                'id': 12,
                'name': 'Independent C',
                'max_streams': 1,
                'is_active': True,
                'is_default': False,
                'search_pattern': r'^http://provider[.]test/c/(.+)$',
                'replace_pattern': r'http://other-user:other-pass@provider.test/live/$1',
            },
        ]

        class FakeUDI(_RealProfileRouteResolverMixin):
            def get_m3u_account_by_id(self, account_id):
                return {'id': 1, 'profiles': profiles} if account_id == 1 else None

            def get_active_streams_for_account(self, _account_id):
                return 1

            def get_active_stream_context_per_profile(self, _account_id):
                return {
                    11: {
                        'active_streams': 1,
                        'real_viewers': 1,
                        'real_viewer_streams': 1,
                        'shadow_watchers': 0,
                    },
                }

        limiter = AccountStreamLimiter(udi_manager=FakeUDI())
        limiter.set_account_limit(1, 1, profiles=profiles)
        self.assertEqual(limiter.get_account_limit(1), 2)
        self.assertEqual(limiter.profile_route_keys[10], limiter.profile_route_keys[11])
        self.assertNotEqual(limiter.profile_route_keys[10], limiter.profile_route_keys[12])

        account_acquired, reason = limiter.acquire(1, timeout=0)
        self.assertTrue(account_acquired, reason)
        profile_acquired, reason, profile, resolved_url = (
            limiter.reserve_profile_for_stream_with_url({
                'id': 100,
                'url': 'http://provider.test/a/100',
                'm3u_account_id': 1,
            })
        )

        self.assertFalse(profile_acquired)
        self.assertEqual(reason, 'active_viewers')
        self.assertIsNone(profile)
        self.assertEqual(resolved_url, '')
        self.assertEqual(limiter.profile_checking_counts.get(10, 0), 0)
        self.assertTrue(all(count == 0 for count in limiter.route_checking_counts.values()))
        limiter.release(1)

    def test_stale_udi_profile_route_cannot_commit_after_reconfiguration(self):
        """The route map is rechecked atomically before taking capacity."""
        old_profile = {
            'id': 10,
            'name': 'Credential',
            'max_streams': 1,
            'is_active': True,
            'is_default': False,
            'search_pattern': r'^http://provider[.]test/old/(.+)$',
            'replace_pattern': r'http://provider.test/credential-a/$1',
        }
        new_profile = {
            **old_profile,
            'replace_pattern': r'http://provider.test/credential-b/$1',
        }

        class FakeUDI(_RealProfileRouteResolverMixin):
            def __init__(self):
                self.account = {'id': 1, 'profiles': [old_profile]}

            def get_m3u_account_by_id(self, account_id):
                return self.account if account_id == 1 else None

            def get_active_streams_count_per_profile(self, _account_id):
                return {}

        udi = FakeUDI()
        limiter = AccountStreamLimiter(udi_manager=udi)
        limiter.set_account_limit(1, 1, profiles=[new_profile])
        stream = {
            'id': 100,
            'url': 'http://provider.test/old/100',
            'm3u_account_id': 1,
        }

        acquired, reason, profile, resolved_url = (
            limiter.reserve_profile_for_stream_with_url(stream)
        )

        self.assertFalse(acquired)
        self.assertEqual(reason, 'provider_profile_unavailable')
        self.assertIsNone(profile)
        self.assertEqual(resolved_url, '')
        self.assertEqual(limiter.profile_checking_counts.get(10, 0), 0)
        self.assertTrue(all(count == 0 for count in limiter.route_checking_counts.values()))

        udi.account = {'id': 1, 'profiles': [new_profile]}
        acquired, reason, profile, resolved_url = (
            limiter.reserve_profile_for_stream_with_url(stream)
        )
        self.assertTrue(acquired)
        self.assertEqual(reason, 'acquired')
        self.assertEqual(resolved_url, 'http://provider.test/credential-b/100')
        limiter.release_profile(profile)

    def test_stale_udi_profile_search_cannot_commit_to_same_route_target(self):
        """A search-only refresh must invalidate an old resolved URL candidate."""
        old_profile = {
            'id': 10,
            'name': 'Credential',
            'max_streams': 1,
            'is_active': True,
            'is_default': False,
            'search_pattern': r'^http://provider[.]test/old/(.+)$',
            'replace_pattern': r'http://profile-user:profile-pass@provider.test/live/$1',
        }
        new_profile = {
            **old_profile,
            'search_pattern': r'^http://provider[.]test/new/(.+)$',
        }

        class FakeUDI(_RealProfileRouteResolverMixin):
            def __init__(self):
                self.account = {'id': 1, 'profiles': [old_profile]}

            def get_m3u_account_by_id(self, account_id):
                return self.account if account_id == 1 else None

            def get_active_streams_count_per_profile(self, _account_id):
                return {}

        udi = FakeUDI()
        limiter = AccountStreamLimiter(udi_manager=udi)
        limiter.set_account_limit(1, 1, profiles=[new_profile])
        old_stream = {
            'id': 100,
            'url': 'http://provider.test/old/100',
            'm3u_account_id': 1,
        }

        acquired, reason, profile, resolved_url = (
            limiter.reserve_profile_for_stream_with_url(old_stream)
        )

        self.assertFalse(acquired)
        self.assertEqual(reason, 'provider_profile_unavailable')
        self.assertIsNone(profile)
        self.assertEqual(resolved_url, '')
        self.assertEqual(limiter.profile_checking_counts.get(10, 0), 0)
        self.assertTrue(all(count == 0 for count in limiter.route_checking_counts.values()))

        udi.account = {'id': 1, 'profiles': [new_profile]}
        acquired, reason, profile, resolved_url = (
            limiter.reserve_profile_for_stream_with_url({
                **old_stream,
                'url': 'http://provider.test/new/100',
            })
        )
        self.assertTrue(acquired)
        self.assertEqual(reason, 'acquired')
        self.assertEqual(
            resolved_url,
            'http://profile-user:profile-pass@provider.test/live/100',
        )
        limiter.release_profile(profile)

    def test_profile_reservation_uses_reconfigured_limit_after_waiting_for_lock(self):
        """A 0 -> 1 profile update must block a stale second unlimited reservation."""
        original_profile = {
            'id': 10,
            'name': 'Primary',
            'max_streams': 0,
            'is_active': True,
        }

        class FakeUDI:
            def __init__(self):
                self.account = {'id': 1, 'profiles': [original_profile]}

            def get_m3u_account_by_id(self, account_id):
                return self.account if account_id == 1 else None

            def get_active_streams_count_per_profile(self, _account_id):
                return {}

        udi = FakeUDI()
        limiter = AccountStreamLimiter(udi_manager=udi)
        limiter.set_account_limit(1, 0, profiles=udi.account['profiles'])
        stream = {'id': 100, 'url': 'http://example.test/stream', 'm3u_account_id': 1}
        acquired, _reason, first_profile = limiter.reserve_profile_for_stream(stream)
        self.assertTrue(acquired)

        reservation_at_lock = threading.Event()
        allow_reservation = threading.Event()

        class GateLock:
            def __init__(self):
                self._lock = threading.Lock()
                self._gated = False

            def acquire(self, *args, **kwargs):
                if threading.current_thread().name == 'profile-reconfiguration-reserver' and not self._gated:
                    self._gated = True
                    reservation_at_lock.set()
                    if not allow_reservation.wait(2):
                        raise AssertionError('Timed out waiting to linearize profile reconfiguration')
                return self._lock.acquire(*args, **kwargs)

            def release(self):
                self._lock.release()

            def __enter__(self):
                self.acquire()
                return self

            def __exit__(self, _exc_type, _exc, _tb):
                self.release()

        limiter.lock = GateLock()
        result = []
        worker = threading.Thread(
            name='profile-reconfiguration-reserver',
            target=lambda: result.append(limiter.reserve_profile_for_stream(stream)),
        )
        worker.start()
        self.assertTrue(reservation_at_lock.wait(2))
        reconfigured_profile = {
            'id': 10,
            'name': 'Primary',
            'max_streams': 1,
            'is_active': True,
        }
        udi.account = {'id': 1, 'profiles': [reconfigured_profile]}
        limiter.set_account_limit(1, 0, profiles=udi.account['profiles'])
        allow_reservation.set()
        worker.join(2)

        self.assertFalse(worker.is_alive())
        self.assertEqual(len(result), 1)
        self.assertFalse(result[0][0])
        self.assertEqual(result[0][1], 'checking_capacity')
        self.assertEqual(limiter.profile_checking_counts[10], 1)
        limiter.release_profile(first_profile)
        self.assertEqual(limiter.profile_checking_counts[10], 0)

    def test_account_preemption_protects_sibling_viewer_with_unlimited_profile(self):
        """An account cap must preempt P10 when a real viewer arrives on sibling P11."""
        profiles = [
            {'id': 10, 'name': 'Unlimited alias', 'max_streams': 0, 'is_active': True},
            {'id': 11, 'name': 'Viewer alias', 'max_streams': 1, 'is_active': True},
        ]

        class FakeUDI:
            def __init__(self):
                self.context = {
                    10: {'active_streams': 0, 'real_viewers': 0, 'shadow_watchers': 0},
                    11: {'active_streams': 0, 'real_viewers': 0, 'shadow_watchers': 0},
                }

            def get_active_streams_for_account(self, _account_id):
                return sum(item['active_streams'] for item in self.context.values())

            def get_active_stream_context_per_profile(self, _account_id):
                return self.context

            def get_active_streams_for_account(self, _account_id):
                return sum(item['active_streams'] for item in self.context.values())

            def get_m3u_account_by_id(self, account_id):
                return {'id': 1, 'profiles': profiles} if account_id == 1 else None

        udi = FakeUDI()
        limiter = AccountStreamLimiter(udi_manager=udi)
        limiter.set_account_limit(1, 1, profiles=profiles)
        acquired_account, _reason = limiter.acquire(1, timeout=0)
        acquired_profile, _reason, profile = limiter.reserve_profile_for_stream({
            'id': 100,
            'url': 'http://example.test/stream',
            'm3u_account_id': 1,
        })
        self.assertTrue(acquired_account)
        self.assertTrue(acquired_profile)
        self.assertEqual(profile['id'], 10)
        self.assertFalse(limiter.should_preempt_profile_for_viewer(profile, account_id=1))

        udi.context[11] = {
            'active_streams': 1,
            'real_viewers': 1,
            'shadow_watchers': 0,
        }
        self.assertTrue(limiter.should_preempt_profile_for_viewer(profile, account_id=1))

        limiter.release_profile(profile)
        limiter.release(1)

    def test_profile_preemption_counts_all_checker_reservations(self):
        """Two checks plus a viewer overcommit a profile with max_streams=2."""
        profiles = [
            {'id': 10, 'name': 'Finite', 'max_streams': 2, 'is_active': True},
            {'id': 11, 'name': 'Unlimited fallback', 'max_streams': 0, 'is_active': True},
        ]

        class FakeUDI:
            def __init__(self):
                self.context = {
                    10: {'active_streams': 0, 'real_viewers': 0, 'shadow_watchers': 0},
                    11: {'active_streams': 0, 'real_viewers': 0, 'shadow_watchers': 0},
                }

            def get_active_streams_for_account(self, _account_id):
                return sum(item['active_streams'] for item in self.context.values())

            def get_active_stream_context_per_profile(self, _account_id):
                return self.context

            def get_m3u_account_by_id(self, account_id):
                return {'id': 1, 'profiles': profiles} if account_id == 1 else None

        udi = FakeUDI()
        limiter = AccountStreamLimiter(udi_manager=udi)
        limiter.set_account_limit(1, 0, profiles=profiles)
        stream = {'id': 100, 'url': 'http://example.test/stream', 'm3u_account_id': 1}
        reservations = []
        for _ in range(2):
            acquired_account, _reason = limiter.acquire(1, timeout=0)
            acquired_profile, _reason, profile = limiter.reserve_profile_for_stream(stream)
            self.assertTrue(acquired_account)
            self.assertTrue(acquired_profile)
            self.assertEqual(profile['id'], 10)
            reservations.append(profile)

        self.assertFalse(limiter.should_preempt_profile_for_viewer(profile, account_id=1))
        udi.context[10] = {
            'active_streams': 1,
            'real_viewers': 1,
            'shadow_watchers': 0,
        }
        self.assertTrue(limiter.should_preempt_profile_for_viewer(profile, account_id=1))

        first_token = object()
        second_token = object()
        self.assertTrue(limiter.should_preempt_profile_for_viewer(
            profile,
            account_id=1,
            reservation_token=first_token,
        ))
        self.assertFalse(limiter.should_preempt_profile_for_viewer(
            profile,
            account_id=1,
            reservation_token=second_token,
        ))
        limiter.release_viewer_preemption_claim(first_token)

        for reserved_profile in reservations:
            limiter.release_profile(reserved_profile)
            limiter.release(1)

    def test_profile_local_preemption_wins_over_sibling_account_claim(self):
        """A sibling account callback must not steal a profile-local preemption."""
        profiles = [
            _credential_profile(10, 'Viewer profile'),
            _credential_profile(11, 'Sibling profile'),
        ]

        class FakeUDI:
            def __init__(self):
                self.account = {'id': 1, 'profiles': profiles}
                self.context = {
                    10: {
                        'active_streams': 1,
                        'real_viewers': 1,
                        'real_viewer_streams': 1,
                        'shadow_watchers': 0,
                    },
                    11: {
                        'active_streams': 0,
                        'real_viewers': 0,
                        'real_viewer_streams': 0,
                        'shadow_watchers': 0,
                    },
                }

            def get_active_stream_context_per_profile(self, _account_id):
                return self.context

            def get_m3u_account_by_id(self, account_id):
                return self.account if account_id == 1 else None

        limiter = AccountStreamLimiter(udi_manager=FakeUDI())
        limiter.set_account_limit(1, 2, profiles=profiles)
        limiter.account_checking_counts[1] = 2
        limiter.profile_checking_counts.update({10: 1, 11: 1})
        sibling_token = object()
        viewer_profile_token = object()

        self.assertFalse(limiter.should_preempt_profile_for_viewer(
            profiles[1],
            account_id=1,
            reservation_token=sibling_token,
        ))
        self.assertTrue(limiter.should_preempt_profile_for_viewer(
            profiles[0],
            account_id=1,
            reservation_token=viewer_profile_token,
        ))
        self.assertFalse(limiter.should_preempt_profile_for_viewer(
            profiles[1],
            account_id=1,
            reservation_token=sibling_token,
        ))
        limiter.release_viewer_preemption_claim(viewer_profile_token)

    def test_external_profile_excess_without_checker_does_not_hide_account_preemption(self):
        """External-only local excess cannot satisfy an account claim by itself."""
        profiles = [
            {'id': 10, 'name': 'Viewer profile', 'max_streams': 1, 'is_active': True},
            {'id': 11, 'name': 'Checker sibling', 'max_streams': 1, 'is_active': True},
        ]

        class FakeUDI:
            def get_active_stream_context_per_profile(self, _account_id):
                return {
                    10: {
                        'active_streams': 2,
                        'real_viewers': 2,
                        'real_viewer_streams': 2,
                        'shadow_watchers': 0,
                    },
                    11: {
                        'active_streams': 0,
                        'real_viewers': 0,
                        'real_viewer_streams': 0,
                        'shadow_watchers': 0,
                    },
                }

            def get_m3u_account_by_id(self, account_id):
                return {'id': 1, 'profiles': profiles} if account_id == 1 else None

        limiter = AccountStreamLimiter(udi_manager=FakeUDI())
        limiter.set_account_limit(1, 2, profiles=profiles)
        limiter.account_checking_counts[1] = 1
        limiter.profile_checking_counts[11] = 1
        token = object()

        self.assertTrue(limiter.should_preempt_profile_for_viewer(
            profiles[1],
            account_id=1,
            reservation_token=token,
        ))
        limiter.release_viewer_preemption_claim(token)

    def test_multiple_clients_on_one_proxy_stream_use_one_provider_slot(self):
        """Client fan-out on one Dispatcharr proxy stream must not over-preempt."""
        profile = {'id': 10, 'name': 'Primary', 'max_streams': 2, 'is_active': True}

        class FakeUDI:
            def get_active_stream_context_per_profile(self, _account_id):
                return {
                    10: {
                        'active_streams': 1,
                        'real_viewers': 3,
                        'real_viewer_streams': 1,
                        'shadow_watchers': 0,
                    }
                }

            def get_m3u_account_by_id(self, account_id):
                return {'id': 1, 'profiles': [profile]} if account_id == 1 else None

        limiter = AccountStreamLimiter(udi_manager=FakeUDI())
        limiter.set_account_limit(1, 2, profiles=[profile])
        limiter.account_checking_counts[1] = 1
        limiter.profile_checking_counts[10] = 1

        self.assertFalse(limiter.should_preempt_profile_for_viewer(profile, account_id=1))

    def test_real_viewer_preemption_counts_shadow_occupied_provider_slots(self):
        """Viewer priority uses all upstream slots while ignoring shadow as the trigger."""
        profile = {'id': 10, 'name': 'Primary', 'max_streams': 2, 'is_active': True}

        class FakeUDI:
            def get_active_stream_context_per_profile(self, _account_id):
                return {
                    10: {
                        'active_streams': 2,
                        'real_viewers': 1,
                        'real_viewer_streams': 1,
                        'shadow_watchers': 1,
                    }
                }

            def get_m3u_account_by_id(self, account_id):
                return {'id': 1, 'profiles': [profile]} if account_id == 1 else None

        limiter = AccountStreamLimiter(udi_manager=FakeUDI())
        limiter.set_account_limit(1, 2, profiles=[profile])
        limiter.account_checking_counts[1] = 1
        limiter.profile_checking_counts[10] = 1

        self.assertTrue(limiter.should_preempt_profile_for_viewer(profile, account_id=1))

    def test_preemption_refreshes_profile_limit_after_udi_replacement(self):
        """A refreshed 2 -> 1 profile limit must replace the reservation's stale dict."""
        stale_profile = {'id': 10, 'name': 'Primary', 'max_streams': 2, 'is_active': True}
        unlimited_sibling = {'id': 11, 'name': 'Fallback', 'max_streams': 0, 'is_active': True}

        class FakeUDI:
            def __init__(self):
                self.account = {'id': 1, 'profiles': [stale_profile, unlimited_sibling]}

            def get_active_stream_context_per_profile(self, _account_id):
                return {
                    10: {
                        'active_streams': 1,
                        'real_viewers': 1,
                        'real_viewer_streams': 1,
                        'shadow_watchers': 0,
                    }
                }

            def get_m3u_account_by_id(self, account_id):
                return self.account if account_id == 1 else None

        udi = FakeUDI()
        limiter = AccountStreamLimiter(udi_manager=udi)
        limiter.set_account_limit(1, 0, profiles=udi.account['profiles'])
        limiter.account_checking_counts[1] = 1
        limiter.profile_checking_counts[10] = 1
        self.assertFalse(limiter.should_preempt_profile_for_viewer(stale_profile, account_id=1))

        udi.account = {
            'id': 1,
            'profiles': [
                {'id': 10, 'name': 'Primary', 'max_streams': 1, 'is_active': True},
                unlimited_sibling,
            ],
        }
        limiter.set_account_limit(1, 0, profiles=udi.account['profiles'])
        self.assertTrue(limiter.should_preempt_profile_for_viewer(stale_profile, account_id=1))

    def test_negative_account_and_profile_limits_fail_closed(self):
        """Negative imported limits must never become unlimited authority."""
        from apps.udi.manager import UDIManager

        profile = {'id': 10, 'name': 'Invalid', 'max_streams': -3, 'is_active': True}

        class FakeUDI:
            _get_stream_m3u_account_id = staticmethod(UDIManager._get_stream_m3u_account_id)

            def _ensure_initialized(self):
                return None

            def get_m3u_account_by_id(self, account_id):
                return {'id': 1, 'profiles': [profile]} if account_id == 1 else None

            def get_active_streams_count_per_profile(self, _account_id):
                return {}

            def get_active_streams_for_account(self, _account_id):
                return 0

            def find_available_profile_for_stream(self, stream):
                return UDIManager.find_available_profile_for_stream(self, stream)

            def check_stream_can_run(self, stream):
                return UDIManager.check_stream_can_run(self, stream)

            def apply_profile_url_transformation(self, stream, profile=None):
                return stream['url']

        limiter = AccountStreamLimiter(udi_manager=FakeUDI())
        limiter.set_account_limit(1, -2, profiles=[profile])
        self.assertEqual(limiter.get_account_limit(1), 0)

        acquired, reason, reserved_profile = limiter.reserve_profile_for_stream({
            'id': 100,
            'url': 'http://example.test/stream',
            'm3u_account_id': 1,
        })
        self.assertFalse(acquired)
        self.assertEqual(reason, 'provider_profile_unavailable')
        self.assertIsNone(reserved_profile)
        snapshot = limiter.get_profile_slot_snapshot(1)
        self.assertEqual(snapshot, [])

        check_calls = []
        results = SmartStreamScheduler(limiter, global_limit=1).check_streams_with_limits(
            streams=[{
                'id': 100,
                'name': 'Negative limit stream',
                'url': 'http://example.test/stream',
                'm3u_account_id': 1,
            }],
            check_function=lambda **kwargs: check_calls.append(kwargs),
            provider_wait_timeout=0.1,
        )
        self.assertFalse(check_calls)
        self.assertEqual(results[0]['status'], 'SKIPPED_PROVIDER_LIMIT')
        self.assertEqual(results[0]['reason_detail'], 'provider_profile_unavailable')
    
    def test_single_stream_limit(self):
        """Test account with max_streams=1."""
        self.limiter.set_account_limit(1, 1)
        
        # First acquire should succeed
        self.assertTrue(self._acquire(1, timeout=0.1))
        
        # Second acquire should timeout
        self.assertFalse(self._acquire(1, timeout=0.1))
        
        # After release, should be able to acquire again
        self.limiter.release(1)
        self.assertTrue(self._acquire(1, timeout=0.1))
    
    def test_multiple_stream_limit(self):
        """Test account with max_streams=2."""
        self.limiter.set_account_limit(1, 2)
        
        # First two acquires should succeed
        self.assertTrue(self._acquire(1, timeout=0.1))
        self.assertTrue(self._acquire(1, timeout=0.1))
        
        # Third acquire should timeout
        self.assertFalse(self._acquire(1, timeout=0.1))
        
        # After one release, should be able to acquire one more
        self.limiter.release(1)
        self.assertTrue(self._acquire(1, timeout=0.1))
    
    def test_multiple_accounts_independent(self):
        """Test that different accounts have independent limits."""
        self.limiter.set_account_limit(1, 1)
        self.limiter.set_account_limit(2, 2)
        
        # Account 1: max 1 stream
        self.assertTrue(self._acquire(1, timeout=0.1))
        self.assertFalse(self._acquire(1, timeout=0.1))
        
        # Account 2: max 2 streams (should still work)
        self.assertTrue(self._acquire(2, timeout=0.1))
        self.assertTrue(self._acquire(2, timeout=0.1))
        self.assertFalse(self._acquire(2, timeout=0.1))
    
    def test_custom_stream_always_allowed(self):
        """Test that custom streams (None account) are always allowed."""
        # Even without setting any limits
        for _ in range(100):
            self.assertTrue(self._acquire(None))
        
        # Releases should not fail
        for _ in range(100):
            self.limiter.release(None)
    
    def test_concurrent_access(self):
        """Test concurrent access to the limiter."""
        self.limiter.set_account_limit(1, 2)
        
        acquired_count = []
        lock = threading.Lock()
        
        def worker():
            """Worker thread that tries to acquire."""
            if self.limiter.acquire(1, timeout=1.0):
                with lock:
                    acquired_count.append(1)
                time.sleep(0.1)
                self.limiter.release(1)
        
        # Start 10 threads
        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        
        # All threads should eventually acquire
        self.assertEqual(len(acquired_count), 10)


class TestCredentialRouteAuthorityHardening(unittest.TestCase):
    def _profile(self, profile_id, *, default, replace=None, limit=1):
        profile = {
            'id': profile_id,
            'name': f'P{profile_id}',
            'max_streams': limit,
            'is_active': True,
            'is_default': default,
        }
        if replace is not None:
            profile.update({
                'search_pattern': r'^(http://provider[.]test/live/)(.+)$',
                'replace_pattern': replace,
            })
        return profile

    def test_default_rewrite_bridges_raw_and_canonical_target_capacity(self):
        profiles = [
            self._profile(10, default=True),
            self._profile(11, default=True, replace=r'$1$2'),
            self._profile(12, default=False, replace=r'\1\2'),
        ]

        class FakeUDI(_RealProfileRouteResolverMixin):
            account = {'id': 1, 'max_streams': 1, 'profiles': profiles}

            def get_m3u_account_by_id(self, account_id):
                return self.account if account_id == 1 else None

            def get_active_streams_count_per_profile(self, _account_id):
                return {}

        limiter = AccountStreamLimiter(udi_manager=FakeUDI())
        limiter.set_account_limit(1, 1, profiles=profiles)
        self.assertEqual(limiter.get_account_limit(1), 1)
        stream = {
            'id': 100,
            'url': 'http://provider.test/live/channel.ts',
            'm3u_account_id': 1,
        }
        acquired, _reason, first = limiter.reserve_profile_for_stream(stream)
        self.assertTrue(acquired)
        second_acquired, second_reason, _second = limiter.reserve_profile_for_stream(
            stream
        )
        self.assertFalse(second_acquired)
        self.assertEqual(second_reason, 'checking_capacity')
        limiter.release_profile(first)

    def test_removed_alias_usage_survives_component_expansion_and_preempts(self):
        old_profile = self._profile(10, default=True)
        bridge_profile = self._profile(11, default=True, replace=r'$1$2')
        sibling_profile = self._profile(
            20,
            default=False,
            replace=r'http://credential-b.test/live/\2',
        )

        class FakeUDI(_RealProfileRouteResolverMixin):
            def __init__(self):
                self.account = {'id': 1, 'max_streams': 1, 'profiles': [old_profile]}
                self.context = {
                    10: {'active_streams': 0, 'real_viewers': 0, 'shadow_watchers': 0},
                    11: {'active_streams': 0, 'real_viewers': 0, 'shadow_watchers': 0},
                    20: {'active_streams': 0, 'real_viewers': 0, 'shadow_watchers': 0},
                }

            def get_m3u_account_by_id(self, account_id):
                return self.account if account_id == 1 else None

            def get_active_stream_context_per_profile(self, _account_id):
                return self.context

            def get_active_streams_for_account(self, _account_id):
                return sum(item['active_streams'] for item in self.context.values())

        udi = FakeUDI()
        limiter = AccountStreamLimiter(udi_manager=udi)
        limiter.set_account_limit(1, 1, profiles=[old_profile])
        udi.account = {
            'id': 1,
            'max_streams': 1,
            'profiles': [bridge_profile, sibling_profile],
        }
        limiter.set_account_limit(
            1,
            1,
            profiles=udi.account['profiles'],
        )
        stream = {
            'id': 100,
            'url': 'http://provider.test/live/channel.ts',
            'm3u_account_id': 1,
        }

        account_acquired, _reason = limiter.acquire(1, timeout=0)
        self.assertTrue(account_acquired)
        acquired, _reason, reserved = limiter.reserve_profile_for_stream(stream)
        self.assertTrue(acquired)
        self.assertEqual(reserved['id'], 11)
        udi.context[10] = {
            'active_streams': 1,
            'real_viewers': 1,
            'real_viewer_streams': 1,
            'shadow_watchers': 0,
        }
        self.assertTrue(
            limiter.should_preempt_profile_for_viewer(reserved, account_id=1)
        )
        snapshots = {
            item['id']: item for item in limiter.get_profile_slot_snapshot(1)
        }
        self.assertTrue(snapshots[11]['full'])
        self.assertFalse(snapshots[20]['full'])
        limiter.release_profile(reserved)
        limiter.release(1)

    def test_removed_bridge_usage_survives_component_shrink(self):
        bridge_profile = self._profile(11, default=True, replace=r'$1$2')
        raw_profile = self._profile(10, default=True)
        sibling_profile = self._profile(
            20,
            default=False,
            replace=r'http://credential-b.test/live/\2',
        )

        class FakeUDI(_RealProfileRouteResolverMixin):
            def __init__(self):
                self.account = {'id': 1, 'max_streams': 1, 'profiles': [bridge_profile]}
                self.context = {
                    11: {'active_streams': 1, 'real_viewers': 1, 'real_viewer_streams': 1, 'shadow_watchers': 0},
                    10: {'active_streams': 0, 'real_viewers': 0, 'shadow_watchers': 0},
                    20: {'active_streams': 0, 'real_viewers': 0, 'shadow_watchers': 0},
                }

            def get_m3u_account_by_id(self, account_id):
                return self.account if account_id == 1 else None

            def get_active_stream_context_per_profile(self, _account_id):
                return self.context

        udi = FakeUDI()
        limiter = AccountStreamLimiter(udi_manager=udi)
        limiter.set_account_limit(1, 1, profiles=[bridge_profile])
        udi.account = {
            'id': 1,
            'max_streams': 1,
            'profiles': [raw_profile, sibling_profile],
        }
        limiter.set_account_limit(1, 1, profiles=udi.account['profiles'])
        acquired, _reason, reserved = limiter.reserve_profile_for_stream({
            'id': 100,
            'url': 'http://provider.test/live/channel.ts',
            'm3u_account_id': 1,
        })
        self.assertTrue(acquired)
        self.assertEqual(reserved['id'], 20)
        limiter.release_profile(reserved)

    def test_pointer_swap_during_resolution_cannot_commit_stale_profile(self):
        original = self._profile(
            10,
            default=False,
            replace=r'http://credential-a.test/live/\2',
        )
        changed = self._profile(
            10,
            default=False,
            replace=r'http://credential-b.test/live/\2',
        )
        resolution_started = threading.Event()
        allow_resolution = threading.Event()

        class FakeUDI(_RealProfileRouteResolverMixin):
            def __init__(self):
                self.account = {'id': 1, 'max_streams': 1, 'profiles': [original]}

            def get_m3u_account_by_id(self, account_id):
                return self.account if account_id == 1 else None

            def get_active_streams_count_per_profile(self, _account_id):
                return {}

            def resolve_profile_stream_url(self, stream, profile):
                resolution_started.set()
                if not allow_resolution.wait(2):
                    raise AssertionError('resolution gate timed out')
                return super().resolve_profile_stream_url(stream, profile)

        udi = FakeUDI()
        limiter = AccountStreamLimiter(udi_manager=udi)
        limiter.set_account_limit(1, 1, profiles=[original])
        result = []
        worker = threading.Thread(target=lambda: result.append(
            limiter.reserve_profile_for_stream_with_url({
                'id': 100,
                'url': 'http://provider.test/live/channel.ts',
                'm3u_account_id': 1,
            })
        ))
        worker.start()
        self.assertTrue(resolution_started.wait(2))
        udi.account = {'id': 1, 'max_streams': 1, 'profiles': [changed]}
        allow_resolution.set()
        worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(result[0][0:2], (False, 'provider_profile_unavailable'))
        self.assertEqual(limiter.profile_checking_counts.get(10, 0), 0)
        self.assertFalse(limiter.profile_reservations_by_token)

    def test_atomic_authority_lease_linearizes_final_swap_before_or_after_commit(self):
        original = self._profile(
            10,
            default=False,
            replace=r'http://credential-a.test/live/\2',
        )
        changed = self._profile(
            10,
            default=False,
            replace=r'http://credential-b.test/live/\2',
        )
        lease_acquired = threading.Event()
        allow_lease_body = threading.Event()
        swap_attempted = threading.Event()
        swap_finished = threading.Event()

        class FakeUDI(_RealProfileRouteResolverMixin):
            supports_account_authority_lease = True

            def __init__(self):
                self.account = {'id': 1, 'max_streams': 1, 'profiles': [original]}
                self.lock = threading.RLock()

            def get_m3u_account_by_id(self, account_id):
                with self.lock:
                    return self.account if account_id == 1 else None

            @contextmanager
            def account_authority_lease(self, account_id):
                with self.lock:
                    lease_acquired.set()
                    if not allow_lease_body.wait(2):
                        raise AssertionError('authority lease gate timed out')
                    yield self.account if account_id == 1 else None

            def get_active_streams_count_per_profile(self, _account_id):
                return {}

        udi = FakeUDI()
        limiter = AccountStreamLimiter(udi_manager=udi)
        limiter.set_account_limit(1, 1, profiles=[original])
        result = []
        worker = threading.Thread(target=lambda: result.append(
            limiter.reserve_profile_for_stream_with_url({
                'id': 100,
                'url': 'http://provider.test/live/channel.ts',
                'm3u_account_id': 1,
            })
        ))
        worker.start()
        self.assertTrue(lease_acquired.wait(2))

        def swap_authority():
            swap_attempted.set()
            with udi.lock:
                udi.account = {
                    'id': 1,
                    'max_streams': 1,
                    'profiles': [changed],
                }
            swap_finished.set()

        swapper = threading.Thread(target=swap_authority)
        swapper.start()
        self.assertTrue(swap_attempted.wait(2))
        self.assertFalse(swap_finished.wait(0.05))
        allow_lease_body.set()
        worker.join(2)
        swapper.join(2)
        self.assertFalse(worker.is_alive())
        self.assertFalse(swapper.is_alive())
        self.assertTrue(result[0][0])
        self.assertTrue(swap_finished.is_set())
        self.assertTrue(
            limiter.should_preempt_profile_for_viewer(result[0][2], account_id=1)
        )
        limiter.release_profile(result[0][2])

    def test_slot_snapshot_rejects_counter_change_before_final_lease(self):
        profile = self._profile(10, default=True)
        final_lease_requested = threading.Event()
        allow_final_lease = threading.Event()

        class FakeUDI:
            supports_account_authority_lease = True

            def __init__(self):
                self.account = {'id': 1, 'max_streams': 1, 'profiles': [profile]}
                self.lock = threading.RLock()

            def get_m3u_account_by_id(self, account_id):
                with self.lock:
                    return self.account if account_id == 1 else None

            @contextmanager
            def account_authority_lease(self, account_id):
                if threading.current_thread().name == 'slot-snapshot-worker':
                    final_lease_requested.set()
                    if not allow_final_lease.wait(2):
                        raise AssertionError('slot snapshot lease gate timed out')
                with self.lock:
                    yield self.account if account_id == 1 else None

            def get_active_stream_context_per_profile(self, _account_id):
                return {
                    10: {'active_streams': 0, 'real_viewers': 0, 'shadow_watchers': 0}
                }

        udi = FakeUDI()
        limiter = AccountStreamLimiter(udi_manager=udi)
        limiter.set_account_limit(1, 1, profiles=[profile])
        snapshots = []
        worker = threading.Thread(
            name='slot-snapshot-worker',
            target=lambda: snapshots.append(limiter.get_profile_slot_snapshot(1)),
        )
        worker.start()
        self.assertTrue(final_lease_requested.wait(2))
        acquired, _reason, reserved = limiter.reserve_profile_for_stream({
            'id': 100,
            'url': 'http://provider.test/live/channel.ts',
            'm3u_account_id': 1,
        })
        self.assertTrue(acquired)
        allow_final_lease.set()
        worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(snapshots, [[]])
        limiter.release_profile(reserved)

    def test_malformed_default_and_usage_id_aliases_fail_closed(self):
        malformed_default = {
            'id': 10,
            'name': 'Malformed default flag',
            'max_streams': 1,
            'is_active': True,
            'is_default': 'false',
        }

        class FakeUDI(_RealProfileRouteResolverMixin):
            def __init__(self, profile, usage):
                self.account = {'id': 1, 'max_streams': 1, 'profiles': [profile]}
                self.usage = usage

            def get_m3u_account_by_id(self, account_id):
                return self.account if account_id == 1 else None

            def get_active_stream_context_per_profile(self, _account_id):
                return self.usage

        stream = {
            'id': 100,
            'url': 'http://provider.test/live/channel.ts',
            'm3u_account_id': 1,
        }
        limiter = AccountStreamLimiter(
            udi_manager=FakeUDI(malformed_default, {})
        )
        limiter.set_account_limit(1, 1, profiles=[malformed_default])
        self.assertEqual(
            limiter.reserve_profile_for_stream(stream)[0:2],
            (False, 'provider_profile_unavailable'),
        )

        valid_profile = self._profile(10, default=True)
        valid_usage = {
            10: {'active_streams': 0, 'real_viewers': 0, 'shadow_watchers': 0}
        }
        malformed_usage_cases = [
            {True: {'active_streams': 0, 'real_viewers': 0, 'shadow_watchers': 0}},
            {
                **valid_usage,
                '10': {'active_streams': 0, 'real_viewers': 0, 'shadow_watchers': 0},
            },
        ]
        for malformed_usage in malformed_usage_cases:
            with self.subTest(usage_keys=list(malformed_usage)):
                udi = FakeUDI(valid_profile, malformed_usage)
                limiter = AccountStreamLimiter(udi_manager=udi)
                limiter.set_account_limit(1, 1, profiles=[valid_profile])
                self.assertEqual(
                    limiter.reserve_profile_for_stream(stream)[0:2],
                    (False, 'provider_profile_unavailable'),
                )

    def test_provider_stream_requires_valid_account_or_explicit_custom_marker(self):
        checked = []
        limiter = AccountStreamLimiter()
        scheduler = SmartStreamScheduler(limiter, global_limit=1)
        malformed_streams = [
            {'id': 1, 'name': 'missing', 'url': 'http://provider.test/1'},
            {'id': 2, 'name': 'zero', 'url': 'http://provider.test/2', 'm3u_account_id': 0},
            {'id': 3, 'name': 'bool', 'url': 'http://provider.test/3', 'm3u_account_id': True},
            {'id': 4, 'name': 'text', 'url': 'http://provider.test/4', 'm3u_account_id': 'abc'},
        ]
        results = scheduler.check_streams_with_limits(
            streams=malformed_streams,
            check_function=lambda **kwargs: checked.append(kwargs),
            provider_wait_timeout=0,
        )
        self.assertFalse(checked)
        self.assertEqual(len(results), len(malformed_streams))
        self.assertTrue(all(
            result.get('reason_detail') == 'provider_profile_unavailable'
            for result in results
        ))

        custom_results = scheduler.check_streams_with_limits(
            streams=[{
                'id': 5,
                'name': 'custom',
                'url': 'http://custom.test/5',
                'is_custom': True,
            }],
            check_function=lambda **kwargs: {
                'stream_id': kwargs['stream_id'],
                'status': 'OK',
            },
        )
        self.assertEqual(custom_results[0]['status'], 'OK')

    def test_inflight_raw_and_profile_authority_drift_preempts(self):
        profile = self._profile(
            10,
            default=False,
            replace=r'http://credential-a.test/live/\2',
        )

        class FakeUDI(_RealProfileRouteResolverMixin):
            def __init__(self):
                self.account = {'id': 1, 'max_streams': 2, 'profiles': []}
                self.raise_account = False

            def get_m3u_account_by_id(self, account_id):
                if self.raise_account:
                    raise RuntimeError('authority unavailable')
                return self.account if account_id == 1 else None

            def get_active_streams_for_account(self, _account_id):
                return 0

            def get_active_stream_context_per_profile(self, _account_id):
                return {
                    10: {'active_streams': 0, 'real_viewers': 0, 'shadow_watchers': 0}
                }

        udi = FakeUDI()
        limiter = AccountStreamLimiter(udi_manager=udi)
        limiter.set_account_limit(1, 2, profiles=[])
        self.assertTrue(limiter.acquire(1, timeout=0)[0])
        acquired, _reason, raw_reservation = limiter.reserve_profile_for_stream({
            'id': 100,
            'url': 'http://provider.test/live/channel.ts',
            'm3u_account_id': 1,
        })
        self.assertTrue(acquired)
        self.assertIsNone(raw_reservation)

        udi.account = {'id': 1, 'max_streams': 2, 'profiles': [profile]}
        limiter.set_account_limit(1, 2, profiles=[profile])
        self.assertTrue(
            limiter.should_preempt_profile_for_viewer(None, account_id=1)
        )
        limiter.release(1)

        self.assertTrue(limiter.acquire(1, timeout=0)[0])
        acquired, _reason, reserved = limiter.reserve_profile_for_stream({
            'id': 101,
            'url': 'http://provider.test/live/channel.ts',
            'm3u_account_id': 1,
        })
        self.assertTrue(acquired)
        changed_search = dict(profile)
        changed_search['search_pattern'] = r'^(http://provider[.]test/)(live/.+)$'
        udi.account = {
            'id': 1,
            'max_streams': 2,
            'profiles': [changed_search],
        }
        limiter.set_account_limit(1, 2, profiles=[changed_search])
        self.assertTrue(
            limiter.should_preempt_profile_for_viewer(reserved, account_id=1)
        )
        limiter.release_profile(reserved)
        limiter.release(1)

        # Authority getter failures must preempt an otherwise under-capacity
        # running probe rather than silently using empty/current defaults.
        udi.account = {'id': 1, 'max_streams': 2, 'profiles': [profile]}
        limiter.set_account_limit(1, 2, profiles=[profile])
        self.assertTrue(limiter.acquire(1, timeout=0)[0])
        acquired, _reason, reserved = limiter.reserve_profile_for_stream({
            'id': 102,
            'url': 'http://provider.test/live/channel.ts',
            'm3u_account_id': 1,
        })
        self.assertTrue(acquired)
        udi.raise_account = True
        self.assertTrue(
            limiter.should_preempt_profile_for_viewer(reserved, account_id=1)
        )
        udi.raise_account = False
        limiter.release_profile(reserved)
        limiter.release(1)

    def test_shadow_only_account_saturation_preserves_shadow_reason(self):
        profile = self._profile(10, default=True)

        class FakeUDI:
            account = {'id': 1, 'max_streams': 1, 'profiles': [profile]}

            def get_m3u_account_by_id(self, account_id):
                return self.account if account_id == 1 else None

            def get_active_stream_context_per_profile(self, _account_id):
                return {
                    10: {
                        'active_streams': 1,
                        'real_viewers': 0,
                        'real_viewer_streams': 0,
                        'shadow_watchers': 1,
                    }
                }

            def get_stream_by_id(self, _stream_id):
                return None

        limiter = AccountStreamLimiter(udi_manager=FakeUDI())
        limiter.set_account_limit(1, 1, profiles=[profile])
        checked = []
        results = SmartStreamScheduler(limiter, global_limit=1).check_streams_with_limits(
            streams=[{
                'id': 100,
                'name': 'Shadow-saturated stream',
                'url': 'http://provider.test/live/channel.ts',
                'm3u_account_id': 1,
            }],
            check_function=lambda **kwargs: checked.append(kwargs),
            provider_wait_timeout=0,
        )
        self.assertFalse(checked)
        self.assertEqual(results[0]['reason_detail'], 'shadow_watchers')
        self.assertEqual(results[0]['skipped_reason'], 'shadow_watcher_capacity')


class TestSmartStreamScheduler(unittest.TestCase):
    """Test cases for SmartStreamScheduler."""

    def setUp(self):
        """Set up test fixtures."""
        class SchedulerUDI:
            def __init__(self):
                self.limiter = None

            def get_active_streams_for_account(self, _account_id):
                return 0

            def get_m3u_account_by_id(self, account_id):
                if self.limiter is None:
                    return None
                with self.limiter.lock:
                    if account_id not in self.limiter.account_inventory_ids:
                        return None
                    return {
                        'id': account_id,
                        'max_streams': self.limiter.account_fallback_limits.get(
                            account_id,
                            0,
                        ),
                        'profiles': [],
                    }

            def get_stream_by_id(self, _stream_id):
                return None

        self.udi = SchedulerUDI()
        self.limiter = AccountStreamLimiter(udi_manager=self.udi)
        self.udi.limiter = self.limiter
        self.scheduler = SmartStreamScheduler(self.limiter, global_limit=10)

    def test_empty_streams(self):
        """Test with no streams."""
        results = self.scheduler.check_streams_with_limits(
            streams=[],
            check_function=lambda **kwargs: {'result': 'ok'}
        )
        self.assertEqual(len(results), 0)

    def test_single_account_single_stream(self):
        """Test with one account and one stream."""
        self.limiter.set_account_limit(1, 1)

        def mock_check(**kwargs):
            time.sleep(0.1)
            return {'stream_id': kwargs['stream_id'], 'status': 'OK'}

        streams = [
            {'id': 1, 'name': 'Stream 1', 'url': 'http://test.com/1', 'm3u_account': 1}
        ]

        results = self.scheduler.check_streams_with_limits(
            streams=streams,
            check_function=mock_check
        )

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]['stream_id'], 1)
        self.assertEqual(results[0]['status'], 'OK')

    def test_single_account_respects_limit(self):
        """Test that single account limit is respected."""
        self.limiter.set_account_limit(1, 1)

        max_concurrent = [0]
        current_concurrent = [0]
        lock = threading.Lock()

        def mock_check(**kwargs):
            with lock:
                current_concurrent[0] += 1
                if current_concurrent[0] > max_concurrent[0]:
                    max_concurrent[0] = current_concurrent[0]

            time.sleep(0.2)  # Simulate work

            with lock:
                current_concurrent[0] -= 1

            return {'stream_id': kwargs['stream_id'], 'status': 'OK'}

        streams = [
            {'id': i, 'name': f'Stream {i}', 'url': f'http://test.com/{i}', 'm3u_account': 1}
            for i in range(5)
        ]
        
        results = self.scheduler.check_streams_with_limits(
            streams=streams,
            check_function=mock_check
        )
        
        self.assertEqual(len(results), 5)
        # With max_streams=1, should never have more than 1 concurrent
        self.assertEqual(max_concurrent[0], 1)
    
    def test_multiple_accounts_parallel(self):
        """Test that multiple accounts can run in parallel."""
        self.limiter.set_account_limit(1, 1)
        self.limiter.set_account_limit(2, 1)
        
        max_concurrent = [0]
        current_concurrent = [0]
        lock = threading.Lock()
        
        def mock_check(**kwargs):
            with lock:
                current_concurrent[0] += 1
                if current_concurrent[0] > max_concurrent[0]:
                    max_concurrent[0] = current_concurrent[0]
            
            time.sleep(0.2)  # Simulate work
            
            with lock:
                current_concurrent[0] -= 1
            
            return {'stream_id': kwargs['stream_id'], 'status': 'OK'}
        
        streams = [
            {'id': 1, 'name': 'Stream A1', 'url': 'http://test.com/a1', 'm3u_account': 1},
            {'id': 2, 'name': 'Stream A2', 'url': 'http://test.com/a2', 'm3u_account': 1},
            {'id': 3, 'name': 'Stream B1', 'url': 'http://test.com/b1', 'm3u_account': 2},
            {'id': 4, 'name': 'Stream B2', 'url': 'http://test.com/b2', 'm3u_account': 2},
        ]
        
        results = self.scheduler.check_streams_with_limits(
            streams=streams,
            check_function=mock_check
        )
        
        self.assertEqual(len(results), 4)
        # With 2 accounts each having max_streams=1, max concurrent should be 2
        self.assertEqual(max_concurrent[0], 2)
    
    def test_mixed_limits(self):
        """Test the example from requirements: A(1), B(2) with streams A1,A2,B1,B2,B3."""
        self.limiter.set_account_limit(1, 1)  # Account A: max 1
        self.limiter.set_account_limit(2, 2)  # Account B: max 2
        
        max_concurrent = [0]
        current_concurrent = [0]
        account_concurrent = {1: 0, 2: 0}
        max_account_concurrent = {1: 0, 2: 0}
        lock = threading.Lock()
        
        # Map stream IDs to account IDs
        stream_to_account = {1: 1, 2: 1, 3: 2, 4: 2, 5: 2}
        
        def mock_check(**kwargs):
            stream_id = kwargs.get('stream_id')
            account_id = stream_to_account.get(stream_id, 1)
            
            with lock:
                current_concurrent[0] += 1
                account_concurrent[account_id] += 1
                
                if current_concurrent[0] > max_concurrent[0]:
                    max_concurrent[0] = current_concurrent[0]
                if account_concurrent[account_id] > max_account_concurrent[account_id]:
                    max_account_concurrent[account_id] = account_concurrent[account_id]
            
            time.sleep(0.2)  # Simulate work
            
            with lock:
                current_concurrent[0] -= 1
                account_concurrent[account_id] -= 1
            
            return {'stream_id': kwargs['stream_id'], 'status': 'OK'}
        
        streams = [
            {'id': 1, 'name': 'Stream A1', 'url': 'http://test.com/a1', 'm3u_account': 1},
            {'id': 2, 'name': 'Stream A2', 'url': 'http://test.com/a2', 'm3u_account': 1},
            {'id': 3, 'name': 'Stream B1', 'url': 'http://test.com/b1', 'm3u_account': 2},
            {'id': 4, 'name': 'Stream B2', 'url': 'http://test.com/b2', 'm3u_account': 2},
            {'id': 5, 'name': 'Stream B3', 'url': 'http://test.com/b3', 'm3u_account': 2},
        ]
        
        results = self.scheduler.check_streams_with_limits(
            streams=streams,
            check_function=mock_check
        )
        
        self.assertEqual(len(results), 5)
        # Account A should never exceed 1 concurrent
        self.assertLessEqual(max_account_concurrent[1], 1)
        # Account B should never exceed 2 concurrent
        self.assertLessEqual(max_account_concurrent[2], 2)
        # Overall, should be able to run 3 streams concurrently (A1+B1+B2)
        self.assertEqual(max_concurrent[0], 3)
    
    def test_active_viewers_limit_concurrent_checks(self):
        """Test that active viewers reduce available slots for concurrent checks.
        
        This is the scenario from the problem statement:
        - M3U account has max_streams=2
        - 1 stream is currently being played (active viewer)
        - Channel check runs with concurrent checking enabled
        - Only 1 stream should be checked at a time (respecting the limit)
        """
        # Create a mock UDI manager that reports 1 active stream
        mock_udi = Mock()
        mock_udi.get_active_streams_for_account.return_value = 1
        mock_udi.get_m3u_account_by_id.return_value = {
            'id': 1,
            'max_streams': 2,
            'profiles': [],
        }
        # Mock the new profile-aware checking to always allow (let the limiter handle it)
        mock_udi.check_stream_can_run.return_value = (True, None)
        
        # Create limiter with mock UDI
        limiter = AccountStreamLimiter(udi_manager=mock_udi)
        limiter.set_account_limit(1, 2)  # max_streams=2
        
        scheduler = SmartStreamScheduler(limiter, global_limit=10)
        
        max_concurrent = [0]
        current_concurrent = [0]
        lock = threading.Lock()
        
        def mock_check(**kwargs):
            with lock:
                current_concurrent[0] += 1
                if current_concurrent[0] > max_concurrent[0]:
                    max_concurrent[0] = current_concurrent[0]
            
            time.sleep(0.2)  # Simulate work
            
            with lock:
                current_concurrent[0] -= 1
            
            return {'stream_id': kwargs['stream_id'], 'status': 'OK'}
        
        # All streams from the same account
        streams = [
            {'id': 1, 'name': 'Stream 1', 'url': 'http://test.com/1', 'm3u_account': 1},
            {'id': 2, 'name': 'Stream 2', 'url': 'http://test.com/2', 'm3u_account': 1},
            {'id': 3, 'name': 'Stream 3', 'url': 'http://test.com/3', 'm3u_account': 1},
        ]
        
        results = scheduler.check_streams_with_limits(
            streams=streams,
            check_function=mock_check
        )
        
        self.assertEqual(len(results), 3)
        # With 1 active viewer and max_streams=2, only 1 check should run at a time
        # (1 active + 1 checking = 2/2 limit)
        self.assertEqual(max_concurrent[0], 1, 
                        "Should only check 1 stream at a time when 1 active viewer exists")

    def test_saturated_provider_does_not_block_free_provider(self):
        """A waiting provider must not prevent a later free provider from running."""
        self.limiter.set_account_limit(1, 1)
        self.limiter.set_account_limit(2, 1)
        mock_udi = Mock()
        mock_udi.get_active_streams_for_account.side_effect = (
            lambda account_id: 1 if account_id == 1 else 0
        )
        mock_udi.get_m3u_account_by_id.side_effect = lambda account_id: {
            'id': account_id,
            'max_streams': 1,
            'profiles': [],
        }
        mock_udi.get_stream_by_id.return_value = None
        self.limiter.udi_manager = mock_udi

        checked_ids = []
        defer_calls = []
        lock = threading.Lock()

        def mock_check(**kwargs):
            with lock:
                checked_ids.append(kwargs['stream_id'])
            return {'stream_id': kwargs['stream_id'], 'status': 'OK'}

        streams = [
            {'id': 1, 'name': 'Blocked A', 'url': 'http://test.com/a', 'm3u_account': 1},
            {'id': 2, 'name': 'Free B', 'url': 'http://test.com/b', 'm3u_account': 2},
        ]

        results = self.scheduler.check_streams_with_limits(
            streams=streams,
            check_function=mock_check,
            defer_callback=lambda stream, reason: defer_calls.append((stream['id'], reason)),
            provider_wait_timeout=0.2,
        )

        self.assertIn(2, checked_ids)
        self.assertNotIn(1, checked_ids)
        self.assertTrue(any(stream_id == 1 for stream_id, _reason in defer_calls))
        skipped = [r for r in results if r.get('stream_id') == 1]
        self.assertEqual(len(skipped), 1)
        self.assertTrue(skipped[0].get('provider_limit_skipped'))

    def test_saturated_provider_batch_does_not_hide_later_free_provider(self):
        """Round-robin scheduling must reach a free provider after many blocked streams."""
        self.limiter.set_account_limit(1, 1)
        self.limiter.set_account_limit(2, 1)
        mock_udi = Mock()
        mock_udi.get_active_streams_for_account.side_effect = (
            lambda account_id: 1 if account_id == 1 else 0
        )
        mock_udi.get_m3u_account_by_id.side_effect = lambda account_id: {
            'id': account_id,
            'max_streams': 1,
            'profiles': [],
        }
        mock_udi.get_stream_by_id.return_value = None
        self.limiter.udi_manager = mock_udi

        checked_ids = []

        def mock_check(**kwargs):
            checked_ids.append(kwargs['stream_id'])
            return {'stream_id': kwargs['stream_id'], 'status': 'OK'}

        blocked_streams = [
            {'id': i, 'name': f'Blocked A{i}', 'url': f'http://test.com/a{i}', 'm3u_account': 1}
            for i in range(1, 41)
        ]
        free_stream = {'id': 100, 'name': 'Free B', 'url': 'http://test.com/b', 'm3u_account': 2}

        results = self.scheduler.check_streams_with_limits(
            streams=blocked_streams + [free_stream],
            check_function=mock_check,
            provider_wait_timeout=0.1,
        )

        self.assertIn(100, checked_ids)
        free_result = [r for r in results if r.get('stream_id') == 100]
        self.assertEqual(len(free_result), 1)
        self.assertEqual(free_result[0]['status'], 'OK')

    def test_parallel_checks_reserve_distinct_profiles(self):
        """Concurrent checks for one account should not reuse the same credential."""
        profiles = [
            _credential_profile(10, 'Credential 1'),
            _credential_profile(11, 'Credential 2'),
        ]

        class FakeUDI(_RealProfileRouteResolverMixin):
            account = {
                'id': 1,
                'name': 'Provider A',
                'profiles': profiles,
            }

            def get_active_streams_for_account(self, _account_id):
                return 0

            def get_active_streams_count_per_profile(self, _account_id):
                return {}

            def get_m3u_account_by_id(self, account_id):
                return self.account if account_id == 1 else None

            def get_stream_by_id(self, _stream_id):
                return None

        limiter = AccountStreamLimiter(udi_manager=FakeUDI())
        limiter.set_account_limit(
            1,
            0,
            profiles=profiles,
        )
        scheduler = SmartStreamScheduler(limiter, global_limit=2)

        started_urls = []
        both_started = threading.Event()
        lock = threading.Lock()

        def mock_check(**kwargs):
            with lock:
                started_urls.append(kwargs['stream_url'])
                if len(started_urls) == 2:
                    both_started.set()
            both_started.wait(0.5)
            return {'stream_id': kwargs['stream_id'], 'status': 'OK'}

        results = scheduler.check_streams_with_limits(
            streams=[
                {'id': 1, 'name': 'Stream 1', 'url': 'http://test.com/1', 'm3u_account': 1},
                {'id': 2, 'name': 'Stream 2', 'url': 'http://test.com/2', 'm3u_account': 1},
            ],
            check_function=mock_check,
            provider_wait_timeout=0.1,
        )

        self.assertEqual(len(results), 2)
        self.assertEqual(sorted(url.rsplit('=', 1)[-1] for url in started_urls), ['10', '11'])

    def test_transformed_probe_url_is_rebound_to_canonical_result_url(self):
        """Alternate credentials are probe-only and must not escape in results."""
        raw_url = 'http://provider.example/live/source-user/source-pass/101.ts'
        alternate_url = 'http://provider.example/live/alternate-user/alternate-pass/101.ts'
        profile = {
            'id': 10,
            'name': 'Alternate Credential',
            'max_streams': 1,
            'is_active': True,
            'is_default': False,
            'search_pattern': (
                r'^http://provider[.]example/live/source-user/source-pass/(.+)$'
            ),
            'replace_pattern': (
                r'http://provider.example/live/alternate-user/alternate-pass/$1'
            ),
        }

        class FakeUDI(_RealProfileRouteResolverMixin):
            account = {'id': 1, 'profiles': [profile]}

            def get_active_streams_for_account(self, _account_id):
                return 0

            def get_active_streams_count_per_profile(self, _account_id):
                return {}

            def get_m3u_account_by_id(self, account_id):
                return self.account if account_id == 1 else None

            def get_stream_by_id(self, _stream_id):
                return None

        limiter = AccountStreamLimiter(udi_manager=FakeUDI())
        limiter.set_account_limit(1, 1, profiles=[profile])
        scheduler = SmartStreamScheduler(limiter, global_limit=1)
        probed_urls = []

        def mock_check(**kwargs):
            probed_urls.append(kwargs['stream_url'])
            return {
                'stream_id': kwargs['stream_id'],
                'stream_url': kwargs['stream_url'],
                'status': 'OK',
            }

        results = scheduler.check_streams_with_limits(
            streams=[{
                'id': 101,
                'name': 'Credential-bound stream',
                'url': raw_url,
                'm3u_account_id': 1,
            }],
            check_function=mock_check,
            provider_wait_timeout=0.1,
        )

        self.assertEqual(probed_urls, [alternate_url])
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]['stream_url'], raw_url)
        self.assertNotIn(alternate_url, repr(results[0]))

    def test_distinct_profile_routes_raise_capacity_and_bind_each_probe_to_its_reservation(self):
        """Independent credential routes must overlap without crossing profile URLs."""
        raw_url_template = (
            'http://provider.example/live/source-user/source-token/{stream_id}.ts'
        )
        profiles = [
            {
                'id': 10,
                'name': 'Default Credential',
                'max_streams': 1,
                'is_active': True,
                'is_default': True,
                'search_pattern': (
                    r'^http://provider\.example/live/source-user/source-token/(.+)$'
                ),
                'replace_pattern': (
                    r'http://provider.example/live/source-user/source-token/$1'
                ),
            },
            {
                'id': 11,
                'name': 'Credential B',
                'max_streams': 1,
                'is_active': True,
                'is_default': False,
                'search_pattern': (
                    r'^http://provider\.example/live/source-user/source-token/(.+)$'
                ),
                'replace_pattern': (
                    r'http://provider.example/live/profile-b-user/profile-b-token/$1'
                ),
            },
        ]

        class FakeUDI(_RealProfileRouteResolverMixin):
            account = {
                'id': 1,
                'name': 'Provider A',
                'max_streams': 1,
                'profiles': profiles,
            }

            def __init__(self):
                self.resolution_calls = []

            def get_active_streams_for_account(self, _account_id):
                return 0

            def get_active_streams_count_per_profile(self, _account_id):
                return {}

            def get_m3u_account_by_id(self, account_id):
                return self.account if account_id == 1 else None

            def resolve_profile_stream_url(self, stream, profile):
                resolved = super().resolve_profile_stream_url(stream, profile)
                self.resolution_calls.append(
                    (stream['id'], profile['id'], resolved[0], resolved[1])
                )
                return resolved

            def apply_profile_url_transformation(self, _stream, profile=None):
                raise AssertionError(
                    f'profile {profile.get("id") if profile else None} was transformed after reservation'
                )

            def get_stream_by_id(self, _stream_id):
                return None

        udi = FakeUDI()
        limiter = AccountStreamLimiter(udi_manager=udi)
        limiter.set_account_limit(1, 1, profiles=profiles)
        scheduler = SmartStreamScheduler(limiter, global_limit=2)
        streams = [
            {
                'id': stream_id,
                'name': f'Stream {stream_id}',
                'url': raw_url_template.format(stream_id=stream_id),
                'm3u_account': 1,
            }
            for stream_id in (101, 102)
        ]

        started_profiles = {}
        probe_records = []
        overlap_wait_results = []
        active_probes = 0
        max_active_probes = 0
        both_probes_started = threading.Event()
        state_lock = threading.Lock()
        reservation_records = []
        original_reserve = limiter.reserve_profile_for_stream_with_url

        def observed_reserve(stream):
            outcome = original_reserve(stream)
            if outcome[0]:
                with state_lock:
                    reservation_records.append(
                        (stream['id'], outcome[2]['id'] if outcome[2] else None, outcome[3])
                    )
            return outcome

        limiter.reserve_profile_for_stream_with_url = observed_reserve

        def started(stream, profile):
            with state_lock:
                started_profiles[stream['id']] = profile['id'] if profile else None

        def mock_check(**kwargs):
            nonlocal active_probes, max_active_probes
            with state_lock:
                profile_id = started_profiles.get(kwargs['stream_id'])
                probe_records.append(
                    (kwargs['stream_id'], profile_id, kwargs['stream_url'])
                )
                active_probes += 1
                max_active_probes = max(max_active_probes, active_probes)
                if len(probe_records) == 2:
                    both_probes_started.set()

            overlapped = both_probes_started.wait(2)

            with state_lock:
                overlap_wait_results.append(overlapped)
                active_probes -= 1

            return {'stream_id': kwargs['stream_id'], 'status': 'OK'}

        results = scheduler.check_streams_with_limits(
            streams=streams,
            check_function=mock_check,
            start_callback=started,
            provider_wait_timeout=0.1,
        )

        expected_urls = {
            (stream['id'], profile['id']): UDIManager.resolve_profile_stream_url(
                udi,
                stream,
                profile,
            )[1]
            for stream in streams
            for profile in profiles
        }

        self.assertEqual(limiter.get_account_limit(1), 2)
        self.assertEqual(len(results), 2)
        self.assertTrue(all(result['status'] == 'OK' for result in results))
        self.assertEqual(max_active_probes, 2)
        self.assertEqual(overlap_wait_results, [True, True])
        self.assertEqual({record[1] for record in probe_records}, {10, 11})
        for stream_id, profile_id, stream_url in probe_records:
            self.assertEqual(stream_url, expected_urls[(stream_id, profile_id)])
        self.assertEqual(set(reservation_records), set(probe_records))
        for stream_id, profile_id, eligible, resolved_url in udi.resolution_calls:
            self.assertTrue(eligible)
            self.assertEqual(resolved_url, expected_urls[(stream_id, profile_id)])
        self.assertEqual(limiter.account_checking_counts[1], 0)
        self.assertTrue(all(count == 0 for count in limiter.profile_checking_counts.values()))

    def test_invalid_alternate_profile_route_never_probes_with_raw_default_url(self):
        """A reserved alternate profile must fail closed if its rewrite is unusable."""
        raw_url = 'http://provider.example/live/source-user/source-token/101.ts'
        default_profile = {
            'id': 10,
            'name': 'Default Credential',
            'max_streams': 1,
            'is_active': True,
            'search_pattern': None,
            'replace_pattern': None,
        }

        for case_name, alternate_profile in {
            'nonmatching': {
                'id': 11,
                'name': 'Nonmatching Alternate',
                'max_streams': 1,
                'is_active': True,
                'search_pattern': r'^http://different-provider\.example/(.+)$',
                'replace_pattern': r'http://alternate.example/live/user/token/$1',
            },
            'malformed': {
                'id': 11,
                'name': 'Malformed Alternate',
                'max_streams': 1,
                'is_active': True,
                'search_pattern': r'(',
                'replace_pattern': r'http://alternate.example/live/user/token/$1',
            },
        }.items():
            with self.subTest(route=case_name):
                profiles = [alternate_profile, default_profile]

                class FakeUDI(_RealProfileRouteResolverMixin):
                    def __init__(self):
                        self.account = {
                            'id': 1,
                            'name': 'Provider A',
                            'max_streams': 1,
                            'profiles': profiles,
                        }

                    def get_active_streams_for_account(self, _account_id):
                        return 0

                    def get_active_streams_count_per_profile(self, _account_id):
                        return {}

                    def get_m3u_account_by_id(self, account_id):
                        return self.account if account_id == 1 else None

                    def apply_profile_url_transformation(self, _stream, profile=None):
                        raise AssertionError(
                            f'profile {profile.get("id") if profile else None} was transformed after reservation'
                        )

                    def get_stream_by_id(self, _stream_id):
                        return None

                udi = FakeUDI()
                limiter = AccountStreamLimiter(udi_manager=udi)
                limiter.set_account_limit(1, 1, profiles=profiles)
                scheduler = SmartStreamScheduler(limiter, global_limit=1)
                reservation_records = []
                started_profiles = {}
                probe_records = []
                original_reserve = limiter.reserve_profile_for_stream_with_url

                def observed_reserve(stream):
                    outcome = original_reserve(stream)
                    if outcome[0]:
                        reservation_records.append(
                            (outcome[2]['id'] if outcome[2] else None, outcome[3])
                        )
                    return outcome

                limiter.reserve_profile_for_stream_with_url = observed_reserve

                def started(stream, profile):
                    started_profiles[stream['id']] = profile['id'] if profile else None

                def mock_check(**kwargs):
                    probe_records.append(
                        (
                            started_profiles.get(kwargs['stream_id']),
                            kwargs['stream_url'],
                        )
                    )
                    return {'stream_id': kwargs['stream_id'], 'status': 'OK'}

                results = scheduler.check_streams_with_limits(
                    streams=[{
                        'id': 101,
                        'name': 'Stream 101',
                        'url': raw_url,
                        'm3u_account': 1,
                    }],
                    check_function=mock_check,
                    start_callback=started,
                    provider_wait_timeout=0,
                )

                self.assertEqual(reservation_records, [(10, raw_url)])
                self.assertEqual(probe_records, [(10, raw_url)])
                self.assertEqual(len(results), 1)
                self.assertEqual(results[0]['status'], 'OK')
                self.assertEqual(limiter.account_checking_counts[1], 0)
                self.assertTrue(
                    all(count == 0 for count in limiter.profile_checking_counts.values())
                )

    def test_missing_profile_reservation_cannot_trigger_unreserved_auto_selection(self):
        """The scheduler must not ask UDI to auto-select after reserving no profile."""
        raw_url = 'http://provider.example/live/source-user/source-token/101.ts'
        auto_selected_url = 'http://provider.example/live/unreserved-user/unreserved-token/101.ts'

        class FakeUDI:
            account = {
                'id': 1,
                'name': 'Provider A',
                'max_streams': 1,
                'profiles': [],
            }

            def __init__(self):
                self.transform_profiles = []

            def get_active_streams_for_account(self, _account_id):
                return 0

            def get_m3u_account_by_id(self, account_id):
                return self.account if account_id == 1 else None

            def check_stream_can_run(self, _stream):
                return (True, None)

            def apply_profile_url_transformation(self, _stream, profile=None):
                self.transform_profiles.append(profile)
                return auto_selected_url

            def get_stream_by_id(self, _stream_id):
                return None

        udi = FakeUDI()
        limiter = AccountStreamLimiter(udi_manager=udi)
        limiter.set_account_limit(1, 1, profiles=[])
        scheduler = SmartStreamScheduler(limiter, global_limit=1)
        probed_urls = []

        results = scheduler.check_streams_with_limits(
            streams=[{
                'id': 101,
                'name': 'Stream 101',
                'url': raw_url,
                'm3u_account': 1,
            }],
            check_function=lambda **kwargs: (
                probed_urls.append(kwargs['stream_url'])
                or {'stream_id': kwargs['stream_id'], 'status': 'OK'}
            ),
            provider_wait_timeout=0,
        )

        self.assertEqual(udi.transform_profiles, [])
        self.assertNotIn(auto_selected_url, probed_urls)
        self.assertTrue(all(url == raw_url for url in probed_urls))
        self.assertEqual(len(results), 1)
        self.assertEqual(limiter.account_checking_counts[1], 0)

    def test_equivalent_profile_routes_do_not_inflate_account_capacity(self):
        """Null or identical routes share the imported account's single slot."""
        transform_cases = {
            'null': (
                [
                    {
                        'id': 10,
                        'name': 'Alias 1',
                        'max_streams': 1,
                        'is_active': True,
                        'search_pattern': None,
                        'replace_pattern': None,
                    },
                    {
                        'id': 11,
                        'name': 'Alias 2',
                        'max_streams': 1,
                        'is_active': True,
                        'search_pattern': None,
                        'replace_pattern': None,
                    },
                ],
                lambda url: url,
            ),
            'identical': (
                [
                    {
                        'id': 10,
                        'name': 'Alias 1',
                        'max_streams': 1,
                        'is_active': True,
                        'search_pattern': r'example\.test',
                        'replace_pattern': 'shared.example.test',
                    },
                    {
                        'id': 11,
                        'name': 'Alias 2',
                        'max_streams': 1,
                        'is_active': True,
                        'search_pattern': r'example\.test',
                        'replace_pattern': 'shared.example.test',
                    },
                ],
                lambda url: url.replace('example.test', 'shared.example.test'),
            ),
            'different_search_same_target': (
                [
                    {
                        'id': 10,
                        'name': 'Alias 1',
                        'max_streams': 1,
                        'is_active': True,
                        'search_pattern': r'example\.test',
                        'replace_pattern': 'shared.example.test',
                    },
                    {
                        'id': 11,
                        'name': 'Alias 2',
                        'max_streams': 1,
                        'is_active': True,
                        'search_pattern': r'example[.]test',
                        'replace_pattern': 'shared.example.test',
                    },
                ],
                lambda url: url.replace('example.test', 'shared.example.test'),
            ),
        }

        for case_name, (profiles, transform) in transform_cases.items():
            with self.subTest(transform=case_name):
                class FakeUDI:
                    account = {
                        'id': 1,
                        'name': 'Provider A',
                        'max_streams': 1,
                        'profiles': profiles,
                    }

                    def get_active_streams_for_account(self, _account_id):
                        return 0

                    def get_active_streams_count_per_profile(self, _account_id):
                        return {}

                    def get_m3u_account_by_id(self, account_id):
                        return self.account if account_id == 1 else None

                    def check_stream_can_run(self, _stream):
                        return (True, None)

                    def apply_profile_url_transformation(self, stream, profile=None):
                        return transform(stream['url'])

                    def get_stream_by_id(self, _stream_id):
                        return None

                limiter = AccountStreamLimiter(udi_manager=FakeUDI())
                limiter.set_account_limit(1, 1, profiles=profiles)
                scheduler = SmartStreamScheduler(limiter, global_limit=2)
                active_checks = 0
                max_active_checks = 0
                checked_urls = []
                active_lock = threading.Lock()
                first_check_started = threading.Event()
                release_first_check = threading.Event()
                second_acquire_observed = threading.Event()
                continue_second_acquire = threading.Event()
                acquire_results = []

                original_acquire = limiter.acquire
                acquire_call_count = 0

                def observed_acquire(account_id, timeout=None):
                    nonlocal acquire_call_count
                    with active_lock:
                        acquire_call_count += 1
                        call_number = acquire_call_count
                    result = original_acquire(account_id, timeout=timeout)
                    if call_number == 2:
                        acquire_results.append(bool(result))
                        second_acquire_observed.set()
                        if not continue_second_acquire.wait(2):
                            raise AssertionError('Timed out waiting to continue second acquire')
                    return result

                limiter.acquire = observed_acquire

                def mock_check(**kwargs):
                    nonlocal active_checks, max_active_checks
                    with active_lock:
                        active_checks += 1
                        max_active_checks = max(max_active_checks, active_checks)
                        checked_urls.append(kwargs['stream_url'])
                        is_first = len(checked_urls) == 1
                    if is_first:
                        first_check_started.set()
                        if not release_first_check.wait(2):
                            raise AssertionError('Timed out waiting to release first check')
                    with active_lock:
                        active_checks -= 1
                    return {'stream_id': kwargs['stream_id'], 'status': 'OK'}

                results_holder = []

                runner = threading.Thread(target=lambda: results_holder.extend(
                    scheduler.check_streams_with_limits(
                        streams=[
                            {
                                'id': 1,
                                'name': 'Stream 1',
                                'url': 'http://example.test/1',
                                'm3u_account': 1,
                            },
                            {
                                'id': 2,
                                'name': 'Stream 2',
                                'url': 'http://example.test/2',
                                'm3u_account': 1,
                            },
                        ],
                        check_function=mock_check,
                        provider_wait_timeout=0.1,
                    )
                ))
                runner.start()
                try:
                    self.assertTrue(first_check_started.wait(2))
                    self.assertTrue(second_acquire_observed.wait(2))
                    self.assertEqual(acquire_results, [False])
                finally:
                    continue_second_acquire.set()
                    release_first_check.set()
                runner.join(3)

                self.assertFalse(runner.is_alive())
                results = results_holder
                self.assertEqual(limiter.get_account_limit(1), 1)
                self.assertEqual(len(results), 2)
                self.assertEqual(max_active_checks, 1)
                self.assertEqual(
                    sorted(checked_urls),
                    sorted([
                        transform('http://example.test/1'),
                        transform('http://example.test/2'),
                    ]),
                )

    def test_active_profile_does_not_block_free_sibling_profile(self):
        """A viewer on one credential should still leave a sibling credential usable."""
        profiles = [
            _credential_profile(10, 'Busy Credential'),
            _credential_profile(11, 'Free Credential'),
        ]

        class FakeUDI(_RealProfileRouteResolverMixin):
            account = {
                'id': 1,
                'name': 'Provider A',
                'profiles': profiles,
            }

            def get_active_streams_for_account(self, _account_id):
                return 1

            def get_active_streams_count_per_profile(self, _account_id):
                return {10: 1, 11: 0}

            def get_m3u_account_by_id(self, account_id):
                return self.account if account_id == 1 else None

            def get_stream_by_id(self, _stream_id):
                return None

        limiter = AccountStreamLimiter(udi_manager=FakeUDI())
        limiter.set_account_limit(
            1,
            0,
            profiles=profiles,
        )
        scheduler = SmartStreamScheduler(limiter, global_limit=1)

        checked_urls = []

        def mock_check(**kwargs):
            checked_urls.append(kwargs['stream_url'])
            return {'stream_id': kwargs['stream_id'], 'status': 'OK'}

        results = scheduler.check_streams_with_limits(
            streams=[{'id': 1, 'name': 'Stream 1', 'url': 'http://test.com/1', 'm3u_account': 1}],
            check_function=mock_check,
            provider_wait_timeout=0.1,
        )

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]['status'], 'OK')
        self.assertEqual(checked_urls, ['http://test.com/1?profile=11'])

    def test_deferred_provider_stream_runs_after_capacity_frees(self):
        """A deferred stream should run once provider capacity becomes available."""
        self.limiter.set_account_limit(1, 1)
        acquired, _ = self.limiter.acquire(1, timeout=0)
        self.assertTrue(acquired)

        checked_ids = []

        def release_later():
            time.sleep(0.1)
            self.limiter.release(1)

        def mock_check(**kwargs):
            checked_ids.append(kwargs['stream_id'])
            return {'stream_id': kwargs['stream_id'], 'status': 'OK'}

        releaser = threading.Thread(target=release_later)
        releaser.start()
        results = self.scheduler.check_streams_with_limits(
            streams=[{'id': 1, 'name': 'Deferred A', 'url': 'http://test.com/a', 'm3u_account': 1}],
            check_function=mock_check,
            provider_wait_timeout=1.0,
        )
        releaser.join()

        self.assertEqual(checked_ids, [1])
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]['status'], 'OK')

    def test_internal_checker_capacity_waits_past_external_timeout(self):
        """Checker-owned provider slots should wait for completion instead of skipping."""
        self.limiter.set_account_limit(1, 1)
        acquired, _ = self.limiter.acquire(1, timeout=0)
        self.assertTrue(acquired)

        checked_ids = []
        defer_reasons = []

        def release_later():
            time.sleep(0.25)
            self.limiter.release(1)

        def mock_check(**kwargs):
            checked_ids.append(kwargs['stream_id'])
            return {'stream_id': kwargs['stream_id'], 'status': 'OK'}

        releaser = threading.Thread(target=release_later)
        releaser.start()
        results = self.scheduler.check_streams_with_limits(
            streams=[{'id': 1, 'name': 'Deferred A', 'url': 'http://test.com/a', 'm3u_account': 1}],
            check_function=mock_check,
            defer_callback=lambda _stream, reason: defer_reasons.append(reason),
            provider_wait_timeout=0.1,
        )
        releaser.join()

        self.assertEqual(checked_ids, [1])
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]['status'], 'OK')
        self.assertIn('checking_capacity', defer_reasons)

    def test_provider_wait_timeout_returns_skip_result_not_error(self):
        """Active-viewer capacity timeout should preserve state instead of creating an error."""
        self.limiter.set_account_limit(1, 1)
        mock_udi = Mock()
        mock_udi.get_active_streams_for_account.return_value = 1
        mock_udi.get_stream_by_id.return_value = None
        self.limiter.udi_manager = mock_udi

        results = self.scheduler.check_streams_with_limits(
            streams=[{'id': 1, 'name': 'Blocked A', 'url': 'http://test.com/a', 'm3u_account': 1}],
            check_function=lambda **_kwargs: self.fail("check_function should not run"),
            provider_wait_timeout=0.1,
        )

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]['status'], 'SKIPPED_PROVIDER_LIMIT')
        self.assertTrue(results[0]['provider_limit_skipped'])
        self.assertEqual(results[0]['skipped_reason'], 'quota_consumed_by_active_viewers')

    def test_viewer_preempts_reserved_profile_probe(self):
        """A viewer taking a reserved credential should abort only that probe."""
        class FakeUDI:
            account = {
                'id': 1,
                'name': 'Provider A',
                'profiles': [
                    {'id': 10, 'name': 'Credential 1', 'max_streams': 1, 'is_active': True},
                ],
            }

            def __init__(self):
                self.active_count = 0

            def get_active_streams_for_account(self, _account_id):
                return self.active_count

            def get_active_streams_count_per_profile(self, _account_id):
                return {10: self.active_count}

            def get_active_streams_for_profile(self, profile_id):
                return self.active_count if profile_id == 10 else 0

            def get_m3u_account_by_id(self, account_id):
                return self.account if account_id == 1 else None

            def check_stream_can_run(self, _stream):
                return (True, None)

            def apply_profile_url_transformation(self, stream, profile=None):
                profile_id = profile.get('id') if profile else 'none'
                return f"{stream['url']}?profile={profile_id}"

            def get_stream_by_id(self, _stream_id):
                return {'stream_stats': {'resolution': '1920x1080', 'ffmpeg_output_bitrate': 5000}}

        fake_udi = FakeUDI()
        limiter = AccountStreamLimiter(udi_manager=fake_udi)
        limiter.set_account_limit(
            1,
            0,
            profiles=[{'id': 10, 'name': 'Credential 1', 'max_streams': 1, 'is_active': True}],
        )
        scheduler = SmartStreamScheduler(limiter, global_limit=1)

        def mock_check(**kwargs):
            self.assertIn('preempt_check', kwargs)
            self.assertFalse(kwargs['preempt_check']())
            fake_udi.active_count = 1
            self.assertTrue(kwargs['preempt_check']())
            return {
                'stream_id': kwargs['stream_id'],
                'stream_name': kwargs['stream_name'],
                'stream_url': kwargs['stream_url'],
                'status': 'PREEMPTED',
                'preempted': True,
                'preempt_reason': 'viewer_preempted',
            }

        results = scheduler.check_streams_with_limits(
            streams=[{'id': 1, 'name': 'Stream 1', 'url': 'http://test.com/1', 'm3u_account': 1}],
            check_function=mock_check,
            provider_wait_timeout=0.1,
        )

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]['status'], 'SKIPPED_PROVIDER_LIMIT')
        self.assertTrue(results[0]['provider_limit_skipped'])
        self.assertEqual(results[0]['skipped_reason'], 'viewer_preempted')
        self.assertEqual(results[0]['reason_detail'], 'viewer_preempted')
        self.assertTrue(results[0]['cached'])

    def test_viewer_preempted_probe_retries_free_sibling_profile(self):
        """A preempted probe should move to another free profile before skipping."""
        profiles = [
            _credential_profile(10, 'Credential 1'),
            _credential_profile(11, 'Credential 2'),
        ]

        class FakeUDI(_RealProfileRouteResolverMixin):
            account = {
                'id': 1,
                'name': 'Provider A',
                'profiles': profiles,
            }

            def __init__(self):
                self.active_by_profile = {10: 0, 11: 0}

            def get_active_streams_for_account(self, _account_id):
                return sum(self.active_by_profile.values())

            def get_active_streams_count_per_profile(self, _account_id):
                return dict(self.active_by_profile)

            def get_active_streams_for_profile(self, profile_id):
                return self.active_by_profile.get(profile_id, 0)

            def get_m3u_account_by_id(self, account_id):
                return self.account if account_id == 1 else None

            def get_stream_by_id(self, _stream_id):
                return {'stream_stats': {'resolution': '1920x1080', 'ffmpeg_output_bitrate': 5000}}

        fake_udi = FakeUDI()
        limiter = AccountStreamLimiter(udi_manager=fake_udi)
        limiter.set_account_limit(1, 0, profiles=fake_udi.account['profiles'])
        scheduler = SmartStreamScheduler(limiter, global_limit=1)

        checked_urls = []
        defer_reasons = []

        def mock_check(**kwargs):
            checked_urls.append(kwargs['stream_url'])
            if kwargs['stream_url'].endswith('profile=10'):
                self.assertFalse(kwargs['preempt_check']())
                fake_udi.active_by_profile[10] = 1
                self.assertTrue(kwargs['preempt_check']())
                return {
                    'stream_id': kwargs['stream_id'],
                    'stream_name': kwargs['stream_name'],
                    'stream_url': kwargs['stream_url'],
                    'status': 'PREEMPTED',
                    'preempted': True,
                    'preempt_reason': 'viewer_preempted',
                }
            return {
                'stream_id': kwargs['stream_id'],
                'stream_name': kwargs['stream_name'],
                'stream_url': kwargs['stream_url'],
                'status': 'OK',
            }

        results = scheduler.check_streams_with_limits(
            streams=[{'id': 1, 'name': 'Stream 1', 'url': 'http://test.com/1', 'm3u_account': 1}],
            check_function=mock_check,
            defer_callback=lambda _stream, reason: defer_reasons.append(reason),
            provider_wait_timeout=0.1,
        )

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]['status'], 'OK')
        self.assertEqual(
            checked_urls,
            ['http://test.com/1?profile=10', 'http://test.com/1?profile=11'],
        )
        self.assertIn('viewer_preempted', defer_reasons)

    def test_viewer_preempted_probe_waits_for_checker_sibling_profile(self):
        """If the sibling profile is checker-owned, the stream should wait and retry."""
        profiles = [
            _credential_profile(10, 'Credential 1'),
            _credential_profile(11, 'Credential 2'),
        ]

        class FakeUDI(_RealProfileRouteResolverMixin):
            account = {
                'id': 1,
                'name': 'Provider A',
                'profiles': profiles,
            }

            def __init__(self):
                self.active_by_profile = {10: 0, 11: 0}

            def get_active_streams_for_account(self, _account_id):
                return sum(self.active_by_profile.values())

            def get_active_streams_count_per_profile(self, _account_id):
                return dict(self.active_by_profile)

            def get_active_streams_for_profile(self, profile_id):
                return self.active_by_profile.get(profile_id, 0)

            def get_m3u_account_by_id(self, account_id):
                return self.account if account_id == 1 else None

            def get_stream_by_id(self, _stream_id):
                return {'stream_stats': {'resolution': '1920x1080', 'ffmpeg_output_bitrate': 5000}}

        fake_udi = FakeUDI()
        limiter = AccountStreamLimiter(udi_manager=fake_udi)
        limiter.set_account_limit(1, 0, profiles=fake_udi.account['profiles'])
        limiter.profile_checking_counts[11] = 1
        limiter.account_checking_counts[1] = 1
        scheduler = SmartStreamScheduler(limiter, global_limit=1)

        checked_urls = []
        defer_reasons = []

        def release_sibling_later():
            time.sleep(0.2)
            limiter.release_profile(fake_udi.account['profiles'][1])
            limiter.release(1)

        releaser = threading.Thread(target=release_sibling_later)
        releaser.start()

        def mock_check(**kwargs):
            checked_urls.append(kwargs['stream_url'])
            if kwargs['stream_url'].endswith('profile=10'):
                fake_udi.active_by_profile[10] = 1
                self.assertTrue(kwargs['preempt_check']())
                return {
                    'stream_id': kwargs['stream_id'],
                    'stream_name': kwargs['stream_name'],
                    'stream_url': kwargs['stream_url'],
                    'status': 'PREEMPTED',
                    'preempted': True,
                    'preempt_reason': 'viewer_preempted',
                }
            return {
                'stream_id': kwargs['stream_id'],
                'stream_name': kwargs['stream_name'],
                'stream_url': kwargs['stream_url'],
                'status': 'OK',
            }

        results = scheduler.check_streams_with_limits(
            streams=[{'id': 1, 'name': 'Stream 1', 'url': 'http://test.com/1', 'm3u_account': 1}],
            check_function=mock_check,
            defer_callback=lambda _stream, reason: defer_reasons.append(reason),
            provider_wait_timeout=0.05,
        )
        releaser.join()

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]['status'], 'OK')
        self.assertEqual(
            checked_urls,
            ['http://test.com/1?profile=10', 'http://test.com/1?profile=11'],
        )
        self.assertIn('viewer_preempted', defer_reasons)
        self.assertIn('checking_capacity', defer_reasons)

    def test_default_provider_wait_timeout_is_visible_config(self):
        self.assertEqual(
            StreamCheckConfig.DEFAULT_CONFIG['concurrent_streams']['provider_wait_timeout'],
            180,
        )
    
    def test_progress_callback(self):
        """Test that progress callback is called correctly."""
        self.limiter.set_account_limit(1, 2)
        
        progress_calls = []
        
        def progress_callback(completed, total, result):
            progress_calls.append((completed, total, result['stream_id']))
        
        def mock_check(**kwargs):
            return {'stream_id': kwargs['stream_id'], 'status': 'OK'}
        
        streams = [
            {'id': i, 'name': f'Stream {i}', 'url': f'http://test.com/{i}', 'm3u_account': 1}
            for i in range(3)
        ]
        
        results = self.scheduler.check_streams_with_limits(
            streams=streams,
            check_function=mock_check,
            progress_callback=progress_callback
        )
        
        self.assertEqual(len(results), 3)
        self.assertEqual(len(progress_calls), 3)
        
        # Verify all streams were reported
        reported_ids = [call[2] for call in progress_calls]
        self.assertEqual(sorted(reported_ids), [0, 1, 2])


class TestInitializeAccountLimits(unittest.TestCase):
    """Test cases for initialize_account_limits function."""
    
    def test_initialize_single_account(self):
        """Test initializing a single account."""
        limiter = get_account_limiter()
        limiter.clear()
        
        accounts = [
            {'id': 1, 'max_streams': 2}
        ]
        
        initialize_account_limits(accounts)
        
        self.assertEqual(limiter.get_account_limit(1), 2)
    
    def test_initialize_multiple_accounts(self):
        """Test initializing multiple accounts."""
        limiter = get_account_limiter()
        limiter.clear()
        
        accounts = [
            {'id': 1, 'max_streams': 1},
            {'id': 2, 'max_streams': 2},
            {'id': 3, 'max_streams': 0},  # Unlimited
        ]
        
        initialize_account_limits(accounts)
        
        self.assertEqual(limiter.get_account_limit(1), 1)
        self.assertEqual(limiter.get_account_limit(2), 2)
        self.assertEqual(limiter.get_account_limit(3), 0)
    
    def test_initialize_equivalent_default_profiles_do_not_multiply_capacity(self):
        """Default-route aliases share one route instead of multiplying slots."""
        limiter = get_account_limiter()
        limiter.clear()
        
        accounts = [
            {
                'id': 26,
                'name': 'DE-00',
                'max_streams': 1,
                'profiles': [
                    {'id': 38, 'name': 'D4 - 01', 'max_streams': 1, 'is_active': True},
                    {'id': 27, 'name': 'D4 - 00', 'max_streams': 1, 'is_active': True}
                ]
            }
        ]
        
        initialize_account_limits(accounts)
        
        self.assertEqual(limiter.get_account_limit(26), 1)
    
    def test_initialize_account_with_inactive_profile(self):
        """Test that inactive profiles are excluded from limit calculation."""
        limiter = get_account_limiter()
        limiter.clear()
        
        accounts = [
            {
                'id': 1,
                'name': 'Test Account',
                'max_streams': 0,
                'profiles': [
                    {'id': 1, 'name': 'Profile 1', 'max_streams': 2, 'is_active': True},
                    {'id': 2, 'name': 'Profile 2', 'max_streams': 3, 'is_active': False}  # Inactive
                ]
            }
        ]
        
        initialize_account_limits(accounts)
        
        # With an unlimited account fallback, only the active finite profile contributes.
        self.assertEqual(limiter.get_account_limit(1), 2)

    def test_empty_or_malformed_inventory_revokes_all_provider_authority_atomically(self):
        limiter = get_account_limiter()
        limiter.clear()
        self.assertTrue(initialize_account_limits([
            {'id': 1, 'max_streams': 1, 'profiles': []},
        ]))
        self.assertTrue(limiter.account_inventory_trusted)
        self.assertEqual(limiter.account_inventory_ids, {1})

        self.assertFalse(initialize_account_limits([
            None,
            {'id': 2, 'max_streams': 1, 'profiles': []},
        ]))
        self.assertFalse(limiter.account_inventory_trusted)
        self.assertEqual(limiter.account_inventory_ids, set())
        self.assertEqual(
            limiter.acquire(1, timeout=0)[0:2],
            (False, 'provider_profile_unavailable'),
        )
        self.assertEqual(
            limiter.acquire(2, timeout=0)[0:2],
            (False, 'provider_profile_unavailable'),
        )

        self.assertTrue(initialize_account_limits([]))
        self.assertTrue(limiter.account_inventory_trusted)
        self.assertEqual(limiter.account_inventory_ids, set())
        self.assertEqual(
            limiter.acquire(1, timeout=0)[0:2],
            (False, 'provider_profile_unavailable'),
        )

    def test_publication_exception_never_exposes_a_trusted_partial_inventory(self):
        """A later setter failure revokes the prefix before releasing the publish lock."""
        limiter = get_account_limiter()
        limiter.clear()

        original_set_account_limit = limiter.set_account_limit
        original_invalidate = limiter.invalidate_account_inventory
        invalidation_entered = threading.Event()
        allow_invalidation = threading.Event()
        setter_calls = 0
        result = []

        def failing_set_account_limit(account_id, max_streams, profiles=None):
            nonlocal setter_calls
            setter_calls += 1
            if setter_calls == 2:
                raise RuntimeError('synthetic publication failure')
            return original_set_account_limit(account_id, max_streams, profiles)

        def gated_invalidate():
            invalidation_entered.set()
            if not allow_invalidation.wait(timeout=5):
                raise AssertionError('test did not release gated invalidation')
            return original_invalidate()

        limiter.set_account_limit = failing_set_account_limit
        limiter.invalidate_account_inventory = gated_invalidate
        worker = threading.Thread(
            target=lambda: result.append(initialize_account_limits([
                {'id': 1, 'max_streams': 1, 'profiles': []},
                {'id': 2, 'max_streams': 1, 'profiles': []},
            ])),
            daemon=True,
        )

        try:
            worker.start()
            self.assertTrue(
                invalidation_entered.wait(timeout=5),
                'publication failure did not reach outer invalidation',
            )
            self.assertFalse(limiter.account_inventory_trusted)
            self.assertEqual(limiter.account_inventory_ids, set())
            self.assertEqual(
                limiter.acquire(1, timeout=0)[0:2],
                (False, 'provider_profile_unavailable'),
            )
        finally:
            allow_invalidation.set()
            worker.join(timeout=5)
            limiter.set_account_limit = original_set_account_limit
            limiter.invalidate_account_inventory = original_invalidate

        self.assertFalse(worker.is_alive())
        self.assertEqual(result, [False])

    def test_inventory_normalizes_numeric_ids_and_rejects_canonical_duplicates(self):
        limiter = get_account_limiter()
        limiter.clear()
        self.assertTrue(initialize_account_limits([{
            'id': '1',
            'max_streams': 1,
            'profiles': [{
                'id': '10',
                'max_streams': 1,
                'is_active': True,
                'is_default': True,
            }],
        }]))
        self.assertEqual(limiter.account_inventory_ids, {1})
        self.assertEqual(limiter.account_profile_ids[1], {10})

        for malformed_inventory in (
            [
                {'id': 1, 'max_streams': 1, 'profiles': []},
                {'id': '1', 'max_streams': 1, 'profiles': []},
            ],
            [{
                'id': 1,
                'max_streams': 1,
                'profiles': [
                    {'id': 10, 'max_streams': 1, 'is_active': True},
                    {'id': '10', 'max_streams': 1, 'is_active': False},
                ],
            }],
            [{'id': True, 'max_streams': 1, 'profiles': []}],
        ):
            with self.subTest(inventory=malformed_inventory):
                self.assertFalse(initialize_account_limits(malformed_inventory))
                self.assertFalse(limiter.account_inventory_trusted)
    
    def test_initialize_account_with_no_profiles(self):
        """Test account without profiles uses account-level limit."""
        limiter = get_account_limiter()
        limiter.clear()
        
        accounts = [
            {
                'id': 1,
                'name': 'Test Account',
                'max_streams': 5,
                'profiles': []  # No profiles
            }
        ]
        
        initialize_account_limits(accounts)
        
        # Should use account-level limit
        self.assertEqual(limiter.get_account_limit(1), 5)
    
    def test_initialize_distinct_profile_routes_override_account_fallback(self):
        """Usable profile routes, not a positive account fallback, define capacity."""
        limiter = get_account_limiter()
        limiter.clear()
        
        accounts = [
            {
                'id': 1,
                'name': 'Test Account',
                'max_streams': 1,
                'profiles': [
                    _credential_profile(1, 'Profile 1', max_streams=3),
                    _credential_profile(2, 'Profile 2', max_streams=2),
                ]
            }
        ]
        
        initialize_account_limits(accounts)
        
        self.assertEqual(limiter.get_account_limit(1), 5)

    def test_zero_or_unset_account_limit_uses_finite_active_profile_sum(self):
        """Finite profiles provide the aggregate fallback for an unlimited account."""
        limiter = get_account_limiter()

        for case_name, account in {
            'zero': {
                'id': 1,
                'max_streams': 0,
                'profiles': [
                    _credential_profile(10, max_streams=1),
                    _credential_profile(11, max_streams=2),
                ],
            },
            'unset': {
                'id': 2,
                'profiles': [
                    _credential_profile(20, max_streams=1),
                    _credential_profile(21, max_streams=2),
                ],
            },
        }.items():
            with self.subTest(account_limit=case_name):
                limiter.clear()
                initialize_account_limits([account])
                self.assertEqual(limiter.get_account_limit(account['id']), 3)

    def test_unlimited_active_profile_keeps_zero_account_fallback_unlimited(self):
        """A mixed finite/unlimited profile set has no finite aggregate fallback."""
        limiter = get_account_limiter()
        limiter.clear()

        initialize_account_limits([{
            'id': 1,
            'max_streams': 0,
            'profiles': [
                _credential_profile(10, max_streams=2),
                _credential_profile(11, max_streams=0),
            ],
        }])

        self.assertEqual(limiter.get_account_limit(1), 0)

    def test_unlimited_distinct_profile_route_overrides_positive_account_fallback(self):
        """An unlimited credential route makes the aggregate unlimited."""
        limiter = get_account_limiter()
        limiter.clear()

        initialize_account_limits([{
            'id': 1,
            'max_streams': 1,
            'profiles': [
                _credential_profile(10, max_streams=2),
                _credential_profile(11, max_streams=0),
            ],
        }])

        self.assertEqual(limiter.get_account_limit(1), 0)


class TestProfileAwareStreamChecking(unittest.TestCase):
    """Test cases for profile-aware stream checking via UDI."""

    def _seed_m3u_accounts(self, udi, accounts):
        """Seed the UDI singleton account cache and rebuild lookup indexes."""
        udi._m3u_accounts_cache = accounts
        udi._initialized = True
        udi._build_indexes()
    
    def test_find_available_profile_with_free_slots(self):
        """Test finding an available profile when one has free slots."""
        from apps.udi import get_udi_manager
        udi = get_udi_manager()
        
        # Mock the UDI data
        self._seed_m3u_accounts(udi, [
            {
                'id': 1,
                'name': 'Test Account',
                'profiles': [
                    {'id': 10, 'name': 'Profile 1', 'max_streams': 2, 'is_active': True},
                    {'id': 11, 'name': 'Profile 2', 'max_streams': 1, 'is_active': True}
                ]
            }
        ])
        
        # Mock profile usage (Profile 1 has 1/2 slots used, Profile 2 has 0/1)
        def mock_get_usage(account_id):
            if account_id == 1:
                return {10: 1, 11: 0}  # Profile 10 has 1 active, Profile 11 has 0
            return {}
        
        udi.get_active_streams_count_per_profile = mock_get_usage
        
        # Test stream with account 1
        stream = {'id': 100, 'm3u_account': 1, 'url': 'http://example.com/stream'}
        
        profile = udi.find_available_profile_for_stream(stream)
        
        # Should find Profile 1 (first available)
        self.assertIsNotNone(profile)
        self.assertEqual(profile['id'], 10)
    
    def test_find_available_profile_all_at_capacity(self):
        """Test that no profile is returned when all are at capacity."""
        from apps.udi import get_udi_manager
        udi = get_udi_manager()
        
        # Mock the UDI data
        self._seed_m3u_accounts(udi, [
            {
                'id': 1,
                'name': 'Test Account',
                'profiles': [
                    {'id': 10, 'name': 'Profile 1', 'max_streams': 1, 'is_active': True},
                    {'id': 11, 'name': 'Profile 2', 'max_streams': 1, 'is_active': True}
                ]
            }
        ])
        
        # Mock profile usage (both profiles at capacity)
        def mock_get_usage(account_id):
            if account_id == 1:
                return {10: 1, 11: 1}  # Both at 1/1
            return {}
        
        udi.get_active_streams_count_per_profile = mock_get_usage
        
        # Test stream with account 1
        stream = {'id': 100, 'm3u_account': 1, 'url': 'http://example.com/stream'}
        
        profile = udi.find_available_profile_for_stream(stream)
        
        # Should return None (all at capacity)
        self.assertIsNone(profile)
    
    def test_check_stream_can_run_with_available_profile(self):
        """Test stream can run check when profile is available."""
        from apps.udi import get_udi_manager
        udi = get_udi_manager()
        
        # Mock the UDI data
        self._seed_m3u_accounts(udi, [
            {
                'id': 1,
                'name': 'Test Account',
                'profiles': [
                    {'id': 10, 'name': 'Profile 1', 'max_streams': 2, 'is_active': True}
                ]
            }
        ])
        
        # Mock profile usage
        def mock_get_usage(account_id):
            return {10: 0}  # No active streams
        
        udi.get_active_streams_count_per_profile = mock_get_usage
        
        stream = {'id': 100, 'm3u_account': 1, 'url': 'http://example.com/stream'}
        
        can_run, reason = udi.check_stream_can_run(stream)
        
        # Should be able to run
        self.assertTrue(can_run)
        self.assertIsNone(reason)
    
    def test_check_stream_can_run_all_profiles_at_capacity(self):
        """Test stream cannot run when all profiles are at capacity."""
        from apps.udi import get_udi_manager
        udi = get_udi_manager()
        
        # Mock the UDI data
        self._seed_m3u_accounts(udi, [
            {
                'id': 1,
                'name': 'Test Account',
                'profiles': [
                    {'id': 10, 'name': 'Profile 1', 'max_streams': 1, 'is_active': True}
                ]
            }
        ])
        
        # Mock profile usage (at capacity)
        def mock_get_usage(account_id):
            return {10: 1}  # 1/1 active
        
        udi.get_active_streams_count_per_profile = mock_get_usage
        
        stream = {'id': 100, 'm3u_account': 1, 'url': 'http://example.com/stream'}
        
        can_run, reason = udi.check_stream_can_run(stream)
        
        # Should not be able to run
        self.assertFalse(can_run)
        self.assertIsNotNone(reason)
        self.assertIn('Test Account', reason)


if __name__ == '__main__':
    unittest.main()
