"""
AceStream HTTP-based Monitoring

Alternative to ffmpeg-based monitoring using lightweight HTTP range requests
to keep streams alive in the orchestrator while consuming minimal resources.

This approach:
1. Makes periodic HTTP range requests to the stream URL
2. Requests small chunks (e.g., 64KB) to verify stream is alive
3. Detects stream health issues (EOF, connection errors, timeouts)
4. Uses significantly less CPU/memory than continuous ffmpeg processes
5. Still provides health metrics from orchestrator /streams endpoint
"""

import requests
import threading
import time
from typing import Dict, Optional, Any
from datetime import datetime, timedelta
from logging_config import setup_logging

logger = setup_logging(__name__)


class HTTPStreamKeepAlive:
    """
    Lightweight HTTP-based stream keep-alive mechanism.
    
    Instead of using ffmpeg to continuously consume stream data,
    this class makes periodic HTTP range requests to:
    1. Keep the stream registered in orchestrator's /streams endpoint
    2. Detect dead/broken streams (connection errors, EOF, timeouts)
    3. Consume minimal resources (only small chunks periodically)
    """
    
    def __init__(self):
        """Initialize the HTTP keep-alive manager."""
        # Track active stream connections
        # Format: {stream_id: {'thread': Thread, 'stop_event': Event, 'stats': Dict}}
        self.active_streams: Dict[int, Dict[str, Any]] = {}
        
        # Track stream health and failures
        # Format: {stream_id: {'failures': int, 'last_failure': datetime, 'last_success': datetime}}
        self.stream_health: Dict[int, Dict[str, Any]] = {}
        
    def start_keepalive(
        self,
        stream_id: int,
        stream_url: str,
        interval: int = 10,
        chunk_size: int = 65536  # 64KB chunks
    ):
        """
        Start HTTP keep-alive for a stream.
        
        Args:
            stream_id: Stream ID
            stream_url: Stream URL to request
            interval: Seconds between requests (default: 10)
            chunk_size: Bytes to request per chunk (default: 64KB)
        """
        if stream_id in self.active_streams:
            logger.debug(f"Stream {stream_id} already has active keep-alive")
            return
        
        # Create stop event for this stream
        stop_event = threading.Event()
        
        # Start keep-alive thread
        thread = threading.Thread(
            target=self._keepalive_loop,
            args=(stream_id, stream_url, interval, chunk_size, stop_event),
            daemon=True,
            name=f"HTTPKeepAlive-{stream_id}"
        )
        
        self.active_streams[stream_id] = {
            'thread': thread,
            'stop_event': stop_event,
            'stats': {
                'url': stream_url,
                'started_at': datetime.now(),
                'requests_sent': 0,
                'bytes_received': 0,
                'last_request_time': None,
                'last_success_time': None,
                'last_error': None
            }
        }
        
        # Initialize health tracking
        self.stream_health[stream_id] = {
            'failures': 0,
            'last_failure': None,
            'last_success': None,
            'is_alive': True
        }
        
        thread.start()
        logger.info(f"Started HTTP keep-alive for stream {stream_id} (interval: {interval}s, chunk: {chunk_size} bytes)")
    
    def stop_keepalive(self, stream_id: int):
        """
        Stop HTTP keep-alive for a stream.
        
        Args:
            stream_id: Stream ID
        """
        if stream_id not in self.active_streams:
            logger.debug(f"Stream {stream_id} has no active keep-alive to stop")
            return
        
        # Signal thread to stop
        self.active_streams[stream_id]['stop_event'].set()
        
        # Wait for thread to finish (with timeout)
        thread = self.active_streams[stream_id]['thread']
        thread.join(timeout=5)
        
        if thread.is_alive():
            logger.warning(f"Keep-alive thread for stream {stream_id} did not stop in time")
        
        # Clean up
        del self.active_streams[stream_id]
        logger.info(f"Stopped HTTP keep-alive for stream {stream_id}")
    
    def _keepalive_loop(
        self,
        stream_id: int,
        stream_url: str,
        interval: int,
        chunk_size: int,
        stop_event: threading.Event
    ):
        """
        Main keep-alive loop that periodically requests stream chunks.
        
        This runs in a separate thread for each stream.
        """
        logger.debug(f"Keep-alive loop started for stream {stream_id}")
        
        # Use a session for connection pooling
        session = requests.Session()
        
        # Current byte position in stream
        byte_position = 0
        
        # Consecutive failures counter
        consecutive_failures = 0
        max_consecutive_failures = 3
        
        try:
            while not stop_event.is_set():
                request_start = datetime.now()
                stats = self.active_streams[stream_id]['stats']
                stats['last_request_time'] = request_start
                stats['requests_sent'] += 1
                
                try:
                    # Make HTTP range request for a small chunk
                    # This simulates a player consuming data without actually processing it
                    headers = {
                        'Range': f'bytes={byte_position}-{byte_position + chunk_size - 1}',
                        'User-Agent': 'AceStream/3.1.0'
                    }
                    
                    response = session.get(
                        stream_url,
                        headers=headers,
                        timeout=10,
                        stream=True  # Stream mode to avoid loading all into memory
                    )
                    
                    # Check response status
                    if response.status_code in (200, 206):  # OK or Partial Content
                        # Read the chunk
                        chunk_data = response.content
                        bytes_received = len(chunk_data)
                        
                        if bytes_received == 0:
                            # EOF or stream ended
                            logger.warning(f"Stream {stream_id} reached EOF (no data received)")
                            self._handle_stream_failure(
                                stream_id,
                                'eof',
                                "Stream reached end of file"
                            )
                            consecutive_failures += 1
                        else:
                            # Success - update stats
                            stats['bytes_received'] += bytes_received
                            stats['last_success_time'] = datetime.now()
                            byte_position += bytes_received
                            
                            # Update health tracking
                            self.stream_health[stream_id]['last_success'] = datetime.now()
                            self.stream_health[stream_id]['is_alive'] = True
                            self.stream_health[stream_id]['failures'] = 0
                            consecutive_failures = 0
                            
                            logger.debug(
                                f"Stream {stream_id} keep-alive: received {bytes_received} bytes "
                                f"(total: {stats['bytes_received']} bytes, position: {byte_position})"
                            )
                    else:
                        # Unexpected status code
                        error_msg = f"HTTP {response.status_code}"
                        logger.warning(f"Stream {stream_id} returned {error_msg}")
                        self._handle_stream_failure(stream_id, 'http_error', error_msg)
                        consecutive_failures += 1
                    
                    response.close()
                    
                except requests.Timeout:
                    logger.warning(f"Stream {stream_id} request timed out")
                    self._handle_stream_failure(stream_id, 'timeout', "Request timed out")
                    consecutive_failures += 1
                    
                except requests.ConnectionError as e:
                    logger.warning(f"Stream {stream_id} connection error: {e}")
                    self._handle_stream_failure(stream_id, 'connection_error', str(e))
                    consecutive_failures += 1
                    
                except Exception as e:
                    logger.error(f"Stream {stream_id} unexpected error: {e}")
                    self._handle_stream_failure(stream_id, 'unknown_error', str(e))
                    consecutive_failures += 1
                
                # Check if we've had too many consecutive failures
                if consecutive_failures >= max_consecutive_failures:
                    logger.error(
                        f"Stream {stream_id} has {consecutive_failures} consecutive failures, "
                        f"marking as dead"
                    )
                    self.stream_health[stream_id]['is_alive'] = False
                    break
                
                # Wait for next interval (unless stopping)
                stop_event.wait(interval)
                
        except Exception as e:
            logger.error(f"Fatal error in keep-alive loop for stream {stream_id}: {e}", exc_info=True)
        finally:
            session.close()
            logger.debug(f"Keep-alive loop ended for stream {stream_id}")
    
    def _handle_stream_failure(self, stream_id: int, error_type: str, error_msg: str):
        """
        Handle a stream failure.
        
        Args:
            stream_id: Stream ID
            error_type: Type of error (eof, timeout, connection_error, etc.)
            error_msg: Error message
        """
        if stream_id in self.active_streams:
            stats = self.active_streams[stream_id]['stats']
            stats['last_error'] = {
                'type': error_type,
                'message': error_msg,
                'time': datetime.now()
            }
        
        if stream_id in self.stream_health:
            self.stream_health[stream_id]['failures'] += 1
            self.stream_health[stream_id]['last_failure'] = datetime.now()
    
    def get_stream_stats(self, stream_id: int) -> Optional[Dict]:
        """
        Get statistics for a stream's keep-alive.
        
        Args:
            stream_id: Stream ID
            
        Returns:
            Dict with stats or None if stream not found
        """
        if stream_id not in self.active_streams:
            return None
        
        stats = self.active_streams[stream_id]['stats'].copy()
        health = self.stream_health.get(stream_id, {})
        
        return {
            'stats': stats,
            'health': health
        }
    
    def get_stream_health(self, stream_id: int) -> Optional[Dict]:
        """
        Get health information for a stream.
        
        Args:
            stream_id: Stream ID
            
        Returns:
            Dict with health info or None if stream not tracked
        """
        return self.stream_health.get(stream_id)
    
    def is_stream_alive(self, stream_id: int) -> bool:
        """
        Check if a stream is considered alive.
        
        Args:
            stream_id: Stream ID
            
        Returns:
            True if stream is alive, False otherwise
        """
        health = self.stream_health.get(stream_id)
        if not health:
            return False
        
        return health.get('is_alive', False)
    
    def stop_all(self):
        """Stop all active keep-alive threads."""
        logger.info(f"Stopping all HTTP keep-alive threads ({len(self.active_streams)} active)")
        
        # Get all stream IDs to stop
        stream_ids = list(self.active_streams.keys())
        
        for stream_id in stream_ids:
            self.stop_keepalive(stream_id)
        
        logger.info("All HTTP keep-alive threads stopped")
