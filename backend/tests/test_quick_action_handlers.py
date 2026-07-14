from unittest.mock import Mock

import pytest
from flask import Flask

from apps.api.quick_action_handlers import refresh_playlist_response
from apps.automation.automated_stream_manager import RefreshResult


@pytest.fixture
def app():
    return Flask(__name__)


def test_refresh_playlist_forwards_requested_account_id(app):
    manager = Mock()
    manager.refresh_playlists.return_value = (True, [{"id": 17}])

    with app.app_context():
        response = refresh_playlist_response(
            payload={"account_id": 17},
            get_automation_manager=lambda: manager,
        )

    assert response.status_code == 200
    assert response.get_json() == {"message": "Playlist refresh request accepted"}
    manager.refresh_playlists.assert_called_once_with(force=True, account_id=17)


def test_refresh_playlist_propagates_failed_refresh_outcome(app):
    manager = Mock()
    manager.refresh_playlists.return_value = RefreshResult(
        False,
        [],
        failed_refresh_requests=[{"id": 17, "reason": "account_unavailable"}],
        outcome="failed",
    )

    with app.app_context():
        response, status = refresh_playlist_response(
            payload={"account_id": 17},
            get_automation_manager=lambda: manager,
        )

    assert status == 500
    assert response.get_json() == {
        "error": "Playlist refresh request was not accepted",
        "outcome": "failed",
    }
    manager.refresh_playlists.assert_called_once_with(force=True, account_id=17)


@pytest.mark.parametrize("payload", [None, {}, {"account_id": None}])
def test_refresh_playlist_without_account_id_keeps_all_accounts_behavior(app, payload):
    manager = Mock()
    manager.refresh_playlists.return_value = (True, [])

    with app.app_context():
        response = refresh_playlist_response(
            payload=payload,
            get_automation_manager=lambda: manager,
        )

    assert response.status_code == 200
    manager.refresh_playlists.assert_called_once_with(force=True)


def test_refresh_playlist_global_failure_keeps_legacy_response_contract(app):
    manager = Mock()
    manager.refresh_playlists.return_value = RefreshResult(False, [], outcome="failed")

    with app.app_context():
        response, status = refresh_playlist_response(
            payload={},
            get_automation_manager=lambda: manager,
        )

    assert status == 500
    assert response.get_json() == {"error": "Playlist refresh failed"}
    manager.refresh_playlists.assert_called_once_with(force=True)


@pytest.mark.parametrize("account_id", [True, 0, -1, "17", [], {}])
def test_refresh_playlist_rejects_invalid_account_id_without_refreshing(app, account_id):
    get_automation_manager = Mock()

    with app.app_context():
        response, status = refresh_playlist_response(
            payload={"account_id": account_id},
            get_automation_manager=get_automation_manager,
        )

    assert status == 400
    assert response.get_json() == {"error": "account_id must be a positive integer"}
    get_automation_manager.assert_not_called()


@pytest.mark.parametrize("payload", [[17], "17", 17, True])
def test_refresh_playlist_rejects_non_object_payload_without_refreshing(app, payload):
    get_automation_manager = Mock()

    with app.app_context():
        response, status = refresh_playlist_response(
            payload=payload,
            get_automation_manager=get_automation_manager,
        )

    assert status == 400
    assert response.get_json() == {"error": "Request body must be a valid JSON object"}
    get_automation_manager.assert_not_called()


@pytest.fixture
def refresh_route(monkeypatch):
    from apps.api import web_api

    manager = Mock()
    manager.refresh_playlists.return_value = (True, [])
    monkeypatch.setattr(web_api, "get_automation_manager", lambda: manager)
    monkeypatch.setitem(web_api.app.config, "TESTING", True)

    with web_api.app.test_client() as client:
        yield client, manager


@pytest.mark.parametrize(
    "request_kwargs",
    [
        {},
        {"data": b"", "content_type": "application/json"},
        {"json": {}},
    ],
)
def test_refresh_playlist_route_keeps_empty_request_as_global_refresh(
    refresh_route,
    request_kwargs,
):
    client, manager = refresh_route

    response = client.post("/api/refresh-playlist", **request_kwargs)

    assert response.status_code == 200
    assert response.get_json() == {"message": "Playlist refresh request accepted"}
    manager.refresh_playlists.assert_called_once_with(force=True)


def test_refresh_playlist_route_forwards_target_account(refresh_route):
    client, manager = refresh_route

    response = client.post("/api/refresh-playlist", json={"account_id": 17})

    assert response.status_code == 200
    manager.refresh_playlists.assert_called_once_with(force=True, account_id=17)


@pytest.mark.parametrize(
    ("body", "content_type"),
    [
        ('{"account_id":', "application/json"),
        ("null", "application/json"),
        ("[]", "application/json"),
        ('"17"', "application/json"),
        ("account_id=17", "text/plain"),
    ],
)
def test_refresh_playlist_route_rejects_malformed_or_non_object_body(
    refresh_route,
    body,
    content_type,
):
    client, manager = refresh_route

    response = client.post(
        "/api/refresh-playlist",
        data=body,
        content_type=content_type,
    )

    assert response.status_code == 400
    assert response.get_json() == {
        "error": "Request body must be a valid JSON object",
    }
    manager.refresh_playlists.assert_not_called()


def test_refresh_playlist_route_propagates_unavailable_target_failure(refresh_route):
    client, manager = refresh_route
    manager.refresh_playlists.return_value = RefreshResult(
        False,
        [],
        failed_refresh_requests=[{"id": 17, "reason": "account_unavailable"}],
        outcome="failed",
    )

    response = client.post("/api/refresh-playlist", json={"account_id": 17})

    assert response.status_code == 500
    assert response.get_json() == {
        "error": "Playlist refresh request was not accepted",
        "outcome": "failed",
    }
    manager.refresh_playlists.assert_called_once_with(force=True, account_id=17)
