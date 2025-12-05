#!/usr/bin/env python3
"""
Universal Data Index (UDI) Database Analyzer for StreamFlow.

This tool analyzes the codebase to discover all API calls to Dispatcharr,
tests data retrieval, and generates a comprehensive report for building
a local database that can reduce API calls.

The tool:
1. Discovers all API endpoints used in the codebase
2. Documents data structures and relationships
3. Tests connectivity and data fetching
4. Generates agent-readable output for subsequent UDI implementation

Usage:
    python udi_analyzer.py [--output OUTPUT_FILE] [--format {json,text}]
"""

import argparse
import ast
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Set, Any, Tuple
from collections import defaultdict

# Optional import for HTTP requests (used in connectivity testing)
try:
    import requests
    REQUESTS_AVAILABLE = True
except ImportError:
    REQUESTS_AVAILABLE = False

# Add backend to path for imports
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from logging_config import setup_logging

logger = setup_logging(__name__)

# Constants
DEFAULT_CACHE_TTL_SECONDS = 300  # 5 minutes
HTTP_SUCCESS_CODES = [200, 401, 403]  # 401/403 indicate server is reachable but needs auth


@dataclass
class APIEndpoint:
    """Represents a discovered API endpoint."""
    path: str
    method: str  # GET, POST, PATCH, DELETE
    source_file: str
    source_line: int
    description: str = ""
    parameters: List[str] = field(default_factory=list)
    response_type: str = ""  # list, dict, single
    data_fields: List[str] = field(default_factory=list)
    estimated_frequency: str = ""  # high, medium, low
    cacheable: bool = True
    cache_ttl_seconds: int = DEFAULT_CACHE_TTL_SECONDS


@dataclass
class DataEntity:
    """Represents a data entity that would be stored in the UDI database."""
    name: str
    description: str
    source_endpoint: str
    primary_key: str = "id"
    fields: Dict[str, str] = field(default_factory=dict)  # field_name: field_type
    relationships: List[str] = field(default_factory=list)  # related entity names
    update_strategy: str = "full_refresh"  # full_refresh, incremental, event_driven
    sample_data: Optional[Dict] = None


@dataclass
class UDIAnalysisReport:
    """Complete analysis report for UDI implementation."""
    generated_at: str
    codebase_version: str = "current"
    endpoints: List[APIEndpoint] = field(default_factory=list)
    entities: List[DataEntity] = field(default_factory=list)
    api_connectivity_test: Dict[str, Any] = field(default_factory=dict)
    data_fetch_test: Dict[str, Any] = field(default_factory=dict)
    recommendations: List[str] = field(default_factory=list)
    implementation_notes: List[str] = field(default_factory=list)
    
    def to_dict(self) -> Dict:
        """Convert to dictionary for JSON serialization."""
        return {
            "generated_at": self.generated_at,
            "codebase_version": self.codebase_version,
            "endpoints": [asdict(e) for e in self.endpoints],
            "entities": [asdict(e) for e in self.entities],
            "api_connectivity_test": self.api_connectivity_test,
            "data_fetch_test": self.data_fetch_test,
            "recommendations": self.recommendations,
            "implementation_notes": self.implementation_notes
        }


