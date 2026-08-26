#!/usr/bin/env python3
"""
Tests for single-channel profile flag enforcement in check_single_channel().

Covers:
  - m3u_update.enabled = False skips the Dispatcharr provider fetch
  - m3u_update.enabled = False still syncs UDI when matching or checking is enabled
  - m3u_update.enabled = False with all flags off skips UDI sync entirely
  - m3u_update.enabled = True calls provider fetch then UDI sync
  - Step 3 (dead stream clearing) is unconditional regardless of flags
  - _wait_for_udi_stream_count_stabilise returns correctly
"""

import sys
import os
import json
import shutil
import tempfile
import threading
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock, call

# Add backend directory to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ---------------------------------------------------------------------------
# Shared helpers - mirrors the pattern used in test_stream_checker_profile_flags.py
# ---------------------------------------------------------------------------

def _make_profile(
    m3u_update_enabled=False,
    matching_enabled=False,
    checking_enabled=False,
    playlists=None,
):
    return {
        'name': 'Test Profile',
        'm3u_update': {'enabled': m3u_update_enabled, 'playlists': list(playlists or [])},
        'stream_matching': {'enabled': matching_enabled},
        'stream_checking': {
            'enabled': checking_enabled,
            'grace_period': False,
            'allow_revive': False,
            'check_all_streams': False,
            'stream_limit': 0,
            'm3u_priority': [],
            'm3u_priority_mode': 'absolute',
        },
        'scoring_weights': {
            'bitrate': 0.35, 'resolution': 0.30, 'fps': 0.15,
            'codec': 0.10, 'hdr': 0.10, 'prefer_h265': True,
        },
    }


def _make_mock_config():
    cfg = Mock()
    cfg.get = Mock(side_effect=lambda key, default=None: default)
    cfg.is_auto_quality_checking_enabled = Mock(return_value=True)
    return cfg


def _make_mock_udi(channel_id, channel_name, streams, m3u_accounts=None):
    udi = Mock()
    udi.get_channel_by_id.return_value = {
        'id': channel_id,
        'name': channel_name,
        'channel_group_id': None,
        'logo_id': None,
        'streams': [s['id'] for s in streams],
    }
    udi.get_streams.return_value = streams
    udi.get_stream_count.return_value = len(streams)
    udi.get_last_refresh_duration.return_value = 0
    udi.get_stream_by_id.return_value = None
    udi.get_channel_streams.return_value = streams
    udi.is_network_ready.return_value = False
    udi.refresh_streams = Mock()
    udi.refresh_channels = Mock()
    udi.refresh_m3u_accounts = Mock()
    udi.refresh_channel_groups = Mock()
    udi.refresh_channel_by_id = Mock()
    udi.get_m3u_accounts.return_value = list(m3u_accounts or [])
    return udi


def _make_mock_acm(profile):
    acm = Mock()
    acm.get_profile.return_value = None
    acm.get_effective_epg_scheduled_profile.return_value = None
    acm.get_effective_configuration.return_value = {'profile': profile, 'periods': []}
    return acm


