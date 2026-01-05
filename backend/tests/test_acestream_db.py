"""
Tests for AceStream database operations.

This module tests the AceStreamDatabase class functionality including:
- Database initialization
- Session management
- Metrics storage and retrieval
- Data cleanup
"""

import unittest
import tempfile
import os
from datetime import datetime, timedelta
import sys

# Add parent directory to path to import the module
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from acestream_db import AceStreamDatabase


class TestAceStreamDatabase(unittest.TestCase):
    """Test cases for AceStream database operations."""
    
    def setUp(self):
        """Set up test database before each test."""
        # Create a temporary database file
        self.db_fd, self.db_path = tempfile.mkstemp(suffix='.db')
        self.db = AceStreamDatabase(db_path=self.db_path)
    
    def tearDown(self):
        """Clean up test database after each test."""
        os.close(self.db_fd)
        os.unlink(self.db_path)
    
    def test_database_initialization(self):
        """Test that database tables are created properly."""
        import sqlite3
        
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            
            # Check monitoring_sessions table exists
            cursor.execute("""
                SELECT name FROM sqlite_master 
                WHERE type='table' AND name='monitoring_sessions'
            """)
            self.assertIsNotNone(cursor.fetchone())
            
            # Check stream_metrics table exists
            cursor.execute("""
                SELECT name FROM sqlite_master 
                WHERE type='table' AND name='stream_metrics'
            """)
            self.assertIsNotNone(cursor.fetchone())
            
            # Check indexes exist
            cursor.execute("""
                SELECT name FROM sqlite_master 
                WHERE type='index' AND name='idx_metrics_session_time'
            """)
            self.assertIsNotNone(cursor.fetchone())
    
    def test_create_session(self):
        """Test creating a monitoring session."""
        session_id = self.db.create_session(
            stream_id=1,
            channel_id=10,
            acestream_id='abc123',
            command_url='http://test/command',
            stat_url='http://test/stat'
        )
        
        self.assertIsNotNone(session_id)
        self.assertIsInstance(session_id, int)
        self.assertGreater(session_id, 0)
    
    def test_get_active_session(self):
        """Test retrieving an active session."""
        # Create a session
        session_id = self.db.create_session(
            stream_id=1,
            channel_id=10,
            acestream_id='abc123'
        )
        
        # Retrieve it
        session = self.db.get_active_session(stream_id=1)
        
        self.assertIsNotNone(session)
        self.assertEqual(session['id'], session_id)
        self.assertEqual(session['stream_id'], 1)
        self.assertEqual(session['channel_id'], 10)
        self.assertEqual(session['acestream_id'], 'abc123')
        self.assertEqual(session['status'], 'active')
    
    def test_close_session(self):
        """Test closing a monitoring session."""
        # Create a session
        session_id = self.db.create_session(
            stream_id=1,
            channel_id=10,
            acestream_id='abc123'
        )
        
        # Close it
        self.db.close_session(session_id)
        
        # Verify it's closed
        session = self.db.get_active_session(stream_id=1)
        self.assertIsNone(session)
    
    def test_save_metrics(self):
        """Test saving stream metrics."""
        # Create a session
        session_id = self.db.create_session(
            stream_id=1,
            channel_id=10,
            acestream_id='abc123'
        )
        
        # Save metrics
        orchestrator_stats = {
            'peers': 25,
            'speed_down': 5000,
            'speed_up': 100,
            'downloaded': 1000000,
            'uploaded': 50000,
            'livepos': {'buffer_pieces': 50}
        }
        
        ffmpeg_stats = {
            'bitrate': 3500,
            'resolution': '1920x1080',
            'fps': 25.0,
            'errors': 0
        }
        
        self.db.save_metrics(
            session_id=session_id,
            health_score=85.5,
            orchestrator_stats=orchestrator_stats,
            ffmpeg_stats=ffmpeg_stats
        )
        
        # Verify metrics were saved
        health = self.db.get_latest_stream_health(stream_id=1)
        self.assertIsNotNone(health)
        self.assertEqual(health['peers'], 25)
        self.assertEqual(health['speed_down'], 5000)
        self.assertEqual(health['health_score'], 85.5)
    
    def test_get_channel_metrics(self):
        """Test retrieving aggregated channel metrics."""
        # Create sessions and add metrics
        session1 = self.db.create_session(1, 10, 'abc123')
        session2 = self.db.create_session(2, 10, 'def456')
        
        # Add metrics for both streams
        for session_id in [session1, session2]:
            self.db.save_metrics(
                session_id=session_id,
                health_score=80.0,
                orchestrator_stats={'peers': 20, 'speed_down': 4000},
                ffmpeg_stats={'bitrate': 3000}
            )
        
        # Get channel metrics
        metrics = self.db.get_channel_metrics(channel_id=10, hours=1)
        
        # Should have at least one data point
        self.assertGreater(len(metrics), 0)
        
        # Check that metrics contain expected fields
        if metrics:
            metric = metrics[0]
            self.assertIn('timestamp', metric)
            self.assertIn('avg_health_score', metric)
            self.assertIn('avg_peers', metric)
    
    def test_get_active_sessions_for_channel(self):
        """Test getting all active sessions for a channel."""
        # Create multiple sessions for same channel
        session1 = self.db.create_session(1, 10, 'abc123')
        session2 = self.db.create_session(2, 10, 'def456')
        session3 = self.db.create_session(3, 20, 'ghi789')  # Different channel
        
        # Get sessions for channel 10
        sessions = self.db.get_active_sessions_for_channel(channel_id=10)
        
        self.assertEqual(len(sessions), 2)
        session_ids = [s['id'] for s in sessions]
        self.assertIn(session1, session_ids)
        self.assertIn(session2, session_ids)
        self.assertNotIn(session3, session_ids)
    
    def test_close_all_active_sessions(self):
        """Test closing all active sessions."""
        # Create multiple sessions
        self.db.create_session(1, 10, 'abc123')
        self.db.create_session(2, 10, 'def456')
        self.db.create_session(3, 20, 'ghi789')
        
        # Close all
        self.db.close_all_active_sessions()
        
        # Verify all are closed
        self.assertIsNone(self.db.get_active_session(1))
        self.assertIsNone(self.db.get_active_session(2))
        self.assertIsNone(self.db.get_active_session(3))
    
    def test_cleanup_old_metrics(self):
        """Test cleaning up old metrics."""
        # This test is simplified since we can't easily manipulate timestamps
        # Just verify the method runs without error
        deleted = self.db.cleanup_old_metrics(days=7)
        self.assertIsInstance(deleted, int)
        self.assertGreaterEqual(deleted, 0)


if __name__ == '__main__':
    unittest.main()
