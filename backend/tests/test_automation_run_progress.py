from apps.automation.automated_stream_manager import AutomatedStreamManager


def test_run_progress_tracks_active_stage():
    manager = AutomatedStreamManager()

    manager._start_run_status(forced=True, forced_period_id="period-1")
    manager._update_run_stage("matching", message="Matching streams", current=25, total=100)

    status = manager.get_run_status()

    assert status["active"] is True
    assert status["status"] == "running"
    assert status["stage"] == "matching"
    assert status["stage_label"] == "Matching"
    assert status["current"] == 25
    assert status["total"] == 100
    assert status["percent"] == 25
    assert status["forced"] is True
    assert status["forced_period_id"] == "period-1"
    assert status["stages"][0]["status"] == "completed"
    assert status["stages"][4]["status"] == "running"


def test_skipped_stage_keeps_run_active_until_finish():
    manager = AutomatedStreamManager()

    manager._start_run_status()
    manager._update_run_stage("m3u_refresh", status="skipped", message="No refresh needed")

    status = manager.get_run_status()
    assert status["active"] is True
    assert status["status"] == "running"
    assert status["stages"][2]["status"] == "skipped"

    manager._finish_run_status("completed", "Done")
    status = manager.get_run_status()
    assert status["active"] is False
    assert status["status"] == "completed"
    assert status["message"] == "Done"
