"""Scheduling responses must not expose EPG exception diagnostics."""

from unittest.mock import Mock

import pytest
from flask import Flask

from apps.api import scheduling_handlers
from apps.automation.epg_snapshot import EpgSnapshotIncompleteError
from apps.automation.scheduling_service import NoTvgIdError, missing_tvg_id_message


PUBLIC_MESSAGE = (
    "The EPG snapshot is incomplete. Auto-Create did not use the partial guide. "
    "Retry the EPG refresh and check its snapshot status."
)
PRIVATE_DIAGNOSTIC = "Traceback /private/guide.db SELECT secret FROM credentials token=canary"


def call_handler(kind, factory):
    if kind == "channel":
        return scheduling_handlers.get_channel_programs_response(
            channel_id=42, get_scheduling_service=factory,
        )
    return scheduling_handlers.test_auto_create_rule_response(
        payload={"channel_ids": [42], "regex_pattern": "news"},
        get_scheduling_service=factory,
    )


@pytest.mark.parametrize("kind", ["channel", "rule"])
@pytest.mark.parametrize("during_factory", [False, True])
def test_incomplete_snapshot_returns_public_guidance_only(kind, during_factory):
    error = EpgSnapshotIncompleteError()
    # Future service diagnostics must never become part of the public contract.
    error.args = (PRIVATE_DIAGNOSTIC,)
    service = Mock()
    service.get_programs_by_channel.side_effect = error
    service.test_regex_against_epg_for_rule.side_effect = error
    factory = Mock(side_effect=error) if during_factory else Mock(return_value=service)

    with Flask(__name__).app_context():
        response, status = call_handler(kind, factory)
        assert status == 503
        assert response.get_json() == {
            "success": False,
            "error": PUBLIC_MESSAGE,
            "code": "epg_snapshot_incomplete",
        }
        assert PRIVATE_DIAGNOSTIC not in response.get_data(as_text=True)


@pytest.mark.parametrize("kind", ["channel", "rule"])
def test_successful_epg_response_is_preserved(kind):
    result = [{"title": "News"}] if kind == "channel" else {"matches": 1, "programs": [{"title": "News"}]}
    service = Mock()
    service.get_programs_by_channel.return_value = result
    service.test_regex_against_epg_for_rule.return_value = result
    with Flask(__name__).app_context():
        response = call_handler(kind, lambda: service)
        assert response.status_code == 200
        assert response.get_json() == result


@pytest.mark.parametrize("kind", ["channel", "rule"])
def test_missing_tvg_id_retains_structured_guidance(kind):
    service = Mock()
    service.get_programs_by_channel.side_effect = NoTvgIdError(42)
    service.test_regex_against_epg_for_rule.side_effect = NoTvgIdError(42)
    with Flask(__name__).app_context():
        response, status = call_handler(kind, lambda: service)
        expected = {"programs": [], "no_tvg_id": True, "message": missing_tvg_id_message(42)}
        if kind == "rule":
            expected["matches"] = 0
        assert status == 200
        assert response.get_json() == expected
