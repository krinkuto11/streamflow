import ast
from pathlib import Path
from unittest.mock import Mock

from apps.automation.automated_stream_manager import AutomatedStreamManager


def test_run_progress_tracks_active_stage():
    manager = AutomatedStreamManager()

    manager._start_run_status(forced=True, forced_period_id="period-1")
    manager._update_run_status(
        stage="stream_matching",
        stage_label="Matching Streams",
        message="Matching streams",
        progress={"current": 25, "total": 100, "message": "Matching streams"},
    )

    status = manager.get_run_status()

    assert status["state"] == "running"
    assert status["stage"] == "stream_matching"
    assert status["stage_label"] == "Matching Streams"
    assert status["message"] == "Matching streams"
    assert status["progress"]["current"] == 25
    assert status["progress"]["total"] == 100
    assert status["progress"]["percent"] == 25
    assert status["forced"] is True
    assert status["forced_period_id"] == "period-1"


def test_stage_duration_updates_when_stage_changes():
    manager = AutomatedStreamManager()

    manager._start_run_status(forced=False, forced_period_id=None)
    manager._update_run_status(
        stage="m3u_refresh",
        stage_label="M3U Refresh",
        message="Refreshing playlists",
        progress={"current": 1, "total": 4, "message": "Refreshing playlists"},
    )

    status = manager.get_run_status()
    assert status["stage"] == "m3u_refresh"
    assert status["stage_started_at"] is not None
    assert status["stage_duration_seconds"] is not None

    manager._finish_run_status(
        state="completed",
        stage="completed",
        stage_label="Completed",
        message="Done",
    )

    status = manager.get_run_status()
    assert status["state"] == "completed"
    assert status["active"] is False
    assert status["status"] == "completed"
    assert status["message"] == "Done"
    assert status["stage_duration_seconds"] is not None


def test_udi_cache_sync_progress_tracks_streams_and_channels():
    manager = AutomatedStreamManager()
    manager._start_run_status(forced=True, forced_period_id="period-1")
    manager._update_run_status(
        stage="cache_sync",
        stage_label="Syncing Cache",
        message="Refreshing cache after playlist update",
    )

    observed = []
    udi = Mock()

    def refresh_streams():
        observed.append(manager.get_run_status()["progress"].copy())
        return True

    def refresh_channels():
        observed.append(manager.get_run_status()["progress"].copy())
        return True

    udi.refresh_streams.side_effect = refresh_streams
    udi.refresh_channels.side_effect = refresh_channels

    assert manager._sync_udi_cache_after_playlist_refresh(udi) is True

    assert observed[0] == {
        "current": 0,
        "total": 2,
        "percent": 0,
        "message": "Syncing stream cache",
    }
    assert observed[1] == {
        "current": 1,
        "total": 2,
        "percent": 50,
        "message": "Syncing channel cache",
    }

    status = manager.get_run_status()
    assert status["stage"] == "cache_sync"
    assert status["progress"] == {
        "current": 2,
        "total": 2,
        "percent": 100,
        "message": "Syncing channel cache completed",
    }
    cache_stage = next(stage for stage in status["stages"] if stage["key"] == "cache_sync")
    assert cache_stage["current"] == 2
    assert cache_stage["total"] == 2
    assert cache_stage["percent"] == 100


def test_udi_cache_sync_progress_reports_partial_warning():
    manager = AutomatedStreamManager()
    manager._start_run_status(forced=True, forced_period_id="period-1")
    manager._update_run_status(stage="cache_sync", stage_label="Syncing Cache")

    udi = Mock()
    udi.refresh_streams.return_value = True
    udi.refresh_channels.return_value = False

    assert manager._sync_udi_cache_after_playlist_refresh(udi) is False

    status = manager.get_run_status()
    assert status["progress"] == {
        "current": 2,
        "total": 2,
        "percent": 100,
        "message": "Syncing channel cache reported warnings",
    }


def test_automation_duration_start_variables_are_defined_before_use():
    source_path = (
        Path(__file__).resolve().parents[1]
        / "apps"
        / "automation"
        / "automated_stream_manager.py"
    )
    tree = ast.parse(source_path.read_text(encoding="utf-8"))

    manager_class = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "AutomatedStreamManager"
    )
    run_cycle = next(
        node
        for node in manager_class.body
        if isinstance(node, ast.FunctionDef) and node.name == "run_automation_cycle"
    )

    assignments = {}
    usages = []

    for node in ast.walk(run_cycle):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id.endswith("_started"):
                    assignments.setdefault(target.id, []).append(node.lineno)
        elif isinstance(node, ast.AnnAssign):
            target = node.target
            if isinstance(target, ast.Name) and target.id.endswith("_started"):
                assignments.setdefault(target.id, []).append(node.lineno)
        elif (
            isinstance(node, ast.BinOp)
            and isinstance(node.op, ast.Sub)
            and isinstance(node.right, ast.Name)
            and node.right.id.endswith("_started")
        ):
            usages.append((node.right.id, node.lineno))

    missing = [
        f"{name} used at line {line}"
        for name, line in usages
        if not any(assigned_line < line for assigned_line in assignments.get(name, []))
    ]

    assert missing == []


def test_automation_run_status_tracks_freeze_stream_counts():
    source_path = (
        Path(__file__).resolve().parents[1]
        / "apps"
        / "automation"
        / "automated_stream_manager.py"
    )
    source = source_path.read_text(encoding="utf-8")

    assert "'freeze_streams': 0" in source
    assert "freeze_streams_count += ch_freeze" in source
    assert "'freeze_streams_count': ch_freeze" in source
    assert '"freeze_streams": freeze_streams_count' in source
