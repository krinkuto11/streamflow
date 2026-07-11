#!/usr/bin/env python3
"""Tests for quality-check blank-screen detection."""

import os
import subprocess
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from apps.core.stream_stats_utils import is_stream_dead
from apps.stream import stream_check_utils
from apps.stream.stream_checker_service import StreamCheckerService
from apps.stream.stream_check_utils import (
    _parse_blank_detection,
    _parse_freeze_detection,
    _visual_probe_duration,
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


class TestFreezeDetectionParsing(unittest.TestCase):
    def test_freeze_window_above_ratio_threshold_is_detected(self):
        output = _ffmpeg_output(
            "[freezedetect @ 000] lavfi.freezedetect.freeze_start: 0\n"
            "[freezedetect @ 000] lavfi.freezedetect.freeze_duration: 26\n"
            "[freezedetect @ 000] lavfi.freezedetect.freeze_end: 26"
        )

        result = _parse_freeze_detection(output, duration=30, freeze_ratio_threshold=0.80)

        self.assertTrue(result["freeze_detected"])
        self.assertEqual(result["freeze_duration_secs"], 26.0)
        self.assertAlmostEqual(result["freeze_ratio"], 0.8667)
        self.assertEqual(result["freeze_segments"][0]["start"], 0.0)

    def test_short_freeze_window_below_ratio_threshold_is_clean(self):
        output = _ffmpeg_output(
            "[freezedetect @ 000] lavfi.freezedetect.freeze_start: 10\n"
            "[freezedetect @ 000] lavfi.freezedetect.freeze_duration: 2\n"
            "[freezedetect @ 000] lavfi.freezedetect.freeze_end: 12"
        )

        result = _parse_freeze_detection(output, duration=30, freeze_ratio_threshold=0.80)

        self.assertFalse(result["freeze_detected"])
        self.assertEqual(result["freeze_duration_secs"], 2.0)
        self.assertAlmostEqual(result["freeze_ratio"], 0.0667)

    def test_open_freeze_segment_runs_until_probe_end(self):
        output = _ffmpeg_output("[freezedetect @ 000] lavfi.freezedetect.freeze_start: 0")

        result = _parse_freeze_detection(output, duration=30, freeze_ratio_threshold=0.80)

        self.assertTrue(result["freeze_detected"])
        self.assertEqual(result["freeze_duration_secs"], 30.0)
        self.assertEqual(result["freeze_ratio"], 1.0)


class TestVisualProbeDurationContract(unittest.TestCase):
    def test_no_visual_detector_keeps_requested_duration(self):
        self.assertEqual(_visual_probe_duration(
            duration=5,
            blank_check_enabled=False,
            blank_check_min_duration=2.0,
            freeze_check_enabled=False,
            freeze_check_min_duration=5.0,
        ), 5)

    def test_visual_detector_can_extend_beyond_requested_duration(self):
        self.assertEqual(_visual_probe_duration(
            duration=5,
            blank_check_enabled=False,
            blank_check_min_duration=2.0,
            freeze_check_enabled=True,
            freeze_check_min_duration=5.0,
        ), 10)
        self.assertEqual(_visual_probe_duration(
            duration=5,
            blank_check_enabled=True,
            blank_check_min_duration=12.0,
            freeze_check_enabled=False,
            freeze_check_min_duration=5.0,
        ), 13)

    def test_visual_probe_remains_short_when_requested_basis_window_is_longer(self):
        self.assertEqual(_visual_probe_duration(
            duration=30,
            blank_check_enabled=True,
            blank_check_min_duration=2.0,
            freeze_check_enabled=True,
            freeze_check_min_duration=5.0,
        ), 10)


class TestBlankDetectionFfmpegCommand(unittest.TestCase):
    @patch.object(stream_check_utils, "_ffprobe_media_fallback")
    @patch.object(stream_check_utils.subprocess, "run")
    def test_visual_probe_runs_after_basis_timeout_with_valid_media_fallback(
        self,
        mock_run,
        mock_ffprobe_fallback,
    ):
        mock_run.side_effect = [
            subprocess.TimeoutExpired(["ffmpeg"], 45),
            Mock(stderr=_ffmpeg_output(), returncode=0),
        ]
        mock_ffprobe_fallback.return_value = {
            "video_codec": "h264",
            "audio_codec": "aac",
            "resolution": "1920x1080",
            "fps": 25.0,
            "bitrate_kbps": None,
            "bitrate_source": "ffprobe_media_fallback_no_bitrate",
            "hdr_format": None,
            "pixel_format": "yuv420p",
            "audio_sample_rate": 48000,
            "audio_channels": 2,
            "channel_layout": "stereo",
            "audio_bitrate": 128,
            "status": "OK",
            "ffprobe_fallback_ran": True,
            "ffprobe_fallback_reason": "ffmpeg_timeout",
            "ffprobe_fallback_elapsed_time": 0.5,
        }

        result = get_stream_info_and_bitrate(
            "http://example.com/test.m3u8",
            duration=5,
            timeout=30,
            blank_check_enabled=True,
            freeze_check_enabled=True,
            freeze_check_min_duration=5.0,
        )

        self.assertEqual(mock_run.call_count, 2)
        visual_command = mock_run.call_args_list[1].args[0]
        self.assertIn("-vf", visual_command)
        self.assertEqual(visual_command[visual_command.index("-t") + 1], "10")
        self.assertTrue(result["ffprobe_fallback_ran"])
        self.assertTrue(result["visual_probe_ran"])
        self.assertTrue(result["visual_probe_completed"])
        self.assertTrue(result["blank_probe_ran"])
        self.assertTrue(result["freeze_probe_ran"])

    @patch.object(stream_check_utils.subprocess, "run")
    def test_visual_probe_extends_to_detector_minimum_and_reports_adjustment(self, mock_run):
        mock_run.side_effect = [
            Mock(stderr=_ffmpeg_output(), returncode=0),
            Mock(stderr=_ffmpeg_output(), returncode=0),
        ]

        result = get_stream_info_and_bitrate(
            "http://example.com/test.m3u8",
            duration=5,
            timeout=30,
            freeze_check_enabled=True,
            freeze_check_min_duration=5.0,
        )

        visual_command = mock_run.call_args_list[1].args[0]
        duration_index = visual_command.index("-t") + 1
        self.assertEqual(visual_command[duration_index], "10")
        self.assertEqual(result["visual_probe_requested_duration_seconds"], 5)
        self.assertEqual(result["visual_probe_minimum_duration_seconds"], 10)
        self.assertEqual(result["visual_probe_duration_seconds"], 10)
        self.assertTrue(result["visual_probe_duration_adjusted"])
        self.assertEqual(
            result["visual_probe_duration_adjustment_reason"],
            "detector_minimum_window",
        )
        self.assertTrue(result["visual_probe_completed"])

    @patch.object(stream_check_utils.subprocess, "run")
    def test_blank_detection_runs_separate_visual_probe(self, mock_run):
        mock_run.side_effect = [
            Mock(stderr=_ffmpeg_output(), returncode=0),
            Mock(
                stderr=_ffmpeg_output(
                    "[blackdetect @ 000] black_start:0 black_end:8 black_duration:8"
                ),
                returncode=0,
            ),
        ]

        result = get_stream_info_and_bitrate(
            "http://example.com/test.m3u8",
            duration=30,
            timeout=30,
            blank_check_enabled=True,
        )

        self.assertEqual(mock_run.call_count, 2)
        basis_command = mock_run.call_args_list[0].args[0]
        visual_command = mock_run.call_args_list[1].args[0]
        self.assertEqual(basis_command[0], "ffmpeg")
        self.assertEqual(basis_command.count("-i"), 1)
        self.assertNotIn("ffprobe", basis_command)
        self.assertNotIn("-vf", basis_command)
        self.assertEqual(visual_command.count("-i"), 1)
        self.assertIn("-vf", visual_command)
        self.assertTrue(any("blackdetect=d=2:pix_th=0.1" in arg for arg in visual_command))
        self.assertEqual(result["bitrate_kbps"], 3333.3)
        self.assertTrue(result["visual_probe_ran"])
        self.assertTrue(result["visual_probe_completed"])
        self.assertTrue(result["blank_probe_ran"])
        self.assertTrue(result["blank_detected"])

    @patch.object(stream_check_utils.subprocess, "run")
    def test_freeze_detection_runs_separate_visual_probe(self, mock_run):
        mock_run.side_effect = [
            Mock(stderr=_ffmpeg_output(), returncode=0),
            Mock(
                stderr=_ffmpeg_output(
                    "[freezedetect @ 000] lavfi.freezedetect.freeze_start: 0\n"
                    "[freezedetect @ 000] lavfi.freezedetect.freeze_duration: 10\n"
                    "[freezedetect @ 000] lavfi.freezedetect.freeze_end: 10"
                ),
                returncode=0,
            ),
        ]

        result = get_stream_info_and_bitrate(
            "http://example.com/test.m3u8",
            duration=30,
            timeout=30,
            freeze_check_enabled=True,
        )

        self.assertEqual(mock_run.call_count, 2)
        basis_command = mock_run.call_args_list[0].args[0]
        visual_command = mock_run.call_args_list[1].args[0]
        self.assertEqual(basis_command[0], "ffmpeg")
        self.assertEqual(basis_command.count("-i"), 1)
        self.assertNotIn("-vf", basis_command)
        self.assertEqual(visual_command.count("-i"), 1)
        self.assertIn("-vf", visual_command)
        self.assertTrue(any("freezedetect=n=0.001:d=5" in arg for arg in visual_command))
        self.assertEqual(result["bitrate_kbps"], 3333.3)
        self.assertTrue(result["visual_probe_ran"])
        self.assertTrue(result["visual_probe_completed"])
        self.assertTrue(result["freeze_probe_ran"])
        self.assertTrue(result["freeze_detected"])

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

    @patch.object(stream_check_utils.subprocess, "run")
    def test_visual_probe_timeout_preserves_basis_bitrate(self, mock_run):
        def run_side_effect(command, **kwargs):
            if "-vf" in command:
                raise subprocess.TimeoutExpired(command, kwargs.get("timeout", 30))
            return Mock(stderr=_ffmpeg_output(), returncode=0)

        mock_run.side_effect = run_side_effect

        result = get_stream_info_and_bitrate(
            "http://example.com/test.m3u8",
            duration=30,
            timeout=30,
            blank_check_enabled=True,
            freeze_check_enabled=True,
        )

        self.assertEqual(result["bitrate_kbps"], 3333.3)
        self.assertEqual(result["bitrate_source"], "ffmpeg_progress")
        self.assertTrue(result["blank_probe_ran"])
        self.assertFalse(result["blank_detected"])
        self.assertTrue(result["freeze_probe_ran"])
        self.assertFalse(result["freeze_detected"])
        self.assertTrue(result["visual_probe_ran"])
        self.assertFalse(result["visual_probe_completed"])
        self.assertTrue(result["visual_probe_incomplete"])
        self.assertEqual(result["visual_probe_incomplete_reason"], "timeout")
        self.assertTrue(result["measurement_incomplete"])


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
                "freeze_probe_ran": True,
                "freeze_detected": True,
                "freeze_duration_secs": 30.0,
                "freeze_ratio": 1.0,
                "freeze_segments": [{"start": 0.0, "end": 30.0, "duration": 30.0}],
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
                    freeze_check_enabled=True,
                    freeze_check_min_duration=4.0,
                    freeze_check_noise_threshold=0.002,
                    freeze_check_ratio_threshold=0.70,
                )

        kwargs = mock_probe.call_args.kwargs
        self.assertTrue(kwargs["blank_check_enabled"])
        self.assertEqual(kwargs["blank_check_min_duration"], 3.0)
        self.assertEqual(kwargs["blank_check_pixel_threshold"], 0.08)
        self.assertEqual(kwargs["blank_check_ratio_threshold"], 0.75)
        self.assertTrue(kwargs["freeze_check_enabled"])
        self.assertEqual(kwargs["freeze_check_min_duration"], 4.0)
        self.assertEqual(kwargs["freeze_check_noise_threshold"], 0.002)
        self.assertEqual(kwargs["freeze_check_ratio_threshold"], 0.70)
        self.assertTrue(result["blank_probe_ran"])
        self.assertTrue(result["blank_detected"])
        self.assertTrue(result["freeze_probe_ran"])
        self.assertTrue(result["freeze_detected"])
        self.assertEqual(result["blank_duration_secs"], 30.0)
        self.assertEqual(result["blank_ratio"], 1.0)
        blank_log = "\n".join(
            message for message in logs.output if "[blank-detect]" in message
        )
        self.assertIn("[blank-detect] stream_ref=", blank_log)
        self.assertIn("detected=True", blank_log)
        self.assertIn("ratio=1.000", blank_log)
        self.assertNotIn("Blank Test", blank_log)
        self.assertNotIn("ID: 42", blank_log)

    def test_freeze_detection_marks_stream_dead(self):
        stream_data = {
            "resolution": "1920x1080",
            "bitrate_kbps": 3333.3,
            "freeze_probe_ran": True,
            "freeze_detected": True,
        }

        self.assertEqual(is_stream_dead(stream_data), (True, "freeze"))

    def test_freeze_detection_can_be_kept_out_of_dead_classification(self):
        stream_data = {
            "resolution": "1920x1080",
            "bitrate_kbps": 3333.3,
            "freeze_probe_ran": True,
            "freeze_detected": True,
        }

        self.assertEqual(
            is_stream_dead(stream_data, {"treat_freeze_as_dead": False}),
            (False, "none"),
        )

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

    def test_profile_blank_check_ignores_legacy_blank_as_dead_false(self):
        stream_data = {
            "resolution": "1920x1080",
            "bitrate_kbps": 3333.3,
            "blank_probe_ran": True,
            "blank_detected": True,
        }
        threshold_config = StreamCheckerService._build_threshold_config_from_profile(
            None,
            {"blank_check_enabled": True, "treat_blank_as_dead": False},
        )

        self.assertEqual(is_stream_dead(stream_data, threshold_config), (True, "blank"))

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

    def test_blank_detection_audit_logs_all_candidates_without_names(self):
        service = StreamCheckerService.__new__(StreamCheckerService)
        streams = [
            {
                "stream_id": 1000 + index,
                "stream_name": f"Sensitive Provider {index}",
                "blank_probe_ran": True,
                "blank_detected": True,
                "blank_duration_secs": 30.0,
                "blank_ratio": 1.0,
                "blank_segments": [{"start": 0.0, "end": 30.0, "duration": 30.0}],
                "dead_reason": "blank",
            }
            for index in range(12)
        ]
        streams.append({
            "stream_id": 2000,
            "stream_name": "Sensitive Clean Provider",
            "blank_probe_ran": True,
            "blank_detected": False,
            "blank_duration_secs": 0.0,
            "blank_ratio": 0.0,
            "blank_segments": [],
        })

        with self.assertLogs("apps.stream.stream_checker_service", level="INFO") as logs:
            service._log_blank_detection_summary(
                777,
                "Sensitive Channel",
                streams,
                dead_stream_ids={stream["stream_id"] for stream in streams[:12]},
                dead_stream_removal_enabled=True,
            )

        audit_log = "\n".join(logs.output)
        self.assertIn("blank=12", audit_log)
        self.assertEqual(audit_log.count("Blank candidate"), 12)
        self.assertIn("channel_ref=", audit_log)
        self.assertIn("stream_ref=", audit_log)
        self.assertIn("marked_dead=True", audit_log)
        self.assertIn("reason=blank", audit_log)
        self.assertIn("action=remove", audit_log)
        self.assertNotIn("Sensitive", audit_log)
        self.assertNotIn("stream_id=", audit_log)
        self.assertNotIn("channel_id=", audit_log)

    def test_blank_detection_refreshes_existing_dead_reason_without_sensitive_log(self):
        class FakeDeadStreamsTracker:
            def __init__(self):
                self.updated = []

            def get_dead_reason(self, stream_url):
                return "low_quality"

            def update_dead_reason(self, stream_url, reason, channel_id=None):
                self.updated.append((stream_url, reason, channel_id))
                return True

        service = StreamCheckerService.__new__(StreamCheckerService)
        service.dead_streams_tracker = FakeDeadStreamsTracker()

        with self.assertLogs("apps.stream.stream_checker_service", level="WARNING") as logs:
            updated = service._refresh_dead_stream_reason_if_needed(
                "http://sensitive.example/stream.m3u8",
                12345,
                "Sensitive Provider Stream",
                777,
                "blank",
                blank_detected=True,
            )

        self.assertTrue(updated)
        self.assertEqual(
            service.dead_streams_tracker.updated,
            [("http://sensitive.example/stream.m3u8", "blank", 777)],
        )
        audit_log = "\n".join(logs.output)
        self.assertIn("[blank-detect] Stream dead reason updated", audit_log)
        self.assertIn("reason=blank", audit_log)
        self.assertNotIn("sensitive.example", audit_log)
        self.assertNotIn("Sensitive Provider", audit_log)

    def test_dead_reason_refresh_skips_when_reason_is_current(self):
        class FakeDeadStreamsTracker:
            def __init__(self):
                self.updated = False

            def get_dead_reason(self, stream_url):
                return "blank"

            def update_dead_reason(self, stream_url, reason, channel_id=None):
                self.updated = True
                return True

        service = StreamCheckerService.__new__(StreamCheckerService)
        service.dead_streams_tracker = FakeDeadStreamsTracker()

        updated = service._refresh_dead_stream_reason_if_needed(
            "http://sensitive.example/stream.m3u8",
            12345,
            "Sensitive Provider Stream",
            777,
            "blank",
            blank_detected=True,
        )

        self.assertFalse(updated)
        self.assertFalse(service.dead_streams_tracker.updated)


if __name__ == "__main__":
    unittest.main()
