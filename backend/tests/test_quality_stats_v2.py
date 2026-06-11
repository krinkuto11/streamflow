from flask import Flask

from apps.api.quality_stats_v2_handlers import (
    get_quality_stats_v2_provider_response,
    get_quality_stats_v2_stream_response,
    post_quality_stats_v2_bulk_response,
)
from apps.stream.quality_stats_v2 import build_provider_quality_stats, build_stream_quality_stats


class _Args(dict):
    def get(self, key, default=None, type=None):
        value = super().get(key, default)
        if type is not None and value is not None:
            return type(value)
        return value


class _Udi:
    def __init__(self, streams):
        self._streams = streams

    def get_stream_by_id(self, stream_id):
        return next((stream for stream in self._streams if stream.get("id") == stream_id), None)

    def get_streams(self, log_result=True):
        return self._streams


def test_stream_quality_stats_marks_measured_uhd_over_name_hint():
    result = build_stream_quality_stats(
        {
            "id": 10,
            "name": "Provider 4K Label",
            "m3u_account": 7,
            "stream_stats": {
                "resolution": "3840x2160",
                "source_fps": 50,
                "ffmpeg_output_bitrate": 12000,
                "video_codec": "hevc",
                "hdr_format": "hdr10",
            },
        }
    )

    assert result["version"] == 2
    assert result["provider_id"] == 7
    assert result["markers"]["quality_bucket"] == "uhd"
    assert result["markers"]["measured_uhd"] is True
    assert result["markers"]["name_uhd_hint"] is True
    assert result["markers"]["uhd_confidence"] == "measured"
    assert result["markers"]["hdr"] is True


def test_provider_quality_stats_aggregates_buckets():
    streams = [
        {"id": 1, "name": "A", "m3u_account": 7, "stream_stats": {"resolution": "3840x2160"}},
        {"id": 2, "name": "B", "m3u_account": 7, "stream_stats": {"resolution": "1920x1080"}},
        {"id": 3, "name": "C", "m3u_account": 8, "stream_stats": {"resolution": "0x0"}},
    ]

    result = build_provider_quality_stats(streams, 7)

    assert result["provider_id"] == 7
    assert result["summary"]["total_streams"] == 2
    assert result["summary"]["uhd_streams"] == 1
    assert result["summary"]["buckets"] == {"uhd": 1, "fhd": 1}


def test_quality_stats_v2_handlers_return_stream_provider_and_bulk_payloads():
    app = Flask(__name__)
    streams = [
        {"id": 1, "name": "One UHD", "m3u_account": 7, "stream_stats": {"resolution": "3840x2160"}},
        {"id": 2, "name": "Two HD", "m3u_account": 7, "stream_stats": {"resolution": "1280x720"}},
        {"id": 3, "name": "Three", "m3u_account": 8, "stream_stats": {"resolution": "0x0"}},
    ]
    udi = _Udi(streams)

    with app.app_context():
        stream_response = get_quality_stats_v2_stream_response(
            stream_id=1,
            get_udi_manager=lambda: udi,
        )
        provider_response = get_quality_stats_v2_provider_response(
            provider_id=7,
            request_args=_Args({"limit": "1"}),
            get_udi_manager=lambda: udi,
        )
        bulk_response = post_quality_stats_v2_bulk_response(
            payload={"stream_ids": [1, 3], "provider_ids": [7], "provider_limit": 2},
            get_udi_manager=lambda: udi,
        )

    assert stream_response.get_json()["markers"]["measured_uhd"] is True
    provider_payload = provider_response.get_json()
    assert provider_payload["summary"]["total_streams"] == 2
    assert len(provider_payload["streams"]) == 1
    assert provider_payload["truncated"] is True

    bulk_payload = bulk_response.get_json()
    assert len(bulk_payload["streams"]) == 2
    assert bulk_payload["providers"]["7"]["summary"]["total_streams"] == 2
