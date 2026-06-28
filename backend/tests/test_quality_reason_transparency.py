from apps.core.stream_stats_utils import is_stream_dead
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
