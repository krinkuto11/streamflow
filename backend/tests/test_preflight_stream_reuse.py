"""Reuse requires fresh, complete evidence for this exact event and source."""
from copy import deepcopy
from datetime import datetime, timezone

import pytest

from apps.core.stream_stats_utils import is_stream_dead
from apps.stream.preflight_stream_reuse import (
    PreflightStreamReuse, STATE_KEY, automatic_reuse_context,
)
from apps.stream.teamarr_preflight_service import normalize_config


class Database:
    def __init__(self):
        self.values = {}

    def get_system_setting(self, key, default=None):
        return deepcopy(self.values.get(key, default))

    def set_system_setting(self, key, value):
        self.values[key] = deepcopy(value)


def measured(sid=1, **overrides):
    result = dict(stream_id=sid, resolution="1920x1080", fps=50,
                  bitrate_kbps=4000, video_codec="h264", audio_codec="aac",
                  quality_reason="none", status="completed")
    result.update(overrides)
    return result


def source(sid=1, **facts):
    return dict(id=sid, name=f"Feed {sid}", url=f"https://host/secret/{sid}",
                m3u_account=2, stream_stats=measured(sid, **facts))


@pytest.fixture
def evidence():
    db, clock = Database(), [1700000000.0]
    profile = {"id": "7", "stream_checking": {"enabled": True, "min_bitrate": 2000}}
    config = normalize_config({"skip_known_good_streams": True, "post_start_grace_minutes": 10})
    event = {"identity": "event:1", "event_date": datetime.fromtimestamp(clock[0] + 1200, timezone.utc).isoformat(),
             "dispatcharr_channel_id": 77, "dispatcharr_uuid": "channel-uuid", "trigger_bucket": "20m"}
    channel = {"id": 77, "uuid": "channel-uuid"}
    accounts = [{"id": 2, "profiles": [{"id": 3, "is_active": True, "search_pattern": "old", "replace_pattern": "secret"}]}]
    values = dict(db=db, clock=clock, profile=profile, config=config, event=event, channel=channel,
                  accounts=accounts, streams=[source()], analysis={"ffmpeg_duration": 20})

    def prepare(**overrides):
        args = dict(context=automatic_reuse_context(values["event"], values["config"]),
                    channel=values["channel"], streams=values["streams"], profile=values["profile"],
                    analysis_config=values["analysis"], accounts=values["accounts"],
                    is_good=lambda item: not is_stream_dead(item, {"min_bitrate_kbps": 2000})[0],
                    is_dead=lambda url: False)
        args.update(overrides)
        return PreflightStreamReuse(lambda: db, lambda: clock[0]).prepare(**args)

    values["prepare"] = prepare
    return values


def seed(evidence):
    run = evidence["prepare"]()
    assert run.target_ids == [1]
    run.finish([measured()], {1}, set())
    return run


def test_default_off_and_manual_force_never_reuse(evidence):
    assert normalize_config({})["skip_known_good_streams"] is False
    assert normalize_config({"skip_known_good_streams": "false"})["skip_known_good_streams"] is False
    assert automatic_reuse_context(evidence["event"], normalize_config({})) is None
    evidence["event"]["trigger_bucket"] = "manual"
    assert automatic_reuse_context(evidence["event"], evidence["config"]) is None


def test_checkpoints_retry_failures_and_stop_after_recovery(evidence):
    evidence["streams"].append(source(2, bitrate_kbps=500))
    run = evidence["prepare"]()
    assert run.target_ids == [1, 2]
    run.finish([measured(), measured(2, bitrate_kbps=500)], {1, 2}, {2})
    original = next(iter(evidence["db"].values[STATE_KEY]["entries"].values()))["measured_at"]
    for minute, expected in [(-5, [2]), (2, [2]), (5, [])]:
        evidence["clock"][0] = 1700001200 + minute * 60
        run = evidence["prepare"]()
        assert run.target_ids == expected
        if minute == 2:
            evidence["streams"][1] = source(2)
            run.finish([measured(2)], {2}, set())
        else:
            run.finish([], set(), set())
    entries = evidence["db"].values[STATE_KEY]["entries"]
    assert sorted(item["measured_at"] for item in entries.values())[0] == original
    assert original == 1700000000
    assert "secret" not in str(entries) and "https://" not in str(entries)


