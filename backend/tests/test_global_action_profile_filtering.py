#!/usr/bin/env python3
"""
Test that global action respects profile filtering.

When a profile is selected, global action should only queue channels
that are in that profile, not all channels.
"""

import os
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock

# Set up environment before importing modules
os.environ['CONFIG_DIR'] = tempfile.mkdtemp()
os.environ['DISPATCHARR_URL'] = 'http://test'
os.environ['DISPATCHARR_API_KEY'] = 'test_key'

from apps.stream.stream_checker_service import StreamCheckerService


class TestGlobalActionProfileFiltering(unittest.TestCase):
    """Test global action profile filtering functionality."""

    def setUp(self):
        """Set up test environment."""
        self.test_dir = tempfile.mkdtemp()
        os.environ['CONFIG_DIR'] = self.test_dir
        
        self.service = StreamCheckerService.__new__(StreamCheckerService)
        self.service.lock = threading.Lock()
        self.service._cancel_queueing = False
        self.service.config = MagicMock()
        self.service.config.get.side_effect = lambda key, default=None: {
            'queue.max_channels_per_run': 50,
            'queue.start_mode': 'first',
            'queue.start_channel_id': None,
        }.get(key, default)
        self.service.check_queue = MagicMock()
        def add_channels(channel_ids, priority=5, on_accepted=None):
            if on_accepted:
                for channel_id in channel_ids:
                    on_accepted(channel_id)
            return len(channel_ids)

        self.service.check_queue.add_channels.side_effect = add_channels
        self.service.check_queue.remove_from_completed = MagicMock()
        self.service.update_tracker = MagicMock()
        
        # Mock UDI to return test channels
        self.mock_udi = MagicMock()
        self.all_channels = [
            {'id': 1, 'name': 'Channel 1', 'channel_group_id': 1},
            {'id': 2, 'name': 'Channel 2', 'channel_group_id': 1},
            {'id': 3, 'name': 'Channel 3', 'channel_group_id': 2},
            {'id': 4, 'name': 'Channel 4', 'channel_group_id': 2},
            {'id': 5, 'name': 'Channel 5', 'channel_group_id': 3},
        ]
        self.mock_udi.get_channels.return_value = self.all_channels
        self.mock_udi.is_network_ready.return_value = True
        
    def tearDown(self):
        """Clean up test environment."""
        import shutil
        if os.path.exists(self.test_dir):
            shutil.rmtree(self.test_dir)
    
    def _extract_queued_channels(self, mock_add):
        """Helper method to extract channel IDs from mock add_channels calls."""
        all_queued = []
        for call_args in mock_add.call_args_list:
            args, kwargs = call_args
            all_queued.extend(args[0])
        return all_queued

    @patch('apps.automation.automation_config_manager.get_automation_config_manager')
    @patch('apps.stream.stream_checker_service.get_udi_manager')
    def test_queue_all_channels_with_profile_filter(self, mock_get_udi, mock_get_automation_config):
        """Test that _queue_all_channels respects automation-profile filtering."""
        mock_get_udi.return_value = self.mock_udi

        automation_config = MagicMock()
        enabled_ids = {1, 2, 5}

        def effective_config(channel_id, group_id=None):
            return {
                'profile': {
                    'stream_checking': {'enabled': channel_id in enabled_ids},
                },
            }

        automation_config.get_effective_configuration.side_effect = effective_config
        mock_get_automation_config.return_value = automation_config

        self.service._queue_all_channels(force_check=False)

        all_queued = self._extract_queued_channels(self.service.check_queue.add_channels)
        self.assertEqual(set(all_queued), enabled_ids)

    @patch('apps.automation.automation_config_manager.get_automation_config_manager')
    @patch('apps.stream.stream_checker_service.get_udi_manager')
    def test_queue_all_channels_without_active_periods_queues_nothing(self, mock_get_udi, mock_get_automation_config):
        """Test that _queue_all_channels skips channels with no active automation profile."""
        mock_get_udi.return_value = self.mock_udi
        automation_config = MagicMock()
        automation_config.get_effective_configuration.return_value = None
        mock_get_automation_config.return_value = automation_config

        self.service._queue_all_channels(force_check=False)

        self.service.check_queue.add_channels.assert_not_called()


if __name__ == '__main__':
    unittest.main()
