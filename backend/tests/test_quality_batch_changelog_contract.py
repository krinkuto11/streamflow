import threading
import json
from copy import deepcopy
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock, patch

from apps.api.telemetry_handlers import (
    _extract_changelog_run_stream_rows,
    _render_changelog_run_export,
)
from apps.stream.stream_checker_service import StreamCheckerService


def _service_with_changelog():
    service = StreamCheckerService.__new__(StreamCheckerService)
    service.batch_lock = threading.Lock()
    service.batch_start_time = datetime.now().isoformat()
    service.batch_changelog_entries = []
    service.changelog = Mock()
    return service


def _mixed_stream_stats():
    return [
        {
            "stream_id": 1,
            "status": "completed",
            "bitrate": "8.0 Mbps",
            "visual_probe_ran": True,
            "visual_probe_completed": True,
            "visual_probe_incomplete": False,
            "visual_probe_requested_duration_seconds": 5,
            "visual_probe_minimum_duration_seconds": 10,
            "visual_probe_duration_seconds": 10,
            "visual_probe_duration_adjusted": True,
            "visual_probe_duration_adjustment_reason": "detector_minimum_window",
        },
        {"stream_id": 2, "status": "dead", "reason": "offline"},
        {"stream_id": 3, "status": "blank", "blank_detected": True},
        {"stream_id": 4, "status": "freeze", "freeze_detected": True},
        {"stream_id": 5, "status": "revived", "bitrate": "5.0 Mbps"},
        {
            "stream_id": 6,
            "status": "incomplete_bitrate",
            "measurement_incomplete_reason": "missing_bitrate",
        },
        {"stream_id": 7, "status": "low_quality"},
        *[
            {"stream_id": stream_id, "status": "completed", "bitrate": "4.0 Mbps"}
            for stream_id in range(8, 15)
        ],
    ]


def test_canonical_batch_entry_keeps_all_streams_and_derives_counts():
    service = _service_with_changelog()
    stream_stats = _mixed_stream_stats()
    expected = deepcopy(stream_stats)

    entry = service._build_batch_changelog_entry(
        channel_id=42,
        channel_name="Quality Contract",
        logo_url="/api/logos/42",
        total_streams=len(stream_stats),
        stream_stats=stream_stats,
        averages={
            "avg_resolution": "1920x1080",
            "avg_bitrate": "6.0 Mbps",
            "avg_fps": "50 fps",
        },
        skipped_streams=[{"id": 99, "reason": "active_viewer"}],
        channel_visibility={"action": "hidden", "changed": True},
    )

    stream_stats[0]["status"] = "mutated_after_build"

    assert "stream_details" not in entry
    assert entry["stream_stats"] == expected
    assert len(entry["stream_stats"]) == 14
    assert entry["streams_analyzed"] == 14
    assert entry["dead_streams_detected"] == 4
    assert entry["blank_streams_detected"] == 1
    assert entry["freeze_streams_detected"] == 1
    assert entry["streams_revived"] == 1
    assert entry["incomplete_bitrate_streams"] == 1
    assert entry["channel_visibility"]["action"] == "hidden"


def test_batch_finalizer_persists_complete_details_and_matching_totals():
    service = _service_with_changelog()
    stream_stats = _mixed_stream_stats()
    entry = service._build_batch_changelog_entry(
        channel_id=42,
        channel_name="Quality Contract",
        logo_url=None,
        total_streams=len(stream_stats),
        stream_stats=stream_stats,
        averages={},
    )
    service.batch_changelog_entries = [entry]

    service._finalize_batch_changelog()

    service.changelog.add_entry.assert_called_once()
    payload = service.changelog.add_entry.call_args.kwargs
    details = payload["details"]
    channel_stats = payload["subentries"][0]["items"][0]["stats"]

    assert payload["action"] == "batch_stream_check"
    assert details["streams_analyzed"] == len(stream_stats)
    assert details["dead_streams"] == 4
    assert details["blank_streams"] == 1
    assert details["freeze_streams"] == 1
    assert details["streams_revived"] == 1
    assert details["incomplete_bitrate_streams"] == 1
    assert channel_stats["stream_details"] == stream_stats
    assert len(channel_stats["stream_details"]) == 14
    assert channel_stats["streams_analyzed"] == 14
    assert channel_stats["dead_streams"] == 4
    assert channel_stats["incomplete_bitrate_streams"] == 1
    assert service.batch_start_time is None
    assert service.batch_changelog_entries == []


def test_batch_finalizer_serialized_json_export_keeps_every_stream_row():
    service = _service_with_changelog()
    stream_stats = _mixed_stream_stats()
    service.batch_changelog_entries = [service._build_batch_changelog_entry(
        channel_id=42,
        channel_name="Quality Contract",
        logo_url=None,
        total_streams=len(stream_stats),
        stream_stats=stream_stats,
        averages={},
    )]

    service._finalize_batch_changelog()

    finalizer_payload = service.changelog.add_entry.call_args.kwargs
    persisted_details = json.loads(json.dumps(finalizer_payload["details"]))
    persisted_subentries = json.loads(json.dumps(finalizer_payload["subentries"]))
    run = SimpleNamespace(
        id=77,
        timestamp=datetime.now(),
        run_type="batch_stream_check",
        job_category="automation",
        job_outcome="completed",
    )
    with patch(
        "apps.api.telemetry_handlers._provider_reference_context",
        return_value={},
    ):
        rows = _extract_changelog_run_stream_rows(
            run,
            persisted_details,
            persisted_subentries,
        )
    content, extension, mimetype = _render_changelog_run_export(
        rows,
        export_format="json",
        include_url=False,
    )

    payload = json.loads(content)
    assert extension == "json"
    assert mimetype.startswith("application/json")
    assert payload["total_stream_rows"] == len(stream_stats)
    assert len(payload["streams"]) == 14
    assert {row["stream_id"] for row in payload["streams"]} == set(range(1, 15))
    statuses = {row["stream_id"]: row["status"] for row in payload["streams"]}
    visual = next(row for row in payload["streams"] if row["stream_id"] == 1)
    assert visual["visual_probe_completed"] is True
    assert visual["visual_probe_requested_duration_seconds"] == 5
    assert visual["visual_probe_minimum_duration_seconds"] == 10
    assert visual["visual_probe_duration_seconds"] == 10
    assert visual["visual_probe_duration_adjusted"] is True
    assert statuses[2] == "dead"
    assert statuses[3] == "blank"
    assert statuses[4] == "freeze"
    assert statuses[5] == "revived"
    assert statuses[6] == "incomplete_bitrate"
