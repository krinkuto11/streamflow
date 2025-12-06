"""
Test stream check modes (full vs quick).

This test verifies:
1. Full mode analyzes bitrate and marks streams as dead if bitrate is 0
2. Quick mode skips bitrate analysis and doesn't mark streams as dead if bitrate is 0
3. Score calculation properly handles missing bitrate in quick mode
4. Configuration properly sets check_mode and probe_duration
"""

import unittest
from unittest.mock import Mock, patch, MagicMock
import sys
import os
from pathlib import Path

# Add backend directory to path
backend_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, backend_dir)

from stream_checker_service import StreamCheckerService


class TestStreamCheckModes(unittest.TestCase):
    """Test stream check modes functionality."""
    
    def setUp(self):
        """Set up test fixtures."""
        # Mock the config directory to avoid file system operations
        with patch('stream_checker_service.CONFIG_DIR', Path('/tmp/test_config')):
            with patch('stream_checker_service.ChannelUpdateTracker'):
                with patch('stream_checker_service.StreamCheckQueue'):
                    with patch('stream_checker_service.StreamCheckerProgress'):
                        with patch('stream_checker_service.DeadStreamsTracker'):
                            with patch('stream_checker_service.ChangelogManager'):
                                with patch('stream_checker_service.StreamCheckConfig._load_config', return_value={
                                    'enabled': True,
                                    'pipeline_mode': 'pipeline_1_5',
                                    'global_check_schedule': {'enabled': True, 'cron_expression': '0 3 * * *'},
                                    'stream_analysis': {'timeout': 45, 'retries': 2, 'retry_delay': 10, 'user_agent': 'VLC/3.0.14', 'check_mode': 'full', 'probe_duration': 10},
                                    'scoring': {'weights': {'bitrate': 0.30, 'resolution': 0.25, 'fps': 0.15, 'codec': 0.10, 'errors': 0.20}, 'min_score': 0.0, 'prefer_h265': True},
                                    'queue': {'max_size': 1000, 'check_on_update': True, 'max_channels_per_run': 50}
                                }):
                                    self.service = StreamCheckerService()
    
    def test_full_mode_considers_bitrate_for_dead_check(self):
        """Test that full mode considers bitrate when checking if stream is dead."""
        # Set config to full mode
        self.service.config.config['stream_analysis'] = {
            'check_mode': 'full',
            'probe_duration': 5
        }
        
        # Stream with 0 bitrate should be dead in full mode
        stream_data = {
            'resolution': '1920x1080',
            'bitrate_kbps': 0,
            'fps': 25,
            'video_codec': 'h264'
        }
        
        result = self.service._is_stream_dead(stream_data)
        self.assertTrue(result, "Stream with 0 bitrate should be dead in full mode")
    
    def test_quick_mode_ignores_bitrate_for_dead_check(self):
        """Test that quick mode ignores bitrate when checking if stream is dead."""
        # Set config to quick mode
        self.service.config.config['stream_analysis'] = {
            'check_mode': 'quick',
            'probe_duration': 5  # Not used in quick mode
        }
        
        # Stream with 0 bitrate should NOT be dead in quick mode
        stream_data = {
            'resolution': '1920x1080',
            'bitrate_kbps': 0,
            'fps': 25,
            'video_codec': 'h264'
        }
        
        result = self.service._is_stream_dead(stream_data)
        self.assertFalse(result, "Stream with 0 bitrate should NOT be dead in quick mode")
    
    def test_resolution_zero_is_dead_in_both_modes(self):
        """Test that 0x0 resolution marks stream as dead in both modes."""
        for mode in ['full', 'quick']:
            with self.subTest(mode=mode):
                self.service.config.config['stream_analysis'] = {
                    'check_mode': mode,
                    'probe_duration': 5
                }
                
                stream_data = {
                    'resolution': '0x0',
                    'bitrate_kbps': 5000,
                    'fps': 25,
                    'video_codec': 'h264'
                }
                
                result = self.service._is_stream_dead(stream_data)
                self.assertTrue(result, f"Stream with 0x0 resolution should be dead in {mode} mode")
    
    def test_full_mode_score_with_bitrate(self):
        """Test that full mode includes bitrate in score calculation."""
        self.service.config.config['stream_analysis'] = {
            'check_mode': 'full',
            'probe_duration': 5
        }
        self.service.config.config['scoring'] = {
            'weights': {
                'bitrate': 0.30,
                'resolution': 0.25,
                'fps': 0.15,
                'codec': 0.10,
                'errors': 0.20
            },
            'prefer_h265': True
        }
        
        # Stream with good bitrate
        stream_data = {
            'resolution': '1920x1080',
            'bitrate_kbps': 8000,
            'fps': 50,
            'video_codec': 'h264',
            'status': 'OK'
        }
        
        score = self.service._calculate_stream_score(stream_data)
        # Score should be high (close to 1.0)
        self.assertGreater(score, 0.8, "Stream with good metrics should have high score in full mode")
    
    def test_quick_mode_score_without_bitrate(self):
        """Test that quick mode normalizes score when bitrate is missing."""
        self.service.config.config['stream_analysis'] = {
            'check_mode': 'quick',
            'probe_duration': 5
        }
        self.service.config.config['scoring'] = {
            'weights': {
                'bitrate': 0.30,
                'resolution': 0.25,
                'fps': 0.15,
                'codec': 0.10,
                'errors': 0.20
            },
            'prefer_h265': True
        }
        
        # Stream without bitrate (common in quick mode)
        stream_data = {
            'resolution': '1920x1080',
            'bitrate_kbps': 0,  # Missing/0 bitrate
            'fps': 50,
            'video_codec': 'h264',
            'status': 'OK'
        }
        
        score = self.service._calculate_stream_score(stream_data)
        # Score should still be reasonable (not 0) even without bitrate
        self.assertGreater(score, 0.5, "Stream with good resolution/fps should have decent score in quick mode even without bitrate")
        self.assertLessEqual(score, 1.0, "Score should not exceed 1.0")
    
    def test_quick_mode_score_is_not_zero(self):
        """Test that quick mode doesn't give zero score to streams without bitrate."""
        self.service.config.config['stream_analysis'] = {
            'check_mode': 'quick',
            'probe_duration': 5
        }
        self.service.config.config['scoring'] = {
            'weights': {
                'bitrate': 0.30,
                'resolution': 0.25,
                'fps': 0.15,
                'codec': 0.10,
                'errors': 0.20
            },
            'prefer_h265': True
        }
        
        # Stream with decent resolution and fps but no bitrate
        stream_data = {
            'resolution': '1920x1080',
            'bitrate_kbps': 0,
            'fps': 25,
            'video_codec': 'h264',
            'status': 'OK'
        }
        
        score = self.service._calculate_stream_score(stream_data)
        self.assertNotEqual(score, 0.0, "Quick mode should not give 0 score to streams with good resolution/fps")
    
    def test_config_defaults(self):
        """Test that default configuration includes check_mode and probe_duration."""
        # Check default config
        default_config = self.service.config.DEFAULT_CONFIG
        
        self.assertIn('stream_analysis', default_config)
        self.assertIn('check_mode', default_config['stream_analysis'])
        self.assertIn('probe_duration', default_config['stream_analysis'])
        
        # Check default values (updated for better buffering support)
        self.assertEqual(default_config['stream_analysis']['check_mode'], 'full')
        self.assertEqual(default_config['stream_analysis']['probe_duration'], 10)
        self.assertEqual(default_config['stream_analysis']['timeout'], 45)
        self.assertEqual(default_config['stream_analysis']['retries'], 2)


