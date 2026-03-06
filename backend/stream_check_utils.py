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
"""

import json
import logging
import os
import re
import shlex
import subprocess
import time
from datetime import datetime
from typing import Dict, Optional, Tuple, Any

from logging_config import setup_logging

logger = setup_logging(__name__)

# Constants for error detection and logging
EARLY_EXIT_THRESHOLD = 0.8  # Consider ffmpeg exited early if elapsed < 80% of expected duration
MAX_ERROR_LINES_TO_LOG = 5  # Maximum number of error lines to log from ffmpeg output
MAX_DEBUG_LINES_TO_LOG = 10  # Maximum number of debug lines to log from ffmpeg output

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
        # Only show verbose ffmpeg error details in debug mode
        # In production, just log that errors occurred without the full verbose output
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(f"  ⚠ ffmpeg error details (DEBUG_MODE):")
            for error_line in error_lines[:MAX_DEBUG_LINES_TO_LOG]:
                logger.debug(f"     {error_line}")
        else:
            # In production mode, just note that errors were found without verbose details
            logger.warning(f"  ⚠ ffmpeg encountered {len(error_lines)} error(s) (set DEBUG_MODE=true for details)")
    elif logger.isEnabledFor(logging.DEBUG):
        # Log last few lines of output for debugging - only in debug mode
        logger.debug(f"  → Last lines of ffmpeg output (DEBUG_MODE):")
        for line in output.splitlines()[-MAX_DEBUG_LINES_TO_LOG:]:
            if line.strip():
                logger.debug(f"     {line.strip()}")


def _extract_codec_from_line(line: str, codec_type: str) -> Optional[str]:
    """
    Extract codec from FFmpeg output line with robust handling of wrapped codecs.
    
    This implements Dispatcharr's robust codec extraction logic:
    1. Takes the first non-empty token after 'Video:' or 'Audio:'
    2. If the token is a generic wrapper (wrapped_avframe, unknown, etc.):
       - Looks inside the parentheses immediately following the codec
       - Extracts the first codec string inside parentheses
       - Ignores hexadecimal/fourcc codes like '0x31637661'
    3. Returns the extracted codec or None if not found
    
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
    # Step 1: Extract the first token after 'Video:' or 'Audio:'
    # This regex captures the first word (alphanumeric + underscore + hyphen) after the codec type
    # Supports codec names like 'h264', 'x264-high', 'wrapped_avframe', etc.
    pattern = rf'{codec_type}:\s*([a-zA-Z0-9_-]+)'
    codec_match = re.search(pattern, line)
    
    if not codec_match:
        return None
    
    codec = codec_match.group(1).strip()
    logger.debug(f"  → Initial codec extraction: '{codec}'")
    
    # Step 2: Check if the codec is a generic wrapper
    # These wrappers indicate we should look for the actual codec in parentheses
    wrapper_codecs = {'wrapped_avframe', 'unknown', 'none', 'null'}
    
    if codec.lower() in wrapper_codecs:
        logger.debug(f"  → Detected wrapper codec '{codec}', looking for actual codec in parentheses")
        
        # Step 3: Look for codec in parentheses immediately after the wrapper
        # Pattern: finds content within parentheses after the wrapper codec
        # Example: "wrapped_avframe (avc1 / 0x31637661)" -> captures "avc1 / 0x31637661"
        paren_pattern = rf'{re.escape(codec)}\s*\(([^)]+)\)'
        paren_match = re.search(paren_pattern, line, re.IGNORECASE)
        
        if paren_match:
            paren_content = paren_match.group(1).strip()
            logger.debug(f"  → Found parentheses content: '{paren_content}'")
            
            # Step 4: Extract the first codec token from parentheses, ignoring hex codes
            # Split by common delimiters (/, comma, space) and take first valid token
            tokens = re.split(r'[/,\s]+', paren_content)
            
            for token in tokens:
                token = token.strip()
                # Skip empty tokens and hexadecimal codes (0x...)
                # Support codec names with hyphens (e.g., x264-high)
                if token and not token.startswith('0x') and re.match(r'^[a-zA-Z0-9_-]+$', token):
                    logger.debug(f"  → Extracted actual codec from parentheses: '{token}'")
                    return token
            
            # If no valid codec found in parentheses, the wrapper is invalid
            logger.debug(f"  → No valid codec found in parentheses")
            return None
        else:
            # Wrapper codec without parentheses is invalid
            logger.debug(f"  → No parentheses found after wrapper codec")
            return None
    
    # Step 5: Return the codec as-is if it's not a wrapper
    return codec


