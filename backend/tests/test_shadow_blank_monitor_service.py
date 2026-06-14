import io
import threading
import time
from datetime import datetime, timedelta, timezone

from flask import Flask

from apps.api.shadow_blank_monitor_handlers import (
    learn_shadow_offline_image_response,
    run_shadow_blank_monitor_once_response,
    start_shadow_blank_monitor_response,
)
from apps.stream import shadow_blank_monitor_service as shadow_module
from apps.stream.shadow_blank_monitor_service import (
    SHADOW_MONITOR_SCAN_ERROR_MESSAGE,
    ShadowBlankMonitorService,
    normalize_config,
)


class FakeUdi:
    def __init__(self, *, statuses, channels, streams=None):
        self.statuses = list(statuses)
        self.channels = channels
        self.streams = streams or {}
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

    def get_stream_by_id(self, stream_id):
        return self.streams.get(stream_id)

    def get_channel_streams(self, channel_id):
        channel = self.get_channel_by_id(channel_id)
        return [
            self.streams.get(stream_id)
            for stream_id in (channel or {}).get("streams", [])
            if self.streams.get(stream_id) is not None
        ]


class FakeStreamChecker:
    def __init__(self, status=None):
        self.status = status or {"stream_checking_mode": False, "queue": {}, "progress": {}}

    def get_status(self):
        return self.status


class FakeProbeStderr:
    def __init__(self, lines, *, line_delay=0.0):
        self._lines = list(lines)
        self._index = 0
        self._line_delay = line_delay

    @property
    def done(self):
        return self._index >= len(self._lines)

    def readline(self):
        if self._index >= len(self._lines):
            return ""
        if self._line_delay:
            time.sleep(self._line_delay)
        line = self._lines[self._index]
        self._index += 1
        return line


class FakeProbeProcess:
    def __init__(self, lines, *, immediate_exit=False, line_delay=0.0):
        self.stderr = FakeProbeStderr(lines, line_delay=line_delay)
        self.returncode = 1
        self._immediate_exit = immediate_exit

    def poll(self):
        if self._immediate_exit:
            return self.returncode
        return self.returncode if self.stderr.done else None

    def terminate(self):
        self.returncode = 1

    def kill(self):
        self.returncode = 1

    def wait(self, timeout=None):
        return self.returncode


class PersistentFakeProbeProcess(FakeProbeProcess):
    def __init__(self, lines, *, line_delay=0.0):
        super().__init__(lines, line_delay=line_delay)
        self.returncode = None

    def poll(self):
        return self.returncode

    def terminate(self):
        self.returncode = 1

    def kill(self):
        self.returncode = 1

    def wait(self, timeout=None):
        if self.returncode is None:
            self.returncode = 1
        return self.returncode


def make_service(
    tmp_path,
    *,
    udi,
    blank_probe=None,
    loop_probe=None,
    switch_calls=None,
    clock=None,
    checker=None,
    watcher_api_key="test-watcher-key",
):
    switch_calls = switch_calls if switch_calls is not None else []

    def switch_stream(channel_id, stream_id=None, url=None):
        switch_calls.append((channel_id, stream_id, url))
        return True

    service = ShadowBlankMonitorService(
        config_file=tmp_path / "shadow.json",
        udi_provider=lambda: udi,
        switch_stream=switch_stream,
        base_url_provider=lambda: "http://dispatcharr.local",
        blank_probe=blank_probe or (lambda url, config: {"blank_detected": False}),
        loop_probe=loop_probe,
        stream_checker_provider=lambda: checker or FakeStreamChecker(),
        clock=clock or (lambda: 1000.0),
    )
    if watcher_api_key is not None:
        with service._lock:
            service._config["watcher_api_key"] = watcher_api_key
            service._save_config()
    return service


def active_status(stream_id=10, clients=None):
    return {
        "state": "active",
        "channel_id": "uuid-1",
        "stream_id": stream_id,
        "clients": clients if clients is not None else [{"user_agent": "VLC"}],
    }


def test_watch_mode_controls_scan_delay():
    defaults = normalize_config({})
    assert defaults["enabled"] is False
    assert defaults["dry_run"] is False
    assert defaults["watch_mode"] == "continuous"
    assert defaults["poll_interval_seconds"] == 5
    assert defaults["watch_gap_seconds"] == 1
    assert defaults["probe_duration_seconds"] == 60
    assert defaults["freeze_detection_enabled"] is True
    assert defaults["no_decodable_frames_detection_enabled"] is True
    assert defaults["no_decodable_frames_min_duration_seconds"] == 10.0
    assert defaults["garbled_audio_detection_enabled"] is False
    assert defaults["garbled_audio_error_threshold"] == 3
    assert defaults["silent_audio_detection_enabled"] is False
    assert defaults["silent_audio_min_duration_seconds"] == 10.0
    assert defaults["silent_audio_noise_db"] == -50
    assert defaults["offline_image_detection_enabled"] is False
    assert defaults["offline_image_reference_hashes"] == []
    assert defaults["offline_image_hash_threshold"] == 4
    assert defaults["loop_detection_enabled"] is False
    assert defaults["loop_probe_duration_seconds"] == 120
    assert defaults["next_stream_pre_probe_enabled"] is False
    assert defaults["next_stream_pre_probe_duration_seconds"] == 8
    assert defaults["confirmation_count"] == 2
    assert defaults["channel_cooldown_seconds"] == 300
    assert defaults["max_switches_per_hour"] == 3
    assert defaults["max_concurrent_watchers"] == 2

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
    assert invalid["watch_mode"] == "continuous"
    assert invalid["watch_gap_seconds"] == 1

    loop_bounds = normalize_config({"loop_detection_enabled": True, "loop_probe_duration_seconds": 999})
    assert loop_bounds["loop_detection_enabled"] is True
    assert loop_bounds["loop_probe_duration_seconds"] == 720


def test_offline_image_status_warns_about_missing_or_invalid_hashes(tmp_path):
    service = make_service(
        tmp_path,
        udi=FakeUdi(statuses=[{}], channels=[]),
    )

    service.update_config({
        "offline_image_detection_enabled": True,
        "offline_image_reference_hashes": ["not-a-phash", "0123456789abcdef", "0123456789ABCDEF"],
    })

    status = service.get_status()
    assert status["offline_image"]["enabled"] is True
    assert status["offline_image"]["reference_count"] == 2
    assert status["offline_image"]["valid_reference_count"] == 1
    assert status["offline_image"]["invalid_reference_count"] == 1
    assert "invalid_reference_hash" in status["offline_image"]["warnings"]


def test_status_exposes_shadow_loop_detection_context(tmp_path):
    service = make_service(
        tmp_path,
        udi=FakeUdi(statuses=[{}], channels=[]),
    )

    service.update_config({
        "loop_detection_enabled": True,
        "loop_probe_duration_seconds": 180,
        "watch_mode": "periodic",
    })

    status = service.get_status()
    assert status["watch_mode"] == "periodic"
    assert status["has_watcher_api_key"] is True
    assert status["loop_detection_enabled"] is True
    assert status["loop_probe_duration_seconds"] == 180
    assert status["loop_switch_requires_pre_probe"] is True
    assert status["loop_switch_gate_satisfied"] is False
    assert status["loop_detection_gates"] == {
        "enabled": True,
        "active_real_viewer_required": True,
        "confirmation_required": True,
        "cooldown_required": True,
        "switch_rate_limit_required": True,
        "stale_stream_guard_required": True,
        "watcher_recovery_guard_required": True,
        "next_stream_pre_probe_required": True,
        "next_stream_pre_probe_enabled": False,
        "switch_gate_satisfied": False,
    }


def test_status_exposes_shadow_detection_toggle_context(tmp_path):
    service = make_service(
        tmp_path,
        udi=FakeUdi(statuses=[{}], channels=[]),
    )

    service.update_config({
        "freeze_detection_enabled": False,
        "garbled_audio_detection_enabled": True,
        "silent_audio_detection_enabled": True,
        "offline_image_detection_enabled": True,
        "next_stream_pre_probe_enabled": True,
    })

    status = service.get_status()
    assert status["freeze_detection_enabled"] is False
    assert status["garbled_audio_detection_enabled"] is True
    assert status["silent_audio_detection_enabled"] is True
    assert status["offline_image_detection_enabled"] is True
    assert status["next_stream_pre_probe_enabled"] is True
    assert status["offline_image"]["enabled"] is True
    assert status["loop_detection_gates"]["next_stream_pre_probe_enabled"] is True


def test_shadow_loop_detection_switches_after_required_confirmation(tmp_path):
    switch_calls = []
    loop_calls = []
    udi = FakeUdi(
        statuses=[
            {"/proxy/ts/stream/uuid-1": active_status(10)},
            {"/proxy/ts/stream/uuid-1": active_status(10)},
            {"/proxy/ts/stream/uuid-1": active_status(10)},
        ],
        channels=[{"id": 1, "uuid": "uuid-1", "streams": [10, 11]}],
        streams={
            10: {"id": 10, "url": "http://provider.example/old.ts"},
            11: {"id": 11, "url": "http://provider.example/new.ts"},
        },
    )

    def loop_probe(url, config):
        loop_calls.append((url, config["loop_probe_duration_seconds"]))
        loop_detected = "/proxy/ts/stream/" in url
        return {
            "loop_probe_ran": True,
            "loop_detected": loop_detected,
            "loop_duration_secs": 12.5 if loop_detected else 0,
            "loop_frames_processed": 240 if loop_detected else 120,
        }

    service = make_service(
        tmp_path,
        udi=udi,
        blank_probe=lambda url, config: {"blank_detected": False, "freeze_detected": False},
        loop_probe=loop_probe,
        switch_calls=switch_calls,
    )
    config = normalize_config({
        **service.get_config(include_secret=True),
        "loop_detection_enabled": True,
        "loop_probe_duration_seconds": 180,
        "confirmation_count": 1,
        "next_stream_pre_probe_enabled": True,
    })
    service._run_blank_probe = lambda url, config: {
        "blank_detected": False,
        "freeze_detected": False,
        "no_decodable_frames_detected": False,
    }
    target = {
        "channel_uuid": "uuid-1",
        "channel_id": 1,
        "channel_ref": "channel-1",
        "stream_id": 10,
        "stream_ref": "stream-10",
        "real_client_count": 1,
    }

    assert service._probe_target_once(udi, target, config) is False

    assert loop_calls == [
        ("http://dispatcharr.local/proxy/ts/stream/uuid-1", 180),
        ("http://provider.example/new.ts", 180),
    ]
    assert switch_calls == [("uuid-1", 11, None)]
    event = service.get_status()["recent_events"][0]
    assert event["type"] == "switch_success"
    assert event["trigger_reason"] == "loop"
    assert event["details"]["pre_probe"]["result"] == "ok"
    assert event["details"]["detection"]["measurements"] == {
        "loop_duration_secs": 12.5,
        "loop_frames_processed": 240,
    }


def test_shadow_loop_switch_requires_next_stream_pre_probe_gate(tmp_path):
    switch_calls = []
    udi = FakeUdi(
        statuses=[
            {"/proxy/ts/stream/uuid-1": active_status(10)},
            {"/proxy/ts/stream/uuid-1": active_status(10)},
            {"/proxy/ts/stream/uuid-1": active_status(10)},
        ],
        channels=[{"id": 1, "uuid": "uuid-1", "streams": [10, 11]}],
        streams={
            10: {"id": 10, "url": "http://provider.example/old.ts"},
            11: {"id": 11, "url": "http://provider.example/new.ts"},
        },
    )
    service = make_service(
        tmp_path,
        udi=udi,
        blank_probe=lambda url, config: {"blank_detected": False, "freeze_detected": False},
        loop_probe=lambda url, config: {
            "loop_probe_ran": True,
            "loop_detected": True,
            "loop_duration_secs": 18,
            "loop_frames_processed": 360,
        },
        switch_calls=switch_calls,
    )
    service.update_config({
        "enabled": False,
        "loop_detection_enabled": True,
        "confirmation_count": 1,
        "next_stream_pre_probe_enabled": False,
    })
    target = {
        "channel_uuid": "uuid-1",
        "channel_id": 1,
        "channel_ref": "channel-1",
        "stream_id": 10,
        "stream_ref": "stream-10",
        "real_client_count": 1,
    }

    assert service._probe_target_once(udi, target, service.get_config(include_secret=True)) is False

    status = service.get_status()
    assert switch_calls == []
    event = status["recent_events"][0]
    assert event["type"] == "loop_pre_probe_required"
    assert event["decision_group"] == "guard"
    assert event["trigger_reason"] == "loop"
    assert event["details"]["operator_action"] == "enable_next_stream_pre_probe"
    assert event["details"]["detection"]["measurements"] == {
        "loop_duration_secs": 18,
        "loop_frames_processed": 360,
    }
    assert status["loop_switch_gate_satisfied"] is False
    assert status["loop_detection_gates"]["next_stream_pre_probe_required"] is True
    assert status["switch_summary"]["loop_pre_probe_required_skips"] == 1
    assert status["switch_summary"]["prevented_false_switches"] == 1


def test_shadow_loop_detection_is_gated_by_config(tmp_path):
    loop_calls = []
    udi = FakeUdi(
        statuses=[
            {"/proxy/ts/stream/uuid-1": active_status(10)},
            {"/proxy/ts/stream/uuid-1": active_status(10)},
        ],
        channels=[{"id": 1, "uuid": "uuid-1", "streams": [10, 11]}],
        streams={10: {"id": 10}, 11: {"id": 11}},
    )
    service = make_service(
        tmp_path,
        udi=udi,
        blank_probe=lambda url, config: {"blank_detected": False, "freeze_detected": False},
        loop_probe=lambda url, config: loop_calls.append(url) or {"loop_detected": True},
    )
    config = normalize_config({
        **service.get_config(include_secret=True),
        "loop_detection_enabled": False,
        "confirmation_count": 1,
    })
    target = {
        "channel_uuid": "uuid-1",
        "channel_id": 1,
        "channel_ref": "channel-1",
        "stream_id": 10,
        "stream_ref": "stream-10",
        "real_client_count": 1,
    }

    assert service._probe_target_once(udi, target, config) is True

    assert loop_calls == []
    assert service.get_status()["recent_events"][0]["type"] == "probe_ok"


def test_shadow_loop_detection_requires_real_viewer_not_shadow_watcher(tmp_path):
    loop_calls = []
    watcher_client = {
        "user_agent": "StreamFlow-Shadow-Blank-Monitor/1.0",
        "client_id": "shadow-client",
    }
    udi = FakeUdi(
        statuses=[
            {
                "/proxy/ts/stream/uuid-1": active_status(
                    10,
                    clients=[watcher_client],
                )
            },
        ],
        channels=[{"id": 1, "uuid": "uuid-1", "streams": [10, 11]}],
        streams={10: {"id": 10}, 11: {"id": 11}},
    )
    service = make_service(
        tmp_path,
        udi=udi,
        blank_probe=lambda url, config: {"blank_detected": False, "freeze_detected": False},
        loop_probe=lambda url, config: loop_calls.append(url) or {"loop_detected": True},
    )
    service.update_config({
        "enabled": False,
        "loop_detection_enabled": True,
        "confirmation_count": 1,
        "next_stream_pre_probe_enabled": True,
    })

    status = service.run_once(force=True)

    assert loop_calls == []
    assert status["watched_channels"] == []
    assert status["recent_events"] == []


