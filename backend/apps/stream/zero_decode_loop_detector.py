import logging
import subprocess
import threading
import time
from typing import Optional, List, Callable

from apps.core.logging_config import setup_logging

logger = setup_logging(__name__)

class ZeroDecodeLoopDetector:
    """
    Zero-decode loop detector for video streams using packet sizes.
    Uses ffprobe to extract PTS times and compressed packet sizes, creating
    a fingerprint of the initial segment of the stream. It continuously scans
    incoming packets to find an exact repetition of this fingerprint.
    """
    
    def __init__(self, url: str, session_id: str, stream_id: str, on_loop_detected: Callable[[float], None]):
        self.url = url
        self.session_id = session_id
        self.stream_id = stream_id
        self.on_loop_detected = on_loop_detected
        
        self._process: Optional[subprocess.Popen] = None
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        
        # Buffer configuration
        self.buffer_duration_s = 60.0
        self.fingerprint_duration_s = 10.0  # 10 seconds of fingerprint
        
        # State
        self.packet_sizes: List[int] = []
        self.pts_times: List[float] = []
        
        self.fingerprint_sizes: List[int] = []
        self.fingerprint_locked = False
        
    def start(self):
        """Starts the ffprobe subprocess and reading thread in background."""
        if self._thread and self._thread.is_alive():
            return
            
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_loop,
            name=f"ZeroDecodeLoop-{self.stream_id}",
            daemon=True
        )
        self._thread.start()
        logger.info(f"Started ZeroDecodeLoopDetector for stream {self.stream_id} (session: {self.session_id})")
        
    def stop(self):
        """Signals the detector to stop and kills the ffprobe process."""
        self._stop_event.set()
        if self._process:
            try:
                self._process.terminate()
            except Exception:
                pass
        
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)
            
        logger.debug(f"Stopped ZeroDecodeLoopDetector for stream {self.stream_id}")
            
    def _run_loop(self):
        cmd = [
            'ffprobe',
            '-v', 'warning',
            '-user_agent', 'VLC/3.0.14',
            '-analyzeduration', '15000000',
            '-probesize', '15000000',
            '-select_streams', 'v:0',
            '-show_entries', 'packet=pts_time,size',
            '-of', 'csv=p=0',
            self.url
        ]
        
        try:
            self._process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1
            )
            
            # Read stdout line by line
            for line in iter(self._process.stdout.readline, ''):
                if self._stop_event.is_set():
                    break
                    
                line = line.strip()
                if not line:
                    continue
                    
                parts = line.split(',')
                if len(parts) != 2:
                    continue
                    
                try:
                    pts = float(parts[0])
                    size = int(parts[1])
                except ValueError:
                    continue
                    
                self._process_packet(pts, size)
                
                # If we haven't locked fingerprint yet, check if we have enough data
                if not self.fingerprint_locked and self.pts_times:
                    duration = self.pts_times[-1] - self.pts_times[0]
                    if duration >= self.fingerprint_duration_s:
                        self.fingerprint_sizes = list(self.packet_sizes)
                        self.fingerprint_locked = True
                        logger.debug(f"Fingerprint locked for stream {self.stream_id} with {len(self.fingerprint_sizes)} packets")
                        
                # Ensure rolling buffer is no longer than buffer_duration_s
                if self.pts_times:
                    while len(self.pts_times) > 0 and (self.pts_times[-1] - self.pts_times[0] > self.buffer_duration_s):
                        self.pts_times.pop(0)
                        self.packet_sizes.pop(0)
                        
                # Check for loop
                if self.fingerprint_locked:
                    self._check_for_loop()
                    
        except Exception as e:
            if not self._stop_event.is_set():
                logger.error(f"Error in ZeroDecodeLoopDetector for stream {self.stream_id}: {e}")
        finally:
            if self._process:
                try:
                    self._process.terminate()
                    self._process.wait(timeout=1.0)
                except Exception:
                    try:
                        self._process.kill()
                    except Exception:
                        pass
                self._process = None

    def _process_packet(self, pts: float, size: int):
        self.pts_times.append(pts)
        self.packet_sizes.append(size)

    def _check_for_loop(self):
        """Scans the current buffer (after the original fingerprint window) for an exact match."""
        if not self.fingerprint_locked or not self.fingerprint_sizes:
            return
            
        fp_len = len(self.fingerprint_sizes)
        # Sequence must be at least somewhat strictly matched to avoid false positives
        if fp_len < 10:
            return
            
        # We need enough packets to contain the fingerprint again AFTER the original footprint
        total_len = len(self.packet_sizes)
        if total_len <= fp_len * 2:
            return
            
        # We only check from index fp_len to ensure we don't match the original occurrence
        # Optimization: scan for the first element of fingerprint
        fp_first = self.fingerprint_sizes[0]
        
        for i in range(fp_len, total_len - fp_len + 1):
            if self.packet_sizes[i] == fp_first:
                # Potential match, verifiable
                match = True
                for j in range(1, fp_len):
                    if self.packet_sizes[i + j] != self.fingerprint_sizes[j]:
                        match = False
                        break
                        
                if match:
                    # Calculate loop duration
                    # i is the index where the repeated fingerprint starts
                    # The original fingerprint started at index 0.
                    loop_duration = self.pts_times[i] - self.pts_times[0]
                    logger.warning(f"Zero-decode loop detected on stream {self.stream_id}! Duration: {loop_duration:.2f}s")
                    self._stop_event.set()
                    self.on_loop_detected(loop_duration)
                    return