class CodebaseAnalyzer:
    """Analyzes the StreamFlow codebase for API endpoint usage."""
    
    # Known API patterns - used for enriching discovered endpoints with descriptions
    KNOWN_ENDPOINT_DESCRIPTIONS = {
        '/api/channels/channels/': 'Fetch all channels',
        '/api/channels/channels/{id}/': 'Fetch or update single channel',
        '/api/channels/channels/{id}/streams/': 'Fetch channel streams',
        '/api/channels/channels/from-stream/': 'Create channel from stream',
        '/api/channels/groups/': 'Fetch channel groups',
        '/api/channels/logos/': 'Fetch channel logos',
        '/api/channels/logos/{id}/': 'Fetch single logo',
        '/api/channels/streams/': 'Fetch all streams',
        '/api/channels/streams/{id}/': 'Fetch or update single stream',
        '/api/m3u/accounts/': 'Fetch M3U accounts',
        '/api/m3u/refresh/': 'Refresh all M3U playlists',
        '/api/m3u/refresh/{id}/': 'Refresh specific M3U playlist',
        '/api/accounts/token/': 'Authentication/login',
    }
    
    def __init__(self, backend_dir: Path):
        self.backend_dir = backend_dir
        self.source_files = [
            'api_utils.py',
            'automated_stream_manager.py',
            'stream_checker_service.py',
            'web_api.py'
        ]
    
    def analyze(self) -> List[APIEndpoint]:
        """Analyze the codebase for API endpoint usage."""
        endpoints = []
        
        for source_file in self.source_files:
            file_path = self.backend_dir / source_file
            if file_path.exists():
                file_endpoints = self._analyze_file(file_path)
                endpoints.extend(file_endpoints)
        
        # Deduplicate and consolidate endpoints
        return self._consolidate_endpoints(endpoints)
    
    def _analyze_file(self, file_path: Path) -> List[APIEndpoint]:
        """Analyze a single source file for API calls."""
        endpoints = []
        
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                content = f.read()
                lines = content.split('\n')
            
            # Look for URL patterns in f-strings and string concatenations
            url_patterns = [
                # f-string patterns
                r'f"([^"]*\{[^}]*\}/api/[^"]*)"',
                r"f'([^']*\{[^}]*\}/api/[^']*)'",
                r'f"(\{[^}]*\}/api/[^"]*)"',
                r"f'(\{[^}]*\}/api/[^']*)'",
                # Direct URL patterns
                r'"([^"]*)/api/([^"]*)"',
                r"'([^']*)/api/([^']*)'",
            ]
            
            for line_num, line in enumerate(lines, 1):
                # Skip comments
                if line.strip().startswith('#'):
                    continue
                
                # Look for API URL patterns
                for pattern in url_patterns:
                    matches = re.findall(pattern, line)
                    for match in matches:
                        if isinstance(match, tuple):
                            url_part = '/api/' + match[-1] if not match[-1].startswith('api/') else '/' + match[-1]
                        else:
                            url_part = match
                        
                        # Clean up the URL
                        url_part = self._normalize_url(url_part)
                        
                        if '/api/' in url_part:
                            method = self._infer_method(line, lines, line_num)
                            desc = self._find_description(lines, line_num)
                            
                            endpoints.append(APIEndpoint(
                                path=url_part,
                                method=method,
                                source_file=file_path.name,
                                source_line=line_num,
                                description=desc
                            ))
        
        except Exception as e:
            logger.warning(f"Error analyzing {file_path}: {e}")
        
        return endpoints
    
    def _normalize_url(self, url: str) -> str:
        """Normalize a URL pattern for comparison."""
        # Extract just the API path
        if '/api/' in url:
            url = '/api/' + url.split('/api/')[-1]
        
        # Replace variable patterns with {id} placeholders
        url = re.sub(r'\{[^}]+\}', '{id}', url)
        url = re.sub(r'/\d+/', '/{id}/', url)
        
        # Ensure trailing slash
        if not url.endswith('/') and '?' not in url:
            url = url + '/'
        
        # Remove query params for pattern matching
        if '?' in url:
            url = url.split('?')[0]
            if not url.endswith('/'):
                url = url + '/'
        
        return url
    
    def _infer_method(self, line: str, lines: List[str], line_num: int) -> str:
        """Infer HTTP method from the code context."""
        # Look at the line and surrounding context
        context = ' '.join(lines[max(0, line_num-5):min(len(lines), line_num+3)])
        
        if 'patch_request' in context.lower() or 'requests.patch' in context.lower():
            return 'PATCH'
        elif 'post_request' in context.lower() or 'requests.post' in context.lower():
            return 'POST'
        elif 'delete' in context.lower():
            return 'DELETE'
        else:
            return 'GET'
    
    def _find_description(self, lines: List[str], line_num: int) -> str:
        """Try to find a description from comments or docstrings."""
        # Look for comments in the preceding lines
        for i in range(line_num - 2, max(0, line_num - 5), -1):
            line = lines[i].strip()
            if line.startswith('#'):
                return line[1:].strip()
            if '"""' in line or "'''" in line:
                return line.replace('"""', '').replace("'''", '').strip()
        return ""
    
    def _consolidate_endpoints(self, endpoints: List[APIEndpoint]) -> List[APIEndpoint]:
        """Consolidate and deduplicate endpoints."""
        seen = {}
        
        for endpoint in endpoints:
            key = (endpoint.path, endpoint.method)
            if key not in seen:
                # Add known description if available and no description found from code
                if not endpoint.description and endpoint.path in self.KNOWN_ENDPOINT_DESCRIPTIONS:
                    endpoint.description = self.KNOWN_ENDPOINT_DESCRIPTIONS[endpoint.path]
                seen[key] = endpoint
            else:
                # Merge descriptions if different
                existing = seen[key]
                if endpoint.description and endpoint.description != existing.description:
                    if existing.description:
                        existing.description = f"{existing.description}; {endpoint.description}"
                    else:
                        existing.description = endpoint.description
        
        return list(seen.values())


