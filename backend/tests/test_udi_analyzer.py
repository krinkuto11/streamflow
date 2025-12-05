#!/usr/bin/env python3
"""
Unit tests for the UDI (Universal Data Index) Analyzer tool.

Tests cover:
1. Codebase analysis and endpoint discovery
2. Data entity building
3. Report generation
4. JSON and text output formatting
"""

import unittest
import tempfile
import json
import os
import sys
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock

# Add backend to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.tests.udi_analyzer import (
    APIEndpoint,
    DataEntity,
    UDIAnalysisReport,
    CodebaseAnalyzer,
    DataEntityBuilder,
    APIConnectivityTester,
    DataFetchTester,
    RecommendationEngine,
    UDIAnalyzer,
    format_text_report,
    DEFAULT_CACHE_TTL_SECONDS
)


class TestAPIEndpoint(unittest.TestCase):
    """Test APIEndpoint dataclass."""
    
    def test_create_endpoint(self):
        """Test creating an API endpoint."""
        endpoint = APIEndpoint(
            path='/api/channels/channels/',
            method='GET',
            source_file='api_utils.py',
            source_line=62,
            description='Fetch all channels'
        )
        
        self.assertEqual(endpoint.path, '/api/channels/channels/')
        self.assertEqual(endpoint.method, 'GET')
        self.assertEqual(endpoint.source_file, 'api_utils.py')
        self.assertEqual(endpoint.source_line, 62)
        self.assertEqual(endpoint.description, 'Fetch all channels')
        self.assertTrue(endpoint.cacheable)  # Default value
        self.assertEqual(endpoint.cache_ttl_seconds, DEFAULT_CACHE_TTL_SECONDS)  # Default value
    
    def test_endpoint_defaults(self):
        """Test that endpoint defaults are set correctly."""
        endpoint = APIEndpoint(
            path='/api/test/',
            method='POST',
            source_file='test.py',
            source_line=1
        )
        
        self.assertEqual(endpoint.description, '')
        self.assertEqual(endpoint.parameters, [])
        self.assertEqual(endpoint.response_type, '')
        self.assertEqual(endpoint.data_fields, [])
        self.assertEqual(endpoint.estimated_frequency, '')


class TestDataEntity(unittest.TestCase):
    """Test DataEntity dataclass."""
    
    def test_create_entity(self):
        """Test creating a data entity."""
        entity = DataEntity(
            name='channels',
            description='TV channels with stream assignments',
            source_endpoint='/api/channels/channels/',
            primary_key='id',
            fields={'id': 'int', 'name': 'str'},
            relationships=['streams', 'logos'],
            update_strategy='incremental'
        )
        
        self.assertEqual(entity.name, 'channels')
        self.assertEqual(entity.description, 'TV channels with stream assignments')
        self.assertEqual(entity.source_endpoint, '/api/channels/channels/')
        self.assertEqual(entity.primary_key, 'id')
        self.assertEqual(entity.fields, {'id': 'int', 'name': 'str'})
        self.assertEqual(entity.relationships, ['streams', 'logos'])
        self.assertEqual(entity.update_strategy, 'incremental')
    
    def test_entity_defaults(self):
        """Test that entity defaults are set correctly."""
        entity = DataEntity(
            name='test',
            description='Test entity',
            source_endpoint='/api/test/'
        )
        
        self.assertEqual(entity.primary_key, 'id')
        self.assertEqual(entity.fields, {})
        self.assertEqual(entity.relationships, [])
        self.assertEqual(entity.update_strategy, 'full_refresh')
        self.assertIsNone(entity.sample_data)


class TestUDIAnalysisReport(unittest.TestCase):
    """Test UDIAnalysisReport dataclass."""
    
    def test_create_report(self):
        """Test creating an analysis report."""
        report = UDIAnalysisReport(
            generated_at='2024-01-01T00:00:00',
            endpoints=[
                APIEndpoint(
                    path='/api/test/',
                    method='GET',
                    source_file='test.py',
                    source_line=1
                )
            ],
            entities=[
                DataEntity(
                    name='test',
                    description='Test',
                    source_endpoint='/api/test/'
                )
            ],
            recommendations=['Test recommendation'],
            implementation_notes=['Test note']
        )
        
        self.assertEqual(report.generated_at, '2024-01-01T00:00:00')
        self.assertEqual(len(report.endpoints), 1)
        self.assertEqual(len(report.entities), 1)
        self.assertEqual(report.recommendations, ['Test recommendation'])
        self.assertEqual(report.implementation_notes, ['Test note'])
    
    def test_report_to_dict(self):
        """Test converting report to dictionary."""
        report = UDIAnalysisReport(
            generated_at='2024-01-01T00:00:00',
            endpoints=[
                APIEndpoint(
                    path='/api/test/',
                    method='GET',
                    source_file='test.py',
                    source_line=1
                )
            ],
            entities=[],
            recommendations=['Test'],
            implementation_notes=[]
        )
        
        result = report.to_dict()
        
        self.assertIsInstance(result, dict)
        self.assertEqual(result['generated_at'], '2024-01-01T00:00:00')
        self.assertEqual(len(result['endpoints']), 1)
        self.assertEqual(result['endpoints'][0]['path'], '/api/test/')
        self.assertEqual(result['recommendations'], ['Test'])


