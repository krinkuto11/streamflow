#!/usr/bin/env python3
"""
Integration test for concurrent stream limiter with stream_checker_service.

This test validates the complete integration of the concurrent stream limiter
with the stream checking service.
"""

import unittest
import pytest
from unittest.mock import Mock, patch, MagicMock
import sys
import os
import json
import threading
import time
from copy import deepcopy
from datetime import datetime

pytestmark = pytest.mark.integration

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture(autouse=True)
def _restore_concurrent_limiter_singleton():
    """Keep the process singleton isolated across integration tests."""
    from apps.stream.concurrent_stream_limiter import get_account_limiter

    limiter = get_account_limiter()
    previous_udi_manager = limiter.udi_manager
    limiter.clear()
    try:
        yield
    finally:
        limiter.clear()
        limiter.udi_manager = previous_udi_manager


def _make_bitrate_runtime_udi(channel_id, streams, profiles=None):
    streams_by_id = {stream['id']: stream for stream in streams}
    udi = MagicMock()
    udi.fetcher = None
    udi.get_channel_by_id.return_value = {
        'id': channel_id,
        'name': 'Bitrate Runtime Channel',
        'channel_group_id': None,
        'logo_id': None,
        'streams': list(streams_by_id),
    }
    udi.get_channel_streams.return_value = streams
    udi.get_streams.return_value = streams
    udi.get_stream_count.return_value = len(streams)
    udi.get_stream_by_id.side_effect = streams_by_id.get
    account = {
        'id': 1,
        'name': 'Provider A',
        'is_active': True,
        'max_streams': 1,
        'profiles': list(profiles or []),
    }
    udi.get_m3u_accounts.return_value = [account]
    udi.get_m3u_account_by_id.return_value = account
    udi.get_valid_stream_ids.return_value = set(streams_by_id)
    udi.check_stream_can_run.return_value = (True, None)
    udi.get_active_streams_for_account.return_value = 0
    udi.get_active_stream_context_per_profile.return_value = {}
    udi.get_active_streams_count_per_profile.return_value = {}
    udi.get_active_stream_ids_for_channel.return_value = set()
    udi.is_channel_active.return_value = False
    udi.get_playing_stream_ids.return_value = set()
    udi.apply_profile_url_transformation.side_effect = (
        lambda stream, profile=None: stream.get('url', '')
    )
    udi.refresh_channel_by_id.return_value = True
    return udi


def _configure_bitrate_runtime_service(service, tracker_updates):
    service.config.config.setdefault('concurrent_streams', {}).update({
        'enabled': True,
        'global_limit': 1,
        'stagger_delay': 0,
        'provider_wait_timeout': 1,
    })
    service.config.config.setdefault('batch_operations', {}).update({
        'enabled': True,
        'batch_size': 10,
        'verify_updates': False,
    })
    service.config.config['scoring'] = {
        'weights': {
            'bitrate': 1.0,
            'resolution': 0.0,
            'fps': 0.0,
            'codec': 0.0,
            'hdr': 0.0,
        },
        'min_score': 0.0,
        'prefer_h265': True,
    }
    tracker = Mock()
    tracker.updates = tracker_updates
    service.update_tracker = tracker
    service.check_queue.mark_completed = Mock(return_value=True)
    service.check_queue.mark_failed = Mock()
    dead_tracker = Mock()
    dead_tracker.is_dead.return_value = False
    service.dead_streams_tracker = dead_tracker
    service.changelog = None
    service._apply_channel_visibility_after_check = Mock(return_value={
        'action': 'unchanged',
        'changed': False,
    })
    service.abort_current_check.clear()
    return tracker


@pytest.mark.parametrize(
    ('case_name', 'inventory'),
    (
        ('malformed', {'id': 1}),
        ('rejected_malformed', [{'id': 'invalid', 'max_streams': 1}]),
    ),
)
def test_concurrent_channel_inventory_fail_closed_before_scheduler(
    case_name,
    inventory,
):
    from apps.stream.stream_checker_service import StreamCheckerService

    channel_id = 109
    stream = {
        'id': 1901,
        'name': 'Inventory-guarded channel stream',
        'url': 'http://provider.invalid/live/1901.ts',
        'm3u_account': 1,
        'stream_stats': {},
    }
    udi = _make_bitrate_runtime_udi(channel_id, [stream])
    events = []
    udi.get_m3u_accounts.side_effect = (
        lambda value=inventory: events.append('fetch') or value
    )
    automation_config = MagicMock()
    automation_config.get_profile.return_value = None
    automation_config.get_effective_configuration.return_value = {
        'profile': None,
        'periods': [],
    }
    limiter = Mock()
    limiter.invalidate_account_inventory.side_effect = (
        lambda: events.append('invalidate')
    )
    scheduler = Mock()
    service = StreamCheckerService()
    _configure_bitrate_runtime_service(
        service,
        {'channels': {}, 'last_global_check': None},
    )
    service._check_channel_limits = Mock(return_value=None)
    service.progress.update = Mock(return_value=True)

    with (
        patch(
            'apps.stream.stream_checker_service.get_udi_manager',
            return_value=udi,
        ),
        patch(
            'apps.stream.stream_checker_service.get_automation_config_manager',
            return_value=automation_config,
        ),
        patch(
            'apps.stream.stream_checker_service.fetch_channel_streams',
            return_value=[stream],
        ),
        patch(
            'apps.stream.stream_checker_service.analyze_stream',
        ) as analyzer,
        patch('apps.stream.stream_checker_service.update_channel_streams'),
        patch(
            'apps.stream.stream_checker_service._get_base_url',
            return_value='http://localhost:9191',
        ),
        patch(
            'apps.stream.concurrent_stream_limiter.get_account_limiter',
            return_value=limiter,
        ),
        patch(
            'apps.stream.concurrent_stream_limiter.initialize_account_limits',
            side_effect=lambda accounts: (
                events.append('initialize')
                or (False if case_name == 'rejected_malformed' else None)
            ),
        ) as initialize_limits,
        patch(
            'apps.stream.concurrent_stream_limiter.get_smart_scheduler',
            return_value=scheduler,
        ) as get_scheduler,
    ):
        result = service._check_channel_concurrent(
            channel_id,
            force_check_override=False,
        )

    assert result['error'] == (
        'Provider account inventory unavailable for channel probes'
    )
    assert events[0] == 'invalidate'
    limiter.invalidate_account_inventory.assert_called_once_with()
    get_scheduler.assert_not_called()
    scheduler.check_streams_with_limits.assert_not_called()
    analyzer.assert_not_called()
    limiter.acquire.assert_not_called()
    limiter.reserve_profile_for_stream_with_url.assert_not_called()
    limiter.release.assert_not_called()
    limiter.release_profile.assert_not_called()
    if case_name == 'rejected_malformed':
        assert events == ['invalidate', 'fetch', 'initialize']
        initialize_limits.assert_called_once_with(inventory)
    else:
        assert events == ['invalidate', 'fetch']
        initialize_limits.assert_not_called()


