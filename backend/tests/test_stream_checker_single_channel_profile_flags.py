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
import unittest
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock, call

# Add backend directory to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ---------------------------------------------------------------------------
# Shared helpers — mirrors the pattern used in test_stream_checker_profile_flags.py
# ---------------------------------------------------------------------------

def _make_profile(
    m3u_update_enabled=False,
    matching_enabled=False,
    checking_enabled=False,
):
    return {
        'name': 'Test Profile',
        'm3u_update': {'enabled': m3u_update_enabled, 'playlists': []},
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


def _make_mock_udi(channel_id, channel_name, streams):
    udi = Mock()
    udi.get_channel_by_id.return_value = {
        'id': channel_id,
        'name': channel_name,
        'channel_group_id': None,
        'logo_id': None,
        'streams': [s['id'] for s in streams],
    }
    udi.get_streams.return_value = streams
    udi.get_stream_by_id.return_value = None
    udi.refresh_streams = Mock()
    udi.refresh_channels = Mock()
    udi.refresh_m3u_accounts = Mock()
    udi.refresh_channel_groups = Mock()
    udi.refresh_channel_by_id = Mock()
    return udi


def _make_mock_acm(profile):
    acm = Mock()
    acm.get_profile.return_value = None
    acm.get_effective_epg_scheduled_profile.return_value = None
    acm.get_effective_configuration.return_value = {'profile': profile, 'periods': []}
    return acm


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestSingleChannelM3uUpdateFlagDisabled(unittest.TestCase):
    """m3u_update.enabled = False must skip the provider fetch."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    @patch('stream_checker_service.get_udi_manager')
    @patch('stream_checker_service.StreamCheckConfig')
    @patch('apps.stream.stream_checker_service.get_automation_config_manager')
    @patch('apps.stream.stream_checker_service.get_session_manager')
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
        service._check_channel = Mock(return_value={'dead_streams_count': 0, 'revived_streams_count': 0, 'analyzed_streams': []})
        # Stub dead streams tracker
        service.dead_streams_tracker = Mock()
        service.dead_streams_tracker.get_dead_streams_for_channel.return_value = {}
        service.dead_streams_tracker.clear_dead_streams_for_channel = Mock()

        with patch('apps.core.api_utils.refresh_m3u_playlists') as mock_refresh, \
             patch('stream_checker_service._wait_for_udi_stream_count_stabilise') as mock_poll:
            service.check_single_channel(channel_id=channel_id)

        mock_refresh.assert_not_called(), "refresh_m3u_playlists must not be called when m3u_update.enabled=False"
        mock_poll.assert_not_called(), "_wait_for_udi_stream_count_stabilise must not be called when update disabled"

    @patch('stream_checker_service.get_udi_manager')
    @patch('stream_checker_service.StreamCheckConfig')
    @patch('apps.stream.stream_checker_service.get_automation_config_manager')
    @patch('apps.stream.stream_checker_service.get_session_manager')
    @patch('stream_checker_service.fetch_channel_streams')
    def test_m3u_update_disabled_still_syncs_udi_when_checking_enabled(
        self, mock_fetch, mock_session_mgr, mock_acm_factory,
        mock_config_class, mock_get_udi,
    ):
        """When m3u_update=False but checking=True, UDI sync calls must still happen."""
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
        service._check_channel = Mock(return_value={'dead_streams_count': 0, 'revived_streams_count': 0, 'analyzed_streams': []})
        service.dead_streams_tracker = Mock()
        service.dead_streams_tracker.get_dead_streams_for_channel.return_value = {}
        service.dead_streams_tracker.clear_dead_streams_for_channel = Mock()

        with patch('apps.core.api_utils.refresh_m3u_playlists'):
            service.check_single_channel(channel_id=channel_id)

        mock_udi.refresh_streams.assert_called(), "UDI refresh_streams must be called even when m3u_update=False"
        mock_udi.refresh_channels.assert_called(), "UDI refresh_channels must be called"
        mock_udi.refresh_m3u_accounts.assert_called(), "UDI refresh_m3u_accounts must be called"
        mock_udi.refresh_channel_groups.assert_called(), "UDI refresh_channel_groups must be called"

    @patch('stream_checker_service.get_udi_manager')
    @patch('stream_checker_service.StreamCheckConfig')
    @patch('apps.stream.stream_checker_service.get_automation_config_manager')
    @patch('apps.stream.stream_checker_service.get_session_manager')
    @patch('stream_checker_service.fetch_channel_streams')
    def test_m3u_update_disabled_still_syncs_udi_when_matching_enabled(
        self, mock_fetch, mock_session_mgr, mock_acm_factory,
        mock_config_class, mock_get_udi,
    ):
        """When m3u_update=False but matching=True, UDI sync must happen so matching sees fresh data."""
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
        service.dead_streams_tracker = Mock()
        service.dead_streams_tracker.get_dead_streams_for_channel.return_value = {}
        service.dead_streams_tracker.clear_dead_streams_for_channel = Mock()

        with patch('apps.core.api_utils.refresh_m3u_playlists'), \
             patch('automated_stream_manager.AutomatedStreamManager') as mock_asm:
            mock_asm.return_value.discover_and_assign_streams = Mock(return_value={})
            mock_asm.return_value.validate_and_remove_non_matching_streams = Mock(return_value={})
            service.check_single_channel(channel_id=channel_id)

        mock_udi.refresh_streams.assert_called()
        mock_udi.refresh_channels.assert_called()

    @patch('stream_checker_service.get_udi_manager')
    @patch('stream_checker_service.StreamCheckConfig')
    @patch('apps.stream.stream_checker_service.get_automation_config_manager')
    @patch('apps.stream.stream_checker_service.get_session_manager')
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
        service.dead_streams_tracker = Mock()
        service.dead_streams_tracker.get_dead_streams_for_channel.return_value = {}
        service.dead_streams_tracker.clear_dead_streams_for_channel = Mock()

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
    @patch('apps.stream.stream_checker_service.get_automation_config_manager')
    @patch('apps.stream.stream_checker_service.get_session_manager')
    @patch('stream_checker_service.fetch_channel_streams')
    def test_m3u_update_enabled_calls_refresh_then_syncs_udi(
        self, mock_fetch, mock_session_mgr, mock_acm_factory,
        mock_config_class, mock_get_udi,
    ):
        """When m3u_update=True: provider fetch fires, poll helper fires, then UDI sync fires."""
        from apps.stream.stream_checker_service import StreamCheckerService

        profile = _make_profile(m3u_update_enabled=True, matching_enabled=False, checking_enabled=True)
        channel_id = 46
        streams = [{'id': 5, 'url': 'http://x/5', 'm3u_account': 7, 'stream_stats': {}}]

        mock_config_class.return_value = _make_mock_config()
        mock_udi = _make_mock_udi(channel_id, 'Test Channel', streams)
        mock_get_udi.return_value = mock_udi
        mock_session_mgr.return_value.get_channels_in_active_sessions.return_value = []
        mock_acm_factory.return_value = _make_mock_acm(profile)
        mock_fetch.return_value = streams

        service = StreamCheckerService()
        service._check_channel = Mock(return_value={'dead_streams_count': 0, 'revived_streams_count': 0, 'analyzed_streams': []})
        service.dead_streams_tracker = Mock()
        service.dead_streams_tracker.get_dead_streams_for_channel.return_value = {}
        service.dead_streams_tracker.clear_dead_streams_for_channel = Mock()

        call_order = []

        with patch('apps.core.api_utils.refresh_m3u_playlists',
                   side_effect=lambda account_id=None: call_order.append('refresh')) as mock_refresh, \
             patch('stream_checker_service._wait_for_udi_stream_count_stabilise',
                   side_effect=lambda *a, **kw: call_order.append('poll') or True) as mock_poll:

            # Patch UDI sync methods to record order
            original_refresh_streams = mock_udi.refresh_streams
            mock_udi.refresh_streams.side_effect = lambda: call_order.append('udi_sync')

            service.check_single_channel(channel_id=channel_id)

        mock_refresh.assert_called_once_with(account_id=7)
        mock_poll.assert_called_once()

        # Verify order: refresh → poll → udi_sync
        refresh_idx = next((i for i, v in enumerate(call_order) if v == 'refresh'), -1)
        poll_idx = next((i for i, v in enumerate(call_order) if v == 'poll'), -1)
        udi_idx = next((i for i, v in enumerate(call_order) if v == 'udi_sync'), -1)

        self.assertGreater(poll_idx, refresh_idx, "Poll must happen after provider refresh")
        self.assertGreater(udi_idx, poll_idx, "UDI sync must happen after poll confirms completion")


class TestStep3DeadStreamClearIsUnconditional(unittest.TestCase):
    """Step 3 (dead stream clearing) must run regardless of flag state."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    @patch('stream_checker_service.get_udi_manager')
    @patch('stream_checker_service.StreamCheckConfig')
    @patch('apps.stream.stream_checker_service.get_automation_config_manager')
    @patch('apps.stream.stream_checker_service.get_session_manager')
    @patch('stream_checker_service.fetch_channel_streams')
    def test_dead_stream_clear_runs_with_all_flags_off(
        self, mock_fetch, mock_session_mgr, mock_acm_factory,
        mock_config_class, mock_get_udi,
    ):
        """Even with all three flags False, dead streams must be cleared (Step 3)."""
        from apps.stream.stream_checker_service import StreamCheckerService

        # All flags off — a no-op profile from the user's perspective, but
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
        mock_tracker = Mock()
        mock_tracker.get_dead_streams_for_channel.return_value = {}
        mock_tracker.clear_dead_streams_for_channel = Mock()
        service.dead_streams_tracker = mock_tracker

        with patch('apps.core.api_utils.refresh_m3u_playlists'):
            service.check_single_channel(channel_id=channel_id)

        mock_tracker.clear_dead_streams_for_channel.assert_called_with(channel_id)

    @patch('stream_checker_service.get_udi_manager')
    @patch('stream_checker_service.StreamCheckConfig')
    @patch('apps.stream.stream_checker_service.get_automation_config_manager')
    @patch('apps.stream.stream_checker_service.get_session_manager')
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
        service._check_channel = Mock(return_value={'dead_streams_count': 0, 'revived_streams_count': 0, 'analyzed_streams': []})
        mock_tracker = Mock()
        mock_tracker.get_dead_streams_for_channel.return_value = {}
        mock_tracker.clear_dead_streams_for_channel = Mock()
        service.dead_streams_tracker = mock_tracker

        with patch('apps.core.api_utils.refresh_m3u_playlists'):
            service.check_single_channel(channel_id=channel_id)

        mock_tracker.clear_dead_streams_for_channel.assert_called_with(channel_id)


class TestWaitForUdiStreamCountStabilise(unittest.TestCase):
    """Unit tests for _wait_for_udi_stream_count_stabilise helper."""

    def test_returns_true_when_count_changes(self):
        from apps.stream.stream_checker_service import _wait_for_udi_stream_count_stabilise
        import time

        mock_udi = Mock()
        # First poll: unchanged. Second poll: changed.
        mock_udi.get_streams.side_effect = [
            [{'id': i} for i in range(100)],   # poll 1 — same as pre_count
            [{'id': i} for i in range(105)],   # poll 2 — changed
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
        mock_udi.get_streams.return_value = [{'id': i} for i in range(100)]

        with patch('time.sleep'):
            result = _wait_for_udi_stream_count_stabilise(
                mock_udi, pre_count=100, timeout=10, poll_interval=5
            )

        self.assertFalse(result)

    def test_handles_udi_exception_gracefully(self):
        from apps.stream.stream_checker_service import _wait_for_udi_stream_count_stabilise

        mock_udi = Mock()
        # First call raises, second returns changed count
        mock_udi.get_streams.side_effect = [
            Exception("UDI unavailable"),
            [{'id': i} for i in range(105)],
        ]

        with patch('time.sleep'):
            result = _wait_for_udi_stream_count_stabilise(
                mock_udi, pre_count=100, timeout=30, poll_interval=5
            )

        # Should recover from the exception and detect the change on the second poll
        self.assertTrue(result)


if __name__ == '__main__':
    unittest.main(verbosity=2)
