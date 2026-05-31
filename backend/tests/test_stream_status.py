
import unittest
import sys
import time
from unittest.mock import MagicMock

# Mock external dependencies before import
sys.modules['udi'] = MagicMock()
sys.modules['logging_config'] = MagicMock()
sys.modules['api_utils'] = MagicMock()

# Add backend to path
import os
from pathlib import Path
sys.path.append(str(Path(__file__).parent.parent))

import apps.stream.stream_session_manager as stream_session_manager
from apps.stream.stream_session_manager import StreamInfo, SessionInfo, StreamSessionManager

# Patch CONFIG_DIR to avoid mkdir calls
stream_session_manager.CONFIG_DIR = MagicMock()
stream_session_manager.SESSION_DATA_FILE = MagicMock()
stream_session_manager.SESSION_DATA_FILE.exists.return_value = False

class TestStreamStatus(unittest.TestCase):
    def test_stream_info_defaults(self):
        """Test default status is 'review'"""
        info = StreamInfo(
            stream_id=1,
            url="http://test",
            name="Test",
            channel_id=100
        )
        self.assertEqual(info.status, 'review')
        self.assertFalse(info.is_quarantined)
        
    def test_is_quarantined_property(self):
        """Test is_quarantined property getter/setter"""
        info = StreamInfo(
            stream_id=1,
            url="http://test",
            name="Test",
            channel_id=100
        )
        
        # Test setter true
        info.is_quarantined = True
        self.assertEqual(info.status, 'quarantined')
        self.assertTrue(info.is_quarantined)
        
        # Test default last_status_change
        first_change = info.last_status_change
        self.assertGreater(first_change, 0)
        
        time.sleep(0.1)
        
        # Test setter true again (should not update timestamp)
        info.is_quarantined = True
        self.assertEqual(info.last_status_change, first_change)
        
        time.sleep(0.1)
        
        # Test setter false (moves to review)
        info.is_quarantined = False
        self.assertEqual(info.status, 'review')
        self.assertFalse(info.is_quarantined)
        self.assertNotEqual(info.last_status_change, first_change)
        
    def test_deserialization_migration(self):
        """Test migration from is_quarantined bool to status str"""
        # Reset singleton just in case
        StreamSessionManager._instance = None
        
        # Mock load/save methods to avoid file I/O
        StreamSessionManager._load_sessions = MagicMock()
        StreamSessionManager._save_sessions = MagicMock()
        
        manager = StreamSessionManager()
        
        # Mock legacy data
        legacy_data = {
            'session_id': 'test_session',
            'channel_id': 100,
            'channel_name': 'Test Channel',
            'regex_filter': '.*',
            'created_at': 1234567890,
            'is_active': True,
            'streams': {
                '1': {
                    'stream_id': 1,
                    'url': 'http://test1',
                    'name': 'Stream 1',
                    'channel_id': 100,
                    'is_quarantined': True # Legacy field
                },
                '2': {
                    'stream_id': 2,
                    'url': 'http://test2',
                    'name': 'Stream 2',
                    'channel_id': 100,
                    # No is_quarantined field (default behavior)
                }
            }
        }
        
        session = manager._deserialize_session(legacy_data)
        
        # Check stream 1 (quarantined)
        s1 = session.streams[1]
        self.assertEqual(s1.status, 'quarantined')
        
        # Check stream 2 (not quarantined -> default review)
        s2 = session.streams[2]
        self.assertEqual(s2.status, 'review')

if __name__ == '__main__':
    unittest.main()