class DataEntityBuilder:
    """Builds data entity definitions from discovered endpoints."""
    
    # Entity definitions based on the Dispatcharr API
    ENTITY_DEFINITIONS = {
        'channels': {
            'description': 'TV channels with stream assignments',
            'endpoint': '/api/channels/channels/',
            'primary_key': 'id',
            'fields': {
                'id': 'int',
                'channel_number': 'float',
                'name': 'str',
                'channel_group_id': 'int',
                'tvg_id': 'str',
                'epg_data_id': 'int',
                'streams': 'list[int]',  # List of stream IDs
                'stream_profile_id': 'int',
                'uuid': 'str',
                'logo_id': 'int',
                'user_level': 'int'
            },
            'relationships': ['channel_groups', 'streams', 'logos'],
            'update_strategy': 'incremental'
        },
        'streams': {
            'description': 'Video streams from M3U sources',
            'endpoint': '/api/channels/streams/',
            'primary_key': 'id',
            'fields': {
                'id': 'int',
                'name': 'str',
                'url': 'str',
                'm3u_account': 'int',
                'logo_url': 'str',
                'tvg_id': 'str',
                'current_viewers': 'int',
                'stream_profile_id': 'int',
                'is_custom': 'bool',
                'channel_group': 'int',
                'stream_hash': 'str',
                'stream_stats': 'dict'
            },
            'relationships': ['m3u_accounts', 'channel_groups'],
            'update_strategy': 'event_driven'  # Update on M3U refresh
        },
        'channel_groups': {
            'description': 'Groups/categories for organizing channels',
            'endpoint': '/api/channels/groups/',
            'primary_key': 'id',
            'fields': {
                'id': 'int',
                'name': 'str',
                'channel_count': 'int',
                'm3u_account_count': 'int'
            },
            'relationships': ['channels'],
            'update_strategy': 'full_refresh'
        },
        'logos': {
            'description': 'Channel logos/icons',
            'endpoint': '/api/channels/logos/',
            'primary_key': 'id',
            'fields': {
                'id': 'int',
                'name': 'str',
                'url': 'str',
                'cache_url': 'str',
                'channel_count': 'int'
            },
            'relationships': ['channels'],
            'update_strategy': 'full_refresh'
        },
        'm3u_accounts': {
            'description': 'M3U playlist source accounts',
            'endpoint': '/api/m3u/accounts/',
            'primary_key': 'id',
            'fields': {
                'id': 'int',
                'name': 'str',
                'server_url': 'str',
                'file_path': 'str',
                'max_streams': 'int',
                'is_active': 'bool',
                'refresh_interval': 'int',
                'status': 'str',
                'last_message': 'str'
            },
            'relationships': ['streams'],
            'update_strategy': 'full_refresh'
        }
    }
    
    def build_entities(self) -> List[DataEntity]:
        """Build data entity definitions."""
        entities = []
        
        for name, definition in self.ENTITY_DEFINITIONS.items():
            entity = DataEntity(
                name=name,
                description=definition['description'],
                source_endpoint=definition['endpoint'],
                primary_key=definition['primary_key'],
                fields=definition['fields'],
                relationships=definition['relationships'],
                update_strategy=definition['update_strategy']
            )
            entities.append(entity)
        
        return entities


