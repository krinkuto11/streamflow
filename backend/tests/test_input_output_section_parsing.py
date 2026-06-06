#!/usr/bin/env python3
"""
Test that FFmpeg output parsing correctly distinguishes between Input and Output sections.

This test validates the fix for audio codec extraction where the parser was incorrectly
extracting decoded output formats (e.g., "pcm_s16le") instead of the actual input codec
(e.g., "aac", "ac3").

The key issue: FFmpeg outputs both Input and Output sections. We must ONLY parse the
Input section to get the real codec, not the Output section which shows decoded formats.
"""

import unittest
from unittest.mock import patch, Mock
import sys
import os

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from apps.stream.stream_check_utils import get_stream_info_and_bitrate


class TestInputOutputSectionParsing(unittest.TestCase):
    """Test cases for Input vs Output section parsing in FFmpeg output."""
    
    @patch('stream_check_utils.subprocess.run')
    def test_parse_input_section_only(self, mock_run):
        """Test that we parse codecs from Input section, not Output section."""
        # This is the critical test case from the problem statement
        # Input has "aac" codec, Output has "pcm_s16le" decoded format
        # We should extract "aac", not "pcm_s16le"
        ffmpeg_output = """
Input #0, mpegts, from 'http://example.com/stream.m3u8':
  Duration: N/A, start: 0.000000, bitrate: N/A
    Stream #0:0(eng): Audio: aac (LC), 48000 Hz, stereo, fltp, 127 kb/s
    Stream #0:1: Video: h264 (High), yuv420p, 1920x1080, 25 fps
Output #0, null, to 'pipe:':
  Metadata:
    encoder         : Lavf58.76.100
    Stream #0:0: Video: wrapped_avframe, yuv420p, 1920x1080, q=2-31, 200 kb/s, 25 fps
    Stream #0:1(eng): Audio: pcm_s16le, 48000 Hz, stereo, s16, 1536 kb/s
frame= 750 fps=25 q=-0.0 Lsize=N/A time=00:00:30.00 bitrate=N/A speed=1.0x
Statistics: 18750000 bytes read
"""
        mock_result = Mock()
        mock_result.stderr = ffmpeg_output
        mock_result.returncode = 0
        mock_run.return_value = mock_result
        
        result = get_stream_info_and_bitrate('http://example.com/test.m3u8', duration=30, timeout=30)
        
        # Critical assertions: must extract INPUT codecs, not OUTPUT codecs
        self.assertEqual(result['audio_codec'], 'aac', 
                        "Should extract 'aac' from Input section, NOT 'pcm_s16le' from Output section")
        self.assertEqual(result['video_codec'], 'h264', 
                        "Should extract 'h264' from Input section, NOT 'wrapped_avframe' from Output section")
        self.assertEqual(result['resolution'], '1920x1080', "Should extract resolution from Input section")
        self.assertEqual(result['fps'], 25.0, "Should extract FPS from Input section")
        self.assertAlmostEqual(result['bitrate_kbps'], 5000.0, places=1)
        self.assertEqual(result['bitrate_source'], 'ffmpeg_bytes_read_fallback')
    
    @patch('stream_check_utils.subprocess.run')
    def test_ac3_audio_codec_not_pcm(self, mock_run):
        """Test that AC3 audio codec is correctly extracted, not pcm_s16le."""
        # Another example with AC3 audio (common in IPTV streams)
        ffmpeg_output = """
Input #0, mpegts, from 'http://example.com/stream.m3u8':
  Duration: N/A, start: 0.000000, bitrate: N/A
    Stream #0:0: Video: mpeg2video (Main), yuv420p, 1280x720, 50 fps
    Stream #0:1(eng): Audio: ac3, 48000 Hz, 5.1(side), fltp, 384 kb/s
Output #0, null, to 'pipe:':
  Metadata:
    encoder         : Lavf58.76.100
    Stream #0:0: Video: wrapped_avframe, yuv420p, 1280x720, q=2-31, 200 kb/s, 50 fps
    Stream #0:1(eng): Audio: pcm_s16le, 48000 Hz, 5.1(side), s16, 4608 kb/s
frame= 1500 fps=50 q=-0.0 Lsize=N/A time=00:00:30.00 bitrate=N/A speed=1.0x
Statistics: 25000000 bytes read
"""
        mock_result = Mock()
        mock_result.stderr = ffmpeg_output
        mock_result.returncode = 0
        mock_run.return_value = mock_result
        
        result = get_stream_info_and_bitrate('http://example.com/test.m3u8', duration=30, timeout=30)
        
        self.assertEqual(result['audio_codec'], 'ac3', 
                        "Should extract 'ac3' from Input section, NOT 'pcm_s16le' from Output section")
        self.assertEqual(result['video_codec'], 'mpeg2video', "Should extract video codec from Input section")
    
    @patch('stream_check_utils.subprocess.run')
    def test_input_only_no_output_section(self, mock_run):
        """Test parsing when there's only Input section (no Output section)."""
        # Some FFmpeg outputs may not have an Output section
        ffmpeg_output = """
Input #0, mpegts, from 'http://example.com/stream.m3u8':
  Duration: N/A, start: 0.000000, bitrate: N/A
    Stream #0:0: Video: h264, yuv420p, 1920x1080, 30 fps
    Stream #0:1: Audio: aac, 48000 Hz, stereo
frame= 900 fps=30 q=-0.0 Lsize=N/A time=00:00:30.00 bitrate=N/A speed=1.0x
Statistics: 20000000 bytes read
"""
        mock_result = Mock()
        mock_result.stderr = ffmpeg_output
        mock_result.returncode = 0
        mock_run.return_value = mock_result
        
        result = get_stream_info_and_bitrate('http://example.com/test.m3u8', duration=30, timeout=30)
        
        # Should still work correctly when there's no Output section
        self.assertEqual(result['video_codec'], 'h264', "Should extract video codec from Input section")
        self.assertEqual(result['audio_codec'], 'aac', "Should extract audio codec from Input section")
        self.assertEqual(result['fps'], 30.0, "Should extract FPS from Input section")
    
    @patch('stream_check_utils.subprocess.run')
    def test_multiple_audio_streams_input_section(self, mock_run):
        """Test parsing when there are multiple audio streams in Input section."""
        ffmpeg_output = """
Input #0, mpegts, from 'http://example.com/stream.m3u8':
  Duration: N/A, start: 0.000000, bitrate: N/A
    Stream #0:0: Video: h264, yuv420p, 1920x1080, 25 fps
    Stream #0:1(eng): Audio: aac (LC), 48000 Hz, stereo, fltp, 128 kb/s
    Stream #0:2(spa): Audio: ac3, 48000 Hz, 5.1(side), fltp, 384 kb/s
Output #0, null, to 'pipe:':
  Metadata:
    encoder         : Lavf58.76.100
    Stream #0:0: Video: wrapped_avframe, yuv420p, 1920x1080, q=2-31, 200 kb/s, 25 fps
    Stream #0:1(eng): Audio: pcm_s16le, 48000 Hz, stereo, s16, 1536 kb/s
    Stream #0:2(spa): Audio: pcm_s16le, 48000 Hz, 5.1(side), s16, 4608 kb/s
frame= 750 fps=25 q=-0.0 Lsize=N/A time=00:00:30.00 bitrate=N/A speed=1.0x
Statistics: 18750000 bytes read
"""
        mock_result = Mock()
        mock_result.stderr = ffmpeg_output
        mock_result.returncode = 0
        mock_run.return_value = mock_result
        
        result = get_stream_info_and_bitrate('http://example.com/test.m3u8', duration=30, timeout=30)
        
        # Should extract the first audio codec from Input section (aac or ac3, both valid)
        # Current implementation takes last match, so should be ac3
        self.assertIn(result['audio_codec'], ['aac', 'ac3'], 
                     "Should extract audio codec from Input section")
        self.assertNotEqual(result['audio_codec'], 'pcm_s16le', 
                           "Should NOT extract 'pcm_s16le' from Output section")
    
    @patch('stream_check_utils.subprocess.run')
    def test_mp3_audio_codec(self, mock_run):
        """Test that MP3 audio codec is correctly extracted."""
        ffmpeg_output = """
Input #0, mpegts, from 'http://example.com/stream.m3u8':
  Duration: N/A, start: 0.000000, bitrate: N/A
    Stream #0:0: Video: h264, yuv420p, 1280x720, 30 fps
    Stream #0:1: Audio: mp3, 44100 Hz, stereo, fltp, 192 kb/s
Output #0, null, to 'pipe:':
  Metadata:
    encoder         : Lavf58.76.100
    Stream #0:0: Video: wrapped_avframe, yuv420p, 1280x720, q=2-31, 200 kb/s, 30 fps
    Stream #0:1: Audio: pcm_s16le, 44100 Hz, stereo, s16, 1411 kb/s
frame= 900 fps=30 q=-0.0 Lsize=N/A time=00:00:30.00 bitrate=N/A speed=1.0x
Statistics: 15000000 bytes read
"""
        mock_result = Mock()
        mock_result.stderr = ffmpeg_output
        mock_result.returncode = 0
        mock_run.return_value = mock_result
        
        result = get_stream_info_and_bitrate('http://example.com/test.m3u8', duration=30, timeout=30)
        
        self.assertEqual(result['audio_codec'], 'mp3', 
                        "Should extract 'mp3' from Input section, NOT 'pcm_s16le' from Output section")
    
    @patch('stream_check_utils.subprocess.run')
    def test_hevc_video_with_eac3_audio(self, mock_run):
        """Test HEVC video with E-AC3 audio codec extraction."""
        ffmpeg_output = """
Input #0, mpegts, from 'http://example.com/stream.m3u8':
  Duration: N/A, start: 0.000000, bitrate: N/A
    Stream #0:0: Video: hevc (Main 10), yuv420p10le, 3840x2160, 60 fps
    Stream #0:1(eng): Audio: eac3, 48000 Hz, 5.1(side), fltp, 768 kb/s
Output #0, null, to 'pipe:':
  Metadata:
    encoder         : Lavf58.76.100
    Stream #0:0: Video: wrapped_avframe, yuv420p10le, 3840x2160, q=2-31, 200 kb/s, 60 fps
    Stream #0:1(eng): Audio: pcm_s16le, 48000 Hz, 5.1(side), s16, 4608 kb/s
frame= 1800 fps=60 q=-0.0 Lsize=N/A time=00:00:30.00 bitrate=N/A speed=1.0x
Statistics: 50000000 bytes read
"""
        mock_result = Mock()
        mock_result.stderr = ffmpeg_output
        mock_result.returncode = 0
        mock_run.return_value = mock_result
        
        result = get_stream_info_and_bitrate('http://example.com/test.m3u8', duration=30, timeout=30)
        
        self.assertEqual(result['audio_codec'], 'eac3', 
                        "Should extract 'eac3' from Input section, NOT 'pcm_s16le' from Output section")
        self.assertEqual(result['video_codec'], 'hevc', "Should extract HEVC video codec")
        self.assertEqual(result['resolution'], '3840x2160', "Should extract 4K resolution")
        self.assertEqual(result['fps'], 60.0, "Should extract 60 FPS")


if __name__ == '__main__':
    unittest.main()
