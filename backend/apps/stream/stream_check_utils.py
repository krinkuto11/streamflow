#!/usr/bin/env python3
"""
Stream Quality Checking Utility for StreamFlow.

This module provides focused stream checking functionality using ffmpeg/ffprobe
to analyze IPTV streams. It extracts essential quality metrics:
- Resolution (width x height)
- Bitrate (kbps)
- FPS (frames per second)
- Audio codec
- Video codec

The module is designed to work with the UDI (Universal Data Index) storage
system and provides a clean, maintainable API for stream quality analysis.

Bitrate detection
-----------------
Bitrate is read exclusively from ffmpeg's built-in progress/stats line:

    frame= 1234 fps= 30 q=28.0 size=2048kB time=00:00:41.33 bitrate= 406.1kbits/s speed=1.02x

This mirrors Dispatcharr's stream_manager._parse_ffmpeg_stats() exactly.
The stats line is the most accurate source available — ffmpeg derives it from
decoded frame timing, not raw byte counts.

ffmpeg flags used
-----------------
    -v info     Keep Input#/Stream# lines (codec, resolution, fps) while
                suppressing the debug flood that -v debug produces.
                -v warning and below suppress Stream# lines entirely.
    -stats      Force the progress/stats line to be written to stderr even
                when stderr is a pipe (not a TTY).  Without this flag the
                stats line is suppressed when running as a subprocess.
    -re         Read input at its native frame rate (real-time pacing).

With a sufficiently long analysis window (>=30 s, recommended 120 s) the
running bitrate average stabilises into a reliable reading.  If no valid
stats line is produced the stream is considered unanalyzable — a missing
bitrate is a meaningful signal that the stream is dead or unresponsive.
"""

import io
import json
import logging
import os
import re
import shlex
import subprocess
import threading
import time
from datetime import datetime
from typing import Dict, Optional, Tuple, Any

from PIL import Image
from apps.core.logging_config import setup_logging

logger = setup_logging(__name__)

# ── Loop probe constants ──────────────────────────────────────────────────────
# Hamming tolerance for one-shot probes is tighter than the live sidecar value
# (5) — one-shot probes see limited frame diversity so a stricter threshold
# reduces false positives without meaningful impact on recall.
# To catch loops up to 3 minutes, probe for 6 minutes (2 full cycles needed).
_LOOP_PROBE_HAMMING_TOLERANCE = 3
_LOOP_PROBE_DURATION          = 360   # 6 minutes — catches loops up to 3 min period
_LOOP_PROBE_DURATION_MIN      = 60    # enforced floor
_LOOP_PROBE_DURATION_MAX      = 720   # 12 minutes — ceiling for future flexibility

# Constants for error detection and logging
EARLY_EXIT_THRESHOLD = 0.8  # Consider ffmpeg exited early if elapsed < 80% of expected duration
MAX_ERROR_LINES_TO_LOG = 5  # Maximum number of error lines to log from ffmpeg output
MAX_DEBUG_LINES_TO_LOG = 10  # Maximum number of debug lines to log from ffmpeg output

# Minimum plausible bitrate in kbps.
# ffmpeg reports 0.0 kbits/s on the very first stats line before any data
# has flowed.  Values at or below this threshold are startup artifacts and
# are ignored so they cannot be mistaken for a real (very low) bitrate.
MIN_VALID_PROGRESS_BITRATE = 10.0  # kbps

# FourCC to common codec name mapping
FOURCC_TO_CODEC = {
    'avc1': 'h264',
    'avc3': 'h264',
    'h264': 'h264',
    'hvc1': 'hevc',
    'hev1': 'hevc',
    'hevc': 'hevc',
    'vp09': 'vp9',
    'vp08': 'vp8',
    'mp4a': 'aac',  # AAC audio in MP4 container
}


def _get_ffmpeg_extra_args() -> list:
    """
    Return extra ffmpeg args from the environment, if provided.
    """
    extra_args = os.getenv('FFMPEG_EXTRA_ARGS', '').strip()
    if not extra_args:
        return []
    try:
        return shlex.split(extra_args)
    except ValueError as exc:
        logger.warning(f"Invalid FFMPEG_EXTRA_ARGS (ignored): {exc}")
        return []


def _log_ffmpeg_errors(output: str, logger: logging.Logger, error_patterns: list) -> None:
    """
    Helper function to log ffmpeg errors with DEBUG_MODE awareness.

    In DEBUG_MODE, logs verbose error details.
    In production, only logs that errors occurred with a count.

    Args:
        output: FFmpeg stderr output to parse
        logger: Logger instance to use
        error_patterns: List of error patterns to search for
    """
    error_lines = []
    for line in output.splitlines():
        line_lower = line.lower()
        if any(pattern.lower() in line_lower for pattern in error_patterns):
            error_lines.append(line.strip())

    if error_lines:
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(f"  ffmpeg error details (DEBUG_MODE):")
            for error_line in error_lines[:MAX_DEBUG_LINES_TO_LOG]:
                logger.debug(f"     {error_line}")
        else:
            logger.warning(f"  ffmpeg encountered {len(error_lines)} error(s) (set DEBUG_MODE=true for details)")
    elif logger.isEnabledFor(logging.DEBUG):
        logger.debug(f"  Last lines of ffmpeg output (DEBUG_MODE):")
        for line in output.splitlines()[-MAX_DEBUG_LINES_TO_LOG:]:
            if line.strip():
                logger.debug(f"     {line.strip()}")


