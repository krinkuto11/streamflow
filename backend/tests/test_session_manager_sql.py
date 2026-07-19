import threading
import time
from unittest.mock import patch

import pytest

from apps.database.connection import get_session
from apps.database.models import MonitoringSession
from apps.stream.stream_session_manager import SessionInfo, StreamInfo, StreamSessionManager


def _new_manager():
    StreamSessionManager._instance = None
    manager = StreamSessionManager()
    manager.sessions = {}
    manager.session_locks = {}
    manager.scoring_windows = {}
    manager.channel_ownership = {}
    return manager


def _session(session_id="session-one", *, channel_id=101, with_stream=True):
    info = SessionInfo(
        session_id=session_id,
        channel_id=channel_id,
        channel_name=f"Channel {channel_id}",
        regex_filter=".*",
        created_at=time.time(),
        is_active=False,
    )
    if with_stream:
        info.streams[9001] = StreamInfo(
            stream_id=9001,
            url="http://stream.invalid/test",
            name="Test stream",
            channel_id=channel_id,
            status="review",
        )
    return info


def _rows():
    session = get_session()
    try:
        return session.query(MonitoringSession).order_by(MonitoringSession.session_id).all()
    finally:
        session.close()


def test_session_snapshots_persist_parent_and_stream_rows_and_reload():
    manager = _new_manager()
    manager.sessions["session-empty"] = _session("session-empty", with_stream=False)
    manager.sessions["session-one"] = _session("session-one")

    assert manager._save_sessions(wait=True)
    assert [row.session_id for row in _rows()] == [
        "session-empty",
        "session-one",
        "session-one_9001",
    ]

    reloaded = _new_manager()
    reloaded._load_sessions()
    assert set(reloaded.sessions) == {"session-empty", "session-one"}
    assert reloaded.sessions["session-empty"].streams == {}
    assert set(reloaded.sessions["session-one"].streams) == {9001}


def test_delete_commits_before_success_and_does_not_return_after_restart():
    manager = _new_manager()
    manager.sessions["session-one"] = _session("session-one")
    manager.session_locks["session-one"] = threading.Lock()
    manager.scoring_windows["session-one"] = {}
    assert manager._save_sessions(wait=True)

    with patch(
        "apps.stream.stream_monitoring_service.get_monitoring_service"
    ) as monitoring, patch(
        "apps.stream.stream_screenshot_service.get_screenshot_service"
    ) as screenshots:
        monitoring.return_value.stop_session_monitors.return_value = None
        screenshots.return_value.delete_screenshot.return_value = True
        assert manager.delete_session("session-one") is True

    assert _rows() == []
    restarted = _new_manager()
    restarted._load_sessions()
    assert "session-one" not in restarted.sessions


def test_parallel_save_requests_are_serialized_and_latest_snapshot_wins():
    manager = _new_manager()
    manager.sessions["session-one"] = _session("session-one")
    first_started = threading.Event()
    release_first = threading.Event()
    calls = []
    active = 0
    max_active = 0
    call_lock = threading.Lock()

    def persist(snapshot):
        nonlocal active, max_active
        with call_lock:
            active += 1
            max_active = max(max_active, active)
            calls.append(snapshot["session-one"].channel_name)
            call_number = len(calls)
        if call_number == 1:
            first_started.set()
            assert release_first.wait(timeout=2)
        with call_lock:
            active -= 1
        return True, None

    with patch.object(manager, "_persist_sessions_snapshot", side_effect=persist):
        assert manager._save_sessions()
        assert first_started.wait(timeout=2)
        manager.sessions["session-one"].channel_name = "Latest channel name"
        assert manager._save_sessions()
        release_first.set()
        assert manager._save_sessions(wait=True)

    assert max_active == 1
    assert calls[0] == "Channel 101"
    assert calls[-1] == "Latest channel name"


def test_failed_delete_restores_in_memory_session_and_reports_failure():
    manager = _new_manager()
    manager.sessions["session-one"] = _session("session-one")
    manager.session_locks["session-one"] = threading.Lock()
    manager.scoring_windows["session-one"] = {}
    assert manager._save_sessions(wait=True)

    with patch(
        "apps.stream.stream_monitoring_service.get_monitoring_service"
    ), patch(
        "apps.stream.stream_screenshot_service.get_screenshot_service"
    ), patch.object(
        manager,
        "_persist_sessions_snapshot",
        return_value=(False, "database unavailable"),
    ):
        assert manager.delete_session("session-one") is False

    assert "session-one" in manager.sessions
    assert any(row.session_id == "session-one" for row in _rows())


@pytest.fixture(autouse=True)
def reset_singleton_after_test():
    yield
    StreamSessionManager._instance = None
