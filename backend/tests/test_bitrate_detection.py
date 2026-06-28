#!/usr/bin/env python3
"""
Unit tests for bitrate detection in stream analysis.

This test module verifies:
1. Primary bitrate detection method (Statistics: line)
2. Fallback methods for various ffmpeg output formats
3. Progress output parsing
4. Warning when bitrate detection fails
"""

import unittest
import subprocess
from unittest.mock import Mock, patch, MagicMock
import sys
import os

# Add backend to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Import the function we're testing from the new module
from apps.stream.stream_check_utils import get_stream_bitrate


class TestBitrateDetection(unittest.TestCase):
    """Test bitrate detection from various ffmpeg output formats."""

    # Removed test_bitrate_method_1_statistics_line as Method 1 is deprecated.

    @patch('subprocess.run')
    def test_bitrate_method_2_progress_output(self, mock_run):
        """Test Method 2: Fallback detection via progress output with bitrate= pattern."""
        # Simulate ffmpeg output with progress lines but no Statistics
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stderr = """
frame=  500 fps= 25 q=-1.0 size=   12000kB time=00:00:20.00 bitrate=4800.0kbits/s speed=1.0x
frame=  750 fps= 25 q=-1.0 size=   18000kB time=00:00:30.00 bitrate=4800.0kbits/s speed=1.0x
        """
        mock_run.return_value = mock_result
        
        bitrate, status, elapsed = get_stream_bitrate(
            'http://test.com/stream.m3u8',
            duration=30,
            timeout=10
        )
        
        # Should detect bitrate from progress output
        self.assertIsNotNone(bitrate, "Bitrate should be detected from progress output")
        self.assertAlmostEqual(bitrate, 4800.0, places=1, msg="Bitrate should match progress value")

    @patch('subprocess.run')
    def test_bitrate_method_3_bytes_read_fallback_when_progress_bitrate_is_na(self, mock_run):
        """Use ffmpeg bytes-read statistics when a full probe reports bitrate=N/A."""
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stderr = """
frame=  750 fps= 25 q=-1.0 size=N/A time=00:00:30.00 bitrate=N/A speed=1.0x
Statistics: 18750000 bytes read
        """
        mock_run.return_value = mock_result

        bitrate, status, elapsed = get_stream_bitrate(
            'http://test.com/stream.m3u8',
            duration=30,
            timeout=10,
        )

        self.assertEqual(status, "OK")
        self.assertIsNotNone(bitrate, "Bitrate should be estimated from bytes read")
        self.assertAlmostEqual(bitrate, 5000.0, places=1)

    @patch('subprocess.run')
    def test_bitrate_all_methods_fail(self, mock_run):
        """Test that bitrate remains None when all detection methods fail."""
        # Simulate ffmpeg output with no recognizable bitrate patterns
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stderr = """
[info] Stream started
[info] Stream ended
        """
        mock_run.return_value = mock_result
        
        bitrate, status, elapsed = get_stream_bitrate(
            'http://test.com/stream.m3u8',
            duration=30,
            timeout=10
        )
        
        # Bitrate should remain None when no patterns match
        self.assertIsNone(bitrate, "Bitrate should be None when detection fails")

    @patch('subprocess.run')
    def test_bitrate_multiple_progress_lines(self, mock_run):
        """Test that the last progress bitrate is used when Statistics is missing."""
        # Simulate multiple progress updates - should use the last one
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stderr = """
frame=  250 fps= 25 q=-1.0 size=    6000kB time=00:00:10.00 bitrate=4800.0kbits/s speed=1.0x
frame=  500 fps= 25 q=-1.0 size=   11000kB time=00:00:20.00 bitrate=4400.0kbits/s speed=1.0x
frame=  750 fps= 25 q=-1.0 size=   15000kB time=00:00:30.00 bitrate=4000.0kbits/s speed=1.0x
        """
        mock_run.return_value = mock_result
        
        bitrate, status, elapsed = get_stream_bitrate(
            'http://test.com/stream.m3u8',
            duration=30,
            timeout=10
        )
        
        # Should use the last progress bitrate
        self.assertIsNotNone(bitrate, "Bitrate should be detected")
        self.assertAlmostEqual(bitrate, 4000.0, places=1, msg="Should use last progress bitrate")

    # Removed test_bitrate_priority_statistics_over_progress as priority logic is obsolete.

    @patch('subprocess.run')
    def test_bitrate_timeout_handling(self, mock_run):
        """Test that timeout is handled gracefully."""
        test_timeout = 10
        expected_timeout = test_timeout + 30 + 10 + 20  # timeout + duration + startup + completion headroom
        mock_run.side_effect = subprocess.TimeoutExpired(cmd='ffmpeg', timeout=expected_timeout)
        
        bitrate, status, elapsed = get_stream_bitrate(
            'http://test.com/stream.m3u8',
            duration=30,
            timeout=test_timeout
        )
        
        self.assertIsNone(bitrate, "Bitrate should be None on timeout")
        self.assertEqual(status, "Timeout", "Status should indicate timeout")

    @patch('subprocess.run')
    def test_bitrate_error_handling(self, mock_run):
        """Test that general errors are handled gracefully."""
        mock_run.side_effect = Exception("Network error")
        
        bitrate, status, elapsed = get_stream_bitrate(
            'http://test.com/stream.m3u8',
            duration=30,
            timeout=10
        )
        
        self.assertIsNone(bitrate, "Bitrate should be None on error")
        self.assertEqual(status, "Error", "Status should indicate error")

    @patch('subprocess.run')
    @patch('stream_check_utils.logger')
    def test_bitrate_failure_warning_uses_elapsed_time(self, mock_logger, mock_run):
        """Test that the warning message uses actual elapsed time, not intended duration."""
        # Simulate ffmpeg completing quickly with no bitrate data
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stderr = """
[info] Stream started
[info] Stream ended
        """
        mock_run.return_value = mock_result
        
        bitrate, status, elapsed = get_stream_bitrate(
            'http://test.com/stream.m3u8',
            duration=30,  # Intended duration
            timeout=10
        )
        
        # Bitrate should remain None when detection fails
        self.assertIsNone(bitrate, "Bitrate should be None when detection fails")
        self.assertEqual(status, "OK", "Status should be OK even if bitrate is None")
        
        # Verify that warning was called
        mock_logger.warning.assert_called()
        
        # Get all warning messages
        warning_calls = [str(call) for call in mock_logger.warning.call_args_list]
        warning_text = ' '.join(warning_calls)
        
        # Should mention analysis time and expected duration
        self.assertIn("elapsed=", warning_text, "Warning should mention analysis time")
        self.assertIn("expected ~30s", warning_text, "Warning should mention expected duration")
        
        # Verify that elapsed time is actually small (< 1 second)
        self.assertLess(elapsed, 1.0, "Elapsed time should be very small when ffmpeg returns quickly")

    @patch('subprocess.run')
    @patch('stream_check_utils.logger')
    def test_verbose_error_logging_on_ffmpeg_failure(self, mock_logger, mock_run):
        """Test that ffmpeg errors are logged verbosely when it exits early."""
        # Simulate ffmpeg failing with connection error
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stderr = """
[http @ 0x7f8b9c000c00] HTTP error 404 Not Found
http://test.com/stream.m3u8: Server returned 404 Not Found
[info] Connection refused
        """
        mock_run.return_value = mock_result
        
        bitrate, status, elapsed = get_stream_bitrate(
            'http://test.com/stream.m3u8',
            duration=30,
            timeout=10
        )
        
        # Bitrate should be None, status OK (since subprocess didn't timeout/crash)
        self.assertIsNone(bitrate, "Bitrate should be None when ffmpeg fails")
        self.assertEqual(status, "OK", "Status should be OK")
        
        # Verify that warnings were called
        self.assertTrue(mock_logger.warning.called, "Warning should be logged")
        
        # Check that error details were logged
        warning_calls = [str(call) for call in mock_logger.warning.call_args_list]
        warning_text = ' '.join(warning_calls)
        
        # Should mention non-zero exit code
        self.assertIn("exited with code 1", warning_text, "Should log non-zero exit code")

    @patch('subprocess.run')
    @patch('stream_check_utils.logger')
    def test_early_completion_warning(self, mock_logger, mock_run):
        """Test that early completion without errors is also flagged."""
        # Simulate ffmpeg completing very quickly with exit code 0 but no data
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stderr = """
[info] Stream started
[info] Stream ended
        """
        mock_run.return_value = mock_result
        
        bitrate, status, elapsed = get_stream_bitrate(
            'http://test.com/stream.m3u8',
            duration=30,
            timeout=10
        )
        
        # Should warn about early completion
        warning_calls = [str(call) for call in mock_logger.warning.call_args_list]
        warning_text = ' '.join(warning_calls)
        
        # Should mention that it completed early
        self.assertIn("expected ~30s", warning_text, "Should mention expected duration")


if __name__ == '__main__':
    unittest.main()
