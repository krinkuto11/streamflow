from unittest.mock import Mock

from flask import Flask

from apps.api import teamarr_preflight_handlers


def test_stop_response_reports_stopping_without_claiming_success():
    app = Flask(__name__)
    service = Mock()
    service.stop.return_value = False
    service.get_status.return_value = {
        "running": True,
        "stopping": True,
        "scan_status": {"state": "stopping", "pending_requests": 1},
    }

    with app.app_context():
        response, status_code = teamarr_preflight_handlers.stop_teamarr_preflight_response(
            get_service=lambda: service,
        )

    assert status_code == 202
    assert response.get_json()["success"] is False
    assert response.get_json()["stopping"] is True
    assert response.get_json()["status"]["scan_status"]["state"] == "stopping"


def test_start_response_rejects_restart_while_previous_scan_is_stopping():
    app = Flask(__name__)
    service = Mock()
    service.start.return_value = False
    service.get_status.return_value = {"running": False, "stopping": True}

    with app.app_context():
        response, status_code = teamarr_preflight_handlers.start_teamarr_preflight_response(
            get_service=lambda: service,
        )

    assert status_code == 409
    assert response.get_json()["success"] is False
    assert response.get_json()["code"] == "preflight_stopping"


def test_run_once_response_reports_scan_conflict():
    app = Flask(__name__)
    service = Mock()
    service.run_once.return_value = {
        "success": False,
        "error": "Teamarr preflight scan is already running",
        "code": "scan_in_progress",
    }

    with app.app_context():
        response, status_code = teamarr_preflight_handlers.run_teamarr_preflight_once_response(
            get_service=lambda: service,
        )

    assert status_code == 409
    assert response.get_json()["code"] == "scan_in_progress"
