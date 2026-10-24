"""A readable channel list must not stand in for admin capability."""
from unittest.mock import Mock

import pytest
from flask import Flask

from apps.api.dispatcharr_handlers import test_dispatcharr_connection_response as connection_response
from apps.core import auth
from apps.core.dispatcharr_access import DispatcharrAccessError, require_admin_user
from apps.stream.connectivity_guard import StreamConnectivityGuard


@pytest.mark.parametrize("payload", [None, [], {}, {"user_level": True}, {"user_level": "10"}])
def test_unreadable_role_is_not_an_administrator(payload):
    with pytest.raises(DispatcharrAccessError, match="Could not verify"):
        require_admin_user(payload)


@pytest.mark.parametrize("mode", ["api_key", "credentials"])
@pytest.mark.parametrize("level,expected", [(0, 403), (1, 403), (10, 200), (20, 200)])
def test_connection_test_requires_admin_in_both_auth_modes(monkeypatch, mode, level, expected):
    from apps.api import dispatcharr_handlers
    user_response = Mock(status_code=200)
    user_response.json.return_value = {"user_level": level, "is_superuser": True}
    get = Mock(return_value=user_response)
    monkeypatch.setattr(dispatcharr_handlers.requests, "get", get)
    token_response = Mock()
    token_response.json.return_value = {"access": "test-token"}
    monkeypatch.setattr(dispatcharr_handlers.requests, "post", Mock(return_value=token_response))
    config = Mock()
    with Flask(__name__).app_context():
        result = connection_response(
            payload={"base_url": "http://dispatcharr.test/", "auth_mode": mode,
                     "api_key": "test-key", "username": "test", "password": "test-password"},
            get_dispatcharr_config=lambda: config,
        )
        response, status = result if isinstance(result, tuple) else (result, result.status_code)
        assert status == expected
        if expected == 403:
            assert response.get_json()["code"] == "dispatcharr_admin_required"
            assert "administrator" in response.get_json()["error"]
    assert get.call_args.args[0] == "http://dispatcharr.test/api/accounts/users/me/"


def test_token_validation_does_not_cache_standard_user_as_valid(monkeypatch):
    monkeypatch.setattr(auth, "_get_base_url", lambda: "http://dispatcharr.test")
    response = Mock(status_code=200)
    response.json.return_value = {"user_level": 1}
    monkeypatch.setattr(auth.requests, "get", Mock(return_value=response))
    auth._clear_token_validation_cache()
    assert not auth._validate_auth_headers({"X-API-Key": "test-key"})
    assert not auth._token_validation_cache


def test_quality_preflight_rejects_standard_user_without_retry_or_writes():
    response = Mock(status_code=200)
    response.json.return_value = {"user_level": 1}
    transport = Mock()
    transport.get.return_value = response
    resolver = Mock()
    resolver.getaddrinfo.return_value = []
    result = StreamConnectivityGuard(requests_module=transport, socket_module=resolver).check(
        config={"require_internet": False}, dispatcharr_base_url="http://dispatcharr.test",
        dispatcharr_headers_provider=lambda: {"X-API-Key": "test-key"},
    )
    assert not result.ok
    assert result.reason == "dispatcharr_admin_required"
    assert transport.get.call_count == 1
    transport.post.assert_not_called()
    transport.patch.assert_not_called()
