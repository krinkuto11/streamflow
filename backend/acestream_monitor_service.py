"""
AceStream Monitoring Service

Continuous monitoring service for AceStream channels with health tracking,
FFmpeg-based stream monitoring, and automatic stream ordering.
"""

import re
import requests
import subprocess
import threading
import time
from typing import Dict, List, Optional, Tuple, Any
from datetime import datetime

from acestream_db import AceStreamDatabase
from logging_config import setup_logging

logger = setup_logging(__name__)


class AceStreamMonitor:
    """
    Continuous monitoring service for AceStream channels.
    
    Features:
    - FFmpeg-based stream monitoring
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
            config: Configuration dictionary with monitoring_interval and ffmpeg_probe_duration
        """
        self.udi_manager = udi_manager
        self.default_orchestrator_url = default_orchestrator_url
        self.config = config or {}
        self.db = AceStreamDatabase()
        
        self.monitoring_threads: Dict[int, threading.Thread] = {}
        self.shutdown_event = threading.Event()
        self.running = False
        
        logger.info("AceStream monitor initialized")
    
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
            all_channels = self.udi_manager.get_all_channels()
            acestream_channels = [ch for ch in all_channels if getattr(ch, 'is_acestream', False)]
            return acestream_channels
        except Exception as e:
            logger.error(f"Error getting AceStream channels: {e}")
            return []
    
    def _start_channel_monitoring(self, channel):
        """Start monitoring a specific channel."""
        if channel.id in self.monitoring_threads:
            logger.debug(f"Channel {channel.id} already being monitored")
            return
        
        thread = threading.Thread(
            target=self._monitor_channel,
            args=(channel,),
            daemon=True,
            name=f"AceStreamMonitor-Channel{channel.id}"
        )
        self.monitoring_threads[channel.id] = thread
        thread.start()
        logger.info(f"Started monitoring thread for channel {channel.id}: {channel.name}")
    
    def _monitor_channel(self, channel):
        """Main monitoring loop for a channel."""
        logger.info(f"Starting monitoring for channel {channel.id}: {channel.name}")
        
        while not self.shutdown_event.is_set():
            try:
                # Get channel streams
                streams = self._get_channel_streams(channel)
                
                if not streams:
                    logger.debug(f"No streams found for channel {channel.id}")
                    time.sleep(60)
                    continue
                
                # Get orchestrator URL (channel-specific or default)
                orchestrator_url = getattr(channel, 'acestream_orchestrator_url', None) or self.default_orchestrator_url
                
                # Monitor each stream and collect health data
                stream_health = []
                
                for stream in streams:
                    if self.shutdown_event.is_set():
                        break
                    
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
                logger.error(f"Error monitoring channel {channel.id}: {e}", exc_info=True)
                self.shutdown_event.wait(60)
        
        logger.info(f"Stopped monitoring channel {channel.id}")
    
    def _get_channel_streams(self, channel) -> List[Any]:
        """Get streams for a channel from UDI."""
        try:
            stream_ids = channel.streams if hasattr(channel, 'streams') else []
            streams = []
            for stream_id in stream_ids:
                stream = self.udi_manager.get_stream(stream_id)
                if stream:
                    streams.append(stream)
            return streams
        except Exception as e:
            logger.error(f"Error getting streams for channel {channel.id}: {e}")
            return []
    
    def _check_stream_health(self, channel, stream, orchestrator_url: str) -> Optional[Dict]:
        """
        Check stream health by combining FFmpeg stats and Orchestrator data.
        
        Returns health metrics dict or None if check failed.
        """
        try:
            # Extract AceStream ID from URL
            acestream_id = self._extract_acestream_id(stream.url)
            if not acestream_id:
                logger.debug(f"Cannot extract AceStream ID from URL: {stream.url}")
                return None
            
            # Get stats from Orchestrator
            orchestrator_stats = self._get_orchestrator_stats(acestream_id, orchestrator_url)
            
            # Get FFmpeg stats (lightweight check - just probe, don't download much)
            ffmpeg_stats = self._get_ffmpeg_stats(stream.url)
            
            # Calculate health score
            health_score = self._calculate_health_score(
                orchestrator_stats, 
                ffmpeg_stats
            )
            
            # Get or create session and save metrics
            session = self.db.get_active_session(stream.id)
            if session:
                session_id = session['id']
            else:
                session_id = self.db.create_session(
                    stream_id=stream.id,
                    channel_id=channel.id,
                    acestream_id=acestream_id
                )
            
            self.db.save_metrics(session_id, health_score, orchestrator_stats, ffmpeg_stats)
            
            return {
                'stream_id': stream.id,
                'acestream_id': acestream_id,
                'health_score': health_score,
                'orchestrator_stats': orchestrator_stats,
                'ffmpeg_stats': ffmpeg_stats
            }
            
        except Exception as e:
            logger.error(f"Error checking stream {stream.id} health: {e}")
            return None
    
    def _extract_acestream_id(self, url: str) -> Optional[str]:
        """Extract AceStream ID from URL like http://host:port/ace/getstream?id=<id>"""
        match = re.search(r'[?&]id=([a-f0-9]+)', url, re.IGNORECASE)
        return match.group(1) if match else None
    
    def _get_orchestrator_stats(self, acestream_id: str, orchestrator_url: str) -> Optional[Dict]:
        """
        Query Orchestrator /streams endpoint for stream stats.
        
        Example response:
        {
            "id": "74defb8f...|1ddd74d...",
            "key": "74defb8f...",
            "peers": 26,
            "speed_down": 5059,
            "speed_up": 17,
            "downloaded": 203423744,
            "uploaded": 753664,
            "livepos": {...}
        }
        """
        try:
            url = f"{orchestrator_url}/streams"
            response = requests.get(url, timeout=5)
            response.raise_for_status()
            
            streams = response.json()
            
            # Find stream by matching acestream_id with key field
            for stream_data in streams:
                if stream_data.get('key') == acestream_id:
                    return stream_data
            
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
    
    def _get_ffmpeg_stats(self, stream_url: str, duration: Optional[int] = None) -> Optional[Dict]:
        """
        Use FFmpeg to probe stream and get basic stats.
        Keep it lightweight - just enough to verify stream is working.
        
        Args:
            stream_url: Stream URL to probe
            duration: Duration in seconds (uses config if not specified)
        """
        if duration is None:
            duration = self.config.get('ffmpeg_probe_duration', 5)
        
        try:
            # Use ffmpeg with limited duration to minimize resource usage
            cmd = [
                'ffmpeg',
                '-i', stream_url,
                '-t', str(duration),
                '-f', 'null',
                '-'
            ]
            
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=duration + 10
            )
            
            # Parse ffmpeg output for stats
            output = result.stderr
            
            stats = {
                'bitrate': self._parse_bitrate(output),
                'resolution': self._parse_resolution(output),
                'fps': self._parse_fps(output),
                'codec': self._parse_codec(output),
                'errors': self._count_errors(output)
            }
            
            return stats
            
        except subprocess.TimeoutExpired:
            logger.warning(f"FFmpeg timeout for URL: {stream_url}")
            return None
        except FileNotFoundError:
            logger.error("FFmpeg not found - please install ffmpeg")
            return None
        except Exception as e:
            logger.error(f"Error getting FFmpeg stats: {e}")
            return None
    
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
        ffmpeg_stats: Optional[Dict]
    ) -> float:
        """
        Calculate health score (0-100) based on available metrics.
        
        Scoring factors:
        - Peers count (more = better)
        - Download speed (higher = better)
        - Upload speed (should be reasonable)
        - FFmpeg bitrate (higher = better, within reason)
        - FFmpeg errors (fewer = better)
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
        
        # FFmpeg stats score
        if ffmpeg_stats:
            # Stream is working (20 points)
            score += 20
            
            # Bitrate score (0-15 points)
            # Assume good bitrate is 3000+ kbps
            bitrate = ffmpeg_stats.get('bitrate', 0) or 0
            score += min((bitrate / 3000) * 15, 15)
            
            # Penalty for errors (-5 points per error, max -20)
            errors = ffmpeg_stats.get('errors', 0)
            score -= min(errors * 5, 20)
        
        # Ensure score is between 0 and 100
        return max(0, min(score, 100))
    
    def _reorder_streams_by_health(self, channel, stream_health: List[Tuple]):
        """
        Reorder streams in Dispatcharr channel based on health scores.
        
        Args:
            channel: Channel object
            stream_health: List of (stream, health_dict) tuples
        """
        try:
            # Sort by health score (descending)
            sorted_streams = sorted(
                stream_health,
                key=lambda x: x[1]['health_score'],
                reverse=True
            )
            
            # Get stream IDs in new order
            new_order = [s[0].id for s in sorted_streams]
            
            # Only update if order has changed
            current_order = channel.streams if hasattr(channel, 'streams') else []
            if new_order != current_order:
                # Update channel via UDI manager
                channel_data = channel.to_dict() if hasattr(channel, 'to_dict') else {
                    'id': channel.id,
                    'streams': new_order
                }
                channel_data['streams'] = new_order
                
                # Update in UDI cache
                success = self.udi_manager.update_channel(channel.id, channel_data)
                
                if success:
                    logger.info(
                        f"Reordered {len(new_order)} streams for channel {channel.id} "
                        f"by health (best: {sorted_streams[0][1]['health_score']:.1f})"
                    )
                else:
                    logger.warning(f"Failed to update channel {channel.id} stream order in UDI")
            
        except Exception as e:
            logger.error(f"Error reordering streams for channel {channel.id}: {e}")
    
    def stop_monitoring_channel(self, channel_id: int):
        """Stop monitoring a specific channel."""
        if channel_id in self.monitoring_threads:
            logger.info(f"Stopping monitoring for channel {channel_id}")
            # Thread will stop on next iteration when it checks shutdown_event
            # We could add per-channel events if needed, but for now this is simple
    
    def refresh_channels(self):
        """Refresh the list of monitored channels (start new, keep existing)."""
        try:
            current_channels = self._get_acestream_channels()
            current_ids = {ch.id for ch in current_channels}
            monitored_ids = set(self.monitoring_threads.keys())
            
            # Start monitoring for new channels
            for channel in current_channels:
                if channel.id not in monitored_ids:
                    self._start_channel_monitoring(channel)
            
            # Note: We don't stop threads for channels that are no longer AceStream
            # They will stop on next iteration when they don't find themselves in the list
            
        except Exception as e:
            logger.error(f"Error refreshing channels: {e}")
    
    def shutdown(self):
        """Gracefully shutdown monitoring and cleanup resources."""
        logger.info("Shutting down AceStream monitoring service...")
        
        self.running = False
        
        # Signal all threads to stop
        self.shutdown_event.set()
        
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
                orchestrator_url = getattr(channel, 'acestream_orchestrator_url', None) or self.default_orchestrator_url
                
                # Get active sessions for this channel
                sessions = self.db.get_active_sessions_for_channel(channel.id)
                
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
