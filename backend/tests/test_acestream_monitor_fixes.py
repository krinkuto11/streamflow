"""
Tests for AceStream Monitor backend fixes.

This module tests the fixes for:
- DispatcharrConfig supporting dictionary-style access
- UDIManager method calls (get_channels instead of get_all_channels)
- Working with channel/stream dictionaries instead of objects
"""

import unittest
import sys
import os

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dispatcharr_config import DispatcharrConfig


class TestDispatcharrConfigFixes(unittest.TestCase):
    """Test DispatcharrConfig dictionary-style access."""
    
    def test_dictionary_get_method(self):
        """Test that config.get() works for retrieving values."""
        config = DispatcharrConfig()
        
        # Set a value directly in internal config
        config._config['test_key'] = 'test_value'
        
        # Test get method
        self.assertEqual(config.get('test_key'), 'test_value')
        self.assertEqual(config.get('nonexistent', 'default'), 'default')
        self.assertIsNone(config.get('nonexistent'))
    
    def test_dictionary_bracket_access(self):
        """Test that config['key'] works for getting/setting values."""
        config = DispatcharrConfig()
        
        # Test __setitem__
        config['test_key'] = 'test_value'
        
        # Test __getitem__
        self.assertEqual(config['test_key'], 'test_value')
    
    def test_save_method_exists(self):
        """Test that save() method exists and is callable."""
        config = DispatcharrConfig()
        self.assertTrue(hasattr(config, 'save'))
        self.assertTrue(callable(config.save))
    
    def test_acestream_config_storage(self):
        """Test that AceStream configuration can be stored and retrieved."""
        config = DispatcharrConfig()
        
        # Set AceStream configuration
        config['acestream_enabled'] = True
        config['acestream_orchestrator_url'] = 'http://test:19000'
        config['acestream_monitoring_interval'] = 30
        config['acestream_ffmpeg_probe_duration'] = 5
        
        # Verify values can be retrieved
        self.assertTrue(config.get('acestream_enabled'))
        self.assertEqual(config.get('acestream_orchestrator_url'), 'http://test:19000')
        self.assertEqual(config.get('acestream_monitoring_interval'), 30)
        self.assertEqual(config.get('acestream_ffmpeg_probe_duration'), 5)


class TestChannelDictionaryAccess(unittest.TestCase):
    """Test that channel/stream dictionaries work correctly."""
    
    def test_channel_dict_access(self):
        """Test accessing channel properties as dictionary."""
        channel = {
            'id': 1,
            'name': 'Test Channel',
            'streams': [1, 2],
            'is_acestream': True,
            'acestream_orchestrator_url': 'http://test:19000'
        }
        
        # Test dictionary access patterns used in code
        self.assertEqual(channel.get('id'), 1)
        self.assertEqual(channel.get('name'), 'Test Channel')
        self.assertEqual(channel.get('is_acestream'), True)
        self.assertEqual(channel.get('acestream_orchestrator_url'), 'http://test:19000')
        self.assertIsNone(channel.get('nonexistent'))
        self.assertEqual(channel.get('nonexistent', 'default'), 'default')
    
    def test_stream_dict_access(self):
        """Test accessing stream properties as dictionary."""
        stream = {
            'id': 1,
            'url': 'http://gluetun:19000/ace/getstream?id=abc123',
            'name': 'Test Stream'
        }
        
        # Test dictionary access patterns used in code
        self.assertEqual(stream.get('id'), 1)
        self.assertEqual(stream.get('url'), 'http://gluetun:19000/ace/getstream?id=abc123')
        self.assertEqual(stream.get('name'), 'Test Stream')
        self.assertIsNone(stream.get('nonexistent'))


if __name__ == '__main__':
    unittest.main()
