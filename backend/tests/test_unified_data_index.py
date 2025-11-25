#!/usr/bin/env python3
"""
Unit tests for the Unified Data Index (UDI) module.

This test module verifies:
1. Database initialization and schema creation
2. Data insertion and retrieval (streams, channels, accounts)
3. Pending changes tracking
4. Changelog functionality
5. Index rebuild operations
"""

import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock

# Add backend to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestUnifiedDataIndexInitialization(unittest.TestCase):
    """Test database initialization and schema creation."""
    
    def setUp(self):
        """Set up test fixtures."""
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = Path(self.temp_dir) / 'test_index.db'
    
    def tearDown(self):
        """Clean up test fixtures."""
        shutil.rmtree(self.temp_dir, ignore_errors=True)
        # Reset singleton
        import unified_data_index
        unified_data_index._udi_instance = None
        unified_data_index.UnifiedDataIndex._instance = None
    
    def test_database_creation(self):
        """Test that database is created on initialization."""
        from unified_data_index import UnifiedDataIndex
        
        udi = UnifiedDataIndex(db_path=self.db_path)
        
        self.assertTrue(self.db_path.exists())
    
    def test_schema_tables_created(self):
        """Test that all required tables are created."""
        from unified_data_index import UnifiedDataIndex
        import sqlite3
        
        udi = UnifiedDataIndex(db_path=self.db_path)
        
        conn = sqlite3.connect(str(self.db_path))
        cursor = conn.cursor()
        
        # Check required tables exist
        cursor.execute("""
            SELECT name FROM sqlite_master 
            WHERE type='table' 
            ORDER BY name
        """)
        tables = {row[0] for row in cursor.fetchall()}
        
        required_tables = {
            'm3u_accounts', 'channel_groups', 'channels', 'streams',
            'channel_streams', 'pending_changes', 'changelog', 'index_metadata'
        }
        
        for table in required_tables:
            self.assertIn(table, tables, f"Table {table} not found")
        
        conn.close()


class TestUnifiedDataIndexStreams(unittest.TestCase):
    """Test stream-related operations."""
    
    def setUp(self):
        """Set up test fixtures."""
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = Path(self.temp_dir) / 'test_index.db'
        
        # Reset singleton before each test
        import unified_data_index
        unified_data_index._udi_instance = None
        unified_data_index.UnifiedDataIndex._instance = None
    
    def tearDown(self):
        """Clean up test fixtures."""
        shutil.rmtree(self.temp_dir, ignore_errors=True)
        import unified_data_index
        unified_data_index._udi_instance = None
        unified_data_index.UnifiedDataIndex._instance = None
    
    def test_get_all_streams_empty(self):
        """Test getting streams from empty database."""
        from unified_data_index import UnifiedDataIndex
        
        udi = UnifiedDataIndex(db_path=self.db_path)
        streams = udi.get_all_streams()
        
        self.assertEqual(streams, [])
    
    def test_get_valid_stream_ids_empty(self):
        """Test getting valid stream IDs from empty database."""
        from unified_data_index import UnifiedDataIndex
        
        udi = UnifiedDataIndex(db_path=self.db_path)
        ids = udi.get_valid_stream_ids()
        
        self.assertEqual(ids, set())
    
    def test_get_stream_url_mapping_empty(self):
        """Test getting stream URL mapping from empty database."""
        from unified_data_index import UnifiedDataIndex
        
        udi = UnifiedDataIndex(db_path=self.db_path)
        mapping = udi.get_stream_url_mapping()
        
        self.assertEqual(mapping, {})


class TestUnifiedDataIndexChannels(unittest.TestCase):
    """Test channel-related operations."""
    
    def setUp(self):
        """Set up test fixtures."""
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = Path(self.temp_dir) / 'test_index.db'
        
        import unified_data_index
        unified_data_index._udi_instance = None
        unified_data_index.UnifiedDataIndex._instance = None
    
    def tearDown(self):
        """Clean up test fixtures."""
        shutil.rmtree(self.temp_dir, ignore_errors=True)
        import unified_data_index
        unified_data_index._udi_instance = None
        unified_data_index.UnifiedDataIndex._instance = None
    
    def test_get_all_channels_empty(self):
        """Test getting channels from empty database."""
        from unified_data_index import UnifiedDataIndex
        
        udi = UnifiedDataIndex(db_path=self.db_path)
        channels = udi.get_all_channels()
        
        self.assertEqual(channels, [])
    
    def test_get_channel_streams_empty(self):
        """Test getting channel streams when channel doesn't exist."""
        from unified_data_index import UnifiedDataIndex
        
        udi = UnifiedDataIndex(db_path=self.db_path)
        streams = udi.get_channel_streams(999)
        
        self.assertEqual(streams, [])
    
    def test_get_channel_stream_ids_empty(self):
        """Test getting channel stream IDs when channel doesn't exist."""
        from unified_data_index import UnifiedDataIndex
        
        udi = UnifiedDataIndex(db_path=self.db_path)
        ids = udi.get_channel_stream_ids(999)
        
        self.assertEqual(ids, [])