@pytest.mark.parametrize(
    ('is_custom', 'should_analyze'),
    (
        (False, False),
        (True, True),
    ),
)
def test_concurrent_channel_empty_inventory_allows_only_custom_streams(
    is_custom,
    should_analyze,
):
    from apps.stream.concurrent_stream_limiter import get_account_limiter
    from apps.stream.stream_checker_service import StreamCheckerService

    channel_id = 110
    stream = {
        'id': 1902,
        'name': 'Empty-inventory channel stream',
        'url': 'http://stream.invalid/live/1902.ts',
        'stream_stats': {},
    }
    if is_custom:
        stream['is_custom'] = True
    else:
        stream['m3u_account'] = 1
    udi = _make_bitrate_runtime_udi(channel_id, [stream])
    udi.get_m3u_accounts.return_value = []
    automation_config = MagicMock()
    automation_config.get_profile.return_value = None
    automation_config.get_effective_configuration.return_value = {
        'profile': None,
        'periods': [],
    }
    service = StreamCheckerService()
    _configure_bitrate_runtime_service(
        service,
        {'channels': {}, 'last_global_check': None},
    )
    service._check_channel_limits = Mock(return_value=None)
    service.progress.update = Mock(return_value=True)
    analysis = {
        'stream_id': stream['id'],
        'stream_name': stream['name'],
        'stream_url': stream['url'],
        'status': 'OK',
        'resolution': '1920x1080',
        'bitrate_kbps': 4000,
        'fps': 25,
        'video_codec': 'h264',
        'audio_codec': 'aac',
    }

    with (
        patch(
            'apps.stream.stream_checker_service.get_udi_manager',
            return_value=udi,
        ),
        patch(
            'apps.stream.stream_checker_service.get_automation_config_manager',
            return_value=automation_config,
        ),
        patch(
            'apps.stream.stream_checker_service.fetch_channel_streams',
            return_value=[stream],
        ),
        patch(
            'apps.stream.stream_checker_service.analyze_stream',
            return_value=analysis,
        ) as analyzer,
        patch(
            'apps.stream.stream_checker_service.batch_update_stream_stats',
            return_value=(1, 0),
        ),
        patch('apps.stream.stream_checker_service.update_channel_streams'),
        patch(
            'apps.stream.stream_checker_service._get_base_url',
            return_value='http://localhost:9191',
        ),
    ):
        result = service._check_channel_concurrent(
            channel_id,
            force_check_override=False,
        )

    assert 'error' not in result
    if should_analyze:
        analyzer.assert_called_once()
        assert result['checked_streams'][0]['status'] == 'completed'
    else:
        analyzer.assert_not_called()
        # A newly assigned provider stream without authoritative account
        # inventory is excluded rather than promoted from a synthetic result.
        assert result['checked_streams'] == []
    limiter = get_account_limiter()
    assert limiter.account_inventory_trusted is True
    assert limiter.account_inventory_ids == set()
    assert limiter.account_checking_counts == {}
    assert limiter.profile_checking_counts == {}
    assert limiter.profile_reservations_by_token == {}


class TestConcurrentLimiterIntegration(unittest.TestCase):
    """Integration test for concurrent stream limiter."""
    
    @patch('stream_checker_service.get_udi_manager')
    @patch('stream_checker_service.fetch_channel_streams')
    @patch('stream_checker_service.analyze_stream')
    @patch('stream_checker_service.update_channel_streams')
    @patch('stream_checker_service._get_base_url')
    def test_check_channel_respects_account_limits(
        self, 
        mock_base_url,
        mock_update_streams,
        mock_analyze,
        mock_fetch_streams,
        mock_udi
    ):
        """Test that _check_channel_concurrent respects account limits."""
        
        # Setup mocks
        mock_base_url.return_value = 'http://localhost:9191'
        
        # Mock UDI manager
        udi_mock = MagicMock()
        udi_mock.get_channel_by_id.return_value = {
            'id': 1,
            'name': 'Test Channel',
            'streams': [1, 2, 3, 4, 5]
        }
        
        # Mock M3U accounts with different limits
        accounts = [
            {'id': 1, 'name': 'Account A', 'max_streams': 1, 'profiles': []},  # Max 1 concurrent
            {'id': 2, 'name': 'Account B', 'max_streams': 2, 'profiles': []},  # Max 2 concurrent
        ]
        udi_mock.get_m3u_accounts.return_value = accounts
        accounts_by_id = {account['id']: account for account in accounts}
        udi_mock.get_m3u_account_by_id.side_effect = accounts_by_id.get
        udi_mock.get_active_streams_for_account.return_value = 0
        udi_mock.get_active_stream_context_per_profile.return_value = {}
        udi_mock.get_active_streams_count_per_profile.return_value = {}
        
        udi_mock.get_stream_by_id.return_value = None  # No cached data
        udi_mock.refresh_channel_by_id.return_value = True
        
        mock_udi.return_value = udi_mock
        
        # Mock streams from different accounts
        mock_fetch_streams.return_value = [
            {'id': 1, 'name': 'Stream A1', 'url': 'http://a.com/1', 'm3u_account': 1},
            {'id': 2, 'name': 'Stream A2', 'url': 'http://a.com/2', 'm3u_account': 1},
            {'id': 3, 'name': 'Stream B1', 'url': 'http://b.com/1', 'm3u_account': 2},
            {'id': 4, 'name': 'Stream B2', 'url': 'http://b.com/2', 'm3u_account': 2},
            {'id': 5, 'name': 'Stream B3', 'url': 'http://b.com/3', 'm3u_account': 2},
        ]
        
        # Mock stream analysis
        def analyze_side_effect(**kwargs):
            return {
                'stream_id': kwargs['stream_id'],
                'stream_name': kwargs['stream_name'],
                'stream_url': kwargs['stream_url'],
                'resolution': '1920x1080',
                'fps': 30,
                'bitrate_kbps': 5000,
                'video_codec': 'h264',
                'audio_codec': 'aac',
                'status': 'OK'
            }
        
        mock_analyze.side_effect = analyze_side_effect
        
        # Create service and check channel
        from apps.stream.stream_checker_service import StreamCheckerService
        from apps.stream.concurrent_stream_limiter import get_account_limiter
        service = StreamCheckerService()
        # The limiter is a process singleton and owns its own UDI reference.
        # Point it at the same authoritative no-profile account snapshot instead
        # of relying on the former fail-open behavior for an unknown account.
        get_account_limiter().udi_manager = udi_mock
        
        # Enable concurrent checking
        service.config.config['concurrent_streams'] = {
            'enabled': True,
            'global_limit': 10,
            'stagger_delay': 0.0
        }
        
        # Clear any existing tracking
        service.update_tracker.updates = {'channels': {}, 'last_global_check': None}
        
        # Check the channel
        service._check_channel_concurrent(1)
        
        # Verify analyze_stream was called for all streams
        self.assertEqual(mock_analyze.call_count, 5)
        
        # Verify update_channel_streams was called
        self.assertEqual(mock_update_streams.call_count, 1)
        
        # Verify channel was marked as checked
        channel_info = service.update_tracker.updates['channels'].get('1')
        self.assertIsNotNone(channel_info)
        self.assertFalse(channel_info.get('needs_check', True))
    
    def test_initialization_loads_account_limits(self):
        """Test that account limits are properly initialized from UDI."""
        from apps.stream.concurrent_stream_limiter import get_account_limiter, initialize_account_limits
        
        limiter = get_account_limiter()
        limiter.clear()
        
        # Mock accounts
        accounts = [
            {'id': 1, 'name': 'Account A', 'max_streams': 1},
            {'id': 2, 'name': 'Account B', 'max_streams': 2},
            {'id': 3, 'name': 'Account C', 'max_streams': 0},  # Unlimited
        ]
        
        initialize_account_limits(accounts)
        limiter.udi_manager = Mock()
        limiter.udi_manager.get_active_streams_for_account.return_value = 0

        # Verify limits were set
        self.assertEqual(limiter.get_account_limit(1), 1)
        self.assertEqual(limiter.get_account_limit(2), 2)
        self.assertEqual(limiter.get_account_limit(3), 0)
        
        # Verify semaphores work correctly
        # Account 1: max 1
        self.assertTrue(limiter.acquire(1, timeout=0.1))
        self.assertFalse(limiter.acquire(1, timeout=0.1))
        limiter.release(1)
        
        # Account 2: max 2
        self.assertTrue(limiter.acquire(2, timeout=0.1))
        self.assertTrue(limiter.acquire(2, timeout=0.1))
        self.assertFalse(limiter.acquire(2, timeout=0.1))
        limiter.release(2)
        limiter.release(2)
        
        # Account 3: unlimited
        for _ in range(10):
            self.assertTrue(limiter.acquire(3, timeout=0.1))


