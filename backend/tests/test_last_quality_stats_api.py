from datetime import datetime, timedelta, timezone

from apps.database import connection
from apps.database.models import Run, Stream, StreamTelemetry
from apps.telemetry.last_quality_stats import get_last_quality_stats


def _add_run(session, *, timestamp, run_type="automation_full_run"):
    run = Run(
        timestamp=timestamp,
        duration_seconds=10.0,
        total_channels=1,
        total_streams=1,
        run_type=run_type,
    )
    session.add(run)
    session.flush()
    return run


def _add_stream(session, stream_id=683989, *, is_stale=False):
    stream = Stream(
        id=stream_id,
        name=f"Stream {stream_id}",
        url=f"http://example.test/{stream_id}.m3u8",
        m3u_account_id=21,
        is_stale=is_stale,
    )
    session.add(stream)
    return stream


def test_last_quality_stats_reports_never_measured_without_probe():
    payload = get_last_quality_stats(683989, session_factory=connection.get_session)

    assert payload == {
        "stream_id": 683989,
        "measured": False,
        "recheck_required": True,
        "reason": "never_measured",
    }


def test_last_quality_stats_returns_latest_reusable_measurement():
    session = connection.get_session()
    try:
        _add_stream(session)
        old_run = _add_run(session, timestamp=datetime(2026, 6, 7, 10, 0, tzinfo=timezone.utc))
        new_run = _add_run(session, timestamp=datetime(2026, 6, 7, 12, 0, tzinfo=timezone.utc))
        session.add_all(
            [
                StreamTelemetry(
                    run_id=old_run.id,
                    channel_id=1914,
                    provider_id=21,
                    stream_id=683989,
                    bitrate_kbps=8000,
                    resolution_width=1920,
                    resolution_height=1080,
                    fps=25.0,
                    codec="h264",
                    is_hdr=False,
                ),
                StreamTelemetry(
                    run_id=new_run.id,
                    channel_id=1914,
                    provider_id=21,
                    stream_id=683989,
                    bitrate_kbps=14645,
                    resolution_width=3840,
                    resolution_height=2160,
                    fps=50.0,
                    codec="hevc",
                    audio_codec="aac",
                    is_hdr=True,
                ),
            ]
        )
        session.commit()
        new_run_id = new_run.id
    finally:
        session.close()

    payload = get_last_quality_stats(683989, session_factory=connection.get_session)

    assert payload["measured"] is True
    assert payload["recheck_required"] is False
    assert payload["last_run_id"] == new_run_id
    assert payload["run_type"] == "automation_full_run"
    assert payload["channel_id"] == 1914
    assert payload["provider_id"] == 21
    assert payload["resolution"] == "3840x2160"
    assert payload["width"] == 3840
    assert payload["height"] == 2160
    assert payload["fps"] == 50.0
    assert payload["codec"] == "hevc"
    assert payload["audio_codec"] == "aac"
    assert payload["bitrate_kbps"] == 14645
    assert payload["hdr"] is True
    assert payload["status"] == "completed"
    assert payload["quality_reason"] == "none"
    assert payload["source"] == "stream_telemetry"
    assert payload["stale"] is False


def test_last_quality_stats_latest_unusable_result_requires_recheck():
    session = connection.get_session()
    try:
        _add_stream(session)
        old_run = _add_run(session, timestamp=datetime(2026, 6, 7, 10, 0, tzinfo=timezone.utc))
        new_run = _add_run(
            session,
            timestamp=datetime(2026, 6, 7, 12, 0, tzinfo=timezone.utc),
            run_type="single_channel_check",
        )
        new_run.raw_details = (
            '{"stream_details":[{"stream_id":683989,"status":"freeze",'
            '"quality_reason_detail":"freeze_detected"}]}'
        )
        session.add_all(
            [
                StreamTelemetry(
                    run_id=old_run.id,
                    channel_id=1914,
                    provider_id=21,
                    stream_id=683989,
                    bitrate_kbps=14645,
                    resolution_width=3840,
                    resolution_height=2160,
                    fps=50.0,
                    codec="hevc",
                    is_dead=False,
                ),
                StreamTelemetry(
                    run_id=new_run.id,
                    channel_id=1914,
                    provider_id=21,
                    stream_id=683989,
                    is_dead=True,
                ),
            ]
        )
        session.commit()
        new_run_id = new_run.id
    finally:
        session.close()

    payload = get_last_quality_stats(683989, session_factory=connection.get_session)

    assert payload["measured"] is False
    assert payload["recheck_required"] is True
    assert payload["reason"] == "last_result_not_reusable"
    assert payload["last_run_id"] == new_run_id
    assert payload["last_status"] == "freeze"
    assert payload["quality_reason"] == "freeze_detected"