def test_shadow_loop_pre_probe_rejects_looping_alternative(tmp_path):
    udi = FakeUdi(
        statuses=[{"/proxy/ts/stream/uuid-1": active_status(10)}],
        channels=[{"id": 1, "uuid": "uuid-1", "streams": [10, 11]}],
        streams={
            10: {"id": 10, "url": "http://provider.example/old.ts"},
            11: {"id": 11, "url": "http://provider.example/new.ts"},
        },
    )
    service = make_service(tmp_path, udi=udi)
    service._run_blank_probe = lambda url, config: {
        "blank_detected": False,
        "freeze_detected": False,
        "no_decodable_frames_detected": False,
    }
    service.loop_probe = lambda url, config: {
        "loop_probe_ran": True,
        "loop_detected": True,
        "loop_duration_secs": 14.0,
        "loop_frames_processed": 200,
    }
    config = normalize_config({
        **service.get_config(include_secret=True),
        "loop_detection_enabled": True,
        "next_stream_pre_probe_enabled": True,
    })
    target = {
        "channel_uuid": "uuid-1",
        "channel_id": 1,
        "channel_ref": "channel-1",
        "stream_id": 10,
        "stream_ref": "stream-10",
        "real_client_count": 1,
    }

    alternative, details = service._choose_preprobed_alternative_stream(
        udi,
        target,
        config,
        10,
        reason="loop",
    )

    assert alternative is None
    assert details["result"] == "rejected"
    assert details["rejection_reason"] == "loop"
    assert service.get_status()["recent_events"][0]["type"] == "pre_probe_rejected"
    assert service.get_status()["pre_probe"]["last"]["rejection_reason"] == "loop"


def test_shadow_loop_dry_run_records_intended_switch_without_live_change(tmp_path):
    switch_calls = []
    loop_calls = []
    udi = FakeUdi(
        statuses=[
            {"/proxy/ts/stream/uuid-1": active_status(10)},
            {"/proxy/ts/stream/uuid-1": active_status(10)},
            {"/proxy/ts/stream/uuid-1": active_status(10)},
        ],
        channels=[{"id": 1, "uuid": "uuid-1", "streams": [10, 11]}],
        streams={
            10: {"id": 10, "url": "http://provider.example/old.ts"},
            11: {"id": 11, "url": "http://provider.example/new.ts"},
        },
    )

    def loop_probe(url, config):
        loop_calls.append(url)
        return {
            "loop_probe_ran": True,
            "loop_detected": "/proxy/ts/stream/" in url,
            "loop_duration_secs": 12.0,
            "loop_frames_processed": 180,
        }

    service = make_service(
        tmp_path,
        udi=udi,
        blank_probe=lambda url, config: {"blank_detected": False, "freeze_detected": False},
        loop_probe=loop_probe,
        switch_calls=switch_calls,
    )
    service._run_blank_probe = lambda url, config: {
        "blank_detected": False,
        "freeze_detected": False,
        "no_decodable_frames_detected": False,
    }
    service.update_config({
        "enabled": False,
        "dry_run": True,
        "loop_detection_enabled": True,
        "confirmation_count": 1,
        "next_stream_pre_probe_enabled": True,
    })

    status = service.run_once(force=True)

    assert loop_calls == [
        "http://dispatcharr.local/proxy/ts/stream/uuid-1",
        "http://provider.example/new.ts",
    ]
    assert switch_calls == []
    assert status["recent_events"][0]["type"] == "dry_run_switch"
    assert status["recent_events"][0]["trigger_reason"] == "loop"
    assert status["recent_events"][0]["details"]["pre_probe"]["result"] == "ok"
    assert status["cooldowns"][0]["channel_ref"] == status["recent_events"][0]["channel_ref"]
    assert status["switch_summary"]["dry_run_switches"] == 1
    assert status["switch_summary"]["successful_switches"] == 0
    assert status["switch_summary"]["last_switch_reason"] == "loop"


def test_shadow_loop_stale_stream_guard_skips_switch_if_stream_changes(tmp_path):
    switch_calls = []
    udi = FakeUdi(
        statuses=[
            {"/proxy/ts/stream/uuid-1": active_status(10)},
            {"/proxy/ts/stream/uuid-1": active_status(10)},
            {"/proxy/ts/stream/uuid-1": active_status(11)},
        ],
        channels=[{"id": 1, "uuid": "uuid-1", "streams": [10, 11, 12]}],
        streams={11: {"id": 11, "url": "http://provider.example/new.ts"}},
    )
    service = make_service(
        tmp_path,
        udi=udi,
        blank_probe=lambda url, config: {"blank_detected": False, "freeze_detected": False},
        loop_probe=lambda url, config: {
            "loop_probe_ran": True,
            "loop_detected": True,
            "loop_duration_secs": 15.0,
            "loop_frames_processed": 240,
        },
        switch_calls=switch_calls,
    )
    service.update_config({
        "enabled": False,
        "dry_run": False,
        "loop_detection_enabled": True,
        "confirmation_count": 1,
        "next_stream_pre_probe_enabled": True,
    })
    target = {
        "channel_uuid": "uuid-1",
        "channel_id": 1,
        "channel_ref": "channel-1",
        "stream_id": 10,
        "stream_ref": "stream-10",
        "real_client_count": 1,
    }

    assert service._probe_target_once(udi, target, service.get_config(include_secret=True)) is False

    status = service.get_status()
    assert switch_calls == []
    assert status["recent_events"][0]["type"] == "stale_stream_guard"
    assert status["recent_events"][0]["trigger_reason"] == "loop"
    assert status["switch_summary"]["stale_stream_guard_skips"] == 1
    assert status["switch_summary"]["prevented_false_switches"] == 1


def test_shadow_loop_rate_limit_blocks_switch_path(tmp_path):
    switch_calls = []
    udi = FakeUdi(
        statuses=[
            {"/proxy/ts/stream/uuid-1": active_status(10)},
            {"/proxy/ts/stream/uuid-1": active_status(10)},
            {"/proxy/ts/stream/uuid-1": active_status(10)},
        ],
        channels=[{"id": 1, "uuid": "uuid-1", "streams": [10, 11]}],
        streams={11: {"id": 11, "url": "http://provider.example/new.ts"}},
    )
    service = make_service(
        tmp_path,
        udi=udi,
        blank_probe=lambda url, config: {"blank_detected": False, "freeze_detected": False},
        loop_probe=lambda url, config: {
            "loop_probe_ran": True,
            "loop_detected": True,
            "loop_duration_secs": 9.0,
            "loop_frames_processed": 120,
        },
        switch_calls=switch_calls,
        clock=lambda: 1000.0,
    )
    with service._lock:
        service._switch_history["uuid-1"].append(999.0)
    service.update_config({
        "enabled": False,
        "dry_run": False,
        "loop_detection_enabled": True,
        "confirmation_count": 1,
        "next_stream_pre_probe_enabled": True,
        "max_switches_per_hour": 1,
    })
    target = {
        "channel_uuid": "uuid-1",
        "channel_id": 1,
        "channel_ref": "channel-1",
        "stream_id": 10,
        "stream_ref": "stream-10",
        "real_client_count": 1,
    }

    assert service._probe_target_once(udi, target, service.get_config(include_secret=True)) is False

    status = service.get_status()
    assert switch_calls == []
    assert status["recent_events"][0]["type"] == "switch_rate_limited"
    assert status["recent_events"][0]["trigger_reason"] == "loop"
    assert status["switch_summary"]["rate_limited_skips"] == 1
    assert status["switch_summary"]["prevented_false_switches"] == 1


def test_shadow_loop_cooldown_blocks_repeated_probe_switch(tmp_path):
    switch_calls = []
    udi = FakeUdi(
        statuses=[
            {"/proxy/ts/stream/uuid-1": active_status(10)},
            {"/proxy/ts/stream/uuid-1": active_status(10)},
        ],
        channels=[{"id": 1, "uuid": "uuid-1", "streams": [10, 11]}],
        streams={11: {"id": 11, "url": "http://provider.example/new.ts"}},
    )
    service = make_service(
        tmp_path,
        udi=udi,
        blank_probe=lambda url, config: {"blank_detected": False, "freeze_detected": False},
        loop_probe=lambda url, config: {
            "loop_probe_ran": True,
            "loop_detected": True,
            "loop_duration_secs": 10.0,
            "loop_frames_processed": 120,
        },
        switch_calls=switch_calls,
        clock=lambda: 1000.0,
    )
    service.update_config({
        "enabled": False,
        "dry_run": False,
        "loop_detection_enabled": True,
        "confirmation_count": 1,
        "next_stream_pre_probe_enabled": True,
        "channel_cooldown_seconds": 300,
    })
    service._set_cooldown("uuid-1", service.get_config(include_secret=True))
    target = {
        "channel_uuid": "uuid-1",
        "channel_id": 1,
        "channel_ref": "channel-1",
        "stream_id": 10,
        "stream_ref": "stream-10",
        "real_client_count": 1,
    }

    assert service._probe_target_once(udi, target, service.get_config(include_secret=True)) is True

    status = service.get_status()
    assert switch_calls == []
    assert status["recent_events"][0]["type"] == "cooldown"
    assert status["recent_events"][0]["trigger_reason"] == "loop"
    assert status["recent_events"][0]["details"]["cooldown_seconds"] == 300
    assert status["switch_summary"]["cooldown_skips"] == 1
    assert status["switch_summary"]["successful_switches"] == 0


def test_learn_offline_image_from_current_frame_adds_hash(tmp_path, monkeypatch):
    service = make_service(
        tmp_path,
        udi=FakeUdi(statuses=[{}], channels=[]),
    )
    with service._lock:
        service._watched["uuid-1"] = {
            "channel_uuid": "uuid-1",
            "channel_id": 1,
            "channel_ref": "channel-safe",
            "stream_id": 10,
            "stream_ref": "stream-safe",
            "real_client_count": 1,
        }

    monkeypatch.setattr(
        service,
        "_capture_offline_image_hash",
        lambda url, config: {"success": True, "offline_image_hash": "0123456789abcdef"},
    )

    result = service.learn_offline_image_from_current_frame(channel_ref="channel-safe")

    assert result["success"] is True
    assert result["learned"] is True
    assert result["deduplicated"] is False
    assert result["offline_image_hash"] == "0123456789abcdef"
    assert result["config"]["offline_image_reference_hashes"] == ["0123456789abcdef"]
    assert result["status"]["recent_events"][0]["type"] == "offline_image_learned"


def test_learn_offline_image_deduplicates_near_existing_hash(tmp_path, monkeypatch):
    service = make_service(
        tmp_path,
        udi=FakeUdi(statuses=[{}], channels=[]),
    )
    service.update_config({
        "offline_image_reference_hashes": ["0123456789abcdee"],
        "offline_image_hash_threshold": 4,
    })
    with service._lock:
        service._watched["uuid-1"] = {
            "channel_uuid": "uuid-1",
            "channel_ref": "channel-safe",
            "stream_ref": "stream-safe",
            "real_client_count": 1,
        }

    monkeypatch.setattr(
        service,
        "_capture_offline_image_hash",
        lambda url, config: {"success": True, "offline_image_hash": "0123456789abcdef"},
    )

    result = service.learn_offline_image_from_current_frame()

    assert result["success"] is True
    assert result["learned"] is False
    assert result["deduplicated"] is True
    assert result["offline_image_distance"] == 1
    assert result["config"]["offline_image_reference_hashes"] == ["0123456789abcdee"]


def test_learn_offline_image_handler_reports_missing_watched_channel():
    class EmptyService:
        def learn_offline_image_from_current_frame(self, **_kwargs):
            return {
                "success": False,
                "reason": "no_watched_channel",
                "message": "No active Shadow-watched channel is available.",
            }

    app = Flask(__name__)
    with app.app_context():
        response, status_code = learn_shadow_offline_image_response(
            payload={},
            get_service=lambda: EmptyService(),
        )

    assert status_code == 400
    assert response.get_json()["code"] == "no_watched_channel"


def test_start_requires_watcher_api_key(tmp_path):
    udi = FakeUdi(
        statuses=[{"uuid-1": active_status()}],
        channels=[{"id": 1, "uuid": "uuid-1", "streams": [10, 11]}],
    )
    service = make_service(tmp_path, udi=udi, watcher_api_key=None)

    assert service.start() is False

    status = service.get_status()
    config = service.get_config()
    assert status["configuration_required"] is True
    assert status["configuration_issue"] == "watcher_api_key_required"
    assert status["running"] is False
    assert config["enabled"] is False


def test_start_enables_configured_paused_monitor(tmp_path):
    udi = FakeUdi(
        statuses=[{"uuid-1": active_status()}],
        channels=[{"id": 1, "uuid": "uuid-1", "streams": [10, 11]}],
    )
    service = make_service(tmp_path, udi=udi)

    try:
        assert service.get_config()["enabled"] is False
        assert service.start() is True

        status = service.get_status()
        config = service.get_config()
        assert config["enabled"] is True
        assert status["enabled"] is True
        assert status["running"] is True
        assert status["configuration_required"] is False
    finally:
        service.stop()


def test_start_handler_enables_configured_paused_monitor(tmp_path):
    udi = FakeUdi(
        statuses=[{"uuid-1": active_status()}],
        channels=[{"id": 1, "uuid": "uuid-1", "streams": [10, 11]}],
    )
    service = make_service(tmp_path, udi=udi)

    app = Flask(__name__)
    with app.app_context():
        try:
            response, status_code = start_shadow_blank_monitor_response(get_service=lambda: service)
            payload = response.get_json()

            assert status_code == 200
            assert payload["success"] is True
            assert payload["status"]["enabled"] is True
            assert payload["status"]["running"] is True
            assert service.get_config()["enabled"] is True
        finally:
            service.stop()


def test_enabled_config_without_watcher_api_key_stays_off(tmp_path):
    udi = FakeUdi(
        statuses=[{"uuid-1": active_status()}],
        channels=[{"id": 1, "uuid": "uuid-1", "streams": [10, 11]}],
    )
    service = make_service(tmp_path, udi=udi, watcher_api_key=None)

    config = service.update_config({"enabled": True})
    status = service.get_status()

    assert config["enabled"] is False
    assert status["enabled"] is False
    assert status["running"] is False
    assert status["configuration_required"] is True
    assert status["last_error"] == "Watcher API Key is required before Shadow Monitor can start."


