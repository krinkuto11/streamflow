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
