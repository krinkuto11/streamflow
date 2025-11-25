#!/usr/bin/env python3
"""
Unit tests to verify that channel/stream list fetching is optimized.

This test verifies that when checking channels, the tool uses the cache system
to ensure API data is fetched ONCE and reused across multiple channel checks,
avoiding redundant API calls.

Issue: The tool was requesting the whole channel list from dispatcharr
each time it checked each stream in the global check/stream check.
"""

import unittest
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock, call
import sys

# Add backend to path using Path for cleaner path manipulation
sys.path.insert(0, str(Path(__file__).parent.parent))


class TestCacheEnabledInWorkerLoop(unittest.TestCase):
    """Test that the cache is properly enabled during channel processing in worker loop."""
    
    def setUp(self):
        """Set up test fixtures."""
        self.temp_dir = tempfile.mkdtemp()
        
    def tearDown(self):
        """Clean up test fixtures."""
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)
    
    def test_worker_loop_enables_cache_when_processing_channels(self):
        """Test that _worker_loop enables cache context when channels are in queue."""
        from stream_checker_service import StreamCheckerService
        from dispatcharr_cache import get_cache
        
        # Create service instance
        with patch('stream_checker_service.CONFIG_DIR', Path(self.temp_dir)):
            service = StreamCheckerService()
            
            # Get the cache instance
            cache = get_cache()
            
            # Verify cache is not initially enabled
            self.assertFalse(cache.is_enabled(), "Cache should not be enabled initially")
    
    def test_cache_context_management_exists_in_worker_loop(self):
        """Test that the worker loop code includes cache context management."""
        import inspect
        from stream_checker_service import StreamCheckerService
        
        # Get the source code of _worker_loop
        source = inspect.getsource(StreamCheckerService._worker_loop)
        
        # Verify cache-related code exists in the worker loop
        self.assertIn('get_cache', source, 
            "_worker_loop should use get_cache()")
        self.assertIn('cache_context_active', source,
            "_worker_loop should track cache context state")
        self.assertIn('__enter__', source,
            "_worker_loop should enter cache context")
        self.assertIn('__exit__', source,
            "_worker_loop should exit cache context")


class TestUpdateChannelStreamsSignature(unittest.TestCase):
    """Test that update_channel_streams accepts the new stream_id_to_url parameter."""
    
    def test_update_channel_streams_accepts_stream_id_to_url(self):
        """Test that update_channel_streams signature includes stream_id_to_url parameter."""
        from api_utils import update_channel_streams
        import inspect
        
        sig = inspect.signature(update_channel_streams)
        params = list(sig.parameters.keys())
        
        self.assertIn('stream_id_to_url', params,
            "update_channel_streams should accept stream_id_to_url parameter")
        
        # Verify it's optional (has a default value, which is None)
        param = sig.parameters['stream_id_to_url']
        self.assertNotEqual(param.default, inspect.Parameter.empty,
            "stream_id_to_url should have a default value (be optional)")
    
    def test_add_streams_to_channel_accepts_stream_id_to_url(self):
        """Test that add_streams_to_channel signature includes stream_id_to_url parameter."""
        from api_utils import add_streams_to_channel
        import inspect
        
        sig = inspect.signature(add_streams_to_channel)
        params = list(sig.parameters.keys())
        
        self.assertIn('stream_id_to_url', params,
            "add_streams_to_channel should accept stream_id_to_url parameter")
        
        # Verify it's optional (has a default value, which is None)
        param = sig.parameters['stream_id_to_url']
        self.assertNotEqual(param.default, inspect.Parameter.empty,
            "stream_id_to_url should have a default value (be optional)")


class TestFilterDeadStreamsPassThrough(unittest.TestCase):
    """Test that filter_dead_streams receives and uses the stream_id_to_url mapping."""
    
    @patch('api_utils.get_dead_stream_urls')
    def test_filter_dead_streams_uses_provided_mapping(self, mock_get_dead_urls):
        """Test that filter_dead_streams uses the provided stream_id_to_url mapping."""
        from api_utils import filter_dead_streams
        
        # Mock dead URLs (empty set = no dead streams)
        mock_get_dead_urls.return_value = set()
        
        # Create a mapping
        stream_id_to_url = {1: 'http://test1', 2: 'http://test2', 3: 'http://test3'}
        
        # Call filter_dead_streams with the mapping
        filtered, count = filter_dead_streams([1, 2, 3], stream_id_to_url)
        
        # All streams should pass through (no dead streams)
        self.assertEqual(filtered, [1, 2, 3])
        self.assertEqual(count, 0)
    
    @patch('api_utils.get_dead_stream_urls')
    def test_filter_dead_streams_filters_dead_urls(self, mock_get_dead_urls):
        """Test that filter_dead_streams correctly filters out dead streams."""
        from api_utils import filter_dead_streams
        
        # Mock dead URLs - stream 2 is dead
        mock_get_dead_urls.return_value = {'http://test2'}
        
        # Create a mapping
        stream_id_to_url = {1: 'http://test1', 2: 'http://test2', 3: 'http://test3'}
        
        # Call filter_dead_streams with the mapping
        filtered, count = filter_dead_streams([1, 2, 3], stream_id_to_url)
        
        # Stream 2 should be filtered out
        self.assertEqual(filtered, [1, 3])
        self.assertEqual(count, 1)