def test_viewer_preemption_clears_released_profile_before_sibling_retry():
    from apps.stream.stream_checker_service import StreamCheckerService
    from apps.stream.concurrent_stream_limiter import get_account_limiter

    channel_id = 100
    stream = {
        'id': 901,
        'name': 'Viewer preemption retry',
        'url': 'http://provider.invalid/live/main-user/main-pass/901.ts',
        'm3u_account': 1,
        'stream_stats': {},
    }
    profiles = [
        {
            'id': 201,
            'name': 'Provider A profile',
            'is_active': True,
            'is_default': True,
            'max_streams': 1,
            'search_pattern': 'main-user/main-pass',
            'replace_pattern': 'profile-a-user/profile-a-pass',
        },
        {
            'id': 202,
            'name': 'Provider B profile',
            'is_active': True,
            'is_default': False,
            'max_streams': 1,
            'search_pattern': 'main-user/main-pass',
            'replace_pattern': 'profile-b-user/profile-b-pass',
        },
    ]
    profile_urls = {
        201: 'http://provider.invalid/live/profile-a-user/profile-a-pass/901.ts',
        202: 'http://provider.invalid/live/profile-b-user/profile-b-pass/901.ts',
    }
    udi = _make_bitrate_runtime_udi(channel_id, [stream], profiles=profiles)
    udi.apply_profile_url_transformation.side_effect = (
        lambda _stream, profile=None: profile_urls[profile['id']]
    )
    active_usage = {}
    udi.get_active_stream_context_per_profile.side_effect = (
        lambda _account_id: deepcopy(active_usage)
    )
    automation_config = MagicMock()
    automation_config.get_profile.return_value = None
    automation_config.get_effective_configuration.return_value = {
        'profile': None,
        'periods': [],
    }
    analyzed_urls = []

    def analyze_with_viewer_preemption(**kwargs):
        analyzed_urls.append(kwargs['stream_url'])
        if len(analyzed_urls) == 1:
            assert kwargs['preempt_check']() is False
            active_usage[201] = {
                'active_streams': 1,
                'real_viewers': 1,
                'real_viewer_streams': 1,
                'shadow_watchers': 0,
            }
            assert kwargs['preempt_check']() is True
            return {
                'stream_id': stream['id'],
                'stream_name': stream['name'],
                'stream_url': kwargs['stream_url'],
                'status': 'PREEMPTED',
                'preempted': True,
                'preempt_reason': 'viewer_preempted',
            }
        return {
            'stream_id': stream['id'],
            'stream_name': stream['name'],
            'stream_url': kwargs['stream_url'],
            'status': 'OK',
            'resolution': '1920x1080',
            'fps': 30,
            'video_codec': 'h264',
            'audio_codec': 'aac',
            'bitrate_kbps': 5000,
            'blank_probe_ran': True,
            'blank_detected': False,
            'freeze_probe_ran': True,
            'freeze_detected': False,
        }

    progress_updates = []
    with (
        patch('apps.stream.stream_checker_service.get_udi_manager', return_value=udi),
        patch(
            'apps.stream.stream_checker_service.get_automation_config_manager',
            return_value=automation_config,
        ),
        patch(
            'apps.stream.stream_checker_service.fetch_channel_streams',
            return_value=[stream],
        ),
        patch(
            'apps.stream.stream_checker_service.analyze_stream',
            side_effect=analyze_with_viewer_preemption,
        ) as mock_analyze,
        patch('apps.stream.stream_checker_service.update_channel_streams'),
        patch(
            'apps.stream.stream_checker_service.batch_update_stream_stats',
            return_value=(1, 0),
        ),
        patch(
            'apps.stream.stream_checker_service._get_base_url',
            return_value='http://localhost:9191',
        ),
    ):
        limiter = get_account_limiter()
        limiter.clear()
        limiter.udi_manager = udi
        try:
            service = StreamCheckerService()
            _configure_bitrate_runtime_service(
                service,
                {'channels': {}, 'last_global_check': None},
            )
            service.progress.update = (
                lambda **kwargs: progress_updates.append(deepcopy(kwargs))
            )
            result = service._check_channel_concurrent(
                channel_id,
                force_check_override=False,
            )
        finally:
            limiter.clear()

    assert 'error' not in result
    assert mock_analyze.call_count == 2
    assert analyzed_urls == [profile_urls[201], profile_urls[202]]

    start_updates = [
        update
        for update in progress_updates
        if update.get('step_detail') == f"Started checking {stream['name']}"
    ]
    assert len(start_updates) == 2
    assert start_updates[0]['streams_detail'][0]['reserved_profile_id'] == 201
    assert start_updates[1]['streams_detail'][0]['reserved_profile_id'] == 202
    expected_reserved_fields = {
        'reserved_profile_id',
        'reserved_profile_name',
        'reserved_profile_limit',
    }
    for update in start_updates:
        row = update['streams_detail'][0]
        assert {
            key for key in row if key.startswith('reserved_profile_')
        } == expected_reserved_fields

    waiting_update = next(
        update
        for update in progress_updates
        if update.get('step_detail') == f"Waiting for provider capacity: {stream['name']}"
    )
    waiting_row = waiting_update['streams_detail'][0]
    assert waiting_row['status'] == 'waiting_provider_limit'
    assert waiting_row['reason_detail'] == 'viewer_preempted'
    assert 'reserved_profile_id' not in waiting_row
    assert 'reserved_profile_name' not in waiting_row
    assert 'reserved_profile_limit' not in waiting_row
    assert progress_updates.index(start_updates[0]) < progress_updates.index(waiting_update)
    assert progress_updates.index(waiting_update) < progress_updates.index(start_updates[1])

    completed_update = next(
        update
        for update in progress_updates
        if update.get('step_detail') == 'Completed 1/1'
    )
    completed_row = completed_update['streams_detail'][0]
    assert completed_row['status'] == 'completed'
    assert completed_row['reserved_profile_id'] == 202
    assert completed_row['reserved_profile_name'] == 'Provider B profile'
    assert completed_row['reserved_profile_limit'] == 1
    assert {
        key for key in completed_row if key.startswith('reserved_profile_')
    } == expected_reserved_fields

    assert progress_updates
    assert all(
        update.get('expected_generation') == 0
        for update in progress_updates
    )

    serialized_progress = json.dumps(progress_updates, sort_keys=True)
    for probe_url in (stream['url'], *profile_urls.values()):
        assert probe_url not in serialized_progress
    for credential_fragment in (
        'main-user',
        'main-pass',
        'profile-a-user',
        'profile-a-pass',
        'profile-b-user',
        'profile-b-pass',
    ):
        assert credential_fragment not in serialized_progress


