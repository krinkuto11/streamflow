import threading
import time

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


def test_freeze_detection_config_and_probe_command():
    config = normalize_config({
        "freeze_detection_enabled": True,
        "freeze_min_duration_seconds": 7,
        "freeze_noise_threshold": 0.002,
        "freeze_ratio_threshold": 0.9,
    })

    command, duration = ShadowBlankMonitorService._blank_probe_command(
        "http://dispatcharr.local/proxy/ts/stream/uuid-1",
        config,
    )

    assert duration == config["probe_duration_seconds"]
    assert any("freezedetect=n=0.002:d=7.0" in arg for arg in command)
    assert config["freeze_detection_enabled"] is True
    assert config["freeze_ratio_threshold"] == 0.9


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


def test_confirmed_freeze_switches_to_next_stream_when_live(tmp_path):
    switch_calls = []
    udi = FakeUdi(
        statuses=[{"uuid-1": active_status()}],
        channels=[{"id": 1, "uuid": "uuid-1", "streams": [10, 11, 12]}],
    )
    service = make_service(
        tmp_path,
        udi=udi,
        blank_probe=lambda url, config: {"blank_detected": False, "freeze_detected": True},
        switch_calls=switch_calls,
    )
    service.update_config({
        "enabled": False,
        "dry_run": False,
        "confirmation_count": 1,
        "freeze_detection_enabled": True,
    })

    status = service.run_once(force=True)

    assert switch_calls == [("uuid-1", 11, None)]
    assert status["recent_events"][0]["type"] == "switch_success"
    assert status["recent_events"][0]["details"]["reason"] == "freeze"


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


def test_viewer_left_after_probe_clears_watched_snapshot(tmp_path):
    udi = FakeUdi(
        statuses=[
            {"uuid-1": active_status(stream_id=10)},
            {
                "uuid-1": {
                    "state": "active",
                    "channel_id": "uuid-1",
                    "stream_id": 10,
                    "clients": [{"user_agent": "StreamFlow-Shadow-Blank-Monitor/1.0"}],
                },
            },
        ],
        channels=[{"id": 1, "uuid": "uuid-1", "streams": [10, 11]}],
    )
    service = make_service(
        tmp_path,
        udi=udi,
        blank_probe=lambda url, config: {"blank_detected": False},
    )
    service.update_config({"enabled": False, "dry_run": False})

    status = service.run_once(force=True)

    assert status["recent_events"][0]["type"] == "viewer_left"
    assert status["watched_count"] == 0
    assert status["watched_channels"] == []


def test_max_watchers_probe_targets_in_parallel(tmp_path):
    probe_order = []
    release = threading.Event()

    def blank_probe(url, config):
        probe_order.append(url)
        if len(probe_order) == 3:
            release.set()
        assert release.wait(1)
        return {"blank_detected": False}

    statuses = {
        f"uuid-{index}": {
            "state": "active",
            "channel_id": f"uuid-{index}",
            "stream_id": index * 10,
            "clients": [{"user_agent": f"Client {index}"}],
        }
        for index in range(1, 5)
    }
    channels = [
        {"id": index, "uuid": f"uuid-{index}", "streams": [index * 10, index * 10 + 1]}
        for index in range(1, 5)
    ]
    udi = FakeUdi(statuses=[statuses], channels=channels)
    service = make_service(tmp_path, udi=udi, blank_probe=blank_probe)
    service.update_config({"enabled": False, "dry_run": False, "max_concurrent_watchers": 3})

    status = service.run_once(force=True)

    assert len(probe_order) == 3
    assert sorted(probe_order) == [
        "http://dispatcharr.local/proxy/ts/stream/uuid-1",
        "http://dispatcharr.local/proxy/ts/stream/uuid-2",
        "http://dispatcharr.local/proxy/ts/stream/uuid-3",
    ]
    assert [event["type"] for event in status["recent_events"][:3]] == ["probe_ok"] * 3