def _extract_codec_from_line(line: str, codec_type: str) -> Optional[str]:
    """
    Extract codec from FFmpeg output line with robust handling of wrapped codecs.

    Examples:
        "Video: wrapped_avframe (avc1 / 0x31637661), yuv420p" -> "avc1"
        "Video: h264, yuv420p, 1920x1080" -> "h264"
        "Audio: wrapped_avframe (aac)" -> "aac"
        "Audio: aac, 48000 Hz, stereo" -> "aac"

    Args:
        line: FFmpeg output line containing stream information
        codec_type: Either 'Video' or 'Audio'

    Returns:
        Extracted codec name, or None if parsing fails
    """
    pattern = rf'{codec_type}:\s*([a-zA-Z0-9_-]+)'
    codec_match = re.search(pattern, line)

    if not codec_match:
        return None

    codec = codec_match.group(1).strip()
    logger.debug(f"  Initial codec extraction: '{codec}'")

    wrapper_codecs = {'wrapped_avframe', 'unknown', 'none', 'null'}

    if codec.lower() in wrapper_codecs:
        logger.debug(f"  Detected wrapper codec '{codec}', looking for actual codec in parentheses")

        paren_pattern = rf'{re.escape(codec)}\s*\(([^)]+)\)'
        paren_match = re.search(paren_pattern, line, re.IGNORECASE)

        if paren_match:
            paren_content = paren_match.group(1).strip()
            logger.debug(f"  Found parentheses content: '{paren_content}'")

            tokens = re.split(r'[/,\s]+', paren_content)
            for token in tokens:
                token = token.strip()
                if token and not token.startswith('0x') and re.match(r'^[a-zA-Z0-9_-]+$', token):
                    logger.debug(f"  Extracted actual codec from parentheses: '{token}'")
                    return token

            logger.debug(f"  No valid codec found in parentheses")
            return None
        else:
            logger.debug(f"  No parentheses found after wrapper codec")
            return None

    if codec.isdigit():
        logger.debug(f"  Codec '{codec}' is purely numeric, likely a channel count. Returning None.")
        return None

    return codec


def _sanitize_codec_name(codec: str) -> str:
    """
    Sanitize codec name to filter out invalid or placeholder codec names.
    Also normalizes FourCC codes to common codec names (e.g., avc1 -> h264).

    Args:
        codec: The raw codec name extracted from ffmpeg output

    Returns:
        Sanitized codec name, or 'N/A' if invalid
    """
    if not codec:
        return 'N/A'

    invalid_codecs = {
        'wrapped_avframe',
        'none',
        'unknown',
        'null',
    }

    codec_lower = codec.lower()
    if codec_lower in invalid_codecs:
        logger.debug(f"  Filtered out invalid codec: {codec}")
        return 'N/A'

    normalized = FOURCC_TO_CODEC.get(codec_lower, codec)
    if normalized != codec:
        logger.debug(f"  Normalized codec {codec} -> {normalized}")

    return normalized


def _get_hdr_metadata(url: str, timeout: int = 10, user_agent: str = 'VLC/3.0.14') -> Optional[Dict[str, str]]:
    """
    Get HDR metadata from a stream using ffprobe with JSON output.

    Args:
        url: Stream URL to analyze
        timeout: Timeout in seconds for the ffprobe operation
        user_agent: User agent string to use for HTTP requests

    Returns:
        Dictionary with HDR metadata fields, or None if extraction fails
    """
    logger.debug(f"Extracting HDR metadata with ffprobe for: {url[:50]}...")
    command = [
        'ffprobe',
        '-user_agent', user_agent,
        '-v', 'quiet',
        '-show_entries', 'stream=color_space,color_primaries,color_transfer,pix_fmt,profile',
        '-of', 'json',
        url
    ]

    try:
        result = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            text=True
        )

        if result.stdout:
            data = json.loads(result.stdout)
            streams = data.get('streams', [])

            for stream in streams:
                if stream.get('pix_fmt') or stream.get('profile'):
                    logger.debug(f"  Found video stream with metadata: {stream}")
                    return {
                        'color_transfer': stream.get('color_transfer'),
                        'color_primaries': stream.get('color_primaries'),
                        'color_space': stream.get('color_space'),
                        'pix_fmt': stream.get('pix_fmt'),
                        'profile': stream.get('profile')
                    }

            logger.debug("  No video stream with HDR metadata found")
            return None

        logger.debug("ffprobe returned empty output")
        return None

    except subprocess.TimeoutExpired:
        logger.debug(f"Timeout ({timeout}s) while fetching HDR metadata")
        return None
    except json.JSONDecodeError as e:
        logger.debug(f"Failed to decode JSON from ffprobe: {e}")
        return None
    except Exception as e:
        logger.debug(f"HDR metadata extraction failed: {e}")
        return None


def _detect_hdr_format(metadata: Optional[Dict[str, str]]) -> Optional[str]:
    """
    Detect HDR format from color metadata.

    Args:
        metadata: Dictionary with color metadata from ffprobe

    Returns:
        HDR format string ('HDR10', 'HLG', 'Dolby Vision') or None for SDR
    """
    if not metadata:
        return None

    color_transfer = (metadata.get('color_transfer') or '').lower()
    color_primaries = (metadata.get('color_primaries') or '').lower()
    pix_fmt = (metadata.get('pix_fmt') or '').lower()
    profile = (metadata.get('profile') or '').lower()

    is_10bit_or_higher = '10' in pix_fmt or '12' in pix_fmt or '16' in pix_fmt
    is_bt2020 = 'bt2020' in color_primaries

    if 'dv' in profile or 'dolby' in profile:
        logger.debug(f"  Detected Dolby Vision (profile: {metadata.get('profile')})")
        return 'Dolby Vision'

    if is_bt2020 and is_10bit_or_higher and 'smpte2084' in color_transfer:
        logger.debug(f"  Detected HDR10 (transfer: {metadata.get('color_transfer')})")
        return 'HDR10'

    if is_bt2020 and is_10bit_or_higher and 'arib-std-b67' in color_transfer:
        logger.debug(f"  Detected HLG (transfer: {metadata.get('color_transfer')})")
        return 'HLG'

    logger.debug(f"  No HDR format detected (transfer: {color_transfer}, primaries: {color_primaries}, pix_fmt: {pix_fmt})")
    return None


