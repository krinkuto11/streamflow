#!/usr/bin/env python3
"""
Unit tests for stream groups endpoint.

This module tests:
- Fetching stream groups (channel groups) from the UDI
- Proper formatting of stream groups for the frontend
"""

import unittest
from unittest.mock import Mock, patch, MagicMock
import sys
import os
import json

# Add backend to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestStreamGroupsEndpoint(unittest.TestCase):
    """Test stream groups endpoint."""
    
    @patch('web_api.get_udi_manager')
    def test_returns_stream_groups(self, mock_get_udi):
        """Test that stream groups endpoint returns channel groups from UDI."""
        from web_api import app
        
        # Mock UDI manager
        mock_udi = MagicMock()
        mock_udi.get_channel_groups.return_value = [
            {'id': 1, 'name': 'Sports'},
            {'id': 2, 'name': 'Movies'},
            {'id': 3, 'name': 'News'},
        ]
        mock_get_udi.return_value = mock_udi
        
        with app.test_client() as client:
            response = client.get('/api/stream-groups')
            data = json.loads(response.data)
            
            # Should return a list of groups
            self.assertIsInstance(data, list)
            self.assertEqual(len(data), 3)
            
            # Each group should have id and name
            for group in data:
                self.assertIn('id', group)
                self.assertIn('name', group)
            
            # Verify specific groups
            group_names = [g['name'] for g in data]
            self.assertIn('Sports', group_names)
            self.assertIn('Movies', group_names)
            self.assertIn('News', group_names)
    
    @patch('web_api.get_udi_manager')
    def test_filters_groups_without_names(self, mock_get_udi):
        """Test that groups without names are filtered out."""
        from web_api import app
        
        # Mock UDI manager with some groups having no names, empty names, or whitespace
        mock_udi = MagicMock()
        mock_udi.get_channel_groups.return_value = [
            {'id': 1, 'name': 'Sports'},
            {'id': 2, 'name': ''},  # Empty name
            {'id': 3, 'name': 'Movies'},
            {'id': 4},  # No name field
            {'id': 5, 'name': '   '},  # Whitespace only
            {'id': 6, 'name': None},  # None name
        ]
        mock_get_udi.return_value = mock_udi
        
        with app.test_client() as client:
            response = client.get('/api/stream-groups')
            data = json.loads(response.data)
            
            # Should only return groups with valid names
            self.assertEqual(len(data), 2)
            group_names = [g['name'] for g in data]
            self.assertIn('Sports', group_names)
            self.assertIn('Movies', group_names)
    
    @patch('web_api.get_udi_manager')
    def test_handles_empty_channel_groups(self, mock_get_udi):
        """Test that endpoint handles empty channel groups list."""
        from web_api import app
        
        # Mock UDI manager with empty channel groups
        mock_udi = MagicMock()
        mock_udi.get_channel_groups.return_value = []
        mock_get_udi.return_value = mock_udi
        
        with app.test_client() as client:
            response = client.get('/api/stream-groups')
            data = json.loads(response.data)
            
            # Should return empty list
            self.assertIsInstance(data, list)
            self.assertEqual(len(data), 0)
    
    @patch('web_api.get_udi_manager')
    def test_handles_udi_error(self, mock_get_udi):
        """Test that endpoint handles UDI errors gracefully."""
        from web_api import app
        
        # Mock UDI manager that returns None (error)
        mock_udi = MagicMock()
        mock_udi.get_channel_groups.return_value = None
        mock_get_udi.return_value = mock_udi
        
        with app.test_client() as client:
            response = client.get('/api/stream-groups')
            
            # Should return 500 error
            self.assertEqual(response.status_code, 500)
            data = json.loads(response.data)
            self.assertIn('error', data)


if __name__ == '__main__':
    unittest.main()