def test_last_quality_stats_rejects_zero_resolution():
    session = connection.get_session()
    try:
        _add_stream(session)
        run = _add_run(session, timestamp=datetime(2026, 6, 7, 12, 0, tzinfo=timezone.utc))
        session.add(
            StreamTelemetry(
                run_id=run.id,
                channel_id=1914,
                provider_id=21,
                stream_id=683989,
                resolution_width=0,
                resolution_height=0,
                fps=50.0,
                codec="hevc",
            )
        )
        session.commit()
    finally:
        session.close()

    payload = get_last_quality_stats(683989, session_factory=connection.get_session)

    assert payload["measured"] is False
    assert payload["recheck_required"] is True
    assert payload["reason"] == "last_result_not_reusable"
    assert payload["last_status"] == "0x0"


def test_last_quality_stats_missing_bitrate_requires_recheck_without_dead_reason():
    session = connection.get_session()
    try:
        _add_stream(session)
        run = _add_run(session, timestamp=datetime(2026, 6, 7, 12, 0, tzinfo=timezone.utc))
        session.add(
            StreamTelemetry(
                run_id=run.id,
                channel_id=1914,
                provider_id=21,
                stream_id=683989,
                bitrate_kbps=None,
                resolution_width=3840,
                resolution_height=2160,
                fps=50.0,
                codec="hevc",
                is_dead=False,
            )
        )
        session.commit()
    finally:
        session.close()

    payload = get_last_quality_stats(683989, session_factory=connection.get_session)

    assert payload["measured"] is False
    assert payload["recheck_required"] is True
    assert payload["reason"] == "missing_bitrate"
    assert payload["last_status"] == "incomplete_bitrate"
    assert payload["quality_reason"] == "none"


def test_last_quality_stats_marks_current_stream_stale():
    session = connection.get_session()
    try:
        _add_stream(session, is_stale=True)
        run = _add_run(session, timestamp=datetime(2026, 6, 7, 12, 0, tzinfo=timezone.utc))
        session.add(
            StreamTelemetry(
                run_id=run.id,
                channel_id=1914,
                provider_id=21,
                stream_id=683989,
                bitrate_kbps=12000,
                resolution_width=3840,
                resolution_height=2160,
                fps=50.0,
                codec="hevc",
            )
        )
        session.commit()
    finally:
        session.close()

    payload = get_last_quality_stats(683989, session_factory=connection.get_session)

    assert payload["measured"] is True
    assert payload["stale"] is True


def test_last_quality_stats_route_does_not_touch_stream_checker(monkeypatch):
    import apps.api.web_api as web_api

    session = connection.get_session()
    try:
        _add_stream(session)
        run = _add_run(session, timestamp=datetime.now(timezone.utc) - timedelta(minutes=5))
        session.add(
            StreamTelemetry(
                run_id=run.id,
                channel_id=1914,
                provider_id=21,
                stream_id=683989,
                bitrate_kbps=5000,
                resolution_width=1920,
                resolution_height=1080,
                fps=25.0,
                codec="h264",
            )
        )
        session.commit()
    finally:
        session.close()

    def fail_if_called():
        raise AssertionError("last-quality-stats must not probe or touch StreamChecker")

    monkeypatch.setattr(web_api, "get_stream_checker_service", fail_if_called)

    with web_api.app.test_client() as client:
        response = client.get("/api/stream-checker/streams/683989/last-quality-stats")

    assert response.status_code == 200
    assert response.get_json()["measured"] is True