def _snap_to_common_fps(fps: float) -> float:
    """
    Snap a given FPS reading to the closest common TV/broadcast framerate.
    """
    if not fps or fps <= 0:
        return 0
    common_values = [23.976, 24, 25, 29.97, 30, 50, 59.94, 60]
    return min(common_values, key=lambda x: abs(x - fps))


def check_ffmpeg_installed() -> bool:
    """
    Check if ffmpeg and ffprobe are installed and available.

    Returns:
        bool: True if both tools are available, False otherwise
    """
    try:
        subprocess.run(['ffmpeg', '-h'], capture_output=True, check=True, text=True)
        subprocess.run(['ffprobe', '-h'], capture_output=True, check=True, text=True)
        return True
    except FileNotFoundError:
        logger.error("ffmpeg or ffprobe not found. Please install them and ensure they are in your system's PATH.")
        return False
    except subprocess.CalledProcessError as e:
        logger.error(f"Error checking ffmpeg/ffprobe installation: {e}")
        return False


def get_stream_info(url: str, timeout: int = 30, user_agent: str = 'VLC/3.0.14') -> Tuple[Optional[Dict], Optional[Dict]]:
    """
    DEPRECATED: Use get_stream_info_and_bitrate() instead for better performance.

    Get stream information using ffprobe to extract codec, resolution, and FPS.
    """
    logger.debug(f"Running ffprobe for URL: {url[:50]}...")
    command = [
        'ffprobe',
        '-user_agent', user_agent,
        '-v', 'error',
        '-show_entries', 'stream=codec_name,width,height,avg_frame_rate',
        '-of', 'json',
        url
    ]

    try:
        result = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            text=True
        )

        if result.stdout:
            data = json.loads(result.stdout)
            streams = data.get('streams', [])
            logger.debug(f"ffprobe returned {len(streams)} streams")

            video_info = next((s for s in streams if 'width' in s), None)
            audio_info = next((s for s in streams if 'codec_name' in s and 'width' not in s), None)

            return video_info, audio_info

        logger.debug("ffprobe returned empty output")
        return None, None

    except subprocess.TimeoutExpired:
        logger.warning(f"Timeout ({timeout}s) while fetching stream info for: {url[:50]}...")
        return None, None
    except json.JSONDecodeError as e:
        logger.warning(f"Failed to decode JSON from ffprobe for {url[:50]}...: {e}")
        return None, None
    except Exception as e:
        logger.error(f"Stream info check failed for {url[:50]}...: {e}")
        return None, None