def test_heartbeat_snapshot_is_atomic_and_reports_effective_shared_route_limit():
    import apps.stream.concurrent_stream_limiter as limiter_module
    import apps.stream.stream_checker_service as service_module
    from apps.stream.concurrent_stream_limiter import get_account_limiter
    from apps.stream.stream_checker_service import StreamCheckerService

    channel_id = 102
    stream = {
        'id': 1101,
        'name': 'Atomic shared-route probe',
        'url': 'http://provider.invalid/live/main-user/main-pass/1101.ts',
        'm3u_account': 1,
        'stream_stats': {},
    }
    profiles = [
        {
            'id': 301,
            'name': 'Wide alias',
            'is_active': True,
            'is_default': False,
            'max_streams': 5,
            'search_pattern': 'main-user/main-pass',
            'replace_pattern': 'shared-user/shared-pass',
        },
        {
            'id': 302,
            'name': 'Strict alias',
            'is_active': True,
            'is_default': False,
            'max_streams': 2,
            'search_pattern': 'main-user/main-pass',
            'replace_pattern': 'shared-user/shared-pass',
        },
    ]
    resolved_url = (
        'http://provider.invalid/live/shared-user/shared-pass/1101.ts'
    )
    udi = _make_bitrate_runtime_udi(channel_id, [stream], profiles=profiles)
    udi.apply_profile_url_transformation.side_effect = None
    udi.apply_profile_url_transformation.return_value = resolved_url
    automation_config = MagicMock()
    automation_config.get_profile.return_value = None
    automation_config.get_effective_configuration.return_value = {
        'profile': None,
        'periods': [],
    }

    transition_blocked = threading.Event()
    release_transition = threading.Event()
    heartbeat_seen = threading.Event()
    reserve_boundary_open = threading.Event()
    release_boundary_open = threading.Event()
    heartbeat_attempted_during_reserve = threading.Event()
    heartbeat_attempted_during_release = threading.Event()
    heartbeat_boundary_locks = {'reserve': None, 'release': None}
    original_reserved_profile = limiter_module.ReservedProfile
    original_rlock = threading.RLock

    class ObservableRLock:
        """Expose a heartbeat lock attempt without weakening the real lock."""

        def __init__(self, *args, **kwargs):
            self._lock = original_rlock(*args, **kwargs)
            self._owner_lock = original_rlock()
            self.owner_thread_id = None
            self.depth = 0

        def acquire(self, *args, **kwargs):
            if threading.current_thread().name == 'stream-checker-heartbeat':
                if reserve_boundary_open.is_set():
                    heartbeat_boundary_locks['reserve'] = self
                    heartbeat_attempted_during_reserve.set()
                if release_boundary_open.is_set():
                    heartbeat_boundary_locks['release'] = self
                    heartbeat_attempted_during_release.set()
            acquired = self._lock.acquire(*args, **kwargs)
            if acquired:
                thread_id = threading.get_ident()
                with self._owner_lock:
                    if self.owner_thread_id == thread_id:
                        self.depth += 1
                    else:
                        assert self.owner_thread_id is None
                        assert self.depth == 0
                        self.owner_thread_id = thread_id
                        self.depth = 1
            return acquired

        def release(self):
            thread_id = threading.get_ident()
            with self._owner_lock:
                assert self.owner_thread_id == thread_id
                assert self.depth > 0
                self.depth -= 1
                if self.depth == 0:
                    self.owner_thread_id = None
            return self._lock.release()

        def held_by_current_thread(self):
            with self._owner_lock:
                return (
                    self.owner_thread_id == threading.get_ident()
                    and self.depth > 0
                )

        def __enter__(self):
            self.acquire()
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            self.release()

        def __getattr__(self, name):
            return getattr(self._lock, name)

    class BlockingReservedProfile(original_reserved_profile):
        """Pause after the safe id is written but before name/limit are written."""

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._name_blocked = False

        def get(self, key, default=None):
            if key == 'name' and not self._name_blocked:
                self._name_blocked = True
                transition_blocked.set()
                if not release_transition.wait(timeout=2):
                    raise RuntimeError('timed out waiting to release profile transition')
            return super().get(key, default)

    progress_updates = []

    def record_progress(**kwargs):
        progress_updates.append(deepcopy(kwargs))
        if kwargs.get('step_detail') == 'Checking streams...':
            heartbeat_seen.set()

    def analyze_after_heartbeat(**kwargs):
        assert kwargs['stream_url'] == resolved_url
        assert heartbeat_seen.wait(timeout=2), 'heartbeat did not publish during probe'
        return {
            'stream_id': stream['id'],
            'stream_name': stream['name'],
            'stream_url': kwargs['stream_url'],
            'status': 'OK',
            'resolution': '1920x1080',
            'fps': 30,
            'video_codec': 'h264',
            'audio_codec': 'aac',
            'bitrate_kbps': 5000,
            'blank_probe_ran': True,
            'blank_detected': False,
            'freeze_probe_ran': True,
            'freeze_detected': False,
        }

    def release_after_heartbeat_attempt():
        if transition_blocked.wait(timeout=2):
            time.sleep(0.05)
            release_transition.set()

    release_thread = threading.Thread(
        target=release_after_heartbeat_attempt,
        daemon=True,
    )
    release_thread.start()

    with (
        patch('apps.stream.stream_checker_service.get_udi_manager', return_value=udi),
        patch(
            'apps.stream.stream_checker_service.get_automation_config_manager',
            return_value=automation_config,
        ),
        patch(
            'apps.stream.stream_checker_service.fetch_channel_streams',
            return_value=[stream],
        ),
        patch(
            'apps.stream.stream_checker_service.analyze_stream',
            side_effect=analyze_after_heartbeat,
        ),
        patch('apps.stream.stream_checker_service.update_channel_streams'),
        patch(
            'apps.stream.stream_checker_service.batch_update_stream_stats',
            return_value=(1, 0),
        ),
        patch(
            'apps.stream.stream_checker_service._get_base_url',
            return_value='http://localhost:9191',
        ),
        patch.object(limiter_module, 'ReservedProfile', BlockingReservedProfile),
        patch.object(service_module.threading, 'RLock', ObservableRLock),
        patch.object(
            service_module,
            '_STREAM_STATUS_HEARTBEAT_INTERVAL_SECONDS',
            0.01,
        ),
    ):
        limiter = get_account_limiter()
        limiter.udi_manager = udi
        original_reserve = limiter.reserve_profile_for_stream_with_url
        original_release = limiter.release_profile

        def reserve_with_heartbeat_gap(*args, **kwargs):
            reservation = original_reserve(*args, **kwargs)
            if reservation[0]:
                reserve_boundary_open.set()
                assert heartbeat_attempted_during_reserve.wait(timeout=2), (
                    'heartbeat did not attempt the reserve-to-start boundary'
                )
                boundary_lock = heartbeat_boundary_locks['reserve']
                assert isinstance(boundary_lock, ObservableRLock)
                assert boundary_lock.held_by_current_thread()
                assert boundary_lock.owner_thread_id == threading.get_ident()
                assert boundary_lock.depth > 0
                reserve_boundary_open.clear()
            return reservation

        def release_with_heartbeat_gap(reserved_profile):
            original_release(reserved_profile)
            if reserved_profile:
                release_boundary_open.set()
                assert heartbeat_attempted_during_release.wait(timeout=2), (
                    'heartbeat did not attempt the release-to-terminal boundary'
                )
                boundary_lock = heartbeat_boundary_locks['release']
                assert isinstance(boundary_lock, ObservableRLock)
                assert boundary_lock.held_by_current_thread()
                assert boundary_lock.owner_thread_id == threading.get_ident()
                assert boundary_lock.depth > 0
                release_boundary_open.clear()

        with (
            patch.object(
                limiter,
                'reserve_profile_for_stream_with_url',
                side_effect=reserve_with_heartbeat_gap,
            ),
            patch.object(
                limiter,
                'release_profile',
                side_effect=release_with_heartbeat_gap,
            ),
        ):
            service = StreamCheckerService()
            _configure_bitrate_runtime_service(
                service,
                {'channels': {}, 'last_global_check': None},
            )
            service.progress.update = record_progress
            result = service._check_channel_concurrent(
                channel_id,
                force_check_override=False,
            )

    release_thread.join(timeout=2)
    assert not release_thread.is_alive()
    assert 'error' not in result
    assert transition_blocked.is_set()
    assert heartbeat_seen.is_set()
    assert heartbeat_attempted_during_reserve.is_set()
    assert heartbeat_attempted_during_release.is_set()
    assert heartbeat_boundary_locks['reserve'] is heartbeat_boundary_locks['release']

    heartbeat_updates = [
        update
        for update in progress_updates
        if update.get('step_detail') == 'Checking streams...'
    ]
    assert heartbeat_updates
    reserved_fields = {
        'reserved_profile_id',
        'reserved_profile_name',
        'reserved_profile_limit',
    }
    for update in progress_updates:
        for row in update.get('streams_detail') or []:
            present_fields = {
                key for key in row if key.startswith('reserved_profile_')
            }
            assert present_fields in (set(), reserved_fields)
            if row.get('status') == 'waiting_provider_limit':
                assert present_fields == set()

    heartbeat_row = heartbeat_updates[0]['streams_detail'][0]
    assert heartbeat_row['status'] == 'checking'
    assert heartbeat_row['reserved_profile_id'] == 301
    assert heartbeat_row['reserved_profile_name'] == 'Wide alias'
    # Profile 301 advertises five slots, but its shared credential route is
    # constrained by sibling alias 302 to two. Telemetry must match enforcement.
    assert heartbeat_row['reserved_profile_limit'] == 2

    slot_rows = heartbeat_updates[0]['provider_profile_slots']['1']
    assert {row['id']: row['limit'] for row in slot_rows} == {301: 2, 302: 2}

    capacity_updates = [
        update
        for update in progress_updates
        if update.get('streams_detail')
        and update.get('provider_profile_slots', {}).get('1')
    ]
    assert any(
        update['streams_detail'][0]['status'] == 'completed'
        for update in capacity_updates
    )
    for update in capacity_updates:
        stream_status = update['streams_detail'][0]['status']
        capacity_counted_checking = sum(
            int(slot.get('checking') or 0)
            for slot in update['provider_profile_slots']['1']
            if slot.get('capacity_counted') is True
        )
        expected_checking = 1 if stream_status == 'checking' else 0
        assert capacity_counted_checking == expected_checking, update

    serialized_progress = json.dumps(progress_updates, sort_keys=True)
    for secret_fragment in (
        stream['url'],
        resolved_url,
        'main-user',
        'main-pass',
        'shared-user',
        'shared-pass',
    ):
        assert secret_fragment not in serialized_progress


