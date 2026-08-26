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
import threading
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
             patch.object(service, '_require_quality_check_connectivity', return_value=None), \
             patch('automated_stream_manager.AutomatedStreamManager') as mock_asm:
            mock_asm.return_value.discover_and_assign_streams = Mock(return_value={})
            result = service.check_single_channel(channel_id=201)

        self.assertNotEqual(result.get('error'), 'no_profile')
        service._check_channel.assert_called_once()

    @patch('stream_checker_service.get_udi_manager')
    @patch('stream_checker_service.StreamCheckConfig')
    @patch('apps.stream.stream_checker_service.get_automation_config_manager')
    @patch('apps.stream.stream_checker_service.get_session_manager')
    def test_late_abort_during_final_progress_clear_does_not_return_success_or_sync(
        self, mock_get_session_mgr, mock_get_acm, mock_config_class, mock_get_udi
    ):
        profile = _make_profile(matching_enabled=False, checking_enabled=True)
        service, mock_streams = self._setup_service_with_profile(
            202,
            'Late Abort Channel',
            profile,
            mock_config_class,
            mock_get_udi,
            mock_get_acm,
            mock_get_session_mgr,
        )
        mock_get_udi.return_value.is_network_ready.return_value = True
        service.progress.clear = Mock(
            side_effect=lambda: service.request_abort('late_finalization_test')
        )

        with (
            patch('stream_checker_service.fetch_channel_streams', return_value=mock_streams),
            patch('api_utils.refresh_m3u_playlists'),
            patch.object(service, '_require_quality_check_connectivity', return_value=None),
            patch('apps.stream.stream_checker_service.threading.Thread') as thread_class,
        ):
            result = service.check_single_channel(channel_id=202)

        self.assertFalse(result['success'])
        self.assertEqual(result['error'], 'aborted')
        self.assertTrue(result['aborted'])
        thread_class.assert_not_called()


