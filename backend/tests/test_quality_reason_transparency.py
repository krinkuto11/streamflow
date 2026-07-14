from pathlib import Path

from apps.core.stream_stats_utils import (
    extract_stream_stats,
    format_stream_stats_for_display,
    is_stream_dead,
)
from apps.stream.stream_checker_service import StreamCheckerService


def test_low_quality_result_carries_machine_readable_threshold_detail():
    result = is_stream_dead(
        {
            "resolution": "1280x720",
            "fps": 25,
            "bitrate_kbps": 742.4,
        },
        {"min_bitrate_kbps": 1500},
    )

    assert result == (True, "low_quality")
    assert result.reason_detail == "bitrate_below_threshold"
    assert result.details == {"actual": 742.4, "threshold": 1500}


def test_timeout_result_carries_machine_readable_analysis_context():
    result = is_stream_dead(
        {
            "status": "Timeout",
            "resolution": "0x0",
            "elapsed_time": 65,
            "timeout_seconds": 65,
            "operation_timeout_seconds": 30,
            "ffmpeg_duration_seconds": 30,
            "startup_buffer_seconds": 5,
            "attempt": 2,
            "max_attempts": 2,
            "stage": "stream analysis",
        },
        {"min_bitrate_kbps": 1500},
    )

    assert result == (True, "offline")
    assert result.reason_detail == "stream_timeout"
    assert result.details == {
        "elapsed_seconds": 65,
        "timeout_seconds": 65,
        "operation_timeout_seconds": 30,
        "ffmpeg_duration_seconds": 30,
        "startup_buffer_seconds": 5,
        "attempt": 2,
        "max_attempts": 2,
        "stage": "stream analysis",
    }


def test_quality_reason_fields_are_prepared_for_stream_stats_payload():
    service = object.__new__(StreamCheckerService)
    stream_data = {
        "stream_id": 42,
        "resolution": "1280x720",
        "fps": 25,
        "video_codec": "h264",
        "audio_codec": "aac",
        "bitrate_kbps": 742.4,
    }

    result = is_stream_dead(stream_data, {"min_bitrate_kbps": 1500})
    service._apply_quality_classification(stream_data, result)
    payload = service._prepare_stream_stats_for_batch(stream_data)

    assert payload["stream_stats"]["quality_reason"] == "low_quality"
    assert payload["stream_stats"]["quality_reason_detail"] == "bitrate_below_threshold"
    assert payload["stream_stats"]["quality_reason_context"] == {
        "actual": 742.4,
        "threshold": 1500,
    }


def test_missing_bitrate_incomplete_fields_are_prepared_for_stream_stats_payload():
    service = object.__new__(StreamCheckerService)
    stream_data = {
        "stream_id": 42,
        "resolution": "3840x2160",
        "fps": 50,
        "video_codec": "hevc",
        "audio_codec": "aac",
        "bitrate_kbps": None,
        "bitrate_source": "ffprobe_media_fallback_no_bitrate",
        "quality_reason": "none",
        "quality_reason_detail": "none",
        "quality_reason_context": {},
        "measurement_incomplete": True,
        "measurement_incomplete_reason": "missing_bitrate",
        "measurement_incomplete_context": {
            "bitrate_source": "ffprobe_media_fallback_no_bitrate",
        },
        "bitrate_recheck_required": True,
    }

    payload = service._prepare_stream_stats_for_batch(stream_data)

    assert "ffmpeg_output_bitrate" not in payload["stream_stats"]
    assert payload["stream_stats"]["measurement_incomplete"] is True
    assert payload["stream_stats"]["measurement_incomplete_reason"] == "missing_bitrate"
    assert payload["stream_stats"]["measurement_incomplete_context"] == {
        "bitrate_source": "ffprobe_media_fallback_no_bitrate",
    }
    assert payload["stream_stats"]["bitrate_recheck_required"] is True


def test_incomplete_bitrate_status_overrides_clean_completed_reason():
    target = {
        "status": "completed",
        "quality_reason": "none",
        "quality_reason_detail": "none",
        "quality_reason_context": {},
    }
    source = {
        "bitrate_kbps": None,
        "measurement_incomplete": True,
        "measurement_incomplete_reason": "missing_bitrate",
        "measurement_incomplete_context": {
            "bitrate_source": "ffprobe_media_fallback_no_bitrate",
        },
        "bitrate_recheck_required": True,
    }

    StreamCheckerService._apply_incomplete_bitrate_status(target, source)

    assert target["status"] == "incomplete_bitrate"
    assert target["reason_detail"] == "missing_bitrate"
    assert target["quality_reason"] == "missing_bitrate"
    assert target["quality_reason_detail"] == "missing_bitrate"
    assert target["quality_reason_context"] == {
        "bitrate_source": "ffprobe_media_fallback_no_bitrate",
    }
    assert target["measurement_incomplete"] is True
    assert target["bitrate_recheck_required"] is True