@pytest.mark.parametrize('failure_stage', ('classification', 'publish'))
def test_progress_failure_republishes_new_non_active_revision(failure_stage):
    from apps.stream.concurrent_stream_limiter import get_account_limiter
    from apps.stream.stream_checker_service import StreamCheckerService

    channel_id = 113
    stream = {
        'id': 2201,
        'name': f'{failure_stage} progress failure',
        'url': 'http://provider.invalid/live/2201.ts',
        'm3u_account': 1,
        'stream_stats': {},
    }
    analysis_result = {
        'stream_id': stream['id'],
        'stream_name': stream['name'],
        'stream_url': stream['url'],
        'status': 'OK',
        'resolution': '1920x1080',
        'fps': 30,
        'video_codec': 'h264',
        'audio_codec': 'aac',
        'bitrate_kbps': 5000,
        'blank_probe_ran': True,
        'blank_detected': False,
        'freeze_probe_ran': True,
        'freeze_detected': False,
    }
    udi = _make_bitrate_runtime_udi(channel_id, [stream])
    automation_config = MagicMock()
    automation_config.get_profile.return_value = None
    automation_config.get_effective_configuration.return_value = {
        'profile': None,
        'periods': [],
    }
    publish_attempts = []
    published_updates = []
    failed_terminal_publish = {'value': False}

    def record_progress(**kwargs):
        snapshot = deepcopy(kwargs)
        publish_attempts.append(snapshot)
        if (
            failure_stage == 'publish'
            and kwargs.get('step_detail') == 'Completed 1/1'
            and not failed_terminal_publish['value']
        ):
            failed_terminal_publish['value'] = True
            raise RuntimeError('synthetic terminal progress publish failure')
        published_updates.append(snapshot)
        return True

    with (
        patch('apps.stream.stream_checker_service.get_udi_manager', return_value=udi),
        patch(
            'apps.stream.stream_checker_service.get_automation_config_manager',
            return_value=automation_config,
        ),
        patch(
            'apps.stream.stream_checker_service.fetch_channel_streams',
            return_value=[stream],
        ),
        patch(
            'apps.stream.stream_checker_service.analyze_stream',
            return_value=analysis_result,
        ),
        patch('apps.stream.stream_checker_service.update_channel_streams'),
        patch(
            'apps.stream.stream_checker_service.batch_update_stream_stats',
            return_value=(1, 0),
        ) as mock_batch_update,
        patch(
            'apps.stream.stream_checker_service._get_base_url',
            return_value='http://localhost:9191',
        ),
    ):
        limiter = get_account_limiter()
        limiter.udi_manager = udi
        service = StreamCheckerService()
        _configure_bitrate_runtime_service(
            service,
            {'channels': {}, 'last_global_check': None},
        )
        service._require_quality_check_connectivity = Mock(return_value=None)
        service.progress.update = record_progress

        if failure_stage == 'classification':
            original_calculate_score = service._calculate_stream_score
            score_calls = {'count': 0}

            def fail_first_score(*args, **kwargs):
                score_calls['count'] += 1
                if score_calls['count'] == 1:
                    raise RuntimeError('synthetic progress classification failure')
                return original_calculate_score(*args, **kwargs)

            service._calculate_stream_score = fail_first_score

        result = service._check_channel_concurrent(
            channel_id,
            force_check_override=False,
        )

    assert 'error' not in result
    assert analysis_result['status'] == 'OK'
    assert analysis_result['quality_reason'] == 'none'
    assert analysis_result['quality_reason_detail'] == 'none'
    assert limiter.account_checking_counts.get(1, 0) == 0

    terminal_step = (
        'Closed failed progress 1/1'
        if failure_stage == 'classification'
        else 'Republished terminal progress 1/1'
    )
    terminal_updates = [
        update
        for update in published_updates
        if update.get('step_detail') == terminal_step
    ]
    assert len(terminal_updates) == 1
    terminal_update = terminal_updates[0]
    terminal_row = terminal_update['streams_detail'][0]
    if failure_stage == 'classification':
        assert terminal_row['status'] == 'error'
        assert terminal_row['reason_detail'] == 'progress_callback_error'
        assert terminal_row['quality_reason'] == 'offline'
        assert terminal_row['quality_reason_context']['stage'] == 'stream progress'
    else:
        assert terminal_row['status'] == 'completed'
        assert terminal_row['quality_reason'] == 'none'
        assert terminal_row['quality_reason_context'] == {}
    assert sum(
        int(slot.get('checking') or 0)
        for slot in terminal_update.get('provider_profile_slots', {}).get('1', [])
        if slot.get('capacity_counted') is True
    ) == 0
    assert not any(
        row.get('status') in {'checking', 'rechecking_bitrate'}
        for update in published_updates[published_updates.index(terminal_update):]
        for row in update.get('streams_detail') or []
    )

    if failure_stage == 'publish':
        assert failed_terminal_publish['value'] is True
        attempted_steps = [
            update.get('step_detail') for update in publish_attempts
        ]
        assert attempted_steps.index('Completed 1/1') < attempted_steps.index(
            'Republished terminal progress 1/1'
        )

    mock_batch_update.assert_called_once()
    persisted_stats = mock_batch_update.call_args.args[0][0]['stream_stats']
    assert persisted_stats['quality_reason'] == 'none'
    assert persisted_stats['quality_reason_detail'] == 'none'