class TestSingleChannelHandlerNoProfileResponse(unittest.TestCase):
    """Tests that the stream_checker_handlers layer surfaces no_profile cleanly."""

    def test_handler_returns_400_for_no_profile(self):
        """Handler must return 400 (not 500) when backend signals no_profile."""
        from flask import Flask
        from apps.api.stream_checker_handlers import check_single_channel_now_response

        mock_service = Mock()
        mock_service.check_single_channel.return_value = {
            'success': False,
            'error': 'no_profile',
            'message': 'Channel ESPN has no automation profile assigned.',
            'channel_id': 1,
            'channel_name': 'ESPN',
        }

        app = Flask(__name__)
        with app.app_context():
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

    def test_handler_returns_200_for_guard_skip(self):
        """Handler must not surface intentional guard skips as HTTP 500."""
        from flask import Flask
        from apps.api.stream_checker_handlers import check_single_channel_now_response

        mock_service = Mock()
        mock_service.check_single_channel.return_value = {
            'success': True,
            'skipped': True,
            'reason': 'active_viewers',
            'message': 'Channel check skipped: active_viewers',
            'channel_id': 1,
            'channel_name': 'Das Erste HD',
            'details': {
                'skipped': True,
                'skip_reason': 'active_viewers',
            },
        }

        app = Flask(__name__)
        with app.app_context():
            result = check_single_channel_now_response(
                payload={'channel_id': 1},
                get_stream_checker_service=lambda: mock_service,
            )

        response, status_code = result if isinstance(result, tuple) else (result, 200)
        self.assertEqual(status_code, 200)

        import json as json_mod
        data = json_mod.loads(response.get_data(as_text=True))
        self.assertTrue(data.get('skipped'))
        self.assertEqual(data.get('reason'), 'active_viewers')

    def test_handler_blocks_immediate_check_during_active_automation_run(self):
        """Immediate full checks must not run in parallel with an automation cycle."""
        from flask import Flask
        from apps.api.stream_checker_handlers import check_single_channel_now_response

        mock_service = Mock()
        mock_service.get_status.return_value = {'checking': False, 'queue': {}, 'progress': {}}
        mock_manager = Mock()
        mock_manager.get_run_status.return_value = {'active': True, 'state': 'running'}

        app = Flask(__name__)
        with app.app_context():
            result = check_single_channel_now_response(
                payload={'channel_id': 1},
                get_stream_checker_service=lambda: mock_service,
                get_automation_manager=lambda: mock_manager,
            )

        response, status_code = result if isinstance(result, tuple) else (result, 200)
        self.assertEqual(status_code, 409)

        import json as json_mod
        data = json_mod.loads(response.get_data(as_text=True))
        self.assertEqual(data.get('error'), 'automation_run_active')
        mock_service.check_single_channel.assert_not_called()

    def test_handler_allows_immediate_check_when_automation_service_is_idle(self):
        """The background automation service thread alone must not block manual checks."""
        from flask import Flask
        from apps.api.stream_checker_handlers import check_single_channel_now_response

        mock_service = Mock()
        mock_service.get_status.return_value = {'checking': False, 'queue': {}, 'progress': {}}
        mock_service.check_single_channel.return_value = {
            'success': True,
            'channel_id': 1,
            'channel_name': 'Das Erste HD',
            'stats': {'total_streams': 1, 'dead_streams': 0},
        }
        mock_manager = Mock()
        mock_manager.get_run_status.return_value = {'active': False, 'state': 'skipped'}
        mock_manager.automation_running = True
        mock_thread = Mock()
        mock_thread.is_alive.return_value = True
        mock_manager.automation_thread = mock_thread

        app = Flask(__name__)
        with app.app_context():
            result = check_single_channel_now_response(
                payload={'channel_id': 1},
                get_stream_checker_service=lambda: mock_service,
                get_automation_manager=lambda: mock_manager,
            )

        response, status_code = result if isinstance(result, tuple) else (result, 200)
        self.assertEqual(status_code, 200)
        mock_service.check_single_channel.assert_called_once()

    def test_handler_blocks_immediate_check_during_active_stream_checker(self):
        """A second immediate full check must not overlap an active Stream Checker run."""
        from flask import Flask
        from apps.api.stream_checker_handlers import check_single_channel_now_response

        mock_service = Mock()
        mock_service.get_status.return_value = {
            'checking': False,
            'queue': {'queue_size': 0, 'in_progress': 0},
            'progress': {'is_single_channel_check': True},
        }

        app = Flask(__name__)
        with app.app_context():
            result = check_single_channel_now_response(
                payload={'channel_id': 1},
                get_stream_checker_service=lambda: mock_service,
            )

        response, status_code = result if isinstance(result, tuple) else (result, 200)
        self.assertEqual(status_code, 409)

        import json as json_mod
        data = json_mod.loads(response.get_data(as_text=True))
        self.assertEqual(data.get('error'), 'stream_checker_active')
        mock_service.check_single_channel.assert_not_called()

    def test_handler_allows_immediate_check_when_only_progress_is_stale(self):
        """Stale single-channel progress must not block a new immediate full check."""
        from flask import Flask
        from apps.api.stream_checker_handlers import check_single_channel_now_response

        mock_service = Mock()
        mock_service.get_status.return_value = {
            'checking': False,
            'stream_checking_mode': False,
            'progress_stale': True,
            'queue': {'queue_size': 0, 'in_progress': 0, 'current_channel': None},
            'progress': {
                'is_single_channel_check': True,
                'stale': True,
                'stale_reason': 'no_active_worker',
            },
        }
        mock_service.check_single_channel.return_value = {'success': True, 'channel_id': 1}

        app = Flask(__name__)
        with app.app_context():
            result = check_single_channel_now_response(
                payload={'channel_id': 1},
                get_stream_checker_service=lambda: mock_service,
            )

        response, status_code = result if isinstance(result, tuple) else (result, 200)
        self.assertEqual(status_code, 200)
        self.assertEqual(response.get_json().get('success'), True)
        mock_service.check_single_channel.assert_called_once_with(
            1,
            forced_profile_id=None,
            force_check=False,
        )

    def test_handler_sanitizes_unexpected_service_errors(self):
        """Handler must not expose internal exception details from service results."""
        from flask import Flask
        from apps.api.stream_checker_handlers import check_single_channel_now_response

        mock_service = Mock()
        mock_service.check_single_channel.return_value = {
            "success": "Traceback with /app/data/secret/path",
            "skipped": {"exception": "Traceback with /app/data/secret/path"},
            "error": "Traceback with /app/data/secret/path",
            "channel_id": 1,
        }

        app = Flask(__name__)
        with app.app_context():
            result = check_single_channel_now_response(
                payload={"channel_id": 1},
                get_stream_checker_service=lambda: mock_service,
            )

        response, status_code = result if isinstance(result, tuple) else (result, 200)
        self.assertEqual(status_code, 500)

        import json as json_mod

        data = json_mod.loads(response.get_data(as_text=True))
        self.assertEqual(data.get("error"), "Internal Server Error")
        self.assertNotIn("secret", response.get_data(as_text=True))

    def test_handler_rebuilds_known_service_errors_without_internal_details(self):
        """Known status mappings must not echo arbitrary service-result fields."""
        from flask import Flask
        from apps.api.stream_checker_handlers import check_single_channel_now_response

        secret = "Traceback with /app/data/secret/path"
        cases = (
            ("aborted", 409),
            ("no_profile", 400),
            ("stream_checker_active", 409),
        )
        app = Flask(__name__)

        for error_code, expected_status in cases:
            with self.subTest(error_code=error_code):
                mock_service = Mock()
                mock_service.get_status.return_value = {
                    "checking": False,
                    "queue": {},
                    "progress": {},
                }
                mock_service.check_single_channel.return_value = {
                    "success": secret,
                    "skipped": {"exception": secret},
                    "error": error_code,
                    "message": secret,
                    "traceback": secret,
                    "analysis": {"exception": secret},
                    "channel_id": 1,
                }

                with app.app_context():
                    response, status_code = check_single_channel_now_response(
                        payload={"channel_id": 1},
                        get_stream_checker_service=lambda: mock_service,
                    )

                self.assertEqual(status_code, expected_status)
                self.assertEqual(response.get_json()["error"], error_code)
                self.assertNotIn("secret", response.get_data(as_text=True))
                self.assertNotIn("traceback", response.get_json())
                self.assertNotIn("analysis", response.get_json())
                if error_code == "aborted":
                    self.assertEqual(
                        {
                            key: response.get_json()[key]
                            for key in (
                                "dead_streams_count",
                                "revived_streams_count",
                                "checked_streams",
                                "skipped",
                                "skip_reason",
                            )
                        },
                        {
                            "dead_streams_count": 0,
                            "revived_streams_count": 0,
                            "checked_streams": [],
                            "skipped": True,
                            "skip_reason": "aborted",
                        },
                    )

    def test_service_exception_result_uses_stable_public_error_code(self):
        """Service failures keep exception details in logs, not return values."""
        from apps.stream.stream_checker_service import StreamCheckerService

        service = object.__new__(StreamCheckerService)
        service._begin_single_channel_check_operation = Mock(return_value=True)
        service._end_single_channel_check_operation = Mock()
        service._abort_channel_check_if_requested = Mock(return_value=None)
        service.progress = Mock()
        secret = "Traceback with /app/data/secret/path"

        with patch(
            "apps.stream.stream_checker_service.get_udi_manager",
            side_effect=RuntimeError(secret),
        ):
            result = StreamCheckerService.check_single_channel(service, 1)

        self.assertEqual(
            result,
            {
                "success": False,
                "error": "single_channel_check_failed",
                "channel_id": 1,
            },
        )
        self.assertNotIn("secret", repr(result))
        service.progress.clear.assert_called_once()
        service._end_single_channel_check_operation.assert_called_once()


