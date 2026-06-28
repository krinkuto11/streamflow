#!/usr/bin/env python3
"""
Unit tests for stream_check_utils module.

Tests the new focused stream checking implementation that extracts
essential quality metrics using ffmpeg/ffprobe.
"""

import unittest
from unittest.mock import patch, MagicMock
import io
import json
import subprocess
import sys
import os
from time import monotonic as real_monotonic, sleep as real_sleep

# Add backend to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from apps.stream import stream_check_utils
from apps.stream.stream_check_utils import (
    check_ffmpeg_installed,
    get_stream_info,
    get_stream_info_and_bitrate,
    get_stream_bitrate,
    analyze_stream,
    _probe_stream_for_loops,
)


class TestFFmpegInstalled(unittest.TestCase):
    """Test checking for ffmpeg/ffprobe installation."""
    
    @patch('subprocess.run')
    def test_ffmpeg_installed(self, mock_run):
        """Test successful ffmpeg/ffprobe detection."""
        mock_run.return_value = MagicMock(returncode=0)
        self.assertTrue(check_ffmpeg_installed())
    
    @patch('subprocess.run')
    def test_ffmpeg_not_found(self, mock_run):
        """Test handling when ffmpeg/ffprobe not found."""
        mock_run.side_effect = FileNotFoundError()
        self.assertFalse(check_ffmpeg_installed())


