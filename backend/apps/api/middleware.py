"""API middleware helpers such as lightweight in-memory rate limiting."""

import ipaddress
import os
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable, Deque, Dict, Iterable, Optional, Union


ProxyNetwork = Union[ipaddress.IPv4Network, ipaddress.IPv6Network]


@dataclass(frozen=True)
class RateLimitDecision:
    allowed: bool
    retry_after_seconds: int = 0


class InMemoryRateLimiter:
    """Simple sliding-window rate limiter for API requests."""

    def __init__(
        self,
        *,
        max_requests: int,
        window_seconds: int,
        max_buckets: int = 4096,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self.max_buckets = max(1, max_buckets)
        self._clock = clock
        self._events: Dict[str, Deque[float]] = {}
        self._last_seen: Dict[str, float] = {}
        self._last_prune = 0.0
        self._lock = threading.Lock()

    def _prune(self, threshold: float) -> None:
        expired = []
        for key, queue in self._events.items():
            while queue and queue[0] < threshold:
                queue.popleft()
            if not queue:
                expired.append(key)
        for key in expired:
            self._events.pop(key, None)
            self._last_seen.pop(key, None)

    def _make_room(self, threshold: float) -> None:
        self._prune(threshold)
        while len(self._events) >= self.max_buckets:
            oldest = min(self._last_seen, key=self._last_seen.get)
            self._events.pop(oldest, None)
            self._last_seen.pop(oldest, None)

    def check(self, key: str) -> RateLimitDecision:
        now = self._clock()
        threshold = now - self.window_seconds

        with self._lock:
            cleanup_interval = max(1, min(self.window_seconds, 60))
            if now - self._last_prune >= cleanup_interval:
                self._prune(threshold)
                self._last_prune = now
            queue = self._events.get(key)
            if queue is None:
                self._make_room(threshold)
                queue = deque()
                self._events[key] = queue
            else:
                while queue and queue[0] < threshold:
                    queue.popleft()
            self._last_seen[key] = now

            if len(queue) >= self.max_requests:
                retry_after = max(1, int(self.window_seconds - (now - queue[0])))
                return RateLimitDecision(allowed=False, retry_after_seconds=retry_after)

            queue.append(now)
            return RateLimitDecision(allowed=True)

    @property
    def bucket_count(self) -> int:
        with self._lock:
            return len(self._events)


def parse_trusted_proxy_networks(raw_value: str) -> tuple[ProxyNetwork, ...]:
    """Parse an explicit comma-separated trusted-proxy CIDR allowlist."""
    networks = []
    for item in (raw_value or "").split(","):
        value = item.strip()
        if not value:
            continue
        try:
            networks.append(ipaddress.ip_network(value, strict=False))
        except ValueError:
            continue
    return tuple(networks)


def resolve_client_ip(
    remote_addr: Optional[str],
    forwarded_for: Optional[str],
    trusted_proxy_networks: Iterable[ProxyNetwork],
) -> str:
    """Resolve a client IP without trusting forwarding headers by default."""
    try:
        peer = ipaddress.ip_address((remote_addr or "").strip())
    except ValueError:
        return "unknown"

    if not any(peer in network for network in trusted_proxy_networks):
        return str(peer)

    for candidate in (forwarded_for or "").split(","):
        try:
            return str(ipaddress.ip_address(candidate.strip()))
        except ValueError:
            continue
    return str(peer)


API_RATE_LIMIT_ENABLED = os.getenv("API_RATE_LIMIT_ENABLED", "true").lower() in {"1", "true", "yes", "on"}
API_RATE_LIMIT_MAX_REQUESTS = int(os.getenv("API_RATE_LIMIT_MAX_REQUESTS", "240"))
API_RATE_LIMIT_WINDOW_SECONDS = int(os.getenv("API_RATE_LIMIT_WINDOW_SECONDS", "60"))
API_RATE_LIMIT_MAX_BUCKETS = int(os.getenv("API_RATE_LIMIT_MAX_BUCKETS", "4096"))
TRUSTED_PROXY_NETWORKS = parse_trusted_proxy_networks(
    os.getenv("STREAMFLOW_TRUSTED_PROXY_CIDRS", "")
)

api_rate_limiter = InMemoryRateLimiter(
    max_requests=API_RATE_LIMIT_MAX_REQUESTS,
    window_seconds=API_RATE_LIMIT_WINDOW_SECONDS,
    max_buckets=API_RATE_LIMIT_MAX_BUCKETS,
)