def get_stream_info_and_bitrate(url: str, duration: int = 30, timeout: int = 30, user_agent: str = 'VLC/3.0.14', stream_startup_buffer: int = 10) -> Dict[str, Any]:
    """
    Get complete stream information using ffmpeg in a single call.

    Uses ffmpeg flags:
        -v info   : preserves Input#/Stream# lines for codec+resolution parsing
        -stats    : forces the progress/stats line to stderr through a pipe
        -re       : reads at native frame rate (real-time pacing)

    Bitrate is taken exclusively from ffmpeg's progress/stats line, exactly
    mirroring Dispatcharr's stream_manager._parse_ffmpeg_stats().

    Args:
        url: Stream URL to analyze (will be validated and sanitized)
        duration: Duration in seconds to analyze the stream
        timeout: Base timeout in seconds (actual timeout includes duration + overhead)
        user_agent: User agent string to use for HTTP requests
        stream_startup_buffer: Buffer in seconds for stream startup (default: 10s)

    Returns:
        Dictionary containing:
        - video_codec, audio_codec, resolution, fps, bitrate_kbps
        - hdr_format, pixel_format, audio_sample_rate, audio_channels
        - channel_layout, audio_bitrate, status, elapsed_time
    """
    # Validate and sanitize URL to prevent command injection
    if not url or not isinstance(url, str):
        logger.error("Invalid URL: must be a non-empty string")
        return {
            'video_codec': 'N/A', 'audio_codec': 'N/A', 'resolution': '0x0',
            'fps': 0, 'bitrate_kbps': None, 'hdr_format': None,
            'pixel_format': None, 'audio_sample_rate': None,
            'audio_channels': None, 'channel_layout': None,
            'audio_bitrate': None, 'status': 'Error', 'elapsed_time': 0
        }

    url_lower = url.lower()
    if not (url_lower.startswith('http://') or url_lower.startswith('https://') or
            url_lower.startswith('rtmp://') or url_lower.startswith('rtmps://')):
        logger.error(f"Invalid URL protocol: {url[:50]}... (must be http://, https://, rtmp://, or rtmps://)")
        return {
            'video_codec': 'N/A', 'audio_codec': 'N/A', 'resolution': '0x0',
            'fps': 0, 'bitrate_kbps': None, 'hdr_format': None,
            'pixel_format': None, 'audio_sample_rate': None,
            'audio_channels': None, 'channel_layout': None,
            'audio_bitrate': None, 'status': 'Error', 'elapsed_time': 0
        }

    logger.debug(f"Analyzing stream with ffmpeg for {duration}s: {url[:50]}...")

    extra_args = _get_ffmpeg_extra_args()

    # Flag rationale:
    #   -re      : read at native frame rate so bitrate reflects real playback
    #   -v info  : minimum verbosity that still emits Input#/Stream# lines
    #              (-v warning and below suppress them entirely)
    #   -stats   : force the "frame= fps= bitrate= speed=" progress line to
    #              stderr even when stderr is a pipe, not a TTY
    # Remux to MPEG-TS on stdout rather than discarding with -f null.
    # This gives ffmpeg a real continuous output pipeline — identical to
    # how Dispatcharr's proxy operates — so the frame=/fps=/bitrate=/speed=
    # stats line fires reliably for all stream types including HLS.
    #
    # With -f null, ffmpeg discards output internally and loses the timing
    # baseline needed to calculate bitrate across HLS segment boundaries.
    # Writing real MPEG-TS to stdout (pipe:1) and draining it via
    # subprocess.DEVNULL means ffmpeg measures a genuine real-time output
    # pipeline — the same condition Dispatcharr's proxy creates naturally.
    #
    # -c copy: remux only, no decode/encode — negligible CPU overhead.
    command = [
        'ffmpeg', '-re', '-v', 'info', '-stats',
        '-user_agent', user_agent,
    ] + extra_args + [
        '-i', url, '-t', str(duration),
        '-c', 'copy', '-f', 'mpegts', 'pipe:1'
    ]

    result_data = {
        'video_codec': 'N/A', 'audio_codec': 'N/A', 'resolution': '0x0',
        'fps': 0, 'bitrate_kbps': None, 'hdr_format': None,
        'pixel_format': None, 'audio_sample_rate': None,
        'audio_channels': None, 'channel_layout': None,
        'audio_bitrate': None, 'status': 'OK', 'elapsed_time': 0
    }

    # Total subprocess timeout: analysis window + startup headroom
    actual_timeout = timeout + duration + stream_startup_buffer

    try:
        start = time.time()
        result = subprocess.run(
            command,
            stdout=subprocess.DEVNULL,  # OS drains stdout so ffmpeg never blocks
            stderr=subprocess.PIPE,
            timeout=actual_timeout,
            text=True
        )
        elapsed = time.time() - start
        result_data['elapsed_time'] = elapsed

        output = result.stderr
        progress_bitrate = None
        last_stats_line = None  # keeps updating; final line is the most stable average

        # Track Input vs Output sections so we only parse input stream codecs,
        # not decoded output formats (e.g. pcm_s16le instead of aac)
        in_input_section = False

        for line in output.splitlines():

            # ── Section tracking ─────────────────────────────────────────────
            if 'Input #' in line:
                in_input_section = True
                logger.debug(f"  Entered Input section: {line.strip()}")
                continue

            if 'Output #' in line:
                in_input_section = False
                logger.debug(f"  Entered Output section (skipping stream parsing): {line.strip()}")
                continue

            # ── Video stream info (codec, resolution, header FPS, HDR) ───────
            if in_input_section and 'Stream #' in line and 'Video:' in line:
                try:
                    video_codec = _extract_codec_from_line(line, 'Video')
                    if video_codec:
                        video_codec = _sanitize_codec_name(video_codec)
                        if video_codec != 'N/A':
                            result_data['video_codec'] = video_codec
                            logger.debug(f"  Video codec: {result_data['video_codec']}")

                    res_match = re.search(r'(\d{2,5})x(\d{2,5})', line)
                    if res_match:
                        width, height = res_match.groups()
                        result_data['resolution'] = f"{width}x{height}"
                        logger.debug(f"  Resolution: {result_data['resolution']}")

                    fps_match = re.search(r'(\d+\.?\d*)\s*fps', line)
                    if fps_match:
                        result_data['fps'] = round(float(fps_match.group(1)), 0)
                        logger.debug(f"  Header FPS: {result_data['fps']}")

                    if not result_data['hdr_format']:
                        color_match = re.search(
                            r'(yuv\w+?p?\d{1,2}le)[^,]*,\s*([^,)]+)',
                            line, re.IGNORECASE
                        )
                        if color_match:
                            pix_fmt = color_match.group(1).lower()
                            color_info = color_match.group(2).lower()

                            pix_fmt_match = re.search(
                                r'Video:\s*[a-zA-Z0-9_-]+\s*(?:\([^)]+\))?\s*,\s*([a-zA-Z0-9_-]+)', line
                            )
                            if pix_fmt_match:
                                result_data['pixel_format'] = pix_fmt_match.group(1).lower()
                                logger.debug(f"  Pixel format: {result_data['pixel_format']}")

                            color_parts = color_info.replace('tv,', '').strip().split('/')
                            color_primaries = None
                            color_transfer = None
                            for part in color_parts:
                                part = part.strip()
                                if 'bt2020' in part and not color_primaries:
                                    color_primaries = 'bt2020'
                                elif 'smpte2084' in part or 'arib-std-b67' in part:
                                    color_transfer = part

                            is_10bit = '10' in pix_fmt or '12' in pix_fmt or '16' in pix_fmt
                            if color_primaries == 'bt2020' and is_10bit:
                                if color_transfer and 'smpte2084' in color_transfer:
                                    result_data['hdr_format'] = 'HDR10'
                                    logger.debug(f"  HDR format: HDR10")
                                elif color_transfer and 'arib-std-b67' in color_transfer:
                                    result_data['hdr_format'] = 'HLG'
                                    logger.debug(f"  HDR format: HLG")
                except (ValueError, AttributeError) as e:
                    logger.debug(f"  Error parsing video stream line: {e}")

            # ── Audio stream info ─────────────────────────────────────────────
            if in_input_section and 'Stream #' in line and 'Audio:' in line:
                try:
                    audio_codec = _extract_codec_from_line(line, 'Audio')
                    if audio_codec:
                        audio_codec = _sanitize_codec_name(audio_codec)
                        if audio_codec != 'N/A':
                            result_data['audio_codec'] = audio_codec
                            logger.debug(f"  Audio codec: {result_data['audio_codec']}")

                    sample_rate_match = re.search(r'(\d+)\s*Hz', line)
                    if sample_rate_match:
                        result_data['audio_sample_rate'] = int(sample_rate_match.group(1))
                        logger.debug(f"  Audio sample rate: {result_data['audio_sample_rate']} Hz")

                    channel_match = re.search(r'Hz,\s*([a-zA-Z0-9_.()]+)', line)
                    if channel_match:
                        layout = channel_match.group(1).lower()
                        result_data['channel_layout'] = layout
                        logger.debug(f"  Audio channel layout: {layout}")

                        layout_to_channels = {
                            'mono': 1, 'stereo': 2, '2.0': 2, '1.0': 1,
                            '5.1': 6, '5.1(side)': 6, '7.1': 8,
                            '7.1(wide)': 8, 'quad': 4, '2.1': 3, '4.0': 4
                        }
                        result_data['audio_channels'] = layout_to_channels.get(layout)
                        if result_data['audio_channels']:
                            logger.debug(f"  Audio channels: {result_data['audio_channels']}")

                    audio_bitrate_match = re.search(r'(\d+)\s*kb/s', line)
                    if audio_bitrate_match:
                        result_data['audio_bitrate'] = int(audio_bitrate_match.group(1))
                        logger.debug(f"  Audio bitrate: {result_data['audio_bitrate']} kbps")
                except (ValueError, AttributeError) as e:
                    logger.debug(f"  Error parsing audio stream line: {e}")

            # ── Bitrate + actual FPS from ffmpeg progress/stats line ──────────
            #
            # Sole bitrate source — mirrors Dispatcharr stream_manager exactly.
            # ffmpeg emits this line roughly every 0.5 s during analysis:
            #
            #   frame= 1234 fps= 30 q=28.0 size=2048kB time=00:00:41.33 bitrate= 406.1kbits/s speed=1.02x
            #
            # Unit conversion (Dispatcharr parity):
            #   kbits/s  -> already kbps         (most streams)
            #   Mbits/s  -> x 1000               (4K / high-bitrate streams)
            #   Gbits/s  -> x 1 000 000          (theoretical)
            #   bits/s   -> / 1000               (very low-bitrate streams)
            #
            # Actual source FPS = ffmpeg_fps / speed.
            # The stats line fps= is the encode FPS; dividing by playback speed
            # recovers the true source framerate (Dispatcharr parity).
            if 'bitrate=' in line and 'bits/s' in line:

                # Log every stats line at DEBUG — visible only when debugging
                logger.debug(f"  [ffmpeg stats] {line.strip()}")
                last_stats_line = line.strip()

                try:
                    bitrate_match = re.search(
                        r'bitrate=\s*([0-9]+(?:\.[0-9]+)?)\s*([kmg]?)bits/s',
                        line, re.IGNORECASE
                    )
                    if bitrate_match:
                        bitrate_value = float(bitrate_match.group(1))
                        unit = bitrate_match.group(2).lower()
                        if unit == 'm':
                            bitrate_value *= 1000        # Mbits/s -> kbps
                        elif unit == 'g':
                            bitrate_value *= 1_000_000   # Gbits/s -> kbps
                        elif unit == '':
                            bitrate_value /= 1000        # bits/s  -> kbps
                        # 'k' or no prefix: already kbps
                        if bitrate_value > MIN_VALID_PROGRESS_BITRATE:
                            progress_bitrate = bitrate_value
                            logger.debug(f"  Parsed bitrate: {progress_bitrate:.2f} kbps")

                    # Actual source FPS from same line (Dispatcharr parity)
                    fps_stats_match = re.search(r'fps=\s*([0-9.]+)', line)
                    speed_match = re.search(r'speed=\s*([0-9.]+)x?', line)
                    if fps_stats_match and speed_match:
                        ffmpeg_fps = float(fps_stats_match.group(1))
                        ffmpeg_speed = float(speed_match.group(1))
                        if ffmpeg_speed > 0:
                            actual_fps = _snap_to_common_fps(ffmpeg_fps / ffmpeg_speed)
                            if actual_fps > 0:
                                result_data['fps'] = actual_fps

                except (ValueError, AttributeError):
                    pass

        # ── Post-loop: commit final (most stable) bitrate reading ─────────────
        if progress_bitrate is not None:
            result_data['bitrate_kbps'] = progress_bitrate
            if last_stats_line:
                logger.debug(
                    f"  [ffmpeg final] {last_stats_line} "
                    f"-> bitrate={progress_bitrate:.2f} kbps  fps={result_data['fps']}"
                )

        # ── Warn clearly when no stats line was produced ──────────────────────
        expected_min_time = duration * EARLY_EXIT_THRESHOLD
        exited_early = elapsed < expected_min_time

        if result_data['bitrate_kbps'] is None:
            logger.warning(
                f"  [!] No bitrate detected — ffmpeg produced no valid stats lines "
                f"(elapsed={elapsed:.2f}s, expected ~{duration}s). "
                f"Stream is likely dead or unresponsive."
            )
            if exited_early or result.returncode != 0:
                if result.returncode != 0:
                    logger.warning(f"  [!] ffmpeg exited with code {result.returncode}")
                else:
                    logger.warning(f"  [!] ffmpeg completed in {elapsed:.2f}s (expected ~{duration}s)")

                error_patterns = [
                    "Connection refused", "Connection timed out", "Invalid data found",
                    "Server returned", "404 Not Found", "403 Forbidden", "401 Unauthorized",
                    "No route to host", "could not find codec", "Protocol not found",
                    "Error opening input", "Operation timed out", "I/O error",
                    "HTTP error", "SSL", "TLS", "Certificate"
                ]
                _log_ffmpeg_errors(output, logger, error_patterns)

        logger.debug(f"  Analysis completed in {elapsed:.2f}s")

    except subprocess.TimeoutExpired:
        logger.warning(f"Timeout ({actual_timeout}s) while analyzing stream")
        result_data['status'] = "Timeout"
        result_data['elapsed_time'] = actual_timeout
    except Exception as e:
        logger.error(f"Stream analysis failed: {e}")
        result_data['status'] = "Error"
        result_data['elapsed_time'] = 0

    return result_data


