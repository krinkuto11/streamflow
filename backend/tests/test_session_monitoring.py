#!/usr/bin/env python3
"""
Test suite for Stream Monitoring Session functionality.

Tests the session creation, EPG event attachment, and monitoring features.
"""

import os
import sys
import unittest
from unittest.mock import Mock, patch, MagicMock
from datetime import datetime
import tempfile
import shutil

# Add backend to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

# Mock the UDI manager before importing modules that use it
mock_udi = MagicMock()


class TestStreamMonitoring(unittest.TestCase):
    """Test stream monitoring session functionality."""

    def setUp(self):
        """Set up test fixtures."""
        # Create a temporary directory for test data
        self.test_dir = tempfile.mkdtemp()
        os.environ['CONFIG_DIR'] = self.test_dir

    def tearDown(self):
        """Clean up test fixtures."""
        # Remove temporary directory
        if os.path.exists(self.test_dir):
            shutil.rmtree(self.test_dir)

    def test_legacy_singleton_initializes_runtime_refresh_state(self):
        """Test reused manager instances recover volatile refresh state."""
        from apps.stream.stream_session_manager import StreamSessionManager

        manager = StreamSessionManager()
        if hasattr(manager, '_last_streams_refresh'):
            delattr(manager, '_last_streams_refresh')

        manager._ensure_runtime_state()

        self.assertEqual(manager._last_streams_refresh, 0)

    @patch('stream_session_manager.get_udi_manager')
    @patch('stream_session_manager.get_dispatcharr_config')
    def test_session_creation_with_epg_event(self, mock_config, mock_udi):
        """Test creating a session with EPG event attachment."""
        # Mock UDI manager response
        mock_udi_instance = Mock()
        mock_udi_instance.get_channel_by_id.return_value = {
            'id': 1,
            'name': 'Test Channel',
            'tvg_id': 'test-channel',
            'logo_id': 123
        }
        mock_udi.return_value = mock_udi_instance

        # Mock dispatcharr config
        mock_config_instance = Mock()
        mock_config_instance.get_base_url.return_value = 'http://localhost:9191'
        mock_config.return_value = mock_config_instance

        # Import after mocking
        from apps.stream.stream_session_manager import get_session_manager

        session_manager = get_session_manager()

        # Create EPG event
        epg_event = {
            'id': 1,
            'title': 'Test Program',
            'start_time': '2024-01-01T20:00:00Z',
            'end_time': '2024-01-01T21:00:00Z',
            'description': 'A test program'
        }

        # Create session with EPG event
        session_id = session_manager.create_session(
            channel_id=1,
            regex_filter='.*test.*',
            pre_event_minutes=30,
            epg_event=epg_event
        )

        # Verify session was created
        self.assertIsNotNone(session_id)
        self.assertTrue(session_id.startswith('session_1_'))

        # Get the session and verify fields
        session = session_manager.get_session(session_id)
        self.assertIsNotNone(session)
        self.assertEqual(session.channel_id, 1)
        self.assertEqual(session.channel_name, 'Test Channel')
        self.assertEqual(session.regex_filter, '.*test.*')
        self.assertEqual(session.pre_event_minutes, 30)
        
        # Verify EPG event fields
        self.assertEqual(session.epg_event_id, 1)
        self.assertEqual(session.epg_event_title, 'Test Program')
        self.assertEqual(session.epg_event_start, '2024-01-01T20:00:00Z')
        self.assertEqual(session.epg_event_end, '2024-01-01T21:00:00Z')
        self.assertEqual(session.epg_event_description, 'A test program')
        
        # Verify channel info fields
        self.assertEqual(session.channel_tvg_id, 'test-channel')
        self.assertEqual(session.channel_logo_url, 'http://localhost:9191/api/channels/logos/123/')

    @patch('stream_session_manager.get_udi_manager')
    @patch('stream_session_manager.get_dispatcharr_config')
    def test_session_without_epg_event(self, mock_config, mock_udi):
        """Test creating a session without EPG event."""
        # Mock UDI manager response
        mock_udi_instance = Mock()
        mock_udi_instance.get_channel_by_id.return_value = {
            'id': 2,
            'name': 'Channel Two',
            'tvg_id': 'channel-two'
        }
        mock_udi.return_value = mock_udi_instance

        # Mock dispatcharr config
        mock_config_instance = Mock()
        mock_config_instance.get_base_url.return_value = 'http://localhost:9191'
        mock_config.return_value = mock_config_instance

        # Import after mocking
        from apps.stream.stream_session_manager import get_session_manager

        session_manager = get_session_manager()

        # Create session without EPG event
        session_id = session_manager.create_session(
            channel_id=2,
            regex_filter='.*'
        )

        # Verify session was created
        self.assertIsNotNone(session_id)

        # Get the session and verify fields
        session = session_manager.get_session(session_id)
        self.assertIsNotNone(session)
        
        # Verify EPG event fields are None
        self.assertIsNone(session.epg_event_id)
        self.assertIsNone(session.epg_event_title)
        self.assertIsNone(session.epg_event_start)
        self.assertIsNone(session.epg_event_end)

    def test_dictionary_iteration_fix(self):
        """Test that the dictionary iteration bug is fixed."""
        # This test verifies that we don't get RuntimeError when monitors dict changes during iteration
        from apps.stream.stream_monitoring_service import StreamMonitoringService
        
        service = StreamMonitoringService()
        
        # Create a mock session with monitors
        service.monitors = {
            'session_1': {
                1: Mock(get_stats=Mock(return_value=Mock(
                    speed=1.0, bitrate=5000, fps=30, is_alive=True, last_updated=0
                )), is_buffering=Mock(return_value=False), stop=Mock()),
                2: Mock(get_stats=Mock(return_value=Mock(
                    speed=1.0, bitrate=5000, fps=30, is_alive=True, last_updated=0
                )), is_buffering=Mock(return_value=False), stop=Mock())
            }
        }
        
        # Mock session manager
        with patch.object(service, 'session_manager') as mock_sm:
            mock_session = Mock()
            mock_session.streams = {
                1: Mock(is_quarantined=False, metrics_history=[]),
                2: Mock(is_quarantined=False, metrics_history=[])
            }
            mock_session.timeout_ms = 30000
            mock_sm.get_session.return_value = mock_session
            mock_sm.scoring_windows = {'session_1': {1: Mock(), 2: Mock()}}
            
            # This should not raise RuntimeError
            try:
                service._evaluate_session_streams('session_1')
            except RuntimeError as e:
                if 'dictionary changed size during iteration' in str(e):
                    self.fail("Dictionary iteration error still occurs!")
                raise


if __name__ == '__main__':
    unittest.main()