class TestStreamAnalyzerModes(unittest.TestCase):
    """Test stream_analyzer module modes."""
    
    def test_get_stream_info_full_mode_parameters(self):
        """Test that full mode uses correct ffprobe parameters."""
        from stream_analyzer import get_stream_info
        
        with patch('stream_analyzer.subprocess.run') as mock_run:
            mock_run.return_value = Mock(stdout='{"streams":[],"format":{}}', stderr='')
            
            # Call with full mode
            get_stream_info('http://test.url', timeout=30, check_mode='full', probe_duration=10)
            
            # Check that the command includes correct analyzeduration and probesize
            call_args = mock_run.call_args[0][0]
            self.assertIn('-analyzeduration', call_args)
            self.assertIn('-probesize', call_args)
            
            # Find the analyzeduration value (should be 10 seconds = 10000000 microseconds)
            analyze_idx = call_args.index('-analyzeduration')
            analyze_value = int(call_args[analyze_idx + 1])
            self.assertEqual(analyze_value, 10000000, "Full mode should use probe_duration * 1000000")
    
    def test_get_stream_info_quick_mode_parameters(self):
        """Test that quick mode uses minimal ffprobe parameters."""
        from stream_analyzer import get_stream_info
        
        with patch('stream_analyzer.subprocess.run') as mock_run:
            mock_run.return_value = Mock(stdout='{"streams":[],"format":{}}', stderr='')
            
            # Call with quick mode
            get_stream_info('http://test.url', timeout=30, check_mode='quick', probe_duration=10)
            
            # Check that the command includes minimal analyzeduration
            call_args = mock_run.call_args[0][0]
            self.assertIn('-analyzeduration', call_args)
            self.assertIn('-rw_timeout', call_args)
            
            # Find the analyzeduration value (should be 1000000 microseconds = 1 second)
            analyze_idx = call_args.index('-analyzeduration')
            analyze_value = int(call_args[analyze_idx + 1])
            self.assertEqual(analyze_value, 1000000, "Quick mode should use 1 second analyzeduration")
    
    def test_get_stream_info_includes_network_timeout(self):
        """Test that ffprobe includes -rw_timeout parameter for network streams."""
        from stream_analyzer import get_stream_info
        
        with patch('stream_analyzer.subprocess.run') as mock_run:
            mock_run.return_value = Mock(stdout='{"streams":[],"format":{}}', stderr='')
            
            # Call with full mode
            get_stream_info('http://test.url', timeout=30, check_mode='full', probe_duration=10)
            
            # Check that the command includes -rw_timeout
            call_args = mock_run.call_args[0][0]
            self.assertIn('-rw_timeout', call_args)
            
            # The network timeout should be 80% of the timeout (24 seconds = 24000000 microseconds)
            rw_timeout_idx = call_args.index('-rw_timeout')
            rw_timeout_value = int(call_args[rw_timeout_idx + 1])
            expected_timeout_us = int(30 * 0.8 * 1000000)
            self.assertEqual(rw_timeout_value, expected_timeout_us, "Network timeout should be 80% of subprocess timeout")


if __name__ == '__main__':
    unittest.main()