def test_existing_watcher_client_prevents_duplicate_probe(tmp_path):
    probe_urls = []
    udi = FakeUdi(
        statuses=[{
            "uuid-1": active_status(
                stream_id=10,
                clients=[
                    {"user_agent": "VLC"},
                    {"user_agent": "StreamFlow-Shadow-Blank-Monitor/1.0"},
                ],
            ),
            "uuid-2": {
                "state": "active",
                "channel_id": "uuid-2",
                "stream_id": 20,
                "clients": [{"user_agent": "VLC"}],
            },
        }],
        channels=[
            {"id": 1, "uuid": "uuid-1", "streams": [10, 11]},
            {"id": 2, "uuid": "uuid-2", "streams": [20, 21]},
        ],
    )
    service = make_service(
        tmp_path,
        udi=udi,
        blank_probe=lambda url, config: probe_urls.append(url) or {"blank_detected": False},
    )
    service.update_config({"enabled": False, "dry_run": False, "max_concurrent_watchers": 2})

    status = service.run_once(force=True)

    assert probe_urls == ["http://dispatcharr.local/proxy/ts/stream/uuid-2"]
    assert status["watched_count"] == 2
    assert status["recent_events"][0]["type"] == "probe_ok"


def test_continuous_mode_starts_uncovered_target_when_another_has_watcher(tmp_path):
    probe_urls = []
    udi = FakeUdi(
        statuses=[{
            "uuid-1": active_status(
                stream_id=10,
                clients=[
                    {"user_agent": "VLC"},
                    {"user_agent": "StreamFlow-Shadow-Blank-Monitor/1.0"},
                ],
            ),
            "uuid-2": {
                "state": "active",
                "channel_id": "uuid-2",
                "stream_id": 20,
                "clients": [{"user_agent": "VLC"}],
            },
        }],
        channels=[
            {"id": 1, "uuid": "uuid-1", "streams": [10, 11]},
            {"id": 2, "uuid": "uuid-2", "streams": [20, 21]},
        ],
    )
    service = make_service(
        tmp_path,
        udi=udi,
        blank_probe=lambda url, config: probe_urls.append(url) or {"blank_detected": False},
    )
    service.update_config({
        "enabled": False,
        "dry_run": False,
        "watch_mode": "continuous",
        "max_concurrent_watchers": 2,
    })

    status = service.run_once(force=True)

    assert probe_urls == ["http://dispatcharr.local/proxy/ts/stream/uuid-2"]
    assert status["watched_count"] == 2
    assert status["recent_events"][0]["type"] == "probe_ok"


def test_continuous_default_probe_does_not_block_new_scans(tmp_path):
    started = threading.Event()
    release = threading.Event()
    udi = FakeUdi(
        statuses=[{"uuid-1": active_status(stream_id=10)}],
        channels=[{"id": 1, "uuid": "uuid-1", "streams": [10, 11]}],
    )
    service = ShadowBlankMonitorService(
        config_file=tmp_path / "shadow.json",
        udi_provider=lambda: udi,
        switch_stream=lambda *args, **kwargs: True,
        base_url_provider=lambda: "http://dispatcharr.local",
        stream_checker_provider=lambda: FakeStreamChecker(),
        clock=lambda: 1000.0,
    )

    def fake_continuous_probe(url, config, probe_udi, target):
        started.set()
        assert release.wait(1)
        return {"blank_detected": False, "viewer_left": True}

    service._run_blank_probe_until_viewer_left = fake_continuous_probe
    service.update_config({
        "enabled": False,
        "dry_run": False,
        "watch_mode": "continuous",
    })

    status = service.run_once(force=True)

    assert status["watched_count"] == 1
    assert started.wait(0.5)
    with service._lock:
        assert service._active_probes == {"uuid-1"}

    release.set()
    for _ in range(20):
        with service._lock:
            if not service._active_probes:
                break
        time.sleep(0.05)

    assert service.get_status()["recent_events"][0]["type"] == "viewer_left"


def test_quality_checker_guard_is_opt_in(tmp_path):
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
        blank_probe=lambda url, config: probe_urls.append(url) or {"blank_detected": False},
    )
    service.update_config({"enabled": False, "dry_run": False})

    status = service.run_once(force=True)

    assert probe_urls == ["http://dispatcharr.local/proxy/ts/stream/uuid-1"]
    assert status["recent_events"][0]["type"] == "probe_ok"


def test_quality_checker_state_does_not_pause_shadow_probe(tmp_path):
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
        blank_probe=lambda url, config: probe_urls.append(url) or {"blank_detected": False},
    )
    service.update_config({
        "enabled": False,
        "dry_run": False,
        "confirmation_count": 1,
        "skip_during_quality_check": True,
    })

    status = service.run_once(force=True)

    assert service.get_config()["skip_during_quality_check"] is False
    assert probe_urls == ["http://dispatcharr.local/proxy/ts/stream/uuid-1"]
    assert status["recent_events"][0]["type"] == "probe_ok"