class TestSingleStreamCheckHandler(unittest.TestCase):
    """Tests for one-off stream checks that do not require channel assignment."""

    def test_handler_accepts_stream_reference_and_forwards_options(self):
        from flask import Flask
        from apps.api.stream_checker_handlers import check_single_stream_now_response

        mock_service = Mock()
        mock_service.get_status.return_value = {'checking': False, 'queue': {}, 'progress': {}}
        mock_service.check_single_stream.return_value = {
            'success': True,
            'stream_id': 456,
            'stats_payload': {'resolution': '1920x1080'},
        }

        app = Flask(__name__)
        with app.app_context():
            result = check_single_stream_now_response(
                payload={
                    'stream_reference': 'stream-456',
                    'persist': 'false',
                    'detect_blank': 'true',
                    'detect_freeze': True,
                    'detect_loop': 'false',
                },
                get_stream_checker_service=lambda: mock_service,
            )

        response, status_code = result if isinstance(result, tuple) else (result, 200)
        self.assertEqual(status_code, 200)
        self.assertEqual(response.get_json().get('stream_id'), 456)
        mock_service.check_single_stream.assert_called_once_with(
            456,
            persist=False,
            blank_check_enabled=True,
            freeze_check_enabled=True,
            loop_check_enabled=False,
        )

    def test_handler_rejects_missing_stream_id(self):
        from flask import Flask
        from apps.api.stream_checker_handlers import check_single_stream_now_response

        mock_service = Mock()

        app = Flask(__name__)
        with app.app_context():
            result = check_single_stream_now_response(
                payload={'stream_reference': 'not-a-stream'},
                get_stream_checker_service=lambda: mock_service,
            )

        response, status_code = result if isinstance(result, tuple) else (result, 200)
        self.assertEqual(status_code, 400)
        self.assertEqual(response.get_json().get('error'), 'stream_id required')
        mock_service.check_single_stream.assert_not_called()

    def test_handler_blocks_during_active_stream_checker(self):
        from flask import Flask
        from apps.api.stream_checker_handlers import check_single_stream_now_response

        mock_service = Mock()
        mock_service.get_status.return_value = {
            'checking': True,
            'queue': {'queue_size': 0, 'in_progress': 0},
            'progress': {},
        }

        app = Flask(__name__)
        with app.app_context():
            result = check_single_stream_now_response(
                payload={'stream_id': 456},
                get_stream_checker_service=lambda: mock_service,
            )

        response, status_code = result if isinstance(result, tuple) else (result, 200)
        self.assertEqual(status_code, 409)
        self.assertEqual(response.get_json().get('error'), 'stream_checker_active')
        mock_service.check_single_stream.assert_not_called()

    def test_handler_maps_stream_not_found_to_404(self):
        from flask import Flask
        from apps.api.stream_checker_handlers import check_single_stream_now_response

        mock_service = Mock()
        mock_service.get_status.return_value = {'checking': False, 'queue': {}, 'progress': {}}
        mock_service.check_single_stream.return_value = {
            'success': False,
            'error': 'stream_not_found',
            'stream_id': 999,
        }

        app = Flask(__name__)
        with app.app_context():
            result = check_single_stream_now_response(
                payload={'stream_id': 999},
                get_stream_checker_service=lambda: mock_service,
            )

        response, status_code = result if isinstance(result, tuple) else (result, 200)
        self.assertEqual(status_code, 404)
        self.assertEqual(response.get_json().get('error'), 'stream_not_found')

    def test_handler_returns_capacity_conflict_without_sanitizing_reason(self):
        from flask import Flask
        from apps.api.stream_checker_handlers import check_single_stream_now_response

        mock_service = Mock()
        mock_service.get_status.return_value = {'checking': False, 'queue': {}, 'progress': {}}
        mock_service.check_single_stream.return_value = {
            'success': False,
            'error': 'provider_capacity_unavailable',
            'reason_detail': 'quota_consumed_by_active_viewers',
            'stream_id': 456,
            'stream_name': 'Loose Stream',
            'analysis': {
                'stream_url': 'http://user:secret@provider.example/live',
                'provider_limit_skipped': True,
            },
        }

        app = Flask(__name__)
        with app.app_context():
            response, status_code = check_single_stream_now_response(
                payload={'stream_id': 456},
                get_stream_checker_service=lambda: mock_service,
            )

        self.assertEqual(status_code, 409)
        self.assertEqual(response.get_json()['error'], 'provider_capacity_unavailable')
        self.assertEqual(
            response.get_json()['reason_detail'],
            'quota_consumed_by_active_viewers',
        )
        self.assertEqual(response.get_json()['stream_name'], 'Loose Stream')
        self.assertNotIn('analysis', response.get_json())
        self.assertNotIn('secret', response.get_data(as_text=True))

    def test_handler_strips_provider_routes_from_successful_direct_check(self):
        from flask import Flask
        from apps.api.stream_checker_handlers import check_single_stream_now_response

        mock_service = Mock()
        mock_service.get_status.return_value = {
            'checking': False,
            'queue': {},
            'progress': {},
        }
        mock_service.check_single_stream.return_value = {
            'success': True,
            'stream_id': 456,
            'stream_name': 'Loose Stream',
            'persisted': True,
            'analysis': {
                'stream_url': 'http://alternate-user:alternate-secret@provider.invalid/live',
                'bitrate_kbps': 6123,
                'quality_reason_context': {
                    'probe_url': 'http://main-user:main-secret@provider.invalid/live',
                    'stage': 'stream analysis',
                },
            },
            'stats_payload': {'resolution': '1920x1080'},
        }

        app = Flask(__name__)
        with app.app_context():
            response, status_code = check_single_stream_now_response(
                payload={'stream_id': 456},
                get_stream_checker_service=lambda: mock_service,
            )

        body = response.get_json()
        response_text = response.get_data(as_text=True)
        self.assertEqual(status_code, 200)
        self.assertEqual(body['analysis']['bitrate_kbps'], 6123)
        self.assertEqual(body['analysis']['quality_reason_context']['stage'], 'stream analysis')
        self.assertNotIn('stream_url', body['analysis'])
        self.assertNotIn('probe_url', body['analysis']['quality_reason_context'])
        self.assertNotIn('alternate-secret', response_text)
        self.assertNotIn('main-secret', response_text)

    def test_handler_returns_aborted_direct_check_as_conflict(self):
        from flask import Flask
        from apps.api.stream_checker_handlers import check_single_stream_now_response

        mock_service = Mock()
        mock_service.get_status.return_value = {'checking': False, 'queue': {}, 'progress': {}}
        mock_service.check_single_stream.return_value = {
            'success': False,
            'error': 'aborted',
            'aborted': True,
            'stream_id': 456,
        }
        app = Flask(__name__)

        with app.app_context():
            response, status_code = check_single_stream_now_response(
                payload={'stream_id': 456},
                get_stream_checker_service=lambda: mock_service,
            )

        self.assertEqual(status_code, 409)
        self.assertTrue(response.get_json()['aborted'])


