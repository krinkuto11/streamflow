from unittest.mock import Mock

import pytest
from flask import Flask

from apps.api.quick_action_handlers import refresh_playlist_response


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
    assert response.get_json() == {"error": "Request body must be a JSON object"}
    get_automation_manager.assert_not_called()
