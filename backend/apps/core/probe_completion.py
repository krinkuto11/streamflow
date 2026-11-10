"""Shared duration policy for probe retries and final stream classification."""

import math

DEFAULT_MINIMUM_PROBE_COMPLETION_PERCENT = 80


def _finite(value):
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def normalize_minimum_probe_completion_percent(value):
    number = _finite(value)
    if number is None or not 0 <= number <= 100:
        return DEFAULT_MINIMUM_PROBE_COMPLETION_PERCENT
    return number


def media_window_seconds(progress_times, requested_seconds):
    """Accept bounded, continuous output progress, not an isolated timestamp.

    Large initial offsets, jumps, backwards time and implausible overruns cannot
    prove media coverage. Such inputs retain the wall-clock duration fallback.
    """
    expected = _finite(requested_seconds)
    times = [_finite(value) for value in progress_times]
    if expected is None or expected <= 0 or len(times) < 2 or None in times:
        return None
    if times[0] < 0 or times[0] > min(5, expected * 0.2):
        return None
    if times[-1] > expected + max(2, expected * 0.1):
        return None
    for previous, current in zip(times, times[1:]):
        if current < previous or current - previous > max(5, expected * 0.25):
            return None
    span = times[-1] - times[0]
    return span if span > 0 else None


def probe_exit_failed(data):
    code = data.get('ffmpeg_exit_code')
    return data.get('ffmpeg_error_detected') is True or (
        code is not None and _finite(code) != 0
    )


def probe_window_completion(data, minimum_percent=None):
    """Return (complete, basis); missing legacy timing keeps its old behavior."""
    if probe_exit_failed(data):
        return False, 'ffmpeg_error'
    if minimum_percent is None:
        minimum_percent = data.get('minimum_probe_completion_percent', 80)
    percent = normalize_minimum_probe_completion_percent(minimum_percent)
    if percent == 0:
        return True, 'duration_check_disabled'

    expected_raw = data.get('ffmpeg_duration', data.get('ffmpeg_duration_seconds'))
    elapsed_raw = data.get('elapsed_time')
    if expected_raw is None or elapsed_raw is None:
        return True, 'legacy_without_timing'
    expected, elapsed = _finite(expected_raw), _finite(elapsed_raw)
    if expected is None or elapsed is None or elapsed < 0:
        return False, 'invalid_timing'
    if expected <= 0:
        return True, 'legacy_without_timing'
    required = expected * percent / 100
    if elapsed >= required:
        return True, 'wall_clock'

    media = _finite(data.get('probe_media_seconds'))
    # Media-time acceptance requires an explicitly successful FFmpeg process
    # plus usable measurements; metadata from failed/partial probes cannot win.
    width, _, height = str(data.get('resolution') or '').partition('x')
    metrics = [_finite(value) for value in (
        width, height, data.get('fps'), data.get('bitrate_kbps'),
    )]
    if (data.get('ffmpeg_exit_code') == 0 and media is not None
            and required <= media <= expected + max(2, expected * 0.1)
            and all(value is not None and value > 0 for value in metrics)):
        return True, 'media_time'
    return False, 'incomplete_window'