class TestUnifiedDataIndexChanges(unittest.TestCase):
    """Test pending changes and changelog operations."""
    
    def setUp(self):
        """Set up test fixtures."""
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = Path(self.temp_dir) / 'test_index.db'
        
        import unified_data_index
        unified_data_index._udi_instance = None
        unified_data_index.UnifiedDataIndex._instance = None
    
    def tearDown(self):
        """Clean up test fixtures."""
        shutil.rmtree(self.temp_dir, ignore_errors=True)
        import unified_data_index
        unified_data_index._udi_instance = None
        unified_data_index.UnifiedDataIndex._instance = None
    
    def test_get_pending_changes_empty(self):
        """Test getting pending changes from empty database."""
        from unified_data_index import UnifiedDataIndex
        
        udi = UnifiedDataIndex(db_path=self.db_path)
        changes = udi.get_pending_changes()
        
        self.assertEqual(changes, [])
    
    def test_add_changelog_entry(self):
        """Test adding changelog entries."""
        from unified_data_index import UnifiedDataIndex
        
        udi = UnifiedDataIndex(db_path=self.db_path)
        
        udi.add_changelog_entry(
            action='test_action',
            entity_type='stream',
            entity_id=123,
            entity_name='Test Stream',
            details='{"test": "data"}',
            source='test'
        )
        
        # Get changelog
        changelog = udi.get_changelog(days=1)
        
        self.assertEqual(len(changelog), 1)
        self.assertEqual(changelog[0]['action'], 'test_action')
        self.assertEqual(changelog[0]['entity_type'], 'stream')
        self.assertEqual(changelog[0]['entity_id'], 123)
    
    def test_get_changelog_empty(self):
        """Test getting changelog from empty database."""
        from unified_data_index import UnifiedDataIndex
        
        udi = UnifiedDataIndex(db_path=self.db_path)
        changelog = udi.get_changelog()
        
        self.assertEqual(changelog, [])


class TestUnifiedDataIndexRebuild(unittest.TestCase):
    """Test index rebuild from Dispatcharr."""
    
    def setUp(self):
        """Set up test fixtures."""
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = Path(self.temp_dir) / 'test_index.db'
        
        import unified_data_index
        unified_data_index._udi_instance = None
        unified_data_index.UnifiedDataIndex._instance = None
    
    def tearDown(self):
        """Clean up test fixtures."""
        shutil.rmtree(self.temp_dir, ignore_errors=True)
        import unified_data_index
        unified_data_index._udi_instance = None
        unified_data_index.UnifiedDataIndex._instance = None
    
    def test_rebuild_with_mock_fetcher(self):
        """Test index rebuild with mock API fetcher."""
        from unified_data_index import UnifiedDataIndex
        
        udi = UnifiedDataIndex(db_path=self.db_path)
        
        # Create mock fetcher
        mock_fetcher = Mock()
        mock_fetcher.fetch_m3u_accounts.return_value = [
            {'id': 1, 'name': 'Test Account', 'is_active': True}
        ]
        mock_fetcher.fetch_channel_groups.return_value = [
            {'id': 1, 'name': 'Test Group'}
        ]
        mock_fetcher.fetch_streams.return_value = [
            {'id': 1, 'name': 'Test Stream', 'url': 'http://test.com/stream1'},
            {'id': 2, 'name': 'Test Stream 2', 'url': 'http://test.com/stream2'}
        ]
        mock_fetcher.fetch_channels.return_value = [
            {'id': 1, 'name': 'Test Channel', 'streams': [1, 2]}
        ]
        
        # Rebuild index
        counts = udi.rebuild_from_dispatcharr(mock_fetcher)
        
        # Verify counts
        self.assertEqual(counts['accounts'], 1)
        self.assertEqual(counts['groups'], 1)
        self.assertEqual(counts['streams'], 2)
        self.assertEqual(counts['channels'], 1)
        self.assertEqual(counts['channel_streams'], 2)
        
        # Verify data was stored
        streams = udi.get_all_streams()
        self.assertEqual(len(streams), 2)
        
        channels = udi.get_all_channels()
        self.assertEqual(len(channels), 1)
        
        # Verify channel-stream relationship
        channel_streams = udi.get_channel_stream_ids(1)
        self.assertEqual(channel_streams, [1, 2])