def test_single_channel_fetch_clear_cannot_resurrect_progress():
    from apps.stream.stream_checker_service import StreamCheckerService

    class FakeDB:
        def __init__(self):
            self.settings = {}

        def get_system_setting(self, key, default=None):
            return self.settings.get(key, default)

        def set_system_setting(self, key, value):
            self.settings[key] = value

    profile = _make_profile(
        m3u_update_enabled=False,
        matching_enabled=False,
        checking_enabled=False,
    )
    streams = [
        {
            'id': 1,
            'url': 'http://provider.invalid/stream/1',
            'm3u_account': 5,
            'stream_stats': {},
        },
    ]
    fake_db = FakeDB()
    fetch_entered = threading.Event()
    release_fetch = threading.Event()

    def blocked_fetch(_channel_id):
        fetch_entered.set()
        assert release_fetch.wait(timeout=2)
        return streams

    mock_udi = _make_mock_udi(42, 'Clear Race Channel', streams)
    mock_acm = _make_mock_acm(profile)
    mock_session_manager = Mock()
    mock_session_manager.get_channels_in_active_sessions.return_value = []

    with patch('apps.database.manager.get_db_manager', return_value=fake_db), patch(
        'apps.stream.stream_checker_service.StreamCheckConfig'
    ) as mock_config_class, patch(
        'apps.stream.stream_checker_service.get_udi_manager',
        return_value=mock_udi,
    ), patch(
        'apps.stream.stream_checker_service.get_automation_config_manager',
        return_value=mock_acm,
    ), patch(
        'apps.stream.stream_checker_service.get_session_manager',
        return_value=mock_session_manager,
    ), patch(
        'apps.stream.stream_checker_service.fetch_channel_streams',
        side_effect=blocked_fetch,
    ):
        mock_config_class.return_value = _make_mock_config()
        service = StreamCheckerService()
        service._require_quality_check_connectivity = Mock(return_value=None)
        service._check_channel_limits = Mock(return_value=None)
        service.dead_streams_tracker = Mock()
        service.dead_streams_tracker.get_dead_streams_for_channel.return_value = {}
        service.dead_streams_tracker.cleanup_removed_streams.return_value = 0

        progress_updates = []
        original_update = service.progress.update

        def record_progress(**kwargs):
            progress_updates.append(dict(kwargs))
            return original_update(**kwargs)

        service.progress.update = record_progress
        results = []
        check_thread = threading.Thread(
            target=lambda: results.append(service.check_single_channel(42)),
        )
        check_thread.start()
        assert fetch_entered.wait(timeout=2)

        clear_result = service.clear_queue()
        release_fetch.set()
        check_thread.join(timeout=2)

    assert not check_thread.is_alive()
    assert clear_result['abort_requested'] is True
    assert results and results[0].get('aborted') is True
    post_fetch_update = next(
        update
        for update in progress_updates
        if update.get('step') == 'Identifying provider accounts'
    )
    assert post_fetch_update['expected_generation'] == 0
    assert fake_db.settings['stream_checker_progress'] == {}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestSingleChannelM3uUpdateFlagDisabled(unittest.TestCase):
    """m3u_update.enabled = False must skip the provider fetch."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_single_channel_run_snapshot_records_sanitized_stale_status(self):
        from apps.stream.stream_checker_service import StreamCheckerService

        service = StreamCheckerService.__new__(StreamCheckerService)
        udi = Mock()
        udi.is_network_ready.return_value = True
        udi.get_m3u_accounts.return_value = [
            {
                'id': 5,
                'name': 'Provider A',
                'is_active': True,
                'status': 'fetching',
                'last_message': 'Processing completed in 120.0 seconds.',
                'updated_at': '2026-06-13T08:02:50Z',
            },
            {
                'id': 7,
                'name': 'Provider B',
                'is_active': True,
                'status': 'success',
                'last_message': 'Processing completed in 95.0 seconds.',
            },
        ]

        snapshot = service._build_single_channel_run_snapshot(
            channel_id=99,
            channel_name='Snapshot Channel',
            start_time=1781352000.0,
            completed_at=datetime.fromisoformat('2026-06-13T13:01:00'),
            duration_seconds=60,
            profile=_make_profile(checking_enabled=True),
            profile_progress_context={
                'run_mode': 'single_channel_check',
                'run_profile_id': 'profile-v7',
                'run_profile_name': 'V7 Snapshot Profile',
                'run_profile_source': 'forced',
                'quality_profile_id': 'profile-v7',
                'quality_profile_name': 'V7 Snapshot Profile',
            },
            check_stats={'total_streams': 2, 'dead_streams': 1},
            visibility_summary={'channels_hidden': 0, 'channels_ready': 0, 'channel_visibility_changed': 0},
            checking_enabled=True,
            matching_enabled=False,
            m3u_update_enabled=False,
            forced_profile_id='profile-v7',
            force_check=False,
            provider_limit_override=False,
            is_epg_scheduled=False,
            m3u_refresh_scope='none',
            m3u_refresh_account_count=0,
            udi=udi,
        )

        stale_status = snapshot['dispatcharr_status']['stale_status']
        snapshot_json = json.dumps(snapshot)
        self.assertEqual(stale_status['status'], 'stale_risk')
        self.assertTrue(stale_status['stale_status_suspected'])
        self.assertEqual(stale_status['stale_suspected_count'], 1)
        self.assertEqual(stale_status['m3u_status_counts'], {'fetching': 1, 'success': 1})
        self.assertEqual(snapshot['stale_warnings'][0]['type'], 'dispatcharr_status_risk')
        self.assertNotIn('Provider A', snapshot_json)
        self.assertNotIn('Processing completed', snapshot_json)

    def test_failed_stream_count_includes_visual_failures_but_not_missing_bitrate(self):
        from apps.stream.stream_checker_service import StreamCheckerService

        count = StreamCheckerService._count_failed_checked_streams(
            {
                'checked_streams': [
                    {'stream_id': 1, 'status': 'blank', 'blank_detected': True},
                    {'stream_id': 2, 'status': 'freeze', 'freeze_detected': True},
                    {
                        'stream_id': 3,
                        'status': 'incomplete_bitrate',
                        'quality_reason': 'missing_bitrate',
                        'quality_reason_detail': 'missing_bitrate',
                    },
                    {'stream_id': 4, 'status': 'completed'},
                ],
            }
        )

        self.assertEqual(count, 2)

    @patch('stream_checker_service.get_udi_manager')
    @patch('stream_checker_service.StreamCheckConfig')
    @patch('stream_checker_service.get_automation_config_manager')
    @patch('apps.stream.stream_session_manager.get_session_manager')
    @patch('stream_checker_service.fetch_channel_streams')
    def test_m3u_update_disabled_skips_playlist_refresh_api_call(
        self, mock_fetch, mock_session_mgr, mock_acm_factory,
        mock_config_class, mock_get_udi,
    ):
        """When m3u_update.enabled is False, refresh_m3u_playlists must NOT be called."""
        from apps.stream.stream_checker_service import StreamCheckerService

        profile = _make_profile(m3u_update_enabled=False, matching_enabled=False, checking_enabled=True)
        channel_id = 42
        streams = [{'id': 1, 'url': 'http://x/1', 'm3u_account': 5, 'stream_stats': {}}]

        mock_config_class.return_value = _make_mock_config()
        mock_get_udi.return_value = _make_mock_udi(channel_id, 'Test Channel', streams)
        mock_session_mgr.return_value.get_channels_in_active_sessions.return_value = []
        mock_acm_factory.return_value = _make_mock_acm(profile)
        mock_fetch.return_value = streams

        service = StreamCheckerService()
        service._require_quality_check_connectivity = Mock(return_value=None)
        service._check_channel = Mock(return_value={'dead_streams_count': 0, 'revived_streams_count': 0, 'analyzed_streams': []})
        # Stub dead streams tracker
        service.dead_streams_tracker = Mock()
        service.dead_streams_tracker.get_dead_streams_for_channel.return_value = {}
        service.dead_streams_tracker.cleanup_removed_streams.return_value = 0

        with patch('apps.core.api_utils.refresh_m3u_playlists') as mock_refresh, \
             patch('stream_checker_service._wait_for_udi_stream_count_stabilise') as mock_poll:
            service.check_single_channel(channel_id=channel_id)

        mock_refresh.assert_not_called(), "refresh_m3u_playlists must not be called when m3u_update.enabled=False"
        mock_poll.assert_not_called(), "_wait_for_udi_stream_count_stabilise must not be called when update disabled"

    @patch('stream_checker_service.get_udi_manager')
    @patch('stream_checker_service.StreamCheckConfig')
    @patch('stream_checker_service.get_automation_config_manager')
    @patch('apps.stream.stream_session_manager.get_session_manager')
    @patch('stream_checker_service.fetch_channel_streams')
    def test_m3u_update_disabled_uses_existing_cache_when_checking_enabled(
        self, mock_fetch, mock_session_mgr, mock_acm_factory,
        mock_config_class, mock_get_udi,
    ):
        """When m3u_update=False but checking=True, the existing UDI cache is used."""
        from apps.stream.stream_checker_service import StreamCheckerService

        profile = _make_profile(m3u_update_enabled=False, matching_enabled=False, checking_enabled=True)
        channel_id = 43
        streams = [{'id': 2, 'url': 'http://x/2', 'm3u_account': 5, 'stream_stats': {}}]

        mock_config_class.return_value = _make_mock_config()
        mock_udi = _make_mock_udi(channel_id, 'Test Channel', streams)
        mock_get_udi.return_value = mock_udi
        mock_session_mgr.return_value.get_channels_in_active_sessions.return_value = []
        mock_acm_factory.return_value = _make_mock_acm(profile)
        mock_fetch.return_value = streams

        service = StreamCheckerService()
        service._require_quality_check_connectivity = Mock(return_value=None)
        service._check_channel = Mock(return_value={'dead_streams_count': 0, 'revived_streams_count': 0, 'analyzed_streams': []})
        service.dead_streams_tracker = Mock()
        service.dead_streams_tracker.get_dead_streams_for_channel.return_value = {}
        service.dead_streams_tracker.cleanup_removed_streams.return_value = 0

        with patch('apps.core.api_utils.refresh_m3u_playlists'):
            service.check_single_channel(channel_id=channel_id)

        mock_udi.refresh_streams.assert_not_called()
        mock_udi.refresh_channels.assert_not_called()
        mock_udi.refresh_m3u_accounts.assert_not_called()
        mock_udi.refresh_channel_groups.assert_not_called()

    @patch('stream_checker_service.get_udi_manager')
    @patch('stream_checker_service.StreamCheckConfig')
    @patch('stream_checker_service.get_automation_config_manager')
    @patch('stream_checker_service.get_session_manager')
    @patch('stream_checker_service.fetch_channel_streams')
    def test_single_channel_check_records_v7_snapshot_and_visibility_counters(
        self, mock_fetch, mock_session_mgr, mock_acm_factory,
        mock_config_class, mock_get_udi,
    ):
        """Single-channel checks carry immutable V7 run context into stats/changelog."""
        from apps.stream.stream_checker_service import StreamCheckerService

        profile = _make_profile(m3u_update_enabled=False, matching_enabled=False, checking_enabled=True)
        profile['id'] = 'profile-v7'
        profile['name'] = 'V7 Snapshot Profile'
        profile['stream_checking']['check_all_streams'] = True
        profile['stream_checking']['stream_limit'] = 4
        channel_id = 52
        streams = [{'id': 11, 'url': 'http://x/11', 'm3u_account': 5, 'stream_stats': {}}]

        mock_config_class.return_value = _make_mock_config()
        mock_udi = _make_mock_udi(channel_id, 'V7 Snapshot Channel', streams)
        mock_get_udi.return_value = mock_udi
        mock_session_mgr.return_value.get_channels_in_active_sessions.return_value = []
        mock_acm = _make_mock_acm(profile)
        mock_acm.get_profile.return_value = profile
        mock_acm_factory.return_value = mock_acm
        mock_fetch.return_value = streams

        service = StreamCheckerService()
        service._require_quality_check_connectivity = Mock(return_value=None)
        service._check_channel = Mock(return_value={
            'dead_streams_count': 1,
            'revived_streams_count': 0,
            'analyzed_streams': [],
            'channel_visibility': {
                'action': 'hidden',
                'changed': True,
                'channel_id': channel_id,
                'reason': 'all_failed',
            },
        })
        service.dead_streams_tracker = Mock()
        service.dead_streams_tracker.get_dead_streams_for_channel.return_value = {}
        service.dead_streams_tracker.cleanup_removed_streams.return_value = 0
        service.changelog = Mock()

        with patch('apps.core.api_utils.refresh_m3u_playlists'):
            result = service.check_single_channel(channel_id=channel_id, forced_profile_id='profile-v7')

        stats = result['stats']
        snapshot = stats['run_snapshot']
        self.assertEqual(result['run_mode'], 'single_channel_check')
        self.assertEqual(result['channels_hidden'], 1)
        self.assertEqual(result['channels_ready'], 0)
        self.assertEqual(stats['run_profile_name'], 'V7 Snapshot Profile')
        self.assertEqual(stats['quality_profile_name'], 'V7 Snapshot Profile')
        self.assertEqual(stats['capacity_profile_source'], 'm3u_account_profiles')
        self.assertEqual(stats['channels_hidden'], 1)
        self.assertEqual(stats['channels_ready'], 0)
        self.assertEqual(stats['channel_visibility_changed'], 1)
        self.assertEqual(snapshot['run_mode'], 'single_channel_check')
        self.assertEqual(snapshot['start_source'], 'manual_forced_profile')
        self.assertEqual(snapshot['effective_profiles'][0]['profile_name'], 'V7 Snapshot Profile')
        self.assertEqual(snapshot['quality_rules'][0]['stream_limit'], 4)
        self.assertEqual(snapshot['capacity_profile_context']['type'], 'provider_account_profiles')
        self.assertEqual(snapshot['result_summary']['channels_hidden'], 1)
        self.assertEqual(snapshot['result_summary']['channels_ready'], 0)
        self.assertFalse(snapshot['snapshot_truncated'])
        self.assertNotIn('stream_details', snapshot)
        self.assertNotIn('stream_url', json.dumps(snapshot))
        service._check_channel.assert_called_once_with(
            channel_id,
            skip_batch_changelog=True,
            forced_profile_id='profile-v7',
            run_mode='single_channel_check',
            is_single_channel_check=True,
            expected_progress_generation=0,
            force_check_override=False,
            force_check_generation=None,
        )

        service.changelog.add_single_channel_check_entry.assert_called_once()
        changelog_stats = service.changelog.add_single_channel_check_entry.call_args.kwargs['check_stats']
        self.assertIs(changelog_stats, stats)

    @patch('stream_checker_service.get_udi_manager')
    @patch('stream_checker_service.StreamCheckConfig')
    @patch('stream_checker_service.get_automation_config_manager')
    @patch('apps.stream.stream_session_manager.get_session_manager')
    @patch('stream_checker_service.fetch_channel_streams')
    def test_m3u_update_disabled_refreshes_channel_after_matching_only(
        self, mock_fetch, mock_session_mgr, mock_acm_factory,
        mock_config_class, mock_get_udi,
    ):
        """When m3u_update=False but matching=True, only the channel cache is refreshed after matching."""
        from apps.stream.stream_checker_service import StreamCheckerService

        profile = _make_profile(m3u_update_enabled=False, matching_enabled=True, checking_enabled=False)
        channel_id = 44
        streams = [{'id': 3, 'url': 'http://x/3', 'm3u_account': 5, 'stream_stats': {}}]

        mock_config_class.return_value = _make_mock_config()
        mock_udi = _make_mock_udi(channel_id, 'Test Channel', streams)
        mock_get_udi.return_value = mock_udi
        mock_session_mgr.return_value.get_channels_in_active_sessions.return_value = []
        mock_acm_factory.return_value = _make_mock_acm(profile)
        mock_fetch.return_value = streams

        service = StreamCheckerService()
        service._require_quality_check_connectivity = Mock(return_value=None)
        service.dead_streams_tracker = Mock()
        service.dead_streams_tracker.get_dead_streams_for_channel.return_value = {}
        service.dead_streams_tracker.cleanup_removed_streams.return_value = 0

        with patch('apps.core.api_utils.refresh_m3u_playlists'), \
             patch('automated_stream_manager.AutomatedStreamManager') as mock_asm:
            mock_asm.return_value.discover_and_assign_streams = Mock(return_value={})
            mock_asm.return_value.validate_and_remove_non_matching_streams = Mock(return_value={})
            service.check_single_channel(channel_id=channel_id)

        mock_udi.refresh_streams.assert_not_called()
        mock_udi.refresh_channels.assert_not_called()
        mock_udi.refresh_channel_by_id.assert_called_with(channel_id)

    @patch('stream_checker_service.get_udi_manager')
    @patch('stream_checker_service.StreamCheckConfig')
    @patch('stream_checker_service.get_automation_config_manager')
    @patch('apps.stream.stream_session_manager.get_session_manager')
    @patch('stream_checker_service.fetch_channel_streams')
    def test_all_flags_false_skips_both_refresh_and_udi_sync(
        self, mock_fetch, mock_session_mgr, mock_acm_factory,
        mock_config_class, mock_get_udi,
    ):
        """When all three flags are False, no provider fetch and no UDI sync should occur."""
        from apps.stream.stream_checker_service import StreamCheckerService

        profile = _make_profile(m3u_update_enabled=False, matching_enabled=False, checking_enabled=False)
        channel_id = 45
        streams = [{'id': 4, 'url': 'http://x/4', 'm3u_account': 5, 'stream_stats': {}}]

        mock_config_class.return_value = _make_mock_config()
        mock_udi = _make_mock_udi(channel_id, 'Test Channel', streams)
        mock_get_udi.return_value = mock_udi
        mock_session_mgr.return_value.get_channels_in_active_sessions.return_value = []
        mock_acm_factory.return_value = _make_mock_acm(profile)
        mock_fetch.return_value = streams

        service = StreamCheckerService()
        service._require_quality_check_connectivity = Mock(return_value=None)
        service.dead_streams_tracker = Mock()
        service.dead_streams_tracker.get_dead_streams_for_channel.return_value = {}
        service.dead_streams_tracker.cleanup_removed_streams.return_value = 0

        with patch('apps.core.api_utils.refresh_m3u_playlists') as mock_refresh:
            service.check_single_channel(channel_id=channel_id)

        mock_refresh.assert_not_called()
        mock_udi.refresh_streams.assert_not_called()
        mock_udi.refresh_channels.assert_not_called()
        mock_udi.refresh_m3u_accounts.assert_not_called()
        mock_udi.refresh_channel_groups.assert_not_called()


class TestSingleChannelM3uUpdateFlagEnabled(unittest.TestCase):
    """m3u_update.enabled = True must call provider fetch then UDI sync."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    @patch('stream_checker_service.get_udi_manager')
    @patch('stream_checker_service.StreamCheckConfig')
    @patch('stream_checker_service.get_automation_config_manager')
    @patch('apps.stream.stream_session_manager.get_session_manager')
    @patch('stream_checker_service.fetch_channel_streams')
    def test_empty_profile_playlists_refresh_all_active_non_custom_accounts_then_syncs_udi(
        self, mock_fetch, mock_session_mgr, mock_acm_factory,
        mock_config_class, mock_get_udi,
    ):
        """Empty m3u_update.playlists means all active non-custom accounts, not just attached accounts."""
        from apps.stream.stream_checker_service import StreamCheckerService

        profile = _make_profile(m3u_update_enabled=True, matching_enabled=False, checking_enabled=True)
        channel_id = 46
        streams = [{'id': 5, 'url': 'http://x/5', 'm3u_account': 7, 'stream_stats': {}}]
        m3u_accounts = [
            {'id': 7, 'name': 'Provider 7', 'is_active': True},
            {'id': 8, 'name': 'Provider 8', 'is_active': True},
            {'id': 9, 'name': 'Provider 9', 'is_active': False},
            {'id': 10, 'name': 'custom', 'is_active': True},
        ]

        mock_config_class.return_value = _make_mock_config()
        mock_udi = _make_mock_udi(channel_id, 'Test Channel', streams, m3u_accounts=m3u_accounts)
        mock_get_udi.return_value = mock_udi
        mock_session_mgr.return_value.get_channels_in_active_sessions.return_value = []
        mock_acm_factory.return_value = _make_mock_acm(profile)
        mock_fetch.return_value = streams

        service = StreamCheckerService()
        service._require_quality_check_connectivity = Mock(return_value=None)
        service._check_channel = Mock(return_value={'dead_streams_count': 0, 'revived_streams_count': 0, 'analyzed_streams': []})
        service.dead_streams_tracker = Mock()
        service.dead_streams_tracker.get_dead_streams_for_channel.return_value = {}
        service.dead_streams_tracker.cleanup_removed_streams.return_value = 0

        call_order = []

        with patch('apps.core.api_utils.refresh_m3u_playlists',
                   side_effect=lambda account_id=None: call_order.append('refresh')) as mock_refresh, \
             patch('apps.stream.stream_checker_service._wait_for_udi_stream_count_stabilise',
                   side_effect=lambda *a, **kw: call_order.append('poll') or True) as mock_poll:

            # Patch UDI sync methods to record order
            mock_udi.refresh_streams.side_effect = lambda: call_order.append('udi_sync')

            result = service.check_single_channel(channel_id=channel_id)

        self.assertEqual(
            mock_refresh.call_args_list,
            [call(account_id=7), call(account_id=8)],
        )
        mock_poll.assert_called_once()
        self.assertEqual(result['stats']['m3u_refresh_scope'], 'all_active_non_custom')
        self.assertEqual(result['stats']['m3u_refresh_account_count'], 2)

        # Verify order: all refreshes -> poll -> UDI sync.
        refresh_indices = [i for i, v in enumerate(call_order) if v == 'refresh']
        poll_idx = next((i for i, v in enumerate(call_order) if v == 'poll'), -1)
        udi_idx = next((i for i, v in enumerate(call_order) if v == 'udi_sync'), -1)

        self.assertTrue(refresh_indices)
        self.assertTrue(all(poll_idx > idx for idx in refresh_indices), "Poll must happen after provider refreshes")
        self.assertGreater(udi_idx, poll_idx, "UDI sync must happen after poll confirms completion")

    @patch('stream_checker_service.get_udi_manager')
    @patch('stream_checker_service.StreamCheckConfig')
    @patch('stream_checker_service.get_automation_config_manager')
    @patch('apps.stream.stream_session_manager.get_session_manager')
    @patch('stream_checker_service.fetch_channel_streams')
    def test_explicit_profile_playlists_refresh_exact_scope_not_channel_accounts(
        self, mock_fetch, mock_session_mgr, mock_acm_factory,
        mock_config_class, mock_get_udi,
    ):
        """Explicit m3u_update.playlists is authoritative for provider fetch scope."""
        from apps.stream.stream_checker_service import StreamCheckerService

        profile = _make_profile(
            m3u_update_enabled=True,
            matching_enabled=False,
            checking_enabled=True,
            playlists=[11, '12', 11],
        )
        channel_id = 49
        streams = [{'id': 8, 'url': 'http://x/8', 'm3u_account': 7, 'stream_stats': {}}]
        m3u_accounts = [
            {'id': 7, 'name': 'Provider 7', 'is_active': True},
            {'id': 8, 'name': 'Provider 8', 'is_active': True},
        ]

        mock_config_class.return_value = _make_mock_config()
        mock_udi = _make_mock_udi(channel_id, 'Test Channel', streams, m3u_accounts=m3u_accounts)
        mock_get_udi.return_value = mock_udi
        mock_session_mgr.return_value.get_channels_in_active_sessions.return_value = []
        mock_acm_factory.return_value = _make_mock_acm(profile)
        mock_fetch.return_value = streams

        service = StreamCheckerService()
        service._require_quality_check_connectivity = Mock(return_value=None)
        service._check_channel = Mock(return_value={'dead_streams_count': 0, 'revived_streams_count': 0, 'analyzed_streams': []})
        service.dead_streams_tracker = Mock()
        service.dead_streams_tracker.get_dead_streams_for_channel.return_value = {}
        service.dead_streams_tracker.cleanup_removed_streams.return_value = 0

        with patch('apps.core.api_utils.refresh_m3u_playlists') as mock_refresh, \
             patch('apps.stream.stream_checker_service._wait_for_udi_stream_count_stabilise',
                   return_value=True):
            result = service.check_single_channel(channel_id=channel_id)

        self.assertEqual(
            mock_refresh.call_args_list,
            [call(account_id=11), call(account_id=12)],
        )
        self.assertEqual(result['stats']['m3u_refresh_scope'], 'profile_playlists')
        self.assertEqual(result['stats']['m3u_refresh_account_count'], 2)