@pytest.mark.parametrize("change", ["event", "date", "uuid", "url", "name", "provider_profile", "profile", "analysis", "new_measurement"])
def test_changed_identity_source_policy_or_measurement_requires_probe(evidence, change):
    seed(evidence)
    evidence["clock"][0] += 60
    assert evidence["prepare"]().target_ids == []
    if change == "event": evidence["event"]["identity"] = "event:2"
    elif change == "date": evidence["event"]["event_date"] = datetime.fromtimestamp(1700001240, timezone.utc).isoformat()
    elif change == "uuid": evidence["event"]["dispatcharr_uuid"] = evidence["channel"]["uuid"] = "new"
    elif change == "url": evidence["streams"][0]["url"] += "new"
    elif change == "name": evidence["streams"][0]["name"] += "new"
    elif change == "provider_profile": evidence["accounts"][0]["profiles"][0]["replace_pattern"] = "changed"
    elif change == "profile": evidence["profile"]["stream_checking"]["min_bitrate"] = 3000
    elif change == "analysis": evidence["analysis"]["ffmpeg_duration"] = 30
    elif change == "new_measurement": evidence["streams"][0]["stream_stats"]["bitrate_kbps"] = 4500
    assert evidence["prepare"]().target_ids == [1]


@pytest.mark.parametrize("bad", [
    {"bitrate_kbps": 0}, {"bitrate_kbps": None}, {"bitrate_kbps": 500},
    {"bitrate_kbps": float("nan")}, {"resolution": "0x0"},
    {"fps": None}, {"fps": 0}, {"status": "failed"},
    {"measurement_incomplete": True}, {"bitrate_recheck_required": True},
    {"visual_probe_incomplete": True}, {"blank_detected": True}, {"freeze_detected": True},
    {"loop_detected": True}, {"loop_probe_ran": True, "loop_detected": None},
    {"quality_reason": "offline"}, {"provider_limit_skipped": True},
    {"error": "timeout"}, {"status": "cached"},
])
def test_incomplete_or_failed_probes_do_not_become_success_evidence(evidence, bad):
    evidence["prepare"]().finish([measured(**bad)], {1}, set())
    assert evidence["prepare"]().target_ids == [1]


def test_newer_failure_or_dead_tracker_overrides_old_success(evidence):
    seed(evidence)
    assert evidence["prepare"](is_dead=lambda _: True).target_ids == [1]
    evidence["streams"][0]["stream_stats"]["measurement_incomplete"] = True
    run = evidence["prepare"]()
    assert run.target_ids == [1]
    run.finish([measured(measurement_incomplete=True)], {1}, set())
    evidence["streams"][0] = source()
    assert evidence["prepare"]().target_ids == [1]


def test_expired_restart_and_corrupt_state_fail_closed(evidence):
    seed(evidence)
    # prepare creates a new store each time, as after a restart.
    assert evidence["prepare"]().target_ids == []
    evidence["clock"][0] += 1801
    assert evidence["prepare"]() is None
    evidence["clock"][0] -= 1801
    evidence["db"].values[STATE_KEY] = {"version": 999, "entries": "broken"}
    assert evidence["prepare"]().target_ids == [1]
    evidence["db"].get_system_setting = lambda *args: (_ for _ in ()).throw(RuntimeError("unavailable"))
    assert evidence["prepare"]() is None


def test_active_viewer_counts_do_not_invalidate_source_and_new_streams_are_checked(evidence):
    seed(evidence)
    evidence["accounts"][0]["profiles"][0]["current_viewers"] = 9
    evidence["streams"].append(source(2))
    run = evidence["prepare"]()
    assert run.target_ids == [2] and list(run.reused) == [1]


def test_static_team_live_flag_does_not_change_event_window_identity(evidence):
    evidence["event"].update(preflight_kind="team", next_live_window={"start": "start", "stop": "end", "is_live": False})
    seed(evidence)
    evidence["event"]["next_live_window"]["is_live"] = True
    assert evidence["prepare"]().target_ids == []


def test_enabled_visual_probes_require_complete_measurement(evidence):
    evidence["profile"]["stream_checking"]["blank_check_enabled"] = True
    evidence["prepare"]().finish([measured()], {1}, set())
    assert evidence["prepare"]().target_ids == [1]


def test_bounded_persistence_keeps_sibling_events(evidence, monkeypatch):
    from apps.stream import preflight_stream_reuse as module
    monkeypatch.setattr(module, "MAX_ENTRIES", 2)
    for index in range(3):
        evidence["event"]["identity"] = f"event:{index}"
        evidence["clock"][0] += 1
        evidence["prepare"]().finish([measured()], {1}, set())
    assert len(evidence["db"].values[STATE_KEY]["entries"]) == 2
    assert evidence["prepare"]().target_ids == []


def test_newer_failure_on_transformed_provider_route_prevents_reuse(evidence):
    from types import SimpleNamespace
    from apps.stream.stream_checker_service import StreamCheckerService
    checker = StreamCheckerService.__new__(StreamCheckerService)
    checker.dead_streams_tracker = SimpleNamespace(is_dead=lambda url: url == 'https://transformed/failed')
    udi = SimpleNamespace(apply_profile_url_transformation=lambda stream, profile: 'https://transformed/failed')
    seed(evidence)
    run = evidence['prepare'](is_dead=lambda stream: checker._preflight_source_is_dead(stream, udi, evidence['accounts']))
    assert run.target_ids == [1]