class APIConnectivityTester:
    """Tests connectivity to the Dispatcharr API."""
    
    def __init__(self):
        self.base_url = os.getenv('DISPATCHARR_BASE_URL', '')
    
    def test_connectivity(self) -> Dict[str, Any]:
        """Test API connectivity without making authenticated requests."""
        results = {
            'timestamp': datetime.now().isoformat(),
            'base_url': self.base_url,
            'base_url_configured': bool(self.base_url),
            'connection_test': None,
            'authentication_configured': False,
            'endpoints_tested': []
        }
        
        # Check if credentials are configured
        results['authentication_configured'] = all([
            os.getenv('DISPATCHARR_USER'),
            os.getenv('DISPATCHARR_PASS')
        ])
        
        if not self.base_url:
            results['connection_test'] = {
                'success': False,
                'error': 'DISPATCHARR_BASE_URL not configured'
            }
            return results
        
        # Try a simple connection test
        if not REQUESTS_AVAILABLE:
            results['connection_test'] = {
                'success': False,
                'error': 'requests library not available'
            }
            return results
        
        try:
            response = requests.get(
                f"{self.base_url}/api/",
                timeout=5,
                allow_redirects=True
            )
            results['connection_test'] = {
                'success': response.status_code in HTTP_SUCCESS_CODES,
                'status_code': response.status_code,
                'response_time_ms': response.elapsed.total_seconds() * 1000
            }
        except requests.exceptions.ConnectionError as e:
            results['connection_test'] = {
                'success': False,
                'error': f'Connection failed: {str(e)}'
            }
        except requests.exceptions.Timeout:
            results['connection_test'] = {
                'success': False,
                'error': 'Connection timeout'
            }
        except Exception as e:
            results['connection_test'] = {
                'success': False,
                'error': f'Unexpected error: {str(e)}'
            }
        
        return results


