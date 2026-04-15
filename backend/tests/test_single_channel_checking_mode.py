#!/usr/bin/env python3
"""
Test to verify that single channel check enforces the opt-in model:
- No profile assigned -> hard halt, structured error response
- Profile assigned, matching disabled -> skip matching, proceed with check
- Profile assigned, checking disabled -> skip checking, proceed
- Profile assigned, both enabled -> full check
- EPG-scheduled path with no profile -> hard halt (same guard, different entry point)
"""

import unittest
import tempfile
import shutil
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _make_mock_config(side_effect=None):
    mock_config = Mock()
    mock_config.get = Mock(side_effect=side_effect or (lambda key, default=None: default))
    return mock_config


def _make_mock_udi(channel_id, channel_name, streams=None):
    mock_udi_instance = Mock()
    mock_udi_instance.get_channel_by_id.return_value = {
        'id': channel_id,
        'name': channel_name,
        'channel_group_id': None,
        'logo_id': None,
    }
    mock_udi_instance.is_channel_active.return_value = False
    mock_udi_instance.refresh_streams = Mock()
    mock_udi_instance.refresh_channels = Mock()
    mock_udi_instance.get_streams = Mock(return_value=streams or [])
    return mock_udi_instance


def _make_profile(matching_enabled=True, checking_enabled=True):
    return {
        'name': 'Test Profile',
        'stream_matching': {'enabled': matching_enabled},
        'stream_checking': {'enabled': checking_enabled},
    }