def test_loop_probe_fully_protected_target_uses_empty_status_snapshot():
    from apps.stream.concurrent_stream_limiter import get_account_limiter
    from apps.stream.stream_checker_service import StreamCheckerService

    channel_id = 103
    stream = {
        'id': 1201,
        'name': 'Viewer-protected loop target',
        'url': 'http://provider.invalid/live/protected/1201.ts',
        'm3u_account': 1,
        'stream_stats': {},
    }
    udi = _make_bitrate_runtime_udi(channel_id, [stream])
    automation_config = MagicMock()
    automation_config.get_profile.return_value = None
    automation_config.get_effective_configuration.return_value = {
        'profile': {
            'name': 'Loop-enabled test profile',
            'stream_checking': {
                'enabled': True,
                'grace_period': False,
                'loop_check_enabled': True,
            },
            'scoring_weights': {'loop_penalty': -0.1},
        },
        'periods': [],
    }

    with (
        patch('apps.stream.stream_checker_service.get_udi_manager', return_value=udi),
        patch(
            'apps.stream.stream_checker_service.get_automation_config_manager',
            return_value=automation_config,
        ),
        patch(
            'apps.stream.stream_checker_service.fetch_channel_streams',
            return_value=[stream],
        ),
        patch('apps.stream.stream_checker_service.analyze_stream') as mock_analyze,
        patch('apps.stream.stream_checker_service.update_channel_streams'),
        patch(
            'apps.stream.stream_checker_service.batch_update_stream_stats',
            return_value=(0, 0),
        ),
        patch(
            'apps.stream.stream_checker_service._get_base_url',
            return_value='http://localhost:9191',
        ),
    ):
        limiter = get_account_limiter()
        limiter.udi_manager = udi
        service = StreamCheckerService()
        _configure_bitrate_runtime_service(
            service,
            {'channels': {}, 'last_global_check': None},
        )
        service._get_active_viewer_protected_stream_ids = Mock(
            return_value={stream['id']},
        )
        service._run_loop_probes = Mock()

        result = service._check_channel_concurrent(
            channel_id,
            target_stream_ids=[stream['id']],
            force_check_override=True,
        )

    assert 'error' not in result
    mock_analyze.assert_not_called()
    service._run_loop_probes.assert_called_once()
    loop_call = service._run_loop_probes.call_args
    assert loop_call.args[0] == []
    assert loop_call.kwargs['streams_detail'] == []


