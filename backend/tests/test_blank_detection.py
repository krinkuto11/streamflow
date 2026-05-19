#!/usr/bin/env python3
"""Tests for quality-check blank-screen detection."""

import os
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from apps.core.stream_stats_utils import is_stream_dead
from apps.stream import stream_check_utils
from apps.stream.stream_check_utils import (
    _parse_blank_detection,
    analyze_stream,
    get_stream_info_and_bitrate,
)


def _ffmpeg_output(extra: str = "") -> str:
    return f"""
Input #0, mpegts, from 'http://example.com/stream.m3u8':
  Duration: N/A, start: 0.000000, bitrate: N/A
    Stream #0:0: Video: h264, yuv420p, 1920x1080, 25 fps
    Stream #0:1: Audio: aac, 48000 Hz, stereo
frame=  750 fps=25 q=-1.0 size=12345kB time=00:00:30.00 bitrate=3333.3kbits/s speed=1.0x
{extra}
"""


class TestBlankDetectionParsing(unittest.TestCase):
    def test_blank_window_above_ratio_threshold_is_detected(self):
        output = _ffmpeg_output(
            "[blackdetect @ 000] black_start:0 black_end:25 black_duration:25"
        )

        result = _parse_blank_detection(output, duration=30, blank_ratio_threshold=0.80)

        self.assertTrue(result["blank_detected"])
        self.assertEqual(result["blank_duration_secs"], 25.0)
        self.assertAlmostEqual(result["blank_ratio"], 0.8333)
        self.assertEqual(result["blank_segments"][0]["start"], 0.0)

    def test_short_blank_window_below_ratio_threshold_is_clean(self):
        output = _ffmpeg_output(
            "[blackdetect @ 000] black_start:10 black_end:12 black_duration:2"
        )

        result = _parse_blank_detection(output, duration=30, blank_ratio_threshold=0.80)

        self.assertFalse(result["blank_detected"])
        self.assertEqual(result["blank_duration_secs"], 2.0)
        self.assertAlmostEqual(result["blank_ratio"], 0.0667)

    def test_open_blank_segment_runs_until_probe_end(self):
        output = _ffmpeg_output("[blackdetect @ 000] black_start:0")

        result = _parse_blank_detection(output, duration=30, blank_ratio_threshold=0.80)

        self.assertTrue(result["blank_detected"])
        self.assertEqual(result["blank_duration_secs"], 30.0)
        self.assertEqual(result["blank_ratio"], 1.0)


class TestBlankDetectionFfmpegCommand(unittest.TestCase):
    @patch.object(stream_check_utils.subprocess, "run")
    def test_blank_detection_uses_one_input_connection(self, mock_run):
        mock_run.return_value = Mock(
            stderr=_ffmpeg_output(
                "[blackdetect @ 000] black_start:0 black_end:30 black_duration:30"
            ),
            returncode=0,
        )

        result = get_stream_info_and_bitrate(
            "http://example.com/test.m3u8",
            duration=30,
            timeout=30,
            blank_check_enabled=True,
        )

        command = mock_run.call_args.args[0]
        self.assertEqual(command[0], "ffmpeg")
        self.assertEqual(command.count("-i"), 1)
        self.assertNotIn("ffprobe", command)
        self.assertIn("-vf", command)
        self.assertTrue(any("blackdetect=d=2:pix_th=0.1" in arg for arg in command))
        self.assertTrue(result["blank_probe_ran"])
        self.assertTrue(result["blank_detected"])

    @patch.object(stream_check_utils.subprocess, "run")
    def test_blank_detection_disabled_preserves_plain_quality_command(self, mock_run):
        mock_run.return_value = Mock(stderr=_ffmpeg_output(), returncode=0)

        result = get_stream_info_and_bitrate(
            "http://example.com/test.m3u8",
            duration=30,
            timeout=30,
        )

        command = mock_run.call_args.args[0]
        self.assertEqual(command.count("-i"), 1)
        self.assertFalse(any("blackdetect" in arg for arg in command))
        self.assertFalse(result["blank_probe_ran"])
        self.assertFalse(result["blank_detected"])


class TestBlankDetectionAnalysis(unittest.TestCase):
    def test_analyze_stream_passes_blank_options_and_returns_metrics(self):
        with patch.object(stream_check_utils, "get_stream_info_and_bitrate") as mock_probe:
            mock_probe.return_value = {
                "video_codec": "h264",
                "audio_codec": "aac",
                "resolution": "1920x1080",
                "fps": 25.0,
                "bitrate_kbps": 3333.3,
                "hdr_format": None,
                "pixel_format": None,
                "audio_sample_rate": None,
                "audio_channels": None,
                "channel_layout": None,
                "audio_bitrate": None,
                "status": "OK",
                "elapsed_time": 30.0,
                "blank_probe_ran": True,
                "blank_detected": True,
                "blank_duration_secs": 30.0,
                "blank_ratio": 1.0,
                "blank_segments": [{"start": 0.0, "end": 30.0, "duration": 30.0}],
            }

            with self.assertLogs(stream_check_utils.logger, level="INFO") as logs:
                result = analyze_stream(
                    stream_url="http://example.com/test.m3u8",
                    stream_id=42,
                    stream_name="Blank Test",
                    blank_check_enabled=True,
                    blank_check_min_duration=3.0,
                    blank_check_pixel_threshold=0.08,
                    blank_check_ratio_threshold=0.75,
                )

        kwargs = mock_probe.call_args.kwargs
        self.assertTrue(kwargs["blank_check_enabled"])
        self.assertEqual(kwargs["blank_check_min_duration"], 3.0)
        self.assertEqual(kwargs["blank_check_pixel_threshold"], 0.08)
        self.assertEqual(kwargs["blank_check_ratio_threshold"], 0.75)
        self.assertTrue(result["blank_probe_ran"])
        self.assertTrue(result["blank_detected"])
        self.assertEqual(result["blank_duration_secs"], 30.0)
        self.assertEqual(result["blank_ratio"], 1.0)
        self.assertTrue(any(
            "[blank-detect] Blank Test (ID: 42)" in message and "ratio=1.000" in message
            for message in logs.output
        ))

    def test_blank_detection_marks_stream_dead(self):
        stream_data = {
            "resolution": "1920x1080",
            "bitrate_kbps": 3333.3,
            "blank_probe_ran": True,
            "blank_detected": True,
        }

        self.assertEqual(is_stream_dead(stream_data), (True, "blank"))

    def test_blank_detection_can_be_kept_out_of_dead_classification(self):
        stream_data = {
            "resolution": "1920x1080",
            "bitrate_kbps": 3333.3,
            "blank_probe_ran": True,
            "blank_detected": True,
        }

        self.assertEqual(
            is_stream_dead(stream_data, {"treat_blank_as_dead": False}),
            (False, "none"),
        )

    def test_persisted_blank_detection_marks_stream_dead(self):
        stream_data = {
            "stream_stats": {
                "resolution": "1920x1080",
                "ffmpeg_output_bitrate": 3333,
                "blank_probe_ran": True,
                "blank_detected": True,
            }
        }

        self.assertEqual(is_stream_dead(stream_data), (True, "blank"))


if __name__ == "__main__":
    unittest.main()