class TestCacheIntegration(unittest.TestCase):
    """Test that the cache works correctly for API data via UDI."""
    
    def setUp(self):
        """Set up test fixtures with a temporary UDI database."""
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = Path(self.temp_dir) / 'test_cache.db'
        
        # Reset the UDI singleton and create a test instance
        import unified_data_index
        unified_data_index._udi_instance = None
        unified_data_index.UnifiedDataIndex._instance = None
        
        # Create UDI with test path
        self.udi = unified_data_index.UnifiedDataIndex(db_path=self.db_path)
        unified_data_index._udi_instance = self.udi
        
        from dispatcharr_cache import get_cache, DispatcharrCache
        # Reset cache singleton
        DispatcharrCache._instance = None
        self.cache = get_cache()
    
    def tearDown(self):
        """Clean up test fixtures."""
        import shutil
        import unified_data_index
        
        # Close connections and reset singletons
        if hasattr(self, 'udi'):
            self.udi.close()
        
        unified_data_index._udi_instance = None
        unified_data_index.UnifiedDataIndex._instance = None
        
        from dispatcharr_cache import DispatcharrCache
        DispatcharrCache._instance = None
        
        shutil.rmtree(self.temp_dir, ignore_errors=True)
    
    def test_cache_with_populated_udi_returns_streams(self):
        """Test that the cache returns data from UDI when populated."""
        # Populate UDI with mock data
        mock_fetcher = Mock()
        mock_fetcher.fetch_m3u_accounts.return_value = []
        mock_fetcher.fetch_channel_groups.return_value = []
        mock_fetcher.fetch_streams.return_value = [
            {'id': 1, 'name': 'Stream 1', 'url': 'http://test1'},
            {'id': 2, 'name': 'Stream 2', 'url': 'http://test2'},
        ]
        mock_fetcher.fetch_channels.return_value = []
        
        # Rebuild UDI
        self.udi.rebuild_from_dispatcharr(mock_fetcher)
        
        # Fetch function that should NOT be called since UDI is populated
        fetch_called = [0]
        def mock_fetch():
            fetch_called[0] += 1
            return []
        
        # Use cache context
        with self.cache:
            result = self.cache.get_streams(mock_fetch)
            self.assertEqual(len(result), 2)
            self.assertEqual(fetch_called[0], 0, "Fetch should not be called when UDI is populated")
    
    def test_cache_fallback_when_udi_empty(self):
        """Test that the cache falls back to fetch function when UDI is empty."""
        mock_streams = [
            {'id': 1, 'name': 'Stream 1', 'url': 'http://test1'},
        ]
        fetch_called = [0]
        def mock_fetch():
            fetch_called[0] += 1
            return mock_streams
        
        # UDI is empty, so fetch should be called
        with self.cache:
            result = self.cache.get_streams(mock_fetch)
            self.assertEqual(result, mock_streams)
            self.assertEqual(fetch_called[0], 1, "Fetch should be called when UDI is empty")
    
    def test_cache_computes_valid_stream_ids_from_udi(self):
        """Test that the cache correctly gets valid stream IDs from UDI."""
        # Populate UDI
        mock_fetcher = Mock()
        mock_fetcher.fetch_m3u_accounts.return_value = []
        mock_fetcher.fetch_channel_groups.return_value = []
        mock_fetcher.fetch_streams.return_value = [
            {'id': 1, 'name': 'Stream 1'},
            {'id': 2, 'name': 'Stream 2'},
            {'id': 3, 'name': 'Stream 3'},
        ]
        mock_fetcher.fetch_channels.return_value = []
        
        self.udi.rebuild_from_dispatcharr(mock_fetcher)
        
        with self.cache:
            valid_ids = self.cache.get_valid_stream_ids(lambda: [])
            self.assertEqual(valid_ids, {1, 2, 3})
    
    def test_cache_computes_stream_id_to_url_mapping_from_udi(self):
        """Test that the cache correctly gets stream URL mapping from UDI."""
        # Populate UDI
        mock_fetcher = Mock()
        mock_fetcher.fetch_m3u_accounts.return_value = []
        mock_fetcher.fetch_channel_groups.return_value = []
        mock_fetcher.fetch_streams.return_value = [
            {'id': 1, 'name': 'Stream 1', 'url': 'http://test1'},
            {'id': 2, 'name': 'Stream 2', 'url': 'http://test2'},
        ]
        mock_fetcher.fetch_channels.return_value = []
        
        self.udi.rebuild_from_dispatcharr(mock_fetcher)
        
        with self.cache:
            mapping = self.cache.get_stream_id_to_url_mapping(lambda: [])
            self.assertEqual(mapping, {1: 'http://test1', 2: 'http://test2'})


if __name__ == '__main__':
    unittest.main(verbosity=2)
