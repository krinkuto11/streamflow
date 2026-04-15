#!/usr/bin/env python3
"""
Tests for checking-only channel queue gap fix in _discover_and_assign_streams_impl().

Covers:
  - Channels with matching=False, checking=True are queued for the checker
    after the matching pass completes
  - Those channels are NOT included in the matching/assignment pass itself
  - Channels with both matching=False and checking=False are not queued
  - forced_period_id filtering is respected for checking-only collection
  - Mixed-profile cycles (some matching+checking, some checking-only) both complete
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
# Helpers
# ---------------------------------------------------------------------------

def _make_profile(matching_enabled=True, checking_enabled=True):
    return {
        'name': 'Test Profile',
        'm3u_update': {'enabled': False, 'playlists': []},
        'stream_matching': {'enabled': matching_enabled},
        'stream_checking': {'enabled': checking_enabled, 'grace_period': False, 'allow_revive': False},
        'global_action': {'affected': False},
    }


def _make_channel(channel_id, name='Channel', group_id=None):
    return {
        'id': channel_id,
        'name': name,
        'channel_group_id': group_id,
        'group_id': group_id,
        'streams': [],
        'tvg_id': f'ch.{channel_id}',
    }


class TestCheckingOnlyChannelQueueGap(unittest.TestCase):
    """Channels with matching=False, checking=True must be queued after matching pass."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _build_manager(self):
        from apps.automation.automated_stream_manager import AutomatedStreamManager
        with patch('automated_stream_manager.CONFIG_DIR', Path(self.temp_dir)):
            mgr = AutomatedStreamManager()
        mgr.changelog = Mock()
        mgr._lock = __import__('threading').Lock()
        return mgr

    @patch('automated_stream_manager.get_udi_manager')
    @patch('automated_stream_manager.get_automation_config_manager')
    @patch('apps.stream.stream_checker_service.get_stream_checker_service')
    def test_checking_only_channels_queued_after_matching_pass(
        self, mock_get_scs, mock_get_acm, mock_get_udi,
    ):
        """Channels with matching=False, checking=True appear in mark_channels_updated."""
        matching_profile = _make_profile(matching_enabled=True, checking_enabled=True)
        checking_only_profile = _make_profile(matching_enabled=False, checking_enabled=True)

        ch_matching = _make_channel(101, 'Matching Channel')
        ch_checking = _make_channel(102, 'Checking Only Channel')

        mock_udi = Mock()
        mock_udi.get_channels.return_value = [ch_matching, ch_checking]
        mock_udi.get_streams.return_value = []
        mock_udi.get_m3u_accounts.return_value = []
        mock_udi.get_channel_by_id.side_effect = lambda cid: (
            ch_matching if cid == 101 else ch_checking
        )
        mock_get_udi.return_value = mock_udi

        mock_acm = Mock()

        def _effective_config(channel_id, group_id=None):
            profile = matching_profile if channel_id == 101 else checking_only_profile
            return {
                'profile': profile,
                'periods': [{'id': 'p1', 'profile': profile}],
                'period_id': 'p1',
            }

        mock_acm.get_effective_configuration.side_effect = _effective_config
        mock_get_acm.return_value = mock_acm

        mock_tracker = Mock()
        mock_scs = Mock()
        mock_scs.update_tracker = mock_tracker
        mock_get_scs.return_value = mock_scs

        mgr = self._build_manager()
        mgr.regex_matcher = Mock()
        mgr.regex_matcher.get_patterns.return_value = {'patterns': {}, 'global_settings': {}}

        with patch.object(mgr, 'validate_and_remove_non_matching_streams', return_value={'details': []}), \
             patch('automated_stream_manager.get_streams', return_value=[]), \
             patch('automated_stream_manager.add_streams_to_channel', return_value=True):

            mgr._discover_and_assign_streams_impl(
                force=True, skip_check_trigger=False, channel_id=None
            )

        # mark_channels_updated must have been called at some point for channel 102
        all_calls = mock_tracker.mark_channels_updated.call_args_list
        all_marked_ids = []
        for c in all_calls:
            args = c[0]
            if args:
                all_marked_ids.extend(args[0])

        self.assertIn(102, all_marked_ids,
                      "Checking-only channel 102 must be marked for quality checking")

    @patch('automated_stream_manager.get_udi_manager')
    @patch('automated_stream_manager.get_automation_config_manager')
    @patch('apps.stream.stream_checker_service.get_stream_checker_service')
    def test_checking_only_channels_excluded_from_matching_pass(
        self, mock_get_scs, mock_get_acm, mock_get_udi,
    ):
        """Channels with matching=False must NOT appear in stream assignment calls."""
        matching_profile = _make_profile(matching_enabled=True, checking_enabled=True)
        checking_only_profile = _make_profile(matching_enabled=False, checking_enabled=True)

        ch_matching = _make_channel(201)
        ch_checking = _make_channel(202)

        mock_udi = Mock()
        mock_udi.get_channels.return_value = [ch_matching, ch_checking]
        mock_udi.get_streams.return_value = []
        mock_udi.get_m3u_accounts.return_value = []
        mock_udi.get_channel_by_id.side_effect = lambda cid: (
            ch_matching if cid == 201 else ch_checking
        )
        mock_get_udi.return_value = mock_udi

        mock_acm = Mock()
        mock_acm.get_effective_configuration.side_effect = lambda cid, gid=None: {
            'profile': matching_profile if cid == 201 else checking_only_profile,
            'periods': [{'id': 'p1', 'profile': matching_profile if cid == 201 else checking_only_profile}],
        }
        mock_get_acm.return_value = mock_acm

        mock_get_scs.return_value = Mock()
        mock_get_scs.return_value.update_tracker = Mock()

        assigned_channel_ids = []

        def _mock_add_streams(channel_id, stream_ids, **kwargs):
            assigned_channel_ids.append(channel_id)
            return True

        mgr = self._build_manager()
        mgr.regex_matcher = Mock()
        mgr.regex_matcher.get_patterns.return_value = {'patterns': {}, 'global_settings': {}}

        with patch.object(mgr, 'validate_and_remove_non_matching_streams', return_value={'details': []}), \
             patch('automated_stream_manager.get_streams', return_value=[]), \
             patch('automated_stream_manager.add_streams_to_channel', side_effect=_mock_add_streams):

            mgr._discover_and_assign_streams_impl(force=True, skip_check_trigger=True)

        self.assertNotIn(202, assigned_channel_ids,
                         "Checking-only channel 202 must not appear in stream assignment calls")

    @patch('automated_stream_manager.get_udi_manager')
    @patch('automated_stream_manager.get_automation_config_manager')
    @patch('apps.stream.stream_checker_service.get_stream_checker_service')
    def test_matching_and_checking_false_not_queued(
        self, mock_get_scs, mock_get_acm, mock_get_udi,
    ):
        """Channels with both matching=False and checking=False must not be queued."""
        noop_profile = _make_profile(matching_enabled=False, checking_enabled=False)
        ch = _make_channel(301)

        mock_udi = Mock()
        mock_udi.get_channels.return_value = [ch]
        mock_udi.get_streams.return_value = []
        mock_udi.get_m3u_accounts.return_value = []
        mock_udi.get_channel_by_id.return_value = ch
        mock_get_udi.return_value = mock_udi

        mock_acm = Mock()
        mock_acm.get_effective_configuration.return_value = {
            'profile': noop_profile,
            'periods': [{'id': 'p1', 'profile': noop_profile}],
        }
        mock_get_acm.return_value = mock_acm

        mock_tracker = Mock()
        mock_scs = Mock()
        mock_scs.update_tracker = mock_tracker
        mock_get_scs.return_value = mock_scs

        mgr = self._build_manager()
        mgr.regex_matcher = Mock()
        mgr.regex_matcher.get_patterns.return_value = {'patterns': {}, 'global_settings': {}}

        with patch.object(mgr, 'validate_and_remove_non_matching_streams', return_value={'details': []}), \
             patch('automated_stream_manager.get_streams', return_value=[]), \
             patch('automated_stream_manager.add_streams_to_channel', return_value=True):

            mgr._discover_and_assign_streams_impl(force=True, skip_check_trigger=True)

        # mark_channels_updated should not have been called with channel 301
        all_calls = mock_tracker.mark_channels_updated.call_args_list
        all_marked_ids = []
        for c in all_calls:
            args = c[0]
            if args:
                all_marked_ids.extend(args[0])

        self.assertNotIn(301, all_marked_ids,
                         "No-op channel 301 must not be queued for checking")

    @patch('automated_stream_manager.get_udi_manager')
    @patch('automated_stream_manager.get_automation_config_manager')
    @patch('apps.stream.stream_checker_service.get_stream_checker_service')
    def test_forced_period_id_respected_for_checking_only_collection(
        self, mock_get_scs, mock_get_acm, mock_get_udi,
    ):
        """forced_period_id must filter checking-only collection the same as matching pass."""
        period_a = 'period-a'
        period_b = 'period-b'

        profile_a = _make_profile(matching_enabled=False, checking_enabled=True)
        profile_b = _make_profile(matching_enabled=False, checking_enabled=True)

        ch_a = _make_channel(401)  # belongs to period_a
        ch_b = _make_channel(402)  # belongs to period_b

        mock_udi = Mock()
        mock_udi.get_channels.return_value = [ch_a, ch_b]
        mock_udi.get_streams.return_value = []
        mock_udi.get_m3u_accounts.return_value = []
        mock_udi.get_channel_by_id.side_effect = lambda cid: (
            ch_a if cid == 401 else ch_b
        )
        mock_get_udi.return_value = mock_udi

        def _config(cid, gid=None):
            if cid == 401:
                return {
                    'profile': profile_a,
                    'periods': [{'id': period_a, 'profile': profile_a}],
                }
            return {
                'profile': profile_b,
                'periods': [{'id': period_b, 'profile': profile_b}],
            }

        mock_acm = Mock()
        mock_acm.get_effective_configuration.side_effect = _config
        mock_get_acm.return_value = mock_acm

        mock_tracker = Mock()
        mock_scs = Mock()
        mock_scs.update_tracker = mock_tracker
        mock_get_scs.return_value = mock_scs

        mgr = self._build_manager()
        mgr.regex_matcher = Mock()
        mgr.regex_matcher.get_patterns.return_value = {'patterns': {}, 'global_settings': {}}

        with patch.object(mgr, 'validate_and_remove_non_matching_streams', return_value={'details': []}), \
             patch('automated_stream_manager.get_streams', return_value=[]), \
             patch('automated_stream_manager.add_streams_to_channel', return_value=True):

            # Force only period_a to run
            mgr._discover_and_assign_streams_impl(
                force=True,
                skip_check_trigger=True,
                forced_period_id=period_a,
            )

        all_calls = mock_tracker.mark_channels_updated.call_args_list
        all_marked_ids = []
        for c in all_calls:
            args = c[0]
            if args:
                all_marked_ids.extend(args[0])

        self.assertIn(401, all_marked_ids,
                      "Channel 401 (period_a) must be queued when forced_period_id=period_a")
        self.assertNotIn(402, all_marked_ids,
                         "Channel 402 (period_b) must NOT be queued when forced_period_id=period_a")

    @patch('automated_stream_manager.get_udi_manager')
    @patch('automated_stream_manager.get_automation_config_manager')
    @patch('apps.stream.stream_checker_service.get_stream_checker_service')
    def test_skip_check_trigger_respected_for_checking_only(
        self, mock_get_scs, mock_get_acm, mock_get_udi,
    ):
        """When skip_check_trigger=True, trigger_check_updated_channels must not be called."""
        checking_only_profile = _make_profile(matching_enabled=False, checking_enabled=True)
        ch = _make_channel(501)

        mock_udi = Mock()
        mock_udi.get_channels.return_value = [ch]
        mock_udi.get_streams.return_value = []
        mock_udi.get_m3u_accounts.return_value = []
        mock_udi.get_channel_by_id.return_value = ch
        mock_get_udi.return_value = mock_udi

        mock_acm = Mock()
        mock_acm.get_effective_configuration.return_value = {
            'profile': checking_only_profile,
            'periods': [{'id': 'p1', 'profile': checking_only_profile}],
        }
        mock_get_acm.return_value = mock_acm

        mock_tracker = Mock()
        mock_scs = Mock()
        mock_scs.update_tracker = mock_tracker
        mock_scs.trigger_check_updated_channels = Mock()
        mock_get_scs.return_value = mock_scs

        mgr = self._build_manager()
        mgr.regex_matcher = Mock()
        mgr.regex_matcher.get_patterns.return_value = {'patterns': {}, 'global_settings': {}}

        with patch.object(mgr, 'validate_and_remove_non_matching_streams', return_value={'details': []}), \
             patch('automated_stream_manager.get_streams', return_value=[]), \
             patch('automated_stream_manager.add_streams_to_channel', return_value=True):

            mgr._discover_and_assign_streams_impl(
                force=True, skip_check_trigger=True
            )

        mock_scs.trigger_check_updated_channels.assert_not_called()


if __name__ == '__main__':
    unittest.main(verbosity=2)
