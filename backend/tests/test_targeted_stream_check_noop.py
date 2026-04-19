#!/usr/bin/env python3
"""Regression tests for targeted stream checks with no matching streams."""

import os
import sys
import unittest
from unittest.mock import MagicMock, Mock, patch

# Add backend to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from apps.stream import stream_checker_service as scs


class _TestConfig:
    def get(self, key, default=None):
        if key == 'batch_operations':
            return {'enabled': True, 'batch_size': 10, 'verify_updates': False}
        return default


class TestTargetedStreamCheckNoop(unittest.TestCase):
    """Targeted checks should not clear channels when no target streams match."""

    def _make_service(self):
        service = scs.StreamCheckerService.__new__(scs.StreamCheckerService)
        service.config = _TestConfig()
        service.progress = Mock()
        service.check_queue = Mock()
        service.update_tracker = Mock()
        service.update_tracker.updates = {'channels': {}}
        service.update_tracker.should_force_check.return_value = False
        service._check_channel_limits = Mock(return_value=None)
        service.changelog = None
        return service

    def _run_targeted_check(self, target_stream_ids):
        service = self._make_service()
        streams = [
            {'id': 101, 'name': 'BBC News HD', 'url': 'http://example.com/101'},
            {'id': 102, 'name': 'BBC News FHD', 'url': 'http://example.com/102'},
        ]

        udi = MagicMock()
        udi.get_channel_by_id.return_value = {'id': 73, 'name': 'BBC News'}

        automation_config = MagicMock()
        automation_config.get_effective_configuration.return_value = None

        with patch.object(scs, 'get_udi_manager', return_value=udi), \
                patch.object(scs, 'fetch_channel_streams', return_value=streams), \
                patch.object(scs, '_get_base_url', return_value='http://dispatcharr:9191'), \
                patch.object(scs, 'update_channel_streams') as update_channel_streams, \
                patch(
                    'apps.automation.automation_config_manager.get_automation_config_manager',
                    return_value=automation_config,
                ):
            result = service._check_channel_concurrent(
                73,
                skip_batch_changelog=True,
                target_stream_ids=target_stream_ids,
            )

        return service, result, update_channel_streams

    def test_empty_targeted_check_does_not_update_channel_with_empty_streams(self):
        service, result, update_channel_streams = self._run_targeted_check([])

        update_channel_streams.assert_not_called()
        service.check_queue.mark_completed.assert_called_once_with(73)
        service.update_tracker.mark_channel_checked.assert_called_once_with(
            73,
            stream_count=2,
            checked_stream_ids=[101, 102],
        )
        self.assertTrue(result['skipped'])
        self.assertEqual(result['skip_reason'], 'no_matching_target_streams')

    def test_nonmatching_targeted_check_does_not_update_channel_with_empty_streams(self):
        service, result, update_channel_streams = self._run_targeted_check(['999'])

        update_channel_streams.assert_not_called()
        service.check_queue.mark_completed.assert_called_once_with(73)
        service.update_tracker.mark_channel_checked.assert_called_once_with(
            73,
            stream_count=2,
            checked_stream_ids=[101, 102],
        )
        self.assertTrue(result['skipped'])
        self.assertEqual(result['skip_reason'], 'no_matching_target_streams')


if __name__ == '__main__':
    unittest.main()
