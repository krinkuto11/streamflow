"""
Stream Analyzer Module

Provides stream quality analysis using ffprobe.
This module is used by the stream checker service for analyzing IPTV streams.

The analyzer:
- Extracts video/audio codecs, resolution, bitrate, fps
- Handles provider-based rate limiting with semaphores
- Supports retry logic for failed streams
- Uses ffprobe with optimized parameters for MPEG-TS streams
"""

import json
import logging
import subprocess
import threading
import time
from datetime import datetime
from typing import Dict, Optional, Any
from urllib.parse import urlparse

from logging_config import setup_logging

logger = setup_logging(__name__)

# Provider-based semaphores to prevent overwhelming a single provider
provider_semaphores: Dict[str, threading.Semaphore] = {}
semaphore_lock = threading.Lock()


def check_ffmpeg_installed() -> bool:
    """Checks if ffmpeg and ffprobe are installed and in PATH.
    
    Returns:
        bool: True if both are available, False otherwise
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


def get_provider_from_url(url: str) -> str:
    """Extracts the hostname and port as a provider identifier.
    
    Args:
        url: Stream URL
        
    Returns:
        str: Provider identifier (hostname:port or 'unknown_provider')
    """
    try:
        return urlparse(url).netloc
    except Exception:
        return "unknown_provider"


def get_stream_info(url: str, timeout: int, user_agent: str = 'VLC/3.0.14', 
                    check_mode: str = 'full', probe_duration: int = 5) -> Optional[Dict[str, Any]]:
    """Gets stream information using a single ffprobe command.
    
    Retrieves all necessary stream information in one call:
    - Video: codec, resolution, framerate, bitrate
    - Audio: codec, sample_rate, channels, bitrate
    
    For MPEG-TS streams, bitrate might not be in headers, so we use
    -analyzeduration and -probesize to give ffprobe time to calculate it.
    These values are optimized for speed while ensuring accuracy.
    
    Args:
        url: Stream URL to analyze
        timeout: Timeout in seconds for ffprobe operation
        user_agent: User agent string for HTTP requests
        check_mode: 'full' (analyze bitrate) or 'quick' (skip bitrate analysis)
        probe_duration: Seconds to analyze stream (only used in 'full' mode)
    
    Returns:
        Dict with parsed stream information or None on failure
        
    Example return value:
        {
            'video_codec': 'h264',
            'audio_codec': 'aac',
            'resolution': '1920x1080',
            'fps': 25.0,
            'bitrate_kbps': 4500,
            'audio_sample_rate': '48000',
            'audio_channels': 2,
            'status': 'OK'
        }
    """
    logger.debug(f"Running ffprobe for URL: {url[:50]}... (mode: {check_mode})")
    
    # Calculate network timeout in microseconds (should be less than subprocess timeout)
    # Use 80% of the timeout to leave time for cleanup
    network_timeout_us = int(timeout * 0.8 * 1000000)
    
    # Build command based on check mode
    command = ['ffprobe', '-user_agent', user_agent]
    
    # Add network timeout to prevent hanging on slow/buffering streams
    # This applies to all network protocols (http, https, hls, etc.)
    command.extend([
        '-rw_timeout', str(network_timeout_us)
    ])
    
    if check_mode == 'full':
        # Full mode: analyze bitrate (takes longer)
        # Convert probe_duration to microseconds for -analyzeduration
        analyze_duration_us = probe_duration * 1000000
        # Set probesize to 2MB per second of probe duration (minimum 1MB)
        probe_size_bytes = max(probe_duration * 2000000, 1000000)
        command.extend([
            '-analyzeduration', str(analyze_duration_us),
            '-probesize', str(probe_size_bytes)
        ])
    else:
        # Quick mode: minimal analysis (faster, no bitrate)
        # Increased from 0.1s to 1s and 500KB to 1MB to handle buffering better
        command.extend([
            '-analyzeduration', '1000000',  # 1 second (increased from 0.1s)
            '-probesize', '1000000'  # 1MB (increased from 500KB)
        ])
    
    command.extend([
        '-v', 'error',
        '-show_entries', 'stream=codec_name,codec_type,width,height,avg_frame_rate,bit_rate,sample_rate,channels:format=bit_rate,duration',
        '-of', 'json',
        url
    ])
    try:
        logger.debug(f"Executing ffprobe command with timeout={timeout}s, check_mode={check_mode}")
        result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout, text=True)
        
        # Log any errors from ffprobe stderr (even if successful)
        if result.stderr:
            logger.debug(f"ffprobe stderr: {result.stderr[:200]}")
        
        if result.stdout:
            data = json.loads(result.stdout)
            streams = data.get('streams', [])
            format_info = data.get('format', {})
            
            logger.debug(f"ffprobe returned {len(streams)} streams")
            
            # Parse video and audio streams
            video_stream = None
            audio_stream = None
            
            for stream in streams:
                codec_type = stream.get('codec_type', '')
                if codec_type == 'video' and not video_stream:
                    video_stream = stream
                elif codec_type == 'audio' and not audio_stream:
                    audio_stream = stream
            
            # Build result dict
            result_dict = {
                'video_codec': 'N/A',
                'audio_codec': 'N/A',
                'resolution': '0x0',
                'fps': 0,
                'bitrate_kbps': 0,
                'audio_sample_rate': 'N/A',
                'audio_channels': 'N/A',
                'status': 'OK'
            }
            
            # Extract video info
            if video_stream:
                result_dict['video_codec'] = video_stream.get('codec_name', 'N/A')
                width = video_stream.get('width', 0)
                height = video_stream.get('height', 0)
                result_dict['resolution'] = f"{width}x{height}"
                
                # Parse FPS
                fps_str = video_stream.get('avg_frame_rate', '0/1')
                try:
                    num, den = map(int, fps_str.split('/'))
                    result_dict['fps'] = round(num / den, 2) if den != 0 else 0
                except (ValueError, ZeroDivisionError, AttributeError):
                    result_dict['fps'] = 0
                
                # Get video bitrate (prefer stream bitrate, fallback to format bitrate)
                video_bitrate = video_stream.get('bit_rate')
                if video_bitrate is not None:
                    try:
                        result_dict['bitrate_kbps'] = round(int(video_bitrate) / 1000, 2)
                    except (ValueError, TypeError):
                        pass
            
            # If video bitrate not available from stream, try format bitrate
            if result_dict['bitrate_kbps'] == 0:
                format_bitrate = format_info.get('bit_rate')
                if format_bitrate is not None:
                    try:
                        result_dict['bitrate_kbps'] = round(int(format_bitrate) / 1000, 2)
                    except (ValueError, TypeError):
                        pass
            
            # Extract audio info
            if audio_stream:
                result_dict['audio_codec'] = audio_stream.get('codec_name', 'N/A')
                result_dict['audio_sample_rate'] = audio_stream.get('sample_rate', 'N/A')
                result_dict['audio_channels'] = audio_stream.get('channels', 'N/A')
            
            # Determine status based on extracted data
            if result_dict['resolution'] == '0x0' or result_dict['video_codec'] == 'N/A':
                result_dict['status'] = 'No Video'
            elif check_mode == 'full' and (not result_dict['bitrate_kbps'] or result_dict['bitrate_kbps'] == 0):
                # In full mode, missing bitrate is an issue
                result_dict['status'] = 'No Bitrate'
            # In quick mode, missing bitrate is expected and OK
            
            return result_dict
            
        logger.debug("ffprobe returned empty output")
        return None
    except subprocess.TimeoutExpired:
        logger.warning(f"Timeout ({timeout}s) while fetching stream info for: {url[:50]}...")
        return None
    except json.JSONDecodeError as e:
        logger.warning(f"Failed to decode JSON from ffprobe for {url[:50]}...: {e}")
        return None
    except Exception as e:
        logger.error(f"Stream info check failed for {url[:50]}...: {e}")
        return None


def analyze_stream(stream_data: Dict[str, Any], timeout: int = 30, retries: int = 1, 
                   retry_delay: int = 10, user_agent: str = 'VLC/3.0.14',
                   check_mode: str = 'full', probe_duration: int = 5) -> Dict[str, Any]:
    """Analyzes a stream using ffprobe with provider-based rate limiting.
    
    This function:
    - Extracts stream quality metrics (codec, resolution, bitrate, fps)
    - Implements provider-based semaphores to prevent overwhelming providers
    - Supports retry logic for failed streams
    - Returns a dictionary with analysis results
    
    Args:
        stream_data: Dictionary containing stream information. Must include:
            - stream_url: The URL of the stream
            - stream_id: Unique identifier for the stream
            - stream_name: Display name of the stream
            - channel_id: Channel ID (optional)
            - channel_name: Channel name (optional)
        timeout: Timeout in seconds for ffprobe operation (default: 30)
        retries: Number of retry attempts on failure (default: 1)
        retry_delay: Delay in seconds between retries (default: 10)
        user_agent: User agent string for HTTP requests (default: 'VLC/3.0.14')
        check_mode: 'full' (analyze bitrate) or 'quick' (skip bitrate, faster)
        probe_duration: Seconds to analyze stream (only used in 'full' mode, default: 5)
    
    Returns:
        Dict with analysis results including all input fields plus:
            - timestamp: ISO format timestamp of analysis
            - video_codec: Video codec name or 'N/A'
            - audio_codec: Audio codec name or 'N/A'
            - resolution: Resolution in format 'WIDTHxHEIGHT'
            - fps: Frames per second
            - bitrate_kbps: Bitrate in kilobits per second
            - audio_sample_rate: Audio sample rate or 'N/A'
            - audio_channels: Number of audio channels or 'N/A'
            - status: 'OK', 'No Video', 'No Bitrate', or 'Error'
    
    Example:
        stream = {
            'stream_url': 'http://example.com/stream.m3u8',
            'stream_id': 123,
            'stream_name': 'Channel 1 HD',
            'channel_id': 1,
            'channel_name': 'Channel 1'
        }
        result = analyze_stream(stream, timeout=30, retries=2, check_mode='quick')
    """
    url = stream_data.get('stream_url')
    stream_name = stream_data.get('stream_name', 'Unknown')
    stream_id = stream_data.get('stream_id', 'Unknown')
    
    if not url:
        logger.warning(f"No URL for stream {stream_name} (ID: {stream_id})")
        return stream_data

    provider = get_provider_from_url(url)
    
    # Get or create provider-specific semaphore for rate limiting
    with semaphore_lock:
        if provider not in provider_semaphores:
            provider_semaphores[provider] = threading.Semaphore(1)
        provider_semaphore = provider_semaphores[provider]

    with provider_semaphore:
        logger.info(f"▶ Processing stream: {stream_name} (ID: {stream_id}, Provider: {provider})")

        for attempt in range(retries + 1):
            if attempt > 0:
                logger.info(f"  Retry attempt {attempt}/{retries} for {stream_name}")
                
            # Initialize fields for each attempt
            stream_data['timestamp'] = datetime.now().isoformat()
            stream_data['video_codec'] = 'N/A'
            stream_data['audio_codec'] = 'N/A'
            stream_data['resolution'] = '0x0'
            stream_data['fps'] = 0
            stream_data['bitrate_kbps'] = 0
            stream_data['status'] = 'N/A'
            stream_data['audio_sample_rate'] = 'N/A'
            stream_data['audio_channels'] = 'N/A'

            # Get all stream info with single ffprobe command
            logger.info(f"  Analyzing stream with ffprobe...")
            stream_info = get_stream_info(url, timeout, user_agent, check_mode, probe_duration)
            
            if stream_info:
                # Update stream_data with all extracted information
                stream_data['video_codec'] = stream_info.get('video_codec', 'N/A')
                stream_data['audio_codec'] = stream_info.get('audio_codec', 'N/A')
                stream_data['resolution'] = stream_info.get('resolution', '0x0')
                stream_data['fps'] = stream_info.get('fps', 0)
                stream_data['bitrate_kbps'] = stream_info.get('bitrate_kbps', 0)
                stream_data['audio_sample_rate'] = stream_info.get('audio_sample_rate', 'N/A')
                stream_data['audio_channels'] = stream_info.get('audio_channels', 'N/A')
                stream_data['status'] = stream_info.get('status', 'OK')
                
                logger.info(f"    ✓ Video: {stream_data['video_codec']}, {stream_data['resolution']}, {stream_data['fps']} FPS, {stream_data['bitrate_kbps']} kbps")
                logger.info(f"    ✓ Audio: {stream_data['audio_codec']}, {stream_data['audio_sample_rate']} Hz, {stream_data['audio_channels']} channels")
                logger.info(f"    ✓ Status: {stream_data['status']}")
            else:
                stream_data['status'] = 'Error'
                logger.warning(f"    ✗ Failed to analyze stream")

            # If the status is OK, break the retry loop
            if stream_data['status'] == 'OK':
                logger.info(f"  ✓ Stream analysis complete for {stream_name}")
                break

            # If not the last attempt, wait before retrying
            if attempt < retries:
                logger.warning(f"  Stream '{stream_name}' failed with status '{stream_data['status']}'. Retrying in {retry_delay} seconds... ({attempt + 1}/{retries})")
                time.sleep(retry_delay)

    return stream_data