def test_forced_scan_requires_watcher_api_key(tmp_path):
    udi = FakeUdi(
        statuses=[{"uuid-1": active_status()}],
        channels=[{"id": 1, "uuid": "uuid-1", "streams": [10, 11]}],
    )
    service = make_service(tmp_path, udi=udi, watcher_api_key=None)

    status = service.run_once(force=True)

    assert status["configuration_required"] is True
    assert status["watched_count"] == 0
    assert status["last_scan_at"] is None
    assert udi.status_calls == 0


def test_scan_error_status_does_not_expose_raw_exception(tmp_path):
    def broken_udi():
        raise RuntimeError("secret dispatcharr stack trace path")

    service = ShadowBlankMonitorService(
        config_file=tmp_path / "shadow.json",
        udi_provider=broken_udi,
        switch_stream=lambda *_args, **_kwargs: True,
        base_url_provider=lambda: "http://dispatcharr.local",
        blank_probe=lambda url, config: {"blank_detected": False},
        stream_checker_provider=lambda: FakeStreamChecker(),
        clock=lambda: 1000.0,
    )
    with service._lock:
        service._config["watcher_api_key"] = "test-watcher-key"
        service._save_config()

    status = service.run_once(force=True)

    assert status["last_error"] == SHADOW_MONITOR_SCAN_ERROR_MESSAGE
    assert "secret" not in str(status)


def test_shadow_monitor_handler_configuration_details_are_sanitized():
    class FakeService:
        def run_once(self, force=False):
            return {
                "configuration_required": True,
                "configuration_issue": "watcher_api_key_required",
                "configuration_message": "Watcher API Key is required before Shadow Monitor can start.",
                "last_error": "secret stack trace path",
                "recent_events": [{"error": "secret stack trace path"}],
            }

        def start(self):
            return False

        def get_status(self):
            return self.run_once(force=True)

    app = Flask(__name__)
    with app.app_context():
        response, status_code = run_shadow_blank_monitor_once_response(get_service=lambda: FakeService())

    payload = response.get_json()
    assert status_code == 400
    assert payload["code"] == "watcher_api_key_required"
    assert "last_error" not in payload["details"]["status"]
    assert "recent_events" not in payload["details"]["status"]
    assert "secret" not in str(payload)

    with app.app_context():
        start_response, start_status_code = start_shadow_blank_monitor_response(get_service=lambda: FakeService())

    start_payload = start_response.get_json()
    assert start_status_code == 400
    assert "secret" not in str(start_payload)


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


def test_continuous_probe_command_keeps_connection_open():
    config = normalize_config({
        "watch_mode": "continuous",
        "probe_duration_seconds": 8,
    })

    periodic_command, _ = ShadowBlankMonitorService._blank_probe_command(
        "http://dispatcharr.local/proxy/ts/stream/uuid-1",
        config,
    )
    continuous_command, _ = ShadowBlankMonitorService._blank_probe_command(
        "http://dispatcharr.local/proxy/ts/stream/uuid-1",
        config,
        continuous=True,
    )

    assert "-t" in periodic_command
    assert "-t" not in continuous_command


