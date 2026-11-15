"""The recovery API requires an explicit, current operator resolution."""

from unittest.mock import Mock
from uuid import uuid4

from flask import Flask
import pytest
import requests

from apps.api.m3u_refresh_handlers import m3u_refresh_attempts_response
from apps.core.m3u_refresh_guard import M3URefreshGuard


@pytest.fixture
def pending():
    account = {'id': 9, 'name': 'Fixture', 'status': 'success'}
    guard = M3URefreshGuard('http://private-target.invalid', lambda _: account)
    send = Mock(side_effect=requests.exceptions.ReadTimeout())
    with pytest.raises(requests.exceptions.ReadTimeout):
        guard.start(9, send)
    return guard, account, guard.pending()[0]


def call(guard, method='GET', payload=None):
    with Flask(__name__).app_context():
        response, code = m3u_refresh_attempts_response(method=method, payload=payload, get_guard=lambda: guard)
        return response.get_json(), code


def test_pending_status_reports_unknown_without_exposing_server_scope(pending):
    guard, _, attempt = pending
    data, code = call(guard)
    assert code == 200
    assert data['data']['attempts'][0]['attempt_id'] == attempt['attempt_id']
    assert data['data']['attempts'][0]['state'] == 'unknown'
    assert data['data']['attempts'][0]['status_available'] is True
    assert 'server_scope' not in data['data']['attempts'][0]
    assert 'private-target' not in str(data)


@pytest.mark.parametrize('change', [
    {'confirmed_finished': False}, {'confirmed_finished': 'true'},
    {'confirmed_finished': None}, {'account_id': True}, {'account_id': '9'},
    {'account_id': -1}, {'attempt_id': ''}, {'attempt_id': []},
])
def test_invalid_resolution_cannot_remove_protection(pending, change):
    guard, _, attempt = pending
    payload = {'account_id': 9, 'attempt_id': attempt['attempt_id'], 'confirmed_finished': True, **change}
    data, code = call(guard, 'POST', payload)
    assert code == 400
    assert data['success'] is False
    assert len(guard.pending()) == 1


def test_resolution_is_conditional_and_does_not_start_a_refresh(pending):
    guard, _, attempt = pending
    payload = {'account_id': 9, 'attempt_id': str(uuid4()), 'confirmed_finished': True}
    assert call(guard, 'POST', payload)[1] == 409
    payload['attempt_id'] = attempt['attempt_id']
    data, code = call(guard, 'POST', payload)
    assert code == 200
    assert 'no refresh was started' in data['message']
    assert guard.pending() == []
    assert call(guard, 'POST', payload)[1] == 409


def test_live_busy_status_denies_resolution(pending):
    guard, account, attempt = pending
    account['status'] = 'parsing'
    data, code = call(guard, 'POST', {
        'account_id': 9, 'attempt_id': attempt['attempt_id'], 'confirmed_finished': True,
    })
    assert code == 409
    assert 'running refresh' in data['error']
    assert len(guard.pending()) == 1