class DataFetchTester:
    """Tests data fetching capability from Dispatcharr API."""
    
    def __init__(self):
        pass  # No instance state needed
    
    def test_data_fetching(self) -> Dict[str, Any]:
        """Test data fetching from various endpoints."""
        results = {
            'timestamp': datetime.now().isoformat(),
            'overall_success': False,
            'tests': []
        }
        
        # Only run fetch tests if we have credentials
        if not all([
            os.getenv('DISPATCHARR_BASE_URL'),
            os.getenv('DISPATCHARR_USER'),
            os.getenv('DISPATCHARR_PASS')
        ]):
            results['tests'].append({
                'name': 'credentials_check',
                'success': False,
                'error': 'Missing required environment variables (DISPATCHARR_BASE_URL, DISPATCHARR_USER, DISPATCHARR_PASS)'
            })
            return results
        
        # Import API utilities for authenticated requests
        # Note: Using internal _get_base_url as there's no public alternative
        try:
            from api_utils import (
                fetch_data_from_url, 
                get_streams, 
                get_m3u_accounts,
                _get_base_url
            )
            
            base_url = _get_base_url()
            
            # Test fetching channels
            test_results = []
            
            # Test 1: Fetch channels
            try:
                start_time = time.time()
                channels = fetch_data_from_url(f"{base_url}/api/channels/channels/")
                elapsed = (time.time() - start_time) * 1000
                
                if channels is not None:
                    # Handle paginated response
                    if isinstance(channels, dict) and 'results' in channels:
                        channel_list = channels['results']
                        total_count = channels.get('count', len(channel_list))
                    else:
                        channel_list = channels if isinstance(channels, list) else []
                        total_count = len(channel_list)
                    
                    test_results.append({
                        'name': 'fetch_channels',
                        'success': True,
                        'count': total_count,
                        'sample_fields': list(channel_list[0].keys()) if channel_list else [],
                        'response_time_ms': elapsed
                    })
                else:
                    test_results.append({
                        'name': 'fetch_channels',
                        'success': False,
                        'error': 'No data returned'
                    })
            except Exception as e:
                test_results.append({
                    'name': 'fetch_channels',
                    'success': False,
                    'error': str(e)
                })
            
            # Test 2: Fetch streams
            try:
                start_time = time.time()
                streams = get_streams(log_result=False)
                elapsed = (time.time() - start_time) * 1000
                
                if streams:
                    test_results.append({
                        'name': 'fetch_streams',
                        'success': True,
                        'count': len(streams),
                        'sample_fields': list(streams[0].keys()) if streams else [],
                        'response_time_ms': elapsed
                    })
                else:
                    test_results.append({
                        'name': 'fetch_streams',
                        'success': True,
                        'count': 0,
                        'note': 'No streams found (this may be expected)'
                    })
            except Exception as e:
                test_results.append({
                    'name': 'fetch_streams',
                    'success': False,
                    'error': str(e)
                })
            
            # Test 3: Fetch M3U accounts
            try:
                start_time = time.time()
                accounts = get_m3u_accounts()
                elapsed = (time.time() - start_time) * 1000
                
                if accounts is not None:
                    test_results.append({
                        'name': 'fetch_m3u_accounts',
                        'success': True,
                        'count': len(accounts),
                        'sample_fields': list(accounts[0].keys()) if accounts else [],
                        'response_time_ms': elapsed
                    })
                else:
                    test_results.append({
                        'name': 'fetch_m3u_accounts',
                        'success': True,
                        'count': 0,
                        'note': 'No M3U accounts found (this may be expected)'
                    })
            except Exception as e:
                test_results.append({
                    'name': 'fetch_m3u_accounts',
                    'success': False,
                    'error': str(e)
                })
            
            # Test 4: Fetch channel groups
            try:
                start_time = time.time()
                groups = fetch_data_from_url(f"{base_url}/api/channels/groups/")
                elapsed = (time.time() - start_time) * 1000
                
                if groups is not None:
                    group_list = groups if isinstance(groups, list) else []
                    test_results.append({
                        'name': 'fetch_channel_groups',
                        'success': True,
                        'count': len(group_list),
                        'sample_fields': list(group_list[0].keys()) if group_list else [],
                        'response_time_ms': elapsed
                    })
                else:
                    test_results.append({
                        'name': 'fetch_channel_groups',
                        'success': True,
                        'count': 0,
                        'note': 'No channel groups found'
                    })
            except Exception as e:
                test_results.append({
                    'name': 'fetch_channel_groups',
                    'success': False,
                    'error': str(e)
                })
            
            results['tests'] = test_results
            results['overall_success'] = all(t.get('success', False) for t in test_results)
            
        except ImportError as e:
            results['tests'].append({
                'name': 'import_check',
                'success': False,
                'error': f'Failed to import API utilities: {str(e)}'
            })
        except Exception as e:
            results['tests'].append({
                'name': 'general',
                'success': False,
                'error': f'Unexpected error: {str(e)}'
            })
        
        return results


