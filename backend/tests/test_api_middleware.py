import ipaddress

from apps.api.middleware import (
    InMemoryRateLimiter,
    parse_trusted_proxy_networks,
    resolve_client_ip,
)


def test_forwarded_for_is_ignored_without_trusted_proxy():
    assert resolve_client_ip("192.0.2.10", "203.0.113.8", ()) == "192.0.2.10"


def test_forwarded_for_is_used_only_for_trusted_peer():
    networks = parse_trusted_proxy_networks("10.0.0.0/8, 2001:db8::/32")

    assert resolve_client_ip("10.0.0.5", "203.0.113.8, 10.0.0.4", networks) == "203.0.113.8"
    assert resolve_client_ip("192.0.2.10", "203.0.113.8", networks) == "192.0.2.10"


def test_invalid_proxy_entries_and_forwarded_values_are_ignored():
    networks = parse_trusted_proxy_networks("invalid, 10.0.0.1")

    assert networks == (ipaddress.ip_network("10.0.0.1/32"),)
    assert resolve_client_ip("10.0.0.1", "bad, 203.0.113.9", networks) == "203.0.113.9"
    assert resolve_client_ip("not-an-ip", "203.0.113.9", networks) == "unknown"


def test_rate_limiter_expires_empty_buckets():
    now = [100.0]
    limiter = InMemoryRateLimiter(
        max_requests=2,
        window_seconds=10,
        max_buckets=10,
        clock=lambda: now[0],
    )

    assert limiter.check("one").allowed
    assert limiter.bucket_count == 1

    now[0] = 111.0
    assert limiter.check("two").allowed
    assert limiter.bucket_count == 1


def test_rate_limiter_enforces_limit_and_bounds_bucket_count():
    now = [100.0]
    limiter = InMemoryRateLimiter(
        max_requests=1,
        window_seconds=60,
        max_buckets=2,
        clock=lambda: now[0],
    )

    assert limiter.check("one").allowed
    assert not limiter.check("one").allowed
    now[0] += 1
    assert limiter.check("two").allowed
    now[0] += 1
    assert limiter.check("three").allowed
    assert limiter.bucket_count == 2