def get_stream_bitrate(url: str, duration: int = 30, timeout: int = 30, user_agent: str = 'VLC/3.0.14', stream_startup_buffer: int = 10) -> Tuple[Optional[float], str, float]:
    """
    Get stream bitrate using ffmpeg.

    Bitrate is read exclusively from ffmpeg's progress/stats line, mirroring
    Dispatcharr's stream_manager._parse_ffmpeg_stats() approach.

    Args:
        url: Stream URL to analyze
        duration: Duration in seconds to analyze the stream
        timeout: Base timeout in seconds (actual timeout includes duration + overhead)
        user_agent: User agent string to use for HTTP requests
        stream_startup_buffer: Buffer in seconds for stream startup (default: 10s)

    Returns:
        Tuple of (bitrate_kbps, status, elapsed_time)
        bitrate_kbps: Bitrate in kilobits per second, or None if stream is dead
        status: "OK", "Timeout", or "Error"
        elapsed_time: Time taken for the operation
    """
    logger.debug(f"Analyzing bitrate for {duration}s...")
    extra_args = _get_ffmpeg_extra_args()
    command = [
        'ffmpeg', '-re', '-v', 'info', '-stats',
        '-user_agent', user_agent,
    ] + extra_args + [
        '-i', url, '-t', str(duration),
        '-c', 'copy', '-f', 'mpegts', 'pipe:1'
    ]

    bitrate = None
    status = "OK"
    actual_timeout = timeout + duration + stream_startup_buffer

    try:
        start = time.time()
        result = subprocess.run(
            command,
            stdout=subprocess.DEVNULL,  # OS drains stdout so ffmpeg never blocks
            stderr=subprocess.PIPE,
            timeout=actual_timeout,
            text=True
        )
        elapsed = time.time() - start
        output = result.stderr
        progress_bitrate = None
        last_stats_line = None

        for line in output.splitlines():
            if 'bitrate=' in line and 'bits/s' in line:
                logger.debug(f"  [ffmpeg stats] {line.strip()}")
                last_stats_line = line.strip()
                try:
                    bitrate_match = re.search(
                        r'bitrate=\s*([0-9]+(?:\.[0-9]+)?)\s*([kmg]?)bits/s',
                        line, re.IGNORECASE
                    )
                    if bitrate_match:
                        bitrate_value = float(bitrate_match.group(1))
                        unit = bitrate_match.group(2).lower()
                        if unit == 'm':
                            bitrate_value *= 1000
                        elif unit == 'g':
                            bitrate_value *= 1_000_000
                        elif unit == '':
                            bitrate_value /= 1000
                        if bitrate_value > MIN_VALID_PROGRESS_BITRATE:
                            progress_bitrate = bitrate_value
                except (ValueError, AttributeError):
                    pass

        if progress_bitrate is not None:
            bitrate = progress_bitrate
            if last_stats_line:
                logger.debug(
                    f"  [ffmpeg final] {last_stats_line} "
                    f"-> bitrate={bitrate:.2f} kbps"
                )

        expected_min_time = duration * EARLY_EXIT_THRESHOLD
        exited_early = elapsed < expected_min_time

        if bitrate is None:
            logger.warning(
                f"  [!] No bitrate detected — ffmpeg produced no valid stats lines "
                f"(elapsed={elapsed:.2f}s, expected ~{duration}s). "
                f"Stream is likely dead or unresponsive."
            )
            logger.debug(f"  Searched {len(output.splitlines())} lines of output")
            if exited_early or result.returncode != 0:
                if result.returncode != 0:
                    logger.warning(f"  [!] ffmpeg exited with code {result.returncode}")
                else:
                    logger.warning(f"  [!] ffmpeg completed in {elapsed:.2f}s (expected ~{duration}s)")

                error_patterns = [
                    "Connection refused", "Connection timed out", "Invalid data found",
                    "Server returned", "404 Not Found", "403 Forbidden", "401 Unauthorized",
                    "No route to host", "could not find codec", "Protocol not found",
                    "Error opening input", "Operation timed out", "I/O error",
                    "HTTP error", "SSL", "TLS", "Certificate"
                ]
                _log_ffmpeg_errors(output, logger, error_patterns)

        logger.debug(f"  Analysis completed in {elapsed:.2f}s")

    except subprocess.TimeoutExpired:
        logger.warning(f"Timeout ({actual_timeout}s) while fetching bitrate")
        status = "Timeout"
        elapsed = actual_timeout
    except Exception as e:
        logger.error(f"Bitrate check failed: {e}")
        status = "Error"
        elapsed = 0

    return bitrate, status, elapsed



