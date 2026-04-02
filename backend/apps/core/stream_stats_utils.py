#!/usr/bin/env python3
"""
Stream Statistics Utilities for StreamFlow.

This module provides centralized utilities for handling stream statistics
consistently across all parts of the application. It ensures that:
1. Field names are standardized (ffmpeg_output_bitrate, source_fps, resolution)
2. Formatting is consistent (bitrate in kbps/Mbps, FPS with 'fps' suffix)
3. Data extraction works the same way everywhere

This eliminates inconsistencies where different parts of the code used different
field names or formatting approaches.
"""

import re
from typing import Dict, Any, Optional, Tuple, List
from collections import Counter


def parse_bitrate_value(bitrate_raw) -> Optional[float]:
    """Parse bitrate from various formats to kbps.
    
    Handles formats like:
    - "1234 kbps"
    - "1234.5"
    - "1.2 Mbps"
    - "1234"
    - 1234 (int/float)
    - None
    
    Args:
        bitrate_raw: Raw bitrate value in various formats
        
    Returns:
        Bitrate in kbps as float, or None if parsing fails or value is invalid
    """
    if not bitrate_raw:
        return None
    
    try:
        # If it's already a number
        if isinstance(bitrate_raw, (int, float)):
            return float(bitrate_raw) if bitrate_raw > 0 else None
        
        # If it's a string, parse it
        if isinstance(bitrate_raw, str):
            bitrate_str = bitrate_raw.strip().lower()
            
            # Use regex to extract numeric value (handles single decimal point correctly)
            # Try Mbps first (convert to kbps)
            if 'mbps' in bitrate_str or 'mb/s' in bitrate_str:
                match = re.search(r'(\d+\.?\d*)', bitrate_str)
                if match:
                    value = float(match.group(1))
                    return value * 1000  # Convert Mbps to kbps
            
            # Try kbps or kb/s
            if 'kbps' in bitrate_str or 'kb/s' in bitrate_str:
                match = re.search(r'(\d+\.?\d*)', bitrate_str)
                if match:
                    return float(match.group(1))
            
            # Try plain number (assume kbps)
            match = re.search(r'(\d+\.?\d*)', bitrate_str)
            if match:
                value = float(match.group(1))
                return value if value > 0 else None
    except (ValueError, TypeError, AttributeError):
        pass
    
    return None


def format_bitrate(bitrate_kbps: Optional[float]) -> str:
    """Format bitrate for display.
    
    Args:
        bitrate_kbps: Bitrate in kbps (float or None)
        
    Returns:
        Formatted string like "1234 kbps" or "1.2 Mbps" or "N/A"
    """
    if bitrate_kbps is None or bitrate_kbps <= 0:
        return 'N/A'
    
    # If over 1000 kbps, show in Mbps
    if bitrate_kbps >= 1000:
        return f"{bitrate_kbps / 1000:.1f} Mbps"
    
    return f"{bitrate_kbps:.0f} kbps"


def parse_fps_value(fps_raw) -> Optional[float]:
    """Parse FPS from various formats.
    
    Handles formats like:
    - "25 fps"
    - "25.0"
    - "25"
    - 25 (int/float)
    - None
    
    Args:
        fps_raw: Raw FPS value in various formats
        
    Returns:
        FPS as float, or None if parsing fails or value is invalid
    """
    if not fps_raw:
        return None
    
    try:
        # If it's already a number
        if isinstance(fps_raw, (int, float)):
            return float(fps_raw) if fps_raw > 0 else None
        
        # If it's a string, parse it
        if isinstance(fps_raw, str):
            fps_str = fps_raw.strip().lower()
            
            # Use regex to extract numeric value (handles single decimal point correctly)
            match = re.search(r'(\d+\.?\d*)', fps_str)
            if match:
                value = float(match.group(1))
                return value if value > 0 else None
    except (ValueError, TypeError, AttributeError):
        pass
    
    return None