class TestLoopProbeSampling(unittest.TestCase):
    """Loop probe tests for high-FPS FFmpeg frame pipes."""

    @staticmethod
    def _ppm_frame(seed):
        width, height = 32, 32
        pixels = bytearray()
        for y in range(height):
            for x in range(width):
                pixels.extend((
                    (x * 7 + seed * 13) % 256,
                    (y * 11 + seed * 17) % 256,
                    (((x ^ y) * 19) + seed * 23) % 256,
                ))
        return f"P6\n{width} {height}\n255\n".encode() + bytes(pixels)

    def test_loop_probe_samples_high_fps_frames_before_buffering(self):
        frames_per_second = 25
        loop_period_seconds = 20
        total_seconds = 45
        raw_frames = [
            self._ppm_frame((frame_index // frames_per_second) % loop_period_seconds)
            for frame_index in range(frames_per_second * total_seconds)
        ]
        pipe_bytes = b"".join(raw_frames)

        class FakeProcess:
            def __init__(self, payload):
                self.stdout = io.BytesIO(payload)
                self.stderr = io.BytesIO(b"")
                self._size = len(payload)

            def wait(self, timeout=None):
                deadline = real_monotonic() + 5
                while real_monotonic() < deadline:
                    try:
                        if self.stdout.tell() >= self._size:
                            break
                    except ValueError:
                        break
                    real_sleep(0.001)
                return 0

            def kill(self):
                return None

        monotonic_value = {"value": 100.0}

        def fake_monotonic():
            monotonic_value["value"] += 0.04
            return monotonic_value["value"]

        with patch.object(
            stream_check_utils.subprocess,
            "Popen",
            return_value=FakeProcess(pipe_bytes),
        ) as popen_mock, patch.object(stream_check_utils.time, "monotonic", side_effect=fake_monotonic):
            loop_detected, loop_duration, frames_processed = _probe_stream_for_loops(
                url="http://example.invalid/loop.ts",
                stream_tag="test-loop",
                probe_duration=60,
            )

        command = popen_mock.call_args.args[0]
        self.assertIn('fps=1,scale=32:32:flags=fast_bilinear,format=gray', command)

        self.assertTrue(loop_detected)
        self.assertIsNotNone(loop_duration)
        self.assertGreaterEqual(loop_duration, 10.0)
        self.assertLess(frames_processed, len(raw_frames))
        self.assertGreaterEqual(frames_processed, 20)

    def test_loop_probe_abort_callback_terminates_ffmpeg(self):
        class BlockingProcess:
            def __init__(self):
                self.stdout = io.BytesIO(b"")
                self.stderr = io.BytesIO(b"")
                self.terminated = False
                self.killed = False

            def wait(self, timeout=None):
                if self.terminated or self.killed:
                    return 0
                raise subprocess.TimeoutExpired(cmd="ffmpeg", timeout=timeout)

            def terminate(self):
                self.terminated = True

            def kill(self):
                self.killed = True

        process = BlockingProcess()

        with patch.object(
            stream_check_utils.subprocess,
            "Popen",
            return_value=process,
        ):
            loop_detected, loop_duration, frames_processed = _probe_stream_for_loops(
                url="http://example.invalid/loop.ts",
                stream_tag="test-loop-abort",
                probe_duration=60,
                should_abort=lambda: True,
            )

        self.assertFalse(loop_detected)
        self.assertIsNone(loop_duration)
        self.assertEqual(frames_processed, 0)
        self.assertTrue(process.terminated)

    def test_loop_probe_latches_transient_loop_detection(self):
        raw_frames = [self._ppm_frame(frame_index) for frame_index in range(40)]
        pipe_bytes = b"".join(raw_frames)

        class FakeProcess:
            def __init__(self, payload):
                self.stdout = io.BytesIO(payload)
                self.stderr = io.BytesIO(b"")
                self._size = len(payload)

            def wait(self, timeout=None):
                deadline = real_monotonic() + 5
                while real_monotonic() < deadline:
                    try:
                        if self.stdout.tell() >= self._size:
                            break
                    except ValueError:
                        break
                    real_sleep(0.001)
                return 0

            def kill(self):
                return None

        monotonic_value = {"value": 100.0}
        detect_calls = {"count": 0}

        def fake_monotonic():
            monotonic_value["value"] += 1.1
            return monotonic_value["value"]

        def fake_detect_loop(self, hamming_tolerance=None):
            detect_calls["count"] += 1
            return 12.0 if detect_calls["count"] == 10 else None

        with patch.object(
            stream_check_utils.subprocess,
            "Popen",
            return_value=FakeProcess(pipe_bytes),
        ), patch.object(
            stream_check_utils.time,
            "monotonic",
            side_effect=fake_monotonic,
        ), patch(
            "apps.stream.sidecar_loop_detector.SidecarLoopDetector.detect_loop",
            fake_detect_loop,
        ):
            loop_detected, loop_duration, frames_processed = _probe_stream_for_loops(
                url="http://example.invalid/loop.ts",
                stream_tag="test-loop-latch",
                probe_duration=60,
            )

        self.assertTrue(loop_detected)
        self.assertEqual(loop_duration, 12.0)
        self.assertEqual(frames_processed, len(raw_frames))


class TestGetStreamInfo(unittest.TestCase):
    """Test extracting stream information with ffprobe."""
    
    @patch('subprocess.run')
    def test_successful_stream_info(self, mock_run):
        """Test successful extraction of stream info."""
        mock_output = {
            'streams': [
                {
                    'codec_name': 'h264',
                    'width': 1920,
                    'height': 1080,
                    'avg_frame_rate': '30/1'
                },
                {
                    'codec_name': 'aac'
                }
            ]
        }
        mock_run.return_value = MagicMock(
            stdout=json.dumps(mock_output),
            stderr=""
        )
        
        video_info, audio_info = get_stream_info('http://test.stream', timeout=10)
        
        self.assertIsNotNone(video_info)
        self.assertIsNotNone(audio_info)
        self.assertEqual(video_info['codec_name'], 'h264')
        self.assertEqual(video_info['width'], 1920)
        self.assertEqual(video_info['height'], 1080)
        self.assertEqual(audio_info['codec_name'], 'aac')
    
    @patch('subprocess.run')
    def test_timeout_handling(self, mock_run):
        """Test handling of ffprobe timeout."""
        import subprocess
        mock_run.side_effect = subprocess.TimeoutExpired('ffprobe', 10)
        
        video_info, audio_info = get_stream_info('http://test.stream', timeout=10)
        
        self.assertIsNone(video_info)
        self.assertIsNone(audio_info)
    
    @patch('subprocess.run')
    def test_invalid_json_handling(self, mock_run):
        """Test handling of invalid JSON from ffprobe."""
        mock_run.return_value = MagicMock(
            stdout="invalid json",
            stderr=""
        )
        
        video_info, audio_info = get_stream_info('http://test.stream', timeout=10)
        
        self.assertIsNone(video_info)
        self.assertIsNone(audio_info)


class TestGetStreamBitrate(unittest.TestCase):
    """Test extracting stream bitrate with ffmpeg."""
    
    # Removed test_bitrate_method_1_statistics as Method 1 (Statistics: bytes read) is deprecated.
    
    @patch('subprocess.run')
    def test_bitrate_method_2_progress(self, mock_run):
        """Test bitrate detection using progress output."""
        mock_output = """
        frame= 900 fps= 30 q=-1.0 size=12345kB time=00:00:30.00 bitrate=3333.3kbits/s speed=1.0x
        """
        mock_run.return_value = MagicMock(
            stderr=mock_output,
            returncode=0
        )
        
        bitrate, status, elapsed = get_stream_bitrate('http://test.stream', duration=30, timeout=10)
        
        self.assertIsNotNone(bitrate)
        self.assertEqual(bitrate, 3333.3)
        self.assertEqual(status, "OK")
    
    @patch('subprocess.run')
    def test_timeout_handling(self, mock_run):
        """Test handling of ffmpeg timeout."""
        import subprocess
        mock_run.side_effect = subprocess.TimeoutExpired('ffmpeg', 40)
        
        bitrate, status, elapsed = get_stream_bitrate('http://test.stream', duration=30, timeout=10)
        
        self.assertIsNone(bitrate)
        self.assertEqual(status, "Timeout")


class TestGetStreamInfoAndBitrate(unittest.TestCase):
    """Test combined ffmpeg analysis with ffprobe safety fallbacks."""

    @patch('subprocess.run')
    def test_ffprobe_fallback_accepts_valid_media_after_ffmpeg_timeout(self, mock_run):
        """KPTV-style streams should not be marked dead when ffprobe proves media."""
        ffprobe_payload = {
            'programs': [
                {
                    'streams': [
                        {
                            'index': 0,
                            'codec_name': 'h264',
                            'codec_type': 'video',
                            'width': 1216,
                            'height': 684,
                            'r_frame_rate': '30/1',
                            'avg_frame_rate': '30/1',
                        },
                        {
                            'index': 1,
                            'codec_name': 'aac',
                            'codec_type': 'audio',
                            'bit_rate': '98800',
                        },
                    ],
                },
            ],
            'streams': [
                {
                    'index': 0,
                    'codec_name': 'h264',
                    'codec_type': 'video',
                    'width': 1216,
                    'height': 684,
                    'r_frame_rate': '30/1',
                    'avg_frame_rate': '30/1',
                },
                {
                    'index': 1,
                    'codec_name': 'aac',
                    'codec_type': 'audio',
                    'bit_rate': '98800',
                },
            ],
        }
        mock_run.side_effect = [
            subprocess.TimeoutExpired(cmd='ffmpeg', timeout=90),
            MagicMock(returncode=0, stdout=json.dumps(ffprobe_payload), stderr=''),
        ]

        result = get_stream_info_and_bitrate(
            'http://test.com/kptv-proxy-stream',
            duration=30,
            timeout=30,
            stream_startup_buffer=10,
        )

        self.assertEqual(result['status'], 'OK')
        self.assertEqual(result['resolution'], '1216x684')
        self.assertEqual(result['fps'], 30)
        self.assertEqual(result['video_codec'], 'h264')
        self.assertEqual(result['audio_codec'], 'aac')
        self.assertTrue(result['ffprobe_fallback_ran'])
        self.assertEqual(result['ffprobe_fallback_reason'], 'ffmpeg_timeout')
        self.assertEqual(result['bitrate_source'], 'ffprobe_media_fallback_no_bitrate')


class TestAnalyzeStream(unittest.TestCase):
    """Test complete stream analysis."""
    
    @patch('stream_check_utils.get_stream_info_and_bitrate')
    def test_successful_analysis(self, mock_get_info_and_bitrate):
        """Test successful complete stream analysis."""
        # Mock the combined function
        mock_get_info_and_bitrate.return_value = {
            'video_codec': 'h264',
            'audio_codec': 'aac',
            'resolution': '1920x1080',
            'fps': 30.0,
            'bitrate_kbps': 5000.0,
            'hdr_format': None,
            'pixel_format': None,
            'audio_sample_rate': None,
            'audio_channels': None,
            'channel_layout': None,
            'audio_bitrate': None,
            'status': 'OK',
            'elapsed_time': 30.5
        }
        
        result = analyze_stream(
            stream_url='http://test.stream',
            stream_id=123,
            stream_name='Test Stream',
            ffmpeg_duration=30,
            timeout=30,
            retries=1,
            retry_delay=5
        )
        
        self.assertEqual(result['stream_id'], 123)
        self.assertEqual(result['stream_name'], 'Test Stream')
        self.assertEqual(result['stream_url'], 'http://test.stream')
        self.assertEqual(result['video_codec'], 'h264')
        self.assertEqual(result['audio_codec'], 'aac')
        self.assertEqual(result['resolution'], '1920x1080')
        self.assertEqual(result['fps'], 30.0)
        self.assertEqual(result['bitrate_kbps'], 5000.0)
        self.assertEqual(result['status'], 'OK')
    
    @patch('stream_check_utils.get_stream_info_and_bitrate')
    def test_no_video_info(self, mock_get_info_and_bitrate):
        """Test handling when no video info is available."""
        mock_get_info_and_bitrate.return_value = {
            'video_codec': 'N/A',
            'audio_codec': 'N/A',
            'resolution': '0x0',
            'fps': 0,
            'bitrate_kbps': None,
            'hdr_format': None,
            'pixel_format': None,
            'audio_sample_rate': None,
            'audio_channels': None,
            'channel_layout': None,
            'audio_bitrate': None,
            'status': 'Error',
            'elapsed_time': 0
        }
        
        result = analyze_stream(
            stream_url='http://test.stream',
            stream_id=123,
            stream_name='Test Stream'
        )
        
        self.assertEqual(result['video_codec'], 'N/A')
        self.assertEqual(result['audio_codec'], 'N/A')
        self.assertEqual(result['resolution'], '0x0')
        self.assertEqual(result['fps'], 0)
    
    @patch('stream_check_utils.get_stream_info_and_bitrate')
    @patch('time.sleep')
    def test_retry_on_failure(self, mock_sleep, mock_get_info_and_bitrate):
        """Test retry logic when stream analysis fails."""
        # First call fails, second succeeds
        mock_get_info_and_bitrate.side_effect = [
            {
                'video_codec': 'h264',
                'audio_codec': 'N/A',
                'resolution': '1920x1080',
                'fps': 30.0,
                'bitrate_kbps': None,
                'hdr_format': None,
                'pixel_format': None,
                'audio_sample_rate': None,
                'audio_channels': None,
                'channel_layout': None,
                'audio_bitrate': None,
                'status': 'Timeout',
                'elapsed_time': 40
            },
            {
                'video_codec': 'h264',
                'audio_codec': 'N/A',
                'resolution': '1920x1080',
                'fps': 30.0,
                'bitrate_kbps': 5000.0,
                'hdr_format': None,
                'pixel_format': None,
                'audio_sample_rate': None,
                'audio_channels': None,
                'channel_layout': None,
                'audio_bitrate': None,
                'status': 'OK',
                'elapsed_time': 30.5
            }
        ]
        
        result = analyze_stream(
            stream_url='http://test.stream',
            stream_id=123,
            stream_name='Test Stream',
            retries=1,
            retry_delay=5
        )
        
        # Should have retried
        self.assertEqual(mock_get_info_and_bitrate.call_count, 2)
        self.assertEqual(mock_sleep.call_count, 1)
        
        # Final result should be successful
        self.assertEqual(result['status'], 'OK')
        self.assertEqual(result['bitrate_kbps'], 5000.0)

    @patch('stream_check_utils.get_stream_info_and_bitrate')
    @patch('time.sleep')
    def test_retry_on_completed_probe_missing_bitrate(self, mock_sleep, mock_get_info_and_bitrate):
        """A full-length OK probe without bitrate should be retried before reuse."""
        mock_get_info_and_bitrate.side_effect = [
            {
                'video_codec': 'hevc',
                'audio_codec': 'aac',
                'resolution': '3840x2160',
                'fps': 50.0,
                'bitrate_kbps': None,
                'bitrate_source': 'ffprobe_media_fallback_no_bitrate',
                'hdr_format': 'HLG',
                'pixel_format': None,
                'audio_sample_rate': None,
                'audio_channels': None,
                'channel_layout': None,
                'audio_bitrate': None,
                'status': 'OK',
                'elapsed_time': 30.5,
                'ffprobe_fallback_ran': True,
                'ffprobe_fallback_reason': 'ffmpeg_timeout',
            },
            {
                'video_codec': 'hevc',
                'audio_codec': 'aac',
                'resolution': '3840x2160',
                'fps': 50.0,
                'bitrate_kbps': 17870.0,
                'bitrate_source': 'ffmpeg_progress',
                'hdr_format': 'HLG',
                'pixel_format': None,
                'audio_sample_rate': None,
                'audio_channels': None,
                'channel_layout': None,
                'audio_bitrate': None,
                'status': 'OK',
                'elapsed_time': 30.5,
            },
        ]

        result = analyze_stream(
            stream_url='http://test.stream',
            stream_id=123,
            stream_name='Test Stream',
            ffmpeg_duration=30,
            retries=1,
            retry_delay=5,
        )

        self.assertEqual(mock_get_info_and_bitrate.call_count, 2)
        self.assertEqual(mock_sleep.call_count, 1)
        self.assertEqual(result['status'], 'OK')
        self.assertEqual(result['bitrate_kbps'], 17870.0)
        self.assertFalse(result['measurement_incomplete'])
        self.assertEqual(result['measurement_incomplete_reason'], 'none')
        self.assertFalse(result['bitrate_recheck_required'])

    @patch('stream_check_utils.get_stream_info_and_bitrate')
    def test_missing_bitrate_stays_alive_but_incomplete_after_last_attempt(self, mock_get_info_and_bitrate):
        """Missing bitrate must not mark a stream dead, but it is not reusable."""
        mock_get_info_and_bitrate.return_value = {
            'video_codec': 'hevc',
            'audio_codec': 'aac',
            'resolution': '3840x2160',
            'fps': 50.0,
            'bitrate_kbps': None,
            'bitrate_source': 'ffprobe_media_fallback_no_bitrate',
            'hdr_format': 'HLG',
            'pixel_format': None,
            'audio_sample_rate': None,
            'audio_channels': None,
            'channel_layout': None,
            'audio_bitrate': None,
            'status': 'OK',
            'elapsed_time': 30.5,
            'ffprobe_fallback_ran': True,
            'ffprobe_fallback_reason': 'ffmpeg_timeout',
        }

        result = analyze_stream(
            stream_url='http://test.stream',
            stream_id=123,
            stream_name='Test Stream',
            ffmpeg_duration=30,
            retries=0,
        )

        self.assertEqual(result['status'], 'OK')
        self.assertEqual(result['quality_reason'], 'none')
        self.assertEqual(result['quality_reason_detail'], 'none')
        self.assertTrue(result['measurement_incomplete'])
        self.assertEqual(result['measurement_incomplete_reason'], 'missing_bitrate')
        self.assertTrue(result['bitrate_recheck_required'])
        self.assertEqual(result['measurement_incomplete_context']['bitrate_source'], 'ffprobe_media_fallback_no_bitrate')

    @patch('stream_check_utils.get_stream_info_and_bitrate')
    def test_timeout_analysis_carries_quality_reason_context(self, mock_get_info_and_bitrate):
        """Timeouts should expose structured context for UI and persisted stats."""
        mock_get_info_and_bitrate.return_value = {
            'video_codec': 'N/A',
            'audio_codec': 'N/A',
            'resolution': '0x0',
            'fps': 0,
            'bitrate_kbps': None,
            'hdr_format': None,
            'pixel_format': None,
            'audio_sample_rate': None,
            'audio_channels': None,
            'channel_layout': None,
            'audio_bitrate': None,
            'status': 'Timeout',
            'elapsed_time': 65,
            'timeout_seconds': 65,
            'operation_timeout_seconds': 30,
            'ffmpeg_duration_seconds': 30,
            'startup_buffer_seconds': 5,
        }

        result = analyze_stream(
            stream_url='http://test.stream',
            stream_id=123,
            stream_name='Test Stream',
            ffmpeg_duration=30,
            timeout=30,
            retries=0,
            stream_startup_buffer=5,
        )

        self.assertEqual(result['status'], 'Timeout')
        self.assertEqual(result['quality_reason'], 'offline')
        self.assertEqual(result['quality_reason_detail'], 'stream_timeout')
        self.assertEqual(result['quality_reason_context'], {
            'elapsed_seconds': 65,
            'timeout_seconds': 65,
            'operation_timeout_seconds': 30,
            'ffmpeg_duration_seconds': 30,
            'startup_buffer_seconds': 5,
            'attempt': 1,
            'attempts': 1,
            'max_attempts': 1,
            'stage': 'stream analysis',
        })

    @patch('stream_check_utils.get_stream_info_and_bitrate')
    @patch('time.sleep')
    def test_preempted_analysis_does_not_retry(self, mock_sleep, mock_get_info_and_bitrate):
        """Viewer preemption should return a distinct non-retried status."""
        mock_get_info_and_bitrate.return_value = {
            'video_codec': 'N/A',
            'audio_codec': 'N/A',
            'resolution': '0x0',
            'fps': 0,
            'bitrate_kbps': None,
            'hdr_format': None,
            'pixel_format': None,
            'audio_sample_rate': None,
            'audio_channels': None,
            'channel_layout': None,
            'audio_bitrate': None,
            'status': 'PREEMPTED',
            'elapsed_time': 1.0,
            'preempted': True,
            'preempt_reason': 'viewer_preempted',
        }

        result = analyze_stream(
            stream_url='http://test.stream',
            stream_id=123,
            stream_name='Test Stream',
            retries=2,
            retry_delay=5,
        )

        self.assertEqual(mock_get_info_and_bitrate.call_count, 1)
        self.assertEqual(mock_sleep.call_count, 0)
        self.assertEqual(result['status'], 'PREEMPTED')
        self.assertTrue(result['preempted'])
        self.assertEqual(result['preempt_reason'], 'viewer_preempted')


if __name__ == '__main__':
    unittest.main()
