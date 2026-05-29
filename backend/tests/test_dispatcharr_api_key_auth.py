import threading
import time
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


def test_legacy_credentials_config_remains_configured(monkeypatch):
    from apps.config import dispatcharr_config as config_module

    fake_db = FakeDB({
        "base_url": "http://dispatcharr.test",
        "username": "admin",
        "password": "secret",
    })
    monkeypatch.setattr("apps.database.manager.get_db_manager", lambda: fake_db)

    config = config_module.DispatcharrConfig()

    assert config.get_auth_mode() == "credentials"
    assert config.is_configured() is True
    assert config.get_config()["auth_mode"] == "credentials"
    assert config.get_config()["has_password"] is True
    assert config.get_config()["has_api_key"] is False


def test_api_key_config_can_be_configured_without_username_password(monkeypatch):
    from apps.config import dispatcharr_config as config_module

    fake_db = FakeDB({
        "base_url": "http://dispatcharr.test",
        "auth_mode": "api_key",
        "api_key": "secret-key",
    })
    monkeypatch.setattr("apps.database.manager.get_db_manager", lambda: fake_db)

    config = config_module.DispatcharrConfig()

    assert config.get_auth_mode() == "api_key"
    assert config.is_configured() is True
    assert config.get_config()["username"] == ""
    assert config.get_config()["has_api_key"] is True


def test_api_key_only_legacy_config_selects_api_key_mode(monkeypatch):
    from apps.config import dispatcharr_config as config_module

    fake_db = FakeDB({
        "base_url": "http://dispatcharr.test",
        "api_key": "secret-key",
    })
    monkeypatch.setattr("apps.database.manager.get_db_manager", lambda: fake_db)

    config = config_module.DispatcharrConfig()

    assert config.get_auth_mode() == "api_key"
    assert config.is_configured() is True


def test_auth_headers_keep_existing_bearer_flow(monkeypatch):
    from apps.core import auth

    config = Mock()
    config.get_auth_mode.return_value = "credentials"
    monkeypatch.setattr(auth, "get_dispatcharr_config", lambda: config)
    monkeypatch.setattr(auth.os, "getenv", lambda key: "saved-token" if key == "DISPATCHARR_TOKEN" else None)

    headers = auth._get_auth_headers()

    assert headers["Authorization"] == "Bearer saved-token"
    assert "X-API-Key" not in headers


def test_auth_headers_use_dispatcharr_api_key(monkeypatch):
    from apps.core import auth

    config = Mock()
    config.get_auth_mode.return_value = "api_key"
    config.get_api_key.return_value = "secret-key"
    monkeypatch.setattr(auth, "get_dispatcharr_config", lambda: config)

    headers = auth._get_auth_headers()

    assert headers["Authorization"] == "ApiKey secret-key"
    assert headers["X-API-Key"] == "secret-key"


def test_auth_headers_serializes_initial_credentials_login(monkeypatch):
    from apps.core import auth

    monkeypatch.delenv("DISPATCHARR_TOKEN", raising=False)

    config = Mock()
    config.get_auth_mode.return_value = "credentials"
    monkeypatch.setattr(auth, "get_dispatcharr_config", lambda: config)

    login_calls = 0
    calls_lock = threading.Lock()

    def fake_login():
        nonlocal login_calls
        with calls_lock:
            login_calls += 1
        time.sleep(0.05)
        auth.os.environ["DISPATCHARR_TOKEN"] = "new-token"
        return True

    monkeypatch.setattr(auth, "_login", fake_login)

    barrier = threading.Barrier(6)
    results = []
    errors = []
    results_lock = threading.Lock()

    def worker():
        try:
            barrier.wait(timeout=2)
            headers = auth._get_auth_headers()
            with results_lock:
                results.append(headers["Authorization"])
        except Exception as exc:
            with results_lock:
                errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=3)

    assert not errors
    assert results == ["Bearer new-token"] * 6
    assert login_calls == 1


def test_update_config_preserves_saved_secret_fields():
    flask = pytest.importorskip("flask")
    from apps.api.dispatcharr_handlers import update_dispatcharr_config_response

    config_manager = Mock()
    config_manager.update_config.return_value = True
    config_manager.is_configured.return_value = False
    config_manager.get_password.return_value = "saved-password"
    config_manager.get_api_key.return_value = "saved-key"

    app = flask.Flask(__name__)
    with app.app_context():
        response = update_dispatcharr_config_response(
            payload={
                "base_url": "http://dispatcharr.test",
                "auth_mode": "api_key",
                "api_key": "",
                "password": "",
            },
            get_dispatcharr_config=lambda: config_manager,
            get_udi_manager=Mock(),
        )

    assert response.status_code == 200
    config_manager.update_config.assert_called_once_with(
        base_url="http://dispatcharr.test",
        auth_mode="api_key",
        username=None,
        password=None,
        api_key=None,
        stream_fetch_page_size=None,
        stream_fetch_max_workers=None,
    )