class TestSingleChannelNoProfileGuard(unittest.TestCase):
    """Tests for the no-profile hard halt — the core opt-in enforcement."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    @patch('stream_checker_service.get_udi_manager')
    @patch('stream_checker_service.StreamCheckConfig')
    @patch('apps.stream.stream_checker_service.get_automation_config_manager')
    @patch('apps.stream.stream_checker_service.get_session_manager')
    def test_no_profile_returns_no_profile_error(
        self, mock_get_session_mgr, mock_get_acm, mock_config_class, mock_get_udi
    ):
        """When no automation period or EPG profile is assigned, check must hard-halt."""
        from apps.stream.stream_checker_service import StreamCheckerService

        channel_id = 101
        mock_config_class.return_value = _make_mock_config()
        mock_get_udi.return_value = _make_mock_udi(channel_id, 'ESPN')

        # No monitoring sessions
        mock_session_mgr = Mock()
        mock_session_mgr.get_channels_in_active_sessions.return_value = []
        mock_get_session_mgr.return_value = mock_session_mgr

        # No EPG profile, no period-based config -> profile resolves to None
        mock_acm = Mock()
        mock_acm.get_effective_epg_scheduled_profile.return_value = None
        mock_acm.get_effective_configuration.return_value = None
        mock_get_acm.return_value = mock_acm

        service = StreamCheckerService()
        result = service.check_single_channel(channel_id=channel_id)

        self.assertFalse(result.get('success'))
        self.assertEqual(result.get('error'), 'no_profile')
        self.assertIn('ESPN', result.get('message', ''))
        self.assertEqual(result.get('channel_id'), channel_id)

    @patch('stream_checker_service.get_udi_manager')
    @patch('stream_checker_service.StreamCheckConfig')
    @patch('apps.stream.stream_checker_service.get_automation_config_manager')
    @patch('apps.stream.stream_checker_service.get_session_manager')
    def test_no_profile_epg_path_also_hard_halts(
        self, mock_get_session_mgr, mock_get_acm, mock_config_class, mock_get_udi
    ):
        """EPG-scheduled path with no resolvable profile must also hard-halt."""
        from apps.stream.stream_checker_service import StreamCheckerService

        channel_id = 102
        mock_config_class.return_value = _make_mock_config()
        mock_get_udi.return_value = _make_mock_udi(channel_id, 'Sky Sports')

        mock_session_mgr = Mock()
        mock_session_mgr.get_channels_in_active_sessions.return_value = []
        mock_get_session_mgr.return_value = mock_session_mgr

        # No EPG override, no period config
        mock_acm = Mock()
        mock_acm.get_effective_epg_scheduled_profile.return_value = None
        mock_acm.get_effective_configuration.return_value = None
        mock_get_acm.return_value = mock_acm

        service = StreamCheckerService()
        result = service.check_single_channel(channel_id=channel_id, is_epg_scheduled=True)

        self.assertFalse(result.get('success'))
        self.assertEqual(result.get('error'), 'no_profile')

    @patch('stream_checker_service.get_udi_manager')
    @patch('stream_checker_service.StreamCheckConfig')
    @patch('apps.stream.stream_checker_service.get_automation_config_manager')
    @patch('apps.stream.stream_checker_service.get_session_manager')
    def test_epg_profile_override_allows_check(
        self, mock_get_session_mgr, mock_get_acm, mock_config_class, mock_get_udi
    ):
        """When EPG override profile is set, the check must proceed (not halt)."""
        from apps.stream.stream_checker_service import StreamCheckerService

        channel_id = 103
        mock_streams = [
            {'id': 1, 'url': 'http://example.com/1', 'm3u_account': 1,
             'stream_stats': {'status': 'ok'}},
        ]
        mock_config_class.return_value = _make_mock_config()
        mock_get_udi.return_value = _make_mock_udi(channel_id, 'BT Sport', mock_streams)

        mock_session_mgr = Mock()
        mock_session_mgr.get_channels_in_active_sessions.return_value = []
        mock_get_session_mgr.return_value = mock_session_mgr

        # EPG profile exists
        mock_acm = Mock()
        mock_acm.get_effective_epg_scheduled_profile.return_value = _make_profile()
        mock_get_acm.return_value = mock_acm

        service = StreamCheckerService()
        # Stub the rest of the pipeline to avoid full ffmpeg execution
        service._check_channel = Mock(return_value={'dead_streams_count': 0, 'revived_streams_count': 0})

        with patch('stream_checker_service.fetch_channel_streams', return_value=mock_streams), \
             patch('api_utils.refresh_m3u_playlists'), \
             patch('automated_stream_manager.AutomatedStreamManager') as mock_asm:
            mock_asm.return_value.discover_and_assign_streams = Mock(return_value={})
            result = service.check_single_channel(channel_id=channel_id, is_epg_scheduled=True)

        # Should not be a no_profile error
        self.assertNotEqual(result.get('error'), 'no_profile')

    @patch('stream_checker_service.get_udi_manager')
    @patch('stream_checker_service.StreamCheckConfig')
    @patch('apps.stream.stream_checker_service.get_automation_config_manager')
    @patch('apps.stream.stream_checker_service.get_session_manager')
    def test_period_profile_allows_check(
        self, mock_get_session_mgr, mock_get_acm, mock_config_class, mock_get_udi
    ):
        """When a period-based profile resolves, check must proceed (not halt)."""
        from apps.stream.stream_checker_service import StreamCheckerService

        channel_id = 104
        mock_streams = [
            {'id': 2, 'url': 'http://example.com/2', 'm3u_account': 1,
             'stream_stats': {'status': 'ok'}},
        ]
        mock_config_class.return_value = _make_mock_config()
        mock_get_udi.return_value = _make_mock_udi(channel_id, 'Fox News', mock_streams)

        mock_session_mgr = Mock()
        mock_session_mgr.get_channels_in_active_sessions.return_value = []
        mock_get_session_mgr.return_value = mock_session_mgr

        # No EPG override, but period config exists with profile
        mock_acm = Mock()
        mock_acm.get_effective_epg_scheduled_profile.return_value = None
        mock_acm.get_effective_configuration.return_value = {
            'profile': _make_profile(matching_enabled=True, checking_enabled=True),
            'periods': [],
        }
        mock_get_acm.return_value = mock_acm

        service = StreamCheckerService()
        service._check_channel = Mock(return_value={'dead_streams_count': 0, 'revived_streams_count': 0})

        with patch('stream_checker_service.fetch_channel_streams', return_value=mock_streams), \
             patch('api_utils.refresh_m3u_playlists'), \
             patch('automated_stream_manager.AutomatedStreamManager') as mock_asm:
            mock_asm.return_value.discover_and_assign_streams = Mock(return_value={})
            result = service.check_single_channel(channel_id=channel_id)

        self.assertNotEqual(result.get('error'), 'no_profile')

    @patch('stream_checker_service.get_udi_manager')
    @patch('stream_checker_service.StreamCheckConfig')
    @patch('apps.stream.stream_checker_service.get_automation_config_manager')
    @patch('apps.stream.stream_checker_service.get_session_manager')
    def test_config_with_none_profile_is_no_profile(
        self, mock_get_session_mgr, mock_get_acm, mock_config_class, mock_get_udi
    ):
        """Config dict exists but profile key is None -> still a no_profile halt."""
        from apps.stream.stream_checker_service import StreamCheckerService

        channel_id = 105
        mock_config_class.return_value = _make_mock_config()
        mock_get_udi.return_value = _make_mock_udi(channel_id, 'CNN')

        mock_session_mgr = Mock()
        mock_session_mgr.get_channels_in_active_sessions.return_value = []
        mock_get_session_mgr.return_value = mock_session_mgr

        # Config exists but profile is None (period assigned, no profile on assignment)
        mock_acm = Mock()
        mock_acm.get_effective_epg_scheduled_profile.return_value = None
        mock_acm.get_effective_configuration.return_value = {'profile': None, 'periods': []}
        mock_get_acm.return_value = mock_acm

        service = StreamCheckerService()
        result = service.check_single_channel(channel_id=channel_id)

        self.assertFalse(result.get('success'))
        self.assertEqual(result.get('error'), 'no_profile')

    @patch('stream_checker_service.get_udi_manager')
    @patch('stream_checker_service.StreamCheckConfig')
    @patch('apps.stream.stream_checker_service.get_automation_config_manager')
    @patch('apps.stream.stream_checker_service.get_session_manager')
    def test_no_profile_does_not_consult_global_controls(
        self, mock_get_session_mgr, mock_get_acm, mock_config_class, mock_get_udi
    ):
        """Global automation controls must never be used as profile fallback."""
        from apps.stream.stream_checker_service import StreamCheckerService

        channel_id = 106

        # Global controls say True/True — but no profile should still halt
        def config_side_effect(key, default=None):
            if key == 'automation_controls.auto_stream_matching':
                return True
            if key == 'automation_controls.auto_quality_checking':
                return True
            return default

        mock_config_class.return_value = _make_mock_config(side_effect=config_side_effect)
        mock_get_udi.return_value = _make_mock_udi(channel_id, 'MSNBC')

        mock_session_mgr = Mock()
        mock_session_mgr.get_channels_in_active_sessions.return_value = []
        mock_get_session_mgr.return_value = mock_session_mgr

        mock_acm = Mock()
        mock_acm.get_effective_epg_scheduled_profile.return_value = None
        mock_acm.get_effective_configuration.return_value = None
        mock_get_acm.return_value = mock_acm

        service = StreamCheckerService()
        # Ensure _check_channel is NOT called (would prove global controls were used)
        service._check_channel = Mock()

        result = service.check_single_channel(channel_id=channel_id)

        self.assertFalse(result.get('success'))
        self.assertEqual(result.get('error'), 'no_profile')
        service._check_channel.assert_not_called()


class TestSingleChannelProfileRespected(unittest.TestCase):
    """Tests that matching/checking flags from the resolved profile are honoured."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _setup_service_with_profile(self, channel_id, channel_name, profile,
                                     mock_config_class, mock_get_udi,
                                     mock_get_acm, mock_get_session_mgr,
                                     streams=None):
        from apps.stream.stream_checker_service import StreamCheckerService

        mock_streams = streams or [
            {'id': 1, 'url': 'http://example.com/1', 'm3u_account': 1,
             'stream_stats': {'status': 'ok'}},
        ]
        mock_config_class.return_value = _make_mock_config()
        mock_get_udi.return_value = _make_mock_udi(channel_id, channel_name, mock_streams)

        mock_session_mgr = Mock()
        mock_session_mgr.get_channels_in_active_sessions.return_value = []
        mock_get_session_mgr.return_value = mock_session_mgr

        mock_acm = Mock()
        mock_acm.get_effective_epg_scheduled_profile.return_value = None
        mock_acm.get_effective_configuration.return_value = {
            'profile': profile,
            'periods': [],
        }
        mock_get_acm.return_value = mock_acm

        service = StreamCheckerService()
        service._check_channel = Mock(return_value={
            'dead_streams_count': 0, 'revived_streams_count': 0
        })
        return service, mock_streams

    @patch('stream_checker_service.get_udi_manager')
    @patch('stream_checker_service.StreamCheckConfig')
    @patch('apps.stream.stream_checker_service.get_automation_config_manager')
    @patch('apps.stream.stream_checker_service.get_session_manager')
    def test_checking_disabled_in_profile_skips_check(
        self, mock_get_session_mgr, mock_get_acm, mock_config_class, mock_get_udi
    ):
        """Profile with stream_checking disabled must skip _check_channel."""
        profile = _make_profile(matching_enabled=True, checking_enabled=False)
        service, mock_streams = self._setup_service_with_profile(
            200, 'Test Channel', profile,
            mock_config_class, mock_get_udi, mock_get_acm, mock_get_session_mgr
        )

        with patch('stream_checker_service.fetch_channel_streams', return_value=mock_streams), \
             patch('api_utils.refresh_m3u_playlists'), \
             patch('automated_stream_manager.AutomatedStreamManager') as mock_asm:
            mock_asm.return_value.discover_and_assign_streams = Mock(return_value={})
            result = service.check_single_channel(channel_id=200)

        self.assertNotEqual(result.get('error'), 'no_profile')
        service._check_channel.assert_not_called()

    @patch('stream_checker_service.get_udi_manager')
    @patch('stream_checker_service.StreamCheckConfig')
    @patch('apps.stream.stream_checker_service.get_automation_config_manager')
    @patch('apps.stream.stream_checker_service.get_session_manager')
    def test_checking_enabled_in_profile_runs_check(
        self, mock_get_session_mgr, mock_get_acm, mock_config_class, mock_get_udi
    ):
        """Profile with stream_checking enabled must call _check_channel."""
        profile = _make_profile(matching_enabled=False, checking_enabled=True)
        service, mock_streams = self._setup_service_with_profile(
            201, 'Test Channel 2', profile,
            mock_config_class, mock_get_udi, mock_get_acm, mock_get_session_mgr
        )

        with patch('stream_checker_service.fetch_channel_streams', return_value=mock_streams), \
             patch('api_utils.refresh_m3u_playlists'), \
             patch('automated_stream_manager.AutomatedStreamManager') as mock_asm:
            mock_asm.return_value.discover_and_assign_streams = Mock(return_value={})
            result = service.check_single_channel(channel_id=201)

        self.assertNotEqual(result.get('error'), 'no_profile')
        service._check_channel.assert_called_once()