def _probe_stream_for_loops(
    url: str,
    stream_tag: str,
    probe_duration: int = _LOOP_PROBE_DURATION,
    user_agent: str = 'VLC/3.0.14',
) -> 'tuple[bool, float | None, int]':
    """
    Probe a stream for looping content using a single lightweight FFmpeg process.

    Architecture — one provider connection, one FFmpeg process, one output:

        FFmpeg -i <url> -t <probe_duration>
            └── stdout (subprocess.PIPE)   32x32 grayscale PPM frames
                                           consumed by SidecarLoopDetector
                                           in a daemon thread

    Runs sequentially after quality analysis — the quality analysis connection
    has already been closed before this is called.

    Args:
        url:            Stream URL to probe.
        stream_tag:     Short identifier for log messages (hostname/path).
        probe_duration: Seconds to run. Clamped to
                        [_LOOP_PROBE_DURATION_MIN, _LOOP_PROBE_DURATION_MAX].
        user_agent:     HTTP User-Agent forwarded to FFmpeg.

    Returns:
        (loop_detected, loop_duration_secs, frames_processed)
        loop_duration_secs is None when loop_detected is False.
        frames_processed is 0 when the probe produced no usable frames.
    """
    try:
        from apps.stream.sidecar_loop_detector import SidecarLoopDetector
    except ImportError:
        logger.error(f"[loop-probe:{stream_tag}] SidecarLoopDetector unavailable")
        return False, None, 0

    try:
        import imagehash as _imagehash
    except ImportError:
        _imagehash = None

    clamped = max(_LOOP_PROBE_DURATION_MIN, min(_LOOP_PROBE_DURATION_MAX, probe_duration))

    logger.info(
        f"[loop-probe:{stream_tag}] Starting {clamped}s probe "
        f"(hamming_tolerance={_LOOP_PROBE_HAMMING_TOLERANCE}, "
        f"sequence_length=3, duration_threshold=10.0s)"
    )

    cmd = [
        'ffmpeg',
        '-hide_banner',
        '-nostdin',
        '-loglevel', 'warning',
        '-user_agent', user_agent,
        '-i', url,
        '-t', str(clamped),
        '-map', '0:v:0',
        '-an', '-sn',
        '-vf', 'scale=32:32:flags=fast_bilinear,format=gray',
        '-c:v', 'ppm',
        '-f', 'image2pipe',
        'pipe:1',
    ]

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            bufsize=0,
        )
    except Exception as exc:
        logger.error(f"[loop-probe:{stream_tag}] FFmpeg failed to start: {exc}")
        return False, None, 0

    detector      = SidecarLoopDetector(proc.stdout, stream_id=None)
    frames_done   = [0]
    loop_detected = False
    loop_duration = None

    def _reader():
        nonlocal loop_detected, loop_duration
        logger.debug(f"[loop-probe:{stream_tag}] Reader thread started")
        try:
            while not detector.is_closed:
                frame_data = detector._read_ppm_frame()
                if not frame_data:
                    logger.debug(f"[loop-probe:{stream_tag}] Pipe EOF — reader exiting")
                    break

                ts = time.monotonic()
                detector.last_frame_time = ts

                try:
                    img = Image.open(io.BytesIO(frame_data))
                    h   = _imagehash.phash(img) if _imagehash else detector._simple_hash(img)
                    detector.buffer.append((ts, h))
                    frames_done[0] += 1

                    logger.debug(
                        f"[loop-probe:{stream_tag}] "
                        f"frame={frames_done[0]:4d}  hash={h}  buffer={len(detector.buffer)}"
                    )

                    detected = detector.detect_loop(
                        hamming_tolerance=_LOOP_PROBE_HAMMING_TOLERANCE
                    )
                    if detected:
                        detector._is_looping   = True
                        detector._loop_duration = detected
                        logger.debug(
                            f"[loop-probe:{stream_tag}] Loop signal at "
                            f"frame {frames_done[0]}: period={detected:.1f}s"
                        )
                    else:
                        detector._is_looping   = False
                        detector._loop_duration = 0.0

                except Exception as frame_err:
                    logger.debug(f"[loop-probe:{stream_tag}] Frame error: {frame_err}")
                    continue

        except Exception as err:
            logger.debug(f"[loop-probe:{stream_tag}] Reader error: {err}")
        finally:
            detector.is_closed = True
            try:
                proc.stdout.close()
            except Exception:
                pass

        loop_detected = detector.is_looping()
        loop_duration = detector.get_loop_duration() if loop_detected else None

    reader = threading.Thread(
        target=_reader, daemon=True,
        name=f"LoopProbe-Reader[{stream_tag}]"
    )
    reader.start()

    # Wait for FFmpeg; hard-kill if it overshoots
    try:
        proc.wait(timeout=clamped + 20)
    except subprocess.TimeoutExpired:
        logger.warning(
            f"[loop-probe:{stream_tag}] FFmpeg exceeded {clamped + 20}s — killing"
        )
        proc.kill()
        proc.wait()

    detector.is_closed = True
    reader.join(timeout=10)

    n = frames_done[0]

    # Capture stderr for diagnostics on zero-frame failures
    try:
        stderr_out = proc.stderr.read().decode('utf-8', errors='replace').strip()
    except Exception:
        stderr_out = ''

    if n == 0:
        logger.warning(
            f"[loop-probe:{stream_tag}] 0 frames received — loop detection "
            f"inconclusive. Stream may not expose decodable video frames."
        )
        if stderr_out:
            logger.warning(f"[loop-probe:{stream_tag}] FFmpeg said: {stderr_out[:300]}")
    elif loop_detected:
        logger.warning(
            f"[loop-probe:{stream_tag}] LOOP DETECTED — "
            f"period~{loop_duration:.1f}s  frames={n}"
        )
    else:
        logger.info(
            f"[loop-probe:{stream_tag}] Clean — no loop detected  frames={n}"
        )

    return loop_detected, loop_duration, n


