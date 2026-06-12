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
from unittest.mock import Mock, patch
import sys
import os

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from apps.stream.concurrent_stream_limiter import (
    AccountStreamLimiter,
    SmartStreamScheduler,
    get_account_limiter,
    get_smart_scheduler,
    initialize_account_limits
)
from apps.stream.stream_checker_components import StreamCheckConfig


class TestAccountStreamLimiter(unittest.TestCase):
    """Test cases for AccountStreamLimiter."""
    
    def setUp(self):
        """Set up test fixtures."""
        self.limiter = AccountStreamLimiter()
    
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


class TestSmartStreamScheduler(unittest.TestCase):
    """Test cases for SmartStreamScheduler."""
    
    def setUp(self):
        """Set up test fixtures."""
        self.limiter = AccountStreamLimiter()
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
        mock_udi.get_active_streams_for_account.return_value = 0
        mock_udi.get_stream_by_id.return_value = None
        mock_udi.check_stream_can_run.side_effect = (
            lambda stream: (False, 'active_viewers')
            if stream.get('m3u_account') == 1
            else (True, None)
        )
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
        mock_udi.get_active_streams_for_account.return_value = 0
        mock_udi.get_stream_by_id.return_value = None
        mock_udi.check_stream_can_run.side_effect = (
            lambda stream: (False, 'active_viewers')
            if stream.get('m3u_account') == 1
            else (True, None)
        )
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
        class FakeUDI:
            account = {
                'id': 1,
                'name': 'Provider A',
                'profiles': [
                    {'id': 10, 'name': 'Credential 1', 'max_streams': 1, 'is_active': True},
                    {'id': 11, 'name': 'Credential 2', 'max_streams': 1, 'is_active': True},
                ],
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
                profile_id = profile.get('id') if profile else 'none'
                return f"{stream['url']}?profile={profile_id}"

            def get_stream_by_id(self, _stream_id):
                return None

        limiter = AccountStreamLimiter(udi_manager=FakeUDI())
        limiter.set_account_limit(
            1,
            0,
            profiles=[
                {'id': 10, 'name': 'Credential 1', 'max_streams': 1, 'is_active': True},
                {'id': 11, 'name': 'Credential 2', 'max_streams': 1, 'is_active': True},
            ],
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

    def test_active_profile_does_not_block_free_sibling_profile(self):
        """A viewer on one credential should still leave a sibling credential usable."""
        class FakeUDI:
            account = {
                'id': 1,
                'name': 'Provider A',
                'profiles': [
                    {'id': 10, 'name': 'Busy Credential', 'max_streams': 1, 'is_active': True},
                    {'id': 11, 'name': 'Free Credential', 'max_streams': 1, 'is_active': True},
                ],
            }

            def get_active_streams_for_account(self, _account_id):
                return 1

            def get_active_streams_count_per_profile(self, _account_id):
                return {10: 1, 11: 0}

            def get_m3u_account_by_id(self, account_id):
                return self.account if account_id == 1 else None

            def check_stream_can_run(self, _stream):
                return (True, None)

            def apply_profile_url_transformation(self, stream, profile=None):
                profile_id = profile.get('id') if profile else 'none'
                return f"{stream['url']}?profile={profile_id}"

            def get_stream_by_id(self, _stream_id):
                return None

        limiter = AccountStreamLimiter(udi_manager=FakeUDI())
        limiter.set_account_limit(
            1,
            0,
            profiles=[
                {'id': 10, 'name': 'Busy Credential', 'max_streams': 1, 'is_active': True},
                {'id': 11, 'name': 'Free Credential', 'max_streams': 1, 'is_active': True},
            ],
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
        mock_udi.check_stream_can_run.return_value = (False, 'active_viewers')
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
        class FakeUDI:
            account = {
                'id': 1,
                'name': 'Provider A',
                'profiles': [
                    {'id': 10, 'name': 'Credential 1', 'max_streams': 1, 'is_active': True},
                    {'id': 11, 'name': 'Credential 2', 'max_streams': 1, 'is_active': True},
                ],
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

            def check_stream_can_run(self, _stream):
                return (True, None)

            def apply_profile_url_transformation(self, stream, profile=None):
                profile_id = profile.get('id') if profile else 'none'
                return f"{stream['url']}?profile={profile_id}"

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
        class FakeUDI:
            account = {
                'id': 1,
                'name': 'Provider A',
                'profiles': [
                    {'id': 10, 'name': 'Credential 1', 'max_streams': 1, 'is_active': True},
                    {'id': 11, 'name': 'Credential 2', 'max_streams': 1, 'is_active': True},
                ],
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

            def check_stream_can_run(self, _stream):
                return (True, None)

            def apply_profile_url_transformation(self, stream, profile=None):
                profile_id = profile.get('id') if profile else 'none'
                return f"{stream['url']}?profile={profile_id}"

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
    
    def test_initialize_account_with_profiles(self):
        """Test initializing account with multiple profiles - should sum profile limits."""
        limiter = get_account_limiter()
        limiter.clear()
        
        # Account DE-00 has max_streams=1 but has 2 active profiles with 1 stream each
        # Total should be 2 (sum of profile limits)
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
        
        # Should be 2 (sum of two profile limits), not 1 (account-level limit)
        self.assertEqual(limiter.get_account_limit(26), 2)
    
    def test_initialize_account_with_inactive_profile(self):
        """Test that inactive profiles are excluded from limit calculation."""
        limiter = get_account_limiter()
        limiter.clear()
        
        accounts = [
            {
                'id': 1,
                'name': 'Test Account',
                'max_streams': 1,
                'profiles': [
                    {'id': 1, 'name': 'Profile 1', 'max_streams': 2, 'is_active': True},
                    {'id': 2, 'name': 'Profile 2', 'max_streams': 3, 'is_active': False}  # Inactive
                ]
            }
        ]
        
        initialize_account_limits(accounts)
        
        # Should only count the active profile (2), not the inactive one (3)
        self.assertEqual(limiter.get_account_limit(1), 2)
    
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
    
    def test_initialize_account_profile_limit_higher_than_account(self):
        """Test that profile limit sum is used when higher than account limit."""
        limiter = get_account_limiter()
        limiter.clear()
        
        accounts = [
            {
                'id': 1,
                'name': 'Test Account',
                'max_streams': 1,  # Account says 1
                'profiles': [
                    {'id': 1, 'name': 'Profile 1', 'max_streams': 3, 'is_active': True},
                    {'id': 2, 'name': 'Profile 2', 'max_streams': 2, 'is_active': True}
                ]
                # Total profile limit = 5, which is > account limit of 1
            }
        ]
        
        initialize_account_limits(accounts)
        
        # Should use profile sum (5), not account limit (1)
        self.assertEqual(limiter.get_account_limit(1), 5)


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
