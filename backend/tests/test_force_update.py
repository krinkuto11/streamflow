#!/usr/bin/env python3
"""
Unit test for force_update logic in StreamMonitoringService.
"""

import unittest
import sys
import os
import time
import tempfile
import shutil
from pathlib import Path
from unittest.mock import MagicMock, patch

# Add backend to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Mock dotenv before imports
sys.modules['dotenv'] = MagicMock()

from apps.stream.stream_monitoring_service import StreamMonitoringService
from apps.stream.stream_session_manager import SessionInfo, StreamInfo

class TestForceUpdate(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.old_config_dir = os.environ.get('CONFIG_DIR')
        os.environ['CONFIG_DIR'] = self.temp_dir
        
        self.config_dir_patcher = patch('stream_session_manager.CONFIG_DIR', Path(self.temp_dir))
        self.config_dir_mock = self.config_dir_patcher.start()
        
        with patch('stream_monitoring_service.get_session_manager'), \
             patch('stream_monitoring_service.get_screenshot_service'):
            self.service = StreamMonitoringService()
            self.service.session_manager = MagicMock()
            self.service.monitors = {}
            self.service.last_switch_times = {}

    def tearDown(self):
        self.config_dir_patcher.stop()
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir)
        if self.old_config_dir:
            os.environ['CONFIG_DIR'] = self.old_config_dir

    @patch('stream_monitoring_service.get_udi_manager')
    def test_force_update_does_not_patch_unchanged_assignment(self, mock_get_udi):
        """Recalculation after a status change must not generate a no-op PATCH."""
        session_id = 'test_sess'
        chan_id = 99
        
        session = SessionInfo(
            session_id=session_id,
            channel_id=chan_id,
            channel_name="test",
            regex_filter=".*",
            created_at=time.time(),
            is_active=True,
            streams={}
        )
        
        # Stream is stable and good
        stream = StreamInfo(url='http://s1', name='s1', stream_id=101, channel_id=chan_id)
        stream.status = 'stable'
        stream.reliability_score = 90.0
        
        session.streams = {101: stream}
        self.service.session_manager.get_session.return_value = session
        
        mock_udi = MagicMock()
        mock_get_udi.return_value = mock_udi
        # UDI already has this stream (Simulate "It thinks it's there")
        mock_udi.get_channel_by_id.return_value = {'streams': [101]}
        
        # Case 1: Normal update (force_update=False)
        # Should NOT trigger update because order hasn't changed
        with patch('api_utils.update_channel_streams') as mock_update:
            self.service._evaluate_session_streams(session_id, force_update=False)
            mock_update.assert_not_called()
            
        # Case 2: A forced recalculation keeps the same assignment.
        with patch('api_utils.update_channel_streams') as mock_update, \
             patch('api_utils.fetch_data_from_url') as mock_dispatcharr_get, \
             patch('api_utils.patch_request') as mock_dispatcharr_patch:
            self.service._evaluate_session_streams(session_id, force_update=True)

            mock_update.assert_not_called()
            mock_dispatcharr_get.assert_not_called()
            mock_dispatcharr_patch.assert_not_called()
            mock_udi.refresh_channel_by_id.assert_not_called()

if __name__ == '__main__':
    unittest.main()