@pytest.mark.parametrize(
    'recheck_failure_stage',
    (None, 'classification', 'publish'),
)
def test_recovered_bitrate_recheck_updates_live_row_score_and_final_flags(
    recheck_failure_stage,
):
    from apps.stream.stream_checker_service import StreamCheckerService
    from apps.stream.concurrent_stream_limiter import get_account_limiter

    channel_id = 101
    stream = {
        'id': 1001,
        'name': 'Recovered bitrate',
        'url': 'http://provider.invalid/live/main-user/main-pass/1001.ts',
        'm3u_account': 1,
        'stream_stats': {},
    }
    profiles = [
        {
            'id': 101,
            'name': 'Provider A profile',
            'is_active': True,
            'is_default': True,
            'max_streams': 1,
            'search_pattern': 'main-user/main-pass',
            'replace_pattern': 'profile-a-user/profile-a-pass',
        },
        {
            'id': 102,
            'name': 'Provider B profile',
            'is_active': True,
            'is_default': False,
            'max_streams': 1,
            'search_pattern': 'main-user/main-pass',
            'replace_pattern': 'profile-b-user/profile-b-pass',
        },
    ]
    profile_urls = {
        101: 'http://provider.invalid/live/profile-a-user/profile-a-pass/1001.ts',
        102: 'http://provider.invalid/live/profile-b-user/profile-b-pass/1001.ts',
    }
    udi = _make_bitrate_runtime_udi(channel_id, [stream], profiles=profiles)
    udi.apply_profile_url_transformation.side_effect = (
        lambda _stream, profile=None: profile_urls[profile['id']]
    )
    probe_phase = {'recheck': False, 'release_profile_b': False}

    def active_profile_usage(_account_id):
        if not probe_phase['recheck']:
            return {}
        usage = {
            101: {
                'active_streams': 1,
                'real_viewers': 1,
                'real_viewer_streams': 1,
                'shadow_watchers': 0,
            },
        }
        # The first serial-recheck reservation attempt sees both profiles full.
        # This forces a published defer row where the released initial Profile A
        # must no longer be advertised. Profile B becomes available on retry.
        if not probe_phase['release_profile_b']:
            usage[102] = {
                'active_streams': 1,
                'real_viewers': 1,
                'real_viewer_streams': 1,
                'shadow_watchers': 0,
            }
        return usage

    udi.get_active_stream_context_per_profile.side_effect = active_profile_usage

    def release_profile_b_after_capacity_wait(seconds):
        assert seconds == 0.5
        probe_phase['release_profile_b'] = True
    automation_config = MagicMock()
    automation_config.get_profile.return_value = None
    automation_config.get_effective_configuration.return_value = {
        'profile': None,
        'periods': [],
    }
    initial_result = {
        'stream_id': stream['id'],
        'stream_name': stream['name'],
        'stream_url': stream['url'],
        'status': 'OK',
        'resolution': '3840x2160',
        'fps': 60,
        'video_codec': 'hevc',
        'audio_codec': 'aac',
        'bitrate_kbps': None,
        'measurement_incomplete': True,
        'measurement_incomplete_reason': 'missing_bitrate',
        'measurement_incomplete_context': {},
        'bitrate_recheck_required': True,
        'blank_probe_ran': True,
        'blank_detected': False,
        'freeze_probe_ran': True,
        'freeze_detected': False,
    }
    recovered_result = {
        'stream_id': stream['id'],
        'stream_name': stream['name'],
        'stream_url': stream['url'],
        'status': 'OK',
        'resolution': '3840x2160',
        'fps': 60,
        'video_codec': 'hevc',
        'audio_codec': 'aac',
        'bitrate_kbps': 17870,
        'bitrate_source': 'ffmpeg_progress',
        'measurement_incomplete': False,
        'measurement_incomplete_reason': 'none',
        'measurement_incomplete_context': {},
        'bitrate_recheck_required': False,
    }
    progress_attempts = []
    progress_updates = []
    failed_recheck_publish = {'value': False}
    analyzed_urls = []

    def record_progress(**kwargs):
        snapshot = deepcopy(kwargs)
        progress_attempts.append(snapshot)
        if (
            recheck_failure_stage == 'publish'
            and kwargs.get('step_detail') == 'Completed serial bitrate recheck 1/1'
            and not failed_recheck_publish['value']
        ):
            failed_recheck_publish['value'] = True
            raise RuntimeError('synthetic recheck terminal publish failure')
        progress_updates.append(snapshot)
        return True

    def analyze_with_profile_switch(**kwargs):
        analyzed_urls.append(kwargs['stream_url'])
        if len(analyzed_urls) == 1:
            probe_phase['recheck'] = True
            return dict(initial_result, stream_url=kwargs['stream_url'])
        return dict(recovered_result, stream_url=kwargs['stream_url'])

    with (
        patch('apps.stream.stream_checker_service.get_udi_manager', return_value=udi),
        patch(
            'apps.stream.stream_checker_service.get_automation_config_manager',
            return_value=automation_config,
        ),
        patch(
            'apps.stream.stream_checker_service.fetch_channel_streams',
            return_value=[stream],
        ),
        patch(
            'apps.stream.stream_checker_service.analyze_stream',
            side_effect=analyze_with_profile_switch,
        ) as mock_analyze,
        patch(
            'apps.stream.stream_checker_service.update_channel_streams',
        ) as mock_update_channel,
        patch(
            'apps.stream.stream_checker_service.batch_update_stream_stats',
            return_value=(1, 0),
        ) as mock_batch_update,
        patch(
            'apps.stream.stream_checker_service._get_base_url',
            return_value='http://localhost:9191',
        ),
        patch(
            'apps.stream.concurrent_stream_limiter.time.sleep',
            side_effect=release_profile_b_after_capacity_wait,
        ),
    ):
        limiter = get_account_limiter()
        limiter.clear()
        limiter.udi_manager = udi
        try:
            service = StreamCheckerService()
            tracker = _configure_bitrate_runtime_service(
                service,
                {'channels': {}, 'last_global_check': None},
            )
            service.progress.update = record_progress
            failed_recheck_classification = {'value': False}
            if recheck_failure_stage == 'classification':
                original_is_stream_dead = service._is_stream_dead

                def fail_first_recovered_classification(
                    analyzed,
                    *args,
                    **kwargs,
                ):
                    if (
                        analyzed.get('bitrate_kbps') == 17870
                        and not failed_recheck_classification['value']
                    ):
                        failed_recheck_classification['value'] = True
                        raise RuntimeError(
                            'synthetic recheck classification failure'
                        )
                    return original_is_stream_dead(analyzed, *args, **kwargs)

                service._is_stream_dead = fail_first_recovered_classification

            result = service._check_channel_concurrent(
                channel_id,
                force_check_override=False,
            )
        finally:
            limiter.clear()

    assert 'error' not in result
    assert mock_analyze.call_count == 2
    assert analyzed_urls == [profile_urls[101], profile_urls[102]]
    mock_update_channel.assert_called_once()
    mock_batch_update.assert_called_once()
    tracker.mark_channel_checked.assert_called_once()

    initial_progress = next(
        update
        for update in progress_updates
        if update.get('step_detail') == 'Completed 1/1'
    )
    initial_row = initial_progress['streams_detail'][0]
    assert initial_row['status'] == 'incomplete_bitrate'
    assert initial_row['score'] == 0.0
    assert initial_row['bitrate'] is None
    assert initial_row['measurement_incomplete'] is True
    assert initial_row['bitrate_recheck_required'] is True
    assert initial_row['reserved_profile_id'] == 101
    assert initial_row['reserved_profile_name'] == 'Provider A profile'
    assert initial_row['reserved_profile_limit'] == 1
    expected_reserved_fields = {
        'reserved_profile_id',
        'reserved_profile_name',
        'reserved_profile_limit',
    }
    assert {
        key for key in initial_row if key.startswith('reserved_profile_')
    } == expected_reserved_fields

    recheck_waiting_progress = next(
        update
        for update in progress_updates
        if str(update.get('step_detail', '')).startswith(
            'Waiting to start serial bitrate recheck 1/1:'
        )
    )
    recheck_waiting_row = recheck_waiting_progress['streams_detail'][0]
    assert recheck_waiting_row['status'] == 'waiting_provider_limit'
    assert recheck_waiting_row['reason_detail'] == 'active_viewers'
    assert 'reserved_profile_id' not in recheck_waiting_row
    assert 'reserved_profile_name' not in recheck_waiting_row
    assert 'reserved_profile_limit' not in recheck_waiting_row

    recheck_started_progress = next(
        update
        for update in progress_updates
        if update.get('step_detail') == 'Serial bitrate recheck 1/1'
    )
    recheck_started_row = recheck_started_progress['streams_detail'][0]
    assert recheck_started_row['status'] == 'rechecking_bitrate'
    assert recheck_started_row['reserved_profile_id'] == 102
    assert recheck_started_row['reserved_profile_name'] == 'Provider B profile'
    assert recheck_started_row['reserved_profile_limit'] == 1
    assert {
        key for key in recheck_started_row if key.startswith('reserved_profile_')
    } == expected_reserved_fields

    if recheck_failure_stage == 'classification':
        terminal_recheck_step = 'Closed failed serial bitrate recheck 1/1'
    elif recheck_failure_stage == 'publish':
        terminal_recheck_step = (
            'Republished terminal serial bitrate recheck 1/1'
        )
    else:
        terminal_recheck_step = 'Completed serial bitrate recheck 1/1'
    recovered_progress = next(
        update
        for update in progress_updates
        if update.get('step_detail') == terminal_recheck_step
    )
    recovered_row = recovered_progress['streams_detail'][0]
    if recheck_failure_stage == 'classification':
        assert failed_recheck_classification['value'] is True
        assert recovered_row['status'] == 'error'
        assert recovered_row['score'] == 0.0
        assert recovered_row['reason_detail'] == 'bitrate_recheck_progress_error'
        assert recovered_row['quality_reason'] == 'offline'
        assert recovered_row['quality_reason_detail'] == 'error'
        assert recovered_row['quality_reason_context']['stage'] == (
            'bitrate recheck progress'
        )
    else:
        assert recovered_row['status'] == 'completed'
        assert recovered_row['score'] == 1.0
        assert recovered_row['bitrate'] == 17870
        assert recovered_row['reason'] == 'none'
        assert recovered_row['reason_detail'] == 'none'
        assert recovered_row['quality_reason'] == 'none'
        assert recovered_row['quality_reason_detail'] == 'none'
        assert recovered_row['quality_reason_context'] == {}
        assert recovered_row['measurement_incomplete'] is False
        assert recovered_row['measurement_incomplete_reason'] == 'none'
        assert recovered_row['measurement_incomplete_context'] == {}
        assert recovered_row['bitrate_recheck_required'] is False
        assert recovered_row['bitrate_recheck_attempted'] is True
        assert recovered_row['bitrate_recheck_outcome'] == 'recovered'
    assert recovered_row['reserved_profile_id'] == 102
    assert recovered_row['reserved_profile_name'] == 'Provider B profile'
    assert recovered_row['reserved_profile_limit'] == 1
    assert {
        key for key in recovered_row if key.startswith('reserved_profile_')
    } == expected_reserved_fields
    assert sum(
        int(slot.get('checking') or 0)
        for slot in recovered_progress.get('provider_profile_slots', {}).get(
            '1',
            [],
        )
        if slot.get('capacity_counted') is True
    ) == 0
    if recheck_failure_stage == 'publish':
        assert failed_recheck_publish['value'] is True
        attempted_steps = [
            update.get('step_detail') for update in progress_attempts
        ]
        assert attempted_steps.index(
            'Completed serial bitrate recheck 1/1'
        ) < attempted_steps.index(
            'Republished terminal serial bitrate recheck 1/1'
        )

    serialized_progress = json.dumps(progress_updates, sort_keys=True)
    for probe_url in (stream['url'], *profile_urls.values()):
        assert probe_url not in serialized_progress
    for credential_fragment in (
        'main-user',
        'main-pass',
        'profile-a-user',
        'profile-a-pass',
        'profile-b-user',
        'profile-b-pass',
    ):
        assert credential_fragment not in serialized_progress

    checked_row = result['checked_streams'][0]
    assert checked_row['status'] == 'completed'
    assert checked_row['score'] == 1.0
    assert checked_row['bitrate'] == '17.9 Mbps'
    assert checked_row['measurement_incomplete'] is False
    assert checked_row['measurement_incomplete_reason'] == 'none'
    assert checked_row['measurement_incomplete_context'] == {}
    assert checked_row['bitrate_recheck_required'] is False
    assert checked_row['bitrate_recheck_attempted'] is True
    assert checked_row['bitrate_recheck_outcome'] == 'recovered'

    persisted_payload = mock_batch_update.call_args.args[0][0]['stream_stats']
    assert persisted_payload['ffmpeg_output_bitrate'] == 17870
    assert persisted_payload['measurement_incomplete'] is False
    assert persisted_payload['bitrate_recheck_required'] is False
    assert persisted_payload['bitrate_recheck_attempted'] is True
    assert persisted_payload['bitrate_recheck_outcome'] == 'recovered'


