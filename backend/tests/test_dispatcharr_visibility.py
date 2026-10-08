
import unittest
from unittest.mock import MagicMock, patch
import sys
import os

# Add backend to path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Mock dotenv before importing modules that need it
sys.modules['dotenv'] = MagicMock()

from apps.stream.stream_monitoring_service import StreamMonitoringService
from apps.stream.stream_session_manager import SessionInfo, StreamInfo

class TestDispatcharrVisibility(unittest.TestCase):
    @patch('stream_monitoring_service.get_session_manager')
    @patch('stream_monitoring_service.get_screenshot_service')
    def setUp(self, mock_get_screenshot, mock_get_manager):
        self.mock_session_manager = MagicMock()
        mock_get_manager.return_value = self.mock_session_manager
        
        # Mock screenshot service
        self.mock_screenshot_service = MagicMock()
        mock_get_screenshot.return_value = self.mock_screenshot_service
        
        # StreamMonitoringService is a singleton and init takes no args
        self.service = StreamMonitoringService()
        # Ensure we use the mock
        self.service.session_manager = self.mock_session_manager
        
    @patch('api_utils.update_channel_streams')
    @patch('stream_monitoring_service.get_udi_manager')
    def test_evaluate_filters_review_streams(self, mock_get_udi, mock_update_streams):
        # Setup session with 1 Stable and 1 Review stream
        session = SessionInfo(
            session_id='test_sess',
            channel_id=1,
            channel_name='Test',
            regex_filter='.*',
            is_active=True,
            created_at=0,
            streams={}
        )
        
        # Stable stream (should be included)
        stable_stream = StreamInfo(
            stream_id=101, url='http://s1', name='Stable', channel_id=1,
            status='stable', reliability_score=100
        )
        # Review stream (should be excluded)
        review_stream = StreamInfo(
            stream_id=102, url='http://s2', name='Review', channel_id=1,
            status='review', reliability_score=50
        )
        
        session.streams[101] = stable_stream
        session.streams[102] = review_stream
        
        self.mock_session_manager.get_session.return_value = session
        self.mock_session_manager.get_session_owner.return_value = session.session_id
        
        # Mock UDI to return current streams (including both, as if they were there)
        mock_udi = MagicMock()
        mock_channel = {'streams': [101, 102]} 
        mock_udi.get_channel_by_id.return_value = mock_channel
        mock_get_udi.return_value = mock_udi
        
        # Mock update returning True
        mock_update_streams.return_value = True
        
        # Run evaluation
        self.service._evaluate_session_streams('test_sess')
        
        # Verify update_channel_streams called with ONLY stable stream [101]
        mock_update_streams.assert_called_once()
        args, kwargs = mock_update_streams.call_args
        channel_id, new_order = args
        
        self.assertEqual(channel_id, 1)
        self.assertIn(101, new_order)
        self.assertNotIn(102, new_order)
        self.assertEqual(kwargs['expected_current_stream_ids'], [101, 102])
        print(f"\n[PASS] Dispatcharr Update called with: {new_order} (Review stream 102 excluded)")

if __name__ == '__main__':
    unittest.main()