def format_fps(fps: Optional[float]) -> str:
    """Format FPS for display.
    
    Note: Uses 1 decimal place formatting, which will round values like 29.97 to 30.0.
    This is intentional for cleaner display, as sub-decimal precision is rarely needed.
    
    Args:
        fps: FPS value (float or None)
        
    Returns:
        Formatted string like "25.0 fps" or "N/A"
    """
    if fps is None or fps <= 0:
        return 'N/A'
    
    return f"{fps:.1f} fps"


def normalize_resolution(resolution: Any) -> str:
    """Normalize resolution to standard format.
    
    Important: This function preserves "0x0" and partial zero resolutions (e.g., "1920x0")
    for dead stream detection. These are technically valid resolution formats but indicate
    dead/broken streams. The is_stream_dead() function depends on this behavior.
    
    Args:
        resolution: Resolution in various formats (str, None, etc.)
        
    Returns:
        Normalized resolution string like "1920x1080", "0x0", or "N/A"
    """
    if not resolution or resolution in ['Unknown', 'N/A', None]:
        return 'N/A'
    
    # Already a string in correct format (including "0x0" for dead stream detection)
    if isinstance(resolution, str):
        # Check if it's a valid resolution format (e.g., "1920x1080" or "0x0")
        if 'x' in resolution:
            return resolution
    
    return 'N/A'


def extract_stream_stats(stream_data: Dict[str, Any]) -> Dict[str, Any]:
    """Extract and normalize stream statistics from stream data.
    
    This function handles the various ways stream stats might be stored:
    - In 'stream_stats' field (from UDI/Dispatcharr)
    - As direct fields on the stream object (from stream_check_utils)
    - As JSON string that needs parsing
    
    Args:
        stream_data: Stream data dictionary that may contain stream_stats
        
    Returns:
        Normalized dictionary with standardized field names:
        - resolution: str (e.g., "1920x1080" or "N/A")
        - fps: float or None
        - bitrate_kbps: float or None
        - video_codec: str (e.g., "h264" or "N/A")
        - audio_codec: str (e.g., "aac" or "N/A")
        - pixel_format: str or None
        - audio_sample_rate: int or None
        - audio_channels: int or None
        - channel_layout: str or None
        - audio_bitrate: int or None
    """
    import json
    
    result = {
        'resolution': 'N/A',
        'fps': None,
        'bitrate_kbps': None,
        'video_codec': 'N/A',
        'audio_codec': 'N/A',
        'hdr_format': None,
        'pixel_format': None,
        'audio_sample_rate': None,
        'audio_channels': None,
        'channel_layout': None,
        'audio_bitrate': None
    }
    
    # Try to get stream_stats from various locations
    stream_stats = None
    
    # Check if stream_stats exists as a field
    if 'stream_stats' in stream_data:
        stream_stats = stream_data['stream_stats']
        
        # Handle case where stream_stats is None
        if stream_stats is None:
            stream_stats = {}
        
        # Handle case where stream_stats is a JSON string
        elif isinstance(stream_stats, str):
            try:
                stream_stats = json.loads(stream_stats)
                if stream_stats is None:
                    stream_stats = {}
            except json.JSONDecodeError:
                stream_stats = {}
    
    # If stream_stats exists and is a dict, extract from it
    if stream_stats and isinstance(stream_stats, dict):
        # Resolution - use as-is
        result['resolution'] = normalize_resolution(stream_stats.get('resolution'))
        
        # FPS - use source_fps (standard field name from Dispatcharr)
        result['fps'] = parse_fps_value(stream_stats.get('source_fps'))
        
        # Bitrate - use ffmpeg_output_bitrate (standard field name from Dispatcharr)
        result['bitrate_kbps'] = parse_bitrate_value(stream_stats.get('ffmpeg_output_bitrate'))
        
        # Codecs
        result['video_codec'] = stream_stats.get('video_codec', 'N/A') or 'N/A'
        result['audio_codec'] = stream_stats.get('audio_codec', 'N/A') or 'N/A'
        
        # HDR Format
        result['hdr_format'] = stream_stats.get('hdr_format')

        # Enriched stats
        result['pixel_format'] = stream_stats.get('pixel_format')
        result['audio_sample_rate'] = stream_stats.get('audio_sample_rate')
        result['audio_channels'] = stream_stats.get('audio_channels')
        result['channel_layout'] = stream_stats.get('channel_layout')
        result['audio_bitrate'] = stream_stats.get('audio_bitrate')
    
    # Fallback: check if fields are directly on stream_data (e.g., from analyze_stream)
    if result['resolution'] == 'N/A' and 'resolution' in stream_data:
        result['resolution'] = normalize_resolution(stream_data.get('resolution'))
    
    if result['fps'] is None and 'fps' in stream_data:
        result['fps'] = parse_fps_value(stream_data.get('fps'))
    
    if result['bitrate_kbps'] is None and 'bitrate_kbps' in stream_data:
        result['bitrate_kbps'] = parse_bitrate_value(stream_data.get('bitrate_kbps'))
    
    if result['video_codec'] == 'N/A' and 'video_codec' in stream_data:
        result['video_codec'] = stream_data.get('video_codec', 'N/A') or 'N/A'
    
    if result['audio_codec'] == 'N/A' and 'audio_codec' in stream_data:
        result['audio_codec'] = stream_data.get('audio_codec', 'N/A') or 'N/A'
        
    if result['hdr_format'] is None and 'hdr_format' in stream_data:
        result['hdr_format'] = stream_data.get('hdr_format')

    if result['pixel_format'] is None and 'pixel_format' in stream_data:
        result['pixel_format'] = stream_data.get('pixel_format')

    if result['audio_sample_rate'] is None and 'audio_sample_rate' in stream_data:
        result['audio_sample_rate'] = stream_data.get('audio_sample_rate')

    if result['audio_channels'] is None and 'audio_channels' in stream_data:
        result['audio_channels'] = stream_data.get('audio_channels')

    if result['channel_layout'] is None and 'channel_layout' in stream_data:
        result['channel_layout'] = stream_data.get('channel_layout')

    if result['audio_bitrate'] is None and 'audio_bitrate' in stream_data:
        result['audio_bitrate'] = stream_data.get('audio_bitrate')
    
    return result


