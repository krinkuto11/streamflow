"""
FFmpeg Stream Monitor

Lightweight stream monitoring using FFmpeg's null muxer for continuous
quality assessment without significant resource usage.
"""

import logging
import os
import re
import shutil
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Optional, Callable, Dict, Any

from apps.core.logging_config import setup_logging
from apps.core.log_sanitizer import scrub_urls, url_ref

logger = setup_logging(__name__)

# ──────────────────────────────────────────────────────────────────────────────
# Transport Health Evaluation
# ──────────────────────────────────────────────────────────────────────────────

# Diagnostic patterns grouped by impact severity
_DIAG_REGEX: Dict[str, re.Pattern] = {
    # Minor decoding artefacts
    'decode_error': re.compile(
        r'error while decoding mb|corrupt decoded frame|concealing|'
        r'missing picture in access unit|reference picture missing|'
        r'non-existing pps|decode_slice_header error',
        re.IGNORECASE
    ),
    # Corrupt transport-layer packets
    'corrupt_packet': re.compile(
        r'corrupt input packet|invalid data found when processing input|'
        r'reference frames exceeds max',
        re.IGNORECASE
    ),
    # Fatal / complete stream failure signals
    'fatal': re.compile(
        r'connection timed out|server returned 4|server returned 5|'
        r'end of file|error opening input|connection refused|no route to host',
        re.IGNORECASE
    ),
}

# Freeze / black-screen detection (seconds)
_STALE_FREEZE_THRESHOLD = 5.0   # seconds without speed/bitrate update → freeze


class DiagnosticTracker:
    """
    Maintains rolling 60-second error counts per category and evaluates
    transport health according to the defined threshold matrix.
    """

    WINDOW = 60.0  # seconds

    def __init__(self):
        self._events: Dict[str, deque] = {
            cat: deque() for cat in _DIAG_REGEX
        }
        self._lock = threading.Lock()
        self._fatal_detected = False
        self._last_stats_time: float = time.time()  # for freeze detection
        self._freeze_start: Optional[float] = None

    # ──────────────────────────────────────────────────────────
    # Feed
    # ──────────────────────────────────────────────────────────

    def record_line(self, line: str, ts: float = None) -> None:
        """Parse a single FFmpeg stderr line and record any diagnostic events."""
        if ts is None:
            ts = time.time()

        with self._lock:
            for cat, regex in _DIAG_REGEX.items():
                if regex.search(line):
                    self._events[cat].append(ts)
                    if cat == 'fatal':
                        self._fatal_detected = True
                    break  # one category per line

    def record_stats_update(self) -> None:
        """Call every time FFmpeg emits a live stats line (speed/bitrate update)."""
        with self._lock:
            self._last_stats_time = time.time()
            self._freeze_start = None  # stats flowing → no freeze

    def _prune(self, ts: float) -> None:
        """Remove events older than the rolling window."""
        cutoff = ts - self.WINDOW
        for dq in self._events.values():
            while dq and dq[0] < cutoff:
                dq.popleft()

    def _count(self, categories: list, ts: float) -> int:
        return sum(len(self._events[c]) for c in categories if c in self._events)

    # ──────────────────────────────────────────────────────────
    # Evaluation
    # ──────────────────────────────────────────────────────────

    def get_transport_health(self) -> Dict[str, Any]:
        """Evaluate current transport health and return a structured report."""
        ts = time.time()
        with self._lock:
            self._prune(ts)

            # ── Freeze detection ──────────────────────────────
            freeze_secs = 0.0
            if self._last_stats_time > 0:
                stale = ts - self._last_stats_time
                if stale >= _STALE_FREEZE_THRESHOLD:
                    # Track when the freeze started
                    if self._freeze_start is None:
                        self._freeze_start = self._last_stats_time + _STALE_FREEZE_THRESHOLD
                    freeze_secs = ts - self._freeze_start

            # ── Error densities (per minute) ──────────────────
            decode_count = self._count(['decode_error'], ts)
            corrupt_count = self._count(['corrupt_packet'], ts)
            fatal_detected = self._fatal_detected

            # Peak density = worst category per minute
            peak_density = max(decode_count, corrupt_count) / (self.WINDOW / 60.0)

            # ── Health thresholds ─────────────────────────────
            #
            # CRITICAL: fatal signals, OR freeze > 15s, OR continuous corruption
            # SEVERE:   corrupt_packet ≥ 20/min, OR freeze 5–15s
            # DEGRADED: decode/corrupt 5–20/min, OR freeze 2–4s
            # HEALTHY:  < 3 minor decode errors/min, no freeze/black-screen

            if fatal_detected or freeze_secs > 15:
                status = 'Critical'
                if fatal_detected:
                    summary = 'Critical failure due to stream connection error or total data loss.'
                else:
                    summary = f'Critical failure: stream frozen for {freeze_secs:.0f} seconds.'
            elif corrupt_count >= 20 or (5 <= freeze_secs <= 15):
                status = 'Severe'
                if freeze_secs >= 5:
                    summary = f'Severe degradation: stream frozen for {freeze_secs:.0f} seconds with heavy packet corruption.'
                else:
                    summary = f'Severe degradation: {corrupt_count} corrupt transport packets in the last 60 seconds.'
            elif (decode_count + corrupt_count) >= 5 or (2 <= freeze_secs < 5):
                status = 'Degraded'
                if freeze_secs >= 2:
                    summary = f'Noticeable instability: brief freeze of {freeze_secs:.0f} seconds detected alongside decoding artefacts.'
                else:
                    summary = f'Noticeable instability: {decode_count + corrupt_count} artefact/corruption errors per minute.'
            else:
                status = 'Healthy'
                if decode_count > 0:
                    summary = f'Normal network jitter: {decode_count} minor decoding error(s) in the last 60 seconds.'
                else:
                    summary = 'Stream transport is clean and stable.'

            return {
                'status': status,
                'summary': summary,
                'error_density': round(peak_density, 1),
                'freeze_secs': round(freeze_secs, 1),
                'decode_errors': decode_count,
                'corrupt_packets': corrupt_count,
            }

