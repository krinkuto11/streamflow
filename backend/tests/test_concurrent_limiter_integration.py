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
from copy import deepcopy
from datetime import datetime

pytestmark = pytest.mark.integration

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _make_bitrate_runtime_udi(channel_id, streams):
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
        'profiles': [],
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
        udi_mock.get_m3u_accounts.return_value = [
            {'id': 1, 'name': 'Account A', 'max_streams': 1},  # Max 1 concurrent
            {'id': 2, 'name': 'Account B', 'max_streams': 2},  # Max 2 concurrent
        ]
        
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
        service = StreamCheckerService()
        
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


def test_recovered_bitrate_recheck_updates_live_row_score_and_final_flags():
    from apps.stream.stream_checker_service import StreamCheckerService

    channel_id = 101
    stream = {
        'id': 1001,
        'name': 'Recovered bitrate',
        'url': 'http://example.invalid/recovered',
        'm3u_account': 1,
        'stream_stats': {},
    }
    udi = _make_bitrate_runtime_udi(channel_id, [stream])
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
            side_effect=[initial_result, recovered_result],
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
    ):
        service = StreamCheckerService()
        tracker = _configure_bitrate_runtime_service(
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

    assert 'error' not in result
    assert mock_analyze.call_count == 2
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

    recovered_progress = next(
        update
        for update in progress_updates
        if update.get('step_detail') == 'Completed serial bitrate recheck 1/1'
    )
    recovered_row = recovered_progress['streams_detail'][0]
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
