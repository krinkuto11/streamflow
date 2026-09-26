#!/usr/bin/env python3
"""
Test suite for Stream Monitoring Session Synchronization and Ownership.
"""

import os
import sys
import tempfile
import shutil
import unittest
from unittest.mock import Mock, patch, MagicMock
import time

# Add backend to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

# Mock the UDI manager before importing modules that use it
mock_udi = MagicMock()

class TestSessionSync(unittest.TestCase):
    """Test session synchronization and ownership."""

    def setUp(self):
        """Set up test fixtures."""
        # Create a temporary directory for test data
        self.test_dir = tempfile.mkdtemp()
        os.environ['CONFIG_DIR'] = self.test_dir
        
        self.channel_id = 999
        self.session_id = f"session_{self.channel_id}_1234567890"
        
        # Reset simpletons for clean state
        from apps.stream.stream_session_manager import StreamSessionManager
        StreamSessionManager._instance = None
        
        from apps.stream.stream_monitoring_service import StreamMonitoringService
        StreamMonitoringService._instance = None

    def tearDown(self):
        """Clean up test fixtures."""
        # Remove temporary directory
        if os.path.exists(self.test_dir):
            shutil.rmtree(self.test_dir)

    @patch('stream_session_manager.get_udi_manager')
    def test_exclusive_ownership(self, mock_udi_func):
        """Test that sessions enforce exclusive ownership of channels."""
        from apps.stream.stream_session_manager import get_session_manager
        
        # Setup mocks
        mock_udi_instance = Mock()
        mock_udi_instance.get_channel_by_id.return_value = {
            'id': self.channel_id, 'name': 'Test Channel'
        }
        mock_udi_func.return_value = mock_udi_instance
        
        manager = get_session_manager()
        
        # Create first session
        session1_id = manager.create_session(self.channel_id, regex_filter=".*")
        
        # Start first session -> Should succeed and claim ownership
        with patch.object(manager, '_discover_streams'), \
             patch.object(manager, '_save_sessions'):
            success = manager.start_session(session1_id)
            self.assertTrue(success)
            self.assertEqual(manager.get_session_owner(self.channel_id), session1_id)
            
        # Create second session for SAME channel
        # Note: create_session usually returns existing session ID if active, 
        # but we can force create a new one if allow_duplicate_channel (not exposed but possible)
        # OR we can manually create a session object and try to start it.
        
        # Let's manually register a second session to test the start_session logic
        session2_id = f"session_{self.channel_id}_9999999999"
        from apps.stream.stream_session_manager import SessionInfo
        session2 = SessionInfo(
            session_id=session2_id,
            channel_id=self.channel_id,
            channel_name="Test Channel",
            regex_filter=".*",
            created_at=time.time(),
            is_active=False
        )
        manager.sessions[session2_id] = session2
        manager.session_locks[session2_id] = Mock()
        manager.session_locks[session2_id].__enter__ = Mock()
        manager.session_locks[session2_id].__exit__ = Mock()
        
        # Try to start second session -> Should fail because channel is owned
        with patch.object(manager, '_discover_streams'), \
             patch.object(manager, '_save_sessions'):
            success = manager.start_session(session2_id)
            self.assertFalse(success)
            
        # Stop first session -> Should release ownership
        with patch('stream_monitoring_service.get_monitoring_service') as mock_ms_get:
            mock_ms = Mock()
            mock_ms_get.return_value = mock_ms
            with patch.object(manager, '_save_sessions'):
                manager.stop_session(session1_id)
                self.assertIsNone(manager.get_session_owner(self.channel_id))

    @patch('stream_monitoring_service.get_udi_manager')
    def test_sync_enforcement_trigger(self, mock_get_udi):
        """A due tick verifies remote state and evaluates without a forced write."""
        from apps.stream.stream_monitoring_service import StreamMonitoringService
        from apps.stream.stream_session_manager import SessionInfo
        
        service = StreamMonitoringService()
        
        # Mock session manager
        service.session_manager = Mock()
        service.session_manager.get_session_owner.return_value = "session_1"
        
        # Setup session
        session = Mock()
        session.session_id = "session_1"
        session.channel_id = 999
        session.enforce_sync_interval_ms = 1000 # 1 second
        session.last_sync_time = time.time() - 2.0 # 2 seconds ago (should trigger)
        session.last_authoritative_sync_time = 0.0
        
        # Mock _evaluate_session_streams
        service._evaluate_session_streams = Mock()
        
        # Run check
        with patch.object(service.io_pool, 'submit', side_effect=lambda fn, *args: fn(*args)):
            triggered = service._check_sync_enforcement(session)
        
        self.assertTrue(triggered)
        mock_get_udi.return_value.refresh_channel_by_id.assert_called_once_with(999)
        service._evaluate_session_streams.assert_called_with("session_1", force_update=False)
        # Verify time updated (approximate)
        self.assertAlmostEqual(session.last_sync_time, time.time(), delta=1.0)
        
    # test_sync_enforcement_alien_stream_removal removed as it was replaced by test_alien_stream_removal_mocked

    @patch('api_utils.update_channel_streams')
    @patch('stream_monitoring_service.get_udi_manager')
    def test_alien_stream_removal_mocked(self, mock_get_udi, mock_update_streams):
        """Test alien stream removal with correct mocking."""
        from apps.stream.stream_monitoring_service import StreamMonitoringService
        from apps.stream.stream_session_manager import StreamInfo
        
        service = StreamMonitoringService()
        
        # Setup session with ONE known stream
        session = Mock()
        session.session_id = "session_1"
        session.channel_id = 999
        session.channel_name = "Test"
        stream1 = StreamInfo(stream_id=1, url="http://1", name="Stream 1", channel_id=999, status='stable', reliability_score=100)
        session.streams = {1: stream1}
        
        service.session_manager = Mock()
        service.session_manager.get_session.return_value = session
        service.session_manager.get_session_owner.return_value = session.session_id
        
        # Mock UDI: Channel has session stream (1) AND alien stream (9999)
        mock_udi = Mock()
        mock_udi.get_channel_by_id.return_value = {
            'id': 999,
            'streams': [1, 9999] # 9999 is the alien
        }
        mock_udi.get_playing_stream_ids.return_value = []
        mock_get_udi.return_value = mock_udi
        
        # Mock logging to avoid clutter
        service.last_switch_times = {}
        
        # Call evaluate
        service._evaluate_session_streams("session_1", force_update=True)
        
        # Verify usage
        mock_update_streams.assert_called()
        call_args = mock_update_streams.call_args
        channel_id, new_stream_ids = call_args[0]
        
        self.assertEqual(channel_id, 999)
        self.assertIn(1, new_stream_ids)
        self.assertNotIn(9999, new_stream_ids) # Alien must be gone

if __name__ == '__main__':
    unittest.main()
