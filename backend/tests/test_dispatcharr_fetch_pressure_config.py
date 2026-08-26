import threading
from unittest.mock import Mock

import pytest


class FakeDB:
    def __init__(self, config=None):
        self.settings = {}
        if config is not None:
            self.settings["dispatcharr_config"] = config

    def get_system_setting(self, key, default=None):
        return self.settings.get(key, default)

    def set_system_setting(self, key, value):
        self.settings[key] = value
        return True


def test_dispatcharr_config_returns_default_fetch_pressure(monkeypatch):
    from apps.config import dispatcharr_config as config_module

    fake_db = FakeDB({})
    monkeypatch.setattr("apps.database.manager.get_db_manager", lambda: fake_db)

    config = config_module.DispatcharrConfig()

    assert config.get_stream_fetch_page_size() == 1000
    assert config.get_stream_fetch_max_workers() == 10
    assert config.get_config()["stream_fetch_page_size"] == 1000
    assert config.get_config()["stream_fetch_max_workers"] == 10


def test_dispatcharr_config_saves_bounded_fetch_pressure(monkeypatch):
    from apps.config import dispatcharr_config as config_module

    fake_db = FakeDB({})
    monkeypatch.setattr("apps.database.manager.get_db_manager", lambda: fake_db)

    config = config_module.DispatcharrConfig()
    assert config.update_config(stream_fetch_page_size=50000, stream_fetch_max_workers=0)

    saved = fake_db.settings["dispatcharr_config"]
    assert saved["stream_fetch_page_size"] == 10000
    assert saved["stream_fetch_max_workers"] == 1


def test_update_dispatcharr_config_accepts_fetch_pressure_fields():
    flask = pytest.importorskip("flask")
    from apps.api.dispatcharr_handlers import update_dispatcharr_config_response

    config_manager = Mock()
    config_manager.update_config.return_value = True
    config_manager.is_configured.return_value = False

    app = flask.Flask(__name__)
    with app.app_context():
        response = update_dispatcharr_config_response(
            payload={
                "base_url": "http://dispatcharr.test",
                "username": "user",
                "stream_fetch_page_size": 5000,
                "stream_fetch_max_workers": 2,
            },
            get_dispatcharr_config=lambda: config_manager,
            get_udi_manager=Mock(),
        )

    assert response.status_code == 200
    config_manager.update_config.assert_called_once_with(
        base_url="http://dispatcharr.test",
        auth_mode=None,
        username="user",
        password=None,
        api_key=None,
        stream_fetch_page_size=5000,
        stream_fetch_max_workers=2,
    )


def test_fetch_streams_uses_configured_fetch_pressure(monkeypatch):
    from apps.udi import fetcher as fetcher_module

    fetcher = fetcher_module.UDIFetcher.__new__(fetcher_module.UDIFetcher)
    fetcher.base_url = "http://dispatcharr.test"

    config = Mock()
    config.get_stream_fetch_page_size.return_value = 5000
    config.get_stream_fetch_max_workers.return_value = 2
    monkeypatch.setattr(fetcher_module, "get_dispatcharr_config", lambda: config)

    calls = {}

    def fake_fetch_paginated(url, page_size=1000, max_workers=10):
        calls["url"] = url
        calls["page_size"] = page_size
        calls["max_workers"] = max_workers
        return fetcher_module.FetchResult(items=[{"id": 1}], expected_count=1)

    monkeypatch.setattr(fetcher, "_fetch_paginated", fake_fetch_paginated)

    result = fetcher.fetch_streams()

    assert len(result) == 1
    assert calls == {
        "url": "http://dispatcharr.test/api/channels/streams/",
        "page_size": 5000,
        "max_workers": 2,
    }


def test_fetch_channels_requests_all_visibility_states(monkeypatch):
    from apps.udi import fetcher as fetcher_module

    fetcher = fetcher_module.UDIFetcher.__new__(fetcher_module.UDIFetcher)
    fetcher.base_url = "http://dispatcharr.test"

    calls = {}

    def fake_fetch_paginated(url, page_size=1000, max_workers=10):
        calls["url"] = url
        calls["page_size"] = page_size
        calls["max_workers"] = max_workers
        return fetcher_module.FetchResult(items=[{"id": 1}], expected_count=1)

    monkeypatch.setattr(fetcher, "_fetch_paginated", fake_fetch_paginated)

    result = fetcher.fetch_channels()

    assert len(result) == 1
    assert calls == {
        "url": "http://dispatcharr.test/api/channels/channels/?visibility_filter=all",
        "page_size": 1000,
        "max_workers": 10,
    }


def test_fetch_all_ids_requests_all_channel_visibility_states(monkeypatch):
    from apps.udi import fetcher as fetcher_module

    fetcher = fetcher_module.UDIFetcher.__new__(fetcher_module.UDIFetcher)
    fetcher.base_url = "http://dispatcharr.test"

    requested_urls = []

    def fake_fetch_url(url):
        requested_urls.append(url)
        if "channels/channels/ids" in url:
            return [1, 2]
        if "channels/streams/ids" in url:
            return [10]
        return []

    monkeypatch.setattr(fetcher, "_fetch_url", fake_fetch_url)

    result = fetcher.fetch_all_ids()

    assert result == {"channels": {1, 2}, "streams": {10}}
    assert "http://dispatcharr.test/api/channels/channels/ids/?visibility_filter=all" in requested_urls