# Constants
# Constants for bitrate calculation

# Constants for bitrate calculation
BITS_PER_BYTE = 8
BYTES_TO_KBPS = 1000


@dataclass
class FFmpegStats:
    """Statistics from FFmpeg monitoring"""
    url: str
    speed: float = 0.0
    bitrate: float = 0.0  # kbps
    fps: float = 0.0
    width: int = 0
    height: int = 0
    time: float = 0.0  # seconds
    last_updated: float = 0.0
    is_alive: bool = False
    is_fatal: bool = False
    error_message: Optional[str] = None
    audio_language: Optional[str] = None
    # Additional fields for bitrate calculation
    total_size: int = 0  # bytes
    start_time: float = 0.0


class FFmpegStreamMonitor:
    """
    Lightweight FFmpeg stream monitor using null muxer.
    
    Uses `ffmpeg -i <url> -c copy -f null -` to monitor stream health
    without significant CPU/memory overhead.
    """
    
    # Pre-compiled regexes for output monitoring (avoid recreating inside loop)
    _REGEX_FATAL_ERRORS = re.compile(
        r'error opening input:|error opening input file|error opening input files:|'
        r'server returned 4|server returned 5|connection refused|no route to host|'
        r'connection timed out|end of file|protocol not found',
        re.IGNORECASE
    )

    _REGEX_NON_FATAL_ERRORS = re.compile(
        r'decode_slice_header error|concealing|error while decoding mb|'
        r'missing picture in access unit|error decoding the audio block|'
        r'invalid data found when processing input|non-existing pps 0 referenced|'
        r'no frame!|reference picture missing|i/o error|no such file or directory',
        re.IGNORECASE
    )

    _REGEX_GENERAL_ERROR = re.compile(r'error|failed', re.IGNORECASE)
    _REGEX_FATAL_WORD = re.compile(r'fatal', re.IGNORECASE)

    # Pre-compiled regexes for metadata parsing
    _REGEX_RESOLUTION = re.compile(r'(\d{3,5})x(\d{3,5})')
    _REGEX_FPS_METADATA = re.compile(r'(\d+(?:\.\d+)?)\s*fps')
    _REGEX_AUDIO_LANG = re.compile(r'Stream #\d+:\d+\((\w+)\):')

    # Pre-compiled regexes for stats parsing
    _REGEX_SPEED = re.compile(r'speed=\s*(\d+(?:\.\d+)?)\s*x?')
    _REGEX_BITRATE = re.compile(r'bitrate=\s*([0-9.]+)\s*([kmg]?)bits/s', re.IGNORECASE)
    _REGEX_SIZE = re.compile(r'size=\s*([0-9.]+)\s*([kmg]?)B', re.IGNORECASE)
    _REGEX_FPS = re.compile(r'fps=\s*(\d+(?:\.\d+)?)')
    _REGEX_TIME = re.compile(r'time=(\d{2}):(\d{2}):(\d{2}\.\d{2})')
    
    def __init__(self, url: str, stream_id: Optional[int] = None, on_stats_update: Optional[Callable[[FFmpegStats], None]] = None):
        """
        Initialize stream monitor.
        
        Args:
            url: Stream URL to monitor
            stream_id: Optional stream ID for unique UDP port assignment
            on_stats_update: Optional callback for stats updates
        """
        self.url = url
        self.stream_id = stream_id
        self.on_stats_update = on_stats_update
        self.stats = FFmpegStats(url=url)
        
        # UDP ports for Primary-Sidecar routing and Live Preview
        self.port_a = None
        self.port_b = None
        if stream_id is not None:
            self.port_a = 10000 + stream_id
            self.port_b = 20000 + stream_id
        
        self._process: Optional[subprocess.Popen] = None
        self._monitor_thread: Optional[threading.Thread] = None
        self._running = False
        self._lock = threading.Lock()
        
        # Error tracking for auto-restart
        self._error_history = deque(maxlen=100)
        
        # Transport health diagnostic tracker
        self.diagnostics = DiagnosticTracker()
    
    def start(self) -> bool:
        """
        Start monitoring the stream.
        
        Returns:
            True if started successfully
        """
        with self._lock:
            if self._running:
                logger.warning(f"Monitor already running for {self.url}")
                return False
            
            # Stop any existing process/monitor before starting
            self._stop_no_lock()
            
            try:
                # Validate URL to prevent command injection
                if not self._validate_url(self.url):
                    logger.error(f"Invalid or unsafe URL: {url_ref(self.url)}")
                    self.stats.error_message = "Invalid URL"
                    return False
                
                # Prepare FFmpeg command
                cmd = [
                    'ffmpeg',
                    '-hide_banner', # Reduce log spam
                    '-err_detect', 'ignore_err', # Ignore minor errors to prevent player stalling
                    '-fflags', '+genpts', # Regenerate timestamps (keep as input flag)
                    '-nostdin',     # Disable interactive stdin
                    '-i', self.url,
                ]
                
                if self.port_a and self.port_b:
                    # Duplicate to:
                    # - two local UDP ports (port_a & port_b) for sidecar processes
                    # - one UDP port (port_viewer) for direct MPEG-TS viewing via HTTP proxy
                    # - null output for stats
                    cmd.extend([
                        '-map', '0', '-c', 'copy', '-f', 'fifo', '-fifo_format', 'mpegts', '-drop_pkts_on_overflow', '1', '-attempt_recovery', '1', f'udp://127.0.0.1:{self.port_a}',
                        '-map', '0', '-c', 'copy', '-f', 'fifo', '-fifo_format', 'mpegts', '-drop_pkts_on_overflow', '1', '-attempt_recovery', '1', f'udp://127.0.0.1:{self.port_b}',
                        '-map', '0', '-c', 'copy', '-f', 'null', '-'
                    ])
                else:
                    cmd.extend(['-f', 'null', '-'])
                
                # Start FFmpeg process
                self._process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    stdin=subprocess.DEVNULL, # Explicitly redirect stdin to prevent terminal-related issues
                    universal_newlines=True,
                    bufsize=1
                )
                
                self._running = True
                self.stats.is_alive = True
                self.stats.last_updated = time.time()
                self.stats.start_time = time.time()
                
                # Start monitoring thread
                self._monitor_thread = threading.Thread(
                    target=self._monitor_output,
                    daemon=True,
                    name=f"FFmpegMonitor-{id(self)}"
                )
                self._monitor_thread.start()
                
                logger.info(f"Started monitoring stream: {url_ref(self.url)}")
                return True
                
            except Exception as e:
                logger.error(f"Failed to start FFmpeg monitor: {scrub_urls(e)}")
                self._running = False
                self.stats.is_alive = False
                self.stats.error_message = scrub_urls(e)
                return False
    
    def _validate_url(self, url: str) -> bool:
        """
        Validate URL to prevent command injection and ensure it's a supported protocol.
        
        Args:
            url: URL to validate
            
        Returns:
            True if URL is valid and safe
        """
        if not url or not isinstance(url, str):
            return False
        
        # Check for command injection attempts
        dangerous_chars = ['`', '$', ';', '|', '&', '\n', '\r']
        if any(char in url for char in dangerous_chars):
            logger.warning(f"Rejected URL with dangerous characters: {url_ref(url)}")
            return False
        
        # Check for supported protocols
        import re
        # Allow http(s), acestream, rtmp(s), etc.
        protocol_pattern = r'^(https?|acestream|rtmp|rtmps|rtp|rtsp|udp|tcp)://'
        if not re.match(protocol_pattern, url, re.IGNORECASE):
            logger.warning(f"Rejected URL with unsupported protocol: {url_ref(url)}")
            return False
        
        # Basic length check
        if len(url) > 2000:
            logger.warning(f"Rejected URL that is too long: {len(url)} chars")
            return False
        
        return True
    
    def stop(self):
        """Stop monitoring the stream"""
        with self._lock:
            self._stop_no_lock()
            
    def _stop_no_lock(self):
        """
        Internal stop method called while holding self._lock.
        """
        self._running = False
        
        # Terminate FFmpeg process
        if self._process:
            try:
                self._process.terminate()
                # Give it a moment to terminate gracefully
                try:
                    self._process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self._process.kill()
            except Exception as e:
                logger.error(f"Error stopping FFmpeg process: {scrub_urls(e)}")
            finally:
                self._process = None
        
        # Cleanup process
        self.stats.is_alive = False
        
        logger.info(f"Stopped monitoring stream: {url_ref(self.url)}")
    
    def _monitor_output(self):
        """Monitor FFmpeg stderr output for statistics"""
        if not self._process or not self._process.stderr:
            return
        
        try:
            for line in self._process.stderr:
                if not self._running:
                    break
                
                # Parse stats from FFmpeg output
                self._parse_stats(line)
                self._parse_metadata(line)
                
                # Check for fatal errors that should stop the monitor
                is_fatal = self._REGEX_FATAL_ERRORS.search(line) is not None
                
                # Check if this is a non-fatal error that should be logged at debug level
                is_non_fatal_error = self._REGEX_NON_FATAL_ERRORS.search(line) is not None
                
                # Feed diagnostic tracker for transport health evaluation
                self.diagnostics.record_line(line)

                # Check for errors
                if self._REGEX_GENERAL_ERROR.search(line):
                    if is_non_fatal_error:
                        # Log non-fatal errors at debug level to avoid spam
                        logger.debug(f"FFmpeg non-fatal error for {url_ref(self.url)}: {scrub_urls(line.strip())}")
                        
                        # Track non-fatal error frequency for health checks
                        current_time = time.time()
                        self._error_history.append(current_time)
                        
                        # If more than 50 errors in 30 seconds, consider it a stalled stream that needs restart
                        recent_errors = [t for t in self._error_history if current_time - t < 30]
                        if len(recent_errors) > 50:
                            logger.debug(f"Excessive decoding errors ({len(recent_errors)} in 30s) for {url_ref(self.url)}. Triggering restart.")
                            self.stats.error_message = "Excessive decoding errors"
                            self.stats.is_alive = False
                            self.stats.is_fatal = False # Explicitly non-fatal, just needs restart
                            self._running = False
                            break
                    else:
                        # Log other errors at warning level
                        logger.warning(f"FFmpeg error for {url_ref(self.url)}: {scrub_urls(line.strip())}")
                    
                    if is_fatal or self._REGEX_FATAL_WORD.search(line):
                        self.stats.error_message = scrub_urls(line.strip())
                        self.stats.is_alive = False
                        self.stats.is_fatal = True # Fatal error, stop and quarantine
                        # Stop monitoring on fatal error
                        logger.warning(f"Fatal FFmpeg error detected, stopping monitor for {url_ref(self.url)}")
                        self._running = False
                        break
                
                # Callback if provided
                if self.on_stats_update:
                    try:
                        self.on_stats_update(self.stats)
                    except Exception as e:
                        logger.error(f"Error in stats callback: {scrub_urls(e)}")
        
        except Exception as e:
            logger.error(f"Error monitoring FFmpeg output: {scrub_urls(e)}")
        
        finally:
            # Check if process exited unexpectedly
            if self._process:
                try:
                    exit_code = self._process.poll()
                    if exit_code is not None and exit_code != 0:
                        logger.warning(f"FFmpeg process exited with code {exit_code} for {url_ref(self.url)}")
                        self.stats.error_message = f"FFmpeg exited with code {exit_code}"
                except Exception as e:
                    logger.error(f"Error checking FFmpeg exit code: {scrub_urls(e)}")
            
            with self._lock:
                self._running = False
                self.stats.is_alive = False
    
    def _parse_metadata(self, output: str):
        """Parse stream metadata from FFmpeg output"""
        # Look for video stream info: "Stream #0:0: Video: h264... 1920x1080 ..."
        if 'Video:' in output:
            # Extract resolution
            res_match = self._REGEX_RESOLUTION.search(output)
            if res_match:
                self.stats.width = int(res_match.group(1))
                self.stats.height = int(res_match.group(2))
            
            # Extract FPS from metadata line
            fps_match = self._REGEX_FPS_METADATA.search(output)
            if fps_match:
                self.stats.fps = round(float(fps_match.group(1)), 0)
        
        # Extract audio language: "Stream #0:1(eng): Audio: ..."
        if 'Audio:' in output:
            lang_match = self._REGEX_AUDIO_LANG.search(output)
            if lang_match:
                self.stats.audio_language = lang_match.group(1)
                logger.debug(f"Detected audio language for {url_ref(self.url)}: {self.stats.audio_language}")
    
    def _parse_stats(self, output: str):
        """Parse real-time statistics from FFmpeg output"""
        # Speed (e.g., "speed=1.02x")
        speed_match = self._REGEX_SPEED.search(output)
        if speed_match:
            self.stats.speed = float(speed_match.group(1))
        
        # Bitrate (e.g., "bitrate= 406.1kbits/s")
        bitrate_found = False
        bitrate_match = self._REGEX_BITRATE.search(output)
        if bitrate_match:
            value = float(bitrate_match.group(1))
            unit = bitrate_match.group(2).lower()
            
            # Convert to kbps
            if unit == 'm':
                value *= 1000
            elif unit == 'g':
                value *= 1000000
            # 'k' or empty defaults to kbps
            
            self.stats.bitrate = value
            bitrate_found = True
        
        # Size (e.g., "size= 12345kB") - for calculating bitrate if not provided
        size_match = self._REGEX_SIZE.search(output)
        if size_match:
            value = float(size_match.group(1))
            unit = size_match.group(2).lower()
            
            # Convert to bytes
            if unit == 'k':
                value *= 1024
            elif unit == 'm':
                value *= 1024 * 1024
            elif unit == 'g':
                value *= 1024 * 1024 * 1024
            
            self.stats.total_size = int(value)
            
            # Calculate bitrate from size and time if explicit bitrate not available
            if not bitrate_found and self.stats.time > 0:
                self.stats.bitrate = (self.stats.total_size * BITS_PER_BYTE) / self.stats.time / BYTES_TO_KBPS
        
        # FPS (e.g., "fps= 30")
        fps_match = self._REGEX_FPS.search(output)
        if fps_match:
            ffmpeg_fps = float(fps_match.group(1))
            
            # Calculate actual FPS: actual_fps = ffmpeg_fps / speed
            if self.stats.speed > 0:
                self.stats.fps = round(ffmpeg_fps / self.stats.speed, 0)
            elif ffmpeg_fps > 0:
                self.stats.fps = round(ffmpeg_fps, 0)
        
        # Time (e.g., "time=00:01:23.45")
        time_match = self._REGEX_TIME.search(output)
        if time_match:
            hours = float(time_match.group(1))
            minutes = float(time_match.group(2))
            seconds = float(time_match.group(3))
            self.stats.time = hours * 3600 + minutes * 60 + seconds
        
        # Update timestamp
        self.stats.last_updated = time.time()
        
        # Notify diagnostic tracker that stats are flowing (no freeze)
        self.diagnostics.record_stats_update()
    
    def get_stats(self) -> FFmpegStats:
        """Get current statistics"""
        return self.stats
    
    def get_transport_health(self) -> Dict[str, Any]:
        """Get transport health evaluation from diagnostic tracker."""
        return self.diagnostics.get_transport_health()
    
    def is_running(self) -> bool:
        """Check if monitor is running"""
        return self._running
    
    def is_alive(self) -> bool:
        """Check if stream is alive"""
        return self.stats.is_alive
    
    def is_buffering(self) -> bool:
        """
        Check if stream is buffering.
        
        Stream is considered buffering if:
        - Speed is significantly less than 1.0
        - No recent updates
        """
        if not self.stats.is_alive:
            return False
        
        # Check for low speed (buffering indicator)
        if self.stats.speed > 0 and self.stats.speed < 0.9:
            return True
        
        # Check for stale data (no updates recently)
        time_since_update = time.time() - self.stats.last_updated
        if time_since_update > 10:  # 10 seconds without update
            return True
        
        return False
