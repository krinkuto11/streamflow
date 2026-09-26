#!/usr/bin/env python3
"""
Unit tests for stream protection logic in StreamMonitoringService.
"""

import unittest
import sys
import os
import time
import tempfile
import shutil
from pathlib import Path
from unittest.mock import MagicMock, patch, PropertyMock

# Add backend to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Mock dotenv before imports
sys.modules['dotenv'] = MagicMock()

from apps.stream.stream_monitoring_service import StreamMonitoringService
from apps.stream.stream_session_manager import StreamInfo, SessionInfo

class TestStreamProtection(unittest.TestCase):
    def setUp(self):
        # Setup temp config dir to avoid Read-only file system error
        self.temp_dir = tempfile.mkdtemp()
        self.old_config_dir = os.environ.get('CONFIG_DIR')
        os.environ['CONFIG_DIR'] = self.temp_dir
        
        # Patch CONFIG_DIR in stream_session_manager module
        # This is necessary because the module level constant is computed at import time
        # We need to act as if it points to our temp dir
        self.config_dir_patcher = patch('stream_session_manager.CONFIG_DIR', Path(self.temp_dir))
        self.config_dir_mock = self.config_dir_patcher.start()
        
        # Now we can instantiate the service safely
        # We also mock get_session_manager and get_screenshot_service to avoid full initialization unless needed
        with patch('stream_monitoring_service.get_session_manager') as mock_get_sm, \
             patch('stream_monitoring_service.get_screenshot_service') as mock_get_ss:
            self.service = StreamMonitoringService()
            self.service.session_manager = MagicMock()
            self.service.screenshot_service = MagicMock()
            self.service.monitors = {}
            self.service.last_switch_times = {}

    def tearDown(self):
        self.config_dir_patcher.stop()
        
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir)
            
        if self.old_config_dir:
            os.environ['CONFIG_DIR'] = self.old_config_dir
        elif 'CONFIG_DIR' in os.environ:
            del os.environ['CONFIG_DIR']

    @patch('stream_monitoring_service.get_udi_manager')
    def test_review_stream_no_protection(self, mock_get_udi):
        """Test that a 'review' stream is NOT protected even if it is the primary."""
            
        session_id = 'sess1'
        chan_id = 1
        
        # Setup session with correct arguments
        session = SessionInfo(
            session_id=session_id, 
            channel_id=chan_id, 
            channel_name="test_channel",
            regex_filter="test",
            created_at=time.time(),
            is_active=True,
            streams={}
        )
        
        # Current Primary: Review status, Score 80
        curr_stream = StreamInfo(url='http://s1', name='s1', stream_id=101, channel_id=chan_id)
        curr_stream.status = 'review'
        curr_stream.reliability_score = 80.0
        curr_stream.width = 1920
        curr_stream.height = 1080
        
        # Candidate: Review status, Score 85
        candidate = StreamInfo(url='http://s2', name='s2', stream_id=102, channel_id=chan_id)
        candidate.status = 'review'
        candidate.reliability_score = 85.0
        candidate.width = 1920
        candidate.height = 1080
        
        session.streams = {101: curr_stream, 102: candidate}
        self.service.session_manager.get_session.return_value = session
        self.service.session_manager.get_session_owner.return_value = session_id
        
        mock_udi = MagicMock()
        mock_get_udi.return_value = mock_udi
        mock_udi.get_channel_by_id.return_value = {'streams': [101, 102]}
        
        with patch('api_utils.update_channel_streams') as mock_update:
            mock_update.return_value = True
            
            self.service._evaluate_session_streams(session_id)
            
            mock_update.assert_called()
            args, _ = mock_update.call_args
            new_order = args[1]
            mock_update.assert_called()
            args, _ = mock_update.call_args
            new_order = args[1]
            
            # Since NO stable streams exist, the system should fallback to showing review streams
            self.assertEqual(len(new_order), 2, "Should fallback to showing review streams if no stable streams exist")
            self.assertEqual(new_order[0], 102, "Should pick better review stream")

    @patch('stream_monitoring_service.get_udi_manager')
    def test_stable_idle_no_protection(self, mock_get_udi):
        """Test that a 'stable' stream is NOT protected if NO ONE is playing it."""
        session_id = 'sess2'
        chan_id = 2
        
        session = SessionInfo(
            session_id=session_id, 
            channel_id=chan_id, 
            channel_name="test_channel",
            regex_filter="test",
            created_at=time.time(),
            is_active=True,
            streams={}
        )
        
        # Current Primary: Stable, Score 80
        curr = StreamInfo(url='http://s1', name='s1', stream_id=201, channel_id=chan_id)
        curr.status = 'stable'
        curr.reliability_score = 80.0
        curr.width = 1920
        curr.height = 1080
        
        # Candidate: Stable, Score 82
        cand = StreamInfo(url='http://s2', name='s2', stream_id=202, channel_id=chan_id)
        cand.status = 'stable'
        cand.reliability_score = 82.0
        cand.width = 1920
        cand.height = 1080
        
        session.streams = {201: curr, 202: cand}
        self.service.session_manager.get_session.return_value = session
        self.service.session_manager.get_session_owner.return_value = session_id
        
        mock_udi = MagicMock()
        mock_get_udi.return_value = mock_udi
        mock_udi.get_channel_by_id.return_value = {'streams': [201, 202]}
        
        # IMPORTANT: No playing streams
        mock_udi.get_playing_stream_ids.return_value = set()
        
        with patch('api_utils.update_channel_streams') as mock_update:
            mock_update.return_value = True
            
            self.service._evaluate_session_streams(session_id)
            
            mock_update.assert_called()
            new_order = mock_update.call_args[0][1]
            self.assertEqual(new_order[0], 202, "Idle stable stream should be swapped for better one")

    @patch('stream_monitoring_service.get_udi_manager')
    def test_stable_playing_protection(self, mock_get_udi):
        """Test that a 'stable' stream IS protected if it IS being played."""
        session_id = 'sess3'
        chan_id = 3
        
        session = SessionInfo(
            session_id=session_id, 
            channel_id=chan_id, 
            channel_name="test_channel",
            regex_filter="test",
            created_at=time.time(),
            is_active=True,
            streams={}
        )
        
        curr = StreamInfo(url='http://s1', name='s1', stream_id=301, channel_id=chan_id)
        curr.status = 'stable'
        curr.reliability_score = 80.0
        curr.width = 1920
        curr.height = 1080
        
        # Candidate: 85 (Diff 5 < 10)
        cand = StreamInfo(url='http://s2', name='s2', stream_id=302, channel_id=chan_id)
        cand.status = 'stable'
        cand.reliability_score = 85.0
        cand.width = 1920
        cand.height = 1080
        
        session.streams = {301: curr, 302: cand}
        self.service.session_manager.get_session.return_value = session
        self.service.session_manager.get_session_owner.return_value = session_id
        
        mock_udi = MagicMock()
        mock_get_udi.return_value = mock_udi
        mock_udi.get_channel_by_id.return_value = {'streams': [301, 302]}
        
        # IMPORTANT: Current stream IS playing
        mock_udi.get_playing_stream_ids.return_value = {301}
        
        with patch('api_utils.update_channel_streams') as mock_update:
            self.service._evaluate_session_streams(session_id)
            mock_update.assert_not_called()

    @patch('stream_monitoring_service.get_udi_manager')
    def test_stable_playing_protection_overcome(self, mock_get_udi):
        """Test that protection is Overcome if candidate is SIGNIFICANTLY better."""
        session_id = 'sess4'
        chan_id = 4
        
        session = SessionInfo(
            session_id=session_id, 
            channel_id=chan_id, 
            channel_name="test_channel",
            regex_filter="test",
            created_at=time.time(),
            is_active=True,
            streams={}
        )
        
        curr = StreamInfo(url='http://s1', name='s1', stream_id=401, channel_id=chan_id)
        curr.status = 'stable'
        curr.reliability_score = 70.0
        curr.width = 1920
        curr.height = 1080
        
        # Candidate: 90 (Diff 20 > 10)
        cand = StreamInfo(url='http://s2', name='s2', stream_id=402, channel_id=chan_id)
        cand.status = 'stable'
        cand.reliability_score = 90.0
        cand.width = 1920
        cand.height = 1080
        
        session.streams = {401: curr, 402: cand}
        self.service.session_manager.get_session.return_value = session
        self.service.session_manager.get_session_owner.return_value = session_id
        
        mock_udi = MagicMock()
        mock_get_udi.return_value = mock_udi
        mock_udi.get_channel_by_id.return_value = {'streams': [401, 402]}
        
        # IMPORTANT: Current stream IS playing
        mock_udi.get_playing_stream_ids.return_value = {401}
        
        with patch('api_utils.update_channel_streams') as mock_update:
            mock_update.return_value = True
            self.service._evaluate_session_streams(session_id)
            
            mock_update.assert_called()
            new_order = mock_update.call_args[0][1]
            self.assertEqual(new_order[0], 402, "Should switch despite protection because score diff is high")

if __name__ == '__main__':
    unittest.main()
