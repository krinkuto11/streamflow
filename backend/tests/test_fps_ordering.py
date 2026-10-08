
import unittest
from unittest.mock import MagicMock, patch
from apps.stream.stream_monitoring_service import StreamMonitoringService
from apps.stream.stream_session_manager import SessionInfo, StreamInfo

class TestFPSOrdering(unittest.TestCase):
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
    def test_fps_preference(self, mock_update):
        # Setup session with 2 streams: same resolution, different FPS, similar score
        session = SessionInfo(
            session_id="test_fps",
            channel_id=1,
            channel_name="Test",
            regex_filter=".*",
            created_at=1000,
            is_active=True,
            evaluation_interval_ms=1000,
            streams={}
        )
        
        # Stream 1: 1080p 30fps, Score 90
        s1 = StreamInfo(
            stream_id=101, url="http://1", name="s1", channel_id=1,
            width=1920, height=1080, fps=30,
            status='stable', reliability_score=90.0
        )
        
        # Stream 2: 1080p 60fps, Score 89 (slightly lower score)
        # Should win due to FPS preference in tier sort
        s2 = StreamInfo(
            stream_id=102, url="http://2", name="s2", channel_id=1,
            width=1920, height=1080, fps=60,
            status='stable', reliability_score=89.0
        )
        
        session.streams = {101: s1, 102: s2}
        self.mock_session_manager.get_session.return_value = session
        self.mock_session_manager.get_session_owner.return_value = session.session_id
        
        # Mock UDI to return current order (s1 is primary)
        self.mock_udi.get_channel_by_id.return_value = {'streams': [101, 102]}
        
        # Trigger evaluation
        self.service._evaluate_session_streams("test_fps")
        
        # Expect switch to 102 (60fps) because 101 (30fps) is current
        # Wait, Hysteresis Logic:
        # Current = 101 (30fps). Proposed = 102 (60fps).
        # Score diff = 89 - 90 = -1. < 10.
        # Resolution match.
        # FPS check: prop (60) > curr (30).
        # Logic: if prop_fps <= curr_fps: keep current.
        # Since 60 > 30, we do NOT force keep current. We allow switch.
        
        mock_update.assert_called()
        args, _ = mock_update.call_args
        new_order = args[1]
        self.assertEqual(new_order[0], 102, "Should switch to 60fps stream")

    @patch('api_utils.update_channel_streams')
    def test_fps_hysteresis_holds(self, mock_update):
        # Current is 60fps. Proposed is 30fps. Similar score.
        session = SessionInfo(
            session_id="test_fps_hold",
            channel_id=1,
            channel_name="Test",
            regex_filter=".*",
            created_at=1000,
            is_active=True,
            evaluation_interval_ms=1000,
            streams={}
        )
        
        s1 = StreamInfo(
            stream_id=101, url="http://1", name="s1", channel_id=1,
            width=1920, height=1080, fps=60,
            status='stable', reliability_score=90.0
        )
        
        # Proposed: 30fps, Score 92 (better score, but < 10 diff)
        s2 = StreamInfo(
            stream_id=102, url="http://2", name="s2", channel_id=1,
            width=1920, height=1080, fps=30,
            status='stable', reliability_score=92.0
        )
        
        session.streams = {101: s1, 102: s2}
        self.mock_session_manager.get_session.return_value = session
        
        # Current is 101 (60fps)
        self.mock_udi.get_channel_by_id.return_value = {'streams': [101, 102]}
        
        self.service._evaluate_session_streams("test_fps_hold")
        
        # Logic:
        # Initial sort: s2 (92) > s1 (90). s2 is top candidate.
        # Hysteresis:
        # Current = 101 (60fps). Proposed = 102 (30fps).
        # Score diff = 92 - 90 = 2. < 10.
        # Resolution match.
        # FPS check: prop (30) <= curr (60).
        # Logic: keep current (101).
        
        # update_channel_streams should NOT be called (or called with 101 first)
        # If order doesn't change, it returns early.
        mock_update.assert_not_called()

if __name__ == '__main__':
    unittest.main()
