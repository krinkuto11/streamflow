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
    assert status["stage_duration_seconds"] is not None