def test_incomplete_bitrate_counts_as_playable_but_not_clean_completed():
    result = {
        "checked_streams": [
            {"status": "completed", "quality_reason_detail": "none"},
            {
                "status": "incomplete_bitrate",
                "quality_reason_detail": "missing_bitrate",
                "measurement_incomplete_reason": "missing_bitrate",
            },
            {"status": "completed", "quality_reason_detail": "bitrate_below_threshold"},
            {"status": "completed", "blank_detected": True},
            {"status": "dead"},
        ],
    }

    assert StreamCheckerService._count_good_checked_streams(result) == 2


def test_incomplete_bitrate_can_reuse_previous_value_for_scoring_without_persisting_null():
    service = object.__new__(StreamCheckerService)
    service.config = {"dead_stream_handling": {}}
    stream_data = {
        "stream_id": 42,
        "resolution": "3840x2160",
        "fps": 60,
        "video_codec": "hevc",
        "audio_codec": "aac",
        "bitrate_kbps": None,
        "measurement_incomplete": True,
        "measurement_incomplete_reason": "missing_bitrate",
        "measurement_incomplete_context": {},
        "bitrate_recheck_required": True,
        "visual_probe_ran": True,
        "visual_probe_completed": True,
        "visual_probe_incomplete": False,
        "visual_probe_requested_duration_seconds": 5,
        "visual_probe_minimum_duration_seconds": 10,
        "visual_probe_duration_seconds": 10,
        "visual_probe_duration_adjusted": True,
        "visual_probe_duration_adjustment_reason": "detector_minimum_window",
    }
    existing_stream = {
        "stream_stats": {
            "ffmpeg_output_bitrate": 12000,
        }
    }

    service._apply_previous_bitrate_fallback(stream_data, existing_stream)
    score = service._calculate_stream_score(stream_data)
    payload = service._prepare_stream_stats_for_batch(stream_data)

    assert stream_data["scoring_bitrate_kbps"] == 12000
    assert stream_data["bitrate_preserved_from_previous_measurement"] is True
    assert score == 1.0
    assert "ffmpeg_output_bitrate" not in payload["stream_stats"]
    assert payload["stream_stats"]["measurement_incomplete"] is True
    assert payload["stream_stats"]["bitrate_recheck_required"] is True
    assert payload["stream_stats"]["visual_probe_ran"] is True
    assert payload["stream_stats"]["visual_probe_completed"] is True
    assert payload["stream_stats"]["visual_probe_requested_duration_seconds"] == 5
    assert payload["stream_stats"]["visual_probe_minimum_duration_seconds"] == 10
    assert payload["stream_stats"]["visual_probe_duration_seconds"] == 10
    assert payload["stream_stats"]["visual_probe_duration_adjusted"] is True
    assert (
        payload["stream_stats"]["visual_probe_duration_adjustment_reason"]
        == "detector_minimum_window"
    )


def test_single_channel_details_do_not_mix_cached_bitrate_with_current_missing_bitrate():
    cached_stream = {
        "id": 42,
        "stream_stats": {
            "resolution": "3840x2160",
            "source_fps": 59.9,
            "ffmpeg_output_bitrate": 19300,
            "video_codec": "hevc",
        },
    }
    analyzed = {
        "stream_id": 42,
        "resolution": "3840x2160",
        "fps": 59.9,
        "bitrate_kbps": None,
        "video_codec": "hevc",
        "measurement_incomplete": True,
        "measurement_incomplete_reason": "missing_bitrate",
        "bitrate_recheck_required": True,
    }

    cached_display = format_stream_stats_for_display(extract_stream_stats(cached_stream))
    current_display = format_stream_stats_for_display(
        extract_stream_stats(
            StreamCheckerService._current_probe_stats_source(cached_stream, analyzed)
        )
    )

    assert cached_display["bitrate"] == "19.3 Mbps"
    assert current_display["bitrate"] == "N/A"
    assert StreamCheckerService._has_incomplete_bitrate_measurement(analyzed) is True


def test_single_channel_details_use_current_probe_bitrate_when_present():
    cached_stream = {
        "id": 42,
        "stream_stats": {
            "resolution": "3840x2160",
            "source_fps": 59.9,
            "ffmpeg_output_bitrate": 3000,
            "video_codec": "hevc",
        },
    }
    analyzed = {
        "stream_id": 42,
        "resolution": "3840x2160",
        "fps": 59.9,
        "bitrate_kbps": 19300,
        "video_codec": "hevc",
        "measurement_incomplete": False,
        "measurement_incomplete_reason": "none",
        "bitrate_recheck_required": False,
    }

    current_display = format_stream_stats_for_display(
        extract_stream_stats(
            StreamCheckerService._current_probe_stats_source(cached_stream, analyzed)
        )
    )

    assert current_display["bitrate"] == "19.3 Mbps"
    assert StreamCheckerService._has_incomplete_bitrate_measurement(analyzed) is False