class TestSingleChannelHandlerNoProfileResponse(unittest.TestCase):
    """Tests that the stream_checker_handlers layer surfaces no_profile cleanly."""

    def test_handler_returns_400_for_no_profile(self):
        """Handler must return 400 (not 500) when backend signals no_profile."""
        from apps.api.stream_checker_handlers import check_single_channel_now_response

        mock_service = Mock()
        mock_service.check_single_channel.return_value = {
            'success': False,
            'error': 'no_profile',
            'message': 'Channel ESPN has no automation profile assigned.',
            'channel_id': 1,
            'channel_name': 'ESPN',
        }

        result = check_single_channel_now_response(
            payload={'channel_id': 1},
            get_stream_checker_service=lambda: mock_service,
        )

        # check_single_channel_now_response returns a tuple (response, status_code)
        response, status_code = result if isinstance(result, tuple) else (result, 200)
        self.assertEqual(status_code, 400)

        import json as json_mod
        data = json_mod.loads(response.get_data(as_text=True))
        self.assertEqual(data.get('error'), 'no_profile')



class TestSingleChannelForcedProfileId(unittest.TestCase):
    """Tests for scenario 3 — multi-period channel with explicit profile selection via picker."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    @patch('stream_checker_service.get_udi_manager')
    @patch('stream_checker_service.StreamCheckConfig')
    @patch('apps.stream.stream_checker_service.get_automation_config_manager')
    @patch('apps.stream.stream_checker_service.get_session_manager')
    def test_forced_profile_id_bypasses_period_resolution(
        self, mock_get_session_mgr, mock_get_acm, mock_config_class, mock_get_udi
    ):
        """When forced_profile_id is provided the service must use that profile directly,
        skipping both EPG override and active-period resolution."""
        from apps.stream.stream_checker_service import StreamCheckerService

        channel_id = 301
        forced_id = 'profile-abc'
        forced_profile = _make_profile(matching_enabled=True, checking_enabled=True)
        forced_profile['name'] = 'Picker Selected Profile'

        mock_streams = [
            {'id': 1, 'url': 'http://example.com/1', 'm3u_account': 1,
             'stream_stats': {'status': 'ok'}},
        ]
        mock_config_class.return_value = _make_mock_config()
        mock_get_udi.return_value = _make_mock_udi(channel_id, 'Multi-Period Channel', mock_streams)

        mock_session_mgr = Mock()
        mock_session_mgr.get_channels_in_active_sessions.return_value = []
        mock_get_session_mgr.return_value = mock_session_mgr

        mock_acm = Mock()
        # get_profile called with forced_id should return the profile
        mock_acm.get_profile.return_value = forced_profile
        # These should NOT be called when forced_profile_id resolves
        mock_acm.get_effective_epg_scheduled_profile.return_value = None
        mock_acm.get_effective_configuration.return_value = None
        mock_get_acm.return_value = mock_acm

        service = StreamCheckerService()
        service._check_channel = Mock(return_value={'dead_streams_count': 0, 'revived_streams_count': 0})

        with patch('stream_checker_service.fetch_channel_streams', return_value=mock_streams),              patch('api_utils.refresh_m3u_playlists'),              patch('automated_stream_manager.AutomatedStreamManager') as mock_asm:
            mock_asm.return_value.discover_and_assign_streams = Mock(return_value={})
            result = service.check_single_channel(
                channel_id=channel_id,
                forced_profile_id=forced_id,
            )

        # Must not be a no_profile error
        self.assertNotEqual(result.get('error'), 'no_profile',
                            "Forced profile should prevent no_profile error")

        # get_profile must have been called with the forced_id
        mock_acm.get_profile.assert_called_once_with(forced_id)

        # EPG and period resolution must NOT have been called
        mock_acm.get_effective_epg_scheduled_profile.assert_not_called()
        mock_acm.get_effective_configuration.assert_not_called()

    @patch('stream_checker_service.get_udi_manager')
    @patch('stream_checker_service.StreamCheckConfig')
    @patch('apps.stream.stream_checker_service.get_automation_config_manager')
    @patch('apps.stream.stream_checker_service.get_session_manager')
    def test_forced_profile_id_not_found_falls_back_to_period_resolution(
        self, mock_get_session_mgr, mock_get_acm, mock_config_class, mock_get_udi
    ):
        """If forced_profile_id does not resolve (deleted profile), fall back to
        standard period resolution rather than hard-halting."""
        from apps.stream.stream_checker_service import StreamCheckerService

        channel_id = 302
        forced_id = 'deleted-profile-id'

        mock_streams = [
            {'id': 1, 'url': 'http://example.com/1', 'm3u_account': 1,
             'stream_stats': {'status': 'ok'}},
        ]
        mock_config_class.return_value = _make_mock_config()
        mock_get_udi.return_value = _make_mock_udi(channel_id, 'Channel With Deleted Profile', mock_streams)

        mock_session_mgr = Mock()
        mock_session_mgr.get_channels_in_active_sessions.return_value = []
        mock_get_session_mgr.return_value = mock_session_mgr

        mock_acm = Mock()
        # forced_profile_id doesn't resolve
        mock_acm.get_profile.return_value = None
        # But a period-based profile exists as fallback
        mock_acm.get_effective_epg_scheduled_profile.return_value = None
        mock_acm.get_effective_configuration.return_value = {
            'profile': _make_profile(matching_enabled=True, checking_enabled=True),
            'periods': [],
        }
        mock_get_acm.return_value = mock_acm

        service = StreamCheckerService()
        service._check_channel = Mock(return_value={'dead_streams_count': 0, 'revived_streams_count': 0})

        with patch('stream_checker_service.fetch_channel_streams', return_value=mock_streams),              patch('api_utils.refresh_m3u_playlists'),              patch('automated_stream_manager.AutomatedStreamManager') as mock_asm:
            mock_asm.return_value.discover_and_assign_streams = Mock(return_value={})
            result = service.check_single_channel(
                channel_id=channel_id,
                forced_profile_id=forced_id,
            )

        # Should not hard-halt — period profile is the fallback
        self.assertNotEqual(result.get('error'), 'no_profile')
        # get_effective_configuration must have been called as fallback
        mock_acm.get_effective_configuration.assert_called_once()

    @patch('stream_checker_service.get_udi_manager')
    @patch('stream_checker_service.StreamCheckConfig')
    @patch('apps.stream.stream_checker_service.get_automation_config_manager')
    @patch('apps.stream.stream_checker_service.get_session_manager')
    def test_forced_profile_id_none_uses_normal_resolution(
        self, mock_get_session_mgr, mock_get_acm, mock_config_class, mock_get_udi
    ):
        """forced_profile_id=None must behave identically to not passing it —
        normal EPG/period resolution applies."""
        from apps.stream.stream_checker_service import StreamCheckerService

        channel_id = 303
        mock_config_class.return_value = _make_mock_config()
        mock_get_udi.return_value = _make_mock_udi(channel_id, 'Normal Channel')

        mock_session_mgr = Mock()
        mock_session_mgr.get_channels_in_active_sessions.return_value = []
        mock_get_session_mgr.return_value = mock_session_mgr

        mock_acm = Mock()
        mock_acm.get_effective_epg_scheduled_profile.return_value = None
        mock_acm.get_effective_configuration.return_value = None
        mock_get_acm.return_value = mock_acm

        service = StreamCheckerService()
        result = service.check_single_channel(channel_id=channel_id, forced_profile_id=None)

        # No forced_profile_id and no configured profile — must hard-halt
        self.assertEqual(result.get('error'), 'no_profile')
        # get_profile must NOT have been called (forced_profile_id is None/falsy)
        mock_acm.get_profile.assert_not_called()



if __name__ == '__main__':
    unittest.main(verbosity=2)