class TestStep3DeadStreamClearIsUnconditional(unittest.TestCase):
    """Step 3 (dead stream clearing) must run regardless of flag state."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    @patch('stream_checker_service.get_udi_manager')
    @patch('stream_checker_service.StreamCheckConfig')
    @patch('stream_checker_service.get_automation_config_manager')
    @patch('apps.stream.stream_session_manager.get_session_manager')
    @patch('stream_checker_service.fetch_channel_streams')
    def test_dead_stream_clear_runs_with_all_flags_off(
        self, mock_fetch, mock_session_mgr, mock_acm_factory,
        mock_config_class, mock_get_udi,
    ):
        """Even with all three flags False, dead streams must be cleared (Step 3)."""
        from apps.stream.stream_checker_service import StreamCheckerService

        # All flags off - a no-op profile from the user's perspective, but
        # Step 3 is unconditional and must still fire.
        profile = _make_profile(m3u_update_enabled=False, matching_enabled=False, checking_enabled=False)
        channel_id = 47
        streams = [{'id': 6, 'url': 'http://x/6', 'm3u_account': 3, 'stream_stats': {}}]

        mock_config_class.return_value = _make_mock_config()
        mock_get_udi.return_value = _make_mock_udi(channel_id, 'Test Channel', streams)
        mock_session_mgr.return_value.get_channels_in_active_sessions.return_value = []
        mock_acm_factory.return_value = _make_mock_acm(profile)
        mock_fetch.return_value = streams

        service = StreamCheckerService()
        service._require_quality_check_connectivity = Mock(return_value=None)
        mock_tracker = Mock()
        mock_tracker.get_dead_streams_for_channel.return_value = {}
        mock_tracker.cleanup_removed_streams.return_value = 0
        service.dead_streams_tracker = mock_tracker

        with patch('apps.core.api_utils.refresh_m3u_playlists'):
            service.check_single_channel(channel_id=channel_id)

        mock_tracker.cleanup_removed_streams.assert_called_with({'http://x/6'}, channel_id=channel_id)

    @patch('stream_checker_service.get_udi_manager')
    @patch('stream_checker_service.StreamCheckConfig')
    @patch('stream_checker_service.get_automation_config_manager')
    @patch('apps.stream.stream_session_manager.get_session_manager')
    @patch('stream_checker_service.fetch_channel_streams')
    def test_dead_stream_clear_runs_with_checking_only_profile(
        self, mock_fetch, mock_session_mgr, mock_acm_factory,
        mock_config_class, mock_get_udi,
    ):
        """Checking-only profile: dead streams cleared before check runs."""
        from apps.stream.stream_checker_service import StreamCheckerService

        profile = _make_profile(m3u_update_enabled=False, matching_enabled=False, checking_enabled=True)
        channel_id = 48
        streams = [{'id': 7, 'url': 'http://x/7', 'm3u_account': 3, 'stream_stats': {}}]

        mock_config_class.return_value = _make_mock_config()
        mock_get_udi.return_value = _make_mock_udi(channel_id, 'Test Channel', streams)
        mock_session_mgr.return_value.get_channels_in_active_sessions.return_value = []
        mock_acm_factory.return_value = _make_mock_acm(profile)
        mock_fetch.return_value = streams

        service = StreamCheckerService()
        service._require_quality_check_connectivity = Mock(return_value=None)
        service._check_channel = Mock(return_value={'dead_streams_count': 0, 'revived_streams_count': 0, 'analyzed_streams': []})
        mock_tracker = Mock()
        mock_tracker.get_dead_streams_for_channel.return_value = {}
        mock_tracker.cleanup_removed_streams.return_value = 0
        service.dead_streams_tracker = mock_tracker

        with patch('apps.core.api_utils.refresh_m3u_playlists'):
            service.check_single_channel(channel_id=channel_id)

        mock_tracker.cleanup_removed_streams.assert_called_with({'http://x/7'}, channel_id=channel_id)


class TestWaitForUdiStreamCountStabilise(unittest.TestCase):
    """Unit tests for _wait_for_udi_stream_count_stabilise helper."""

    def test_returns_true_when_count_changes(self):
        from apps.stream.stream_checker_service import _wait_for_udi_stream_count_stabilise
        import time

        mock_udi = Mock()
        # First poll: unchanged. Second poll: changed.
        mock_udi.get_stream_count.side_effect = [
            100,
            105,
        ]

        with patch('time.sleep'):
            result = _wait_for_udi_stream_count_stabilise(
                mock_udi, pre_count=100, timeout=30, poll_interval=5
            )

        self.assertTrue(result)

    def test_returns_false_on_timeout_with_no_change(self):
        from apps.stream.stream_checker_service import _wait_for_udi_stream_count_stabilise

        mock_udi = Mock()
        # Always returns same count
        mock_udi.get_stream_count.return_value = 100

        with patch('time.sleep'):
            result = _wait_for_udi_stream_count_stabilise(
                mock_udi, pre_count=100, timeout=10, poll_interval=5
            )

        self.assertFalse(result)

    def test_handles_udi_exception_gracefully(self):
        from apps.stream.stream_checker_service import _wait_for_udi_stream_count_stabilise

        mock_udi = Mock()
        # First call raises, second returns changed count
        mock_udi.get_stream_count.side_effect = [
            Exception("UDI unavailable"),
            105,
        ]

        with patch('time.sleep'):
            result = _wait_for_udi_stream_count_stabilise(
                mock_udi, pre_count=100, timeout=30, poll_interval=5
            )

        # Should recover from the exception and detect the change on the second poll
        self.assertTrue(result)


if __name__ == '__main__':
    unittest.main(verbosity=2)