def test_deferred_bitrate_rechecks_are_serial_and_preserve_visual_evidence():
    service = object.__new__(StreamCheckerService)
    results = [
        {
            "stream_id": 1,
            "stream_name": "Recover",
            "status": "OK",
            "bitrate_kbps": None,
            "measurement_incomplete": True,
            "measurement_incomplete_reason": "missing_bitrate",
            "measurement_incomplete_context": {},
            "bitrate_recheck_required": True,
            "blank_probe_ran": True,
            "blank_detected": False,
            "freeze_probe_ran": True,
            "freeze_detected": False,
            "scoring_bitrate_kbps": 12000,
            "bitrate_preserved_from_previous_measurement": True,
        },
        {
            "stream_id": 2,
            "stream_name": "Still missing",
            "status": "OK",
            "bitrate_kbps": None,
            "measurement_incomplete": True,
            "measurement_incomplete_reason": "missing_bitrate",
            "measurement_incomplete_context": {"attempts": 1},
            "bitrate_recheck_required": True,
            "blank_probe_ran": True,
            "blank_detected": False,
        },
        {
            "stream_id": 3,
            "stream_name": "Already complete",
            "status": "OK",
            "bitrate_kbps": 4000,
            "measurement_incomplete": False,
            "measurement_incomplete_reason": "none",
            "bitrate_recheck_required": False,
        },
    ]
    streams = {
        1: {"id": 1, "name": "Recover"},
        2: {"id": 2, "name": "Still missing"},
        3: {"id": 3, "name": "Already complete"},
    }
    calls = []
    active = 0
    max_active = 0

    def recheck(stream, _initial):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        calls.append(stream["id"])
        try:
            if stream["id"] == 1:
                return {
                    "status": "OK",
                    "bitrate_kbps": 17870.0,
                    "bitrate_source": "ffmpeg_progress",
                    "elapsed_time": 30.2,
                    # A basis-only retry must not replace visual evidence.
                    "blank_probe_ran": False,
                    "blank_detected": True,
                    "freeze_probe_ran": False,
                    "freeze_detected": True,
                }
            return {
                "status": "OK",
                "bitrate_kbps": None,
                "bitrate_source": None,
                "elapsed_time": 30.1,
            }
        finally:
            active -= 1

    service._run_deferred_bitrate_rechecks(results, streams, recheck)

    assert calls == [1, 2]
    assert max_active == 1
    assert results[0]["bitrate_kbps"] == 17870.0
    assert results[0]["bitrate_source"] == "ffmpeg_progress"
    assert results[0]["measurement_incomplete"] is False
    assert results[0]["bitrate_recheck_required"] is False
    assert results[0]["bitrate_recheck_attempted"] is True
    assert results[0]["bitrate_recheck_outcome"] == "recovered"
    assert "scoring_bitrate_kbps" not in results[0]
    assert results[0]["blank_probe_ran"] is True
    assert results[0]["blank_detected"] is False
    assert results[0]["freeze_probe_ran"] is True
    assert results[0]["freeze_detected"] is False

    assert results[1]["status"] == "OK"
    assert results[1]["bitrate_kbps"] is None
    assert results[1]["measurement_incomplete"] is True
    assert results[1]["measurement_incomplete_reason"] == "missing_bitrate_after_recheck"
    assert results[1]["bitrate_recheck_required"] is True
    assert results[1]["bitrate_recheck_attempted"] is True
    assert results[1]["bitrate_recheck_outcome"] == "unavailable"
    assert results[1]["blank_probe_ran"] is True
    assert results[1]["blank_detected"] is False


def test_provider_capacity_does_not_claim_bitrate_recheck_was_attempted():
    service = object.__new__(StreamCheckerService)
    initial = {
        "stream_id": 9,
        "status": "OK",
        "bitrate_kbps": None,
        "measurement_incomplete": True,
        "measurement_incomplete_reason": "missing_bitrate",
        "measurement_incomplete_context": {},
        "bitrate_recheck_required": True,
    }

    outcome = service._merge_deferred_bitrate_recheck(
        initial,
        {
            "provider_limit_skipped": True,
            "skipped_reason": "quota_consumed_by_active_viewers",
        },
    )

    assert outcome == "provider_capacity_unavailable"
    assert initial["bitrate_recheck_attempted"] is False
    assert initial["bitrate_recheck_outcome"] == "provider_capacity_unavailable"
    assert initial["measurement_incomplete_reason"] == "missing_bitrate"
    assert initial["bitrate_recheck_required"] is True


