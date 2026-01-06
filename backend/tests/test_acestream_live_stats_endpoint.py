#!/usr/bin/env python3
"""
Test AceStream Live Stats Endpoint

Verifies that the /api/acestream/monitoring/stream/{id}/live-stats endpoint
correctly queries the orchestrator and returns real-time stream data.
"""

import sys
import os
import unittest
from unittest.mock import Mock, patch, MagicMock
import json

# Add backend to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))


class TestLiveStatsEndpoint(unittest.TestCase):
    """Test cases for live stats endpoint."""
    
    def setUp(self):
        """Set up test fixtures."""
        # Import Flask app
        from web_api import app
        self.app = app
        self.client = app.test_client()
        self.app.config['TESTING'] = True
    
    @patch('web_api.get_udi_manager')
    @patch('web_api.requests.get')
    def test_live_stats_returns_orchestrator_data(self, mock_requests_get, mock_get_udi):
        """Test that live stats endpoint queries orchestrator and returns data."""
        stream_id = 123
        acestream_id = 'abc123def456'
        
        # Mock UDI to return stream
        mock_udi = Mock()
        mock_udi.get_stream_by_id.return_value = {
            'id': stream_id,
            'name': 'Test Stream',
            'url': f'http://localhost:6878/ace/getstream?id={acestream_id}'
        }
        mock_udi.get_channels.return_value = [
            {
                'id': 1,
                'is_acestream': True,
                'streams': [stream_id],
                'acestream_orchestrator_url': 'http://gluetun:19000'
            }
        ]
        mock_get_udi.return_value = mock_udi
        
        # Mock orchestrator response
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = [
            {
                'key': acestream_id,
                'status': 'started',
                'peers': 25,
                'speed_down': 5000,
                'speed_up': 100,
                'downloaded': 1024000,
                'uploaded': 50000,
                'livepos': {'buffer_pieces': 10}
            },
            {
                'key': 'other123',
                'status': 'started',
                'peers': 5,
                'speed_down': 1000
            }
        ]
        mock_requests_get.return_value = mock_response
        
        # Make request
        response = self.client.get(f'/api/acestream/monitoring/stream/{stream_id}/live-stats')
        
        # Verify response
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.data)
        
        # Check that correct stream data was returned
        self.assertEqual(data['stream_id'], stream_id)
        self.assertEqual(data['acestream_id'], acestream_id)
        self.assertEqual(data['peers'], 25)
        self.assertEqual(data['speed_down'], 5000)
        self.assertEqual(data['speed_up'], 100)
        self.assertEqual(data['downloaded'], 1024000)
        self.assertEqual(data['uploaded'], 50000)
        self.assertTrue(data['is_alive'])
        
        # Verify orchestrator was queried
        mock_requests_get.assert_called_once()
        call_url = mock_requests_get.call_args[0][0]
        self.assertIn('/streams', call_url)
    
    @patch('web_api.get_udi_manager')
    @patch('web_api.requests.get')
    def test_live_stats_stream_not_in_orchestrator(self, mock_requests_get, mock_get_udi):
        """Test that endpoint returns not alive when stream not found in orchestrator."""
        stream_id = 123
        acestream_id = 'abc123def456'
        
        # Mock UDI
        mock_udi = Mock()
        mock_udi.get_stream_by_id.return_value = {
            'id': stream_id,
            'url': f'http://localhost:6878/ace/getstream?id={acestream_id}'
        }
        mock_udi.get_channels.return_value = [
            {
                'id': 1,
                'is_acestream': True,
                'streams': [stream_id],
                'acestream_orchestrator_url': 'http://gluetun:19000'
            }
        ]
        mock_get_udi.return_value = mock_udi
        
        # Mock orchestrator with no matching stream
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = [
            {
                'key': 'other123',
                'status': 'started',
                'peers': 5
            }
        ]
        mock_requests_get.return_value = mock_response
        
        # Make request
        response = self.client.get(f'/api/acestream/monitoring/stream/{stream_id}/live-stats')
        
        # Verify response
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.data)
        
        self.assertEqual(data['stream_id'], stream_id)
        self.assertEqual(data['acestream_id'], acestream_id)
        self.assertFalse(data['is_alive'])
        self.assertIn('message', data)
    
    @patch('web_api.get_udi_manager')
    def test_live_stats_stream_not_found(self, mock_get_udi):
        """Test that endpoint returns 404 when stream not found in UDI."""
        stream_id = 999
        
        # Mock UDI to return None
        mock_udi = Mock()
        mock_udi.get_stream_by_id.return_value = None
        mock_get_udi.return_value = mock_udi
        
        # Make request
        response = self.client.get(f'/api/acestream/monitoring/stream/{stream_id}/live-stats')
        
        # Verify response
        self.assertEqual(response.status_code, 404)
        data = json.loads(response.data)
        self.assertIn('error', data)
    
    @patch('web_api.get_udi_manager')
    def test_live_stats_invalid_acestream_url(self, mock_get_udi):
        """Test that endpoint returns 400 for non-AceStream URLs."""
        stream_id = 123
        
        # Mock UDI with non-AceStream URL
        mock_udi = Mock()
        mock_udi.get_stream_by_id.return_value = {
            'id': stream_id,
            'url': 'http://example.com/stream.m3u8'
        }
        mock_get_udi.return_value = mock_udi
        
        # Make request
        response = self.client.get(f'/api/acestream/monitoring/stream/{stream_id}/live-stats')
        
        # Verify response
        self.assertEqual(response.status_code, 400)
        data = json.loads(response.data)
        self.assertIn('error', data)
        self.assertIn('AceStream', data['error'])
    
    @patch('web_api.get_udi_manager')
    @patch('web_api.requests.get')
    def test_live_stats_orchestrator_timeout(self, mock_requests_get, mock_get_udi):
        """Test that endpoint handles orchestrator timeout gracefully."""
        stream_id = 123
        acestream_id = 'abc123def456'
        
        # Mock UDI
        mock_udi = Mock()
        mock_udi.get_stream_by_id.return_value = {
            'id': stream_id,
            'url': f'http://localhost:6878/ace/getstream?id={acestream_id}'
        }
        mock_udi.get_channels.return_value = []
        mock_get_udi.return_value = mock_udi
        
        # Mock orchestrator timeout
        import requests
        mock_requests_get.side_effect = requests.Timeout("Connection timeout")
        
        # Make request
        response = self.client.get(f'/api/acestream/monitoring/stream/{stream_id}/live-stats')
        
        # Verify response
        self.assertEqual(response.status_code, 504)
        data = json.loads(response.data)
        self.assertIn('error', data)
    
    @patch('web_api.get_udi_manager')
    @patch('web_api.requests.get')
    def test_live_stats_only_returns_started_streams(self, mock_requests_get, mock_get_udi):
        """Test that only streams with status='started' are returned."""
        stream_id = 123
        acestream_id = 'abc123def456'
        
        # Mock UDI
        mock_udi = Mock()
        mock_udi.get_stream_by_id.return_value = {
            'id': stream_id,
            'url': f'http://localhost:6878/ace/getstream?id={acestream_id}'
        }
        mock_udi.get_channels.return_value = [
            {
                'id': 1,
                'is_acestream': True,
                'streams': [stream_id]
            }
        ]
        mock_get_udi.return_value = mock_udi
        
        # Mock orchestrator with stream in different status
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = [
            {
                'key': acestream_id,
                'status': 'idle',  # Not 'started'
                'peers': 0
            }
        ]
        mock_requests_get.return_value = mock_response
        
        # Make request
        response = self.client.get(f'/api/acestream/monitoring/stream/{stream_id}/live-stats')
        
        # Verify response indicates stream not alive
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.data)
        self.assertFalse(data['is_alive'])


def run_tests():
    """Run all tests."""
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromTestCase(TestLiveStatsEndpoint)
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    return result.wasSuccessful()


if __name__ == '__main__':
    success = run_tests()
    sys.exit(0 if success else 1)