def test_continuous_probe_detects_open_blank_after_min_duration(tmp_path, monkeypatch):
    processes = []

    class FakeProcess:
        def __init__(self):
            self.stderr = io.StringIO("[blackdetect @ 000] black_start:0\n")
            self.returncode = None

        def poll(self):
            return self.returncode

        def terminate(self):
            self.returncode = 0

        def kill(self):
            self.returncode = -9

        def wait(self, timeout=None):
            if self.returncode is None:
                self.returncode = 0
            return self.returncode

    def fake_popen(command, **kwargs):
        process = FakeProcess()
        processes.append(process)
        return process

    current_time = {"value": 100.0}

    def fake_monotonic():
        current_time["value"] += 1.1
        return current_time["value"]

    monkeypatch.setattr(shadow_module.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(shadow_module.time, "monotonic", fake_monotonic)
    monkeypatch.setattr(shadow_module.time, "sleep", lambda _seconds: None)

    udi = FakeUdi(
        statuses=[{"uuid-1": active_status(stream_id=10)}],
        channels=[{"id": 1, "uuid": "uuid-1", "streams": [10, 11]}],
    )
    service = make_service(tmp_path, udi=udi)
    config = normalize_config({
        "watch_mode": "continuous",
        "probe_duration_seconds": 60,
        "blank_min_duration_seconds": 2,
        "blank_ratio_threshold": 0.8,
    })

    result = service._run_blank_probe_until_viewer_left(
        "http://dispatcharr.local/proxy/ts/stream/uuid-1",
        config,
        udi,
        {
            "channel_uuid": "uuid-1",
            "channel_id": 1,
            "stream_id": 10,
        },
    )

    assert processes
    assert result["blank_detected"] is True
    assert result["blank_duration_secs"] < 10
    assert result["blank_ratio"] < 0.8


def test_continuous_probe_detects_open_freeze_after_min_duration(tmp_path, monkeypatch):
    processes = []

    class FakeProcess:
        def __init__(self):
            self.stderr = io.StringIO("[freezedetect @ 000] lavfi.freezedetect.freeze_start: 0\n")
            self.returncode = None

        def poll(self):
            return self.returncode

        def terminate(self):
            self.returncode = 0

        def kill(self):
            self.returncode = -9

        def wait(self, timeout=None):
            if self.returncode is None:
                self.returncode = 0
            return self.returncode

    def fake_popen(command, **kwargs):
        process = FakeProcess()
        processes.append(process)
        return process

    current_time = {"value": 100.0}

    def fake_monotonic():
        current_time["value"] += 1.1
        return current_time["value"]

    monkeypatch.setattr(shadow_module.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(shadow_module.time, "monotonic", fake_monotonic)
    monkeypatch.setattr(shadow_module.time, "sleep", lambda _seconds: None)

    udi = FakeUdi(
        statuses=[{"uuid-1": active_status(stream_id=10)}],
        channels=[{"id": 1, "uuid": "uuid-1", "streams": [10, 11]}],
    )
    service = make_service(tmp_path, udi=udi)
    config = normalize_config({
        "watch_mode": "continuous",
        "probe_duration_seconds": 60,
        "freeze_detection_enabled": True,
        "freeze_min_duration_seconds": 2,
        "freeze_ratio_threshold": 0.8,
    })

    result = service._run_blank_probe_until_viewer_left(
        "http://dispatcharr.local/proxy/ts/stream/uuid-1",
        config,
        udi,
        {
            "channel_uuid": "uuid-1",
            "channel_id": 1,
            "stream_id": 10,
        },
    )

    assert processes
    assert result["freeze_detected"] is True
    assert result["freeze_duration_secs"] < 10
    assert result["freeze_ratio"] < 0.8
    assert result.get("blank_detected") is False


def test_continuous_probe_detects_no_decodable_frames_after_min_duration(tmp_path, monkeypatch):
    processes = []

    class FakeProcess:
        def __init__(self):
            self.stderr = io.StringIO(
                "Could not find codec parameters for stream 0 (Video: h264, none): unspecified size\n"
                "Output file does not contain any stream\n"
            )
            self.returncode = None

        def poll(self):
            return self.returncode

        def terminate(self):
            self.returncode = 0

        def kill(self):
            self.returncode = -9

        def wait(self, timeout=None):
            if self.returncode is None:
                self.returncode = 0
            return self.returncode

    def fake_popen(command, **kwargs):
        process = FakeProcess()
        processes.append(process)
        return process

    current_time = {"value": 100.0}

    def fake_monotonic():
        current_time["value"] += 1.1
        return current_time["value"]

    monkeypatch.setattr(shadow_module.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(shadow_module.time, "monotonic", fake_monotonic)
    monkeypatch.setattr(shadow_module.time, "sleep", lambda _seconds: None)

    udi = FakeUdi(
        statuses=[{"uuid-1": active_status(stream_id=10)}],
        channels=[{"id": 1, "uuid": "uuid-1", "streams": [10, 11]}],
    )
    service = make_service(tmp_path, udi=udi)
    config = normalize_config({
        "watch_mode": "continuous",
        "probe_duration_seconds": 60,
        "no_decodable_frames_min_duration_seconds": 10,
    })

    result = service._run_blank_probe_until_viewer_left(
        "http://dispatcharr.local/proxy/ts/stream/uuid-1",
        config,
        udi,
        {
            "channel_uuid": "uuid-1",
            "channel_id": 1,
            "stream_id": 10,
        },
    )

    assert processes
    assert result["no_decodable_frames_detected"] is True
    assert result["no_decodable_frames_duration_secs"] >= 10
    assert result["no_decodable_frames_error"] in {
        "could not find codec parameters",
        "output file does not contain any stream",
    }
    assert result.get("blank_detected") is False


def test_continuous_probe_holds_ffmpeg_until_viewer_leaves(tmp_path, monkeypatch):
    processes = []
    commands = []

    class FakeProcess:
        def __init__(self):
            self.stderr = io.StringIO("")
            self.returncode = None
            self.terminated = False

        def poll(self):
            return self.returncode

        def terminate(self):
            self.terminated = True
            self.returncode = 0

        def kill(self):
            self.returncode = -9

        def wait(self, timeout=None):
            if self.returncode is None:
                self.returncode = 0
            return self.returncode

        def communicate(self, timeout=None):
            return None, ""

    def fake_popen(command, **kwargs):
        commands.append(command)
        process = FakeProcess()
        processes.append(process)
        return process

    current_time = {"value": 100.0}

    def fake_monotonic():
        current_time["value"] += 1.1
        return current_time["value"]

    monkeypatch.setattr(shadow_module.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(shadow_module.time, "monotonic", fake_monotonic)
    monkeypatch.setattr(shadow_module.time, "sleep", lambda _seconds: None)

    udi = FakeUdi(
        statuses=[
            {"uuid-1": active_status(stream_id=10)},
            {
                "uuid-1": active_status(
                    stream_id=10,
                    clients=[{"user_agent": "StreamFlow-Shadow-Blank-Monitor/1.0"}],
                )
            },
        ],
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
    config = normalize_config({
        "enabled": False,
        "dry_run": False,
        "watch_mode": "continuous",
    })
    target = {
        "channel_uuid": "uuid-1",
        "channel_id": 1,
        "channel_ref": "channel-test",
        "stream_id": 10,
        "stream_ref": "stream-test",
        "real_client_count": 1,
        "watcher_client_count": 0,
    }

    result = service._run_blank_probe_until_viewer_left(
        "http://dispatcharr.local/proxy/ts/stream/uuid-1",
        config,
        udi,
        target,
    )

    assert result["viewer_left"] is True
    assert processes[0].terminated is True
    assert "-t" not in commands[0]


def test_continuous_probe_treats_aggregate_only_single_client_as_viewer_left(tmp_path, monkeypatch):
    process = PersistentFakeProbeProcess([])
    commands = []

    def fake_popen(command, **kwargs):
        commands.append(command)
        return process

    current_time = {"value": 100.0}

    def fake_monotonic():
        current_time["value"] += 1.1
        return current_time["value"]

    monkeypatch.setattr(shadow_module.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(shadow_module.time, "monotonic", fake_monotonic)
    monkeypatch.setattr(shadow_module.time, "sleep", lambda _seconds: None)

    udi = FakeUdi(
        statuses=[{
            "uuid-1": {
                "state": "active",
                "channel_id": "uuid-1",
                "stream_id": 10,
                "client_count": 1,
            },
        }],
        channels=[{"id": 1, "uuid": "uuid-1", "streams": [10, 11]}],
    )
    service = ShadowBlankMonitorService(
        config_file=tmp_path / "shadow.json",
        udi_provider=lambda: udi,
        switch_stream=lambda *args, **kwargs: True,
        base_url_provider=lambda: "http://dispatcharr.local",
        stream_checker_provider=lambda: FakeStreamChecker(),
        clock=lambda: 1003.0,
    )
    config = normalize_config({
        "enabled": False,
        "dry_run": False,
        "watch_mode": "continuous",
    })
    target = {
        "channel_uuid": "uuid-1",
        "channel_id": 1,
        "channel_ref": "channel-test",
        "stream_id": 10,
        "stream_ref": "stream-test",
        "real_client_count": 1,
        "watcher_client_count": 0,
        "active_probe_started_at": 1000.0,
    }

    result = service._run_blank_probe_until_viewer_left(
        "http://dispatcharr.local/proxy/ts/stream/uuid-1",
        config,
        udi,
        target,
    )

    assert result["viewer_left"] is True
    assert process.returncode == 1
    assert "-t" not in commands[0]


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


def test_discovery_tracks_all_real_viewer_channels_with_excludes_only(tmp_path):
    udi = FakeUdi(
        statuses=[{
            "uuid-1": active_status(stream_id=10, clients=[{"user_agent": "VLC"}]),
            "uuid-2": {
                "state": "active",
                "channel_id": "uuid-2",
                "stream_id": 20,
                "clients": [{"user_agent": "VLC"}],
            },
            "uuid-3": {
                "state": "active",
                "channel_id": "uuid-3",
                "stream_id": 30,
                "clients": [{"user_agent": "VLC"}],
            },
            "uuid-4": {
                "state": "active",
                "channel_id": "uuid-4",
                "stream_id": 40,
                "clients": [{"user_agent": "VLC"}],
            },
        }],
        channels=[
            {"id": 1, "uuid": "uuid-1", "streams": [10, 11]},
            {"id": 2, "uuid": "uuid-2", "streams": [20, 21]},
            {"id": 4, "uuid": "uuid-4", "streams": [40, 41]},
        ],
    )
    service = make_service(tmp_path, udi=udi)
    config = normalize_config({
        "excluded_channel_ids": [2],
        "excluded_channel_uuids": ["uuid-4"],
        "max_concurrent_watchers": 1,
    })

    targets = service.discover_active_targets(udi, config)

    assert {(target["channel_id"], target["channel_uuid"]) for target in targets} == {
        (1, "uuid-1"),
        (None, "uuid-3"),
    }
    assert all(target["real_client_count"] == 1 for target in targets)
    assert "included_channel_ids" not in normalize_config({})
    assert service.get_status()["watched_count"] == 2


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
    assert status["recent_events"][0]["decision_group"] == "switch"
    assert status["recent_events"][0]["trigger_reason"] == "blank"
    assert status["recent_events"][0]["details"]["origin_stream_ref"].startswith("stream-")
    assert status["recent_events"][0]["details"]["target_stream_ref"].startswith("stream-")
    assert status["recent_events"][0]["details"]["viewer_context"]["real_client_count"] == 1
    detection = status["recent_events"][0]["details"]["detection"]
    assert detection["reason"] == "blank"
    assert detection["measurements"]["blank_ratio"] == 1.0
    assert detection["measurements"]["blank_duration_secs"] == 8.0
    assert detection["thresholds"]["blank_ratio_threshold"] == 0.8
    assert status["cooldowns"][0]["channel_ref"] == status["recent_events"][0]["channel_ref"]
    assert status["switch_summary"]["dry_run_switches"] == 1
    assert status["switch_summary"]["successful_switches"] == 0
    assert status["switch_summary"]["last_switch_reason"] == "blank"


def test_shadow_probe_mirrors_real_viewer_fmp4_output_format(tmp_path):
    probe_urls = []

    udi = FakeUdi(
        statuses=[{
            "uuid-1": active_status(
                stream_id=10,
                clients=[{"user_agent": "VLC", "output_format": "fmp4"}],
            ),
        }],
        channels=[{"id": 1, "uuid": "uuid-1", "streams": [10, 11]}],
    )
    service = make_service(
        tmp_path,
        udi=udi,
        blank_probe=lambda url, _config: probe_urls.append(url) or {"blank_detected": False},
    )
    service.update_config({"enabled": False, "dry_run": False})

    status = service.run_once(force=True)

    assert probe_urls == ["http://dispatcharr.local/proxy/ts/stream/uuid-1?output_format=fmp4"]
    assert status["watched_channels"][0]["viewer_output_format"] == "fmp4"


def test_shadow_probe_mirrors_real_viewer_legacy_ts_output_format(tmp_path):
    probe_urls = []

    udi = FakeUdi(
        statuses=[{
            "uuid-1": active_status(
                stream_id=10,
                clients=[{"user_agent": "VLC", "output_format": "ts"}],
            ),
        }],
        channels=[{"id": 1, "uuid": "uuid-1", "streams": [10, 11]}],
    )
    service = make_service(
        tmp_path,
        udi=udi,
        blank_probe=lambda url, _config: probe_urls.append(url) or {"blank_detected": False},
    )
    service.update_config({"enabled": False, "dry_run": False})

    status = service.run_once(force=True)

    assert probe_urls == ["http://dispatcharr.local/proxy/ts/stream/uuid-1?output_format=mpegts"]
    assert status["watched_channels"][0]["viewer_output_format"] == "mpegts"


def test_shadow_probe_does_not_mirror_watcher_or_invalid_output_format(tmp_path):
    probe_urls = []

    watcher_contaminated_status = active_status(
        stream_id=10,
        clients=[
            {"user_agent": "VLC", "output_format": "hls"},
            {"user_agent": "StreamFlow-Shadow-Blank-Monitor/1.0", "output_format": "fmp4"},
        ],
    )
    udi = FakeUdi(
        statuses=[{
            "uuid-1": active_status(
                stream_id=10,
                clients=[{"user_agent": "VLC", "output_format": "hls"}],
            ),
        }],
        channels=[{"id": 1, "uuid": "uuid-1", "streams": [10, 11]}],
    )
    service = make_service(
        tmp_path,
        udi=udi,
        blank_probe=lambda url, _config: probe_urls.append(url) or {"blank_detected": False},
    )
    service.update_config({"enabled": False, "dry_run": False})

    assert service._real_viewer_output_format(watcher_contaminated_status, normalize_config({})) is None

    status = service.run_once(force=True)

    assert probe_urls == ["http://dispatcharr.local/proxy/ts/stream/uuid-1"]
    assert "viewer_output_format" not in status["watched_channels"][0]


def test_force_run_once_clears_stop_event_for_disabled_continuous_scan(monkeypatch, tmp_path):
    switch_calls = []
    udi = FakeUdi(
        statuses=[
            {"uuid-1": active_status(stream_id=10)},
            {"uuid-1": active_status(stream_id=10)},
        ],
        channels=[{"id": 1, "uuid": "uuid-1", "streams": [10, 11]}],
    )

    def switch_stream(channel_id, stream_id=None, url=None):
        switch_calls.append((channel_id, stream_id, url))
        return True

    service = ShadowBlankMonitorService(
        config_file=tmp_path / "shadow.json",
        udi_provider=lambda: udi,
        switch_stream=switch_stream,
        base_url_provider=lambda: "http://dispatcharr.local",
        stream_checker_provider=lambda: FakeStreamChecker(),
        clock=lambda: 1000.0,
    )
    with service._lock:
        service._config["watcher_api_key"] = "test-watcher-key"
        service._save_config()
    service.update_config({
        "enabled": False,
        "dry_run": True,
        "watch_mode": "continuous",
        "confirmation_count": 1,
        "silent_audio_detection_enabled": True,
        "next_stream_pre_probe_enabled": False,
    })
    assert service._stop_event.is_set()

    stop_event_states = []

    def continuous_probe(url, config, udi_arg, target):
        stop_event_states.append(service._stop_event.is_set())
        return {
            "blank_detected": False,
            "freeze_detected": False,
            "no_decodable_frames_detected": False,
            "garbled_audio_detected": False,
            "silent_audio_detected": True,
            "silent_audio_duration_secs": 12.0,
            "silent_audio_noise_db": -50,
            "audio_stream_present": True,
        }

    monkeypatch.setattr(service, "_run_blank_probe_until_viewer_left", continuous_probe)

    status = service.run_once(force=True)

    assert stop_event_states == [False]
    assert service._stop_event.is_set()
    assert status["recent_events"][0]["type"] == "dry_run_switch"
    assert status["recent_events"][0]["details"]["reason"] == "silent_audio"


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
    assert status["recent_events"][0] == status["decision_history"][0]
    assert status["recent_events"][0]["decision_group"] == "switch"
    assert status["recent_events"][0]["trigger_reason"] == "blank"
    assert status["recent_events"][0]["details"]["post_switch_verification"] is True
    assert status["cooldowns"] == []
    assert status["switch_summary"]["successful_switches"] == 1
    assert status["switch_summary"]["last_switch_reason"] == "blank"
    assert status["switch_summary"]["prevented_false_switches"] == 0


def test_next_stream_pre_probe_disabled_preserves_direct_switch(tmp_path):
    switch_calls = []
    udi = FakeUdi(
        statuses=[{"uuid-1": active_status()}],
        channels=[{"id": 1, "uuid": "uuid-1", "streams": [10, 11, 12]}],
        streams={11: {"id": 11, "url": "http://candidate.local/bad"}},
    )
    service = make_service(
        tmp_path,
        udi=udi,
        blank_probe=lambda url, config: {"blank_detected": True},
        switch_calls=switch_calls,
    )

    def fail_if_pre_probe_runs(url, config):
        raise AssertionError("next-stream pre-probe should be disabled")

    service._run_blank_probe = fail_if_pre_probe_runs
    service.update_config({
        "enabled": False,
        "dry_run": False,
        "confirmation_count": 1,
        "next_stream_pre_probe_enabled": False,
    })

    status = service.run_once(force=True)

    assert switch_calls == [("uuid-1", 11, None)]
    assert status["recent_events"][0]["type"] == "switch_success"


def test_next_stream_pre_probe_skips_bad_candidate(tmp_path):
    switch_calls = []
    udi = FakeUdi(
        statuses=[{"uuid-1": active_status()}],
        channels=[{"id": 1, "uuid": "uuid-1", "streams": [10, 11, 12]}],
        streams={
            11: {"id": 11, "url": "http://candidate.local/bad"},
            12: {"id": 12, "url": "http://candidate.local/good"},
        },
    )
    service = make_service(
        tmp_path,
        udi=udi,
        blank_probe=lambda url, config: {"blank_detected": True},
        switch_calls=switch_calls,
    )
    pre_probe_calls = []

    def pre_probe(url, config):
        pre_probe_calls.append((url, config["probe_duration_seconds"]))
        return {"blank_detected": "bad" in url}

    service._run_blank_probe = pre_probe
    service.update_config({
        "enabled": False,
        "dry_run": False,
        "confirmation_count": 1,
        "next_stream_pre_probe_enabled": True,
        "next_stream_pre_probe_duration_seconds": 5,
    })

    status = service.run_once(force=True)

    assert pre_probe_calls == [
        ("http://candidate.local/bad", 5),
        ("http://candidate.local/good", 5),
    ]
    assert switch_calls == [("uuid-1", 12, None)]
    assert status["recent_events"][0]["type"] == "switch_success"
    assert status["recent_events"][0]["details"]["pre_probe"]["result"] == "ok"
    assert status["recent_events"][0]["details"]["pre_probe"]["pre_probe_metric"] == "preprobe_success"
    assert status["pre_probe"]["metrics"]["preprobe_attempted"] == 2
    assert status["pre_probe"]["metrics"]["preprobe_rejected_media_fault"] == 1
    assert status["pre_probe"]["metrics"]["preprobe_success"] == 1
    assert status["pre_probe"]["last"]["metric"] == "preprobe_success"
    assert status["recent_events"][1]["type"] == "pre_probe_rejected"
    assert status["recent_events"][1]["decision_group"] == "pre_probe"
    assert status["recent_events"][1]["trigger_reason"] == "blank"
    assert status["recent_events"][1]["details"]["rejection_reason"] == "blank"
    assert status["recent_events"][1]["details"]["pre_probe_metric"] == "preprobe_rejected_media_fault"


def test_next_stream_pre_probe_rejects_missing_audio_candidate(tmp_path):
    switch_calls = []
    udi = FakeUdi(
        statuses=[{"uuid-1": active_status()}],
        channels=[{"id": 1, "uuid": "uuid-1", "streams": [10, 11, 12]}],
        streams={
            11: {"id": 11, "url": "http://candidate.local/no-audio"},
            12: {"id": 12, "url": "http://candidate.local/good"},
        },
    )
    service = make_service(
        tmp_path,
        udi=udi,
        blank_probe=lambda url, config: {
            "blank_detected": False,
            "freeze_detected": False,
            "silent_audio_detected": True,
            "silent_audio_duration_secs": 12.0,
        },
        switch_calls=switch_calls,
    )
    pre_probe_calls = []
    missing_audio_output = "Stream specifier ':a' in filtergraph description matches no streams.\n"

    def pre_probe(url, config):
        pre_probe_calls.append(url)
        if "no-audio" not in url:
            return {"blank_detected": False, "freeze_detected": False}
        return {
            "blank_detected": False,
            "freeze_detected": False,
            **ShadowBlankMonitorService._parse_audio_detection(
                missing_audio_output,
                normalize_config(config),
                observed_duration=0.25,
            ),
        }

    service._run_blank_probe = pre_probe
    service.update_config({
        "enabled": False,
        "dry_run": False,
        "confirmation_count": 1,
        "silent_audio_detection_enabled": True,
        "next_stream_pre_probe_enabled": True,
    })

    status = service.run_once(force=True)

    assert pre_probe_calls == [
        "http://candidate.local/no-audio",
        "http://candidate.local/good",
    ]
    assert switch_calls == [("uuid-1", 12, None)]
    assert status["recent_events"][0]["type"] == "switch_success"
    assert status["recent_events"][0]["details"]["pre_probe"]["result"] == "ok"
    assert status["recent_events"][1]["type"] == "pre_probe_rejected"
    assert status["recent_events"][1]["details"]["rejection_reason"] == "silent_audio"
    assert status["recent_events"][1]["details"]["silent_audio_detected"] is True
    assert status["recent_events"][1]["details"]["audio_stream_present"] is False
    assert status["pre_probe"]["metrics"]["preprobe_rejected_media_fault"] == 1
    assert status["pre_probe"]["metrics"]["preprobe_success"] == 1


def test_next_stream_pre_probe_respects_provider_capacity(tmp_path):
    switch_calls = []
    udi = FakeUdi(
        statuses=[{"uuid-1": active_status()}],
        channels=[{"id": 1, "uuid": "uuid-1", "streams": [10, 11, 12]}],
        streams={
            11: {"id": 11, "url": "http://candidate.local/capacity", "m3u_account": 7},
            12: {"id": 12, "url": "http://candidate.local/good", "m3u_account": 8},
        },
    )
    service = make_service(
        tmp_path,
        udi=udi,
        blank_probe=lambda url, config: {"blank_detected": True},
        switch_calls=switch_calls,
    )
    pre_probe_calls = []

    def acquire_slot(_udi, stream):
        if stream["id"] == 11:
            return {
                "acquired": False,
                "reason": "provider_capacity",
                "details": {"provider_limited": True},
            }
        return {
            "acquired": True,
            "reason": "acquired",
            "url": stream["url"],
            "details": {"provider_limited": True, "provider_slot_acquired": True},
        }

    def pre_probe(url, config):
        pre_probe_calls.append(url)
        return {"blank_detected": False}

    released = []
    service._acquire_pre_probe_provider_slot = acquire_slot
    service._release_pre_probe_provider_slot = lambda slot: released.append(slot)
    service._run_blank_probe = pre_probe
    service.update_config({
        "enabled": False,
        "dry_run": False,
        "confirmation_count": 1,
        "next_stream_pre_probe_enabled": True,
    })

    status = service.run_once(force=True)

    assert pre_probe_calls == ["http://candidate.local/good"]
    assert switch_calls == [("uuid-1", 12, None)]
    assert len(released) == 1
    assert status["recent_events"][0]["type"] == "switch_success"
    assert status["recent_events"][0]["details"]["pre_probe"]["provider_slot_acquired"] is True
    assert status["pre_probe"]["metrics"]["preprobe_skipped_provider_limit"] == 1
    assert status["pre_probe"]["metrics"]["preprobe_success"] == 1
    assert status["recent_events"][1]["type"] == "pre_probe_rejected"
    assert status["recent_events"][1]["details"]["origin_stream_ref"].startswith("stream-")
    assert status["recent_events"][1]["details"]["viewer_context"]["real_client_count"] == 1
    assert status["recent_events"][1]["details"]["rejection_reason"] == "provider_capacity"
    assert status["recent_events"][1]["details"]["slot_scope"] == "provider"


def test_next_stream_pre_probe_reports_profile_slot_capacity(tmp_path):
    switch_calls = []
    udi = FakeUdi(
        statuses=[{"uuid-1": active_status()}],
        channels=[{"id": 1, "uuid": "uuid-1", "streams": [10, 11, 12]}],
        streams={
            11: {"id": 11, "url": "http://candidate.local/profile-full", "m3u_account": 7},
            12: {"id": 12, "url": "http://candidate.local/good", "m3u_account": 8},
        },
    )
    service = make_service(
        tmp_path,
        udi=udi,
        blank_probe=lambda url, config: {"blank_detected": True},
        switch_calls=switch_calls,
    )
    pre_probe_calls = []

    def acquire_slot(_udi, stream):
        if stream["id"] == 11:
            return {
                "acquired": False,
                "reason": "checking_capacity",
                "details": {"provider_limited": True, "provider_limit_reason": "checking_capacity"},
            }
        return {
            "acquired": True,
            "reason": "acquired",
            "url": stream["url"],
            "details": {"provider_limited": True, "provider_slot_acquired": True},
        }

    def pre_probe(url, config):
        pre_probe_calls.append(url)
        return {"blank_detected": False}

    released = []
    service._acquire_pre_probe_provider_slot = acquire_slot
    service._release_pre_probe_provider_slot = lambda slot: released.append(slot)
    service._run_blank_probe = pre_probe
    service.update_config({
        "enabled": False,
        "dry_run": False,
        "confirmation_count": 1,
        "next_stream_pre_probe_enabled": True,
    })

    status = service.run_once(force=True)

    assert pre_probe_calls == ["http://candidate.local/good"]
    assert switch_calls == [("uuid-1", 12, None)]
    assert len(released) == 1
    assert status["pre_probe"]["metrics"]["preprobe_skipped_profile_limit"] == 1
    assert status["recent_events"][1]["type"] == "pre_probe_rejected"
    assert status["recent_events"][1]["details"]["rejection_reason"] == "checking_capacity"
    assert status["recent_events"][1]["details"]["slot_scope"] == "profile"


def test_next_stream_pre_probe_timeout_prevents_switch(tmp_path):
    switch_calls = []
    udi = FakeUdi(
        statuses=[{"uuid-1": active_status()}],
        channels=[{"id": 1, "uuid": "uuid-1", "streams": [10, 11]}],
        streams={11: {"id": 11, "url": "http://candidate.local/slow"}},
    )
    service = make_service(
        tmp_path,
        udi=udi,
        blank_probe=lambda url, config: {"blank_detected": True},
        switch_calls=switch_calls,
    )
    service._run_blank_probe = lambda url, config: {"timeout": True}
    service.update_config({
        "enabled": False,
        "dry_run": False,
        "confirmation_count": 1,
        "next_stream_pre_probe_enabled": True,
    })

    status = service.run_once(force=True)

    assert switch_calls == []
    assert status["recent_events"][0]["type"] == "no_alternative"
    assert status["recent_events"][0]["details"]["pre_probe"]["rejection_reason"] == "timeout"
    assert status["recent_events"][1]["type"] == "pre_probe_rejected"
    assert status["pre_probe"]["metrics"]["preprobe_attempted"] == 1
    assert status["pre_probe"]["metrics"]["preprobe_timeout"] == 1
    assert status["pre_probe"]["metrics"]["switch_prevented_by_preprobe"] == 1
    assert status["pre_probe"]["last"]["metric"] == "switch_prevented_by_preprobe"
    assert status["switch_summary"]["pre_probe_prevented_switches"] == 1
    assert status["switch_summary"]["prevented_false_switches"] == 1


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
    assert status["recent_events"][0]["trigger_reason"] == "freeze"
    assert status["recent_events"][0]["details"]["detection"]["reason"] == "freeze"
    assert status["cooldowns"] == []


def test_confirmed_no_decodable_frames_switches_to_next_stream_when_live(tmp_path):
    switch_calls = []
    udi = FakeUdi(
        statuses=[{"uuid-1": active_status()}],
        channels=[{"id": 1, "uuid": "uuid-1", "streams": [10, 11, 12]}],
    )
    service = make_service(
        tmp_path,
        udi=udi,
        blank_probe=lambda url, config: {
            "blank_detected": False,
            "freeze_detected": False,
            "no_decodable_frames_detected": True,
            "no_decodable_frames_duration_secs": 10.2,
            "no_decodable_frames_error": "could not find codec parameters",
        },
        switch_calls=switch_calls,
    )
    service.update_config({
        "enabled": False,
        "dry_run": False,
        "confirmation_count": 1,
    })

    status = service.run_once(force=True)

    assert switch_calls == [("uuid-1", 11, None)]
    assert status["recent_events"][0]["type"] == "switch_success"
    assert status["recent_events"][0]["details"]["reason"] == "no_decodable_frames"
    assert status["cooldowns"] == []


def test_audio_detection_parser_detects_garbled_audio_after_threshold():
    config = normalize_config({
        "garbled_audio_detection_enabled": True,
        "garbled_audio_error_threshold": 2,
    })
    output = (
        "Error while decoding stream #0:1: audio decode error\n"
        "[aac @ 000] channel element 3.7 is not allocated in audio stream #0:1\n"
    )

    parsed = ShadowBlankMonitorService._parse_audio_detection(
        output,
        config,
        observed_duration=12,
    )

    assert parsed["garbled_audio_detected"] is True
    assert parsed["garbled_audio_error_count"] == 2
    assert "audio" in parsed["garbled_audio_error"].lower()


def test_audio_detection_parser_detects_silent_audio_duration():
    config = normalize_config({
        "silent_audio_detection_enabled": True,
        "silent_audio_min_duration_seconds": 10,
        "silent_audio_noise_db": -48,
    })
    output = (
        "[silencedetect @ 000] silence_start: 1.5\n"
        "[silencedetect @ 000] silence_end: 13.0 | silence_duration: 11.5\n"
    )

    parsed = ShadowBlankMonitorService._parse_audio_detection(
        output,
        config,
        observed_duration=14,
    )

    assert parsed["silent_audio_detected"] is True
    assert parsed["silent_audio_duration_secs"] == 11.5
    assert parsed["silent_audio_noise_db"] == -48


def test_audio_detection_parser_treats_missing_audio_as_silent_fault():
    config = normalize_config({
        "silent_audio_detection_enabled": True,
        "silent_audio_min_duration_seconds": 10,
        "silent_audio_noise_db": -48,
    })
    output = "Stream specifier ':a' in filtergraph description matches no streams.\n"

    parsed = ShadowBlankMonitorService._parse_audio_detection(
        output,
        config,
        observed_duration=0.25,
    )

    assert parsed["audio_stream_present"] is False
    assert parsed["silent_audio_detected"] is True
    assert parsed["silent_audio_duration_secs"] == 0.25
    assert parsed["silent_audio_noise_db"] == -48


def test_audio_detection_parser_treats_video_only_stream_listing_as_silent_fault():
    config = normalize_config({
        "silent_audio_detection_enabled": True,
        "silent_audio_min_duration_seconds": 2,
        "silent_audio_noise_db": -48,
    })
    output = (
        "Input #0, mpegts, from 'http://dispatcharr.local/video-only.ts':\n"
        "  Stream #0:0[0x100]: Video: mpeg2video, yuv420p, 640x360, 25 fps\n"
        "Stream mapping:\n"
        "  Stream #0:0 -> #0:0 (mpeg2video (native) -> wrapped_avframe (native))\n"
        "[out#0/null @ 000] video:32KiB audio:0KiB subtitle:0KiB other streams:0KiB\n"
        "frame=   75 fps=0.0 q=-0.0 Lsize=N/A time=00:00:03.00 bitrate=N/A speed= 205x\n"
    )

    parsed = ShadowBlankMonitorService._parse_audio_detection(
        output,
        config,
        observed_duration=3,
    )

    assert parsed["audio_stream_present"] is False
    assert parsed["silent_audio_detected"] is True
    assert parsed["silent_audio_duration_secs"] == 3
    assert parsed["silent_audio_noise_db"] == -48


def test_audio_detection_parser_keeps_audio_stream_listing_ok():
    config = normalize_config({
        "silent_audio_detection_enabled": True,
        "silent_audio_min_duration_seconds": 2,
        "silent_audio_noise_db": -48,
    })
    output = (
        "Input #0, mpegts, from 'http://dispatcharr.local/with-audio.ts':\n"
        "  Stream #0:0[0x100]: Video: mpeg2video, yuv420p, 640x360, 25 fps\n"
        "  Stream #0:1[0x101]: Audio: mp2, 48000 Hz, mono, fltp, 128 kb/s\n"
        "Stream mapping:\n"
        "  Stream #0:0 -> #0:0 (mpeg2video (native) -> wrapped_avframe (native))\n"
        "  Stream #0:1 -> #0:1 (mp2 (native) -> pcm_s16le (native))\n"
        "frame=   75 fps=0.0 q=-0.0 Lsize=N/A time=00:00:03.00 bitrate=N/A speed= 205x\n"
    )

    parsed = ShadowBlankMonitorService._parse_audio_detection(
        output,
        config,
        observed_duration=3,
    )

    assert parsed["audio_stream_present"] is None
    assert parsed["silent_audio_detected"] is False


def test_media_fault_results_are_ignored_unless_enabled(tmp_path):
    switch_calls = []
    udi = FakeUdi(
        statuses=[{"uuid-1": active_status()}],
        channels=[{"id": 1, "uuid": "uuid-1", "streams": [10, 11, 12]}],
    )
    service = make_service(
        tmp_path,
        udi=udi,
        blank_probe=lambda url, config: {
            "garbled_audio_detected": True,
            "silent_audio_detected": True,
            "offline_image_detected": True,
        },
        switch_calls=switch_calls,
    )
    service.update_config({"enabled": False, "dry_run": False, "confirmation_count": 1})

    status = service.run_once(force=True)

    assert switch_calls == []
    assert status["recent_events"][0]["type"] == "probe_ok"


def test_enabled_media_fault_switches_to_next_stream_when_live(tmp_path):
    scenarios = [
        (
            "garbled_audio",
            "garbled_audio_detection_enabled",
            {
                "garbled_audio_detected": True,
                "garbled_audio_error_count": 3,
                "garbled_audio_error": "audio decode error",
            },
        ),
        (
            "silent_audio",
            "silent_audio_detection_enabled",
            {
                "silent_audio_detected": True,
                "silent_audio_duration_secs": 12.0,
            },
        ),
        (
            "offline_image",
            "offline_image_detection_enabled",
            {
                "offline_image_detected": True,
                "offline_image_hash": "ffeeffeeffeeffee",
                "offline_image_distance": 2,
            },
        ),
    ]

    for reason, enabled_key, probe_result in scenarios:
        switch_calls = []
        udi = FakeUdi(
            statuses=[{"uuid-1": active_status()}],
            channels=[{"id": 1, "uuid": "uuid-1", "streams": [10, 11, 12]}],
        )
        service = make_service(
            tmp_path / reason,
            udi=udi,
            blank_probe=lambda url, config, probe_result=probe_result: {
                "blank_detected": False,
                "freeze_detected": False,
                **probe_result,
            },
            switch_calls=switch_calls,
        )
        service.update_config({
            "enabled": False,
            "dry_run": False,
            "confirmation_count": 1,
            enabled_key: True,
        })

        status = service.run_once(force=True)

        assert switch_calls == [("uuid-1", 11, None)]
        assert status["recent_events"][0]["type"] == "switch_success"
        assert status["recent_events"][0]["details"]["reason"] == reason
        assert status["cooldowns"] == []


def test_missing_audio_stream_switches_as_silent_audio_when_enabled(tmp_path):
    switch_calls = []
    udi = FakeUdi(
        statuses=[{"uuid-1": active_status()}],
        channels=[{"id": 1, "uuid": "uuid-1", "streams": [10, 11, 12]}],
    )
    missing_audio_output = "Stream specifier ':a' in filtergraph description matches no streams.\n"

    def probe_missing_audio(_url, config):
        parsed = ShadowBlankMonitorService._parse_audio_detection(
            missing_audio_output,
            normalize_config(config),
            observed_duration=0.25,
        )
        return {
            "blank_detected": False,
            "freeze_detected": False,
            **parsed,
        }

    service = make_service(
        tmp_path,
        udi=udi,
        blank_probe=probe_missing_audio,
        switch_calls=switch_calls,
    )
    service.update_config({
        "enabled": False,
        "dry_run": False,
        "confirmation_count": 1,
        "silent_audio_detection_enabled": True,
    })

    status = service.run_once(force=True)

    assert switch_calls == [("uuid-1", 11, None)]
    assert status["recent_events"][0]["type"] == "switch_success"
    assert status["recent_events"][0]["details"]["reason"] == "silent_audio"
    assert status["recent_events"][0]["details"]["detection"]["reason"] == "silent_audio"
    assert status["watched_channels"][0]["last_probe"]["audio_stream_present"] is False
    assert status["watched_channels"][0]["last_probe"]["silent_audio_detected"] is True


def test_continuous_default_probe_missing_audio_switches_as_silent_audio(monkeypatch, tmp_path):
    switch_calls = []
    udi = FakeUdi(
        statuses=[
            {"uuid-1": active_status(stream_id=10)},
            {"uuid-1": active_status(stream_id=10)},
        ],
        channels=[{"id": 1, "uuid": "uuid-1", "streams": [10, 11]}],
    )

    def switch_stream(channel_id, stream_id=None, url=None):
        switch_calls.append((channel_id, stream_id, url))
        return True

    service = ShadowBlankMonitorService(
        config_file=tmp_path / "shadow.json",
        udi_provider=lambda: udi,
        switch_stream=switch_stream,
        base_url_provider=lambda: "http://dispatcharr.local",
        stream_checker_provider=lambda: FakeStreamChecker(),
        clock=lambda: 1000.0,
    )
    monkeypatch.setattr(
        shadow_module.subprocess,
        "Popen",
        lambda *args, **kwargs: FakeProbeProcess([
            "Stream specifier ':a' in filtergraph description matches no streams.\n",
        ]),
    )
    config = normalize_config({
        "enabled": False,
        "dry_run": False,
        "watch_mode": "continuous",
        "confirmation_count": 1,
        "silent_audio_detection_enabled": True,
        "probe_duration_seconds": 2,
    })
    target = {
        "channel_uuid": "uuid-1",
        "channel_id": 1,
        "channel_ref": "channel-test",
        "stream_id": 10,
        "stream_ref": "stream-test",
        "real_client_count": 1,
    }

    should_continue = service._probe_target_once(udi, target, config)
    status = service.get_status()

    assert should_continue is False
    assert switch_calls == [("uuid-1", 11, None)]
    assert status["recent_events"][0]["type"] == "switch_success"
    assert status["recent_events"][0]["details"]["reason"] == "silent_audio"
    assert status["recent_events"][0]["details"]["detection"]["measurements"]["audio_stream_present"] is False
    assert target["last_probe"]["audio_stream_present"] is False
    assert target["last_probe"]["silent_audio_detected"] is True


def test_continuous_default_probe_reads_fast_exit_missing_audio_stderr(monkeypatch, tmp_path):
    switch_calls = []
    udi = FakeUdi(
        statuses=[
            {"uuid-1": active_status(stream_id=10)},
            {"uuid-1": active_status(stream_id=10)},
        ],
        channels=[{"id": 1, "uuid": "uuid-1", "streams": [10, 11]}],
    )

    def switch_stream(channel_id, stream_id=None, url=None):
        switch_calls.append((channel_id, stream_id, url))
        return True

    service = ShadowBlankMonitorService(
        config_file=tmp_path / "shadow.json",
        udi_provider=lambda: udi,
        switch_stream=switch_stream,
        base_url_provider=lambda: "http://dispatcharr.local",
        stream_checker_provider=lambda: FakeStreamChecker(),
        clock=lambda: 1000.0,
    )
    monkeypatch.setattr(
        shadow_module.subprocess,
        "Popen",
        lambda *args, **kwargs: FakeProbeProcess(
            ["Stream specifier ':a' in filtergraph description matches no streams.\n"],
            immediate_exit=True,
            line_delay=0.05,
        ),
    )
    config = normalize_config({
        "enabled": False,
        "dry_run": False,
        "watch_mode": "continuous",
        "confirmation_count": 1,
        "silent_audio_detection_enabled": True,
        "probe_duration_seconds": 2,
    })
    target = {
        "channel_uuid": "uuid-1",
        "channel_id": 1,
        "channel_ref": "channel-test",
        "stream_id": 10,
        "stream_ref": "stream-test",
        "real_client_count": 1,
    }

    should_continue = service._probe_target_once(udi, target, config)
    status = service.get_status()

    assert should_continue is False
    assert switch_calls == [("uuid-1", 11, None)]
    assert status["recent_events"][0]["type"] == "switch_success"
    assert status["recent_events"][0]["details"]["reason"] == "silent_audio"
    assert target["last_probe"]["audio_stream_present"] is False


def test_continuous_default_probe_video_only_stream_switches_as_silent_audio(monkeypatch, tmp_path):
    switch_calls = []
    udi = FakeUdi(
        statuses=[
            {"uuid-1": active_status(stream_id=10)},
            {"uuid-1": active_status(stream_id=10)},
        ],
        channels=[{"id": 1, "uuid": "uuid-1", "streams": [10, 11]}],
    )

    def switch_stream(channel_id, stream_id=None, url=None):
        switch_calls.append((channel_id, stream_id, url))
        return True

    service = ShadowBlankMonitorService(
        config_file=tmp_path / "shadow.json",
        udi_provider=lambda: udi,
        switch_stream=switch_stream,
        base_url_provider=lambda: "http://dispatcharr.local",
        stream_checker_provider=lambda: FakeStreamChecker(),
        clock=lambda: 1000.0,
    )
    monkeypatch.setattr(
        shadow_module.subprocess,
        "Popen",
        lambda *args, **kwargs: FakeProbeProcess([
            "Input #0, mpegts, from 'http://dispatcharr.local/video-only.ts':\n",
            "  Stream #0:0[0x100]: Video: mpeg2video, yuv420p, 640x360, 25 fps\n",
            "Stream mapping:\n",
            "  Stream #0:0 -> #0:0 (mpeg2video (native) -> wrapped_avframe (native))\n",
            "frame=   75 fps=0.0 q=-0.0 Lsize=N/A time=00:00:03.00 bitrate=N/A speed= 205x\n",
        ]),
    )
    config = normalize_config({
        "enabled": False,
        "dry_run": False,
        "watch_mode": "continuous",
        "confirmation_count": 1,
        "silent_audio_detection_enabled": True,
        "silent_audio_min_duration_seconds": 2,
        "probe_duration_seconds": 2,
    })
    target = {
        "channel_uuid": "uuid-1",
        "channel_id": 1,
        "channel_ref": "channel-test",
        "stream_id": 10,
        "stream_ref": "stream-test",
        "real_client_count": 1,
    }

    should_continue = service._probe_target_once(udi, target, config)
    status = service.get_status()

    assert should_continue is False
    assert switch_calls == [("uuid-1", 11, None)]
    assert status["recent_events"][0]["type"] == "switch_success"
    assert status["recent_events"][0]["details"]["reason"] == "silent_audio"
    assert status["recent_events"][0]["details"]["detection"]["measurements"]["audio_stream_present"] is False
    assert target["last_probe"]["audio_stream_present"] is False
    assert target["last_probe"]["silent_audio_detected"] is True


def test_continuous_default_probe_silencedetect_switches_as_silent_audio(monkeypatch, tmp_path):
    switch_calls = []
    udi = FakeUdi(
        statuses=[
            {"uuid-1": active_status(stream_id=10)},
            {"uuid-1": active_status(stream_id=10)},
        ],
        channels=[{"id": 1, "uuid": "uuid-1", "streams": [10, 11]}],
    )

    def switch_stream(channel_id, stream_id=None, url=None):
        switch_calls.append((channel_id, stream_id, url))
        return True

    service = ShadowBlankMonitorService(
        config_file=tmp_path / "shadow.json",
        udi_provider=lambda: udi,
        switch_stream=switch_stream,
        base_url_provider=lambda: "http://dispatcharr.local",
        stream_checker_provider=lambda: FakeStreamChecker(),
        clock=lambda: 1000.0,
    )
    monkeypatch.setattr(
        shadow_module.subprocess,
        "Popen",
        lambda *args, **kwargs: FakeProbeProcess([
            "[silencedetect @ 000] silence_start: 0.0\n",
            "[silencedetect @ 000] silence_end: 12.25 | silence_duration: 12.25\n",
        ]),
    )
    config = normalize_config({
        "enabled": False,
        "dry_run": False,
        "watch_mode": "continuous",
        "confirmation_count": 1,
        "silent_audio_detection_enabled": True,
        "silent_audio_min_duration_seconds": 10,
        "probe_duration_seconds": 2,
    })
    target = {
        "channel_uuid": "uuid-1",
        "channel_id": 1,
        "channel_ref": "channel-test",
        "stream_id": 10,
        "stream_ref": "stream-test",
        "real_client_count": 1,
    }

    should_continue = service._probe_target_once(udi, target, config)
    status = service.get_status()

    assert should_continue is False
    assert switch_calls == [("uuid-1", 11, None)]
    assert status["recent_events"][0]["type"] == "switch_success"
    assert status["recent_events"][0]["details"]["reason"] == "silent_audio"
    assert status["recent_events"][0]["details"]["detection"]["measurements"]["silent_audio_duration_secs"] == 12.25
    assert target["last_probe"]["silent_audio_detected"] is True
    assert target["last_probe"]["silent_audio_duration_secs"] == 12.25


def test_continuous_default_probe_open_silence_start_switches_as_silent_audio(monkeypatch, tmp_path):
    switch_calls = []
    udi = FakeUdi(
        statuses=[
            {"uuid-1": active_status(stream_id=10)},
            {"uuid-1": active_status(stream_id=10)},
            {"uuid-1": active_status(stream_id=10)},
        ],
        channels=[{"id": 1, "uuid": "uuid-1", "streams": [10, 11]}],
    )

    def switch_stream(channel_id, stream_id=None, url=None):
        switch_calls.append((channel_id, stream_id, url))
        return True

    service = ShadowBlankMonitorService(
        config_file=tmp_path / "shadow.json",
        udi_provider=lambda: udi,
        switch_stream=switch_stream,
        base_url_provider=lambda: "http://dispatcharr.local",
        stream_checker_provider=lambda: FakeStreamChecker(),
        clock=lambda: 1000.0,
    )
    monkeypatch.setattr(
        shadow_module.subprocess,
        "Popen",
        lambda *args, **kwargs: PersistentFakeProbeProcess([
            "frame=10 time=00:00:00.50 bitrate=1000kbits/s speed=1x\n",
            "[silencedetect @ 000] silence_start: 0.0\n",
        ]),
    )
    config = normalize_config({
        "enabled": False,
        "dry_run": False,
        "watch_mode": "continuous",
        "confirmation_count": 1,
        "silent_audio_detection_enabled": True,
        "silent_audio_min_duration_seconds": 2,
        "probe_duration_seconds": 5,
    })
    target = {
        "channel_uuid": "uuid-1",
        "channel_id": 1,
        "channel_ref": "channel-test",
        "stream_id": 10,
        "stream_ref": "stream-test",
        "real_client_count": 1,
    }

    should_continue = service._probe_target_once(udi, target, config)
    status = service.get_status()

    assert should_continue is False
    assert switch_calls == [("uuid-1", 11, None)]
    assert status["recent_events"][0]["type"] == "switch_success"
    assert status["recent_events"][0]["details"]["reason"] == "silent_audio"
    assert status["recent_events"][0]["details"]["detection"]["measurements"]["silent_audio_duration_secs"] >= 2.0
    assert target["last_probe"]["silent_audio_detected"] is True
    assert target["last_probe"]["silent_audio_duration_secs"] >= 2.0


def test_continuous_default_probe_open_freeze_start_switches_as_freeze(monkeypatch, tmp_path):
    switch_calls = []
    udi = FakeUdi(
        statuses=[
            {"uuid-1": active_status(stream_id=10)},
            {"uuid-1": active_status(stream_id=10)},
            {"uuid-1": active_status(stream_id=10)},
        ],
        channels=[{"id": 1, "uuid": "uuid-1", "streams": [10, 11]}],
    )

    def switch_stream(channel_id, stream_id=None, url=None):
        switch_calls.append((channel_id, stream_id, url))
        return True

    service = ShadowBlankMonitorService(
        config_file=tmp_path / "shadow.json",
        udi_provider=lambda: udi,
        switch_stream=switch_stream,
        base_url_provider=lambda: "http://dispatcharr.local",
        stream_checker_provider=lambda: FakeStreamChecker(),
        clock=lambda: 1000.0,
    )
    monkeypatch.setattr(
        shadow_module.subprocess,
        "Popen",
        lambda *args, **kwargs: PersistentFakeProbeProcess([
            "frame=10 time=00:00:00.50 bitrate=1000kbits/s speed=1x\n",
            "[freezedetect @ 000] lavfi.freezedetect.freeze_start: 0\n",
        ]),
    )
    config = normalize_config({
        "enabled": False,
        "dry_run": False,
        "watch_mode": "continuous",
        "confirmation_count": 1,
        "freeze_detection_enabled": True,
        "freeze_min_duration_seconds": 2,
        "probe_duration_seconds": 5,
    })
    target = {
        "channel_uuid": "uuid-1",
        "channel_id": 1,
        "channel_ref": "channel-test",
        "stream_id": 10,
        "stream_ref": "stream-test",
        "real_client_count": 1,
    }

    should_continue = service._probe_target_once(udi, target, config)
    status = service.get_status()

    assert should_continue is False
    assert switch_calls == [("uuid-1", 11, None)]
    assert status["recent_events"][0]["type"] == "switch_success"
    assert status["recent_events"][0]["details"]["reason"] == "freeze"
    assert status["recent_events"][0]["details"]["detection"]["measurements"]["freeze_duration_secs"] >= 2.0
    assert target["last_probe"]["freeze_detected"] is True
    assert target["last_probe"]["freeze_duration_secs"] >= 2.0


def test_continuous_default_probe_full_screen_color_faults_switch(monkeypatch, tmp_path):
    cases = [
        (
            "black",
            ["[blackdetect @ 000] black_start:0\n"],
            "blank",
            "blank_detected",
        ),
        (
            "green",
            [
                "frame=10 time=00:00:00.50 bitrate=1000kbits/s speed=1x\n",
                "[freezedetect @ 000] lavfi.freezedetect.freeze_start: 0\n",
            ],
            "freeze",
            "freeze_detected",
        ),
        (
            "red",
            [
                "frame=10 time=00:00:00.50 bitrate=1000kbits/s speed=1x\n",
                "[freezedetect @ 000] lavfi.freezedetect.freeze_start: 0\n",
            ],
            "freeze",
            "freeze_detected",
        ),
    ]

    for color, ffmpeg_lines, expected_reason, expected_probe_key in cases:
        switch_calls = []
        udi = FakeUdi(
            statuses=[
                {"uuid-1": active_status(stream_id=10)},
                {"uuid-1": active_status(stream_id=10)},
                {"uuid-1": active_status(stream_id=10)},
            ],
            channels=[{"id": 1, "uuid": "uuid-1", "streams": [10, 11]}],
        )

        def switch_stream(channel_id, stream_id=None, url=None):
            switch_calls.append((channel_id, stream_id, url))
            return True

        service = ShadowBlankMonitorService(
            config_file=tmp_path / color / "shadow.json",
            udi_provider=lambda udi=udi: udi,
            switch_stream=switch_stream,
            base_url_provider=lambda: "http://dispatcharr.local",
            stream_checker_provider=lambda: FakeStreamChecker(),
            clock=lambda: 1000.0,
        )
        monkeypatch.setattr(
            shadow_module.subprocess,
            "Popen",
            lambda *args, ffmpeg_lines=ffmpeg_lines, **kwargs: PersistentFakeProbeProcess(ffmpeg_lines),
        )
        config = normalize_config({
            "enabled": False,
            "dry_run": False,
            "watch_mode": "continuous",
            "confirmation_count": 1,
            "blank_min_duration_seconds": 2,
            "freeze_detection_enabled": True,
            "freeze_min_duration_seconds": 2,
            "probe_duration_seconds": 5,
        })
        target = {
            "channel_uuid": "uuid-1",
            "channel_id": 1,
            "channel_ref": f"channel-{color}",
            "stream_id": 10,
            "stream_ref": f"stream-{color}",
            "real_client_count": 1,
        }

        should_continue = service._probe_target_once(udi, target, config)
        status = service.get_status()

        assert should_continue is False, color
        assert switch_calls == [("uuid-1", 11, None)], color
        assert status["recent_events"][0]["type"] == "switch_success", color
        assert status["recent_events"][0]["details"]["reason"] == expected_reason, color
        assert target["last_probe"][expected_probe_key] is True, color


def test_mixed_blank_and_freeze_confirmations_switch_as_video_fault(tmp_path):
    switch_calls = []
    probe_results = iter([
        {"blank_detected": False, "freeze_detected": True, "freeze_duration_secs": 12.0},
        {"blank_detected": True, "blank_duration_secs": 3.0, "freeze_detected": False},
    ])
    udi = FakeUdi(
        statuses=[
            {"uuid-1": active_status(stream_id=10)},
            {"uuid-1": active_status(stream_id=10)},
            {"uuid-1": active_status(stream_id=10)},
        ],
        channels=[{"id": 1, "uuid": "uuid-1", "streams": [10, 11]}],
    )
    service = make_service(
        tmp_path,
        udi=udi,
        blank_probe=lambda url, config: next(probe_results),
        switch_calls=switch_calls,
    )
    config = normalize_config({
        "enabled": False,
        "dry_run": False,
        "watch_mode": "continuous",
        "confirmation_count": 2,
        "freeze_detection_enabled": True,
    })
    target = {
        "channel_uuid": "uuid-1",
        "channel_id": 1,
        "channel_ref": "channel-video-fault",
        "stream_id": 10,
        "stream_ref": "stream-video-fault",
        "real_client_count": 1,
    }

    first_continue = service._probe_target_once(udi, target, config)
    first_status = service.get_status()
    second_continue = service._probe_target_once(udi, target, config)
    second_status = service.get_status()

    assert first_continue is True
    assert first_status["recent_events"][0]["type"] == "freeze_pending"
    assert first_status["recent_events"][0]["details"]["confirmations"] == 1
    assert second_continue is False
    assert switch_calls == [("uuid-1", 11, None)]
    assert second_status["recent_events"][0]["type"] == "switch_success"
    assert second_status["recent_events"][0]["details"]["reason"] == "blank"
    assert target["last_probe"]["blank_detected"] is True


def test_watcher_recovery_guard_clears_pending_detection_and_switch_attempts(tmp_path):
    switch_calls = []
    udi = FakeUdi(
        statuses=[{"uuid-1": active_status(stream_id=10)}],
        channels=[{"id": 1, "uuid": "uuid-1", "streams": [10, 11, 12]}],
    )
    service = make_service(
        tmp_path,
        udi=udi,
        blank_probe=lambda url, config: {"blank_detected": True, "blank_duration_secs": 3.0},
        switch_calls=switch_calls,
    )
    config = normalize_config({
        "enabled": False,
        "dry_run": False,
        "watch_mode": "continuous",
        "confirmation_count": 2,
    })
    target = {
        "channel_uuid": "uuid-1",
        "channel_id": 1,
        "channel_ref": "channel-test",
        "stream_id": 10,
        "stream_ref": "stream-test",
        "real_client_count": 1,
        "watcher_client_ref": "client-old",
    }
    with service._lock:
        service._watched["uuid-1"] = {
            **target,
            "watcher_recovered_after_seconds": 1.5,
        }
        service._blank_counts[service._detection_count_key("uuid-1", "blank")] = 1
        service._switch_attempts["uuid-1"] = {
            "origin_stream_id": 10,
            "target_stream_ids": [11],
        }

    should_continue = service._probe_target_once(udi, target, config)
    status = service.get_status()

    assert should_continue is False
    assert switch_calls == []
    assert status["recent_events"][0]["type"] == "watcher_recovery_guard"
    assert status["cooldowns"][0]["channel_ref"] == "channel-test"
    assert status["cooldowns"][0]["cooldown_seconds"] == 300
    with service._lock:
        assert service._blank_counts[service._detection_count_key("uuid-1", "blank")] == 0
        assert "uuid-1" not in service._switch_attempts


def test_detection_with_fresh_watcher_client_stops_without_pending_or_switch(tmp_path):
    switch_calls = []
    udi = FakeUdi(
        statuses=[
            {
                "uuid-1": active_status(
                    stream_id=10,
                    clients=[
                        {"user_agent": "VLC"},
                        {
                            "user_agent": "StreamFlow-Shadow-Blank-Monitor/1.0",
                            "client_id": "raw-recovered-watcher",
                        },
                    ],
                )
            }
        ],
        channels=[{"id": 1, "uuid": "uuid-1", "streams": [10, 11, 12]}],
    )
    service = make_service(
        tmp_path,
        udi=udi,
        blank_probe=lambda url, config: {"blank_detected": True, "blank_duration_secs": 3.0},
        switch_calls=switch_calls,
    )
    config = normalize_config({
        "enabled": False,
        "dry_run": False,
        "watch_mode": "continuous",
        "confirmation_count": 2,
    })
    target = {
        "channel_uuid": "uuid-1",
        "channel_id": 1,
        "channel_ref": "channel-test",
        "stream_id": 10,
        "stream_ref": "stream-test",
        "real_client_count": 1,
    }
    with service._lock:
        service._blank_counts[service._detection_count_key("uuid-1", "blank")] = 1
        service._switch_attempts["uuid-1"] = {
            "origin_stream_id": 10,
            "target_stream_ids": [11],
        }

    should_continue = service._probe_target_once(udi, target, config)
    status = service.get_status()

    assert should_continue is False
    assert switch_calls == []
    assert status["recent_events"][0]["type"] == "watcher_recovery_guard"
    assert status["recent_events"][0]["details"]["reason"] == "blank"
    assert "blank_pending" not in [event["type"] for event in status["recent_events"]]
    with service._lock:
        assert service._blank_counts[service._detection_count_key("uuid-1", "blank")] == 0
        assert "uuid-1" not in service._switch_attempts


def test_current_probe_watcher_does_not_trigger_recovery_guard(tmp_path):
    switch_calls = []
    udi = FakeUdi(
        statuses=[
            {
                "uuid-1": active_status(
                    stream_id=10,
                    clients=[
                        {"user_agent": "VLC"},
                        {
                            "user_agent": "StreamFlow-Shadow-Blank-Monitor/1.0",
                            "client_id": "raw-current-probe",
                            "connected_at": 1000.0,
                        },
                    ],
                )
            }
        ],
        channels=[{"id": 1, "uuid": "uuid-1", "streams": [10, 11, 12]}],
    )
    service = make_service(
        tmp_path,
        udi=udi,
        blank_probe=lambda url, config: {"blank_detected": True, "blank_duration_secs": 3.0},
        switch_calls=switch_calls,
        clock=lambda: 1000.0,
    )
    config = normalize_config({
        "enabled": False,
        "dry_run": False,
        "watch_mode": "continuous",
        "confirmation_count": 2,
    })
    target = {
        "channel_uuid": "uuid-1",
        "channel_id": 1,
        "channel_ref": "channel-test",
        "stream_id": 10,
        "stream_ref": "stream-test",
        "real_client_count": 1,
    }

    should_continue = service._probe_target_once(udi, target, config)
    status = service.get_status()

    assert should_continue is True
    assert switch_calls == []
    assert status["recent_events"][0]["type"] == "blank_pending"
    assert "watcher_recovery_guard" not in [event["type"] for event in status["recent_events"]]


def test_continuous_media_fault_recovery_guard_requires_pre_probe(tmp_path):
    switch_calls = []
    udi = FakeUdi(
        statuses=[
            {
                "uuid-1": active_status(
                    stream_id=10,
                    clients=[
                        {"user_agent": "VLC"},
                        {
                            "user_agent": "StreamFlow-Shadow-Blank-Monitor/1.0",
                            "client_id": "raw-recovered-watcher",
                        },
                    ],
                )
            }
        ],
        channels=[{"id": 1, "uuid": "uuid-1", "streams": [10, 11]}],
        streams={11: {"id": 11, "url": "http://candidate.local/good"}},
    )
    service = make_service(
        tmp_path,
        udi=udi,
        blank_probe=lambda url, config: {
            "blank_detected": False,
            "freeze_detected": False,
            "silent_audio_detected": True,
            "silent_audio_duration_secs": 12.0,
        },
        switch_calls=switch_calls,
    )
    config = normalize_config({
        "enabled": False,
        "dry_run": False,
        "watch_mode": "continuous",
        "confirmation_count": 2,
        "silent_audio_detection_enabled": True,
        "next_stream_pre_probe_enabled": False,
    })
    target = {
        "channel_uuid": "uuid-1",
        "channel_id": 1,
        "channel_ref": "channel-test",
        "stream_id": 10,
        "stream_ref": "stream-test",
        "real_client_count": 1,
    }
    with service._lock:
        service._blank_counts[service._detection_count_key("uuid-1", "silent_audio")] = 1

    should_continue = service._probe_target_once(udi, target, config)
    status = service.get_status()

    assert should_continue is False
    assert switch_calls == []
    assert status["recent_events"][0]["type"] == "watcher_recovery_guard"
    assert status["recent_events"][0]["details"]["reason"] == "silent_audio"
    with service._lock:
        assert service._blank_counts[service._detection_count_key("uuid-1", "silent_audio")] == 0


def test_continuous_freeze_fault_recovery_guard_needs_second_confirmation(tmp_path):
    switch_calls = []
    udi = FakeUdi(
        statuses=[
            {
                "uuid-1": active_status(
                    stream_id=10,
                    clients=[
                        {"user_agent": "VLC"},
                        {
                            "user_agent": "StreamFlow-Shadow-Blank-Monitor/1.0",
                            "client_id": "raw-recovered-watcher",
                            "connected_at": 990.0,
                        },
                    ],
                )
            }
        ],
        channels=[{"id": 1, "uuid": "uuid-1", "streams": [10, 11]}],
        streams={11: {"id": 11, "url": "http://candidate.local/good"}},
    )
    service = make_service(
        tmp_path,
        udi=udi,
        blank_probe=lambda url, config: {
            "blank_detected": False,
            "freeze_detected": True,
            "freeze_duration_secs": 12.0,
        },
        switch_calls=switch_calls,
    )
    service._run_blank_probe = lambda url, config: {"blank_detected": False, "freeze_detected": False}
    config = normalize_config({
        "enabled": False,
        "dry_run": False,
        "watch_mode": "continuous",
        "confirmation_count": 1,
        "next_stream_pre_probe_enabled": True,
    })
    target = {
        "channel_uuid": "uuid-1",
        "channel_id": 1,
        "channel_ref": "channel-test",
        "stream_id": 10,
        "stream_ref": "stream-test",
        "real_client_count": 1,
    }

    first_result = service._probe_target_once(udi, target, config)
    second_result = service._probe_target_once(udi, target, config)
    status = service.get_status()

    assert first_result is True
    assert second_result is False
    assert switch_calls == [("uuid-1", 11, None)]
    assert status["recent_events"][0]["type"] == "switch_success"
    recovery_guard = status["recent_events"][0]["details"]["recovery_guard"]
    assert recovery_guard["bypassed"] is True
    assert recovery_guard["pre_probe_required"] is True
    assert recovery_guard["confirmations"] == 2
    assert recovery_guard["required"] == 2
    assert status["recent_events"][1]["type"] == "freeze_pending"


def test_continuous_media_fault_recovery_guard_needs_second_confirmation(tmp_path):
    switch_calls = []
    udi = FakeUdi(
        statuses=[
            {
                "uuid-1": active_status(
                    stream_id=10,
                    clients=[
                        {"user_agent": "VLC"},
                        {
                            "user_agent": "StreamFlow-Shadow-Blank-Monitor/1.0",
                            "client_id": "raw-recovered-watcher",
                        },
                    ],
                )
            }
        ],
        channels=[{"id": 1, "uuid": "uuid-1", "streams": [10, 11]}],
        streams={11: {"id": 11, "url": "http://candidate.local/good"}},
    )
    service = make_service(
        tmp_path,
        udi=udi,
        blank_probe=lambda url, config: {
            "blank_detected": False,
            "freeze_detected": False,
            "silent_audio_detected": True,
            "silent_audio_duration_secs": 12.0,
        },
        switch_calls=switch_calls,
    )
    service._run_blank_probe = lambda url, config: {"blank_detected": False, "freeze_detected": False}
    config = normalize_config({
        "enabled": False,
        "dry_run": False,
        "watch_mode": "continuous",
        "confirmation_count": 1,
        "silent_audio_detection_enabled": True,
        "next_stream_pre_probe_enabled": True,
    })
    target = {
        "channel_uuid": "uuid-1",
        "channel_id": 1,
        "channel_ref": "channel-test",
        "stream_id": 10,
        "stream_ref": "stream-test",
        "real_client_count": 1,
    }

    first_result = service._probe_target_once(udi, target, config)
    second_result = service._probe_target_once(udi, target, config)
    status = service.get_status()

    assert first_result is True
    assert second_result is False
    assert switch_calls == [("uuid-1", 11, None)]
    assert status["recent_events"][0]["type"] == "switch_success"
    recovery_guard = status["recent_events"][0]["details"]["recovery_guard"]
    assert recovery_guard["bypassed"] is True
    assert recovery_guard["pre_probe_required"] is True
    assert recovery_guard["confirmations"] == 2
    assert recovery_guard["required"] == 2
    assert status["recent_events"][1]["type"] == "silent_audio_pending"


def test_continuous_probe_stops_when_watcher_reappears_between_confirmations(tmp_path):
    switch_calls = []
    probe_calls = []
    udi = FakeUdi(
        statuses=[
            {"uuid-1": active_status(stream_id=10)},
            {
                "uuid-1": active_status(
                    stream_id=10,
                    clients=[
                        {"user_agent": "VLC"},
                        {
                            "user_agent": "StreamFlow-Shadow-Blank-Monitor/1.0",
                            "client_id": "raw-between-confirmations",
                        },
                    ],
                )
            },
        ],
        channels=[{"id": 1, "uuid": "uuid-1", "streams": [10, 11, 12]}],
    )
    service = ShadowBlankMonitorService(
        config_file=tmp_path / "shadow.json",
        udi_provider=lambda: udi,
        switch_stream=lambda *args, **kwargs: switch_calls.append((args, kwargs)) or True,
        base_url_provider=lambda: "http://dispatcharr.local",
        stream_checker_provider=lambda: FakeStreamChecker(),
        clock=lambda: 1000.0,
    )

    def fake_continuous_probe(url, config, probe_udi, target):
        probe_calls.append((url, target.get("watcher_client_ref")))
        return {"blank_detected": True, "blank_duration_secs": 3.0}

    service._run_blank_probe_until_viewer_left = fake_continuous_probe
    config = normalize_config({
        "enabled": False,
        "dry_run": False,
        "watch_mode": "continuous",
        "confirmation_count": 2,
    })
    target = {
        "channel_uuid": "uuid-1",
        "channel_id": 1,
        "channel_ref": "channel-test",
        "stream_id": 10,
        "stream_ref": "stream-test",
        "real_client_count": 1,
        "watcher_client_count": 0,
    }

    service._probe_target(udi, target, config)
    status = service.get_status()

    assert len(probe_calls) == 1
    assert switch_calls == []
    assert [event["type"] for event in status["recent_events"][:2]] == [
        "watcher_recovery_guard",
        "blank_pending",
    ]
    assert status["recent_events"][0]["details"]["reason"] == "active_watcher_between_confirmations"
    with service._lock:
        assert service._blank_counts[service._detection_count_key("uuid-1", "blank")] == 0


def test_continuous_media_fault_continues_when_watcher_reappears_between_confirmations(tmp_path, monkeypatch):
    switch_calls = []
    probe_calls = []
    monkeypatch.setattr(shadow_module.time, "sleep", lambda _seconds: None)
    udi = FakeUdi(
        statuses=[
            {
                "uuid-1": active_status(
                    stream_id=10,
                    clients=[
                        {"user_agent": "VLC"},
                        {
                            "user_agent": "StreamFlow-Shadow-Blank-Monitor/1.0",
                            "client_id": "raw-between-confirmations",
                        },
                    ],
                )
            }
        ],
        channels=[{"id": 1, "uuid": "uuid-1", "streams": [10, 11]}],
        streams={11: {"id": 11, "url": "http://candidate.local/good"}},
    )

    def switch_stream(channel_id, stream_id=None, url=None):
        switch_calls.append((channel_id, stream_id, url))
        return True

    service = ShadowBlankMonitorService(
        config_file=tmp_path / "shadow.json",
        udi_provider=lambda: udi,
        switch_stream=switch_stream,
        base_url_provider=lambda: "http://dispatcharr.local",
        stream_checker_provider=lambda: FakeStreamChecker(),
        clock=lambda: 1000.0,
    )

    def fake_continuous_probe(url, config, probe_udi, target):
        probe_calls.append((url, target.get("media_recovery_guard_reason")))
        return {
            "blank_detected": False,
            "freeze_detected": False,
            "silent_audio_detected": True,
            "silent_audio_duration_secs": 12.0,
        }

    service._run_blank_probe_until_viewer_left = fake_continuous_probe
    service._run_blank_probe = lambda url, config: {"blank_detected": False, "freeze_detected": False}
    config = normalize_config({
        "enabled": False,
        "dry_run": False,
        "watch_mode": "continuous",
        "confirmation_count": 1,
        "silent_audio_detection_enabled": True,
        "next_stream_pre_probe_enabled": True,
    })
    target = {
        "channel_uuid": "uuid-1",
        "channel_id": 1,
        "channel_ref": "channel-test",
        "stream_id": 10,
        "stream_ref": "stream-test",
        "real_client_count": 1,
        "watcher_client_count": 0,
    }

    service._probe_target(udi, target, config)
    status = service.get_status()

    assert len(probe_calls) == 2
    assert switch_calls == [("uuid-1", 11, None)]
    assert status["recent_events"][0]["type"] == "switch_success"
    assert status["recent_events"][0]["details"]["recovery_guard"]["bypassed"] is True
    assert "watcher_recovery_observed" in [event["type"] for event in status["recent_events"]]


def test_confirmed_blank_rechecks_after_switch_and_skips_attempted_target(tmp_path):
    switch_calls = []
    udi = FakeUdi(
        statuses=[{"uuid-1": active_status(stream_id=10)}],
        channels=[{"id": 1, "uuid": "uuid-1", "streams": [10, 11, 12]}],
    )
    service = make_service(
        tmp_path,
        udi=udi,
        blank_probe=lambda url, config: {"blank_detected": True},
        switch_calls=switch_calls,
    )
    service.update_config({"enabled": False, "dry_run": False, "confirmation_count": 1})

    first_status = service.run_once(force=True)
    second_status = service.run_once(force=True)

    assert switch_calls == [("uuid-1", 11, None), ("uuid-1", 12, None)]
    assert first_status["cooldowns"] == []
    assert second_status["cooldowns"] == []
    assert second_status["recent_events"][0]["type"] == "switch_success"
    assert second_status["recent_events"][0]["details"]["target_stream_ref"].startswith("stream-")


def test_successful_proxy_probe_clears_attempted_switch_targets(tmp_path):
    switch_calls = []
    probe_results = iter([
        {"blank_detected": True},
        {"blank_detected": False},
        {"blank_detected": True},
    ])
    udi = FakeUdi(
        statuses=[{"uuid-1": active_status(stream_id=10)}],
        channels=[{"id": 1, "uuid": "uuid-1", "streams": [10, 11, 12]}],
    )
    service = make_service(
        tmp_path,
        udi=udi,
        blank_probe=lambda url, config: next(probe_results),
        switch_calls=switch_calls,
    )
    service.update_config({"enabled": False, "dry_run": False, "confirmation_count": 1})

    service.run_once(force=True)
    service.run_once(force=True)
    status = service.run_once(force=True)

    assert switch_calls == [("uuid-1", 11, None), ("uuid-1", 11, None)]
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


def test_switch_limit_is_per_channel_not_global(tmp_path):
    udi = FakeUdi(statuses=[{}], channels=[])
    service = make_service(tmp_path, udi=udi, clock=lambda: 1000.0)
    config = normalize_config({"max_switches_per_hour": 1})

    service._switch_history["uuid-1"].append(999.0)

    assert service._switch_allowed("uuid-1", config) is False
    assert service._switch_allowed("uuid-2", config) is True


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


def test_watched_status_includes_sanitized_watcher_identity(tmp_path):
    udi = FakeUdi(
        statuses=[{
            "uuid-1": active_status(
                stream_id=10,
                clients=[
                    {"user_agent": "VLC"},
                    {
                        "user_agent": "StreamFlow-Shadow-Blank-Monitor/1.0",
                        "client_id": "raw-watcher-client-id",
                        "connected_at": 970.0,
                    },
                ],
            ),
        }],
        channels=[{"id": 1, "uuid": "uuid-1", "streams": [10, 11]}],
    )
    service = make_service(tmp_path, udi=udi, clock=lambda: 1000.0)
    service.update_config({"enabled": False, "dry_run": False})

    status = service.run_once(force=True)
    watched = status["watched_channels"][0]

    assert watched["watcher_client_count"] == 1
    assert watched["watcher_client_ref"].startswith("client-")
    assert watched["watcher_connected_at"] == 970.0
    assert watched["watcher_uptime_seconds"] == 30
    assert "raw-watcher-client-id" not in repr(status)


def test_watched_status_includes_current_epg_program(tmp_path, monkeypatch):
    now = datetime.now(timezone.utc)
    scheduling_calls = []

    class FakeSchedulingService:
        def get_programs_by_channel(self, channel_id, tvg_id=None):
            scheduling_calls.append((channel_id, tvg_id))
            return [
                {
                    "title": "Live: MLB",
                    "start_time": (now - timedelta(minutes=5)).isoformat(),
                    "end_time": (now + timedelta(minutes=55)).isoformat(),
                },
                {
                    "title": "SportsCenter",
                    "start_time": (now + timedelta(hours=1)).isoformat(),
                    "end_time": (now + timedelta(hours=2)).isoformat(),
                },
            ]

    monkeypatch.setattr(
        "apps.automation.scheduling_service.get_scheduling_service",
        lambda: FakeSchedulingService(),
    )
    udi = FakeUdi(
        statuses=[{"uuid-1": active_status(stream_id=10)}],
        channels=[{"id": 1, "uuid": "uuid-1", "tvg_id": "mlb.tvg", "streams": [10, 11]}],
    )
    service = make_service(tmp_path, udi=udi)
    service.update_config({"enabled": False, "dry_run": False})

    status = service.run_once(force=True)
    watched = status["watched_channels"][0]

    assert watched["current_program"] == {
        "title": "Live: MLB",
        "state": "current",
        "start_time": (now - timedelta(minutes=5)).isoformat(),
        "end_time": (now + timedelta(minutes=55)).isoformat(),
    }
    assert scheduling_calls == [(1, "mlb.tvg")]
    assert "mlb.tvg" not in repr(status)


def test_continuous_watcher_reconnects_are_visible_without_raw_client_ids(tmp_path):
    now = {"value": 1000.0}
    udi = FakeUdi(
        statuses=[
            {
                "uuid-1": active_status(
                    stream_id=10,
                    clients=[
                        {"user_agent": "VLC"},
                        {
                            "user_agent": "StreamFlow-Shadow-Blank-Monitor/1.0",
                            "client_id": "raw-old-watcher",
                            "connected_at": 990.0,
                        },
                    ],
                )
            },
            {
                "uuid-1": active_status(
                    stream_id=10,
                    clients=[{"user_agent": "VLC"}],
                )
            },
            {
                "uuid-1": active_status(
                    stream_id=10,
                    clients=[
                        {"user_agent": "VLC"},
                        {
                            "user_agent": "StreamFlow-Shadow-Blank-Monitor/1.0",
                            "client_id": "raw-new-watcher",
                            "connected_at": 1010.0,
                        },
                    ],
                )
            },
        ],
        channels=[{"id": 1, "uuid": "uuid-1", "streams": [10, 11]}],
    )
    service = make_service(tmp_path, udi=udi, clock=lambda: now["value"])
    config = normalize_config({"watch_mode": "continuous"})

    service.discover_active_targets(udi, config)
    watched = service.get_status()["watched_channels"][0]
    assert watched["watcher_state"] == "watching"
    assert watched["watcher_client_count"] == 1

    now["value"] = 1005.0
    service.discover_active_targets(udi, config)
    status = service.get_status()
    watched = status["watched_channels"][0]
    assert watched["watcher_state"] == "reconnecting"
    assert watched["watcher_absent_seconds"] == 0
    assert status["recent_events"][0]["type"] == "watcher_reconnecting"

    now["value"] = 1012.0
    service.discover_active_targets(udi, config)
    status = service.get_status()
    watched = status["watched_channels"][0]
    assert watched["watcher_state"] == "watching"
    assert watched["watcher_recovered_after_seconds"] == 7
    assert status["recent_events"][0]["type"] == "watcher_recovered"
    assert status["recent_events"][0]["details"]["downtime_seconds"] == 7
    assert "raw-old-watcher" not in repr(status)
    assert "raw-new-watcher" not in repr(status)


def test_continuous_watcher_ref_change_is_recorded_as_recovery(tmp_path):
    udi = FakeUdi(
        statuses=[
            {
                "uuid-1": active_status(
                    stream_id=10,
                    clients=[
                        {"user_agent": "VLC"},
                        {
                            "user_agent": "StreamFlow-Shadow-Blank-Monitor/1.0",
                            "client_id": "raw-old-watcher",
                            "connected_at": 990.0,
                        },
                    ],
                )
            },
            {
                "uuid-1": active_status(
                    stream_id=10,
                    clients=[
                        {"user_agent": "VLC"},
                        {
                            "user_agent": "StreamFlow-Shadow-Blank-Monitor/1.0",
                            "client_id": "raw-new-watcher",
                            "connected_at": 1001.0,
                        },
                    ],
                )
            },
        ],
        channels=[{"id": 1, "uuid": "uuid-1", "streams": [10, 11]}],
    )
    service = make_service(tmp_path, udi=udi, clock=lambda: 1005.0)
    config = normalize_config({"watch_mode": "continuous"})

    service.discover_active_targets(udi, config)
    service.discover_active_targets(udi, config)
    status = service.get_status()
    watched = status["watched_channels"][0]

    assert watched["watcher_state"] == "watching"
    assert watched["watcher_recovered_after_seconds"] == 0
    assert status["recent_events"][0]["type"] == "watcher_recovered"
    assert status["recent_events"][0]["details"]["downtime_seconds"] == 0
    assert "raw-old-watcher" not in repr(status)
    assert "raw-new-watcher" not in repr(status)


def test_watcher_recovery_cooldown_does_not_block_reconnect_probe(tmp_path):
    now = {"value": 1000.0}
    probe_calls = []
    watcher_old = {
        "user_agent": "StreamFlow-Shadow-Blank-Monitor/1.0",
        "client_id": "raw-old-watcher",
        "connected_at": 990.0,
    }
    watcher_new = {
        "user_agent": "StreamFlow-Shadow-Blank-Monitor/1.0",
        "client_id": "raw-new-watcher",
        "connected_at": 1008.0,
    }
    real_viewer = {"user_agent": "VLC"}
    udi = FakeUdi(
        statuses=[
            {"uuid-1": active_status(stream_id=10, clients=[real_viewer, watcher_old])},
            {"uuid-1": active_status(stream_id=10, clients=[real_viewer])},
            {"uuid-1": active_status(stream_id=10, clients=[real_viewer])},
            {"uuid-1": active_status(stream_id=10, clients=[real_viewer, watcher_new])},
            {"uuid-1": active_status(stream_id=10, clients=[real_viewer])},
        ],
        channels=[{"id": 1, "uuid": "uuid-1", "streams": [10, 11]}],
    )
    service = make_service(
        tmp_path,
        udi=udi,
        blank_probe=lambda url, _config: probe_calls.append(url) or {"blank_detected": False},
        clock=lambda: now["value"],
    )
    service.update_config({
        "enabled": False,
        "watch_mode": "continuous",
        "channel_cooldown_seconds": 300,
    })

    service.run_once(force=True)
    now["value"] = 1005.0
    service.run_once(force=True)
    assert len(probe_calls) == 1

    now["value"] = 1010.0
    recovery_status = service.run_once(force=True)
    assert recovery_status["recent_events"][0]["type"] == "watcher_recovered"
    assert recovery_status["cooldowns"][0]["cooldown_seconds"] == 300

    now["value"] = 1015.0
    cooldown_status = service.run_once(force=True)
    assert len(probe_calls) == 2
    assert cooldown_status["recent_events"][0]["type"] == "probe_ok"
    assert cooldown_status["recent_events"][1]["type"] == "watcher_reconnecting"
    assert cooldown_status["cooldowns"][0]["cooldown_seconds"] == 295


def test_reconnect_probe_blocks_fault_actions_during_cooldown(tmp_path):
    now = {"value": 1000.0}
    probe_calls = []
    switch_calls = []
    probe_results = [
        {"blank_detected": False},
        {"blank_detected": True, "blank_ratio": 1.0, "blank_duration_secs": 60},
    ]
    watcher_old = {
        "user_agent": "StreamFlow-Shadow-Blank-Monitor/1.0",
        "client_id": "raw-old-watcher",
        "connected_at": 990.0,
    }
    watcher_new = {
        "user_agent": "StreamFlow-Shadow-Blank-Monitor/1.0",
        "client_id": "raw-new-watcher",
        "connected_at": 1008.0,
    }
    real_viewer = {"user_agent": "VLC"}
    udi = FakeUdi(
        statuses=[
            {"uuid-1": active_status(stream_id=10, clients=[real_viewer, watcher_old])},
            {"uuid-1": active_status(stream_id=10, clients=[real_viewer])},
            {"uuid-1": active_status(stream_id=10, clients=[real_viewer])},
            {"uuid-1": active_status(stream_id=10, clients=[real_viewer, watcher_new])},
            {"uuid-1": active_status(stream_id=10, clients=[real_viewer])},
            {"uuid-1": active_status(stream_id=10, clients=[real_viewer])},
        ],
        channels=[{"id": 1, "uuid": "uuid-1", "streams": [10, 11]}],
    )

    def blank_probe(url, _config):
        index = min(len(probe_calls), len(probe_results) - 1)
        probe_calls.append(url)
        return probe_results[index]

    service = make_service(
        tmp_path,
        udi=udi,
        blank_probe=blank_probe,
        switch_calls=switch_calls,
        clock=lambda: now["value"],
    )
    service.update_config({
        "enabled": False,
        "watch_mode": "continuous",
        "channel_cooldown_seconds": 300,
        "confirmation_count": 1,
    })

    service.run_once(force=True)
    now["value"] = 1005.0
    service.run_once(force=True)
    assert len(probe_calls) == 1

    now["value"] = 1010.0
    recovery_status = service.run_once(force=True)
    assert recovery_status["recent_events"][0]["type"] == "watcher_recovered"
    assert recovery_status["cooldowns"][0]["cooldown_seconds"] == 300

    now["value"] = 1015.0
    cooldown_status = service.run_once(force=True)
    assert len(probe_calls) == 2
    assert switch_calls == []
    assert cooldown_status["recent_events"][0]["type"] == "cooldown"
    assert cooldown_status["recent_events"][0]["details"]["reason"] == "blank"
    assert cooldown_status["cooldowns"][0]["cooldown_seconds"] == 295


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
        "watcher_api_key": "test-watcher-key",
    })
    config = service.get_config(include_secret=True)
    service._stop_event.clear()

    targets = service.discover_active_targets(udi, config)
    service._probe_targets(udi, targets, config)
    status = service.get_status()

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