def test_cached_provider_capacity_skip_is_not_scheduled_for_deferred_recheck():
    service = object.__new__(StreamCheckerService)
    skipped = {
        "stream_id": 9,
        "status": "SKIPPED_PROVIDER_LIMIT",
        "provider_limit_skipped": True,
        "bitrate_kbps": None,
        "measurement_incomplete": True,
        "measurement_incomplete_reason": "missing_bitrate",
        "bitrate_recheck_required": True,
    }
    called = False

    def recheck(_stream, _initial):
        nonlocal called
        called = True
        return None

    service._run_deferred_bitrate_rechecks(
        [skipped],
        {9: {"id": 9}},
        recheck,
    )

    assert called is False
    assert "bitrate_recheck_attempted" not in skipped


def test_incomplete_bitrate_status_exposes_exhausted_recheck_reason():
    target = {}
    source = {
        "bitrate_kbps": None,
        "measurement_incomplete": True,
        "measurement_incomplete_reason": "missing_bitrate_after_recheck",
        "measurement_incomplete_context": {"bitrate_recheck_outcome": "unavailable"},
        "bitrate_recheck_required": True,
        "bitrate_recheck_attempted": True,
        "bitrate_recheck_outcome": "unavailable",
    }

    StreamCheckerService._apply_incomplete_bitrate_status(target, source)

    assert target["status"] == "incomplete_bitrate"
    assert target["reason_detail"] == "missing_bitrate_after_recheck"
    assert target["quality_reason_detail"] == "missing_bitrate_after_recheck"
    assert target["bitrate_recheck_attempted"] is True
    assert target["bitrate_recheck_outcome"] == "unavailable"


def test_playable_exhausted_bitrate_recheck_persists_explicit_quality_reason():
    service = object.__new__(StreamCheckerService)
    stream_data = {
        "stream_id": 42,
        "status": "OK",
        "resolution": "1920x1080",
        "fps": 50,
        "video_codec": "h264",
        "audio_codec": "aac",
        "bitrate_kbps": None,
        "measurement_incomplete": True,
        "measurement_incomplete_reason": "missing_bitrate_after_recheck",
        "measurement_incomplete_context": {
            "bitrate_recheck_outcome": "unavailable",
        },
        "bitrate_recheck_required": True,
        "bitrate_recheck_attempted": True,
        "bitrate_recheck_outcome": "unavailable",
    }

    result = is_stream_dead(stream_data, {"min_bitrate_kbps": 0})
    assert result == (False, "none")

    service._apply_quality_classification(stream_data, result)
    payload = service._prepare_stream_stats_for_batch(stream_data)

    assert "dead_reason" not in stream_data
    assert payload["stream_stats"]["quality_reason"] == "missing_bitrate_after_recheck"
    assert payload["stream_stats"]["quality_reason_detail"] == "missing_bitrate_after_recheck"
    assert payload["stream_stats"]["quality_reason_context"] == {
        "bitrate_recheck_outcome": "unavailable",
    }


def test_bitrate_recheck_outcome_is_persisted_and_cleared_when_not_needed():
    service = object.__new__(StreamCheckerService)
    recovered = service._prepare_stream_stats_for_batch({
        "stream_id": 42,
        "bitrate_kbps": 17870,
        "measurement_incomplete": False,
        "measurement_incomplete_reason": "none",
        "bitrate_recheck_required": False,
        "bitrate_recheck_attempted": True,
        "bitrate_recheck_outcome": "recovered",
    })
    normal = service._prepare_stream_stats_for_batch({
        "stream_id": 43,
        "bitrate_kbps": 4000,
        "measurement_incomplete": False,
        "measurement_incomplete_reason": "none",
        "bitrate_recheck_required": False,
    })

    assert recovered["stream_stats"]["bitrate_recheck_attempted"] is True
    assert recovered["stream_stats"]["bitrate_recheck_outcome"] == "recovered"
    assert normal["stream_stats"]["bitrate_recheck_attempted"] is False
    assert normal["stream_stats"]["bitrate_recheck_outcome"] == "not_needed"


def test_recovered_bitrate_recheck_evidence_reaches_all_report_rows():
    service = object.__new__(StreamCheckerService)
    target = {"status": "completed"}
    source = {
        "measurement_incomplete": False,
        "measurement_incomplete_reason": "none",
        "measurement_incomplete_context": {},
        "bitrate_recheck_required": False,
        "bitrate_recheck_attempted": True,
        "bitrate_recheck_outcome": "recovered",
    }

    service._copy_bitrate_recheck_report_fields(target, source)

    assert target["bitrate_recheck_attempted"] is True
    assert target["bitrate_recheck_outcome"] == "recovered"
    assert target["bitrate_recheck_required"] is False