class RecommendationEngine:
    """Generates recommendations for UDI implementation."""
    
    @staticmethod
    def generate_recommendations(
        endpoints: List[APIEndpoint],
        entities: List[DataEntity],
        connectivity_results: Dict,
        fetch_results: Dict
    ) -> List[str]:
        """Generate implementation recommendations."""
        recommendations = []
        
        # Check connectivity
        if not connectivity_results.get('base_url_configured'):
            recommendations.append(
                "⚠️ Configure DISPATCHARR_BASE_URL environment variable to enable API connectivity"
            )
        
        if not connectivity_results.get('authentication_configured'):
            recommendations.append(
                "⚠️ Configure DISPATCHARR_USER and DISPATCHARR_PASS for authenticated API access"
            )
        
        # Data storage recommendations
        recommendations.append(
            "📦 Store 'streams' data locally - this is the largest dataset and most frequently accessed"
        )
        recommendations.append(
            "📦 Cache 'channels' data with stream assignments - reduces multiple API calls"
        )
        recommendations.append(
            "📦 Store 'channel_groups' and 'm3u_accounts' as reference data with longer cache TTL"
        )
        
        # Update strategy recommendations
        recommendations.append(
            "🔄 Use event-driven updates for streams when M3U refresh completes"
        )
        recommendations.append(
            "🔄 Use incremental updates for channels (fetch changes since last update)"
        )
        recommendations.append(
            "🔄 Implement full refresh for static reference data (groups, logos) on startup"
        )
        
        # Performance recommendations
        if fetch_results.get('overall_success'):
            for test in fetch_results.get('tests', []):
                if test.get('success') and test.get('response_time_ms', 0) > 1000:
                    recommendations.append(
                        f"⏱️ {test['name']} is slow ({test['response_time_ms']:.0f}ms) - prioritize caching"
                    )
        
        # Architecture recommendations
        recommendations.append(
            "🏗️ Implement a UDIManager class as singleton to handle all data access"
        )
        recommendations.append(
            "🏗️ Use JSON file storage initially for persistence (upgrade to SQLite if needed)"
        )
        recommendations.append(
            "🏗️ Implement background refresh thread with configurable intervals"
        )
        recommendations.append(
            "🏗️ Add change detection to minimize unnecessary API calls"
        )
        
        return recommendations
    
    @staticmethod
    def generate_implementation_notes(
        entities: List[DataEntity],
        fetch_results: Dict
    ) -> List[str]:
        """Generate implementation notes for developers/agents."""
        notes = []
        
        notes.append("=" * 60)
        notes.append("UNIVERSAL DATA INDEX (UDI) IMPLEMENTATION GUIDE")
        notes.append("=" * 60)
        
        notes.append("\n## Data Model Overview\n")
        for entity in entities:
            notes.append(f"### {entity.name}")
            notes.append(f"- Source: {entity.source_endpoint}")
            notes.append(f"- Primary Key: {entity.primary_key}")
            notes.append(f"- Update Strategy: {entity.update_strategy}")
            notes.append(f"- Fields: {', '.join(entity.fields.keys())}")
            notes.append(f"- Relations: {', '.join(entity.relationships)}")
            notes.append("")
        
        notes.append("\n## Suggested File Structure\n")
        notes.append("""
backend/
├── udi/
│   ├── __init__.py
│   ├── manager.py          # UDIManager singleton class
│   ├── storage.py          # Data storage layer (JSON/SQLite)
│   ├── fetcher.py          # API data fetching
│   ├── cache.py            # Cache invalidation logic
│   └── models.py           # Data models/entities
""")
        
        notes.append("\n## Key Implementation Points\n")
        notes.append("1. UDIManager should be the single entry point for all Dispatcharr data")
        notes.append("2. Replace direct API calls in api_utils.py with UDI lookups")
        notes.append("3. Implement data refresh triggered by M3U playlist updates")
        notes.append("4. Add data integrity checks before serving cached data")
        notes.append("5. Implement graceful fallback to API on cache miss")
        
        notes.append("\n## Migration Strategy\n")
        notes.append("1. Create UDI infrastructure alongside existing code")
        notes.append("2. Add UDI data fetch on startup")
        notes.append("3. Gradually replace API calls with UDI lookups")
        notes.append("4. Monitor and compare API call reduction")
        
        # Add data sample info if available
        if fetch_results.get('overall_success'):
            notes.append("\n## Verified Data Fields\n")
            for test in fetch_results.get('tests', []):
                if test.get('success') and test.get('sample_fields'):
                    notes.append(f"### {test['name']}")
                    notes.append(f"Fields: {', '.join(test['sample_fields'])}")
                    notes.append(f"Count: {test.get('count', 'unknown')}")
                    notes.append("")
        
        return notes


