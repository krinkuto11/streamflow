import json
from unittest.mock import Mock, patch

from flask import Flask

from apps.api.telemetry_handlers import (
    _render_dead_stream_export,
    export_dead_streams_response,
)


app = Flask(__name__)


def _dead_rows():
    return {
        "http://provider-a.example/dead-2.m3u8": {
            "stream_id": 2,
            "stream_name": "Second Stream",
            "channel_id": 20,
            "reason": "offline",
            "marked_dead_at": "2026-06-11T10:00:00",
        },
        "http://provider-b.example/dead-1.m3u8": {
            "stream_id": 1,
            "stream_name": "First Stream",
            "channel_id": 10,
            "reason": "low_quality",
            "marked_dead_at": "2026-06-11T09:00:00",
        },
    }


def _fake_db():
    db = Mock()
    db.get_dead_streams.return_value = _dead_rows()
    return db


def test_dead_stream_export_defaults_to_pipe_delimited_text_with_filters():
    provider_context = {
        1: {"provider_id": 8, "provider_name": "Provider B"},
        2: {"provider_id": 7, "provider_name": "Provider A"},
    }
    with app.app_context(), \
        patch("apps.database.manager.get_db_manager", return_value=_fake_db()), \
        patch("apps.api.telemetry_handlers._provider_context_by_stream_id", return_value=provider_context):
        response = export_dead_streams_response(
            request_args={
                "reason": "offline",
                "provider_id": "7",
                "sort_by": "stream_id",
                "sort_dir": "asc",
            }
        )

    assert response.status_code == 200
    assert response.headers["X-Dead-Streams-Count"] == "1"
    assert response.headers["X-Dead-Streams-Format"] == "txt"
    assert "attachment; filename=\"dead-streams-" in response.headers["Content-Disposition"]
    lines = response.get_data(as_text=True).splitlines()
    assert lines[0] == "stream_id|channel_id|provider_id|provider_name|reason|marked_dead_at|stream_name|url"
    assert lines[1] == (
        "2|20|7|Provider A|offline|2026-06-11T10:00:00|Second Stream|"
        "http://provider-a.example/dead-2.m3u8"
    )


def test_dead_stream_export_json_can_omit_urls():
    provider_context = {
        1: {"provider_id": 8, "provider_name": "Provider B"},
        2: {"provider_id": 7, "provider_name": "Provider A"},
    }
    with app.app_context(), \
        patch("apps.database.manager.get_db_manager", return_value=_fake_db()), \
        patch("apps.api.telemetry_handlers._provider_context_by_stream_id", return_value=provider_context):
        response = export_dead_streams_response(
            request_args={
                "format": "json",
                "include_url": "false",
                "provider_name": "provider b",
            }
        )

    assert response.status_code == 200
    payload = json.loads(response.get_data(as_text=True))
    assert payload["format"] == "json"
    assert payload["total_dead_streams"] == 1
    assert "url" not in payload["fields"]
    assert payload["dead_streams"][0]["stream_id"] == 1
    assert payload["dead_streams"][0]["provider_name"] == "Provider B"
    assert "url" not in payload["dead_streams"][0]


def test_dead_stream_export_rejects_unknown_format():
    with app.app_context():
        response, status = export_dead_streams_response(request_args={"format": "xlsx"})

    assert status == 400
    payload = response.get_json()
    assert payload["error"] == "Unsupported dead-stream export format"
    assert "json" in payload["supported_formats"]


def test_pipe_export_escapes_delimiter_and_newlines():
    content, extension, mimetype = _render_dead_stream_export(
        [{
            "stream_id": 3,
            "channel_id": 30,
            "provider_id": 9,
            "provider_name": "Provider|C",
            "reason": "offline",
            "marked_dead_at": "2026-06-11T11:00:00",
            "stream_name": "Line\r\nBreak",
            "url": "http://example.test/stream.m3u8",
        }],
        export_format="txt",
        generated_at="2026-06-11T11:01:00+00:00",
    )

    assert extension == "txt"
    assert mimetype.startswith("text/plain")
    assert "Provider\\|C" in content
    assert "Line  Break" in content
