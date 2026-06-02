#!/usr/bin/env python3
"""
Unit tests for stream_stats_utils module.

Tests the centralized stream statistics utility functions to ensure
consistent data handling across the application.
"""

import unittest
import sys
import os

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from apps.core.stream_stats_utils import (
    parse_bitrate_value,
    format_bitrate,
    parse_fps_value,
    format_fps,
    normalize_resolution,
    extract_stream_stats,
    format_stream_stats_for_display,
    calculate_channel_averages,
    is_stream_dead
)


class TestParseBitrateValue(unittest.TestCase):
    """Test bitrate parsing from various formats."""
    
    def test_parse_integer_bitrate(self):
        """Test parsing integer bitrate."""
        self.assertEqual(parse_bitrate_value(5000), 5000.0)
    
    def test_parse_float_bitrate(self):
        """Test parsing float bitrate."""
        self.assertEqual(parse_bitrate_value(5000.5), 5000.5)
    
    def test_parse_string_kbps(self):
        """Test parsing 'kbps' string."""
        self.assertEqual(parse_bitrate_value("5000 kbps"), 5000.0)
        self.assertEqual(parse_bitrate_value("5000kbps"), 5000.0)
    
    def test_parse_string_mbps(self):
        """Test parsing 'Mbps' string."""
        self.assertEqual(parse_bitrate_value("5 Mbps"), 5000.0)
        self.assertEqual(parse_bitrate_value("5.5 Mbps"), 5500.0)
    
    def test_parse_plain_number_string(self):
        """Test parsing plain number string."""
        self.assertEqual(parse_bitrate_value("5000"), 5000.0)
    
    def test_parse_none(self):
        """Test parsing None."""
        self.assertIsNone(parse_bitrate_value(None))
    
    def test_parse_zero(self):
        """Test parsing explicit zero bitrate."""
        self.assertEqual(parse_bitrate_value(0), 0.0)
        self.assertEqual(parse_bitrate_value("0"), 0.0)


class TestFormatBitrate(unittest.TestCase):
    """Test bitrate formatting for display."""
    
    def test_format_kbps(self):
        """Test formatting values under 1000 kbps."""
        self.assertEqual(format_bitrate(500), "500 kbps")
        self.assertEqual(format_bitrate(999), "999 kbps")
    
    def test_format_mbps(self):
        """Test formatting values over 1000 kbps."""
        self.assertEqual(format_bitrate(1000), "1.0 Mbps")
        self.assertEqual(format_bitrate(5000), "5.0 Mbps")
        self.assertEqual(format_bitrate(5500), "5.5 Mbps")
    
    def test_format_none(self):
        """Test formatting None."""
        self.assertEqual(format_bitrate(None), "N/A")
    
    def test_format_zero(self):
        """Test formatting zero."""
        self.assertEqual(format_bitrate(0), "N/A")


class TestParseFpsValue(unittest.TestCase):
    """Test FPS parsing from various formats."""
    
    def test_parse_integer_fps(self):
        """Test parsing integer FPS."""
        self.assertEqual(parse_fps_value(30), 30.0)
    
    def test_parse_float_fps(self):
        """Test parsing float FPS."""
        self.assertEqual(parse_fps_value(29.97), 29.97)
    
    def test_parse_string_fps(self):
        """Test parsing FPS string."""
        self.assertEqual(parse_fps_value("30 fps"), 30.0)
        self.assertEqual(parse_fps_value("29.97"), 29.97)
    
    def test_parse_none(self):
        """Test parsing None."""
        self.assertIsNone(parse_fps_value(None))
    
    def test_parse_zero(self):
        """Test parsing zero."""
        self.assertIsNone(parse_fps_value(0))


class TestFormatFps(unittest.TestCase):
    """Test FPS formatting for display."""
    
    def test_format_fps(self):
        """Test formatting FPS.
        
        Note: 29.97 is rounded to 30.0 by design - we use 1 decimal place
        formatting for cleaner display. This is intentional behavior.
        """
        self.assertEqual(format_fps(30), "30.0 fps")
        self.assertEqual(format_fps(29.97), "30.0 fps")  # Intentional rounding
        self.assertEqual(format_fps(25), "25.0 fps")
    
    def test_format_none(self):
        """Test formatting None."""
        self.assertEqual(format_fps(None), "N/A")
    
    def test_format_zero(self):
        """Test formatting zero."""
        self.assertEqual(format_fps(0), "N/A")