def test_cached_incomplete_bitrate_is_na_current_but_uses_history_for_scoring():
    from apps.stream.stream_checker_service import StreamCheckerService

    channel_id = 102
    cached_stream = {
        'id': 2001,
        'name': 'Cached incomplete bitrate',
        'url': 'http://example.invalid/cached',
        'm3u_account': 1,
        'stream_stats': {
            'resolution': '3840x2160',
            'source_fps': 60,
            'video_codec': 'hevc',
            'audio_codec': 'aac',
            'ffmpeg_output_bitrate': 12000,
            'quality_reason': 'missing_bitrate_after_recheck',
            'quality_reason_detail': 'missing_bitrate_after_recheck',
            'quality_reason_context': {'bitrate_recheck_outcome': 'unavailable'},
            'measurement_incomplete': True,
            'measurement_incomplete_reason': 'missing_bitrate_after_recheck',
            'measurement_incomplete_context': {
                'bitrate_recheck_outcome': 'unavailable',
            },
            'bitrate_recheck_required': True,
            'bitrate_recheck_attempted': True,
            'bitrate_recheck_outcome': 'unavailable',
        },
    }
    new_stream = {
        'id': 2002,
        'name': 'New measured bitrate',
        'url': 'http://example.invalid/new',
        'm3u_account': 1,
        'stream_stats': {},
    }
    streams = [cached_stream, new_stream]
    udi = _make_bitrate_runtime_udi(channel_id, streams)
    profile = {
        'id': 'bitrate-runtime-profile',
        'name': 'Bitrate Runtime Profile',
        'stream_checking': {
            'enabled': True,
            'stream_limit': 0,
            'allow_revive': True,
            'm3u_priority': [],
            'm3u_priority_mode': 'absolute',
            'grace_period': True,
            'loop_check_enabled': False,
            'blank_check_enabled': False,
            'freeze_check_enabled': False,
            'remove_dead_streams': False,
        },
        'scoring_weights': {
            'bitrate': 1.0,
            'resolution': 0.0,
            'fps': 0.0,
            'codec': 0.0,
            'hdr': 0.0,
            'prefer_h265': True,
        },
    }
    automation_config = MagicMock()
    automation_config.get_profile.return_value = None
    automation_config.get_effective_configuration.return_value = {
        'profile': profile,
        'periods': [],
    }
    measured_new_result = {
        'stream_id': new_stream['id'],
        'stream_name': new_stream['name'],
        'stream_url': new_stream['url'],
        'status': 'OK',
        'resolution': '1920x1080',
        'fps': 50,
        'video_codec': 'h264',
        'audio_codec': 'aac',
        'bitrate_kbps': 4000,
        'measurement_incomplete': False,
        'measurement_incomplete_reason': 'none',
        'measurement_incomplete_context': {},
        'bitrate_recheck_required': False,
    }

    with (
        patch('apps.stream.stream_checker_service.get_udi_manager', return_value=udi),
        patch(
            'apps.stream.stream_checker_service.get_automation_config_manager',
            return_value=automation_config,
        ),
        patch(
            'apps.stream.stream_checker_service.fetch_channel_streams',
            return_value=streams,
        ),
        patch(
            'apps.stream.stream_checker_service.analyze_stream',
            return_value=measured_new_result,
        ) as mock_analyze,
        patch(
            'apps.stream.stream_checker_service.update_channel_streams',
        ) as mock_update_channel,
        patch(
            'apps.stream.stream_checker_service.batch_update_stream_stats',
            return_value=(2, 0),
        ) as mock_batch_update,
        patch(
            'apps.stream.stream_checker_service._get_base_url',
            return_value='http://localhost:9191',
        ),
    ):
        service = StreamCheckerService()
        tracker = _configure_bitrate_runtime_service(
            service,
            {
                'channels': {
                    str(channel_id): {
                        'checked_stream_ids': [cached_stream['id']],
                        'last_check': datetime.now().isoformat(),
                    },
                },
                'last_global_check': None,
            },
        )
        service.progress.update = Mock()

        result = service._check_channel_concurrent(
            channel_id,
            force_check_override=False,
        )

    assert 'error' not in result
    mock_analyze.assert_called_once()
    assert mock_analyze.call_args.kwargs['stream_id'] == new_stream['id']
    mock_update_channel.assert_called_once()
    mock_batch_update.assert_called_once()
    tracker.mark_channel_checked.assert_called_once()

    cached_analysis = next(
        row for row in result['analyzed_streams']
        if row['stream_id'] == cached_stream['id']
    )
    assert cached_analysis['bitrate_kbps'] is None
    assert cached_analysis['scoring_bitrate_kbps'] == 12000
    assert cached_analysis['score'] == 1.0
    assert cached_analysis['measurement_incomplete'] is True
    assert cached_analysis['measurement_incomplete_reason'] == 'missing_bitrate_after_recheck'
    assert cached_analysis['bitrate_recheck_required'] is True
    assert cached_analysis['bitrate_recheck_attempted'] is True
    assert cached_analysis['bitrate_recheck_outcome'] == 'unavailable'

    cached_report_row = next(
        row for row in result['checked_streams']
        if row['stream_id'] == cached_stream['id']
    )
    assert cached_report_row['bitrate'] == 'N/A'
    assert cached_report_row['score'] == 1.0
    assert cached_report_row['status'] == 'incomplete_bitrate'
    assert cached_report_row['reason_detail'] == 'missing_bitrate_after_recheck'
    assert cached_report_row['measurement_incomplete'] is True
    assert cached_report_row['measurement_incomplete_reason'] == 'missing_bitrate_after_recheck'
    assert cached_report_row['bitrate_recheck_required'] is True
    assert cached_report_row['bitrate_recheck_attempted'] is True
    assert cached_report_row['bitrate_recheck_outcome'] == 'unavailable'

    cached_persist_payload = next(
        item['stream_stats']
        for item in mock_batch_update.call_args.args[0]
        if item['stream_id'] == cached_stream['id']
    )
    assert 'ffmpeg_output_bitrate' not in cached_persist_payload
    assert cached_persist_payload['measurement_incomplete'] is True
    assert cached_persist_payload['measurement_incomplete_reason'] == 'missing_bitrate_after_recheck'
    assert cached_persist_payload['bitrate_recheck_required'] is True
    assert cached_persist_payload['bitrate_recheck_attempted'] is True
    assert cached_persist_payload['bitrate_recheck_outcome'] == 'unavailable'


if __name__ == '__main__':
    unittest.main()
