"""
AceStream Monitoring Service

Continuous monitoring service for AceStream channels with health tracking,
stream monitoring (FFmpeg or HTTP-based), and automatic stream ordering.
"""

import re
import requests
import subprocess
import threading
import time
from typing import Dict, List, Optional, Tuple, Any
from datetime import datetime, timedelta

from acestream_db import AceStreamDatabase
from acestream_http_monitor import HTTPStreamKeepAlive
from dead_streams_tracker import DeadStreamsTracker
from logging_config import setup_logging

logger = setup_logging(__name__)


class AceStreamMonitor:
    """
    Continuous monitoring service for AceStream channels.
    
    Features:
    - Stream monitoring (FFmpeg or HTTP-based) to keep streams alive
    - Orchestrator API integration
    - Health scoring and stream ordering
    - Resource-efficient operation
    - Graceful shutdown with cleanup
    """
    
    def __init__(self, udi_manager, default_orchestrator_url: str = "http://gluetun:19000", config: Dict[str, Any] = None):
        """
        Initialize AceStream monitor.
        
        Args:
            udi_manager: UDI manager instance for accessing channels/streams
            default_orchestrator_url: Default Orchestrator URL
            config: Configuration dictionary with monitoring settings
                - monitoring_interval: Seconds between checks (default: 30)
                - dead_stream_retry_interval: Seconds before retrying dead streams (default: 300)
                - monitoring_method: 'ffmpeg' or 'http' (default: 'http')
                - http_keepalive_interval: Seconds between HTTP requests (default: 10, for HTTP method)
                - http_chunk_size: Bytes per HTTP request (default: 65536, for HTTP method)
        """
        self.udi_manager = udi_manager
        self.default_orchestrator_url = default_orchestrator_url
        self.config = config or {}
        self.db = AceStreamDatabase()
        self.dead_streams_tracker = DeadStreamsTracker()
        
        self.monitoring_threads: Dict[int, threading.Thread] = {}
        self.shutdown_event = threading.Event()
        self.running = False
        
        # Determine monitoring method (ffmpeg or http)
        self.monitoring_method = self.config.get('monitoring_method', 'http')
        
        # FFmpeg-based monitoring (original approach)
        # Track continuous FFmpeg processes per stream
        self.ffmpeg_processes: Dict[int, subprocess.Popen] = {}
        self.ffmpeg_stats_cache: Dict[int, Dict] = {}
        self.ffmpeg_threads: Dict[int, threading.Thread] = {}
        self.ffmpeg_failures: Dict[int, Dict[str, Any]] = {}
        
        # HTTP-based monitoring (new lightweight approach)
        self.http_keepalive = HTTPStreamKeepAlive()
        
        # Track when dead streams were last checked for retry
        # Format: {stream_id: timestamp}
        self.dead_stream_retry_times: Dict[int, datetime] = {}
        
        # Track livepos and download speed for health detection
        # Format: {stream_id: {'last_livepos': int, 'last_check': datetime, 'last_speed_down': int, 'speed_down_zero_since': datetime}}
        self.stream_health_tracking: Dict[int, Dict[str, Any]] = {}
        
        logger.info(f"AceStream monitor initialized with {self.monitoring_method} monitoring method")
    
    def start_monitoring(self):
        """Start monitoring all AceStream channels."""
        if self.running:
            logger.warning("AceStream monitoring already running")
            return
        
        self.running = True
        self.shutdown_event.clear()
        
        logger.info("Starting AceStream monitoring service...")
        
        # Get all AceStream channels
        channels = self._get_acestream_channels()
        
        for channel in channels:
            self._start_channel_monitoring(channel)
        
        logger.info(f"Monitoring started for {len(channels)} AceStream channels")
    
    def _get_acestream_channels(self) -> List[Any]:
        """Get all channels marked as AceStream channels."""
        try:
            all_channels = self.udi_manager.get_channels()
            acestream_channels = [ch for ch in all_channels if ch.get('is_acestream', False)]
            return acestream_channels
        except Exception as e:
            logger.error(f"Error getting AceStream channels: {e}")
            return []
    
    def _start_channel_monitoring(self, channel):
        """Start monitoring a specific channel."""
        channel_id = channel.get('id')
        if channel_id in self.monitoring_threads:
            logger.debug(f"Channel {channel_id} already being monitored")
            return
        
        thread = threading.Thread(
            target=self._monitor_channel,
            args=(channel,),
            daemon=True,
            name=f"AceStreamMonitor-Channel{channel_id}"
        )
        self.monitoring_threads[channel_id] = thread
        thread.start()
        logger.info(f"Started monitoring thread for channel {channel_id}: {channel.get('name')}")
    
    def _monitor_channel(self, channel):
        """Main monitoring loop for a channel."""
        channel_id = channel.get('id')
        channel_name = channel.get('name')
        logger.info(f"Starting monitoring for channel {channel_id}: {channel_name}")
        
        # Track which streams are being monitored for this channel
        monitored_stream_ids = set()
        
        while not self.shutdown_event.is_set():
            try:
                # Refresh the channel data to get latest information, but preserve AceStream flag
                # This fixes the issue where UDI refresh would cause "no longer AceStream" message
                refreshed_channel = self.udi_manager.get_channel_by_id(channel_id)
                if refreshed_channel:
                    # Update channel reference with fresh data while preserving is_acestream flag
                    if not refreshed_channel.get('is_acestream', False):
                        # Channel was updated but lost the AceStream flag - restore it
                        logger.warning(f"Channel {channel_id} lost AceStream flag during refresh, preserving it")
                        refreshed_channel['is_acestream'] = channel.get('is_acestream', False)
                        refreshed_channel['acestream_orchestrator_url'] = channel.get('acestream_orchestrator_url')
                        refreshed_channel['acestream_config'] = channel.get('acestream_config')
                        # Update in UDI to persist the flag
                        self.udi_manager.update_channel(channel_id, refreshed_channel)
                    channel = refreshed_channel
                else:
                    # Channel no longer exists
                    logger.info(f"Channel {channel_id} no longer exists, stopping monitoring")
                    break
                
                # Get channel streams
                streams = self._get_channel_streams(channel)
                
                if not streams:
                    logger.debug(f"No streams found for channel {channel_id}")
                    time.sleep(60)
                    continue
                
                # Track current stream IDs
                current_stream_ids = {s.get('id') for s in streams}
                
                # Stop FFmpeg for streams that are no longer in this channel
                removed_stream_ids = monitored_stream_ids - current_stream_ids
                for stream_id in removed_stream_ids:
                    logger.info(f"Stream {stream_id} removed from channel {channel_id}, stopping FFmpeg")
                    self._stop_ffmpeg_process(stream_id)
                
                # Get orchestrator URL (channel-specific or default)
                orchestrator_url = channel.get('acestream_orchestrator_url') or self.default_orchestrator_url
                
                # Monitor each stream and collect health data
                # Stagger stream starts with configurable delay between each
                stream_health = []
                stream_start_stagger = self.config.get('stream_start_stagger', 0.5)
                
                for i, stream in enumerate(streams):
                    if self.shutdown_event.is_set():
                        break
                    
                    stream_id = stream.get('id')
                    
                    # Add to monitored set for new streams (with staggering)
                    if stream_id not in monitored_stream_ids:
                        if i > 0:  # Don't delay the first stream
                            time.sleep(stream_start_stagger)  # Configurable stagger between starts
                        monitored_stream_ids.add(stream_id)
                    
                    health = self._check_stream_health(channel, stream, orchestrator_url)
                    if health:
                        stream_health.append((stream, health))
                
                # Reorder streams by health if we have data
                if stream_health and len(stream_health) > 1:
                    self._reorder_streams_by_health(channel, stream_health)
                
                # Wait before next check (use configured interval or default 30 seconds)
                interval = self.config.get('monitoring_interval', 30)
                self.shutdown_event.wait(interval)
                
            except Exception as e:
                logger.error(f"Error monitoring channel {channel_id}: {e}", exc_info=True)
                self.shutdown_event.wait(60)
        
        # Clean up when exiting - stop all stream monitoring (HTTP or FFmpeg) for this channel's streams
        for stream_id in monitored_stream_ids:
            self._stop_stream_keepalive(stream_id)
        
        if channel_id in self.monitoring_threads:
            del self.monitoring_threads[channel_id]
        
        logger.info(f"Stopped monitoring channel {channel_id}")
    
    def _get_channel_streams(self, channel) -> List[Any]:
        """Get streams for a channel from UDI."""
        try:
            channel_id = channel.get('id')
            stream_ids = channel.get('streams', [])
            streams = []
            for stream_id in stream_ids:
                stream = self.udi_manager.get_stream_by_id(stream_id)
                if stream:
                    streams.append(stream)
            return streams
        except Exception as e:
            logger.error(f"Error getting streams for channel {channel.get('id')}: {e}")
            return []
    
    def _check_stream_health(self, channel, stream, orchestrator_url: str) -> Optional[Dict]:
        """
        Check stream health by combining stream monitoring stats and Orchestrator data.
        
        Uses either FFmpeg or HTTP monitoring based on configuration.
        Stream monitoring stats vary by method:
        - HTTP: is_alive, requests_sent, bytes_received, failures
        - FFmpeg: bitrate, resolution, fps, codec, errors
        
        Returns health metrics dict or None if check failed.
        """
        try:
            stream_id = stream.get('id')
            stream_url = stream.get('url')
            channel_id = channel.get('id')
            
            # Extract AceStream ID from URL
            acestream_id = self._extract_acestream_id(stream_url)
            if not acestream_id:
                logger.debug(f"Cannot extract AceStream ID from URL: {stream_url}")
                return None
            
            # Get stats from Orchestrator
            orchestrator_stats = self._get_orchestrator_stats(acestream_id, orchestrator_url)
            
            # Check stream health before continuing
            if orchestrator_stats:
                dead_reason = self._check_stream_is_dead(stream_id, orchestrator_stats)
                if dead_reason:
                    logger.warning(f"Stream {stream_id} detected as dead: {dead_reason}")
                    self._mark_stream_dead(stream_id, stream_url, channel_id)
                    # Stop keep-alive for dead stream
                    self._stop_stream_keepalive(stream_id)
                    return None
            
            # Ensure stream keep-alive is running (FFmpeg or HTTP)
            self._ensure_stream_keepalive(stream_id, stream_url, channel_id)
            
            # Get stream monitoring stats (from FFmpeg or HTTP)
            monitoring_stats = self._get_monitoring_stats(stream_id)
            
            # Calculate health score
            health_score = self._calculate_health_score(
                orchestrator_stats, 
                monitoring_stats
            )
            
            # Get or create session and save metrics
            session = self.db.get_active_session(stream_id)
            if session:
                session_id = session['id']
            else:
                session_id = self.db.create_session(
                    stream_id=stream_id,
                    channel_id=channel_id,
                    acestream_id=acestream_id
                )
            
            self.db.save_metrics(session_id, health_score, orchestrator_stats, monitoring_stats)
            
            return {
                'stream_id': stream_id,
                'acestream_id': acestream_id,
                'health_score': health_score,
                'orchestrator_stats': orchestrator_stats,
                'monitoring_stats': monitoring_stats
            }
            
        except Exception as e:
            logger.error(f"Error checking stream {stream.get('id')} health: {e}")
            return None
    
    def _ensure_stream_keepalive(self, stream_id: int, stream_url: str, channel_id: int = None):
        """
        Ensure stream keep-alive is running using the configured method.
        
        Args:
            stream_id: Stream ID
            stream_url: Stream URL
            channel_id: Channel ID (optional, for dead stream tracking)
        """
        if self.monitoring_method == 'http':
            self._ensure_http_keepalive(stream_id, stream_url, channel_id)
        else:  # ffmpeg
            self._ensure_ffmpeg_running(stream_id, stream_url, channel_id)
    
    def _ensure_http_keepalive(self, stream_id: int, stream_url: str, channel_id: int = None):
        """
        Ensure HTTP keep-alive is running for a stream.
        
        Args:
            stream_id: Stream ID
            stream_url: Stream URL
            channel_id: Channel ID (optional, for dead stream tracking)
        """
        # Check if stream is in dead streams tracker (globally dead)
        if self.dead_streams_tracker.is_dead(stream_url):
            # Stream is permanently dead, don't start keep-alive
            # Stop it if it's running
            if self.http_keepalive.is_stream_alive(stream_id):
                logger.debug(f"Stopping keep-alive for dead stream {stream_id}")
                self.http_keepalive.stop_keepalive(stream_id)
            return
        
        # Check if stream is marked as dead and should be retried
        if stream_id in self.dead_stream_retry_times:
            retry_interval = self.config.get('dead_stream_retry_interval', 300)
            last_retry = self.dead_stream_retry_times[stream_id]
            if datetime.now() < last_retry + timedelta(seconds=retry_interval):
                return  # Not yet time to retry
            else:
                logger.info(f"Retrying dead stream {stream_id} after {retry_interval}s interval")
                del self.dead_stream_retry_times[stream_id]
        
        # Check if HTTP keep-alive is already running
        if self.http_keepalive.is_stream_alive(stream_id):
            # Already running, check health
            health = self.http_keepalive.get_stream_health(stream_id)
            if health and health.get('failures', 0) >= 3:
                # Too many failures, mark as dead
                logger.error(f"Stream {stream_id} has {health['failures']} failures, marking as dead")
                self._mark_stream_dead(stream_id, stream_url, channel_id)
                self.http_keepalive.stop_keepalive(stream_id)
                return
            return  # Keep-alive is running and healthy
        
        # Start HTTP keep-alive
        interval = self.config.get('http_keepalive_interval', 10)
        chunk_size = self.config.get('http_chunk_size', 65536)  # 64KB
        
        try:
            self.http_keepalive.start_keepalive(
                stream_id=stream_id,
                stream_url=stream_url,
                interval=interval,
                chunk_size=chunk_size
            )
            logger.info(f"Started HTTP keep-alive for stream {stream_id}")
        except Exception as e:
            logger.error(f"Error starting HTTP keep-alive for stream {stream_id}: {e}")
    
    def _get_monitoring_stats(self, stream_id: int) -> Optional[Dict]:
        """
        Get monitoring statistics for a stream based on the monitoring method.
        
        Args:
            stream_id: Stream ID
            
        Returns:
            Dict with monitoring stats or None
        """
        if self.monitoring_method == 'http':
            # Get HTTP keep-alive stats
            stats = self.http_keepalive.get_stream_stats(stream_id)
            if stats:
                # Transform to format compatible with health calculation
                http_stats = stats.get('stats', {})
                health = stats.get('health', {})
                
                return {
                    'method': 'http',
                    'is_alive': health.get('is_alive', False),
                    'requests_sent': http_stats.get('requests_sent', 0),
                    'bytes_received': http_stats.get('bytes_received', 0),
                    'last_success': http_stats.get('last_success_time'),
                    'last_error': http_stats.get('last_error'),
                    'failures': health.get('failures', 0)
                }
            return None
        else:  # ffmpeg
            # Get cached FFmpeg stats
            return self.ffmpeg_stats_cache.get(stream_id)
    
    def _mark_stream_dead(self, stream_id: int, stream_url: str, channel_id: int = None):
        """
        Mark a stream as dead.
        
        Args:
            stream_id: Stream ID
            stream_url: Stream URL
            channel_id: Channel ID
        """
        stream = self.udi_manager.get_stream_by_id(stream_id)
        if stream:
            self.dead_streams_tracker.mark_as_dead(
                stream_url=stream.get('url', stream_url),
                stream_id=stream_id,
                stream_name=stream.get('name', f'Stream {stream_id}'),
                channel_id=channel_id
            )
        
        # Schedule retry check
        retry_interval = self.config.get('dead_stream_retry_interval', 300)
        self.dead_stream_retry_times[stream_id] = datetime.now()
        logger.info(f"Stream {stream_id} will be retried after {retry_interval}s")
    
    def _check_stream_is_dead(self, stream_id: int, orchestrator_stats: Dict) -> Optional[str]:
        """
        Check if a stream should be marked as dead based on livepos and download speed.
        
        The best indicator is live_last field (shows stream is advancing).
        Zero download speed is only considered dead if livepos is also NOT advancing.
        
        Args:
            stream_id: Stream ID
            orchestrator_stats: Stats from orchestrator
            
        Returns:
            Reason string if dead, None if alive
        """
        now = datetime.now()
        
        # Get configuration parameters
        livepos_buffer_tolerance = self.config.get('livepos_buffer_tolerance', 30)  # Default 30 seconds
        speed_down_timeout = self.config.get('speed_down_timeout', 10)  # Default 10 seconds
        
        # Initialize tracking for this stream if not present
        if stream_id not in self.stream_health_tracking:
            self.stream_health_tracking[stream_id] = {
                'last_livepos': None,
                'last_check': now,
                'last_speed_down': None,
                'speed_down_zero_since': None
            }
        
        tracking = self.stream_health_tracking[stream_id]
        
        # Extract livepos data
        livepos = orchestrator_stats.get('livepos', {})
        current_livepos = None
        if isinstance(livepos, dict):
            # Try different keys that might contain the live position timestamp
            current_livepos = livepos.get('live_last') or livepos.get('last') or livepos.get('pos')
        
        # Extract download speed
        speed_down = orchestrator_stats.get('speed_down', 0)
        
        # Track whether livepos is advancing
        livepos_advancing = False
        livepos_stuck_duration = 0
        
        # Check livepos advancement (only if we have livepos data)
        if current_livepos is not None:
            if tracking['last_livepos'] is not None:
                # Check if livepos hasn't advanced
                if current_livepos == tracking['last_livepos']:
                    # Livepos hasn't changed, check how long
                    livepos_stuck_duration = (now - tracking['last_check']).total_seconds()
                    if livepos_stuck_duration > livepos_buffer_tolerance:
                        return f"livepos stuck for {livepos_stuck_duration:.1f}s (tolerance: {livepos_buffer_tolerance}s)"
                else:
                    # Livepos advanced, reset
                    tracking['last_livepos'] = current_livepos
                    tracking['last_check'] = now
                    livepos_advancing = True
            else:
                # First time seeing livepos
                tracking['last_livepos'] = current_livepos
                tracking['last_check'] = now
                livepos_advancing = True
        
        # Check download speed
        # Only mark as dead for zero speed if livepos is also NOT advancing
        if speed_down == 0:
            if tracking['speed_down_zero_since'] is None:
                # Just went to zero
                tracking['speed_down_zero_since'] = now
            else:
                # Has been zero, check how long
                zero_duration = (now - tracking['speed_down_zero_since']).total_seconds()
                # Only fail on zero speed if livepos is also not advancing
                if zero_duration > speed_down_timeout and not livepos_advancing:
                    return f"download speed 0 for {zero_duration:.1f}s and livepos not advancing (timeout: {speed_down_timeout}s)"
        else:
            # Speed is non-zero, reset
            tracking['speed_down_zero_since'] = None
        
        tracking['last_speed_down'] = speed_down
        
        # Stream is healthy
        return None
    
    def _extract_acestream_id(self, url: str) -> Optional[str]:
        """Extract AceStream ID from URL like http://host:port/ace/getstream?id=<id>"""
        match = re.search(r'[?&]id=([a-f0-9]+)', url, re.IGNORECASE)
        return match.group(1) if match else None
    
    def _get_orchestrator_stats(self, acestream_id: str, orchestrator_url: str) -> Optional[Dict]:
        """
        Query Orchestrator /streams endpoint for stream stats.
        Only returns streams with status="started" (active streams).
        
        Example response:
        {
            "id": "74defb8f...|1ddd74d...",
            "key": "74defb8f...",
            "peers": 26,
            "speed_down": 5059,
            "speed_up": 17,
            "downloaded": 203423744,
            "uploaded": 753664,
            "livepos": {...},
            "status": "started"
        }
        """
        try:
            url = f"{orchestrator_url}/streams"
            response = requests.get(url, timeout=5)
            response.raise_for_status()
            
            streams = response.json()
            
            # Find stream by matching acestream_id with key field
            # and filter by status="started" to only check active streams
            for stream_data in streams:
                if stream_data.get('key') == acestream_id:
                    # Check if stream has started status
                    if stream_data.get('status') == 'started':
                        return stream_data
                    else:
                        # Stream exists but not started - don't use it
                        logger.debug(f"Stream {acestream_id} found but status is {stream_data.get('status')}, not 'started'")
                        return None
            
            # Stream not found in Orchestrator (may not be running yet)
            return None
            
        except requests.Timeout:
            logger.warning(f"Timeout querying Orchestrator at {orchestrator_url}")
            return None
        except requests.RequestException as e:
            logger.error(f"Error querying Orchestrator: {e}")
            return None
        except Exception as e:
            logger.error(f"Unexpected error querying Orchestrator: {e}")
            return None
    
    def _ensure_ffmpeg_running(self, stream_id: int, stream_url: str, channel_id: int = None):
        """
        Ensure a continuous FFmpeg process is running for a stream.
        If not running, start it. This keeps the stream alive.
        
        Tracks failures and marks stream as dead after repeated failures.
        
        Args:
            stream_id: Stream ID
            stream_url: Stream URL
            channel_id: Channel ID (optional, for dead stream tracking)
        """
        # Check if stream is marked as dead and should be retried
        if stream_id in self.dead_stream_retry_times:
            retry_interval = self.config.get('dead_stream_retry_interval', 300)  # Default 5 minutes
            last_retry = self.dead_stream_retry_times[stream_id]
            if datetime.now() < last_retry + timedelta(seconds=retry_interval):
                # Not yet time to retry this dead stream
                return
            else:
                # Time to retry - remove from dead retry tracking
                logger.info(f"Retrying dead stream {stream_id} after {retry_interval}s interval")
                del self.dead_stream_retry_times[stream_id]
                # Reset failure count for retry
                if stream_id in self.ffmpeg_failures:
                    del self.ffmpeg_failures[stream_id]
        
        # Check if process is already running and healthy
        if stream_id in self.ffmpeg_processes:
            process = self.ffmpeg_processes[stream_id]
            if process.poll() is None:  # Still running
                return
            else:
                # Process died, track failure
                logger.warning(f"FFmpeg process for stream {stream_id} died")
                
                # Track failure
                if stream_id not in self.ffmpeg_failures:
                    self.ffmpeg_failures[stream_id] = {'failures': 0, 'last_failure': None}
                
                self.ffmpeg_failures[stream_id]['failures'] += 1
                self.ffmpeg_failures[stream_id]['last_failure'] = datetime.now()
                
                failure_count = self.ffmpeg_failures[stream_id]['failures']
                max_failures = self.config.get('max_ffmpeg_failures', 3)  # Default 3 failures before marking dead
                
                if failure_count >= max_failures:
                    # Stream has failed too many times, mark as dead
                    logger.error(f"Stream {stream_id} failed {failure_count} times, marking as dead")
                    stream = self.udi_manager.get_stream_by_id(stream_id)
                    if stream:
                        self.dead_streams_tracker.mark_as_dead(
                            stream_url=stream.get('url', stream_url),
                            stream_id=stream_id,
                            stream_name=stream.get('name', f'Stream {stream_id}'),
                            channel_id=channel_id
                        )
                    
                    # Schedule retry check
                    retry_interval = self.config.get('dead_stream_retry_interval', 300)
                    self.dead_stream_retry_times[stream_id] = datetime.now()
                    logger.info(f"Stream {stream_id} will be retried after {retry_interval}s")
                    
                    # Stop the process and don't restart
                    self._stop_ffmpeg_process(stream_id)
                    return
                else:
                    # Haven't hit max failures yet, restart
                    logger.warning(f"FFmpeg process for stream {stream_id} died (failure {failure_count}/{max_failures}), restarting")
                    self._stop_ffmpeg_process(stream_id)
        
        # Start new continuous FFmpeg process
        try:
            cmd = [
                'ffmpeg',
                '-i', stream_url,
                '-f', 'null',
                '-',
                '-nostats'  # Reduce output noise
            ]
            
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1  # Line buffered
            )
            
            self.ffmpeg_processes[stream_id] = process
            
            # Start thread to read FFmpeg output continuously
            reader_thread = threading.Thread(
                target=self._read_ffmpeg_output,
                args=(stream_id, process),
                daemon=True,
                name=f"FFmpeg-Reader-{stream_id}"
            )
            self.ffmpeg_threads[stream_id] = reader_thread
            reader_thread.start()
            
            logger.info(f"Started continuous FFmpeg process for stream {stream_id}")
            
        except FileNotFoundError:
            logger.error("FFmpeg not found - please install ffmpeg")
        except Exception as e:
            logger.error(f"Error starting FFmpeg process for stream {stream_id}: {e}")
    
    def _read_ffmpeg_output(self, stream_id: int, process: subprocess.Popen):
        """
        Continuously read FFmpeg output and parse stats.
        
        Args:
            stream_id: Stream ID
            process: FFmpeg process
        """
        logger.debug(f"Started FFmpeg output reader for stream {stream_id}")
        
        # Buffer size constants
        BUFFER_MAX_LINES = 100
        BUFFER_KEEP_LINES = 50
        STATS_UPDATE_INTERVAL = 10  # seconds
        
        stderr_buffer = []
        last_stats_update = time.time()
        
        try:
            # Read stderr for stream info (codec, resolution, etc.)
            while not self.shutdown_event.is_set():
                # Check if process is still running
                if process.poll() is not None:
                    logger.warning(f"FFmpeg process for stream {stream_id} ended")
                    break
                
                # Use a short timeout to check periodically
                try:
                    # Set a small timeout on the readline to avoid blocking indefinitely
                    import select
                    import sys
                    
                    # For Unix-like systems, use select for timeout
                    if hasattr(select, 'select'):
                        ready, _, _ = select.select([process.stderr], [], [], 0.5)
                        if not ready:
                            continue
                    
                    line = process.stderr.readline()
                    if not line:
                        # If we get empty line and process is still running, continue
                        if process.poll() is None:
                            continue
                        else:
                            break
                    
                    stderr_buffer.append(line)
                    
                    # Update stats periodically from stderr
                    if time.time() - last_stats_update > STATS_UPDATE_INTERVAL:
                        stderr_text = ''.join(stderr_buffer)
                        stats = self._parse_ffmpeg_stderr(stderr_text)
                        if stats:
                            self.ffmpeg_stats_cache[stream_id] = stats
                            last_stats_update = time.time()
                            logger.debug(f"Updated FFmpeg stats for stream {stream_id}: {stats}")
                    
                    # Keep only recent lines
                    if len(stderr_buffer) > BUFFER_MAX_LINES:
                        stderr_buffer = stderr_buffer[-BUFFER_KEEP_LINES:]
                
                except Exception as read_error:
                    # Log but don't crash on individual read errors
                    logger.debug(f"Error reading line for stream {stream_id}: {read_error}")
                    time.sleep(0.1)
            
        except Exception as e:
            logger.error(f"Error reading FFmpeg output for stream {stream_id}: {e}")
        finally:
            logger.debug(f"FFmpeg output reader stopped for stream {stream_id}")
    
    def _parse_ffmpeg_stderr(self, output: str) -> Optional[Dict]:
        """
        Parse FFmpeg stderr output for stream statistics.
        
        Args:
            output: FFmpeg stderr output
            
        Returns:
            Dictionary with parsed stats or None
        """
        try:
            stats = {
                'bitrate': self._parse_bitrate(output),
                'resolution': self._parse_resolution(output),
                'fps': self._parse_fps(output),
                'codec': self._parse_codec(output),
                'errors': self._count_errors(output)
            }
            
            # Only return if we got at least some valid data
            if any(stats.values()):
                return stats
            return None
            
        except Exception as e:
            logger.error(f"Error parsing FFmpeg output: {e}")
            return None
    
    def _stop_ffmpeg_process(self, stream_id: int):
        """
        Stop the continuous FFmpeg process for a stream.
        
        Args:
            stream_id: Stream ID
        """
        if stream_id in self.ffmpeg_processes:
            try:
                process = self.ffmpeg_processes[stream_id]
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                logger.info(f"Stopped FFmpeg process for stream {stream_id}")
            except Exception as e:
                logger.error(f"Error stopping FFmpeg process for stream {stream_id}: {e}")
            finally:
                del self.ffmpeg_processes[stream_id]
                if stream_id in self.ffmpeg_stats_cache:
                    del self.ffmpeg_stats_cache[stream_id]
                if stream_id in self.ffmpeg_threads:
                    del self.ffmpeg_threads[stream_id]
                # Don't delete failure tracking here - we want to track across restarts
                # Only delete if stream is removed from channel or monitoring stops
    
    def _parse_bitrate(self, output: str) -> Optional[int]:
        """Parse bitrate from FFmpeg output."""
        match = re.search(r'bitrate:\s*(\d+)\s*kb/s', output, re.IGNORECASE)
        return int(match.group(1)) if match else None
    
    def _parse_resolution(self, output: str) -> Optional[str]:
        """Parse resolution from FFmpeg output."""
        match = re.search(r'(\d{3,4})x(\d{3,4})', output)
        return match.group(0) if match else None
    
    def _parse_fps(self, output: str) -> Optional[float]:
        """Parse FPS from FFmpeg output."""
        match = re.search(r'(\d+\.?\d*)\s*fps', output)
        return float(match.group(1)) if match else None
    
    def _parse_codec(self, output: str) -> Optional[str]:
        """Parse video codec from FFmpeg output."""
        match = re.search(r'Video:\s*(\w+)', output)
        return match.group(1) if match else None
    
    def _count_errors(self, output: str) -> int:
        """Count errors in FFmpeg output."""
        error_keywords = ['error', 'corrupt', 'invalid', 'failed']
        count = 0
        for line in output.lower().split('\n'):
            if any(keyword in line for keyword in error_keywords):
                count += 1
        return count
    
    def _calculate_health_score(
        self, 
        orchestrator_stats: Optional[Dict], 
        monitoring_stats: Optional[Dict]
    ) -> float:
        """
        Calculate health score (0-100) based on available metrics.
        
        Works with both FFmpeg and HTTP monitoring stats.
        
        Scoring factors:
        - Peers count (more = better)
        - Download speed (higher = better)
        - Upload speed (should be reasonable)
        - Stream monitoring stats (bitrate, errors, or alive status)
        - Stream availability (working = better)
        """
        score = 0.0
        
        # Base score for having orchestrator stats
        if orchestrator_stats:
            # Peers score (0-25 points)
            peers = orchestrator_stats.get('peers', 0)
            score += min(peers * 1.0, 25)
            
            # Download speed score (0-25 points)
            # Assume good speed is 5000+ KB/s
            speed_down = orchestrator_stats.get('speed_down', 0)
            score += min((speed_down / 5000) * 25, 25)
            
            # Upload contribution (0-10 points)
            # Having some upload is good
            speed_up = orchestrator_stats.get('speed_up', 0)
            if speed_up > 0:
                score += min(speed_up / 5, 10)
        
        # Monitoring stats score (FFmpeg or HTTP)
        if monitoring_stats:
            method = monitoring_stats.get('method', 'ffmpeg')
            
            if method == 'http':
                # HTTP monitoring stats
                # Stream is working (20 points)
                if monitoring_stats.get('is_alive', False):
                    score += 20
                
                # Minimal failures is good (0-15 points)
                failures = monitoring_stats.get('failures', 0)
                if failures == 0:
                    score += 15
                elif failures == 1:
                    score += 10
                elif failures == 2:
                    score += 5
                
                # Penalty for errors (-5 points per failure beyond 2)
                if failures > 2:
                    score -= min((failures - 2) * 5, 20)
            else:
                # FFmpeg stats
                # Stream is working (20 points)
                score += 20
                
                # Bitrate score (0-15 points)
                # Assume good bitrate is 3000+ kbps
                bitrate = monitoring_stats.get('bitrate', 0) or 0
                score += min((bitrate / 3000) * 15, 15)
                
                # Penalty for errors (-5 points per error, max -20)
                errors = monitoring_stats.get('errors', 0)
                score -= min(errors * 5, 20)
        
        # Ensure score is between 0 and 100
        return max(0, min(score, 100))
    
    def _reorder_streams_by_health(self, channel, stream_health: List[Tuple]):
        """
        Reorder streams in Dispatcharr channel based on health scores.
        
        Args:
            channel: Channel dict
            stream_health: List of (stream, health_dict) tuples
        """
        if not channel:
            logger.error("Cannot reorder streams: channel is None")
            return
            
        try:
            channel_id = channel.get('id')
            if not channel_id:
                logger.error("Cannot reorder streams: channel has no ID")
                return
            
            # Sort by health score (descending)
            sorted_streams = sorted(
                stream_health,
                key=lambda x: x[1]['health_score'],
                reverse=True
            )
            
            # Get stream IDs in new order
            new_order = [s[0].get('id') for s in sorted_streams]
            
            # Only update if order has changed
            current_order = channel.get('streams', [])
            if new_order != current_order:
                # Update in Dispatcharr via API
                from api_utils import update_channel_streams
                try:
                    api_success = update_channel_streams(channel_id, new_order, allow_dead_streams=False)
                    if api_success:
                        # Update in UDI cache after successful API update
                        channel_data = dict(channel)
                        channel_data['streams'] = new_order
                        self.udi_manager.update_channel(channel_id, channel_data)
                        
                        logger.info(
                            f"Reordered {len(new_order)} streams for channel {channel_id} "
                            f"by health (best: {sorted_streams[0][1]['health_score']:.1f})"
                        )
                    else:
                        logger.warning(f"Failed to update channel {channel_id} stream order in Dispatcharr")
                except Exception as api_error:
                    logger.error(f"Error updating channel {channel_id} in Dispatcharr: {api_error}")
            
        except Exception as e:
            # Safely get channel ID for error message
            channel_id = channel.get('id') if channel else 'unknown'
            logger.error(f"Error reordering streams for channel {channel_id}: {e}")
    
    def stop_monitoring_channel(self, channel_id: int):
        """Stop monitoring a specific channel."""
        if channel_id in self.monitoring_threads:
            logger.info(f"Stopping monitoring for channel {channel_id}")
            # Thread will stop on next iteration when it checks shutdown_event
            # We could add per-channel events if needed, but for now this is simple
    
    def refresh_channels(self):
        """Refresh the list of monitored channels (start new, stop removed)."""
        try:
            current_channels = self._get_acestream_channels()
            current_ids = {ch.get('id') for ch in current_channels}
            monitored_ids = set(self.monitoring_threads.keys())
            
            # Start monitoring for new channels
            for channel in current_channels:
                channel_id = channel.get('id')
                if channel_id not in monitored_ids:
                    self._start_channel_monitoring(channel)
            
            # Stop monitoring for channels that are no longer AceStream
            removed_ids = monitored_ids - current_ids
            for channel_id in removed_ids:
                logger.info(f"Channel {channel_id} is no longer AceStream, stopping monitoring")
                # Thread will stop on next iteration when it checks the channel list
                # and doesn't find itself. We could set a per-channel event here
                # for immediate stopping, but for simplicity we let it exit naturally.
                
                # Remove from tracking (thread will exit on next iteration)
                if channel_id in self.monitoring_threads:
                    # Note: Thread will stop itself when it checks _get_acestream_channels
                    # and doesn't find itself in the list anymore
                    pass
            
        except Exception as e:
            logger.error(f"Error refreshing channels: {e}")
    
    def _stop_stream_keepalive(self, stream_id: int):
        """
        Stop stream keep-alive (FFmpeg or HTTP) for a stream.
        
        Args:
            stream_id: Stream ID
        """
        if self.monitoring_method == 'http':
            self.http_keepalive.stop_keepalive(stream_id)
        else:  # ffmpeg
            self._stop_ffmpeg_process(stream_id)
    
    def shutdown(self):
        """Gracefully shutdown monitoring and cleanup resources."""
        logger.info("Shutting down AceStream monitoring service...")
        
        self.running = False
        
        # Signal all threads to stop
        self.shutdown_event.set()
        
        # Stop all stream keep-alive (FFmpeg or HTTP)
        logger.info(f"Stopping all stream keep-alive processes ({self.monitoring_method} method)...")
        if self.monitoring_method == 'http':
            self.http_keepalive.stop_all()
        else:  # ffmpeg
            for stream_id in list(self.ffmpeg_processes.keys()):
                self._stop_ffmpeg_process(stream_id)
        
        # Send cleanup requests to Orchestrator
        self._cleanup_orchestrator_sessions()
        
        # Wait for threads to finish (with timeout)
        for channel_id, thread in self.monitoring_threads.items():
            thread.join(timeout=5)
            if thread.is_alive():
                logger.warning(f"Thread for channel {channel_id} did not stop in time")
        
        # Close active sessions in database
        self.db.close_all_active_sessions()
        
        # Clear monitoring threads
        self.monitoring_threads.clear()
        
        logger.info("AceStream monitoring service shutdown complete")
    
    def _cleanup_orchestrator_sessions(self):
        """Send cleanup requests to command_url for all active sessions."""
        logger.info("Cleaning up Orchestrator sessions...")
        
        try:
            # Get all AceStream channels to get their orchestrator URLs
            channels = self._get_acestream_channels()
            
            for channel in channels:
                channel_id = channel.get('id')
                orchestrator_url = channel.get('acestream_orchestrator_url') or self.default_orchestrator_url
                
                # Get active sessions for this channel
                sessions = self.db.get_active_sessions_for_channel(channel_id)
                
                if not sessions:
                    continue
                
                # Get current stream data from Orchestrator
                try:
                    url = f"{orchestrator_url}/streams"
                    response = requests.get(url, timeout=5)
                    response.raise_for_status()
                    streams = response.json()
                except Exception as e:
                    logger.error(f"Error getting streams from Orchestrator: {e}")
                    continue
                
                # Send stop command for each session
                for session in sessions:
                    acestream_id = session['acestream_id']
                    
                    # Find matching stream in orchestrator data
                    for stream_data in streams:
                        if stream_data.get('key') == acestream_id:
                            command_url = stream_data.get('command_url')
                            if command_url:
                                try:
                                    # Send stop command
                                    requests.post(f"{command_url}?method=stop", timeout=5)
                                    logger.info(f"Sent stop command for stream {acestream_id}")
                                except Exception as e:
                                    logger.error(f"Error sending stop command: {e}")
                            break
            
        except Exception as e:
            logger.error(f"Error during Orchestrator cleanup: {e}")
    
    def get_status(self) -> Dict[str, Any]:
        """Get current monitoring status."""
        return {
            'running': self.running,
            'active_channels': len(self.monitoring_threads),
            'monitored_channel_ids': list(self.monitoring_threads.keys())
        }