def _sanitize_codec_name(codec: str) -> str:
    """
    Sanitize codec name to filter out invalid or placeholder codec names.
    
    ffmpeg sometimes outputs codec names like 'wrapped_avframe' when it cannot
    properly identify the codec (e.g., for hardware-accelerated streams). This
    function filters out such values and returns 'N/A' for invalid codecs.
    
    Also normalizes FourCC codes to common codec names (e.g., avc1 -> h264).
    
    Args:
        codec: The raw codec name extracted from ffmpeg output
        
    Returns:
        Sanitized codec name, or 'N/A' if invalid
    """
    if not codec:
        return 'N/A'
    
    # List of invalid/placeholder codec names to filter out
    invalid_codecs = {
        'wrapped_avframe',  # Hardware acceleration placeholder
        'none',             # No codec
        'unknown',          # Unknown codec
        'null',             # Null codec
    }
    
    codec_lower = codec.lower()
    if codec_lower in invalid_codecs:
        logger.debug(f"  → Filtered out invalid codec: {codec}")
        return 'N/A'
    
    # Normalize FourCC codes to common codec names
    normalized = FOURCC_TO_CODEC.get(codec_lower, codec)
    if normalized != codec:
        logger.debug(f"  → Normalized codec {codec} -> {normalized}")
    
    return normalized


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

    Args:
        url: Stream URL to analyze
        timeout: Timeout in seconds for the ffprobe operation
        user_agent: User agent string to use for HTTP requests

    Returns:
        Tuple of (video_info, audio_info) dictionaries, or (None, None) on error
        video_info contains: codec_name, width, height, avg_frame_rate
        audio_info contains: codec_name
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

            # Extract video and audio stream info
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
    
    This function replaces the previous two-step process (ffprobe + ffmpeg) with a 
    single ffmpeg call that extracts all needed information: codec, resolution, FPS, 
    and bitrate. This reduces network overhead and processing time.

    Args:
        url: Stream URL to analyze (will be validated and sanitized)
        duration: Duration in seconds to analyze the stream
        timeout: Base timeout in seconds (actual timeout includes duration + overhead)
        user_agent: User agent string to use for HTTP requests
        stream_startup_buffer: Buffer in seconds for stream startup (default: 10s)

    Returns:
        Dictionary containing:
        - video_codec: Video codec name (e.g., 'h264', 'hevc')
        - audio_codec: Audio codec name (e.g., 'aac', 'mp3')
        - resolution: Resolution string (e.g., '1920x1080')
        - fps: Frames per second (float)
        - bitrate_kbps: Bitrate in kbps (float or None)
        - status: "OK", "Timeout", or "Error"
        - elapsed_time: Time taken for the operation
    """
    # Validate and sanitize URL to prevent command injection
    if not url or not isinstance(url, str):
        logger.error("Invalid URL: must be a non-empty string")
        return {
            'video_codec': 'N/A',
            'audio_codec': 'N/A',
            'resolution': '0x0',
            'fps': 0,
            'bitrate_kbps': None,
            'status': 'Error',
            'elapsed_time': 0
        }
    
    # Basic URL validation - must start with http://, https://, or rtmp://
    url_lower = url.lower()
    if not (url_lower.startswith('http://') or url_lower.startswith('https://') or 
            url_lower.startswith('rtmp://') or url_lower.startswith('rtmps://')):
        logger.error(f"Invalid URL protocol: {url[:50]}... (must be http://, https://, rtmp://, or rtmps://)")
        return {
            'video_codec': 'N/A',
            'audio_codec': 'N/A',
            'resolution': '0x0',
            'fps': 0,
            'bitrate_kbps': None,
            'status': 'Error',
            'elapsed_time': 0
        }
    
    logger.debug(f"Analyzing stream with ffmpeg for {duration}s: {url[:50]}...")
    # Use list arguments to pass URL safely to subprocess without shell interpretation
    extra_args = _get_ffmpeg_extra_args()
    command = [
        'ffmpeg', '-re', '-v', 'debug', '-user_agent', user_agent,
    ] + extra_args + [
        '-i', url, '-t', str(duration), '-f', 'null', '-'
    ]

    result_data = {
        'video_codec': 'N/A',
        'audio_codec': 'N/A',
        'resolution': '0x0',
        'fps': 0,
        'bitrate_kbps': None,
        'status': 'OK',
        'elapsed_time': 0,
        'video_profile': None,
        'video_bit_depth': None,
        'audio_language': None,
        'sample_rate': None,
        'audio_channels': None,
    }

    # Add buffer to timeout to account for ffmpeg startup, network latency, and shutdown overhead
    # Uses configurable stream_startup_buffer for high quality streams that take longer to start
    actual_timeout = timeout + duration + stream_startup_buffer

    try:
        start = time.time()
        result = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=actual_timeout,
            text=True
        )
        elapsed = time.time() - start
        result_data['elapsed_time'] = elapsed
        
        output = result.stderr
        total_bytes = 0
        progress_bitrate = None
        
        # Track whether we're in the Input or Output section of FFmpeg output
        # This ensures we only parse input stream codecs, not decoded output formats
        in_input_section = False
        
        # Parse ffmpeg output to extract all information
        # Only process Stream lines from the Input section to get actual input codecs
        # (e.g., "aac", "ac3") instead of decoded output formats (e.g., "pcm_s16le")
        for line in output.splitlines():
            # Track when we enter the Input section
            if 'Input #' in line:
                in_input_section = True
                logger.debug(f"  → Entered Input section: {line.strip()}")
                continue
            
            # Track when we enter the Output section - stop parsing stream info
            if 'Output #' in line:
                in_input_section = False
                logger.debug(f"  → Entered Output section (will skip stream parsing): {line.strip()}")
                continue
            
            # Extract video codec, resolution, and FPS from Input stream lines only
            # Example: "Stream #0:0: Video: h264, yuv420p, 1920x1080, 25 fps"
            # Example with wrapped codec: "Stream #0:0(und): Video: wrapped_avframe (avc1 / 0x31637661), yuv420p, 1920x1080, 25 fps"
            # Only process Stream lines when in_input_section to avoid parsing output codecs
            if in_input_section and 'Stream #' in line and 'Video:' in line:
                try:
                    # Use robust codec extraction that handles wrapped codecs
                    # This will look inside parentheses if codec is a wrapper like 'wrapped_avframe'
                    video_codec = _extract_codec_from_line(line, 'Video')
                    if video_codec:
                        # Sanitize and normalize the extracted codec
                        video_codec = _sanitize_codec_name(video_codec)
                        # Only update if we got a valid codec (not N/A)
                        # This prevents overwriting a detected codec with N/A
                        if video_codec != 'N/A':
                            result_data['video_codec'] = video_codec
                            logger.debug(f"  → Final video codec: {result_data['video_codec']}")
                    
                    # Extract resolution
                    res_match = re.search(r'(\d{2,5})x(\d{2,5})', line)
                    if res_match:
                        width, height = res_match.groups()
                        result_data['resolution'] = f"{width}x{height}"
                        logger.debug(f"  → Detected resolution: {result_data['resolution']}")
                    
                    # Extract FPS
                    fps_match = re.search(r'(\d+\.?\d*)\s*fps', line)
                    if fps_match:
                        result_data['fps'] = round(float(fps_match.group(1)), 2)
                        logger.debug(f"  → Detected FPS: {result_data['fps']}")

                    # Extract video profile — e.g. "h264 (High)" or "hevc (Main 10)"
                    # Skip if it looks like a codec alias (contains / or 0x)
                    profile_match = re.search(r'Video:\s+\w+\s+\(([^)]+)\)', line)
                    if profile_match:
                        candidate = profile_match.group(1).strip()
                        if '/' not in candidate and '0x' not in candidate:
                            result_data['video_profile'] = candidate
                            logger.debug(f"  → Detected video profile: {candidate}")

                    # Extract bit depth from pixel format
                    # e.g. yuv420p10le → 10, yuv420p12le → 12, yuv420p → 8
                    depth_match = re.search(r'yuv[j\d]+p(\d{2})', line)
                    if depth_match:
                        result_data['video_bit_depth'] = int(depth_match.group(1))
                        logger.debug(f"  → Detected bit depth: {result_data['video_bit_depth']}")
                    elif re.search(r'yuv[j\d]+p\b', line):
                        result_data['video_bit_depth'] = 8
                        logger.debug(f"  → Detected bit depth: 8 (standard yuv)")
                except (ValueError, AttributeError) as e:
                    logger.debug(f"  → Error parsing video stream line: {e}")
            
            # Extract audio codec from Input stream lines only
            # Example: "Stream #0:1: Audio: aac, 48000 Hz, stereo"
            # Example with wrapped codec: "Stream #0:1(und): Audio: wrapped_avframe (aac)"
            # Only process Stream lines when in_input_section to avoid parsing decoded output (e.g., pcm_s16le)
            if in_input_section and 'Stream #' in line and 'Audio:' in line:
                try:
                    # Use robust codec extraction that handles wrapped codecs
                    audio_codec = _extract_codec_from_line(line, 'Audio')
                    if audio_codec:
                        # Sanitize and normalize the extracted codec
                        audio_codec = _sanitize_codec_name(audio_codec)
                        # Only update if we got a valid codec (not N/A)
                        # This prevents overwriting a detected codec with N/A
                        if audio_codec != 'N/A':
                            result_data['audio_codec'] = audio_codec
                            logger.debug(f"  → Final audio codec: {result_data['audio_codec']}")

                    # Extract audio language from stream specifier e.g. "Stream #0:1(eng):"
                    lang_match = re.search(r'Stream #\d+:\d+\((\w+)\):', line)
                    if lang_match:
                        lang = lang_match.group(1)
                        if lang.lower() not in ('und', 'unknown'):
                            result_data['audio_language'] = lang
                            logger.debug(f"  → Detected audio language: {lang}")

                    # Extract sample rate e.g. "48000 Hz"
                    sr_match = re.search(r'(\d+)\s+Hz', line)
                    if sr_match:
                        result_data['sample_rate'] = int(sr_match.group(1))
                        logger.debug(f"  → Detected sample rate: {result_data['sample_rate']}")

                    # Extract channel layout e.g. "stereo", "5.1(side)", "7.1"
                    # Strip trailing (side)/(back) qualifiers so "5.1(side)" → "5.1"
                    ch_match = re.search(r'\d+\s+Hz,\s+([^,\s]+)', line)
                    if ch_match:
                        layout = ch_match.group(1)
                        paren = layout.find('(')
                        if paren > 0:
                            layout = layout[:paren]
                        if layout:
                            result_data['audio_channels'] = layout.strip()
                            logger.debug(f"  → Detected audio channels: {result_data['audio_channels']}")
                except (ValueError, AttributeError) as e:
                    logger.debug(f"  → Error parsing audio stream line: {e}")
            
            # Extract bitrate using multiple methods (same as get_stream_bitrate)
            # Method 1: Statistics line with bytes read
            if "Statistics:" in line and "bytes read" in line:
                try:
                    parts = line.split("bytes read")
                    size_str = parts[0].strip().split()[-1]
                    total_bytes = int(size_str)
                    if total_bytes > 0 and duration > 0:
                        result_data['bitrate_kbps'] = (total_bytes * 8) / 1000 / duration
                        logger.debug(f"  → Calculated bitrate (method 1): {result_data['bitrate_kbps']:.2f} kbps from {total_bytes} bytes")
                except ValueError:
                    pass

            # Method 2: Parse progress output
            if "bitrate=" in line and "kbits/s" in line:
                try:
                    bitrate_match = re.search(r'bitrate=\s*(\d+\.?\d*)\s*kbits/s', line)
                    if bitrate_match:
                        progress_bitrate = float(bitrate_match.group(1))
                        logger.debug(f"  → Found progress bitrate (method 2): {progress_bitrate:.2f} kbps")
                except (ValueError, AttributeError):
                    pass

            # Method 3: Alternative bytes read pattern
            if result_data['bitrate_kbps'] is None and "bytes read" in line and "Statistics:" not in line:
                try:
                    bytes_match = re.search(r'(\d+)\s+bytes read', line)
                    if bytes_match:
                        total_bytes = int(bytes_match.group(1))
                        if total_bytes > 0 and duration > 0:
                            calculated_bitrate = (total_bytes * 8) / 1000 / duration
                            logger.debug(f"  → Calculated bitrate (method 3): {calculated_bitrate:.2f} kbps from {total_bytes} bytes")
                            result_data['bitrate_kbps'] = calculated_bitrate
                except (ValueError, AttributeError):
                    pass

        # Use progress bitrate as final fallback
        if result_data['bitrate_kbps'] is None and progress_bitrate is not None:
            result_data['bitrate_kbps'] = progress_bitrate
            logger.debug(f"  → Using last progress bitrate as fallback: {result_data['bitrate_kbps']:.2f} kbps")

        # Check if ffmpeg exited early with errors
        expected_min_time = duration * EARLY_EXIT_THRESHOLD
        exited_early = elapsed < expected_min_time
        
        # Log warnings if detection failed
        if result_data['bitrate_kbps'] is None:
            logger.warning(f"  ⚠ Failed to detect bitrate from ffmpeg output (analyzed for {elapsed:.2f}s, expected ~{duration}s)")
            
            if exited_early or result.returncode != 0:
                if result.returncode != 0:
                    logger.warning(f"  ⚠ ffmpeg exited with code {result.returncode}")
                else:
                    logger.warning(f"  ⚠ ffmpeg completed in {elapsed:.2f}s (expected ~{duration}s)")
                
                # Look for and log error messages using helper function
                error_patterns = [
                    "Connection refused", "Connection timed out", "Invalid data found",
                    "Server returned", "404 Not Found", "403 Forbidden", "401 Unauthorized",
                    "No route to host", "could not find codec", "Protocol not found",
                    "Error opening input", "Operation timed out", "I/O error",
                    "HTTP error", "SSL", "TLS", "Certificate"
                ]
                
                _log_ffmpeg_errors(output, logger, error_patterns)

        logger.debug(f"  → Analysis completed in {elapsed:.2f}s")
        
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
    Get stream bitrate using ffmpeg to analyze actual stream data.

    Uses multiple methods to detect bitrate:
    1. Primary: Parse "Statistics:" line with "bytes read"
    2. Fallback 1: Parse progress output lines (e.g., "bitrate=3333.3kbits/s")
    3. Fallback 2: Calculate from total bytes transferred

    Args:
        url: Stream URL to analyze
        duration: Duration in seconds to analyze the stream
        timeout: Base timeout in seconds (actual timeout includes duration + overhead)
        user_agent: User agent string to use for HTTP requests
        stream_startup_buffer: Buffer in seconds for stream startup (default: 10s)

    Returns:
        Tuple of (bitrate_kbps, status, elapsed_time)
        bitrate_kbps: Bitrate in kilobits per second, or None if detection failed
        status: "OK", "Timeout", or "Error"
        elapsed_time: Time taken for the operation
    """
    logger.debug(f"Analyzing bitrate for {duration}s...")
    extra_args = _get_ffmpeg_extra_args()
    command = [
        'ffmpeg', '-re', '-v', 'debug', '-user_agent', user_agent,
    ] + extra_args + [
        '-i', url, '-t', str(duration), '-f', 'null', '-'
    ]

    bitrate = None
    status = "OK"

    # Add buffer to timeout to account for ffmpeg startup, network latency, and shutdown overhead
    # Since -re flag reads at real-time, ffmpeg takes at least duration seconds
    # Uses configurable stream_startup_buffer for high quality streams that take longer to start
    actual_timeout = timeout + duration + stream_startup_buffer

    try:
        start = time.time()
        result = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=actual_timeout,
            text=True
        )
        elapsed = time.time() - start
        output = result.stderr
        total_bytes = 0
        progress_bitrate = None  # Track last progress bitrate separately

        for line in output.splitlines():
            # Method 1: Primary method - Statistics line with bytes read
            if "Statistics:" in line and "bytes read" in line:
                try:
                    parts = line.split("bytes read")
                    size_str = parts[0].strip().split()[-1]
                    total_bytes = int(size_str)
                    if total_bytes > 0 and duration > 0:
                        bitrate = (total_bytes * 8) / 1000 / duration
                        logger.debug(f"  → Calculated bitrate (method 1): {bitrate:.2f} kbps from {total_bytes} bytes")
                except ValueError:
                    pass

            # Method 2: Parse progress output (e.g., "size=12345kB time=00:00:30.00 bitrate=3333.3kbits/s")
            # Track latest progress bitrate as fallback, will use last one found
            if "bitrate=" in line and "kbits/s" in line:
                try:
                    bitrate_match = re.search(r'bitrate=\s*(\d+\.?\d*)\s*kbits/s', line)
                    if bitrate_match:
                        # Store progress bitrate, will keep updating with later values
                        progress_bitrate = float(bitrate_match.group(1))
                        logger.debug(f"  → Found progress bitrate (method 2): {progress_bitrate:.2f} kbps")
                except (ValueError, AttributeError):
                    pass

            # Method 3: Alternative bytes read pattern (not requiring Statistics:)
            if bitrate is None and "bytes read" in line and "Statistics:" not in line:
                try:
                    # Look for pattern like "12345 bytes read"
                    bytes_match = re.search(r'(\d+)\s+bytes read', line)
                    if bytes_match:
                        total_bytes = int(bytes_match.group(1))
                        if total_bytes > 0 and duration > 0:
                            calculated_bitrate = (total_bytes * 8) / 1000 / duration
                            logger.debug(f"  → Calculated bitrate (method 3): {calculated_bitrate:.2f} kbps from {total_bytes} bytes")
                            bitrate = calculated_bitrate
                except (ValueError, AttributeError):
                    pass

        # Use progress bitrate as final fallback if primary methods didn't find anything
        if bitrate is None and progress_bitrate is not None:
            bitrate = progress_bitrate
            logger.debug(f"  → Using last progress bitrate as fallback: {bitrate:.2f} kbps")

        # Check if ffmpeg exited early with errors
        # If elapsed time is much less than duration, ffmpeg likely encountered an error
        expected_min_time = duration * EARLY_EXIT_THRESHOLD
        exited_early = elapsed < expected_min_time
        
        # Log if bitrate detection failed
        if bitrate is None:
            logger.warning(f"  ⚠ Failed to detect bitrate from ffmpeg output (analyzed for {elapsed:.2f}s, expected ~{duration}s)")
            logger.debug(f"  → Searched {len(output.splitlines())} lines of output")
            
            # If ffmpeg exited early or returned non-zero, provide more details
            if exited_early or result.returncode != 0:
                if result.returncode != 0:
                    logger.warning(f"  ⚠ ffmpeg exited with code {result.returncode}")
                else:
                    logger.warning(f"  ⚠ ffmpeg completed in {elapsed:.2f}s (expected ~{duration}s) - stream may have ended early or encountered an error")
                
                # Look for and log specific error messages from ffmpeg output using helper function
                error_patterns = [
                    "Connection refused", "Connection timed out", "Invalid data found",
                    "Server returned", "404 Not Found", "403 Forbidden", "401 Unauthorized",
                    "No route to host", "could not find codec", "Protocol not found",
                    "Error opening input", "Operation timed out", "I/O error",
                    "HTTP error", "SSL", "TLS", "Certificate"
                ]
                
                _log_ffmpeg_errors(output, logger, error_patterns)

        logger.debug(f"  → Analysis completed in {elapsed:.2f}s")

    except subprocess.TimeoutExpired:
        logger.warning(f"Timeout ({actual_timeout}s) while fetching bitrate")
        status = "Timeout"
        elapsed = actual_timeout
    except Exception as e:
        logger.error(f"Bitrate check failed: {e}")
        status = "Error"
        elapsed = 0

    return bitrate, status, elapsed


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
        - stream_id: Stream identifier
        - stream_name: Stream name
        - stream_url: Stream URL
        - timestamp: ISO format timestamp of analysis
        - video_codec: Video codec name (e.g., 'h264', 'hevc')
        - audio_codec: Audio codec name (e.g., 'aac', 'mp3')
        - resolution: Resolution string (e.g., '1920x1080')
        - fps: Frames per second (float)
        - bitrate_kbps: Bitrate in kbps (float or None)
        - status: "OK", "Timeout", or "Error"
    """
    # In debug mode, show detailed entry log; in non-debug mode, be more concise
    if logger.isEnabledFor(logging.DEBUG):
        logger.info(f"▶ Analyzing stream: {stream_name} (ID: {stream_id})")
    else:
        logger.info(f"▶ Checking {stream_name}")

    # Default result in case of failure - includes all required fields
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
        'status': 'Error'
    }
    
    try:
        # Convert retries to total attempts: retries=0 means 1 attempt, retries=1 means 2 attempts, etc.
        total_attempts = retries + 1
        for attempt in range(total_attempts):
            if attempt > 0:
                if logger.isEnabledFor(logging.DEBUG):
                    logger.info(f"  Retry attempt {attempt} of {retries} (attempt {attempt + 1} of {total_attempts}) for {stream_name}")
                else:
                    # Show current attempt out of total attempts
                    logger.info(f"  ↻ Retry {attempt + 1}/{total_attempts} for {stream_name}")
                time.sleep(retry_delay)

            try:
                # Use single ffmpeg call to get all stream information
                if logger.isEnabledFor(logging.DEBUG):
                    logger.info("  Analyzing stream (single ffmpeg call)...")
                result_data = get_stream_info_and_bitrate(
                    url=stream_url,
                    duration=ffmpeg_duration,
                    timeout=timeout,
                    user_agent=user_agent,
                    stream_startup_buffer=stream_startup_buffer
                )

                # Build result dictionary
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
                    'status': result_data['status'],
                    'video_profile': result_data.get('video_profile'),
                    'video_bit_depth': result_data.get('video_bit_depth'),
                    'audio_language': result_data.get('audio_language'),
                    'sample_rate': result_data.get('sample_rate'),
                    'audio_channels': result_data.get('audio_channels'),
                }

                # Log results
                # In debug mode, show detailed multi-line logs
                # In non-debug mode, use one-liner for failures
                if logger.isEnabledFor(logging.DEBUG):
                    # Debug mode: verbose multi-line logging
                    if result['video_codec'] != 'N/A' or result['resolution'] != '0x0':
                        logger.info(f"    ✓ Video: {result['video_codec']}, {result['resolution']}, {result['fps']} FPS")
                    else:
                        logger.warning("    ✗ No video info found")

                    if result['audio_codec'] != 'N/A':
                        logger.info(f"    ✓ Audio: {result['audio_codec']}")
                    else:
                        logger.warning("    ✗ No audio info found")

                    if result['status'] == "OK":
                        if result['bitrate_kbps'] is not None:
                            logger.info(f"    ✓ Bitrate: {result['bitrate_kbps']:.2f} kbps (elapsed: {result_data['elapsed_time']:.2f}s)")
                        else:
                            logger.warning(f"    ⚠ Bitrate detection failed (elapsed: {result_data['elapsed_time']:.2f}s)")
                        logger.info(f"  ✓ Stream analysis complete for {stream_name}")
                    else:
                        logger.warning(f"    ✗ Status: {result['status']} (elapsed: {result_data['elapsed_time']:.2f}s)")
                else:
                    # Non-debug mode: one-liner for results
                    if result['status'] == "OK":
                        # Success: one line with key metrics
                        bitrate_str = f"{result['bitrate_kbps']:.2f} kbps" if result['bitrate_kbps'] is not None else "N/A"
                        logger.info(f"  ✓ {stream_name}: {result['resolution']}, {result['fps']} FPS, {bitrate_str}, {result['video_codec']}/{result['audio_codec']} ({result_data['elapsed_time']:.2f}s)")
                    else:
                        # Failure: one-liner with status and elapsed time
                        logger.warning(f"  ✗ {stream_name}: Check failed - {result['status']} ({result_data['elapsed_time']:.2f}s)")
                
                # Break on success
                if result['status'] == "OK":
                    break
                else:
                    # If not the last attempt, continue to retry
                    if attempt < total_attempts - 1:
                        if logger.isEnabledFor(logging.DEBUG):
                            logger.warning(f"  Stream '{stream_name}' failed with status '{result['status']}'. Retrying in {retry_delay} seconds... (attempt {attempt + 1} of {total_attempts})")
                        else:
                            # Show next attempt number correctly
                            logger.warning(f"  ↻ Retrying {stream_name} in {retry_delay}s (attempt {attempt + 2}/{total_attempts})")
            except Exception as inner_e:
                logger.error(f"  Exception during stream analysis (attempt {attempt + 1} of {total_attempts}): {inner_e}")
                # Continue to next retry if available, otherwise use the default error result
                if attempt < total_attempts - 1:
                    logger.warning(f"  Retrying in {retry_delay} seconds... (attempt {attempt + 1} of {total_attempts})")
    except Exception as outer_e:
        logger.error(f"Unexpected error in analyze_stream for {stream_name}: {outer_e}")
        # Result already has default error values, so just return it

    return result