class TestNormalizeResolution(unittest.TestCase):
    """Test resolution normalization."""
    
    def test_normalize_valid_resolution(self):
        """Test normalizing valid resolution."""
        self.assertEqual(normalize_resolution("1920x1080"), "1920x1080")
        self.assertEqual(normalize_resolution("1280x720"), "1280x720")
    
    def test_normalize_invalid_resolution(self):
        """Test normalizing invalid resolution."""
        # Note: "0x0" is preserved for dead stream detection, not converted to "N/A"
        self.assertEqual(normalize_resolution("0x0"), "0x0")
        self.assertEqual(normalize_resolution("Unknown"), "N/A")
        self.assertEqual(normalize_resolution(None), "N/A")
        self.assertEqual(normalize_resolution(""), "N/A")


class TestExtractStreamStats(unittest.TestCase):
    """Test stream stats extraction from various formats."""
    
    def test_extract_from_stream_stats_dict(self):
        """Test extraction from stream_stats dictionary."""
        stream_data = {
            'id': 1,
            'name': 'Test Stream',
            'stream_stats': {
                'resolution': '1920x1080',
                'source_fps': 30,
                'ffmpeg_output_bitrate': 5000,
                'video_codec': 'h264',
                'audio_codec': 'aac'
            }
        }
        
        result = extract_stream_stats(stream_data)
        self.assertEqual(result['resolution'], '1920x1080')
        self.assertEqual(result['fps'], 30.0)
        self.assertEqual(result['bitrate_kbps'], 5000.0)
        self.assertEqual(result['video_codec'], 'h264')
        self.assertEqual(result['audio_codec'], 'aac')
    
    def test_extract_from_direct_fields(self):
        """Test extraction from direct fields (e.g., from analyze_stream)."""
        stream_data = {
            'stream_id': 1,
            'stream_name': 'Test Stream',
            'resolution': '1920x1080',
            'fps': 30,
            'bitrate_kbps': 5000,
            'video_codec': 'h264',
            'audio_codec': 'aac'
        }
        
        result = extract_stream_stats(stream_data)
        self.assertEqual(result['resolution'], '1920x1080')
        self.assertEqual(result['fps'], 30.0)
        self.assertEqual(result['bitrate_kbps'], 5000.0)
        self.assertEqual(result['video_codec'], 'h264')
        self.assertEqual(result['audio_codec'], 'aac')
    
    def test_extract_with_none_stream_stats(self):
        """Test extraction when stream_stats is None."""
        stream_data = {
            'id': 1,
            'name': 'Test Stream',
            'stream_stats': None
        }
        
        result = extract_stream_stats(stream_data)
        self.assertEqual(result['resolution'], 'N/A')
        self.assertIsNone(result['fps'])
        self.assertIsNone(result['bitrate_kbps'])
    
    def test_extract_with_json_string_stream_stats(self):
        """Test extraction when stream_stats is a JSON string."""
        import json
        stream_data = {
            'id': 1,
            'name': 'Test Stream',
            'stream_stats': json.dumps({
                'resolution': '1920x1080',
                'source_fps': 30,
                'ffmpeg_output_bitrate': 5000
            })
        }
        
        result = extract_stream_stats(stream_data)
        self.assertEqual(result['resolution'], '1920x1080')
        self.assertEqual(result['fps'], 30.0)
        self.assertEqual(result['bitrate_kbps'], 5000.0)

    def test_extract_legacy_stream_stats_aliases(self):
        """Legacy keys should still map to scoring fields."""
        stream_data = {
            'id': 1,
            'stream_stats': {
                'resolution_width': 1920,
                'resolution_height': 1080,
                'fps': '25',
                'bitrate': '4200 kbps',
                'codec': 'h264',
                'audio_codec': 'aac'
            }
        }

        result = extract_stream_stats(stream_data)
        self.assertEqual(result['resolution'], '1920x1080')
        self.assertEqual(result['fps'], 25.0)
        self.assertEqual(result['bitrate_kbps'], 4200.0)
        self.assertEqual(result['video_codec'], 'h264')
        self.assertEqual(result['audio_codec'], 'aac')

    def test_extract_direct_legacy_fields(self):
        """Legacy direct fields should be normalized when stream_stats is absent."""
        stream_data = {
            'stream_id': 99,
            'resolution_width': 1280,
            'resolution_height': 720,
            'ffmpeg_output_bitrate': 3100,
            'source_fps': 50,
            'codec': 'hevc',
            'audio_codec': 'ac3',
        }

        result = extract_stream_stats(stream_data)
        self.assertEqual(result['resolution'], '1280x720')
        self.assertEqual(result['fps'], 50.0)
        self.assertEqual(result['bitrate_kbps'], 3100.0)
        self.assertEqual(result['video_codec'], 'hevc')
        self.assertEqual(result['audio_codec'], 'ac3')


