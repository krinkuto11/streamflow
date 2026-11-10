"""Regression coverage for accelerated probes and per-profile sample windows."""

import sys
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from apps.core.probe_completion import (
    media_window_seconds, normalize_minimum_probe_completion_percent,
    probe_window_completion,
)
from apps.core.stream_stats_utils import is_stream_dead
from apps.stream import stream_check_utils


def measured(**changes):
    return {
        'video_codec': 'hevc', 'audio_codec': 'aac', 'resolution': '3840x2160',
        'fps': 50, 'bitrate_kbps': 12000, 'hdr_format': 'HLG',
        'pixel_format': None, 'audio_sample_rate': None, 'audio_channels': None,
        'channel_layout': None, 'audio_bitrate': None, 'status': 'OK',
        'elapsed_time': 20, 'ffmpeg_duration': 30, 'ffmpeg_exit_code': 0,
        'probe_media_seconds': None,
        **changes,
    }


def test_full_media_window_is_accepted_below_wall_clock_threshold():
    row = measured(probe_media_seconds=media_window_seconds(range(31), 30))
    assert probe_window_completion(row) == (True, 'media_time')
    assert is_stream_dead(row) == (False, 'none')


@pytest.mark.parametrize('times', [
    [30], [20, 21, 30], [0, 1, 30], [0, 5, 4, 30],
    [0, 5, float('nan')], [0, 5, float('inf')], [-2, 0, 5], [0, 5, 300],
])
def test_bad_timestamps_retain_wall_clock_fallback(times):
    row = measured(probe_media_seconds=media_window_seconds(times, 30))
    assert row['probe_media_seconds'] is None
    assert is_stream_dead(row) == (True, 'unstable')


@pytest.mark.parametrize('percent, expected_calls, dead', [(80, 2, True), (60, 1, False), (0, 1, False)])
def test_profile_threshold_controls_retries_and_final_verdict_consistently(percent, expected_calls, dead):
    with patch.object(stream_check_utils, 'get_stream_info_and_bitrate', return_value=measured()) as probe:
        result = stream_check_utils.analyze_stream(
            'http://fixture.test/live.ts', 1, 'UHD fixture', ffmpeg_duration=30,
            retries=1, retry_delay=0, minimum_probe_completion_percent=percent,
        )
    assert probe.call_count == expected_calls
    assert is_stream_dead(result)[0] is dead
    assert result['minimum_probe_completion_percent'] == percent


@pytest.mark.parametrize('failure', [
    {'ffmpeg_exit_code': 1}, {'ffmpeg_error_detected': True}, {'status': 'Error'}, {'status': 'Timeout'},
])
def test_relaxed_duration_never_accepts_probe_errors(failure):
    row = measured(probe_media_seconds=30, **failure)
    assert not probe_window_completion(row, 0)[0]
    assert is_stream_dead(row, {'minimum_probe_completion_percent': 0}) == (True, 'offline')


@pytest.mark.parametrize('flag, reason', [('blank', 'blank'), ('freeze', 'freeze')])
def test_duration_disabled_keeps_enabled_visual_failures(flag, reason):
    row = measured(**{flag + '_detected': True, flag + '_probe_ran': True})
    config = {'minimum_probe_completion_percent': 0, 'treat_' + flag + '_as_dead': True}
    assert is_stream_dead(row, config) == (True, reason)


def test_media_time_requires_successful_exit_and_usable_measurements():
    for change in [{'ffmpeg_exit_code': None}, {'bitrate_kbps': None}, {'fps': 0}, {'resolution': '0x0'}]:
        assert not probe_window_completion(measured(probe_media_seconds=30, **change))[0]


@pytest.mark.parametrize('bad', [None, True, False, 'bad', 'nan', float('inf'), -1, 101])
def test_invalid_profile_values_use_safe_default(bad):
    assert normalize_minimum_probe_completion_percent(bad) == 80


def test_legacy_cached_metrics_without_timing_remain_usable():
    row = measured()
    del row['elapsed_time']
    del row['ffmpeg_duration']
    del row['ffmpeg_exit_code']
    assert is_stream_dead(row) == (False, 'none')


def test_profile_serialization_preserves_zero_and_normalizes_legacy_values():
    from apps.automation.automation_config_manager import AutomationConfigManager
    manager = object.__new__(AutomationConfigManager)
    for configured, expected in [(0, 0), (60, 60), ('70', 70), ('invalid', 80)]:
        result = manager._normalize_profile_settings({
            'stream_checking': {'minimum_probe_completion_percent': configured},
        })
        assert result['stream_checking']['minimum_probe_completion_percent'] == expected
    assert manager._normalize_profile_settings({'stream_checking': {}})[
        'stream_checking']['minimum_probe_completion_percent'] == 80


@pytest.mark.parametrize('exit_code, trailing_error', [(0, ''), (1, ''), (0, 'Error during demuxing: Input/output error')])
def test_analyzer_carries_real_progress_evidence_and_rejects_ffmpeg_failures(exit_code, trailing_error):
    output = '\n'.join([
        'Input #0, mpegts, from fixture:',
        '  Stream #0:0: Video: hevc (Main 10), yuv420p10le, 3840x2160, 50 fps, 50 tbr',
        '  Stream #0:1: Audio: aac (LC), 48000 Hz, stereo, 128 kb/s',
        'Output #0, mpegts, to pipe:1:',
        *[f'frame= {i * 50} fps=75 size=1000kB time=00:00:{i:05.2f} bitrate=12000.0kbits/s speed=1.5x'
          for i in range(31)], trailing_error,
    ])
    completed = subprocess.CompletedProcess([], exit_code, stderr=output)
    with patch.object(stream_check_utils, '_run_ffmpeg_with_optional_fallback', return_value=completed), \
            patch.object(stream_check_utils.time, 'monotonic', side_effect=[100, 120]):
        result = stream_check_utils.get_stream_info_and_bitrate('http://fixture.test/live.ts', duration=30)
    assert result['probe_media_seconds'] == 30
    assert result['resolution'] == '3840x2160'
    assert result['fps'] == 50
    assert result['bitrate_kbps'] == 12000
    result['ffmpeg_duration'] = 30
    if exit_code or trailing_error:
        assert result['status'] == 'Error'
        assert is_stream_dead(result, {'minimum_probe_completion_percent': 0}) == (True, 'offline')
    else:
        assert probe_window_completion(result) == (True, 'media_time')
        assert is_stream_dead(result) == (False, 'none')
