"""Full runs resolve one consistent configuration without per-channel SQL reads."""

from datetime import datetime, timedelta
import time
from unittest.mock import Mock

import pytest
from sqlalchemy import event

from apps.automation.automated_stream_manager import AutomatedStreamManager
from apps.automation.automation_config_manager import AutomationConfigManager


def _create_profile(manager, name):
    profile_id = manager.create_profile({"name": name, "stream_matching": {"enabled": True}})
    assert profile_id
    return profile_id


def _create_period(manager, name, priority):
    period_id = manager.create_period({
        "name": name,
        "schedule": {"type": "interval", "value": 60},
        "priority": priority,
    })
    assert period_id
    return period_id


def test_run_snapshot_preserves_overrides_priority_and_edits_between_runs(clean_test_db):
    manager = AutomationConfigManager()
    group_profile = _create_profile(manager, "Group profile")
    channel_profile = _create_profile(manager, "Channel profile")
    low_period = _create_period(manager, "Low", 1)
    high_period = _create_period(manager, "High", 10)
    assert manager.assign_period_to_groups(low_period, [7], group_profile)
    assert manager.assign_period_to_groups(high_period, [7], group_profile)
    assert manager.assign_period_to_channels(high_period, [42], channel_profile)

    original = manager.get_effective_configuration(42, 7)
    assert original['period_id'] == high_period
    assert original['profile']['name'] == "Channel profile"
    assert [period['id'] for period in original['periods']] == [high_period, low_period]
    assert next(period for period in original['periods'] if period['id'] == low_period)['profile']['id'] == group_profile

    previous = manager.begin_run_snapshot()
    try:
        assert manager.get_effective_configuration(42, 7) == original
        assert manager.get_effective_configuration(43, 7)['profile']['id'] == group_profile
        assert manager.update_profile(channel_profile, {"name": "Changed during run"})
        assert manager.get_effective_configuration(42, 7)['profile']['name'] == "Channel profile"
    finally:
        manager.end_run_snapshot(previous)

    next_run = manager.begin_run_snapshot()
    try:
        assert manager.get_effective_configuration(42, 7)['profile']['name'] == "Changed during run"
    finally:
        manager.end_run_snapshot(next_run)


@pytest.mark.filterwarnings(r'ignore:The Query.get\(\) method is considered legacy:sqlalchemy.exc.LegacyAPIWarning')
def test_222_channel_three_pass_snapshot_reduces_selects_to_three(clean_test_db):
    manager = AutomationConfigManager()
    profile_id = _create_profile(manager, "Shared profile")
    period_id = _create_period(manager, "Shared period", 0)
    assert manager.assign_period_to_groups(period_id, [7], profile_id)

    selects = []

    def record_select(_connection, _cursor, statement, _parameters, _context, _executemany):
        if statement.lstrip().upper().startswith('SELECT'):
            selects.append(statement)

    def resolve_three_passes():
        for _ in range(3):
            for channel_id in range(1, 223):
                assert manager.get_effective_configuration(channel_id, 7)['period_id'] == period_id

    event.listen(clean_test_db, 'before_cursor_execute', record_select)
    try:
        legacy_started = time.perf_counter()
        resolve_three_passes()
        legacy_seconds = time.perf_counter() - legacy_started
        legacy_selects = len(selects)
        selects.clear()

        snapshot_started = time.perf_counter()
        previous = manager.begin_run_snapshot()
        try:
            resolve_three_passes()
        finally:
            manager.end_run_snapshot(previous)
        snapshot_seconds = time.perf_counter() - snapshot_started
        snapshot_selects = len(selects)
    finally:
        event.remove(clean_test_db, 'before_cursor_execute', record_select)

    assert legacy_selects == 222 * 3 * 4
    assert snapshot_selects == 3
    print(
        f"222 channels x 3 passes: {legacy_selects} SELECTs/{legacy_seconds:.3f}s "
        f"before, {snapshot_selects} SELECTs/{snapshot_seconds:.3f}s with run snapshot"
    )


def test_due_period_is_evaluated_once_and_grace_skip_is_not_duplicated():
    manager = object.__new__(AutomatedStreamManager)
    manager.period_last_run = {"12": datetime.now() - timedelta(minutes=70)}
    manager._scheduler_retry_state = {}
    manager._period_skip_history = {}
    manager._save_state = Mock()
    period = {
        "id": "12",
        "name": "Scheduled period",
        "schedule": {"type": "interval", "value": 60},
        "missed_run_grace_minutes": 5,
    }
    due_cache = {}

    for _ in range(222):
        assert manager._is_period_due_for_cycle("12", period, due_cache) is False

    assert len(manager._period_skip_history["12"]) == 1
    manager._save_state.assert_called_once()


def test_due_period_clears_exhausted_retry_once_for_all_assigned_channels():
    manager = object.__new__(AutomatedStreamManager)
    manager.period_last_run = {"12": datetime.now() - timedelta(minutes=61)}
    manager._scheduler_retry_state = {"12": {"exhausted": True}}
    manager._save_state = Mock()
    period = {"id": "12", "schedule": {"type": "interval", "value": 60}}
    due_cache = {}

    for _ in range(222):
        assert manager._is_period_due_for_cycle("12", period, due_cache) is True

    assert "12" not in manager._scheduler_retry_state
    manager._save_state.assert_called_once()
