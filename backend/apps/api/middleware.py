"""API middleware helpers such as lightweight in-memory rate limiting."""

import os
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Deque, Dict


@dataclass(frozen=True)
class RateLimitDecision:
    allowed: bool
    retry_after_seconds: int = 0


class InMemoryRateLimiter:
    """Simple sliding-window rate limiter for API requests."""

    def __init__(self, *, max_requests: int, window_seconds: int) -> None:
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._events: Dict[str, Deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def check(self, key: str) -> RateLimitDecision:
        now = time.time()
        threshold = now - self.window_seconds

        with self._lock:
            queue = self._events[key]
            while queue and queue[0] < threshold:
                queue.popleft()

            if len(queue) >= self.max_requests:
                retry_after = max(1, int(self.window_seconds - (now - queue[0])))
                return RateLimitDecision(allowed=False, retry_after_seconds=retry_after)

            queue.append(now)
            return RateLimitDecision(allowed=True)


API_RATE_LIMIT_ENABLED = os.getenv("API_RATE_LIMIT_ENABLED", "true").lower() in {"1", "true", "yes", "on"}
API_RATE_LIMIT_MAX_REQUESTS = int(os.getenv("API_RATE_LIMIT_MAX_REQUESTS", "240"))
API_RATE_LIMIT_WINDOW_SECONDS = int(os.getenv("API_RATE_LIMIT_WINDOW_SECONDS", "60"))

api_rate_limiter = InMemoryRateLimiter(
    max_requests=API_RATE_LIMIT_MAX_REQUESTS,
    window_seconds=API_RATE_LIMIT_WINDOW_SECONDS,
)