class TestSingleStreamCheckService(unittest.TestCase):
    """Service-level one-off stream check contract."""

    def _build_service(self):
        from apps.stream.stream_checker_service import StreamCheckerService
        from apps.stream.stream_checker_components import StreamCheckQueue

        service = object.__new__(StreamCheckerService)
        service.lock = threading.Lock()
        service.check_queue = StreamCheckQueue(max_size=10)
        service.checking = False
        service.sync_batch_state = {'active': False}
        service._single_stream_check_active = False
        service._single_stream_previous_queue_paused = False
        service._sync_batch_execution_active = False
        service._sync_batch_execution_generation = None
        service.progress = Mock()
        service.abort_current_check = threading.Event()
        service._require_quality_check_connectivity = Mock(return_value=None)
        service._is_stream_dead = Mock(return_value=(False, 'none'))
        service._apply_quality_classification = (
            lambda stream_data, result: StreamCheckerService._apply_quality_classification(
                stream_data,
                result,
            )
        )
        service._calculate_stream_score = Mock(return_value=0.93)
        service._run_loop_probes = Mock()
        service._prepare_stream_stats_for_batch = Mock(return_value={
            'stream_id': 456,
            'stream_stats': {
                'resolution': '1920x1080',
                'quality_score': 0.93,
            },
        })
        service._update_stream_stats = Mock(return_value=True)
        service._run_capacity_limited_stream_probes = Mock()

        mock_config = Mock()
        mock_config.get.side_effect = lambda key, default=None: {
            'stream_analysis': {
                'ffmpeg_duration': 7,
                'timeout': 8,
                'retries': 2,
                'retry_delay': 1,
                'user_agent': 'StreamFlow-Test',
                'stream_startup_buffer': 3,
            },
            'dead_stream_handling': {'enabled': True},
            'concurrent_streams.enabled': True,
            'concurrent_streams.global_limit': 4,
            'concurrent_streams.provider_wait_timeout': 15,
        }.get(key, default)
        service.config = mock_config

        mock_udi = Mock()
        mock_udi.get_stream_by_id.return_value = {
            'id': 456,
            'name': 'Loose Stream',
            'url': 'http://example.invalid/live.m3u8',
            'm3u_account_id': 12,
        }
        return service, mock_udi

    @patch('apps.stream.stream_checker_service.analyze_stream')
    @patch('apps.stream.stream_checker_service.get_udi_manager')
    def test_check_single_stream_measures_unassigned_udi_stream(self, mock_get_udi, mock_analyze):
        from apps.stream.stream_checker_service import StreamCheckerService

        service, mock_udi = self._build_service()
        mock_get_udi.return_value = mock_udi

        mock_analyze.return_value = {
            'stream_id': 456,
            'name': 'Loose Stream',
            'resolution': '1920x1080',
            'fps': 59.94,
            'video_codec': 'h264',
            'audio_codec': 'aac',
            'bitrate_kbps': 6000,
        }
        service._run_capacity_limited_stream_probes.return_value = [mock_analyze.return_value]

        result = StreamCheckerService.check_single_stream(
            service,
            456,
            persist=True,
            blank_check_enabled=True,
            freeze_check_enabled=True,
            loop_check_enabled=True,
        )

        self.assertTrue(result['success'])
        self.assertEqual(result['stream_id'], 456)
        self.assertEqual(result['run_mode'], 'single_stream_check')
        self.assertTrue(result['persisted'])
        self.assertEqual(result['stats_payload']['resolution'], '1920x1080')
        mock_analyze.assert_not_called()
        service._run_capacity_limited_stream_probes.assert_called_once()
        probe_kwargs = service._run_capacity_limited_stream_probes.call_args.kwargs
        self.assertTrue(probe_kwargs['blank_check_enabled'])
        self.assertTrue(probe_kwargs['freeze_check_enabled'])
        service._run_loop_probes.assert_called_once()
        service._update_stream_stats.assert_called_once()

    @patch('apps.stream.stream_checker_service.get_udi_manager')
    def test_check_single_stream_recovers_bitrate_via_capacity_limited_recheck(self, mock_get_udi):
        from apps.stream.stream_checker_service import StreamCheckerService

        service, mock_udi = self._build_service()
        mock_get_udi.return_value = mock_udi
        initial = {
            'stream_id': 456,
            'stream_name': 'Loose Stream',
            'status': 'OK',
            'resolution': '1920x1080',
            'bitrate_kbps': None,
            'measurement_incomplete': True,
            'measurement_incomplete_reason': 'missing_bitrate',
            'measurement_incomplete_context': {},
            'bitrate_recheck_required': True,
        }
        recovered = {
            'stream_id': 456,
            'stream_name': 'Loose Stream',
            'status': 'OK',
            'bitrate_kbps': 7200,
            'bitrate_source': 'ffmpeg_progress',
        }
        service._run_capacity_limited_stream_probes.side_effect = [
            [initial],
            [recovered],
        ]

        result = StreamCheckerService.check_single_stream(service, 456, persist=True)

        self.assertTrue(result['success'])
        self.assertEqual(service._run_capacity_limited_stream_probes.call_count, 2)
        self.assertEqual(result['analysis']['bitrate_kbps'], 7200)
        self.assertEqual(result['analysis']['bitrate_recheck_outcome'], 'recovered')
        self.assertFalse(result['analysis']['measurement_incomplete'])
        self.assertEqual(result['analysis']['quality_reason_detail'], 'none')
        service._update_stream_stats.assert_called_once_with(result['analysis'])

    @patch('apps.stream.stream_checker_service.get_udi_manager')
    def test_check_single_stream_persists_exhausted_bitrate_recheck_reason(self, mock_get_udi):
        from apps.stream.stream_checker_service import StreamCheckerService

        service, mock_udi = self._build_service()
        mock_get_udi.return_value = mock_udi
        initial = {
            'stream_id': 456,
            'stream_name': 'Loose Stream',
            'status': 'OK',
            'resolution': '1920x1080',
            'bitrate_kbps': None,
            'measurement_incomplete': True,
            'measurement_incomplete_reason': 'missing_bitrate',
            'measurement_incomplete_context': {},
            'bitrate_recheck_required': True,
        }
        unavailable = {
            'stream_id': 456,
            'stream_name': 'Loose Stream',
            'status': 'OK',
            'bitrate_kbps': None,
            'elapsed_time': 7,
        }
        service._run_capacity_limited_stream_probes.side_effect = [
            [initial],
            [unavailable],
        ]

        result = StreamCheckerService.check_single_stream(service, 456, persist=True)

        self.assertTrue(result['success'])
        self.assertFalse(result['dead'])
        self.assertEqual(
            result['analysis']['measurement_incomplete_reason'],
            'missing_bitrate_after_recheck',
        )
        self.assertEqual(
            result['analysis']['quality_reason_detail'],
            'missing_bitrate_after_recheck',
        )
        self.assertEqual(result['analysis']['bitrate_recheck_outcome'], 'unavailable')
        service._update_stream_stats.assert_called_once_with(result['analysis'])

    @patch('apps.stream.stream_checker_service.get_udi_manager')
    def test_check_single_stream_discards_recheck_result_when_aborted(self, mock_get_udi):
        from apps.stream.stream_checker_service import StreamCheckerService

        service, mock_udi = self._build_service()
        mock_get_udi.return_value = mock_udi
        initial = {
            'stream_id': 456,
            'stream_name': 'Loose Stream',
            'status': 'OK',
            'bitrate_kbps': None,
            'measurement_incomplete': True,
            'measurement_incomplete_reason': 'missing_bitrate',
            'measurement_incomplete_context': {},
            'bitrate_recheck_required': True,
        }

        def probe_side_effect(*_args, **_kwargs):
            if service._run_capacity_limited_stream_probes.call_count == 1:
                return [initial]
            service.abort_current_check.set()
            return [{
                'stream_id': 456,
                'status': 'OK',
                'bitrate_kbps': 7200,
            }]

        service._run_capacity_limited_stream_probes.side_effect = probe_side_effect

        result = StreamCheckerService.check_single_stream(service, 456, persist=True)

        self.assertFalse(result['success'])
        self.assertTrue(result['aborted'])
        self.assertNotIn('bitrate_recheck_attempted', initial)
        self.assertIsNone(initial['bitrate_kbps'])
        service._update_stream_stats.assert_not_called()

    @patch('apps.stream.stream_checker_service.get_udi_manager')
    def test_check_single_stream_late_abort_before_persist_is_linearized(self, mock_get_udi):
        from apps.stream.stream_checker_service import StreamCheckerService

        service, mock_udi = self._build_service()
        mock_get_udi.return_value = mock_udi
        analyzed = {
            'stream_id': 456,
            'stream_name': 'Loose Stream',
            'status': 'OK',
            'resolution': '1920x1080',
            'fps': 50,
            'video_codec': 'h264',
            'bitrate_kbps': 6000,
        }
        service._run_capacity_limited_stream_probes.return_value = [analyzed]

        def prepare_then_abort(_analysis):
            service.request_abort('late_single_stream_finalize')
            return {
                'stream_id': 456,
                'stream_stats': {'ffmpeg_output_bitrate': 6000},
            }

        service._prepare_stream_stats_for_batch.side_effect = prepare_then_abort

        result = StreamCheckerService.check_single_stream(service, 456, persist=True)

        self.assertFalse(result['success'])
        self.assertEqual(result['error'], 'aborted')
        self.assertTrue(result['aborted'])
        self.assertFalse(result['persisted'])
        service._update_stream_stats.assert_not_called()

    @patch('apps.stream.stream_checker_service.get_udi_manager')
    def test_check_single_stream_reports_provider_capacity_without_persisting(self, mock_get_udi):
        from apps.stream.stream_checker_service import StreamCheckerService

        service, mock_udi = self._build_service()
        mock_get_udi.return_value = mock_udi
        service._run_capacity_limited_stream_probes.return_value = [{
            'stream_id': 456,
            'stream_name': 'Loose Stream',
            'status': 'SKIPPED_PROVIDER_LIMIT',
            'provider_limit_skipped': True,
            'skipped_reason': 'quota_consumed_by_active_viewers',
            'reason_detail': 'active_viewers',
        }]

        result = StreamCheckerService.check_single_stream(service, 456, persist=True)

        self.assertFalse(result['success'])
        self.assertEqual(result['error'], 'provider_capacity_unavailable')
        self.assertEqual(result['reason_detail'], 'quota_consumed_by_active_viewers')
        service._update_stream_stats.assert_not_called()

    @patch('apps.stream.stream_checker_service.get_udi_manager')
    def test_check_single_stream_persists_capacity_deferred_recheck(self, mock_get_udi):
        from apps.stream.stream_checker_service import StreamCheckerService

        service, mock_udi = self._build_service()
        mock_get_udi.return_value = mock_udi
        initial = {
            'stream_id': 456,
            'stream_name': 'Loose Stream',
            'status': 'OK',
            'bitrate_kbps': None,
            'measurement_incomplete': True,
            'measurement_incomplete_reason': 'missing_bitrate',
            'measurement_incomplete_context': {},
            'bitrate_recheck_required': True,
        }
        capacity = {
            'stream_id': 456,
            'status': 'SKIPPED_PROVIDER_LIMIT',
            'provider_limit_skipped': True,
            'skipped_reason': 'quota_consumed_by_active_viewers',
            'reason_detail': 'active_viewers',
        }
        service._run_capacity_limited_stream_probes.side_effect = [
            [initial],
            [capacity],
        ]

        result = StreamCheckerService.check_single_stream(service, 456, persist=True)

        self.assertTrue(result['success'])
        self.assertFalse(result['analysis']['bitrate_recheck_attempted'])
        self.assertEqual(
            result['analysis']['bitrate_recheck_outcome'],
            'provider_capacity_unavailable',
        )
        self.assertEqual(
            result['analysis']['measurement_incomplete_context']['bitrate_recheck_reason'],
            'quota_consumed_by_active_viewers',
        )
        service._update_stream_stats.assert_called_once_with(result['analysis'])

    @patch('apps.stream.stream_checker_service.get_udi_manager')
    def test_direct_check_reservation_blocks_second_start_and_clear_aborts_first(self, mock_get_udi):
        from apps.stream.stream_checker_service import StreamCheckerService

        service, mock_udi = self._build_service()
        mock_get_udi.return_value = mock_udi
        probe_started = threading.Event()
        release_probe = threading.Event()
        thread_result = {}

        def blocking_probe(*_args, **_kwargs):
            probe_started.set()
            self.assertTrue(release_probe.wait(5))
            return [{
                'stream_id': 456,
                'stream_name': 'Loose Stream',
                'status': 'OK',
                'bitrate_kbps': 6000,
            }]

        service._run_capacity_limited_stream_probes.side_effect = blocking_probe

        worker = threading.Thread(
            target=lambda: thread_result.setdefault(
                'result',
                StreamCheckerService.check_single_stream(service, 456, persist=True),
            )
        )
        worker.start()
        self.assertTrue(probe_started.wait(5))
        self.assertTrue(service.checking)
        self.assertTrue(service.check_queue.paused)

        conflict = StreamCheckerService.check_single_stream(service, 456, persist=True)
        clear_result = service.clear_queue()
        self.assertEqual(conflict['error'], 'stream_checker_active')
        self.assertTrue(clear_result['abort_requested'])
        self.assertTrue(service.check_queue.paused)

        service.running = True
        service.batch_start_time = None
        service._start_batch_changelog = Mock()
        service._finalize_batch_changelog = Mock()
        service._check_channel = Mock()
        with patch.object(service.check_queue, 'get_next_entry') as get_next_entry, patch(
            'apps.stream.stream_checker_service.time.sleep',
            side_effect=lambda _seconds: setattr(service, 'running', False),
        ):
            service._worker_loop()

        self.assertTrue(service.abort_current_check.is_set())
        get_next_entry.assert_not_called()

        release_probe.set()
        worker.join(timeout=5)
        self.assertFalse(worker.is_alive())
        self.assertTrue(thread_result['result']['aborted'])
        self.assertFalse(service.checking)
        self.assertFalse(service.check_queue.paused)
        service._update_stream_stats.assert_not_called()

    @patch('apps.stream.stream_checker_service.get_udi_manager')
    def test_clear_after_direct_reservation_is_not_erased_before_probe(self, mock_get_udi):
        from apps.stream.stream_checker_service import StreamCheckerService

        service, mock_udi = self._build_service()
        mock_get_udi.return_value = mock_udi
        original_begin = service._begin_single_stream_check_operation
        reserved = threading.Event()
        continue_after_clear = threading.Event()
        thread_result = {}
        seen_abort_at_probe = []

        def begin_with_barrier():
            accepted = original_begin()
            if accepted:
                reserved.set()
                self.assertTrue(continue_after_clear.wait(5))
            return accepted

        def probe(*_args, **_kwargs):
            seen_abort_at_probe.append(service.abort_current_check.is_set())
            return [{
                'stream_id': 456,
                'stream_name': 'Loose Stream',
                'status': 'OK',
                'bitrate_kbps': 6000,
            }]

        service._begin_single_stream_check_operation = begin_with_barrier
        service._run_capacity_limited_stream_probes.side_effect = probe
        worker = threading.Thread(
            target=lambda: thread_result.setdefault(
                'result',
                StreamCheckerService.check_single_stream(service, 456, persist=True),
            )
        )
        worker.start()
        self.assertTrue(reserved.wait(5))

        clear_result = service.clear_queue()
        self.assertTrue(clear_result['abort_requested'])
        self.assertTrue(service.abort_current_check.is_set())
        continue_after_clear.set()
        worker.join(timeout=5)

        self.assertFalse(worker.is_alive())
        self.assertEqual(seen_abort_at_probe, [True])
        self.assertEqual(thread_result['result']['error'], 'aborted')
        self.assertFalse(service.checking)
        self.assertFalse(service.check_queue.paused)
        service._update_stream_stats.assert_not_called()

    def test_capacity_limited_probe_helper_uses_shared_limiter_and_scheduler(self):
        from apps.stream.stream_checker_service import StreamCheckerService

        service = object.__new__(StreamCheckerService)
        service.abort_current_check = threading.Event()
        service.config = Mock()
        service.config.get.side_effect = lambda key, default=None: {
            'concurrent_streams.enabled': True,
            'concurrent_streams.global_limit': 4,
            'concurrent_streams.provider_wait_timeout': 15,
        }.get(key, default)
        udi = Mock()
        accounts = [{'id': 12, 'max_streams': 2}]
        udi.get_m3u_accounts.return_value = accounts
        limiter = Mock()
        scheduler = Mock()
        scheduler.check_streams_with_limits.return_value = [{'stream_id': 456, 'status': 'OK'}]
        stream = {
            'id': 456,
            'name': 'Loose Stream',
            'url': 'http://example.invalid/live.m3u8',
            'm3u_account_id': 12,
        }

        with patch(
            'apps.stream.concurrent_stream_limiter.get_account_limiter',
            return_value=limiter,
        ), patch(
            'apps.stream.concurrent_stream_limiter.initialize_account_limits',
        ) as initialize_limits, patch(
            'apps.stream.concurrent_stream_limiter.get_smart_scheduler',
            return_value=scheduler,
        ) as get_scheduler:
            result = StreamCheckerService._run_capacity_limited_stream_probes(
                service,
                [stream],
                udi=udi,
                ffmpeg_duration=7,
                timeout=8,
            )

        self.assertEqual(result[0]['status'], 'OK')
        self.assertIs(limiter.udi_manager, udi)
        limiter.invalidate_account_inventory.assert_called_once_with()
        initialize_limits.assert_called_once_with(accounts)
        get_scheduler.assert_called_once_with(global_limit=4)
        scheduler.check_streams_with_limits.assert_called_once()
        scheduler_kwargs = scheduler.check_streams_with_limits.call_args.kwargs
        self.assertEqual(scheduler_kwargs['provider_wait_timeout'], 15)
        self.assertIs(scheduler_kwargs['abort_event'], service.abort_current_check)
        self.assertEqual(scheduler_kwargs['streams'], [stream])

    def test_one_off_probe_inventory_failures_never_reach_analyzer(self):
        from apps.stream.stream_checker_service import StreamCheckerService

        stream = {
            'id': 456,
            'name': 'Inventory-guarded stream',
            'url': 'http://example.invalid/live.m3u8',
            'm3u_account_id': 12,
        }
        cases = {
            'malformed': {'id': 12},
            'rejected_malformed': [{'id': 'invalid', 'max_streams': 1}],
            'missing_getter': 'missing',
            'throwing_getter': RuntimeError('UDI inventory unavailable'),
        }

        for case_name, inventory in cases.items():
            with self.subTest(case=case_name):
                service = object.__new__(StreamCheckerService)
                service.abort_current_check = threading.Event()
                service.config = Mock()
                service.config.get.side_effect = lambda key, default=None: {
                    'concurrent_streams.enabled': True,
                    'concurrent_streams.global_limit': 4,
                    'concurrent_streams.provider_wait_timeout': 15,
                }.get(key, default)
                udi = Mock()
                events = []
                if inventory == 'missing':
                    udi.get_m3u_accounts = None
                elif isinstance(inventory, Exception):
                    def fail_inventory_fetch(error=inventory):
                        events.append('fetch')
                        raise error

                    udi.get_m3u_accounts.side_effect = fail_inventory_fetch
                else:
                    udi.get_m3u_accounts.side_effect = (
                        lambda value=inventory: events.append('fetch') or value
                    )
                limiter = Mock()
                limiter.invalidate_account_inventory.side_effect = (
                    lambda: events.append('invalidate')
                )
                scheduler = Mock()

                with patch(
                    'apps.stream.concurrent_stream_limiter.get_account_limiter',
                    return_value=limiter,
                ), patch(
                    'apps.stream.concurrent_stream_limiter.initialize_account_limits',
                    side_effect=lambda accounts: (
                        events.append('initialize')
                        or (False if case_name == 'rejected_malformed' else None)
                    ),
                ) as initialize_limits, patch(
                    'apps.stream.concurrent_stream_limiter.get_smart_scheduler',
                    return_value=scheduler,
                ) as get_scheduler, patch(
                    'apps.stream.stream_checker_service.analyze_stream',
                ) as analyzer:
                    result = StreamCheckerService._run_capacity_limited_stream_probes(
                        service,
                        [stream],
                        udi=udi,
                        ffmpeg_duration=7,
                        timeout=8,
                    )

                self.assertEqual(result, [])
                self.assertEqual(events[0], 'invalidate')
                limiter.invalidate_account_inventory.assert_called_once_with()
                get_scheduler.assert_not_called()
                scheduler.check_streams_with_limits.assert_not_called()
                analyzer.assert_not_called()
                limiter.acquire.assert_not_called()
                limiter.reserve_profile_for_stream_with_url.assert_not_called()
                limiter.release.assert_not_called()
                limiter.release_profile.assert_not_called()
                if case_name == 'rejected_malformed':
                    self.assertEqual(events, ['invalidate', 'fetch', 'initialize'])
                    initialize_limits.assert_called_once_with(inventory)
                else:
                    initialize_limits.assert_not_called()

    def test_one_off_empty_inventory_allows_custom_but_blocks_provider(self):
        from apps.stream.concurrent_stream_limiter import get_account_limiter
        from apps.stream.stream_checker_service import StreamCheckerService

        service = object.__new__(StreamCheckerService)
        service.abort_current_check = threading.Event()
        service.config = Mock()
        service.config.get.side_effect = lambda key, default=None: {
            'concurrent_streams.enabled': True,
            'concurrent_streams.global_limit': 2,
            'concurrent_streams.provider_wait_timeout': 0,
        }.get(key, default)
        udi = Mock()
        udi.get_m3u_accounts.return_value = []
        udi.get_stream_by_id.return_value = None
        provider_stream = {
            'id': 456,
            'name': 'Provider stream without authority',
            'url': 'http://provider.invalid/live.m3u8',
            'm3u_account_id': 12,
        }
        custom_stream = {
            'id': 457,
            'name': 'Explicit custom stream',
            'url': 'http://custom.invalid/live.m3u8',
            'is_custom': True,
        }
        limiter = get_account_limiter()
        previous_udi_manager = limiter.udi_manager
        limiter.clear()

        with patch(
            'apps.stream.stream_checker_service.analyze_stream',
            return_value={
                'stream_id': 457,
                'stream_name': 'Explicit custom stream',
                'status': 'OK',
            },
        ) as analyzer:
            try:
                provider_results = (
                    StreamCheckerService._run_capacity_limited_stream_probes(
                        service,
                        [provider_stream],
                        udi=udi,
                        ffmpeg_duration=7,
                        timeout=8,
                    )
                )
                self.assertEqual(len(provider_results), 1)
                self.assertTrue(provider_results[0]['provider_limit_skipped'])
                self.assertEqual(
                    provider_results[0]['reason_detail'],
                    'provider_profile_unavailable',
                )
                analyzer.assert_not_called()

                custom_results = (
                    StreamCheckerService._run_capacity_limited_stream_probes(
                        service,
                        [custom_stream],
                        udi=udi,
                        ffmpeg_duration=7,
                        timeout=8,
                    )
                )
                self.assertEqual(custom_results[0]['status'], 'OK')
                analyzer.assert_called_once()
                self.assertEqual(
                    analyzer.call_args.kwargs['stream_url'],
                    custom_stream['url'],
                )
                self.assertTrue(limiter.account_inventory_trusted)
                self.assertEqual(limiter.account_inventory_ids, set())
                self.assertEqual(limiter.account_checking_counts, {})
                self.assertEqual(limiter.profile_checking_counts, {})
                self.assertEqual(limiter.profile_reservations_by_token, {})
            finally:
                limiter.clear()
                limiter.udi_manager = previous_udi_manager


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