def format_stream_stats_for_display(stream_stats: Dict[str, Any]) -> Dict[str, str]:
    """Format stream statistics for display in UI.
    
    Args:
        stream_stats: Dictionary with extracted stream stats (from extract_stream_stats)
        
    Returns:
        Dictionary with formatted strings ready for display:
        - resolution: str (e.g., "1920x1080" or "N/A")
        - fps: str (e.g., "25.0 fps" or "N/A")
        - bitrate: str (e.g., "5000 kbps" or "5.0 Mbps" or "N/A")
        - video_codec: str (e.g., "h264" or "N/A")
        - audio_codec: str (e.g., "aac" or "N/A")
    """
    return {
        'resolution': stream_stats.get('resolution', 'N/A'),
        'fps': format_fps(stream_stats.get('fps')),
        'bitrate': format_bitrate(stream_stats.get('bitrate_kbps')),
        'video_codec': stream_stats.get('video_codec', 'N/A'),
        'audio_codec': stream_stats.get('audio_codec', 'N/A')
    }


def calculate_channel_averages(streams: list, dead_stream_ids: set = None) -> Dict[str, str]:
    """Calculate channel-level average statistics from multiple streams.
    
    Args:
        streams: List of stream dictionaries (with stream_stats or direct fields)
        dead_stream_ids: Optional set of stream IDs to exclude from averages
        
    Returns:
        Dictionary with formatted average statistics:
        - avg_resolution: str (most common resolution or "N/A")
        - avg_bitrate: str (formatted average bitrate or "N/A")
        - avg_fps: str (formatted average FPS or "N/A")
    """
    if dead_stream_ids is None:
        dead_stream_ids = set()
    
    resolutions_for_avg = []
    bitrates_for_avg = []
    fps_for_avg = []
    
    for stream in streams:
        # Skip dead streams from averages
        stream_id = stream.get('stream_id') or stream.get('id')
        if stream_id in dead_stream_ids:
            continue
        
        # Extract stats using centralized utility
        stats = extract_stream_stats(stream)
        
        if stats['resolution'] != 'N/A':
            resolutions_for_avg.append(stats['resolution'])
        
        if stats['bitrate_kbps'] and stats['bitrate_kbps'] > 0:
            bitrates_for_avg.append(stats['bitrate_kbps'])
        
        if stats['fps'] and stats['fps'] > 0:
            fps_for_avg.append(stats['fps'])
    
    # Calculate averages
    avg_resolution = 'N/A'
    if resolutions_for_avg:
        resolution_counts = Counter(resolutions_for_avg)
        avg_resolution = resolution_counts.most_common(1)[0][0]
    
    avg_bitrate = 'N/A'
    if bitrates_for_avg:
        avg_bitrate_kbps = sum(bitrates_for_avg) / len(bitrates_for_avg)
        avg_bitrate = format_bitrate(avg_bitrate_kbps)
    
    avg_fps = 'N/A'
    if fps_for_avg:
        avg_fps = format_fps(sum(fps_for_avg) / len(fps_for_avg))
    
    return {
        'avg_resolution': avg_resolution,
        'avg_bitrate': avg_bitrate,
        'avg_fps': avg_fps
    }