class UDIAnalyzer:
    """Main analyzer orchestrating all analysis components."""
    
    def __init__(self, backend_dir: Optional[Path] = None):
        if backend_dir is None:
            backend_dir = Path(__file__).parent
        self.backend_dir = backend_dir
        
    def analyze(self, test_connectivity: bool = True, test_fetch: bool = True) -> UDIAnalysisReport:
        """Run complete analysis and generate report."""
        logger.info("Starting UDI Analysis...")
        
        # 1. Analyze codebase for endpoints
        logger.info("Step 1: Analyzing codebase for API endpoints...")
        codebase_analyzer = CodebaseAnalyzer(self.backend_dir)
        endpoints = codebase_analyzer.analyze()
        logger.info(f"  Found {len(endpoints)} unique API endpoints")
        
        # 2. Build data entity definitions
        logger.info("Step 2: Building data entity definitions...")
        entity_builder = DataEntityBuilder()
        entities = entity_builder.build_entities()
        logger.info(f"  Defined {len(entities)} data entities")
        
        # 3. Test API connectivity
        connectivity_results = {}
        if test_connectivity:
            logger.info("Step 3: Testing API connectivity...")
            connectivity_tester = APIConnectivityTester()
            connectivity_results = connectivity_tester.test_connectivity()
            if connectivity_results.get('connection_test', {}).get('success'):
                logger.info("  ✓ API connectivity confirmed")
            else:
                logger.warning(f"  ✗ API connectivity issue: {connectivity_results.get('connection_test', {}).get('error', 'Unknown')}")
        
        # 4. Test data fetching
        fetch_results = {}
        if test_fetch and connectivity_results.get('connection_test', {}).get('success'):
            logger.info("Step 4: Testing data fetching...")
            fetch_tester = DataFetchTester()
            fetch_results = fetch_tester.test_data_fetching()
            if fetch_results.get('overall_success'):
                logger.info("  ✓ Data fetching successful")
            else:
                logger.warning("  ✗ Some data fetch tests failed")
        
        # 5. Generate recommendations
        logger.info("Step 5: Generating recommendations...")
        recommendations = RecommendationEngine.generate_recommendations(
            endpoints, entities, connectivity_results, fetch_results
        )
        
        # 6. Generate implementation notes
        logger.info("Step 6: Generating implementation notes...")
        implementation_notes = RecommendationEngine.generate_implementation_notes(
            entities, fetch_results
        )
        
        # Build final report
        report = UDIAnalysisReport(
            generated_at=datetime.now().isoformat(),
            endpoints=endpoints,
            entities=entities,
            api_connectivity_test=connectivity_results,
            data_fetch_test=fetch_results,
            recommendations=recommendations,
            implementation_notes=implementation_notes
        )
        
        logger.info("Analysis complete!")
        return report