def test_fetch_streams_forwards_progress_callback(monkeypatch):
    from apps.udi import fetcher as fetcher_module

    fetcher = fetcher_module.UDIFetcher.__new__(fetcher_module.UDIFetcher)
    fetcher.base_url = "http://dispatcharr.test"

    config = Mock()
    config.get_stream_fetch_page_size.return_value = 5000
    config.get_stream_fetch_max_workers.return_value = 2
    monkeypatch.setattr(fetcher_module, "get_dispatcharr_config", lambda: config)

    callback = Mock()
    calls = {}

    def fake_fetch_paginated(url, page_size=1000, max_workers=10, progress_callback=None):
        calls["url"] = url
        calls["page_size"] = page_size
        calls["max_workers"] = max_workers
        calls["progress_callback"] = progress_callback
        progress_callback({"completed_pages": 1, "total_pages": 2})
        return fetcher_module.FetchResult(items=[{"id": 1}], expected_count=1)

    monkeypatch.setattr(fetcher, "_fetch_paginated", fake_fetch_paginated)

    result = fetcher.fetch_streams(progress_callback=callback)

    assert len(result) == 1
    assert calls["url"] == "http://dispatcharr.test/api/channels/streams/"
    assert calls["page_size"] == 5000
    assert calls["max_workers"] == 2
    assert calls["progress_callback"] is callback
    callback.assert_called_once_with({"completed_pages": 1, "total_pages": 2})


def test_fetch_streams_forwards_cancel_check(monkeypatch):
    from apps.udi import fetcher as fetcher_module

    fetcher = fetcher_module.UDIFetcher.__new__(fetcher_module.UDIFetcher)
    fetcher.base_url = "http://dispatcharr.test"

    config = Mock()
    config.get_stream_fetch_page_size.return_value = 5000
    config.get_stream_fetch_max_workers.return_value = 2
    monkeypatch.setattr(fetcher_module, "get_dispatcharr_config", lambda: config)

    cancel_check = Mock(return_value=False)
    calls = {}

    def fake_fetch_paginated(
        url,
        page_size=1000,
        max_workers=10,
        progress_callback=None,
        cancel_check=None,
    ):
        calls["cancel_check"] = cancel_check
        return fetcher_module.FetchResult(items=[{"id": 1}], expected_count=1)

    monkeypatch.setattr(fetcher, "_fetch_paginated", fake_fetch_paginated)

    result = fetcher.fetch_streams(cancel_check=cancel_check)

    assert len(result) == 1
    assert calls["cancel_check"] is cancel_check


def test_fetch_paginated_cancel_check_stops_after_progress(monkeypatch):
    from apps.udi import fetcher as fetcher_module

    fetcher = fetcher_module.UDIFetcher.__new__(fetcher_module.UDIFetcher)
    fetcher.base_url = "http://dispatcharr.test"

    def fake_fetch_url(url):
        assert "page=1" in url
        return {"count": 2000, "results": [{"id": 1}]}

    monkeypatch.setattr(fetcher, "_fetch_url", fake_fetch_url)
    progress_calls = []

    def progress_callback(payload):
        progress_calls.append(payload)

    cancel_check = Mock(side_effect=[False, False, True])

    with pytest.raises(fetcher_module.FetchCancelled):
        fetcher._fetch_paginated(
            "http://dispatcharr.test/api/channels/streams/",
            page_size=1000,
            progress_callback=progress_callback,
            cancel_check=cancel_check,
        )

    assert progress_calls == [
        {
            "completed_pages": 1,
            "total_pages": 2,
            "items_fetched": 1,
            "expected_count": 2000,
        }
    ]


def test_fetch_url_retries_transient_timeout(monkeypatch):
    from apps.udi import fetcher as fetcher_module

    fetcher = fetcher_module.UDIFetcher.__new__(fetcher_module.UDIFetcher)
    fetcher.base_url = "http://dispatcharr.test"
    fetcher._request_timings = []
    fetcher._timing_lock = threading.Lock()

    class Response:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {"ok": True}

    calls = {"count": 0}

    def fake_get(url, headers=None, timeout=None):
        calls["count"] += 1
        if calls["count"] == 1:
            raise fetcher_module.requests.exceptions.ReadTimeout("slow page")
        return Response()

    monkeypatch.setattr(fetcher_module.requests, "get", fake_get)
    monkeypatch.setattr(fetcher_module, "_get_auth_headers", lambda: {"Authorization": "Bearer token"})
    monkeypatch.setattr(fetcher_module.time, "sleep", lambda seconds: None)

    result = fetcher._fetch_url("http://dispatcharr.test/api/channels/streams/?page=42")

    assert result == {"ok": True}
    assert calls["count"] == 2


def test_udi_integrity_fails_when_expected_count_is_missing():
    from apps.udi.fetcher import FetchResult
    from apps.udi.manager import _check_fetch_integrity

    result = FetchResult(items=[{"id": i} for i in range(95)], expected_count=100)

    assert _check_fetch_integrity("streams", result) is False
