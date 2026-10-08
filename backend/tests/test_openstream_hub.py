"""The shared per-server poller: one request per cycle for any number of
sources, leases instead of pins, releases that never kill a watched stream."""

import time
from unittest.mock import patch

import pytest

from apps.stream import openstream_hub as hub_module
from apps.stream.openstream_hub import OpenStreamHub
from apps.stream.openstream_monitor import OpenStreamStreamMonitor
from tests.openstream_fake import FakeOpenStream

BASE = "http://os:6878"


def hub_module_dead_fatal():
    from apps.stream.openstream_monitor import DEAD_FATAL_SECS
    return DEAD_FATAL_SECS


def cid(i):
    return f"{i:040x}"


@pytest.fixture
def fake(monkeypatch):
    """A hub wired to a fake server; its thread never starts (tests drive cycle())."""
    fake = FakeOpenStream()
    hub = OpenStreamHub(BASE)
    hub._http = fake
    monkeypatch.setattr(OpenStreamHub, "_ensure_thread", lambda self: None)
    monkeypatch.setattr(hub_module, "get_hub", lambda base: hub)
    monkeypatch.setattr("apps.stream.openstream_monitor.get_hub", lambda base: hub)
    fake.hub = hub
    return fake


def monitor(i, owner="streamflow/s1", **kw):
    m = OpenStreamStreamMonitor(url=f"{BASE}/ace/getstream?id={cid(i)}", stream_id=i, owner=owner, **kw)
    assert m.start()
    return m


def test_one_request_per_cycle_for_many_sources(fake):
    mons = [monitor(i) for i in range(100)]
    for i in range(100):
        fake.healthy(cid(i))
    fake.hub.cycle()
    assert fake.count("GET", "/api/streams") == 1
    assert all(m.stats.is_alive and not m.is_buffering() for m in mons)
    fake.hub.cycle()
    assert fake.count("GET", "/api/streams") == 2  # still one per cycle


def test_start_never_touches_the_network(fake):
    fake.down = True
    t = time.monotonic()
    m = monitor(1)
    assert time.monotonic() - t < 0.1
    assert fake.calls == []
    assert m.stats.is_alive  # not dead before the first poll


def test_sources_are_leased_not_pinned(fake):
    monitor(1)
    fake.hub.cycle()
    assert fake.leases[cid(1)] == {"streamflow/s1"}
    lease_call = [c for c in fake.calls if c[0] == "POST" and c[1].endswith("/api/streams")][0]
    assert lease_call[2]["lease"]["ttlSecs"] == hub_module.LEASE_TTL_SECS


def test_lease_renewed_before_it_expires(fake, monkeypatch):
    monitor(1)
    fake.hub.cycle()
    fake.hub.cycle()
    assert fake.count("POST", "/api/streams") == 1  # not re-leased every cycle
    later = time.monotonic() + hub_module.LEASE_RENEW_SECS + 1
    monkeypatch.setattr(hub_module.time, "monotonic", lambda: later)
    fake.hub.cycle()
    assert fake.count("POST", "/api/streams") == 2
    assert hub_module.LEASE_RENEW_SECS * 3 <= hub_module.LEASE_TTL_SECS  # >=2 missed renewals tolerated


def test_stop_releases_only_this_owners_claim(fake):
    a = monitor(1, owner="streamflow/s1")
    monitor(1, owner="streamflow/s2")  # another session watching the same source
    fake.hub.cycle()
    a.stop()
    fake.hub.cycle()
    assert fake.leases[cid(1)] == {"streamflow/s2"}
    assert fake.removed == []


def test_same_owner_twice_releases_after_the_last(fake):
    a = monitor(1)
    b = monitor(1)
    fake.hub.cycle()
    a.stop()
    fake.hub.cycle()
    assert fake.leases[cid(1)] == {"streamflow/s1"}
    b.stop()
    fake.hub.cycle()
    assert fake.leases[cid(1)] == set()


def test_legacy_server_falls_back_to_remove(fake):
    fake.supports_leases = False
    m = monitor(1)
    fake.hub.cycle()
    m.stop()
    fake.hub.cycle()
    assert fake.removed == [cid(1)]


def test_missing_source_is_leased_again_and_reported_warming(fake):
    m = monitor(1)
    fake.hub.cycle()
    del fake.snapshots[cid(1)]  # server restarted / reaped it
    fake.hub.cycle()
    assert m.stats.is_alive and m.is_buffering()
    fake.hub.cycle()  # the queued re-lease goes out at the start of the next cycle
    assert fake.count("POST", "/api/streams") == 2
    assert cid(1) in fake.snapshots


@pytest.mark.parametrize("status,needle", [(401, "rejected"), (403, "requires an API key"), (428, "first-run setup")])
def test_refused_key_reported_not_fatal(fake, status, needle):
    m = monitor(1)
    fake.status = status
    before = m.stats.last_updated
    time.sleep(0.01)
    fake.hub.cycle()
    assert m.stats.is_alive and not m.stats.is_fatal and m.is_buffering()
    assert needle in m.stats.error_message
    assert m.stats.last_updated > before
    fake.status = 200
    fake.healthy(cid(1))
    fake.hub.cycle()
    assert m.stats.error_message is None and not m.is_buffering()


def test_unreachable_server_is_unknown_not_dead(fake):
    m = monitor(1)
    fake.down = True
    fake.hub.cycle()
    assert m.stats.is_alive and not m.stats.is_fatal and m.is_buffering()
    assert m.stats.last_updated > 0


def test_dead_source_is_fatal_and_can_stop_itself_from_the_callback(fake):
    stopped = []

    def on_update(stats):
        if not stats.is_alive:
            m.stop()  # what the monitoring service does, on the hub thread
            stopped.append(True)

    m = monitor(1, on_stats_update=on_update)
    fake.healthy(cid(1), state="dead")
    fake.hub.cycle()
    assert not stopped  # dead for a moment: ranked down, not evicted
    m._dead_since -= hub_module_dead_fatal()  # it has now been dead long enough
    fake.hub.cycle()
    assert stopped and m.stats.is_fatal
    fake.hub.cycle()  # releases it and no longer polls it
    assert fake.leases[cid(1)] == set()


def test_api_key_sent_on_every_call(fake, monkeypatch):
    monkeypatch.setattr(hub_module, "openstream_headers", lambda: {"X-API-Key": "secret"})
    m = monitor(1)
    fake.hub.cycle()
    m.stop()
    fake.hub.cycle()
    assert fake.calls and all(c[3] == {"X-API-Key": "secret"} for c in fake.calls)