def analyze_stream(
    stream_url: str,
    stream_id: int,
    stream_name: str = "Unknown",
    ffmpeg_duration: int = 30,
    timeout: int = 30,
    retries: int = 1,
    retry_delay: int = 10,
    user_agent: str = 'VLC/3.0.14',
    stream_startup_buffer: int = 10
) -> Dict[str, Any]:
    """
    Perform complete stream analysis including codec, resolution, FPS, bitrate, and audio.

    This is the main entry point for stream checking. Uses a single ffmpeg call to extract
    all information, reducing network overhead and processing time compared to the previous
    two-step process (ffprobe + ffmpeg).

    Args:
        stream_url: URL of the stream to analyze
        stream_id: Unique identifier for the stream
        stream_name: Human-readable name for the stream
        ffmpeg_duration: Duration in seconds for analysis
        timeout: Timeout in seconds for the operation
        retries: Number of retry attempts on failure (0 = try once with no retries,
                 1 = try once then retry once if failed, 2 = try once then retry twice, etc.)
        retry_delay: Delay in seconds between retries
        user_agent: User agent string to use for HTTP requests
        stream_startup_buffer: Buffer in seconds for stream startup (default: 10s)

    Returns:
        Dictionary containing analysis results with keys:
        - stream_id, stream_name, stream_url, timestamp
        - video_codec, audio_codec, resolution, fps, bitrate_kbps
        - hdr_format, pixel_format, audio_sample_rate, audio_channels
        - channel_layout, audio_bitrate, status
    """
    if logger.isEnabledFor(logging.DEBUG):
        logger.info(f"Analyzing stream: {stream_name} (ID: {stream_id})")
    else:
        logger.debug(f"Checking {stream_name}")

    result = {
        'stream_id': stream_id,
        'stream_name': stream_name,
        'stream_url': stream_url,
        'timestamp': datetime.now().isoformat(),
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
        'elapsed_time': 0,
        'ffmpeg_duration': ffmpeg_duration,
    }

    try:
        total_attempts = retries + 1
        for attempt in range(total_attempts):
            if attempt > 0:
                if logger.isEnabledFor(logging.DEBUG):
                    logger.info(f"  Retry attempt {attempt} of {retries} (attempt {attempt + 1} of {total_attempts}) for {stream_name}")
                else:
                    logger.info(f"  Retry {attempt + 1}/{total_attempts} for {stream_name}")
                time.sleep(retry_delay)

            try:
                if logger.isEnabledFor(logging.DEBUG):
                    logger.info("  Analyzing stream (single ffmpeg call)...")

                result_data = get_stream_info_and_bitrate(
                    url=stream_url,
                    duration=ffmpeg_duration,
                    timeout=timeout,
                    user_agent=user_agent,
                    stream_startup_buffer=stream_startup_buffer
                )

                result = {
                    'stream_id': stream_id,
                    'stream_name': stream_name,
                    'stream_url': stream_url,
                    'timestamp': datetime.now().isoformat(),
                    'video_codec': result_data['video_codec'],
                    'audio_codec': result_data['audio_codec'],
                    'resolution': result_data['resolution'],
                    'fps': result_data['fps'],
                    'bitrate_kbps': result_data['bitrate_kbps'],
                    'hdr_format': result_data['hdr_format'],
                    'pixel_format': result_data['pixel_format'],
                    'audio_sample_rate': result_data['audio_sample_rate'],
                    'audio_channels': result_data['audio_channels'],
                    'channel_layout': result_data['channel_layout'],
                    'audio_bitrate': result_data['audio_bitrate'],
                    'status': result_data['status'],
                    'elapsed_time': result_data.get('elapsed_time', 0),
                    'ffmpeg_duration': ffmpeg_duration,
                }

                if logger.isEnabledFor(logging.DEBUG):
                    if result['video_codec'] != 'N/A' or result['resolution'] != '0x0':
                        logger.info(f"    Video: {result['video_codec']}, {result['resolution']}, {result['fps']} FPS")
                    else:
                        logger.warning("    No video info found")

                    if result['audio_codec'] != 'N/A':
                        logger.info(f"    Audio: {result['audio_codec']}")
                    else:
                        logger.warning("    No audio info found")

                    if result['status'] == "OK":
                        if result['bitrate_kbps'] is not None:
                            logger.info(f"    Bitrate: {result['bitrate_kbps']:.2f} kbps (elapsed: {result_data['elapsed_time']:.2f}s)")
                        else:
                            logger.warning(f"    Bitrate detection failed (elapsed: {result_data['elapsed_time']:.2f}s)")
                        logger.info(f"  Stream analysis complete for {stream_name}")
                    else:
                        logger.warning(f"    Status: {result['status']} (elapsed: {result_data['elapsed_time']:.2f}s)")
                else:
                    if result['status'] == "OK":
                        bitrate_str = f"{result['bitrate_kbps']:.2f} kbps" if result['bitrate_kbps'] is not None else "N/A"
                        hdr_str = f", {result['hdr_format']}" if result.get('hdr_format') else ""
                        logger.info(f"  {stream_name}: {result['resolution']}, {result['fps']} FPS, {bitrate_str}, {result['video_codec']}/{result['audio_codec']}{hdr_str} ({result_data['elapsed_time']:.2f}s)")
                    else:
                        logger.warning(f"  {stream_name}: Check failed - {result['status']} ({result_data['elapsed_time']:.2f}s)")

                if result['status'] == "OK":
                    # Check whether FFmpeg ran for a meaningful portion of the
                    # probe window.  A stream that exits early produced partial
                    # data that is not representative of sustained delivery.
                    # Give it another attempt rather than accepting the result.
                    elapsed  = result_data.get('elapsed_time', ffmpeg_duration)
                    completed = elapsed >= ffmpeg_duration * EARLY_EXIT_THRESHOLD
                    if completed:
                        break
                    elif attempt < total_attempts - 1:
                        if logger.isEnabledFor(logging.DEBUG):
                            logger.warning(
                                f"  Stream '{stream_name}' exited early "
                                f"({elapsed:.1f}s / {ffmpeg_duration}s expected) — "
                                f"retrying (attempt {attempt + 2}/{total_attempts})"
                            )
                        else:
                            logger.warning(
                                f"  {stream_name} exited early "
                                f"({elapsed:.1f}s/{ffmpeg_duration}s) — "
                                f"retrying (attempt {attempt + 2}/{total_attempts})"
                            )
                    # If this was the last attempt, fall through without break.
                    # is_stream_dead() will classify it as 'unstable'.
                else:
                    if attempt < total_attempts - 1:
                        if logger.isEnabledFor(logging.DEBUG):
                            logger.warning(f"  Stream '{stream_name}' failed with status '{result['status']}'. Retrying in {retry_delay} seconds...")
                        else:
                            logger.warning(f"  Retrying {stream_name} in {retry_delay}s (attempt {attempt + 2}/{total_attempts})")

            except Exception as inner_e:
                logger.error(f"  Exception during stream analysis (attempt {attempt + 1} of {total_attempts}): {inner_e}")
                if attempt < total_attempts - 1:
                    logger.warning(f"  Retrying in {retry_delay} seconds...")

    except Exception as outer_e:
        logger.error(f"Unexpected error in analyze_stream for {stream_name}: {outer_e}")

    return result
