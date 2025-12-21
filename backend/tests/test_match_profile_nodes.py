#!/usr/bin/env python3
"""
Test Match Profile Node Configuration

Tests that the match profile node configuration and editing works correctly.
"""

import sys
import os
import json
from pathlib import Path

# Add parent directory to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from match_profile_manager import MatchProfileManager, MatchProfile
from match_profile_executor import MatchProfileExecutor


def test_profile_creation():
    """Test creating a match profile with nodes."""
    print("Testing profile creation...")
    
    # Create manager with temp directory
    import tempfile
    with tempfile.TemporaryDirectory() as tmpdir:
        manager = MatchProfileManager(Path(tmpdir))
        
        # Create a profile with configured nodes
        profile_data = {
            'name': 'Test Profile',
            'description': 'Test profile for node configuration',
            'enabled': True,
            'priority': 100,
            'pipeline': {
                'nodes': [
                    {
                        'id': 'source-1',
                        'type': 'source',
                        'config': {
                            'm3u_accounts': [1, 2],
                            'stream_groups': ['Sports', 'News']
                        }
                    },
                    {
                        'id': 'filter-1',
                        'type': 'filter',
                        'config': {
                            'patterns': ['NBA', 'NFL'],
                            'exclude_dead': True,
                            'case_sensitive': False
                        }
                    },
                    {
                        'id': 'transform-1',
                        'type': 'transform',
                        'config': {
                            'remove_prefixes': ['US:', 'USA:'],
                            'remove_suffixes': ['HD', 'FHD'],
                            'normalize_whitespace': True
                        }
                    },
                    {
                        'id': 'match-1',
                        'type': 'match',
                        'config': {
                            'channels': [101, 102],
                            'match_mode': 'regex',
                            'patterns': {
                                '101': ['ESPN.*'],
                                '102': ['Fox Sports.*']
                            },
                            'case_sensitive': False
                        }
                    },
                    {
                        'id': 'action-1',
                        'type': 'action',
                        'config': {
                            'action': 'add_to_channel',
                            'deduplicate': True,
                            'max_streams_per_channel': 10
                        }
                    }
                ],
                'edges': [
                    {'from': 'source-1', 'to': 'filter-1'},
                    {'from': 'filter-1', 'to': 'transform-1'},
                    {'from': 'transform-1', 'to': 'match-1'},
                    {'from': 'match-1', 'to': 'action-1'}
                ]
            }
        }
        
        # Create profile
        created = manager.create_profile(profile_data)
        assert created is not None
        assert created['name'] == 'Test Profile'
        assert len(created['pipeline']['nodes']) == 5
        assert len(created['pipeline']['edges']) == 4
        
        print("✓ Profile created successfully")
        
        # Validate profile
        is_valid, errors = manager.validate_profile(profile_data)
        assert is_valid, f"Profile validation failed: {errors}"
        print("✓ Profile validation passed")
        
        return True


def test_profile_update():
    """Test updating a profile's node configuration."""
    print("\nTesting profile update...")
    
    import tempfile
    with tempfile.TemporaryDirectory() as tmpdir:
        manager = MatchProfileManager(Path(tmpdir))
        
        # Create initial profile
        profile_data = {
            'name': 'Test Profile',
            'description': 'Test',
            'enabled': True,
            'priority': 100,
            'pipeline': {
                'nodes': [
                    {
                        'id': 'source-1',
                        'type': 'source',
                        'config': {'m3u_accounts': [1]}
                    }
                ],
                'edges': []
            }
        }
        
        created = manager.create_profile(profile_data)
        profile_id = created['id']
        
        # Update the configuration
        updated_data = {
            **created,
            'pipeline': {
                'nodes': [
                    {
                        'id': 'source-1',
                        'type': 'source',
                        'config': {'m3u_accounts': [1, 2, 3]}
                    },
                    {
                        'id': 'filter-1',
                        'type': 'filter',
                        'config': {'patterns': ['Test.*']}
                    }
                ],
                'edges': [
                    {'from': 'source-1', 'to': 'filter-1'}
                ]
            }
        }
        
        updated = manager.update_profile(profile_id, updated_data)
        assert len(updated['pipeline']['nodes']) == 2
        assert updated['pipeline']['nodes'][0]['config']['m3u_accounts'] == [1, 2, 3]
        
        print("✓ Profile updated successfully")
        return True


def test_node_types():
    """Test getting node type schemas."""
    print("\nTesting node type schemas...")
    
    node_types = MatchProfileExecutor.get_node_types()
    
    assert 'source' in node_types
    assert 'filter' in node_types
    assert 'transform' in node_types
    assert 'match' in node_types
    assert 'action' in node_types
    
    # Check each type has required fields
    for node_type, schema in node_types.items():
        assert 'name' in schema
        assert 'description' in schema
        assert 'config_schema' in schema
        print(f"  ✓ {node_type}: {schema['name']}")
    
    print("✓ Node type schemas valid")
    return True


def main():
    """Run all tests."""
    print("=" * 60)
    print("Match Profile Node Configuration Tests")
    print("=" * 60)
    
    tests = [
        test_node_types,
        test_profile_creation,
        test_profile_update,
    ]
    
    failed = []
    for test in tests:
        try:
            if not test():
                failed.append(test.__name__)
        except Exception as e:
            print(f"✗ {test.__name__} failed: {e}")
            import traceback
            traceback.print_exc()
            failed.append(test.__name__)
    
    print("\n" + "=" * 60)
    if failed:
        print(f"FAILED: {len(failed)} test(s) failed:")
        for name in failed:
            print(f"  - {name}")
        return False
    else:
        print("SUCCESS: All tests passed!")
        return True


if __name__ == '__main__':
    success = main()
    sys.exit(0 if success else 1)
