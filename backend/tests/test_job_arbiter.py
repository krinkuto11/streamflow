from flask import Flask

from apps.api.job_arbiter_handlers import get_job_arbiter_status_response
from apps.orchestration.job_arbiter import build_job_arbiter_snapshot


class _Service:
    def __init__(self, status):
        self._status = status

    def get_status(self):
        return self._status


def test_full_run_blocks_single_channel_request():
    snapshot = build_job_arbiter_snapshot(
        automation_status={
            "run_status": {
                "active": True,
                "state": "running",
                "stage": "m3u_refresh",
                "stage_label": "M3U Refresh",
                "run_id": "automation-1",
            }
        }
    )

    assert snapshot["summary"]["busy"] is True
    assert snapshot["active_jobs"][0]["category"] == "full_run"
    assert snapshot["request_policies"]["single_channel"]["state"] == "blocked"
    assert snapshot["request_policies"]["single_channel"]["conflicts"][0]["category"] == "full_run"


def test_stream_checker_makes_event_checks_queueable():
    snapshot = build_job_arbiter_snapshot(
        stream_checker_status={
            "stream_checking_mode": True,
            "progress": {"is_single_channel_check": False, "percentage": 42},
            "queue": {"queue_size": 0, "in_progress": 2},
        }
    )

    assert snapshot["active_jobs"][0]["category"] == "stream_checker"
    assert snapshot["request_policies"]["full_run"]["state"] == "blocked"
    assert snapshot["request_policies"]["event_check"]["state"] == "queue"
    assert snapshot["request_policies"]["event_check"]["reason"] == "active_conflict_queueable"


def test_idle_shadow_monitor_is_visible_but_not_busy():
    snapshot = build_job_arbiter_snapshot(
        shadow_status={
            "running": True,
            "watched_count": 0,
            "recent_events": [],
        }
    )

    assert snapshot["active_jobs"][0]["category"] == "shadow_monitor"
    assert snapshot["active_jobs"][0]["active"] is False
    assert snapshot["summary"]["busy"] is False
    assert snapshot["request_policies"]["full_run"]["state"] == "allowed"


def test_job_arbiter_status_handler_returns_snapshot():
    app = Flask(__name__)
    with app.app_context():
        response, status_code = get_job_arbiter_status_response(
            get_automation_manager=lambda: _Service({}),
            get_stream_checker_service=lambda: _Service(
                {
                    "stream_checking_mode": True,
                    "progress": {"is_single_channel_check": True},
                    "queue": {"in_progress": 1},
                }
            ),
            get_shadow_monitor_service=lambda: _Service({"running": False}),
            get_teamarr_preflight_service=lambda: _Service({}),
        )

    payload = response.get_json()
    assert status_code == 200
    assert payload["active_jobs"][0]["category"] == "single_channel"
    assert payload["request_policies"]["full_run"]["state"] == "blocked"