def test_connection_test_accepts_api_key(monkeypatch):
    flask = pytest.importorskip("flask")
    from apps.api import dispatcharr_handlers

    captured = {}

    class Response:
        status_code = 200

        def raise_for_status(self):
            return None

    def fake_get(url, headers=None, params=None, timeout=None):
        captured["url"] = url
        captured["headers"] = headers
        captured["params"] = params
        captured["timeout"] = timeout
        return Response()

    monkeypatch.setattr(dispatcharr_handlers.requests, "get", fake_get)

    config_manager = Mock()
    config_manager.get_base_url.return_value = None
    config_manager.get_auth_mode.return_value = "credentials"
    config_manager.get_api_key.return_value = None

    app = flask.Flask(__name__)
    with app.app_context():
        response = dispatcharr_handlers.test_dispatcharr_connection_response(
            payload={
                "base_url": "http://dispatcharr.test",
                "auth_mode": "api_key",
                "api_key": "secret-key",
            },
            get_dispatcharr_config=lambda: config_manager,
        )

    assert response.status_code == 200
    assert captured["url"] == "http://dispatcharr.test/api/channels/channels/"
    assert captured["headers"]["Authorization"] == "ApiKey secret-key"
    assert captured["headers"]["X-API-Key"] == "secret-key"
    assert captured["params"] == {"page_size": 1}


def test_connection_test_does_not_expose_unexpected_api_key_exception(monkeypatch):
    flask = pytest.importorskip("flask")
    from apps.api import dispatcharr_handlers

    def fake_get(*args, **kwargs):
        raise RuntimeError("internal stack detail with sensitive path")

    monkeypatch.setattr(dispatcharr_handlers.requests, "get", fake_get)

    config_manager = Mock()

    app = flask.Flask(__name__)
    with app.app_context():
        response = dispatcharr_handlers.test_dispatcharr_connection_response(
            payload={
                "base_url": "http://dispatcharr.test",
                "auth_mode": "api_key",
                "api_key": "secret-key",
            },
            get_dispatcharr_config=lambda: config_manager,
        )

    response_obj, status_code = response
    assert status_code == 400
    body = response_obj.get_json()
    assert body["success"] is False
    assert body["error"] == "Connection failed. Please check the URL and credentials."
    assert "internal stack detail" not in body["error"]


def test_connection_test_does_not_expose_unexpected_credential_exception(monkeypatch):
    flask = pytest.importorskip("flask")
    from apps.api import dispatcharr_handlers

    def fake_post(*args, **kwargs):
        raise RuntimeError("internal login detail with sensitive path")

    monkeypatch.setattr(dispatcharr_handlers.requests, "post", fake_post)

    config_manager = Mock()

    app = flask.Flask(__name__)
    with app.app_context():
        response = dispatcharr_handlers.test_dispatcharr_connection_response(
            payload={
                "base_url": "http://dispatcharr.test",
                "auth_mode": "credentials",
                "username": "admin",
                "password": "secret",
            },
            get_dispatcharr_config=lambda: config_manager,
        )

    response_obj, status_code = response
    assert status_code == 400
    body = response_obj.get_json()
    assert body["success"] is False
    assert body["error"] == "Connection failed. Please check the URL and credentials."
    assert "internal login detail" not in body["error"]


def test_udi_connection_test_validates_api_key_headers(monkeypatch):
    from apps.udi import fetcher

    config = Mock()
    config.get_auth_mode.return_value = "api_key"
    config.get_api_key.return_value = "secret-key"
    monkeypatch.setattr(fetcher, "get_dispatcharr_config", lambda: config)
    monkeypatch.setattr(fetcher, "_get_base_url", lambda: "http://dispatcharr.test")

    monkeypatch.setattr(
        fetcher,
        "_get_auth_headers",
        lambda: {
            "Authorization": "ApiKey secret-key",
            "X-API-Key": "secret-key",
            "Accept": "application/json",
        },
    )
    validate_headers = Mock(return_value=True)
    validate_token = Mock(return_value=False)
    monkeypatch.setattr(fetcher, "_validate_auth_headers", validate_headers)
    monkeypatch.setattr(fetcher, "_validate_token", validate_token)

    assert fetcher.UDIFetcher().test_connection() is True
    validate_headers.assert_called_once()
    validate_token.assert_not_called()


def test_udi_fetcher_refreshes_base_url_after_setup_config_save(monkeypatch):
    from apps.udi import fetcher

    base_url = {"value": None}

    monkeypatch.setattr(fetcher, "_get_base_url", lambda: base_url["value"])
    monkeypatch.setattr(
        fetcher,
        "_get_auth_headers",
        lambda: {
            "Authorization": "ApiKey secret-key",
            "X-API-Key": "secret-key",
            "Accept": "application/json",
        },
    )
    validate_headers = Mock(return_value=True)
    monkeypatch.setattr(fetcher, "_validate_auth_headers", validate_headers)

    udi_fetcher = fetcher.UDIFetcher()
    assert udi_fetcher.base_url is None

    base_url["value"] = "http://dispatcharr.test"

    assert udi_fetcher.test_connection() is True
    assert udi_fetcher.base_url == "http://dispatcharr.test"
    validate_headers.assert_called_once()
