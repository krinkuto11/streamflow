#!/usr/bin/env python3
"""Removal of non-matching streams is driven only by the profile toggle.

Regression: scheduled automation runs call
``validate_and_remove_non_matching_streams(force=False)``. That path used to be
short-circuited by a hidden ``automation_controls.remove_non_matching_streams``
global (default False, no UI), so a profile with
``stream_matching.validate_existing_streams`` enabled never had its streams
removed on a schedule — while the dashboard's manual run (force=True) bypassed
the global and removed streams from every matching-enabled channel, even ones
whose profile had the toggle off.

Both halves are covered here: force=False now removes when the profile says so,
and force=True no longer removes when it doesn't.
"""

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

backend_dir = Path(__file__).parent.parent
sys.path.insert(0, str(backend_dir))

os.environ['DISPATCHARR_BASE_URL'] = 'http://test.local'
os.environ['DISPATCHARR_TOKEN'] = 'test_token'


CHANNEL = {'id': 1, 'name': 'News HD', 'channel_group_id': None}
STREAMS = [
    {'id': 101, 'name': 'News Stream 1', 'm3u_account': 1},
    {'id': 102, 'name': 'Sports Stream', 'm3u_account': 1},
]
CHANNEL_STREAMS = {
    1: [{'id': 101, 'name': 'News Stream 1'}, {'id': 102, 'name': 'Sports Stream'}],
}


def _profile(*, validate_existing_streams, global_action_affected=False):
    return {
        'name': 'Test Profile',
        'stream_matching': {
            'enabled': True,
            'validate_existing_streams': validate_existing_streams,
        },
        'global_action': {'affected': global_action_affected},
    }


class TestValidateExistingStreamsIsAuthoritative(unittest.TestCase):
    def setUp(self):
        from apps.automation.automated_stream_manager import AutomatedStreamManager

        self.manager = AutomatedStreamManager()

        # Channel 1 keeps streams matching 'News.*'; 'Sports Stream' must go.
        self.manager.regex_matcher.add_channel_pattern(
            channel_id='1',
            name='News HD',
            regex_patterns=[r'News.*'],
            enabled=True,
        )

        udi_instance = MagicMock()
        udi_instance.get_channels.return_value = [CHANNEL]
        udi_instance.get_streams.return_value = STREAMS
        udi_instance.get_channel_streams.side_effect = lambda ch_id: CHANNEL_STREAMS.get(ch_id, [])
        self._udi_patcher = patch(
            'automated_stream_manager.get_udi_manager', return_value=udi_instance
        )
        self._udi_patcher.start()
        self.addCleanup(self._udi_patcher.stop)

        session_manager = MagicMock()
        session_manager.get_channels_in_active_sessions.return_value = []
        self._session_patcher = patch(
            'apps.stream.stream_session_manager.get_session_manager',
            return_value=session_manager,
        )
        self._session_patcher.start()
        self.addCleanup(self._session_patcher.stop)

        self.mock_update_streams = patch(
            'automated_stream_manager.update_channel_streams', return_value=True
        ).start()
        self.addCleanup(patch.stopall)

    def _run(self, *, force, profile):
        config_manager = MagicMock()
        config_manager.get_effective_configuration.return_value = {
            'source': 'period',
            'periods': [{'id': 'p1', 'name': 'Period 1', 'profile': profile}],
            'period_id': 'p1',
            'period_name': 'Period 1',
            'profile': profile,
        }
        with patch(
            'apps.automation.automation_config_manager.get_automation_config_manager',
            return_value=config_manager,
        ):
            return self.manager.validate_and_remove_non_matching_streams(
                force=force, skip_changelog=True
            )

    def test_scheduled_run_removes_when_profile_toggle_on(self):
        """The reported bug: force=False must honour validate_existing_streams."""
        results = self._run(force=False, profile=_profile(validate_existing_streams=True))

        self.mock_update_streams.assert_called_once()
        channel_id, kept_ids = self.mock_update_streams.call_args[0][:2]
        self.assertEqual(channel_id, 1)
        self.assertEqual(kept_ids, [101], "Only the matching stream should be kept")
        self.assertEqual(results['streams_removed'], 1)
        self.assertEqual(results['channels_modified'], 1)

    def test_scheduled_run_keeps_streams_when_profile_toggle_off(self):
        results = self._run(force=False, profile=_profile(validate_existing_streams=False))

        self.mock_update_streams.assert_not_called()
        self.assertEqual(results['streams_removed'], 0)

    def test_manual_run_keeps_streams_when_profile_toggle_off(self):
        """force=True is not a licence to remove for profiles with the toggle off."""
        results = self._run(force=True, profile=_profile(validate_existing_streams=False))

        self.mock_update_streams.assert_not_called()
        self.assertEqual(results['streams_removed'], 0)

    def test_global_action_override_still_removes(self):
        results = self._run(
            force=True,
            profile=_profile(validate_existing_streams=False, global_action_affected=True),
        )

        self.mock_update_streams.assert_called_once()
        self.assertEqual(results['streams_removed'], 1)


if __name__ == '__main__':
    unittest.main()
