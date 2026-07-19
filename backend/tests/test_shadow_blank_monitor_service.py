import io
import json
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest
from flask import Flask

from apps.core.atomic_json import atomic_write_json
from apps.api.shadow_blank_monitor_handlers import (
    learn_shadow_offline_image_response,
    run_shadow_blank_monitor_once_response,
    start_shadow_blank_monitor_response,
    update_shadow_blank_monitor_config_response,
)
from apps.stream import shadow_blank_monitor_service as shadow_module
from apps.stream.shadow_blank_monitor_service import (
    DEFAULT_WATCHER_USER_AGENT,
    SHADOW_WATCHER_USER_AGENT_MARKER,
    SHADOW_MONITOR_SCAN_ERROR_MESSAGE,
    ShadowConfigConflictError,
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


class FakePersistentWatcherProcess:
    pid = 4242

    def __init__(self):
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


def test_watcher_details_prefers_oldest_visible_watcher(tmp_path):
    service = make_service(
        tmp_path,
        udi=FakeUdi(statuses=[{}], channels=[]),
    )
    config = normalize_config({
        "watcher_user_agent": "StreamFlow-Shadow-Blank-Monitor/1.0",
    })

    details = service._watcher_client_details(
        {
            "clients": [
                {
                    "user_agent": "StreamFlow-Shadow-Blank-Monitor/1.0 ffmpeg",
                    "client_id": "new-probe",
                    "connected_at": 990.0,
                },
                {"user_agent": "VLC", "client_id": "real-viewer", "connected_at": 980.0},
                {
                    "user_agent": "StreamFlow-Shadow-Blank-Monitor/1.0 ffmpeg",
                    "client_id": "persistent-watcher",
                    "connected_at": 900.0,
                },
            ]
        },
        config,
    )

    assert details["watcher_client_count"] == 2
    assert details["watcher_client_ref"] == shadow_module._ref("client", "persistent-watcher")
    assert details["watcher_uptime_seconds"] == 100


def test_default_watcher_user_agent_is_tivimate_like_with_unique_marker():
    config = normalize_config({})

    assert config["watcher_user_agent"] == DEFAULT_WATCHER_USER_AGENT
    assert config["watcher_user_agent"].startswith("TiviMate/")
    assert SHADOW_WATCHER_USER_AGENT_MARKER in config["watcher_user_agent"]


def test_tivimate_like_watcher_user_agent_does_not_hide_real_tivimate_viewers(tmp_path):
    service = make_service(
        tmp_path,
        udi=FakeUdi(statuses=[{}], channels=[]),
    )
    config = normalize_config({})
    status = active_status(
        stream_id=10,
        clients=[
            {
                "user_agent": "TiviMate/5.1.6",
                "client_id": "real-tivimate-viewer",
            },
            {
                "user_agent": config["watcher_user_agent"],
                "client_id": "shadow-watcher",
            },
        ],
    )

    assert service._real_client_count(status, config) == 1
    assert service._watcher_client_count(status, config) == 1


def test_unmarked_probe_client_is_not_counted_as_real_after_viewer_left(tmp_path):
    now = {"value": 1000.0}
    service = make_service(
        tmp_path,
        udi=FakeUdi(statuses=[{}], channels=[]),
        clock=lambda: now["value"],
    )
    config = normalize_config({
        "watch_mode": "continuous",
        "viewer_left_grace_seconds": 5,
    })
    target = {
        "channel_uuid": "uuid-1",
        "active_probe_started_at": 997.0,
        "real_client_refs": ["client_id:real-viewer"],
    }
    status = active_status(
        stream_id=10,
        clients=[
            {
                "client_id": "shadow-probe",
                "user": "bachel",
                "ip": "10.10.30.20",
            }
        ],
    )

    assert service._real_client_count(status, config, target) == 0


def test_discovery_keeps_grace_when_only_unmarked_probe_client_remains(tmp_path):
    now = {"value": 1000.0}
    channel = {"id": 1, "uuid": "uuid-1", "name": "Das Erste HD", "streams": [10, 11]}
    udi = FakeUdi(
        statuses=[
            {
                "uuid-1": active_status(
                    stream_id=10,
                    clients=[
                        {
                            "client_id": "shadow-probe",
                            "user": "bachel",
                            "ip": "10.10.30.20",
                        }
                    ],
                )
            }
        ],
        channels=[channel],
    )
    service = make_service(tmp_path, udi=udi, clock=lambda: now["value"])
    service._watched["uuid-1"] = {
        "channel_uuid": "uuid-1",
        "channel_id": 1,
        "channel_ref": shadow_module._ref("channel", 1),
        "channel_name": "Das Erste HD",
        "stream_id": 10,
        "stream_ref": shadow_module._ref("stream", 10),
        "real_client_count": 1,
        "real_client_refs": ["client_id:real-viewer"],
        "active_probe_started_at": 997.0,
        "probe_state": "probing",
    }
    service._active_probes.add("uuid-1")
    config = normalize_config({
        "watch_mode": "continuous",
        "viewer_left_grace_seconds": 5,
    })

    targets = service.discover_active_targets(udi, config)

    assert len(targets) == 1
    assert targets[0]["viewer_left_grace_active"] is True
    assert targets[0]["real_client_count"] == 0
    assert targets[0]["viewer_left_grace_remaining_seconds"] == 5


def test_shadow_config_recovers_from_last_known_good_copy(tmp_path):
    config_file = tmp_path / "shadow.json"
    atomic_write_json(config_file, {"dry_run": False})
    atomic_write_json(config_file, {"dry_run": True})
    config_file.write_text("{broken", encoding="utf-8")

    service = make_service(
        tmp_path,
        udi=FakeUdi(statuses=[{}], channels=[]),
    )

    assert service.get_config()["dry_run"] is False


def test_shadow_external_watcher_key_is_effective_but_not_persisted(tmp_path, monkeypatch):
    monkeypatch.setenv("SHADOW_WATCHER_API_KEY", "external-watcher-key")
    service = make_service(
        tmp_path,
        udi=FakeUdi(statuses=[{}], channels=[]),
        watcher_api_key=None,
    )

    assert service.get_config()["has_watcher_api_key"] is True
    assert service.get_config()["watcher_api_key_managed_externally"] is True
    assert service.get_config(include_secret=True)["watcher_api_key"] == "external-watcher-key"

    service.update_config({"dry_run": True, "watcher_api_key": "ui-key"})
    persisted = json.loads(service.config_file.read_text(encoding="utf-8"))

    assert persisted["watcher_api_key"] == ""
    assert service.get_config(include_secret=True)["watcher_api_key"] == "external-watcher-key"


def test_missing_external_watcher_key_file_overrides_stored_key(tmp_path, monkeypatch):
    atomic_write_json(tmp_path / "shadow.json", {"watcher_api_key": "stored-key"})
    monkeypatch.setenv("SHADOW_WATCHER_API_KEY_FILE", str(tmp_path / "missing"))

    service = make_service(
        tmp_path,
        udi=FakeUdi(statuses=[{}], channels=[]),
        watcher_api_key=None,
    )

    assert service.get_config()["watcher_api_key_managed_externally"] is True
    assert service.get_config()["has_watcher_api_key"] is False
    assert service.get_config(include_secret=True)["watcher_api_key"] == ""


def test_stale_dispatcharr_client_is_not_counted_as_real_after_short_viewer_grace(tmp_path):
    service = make_service(
        tmp_path,
        udi=FakeUdi(statuses=[{}], channels=[]),
    )
    config = normalize_config({
        "watch_mode": "continuous",
        "viewer_left_grace_seconds": 5,
    })
    fresh_status = active_status(
        stream_id=10,
        clients=[{
            "client_id": "real-viewer",
            "user_agent": "TiviMate/5.1.6",
            "last_active_ago": 4.9,
            "output_format": "fmp4",
        }],
    )
    stale_status = active_status(
        stream_id=10,
        clients=[{
            "client_id": "real-viewer",
            "user_agent": "TiviMate/5.1.6",
            "last_active_ago": 6.1,
            "output_format": "fmp4",
        }],
    )

    assert service._real_client_count(fresh_status, config) == 1
    assert service._real_viewer_output_format(fresh_status, config) == "fmp4"
    assert service._real_client_refs(fresh_status, config) == ["client_id:real-viewer"]
    assert service._real_client_count(stale_status, config) == 0
    assert service._real_viewer_output_format(stale_status, config) is None
    assert service._real_client_refs(stale_status, config) == []


def test_stale_dispatcharr_client_expires_grace_from_last_activity(tmp_path):
    now = {"value": 1000.0}
    channel = {"id": 1, "uuid": "uuid-1", "name": "Das Erste HD", "streams": [10, 11]}
    udi = FakeUdi(
        statuses=[{
            "uuid-1": active_status(
                stream_id=10,
                clients=[{
                    "client_id": "real-viewer",
                    "user_agent": "TiviMate/5.1.6",
                    "last_active_ago": 6.2,
                }],
            )
        }],
        channels=[channel],
    )
    service = make_service(tmp_path, udi=udi, clock=lambda: now["value"])
    with service._lock:
        service._watched["uuid-1"] = {
            "channel_uuid": "uuid-1",
            "channel_id": 1,
            "channel_ref": shadow_module._ref("channel", 1),
            "channel_name": "Das Erste HD",
            "stream_id": 10,
            "stream_ref": shadow_module._ref("stream", 10),
            "real_client_count": 1,
            "real_client_refs": ["client_id:real-viewer"],
        }
    config = normalize_config({
        "watch_mode": "continuous",
        "viewer_left_grace_seconds": 5,
    })

    targets = service.discover_active_targets(udi, config)
    status = service.get_status()

    assert targets == []
    viewer_left_events = [event for event in status["recent_events"] if event["type"] == "viewer_left"]
    assert len(viewer_left_events) == 1
    assert viewer_left_events[0]["details"]["reason"] == "viewer_left_grace_expired"
    assert viewer_left_events[0]["details"]["viewer_absent_seconds"] == 6


def test_continuous_loop_probe_is_rate_limited_between_slices(tmp_path):
    now = {"value": 1000.0}
    loop_calls = []

    def loop_probe(url, config):
        loop_calls.append((url, now["value"]))
        return {"loop_probe_ran": True, "loop_detected": False}

    udi = FakeUdi(
        statuses=[
            {"uuid-1": active_status(stream_id=10, clients=[{"user_agent": "VLC"}])},
        ],
        channels=[{"id": 1, "uuid": "uuid-1", "streams": [10, 11]}],
    )
    service = make_service(
        tmp_path,
        udi=udi,
        loop_probe=loop_probe,
        clock=lambda: now["value"],
    )
    config = normalize_config({
        "watch_mode": "continuous",
        "loop_detection_enabled": True,
        "loop_probe_duration_seconds": 120,
    })
    target = {"channel_uuid": "uuid-1", "channel_ref": "channel-one"}

    first = service._run_loop_probe_if_enabled("http://example.test/stream", target, config, udi=udi)
    now["value"] += 30
    second = service._run_loop_probe_if_enabled("http://example.test/stream", target, config, udi=udi)
    now["value"] += 91
    third = service._run_loop_probe_if_enabled("http://example.test/stream", target, config, udi=udi)

    assert first["loop_probe_ran"] is True
    assert second["loop_probe_ran"] is False
    assert second["loop_probe_skipped"] is True
    assert second["loop_probe_skip_reason"] == "loop_probe_interval"
    assert third["loop_probe_ran"] is True
    assert loop_calls == [
        ("http://example.test/stream", 1000.0),
        ("http://example.test/stream", 1121.0),
    ]


def test_watch_mode_controls_scan_delay():
    defaults = normalize_config({})
    assert defaults["enabled"] is False
    assert defaults["dry_run"] is False
    assert defaults["watch_mode"] == "continuous"
    assert defaults["poll_interval_seconds"] == 5
    assert defaults["watch_gap_seconds"] == 1
    assert defaults["probe_duration_seconds"] == 60
    assert defaults["persistent_watcher_enabled"] is False
    assert defaults["continuous_probe_interval_seconds"] == 120
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
    assert defaults["loop_probe_duration_seconds"] == 360
    assert defaults["next_stream_pre_probe_enabled"] is False
    assert defaults["next_stream_pre_probe_duration_seconds"] == 8
    assert defaults["confirmation_count"] == 2
    assert defaults["channel_cooldown_seconds"] == 300
    assert defaults["viewer_left_grace_seconds"] == 5
    assert defaults["max_switches_per_hour"] == 3
    assert defaults["max_concurrent_watchers"] == 2

    continuous = normalize_config({
        "watch_mode": "continuous",
        "watch_gap_seconds": 2,
        "poll_interval_seconds": 90,
    })
    assert ShadowBlankMonitorService._next_scan_delay(continuous) == 2

    legacy_periodic = normalize_config({
        "watch_mode": "periodic",
        "persistent_watcher_enabled": True,
        "watch_gap_seconds": 2,
        "poll_interval_seconds": 90,
    })
    assert legacy_periodic["watch_mode"] == "continuous"
    assert legacy_periodic["persistent_watcher_enabled"] is True
    assert ShadowBlankMonitorService._next_scan_delay(legacy_periodic) == 2

    invalid = normalize_config({"watch_mode": "always-on", "watch_gap_seconds": 0})
    assert invalid["watch_mode"] == "continuous"
    assert invalid["watch_gap_seconds"] == 1

    loop_bounds = normalize_config({"loop_detection_enabled": True, "loop_probe_duration_seconds": 999})
    assert loop_bounds["loop_detection_enabled"] is True
    assert loop_bounds["loop_probe_duration_seconds"] == 720

    viewer_grace_bounds = normalize_config({"viewer_left_grace_seconds": 30})
    assert viewer_grace_bounds["viewer_left_grace_seconds"] == 10


def test_pre_probe_duration_covers_enabled_detection_thresholds():
    defaults = normalize_config({"next_stream_pre_probe_duration_seconds": 5})
    assert ShadowBlankMonitorService._effective_pre_probe_duration_seconds(defaults) == 12

    silent = normalize_config({
        "next_stream_pre_probe_duration_seconds": 5,
        "no_decodable_frames_detection_enabled": False,
        "silent_audio_detection_enabled": True,
        "silent_audio_min_duration_seconds": 15,
    })
    assert ShadowBlankMonitorService._effective_pre_probe_duration_seconds(silent) == 17

    configured_longer = normalize_config({"next_stream_pre_probe_duration_seconds": 30})
    assert ShadowBlankMonitorService._effective_pre_probe_duration_seconds(configured_longer) == 30


def test_short_completed_probe_is_not_accepted_as_healthy(monkeypatch, tmp_path):
    completed = type("CompletedProbe", (), {"stderr": "", "returncode": 1})()
    timestamps = iter([100.0, 100.1])
    monkeypatch.setattr(shadow_module.time, "time", lambda: next(timestamps))
    monkeypatch.setattr(shadow_module.subprocess, "run", lambda *args, **kwargs: completed)
    service = make_service(
        tmp_path,
        udi=FakeUdi(statuses=[{}], channels=[]),
    )
    config = normalize_config({"probe_duration_seconds": 12})

    result = service._run_blank_probe("http://example.test/ended", config)

    assert result["probe_incomplete"] is True
    assert result["probe_elapsed_seconds"] == 0.1
    assert result["probe_expected_duration_seconds"] == 12
    assert service._pre_probe_rejection_reason(result) == "incomplete_probe"


def test_full_media_window_is_complete_when_ffmpeg_finishes_faster_than_wall_clock(
    monkeypatch,
    tmp_path,
):
    completed = type("CompletedProbe", (), {
        "stderr": (
            "frame=  144 fps=14.0 q=-0.0 size=N/A time=00:00:12.00 "
            "bitrate=N/A speed=1.2x\n"
        ),
        "returncode": 0,
    })()
    timestamps = iter([100.0, 110.2])
    monkeypatch.setattr(shadow_module.time, "time", lambda: next(timestamps))
    monkeypatch.setattr(shadow_module.subprocess, "run", lambda *args, **kwargs: completed)
    service = make_service(
        tmp_path,
        udi=FakeUdi(statuses=[{}], channels=[]),
    )
    config = normalize_config({
        "probe_duration_seconds": 12,
        "offline_image_detection_enabled": False,
    })

    result = service._run_blank_probe("http://example.test/fast-complete", config)

    assert result["probe_elapsed_seconds"] == 10.2
    assert result["probe_media_duration_seconds"] == 12.0
    assert result["decoded_video_frames"] == 144
    assert result["probe_media_window_complete"] is True
    assert result["probe_incomplete"] is False
    assert service._pre_probe_rejection_reason(result) is None


def test_partial_media_stays_incomplete_when_ffmpeg_exits_early(monkeypatch, tmp_path):
    completed = type("CompletedProbe", (), {
        "stderr": (
            "frame=    1 fps=1.0 q=-0.0 size=N/A time=00:00:01.00 "
            "bitrate=N/A speed=1x\n"
        ),
        "returncode": 0,
    })()
    timestamps = iter([100.0, 101.0])
    monkeypatch.setattr(shadow_module.time, "time", lambda: next(timestamps))
    monkeypatch.setattr(shadow_module.subprocess, "run", lambda *args, **kwargs: completed)
    service = make_service(
        tmp_path,
        udi=FakeUdi(statuses=[{}], channels=[]),
    )
    config = normalize_config({
        "probe_duration_seconds": 12,
        "offline_image_detection_enabled": False,
    })

    result = service._run_blank_probe("http://example.test/partial", config)

    assert result["probe_media_window_complete"] is False
    assert result["probe_incomplete"] is True
    assert service._pre_probe_rejection_reason(result) == "incomplete_probe"


def test_full_media_window_survives_ffmpeg_teardown_timeout(monkeypatch, tmp_path):
    stderr = (
        b"frame=  144 fps=12.0 q=-0.0 size=N/A time=00:00:12.00 "
        b"bitrate=N/A speed=1x\n"
    )

    def timeout_probe(*args, **kwargs):
        raise shadow_module.subprocess.TimeoutExpired(args[0], timeout=27, stderr=stderr)

    monkeypatch.setattr(shadow_module.subprocess, "run", timeout_probe)
    service = make_service(
        tmp_path,
        udi=FakeUdi(statuses=[{}], channels=[]),
    )
    config = normalize_config({
        "probe_duration_seconds": 12,
        "offline_image_detection_enabled": False,
    })

    result = service._run_blank_probe("http://example.test/healthy", config)

    assert result["probe_teardown_timeout"] is True
    assert result["probe_media_window_complete"] is True
    assert result["probe_media_duration_seconds"] == 12.0
    assert result["decoded_video_frames"] == 144
    assert result.get("timeout") is not True
    assert service._pre_probe_rejection_reason(result) is None


def test_partial_media_does_not_survive_ffmpeg_teardown_timeout(monkeypatch, tmp_path):
    stderr = (
        b"frame=    1 fps=0.1 q=-0.0 size=N/A time=00:00:01.00 "
        b"bitrate=N/A speed=0.1x\n"
    )

    def timeout_probe(*args, **kwargs):
        raise shadow_module.subprocess.TimeoutExpired(args[0], timeout=27, stderr=stderr)

    monkeypatch.setattr(shadow_module.subprocess, "run", timeout_probe)
    service = make_service(
        tmp_path,
        udi=FakeUdi(statuses=[{}], channels=[]),
    )
    config = normalize_config({
        "probe_duration_seconds": 12,
        "offline_image_detection_enabled": False,
    })

    result = service._run_blank_probe("http://example.test/stalled", config)

    assert result["probe_teardown_timeout"] is True
    assert result["probe_media_window_complete"] is False
    assert result["probe_media_duration_seconds"] == 1.0
    assert result["timeout"] is True
    assert service._pre_probe_rejection_reason(result) == "timeout"


def test_post_switch_probe_uses_bounded_process_timeout(
    monkeypatch,
    tmp_path,
):
    calls = []

    def timeout_probe(*args, **kwargs):
        calls.append(kwargs)
        raise shadow_module.subprocess.TimeoutExpired(args[0], timeout=kwargs["timeout"], stderr=b"")

    monkeypatch.setattr(shadow_module.subprocess, "run", timeout_probe)
    service = make_service(
        tmp_path,
        udi=FakeUdi(statuses=[{}], channels=[]),
    )
    config = normalize_config({
        "probe_duration_seconds": 12,
        "offline_image_detection_enabled": False,
    })
    config["_shadow_probe_subprocess_timeout_seconds"] = 15

    result = service._run_blank_probe("http://example.test/reconnecting", config)

    assert len(calls) == 1
    assert calls[0]["timeout"] == 15
    assert result["timeout"] is True


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
    assert status["watch_mode"] == "continuous"
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
        loop_detected = "/proxy/ts/stream/" in url or url.endswith("/old.ts")
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


def test_shadow_loop_confirmation_survives_one_probe_ok_miss(tmp_path):
    switch_calls = []
    loop_calls = []
    active_loop_results = iter([True, False, True])
    udi = FakeUdi(
        statuses=[
            {"/proxy/ts/stream/uuid-1": active_status(10)},
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
        loop_detected = next(active_loop_results) if (
            "/proxy/ts/stream/" in url or url.endswith("/old.ts")
        ) else False
        return {
            "loop_probe_ran": True,
            "loop_detected": loop_detected,
            "loop_duration_secs": 12.5 if loop_detected else None,
            "loop_frames_processed": 26,
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
        "loop_probe_duration_seconds": 60,
        "confirmation_count": 2,
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

    assert service._probe_target_once(udi, target, config) is True
    assert service.get_status()["recent_events"][0]["type"] == "loop_pending"

    assert service._probe_target_once(udi, target, config) is True
    probe_ok = service.get_status()["recent_events"][0]
    assert probe_ok["type"] == "probe_ok"
    assert probe_ok["details"]["preserved_pending_detection"] == {
        "reason": "loop",
        "confirmations": 1,
        "misses": 1,
        "miss_tolerance": 1,
    }

    assert service._probe_target_once(udi, target, config) is False

    assert loop_calls == [
        "http://dispatcharr.local/proxy/ts/stream/uuid-1",
        "http://dispatcharr.local/proxy/ts/stream/uuid-1",
        "http://dispatcharr.local/proxy/ts/stream/uuid-1",
        "http://provider.example/new.ts",
    ]
    assert switch_calls == [("uuid-1", 11, None)]
    event = service.get_status()["recent_events"][0]
    assert event["type"] == "switch_success"
    assert event["trigger_reason"] == "loop"
    assert event["details"]["reason"] == "loop"
    assert event["details"]["pre_probe"]["result"] == "ok"


def test_shadow_loop_probe_aborts_when_real_viewer_leaves(tmp_path):
    loop_calls = []
    switch_calls = []
    watcher_client = {"user_agent": "StreamFlow-Shadow-Blank-Monitor/1.0"}
    udi = FakeUdi(
        statuses=[
            {"uuid-1": active_status(stream_id=10, clients=[{"user_agent": "VLC"}, watcher_client])},
            {"uuid-1": active_status(stream_id=10, clients=[watcher_client])},
        ],
        channels=[{"id": 1, "uuid": "uuid-1", "streams": [10, 11]}],
        streams={
            10: {"id": 10, "url": "http://provider.example/old.ts"},
            11: {"id": 11, "url": "http://provider.example/new.ts"},
        },
    )

    def loop_probe(url, config):
        loop_calls.append(url)
        abort_check = config.get("_shadow_loop_abort_check")
        assert callable(abort_check)
        assert abort_check() is True
        return {
            "loop_probe_ran": True,
            "loop_detected": False,
            "loop_duration_secs": None,
            "loop_frames_processed": 12,
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
        "watch_mode": "continuous",
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

    assert service._probe_target_once(udi, target, config) is False

    assert loop_calls == ["http://dispatcharr.local/proxy/ts/stream/uuid-1"]
    assert switch_calls == []
    status = service.get_status()
    assert status["recent_events"][0]["type"] == "viewer_left"
    assert status["recent_events"][0]["real_client_count"] == 0
    assert status["watched_channels"] == []


def test_continuous_shadow_loop_probe_is_time_sliced(tmp_path, monkeypatch):
    loop_calls = []
    switch_calls = []
    now = {"value": 100.0}
    udi = FakeUdi(
        statuses=[
            {"uuid-1": active_status(stream_id=10, clients=[{"user_agent": "VLC"}])},
            {"uuid-1": active_status(stream_id=10, clients=[{"user_agent": "VLC"}])},
        ],
        channels=[{"id": 1, "uuid": "uuid-1", "streams": [10, 11]}],
        streams={
            10: {"id": 10, "url": "http://provider.example/old.ts"},
            11: {"id": 11, "url": "http://provider.example/new.ts"},
        },
    )

    monkeypatch.setattr(shadow_module.time, "monotonic", lambda: now["value"])

    def loop_probe(url, config):
        loop_calls.append(url)
        abort_check = config.get("_shadow_loop_abort_check")
        assert callable(abort_check)
        assert abort_check() is False
        now["value"] += 30.0
        assert abort_check() is True
        return {
            "loop_probe_ran": True,
            "loop_detected": False,
            "loop_duration_secs": None,
            "loop_frames_processed": 18,
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
        "watch_mode": "continuous",
        "loop_detection_enabled": True,
        "loop_probe_duration_seconds": 360,
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

    result = service._run_loop_probe_if_enabled(
        "http://dispatcharr.local/proxy/ts/stream/uuid-1",
        target,
        config,
        udi=udi,
    )

    assert loop_calls == ["http://dispatcharr.local/proxy/ts/stream/uuid-1"]
    assert result["loop_probe_ran"] is True
    assert result["loop_detected"] is False
    assert result["loop_probe_sliced"] is True
    assert result["loop_probe_slice_seconds"] == 30.0
    assert result["loop_probe_slice_seconds"] < 360
    assert result.get("viewer_left") is not True
    assert switch_calls == []


def test_shadow_bounded_blank_probe_aborts_before_loop_when_real_viewer_leaves(tmp_path):
    continuous_modes = []
    loop_calls = []
    switch_calls = []
    watcher_client = {"user_agent": "StreamFlow-Shadow-Blank-Monitor/1.0"}
    udi = FakeUdi(
        statuses=[
            {"uuid-1": active_status(stream_id=10, clients=[{"user_agent": "VLC"}, watcher_client])},
            {"uuid-1": active_status(stream_id=10, clients=[watcher_client])},
        ],
        channels=[{"id": 1, "uuid": "uuid-1", "streams": [10, 11]}],
        streams={11: {"id": 11, "url": "http://provider.example/new.ts"}},
    )

    def switch_stream(channel_id, stream_id=None, url=None):
        switch_calls.append((channel_id, stream_id, url))
        return True

    service = ShadowBlankMonitorService(
        config_file=tmp_path / "shadow.json",
        udi_provider=lambda: udi,
        switch_stream=switch_stream,
        base_url_provider=lambda: "http://dispatcharr.local",
        loop_probe=lambda url, config: loop_calls.append(url) or {
            "loop_probe_ran": True,
            "loop_detected": True,
        },
        stream_checker_provider=lambda: FakeStreamChecker(),
        clock=lambda: 1000.0,
    )

    def bounded_probe(url, config, probe_udi, target, *, continuous=True):
        continuous_modes.append(continuous)
        return {"blank_detected": False, "freeze_detected": False, "viewer_left": True}

    service._run_blank_probe_until_viewer_left = bounded_probe
    config = normalize_config({
        **service.get_config(include_secret=True),
        "watch_mode": "continuous",
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

    assert service._probe_target_once(udi, target, config) is False

    assert continuous_modes == [False]
    assert loop_calls == []
    assert switch_calls == []
    status = service.get_status()
    assert status["recent_events"][0]["type"] == "viewer_left"
    assert status["recent_events"][0]["real_client_count"] == 0
    assert status["watched_channels"] == []


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


def test_shadow_non_loop_pre_probe_skips_candidate_loop_check(tmp_path):
    loop_calls = []
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
    service.loop_probe = lambda url, config: loop_calls.append(url) or {
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
        reason="freeze",
    )

    assert alternative == 11
    assert details["result"] == "ok"
    assert details["rejection_reason"] is None
    assert details["loop_probe_ran"] is False
    assert details["loop_detected"] is False
    assert loop_calls == []


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
        loop_detected = "/proxy/ts/stream/" in url or url.endswith("/old.ts")
        return {
            "loop_probe_ran": True,
            "loop_detected": loop_detected,
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


def test_shadow_safety_gates_survive_service_restart_and_expire(tmp_path):
    now = {"value": 1000.0}
    udi = FakeUdi(
        statuses=[{"uuid-1": active_status(stream_id=10)}],
        channels=[{"id": 1, "uuid": "uuid-1", "streams": [10, 11]}],
    )
    config = normalize_config({
        "channel_cooldown_seconds": 300,
        "max_switches_per_hour": 1,
    })

    first = make_service(tmp_path, udi=udi, clock=lambda: now["value"])
    first._set_cooldown("uuid-1", config)
    first._record_successful_switch("uuid-1")

    restarted = make_service(tmp_path, udi=udi, clock=lambda: now["value"])

    assert restarted._cooldown_remaining("uuid-1") == 300
    assert restarted._switch_allowed("uuid-1", config) is False
    assert restarted.safety_state_file.exists()

    now["value"] = 1401.0
    after_cooldown = make_service(tmp_path, udi=udi, clock=lambda: now["value"])
    assert after_cooldown._cooldown_remaining("uuid-1") == 0
    assert after_cooldown._switch_allowed("uuid-1", config) is False

    now["value"] = 4601.0
    after_window = make_service(tmp_path, udi=udi, clock=lambda: now["value"])
    assert after_window._cooldown_remaining("uuid-1") == 0
    assert after_window._switch_allowed("uuid-1", config) is True


def test_learn_offline_image_from_current_frame_adds_hash(tmp_path, monkeypatch):
    service = make_service(
        tmp_path,
        udi=FakeUdi(statuses=[{}], channels=[]),
    )
    cancel_event = threading.Event()
    watcher_process = FakePersistentWatcherProcess()
    with service._lock:
        service._watched["uuid-1"] = {
            "channel_uuid": "uuid-1",
            "channel_id": 1,
            "channel_ref": "channel-safe",
            "stream_id": 10,
            "stream_ref": "stream-safe",
            "real_client_count": 1,
        }
        service._active_probes.add("uuid-1")
        service._probe_cancel_events["uuid-1"] = cancel_event
        service._persistent_watchers["uuid-1"] = {"process": watcher_process}

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
    assert cancel_event.is_set() is True
    assert watcher_process.terminated is True
    assert service._persistent_watchers == {}


def test_learn_offline_image_deduplicates_near_existing_hash(tmp_path, monkeypatch):
    service = make_service(
        tmp_path,
        udi=FakeUdi(statuses=[{}], channels=[]),
    )
    service.update_config({
        "offline_image_reference_hashes": ["0123456789abcdee"],
        "offline_image_hash_threshold": 4,
    })
    cancel_event = threading.Event()
    with service._lock:
        service._watched["uuid-1"] = {
            "channel_uuid": "uuid-1",
            "channel_ref": "channel-safe",
            "stream_ref": "stream-safe",
            "real_client_count": 1,
        }
        service._active_probes.add("uuid-1")
        service._probe_cancel_events["uuid-1"] = cancel_event
    revision = service.get_config()["config_revision"]

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
    assert result["config"]["config_revision"] == revision
    assert cancel_event.is_set() is False


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


def test_shadow_monitor_run_once_handler_passes_transient_include_scope():
    class FakeService:
        def __init__(self):
            self.calls = []

        def run_once(self, **kwargs):
            self.calls.append(kwargs)
            return {
                "configuration_required": False,
                "watched_count": 1,
            }

    service = FakeService()
    app = Flask(__name__)
    with app.app_context():
        response, status_code = run_shadow_blank_monitor_once_response(
            payload={
                "include_channel_ids": [2, "3"],
                "include_channel_uuids": ["uuid-4"],
            },
            get_service=lambda: service,
        )

    assert status_code == 200
    assert response.get_json()["watched_count"] == 1
    assert service.calls == [{
        "force": True,
        "include_channel_ids": [2, "3"],
        "include_channel_uuids": ["uuid-4"],
    }]


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
        "freeze_detection_enabled": False,
        "no_decodable_frames_detection_enabled": True,
        "silent_audio_detection_enabled": True,
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
    assert result["blank_duration_secs"] >= 9.6
    assert result["blank_ratio"] >= 0.8


def test_shadow_probe_flushes_delayed_blackdetect_at_analysis_window(tmp_path, monkeypatch):
    processes = []
    commands = []

    class DeferredBlackStderr:
        def __init__(self):
            self._released = threading.Event()
            self._lines = [
                "[blackdetect @ 000] black_start:0 black_end:4 black_duration:4\n",
            ]
            self._index = 0

        def readline(self):
            self._released.wait(timeout=1.0)
            if self._index >= len(self._lines):
                return ""
            line = self._lines[self._index]
            self._index += 1
            return line

        def release(self):
            self._released.set()

    class FakeProcess:
        def __init__(self):
            self.stderr = DeferredBlackStderr()
            self.returncode = None
            self.terminated = False

        def poll(self):
            return self.returncode

        def terminate(self):
            self.terminated = True
            self.returncode = 0
            self.stderr.release()

        def kill(self):
            self.returncode = -9
            self.stderr.release()

        def wait(self, timeout=None):
            if self.returncode is None:
                self.terminate()
            return self.returncode

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
        statuses=[{"uuid-1": active_status(stream_id=10)}],
        channels=[{"id": 1, "uuid": "uuid-1", "streams": [10, 11]}],
    )
    service = make_service(tmp_path, udi=udi)
    config = normalize_config({
        "watch_mode": "continuous",
        "probe_duration_seconds": 60,
        "blank_min_duration_seconds": 2,
        "blank_ratio_threshold": 0.8,
        "freeze_detection_enabled": False,
        "no_decodable_frames_detection_enabled": False,
        "silent_audio_detection_enabled": False,
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
    assert processes[0].terminated is True
    assert "-t" not in commands[0]
    assert result.get("viewer_left") is not True
    assert result["blank_detected"] is True
    assert result["blank_duration_secs"] == 4.0
    assert result["blank_ratio"] >= 0.8


def test_shadow_probe_ignores_short_broadcast_black_segment_below_ratio_gate(tmp_path, monkeypatch):
    processes = []

    class DeferredBlackStderr:
        def __init__(self):
            self._released = threading.Event()
            self._lines = [
                "[blackdetect @ 000] black_start:0 black_end:3.64 black_duration:3.64\n",
            ]
            self._index = 0

        def readline(self):
            self._released.wait(timeout=1.0)
            if self._index >= len(self._lines):
                return ""
            line = self._lines[self._index]
            self._index += 1
            return line

        def release(self):
            self._released.set()

    class FakeProcess:
        def __init__(self):
            self.stderr = DeferredBlackStderr()
            self.returncode = None

        def poll(self):
            return self.returncode

        def terminate(self):
            self.returncode = 0
            self.stderr.release()

        def kill(self):
            self.returncode = -9
            self.stderr.release()

        def wait(self, timeout=None):
            if self.returncode is None:
                self.terminate()
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
        "freeze_detection_enabled": False,
        "no_decodable_frames_detection_enabled": True,
        "silent_audio_detection_enabled": True,
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
    assert result["blank_detected"] is False
    assert result["blank_duration_secs"] == 3.64
    assert result["blank_ratio"] < 0.8


def test_continuous_probe_waits_for_open_freeze_ratio_threshold(tmp_path, monkeypatch):
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
    assert result["freeze_duration_secs"] >= 9.0
    assert result["freeze_ratio"] >= 0.8
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


def test_continuous_probe_detects_audio_only_no_decoded_video_frames(tmp_path, monkeypatch):
    processes = []

    class FakeProcess:
        def __init__(self):
            self.stderr = io.StringIO(
                "  Stream #0:0: Audio: aac, 48000 Hz, stereo, fltp, 128 kb/s\n"
                "Stream mapping:\n"
                "  Stream #0:0 -> #0:0 (aac (native) -> pcm_s16le (native))\n"
                "frame=   42 fps=2.0 q=-0.0 size=N/A time=00:00:10.00 bitrate=N/A speed=1x\n"
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

    current_time = {"value": 200.0}

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
    assert result["no_decodable_frames_error"] == "no decoded video frames"
    assert result.get("blank_detected") is False


def test_no_decodable_parser_treats_invalid_data_as_terminal_decoder_stall():
    config = normalize_config({
        "watch_mode": "continuous",
        "no_decodable_frames_min_duration_seconds": 10,
    })

    result = ShadowBlankMonitorService._parse_no_decodable_frames_detection(
        "http://provider.example/bad.ts: Invalid data found when processing input\n",
        config,
        observed_duration=0.4,
        returncode=1,
    )

    assert result["no_decodable_frames_detected"] is True
    assert result["no_decodable_frames_duration_secs"] == 10
    assert result["no_decodable_frames_error"] == "invalid data found"


def test_no_decodable_parser_detects_audio_only_stream_without_decoder_error():
    config = normalize_config({
        "watch_mode": "continuous",
        "no_decodable_frames_min_duration_seconds": 10,
    })
    output = (
        "  Stream #0:0[0x100]: Audio: aac (LC), 48000 Hz, mono, fltp, 67 kb/s\n"
        "Stream mapping:\n"
        "  Stream #0:0 -> #0:0 (aac (native) -> pcm_s16le (native))\n"
        "size=N/A time=00:00:12.00 bitrate=N/A speed=1.68x\n"
    )

    result = ShadowBlankMonitorService._parse_no_decodable_frames_detection(
        output,
        config,
        observed_duration=12.6,
        returncode=0,
    )

    assert result["no_decodable_frames_detected"] is True
    assert result["no_decodable_frames_duration_secs"] == 12.6
    assert result["no_decodable_frames_error"] == "no decoded video frames"


def test_no_decodable_parser_keeps_audio_only_stream_pending_before_minimum():
    config = normalize_config({
        "watch_mode": "continuous",
        "no_decodable_frames_min_duration_seconds": 10,
    })
    output = (
        "  Stream #0:0: Audio: aac, 48000 Hz, stereo, fltp, 128 kb/s\n"
        "Stream mapping:\n"
        "  Stream #0:0 -> #0:0 (aac (native) -> pcm_s16le (native))\n"
    )

    result = ShadowBlankMonitorService._parse_no_decodable_frames_detection(
        output,
        config,
        observed_duration=9.9,
        returncode=0,
    )

    assert result["no_decodable_frames_detected"] is False
    assert result["no_decodable_frames_error"] is None


def test_no_decodable_parser_ignores_invalid_data_after_decoded_frames():
    config = normalize_config({
        "watch_mode": "continuous",
        "no_decodable_frames_min_duration_seconds": 10,
    })

    result = ShadowBlankMonitorService._parse_no_decodable_frames_detection(
        "frame=   12 fps=2.0 q=-0.0 size=N/A time=00:00:06.00 bitrate=N/A speed=1x\n"
        "Invalid data found when processing input\n",
        config,
        observed_duration=10.5,
        returncode=1,
    )

    assert result["no_decodable_frames_detected"] is False
    assert result["no_decodable_frames_error"] is None


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


def test_bounded_probe_aborts_when_viewer_leaves_with_loop_detection(tmp_path, monkeypatch):
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
    service = make_service(tmp_path, udi=udi)
    config = normalize_config({
        "enabled": False,
        "dry_run": False,
        "watch_mode": "continuous",
        "loop_detection_enabled": True,
        "probe_duration_seconds": 60,
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
        continuous=False,
    )

    assert result["viewer_left"] is True
    assert processes[0].terminated is True
    assert "-t" in commands[0]


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
    assert normalize_config({})["included_channel_ids"] == []
    assert normalize_config({})["included_channel_uuids"] == []
    status = service.get_status()
    assert status["watched_count"] == 2
    assert status["excluded_active_count"] == 2
    assert {
        (target["channel_id"], target["exclude_reason"])
        for target in status["excluded_active_channels"]
    } == {
        (2, "channel_excluded"),
        (4, "channel_excluded"),
    }


def test_forced_discovery_can_scope_to_included_channels_without_persisting_config(tmp_path):
    probe_urls = []
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
        }],
        channels=[
            {"id": 1, "uuid": "uuid-1", "streams": [10, 11]},
            {"id": 2, "uuid": "uuid-2", "streams": [20, 21]},
            {"id": 3, "uuid": "uuid-3", "streams": [30, 31]},
        ],
    )
    service = make_service(
        tmp_path,
        udi=udi,
        blank_probe=lambda url, config: probe_urls.append(url) or {"blank_detected": False},
    )
    service.update_config({"enabled": False, "dry_run": False, "max_concurrent_watchers": 3})

    status = service.run_once(
        force=True,
        include_channel_ids=[2],
        include_channel_uuids=["uuid-3"],
    )

    assert probe_urls == [
        "http://dispatcharr.local/proxy/ts/stream/uuid-2",
        "http://dispatcharr.local/proxy/ts/stream/uuid-3",
    ]
    assert status["watched_count"] == 2
    assert all(target["real_client_count"] == 1 for target in status["watched_channels"])
    assert "include_channel_ids" not in service.get_config()
    assert "include_channel_uuids" not in service.get_config()


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


def test_shadow_media_probe_uses_viewer_proxy_even_when_stream_url_available(tmp_path):
    probe_urls = []
    probe_keys = []

    udi = FakeUdi(
        statuses=[{
            "uuid-1": active_status(
                stream_id=10,
                clients=[{"user_agent": "VLC", "output_format": "fmp4"}],
            ),
        }],
        channels=[{"id": 1, "uuid": "uuid-1", "streams": [10, 11]}],
        streams={10: {"id": 10, "url": "http://provider.local/live/arte.m3u8"}},
    )
    service = make_service(
        tmp_path,
        udi=udi,
        blank_probe=lambda url, config: (
            probe_urls.append(url)
            or probe_keys.append(config.get("watcher_api_key"))
            or {"blank_detected": False}
        ),
    )
    service.update_config({"enabled": False, "dry_run": False})

    status = service.run_once(force=True)

    assert probe_urls == ["http://dispatcharr.local/proxy/ts/stream/uuid-1?output_format=fmp4"]
    assert probe_keys == ["test-watcher-key"]
    assert status["recent_events"][0]["type"] == "probe_ok"
    assert status["recent_events"][0]["details"]["media_probe_source"] == "channel_proxy"
    assert status["watched_channels"][0]["viewer_output_format"] == "fmp4"


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

    def continuous_probe(url, config, udi_arg, target, *, continuous=True):
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


def test_stopping_during_discovery_cannot_repopulate_watched_snapshot(tmp_path):
    entered_fetch = threading.Event()
    release_fetch = threading.Event()
    probe_calls = []
    udi = FakeUdi(
        statuses=[{"uuid-1": active_status()}],
        channels=[{"id": 1, "uuid": "uuid-1", "streams": [10, 11]}],
    )
    original_get_proxy_status = udi.get_proxy_status

    def delayed_get_proxy_status():
        entered_fetch.set()
        assert release_fetch.wait(timeout=2)
        return original_get_proxy_status()

    udi.get_proxy_status = delayed_get_proxy_status
    service = make_service(
        tmp_path,
        udi=udi,
        blank_probe=lambda url, config: probe_calls.append(url) or {"blank_detected": False},
    )
    with service._lock:
        service._config["enabled"] = True
    service._stop_event.clear()

    scan_thread = threading.Thread(target=service.run_once)
    scan_thread.start()
    assert entered_fetch.wait(timeout=1)

    service.stop(persist=False)
    release_fetch.set()
    scan_thread.join(timeout=2)

    assert not scan_thread.is_alive()
    assert probe_calls == []
    assert service.get_status()["watched_count"] == 0
    assert service.get_status()["watched_channels"] == []


def test_stopping_during_probe_cannot_restore_viewer_grace_snapshot(tmp_path, monkeypatch):
    probe_started = threading.Event()
    release_probe = threading.Event()
    udi = FakeUdi(
        statuses=[{"uuid-1": active_status()}],
        channels=[{"id": 1, "uuid": "uuid-1", "streams": [10, 11]}],
    )
    service = make_service(tmp_path, udi=udi)
    service._uses_default_blank_probe = True
    service.update_config({
        "enabled": False,
        "watch_mode": "continuous",
        "viewer_left_grace_seconds": 5,
        "watcher_api_key": "test-watcher-key",
    })
    config = service.get_config(include_secret=True)
    config["_shadow_allow_viewer_left_grace"] = True
    target = {
        "channel_uuid": "uuid-1",
        "channel_id": 1,
        "channel_ref": "channel-1",
        "stream_id": 10,
        "stream_ref": "stream-10",
        "real_client_count": 1,
        "watcher_client_count": 0,
    }
    with service._lock:
        service._watched["uuid-1"] = dict(target)

    def late_probe(_udi, probe_target, probe_config):
        probe_started.set()
        assert release_probe.wait(timeout=2)
        service._mark_viewer_left_grace(probe_target, probe_config, {})
        return False

    monkeypatch.setattr(service, "_probe_target_once", late_probe)
    service._stop_event.clear()
    service._probe_targets(udi, [target], config)
    assert probe_started.wait(timeout=1)

    service.stop(persist=False)
    release_probe.set()
    for _ in range(40):
        with service._lock:
            if not service._active_probes:
                break
        time.sleep(0.05)

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
    assert len(status["cooldowns"]) == 1
    assert status["cooldowns"][0]["cooldown_seconds"] == 300
    assert status["switch_summary"]["successful_switches"] == 1
    assert status["switch_summary"]["last_switch_reason"] == "blank"
    assert status["switch_summary"]["prevented_false_switches"] == 0


def test_switch_event_keeps_fault_origin_when_shared_target_updates_during_switch(tmp_path):
    udi = FakeUdi(
        statuses=[{"uuid-1": active_status(stream_id=10)}],
        channels=[{"id": 1, "uuid": "uuid-1", "streams": [10, 11]}],
    )
    target = {
        "channel_uuid": "uuid-1",
        "channel_id": 1,
        "channel_ref": shadow_module._ref("channel", 1),
        "stream_id": 10,
        "stream_ref": shadow_module._ref("stream", 10),
        "real_client_count": 1,
    }

    def switch_stream(_channel_id, stream_id=None, url=None):
        assert url is None
        target["stream_id"] = stream_id
        target["stream_ref"] = shadow_module._ref("stream", stream_id)
        return True

    service = make_service(
        tmp_path,
        udi=udi,
        blank_probe=lambda url, config: {"blank_detected": True},
    )
    service.switch_stream = switch_stream
    service._uses_default_switch_stream = False
    config = normalize_config({
        "enabled": False,
        "dry_run": False,
        "confirmation_count": 1,
    })

    should_continue = service._probe_target_once(udi, target, config)
    event = service.get_status()["recent_events"][0]

    assert should_continue is False
    assert event["type"] == "switch_success"
    assert event["stream_ref"] == shadow_module._ref("stream", 10)
    assert event["details"]["origin_stream_ref"] == shadow_module._ref("stream", 10)
    assert event["details"]["target_stream_ref"] == shadow_module._ref("stream", 11)


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
        pre_probe_calls.append((url, config["probe_duration_seconds"], config.get("watcher_api_key")))
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
        ("http://candidate.local/bad", 12, ""),
        ("http://candidate.local/good", 12, ""),
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


def test_next_stream_pre_probe_skips_incomplete_candidate(tmp_path):
    udi = FakeUdi(
        statuses=[{"uuid-1": active_status()}],
        channels=[{"id": 1, "uuid": "uuid-1", "streams": [10, 11, 12]}],
        streams={
            11: {"id": 11, "url": "http://candidate.local/ended"},
            12: {"id": 12, "url": "http://candidate.local/good"},
        },
    )
    service = make_service(tmp_path, udi=udi)
    service._run_blank_probe = lambda url, config: {
        "probe_incomplete": "ended" in url,
        "probe_elapsed_seconds": 0.1 if "ended" in url else config["probe_duration_seconds"],
        "probe_expected_duration_seconds": config["probe_duration_seconds"],
    }
    config = normalize_config({
        "next_stream_pre_probe_enabled": True,
        "next_stream_pre_probe_duration_seconds": 5,
    })
    target = {
        "channel_uuid": "uuid-1",
        "channel_id": 1,
        "channel_ref": "channel-test",
        "stream_id": 10,
        "stream_ref": "stream-test",
        "real_client_count": 1,
    }

    alternative, details = service._choose_preprobed_alternative_stream(
        udi,
        target,
        config,
        10,
        reason="freeze",
    )
    status = service.get_status()

    assert alternative == 12
    assert details["result"] == "ok"
    rejected = next(event for event in status["recent_events"] if event["type"] == "pre_probe_rejected")
    assert rejected["details"]["rejection_reason"] == "incomplete_probe"
    assert rejected["details"]["probe_incomplete"] is True


def test_pre_probe_recovers_when_proxy_status_reports_stale_current_stream(tmp_path, monkeypatch):
    switch_calls = []
    proxy_probe_calls = []
    pre_probe_calls = []
    current_time = {"value": 100.0}

    def fake_monotonic():
        current_time["value"] += 1.0
        return current_time["value"]

    def proxy_probe(url, config):
        proxy_probe_calls.append(url)
        if len(proxy_probe_calls) == 1:
            return {
                "blank_detected": False,
                "solid_color_detected": True,
                "freeze_detected": False,
            }
        return {
            "blank_detected": False,
            "solid_color_detected": False,
            "freeze_detected": False,
        }

    def pre_probe(url, config):
        pre_probe_calls.append(url)
        return {
            "blank_detected": "bad" in url,
            "freeze_detected": False,
        }

    monkeypatch.setattr(shadow_module.time, "monotonic", fake_monotonic)
    monkeypatch.setattr(shadow_module.time, "sleep", lambda _seconds: None)

    udi = FakeUdi(
        statuses=[{"uuid-1": active_status(stream_id=10)}],
        channels=[{"id": 1, "uuid": "uuid-1", "streams": [10, 11]}],
        streams={
            10: {"id": 10, "url": "http://candidate.local/good"},
            11: {"id": 11, "url": "http://candidate.local/bad"},
        },
    )
    service = make_service(
        tmp_path,
        udi=udi,
        blank_probe=proxy_probe,
        switch_calls=switch_calls,
    )
    service._run_blank_probe = pre_probe
    service._uses_default_switch_stream = True
    config = normalize_config({
        "enabled": False,
        "dry_run": False,
        "confirmation_count": 1,
        "next_stream_pre_probe_enabled": True,
        "next_stream_pre_probe_duration_seconds": 1,
    })
    target = {
        "channel_uuid": "uuid-1",
        "channel_id": 1,
        "channel_ref": "channel-test",
        "stream_id": 10,
        "stream_ref": "stream-test",
        "real_client_count": 1,
        "viewer_output_format": "fmp4",
    }

    should_continue = service._probe_target_once(udi, target, config)
    status = service.get_status()
    event = status["recent_events"][0]

    assert should_continue is False
    assert pre_probe_calls == [
        "http://candidate.local/bad",
        "http://candidate.local/good",
    ]
    assert switch_calls == [("uuid-1", 10, None)]
    assert len(proxy_probe_calls) == 2
    assert event["type"] == "switch_success"
    assert event["stream_ref"] == shadow_module._ref("stream", 11)
    assert event["details"]["origin_stream_ref"] == shadow_module._ref("stream", 11)
    assert event["details"]["origin_stream_source"] == "content_matching_preprobe"
    assert event["details"]["pre_probe"]["reported_current_reprobe"] is True
    assert event["details"]["pre_probe"]["reported_current_stream_ref"] == shadow_module._ref("stream", 10)
    assert event["details"]["pre_probe"]["inferred_origin_stream_ref"] == shadow_module._ref("stream", 11)
    assert event["details"]["pre_probe"]["origin_inference"] == (
        "single_preprobe_candidate_reproduced_active_fault"
    )
    assert "_inferred_origin_stream_id" not in event["details"]["pre_probe"]
    assert event["details"]["post_switch_proxy_probe_required"] is True
    assert event["details"]["post_switch_verification_mode"] == "status_stream_id+proxy_probe"
    assert "post_switch_status_mismatch" not in event["details"]


def test_stale_proxy_status_does_not_guess_between_multiple_fault_candidates(tmp_path):
    udi = FakeUdi(
        statuses=[{"uuid-1": active_status(stream_id=10)}],
        channels=[{"id": 1, "uuid": "uuid-1", "streams": [10, 11, 12]}],
        streams={
            10: {"id": 10, "url": "http://candidate.local/good"},
            11: {"id": 11, "url": "http://candidate.local/bad-one"},
            12: {"id": 12, "url": "http://candidate.local/bad-two"},
        },
    )
    service = make_service(tmp_path, udi=udi)
    service._run_blank_probe = lambda url, _config: {
        "blank_detected": "bad" in url,
        "freeze_detected": False,
    }
    config = normalize_config({
        "next_stream_pre_probe_enabled": True,
        "next_stream_pre_probe_duration_seconds": 1,
    })
    target = {
        "channel_uuid": "uuid-1",
        "channel_id": 1,
        "channel_ref": "channel-test",
        "stream_id": 10,
        "stream_ref": "stream-test",
        "real_client_count": 1,
    }

    alternative, details = service._choose_preprobed_alternative_stream(
        udi,
        target,
        config,
        10,
        reason="solid_color",
    )

    assert alternative == 10
    assert details["reported_current_reprobe"] is True
    assert details["origin_inference"] == "ambiguous_preprobe_fault_candidates"
    assert details["matching_fault_candidate_count"] == 2
    assert "inferred_origin_stream_ref" not in details
    assert "_inferred_origin_stream_id" not in details


def test_required_fmp4_proxy_probe_retries_incomplete_reconnect(tmp_path, monkeypatch):
    proxy_probe_calls = []
    proxy_results = iter([
        {"blank_detected": False, "freeze_detected": True},
        {
            "blank_detected": False,
            "freeze_detected": False,
            "probe_incomplete": True,
            "probe_elapsed_seconds": 6.2,
            "probe_expected_duration_seconds": 12,
        },
        {
            "blank_detected": False,
            "freeze_detected": False,
            "probe_incomplete": False,
            "probe_elapsed_seconds": 12.1,
            "probe_expected_duration_seconds": 12,
        },
    ])
    current_time = {"value": 100.0}

    def fake_monotonic():
        current_time["value"] += 1.0
        return current_time["value"]

    def proxy_probe(url, config):
        proxy_probe_calls.append(url)
        return next(proxy_results)

    monkeypatch.setattr(shadow_module.time, "monotonic", fake_monotonic)
    monkeypatch.setattr(shadow_module.time, "sleep", lambda _seconds: None)

    udi = FakeUdi(
        statuses=[{"uuid-1": active_status(stream_id=10)}],
        channels=[{"id": 1, "uuid": "uuid-1", "streams": [10, 11]}],
        streams={
            10: {"id": 10, "url": "http://candidate.local/good"},
            11: {"id": 11, "url": "http://candidate.local/bad"},
        },
    )
    service = make_service(
        tmp_path,
        udi=udi,
        blank_probe=proxy_probe,
        switch_calls=[],
    )
    service._run_blank_probe = lambda url, config: {
        "blank_detected": False,
        "freeze_detected": "bad" in url,
    }
    service._uses_default_switch_stream = True
    config = normalize_config({
        "enabled": False,
        "dry_run": False,
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
        "viewer_output_format": "fmp4",
    }

    should_continue = service._probe_target_once(udi, target, config)
    event = service.get_status()["recent_events"][0]

    assert should_continue is False
    assert len(proxy_probe_calls) == 3
    assert event["type"] == "switch_success"
    assert event["details"]["post_switch_proxy_probe_accepted"] is True
    assert event["details"]["post_switch_proxy_probe"]["probe_incomplete"] is False
    assert event["details"]["post_switch_proxy_probe_attempts"] == 2


def test_post_switch_proxy_retries_wait_for_previous_shadow_client_release(tmp_path, monkeypatch):
    proxy_probe_calls = []
    current_time = {"value": 100.0}
    watcher = {
        "client_id": "previous-shadow-probe",
        "user_agent": DEFAULT_WATCHER_USER_AGENT,
        "connected_at": 99.0,
    }

    def fake_monotonic():
        return current_time["value"]

    def fake_sleep(seconds):
        current_time["value"] += seconds

    def proxy_probe(_url, _config):
        proxy_probe_calls.append(current_time["value"])
        if len(proxy_probe_calls) == 1:
            return {
                "blank_detected": False,
                "freeze_detected": False,
                "probe_incomplete": True,
            }
        return {
            "blank_detected": False,
            "freeze_detected": False,
            "probe_incomplete": False,
        }

    monkeypatch.setattr(shadow_module.time, "monotonic", fake_monotonic)
    monkeypatch.setattr(shadow_module.time, "sleep", fake_sleep)

    with_watcher = {
        "uuid-1": active_status(
            stream_id=11,
            clients=[{"client_id": "viewer", "user_agent": "TiviMate"}, watcher],
        )
    }
    without_watcher = {
        "uuid-1": active_status(
            stream_id=11,
            clients=[{"client_id": "viewer", "user_agent": "TiviMate"}],
        )
    }
    udi = FakeUdi(
        statuses=[
            with_watcher,
            with_watcher,
            without_watcher,
            without_watcher,
            with_watcher,
            with_watcher,
            without_watcher,
            without_watcher,
        ],
        channels=[{"id": 1, "uuid": "uuid-1", "streams": [10, 11]}],
    )
    service = make_service(tmp_path, udi=udi, blank_probe=proxy_probe)
    target = {
        "channel_uuid": "uuid-1",
        "channel_id": 1,
        "channel_ref": "channel-test",
        "stream_id": 10,
        "stream_ref": "stream-test",
        "real_client_count": 1,
        "viewer_output_format": "fmp4",
    }

    success, observed_stream_id, details = service._verify_active_stream_after_switch(
        udi,
        target,
        11,
        normalize_config({"next_stream_pre_probe_enabled": True}),
        require_proxy_probe=True,
    )

    assert success is True
    assert observed_stream_id == 11
    assert len(proxy_probe_calls) == 2
    assert proxy_probe_calls[0] >= 101.5
    assert proxy_probe_calls[1] >= proxy_probe_calls[0] + 2.0
    assert details["post_switch_proxy_probe_attempts"] == 2
    assert details["post_switch_proxy_probe_accepted"] is True


def test_required_proxy_probe_keeps_fault_rejection_until_window_expires(tmp_path, monkeypatch):
    proxy_probe_calls = []
    current_time = {"value": 100.0}

    def fake_monotonic():
        return current_time["value"]

    def fake_sleep(seconds):
        current_time["value"] += seconds

    def proxy_probe(url, config):
        proxy_probe_calls.append(url)
        if len(proxy_probe_calls) == 1:
            return {"blank_detected": False, "silent_audio_detected": True}
        return {"blank_detected": False, "silent_audio_detected": True}

    monkeypatch.setattr(shadow_module.time, "monotonic", fake_monotonic)
    monkeypatch.setattr(shadow_module.time, "sleep", fake_sleep)

    udi = FakeUdi(
        statuses=[{"uuid-1": active_status(stream_id=10)}],
        channels=[{"id": 1, "uuid": "uuid-1", "streams": [10, 11]}],
        streams={
            10: {"id": 10, "url": "http://candidate.local/good"},
            11: {"id": 11, "url": "http://candidate.local/bad"},
        },
    )
    service = make_service(
        tmp_path,
        udi=udi,
        blank_probe=proxy_probe,
        switch_calls=[],
    )
    service._run_blank_probe = lambda url, config: {
        "blank_detected": False,
        "freeze_detected": "bad" in url,
    }
    service._uses_default_switch_stream = True
    config = normalize_config({
        "enabled": False,
        "dry_run": False,
        "confirmation_count": 1,
        "next_stream_pre_probe_enabled": True,
        "silent_audio_detection_enabled": True,
    })
    target = {
        "channel_uuid": "uuid-1",
        "channel_id": 1,
        "channel_ref": "channel-test",
        "stream_id": 10,
        "stream_ref": "stream-test",
        "real_client_count": 1,
        "viewer_output_format": "mpegts",
    }

    should_continue = service._probe_target_once(udi, target, config)
    event = service.get_status()["recent_events"][0]

    assert should_continue is False
    assert len(proxy_probe_calls) >= 2
    assert event["type"] == "switch_failed"
    assert event["details"]["post_switch_proxy_probe_accepted"] is False
    assert event["details"]["post_switch_proxy_probe"]["rejection_reason"] == "silent_audio"
    assert event["details"]["post_switch_proxy_probe_attempts"] > 1


def test_normal_switch_retries_proxy_content_while_status_is_stale(tmp_path, monkeypatch):
    proxy_probe_calls = []
    current_time = {"value": 100.0}

    def fake_monotonic():
        current_time["value"] += 1.0
        return current_time["value"]

    def proxy_probe(url, config):
        proxy_probe_calls.append((url, dict(config)))
        if len(proxy_probe_calls) == 1:
            return {
                "blank_detected": False,
                "freeze_detected": False,
                "probe_incomplete": True,
            }
        return {
            "blank_detected": False,
            "freeze_detected": False,
            "probe_incomplete": False,
        }

    monkeypatch.setattr(shadow_module.time, "monotonic", fake_monotonic)
    monkeypatch.setattr(shadow_module.time, "sleep", lambda _seconds: None)
    udi = FakeUdi(
        statuses=[{"uuid-1": active_status(stream_id=10)}],
        channels=[{"id": 1, "uuid": "uuid-1", "streams": [10, 11]}],
    )
    service = make_service(tmp_path, udi=udi, blank_probe=proxy_probe)
    target = {
        "channel_uuid": "uuid-1",
        "channel_id": 1,
        "viewer_output_format": "mpegts",
    }

    success, observed_stream_id, details = service._verify_active_stream_after_switch(
        udi,
        target,
        11,
        normalize_config({"next_stream_pre_probe_enabled": True}),
    )

    assert success is True
    assert observed_stream_id == 10
    assert len(proxy_probe_calls) == 2
    assert details["post_switch_status_mismatch"] is True
    assert details["post_switch_proxy_probe_attempts"] == 2
    assert details["post_switch_proxy_probe_accepted"] is True


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


def test_pre_probe_provider_slot_polls_viewer_preemption_and_releases_claim(
    tmp_path,
    monkeypatch,
):
    from apps.stream import concurrent_stream_limiter as limiter_module

    profile = {"id": 71, "name": "Alternate", "max_streams": 1}

    class FakeLimiter:
        def __init__(self):
            self.udi_manager = None
            self.preempt = False
            self.preempt_calls = []
            self.released_profiles = []
            self.released_accounts = []
            self.released_claims = []

        def acquire(self, account_id, timeout=0):
            assert timeout == 0
            return True, None

        def reserve_profile_for_stream_with_url(self, stream):
            assert stream["m3u_account_id"] == 7
            return True, "acquired", profile, "https://profile.invalid/live/channel"

        def should_preempt_profile_for_viewer(
            self,
            reserved_profile,
            *,
            account_id,
            reservation_token,
        ):
            self.preempt_calls.append((reserved_profile, account_id, reservation_token))
            return self.preempt

        def release_profile(self, reserved_profile):
            self.released_profiles.append(reserved_profile)

        def release(self, account_id):
            self.released_accounts.append(account_id)

        def release_viewer_preemption_claim(self, reservation_token):
            self.released_claims.append(reservation_token)

    limiter = FakeLimiter()
    monkeypatch.setattr(limiter_module, "get_account_limiter", lambda: limiter)
    monkeypatch.setattr(limiter_module, "initialize_account_limits", lambda _accounts: None)
    udi = FakeUdi(statuses=[{}], channels=[])
    udi.get_m3u_accounts = lambda: [{"id": 7}]
    service = make_service(tmp_path, udi=udi)

    slot = service._acquire_pre_probe_provider_slot(
        udi,
        {
            "id": 11,
            "m3u_account_id": 7,
            "url": "https://default.invalid/live/channel",
        },
    )

    assert slot["acquired"] is True
    assert slot["preemption_state"] == {"viewer_preempted": False}
    assert slot["provider_preempt_check"]() is False
    limiter.preempt = True
    assert slot["provider_preempt_check"]() is True
    assert slot["preemption_state"] == {"viewer_preempted": True}
    assert limiter.preempt_calls[-1][0] is profile
    assert limiter.preempt_calls[-1][1] == 7
    assert limiter.preempt_calls[-1][2] is slot["preemption_token"]

    service._release_pre_probe_provider_slot(slot)

    assert limiter.released_profiles == [profile]
    assert limiter.released_accounts == [7]
    assert limiter.released_claims == [slot["preemption_token"]]


@pytest.mark.parametrize("failure_stage", ["profile", "account"])
def test_pre_probe_provider_slot_releases_claim_when_slot_release_fails(failure_stage):
    token = object()

    class FailingReleaseLimiter:
        def __init__(self):
            self.released_profiles = []
            self.released_accounts = []
            self.released_claims = []

        def release_profile(self, profile):
            self.released_profiles.append(profile)
            if failure_stage == "profile":
                raise RuntimeError("profile release failed")

        def release(self, account_id):
            self.released_accounts.append(account_id)
            if failure_stage == "account":
                raise RuntimeError("account release failed")

        def release_viewer_preemption_claim(self, reservation_token):
            self.released_claims.append(reservation_token)

    limiter = FailingReleaseLimiter()
    profile = {"id": 71}
    with pytest.raises(RuntimeError, match=f"{failure_stage} release failed"):
        ShadowBlankMonitorService._release_pre_probe_provider_slot({
            "limiter": limiter,
            "profile": profile,
            "account_id": 7,
            "preemption_token": token,
        })

    assert limiter.released_profiles == [profile]
    assert limiter.released_accounts == [7]
    assert limiter.released_claims == [token]


def test_next_stream_pre_probe_viewer_preemption_is_not_a_media_fault(tmp_path):
    switch_calls = []
    udi = FakeUdi(
        statuses=[{"uuid-1": active_status()}],
        channels=[{"id": 1, "uuid": "uuid-1", "streams": [10, 11]}],
        streams={
            11: {
                "id": 11,
                "url": "https://candidate.invalid/live/channel",
                "m3u_account_id": 7,
            },
        },
    )
    service = make_service(
        tmp_path,
        udi=udi,
        blank_probe=lambda _url, _config: {"blank_detected": True},
        switch_calls=switch_calls,
    )
    preemption_state = {"viewer_preempted": False}
    released = []

    service._acquire_pre_probe_provider_slot = lambda _udi, stream: {
        "acquired": True,
        "reason": "acquired",
        "url": stream["url"],
        "preemption_state": preemption_state,
        "provider_preempt_check": lambda: True,
        "details": {"provider_limited": True, "provider_slot_acquired": True},
    }
    service._release_pre_probe_provider_slot = lambda slot: released.append(slot)
    service._run_blank_probe = lambda _url, _config: {"blank_detected": False}
    service.update_config({
        "enabled": False,
        "dry_run": False,
        "confirmation_count": 1,
        "next_stream_pre_probe_enabled": True,
    })

    status = service.run_once(force=True)

    assert switch_calls == []
    assert len(released) == 1
    assert status["recent_events"][1]["type"] == "pre_probe_rejected"
    assert status["recent_events"][1]["details"]["rejection_reason"] == "viewer_preempted"
    assert status["recent_events"][1]["details"]["viewer_preempted"] is True
    assert status["recent_events"][1]["details"]["slot_scope"] == "provider"
    assert status["pre_probe"]["metrics"].get("preprobe_rejected_media_fault", 0) == 0
    assert status["pre_probe"]["metrics"]["preprobe_skipped_provider_limit"] == 1


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
    assert len(status["cooldowns"]) == 1
    assert status["cooldowns"][0]["cooldown_seconds"] == 300


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
    assert len(status["cooldowns"]) == 1
    assert status["cooldowns"][0]["cooldown_seconds"] == 300


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


def test_audio_detection_parser_detects_mpegts_aac_buffer_errors_as_garbled_audio():
    config = normalize_config({
        "garbled_audio_detection_enabled": True,
        "garbled_audio_error_threshold": 2,
    })
    output = (
        "[aac @ 000] Input buffer exhausted before END element found\n"
        "[aac @ 000] channel element 0.2 is not allocated\n"
    )

    parsed = ShadowBlankMonitorService._parse_audio_detection(
        output,
        config,
        observed_duration=8,
    )

    assert parsed["garbled_audio_detected"] is True
    assert parsed["garbled_audio_error_count"] == 2
    assert "input buffer exhausted" in parsed["garbled_audio_error"].lower()


def test_audio_detection_parser_detects_fmp4_aac_artifact_warnings_as_garbled_audio():
    config = normalize_config({
        "garbled_audio_detection_enabled": True,
        "garbled_audio_error_threshold": 2,
    })
    output = (
        "[aac @ 000] If you heard an audible artifact, there may be a bug in the decoder. "
        "Clipped noise gain (285 -> 155) is not implemented.\n"
        "[aac @ 000] If you heard an audible artifact, there may be a bug in the decoder. "
        "Clipped noise gain (279 -> 155) is not implemented.\n"
    )

    parsed = ShadowBlankMonitorService._parse_audio_detection(
        output,
        config,
        observed_duration=8,
    )

    assert parsed["garbled_audio_detected"] is True
    assert parsed["garbled_audio_error_count"] == 2
    assert "audible artifact" in parsed["garbled_audio_error"].lower()


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


def test_solid_color_detection_parser_detects_sustained_low_entropy_frames():
    config = normalize_config({
        "freeze_detection_enabled": True,
        "freeze_min_duration_seconds": 5,
    })
    output = "".join(
        (
            f"[Parsed_metadata_3 @ 000] frame:{frame} pts:{frame} pts_time:{frame}\n"
            "[Parsed_metadata_3 @ 000] lavfi.entropy.normalized_entropy.normal.Y=0.000000\n"
            "[Parsed_metadata_3 @ 000] lavfi.entropy.normalized_entropy.normal.U=0.000000\n"
            "[Parsed_metadata_3 @ 000] lavfi.entropy.normalized_entropy.normal.V=0.000000\n"
        )
        for frame in range(6)
    )

    parsed = ShadowBlankMonitorService._parse_solid_color_detection(
        output,
        config,
        observed_duration=6,
    )

    assert parsed["solid_color_detected"] is True
    assert parsed["solid_color_duration_secs"] == 6
    assert parsed["solid_color_sample_count"] == 6
    assert parsed["solid_color_normalized_entropy_max"] == 0.0


def test_solid_color_detection_parser_ignores_short_low_entropy_cuts():
    config = normalize_config({
        "freeze_detection_enabled": True,
        "freeze_min_duration_seconds": 5,
    })
    frame_values = [
        (0.0, 0.0, 0.0),
        (0.0, 0.0, 0.0),
        (0.01, 0.01, 0.01),
        (0.55, 0.42, 0.38),
        (0.63, 0.51, 0.44),
        (0.59, 0.50, 0.43),
    ]
    output = "".join(
        (
            f"[Parsed_metadata_3 @ 000] frame:{frame} pts:{frame} pts_time:{frame}\n"
            f"[Parsed_metadata_3 @ 000] lavfi.entropy.normalized_entropy.normal.Y={y:.6f}\n"
            f"[Parsed_metadata_3 @ 000] lavfi.entropy.normalized_entropy.normal.U={u:.6f}\n"
            f"[Parsed_metadata_3 @ 000] lavfi.entropy.normalized_entropy.normal.V={v:.6f}\n"
        )
        for frame, (y, u, v) in enumerate(frame_values)
    )

    parsed = ShadowBlankMonitorService._parse_solid_color_detection(
        output,
        config,
        observed_duration=6,
    )

    assert parsed["solid_color_detected"] is False
    assert parsed["solid_color_duration_secs"] is None
    assert parsed["solid_color_sample_count"] == 3
    assert parsed["solid_color_normalized_entropy_max"] == 0.63


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
        assert len(status["cooldowns"]) == 1
        assert status["cooldowns"][0]["cooldown_seconds"] == 300


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
        "freeze_ratio_threshold": 0.4,
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
            "freeze_ratio_threshold": 0.4,
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
    assert status["cooldowns"] == []
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


def test_continuous_freeze_with_audio_present_requires_confirmation_and_switches(tmp_path):
    switch_calls = []
    udi = FakeUdi(
        statuses=[
            {
                "uuid-1": active_status(
                    stream_id=10,
                    clients=[{"user_agent": "VLC"}],
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
            "freeze_ratio": 0.95,
            "audio_stream_present": True,
        },
        switch_calls=switch_calls,
    )
    config = normalize_config({
        "enabled": False,
        "dry_run": False,
        "watch_mode": "continuous",
        "confirmation_count": 1,
        "freeze_detection_enabled": True,
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

    first_result = service._probe_target_once(udi, target, config)
    first_status = service.get_status()

    assert first_result is True
    assert switch_calls == []
    assert first_status["recent_events"][0]["type"] == "freeze_pending"
    assert first_status["recent_events"][0]["details"]["required"] == 2
    first_probe = first_status["recent_events"][0]["details"]["detection"]["measurements"]
    assert first_probe["freeze_duration_secs"] == 12.0

    second_result = service._probe_target_once(udi, target, config)
    status = service.get_status()

    assert second_result is False
    assert switch_calls == [("uuid-1", 11, None)]
    assert status["recent_events"][0]["type"] == "switch_success"
    details = status["recent_events"][0]["details"]
    assert details["reason"] == "freeze"
    assert target["last_probe"]["freeze_detected"] is True
    assert target["last_probe"]["freeze_audio_present"] is True
    assert target["last_probe"]["freeze_suppressed_audio_present"] is False
    assert target["last_probe"]["audio_stream_present"] is True


def test_continuous_solid_color_with_audio_present_switches(tmp_path):
    switch_calls = []
    udi = FakeUdi(
        statuses=[
            {
                "uuid-1": active_status(
                    stream_id=10,
                    clients=[{"user_agent": "VLC"}],
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
            "freeze_ratio": 0.95,
            "solid_color_detected": True,
            "solid_color_duration_secs": 12.0,
            "solid_color_normalized_entropy_max": 0.0,
            "solid_color_sample_count": 12,
            "audio_stream_present": True,
        },
        switch_calls=switch_calls,
    )
    config = normalize_config({
        "enabled": False,
        "dry_run": False,
        "watch_mode": "continuous",
        "confirmation_count": 1,
        "freeze_detection_enabled": True,
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

    should_continue = service._probe_target_once(udi, target, config)
    status = service.get_status()

    assert should_continue is False
    assert switch_calls == [("uuid-1", 11, None)]
    event = status["recent_events"][0]
    assert event["type"] == "switch_success"
    assert event["trigger_reason"] == "solid_color"
    details = event["details"]
    assert details["reason"] == "solid_color"
    assert details["detection"]["reason"] == "solid_color"
    measurements = details["detection"]["measurements"]
    assert measurements["solid_color_duration_secs"] == 12.0
    assert measurements["solid_color_normalized_entropy_max"] == 0.0
    assert measurements["solid_color_sample_count"] == 12
    assert target["last_probe"]["audio_stream_present"] is True


def test_continuous_blank_fault_recovery_guard_needs_second_confirmation(tmp_path):
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
            "blank_detected": True,
            "blank_duration_secs": 12.0,
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
    assert status["recent_events"][1]["type"] == "blank_pending"


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

    def fake_continuous_probe(url, config, probe_udi, target, *, continuous=True):
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


def test_continuous_probe_continues_with_current_probe_watcher_between_confirmations(tmp_path, monkeypatch):
    switch_calls = []
    probe_calls = []
    monkeypatch.setattr(shadow_module.time, "sleep", lambda _seconds: None)
    current_probe_watcher = {
        "user_agent": "StreamFlow-Shadow-Blank-Monitor/1.0",
        "client_id": "current-probe-watcher",
        "connected_at": 1000.2,
    }
    active_with_current_watcher = active_status(
        stream_id=10,
        clients=[
            {"user_agent": "VLC"},
            current_probe_watcher,
        ],
    )
    active_without_current_watcher = active_status(
        stream_id=10,
        clients=[{"user_agent": "VLC"}],
    )
    udi = FakeUdi(
        statuses=[
            {"uuid-1": active_with_current_watcher},
            {"uuid-1": active_with_current_watcher},
            {"uuid-1": active_without_current_watcher},
            {"uuid-1": active_without_current_watcher},
        ],
        channels=[{"id": 1, "uuid": "uuid-1", "streams": [10, 11]}],
    )
    service = ShadowBlankMonitorService(
        config_file=tmp_path / "shadow.json",
        udi_provider=lambda: udi,
        switch_stream=lambda *args, **kwargs: switch_calls.append((args, kwargs)) or True,
        base_url_provider=lambda: "http://dispatcharr.local",
        stream_checker_provider=lambda: FakeStreamChecker(),
        clock=lambda: 1000.0,
    )

    def fake_continuous_probe(url, config, probe_udi, target, *, continuous=True):
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

    assert len(probe_calls) == 2
    assert switch_calls == [(("uuid-1",), {"stream_id": 11})]
    assert status["recent_events"][0]["type"] == "switch_success"
    assert status["recent_events"][0]["details"]["reason"] == "blank"
    assert "watcher_recovery_guard" not in [event["type"] for event in status["recent_events"]]


def test_clean_low_impact_probe_waits_when_current_probe_client_is_still_visible(tmp_path):
    probe_calls = []
    wait_calls = []

    class StopAfterWait:
        def __init__(self):
            self.stopped = False

        def is_set(self):
            return self.stopped

        def wait(self, seconds):
            wait_calls.append(seconds)
            self.stopped = True
            return True

    current_probe_watcher = {
        "user_agent": "StreamFlow-Shadow-Blank-Monitor/1.0",
        "client_id": "current-probe-watcher",
        "connected_at": 1000.2,
    }
    udi = FakeUdi(
        statuses=[
            {"uuid-1": active_status(stream_id=10, clients=[{"user_agent": "VLC"}])},
            {
                "uuid-1": active_status(
                    stream_id=10,
                    clients=[{"user_agent": "VLC"}, current_probe_watcher],
                )
            },
        ],
        channels=[{"id": 1, "uuid": "uuid-1", "streams": [10, 11]}],
    )
    service = make_service(tmp_path, udi=udi, clock=lambda: 1000.0)
    service._uses_default_blank_probe = True
    service._run_blank_probe_until_viewer_left = (
        lambda url, config, probe_udi, target, *, continuous=True:
        probe_calls.append(url) or {"blank_detected": False}
    )
    service._stop_event = StopAfterWait()
    config = normalize_config({
        "enabled": False,
        "dry_run": False,
        "watch_mode": "continuous",
        "continuous_probe_interval_seconds": 45,
        "watch_gap_seconds": 1,
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
    watched = status["watched_channels"][0]

    assert probe_calls == ["http://dispatcharr.local/proxy/ts/stream/uuid-1"]
    assert wait_calls == [45]
    assert watched["watcher_state"] == "waiting"
    assert watched["watcher_client_count"] == 0
    with service._lock:
        private_watched = service._watched["uuid-1"]
    assert private_watched["shadow_probe_settling"] is True
    assert private_watched["shadow_probe_settling_client_count"] == 1
    assert [event["type"] for event in status["recent_events"]] == ["probe_ok"]


def test_continuous_probe_ignores_recent_probe_window_recovery_between_confirmations(tmp_path):
    switch_calls = []
    udi = FakeUdi(
        statuses=[{"uuid-1": active_status(stream_id=10)}],
        channels=[{"id": 1, "uuid": "uuid-1", "streams": [10, 11]}],
    )
    service = make_service(
        tmp_path,
        udi=udi,
        switch_calls=switch_calls,
    )
    service._run_blank_probe_until_viewer_left = lambda url, config, probe_udi, target, *, continuous=True: {
        "blank_detected": True,
        "blank_duration_secs": 7.0,
    }
    service._uses_default_blank_probe = True
    config = normalize_config({
        "enabled": False,
        "dry_run": False,
        "watch_mode": "continuous",
        "confirmation_count": 2,
        "probe_duration_seconds": 8,
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
    with service._lock:
        service._watched["uuid-1"] = {
            "active_probe_started_at": target["active_probe_started_at"],
            "watcher_recovered_after_seconds": 1,
        }
    second_result = service._probe_target_once(udi, target, config)
    status = service.get_status()

    assert first_result is True
    assert second_result is False
    assert switch_calls == [("uuid-1", 11, None)]
    assert status["recent_events"][0]["type"] == "switch_success"
    assert "watcher_recovery_guard" not in [event["type"] for event in status["recent_events"]]


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
            },
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
            {"uuid-1": active_status(stream_id=10, clients=[{"user_agent": "VLC"}])},
            {"uuid-1": active_status(stream_id=10, clients=[{"user_agent": "VLC"}])},
            {"uuid-1": active_status(stream_id=10, clients=[{"user_agent": "VLC"}])},
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

    def fake_continuous_probe(url, config, probe_udi, target, *, continuous=True):
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
    now = {"value": 1_000.0}
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
        clock=lambda: now["value"],
    )
    service.update_config({
        "enabled": False,
        "dry_run": False,
        "confirmation_count": 1,
        "channel_cooldown_seconds": 30,
    })

    first_status = service.run_once(force=True)
    now["value"] += 31
    second_status = service.run_once(force=True)

    assert switch_calls == [("uuid-1", 11, None), ("uuid-1", 12, None)]
    assert first_status["cooldowns"][0]["cooldown_seconds"] == 30
    assert second_status["cooldowns"][0]["cooldown_seconds"] == 30
    assert second_status["recent_events"][0]["type"] == "switch_success"
    assert second_status["recent_events"][0]["details"]["target_stream_ref"].startswith("stream-")


def test_default_switch_requires_active_stream_verification(tmp_path, monkeypatch):
    switch_calls = []
    current_time = {"value": 100.0}

    def fake_monotonic():
        current_time["value"] += 1.0
        return current_time["value"]

    monkeypatch.setattr(shadow_module.time, "monotonic", fake_monotonic)
    monkeypatch.setattr(shadow_module.time, "sleep", lambda _seconds: None)

    udi = FakeUdi(
        statuses=[{"uuid-1": active_status(stream_id=10)}],
        channels=[{"id": 1, "uuid": "uuid-1", "streams": [10, 11]}],
    )

    def switch_stream(channel_id, stream_id=None, url=None):
        switch_calls.append((channel_id, stream_id, url))
        return True

    service = make_service(
        tmp_path,
        udi=udi,
        blank_probe=lambda url, config: {"blank_detected": True},
        switch_calls=switch_calls,
    )
    service.switch_stream = switch_stream
    service._uses_default_switch_stream = True
    config = normalize_config({
        "enabled": False,
        "dry_run": False,
        "confirmation_count": 1,
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
    assert status["recent_events"][0]["type"] == "switch_failed"
    assert status["recent_events"][0]["details"]["switch_api_success"] is True
    assert status["recent_events"][0]["details"]["post_switch_verification"] is False
    assert status["recent_events"][0]["details"]["post_switch_verification_mode"] == "proxy_probe"
    assert status["recent_events"][0]["details"]["post_switch_status_mismatch"] is True
    assert status["recent_events"][0]["details"]["observed_stream_ref"].startswith("stream-")


def test_fmp4_switch_verification_waits_for_late_status_update(tmp_path, monkeypatch):
    current_time = {"value": 100.0}

    def fake_monotonic():
        return current_time["value"]

    def fake_sleep(seconds):
        current_time["value"] += seconds

    monkeypatch.setattr(shadow_module.time, "monotonic", fake_monotonic)
    monkeypatch.setattr(shadow_module.time, "sleep", fake_sleep)
    udi = FakeUdi(
        statuses=[
            *({"uuid-1": active_status(stream_id=10)} for _ in range(25)),
            {"uuid-1": active_status(stream_id=11)},
        ],
        channels=[{"id": 1, "uuid": "uuid-1", "streams": [10, 11]}],
    )
    service = make_service(tmp_path, udi=udi)
    service._verify_proxy_output_after_switch = lambda *args, **kwargs: {
        "post_switch_proxy_probe_accepted": False,
        "accepted": False,
    }
    target = {
        "channel_uuid": "uuid-1",
        "channel_id": 1,
        "channel_ref": "channel-test",
        "viewer_output_format": "fmp4",
    }

    success, observed_stream_id, details = service._verify_active_stream_after_switch(
        udi,
        target,
        11,
        normalize_config({}),
    )

    assert success is True
    assert observed_stream_id == 11
    assert details["post_switch_verification_mode"] == "status_stream_id"


def test_default_switch_accepts_proxy_probe_when_status_lacks_stream_id(tmp_path, monkeypatch):
    switch_calls = []
    probe_urls = []
    probe_results = iter([
        {"blank_detected": True},
        {"blank_detected": False, "freeze_detected": False},
    ])
    current_time = {"value": 100.0}

    def fake_monotonic():
        current_time["value"] += 1.0
        return current_time["value"]

    def blank_probe(url, config):
        probe_urls.append(url)
        return next(probe_results)

    monkeypatch.setattr(shadow_module.time, "monotonic", fake_monotonic)
    monkeypatch.setattr(shadow_module.time, "sleep", lambda _seconds: None)

    udi = FakeUdi(
        statuses=[{"uuid-1": {"state": "active", "channel_id": "uuid-1", "clients": [{"user_agent": "VLC"}]}}],
        channels=[{"id": 1, "uuid": "uuid-1", "streams": [10, 11]}],
    )

    def switch_stream(channel_id, stream_id=None, url=None):
        switch_calls.append((channel_id, stream_id, url))
        return True

    service = make_service(
        tmp_path,
        udi=udi,
        blank_probe=blank_probe,
        switch_calls=switch_calls,
    )
    service.switch_stream = switch_stream
    service._uses_default_switch_stream = True
    config = normalize_config({
        "enabled": False,
        "dry_run": False,
        "confirmation_count": 1,
    })
    target = {
        "channel_uuid": "uuid-1",
        "channel_id": 1,
        "channel_ref": "channel-test",
        "stream_id": 10,
        "stream_ref": "stream-test",
        "real_client_count": 1,
        "viewer_output_format": "fmp4",
    }

    should_continue = service._probe_target_once(udi, target, config)
    status = service.get_status()
    event = status["recent_events"][0]

    assert should_continue is False
    assert switch_calls == [("uuid-1", 11, None)]
    assert probe_urls[-1] == "http://dispatcharr.local/proxy/ts/stream/uuid-1?output_format=fmp4"
    assert event["type"] == "switch_success"
    assert event["details"]["switch_api_success"] is True
    assert event["details"]["post_switch_verification"] is True
    assert event["details"]["post_switch_verification_mode"] == "proxy_probe"
    assert event["details"]["post_switch_proxy_probe_accepted"] is True
    assert "observed_stream_ref" not in event["details"]
    assert len(status["cooldowns"]) == 1
    assert status["cooldowns"][0]["cooldown_seconds"] == 300


def test_default_switch_accepts_proxy_probe_when_status_reports_stale_stream(tmp_path, monkeypatch):
    switch_calls = []
    probe_urls = []
    probe_results = iter([
        {"blank_detected": True},
        {"blank_detected": False, "freeze_detected": False},
    ])
    current_time = {"value": 100.0}

    def fake_monotonic():
        current_time["value"] += 1.0
        return current_time["value"]

    def blank_probe(url, config):
        probe_urls.append(url)
        return next(probe_results)

    monkeypatch.setattr(shadow_module.time, "monotonic", fake_monotonic)
    monkeypatch.setattr(shadow_module.time, "sleep", lambda _seconds: None)

    udi = FakeUdi(
        statuses=[{"uuid-1": active_status(stream_id=10)}],
        channels=[{"id": 1, "uuid": "uuid-1", "streams": [10, 11]}],
    )

    def switch_stream(channel_id, stream_id=None, url=None):
        switch_calls.append((channel_id, stream_id, url))
        return True

    service = make_service(
        tmp_path,
        udi=udi,
        blank_probe=blank_probe,
        switch_calls=switch_calls,
    )
    service.switch_stream = switch_stream
    service._uses_default_switch_stream = True
    config = normalize_config({
        "enabled": False,
        "dry_run": False,
        "confirmation_count": 1,
    })
    target = {
        "channel_uuid": "uuid-1",
        "channel_id": 1,
        "channel_ref": "channel-test",
        "stream_id": 10,
        "stream_ref": "stream-test",
        "real_client_count": 1,
        "viewer_output_format": "fmp4",
    }

    should_continue = service._probe_target_once(udi, target, config)
    status = service.get_status()
    event = status["recent_events"][0]

    assert should_continue is False
    assert switch_calls == [("uuid-1", 11, None)]
    assert probe_urls == [
        "http://dispatcharr.local/proxy/ts/stream/uuid-1?output_format=fmp4",
        "http://dispatcharr.local/proxy/ts/stream/uuid-1?output_format=fmp4",
    ]
    assert event["type"] == "switch_success"
    assert event["details"]["switch_api_success"] is True
    assert event["details"]["post_switch_verification"] is True
    assert event["details"]["post_switch_verification_mode"] == "status_stream_id+proxy_probe"
    assert event["details"]["post_switch_status_mismatch"] is True
    assert event["details"]["post_switch_proxy_probe_accepted"] is True
    assert event["details"]["observed_stream_ref"].startswith("stream-")
    assert event["details"]["expected_stream_ref"].startswith("stream-")
    assert len(status["cooldowns"]) == 1
    assert status["cooldowns"][0]["cooldown_seconds"] == 300


def test_default_switch_rejects_proxy_probe_when_output_still_bad(tmp_path, monkeypatch):
    switch_calls = []
    probe_urls = []
    current_time = {"value": 100.0}

    def fake_monotonic():
        current_time["value"] += 1.0
        return current_time["value"]

    def blank_probe(url, config):
        probe_urls.append(url)
        return {"blank_detected": True}

    monkeypatch.setattr(shadow_module.time, "monotonic", fake_monotonic)
    monkeypatch.setattr(shadow_module.time, "sleep", lambda _seconds: None)

    udi = FakeUdi(
        statuses=[{"uuid-1": active_status(stream_id=10)}],
        channels=[{"id": 1, "uuid": "uuid-1", "streams": [10, 11]}],
    )

    def switch_stream(channel_id, stream_id=None, url=None):
        switch_calls.append((channel_id, stream_id, url))
        return True

    service = make_service(
        tmp_path,
        udi=udi,
        blank_probe=blank_probe,
        switch_calls=switch_calls,
    )
    service.switch_stream = switch_stream
    service._uses_default_switch_stream = True
    config = normalize_config({
        "enabled": False,
        "dry_run": False,
        "confirmation_count": 1,
    })
    target = {
        "channel_uuid": "uuid-1",
        "channel_id": 1,
        "channel_ref": "channel-test",
        "stream_id": 10,
        "stream_ref": "stream-test",
        "real_client_count": 1,
        "viewer_output_format": "fmp4",
    }

    should_continue = service._probe_target_once(udi, target, config)
    status = service.get_status()
    event = status["recent_events"][0]

    assert should_continue is False
    assert switch_calls == [("uuid-1", 11, None)]
    assert len(probe_urls) > 2
    assert set(probe_urls) == {
        "http://dispatcharr.local/proxy/ts/stream/uuid-1?output_format=fmp4",
    }
    assert event["type"] == "switch_failed"
    assert event["details"]["switch_api_success"] is True
    assert event["details"]["post_switch_verification"] is False
    assert event["details"]["post_switch_verification_mode"] == "proxy_probe"
    assert event["details"]["post_switch_proxy_probe_accepted"] is False
    assert status["cooldowns"]


def test_switch_falls_back_to_channel_uuid_without_numeric_id(tmp_path):
    switch_calls = []
    udi = FakeUdi(
        statuses=[{"uuid-1": active_status(stream_id=10)}],
        channels=[{"uuid": "uuid-1", "streams": [10, 11]}],
    )
    service = make_service(
        tmp_path,
        udi=udi,
        blank_probe=lambda url, config: {"blank_detected": True},
        switch_calls=switch_calls,
    )
    service._ordered_alternative_streams = lambda *args, **kwargs: [11]
    config = normalize_config({
        "enabled": False,
        "dry_run": False,
        "confirmation_count": 1,
        "next_stream_pre_probe_enabled": False,
    })
    target = {
        "channel_uuid": "uuid-1",
        "channel_id": None,
        "channel_ref": "channel-test",
        "stream_id": 10,
        "stream_ref": "stream-test",
        "real_client_count": 1,
    }

    should_continue = service._probe_target_once(udi, target, config)

    assert should_continue is False
    assert switch_calls == [("uuid-1", 11, None)]


def test_switch_resolves_missing_numeric_channel_id_by_uuid(tmp_path):
    switch_calls = []
    udi = FakeUdi(
        statuses=[{"uuid-1": active_status(stream_id=10)}],
        channels=[{"id": 1, "uuid": "uuid-1", "streams": [10, 11]}],
    )
    service = make_service(
        tmp_path,
        udi=udi,
        blank_probe=lambda url, config: {"blank_detected": True},
        switch_calls=switch_calls,
    )
    config = normalize_config({
        "enabled": False,
        "dry_run": False,
        "confirmation_count": 1,
        "next_stream_pre_probe_enabled": False,
    })
    target = {
        "channel_uuid": "uuid-1",
        "channel_id": None,
        "channel_ref": "channel-test",
        "stream_id": 10,
        "stream_ref": "stream-test",
        "real_client_count": 1,
    }

    should_continue = service._probe_target_once(udi, target, config)

    assert should_continue is False
    assert switch_calls == [("uuid-1", 11, None)]
    assert target["channel_id"] == 1
    assert target["channel_ref"].startswith("channel-")
    assert target["channel_ref"] != "channel-test"


def test_preprobed_switch_resolves_missing_numeric_channel_id_by_uuid(tmp_path):
    switch_calls = []
    udi = FakeUdi(
        statuses=[{"uuid-1": active_status(stream_id=10)}],
        channels=[{"id": 1, "uuid": "uuid-1", "streams": [10, 11]}],
        streams={11: {"id": 11, "url": "http://example.test/good.m3u8"}},
    )
    service = make_service(
        tmp_path,
        udi=udi,
        blank_probe=lambda url, config: {"blank_detected": True},
        switch_calls=switch_calls,
    )
    service._run_blank_probe = lambda url, config: {"blank_detected": False}
    config = normalize_config({
        "enabled": False,
        "dry_run": False,
        "confirmation_count": 1,
        "next_stream_pre_probe_enabled": True,
        "next_stream_pre_probe_duration_seconds": 1,
    })
    target = {
        "channel_uuid": "uuid-1",
        "channel_id": None,
        "channel_ref": "channel-test",
        "stream_id": 10,
        "stream_ref": "stream-test",
        "real_client_count": 1,
    }

    should_continue = service._probe_target_once(udi, target, config)

    assert should_continue is False
    assert switch_calls == [("uuid-1", 11, None)]
    assert target["channel_id"] == 1
    assert target["channel_ref"].startswith("channel-")
    assert target["channel_ref"] != "channel-test"


def test_switch_prefers_channel_uuid_over_numeric_id(tmp_path):
    switch_calls = []
    udi = FakeUdi(
        statuses=[{"uuid-1": active_status(stream_id=10)}],
        channels=[{"id": 1, "uuid": "uuid-1", "streams": [10, 11]}],
    )
    service = make_service(
        tmp_path,
        udi=udi,
        blank_probe=lambda url, config: {"blank_detected": True},
        switch_calls=switch_calls,
    )
    service._ordered_alternative_streams = lambda *args, **kwargs: [11]
    config = normalize_config({
        "enabled": False,
        "dry_run": False,
        "confirmation_count": 1,
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

    should_continue = service._probe_target_once(udi, target, config)

    assert should_continue is False
    assert switch_calls == [("uuid-1", 11, None)]


def test_successful_proxy_probe_clears_attempted_switch_targets(tmp_path):
    now = {"value": 1_000.0}
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
        clock=lambda: now["value"],
    )
    service.update_config({
        "enabled": False,
        "dry_run": False,
        "confirmation_count": 1,
        "channel_cooldown_seconds": 30,
    })

    service.run_once(force=True)
    service.run_once(force=True)
    now["value"] += 31
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
    config = normalize_config({
        "watch_mode": "continuous",
        "persistent_watcher_enabled": True,
        "watcher_api_key": "test-watcher-key",
    })

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


def test_low_impact_shadow_probe_gap_is_waiting_not_reconnecting(tmp_path):
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
                            "client_id": "raw-probe-client",
                            "connected_at": 1000.0,
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
        ],
        channels=[{"id": 1, "uuid": "uuid-1", "streams": [10, 11]}],
    )
    service = make_service(tmp_path, udi=udi, clock=lambda: now["value"])
    config = normalize_config({"watch_mode": "continuous"})

    service.discover_active_targets(udi, config)

    now["value"] = 1005.0
    service.discover_active_targets(udi, config)
    status = service.get_status()
    watched = status["watched_channels"][0]

    assert watched["watcher_state"] == "waiting"
    assert watched["watcher_client_count"] == 0
    assert "watcher_absent_seconds" not in watched
    assert "watcher_reconnecting" not in [event["type"] for event in status["recent_events"]]
    assert "raw-probe-client" not in repr(status)


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
    config = normalize_config({
        "watch_mode": "continuous",
        "persistent_watcher_enabled": True,
        "watcher_api_key": "test-watcher-key",
    })

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


def test_active_probe_watcher_ref_change_does_not_reset_detection(tmp_path):
    udi = FakeUdi(
        statuses=[
            {
                "uuid-1": active_status(
                    stream_id=10,
                    clients=[
                        {"user_agent": "VLC"},
                        {
                            "user_agent": "StreamFlow-Shadow-Blank-Monitor/1.0",
                            "client_id": "raw-new-watcher",
                            "connected_at": 1004.0,
                        },
                    ],
                )
            },
        ],
        channels=[{"id": 1, "uuid": "uuid-1", "streams": [10, 11]}],
    )
    service = make_service(tmp_path, udi=udi, clock=lambda: 1005.0)
    with service._lock:
        service._active_probes.add("uuid-1")
        service._watched["uuid-1"] = {
            "channel_uuid": "uuid-1",
            "channel_id": 1,
            "channel_ref": "channel-test",
            "stream_id": 10,
            "stream_ref": "stream-test",
            "real_client_count": 1,
            "watcher_client_count": 1,
            "watcher_client_ref": "client-old",
            "active_probe_started_at": 1000.0,
        }
    config = normalize_config({"watch_mode": "continuous"})

    service.discover_active_targets(udi, config)
    status = service.get_status()
    watched = status["watched_channels"][0]

    assert watched["watcher_state"] == "watching"
    assert watched["watcher_client_count"] == 1
    with service._lock:
        assert service._watched["uuid-1"]["active_probe_started_at"] == 1000.0
    assert "watcher_recovered" not in [event["type"] for event in status["recent_events"]]


def test_background_continuous_mode_keeps_persistent_watcher_between_scans(tmp_path, monkeypatch):
    now = {"value": 1000.0}
    started = []

    def fake_popen(command, **_kwargs):
        process = FakePersistentWatcherProcess()
        started.append((command, process))
        return process

    monkeypatch.setattr(shadow_module.subprocess, "Popen", fake_popen)
    udi = FakeUdi(
        statuses=[
            {"uuid-1": active_status(stream_id=10, clients=[{"user_agent": "VLC"}])},
        ],
        channels=[{"id": 1, "uuid": "uuid-1", "streams": [10, 11]}],
    )
    service = make_service(tmp_path, udi=udi, clock=lambda: now["value"])
    service._uses_default_blank_probe = True
    config = normalize_config({
        "watch_mode": "continuous",
        "watcher_api_key": "watcher-key",
        "persistent_watcher_enabled": True,
    })

    targets = service.discover_active_targets(udi, config)
    service._sync_persistent_watchers(targets, config)
    first_status = service.get_status()["watched_channels"][0]

    now["value"] = 1015.0
    service._sync_persistent_watchers(targets, config)
    second_status = service.get_status()["watched_channels"][0]

    assert len(started) == 1
    assert "Authorization: ApiKey watcher-key" in " ".join(started[0][0])
    assert first_status["persistent_watcher_state"] == "running"
    assert second_status["persistent_watcher_pid"] == 4242
    assert second_status["persistent_watcher_uptime_seconds"] == 15
    assert started[0][1].poll() is None


def test_continuous_mode_does_not_start_persistent_watcher_by_default(tmp_path, monkeypatch):
    started = []

    def fake_popen(command, **_kwargs):
        process = FakePersistentWatcherProcess()
        started.append((command, process))
        return process

    monkeypatch.setattr(shadow_module.subprocess, "Popen", fake_popen)
    udi = FakeUdi(
        statuses=[
            {"uuid-1": active_status(stream_id=10, clients=[{"user_agent": "VLC"}])},
        ],
        channels=[{"id": 1, "uuid": "uuid-1", "streams": [10, 11]}],
    )
    service = make_service(tmp_path, udi=udi)
    service._uses_default_blank_probe = True
    config = normalize_config({
        "watch_mode": "continuous",
        "watcher_api_key": "watcher-key",
    })

    targets = service.discover_active_targets(udi, config)
    service._sync_persistent_watchers(targets, config)

    assert config["persistent_watcher_enabled"] is False
    assert started == []
    assert service._persistent_watchers == {}


def test_continuous_probe_delay_uses_low_impact_interval_until_fault_pending(tmp_path):
    service = make_service(tmp_path, udi=FakeUdi(statuses=[], channels=[]))
    config = normalize_config({
        "watch_mode": "continuous",
        "continuous_probe_interval_seconds": 45,
        "watch_gap_seconds": 2,
    })

    assert service._continuous_probe_delay_seconds("uuid-1", config) == 45

    service._increment_blank_count("uuid-1", "blank")

    assert service._continuous_probe_delay_seconds("uuid-1", config) == 2


def test_persistent_watcher_does_not_block_followup_media_probe(tmp_path):
    probe_urls = []
    watcher_client = {"user_agent": "StreamFlow-Shadow-Blank-Monitor/1.0"}
    udi = FakeUdi(
        statuses=[{
            "uuid-1": active_status(
                stream_id=10,
                clients=[{"user_agent": "VLC"}, watcher_client],
            ),
        }],
        channels=[{"id": 1, "uuid": "uuid-1", "streams": [10, 11]}],
        streams={10: {"id": 10, "url": "http://provider.example/old.ts"}},
    )
    service = make_service(
        tmp_path,
        udi=udi,
        blank_probe=lambda url, config: probe_urls.append(url) or {"blank_detected": False},
    )
    target = {
        "channel_uuid": "uuid-1",
        "channel_id": 1,
        "channel_ref": "channel-1",
        "stream_id": 10,
        "stream_ref": "stream-10",
        "real_client_count": 1,
        "watcher_client_count": 1,
        "watcher_state": "watching",
        "persistent_watcher_state": "running",
    }
    config = normalize_config({"watch_mode": "continuous"})

    service._probe_targets(udi, [target], config, single_pass=True)

    assert probe_urls == ["http://dispatcharr.local/proxy/ts/stream/uuid-1"]
    assert "watcher_orphaned" not in [event["type"] for event in service.get_status()["recent_events"]]


def test_persistent_watcher_restarts_on_stream_change(tmp_path, monkeypatch):
    started = []

    def fake_popen(command, **_kwargs):
        process = FakePersistentWatcherProcess()
        started.append((command, process))
        return process

    monkeypatch.setattr(shadow_module.subprocess, "Popen", fake_popen)
    udi = FakeUdi(
        statuses=[
            {"uuid-1": active_status(stream_id=10, clients=[{"user_agent": "VLC"}])},
            {"uuid-1": active_status(stream_id=11, clients=[{"user_agent": "VLC"}])},
        ],
        channels=[{"id": 1, "uuid": "uuid-1", "streams": [10, 11]}],
    )
    service = make_service(tmp_path, udi=udi)
    service._uses_default_blank_probe = True
    config = normalize_config({
        "watch_mode": "continuous",
        "watcher_api_key": "watcher-key",
        "persistent_watcher_enabled": True,
    })

    targets = service.discover_active_targets(udi, config)
    service._sync_persistent_watchers(targets, config)
    first_process = started[0][1]
    targets = service.discover_active_targets(udi, config)
    service._sync_persistent_watchers(targets, config)

    assert len(started) == 2
    assert first_process.terminated is True
    assert started[1][1].poll() is None


def test_viewer_left_stops_persistent_watcher(tmp_path, monkeypatch):
    started = []

    def fake_popen(command, **_kwargs):
        process = FakePersistentWatcherProcess()
        started.append(process)
        return process

    monkeypatch.setattr(shadow_module.subprocess, "Popen", fake_popen)
    udi = FakeUdi(
        statuses=[
            {"uuid-1": active_status(stream_id=10, clients=[{"user_agent": "VLC"}])},
        ],
        channels=[{"id": 1, "uuid": "uuid-1", "streams": [10, 11]}],
    )
    service = make_service(tmp_path, udi=udi)
    service._uses_default_blank_probe = True
    config = normalize_config({
        "watch_mode": "continuous",
        "watcher_api_key": "watcher-key",
        "persistent_watcher_enabled": True,
    })

    targets = service.discover_active_targets(udi, config)
    service._sync_persistent_watchers(targets, config)
    service._record_event("viewer_left", targets[0], {})

    assert len(started) == 1
    assert started[0].terminated is True
    assert service._persistent_watchers == {}


def test_viewer_left_grace_keeps_persistent_watcher_for_brief_client_drop(tmp_path, monkeypatch):
    now = {"value": 1000.0}
    started = []
    probe_urls = []
    watcher_client = {"user_agent": "StreamFlow-Shadow-Blank-Monitor/1.0"}

    def fake_popen(command, **_kwargs):
        process = FakePersistentWatcherProcess()
        started.append(process)
        return process

    monkeypatch.setattr(shadow_module.subprocess, "Popen", fake_popen)
    udi = FakeUdi(
        statuses=[
            {"uuid-1": active_status(stream_id=10, clients=[{"user_agent": "VLC"}])},
            {"uuid-1": active_status(stream_id=10, clients=[watcher_client])},
            {"uuid-1": active_status(stream_id=10, clients=[{"user_agent": "VLC"}, watcher_client])},
        ],
        channels=[{"id": 1, "uuid": "uuid-1", "streams": [10, 11]}],
        streams={10: {"id": 10, "url": "http://provider.example/old.ts"}},
    )
    service = make_service(
        tmp_path,
        udi=udi,
        clock=lambda: now["value"],
        blank_probe=lambda url, config: probe_urls.append(url) or {"blank_detected": False},
    )
    service._uses_default_blank_probe = True
    config = normalize_config({
        "watch_mode": "continuous",
        "watcher_api_key": "watcher-key",
        "persistent_watcher_enabled": True,
        "viewer_left_grace_seconds": 5,
    })

    targets = service.discover_active_targets(udi, config)
    service._sync_persistent_watchers(targets, config)

    now["value"] = 1005.0
    grace_targets = service.discover_active_targets(udi, config)
    service._sync_persistent_watchers(grace_targets, config)
    service._probe_targets(udi, grace_targets, config, single_pass=True)

    now["value"] = 1008.0
    recovered_targets = service.discover_active_targets(udi, config)
    service._sync_persistent_watchers(recovered_targets, config)

    assert len(started) == 1
    assert started[0].terminated is False
    assert grace_targets[0]["viewer_left_grace_active"] is True
    assert grace_targets[0]["viewer_left_grace_remaining_seconds"] == 5
    assert recovered_targets[0]["real_client_count"] == 1
    assert probe_urls == []
    assert "viewer_left" not in [event["type"] for event in service.get_status()["recent_events"]]


def test_viewer_left_grace_expires_and_stops_persistent_watcher(tmp_path, monkeypatch):
    now = {"value": 1000.0}
    started = []
    watcher_client = {"user_agent": "StreamFlow-Shadow-Blank-Monitor/1.0"}

    def fake_popen(command, **_kwargs):
        process = FakePersistentWatcherProcess()
        started.append(process)
        return process

    monkeypatch.setattr(shadow_module.subprocess, "Popen", fake_popen)
    udi = FakeUdi(
        statuses=[
            {"uuid-1": active_status(stream_id=10, clients=[{"user_agent": "VLC"}])},
            {"uuid-1": active_status(stream_id=10, clients=[watcher_client])},
            {"uuid-1": active_status(stream_id=10, clients=[watcher_client])},
        ],
        channels=[{"id": 1, "uuid": "uuid-1", "streams": [10, 11]}],
    )
    service = make_service(tmp_path, udi=udi, clock=lambda: now["value"])
    service._uses_default_blank_probe = True
    config = normalize_config({
        "watch_mode": "continuous",
        "watcher_api_key": "watcher-key",
        "persistent_watcher_enabled": True,
        "viewer_left_grace_seconds": 5,
    })

    targets = service.discover_active_targets(udi, config)
    service._sync_persistent_watchers(targets, config)

    now["value"] = 1005.0
    grace_targets = service.discover_active_targets(udi, config)
    service._sync_persistent_watchers(grace_targets, config)

    now["value"] = 1011.0
    expired_targets = service.discover_active_targets(udi, config)
    service._sync_persistent_watchers(expired_targets, config)

    assert len(started) == 1
    assert grace_targets[0]["viewer_left_grace_active"] is True
    assert expired_targets == []
    assert started[0].terminated is True
    assert service._persistent_watchers == {}
    assert service.get_status()["recent_events"][0]["type"] == "viewer_left"
    assert service.get_status()["recent_events"][0]["details"]["reason"] == "viewer_left_grace_expired"


def test_proxy_status_gap_keeps_persistent_watcher_during_viewer_grace(tmp_path, monkeypatch):
    now = {"value": 1000.0}
    started = []

    def fake_popen(command, **_kwargs):
        process = FakePersistentWatcherProcess()
        started.append(process)
        return process

    monkeypatch.setattr(shadow_module.subprocess, "Popen", fake_popen)
    udi = FakeUdi(
        statuses=[
            {"uuid-1": active_status(stream_id=10, clients=[{"user_agent": "VLC"}])},
            {},
            {"uuid-1": active_status(stream_id=10, clients=[{"user_agent": "VLC"}])},
        ],
        channels=[{"id": 1, "uuid": "uuid-1", "streams": [10, 11]}],
    )
    service = make_service(tmp_path, udi=udi, clock=lambda: now["value"])
    service._uses_default_blank_probe = True
    config = normalize_config({
        "watch_mode": "continuous",
        "watcher_api_key": "watcher-key",
        "persistent_watcher_enabled": True,
        "viewer_left_grace_seconds": 5,
    })

    targets = service.discover_active_targets(udi, config)
    service._sync_persistent_watchers(targets, config)

    now["value"] = 1005.0
    gap_targets = service.discover_active_targets(udi, config)
    service._sync_persistent_watchers(gap_targets, config)

    now["value"] = 1008.0
    recovered_targets = service.discover_active_targets(udi, config)
    service._sync_persistent_watchers(recovered_targets, config)

    assert len(started) == 1
    assert started[0].terminated is False
    assert gap_targets[0]["viewer_left_grace_active"] is True
    assert gap_targets[0]["proxy_status_gap"] is True
    assert gap_targets[0]["watcher_state"] == "reconnecting"
    assert recovered_targets[0]["real_client_count"] == 1
    assert "viewer_left" not in [event["type"] for event in service.get_status()["recent_events"]]


def test_proxy_status_gap_expires_and_stops_persistent_watcher(tmp_path, monkeypatch):
    now = {"value": 1000.0}
    started = []

    def fake_popen(command, **_kwargs):
        process = FakePersistentWatcherProcess()
        started.append(process)
        return process

    monkeypatch.setattr(shadow_module.subprocess, "Popen", fake_popen)
    udi = FakeUdi(
        statuses=[
            {"uuid-1": active_status(stream_id=10, clients=[{"user_agent": "VLC"}])},
            {},
            {},
        ],
        channels=[{"id": 1, "uuid": "uuid-1", "streams": [10, 11]}],
    )
    service = make_service(tmp_path, udi=udi, clock=lambda: now["value"])
    service._uses_default_blank_probe = True
    config = normalize_config({
        "watch_mode": "continuous",
        "watcher_api_key": "watcher-key",
        "persistent_watcher_enabled": True,
        "viewer_left_grace_seconds": 5,
    })

    targets = service.discover_active_targets(udi, config)
    service._sync_persistent_watchers(targets, config)

    now["value"] = 1005.0
    gap_targets = service.discover_active_targets(udi, config)
    service._sync_persistent_watchers(gap_targets, config)

    now["value"] = 1011.0
    expired_targets = service.discover_active_targets(udi, config)
    service._sync_persistent_watchers(expired_targets, config)

    assert len(started) == 1
    assert gap_targets[0]["viewer_left_grace_active"] is True
    assert expired_targets == []
    assert started[0].terminated is True
    assert service._persistent_watchers == {}
    assert service.get_status()["recent_events"][0]["type"] == "viewer_left"
    assert service.get_status()["recent_events"][0]["details"]["reason"] == "proxy_status_gap_grace_expired"


def test_viewer_left_final_event_is_recorded_once_until_viewer_returns(tmp_path):
    now = {"value": 1000.0}
    udi = FakeUdi(
        statuses=[{"uuid-1": active_status(stream_id=10, clients=[{"user_agent": "VLC"}])}],
        channels=[{"id": 1, "uuid": "uuid-1", "streams": [10, 11]}],
    )
    service = make_service(tmp_path, udi=udi, clock=lambda: now["value"])
    config = normalize_config({
        "watch_mode": "continuous",
        "viewer_left_grace_seconds": 0,
    })

    targets = service.discover_active_targets(udi, config)
    service._handle_viewer_left_or_grace("uuid-1", targets[0], config, {})

    with service._lock:
        service._watched["uuid-1"] = dict(targets[0])
        service._viewer_absences["uuid-1"] = {"since": now["value"], "last_real_client_count": 1}

    now["value"] = 1001.0
    udi.statuses = [{}]
    udi.status_calls = 0
    assert service.discover_active_targets(udi, config) == []

    events = [event for event in service.get_status()["recent_events"] if event["type"] == "viewer_left"]
    assert len(events) == 1

    now["value"] = 1002.0
    udi.statuses = [{"uuid-1": active_status(stream_id=10, clients=[{"user_agent": "VLC"}])}]
    udi.status_calls = 0
    returned_targets = service.discover_active_targets(udi, config)
    service._handle_viewer_left_or_grace("uuid-1", returned_targets[0], config, {})

    events = [event for event in service.get_status()["recent_events"] if event["type"] == "viewer_left"]
    assert len(events) == 2


def test_probe_viewer_left_uses_grace_before_stopping_watcher(tmp_path):
    udi = FakeUdi(
        statuses=[{"uuid-1": active_status(stream_id=10, clients=[{"user_agent": "VLC"}])}],
        channels=[{"id": 1, "uuid": "uuid-1", "streams": [10, 11]}],
        streams={10: {"id": 10, "url": "http://provider.example/old.ts"}},
    )
    service = make_service(
        tmp_path,
        udi=udi,
        blank_probe=lambda url, config: {"viewer_left": True, "blank_detected": False},
    )
    config = normalize_config({
        "watch_mode": "continuous",
        "watcher_api_key": "watcher-key",
        "viewer_left_grace_seconds": 5,
    })
    config["_shadow_allow_viewer_left_grace"] = True
    target = {
        "channel_uuid": "uuid-1",
        "channel_id": 1,
        "channel_ref": "channel-1",
        "stream_id": 10,
        "stream_ref": "stream-10",
        "real_client_count": 1,
        "watcher_client_count": 1,
        "watcher_state": "watching",
    }
    with service._lock:
        service._watched["uuid-1"] = dict(target)

    should_continue = service._probe_target_once(udi, target, config)
    status = service.get_status()

    assert should_continue is False
    assert status["watched_channels"][0]["viewer_left_grace_active"] is True
    assert status["watched_channels"][0]["real_client_count"] == 0
    assert "viewer_left" not in [event["type"] for event in status["recent_events"]]


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
        "persistent_watcher_enabled": True,
        "channel_cooldown_seconds": 300,
    })

    service.run_once(force=True)
    now["value"] = 1005.0
    service.run_once(force=True)
    assert len(probe_calls) == 1

    now["value"] = 1010.0
    recovery_status = service.run_once(force=True)
    assert recovery_status["recent_events"][0]["type"] == "watcher_recovered"
    assert recovery_status["cooldowns"] == []

    now["value"] = 1015.0
    cooldown_status = service.run_once(force=True)
    assert len(probe_calls) == 2
    assert cooldown_status["recent_events"][0]["type"] == "probe_ok"
    assert cooldown_status["recent_events"][1]["type"] == "watcher_reconnecting"
    assert cooldown_status["cooldowns"] == []


def test_reconnect_probe_can_switch_after_watcher_recovery_without_channel_cooldown(tmp_path):
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
        "persistent_watcher_enabled": True,
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
    assert recovery_status["cooldowns"] == []

    now["value"] = 1015.0
    switch_status = service.run_once(force=True)
    assert len(probe_calls) == 2
    assert switch_calls == [("uuid-1", 11, None)]
    assert switch_status["recent_events"][0]["type"] == "switch_success"
    assert switch_status["recent_events"][0]["details"]["reason"] == "blank"
    assert len(switch_status["cooldowns"]) == 1
    assert switch_status["cooldowns"][0]["cooldown_seconds"] == 300


def test_watcher_recovery_does_not_start_channel_cooldown(tmp_path):
    now = {"value": 1000.0}
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
            {"uuid-1": active_status(stream_id=10, clients=[real_viewer, watcher_new])},
        ],
        channels=[{"id": 1, "uuid": "uuid-1", "streams": [10, 11]}],
    )
    service = make_service(
        tmp_path,
        udi=udi,
        clock=lambda: now["value"],
    )
    service.update_config({
        "enabled": False,
        "watch_mode": "continuous",
        "persistent_watcher_enabled": True,
        "channel_cooldown_seconds": 300,
    })

    service.run_once(force=True)
    now["value"] = 1005.0
    service.run_once(force=True)

    now["value"] = 1010.0
    recovery_status = service.run_once(force=True)

    assert recovery_status["recent_events"][0]["type"] == "watcher_recovered"
    assert recovery_status["cooldowns"] == []


def test_existing_channel_cooldown_blocks_switch_but_records_fault_reason(tmp_path):
    switch_calls = []
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
        blank_probe=lambda url, _config: {
            "blank_detected": True,
            "blank_ratio": 1.0,
            "blank_duration_secs": 60,
        },
        switch_calls=switch_calls,
    )
    service.update_config({
        "enabled": False,
        "watch_mode": "continuous",
        "channel_cooldown_seconds": 300,
        "confirmation_count": 1,
    })
    config = service.get_config(include_secret=True)
    service._set_cooldown("uuid-1", config)

    target = {
        "channel_uuid": "uuid-1",
        "channel_id": 1,
        "channel_ref": "channel-1",
        "channel_name": "Channel 1",
        "stream_id": 10,
        "stream_ref": "stream-10",
        "real_client_count": 1,
        "watcher_client_count": 1,
        "watcher_state": "watching",
    }
    should_continue = service._probe_target_once(udi, target, service.get_config(include_secret=True))
    status = service.get_status()

    assert should_continue is True
    assert switch_calls == []
    assert status["recent_events"][0]["type"] == "cooldown"
    assert status["recent_events"][0]["details"]["reason"] == "blank"
    assert status["recent_events"][0]["details"]["cooldown_seconds"] == 300


def test_external_stream_change_is_recorded_and_clears_cooldown(tmp_path):
    real_viewer = {"user_agent": "VLC"}
    udi = FakeUdi(
        statuses=[
            {"uuid-1": active_status(stream_id=10, clients=[real_viewer])},
            {"uuid-1": active_status(stream_id=12, clients=[real_viewer])},
        ],
        channels=[{"id": 1, "uuid": "uuid-1", "streams": [10, 11, 12]}],
    )
    service = make_service(tmp_path, udi=udi)
    service.update_config({
        "enabled": False,
        "watch_mode": "continuous",
        "channel_cooldown_seconds": 300,
    })
    service._set_cooldown("uuid-1", service.get_config(include_secret=True))

    service.run_once(force=True)
    status = service.run_once(force=True)

    event = next(event for event in status["recent_events"] if event["type"] == "external_stream_change")
    assert event["type"] == "external_stream_change"
    assert event["details"]["origin_stream_ref"] == shadow_module._ref("stream", 10)
    assert event["details"]["target_stream_ref"] == shadow_module._ref("stream", 12)
    assert event["details"]["switch_source"] == "external"
    assert status["cooldowns"] == []


def test_probe_discards_fault_when_stream_changes_during_probe(tmp_path):
    switch_calls = []
    real_viewer = {"user_agent": "VLC"}
    udi = FakeUdi(
        statuses=[{"uuid-1": active_status(stream_id=12, clients=[real_viewer])}],
        channels=[{"id": 1, "uuid": "uuid-1", "streams": [10, 11, 12]}],
    )
    service = make_service(
        tmp_path,
        udi=udi,
        blank_probe=lambda url, _config: {
            "freeze_detected": True,
            "freeze_ratio": 1.0,
            "freeze_duration_secs": 12.0,
            "audio_stream_present": False,
        },
        switch_calls=switch_calls,
    )
    config = normalize_config({
        "watch_mode": "continuous",
        "confirmation_count": 1,
        "channel_cooldown_seconds": 300,
    })
    target = {
        "channel_uuid": "uuid-1",
        "channel_id": 1,
        "channel_ref": shadow_module._ref("channel", 1),
        "stream_id": 10,
        "stream_ref": shadow_module._ref("stream", 10),
        "real_client_count": 1,
        "watcher_client_count": 0,
        "watcher_state": "waiting",
    }
    with service._lock:
        service._watched["uuid-1"] = dict(target)
    service._set_cooldown("uuid-1", config)

    should_continue = service._probe_target_once(udi, target, config)
    status = service.get_status()

    assert should_continue is True
    assert switch_calls == []
    assert target["stream_id"] == 12
    assert target["stream_ref"] == shadow_module._ref("stream", 12)
    assert target["last_probe"]["discarded"] is True
    assert target["last_probe"]["discarded_reason"] == "stream_changed_during_probe"
    assert status["cooldowns"] == []
    event = next(event for event in status["recent_events"] if event["type"] == "external_stream_change")
    assert event["stream_ref"] == shadow_module._ref("stream", 10)
    assert event["details"]["target_stream_ref"] == shadow_module._ref("stream", 12)
    assert event["details"]["probe_result_discarded"] is True
    assert event["details"]["probe_discard_reason"] == "stream_changed_during_probe"


def test_expected_shadow_stream_status_changes_keep_success_cooldown(tmp_path):
    now = {"value": 1_000.0}
    real_viewer = {"user_agent": "VLC"}
    udi = FakeUdi(
        statuses=[
            {"uuid-1": active_status(stream_id=10, clients=[real_viewer])},
            {"uuid-1": active_status(stream_id=11, clients=[real_viewer])},
            {"uuid-1": active_status(stream_id=10, clients=[real_viewer])},
        ],
        channels=[{"id": 1, "uuid": "uuid-1", "streams": [10, 11]}],
    )
    service = make_service(tmp_path, udi=udi, clock=lambda: now["value"])
    service.update_config({
        "enabled": False,
        "watch_mode": "continuous",
        "channel_cooldown_seconds": 300,
    })
    config = service.get_config(include_secret=True)

    service.discover_active_targets(udi, config)
    service._record_switch_attempt("uuid-1", 10, 11)
    service._record_successful_switch("uuid-1", config)

    now["value"] += 1
    service.discover_active_targets(udi, config)
    assert service._cooldown_remaining("uuid-1") == 299

    now["value"] += 1
    status = service.discover_active_targets(udi, config)
    assert status[0]["stream_id"] == 10
    assert service._cooldown_remaining("uuid-1") == 298
    assert not any(
        event["type"] == "external_stream_change"
        for event in service.get_status()["recent_events"]
    )


def test_scoped_continuous_mode_reprobes_orphaned_watcher_and_uncovered_target(tmp_path):
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

    status = service.run_once(
        force=True,
        include_channel_uuids=["uuid-1", "uuid-2"],
    )

    assert probe_urls == [
        "http://dispatcharr.local/proxy/ts/stream/uuid-1",
        "http://dispatcharr.local/proxy/ts/stream/uuid-2",
    ]
    assert status["watched_count"] == 2
    assert "watcher_orphaned" in [event["type"] for event in status["recent_events"]]
    assert [event["type"] for event in status["recent_events"]].count("probe_ok") == 2


def test_orphaned_watcher_does_not_block_confirmed_blank_switch(tmp_path):
    switch_calls = []
    udi = FakeUdi(
        statuses=[{
            "uuid-1": active_status(
                stream_id=10,
                clients=[
                    {"user_agent": "VLC"},
                    {"user_agent": "StreamFlow-Shadow-Blank-Monitor/1.0"},
                ],
            ),
        }],
        channels=[{"id": 1, "uuid": "uuid-1", "streams": [10, 11]}],
    )
    service = make_service(
        tmp_path,
        udi=udi,
        blank_probe=lambda url, config: {
            "blank_detected": True,
            "blank_ratio": 1.0,
            "blank_duration_secs": 3.0,
        },
        switch_calls=switch_calls,
    )
    service.update_config({
        "enabled": False,
        "dry_run": False,
        "watch_mode": "continuous",
        "confirmation_count": 1,
    })

    status = service.run_once(force=True, include_channel_uuids=["uuid-1"])

    assert switch_calls == [("uuid-1", 11, None)]
    assert status["recent_events"][0]["type"] == "switch_success"
    assert "watcher_orphaned" in [event["type"] for event in status["recent_events"]]


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

    def fake_continuous_probe(url, config, probe_udi, target, *, continuous=True):
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

    final_status = service.get_status()
    assert final_status["watched_channels"][0]["viewer_left_grace_active"] is True
    assert final_status["watched_channels"][0]["real_client_count"] == 0
    assert final_status["recent_events"] == []


def test_viewer_left_cancels_healthy_probe_wait_and_releases_channel_slot(tmp_path, monkeypatch):
    probe_started = threading.Event()
    udi = FakeUdi(
        statuses=[{"uuid-1": active_status(stream_id=10, clients=[{"user_agent": "VLC"}])}],
        channels=[{"id": 1, "uuid": "uuid-1", "streams": [10, 11]}],
    )
    service = make_service(tmp_path, udi=udi)
    service._uses_default_blank_probe = True

    def clean_probe(_udi, _target, _config):
        probe_started.set()
        return True

    monkeypatch.setattr(service, "_probe_target_once", clean_probe)
    config = normalize_config({
        "watch_mode": "continuous",
        "continuous_probe_interval_seconds": 45,
        "watcher_api_key": "test-watcher-key",
    })
    target = {
        "channel_uuid": "uuid-1",
        "channel_id": 1,
        "channel_ref": "channel-1",
        "stream_id": 10,
        "stream_ref": "stream-10",
        "real_client_count": 1,
        "watcher_client_count": 0,
    }
    with service._lock:
        service._watched["uuid-1"] = dict(target)

    service._probe_targets(udi, [target], config)
    assert probe_started.wait(0.5)

    for _ in range(20):
        with service._lock:
            if service._watched["uuid-1"].get("probe_state") == "waiting":
                break
        time.sleep(0.01)

    started = time.monotonic()
    service._record_event("viewer_left", target, {})
    for _ in range(50):
        with service._lock:
            if "uuid-1" not in service._active_probes:
                break
        time.sleep(0.01)

    assert time.monotonic() - started < 0.75
    with service._lock:
        assert "uuid-1" not in service._active_probes
        assert "uuid-1" not in service._probe_cancel_events
        assert service._watched["uuid-1"]["probe_state"] == "idle"


def test_waiting_probe_thread_does_not_reclassify_new_real_viewer_as_shadow(tmp_path):
    channel = {"id": 1, "uuid": "uuid-1", "name": "Das Erste HD", "streams": [10, 11]}
    udi = FakeUdi(
        statuses=[{
            "uuid-1": active_status(
                stream_id=10,
                clients=[{"client_id": "new-real-viewer", "user_agent": "TiviMate"}],
            )
        }],
        channels=[channel],
    )
    service = make_service(tmp_path, udi=udi, clock=lambda: 1100.0)
    with service._lock:
        service._watched["uuid-1"] = {
            "channel_uuid": "uuid-1",
            "channel_id": 1,
            "channel_ref": "channel-1",
            "channel_name": "Das Erste HD",
            "stream_id": 10,
            "stream_ref": "stream-10",
            "real_client_count": 1,
            "real_client_refs": ["client_id:old-real-viewer"],
            "active_probe_started_at": 1000.0,
            "probe_state": "waiting",
        }
        service._active_probes.add("uuid-1")
    config = normalize_config({"watch_mode": "continuous", "viewer_left_grace_seconds": 5})

    targets = service.discover_active_targets(udi, config)

    assert len(targets) == 1
    assert targets[0]["real_client_count"] == 1
    assert targets[0].get("viewer_left_grace_active") is not True


def test_discovery_preserves_probe_status_for_ui_between_healthy_probes(tmp_path):
    channel = {"id": 1, "uuid": "uuid-1", "name": "Das Erste HD", "streams": [10, 11]}
    udi = FakeUdi(
        statuses=[{
            "uuid-1": active_status(
                stream_id=10,
                clients=[{"client_id": "real-viewer", "user_agent": "TiviMate"}],
            )
        }],
        channels=[channel],
    )
    service = make_service(tmp_path, udi=udi, clock=lambda: 1100.0)
    last_probe = {"completed_at": 1090.0, "blank_detected": False}
    last_event = {"timestamp": 1090.0, "type": "probe_ok"}
    with service._lock:
        service._watched["uuid-1"] = {
            "channel_uuid": "uuid-1",
            "channel_id": 1,
            "channel_ref": "channel-1",
            "channel_name": "Das Erste HD",
            "stream_id": 10,
            "stream_ref": "stream-10",
            "real_client_count": 1,
            "real_client_refs": ["client_id:real-viewer"],
            "probe_state": "waiting",
            "probe_active": False,
            "next_probe_at": 1210.0,
            "last_probe": last_probe,
            "last_event": last_event,
        }
        service._active_probes.add("uuid-1")
    config = normalize_config({"watch_mode": "continuous"})

    targets = service.discover_active_targets(udi, config)
    public_target = service.get_status()["watched_channels"][0]

    assert targets[0]["probe_state"] == "waiting"
    assert public_target["probe_state"] == "waiting"
    assert public_target["probe_active"] is False
    assert public_target["next_probe_at"] == 1210.0
    assert public_target["last_probe"] == last_probe
    assert public_target["last_event"] == last_event


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


def test_config_revision_persists_across_restart_and_invalid_legacy_values_fall_back(tmp_path):
    udi = FakeUdi(statuses=[{}], channels=[])
    service = make_service(tmp_path, udi=udi, watcher_api_key=None)

    assert service.get_config()["config_revision"] == 0
    saved = service.update_config({
        "expected_config_revision": 0,
        "dry_run": True,
    })
    assert saved["config_revision"] == 1
    assert json.loads(service.config_file.read_text(encoding="utf-8"))["config_revision"] == 1

    restarted = make_service(tmp_path, udi=udi, watcher_api_key=None)
    assert restarted.get_config()["config_revision"] == 1
    assert restarted.get_config()["dry_run"] is True

    for index, invalid_revision in enumerate((True, -1, "1")):
        config_file = tmp_path / f"invalid-shadow-{index}.json"
        atomic_write_json(config_file, {
            "dry_run": True,
            "config_revision": invalid_revision,
        })
        invalid_service = ShadowBlankMonitorService(
            config_file=config_file,
            udi_provider=lambda: udi,
            switch_stream=lambda *_args, **_kwargs: True,
            base_url_provider=lambda: "http://dispatcharr.local",
            blank_probe=lambda _url, _config: {"blank_detected": False},
            stream_checker_provider=lambda: FakeStreamChecker(),
        )
        assert invalid_service.get_config()["config_revision"] == 0
        assert invalid_service.get_config()["dry_run"] is True


def test_config_revision_compare_and_swap_allows_exactly_one_concurrent_writer(tmp_path):
    service = make_service(
        tmp_path,
        udi=FakeUdi(statuses=[{}], channels=[]),
        watcher_api_key=None,
    )
    barrier = threading.Barrier(3)
    results = []

    def write(value):
        barrier.wait()
        try:
            result = service.update_config({
                "expected_config_revision": 0,
                "watch_gap_seconds": value,
            })
            results.append(("saved", result["watch_gap_seconds"], result["config_revision"]))
        except ShadowConfigConflictError as exc:
            results.append(("conflict", exc.details["current_config_revision"]))

    threads = [threading.Thread(target=write, args=(value,)) for value in (2, 3)]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join(timeout=2)

    assert all(not thread.is_alive() for thread in threads)
    assert [result[0] for result in results].count("saved") == 1
    assert [result[0] for result in results].count("conflict") == 1
    persisted = json.loads(service.config_file.read_text(encoding="utf-8"))
    saved_result = next(result for result in results if result[0] == "saved")
    assert persisted["watch_gap_seconds"] == saved_result[1]
    assert persisted["config_revision"] == 1


def test_config_roundtrip_revision_alias_and_noop_put_each_advance_revision(tmp_path):
    service = make_service(
        tmp_path,
        udi=FakeUdi(statuses=[{}], channels=[]),
        watcher_api_key=None,
    )
    first_payload = service.get_config()
    first = service.update_config(first_payload)
    second = service.update_config(first)

    assert first["config_revision"] == 1
    assert second["config_revision"] == 2


def test_invalid_config_revision_handler_returns_400_without_mutation(tmp_path):
    service = make_service(
        tmp_path,
        udi=FakeUdi(statuses=[{}], channels=[]),
        watcher_api_key=None,
    )
    app = Flask(__name__)
    with app.app_context():
        for invalid_revision in (True, -1, "0"):
            response, status_code = update_shadow_blank_monitor_config_response(
                payload={"expected_config_revision": invalid_revision},
                get_service=lambda: service,
            )
            payload = response.get_json()
            assert status_code == 400
            assert payload["code"] == "invalid_shadow_config_revision"
        for invalid_roundtrip in (False, 0.0, "0"):
            response, status_code = update_shadow_blank_monitor_config_response(
                payload={
                    "expected_config_revision": 0,
                    "config_revision": invalid_roundtrip,
                },
                get_service=lambda: service,
            )
            payload = response.get_json()
            assert status_code == 400
            assert payload["code"] == "invalid_shadow_config_revision"
    assert service.get_config()["config_revision"] == 0


def test_config_persist_failure_has_no_state_or_cancellation_side_effect(tmp_path, monkeypatch):
    service = make_service(
        tmp_path,
        udi=FakeUdi(statuses=[{}], channels=[]),
        watcher_api_key=None,
    )
    target = {
        "channel_uuid": "uuid-1",
        "channel_id": 1,
        "channel_ref": "channel-1",
        "real_client_count": 1,
    }
    cancel_event = threading.Event()
    watcher_process = FakePersistentWatcherProcess()
    with service._lock:
        service._watched["uuid-1"] = dict(target)
        service._active_probes.add("uuid-1")
        service._probe_cancel_events["uuid-1"] = cancel_event
        service._persistent_watchers["uuid-1"] = {"process": watcher_process}
    before = service.get_config()

    monkeypatch.setattr(
        shadow_module,
        "atomic_write_json",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk full")),
    )
    with pytest.raises(OSError, match="disk full"):
        service.update_config({
            "expected_config_revision": 0,
            "excluded_channel_uuids": ["uuid-1"],
        })

    assert service.get_config() == before
    assert service._watched["uuid-1"]["channel_id"] == 1
    assert service._probe_cancel_events["uuid-1"] is cancel_event
    assert cancel_event.is_set() is False
    assert service._persistent_watchers["uuid-1"]["process"] is watcher_process
    assert watcher_process.terminated is False


def test_config_write_then_raise_publishes_the_durable_committed_revision(tmp_path, monkeypatch):
    service = make_service(
        tmp_path,
        udi=FakeUdi(statuses=[{}], channels=[]),
        watcher_api_key=None,
    )

    def write_then_raise(path, payload, **_kwargs):
        path.write_text(json.dumps(payload), encoding="utf-8")
        raise OSError("directory fsync failed after replace")

    monkeypatch.setattr(shadow_module, "atomic_write_json", write_then_raise)
    updated = service.update_config({
        "expected_config_revision": 0,
        "dry_run": True,
    })

    durable = json.loads(service.config_file.read_text(encoding="utf-8"))
    assert updated["config_revision"] == 1
    assert updated["dry_run"] is True
    assert durable["config_revision"] == 1
    assert durable["dry_run"] is True


def test_stale_config_handler_returns_redacted_revision_conflict(tmp_path):
    service = make_service(
        tmp_path,
        udi=FakeUdi(statuses=[{}], channels=[]),
        watcher_api_key=None,
    )
    service.update_config({
        "expected_config_revision": 0,
        "watcher_api_key": "super-secret-watcher-key",
        "dry_run": True,
    })

    app = Flask(__name__)
    with app.app_context():
        response, status_code = update_shadow_blank_monitor_config_response(
            payload={"expected_config_revision": 0, "dry_run": False},
            get_service=lambda: service,
        )
        payload = response.get_json()

    assert status_code == 409
    assert payload["code"] == "shadow_config_revision_conflict"
    assert set(payload["details"]) == {
        "expected_config_revision",
        "current_config_revision",
        "current_config",
    }
    assert payload["details"]["expected_config_revision"] == 0
    assert payload["details"]["current_config_revision"] == 1
    assert payload["details"]["current_config"]["config_revision"] == 1
    assert payload["details"]["current_config"]["watcher_api_key"] == ""
    assert payload["details"]["current_config"]["has_watcher_api_key"] is True
    assert "super-secret-watcher-key" not in json.dumps(payload)


def test_all_persistent_shadow_config_mutators_advance_revision(tmp_path, monkeypatch):
    service = make_service(
        tmp_path,
        udi=FakeUdi(statuses=[{}], channels=[]),
    )
    assert service.get_config()["config_revision"] == 1

    try:
        assert service.start() is True
        assert service.get_config()["config_revision"] == 2
    finally:
        service.stop()
    assert service.get_config()["config_revision"] == 3

    target = {
        "channel_uuid": "uuid-1",
        "channel_id": 1,
        "channel_ref": "channel-1",
        "stream_id": 10,
        "stream_ref": "stream-10",
        "real_client_count": 1,
    }
    with service._lock:
        service._watched["uuid-1"] = dict(target)
    monkeypatch.setattr(
        service,
        "_capture_offline_image_hash",
        lambda *_args, **_kwargs: {
            "success": True,
            "offline_image_hash": "0123456789abcdef",
        },
    )

    learned = service.learn_offline_image_from_current_frame(enable_detection=True)
    assert learned["success"] is True
    assert learned["config"]["config_revision"] == 4
    deduplicated = service.learn_offline_image_from_current_frame(enable_detection=True)
    assert deduplicated["deduplicated"] is True
    assert deduplicated["config"]["config_revision"] == 4


def test_include_scope_reports_and_skips_active_channels_outside_scope(tmp_path):
    udi = FakeUdi(
        statuses=[{
            "uuid-1": active_status(stream_id=10),
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
    service = make_service(tmp_path, udi=udi)
    config = normalize_config({"included_channel_ids": [2]})

    targets = service.discover_active_targets(udi, config)
    status = service.get_status()

    assert [(target["channel_id"], target["channel_uuid"]) for target in targets] == [(2, "uuid-2")]
    assert status["excluded_active_count"] == 1
    assert status["excluded_active_channels"][0]["channel_id"] == 1
    assert status["excluded_active_channels"][0]["exclude_reason"] == "channel_not_included"


def test_scope_widening_removes_stale_excluded_status_without_waiting_for_scan(tmp_path):
    udi = FakeUdi(
        statuses=[{"uuid-1": active_status(stream_id=10)}],
        channels=[{"id": 1, "uuid": "uuid-1", "streams": [10, 11]}],
    )
    service = make_service(tmp_path, udi=udi)
    narrowed = service.update_config({
        "expected_config_revision": service.get_config()["config_revision"],
        "included_channel_uuids": ["uuid-2"],
    })
    service.discover_active_targets(udi, service.get_config(include_secret=True))
    assert service.get_status()["excluded_active_count"] == 1

    widened = service.update_config({
        "expected_config_revision": narrowed["config_revision"],
        "included_channel_uuids": [],
    })

    assert widened["included_channel_uuids"] == []
    assert service.get_status()["excluded_active_count"] == 0


def test_transient_run_once_include_filter_does_not_publish_config_scope_warning(tmp_path):
    udi = FakeUdi(
        statuses=[{
            "uuid-1": active_status(stream_id=10),
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
    service = make_service(tmp_path, udi=udi)

    targets = service.discover_active_targets(
        udi,
        service.get_config(include_secret=True),
        include_channel_uuids=["uuid-2"],
    )

    assert [target["channel_uuid"] for target in targets] == ["uuid-2"]
    assert service.get_config()["included_channel_uuids"] == []
    assert service.get_status()["excluded_active_count"] == 0


def test_discovery_recomputes_scope_status_after_concurrent_config_narrowing(tmp_path, monkeypatch):
    udi = FakeUdi(
        statuses=[{"uuid-1": active_status(stream_id=10)}],
        channels=[{"id": 1, "uuid": "uuid-1", "streams": [10, 11]}],
    )
    service = make_service(tmp_path, udi=udi)
    stale_config = service.get_config(include_secret=True)
    original_resolve = service._resolve_channel_id
    narrowed = {"done": False}

    def resolve_and_narrow(*args, **kwargs):
        resolved = original_resolve(*args, **kwargs)
        if not narrowed["done"]:
            narrowed["done"] = True
            service.update_config({
                "expected_config_revision": stale_config["config_revision"],
                "included_channel_uuids": ["uuid-2"],
            })
        return resolved

    monkeypatch.setattr(service, "_resolve_channel_id", resolve_and_narrow)
    targets = service.discover_active_targets(udi, stale_config)
    status = service.get_status()

    assert targets == []
    assert status["excluded_active_count"] == 1
    assert status["excluded_active_channels"][0]["channel_id"] == 1
    assert status["excluded_active_channels"][0]["exclude_reason"] == "channel_not_included"


def test_probe_admission_rechecks_current_scope_after_stale_discovery(tmp_path):
    probe_calls = []
    service = make_service(
        tmp_path,
        udi=FakeUdi(statuses=[{}], channels=[]),
        blank_probe=lambda url, _config: probe_calls.append(url) or {"blank_detected": False},
    )
    stale_config = service.get_config(include_secret=True)
    target = {
        "channel_uuid": "uuid-1",
        "channel_id": 1,
        "channel_ref": "channel-1",
        "stream_id": 10,
        "stream_ref": "stream-10",
        "real_client_count": 1,
        "watcher_client_count": 0,
    }
    service.update_config({
        "expected_config_revision": stale_config["config_revision"],
        "included_channel_uuids": ["uuid-2"],
    })

    service._probe_targets(FakeUdi(statuses=[{}], channels=[]), [target], stale_config)

    assert probe_calls == []
    assert service._active_probes == set()
    assert service._probe_cancel_events == {}


def test_channel_id_exclusion_detaches_active_probe_and_persistent_watcher(tmp_path):
    service = make_service(
        tmp_path,
        udi=FakeUdi(statuses=[{}], channels=[]),
    )

    class AliveMonitorThread:
        @staticmethod
        def is_alive():
            return True

    target = {
        "channel_uuid": "uuid-1",
        "channel_id": 1,
        "channel_ref": "channel-1",
        "real_client_count": 1,
    }
    cancel_event = threading.Event()
    watcher_process = FakePersistentWatcherProcess()
    with service._config_mutation_lock:
        with service._lock:
            candidate = normalize_config({"enabled": True}, service._config)
            service._persist_config_candidate_locked(candidate)
            service._thread = AliveMonitorThread()
            service._watched["uuid-1"] = dict(target)
            service._active_probes.add("uuid-1")
            service._probe_cancel_events["uuid-1"] = cancel_event
            service._persistent_watchers["uuid-1"] = {"process": watcher_process}
    expected_revision = service.get_config()["config_revision"]

    updated = service.update_config({
        "expected_config_revision": expected_revision,
        "excluded_channel_ids": [1],
    })

    assert updated["excluded_channel_ids"] == [1]
    assert cancel_event.is_set() is True
    assert watcher_process.terminated is True
    assert "uuid-1" not in service._watched
    assert "uuid-1" not in service._persistent_watchers


def test_excluding_channel_cancels_blocking_custom_probe_without_late_side_effect(tmp_path):
    probe_started = threading.Event()
    release_probe = threading.Event()
    switch_calls = []

    def blocking_probe(_url, _config):
        probe_started.set()
        assert release_probe.wait(timeout=2)
        return {"blank_detected": True, "blank_ratio": 1.0, "blank_duration_secs": 60.0}

    service = make_service(
        tmp_path,
        udi=FakeUdi(
            statuses=[{"uuid-1": active_status(stream_id=10)}],
            channels=[{"id": 1, "uuid": "uuid-1", "streams": [10, 11]}],
        ),
        blank_probe=blocking_probe,
        switch_calls=switch_calls,
    )
    class AliveMonitorThread:
        @staticmethod
        def is_alive():
            return True

    with service._config_mutation_lock:
        with service._lock:
            candidate = normalize_config({
                "enabled": True,
                "confirmation_count": 1,
                "dry_run": False,
            }, service._config)
            service._persist_config_candidate_locked(candidate)
            service._thread = AliveMonitorThread()
    configured = service.get_config()
    service._stop_event.clear()
    target = {
        "channel_uuid": "uuid-1",
        "channel_id": 1,
        "channel_ref": "channel-1",
        "stream_id": 10,
        "stream_ref": "stream-10",
        "real_client_count": 1,
        "watcher_client_count": 0,
    }
    with service._lock:
        service._watched["uuid-1"] = dict(target)

    probe_thread = threading.Thread(
        target=service._probe_targets,
        args=(service.udi_provider(), [target], service.get_config(include_secret=True)),
        kwargs={"single_pass": True},
    )
    probe_thread.start()
    assert probe_started.wait(timeout=1)

    updated = service.update_config({
        "expected_config_revision": configured["config_revision"],
        "excluded_channel_uuids": ["uuid-1"],
    })
    assert updated["excluded_channel_uuids"] == ["uuid-1"]
    release_probe.set()
    probe_thread.join(timeout=2)

    assert not probe_thread.is_alive()
    assert switch_calls == []
    assert service.get_status()["watched_count"] == 0
    assert service.get_status()["recent_events"] == []
    assert service._active_probes == set()


def test_persistent_watcher_popen_race_rechecks_revision_and_scope(tmp_path, monkeypatch):
    popen_entered = threading.Event()
    release_popen = threading.Event()
    update_finished = threading.Event()
    processes = []

    def delayed_popen(_command, **_kwargs):
        process = FakePersistentWatcherProcess()
        processes.append(process)
        popen_entered.set()
        assert release_popen.wait(timeout=2)
        return process

    monkeypatch.setattr(shadow_module.subprocess, "Popen", delayed_popen)
    service = ShadowBlankMonitorService(
        config_file=tmp_path / "shadow.json",
        udi_provider=lambda: FakeUdi(statuses=[{}], channels=[]),
        base_url_provider=lambda: "http://dispatcharr.local",
        stream_checker_provider=lambda: FakeStreamChecker(),
    )
    configured = service.update_config({
        "expected_config_revision": service.get_config()["config_revision"],
        "watcher_api_key": "watcher-key",
        "persistent_watcher_enabled": True,
    })
    service._stop_event.clear()
    config = service.get_config(include_secret=True)
    target = {
        "channel_uuid": "uuid-1",
        "channel_id": 1,
        "channel_ref": "channel-1",
        "stream_id": 10,
        "real_client_count": 1,
    }

    start_thread = threading.Thread(
        target=service._start_persistent_watcher,
        args=("uuid-1", target, config, "http://dispatcharr.local/proxy/ts/stream/uuid-1"),
    )
    start_thread.start()
    assert popen_entered.wait(timeout=1)
    update_thread = threading.Thread(
        target=lambda: (
            service.update_config({
                "expected_config_revision": configured["config_revision"],
                "excluded_channel_uuids": ["uuid-1"],
            }),
            update_finished.set(),
        ),
    )
    update_thread.start()
    assert update_finished.wait(timeout=0.1) is False
    release_popen.set()
    start_thread.join(timeout=2)
    update_thread.join(timeout=2)

    assert not start_thread.is_alive()
    assert not update_thread.is_alive()
    assert update_finished.is_set() is True
    assert len(processes) == 1
    assert processes[0].terminated is True
    assert service._persistent_watchers == {}


def test_scope_update_completed_before_persistent_watcher_admission_prevents_popen(tmp_path, monkeypatch):
    command_entered = threading.Event()
    release_command = threading.Event()
    popen_calls = []

    class AliveThread:
        @staticmethod
        def is_alive():
            return True

    service = ShadowBlankMonitorService(
        config_file=tmp_path / "shadow.json",
        udi_provider=lambda: FakeUdi(statuses=[{}], channels=[]),
        base_url_provider=lambda: "http://dispatcharr.local",
        stream_checker_provider=lambda: FakeStreamChecker(),
    )
    with service._config_mutation_lock:
        with service._lock:
            configured = normalize_config({
                "enabled": True,
                "watcher_api_key": "watcher-key",
                "persistent_watcher_enabled": True,
            }, service._config)
            service._persist_config_candidate_locked(configured)
            service._thread = AliveThread()
    service._stop_event.clear()
    stale_config = service.get_config(include_secret=True)
    target = {
        "channel_uuid": "uuid-1",
        "channel_id": 1,
        "channel_ref": "channel-1",
        "stream_id": 10,
        "real_client_count": 1,
    }
    original_command = service._persistent_watcher_command

    def delayed_command(url, config):
        command_entered.set()
        assert release_command.wait(timeout=2)
        return original_command(url, config)

    monkeypatch.setattr(service, "_persistent_watcher_command", delayed_command)
    monkeypatch.setattr(
        shadow_module.subprocess,
        "Popen",
        lambda *args, **kwargs: popen_calls.append((args, kwargs)) or FakePersistentWatcherProcess(),
    )

    start_thread = threading.Thread(
        target=service._start_persistent_watcher,
        args=("uuid-1", target, stale_config, "http://dispatcharr.local/proxy/ts/stream/uuid-1"),
    )
    start_thread.start()
    assert command_entered.wait(timeout=1)
    updated = service.update_config({
        "expected_config_revision": stale_config["config_revision"],
        "excluded_channel_uuids": ["uuid-1"],
    })
    assert updated["excluded_channel_uuids"] == ["uuid-1"]
    release_command.set()
    start_thread.join(timeout=2)

    assert not start_thread.is_alive()
    assert popen_calls == []
    assert service._persistent_watchers == {}


def test_persistent_watcher_config_rotation_terminates_old_revision_before_put_returns(tmp_path, monkeypatch):
    starts = []

    def fake_popen(command, **_kwargs):
        process = FakePersistentWatcherProcess()
        starts.append((list(command), process))
        return process

    monkeypatch.setattr(shadow_module.subprocess, "Popen", fake_popen)
    service = ShadowBlankMonitorService(
        config_file=tmp_path / "shadow.json",
        udi_provider=lambda: FakeUdi(statuses=[{}], channels=[]),
        base_url_provider=lambda: "http://dispatcharr.local",
        stream_checker_provider=lambda: FakeStreamChecker(),
    )
    configured = service.update_config({
        "expected_config_revision": 0,
        "watcher_api_key": "old-watcher-key",
        "watcher_user_agent": "Old Watcher Agent",
        "persistent_watcher_enabled": True,
    })
    service._stop_event.clear()
    target = {
        "channel_uuid": "uuid-1",
        "channel_id": 1,
        "channel_ref": "channel-1",
        "stream_id": 10,
        "real_client_count": 1,
    }
    service._start_persistent_watcher(
        "uuid-1",
        target,
        service.get_config(include_secret=True),
        "http://dispatcharr.local/proxy/ts/stream/uuid-1",
    )
    old_process = starts[0][1]

    rotated = service.update_config({
        "expected_config_revision": configured["config_revision"],
        "watcher_api_key": "new-watcher-key",
        "watcher_user_agent": "New Watcher Agent",
    })

    assert old_process.terminated is True
    assert service._persistent_watchers == {}
    assert rotated["config_revision"] == configured["config_revision"] + 1
    service._stop_event.clear()
    service._start_persistent_watcher(
        "uuid-1",
        target,
        service.get_config(include_secret=True),
        "http://dispatcharr.local/proxy/ts/stream/uuid-1",
    )
    new_command = " ".join(starts[1][0])
    assert "new-watcher-key" in new_command
    assert "old-watcher-key" not in new_command
    assert "New Watcher Agent" in new_command


def test_stop_timeout_then_start_is_serialized_and_reports_false_until_old_worker_exits(tmp_path):
    join_entered = threading.Event()
    release_join = threading.Event()

    class TimedOutWorker:
        def __init__(self):
            self.alive = True

        def is_alive(self):
            return self.alive

        def join(self, timeout=None):
            assert timeout == 5
            join_entered.set()
            assert release_join.wait(timeout=2)

    service = make_service(
        tmp_path,
        udi=FakeUdi(statuses=[{}], channels=[]),
    )
    old_worker = TimedOutWorker()
    with service._config_mutation_lock:
        with service._lock:
            configured = normalize_config({"enabled": True}, service._config)
            service._persist_config_candidate_locked(configured)
            service._thread = old_worker
    service._stop_event.clear()
    stop_finished = threading.Event()
    start_result = {}

    stop_thread = threading.Thread(
        target=lambda: (service.stop(), stop_finished.set()),
    )
    start_thread = threading.Thread(
        target=lambda: start_result.setdefault("value", service.start()),
    )
    stop_thread.start()
    assert join_entered.wait(timeout=1)
    start_thread.start()
    assert "value" not in start_result
    release_join.set()
    stop_thread.join(timeout=2)
    start_thread.join(timeout=2)

    assert stop_finished.is_set() is True
    assert start_result["value"] is False
    assert service.get_config()["enabled"] is False
    assert service._stop_event.is_set() is True

    old_worker.alive = False
    assert service.start() is True
    assert service.get_status()["running"] is True
    service.stop()


def test_repeated_start_is_idempotent_and_does_not_stale_active_probe_revision(tmp_path):
    service = make_service(
        tmp_path,
        udi=FakeUdi(statuses=[{}], channels=[]),
    )
    assert service.start() is True
    revision = service.get_config()["config_revision"]
    cancel_event = threading.Event()
    with service._lock:
        service._active_probes.add("uuid-1")
        service._probe_cancel_events["uuid-1"] = cancel_event

    try:
        assert service.start() is True
        assert service.get_config()["config_revision"] == revision
        assert cancel_event.is_set() is False
    finally:
        service.stop()


def test_worker_thread_start_failure_durably_reconciles_enabled_state(tmp_path, monkeypatch):
    class FailingThread:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

        @staticmethod
        def is_alive():
            return False

        @staticmethod
        def start():
            raise RuntimeError("thread resource exhausted")

    service = make_service(
        tmp_path,
        udi=FakeUdi(statuses=[{}], channels=[]),
    )
    initial_revision = service.get_config()["config_revision"]
    monkeypatch.setattr(shadow_module.threading, "Thread", FailingThread)

    with pytest.raises(RuntimeError, match="thread resource exhausted"):
        service.start()

    public = service.get_config()
    durable = json.loads(service.config_file.read_text(encoding="utf-8"))
    assert public["enabled"] is False
    assert public["config_revision"] == initial_revision + 2
    assert durable["enabled"] is False
    assert durable["config_revision"] == public["config_revision"]
    assert service._thread is None
    assert service._stop_event.is_set() is True
    assert service.get_status()["last_error"] == shadow_module.SHADOW_MONITOR_START_ERROR_MESSAGE


def test_update_enabled_thread_start_failure_returns_honest_disabled_revision(tmp_path, monkeypatch):
    class FailingThread:
        def __init__(self, *args, **kwargs):
            pass

        @staticmethod
        def is_alive():
            return False

        @staticmethod
        def start():
            raise RuntimeError("thread start failed")

    service = make_service(
        tmp_path,
        udi=FakeUdi(statuses=[{}], channels=[]),
    )
    initial_revision = service.get_config()["config_revision"]
    monkeypatch.setattr(shadow_module.threading, "Thread", FailingThread)

    with pytest.raises(RuntimeError, match="thread start failed"):
        service.update_config({
            "expected_config_revision": initial_revision,
            "enabled": True,
        })

    public = service.get_config()
    durable = json.loads(service.config_file.read_text(encoding="utf-8"))
    assert public["enabled"] is False
    assert public["config_revision"] == initial_revision + 2
    assert durable["enabled"] is False
    assert durable["config_revision"] == public["config_revision"]
    assert service.get_status()["running"] is False


def test_worker_thread_constructor_failure_durably_reconciles_enabled_state(tmp_path, monkeypatch):
    class ConstructorFailure:
        def __new__(cls, *args, **kwargs):
            raise MemoryError("thread constructor failed")

    service = make_service(
        tmp_path,
        udi=FakeUdi(statuses=[{}], channels=[]),
    )
    initial_revision = service.get_config()["config_revision"]
    monkeypatch.setattr(shadow_module.threading, "Thread", ConstructorFailure)

    with pytest.raises(MemoryError, match="thread constructor failed"):
        service.start()

    durable = json.loads(service.config_file.read_text(encoding="utf-8"))
    assert service.get_config()["enabled"] is False
    assert service.get_config()["config_revision"] == initial_revision + 2
    assert durable["enabled"] is False
    assert durable["config_revision"] == initial_revision + 2
    assert service._thread is None
    assert service._stop_event.is_set() is True


def test_probe_thread_start_failure_releases_only_its_registered_admission(tmp_path, monkeypatch):
    class FailingThread:
        def __init__(self, *args, **kwargs):
            pass

        @staticmethod
        def start():
            raise RuntimeError("probe thread start failed")

    service = make_service(
        tmp_path,
        udi=FakeUdi(statuses=[{}], channels=[]),
    )
    config = service.get_config(include_secret=True)
    target = {
        "channel_uuid": "uuid-1",
        "channel_id": 1,
        "channel_ref": "channel-1",
        "stream_id": 10,
        "stream_ref": "stream-10",
        "real_client_count": 1,
        "watcher_client_count": 0,
    }
    monkeypatch.setattr(shadow_module.threading, "Thread", FailingThread)

    with pytest.raises(RuntimeError, match="probe thread start failed"):
        service._probe_targets(
            FakeUdi(statuses=[{}], channels=[]),
            [target],
            config,
        )

    assert service._active_probes == set()
    assert service._probe_cancel_events == {}


def test_probe_thread_constructor_failure_releases_registered_admission(tmp_path, monkeypatch):
    class ConstructorFailure:
        def __new__(cls, *args, **kwargs):
            raise MemoryError("probe thread constructor failed")

    service = make_service(
        tmp_path,
        udi=FakeUdi(statuses=[{}], channels=[]),
    )
    target = {
        "channel_uuid": "uuid-1",
        "channel_id": 1,
        "channel_ref": "channel-1",
        "stream_id": 10,
        "stream_ref": "stream-10",
        "real_client_count": 1,
        "watcher_client_count": 0,
    }
    monkeypatch.setattr(shadow_module.threading, "Thread", ConstructorFailure)

    with pytest.raises(MemoryError, match="probe thread constructor failed"):
        service._probe_targets(
            FakeUdi(statuses=[{}], channels=[]),
            [target],
            service.get_config(include_secret=True),
        )

    assert service._active_probes == set()
    assert service._probe_cancel_events == {}


def test_cancelable_probe_process_terminates_promptly_on_channel_cancel(tmp_path, monkeypatch):
    process_started = threading.Event()

    class BlockingProcess:
        def __init__(self):
            self.returncode = None
            self.terminated = False

        def poll(self):
            return self.returncode

        def communicate(self, timeout=None):
            if self.returncode is None:
                process_started.set()
                raise shadow_module.subprocess.TimeoutExpired(["ffmpeg"], timeout)
            return b"", b""

        def terminate(self):
            self.terminated = True
            self.returncode = 0

        def kill(self):
            self.returncode = -9

    process = BlockingProcess()
    monkeypatch.setattr(shadow_module.subprocess, "Popen", lambda *_args, **_kwargs: process)
    service = make_service(tmp_path, udi=FakeUdi(statuses=[{}], channels=[]))
    cancel_event = threading.Event()
    result = {}

    thread = threading.Thread(
        target=lambda: result.setdefault(
            "completed",
            service._run_cancelable_command(
                ["ffmpeg"],
                {"_shadow_probe_cancel_event": cancel_event},
                timeout=10,
            ),
        ),
    )
    thread.start()
    assert process_started.wait(timeout=1)
    cancel_event.set()
    thread.join(timeout=1)

    assert not thread.is_alive()
    assert process.terminated is True
    assert result["completed"].returncode == 0


def test_provider_viewer_preemption_cancels_blank_and_offline_image_polls(
    tmp_path,
    monkeypatch,
):
    class BlockingProcess:
        def __init__(self, *, text=False):
            self.returncode = None
            self.terminated = False
            self.text = text

        def poll(self):
            return self.returncode

        def communicate(self, timeout=None):
            if self.text:
                return "", ""
            return b"", b""

        def terminate(self):
            self.terminated = True
            self.returncode = 0

        def kill(self):
            self.returncode = -9

    processes = []

    def popen(*_args, **kwargs):
        process = BlockingProcess(text=bool(kwargs.get("text")))
        processes.append(process)
        return process

    monkeypatch.setattr(shadow_module.subprocess, "Popen", popen)
    monkeypatch.setattr(
        shadow_module.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("provider-aware probes must use polling"),
    )
    service = make_service(tmp_path, udi=FakeUdi(statuses=[{}], channels=[]))

    blank_state = {"viewer_preempted": False}
    blank_config = normalize_config({})
    blank_config.update({
        "_shadow_provider_preempt_check": lambda: True,
        "_shadow_provider_preemption_state": blank_state,
    })
    blank_result = service._run_blank_probe(
        "https://provider.invalid/live/blank",
        blank_config,
    )

    offline_state = {"viewer_preempted": False}
    offline_config = normalize_config({})
    offline_config.update({
        "_shadow_provider_preempt_check": lambda: True,
        "_shadow_provider_preemption_state": offline_state,
    })
    offline_result = service._capture_offline_image_hash(
        "https://provider.invalid/live/offline",
        offline_config,
    )

    assert blank_result == {
        "blank_detected": False,
        "stopped": True,
        "viewer_preempted": True,
    }
    assert offline_result == {"success": False, "reason": "viewer_preempted"}
    assert len(processes) == 2
    assert all(process.terminated for process in processes)


def test_provider_viewer_preemption_aborts_loop_probe_poll(tmp_path):
    preemption_state = {"viewer_preempted": False}
    loop_phase = {"active": False}
    loop_calls = []

    def provider_preempt_check():
        return loop_phase["active"]

    def loop_probe(_url, config):
        loop_calls.append(config)
        loop_phase["active"] = True
        assert config["_shadow_loop_abort_check"]() is True
        return {"loop_probe_ran": True, "loop_detected": True}

    service = make_service(
        tmp_path,
        udi=FakeUdi(statuses=[{}], channels=[]),
        loop_probe=loop_probe,
    )
    service._run_blank_probe = lambda _url, _config: {"blank_detected": False}
    config = normalize_config({"loop_detection_enabled": True})
    config.update({
        "_shadow_provider_preempt_check": provider_preempt_check,
        "_shadow_provider_preemption_state": preemption_state,
    })

    result = service._run_next_stream_pre_probe(
        "https://provider.invalid/live/loop",
        config,
        reason="loop",
    )

    assert len(loop_calls) == 1
    assert result == {"stopped": True, "viewer_preempted": True}
    assert preemption_state == {"viewer_preempted": True}


def test_switch_fence_blocks_excluded_target_and_orders_update_after_inflight_switch(tmp_path):
    target = {
        "channel_uuid": "uuid-1",
        "channel_id": 1,
        "channel_ref": "channel-1",
        "stream_id": 10,
    }
    blocked_calls = []
    (tmp_path / "blocked").mkdir()
    blocked_service = make_service(
        tmp_path / "blocked",
        udi=FakeUdi(statuses=[{}], channels=[]),
        switch_calls=blocked_calls,
    )
    blocked_config = blocked_service.get_config(include_secret=True)
    blocked_config["_shadow_probe_cancel_event"] = threading.Event()
    with blocked_service._config_mutation_lock:
        result = {}
        switch_thread = threading.Thread(
            target=lambda: result.setdefault(
                "value",
                blocked_service._switch_stream_with_config_fence(
                    "uuid-1",
                    11,
                    channel_uuid="uuid-1",
                    target=target,
                    config=blocked_config,
                    origin_stream_id=10,
                ),
            ),
        )
        switch_thread.start()
        blocked_service.update_config({
            "expected_config_revision": blocked_service.get_config()["config_revision"],
            "excluded_channel_uuids": ["uuid-1"],
        })
    switch_thread.join(timeout=2)
    assert result["value"] is None
    assert blocked_calls == []

    switch_entered = threading.Event()
    release_switch = threading.Event()
    switch_finished = threading.Event()
    update_finished = threading.Event()

    def blocking_switch(_channel_id, stream_id=None, url=None):
        switch_entered.set()
        assert release_switch.wait(timeout=2)
        switch_finished.set()
        return True

    ordered_service = ShadowBlankMonitorService(
        config_file=tmp_path / "ordered-shadow.json",
        udi_provider=lambda: FakeUdi(statuses=[{}], channels=[]),
        switch_stream=blocking_switch,
        base_url_provider=lambda: "http://dispatcharr.local",
        blank_probe=lambda _url, _config: {"blank_detected": False},
        stream_checker_provider=lambda: FakeStreamChecker(),
    )
    ordered_config = ordered_service.get_config(include_secret=True)
    ordered_config["_shadow_probe_cancel_event"] = threading.Event()
    ordered_service._uses_default_switch_stream = True
    with ordered_service._lock:
        ordered_service._probe_cancel_events["uuid-1"] = ordered_config[
            "_shadow_probe_cancel_event"
        ]
    switch_result = {}
    switch_thread = threading.Thread(
        target=lambda: switch_result.setdefault(
            "value",
            ordered_service._switch_stream_with_config_fence(
                "uuid-1",
                11,
                channel_uuid="uuid-1",
                target=target,
                config=ordered_config,
                origin_stream_id=10,
            ),
        ),
    )
    update_thread = threading.Thread(
        target=lambda: (
            ordered_service.update_config({
                "expected_config_revision": 0,
                "excluded_channel_uuids": ["uuid-1"],
            }),
            update_finished.set(),
        ),
    )
    switch_thread.start()
    assert switch_entered.wait(timeout=1)
    update_thread.start()
    assert update_finished.wait(timeout=0.1) is False
    release_switch.set()
    switch_thread.join(timeout=2)
    update_thread.join(timeout=2)

    assert switch_result["value"] is True
    assert switch_finished.is_set() is True
    assert update_finished.is_set() is True
    assert ordered_service.get_config()["excluded_channel_uuids"] == ["uuid-1"]
    assert ordered_config["_shadow_probe_cancel_event"].is_set() is True
    assert len(ordered_service._switch_history["uuid-1"]) == 1
    status = ordered_service.get_status()
    api_event = next(
        event
        for event in status["recent_events"]
        if event["type"] == "switch_api_completed"
    )
    assert api_event["decision_group"] == "switch"
    assert api_event["details"]["switch_api_success"] is True
    assert api_event["details"]["post_switch_verification_pending"] is True
    assert status["switch_summary"]["successful_switches"] == 0
    assert status["switch_summary"]["api_completed_switches"] == 1
    assert status["switch_summary"]["api_successful_switch_calls"] == 1
    assert status["switch_summary"]["pending_post_switch_verifications"] == 1
    assert status["switch_summary"]["last_switch_result"] == "switch_api_completed"


def test_switch_fence_rejects_stale_revision_even_when_old_snapshot_was_live_mode(tmp_path):
    switch_calls = []
    service = make_service(
        tmp_path,
        udi=FakeUdi(statuses=[{}], channels=[]),
        switch_calls=switch_calls,
    )
    stale_config = service.get_config(include_secret=True)
    stale_config["_shadow_probe_cancel_event"] = threading.Event()
    service.update_config({
        "expected_config_revision": stale_config["config_revision"],
        "dry_run": True,
    })
    target = {
        "channel_uuid": "uuid-1",
        "channel_id": 1,
        "channel_ref": "channel-1",
        "stream_id": 10,
    }

    result = service._switch_stream_with_config_fence(
        "uuid-1",
        11,
        channel_uuid="uuid-1",
        target=target,
        config=stale_config,
        origin_stream_id=10,
    )

    assert result is None
    assert switch_calls == []


def test_switch_summary_resolves_api_completion_when_final_verification_event_exists():
    summary = ShadowBlankMonitorService._switch_summary([
        {
            "timestamp": 1002.0,
            "type": "switch_success",
            "details": {
                "switch_attempt_ref": "switch-attempt-1",
                "reason": "blank",
            },
        },
        {
            "timestamp": 1001.0,
            "type": "switch_api_completed",
            "details": {
                "switch_attempt_ref": "switch-attempt-1",
                "reason": "blank",
                "switch_api_success": True,
                "post_switch_verification_pending": True,
            },
        },
    ])

    assert summary["successful_switches"] == 1
    assert summary["api_completed_switches"] == 1
    assert summary["api_successful_switch_calls"] == 1
    assert summary["pending_post_switch_verifications"] == 0
    assert summary["last_switch_result"] == "switch_success"
