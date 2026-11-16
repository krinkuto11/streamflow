#!/usr/bin/env python3
"""Validation keeps preserved custom streams through the real Dispatcharr write.

Exercises the actual ``update_channel_streams`` writer and dead-stream filter;
only HTTP and the dead-stream tracker are mocked. A mocked writer cannot catch a
custom stream being removed by that filter.
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

CUSTOM_URL = 'http://127.0.0.1:9999/slate.ts'
CHANNEL = {'id': 1, 'name': 'News HD', 'channel_group_id': None}
STREAMS = [
    {'id': 101, 'name': 'News Stream 1', 'url': 'http://p/101', 'm3u_account': 1},
    {'id': 102, 'name': 'Sports Stream', 'url': 'http://p/102', 'm3u_account': 1},
    {'id': 900, 'name': 'Could Not Dispatch', 'url': CUSTOM_URL, 'm3u_account': None, 'is_custom': True},
]
CHANNEL_STREAMS = {1: [{'id': 101, 'name': 'News Stream 1'}, {'id': 102, 'name': 'Sports Stream'},
                       {'id': 900, 'name': 'Could Not Dispatch'}]}


def _profile(*, preserve):
    return {
        'name': 'Test Profile',
        'stream_matching': {'enabled': True, 'validate_existing_streams': True,
                            'preserve_custom_streams': preserve},
        'global_action': {'affected': False},
    }


class TestValidationKeepsCustomStreamsThroughRealWrite(unittest.TestCase):
    def setUp(self):
        from apps.automation.automated_stream_manager import AutomatedStreamManager

        self.manager = AutomatedStreamManager()
        self.manager.regex_matcher.add_channel_pattern(
            channel_id='1', name='News HD', regex_patterns=[r'News.*'], enabled=True,
        )
        self.manager._is_dead_stream_removal_enabled = lambda: True

        udi = MagicMock()
        udi.get_channels.return_value = [CHANNEL]
        udi.get_streams.return_value = STREAMS
        udi.get_channel_streams.side_effect = lambda ch_id: CHANNEL_STREAMS.get(ch_id, [])
        patch('automated_stream_manager.get_udi_manager', return_value=udi).start()

        session_manager = MagicMock()
        session_manager.get_channels_in_active_sessions.return_value = []
        patch('apps.stream.stream_session_manager.get_session_manager',
              return_value=session_manager).start()

        # Real writer + real dead-stream filter; mock only I/O.
        tracker = MagicMock()
        tracker.get_dead_stream_reasons.side_effect = lambda urls: {
            url: 'offline' for url in urls if url == CUSTOM_URL}
        patch('apps.stream.dead_streams_tracker.DeadStreamsTracker', return_value=tracker).start()
        patch('apps.core.api_utils.get_streams', return_value=STREAMS).start()
        patch('apps.core.api_utils.get_valid_stream_ids', return_value={101, 102, 900}).start()
        patch('apps.core.api_utils._verify_channel_assignment_after_patch', return_value=True).start()
        patch('apps.core.api_utils._get_base_url', return_value='http://test.local').start()
        response = MagicMock(status_code=200)
        self.patch_request = patch('apps.core.api_utils.patch_request', return_value=response).start()
        self.addCleanup(patch.stopall)

    def _run(self, profile):
        config_manager = MagicMock()
        config_manager.get_effective_configuration.return_value = {
            'source': 'period', 'periods': [{'id': 'p1', 'name': 'P1', 'profile': profile}],
            'period_id': 'p1', 'period_name': 'P1', 'profile': profile,
        }
        with patch('apps.automation.automation_config_manager.get_automation_config_manager',
                   return_value=config_manager):
            return self.manager.validate_and_remove_non_matching_streams(force=False, skip_changelog=True)

    def test_offline_custom_stream_survives_the_actual_patch(self):
        results = self._run(_profile(preserve=True))

        self.patch_request.assert_called_once()
        self.assertEqual(self.patch_request.call_args.args[1], {'streams': [101, 900]})
        self.assertEqual(results['streams_removed'], 1)  # only the non-matching Sports stream

    def test_without_the_option_the_offline_custom_stream_is_removed(self):
        self._run(_profile(preserve=False))

        self.patch_request.assert_called_once()
        self.assertEqual(self.patch_request.call_args.args[1], {'streams': [101]})


if __name__ == '__main__':
    unittest.main()
