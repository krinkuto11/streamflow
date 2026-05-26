from apps.stream.shadow_blank_monitor_service import (
    ShadowBlankMonitorService,
    normalize_config,
)


class FakeUdi:
    def __init__(self, *, statuses, channels):
        self.statuses = list(statuses)
        self.channels = channels
        self.status_calls = 0

    def get_proxy_status(self):
        index = min(self.status_calls, len(self.statuses) - 1)
        self.status_calls += 1
        return self.statuses[index]

    def get_channels(self):
        return self.channels

    def get_channel_by_id(self, channel_id):
        for channel in self.channels:
            if channel.get("id") == channel_id:
                return channel
        return None


class FakeStreamChecker:
    def __init__(self, status=None):
        self.status = status or {"stream_checking_mode": False, "queue": {}, "progress": {}}

    def get_status(self):
        return self.status


def make_service(tmp_path, *, udi, blank_probe=None, switch_calls=None, clock=None, checker=None):
    switch_calls = switch_calls if switch_calls is not None else []

    def switch_stream(channel_id, stream_id=None, url=None):
        switch_calls.append((channel_id, stream_id, url))
        return True

    return ShadowBlankMonitorService(
        config_file=tmp_path / "shadow.json",
        udi_provider=lambda: udi,
        switch_stream=switch_stream,
        base_url_provider=lambda: "http://dispatcharr.local",
        blank_probe=blank_probe or (lambda url, config: {"blank_detected": False}),
        stream_checker_provider=lambda: checker or FakeStreamChecker(),
        clock=clock or (lambda: 1000.0),
    )


def active_status(stream_id=10, clients=None):
    return {
        "state": "active",
        "channel_id": "uuid-1",
        "stream_id": stream_id,
        "clients": clients if clients is not None else [{"user_agent": "VLC"}],
    }


def test_watch_mode_controls_scan_delay():
    continuous = normalize_config({
        "watch_mode": "continuous",
        "watch_gap_seconds": 2,
        "poll_interval_seconds": 90,
    })
    assert ShadowBlankMonitorService._next_scan_delay(continuous) == 2

    periodic = normalize_config({
        "watch_mode": "periodic",
        "watch_gap_seconds": 2,
        "poll_interval_seconds": 90,
    })
    assert ShadowBlankMonitorService._next_scan_delay(periodic) == 90

    invalid = normalize_config({"watch_mode": "always-on", "watch_gap_seconds": 0})
    assert invalid["watch_mode"] == "periodic"
    assert invalid["watch_gap_seconds"] == 1


def test_discovers_real_clients_and_hides_raw_channel_identifiers(tmp_path):
    udi = FakeUdi(
        statuses=[{
            "uuid-1": active_status(),
            "uuid-2": {
                "state": "active",
                "channel_id": "uuid-2",
                "stream_id": 20,
                "clients": [{"user_agent": "StreamFlow-Shadow-Blank-Monitor/1.0"}],
            },
        }],
        channels=[
            {"id": 1, "uuid": "uuid-1", "streams": [10, 11]},
            {"id": 2, "uuid": "uuid-2", "streams": [20, 21]},
        ],
    )
    service = make_service(tmp_path, udi=udi)

    targets = service.discover_active_targets(udi, normalize_config({}))

    assert len(targets) == 1
    assert targets[0]["channel_id"] == 1
    assert targets[0]["real_client_count"] == 1

    status = service.get_status()
    assert status["watched_count"] == 1
    assert "uuid-1" not in repr(status)
    assert "uuid-2" not in repr(status)


def test_dry_run_uses_channel_proxy_and_records_intended_switch(tmp_path):
    probe_urls = []
    switch_calls = []

    def blank_probe(url, config):
        probe_urls.append(url)
        return {"blank_detected": True, "blank_ratio": 1.0, "blank_duration_secs": 8.0}

    udi = FakeUdi(
        statuses=[{"uuid-1": active_status()}],
        channels=[{"id": 1, "uuid": "uuid-1", "streams": [10, 11]}],
    )
    service = make_service(tmp_path, udi=udi, blank_probe=blank_probe, switch_calls=switch_calls)
    service.update_config({"enabled": False, "dry_run": True, "confirmation_count": 1})

    status = service.run_once(force=True)

    assert probe_urls == ["http://dispatcharr.local/proxy/ts/stream/uuid-1"]
    assert switch_calls == []
    assert status["recent_events"][0]["type"] == "dry_run_switch"
    assert status["cooldowns"][0]["channel_ref"] == status["recent_events"][0]["channel_ref"]


