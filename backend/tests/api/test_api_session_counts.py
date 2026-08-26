
import unittest
import json
from unittest.mock import MagicMock, patch
import sys
import os
from pathlib import Path

# Add backend to path so the real compatibility modules are imported.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from web_api import app
from apps.stream.stream_session_manager import StreamInfo, SessionInfo

class TestApiSessionCounts(unittest.TestCase):
    def setUp(self):
        self.app = app.test_client()
        self.app.testing = True

    @patch('web_api.get_session_manager')
    def test_get_stream_sessions_includes_counts(self, mock_get_manager):
        # Setup mock session with streams in different states
        mock_manager = MagicMock()
        mock_get_manager.return_value = mock_manager
        
        session_id = 'session_counts_test'
        
        # Create mock stream infos
        s1 = StreamInfo(stream_id=1, url="http://1", name="S1", channel_id=100, status='stable')
        s2 = StreamInfo(stream_id=2, url="http://2", name="S2", channel_id=100, status='review')
        s3 = StreamInfo(stream_id=3, url="http://3", name="S3", channel_id=100, status='quarantined')
        s4 = StreamInfo(stream_id=4, url="http://4", name="S4", channel_id=100, status='stable')
        
        # Create a mock session info
        session_info = SessionInfo(
            session_id=session_id,
            channel_id=100,
            channel_name="Test Channel",
            regex_filter=".*",
            created_at=1234567890,
            is_active=True,
            streams={1: s1, 2: s2, 3: s3, 4: s4}
        )
        
        # Mock get_all_sessions to return our list
        mock_manager.get_all_sessions.return_value = [session_info]
        
        # Call the API
        response = self.app.get('/api/stream-sessions')
        
        # Check response
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.data)
        
        self.assertEqual(len(data), 1)
        session_data = data[0]
        
        # Verify counts
        # Current implementation only has 'stream_count' and 'active_streams'
        # We expect these new fields to be present after our fix
        self.assertEqual(session_data.get('stream_count'), 4)
        
        # These assertions will fail initially
        self.assertIn('stable_count', session_data)
        self.assertEqual(session_data['stable_count'], 2)
        
        self.assertIn('review_count', session_data)
        self.assertEqual(session_data['review_count'], 1)
        
        self.assertIn('quarantined_count', session_data)
        self.assertEqual(session_data['quarantined_count'], 1)

if __name__ == '__main__':
    unittest.main()
