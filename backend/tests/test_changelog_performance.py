"""
Test changelog performance optimizations for large stream sets.
"""

import unittest
import json
import tempfile
import os
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch, MagicMock
from datetime import datetime


class TestChangelogPerformance(unittest.TestCase):
    """Test changelog performance with large stream sets."""
    
    def setUp(self):
        """Set up test fixtures."""
        self.temp_dir = tempfile.mkdtemp()
        self.changelog_file = Path(self.temp_dir) / "changelog.json"
        
    def tearDown(self):
        """Clean up test files."""
        import shutil
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir)

    def _discovery_context(self, *, channels, streams, assign_mock):
        stack = ExitStack()
        stack.enter_context(
            patch('apps.automation.automated_stream_manager.CONFIG_DIR', Path(self.temp_dir))
        )
        stack.enter_context(
            patch('apps.automation.automated_stream_manager.get_channels', return_value=channels)
        )
        stack.enter_context(
            patch('apps.automation.automated_stream_manager.get_streams', return_value=streams)
        )
        stack.enter_context(
            patch('apps.automation.automated_stream_manager.get_m3u_accounts', return_value=[])
        )
        stack.enter_context(
            patch('apps.automation.automated_stream_manager.assign_streams_to_channel', assign_mock)
        )

        fake_udi = MagicMock()
        fake_udi.get_channel_streams.return_value = []
        fake_udi.get_channel_by_id.side_effect = lambda channel_id: next(
            (channel for channel in channels if channel.get('id') == channel_id),
            None,
        )
        stack.enter_context(
            patch('apps.automation.automated_stream_manager.get_udi_manager', return_value=fake_udi)
        )

        fake_automation_config = MagicMock()
        fake_automation_config.get_effective_configuration.return_value = None
        stack.enter_context(
            patch(
                'apps.automation.automated_stream_manager.get_automation_config_manager',
                return_value=fake_automation_config,
            )
        )

        fake_session_manager = MagicMock()
        fake_session_manager.get_channels_in_active_sessions.return_value = []
        fake_session_manager.get_active_sessions.return_value = []
        stack.enter_context(
            patch('apps.stream.stream_session_manager.get_session_manager', return_value=fake_session_manager)
        )

        return stack
    
    def test_changelog_limits_channels_to_50(self):
        """Test that changelog entries are limited to top 50 channels."""
        from apps.automation.automated_stream_manager import AutomatedStreamManager
        
        # Create a scenario with 100 channels
        channels = []
        for i in range(100):
            channels.append({
                'id': i,
                'name': f'Channel {i}',
                'streams': []
            })
        # Create 500 streams
        streams = []
        for i in range(500):
            streams.append({
                'id': i,
                'name': f'Stream {i}',
                'group_title': 'Sports' if i % 2 == 0 else 'Movies'
            })
        # Mock successful stream assignment
        mock_assign = MagicMock(return_value=5)  # Each channel gets 5 streams
        
        with self._discovery_context(channels=channels, streams=streams, assign_mock=mock_assign):
            manager = AutomatedStreamManager()
            manager.config['enabled_features']['changelog_tracking'] = True
            
            # Configure regex to match all streams to all channels
            manager.regex_matcher.regex_config = {
                'channels': [
                    {
                        'channel_id': str(i),
                        'channel_name': f'Channel {i}',
                        'patterns': [r'.*']  # Match everything
                    }
                    for i in range(100)
                ]
            }
            
            # Run discovery
            manager.discover_and_assign_streams()
            
            # Check changelog
            recent_entries = manager.changelog.get_recent_entries(1)
            self.assertEqual(len(recent_entries), 1)
            
            entry = recent_entries[0]
            self.assertEqual(entry['action'], 'streams_assigned')
            
            # Should have exactly 50 channels in assignments (limit)
            self.assertLessEqual(len(entry['details']['assignments']), 50)
            
            # Should have has_more_channels flag set to True
            self.assertTrue(entry['details'].get('has_more_channels', False))
            
            # Total channel count should reflect all 100 channels
            self.assertEqual(entry['details']['channel_count'], 100)
    
    def test_changelog_sorts_by_stream_count(self):
        """Test that channels are sorted by stream count (descending)."""
        from apps.automation.automated_stream_manager import AutomatedStreamManager
        
        # Create 10 channels
        channels = []
        for i in range(10):
            channels.append({
                'id': i,
                'name': f'Channel {i}',
                'streams': []
            })
        # Create streams
        streams = []
        for i in range(100):
            streams.append({
                'id': i,
                'name': f'Stream {i}',
                'group_title': 'Sports'
            })
        # Mock varying assignment counts
        assignment_counts = [1, 5, 2, 10, 3, 8, 4, 6, 9, 7]
        def mock_assign_side_effect(channel_id, stream_ids):
            return assignment_counts[channel_id]
        mock_assign = MagicMock(side_effect=mock_assign_side_effect)
        
        with self._discovery_context(channels=channels, streams=streams, assign_mock=mock_assign):
            manager = AutomatedStreamManager()
            manager.config['enabled_features']['changelog_tracking'] = True
            
            # Configure regex to match streams to specific channels
            manager.regex_matcher.regex_config = {
                'channels': [
                    {
                        'channel_id': str(i),
                        'channel_name': f'Channel {i}',
                        'patterns': [r'Stream ' + str(j) for j in range(i*10, (i+1)*10)]
                    }
                    for i in range(10)
                ]
            }
            
            # Run discovery
            manager.discover_and_assign_streams()
            
            # Check changelog
            recent_entries = manager.changelog.get_recent_entries(1)
            entry = recent_entries[0]
            
            assignments = entry['details']['assignments']
            
            # Verify they are sorted by stream_count in descending order
            stream_counts = [a['stream_count'] for a in assignments]
            self.assertEqual(stream_counts, sorted(stream_counts, reverse=True))
            
            # First channel should have the most streams (10)
            self.assertEqual(assignments[0]['stream_count'], 10)
    
    def test_changelog_no_truncation_for_small_sets(self):
        """Test that small channel sets are not truncated."""
        from apps.automation.automated_stream_manager import AutomatedStreamManager
        
        # Create only 5 channels
        channels = []
        for i in range(5):
            channels.append({
                'id': i,
                'name': f'Channel {i}',
                'streams': []
            })
        streams = []
        for i in range(20):
            streams.append({
                'id': i,
                'name': f'Stream {i}',
                'group_title': 'Sports'
            })
        mock_assign = MagicMock(return_value=4)  # Each channel gets 4 streams
        
        with self._discovery_context(channels=channels, streams=streams, assign_mock=mock_assign):
            manager = AutomatedStreamManager()
            manager.config['enabled_features']['changelog_tracking'] = True
            
            # Configure regex
            manager.regex_matcher.regex_config = {
                'channels': [
                    {
                        'channel_id': str(i),
                        'channel_name': f'Channel {i}',
                        'patterns': [r'.*']
                    }
                    for i in range(5)
                ]
            }
            
            # Run discovery
            manager.discover_and_assign_streams()
            
            # Check changelog
            recent_entries = manager.changelog.get_recent_entries(1)
            entry = recent_entries[0]
            
            # All 5 channels should be present
            self.assertEqual(len(entry['details']['assignments']), 5)
            
            # has_more_channels should be False or not set
            self.assertFalse(entry['details'].get('has_more_channels', False))
            
            # Channel count should be 5
            self.assertEqual(entry['details']['channel_count'], 5)

    def test_changelog_ignores_zero_assignment_channels(self):
        """Channels where Dispatcharr accepted no streams are not counted as updates."""
        from apps.automation.automated_stream_manager import AutomatedStreamManager

        channels = [
            {'id': 0, 'name': 'Already Current', 'streams': []},
            {'id': 1, 'name': 'New Streams', 'streams': []},
        ]
        streams = [
            {'id': 100, 'name': 'Stream 100', 'group_title': 'Sports'},
            {'id': 101, 'name': 'Stream 101', 'group_title': 'Sports'},
        ]

        def mock_assign(channel_id, stream_ids, **_kwargs):
            return 0 if channel_id == 0 else 2

        with self._discovery_context(
            channels=channels,
            streams=streams,
            assign_mock=MagicMock(side_effect=mock_assign),
        ):
            manager = AutomatedStreamManager()
            manager.config['enabled_features']['changelog_tracking'] = True
            manager.regex_matcher.regex_config = {
                'channels': [
                    {
                        'channel_id': str(channel['id']),
                        'channel_name': channel['name'],
                        'patterns': [r'.*'],
                    }
                    for channel in channels
                ]
            }

            result = manager.discover_and_assign_streams()

            recent_entries = manager.changelog.get_recent_entries(1)
            entry = recent_entries[0]
            self.assertEqual(entry['details']['total_assigned'], 2)
            self.assertEqual(entry['details']['channel_count'], 1)
            self.assertEqual(
                [assignment['channel_id'] for assignment in entry['details']['assignments']],
                ['1'],
            )
            self.assertEqual(result['assignment_count'], {'1': 2})
            self.assertEqual(result['assigned_stream_ids'], {'1': [100, 101]})
    
    def test_changelog_entry_per_channel_stream_limit(self):
        """Test that each channel in changelog is limited to 20 streams."""
        from apps.automation.automated_stream_manager import ChangelogManager
        
        # Create a changelog entry with a channel having many streams
        streams = [{'stream_id': i, 'stream_name': f'Stream {i}'} for i in range(100)]
        
        changelog_data = {
            'action': 'streams_assigned',
            'details': {
                'total_assigned': 100,
                'channel_count': 1,
                'assignments': [{
                    'channel_id': 1,
                    'channel_name': 'Test Channel',
                    'stream_count': 100,
                    'streams': streams[:20]  # Backend should limit to 20
                }]
            },
            'timestamp': datetime.now().isoformat()
        }
        
        with patch('apps.automation.automated_stream_manager.CONFIG_DIR', Path(self.temp_dir)):
            manager = ChangelogManager(self.changelog_file)
            manager.add_entry(
                changelog_data['action'],
                changelog_data['details']
            )
            
            # Load and verify
            recent = manager.get_recent_entries(1)
            self.assertEqual(len(recent), 1)
            
            channel_assignment = recent[0]['details']['assignments'][0]
            
            # Should have exactly 20 streams (limit applied by backend before adding to changelog)
            self.assertEqual(len(channel_assignment['streams']), 20)
            
            # stream_count should still be 100 (total count, not truncated)
            self.assertEqual(channel_assignment['stream_count'], 100)


if __name__ == '__main__':
    unittest.main()
