#!/usr/bin/env python3
"""Tests for optional ffmpeg hardware acceleration settings."""

import os
import subprocess
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from apps.stream import stream_check_utils
from apps.stream.stream_check_utils import (
    analyze_stream,
    collect_hardware_acceleration_diagnostics,
    get_stream_info_and_bitrate,
    normalize_hardware_acceleration_config,
)


def _ffmpeg_output() -> str:
    return """
Input #0, mpegts, from 'http://example.com/stream.m3u8':
  Duration: N/A, start: 0.000000, bitrate: N/A
    Stream #0:0: Video: h264, yuv420p, 1920x1080, 25 fps
    Stream #0:1: Audio: aac, 48000 Hz, stereo
frame=  750 fps=25 q=-1.0 size=12345kB time=00:00:30.00 bitrate=3333.3kbits/s speed=1.0x
"""


class TestHardwareAccelerationConfig(unittest.TestCase):
    def test_default_config_is_cpu_safe(self):
        config = normalize_hardware_acceleration_config(None)

        self.assertFalse(config["enabled"])
        self.assertEqual(config["mode"], "auto")
        self.assertTrue(config["allow_fallback"])

    def test_invalid_mode_is_disabled(self):
        config = normalize_hardware_acceleration_config({
            "enabled": True,
            "mode": "definitely-not-a-real-mode",
        })

        self.assertFalse(config["enabled"])
        self.assertEqual(config["mode"], "auto")

    def test_invalid_device_is_dropped(self):
        config = normalize_hardware_acceleration_config({
            "enabled": True,
            "mode": "vaapi",
            "device": "/dev/dri/renderD128;rm -rf /",
        })

        self.assertTrue(config["enabled"])
        self.assertEqual(config["mode"], "vaapi")
        self.assertEqual(config["device"], "")

    def test_startup_diagnostics_reports_supported_cuda(self):
        calls = []

        def runner(command, timeout):
            calls.append(command)
            if command[0] == "ffmpeg":
                return subprocess.CompletedProcess(
                    command,
                    0,
                    stdout="Hardware acceleration methods:\ncuda\nvaapi\n",
                    stderr="",
                )
            return subprocess.CompletedProcess(command, 0, stdout="NVIDIA Test GPU\n", stderr="")

        diagnostics = collect_hardware_acceleration_diagnostics(
            {"enabled": True, "mode": "cuda", "allow_fallback": True},
            command_runner=runner,
        )

        self.assertTrue(diagnostics["ffmpeg_available"])
        self.assertTrue(diagnostics["mode_supported"])
        self.assertTrue(diagnostics["nvidia_smi_ok"])
        self.assertEqual(diagnostics["nvidia_gpus"], ["NVIDIA Test GPU"])
        self.assertEqual(calls[0][0], "ffmpeg")
        self.assertEqual(calls[1][0], "nvidia-smi")

    def test_startup_diagnostics_does_not_probe_when_disabled(self):
        calls = []

        diagnostics = collect_hardware_acceleration_diagnostics(
            {"enabled": False, "mode": "cuda"},
            command_runner=lambda command, timeout: calls.append(command),
        )

        self.assertFalse(diagnostics["config"]["enabled"])
        self.assertEqual(calls, [])


class TestHardwareAccelerationFfmpegCommand(unittest.TestCase):
    @patch.object(stream_check_utils.subprocess, "run")
    def test_default_command_has_no_hwaccel_args(self, mock_run):
        mock_run.return_value = Mock(stderr=_ffmpeg_output(), returncode=0)

        get_stream_info_and_bitrate(
            "http://example.com/test.m3u8",
            duration=30,
            timeout=30,
        )

        command = mock_run.call_args.args[0]
        self.assertNotIn("-hwaccel", command)
        self.assertNotIn("-hwaccel_device", command)
        self.assertNotIn("-vaapi_device", command)

    @patch.object(stream_check_utils.subprocess, "run")
    def test_enabled_auto_mode_adds_hwaccel_args(self, mock_run):
        mock_run.return_value = Mock(stderr=_ffmpeg_output(), returncode=0)

        get_stream_info_and_bitrate(
            "http://example.com/test.m3u8",
            duration=30,
            timeout=30,
            hardware_acceleration={
                "enabled": True,
                "mode": "auto",
                "allow_fallback": True,
            },
        )

        command = mock_run.call_args.args[0]
        self.assertIn("-hwaccel", command)
        self.assertEqual(command[command.index("-hwaccel") + 1], "auto")
        self.assertLess(command.index("-hwaccel"), command.index("-i"))

    @patch.object(stream_check_utils.subprocess, "run")
    def test_vaapi_device_is_placed_before_input(self, mock_run):
        mock_run.return_value = Mock(stderr=_ffmpeg_output(), returncode=0)

        get_stream_info_and_bitrate(
            "http://example.com/test.m3u8",
            duration=30,
            timeout=30,
            hardware_acceleration={
                "enabled": True,
                "mode": "vaapi",
                "device": "/dev/dri/renderD128",
                "allow_fallback": True,
            },
        )

        command = mock_run.call_args.args[0]
        self.assertIn("-vaapi_device", command)
        self.assertIn("-hwaccel", command)
        self.assertLess(command.index("-vaapi_device"), command.index("-i"))
        self.assertLess(command.index("-hwaccel"), command.index("-i"))

    @patch.object(stream_check_utils.subprocess, "run")
    def test_hwaccel_failure_retries_without_hwaccel(self, mock_run):
        mock_run.side_effect = [
            Mock(stderr="Device creation failed: no device available", stdout="", returncode=1),
            Mock(stderr=_ffmpeg_output(), stdout="", returncode=0),
        ]

        result = get_stream_info_and_bitrate(
            "http://example.com/test.m3u8",
            duration=30,
            timeout=30,
            hardware_acceleration={
                "enabled": True,
                "mode": "auto",
                "allow_fallback": True,
            },
        )

        self.assertEqual(result["status"], "OK")
        self.assertEqual(mock_run.call_count, 2)
        first_command = mock_run.call_args_list[0].args[0]
        second_command = mock_run.call_args_list[1].args[0]
        self.assertIn("-hwaccel", first_command)
        self.assertNotIn("-hwaccel", second_command)


class TestHardwareAccelerationAnalysis(unittest.TestCase):
    def test_analyze_stream_passes_hardware_config(self):
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
            }

            analyze_stream(
                stream_url="http://example.com/test.m3u8",
                stream_id=42,
                stream_name="Test",
                hardware_acceleration={
                    "enabled": True,
                    "mode": "auto",
                    "allow_fallback": True,
                },
            )

        self.assertEqual(
            mock_probe.call_args.kwargs["hardware_acceleration"],
            {
                "enabled": True,
                "mode": "auto",
                "allow_fallback": True,
            },
        )


if __name__ == "__main__":
    unittest.main()