def format_text_report(report: UDIAnalysisReport) -> str:
    """Format report as human-readable text."""
    lines = []
    
    lines.append("=" * 70)
    lines.append("UNIVERSAL DATA INDEX (UDI) ANALYSIS REPORT")
    lines.append("=" * 70)
    lines.append(f"Generated: {report.generated_at}")
    lines.append("")
    
    # API Endpoints
    lines.append("-" * 70)
    lines.append("DISCOVERED API ENDPOINTS")
    lines.append("-" * 70)
    for endpoint in report.endpoints:
        lines.append(f"  {endpoint.method:6} {endpoint.path}")
        if endpoint.description:
            lines.append(f"         - {endpoint.description}")
        lines.append(f"         Source: {endpoint.source_file}:{endpoint.source_line}")
    lines.append("")
    
    # Data Entities
    lines.append("-" * 70)
    lines.append("DATA ENTITIES FOR UDI DATABASE")
    lines.append("-" * 70)
    for entity in report.entities:
        lines.append(f"\n  📁 {entity.name.upper()}")
        lines.append(f"     Description: {entity.description}")
        lines.append(f"     Source: {entity.source_endpoint}")
        lines.append(f"     Primary Key: {entity.primary_key}")
        lines.append(f"     Update Strategy: {entity.update_strategy}")
        lines.append(f"     Fields: {', '.join(entity.fields.keys())}")
        lines.append(f"     Relationships: {', '.join(entity.relationships)}")
    lines.append("")
    
    # Connectivity Test
    lines.append("-" * 70)
    lines.append("API CONNECTIVITY TEST")
    lines.append("-" * 70)
    conn = report.api_connectivity_test
    lines.append(f"  Base URL Configured: {'✓' if conn.get('base_url_configured') else '✗'}")
    lines.append(f"  Authentication Configured: {'✓' if conn.get('authentication_configured') else '✗'}")
    if conn.get('connection_test'):
        ct = conn['connection_test']
        lines.append(f"  Connection Test: {'✓ Success' if ct.get('success') else '✗ Failed'}")
        if ct.get('error'):
            lines.append(f"    Error: {ct['error']}")
        if ct.get('response_time_ms'):
            lines.append(f"    Response Time: {ct['response_time_ms']:.0f}ms")
    lines.append("")
    
    # Data Fetch Test
    lines.append("-" * 70)
    lines.append("DATA FETCH TEST")
    lines.append("-" * 70)
    fetch = report.data_fetch_test
    for test in fetch.get('tests', []):
        status = '✓' if test.get('success') else '✗'
        lines.append(f"  {status} {test.get('name', 'unknown')}")
        if test.get('count') is not None:
            lines.append(f"    Records: {test['count']}")
        if test.get('response_time_ms'):
            lines.append(f"    Response Time: {test['response_time_ms']:.0f}ms")
        if test.get('sample_fields'):
            lines.append(f"    Fields: {', '.join(test['sample_fields'][:8])}...")
        if test.get('error'):
            lines.append(f"    Error: {test['error']}")
    lines.append("")
    
    # Recommendations
    lines.append("-" * 70)
    lines.append("RECOMMENDATIONS")
    lines.append("-" * 70)
    for rec in report.recommendations:
        lines.append(f"  {rec}")
    lines.append("")
    
    # Implementation Notes
    lines.append("-" * 70)
    lines.append("IMPLEMENTATION NOTES")
    lines.append("-" * 70)
    for note in report.implementation_notes:
        lines.append(note)
    
    return '\n'.join(lines)


def main():
    """Main entry point for the UDI Analyzer tool."""
    parser = argparse.ArgumentParser(
        description='Universal Data Index (UDI) Analyzer for StreamFlow'
    )
    parser.add_argument(
        '--output', '-o',
        type=str,
        help='Output file path (default: stdout)'
    )
    parser.add_argument(
        '--format', '-f',
        choices=['json', 'text'],
        default='text',
        help='Output format (default: text)'
    )
    parser.add_argument(
        '--skip-connectivity',
        action='store_true',
        help='Skip API connectivity tests'
    )
    parser.add_argument(
        '--skip-fetch',
        action='store_true',
        help='Skip data fetch tests'
    )
    
    args = parser.parse_args()
    
    # For JSON output to stdout, redirect logging to stderr to avoid mixing
    if args.format == 'json' and not args.output:
        import logging
        for handler in logging.root.handlers[:]:
            logging.root.removeHandler(handler)
        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s - %(levelname)s - [%(name)s:%(funcName)s:%(lineno)d] - %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S',
            stream=sys.stderr
        )
    
    # Run analysis
    analyzer = UDIAnalyzer()
    report = analyzer.analyze(
        test_connectivity=not args.skip_connectivity,
        test_fetch=not args.skip_fetch
    )
    
    # Format output
    if args.format == 'json':
        output = json.dumps(report.to_dict(), indent=2)
    else:
        output = format_text_report(report)
    
    # Write output
    if args.output:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, 'w', encoding='utf-8') as f:
            f.write(output)
        print(f"Report written to: {args.output}", file=sys.stderr)
    else:
        print(output)
    
    # Return success/failure based on critical tests
    if report.api_connectivity_test.get('connection_test', {}).get('success', False):
        return 0
    elif not report.api_connectivity_test.get('base_url_configured', False):
        return 0  # Not configured is not a failure for offline analysis
    else:
        return 1


if __name__ == '__main__':
    sys.exit(main())
