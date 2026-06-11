from flask import Flask
from werkzeug.datastructures import MultiDict

from apps.api.telemetry_handlers import export_changelog_run_response, get_changelog_response
from apps.database.models import Run
from apps.telemetry.run_classification import classify_run_metadata
from apps.telemetry.telemetry_db import get_session, save_generic_telemetry


def test_classify_single_channel_metadata():
    metadata = classify_run_metadata(
        "single_channel_check",
        {
            "channel_id": 123,
            "success": True,
            "run_id": "single-123",
        },
    )

    assert metadata == {
        "job_category": "single_channel",
        "job_outcome": "completed",
        "job_subject_ref": "channel:123",
        "job_correlation_id": "single-123",
    }


def test_classify_degraded_provider_refresh():
    metadata = classify_run_metadata(
        "playlist_refresh",
        {
            "provider_id": 7,
            "failed_refresh_requests": 1,
        },
    )

    assert metadata["job_category"] == "provider_refresh"
    assert metadata["job_outcome"] == "completed_degraded"
    assert metadata["job_subject_ref"] == "provider:7"


def test_successful_refresh_with_failed_requests_is_degraded():
    metadata = classify_run_metadata(
        "playlist_refresh",
        {
            "success": True,
            "failed_refresh_requests": 1,
        },
    )

    assert metadata["job_outcome"] == "completed_degraded"


def test_save_generic_telemetry_persists_v6_job_fields():
    save_generic_telemetry(
        "single_channel_check",
        {
            "channel_id": 456,
            "total_streams": 5,
            "dead_streams": 1,
            "success": True,
            "run_id": "single-456",
        },
        subentries=[{"group": "check", "items": []}],
    )

    session = get_session()
    try:
        run = session.query(Run).one()
        assert run.run_type == "single_channel_check"
        assert run.job_category == "single_channel"
        assert run.job_outcome == "completed"
        assert run.job_subject_ref == "channel:456"
        assert run.job_correlation_id == "single-456"
    finally:
        session.close()


def test_changelog_response_filters_by_v6_job_category():
    save_generic_telemetry(
        "single_channel_check",
        {"channel_id": 1, "success": True},
        subentries=[{"group": "check", "items": []}],
    )
    save_generic_telemetry(
        "playlist_refresh",
        {"provider_id": 2, "failed_refresh_requests": 1},
    )

    app = Flask(__name__)
    with app.app_context():
        response = get_changelog_response(
            request_args=MultiDict({"days": "7", "job_category": "provider_refresh"})
        )

    payload = response.get_json()
    assert payload["total"] == 1
    assert payload["data"][0]["action"] == "playlist_refresh"
    assert payload["data"][0]["job_category"] == "provider_refresh"
    assert payload["data"][0]["job_outcome"] == "completed_degraded"
    assert payload["data"][0]["job_subject_ref"] == "provider:2"
    assert payload["data"][0]["id"] is not None


def test_changelog_run_export_is_scoped_to_single_run():
    save_generic_telemetry(
        "single_channel_check",
        {
            "channel_id": 10,
            "channel_name": "Fixture A",
            "success": True,
            "checked_streams": [
                {
                    "stream_id": 100,
                    "stream_name": "Good Stream",
                    "status": "completed",
                    "resolution": "1920x1080",
                }
            ],
            "skipped_streams": [
                {
                    "stream_id": 101,
                    "stream_name": "Watched Stream",
                    "skip_reason": "active_viewer_protected",
                }
            ],
        },
    )
    save_generic_telemetry(
        "single_channel_check",
        {
            "channel_id": 20,
            "channel_name": "Fixture B",
            "success": True,
            "checked_streams": [{"stream_id": 200, "stream_name": "Other Run"}],
        },
    )

    session = get_session()
    try:
        run = session.query(Run).filter(Run.job_subject_ref == "channel:10").one()
        run_id = run.id
    finally:
        session.close()

    app = Flask(__name__)
    with app.app_context():
        response = export_changelog_run_response(
            run_id=run_id,
            request_args=MultiDict({"format": "json", "include_url": "false"}),
        )

    payload = response.get_json()
    assert payload["total_stream_rows"] == 2
    assert {row["stream_id"] for row in payload["streams"]} == {100, 101}
    skipped = next(row for row in payload["streams"] if row["stream_id"] == 101)
    assert skipped["bucket"] == "skipped"
    assert skipped["reason"] == "active_viewer_protected"


def test_changelog_run_export_dead_scope_enriches_reasons_and_profiles():
    save_generic_telemetry(
        "single_channel_check",
        {
            "channel_id": 30,
            "channel_name": "Fixture C",
            "success": True,
            "profile_id": "7",
            "profile_name": "Teamarr Event Preflight",
            "stream_details": [
                {
                    "stream_id": 300,
                    "stream_name": "Good Stream",
                    "status": "completed",
                    "resolution": "1920x1080",
                    "fps": 50,
                    "bitrate": "6000 kbps",
                    "score": 0.91,
                },
                {
                    "stream_id": 301,
                    "stream_name": "Blank Stream",
                    "status": "blank",
                    "quality_reason": "blank",
                    "blank_detected": True,
                    "m3u_account": "Provider One",
                },
                {
                    "stream_id": 302,
                    "stream_name": "Zero Metrics Stream",
                    "resolution": "0x0",
                    "fps": "N/A",
                    "bitrate": "N/A",
                    "score": 0,
                    "m3u_account": "Provider One",
                },
                {
                    "stream_id": 303,
                    "stream_name": "Protected Viewer Stream",
                    "status": "viewer_preempted",
                    "reason": "viewer_preempted",
                    "resolution": "1920x1080",
                    "fps": 50,
                    "bitrate": "6000 kbps",
                },
                {
                    "stream_id": 304,
                    "stream_name": "Legacy Freeze Stream",
                    "freeze_detected": True,
                    "resolution": "1920x1080",
                    "fps": 50,
                    "bitrate": "6000 kbps",
                    "score": 0.2,
                },
            ],
        },
    )

    session = get_session()
    try:
        run = session.query(Run).filter(Run.job_subject_ref == "channel:30").one()
        run_id = run.id
    finally:
        session.close()

    app = Flask(__name__)
    with app.app_context():
        response = export_changelog_run_response(
            run_id=run_id,
            request_args=MultiDict({"format": "json", "scope": "dead", "include_url": "false"}),
        )

    payload = response.get_json()
    assert payload["scope"] == "dead"
    assert payload["total_stream_rows"] == 3
    assert "profile_id" in payload["fields"]
    assert "profile_name" in payload["fields"]

    rows = {row["stream_id"]: row for row in payload["streams"]}
    assert set(rows) == {301, 302, 304}
    assert rows[301]["profile_id"] == "7"
    assert rows[301]["profile_name"] == "Teamarr Event Preflight"
    assert rows[301]["provider_id"] is None
    assert rows[301]["provider_name"] == "Provider One"
    assert rows[301]["reason"] == "blank"
    assert rows[302]["status"] == "low_quality"
    assert rows[302]["reason"] == "low_quality"
    assert rows[302]["reason_detail"] == "inferred_from_run_metrics"
    assert rows[304]["status"] == "freeze"
    assert rows[304]["reason"] == "freeze"