def test_disabling_monitor_clears_watched_snapshot(tmp_path):
    udi = FakeUdi(
        statuses=[{"uuid-1": active_status()}],
        channels=[{"id": 1, "uuid": "uuid-1", "streams": [10, 11]}],
    )
    service = make_service(tmp_path, udi=udi)
    service.update_config({"enabled": False, "dry_run": True})

    status = service.run_once(force=True)
    assert status["watched_count"] == 1

    service.stop()

    assert service.get_status()["watched_count"] == 0
    assert service.get_status()["watched_channels"] == []


def test_confirmed_blank_switches_to_next_stream_when_live(tmp_path):
    switch_calls = []
    udi = FakeUdi(
        statuses=[{"uuid-1": active_status()}],
        channels=[{"id": 1, "uuid": "uuid-1", "streams": [10, 11, 12]}],
    )
    service = make_service(
        tmp_path,
        udi=udi,
        blank_probe=lambda url, config: {"blank_detected": True},
        switch_calls=switch_calls,
    )
    service.update_config({"enabled": False, "dry_run": False, "confirmation_count": 1})

    status = service.run_once(force=True)

    assert switch_calls == [("uuid-1", 11, None)]
    assert status["recent_events"][0]["type"] == "switch_success"


def test_single_stream_channel_records_no_alternative_without_switching(tmp_path):
    switch_calls = []
    udi = FakeUdi(
        statuses=[{"uuid-1": active_status()}],
        channels=[{"id": 1, "uuid": "uuid-1", "streams": [10]}],
    )
    service = make_service(
        tmp_path,
        udi=udi,
        blank_probe=lambda url, config: {"blank_detected": True},
        switch_calls=switch_calls,
    )
    service.update_config({"enabled": False, "dry_run": False, "confirmation_count": 1})

    status = service.run_once(force=True)

    assert switch_calls == []
    assert status["recent_events"][0]["type"] == "no_alternative"


def test_stale_stream_guard_skips_switch_if_dispatcharr_already_changed(tmp_path):
    switch_calls = []
    udi = FakeUdi(
        statuses=[
            {"uuid-1": active_status(stream_id=10)},
            {"uuid-1": active_status(stream_id=11)},
        ],
        channels=[{"id": 1, "uuid": "uuid-1", "streams": [10, 11]}],
    )
    service = make_service(
        tmp_path,
        udi=udi,
        blank_probe=lambda url, config: {"blank_detected": True},
        switch_calls=switch_calls,
    )
    service.update_config({"enabled": False, "dry_run": False, "confirmation_count": 1})

    status = service.run_once(force=True)

    assert switch_calls == []
    assert status["recent_events"][0]["type"] == "stale_stream_guard"


def test_viewer_left_guard_skips_switching(tmp_path):
    switch_calls = []
    udi = FakeUdi(
        statuses=[
            {"uuid-1": active_status(stream_id=10)},
            {
                "uuid-1": {
                    "state": "idle",
                    "channel_id": "uuid-1",
                    "stream_id": 10,
                    "clients": [],
                },
            },
        ],
        channels=[{"id": 1, "uuid": "uuid-1", "streams": [10, 11]}],
    )
    service = make_service(
        tmp_path,
        udi=udi,
        blank_probe=lambda url, config: {"blank_detected": True},
        switch_calls=switch_calls,
    )
    service.update_config({"enabled": False, "dry_run": False, "confirmation_count": 1})

    status = service.run_once(force=True)

    assert switch_calls == []
    assert status["recent_events"][0]["type"] == "viewer_left"


def test_quality_checker_same_channel_guard_skips_probe(tmp_path):
    probe_urls = []
    udi = FakeUdi(
        statuses=[{"uuid-1": active_status(stream_id=10)}],
        channels=[{"id": 1, "uuid": "uuid-1", "streams": [10, 11]}],
    )
    checker = FakeStreamChecker({
        "stream_checking_mode": True,
        "queue": {"current_channel": 1, "in_progress": 1},
        "progress": {},
    })
    service = make_service(
        tmp_path,
        udi=udi,
        checker=checker,
        blank_probe=lambda url, config: probe_urls.append(url) or {"blank_detected": True},
    )
    service.update_config({"enabled": False, "dry_run": False, "confirmation_count": 1})

    status = service.run_once(force=True)

    assert probe_urls == []
    assert status["recent_events"][0]["type"] == "quality_check_active"
