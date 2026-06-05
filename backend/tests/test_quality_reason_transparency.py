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