class TestIsStreamDead(unittest.TestCase):
    """Test dead stream detection."""
    
    def test_dead_stream_zero_resolution(self):
        """Test detecting dead stream with 0x0 resolution."""
        stream_data = {
            'stream_stats': {
                'resolution': '0x0',
                'ffmpeg_output_bitrate': 5000
            }
        }
        self.assertTrue(is_stream_dead(stream_data))
    
    def test_dead_stream_zero_bitrate(self):
        """Test detecting dead stream with zero bitrate."""
        stream_data = {
            'stream_stats': {
                'resolution': '1920x1080',
                'ffmpeg_output_bitrate': 0
            }
        }
        self.assertTrue(is_stream_dead(stream_data))
    
    def test_dead_stream_partial_zero_resolution(self):
        """Test detecting dead stream with partial zero resolution."""
        stream_data = {
            'stream_stats': {
                'resolution': '1920x0',
                'ffmpeg_output_bitrate': 5000
            }
        }
        self.assertTrue(is_stream_dead(stream_data))
        
        stream_data['stream_stats']['resolution'] = '0x1080'
        self.assertTrue(is_stream_dead(stream_data))
    
    def test_healthy_stream(self):
        """Test detecting healthy stream."""
        stream_data = {
            'stream_stats': {
                'resolution': '1920x1080',
                'ffmpeg_output_bitrate': 5000
            }
        }
        self.assertFalse(is_stream_dead(stream_data)[0])


class TestCalculateChannelAverages(unittest.TestCase):
    """Test channel average calculation."""
    
    def test_calculate_averages(self):
        """Test calculating channel averages."""
        streams = [
            {
                'stream_id': 1,
                'stream_stats': {
                    'resolution': '1920x1080',
                    'source_fps': 30,
                    'ffmpeg_output_bitrate': 5000
                }
            },
            {
                'stream_id': 2,
                'stream_stats': {
                    'resolution': '1920x1080',
                    'source_fps': 30,
                    'ffmpeg_output_bitrate': 6000
                }
            },
            {
                'stream_id': 3,
                'stream_stats': {
                    'resolution': '1280x720',
                    'source_fps': 25,
                    'ffmpeg_output_bitrate': 3000
                }
            }
        ]
        
        result = calculate_channel_averages(streams)
        
        # Most common resolution
        self.assertEqual(result['avg_resolution'], '1920x1080')
        
        # Average bitrate: (5000 + 6000 + 3000) / 3 = 4666.67 kbps = 4.7 Mbps
        self.assertIn('4.7', result['avg_bitrate'])
        
        # Average FPS: (30 + 30 + 25) / 3 = 28.33
        self.assertIn('28.3', result['avg_fps'])
    
    def test_calculate_averages_excluding_dead_streams(self):
        """Test calculating averages excluding dead streams."""
        streams = [
            {
                'stream_id': 1,
                'stream_stats': {
                    'resolution': '1920x1080',
                    'source_fps': 30,
                    'ffmpeg_output_bitrate': 5000
                }
            },
            {
                'stream_id': 2,
                'stream_stats': {
                    'resolution': '0x0',
                    'source_fps': 0,
                    'ffmpeg_output_bitrate': 0
                }
            }
        ]
        
        dead_stream_ids = {2}
        result = calculate_channel_averages(streams, dead_stream_ids)
        
        # Should only use stream 1 for averages
        self.assertEqual(result['avg_resolution'], '1920x1080')
        # 5000 kbps is formatted as "5.0 Mbps" (values >= 1000 kbps are shown in Mbps)
        self.assertEqual(result['avg_bitrate'], '5.0 Mbps')
        self.assertEqual(result['avg_fps'], '30.0 fps')


if __name__ == '__main__':
    unittest.main(verbosity=2)