class TestCodebaseAnalyzer(unittest.TestCase):
    """Test CodebaseAnalyzer class."""
    
    def setUp(self):
        """Set up test fixtures."""
        self.temp_dir = tempfile.mkdtemp()
        self.backend_dir = Path(self.temp_dir)
    
    def tearDown(self):
        """Clean up test fixtures."""
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)
    
    def test_normalize_url(self):
        """Test URL normalization."""
        analyzer = CodebaseAnalyzer(self.backend_dir)
        
        # Test basic normalization
        self.assertEqual(
            analyzer._normalize_url('/api/channels/channels/'),
            '/api/channels/channels/'
        )
        
        # Test adding trailing slash
        self.assertEqual(
            analyzer._normalize_url('/api/channels/channels'),
            '/api/channels/channels/'
        )
        
        # Test replacing numeric IDs
        self.assertEqual(
            analyzer._normalize_url('/api/channels/channels/123/'),
            '/api/channels/channels/{id}/'
        )
        
        # Test variable patterns
        self.assertEqual(
            analyzer._normalize_url('/api/channels/channels/{channel_id}/'),
            '/api/channels/channels/{id}/'
        )
    
    def test_infer_method(self):
        """Test HTTP method inference."""
        analyzer = CodebaseAnalyzer(self.backend_dir)
        
        lines = [
            "# Fetch data",
            "response = requests.get(url)",
            "data = response.json()"
        ]
        
        # GET method inference
        self.assertEqual(
            analyzer._infer_method(lines[1], lines, 2),
            'GET'
        )
        
        # PATCH method inference
        patch_lines = [
            "# Update channel",
            "response = patch_request(url, data)",
            "return response"
        ]
        self.assertEqual(
            analyzer._infer_method(patch_lines[1], patch_lines, 2),
            'PATCH'
        )
        
        # POST method inference
        post_lines = [
            "# Create item",
            "response = requests.post(url, json=data)",
            "return response"
        ]
        self.assertEqual(
            analyzer._infer_method(post_lines[1], post_lines, 2),
            'POST'
        )
    
    def test_analyze_file_with_api_calls(self):
        """Test analyzing a file with API calls."""
        # Create a test file with API calls
        test_file = self.backend_dir / 'test_api.py'
        test_file.write_text('''
# Fetch channels from API
def get_channels():
    url = f"{base_url}/api/channels/channels/"
    response = requests.get(url)
    return response.json()

# Update a channel
def update_channel(channel_id, data):
    url = f"{base_url}/api/channels/channels/{channel_id}/"
    response = patch_request(url, data)
    return response
''')
        
        analyzer = CodebaseAnalyzer(self.backend_dir)
        analyzer.source_files = ['test_api.py']
        
        endpoints = analyzer.analyze()
        
        # Should find at least one endpoint
        self.assertGreater(len(endpoints), 0)
        
        # Check that channels endpoint was found
        channel_endpoints = [e for e in endpoints if 'channels' in e.path]
        self.assertGreater(len(channel_endpoints), 0)


