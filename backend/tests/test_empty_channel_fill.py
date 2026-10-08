
import unittest
from unittest.mock import MagicMock, patch
from apps.stream.stream_monitoring_service import StreamMonitoringService
from apps.stream.stream_session_manager import SessionInfo, StreamInfo

class TestEmptyChannelFill(unittest.TestCase):
    @patch('stream_monitoring_service.get_session_manager')
    @patch('stream_monitoring_service.get_screenshot_service')
    @patch('stream_monitoring_service.get_udi_manager')
    def setUp(self, mock_get_udi, mock_get_screenshot, mock_get_manager):
        self.mock_session_manager = MagicMock()
        mock_get_manager.return_value = self.mock_session_manager
        self.mock_udi = MagicMock()
        mock_get_udi.return_value = self.mock_udi
        
        # Reset singleton
        StreamMonitoringService._instance = None
        self.service = StreamMonitoringService()
        self.service.session_manager = self.mock_session_manager

    @patch('api_utils.update_channel_streams')
    def test_fill_empty_channel(self, mock_update):
        # 1. Setup Session with STABLE streams
        session = SessionInfo(
            session_id="test_fill",
            channel_id=99,
            channel_name="Empty Channel",
            regex_filter=".*",
            created_at=1000,
            is_active=True,
            evaluation_interval_ms=1000,
            streams={}
        )
        
        s1 = StreamInfo(
            stream_id=101, url="http://1", name="s1", channel_id=99,
            status='stable', reliability_score=100.0, width=1920, height=1080
        )
        s2 = StreamInfo(
            stream_id=102, url="http://2", name="s2", channel_id=99,
            status='stable', reliability_score=90.0, width=1920, height=1080
        )
        
        session.streams = {101: s1, 102: s2}
        self.mock_session_manager.get_session.return_value = session
        self.mock_session_manager.get_session_owner.return_value = session.session_id
        
        # 2. Mock UDI to return EMPTY channel (no streams currently in channel)
        # get_channel_by_id returns dict with 'streams' list
        self.mock_udi.get_channel_by_id.return_value = {'streams': []}
        
        # 3. Trigger Evaluation
        self.service._evaluate_session_streams("test_fill")
        
        # 4. Verify Update
        # It SHOULD call update_channel_streams with [101, 102]
        mock_update.assert_called_once()
        args, _ = mock_update.call_args
        channel_id = args[0]
        new_order = args[1]
        
        self.assertEqual(channel_id, 99)
        self.assertEqual(new_order, [101, 102])
        print("Test passed: Empty channel was filled with stable streams.")

if __name__ == '__main__':
    unittest.main()