class TestUnifiedDataIndexStats(unittest.TestCase):
    """Test statistics and metadata operations."""
    
    def setUp(self):
        """Set up test fixtures."""
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = Path(self.temp_dir) / 'test_index.db'
        
        import unified_data_index
        unified_data_index._udi_instance = None
        unified_data_index.UnifiedDataIndex._instance = None
    
    def tearDown(self):
        """Clean up test fixtures."""
        shutil.rmtree(self.temp_dir, ignore_errors=True)
        import unified_data_index
        unified_data_index._udi_instance = None
        unified_data_index.UnifiedDataIndex._instance = None
    
    def test_get_stats_empty(self):
        """Test getting stats from empty database."""
        from unified_data_index import UnifiedDataIndex
        
        udi = UnifiedDataIndex(db_path=self.db_path)
        stats = udi.get_stats()
        
        self.assertEqual(stats['accounts'], 0)
        self.assertEqual(stats['groups'], 0)
        self.assertEqual(stats['channels'], 0)
        self.assertEqual(stats['streams'], 0)
        self.assertEqual(stats['channel_streams'], 0)
        self.assertEqual(stats['pending_changes'], 0)
        self.assertIsNone(stats['last_sync'])


class TestUnifiedDataIndexChannelStreamUpdates(unittest.TestCase):
    """Test channel stream update operations."""
    
    def setUp(self):
        """Set up test fixtures."""
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = Path(self.temp_dir) / 'test_index.db'
        
        import unified_data_index
        unified_data_index._udi_instance = None
        unified_data_index.UnifiedDataIndex._instance = None
    
    def tearDown(self):
        """Clean up test fixtures."""
        shutil.rmtree(self.temp_dir, ignore_errors=True)
        import unified_data_index
        unified_data_index._udi_instance = None
        unified_data_index.UnifiedDataIndex._instance = None
    
    def test_update_channel_streams_creates_pending_change(self):
        """Test that updating channel streams creates a pending change."""
        from unified_data_index import UnifiedDataIndex
        
        udi = UnifiedDataIndex(db_path=self.db_path)
        
        # First, rebuild with mock data
        mock_fetcher = Mock()
        mock_fetcher.fetch_m3u_accounts.return_value = []
        mock_fetcher.fetch_channel_groups.return_value = []
        mock_fetcher.fetch_streams.return_value = [
            {'id': 1, 'name': 'Stream 1', 'url': 'http://test.com/1'},
            {'id': 2, 'name': 'Stream 2', 'url': 'http://test.com/2'},
            {'id': 3, 'name': 'Stream 3', 'url': 'http://test.com/3'}
        ]
        mock_fetcher.fetch_channels.return_value = [
            {'id': 1, 'name': 'Channel 1', 'streams': [1, 2]}
        ]
        
        udi.rebuild_from_dispatcharr(mock_fetcher)
        
        # Update channel streams
        success = udi.update_channel_streams(1, [3, 2, 1], source='test')
        
        self.assertTrue(success)
        
        # Verify pending change was created
        changes = udi.get_pending_changes()
        # There might be multiple changes from rebuild, look for our specific one
        update_changes = [c for c in changes if c['operation'] == 'update_streams']
        self.assertGreater(len(update_changes), 0)
        
        # Verify the local index was updated
        stream_ids = udi.get_channel_stream_ids(1)
        self.assertEqual(stream_ids, [3, 2, 1])
    
    def test_update_channel_streams_no_change(self):
        """Test that updating with same order doesn't create pending change."""
        from unified_data_index import UnifiedDataIndex
        
        udi = UnifiedDataIndex(db_path=self.db_path)
        
        # First, rebuild with mock data
        mock_fetcher = Mock()
        mock_fetcher.fetch_m3u_accounts.return_value = []
        mock_fetcher.fetch_channel_groups.return_value = []
        mock_fetcher.fetch_streams.return_value = [
            {'id': 1, 'name': 'Stream 1', 'url': 'http://test.com/1'},
            {'id': 2, 'name': 'Stream 2', 'url': 'http://test.com/2'}
        ]
        mock_fetcher.fetch_channels.return_value = [
            {'id': 1, 'name': 'Channel 1', 'streams': [1, 2]}
        ]
        
        udi.rebuild_from_dispatcharr(mock_fetcher)
        
        # Count existing pending changes
        initial_changes = len(udi.get_pending_changes())
        
        # Update with same order
        success = udi.update_channel_streams(1, [1, 2], source='test')
        
        self.assertTrue(success)
        
        # Verify no new pending change was created
        new_changes = len(udi.get_pending_changes())
        self.assertEqual(initial_changes, new_changes)


if __name__ == '__main__':
    unittest.main(verbosity=2)