class TestDataEntityBuilder(unittest.TestCase):
    """Test DataEntityBuilder class."""
    
    def test_build_entities(self):
        """Test building entity definitions."""
        builder = DataEntityBuilder()
        entities = builder.build_entities()
        
        # Should have all expected entities
        entity_names = [e.name for e in entities]
        self.assertIn('channels', entity_names)
        self.assertIn('streams', entity_names)
        self.assertIn('channel_groups', entity_names)
        self.assertIn('logos', entity_names)
        self.assertIn('m3u_accounts', entity_names)
    
    def test_channels_entity_definition(self):
        """Test channels entity has correct definition."""
        builder = DataEntityBuilder()
        entities = builder.build_entities()
        
        channels_entity = next((e for e in entities if e.name == 'channels'), None)
        
        self.assertIsNotNone(channels_entity)
        self.assertEqual(channels_entity.primary_key, 'id')
        self.assertEqual(channels_entity.source_endpoint, '/api/channels/channels/')
        self.assertIn('id', channels_entity.fields)
        self.assertIn('name', channels_entity.fields)
        self.assertIn('streams', channels_entity.relationships)
    
    def test_streams_entity_definition(self):
        """Test streams entity has correct definition."""
        builder = DataEntityBuilder()
        entities = builder.build_entities()
        
        streams_entity = next((e for e in entities if e.name == 'streams'), None)
        
        self.assertIsNotNone(streams_entity)
        self.assertEqual(streams_entity.update_strategy, 'event_driven')
        self.assertIn('url', streams_entity.fields)
        self.assertIn('m3u_account', streams_entity.fields)


class TestAPIConnectivityTester(unittest.TestCase):
    """Test APIConnectivityTester class."""
    
    def test_test_connectivity_no_base_url(self):
        """Test connectivity check without base URL configured."""
        with patch.dict(os.environ, {}, clear=True):
            tester = APIConnectivityTester()
            results = tester.test_connectivity()
            
            self.assertFalse(results['base_url_configured'])
            self.assertFalse(results['connection_test']['success'])
            self.assertIn('not configured', results['connection_test']['error'])
    
    def test_test_connectivity_success(self):
        """Test connectivity check with successful connection."""
        import requests as req_module
        
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.elapsed.total_seconds.return_value = 0.1
        
        with patch.dict(os.environ, {
            'DISPATCHARR_BASE_URL': 'http://test:8000',
            'DISPATCHARR_USER': 'testuser',
            'DISPATCHARR_PASS': 'testpass'
        }):
            with patch.object(req_module, 'get', return_value=mock_response) as mock_get:
                tester = APIConnectivityTester()
                results = tester.test_connectivity()
                
                self.assertTrue(results['base_url_configured'])
                self.assertTrue(results['authentication_configured'])
                self.assertTrue(results['connection_test']['success'])
                self.assertEqual(results['connection_test']['status_code'], 200)
    
    def test_test_connectivity_timeout(self):
        """Test connectivity check with timeout."""
        import requests as req_module
        
        with patch.dict(os.environ, {
            'DISPATCHARR_BASE_URL': 'http://test:8000'
        }):
            with patch.object(req_module, 'get', side_effect=req_module.exceptions.Timeout()):
                tester = APIConnectivityTester()
                results = tester.test_connectivity()
                
                self.assertFalse(results['connection_test']['success'])
                self.assertIn('timeout', results['connection_test']['error'].lower())


class TestRecommendationEngine(unittest.TestCase):
    """Test RecommendationEngine class."""
    
    def test_generate_recommendations_no_config(self):
        """Test recommendations when API is not configured."""
        recommendations = RecommendationEngine.generate_recommendations(
            endpoints=[],
            entities=[],
            connectivity_results={
                'base_url_configured': False,
                'authentication_configured': False
            },
            fetch_results={}
        )
        
        # Should have warnings about missing configuration
        config_warnings = [r for r in recommendations if 'Configure' in r]
        self.assertGreater(len(config_warnings), 0)
    
    def test_generate_recommendations_always_has_architecture(self):
        """Test that architecture recommendations are always included."""
        recommendations = RecommendationEngine.generate_recommendations(
            endpoints=[],
            entities=[],
            connectivity_results={
                'base_url_configured': True,
                'authentication_configured': True
            },
            fetch_results={'overall_success': True, 'tests': []}
        )
        
        # Should have architecture recommendations
        arch_recommendations = [r for r in recommendations if '🏗️' in r]
        self.assertGreater(len(arch_recommendations), 0)
    
    def test_generate_implementation_notes(self):
        """Test implementation notes generation."""
        entities = [
            DataEntity(
                name='test',
                description='Test entity',
                source_endpoint='/api/test/',
                fields={'id': 'int', 'name': 'str'}
            )
        ]
        
        notes = RecommendationEngine.generate_implementation_notes(
            entities=entities,
            fetch_results={}
        )
        
        # Should contain the entity name
        notes_text = '\n'.join(notes)
        self.assertIn('test', notes_text.lower())
        
        # Should contain implementation structure
        self.assertIn('backend/', notes_text)
        self.assertIn('udi/', notes_text)


