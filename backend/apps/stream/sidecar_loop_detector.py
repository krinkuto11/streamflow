import logging
import time
import collections
import io
from typing import Optional
from PIL import Image
try:
    import imagehash
except ImportError:
    # Fallback if imagehash is not available (though it should be in requirements)
    imagehash = None

from apps.core.logging_config import setup_logging

logger = setup_logging(__name__)

# Constants
SEQUENCE_LENGTH = 3
HAMMING_TOLERANCE = 5
LOOP_DURATION_THRESHOLD = 10.0
BUFFER_MAXLEN = 300  # ~5 minutes at 1fps
STALE_THRESHOLD = 10.0

class SidecarLoopDetector:
    """
    Sidecar loop detector that reads PPM frames from a pipe and detects content loops.
    """
    
    def __init__(self, pipe, stream_id: Optional[int] = None):
        """
        Initialize the loop detector.
        
        Args:
            pipe: A file-like object (e.g., subprocess.stdout) yielding raw PPM frames.
            stream_id: Stream ID for logging context.
        """
        self.pipe = pipe
        self.stream_id = stream_id
        self.buffer = collections.deque(maxlen=BUFFER_MAXLEN)
        self.last_frame_time = 0.0
        self.last_log_time = 0.0
        self.is_closed = False
        self._is_looping = False
        self._loop_duration = 0.0
        
    def is_looping(self) -> bool:
        """Returns True if the detector currently assesses the stream is continuously looping."""
        return self._is_looping

    def get_loop_duration(self) -> float:
        """Returns the duration of the last detected loop."""
        return self._loop_duration
        
    def run(self):
        """
        Continuously read from the pipe and process frames.
        This should be run in a separate thread.
        """
        stream_ctx = f"stream {self.stream_id}" if self.stream_id else "unknown stream"
        logger.info(f"SidecarLoopDetector worker started for {stream_ctx}")
        try:
            last_process_time = 0.0
            while not self.is_closed:
                frame_data = self._read_ppm_frame()
                if not frame_data:
                    logger.info("Pipe EOF or empty read, closing detector")
                    break
                
                timestamp = time.monotonic()
                self.last_frame_time = timestamp
                
                # Frame dropping: Only process roughly 1 FPS
                if timestamp - last_process_time < 1.0:
                    continue  # Skip processing for this frame
                
                last_process_time = timestamp
                
                try:
                    # Parse PPM frame
                    img = Image.open(io.BytesIO(frame_data))
                    
                    # Generate pHash
                    if imagehash:
                        h = imagehash.phash(img)
                    else:
                        h = self._simple_hash(img)
                    
                    self.buffer.append((timestamp, h))
                    
                    # Detect loop
                    detected_duration = self.detect_loop()
                    if detected_duration:
                        self._is_looping = True
                        self._loop_duration = detected_duration
                        # Debounce logging to prevent spam (e.g., max once per minute)
                        if timestamp - self.last_log_time > 60.0:
                            logger.warning(f"Loop detected in {stream_ctx}. Duration: {detected_duration:.2f}s")
                            self.last_log_time = timestamp
                    else:
                        self._is_looping = False
                        self._loop_duration = 0.0
                    
                except Exception as e:
                    logger.error(f"Error processing frame: {e}")
                    continue
                    
        except EOFError:
            logger.info(f"SidecarLoopDetector pipe reached EOF for {stream_ctx}")
        except Exception as e:
            logger.error(f"SidecarLoopDetector unexpected error for {stream_ctx}: {e}", exc_info=True)
        finally:
            self.is_closed = True
            logger.info(f"SidecarLoopDetector worker stopped for {stream_ctx}")

    def detect_loop(self, hamming_tolerance: Optional[int] = None) -> Optional[float]:
        """
        Scans the buffer for content loops.

        Args:
            hamming_tolerance: Hamming distance threshold for frame matching.
                               Defaults to the module-level HAMMING_TOLERANCE (5)
                               when not supplied.  Pass an explicit value to use a
                               different tolerance without mutating the module
                               constant — important for one-shot probes running
                               concurrently with live monitoring sidecars.

        Returns:
            The duration of the detected loop in seconds, or None if no loop is detected.
        """
        # Resolve tolerance — caller may override for tighter one-shot probes
        tolerance = hamming_tolerance if hamming_tolerance is not None else HAMMING_TOLERANCE

        # Stale buffer protection
        if time.monotonic() - self.last_frame_time > STALE_THRESHOLD:
            return None
            
        if len(self.buffer) < SEQUENCE_LENGTH * 2:
            return None
            
        # Get 3 most recent hashes: h_2, h_1, h0 (where h0 is the newest)
        recent = list(self.buffer)[-SEQUENCE_LENGTH:]
        h_2, h_1, h0 = [item[1] for item in recent]
        t0 = recent[-1][0]
        
        # Static image / Black screen filter
        # If the 3 most recent are nearly identical, it's a static image or black screen
        if (h0 - h_1 <= tolerance) and (h_1 - h_2 <= tolerance):
            return None

        # Iterate backward through history (skipping the most recent sequence)
        history = list(self.buffer)[:-SEQUENCE_LENGTH]
        
        # Match sequences of length 3
        for i in range(len(history) - SEQUENCE_LENGTH + 1):
            # Match recent sequence [H0, H-1, H-2] against [H-t, H-(t+1), H-(t+2)]
            # Note: history[i] is [H-(t+2)], history[i+1] is [H-(t+1)], history[i+2] is [H-t]
            match_t2 = history[i][1] - h_2 <= tolerance
            match_t1 = history[i+1][1] - h_1 <= tolerance
            match_t0 = history[i+2][1] - h0 <= tolerance
            
            if match_t2 and match_t1 and match_t0:
                # Found a matching sequence!
                t_match = history[i+2][0]
                duration = t0 - t_match
                
                if duration >= LOOP_DURATION_THRESHOLD:
                    return duration
        
        return None

    def _read_ppm_frame(self) -> Optional[bytes]:
        """
        Reads a single PPM frame from the pipe.
        PPM format (P6):
        P6
        width height
        max_val
        binary_data
        """
        try:
            # Header: P6\nwidth height\nmaxval\n
            # Read 3 lines for the PPM header
            header_lines = []
            for _ in range(3):
                line = self.pipe.readline()
                if not line:
                    return None
                header_lines.append(line)
            header = b"".join(header_lines)
            
            # Parse header to get dimensions
            parsed_lines = [l.decode().strip() for l in header_lines]
            if not parsed_lines or parsed_lines[0] != 'P6':
                return None
            
            dims = parsed_lines[1].split()
            if len(dims) != 2:
                return None
            width, height = map(int, dims)
            
            # Binary data: width * height * 3 bytes (RGB)
            data_size = width * height * 3
            data = self.pipe.read(data_size)
            if len(data) < data_size:
                return None
                
            return header + data
            
        except Exception as e:
            logger.error(f"Error reading PPM frame: {e}")
            return None

    def _simple_hash(self, img):
        """Fallback simple hash if imagehash not installed"""
        # Very crude average hash
        img = img.convert('L').resize((8, 8), Image.Resampling.LANCZOS)
        pixels = list(img.getdata())
        avg = sum(pixels) / 64
        hash_val = sum(1 << i for i, v in enumerate(pixels) if v > avg)
        return SidecarHash(hash_val)

class SidecarHash:
    """Helper to simulate imagehash behavior if not present"""
    def __init__(self, value):
        self.value = value
    def __sub__(self, other):
        # Hamming distance
        return bin(self.value ^ other.value).count('1')