def is_stream_dead(stream_data: Dict[str, Any], config: Dict[str, Any] = None) -> Tuple[bool, str]:
    """Check if a stream should be considered dead based on its statistics.
    
    A stream is dead if:
    - Resolution is '0x0' or contains 0 in width or height (Reason: 'offline')
    - Bitrate is 0 or None (Reason: 'offline')
    - Falls below configured minimum thresholds (Reason: 'low_quality')
    
    Args:
        stream_data: Stream data dictionary (can contain stream_stats or direct fields)
        config: Optional configuration dictionary with thresholds:
                - min_resolution_width: Minimum width in pixels (default: 0 = no check)
                - min_resolution_height: Minimum height in pixels (default: 0 = no check)
                - min_bitrate_kbps: Minimum bitrate in kbps (default: 0 = no check)
                - min_score: Minimum score 0-100 (default: 0 = no check)
        
    Returns:
        Tuple of (bool, str): (is_dead, reason)
        Reasons: 'offline' (truly dead), 'low_quality' (below thresholds), 'none' (not dead)
    """
    # Extract normalized stats
    stats = extract_stream_stats(stream_data)
    
    # Check resolution
    resolution = stats['resolution']
    if resolution and resolution != 'N/A':
        # Check if resolution is exactly 0x0
        if resolution == '0x0':
            return True, 'offline'
        # Check if width or height is 0 (e.g., "0x1080" or "1920x0")
        if 'x' in resolution:
            try:
                parts = resolution.split('x')
                if len(parts) == 2:
                    width, height = int(parts[0]), int(parts[1])
                    if width == 0 or height == 0:
                        return True, 'offline'
                    
                    # Check against configured minimum thresholds if provided
                    if config:
                        min_width = config.get('min_resolution_width', 0)
                        min_height = config.get('min_resolution_height', 0)
                        if min_width > 0 and width < min_width:
                            return True, 'low_quality'
                        if min_height > 0 and height < min_height:
                            return True, 'low_quality'
            except (ValueError, IndexError):
                pass
    
    # Check bitrate
    bitrate = stats['bitrate_kbps']
    # Refined logic: only mark as dead if bitrate is explicitly 0, not just missing (None)
    if isinstance(bitrate, (int, float)) and bitrate == 0:
        return True, 'offline'
    
    # Check against configured minimum bitrate if provided
    if config and bitrate:
        min_bitrate = config.get('min_bitrate_kbps', 0)
        if min_bitrate > 0 and bitrate < min_bitrate:
            return True, 'low_quality'
    
    # Check against configured minimum FPS if provided
    fps = stats['fps']
    if config and fps is not None:
        min_fps = config.get('min_fps', 0)
        if min_fps > 0 and fps < min_fps:
            return True, 'low_quality'
    
    # Check against configured minimum score if provided
    if config:
        min_score = config.get('min_score', 0)
        if min_score > 0:
            # Get score from stream_data if available
            score = stream_data.get('score', 0)
            if isinstance(score, (int, float)) and score < min_score:
                return True, 'low_quality'
    
    return False, 'none'
