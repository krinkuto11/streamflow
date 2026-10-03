"""Bounded operation timings that contain no URLs or credentials."""

import threading
import time
from collections import defaultdict, deque
from contextlib import contextmanager


class OperationTimings:
    def __init__(self, max_samples=100):
        self._lock = threading.Lock()
        self._samples = defaultdict(lambda: deque(maxlen=max_samples))

    def record(self, phase, seconds):
        with self._lock:
            self._samples[str(phase)].append(max(0.0, float(seconds)))

    @contextmanager
    def measure(self, phase):
        started = time.monotonic()
        try:
            yield
        finally:
            self.record(phase, time.monotonic() - started)

    def snapshot(self):
        with self._lock:
            return {
                phase: {
                    "samples": len(values),
                    "last_seconds": round(values[-1], 4),
                    "mean_seconds": round(sum(values) / len(values), 4),
                    "total_seconds": round(sum(values), 4),
                }
                for phase, values in self._samples.items() if values
            }


STREAM_OPERATION_TIMINGS = OperationTimings()
