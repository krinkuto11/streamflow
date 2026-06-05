from unittest.mock import Mock

from flask import Flask

from apps.api import scheduling_handlers


class ImmediateThread:
    def __init__(self, target, daemon=False):
        self.target = target
        self.daemon = daemon

    def start(self):
        self.target()


def test_create_auto_create_rule_matches_once_in_background(monkeypatch):
    app = Flask(__name__)

    class Service:
        def __init__(self):
            self.create_kwargs = None
            self.match_calls = []

        def create_auto_create_rule(self, rule_data, **kwargs):
            self.create_kwargs = kwargs
            return {
                "id": "rule-1",
                "name": rule_data["name"],
                "channel_ids": rule_data["channel_ids"],
                "regex_pattern": rule_data["regex_pattern"],
            }

        def match_programs_to_rules(self, **kwargs):
            self.match_calls.append(kwargs)
            return {"created": 2, "updated": 0, "skipped": 0}

    service = Service()
    wake = Mock()
    monkeypatch.setattr(scheduling_handlers.threading, "Thread", ImmediateThread)

    with app.app_context():
        response, status = scheduling_handlers.create_auto_create_rule_response(
            payload={
                "name": "Live MLB",
                "channel_ids": [10, 11],
                "regex_pattern": "^Live: MLB$",
            },
            get_scheduling_service=lambda: service,
            scheduled_event_processor_wake=wake,
        )

    assert status == 201
    assert response.get_json()["id"] == "rule-1"
    assert service.create_kwargs == {"match_immediately": False}
    assert service.match_calls == [{"force_refresh": True}]
    wake.set.assert_called_once_with()


def test_auto_create_regex_test_uses_rule_wide_preview_for_single_channel():
    app = Flask(__name__)
    service = Mock()
    service.test_regex_against_epg_for_rule.return_value = {
        "matches": 1,
        "programs": [{"title": "Live: MLB"}],
        "channels_tested": 1,
        "channels_with_matches": 1,
    }

    with app.app_context():
        response = scheduling_handlers.test_auto_create_rule_response(
            payload={
                "channel_id": 10,
                "regex_pattern": "^Live: MLB$",
            },
            get_scheduling_service=lambda: service,
        )

    assert response.status_code == 200
    assert response.get_json()["channels_tested"] == 1
    service.test_regex_against_epg_for_rule.assert_called_once_with(
        channel_ids=[10],
        channel_group_ids=[],
        regex_pattern="^Live: MLB$",
    )
    service.test_regex_against_epg.assert_not_called()