class TestUDIAnalyzer(unittest.TestCase):
    """Test main UDIAnalyzer class."""
    
    def setUp(self):
        """Set up test fixtures."""
        self.temp_dir = tempfile.mkdtemp()
        self.backend_dir = Path(self.temp_dir)
        
        # Create minimal test files
        (self.backend_dir / 'api_utils.py').write_text('''
def fetch_channels():
    url = f"{base_url}/api/channels/channels/"
    return requests.get(url).json()
''')
    
    def tearDown(self):
        """Clean up test fixtures."""
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)
    
    def test_analyze_no_connectivity(self):
        """Test analysis without connectivity tests."""
        analyzer = UDIAnalyzer(self.backend_dir)
        report = analyzer.analyze(test_connectivity=False, test_fetch=False)
        
        self.assertIsInstance(report, UDIAnalysisReport)
        self.assertIsNotNone(report.generated_at)
        self.assertIsInstance(report.endpoints, list)
        self.assertIsInstance(report.entities, list)
        self.assertIsInstance(report.recommendations, list)
    
    def test_analyze_generates_entities(self):
        """Test that analysis generates expected entities."""
        analyzer = UDIAnalyzer(self.backend_dir)
        report = analyzer.analyze(test_connectivity=False, test_fetch=False)
        
        entity_names = [e.name for e in report.entities]
        self.assertIn('channels', entity_names)
        self.assertIn('streams', entity_names)


class TestFormatTextReport(unittest.TestCase):
    """Test text report formatting."""
    
    def test_format_text_report(self):
        """Test formatting report as text."""
        report = UDIAnalysisReport(
            generated_at='2024-01-01T00:00:00',
            endpoints=[
                APIEndpoint(
                    path='/api/channels/channels/',
                    method='GET',
                    source_file='api_utils.py',
                    source_line=62,
                    description='Fetch channels'
                )
            ],
            entities=[
                DataEntity(
                    name='channels',
                    description='TV channels',
                    source_endpoint='/api/channels/channels/',
                    fields={'id': 'int', 'name': 'str'},
                    relationships=['streams']
                )
            ],
            api_connectivity_test={
                'base_url_configured': True,
                'authentication_configured': True,
                'connection_test': {'success': True, 'status_code': 200}
            },
            data_fetch_test={'tests': []},
            recommendations=['Test recommendation'],
            implementation_notes=['Test note']
        )
        
        text = format_text_report(report)
        
        # Should contain key sections
        self.assertIn('UNIVERSAL DATA INDEX', text)
        self.assertIn('DISCOVERED API ENDPOINTS', text)
        self.assertIn('DATA ENTITIES', text)
        self.assertIn('API CONNECTIVITY TEST', text)
        self.assertIn('RECOMMENDATIONS', text)
        
        # Should contain endpoint data
        self.assertIn('/api/channels/channels/', text)
        self.assertIn('GET', text)
        
        # Should contain entity data
        self.assertIn('CHANNELS', text)
        self.assertIn('TV channels', text)
    
    def test_format_text_report_no_connection(self):
        """Test formatting report with no API connection."""
        report = UDIAnalysisReport(
            generated_at='2024-01-01T00:00:00',
            api_connectivity_test={
                'base_url_configured': False,
                'authentication_configured': False
            },
            data_fetch_test={},
            recommendations=['Configure API'],
            implementation_notes=[]
        )
        
        text = format_text_report(report)
        
        # Should show unconfigured status
        self.assertIn('✗', text)  # Should have failure markers


class TestJSONOutput(unittest.TestCase):
    """Test JSON output generation."""
    
    def test_report_json_serializable(self):
        """Test that report can be serialized to JSON."""
        report = UDIAnalysisReport(
            generated_at='2024-01-01T00:00:00',
            endpoints=[
                APIEndpoint(
                    path='/api/test/',
                    method='GET',
                    source_file='test.py',
                    source_line=1
                )
            ],
            entities=[
                DataEntity(
                    name='test',
                    description='Test',
                    source_endpoint='/api/test/',
                    fields={'id': 'int'}
                )
            ],
            api_connectivity_test={'test': True},
            data_fetch_test={'test': True},
            recommendations=['Test'],
            implementation_notes=['Note']
        )
        
        # Should not raise exception
        json_str = json.dumps(report.to_dict())
        
        # Should be valid JSON
        parsed = json.loads(json_str)
        self.assertEqual(parsed['generated_at'], '2024-01-01T00:00:00')
        self.assertEqual(len(parsed['endpoints']), 1)
        self.assertEqual(len(parsed['entities']), 1)


if __name__ == '__main__':
    unittest.main()
