"""Refresh starts must not replay an operation whose outcome is unknown."""

from datetime import datetime, timedelta
from unittest.mock import Mock

import pytest
import requests

from apps.automation import automated_stream_manager as automation
from apps.core import api_utils
from apps.core.m3u_refresh_guard import M3URefreshGuard, RefreshStartBlocked


def accepted():
    response = requests.Response()
    response.status_code = 202
    response._content = b'{"success":true}'
    return response


@pytest.fixture
def account():
    return {'id': 9, 'name': 'Fixture', 'status': 'success', 'enable_vod': True}


@pytest.fixture
def guard(account):
    return M3URefreshGuard('http://dispatcharr.invalid', lambda _: account)


def unknown_start(guard):
    send = Mock(side_effect=requests.exceptions.ReadTimeout('Reply lost after admission'))
    with pytest.raises(requests.exceptions.ReadTimeout):
        guard.start(9, send)
    return send


def test_daily_period_does_not_repost_after_unknown_refresh(monkeypatch):
    manager = automation.AutomatedStreamManager.__new__(automation.AutomatedStreamManager)
    manager.config = {
        'enabled_features': {'auto_playlist_update': True, 'changelog_tracking': False},
        'enabled_m3u_accounts': [],
        'm3u_refresh_wait': {'timeout_seconds': 600, 'retry_failed_providers': False},
    }
    manager.period_last_run = {'daily': datetime.now() - timedelta(days=1, minutes=5)}
    manager._get_period_scheduler_retry = lambda _: None
    manager._get_missed_run_grace_minutes = lambda _: 0
    now = datetime.now()
    period = {'schedule': {'type': 'cron', 'value': f'{now.minute} {now.hour} * * *'}}
    post = Mock(side_effect=requests.exceptions.ReadTimeout('Start may have been accepted'))
    monkeypatch.setattr(api_utils, '_get_base_url', lambda: 'http://dispatcharr.invalid')
    monkeypatch.setattr(api_utils, '_get_auth_headers', lambda: {})
    monkeypatch.setattr(api_utils, 'fetch_data_from_url', lambda _: {
        'id': 9, 'name': 'Fixture', 'status': 'idle',
    })
    monkeypatch.setattr(api_utils.http_transport, 'post', post)
    monkeypatch.setattr(automation, 'get_m3u_accounts', lambda: [
        {'id': 9, 'name': 'Fixture', 'is_active': True, 'status': 'idle'},
    ])

    for _ in range(2):
        assert manager._is_period_due('daily', period)
        assert not manager.refresh_playlists(account_id=9, skip_changelog=True)
        assert not manager._advance_period_run_timestamps({('daily', 'Daily'): {}}, 'failed')

    assert post.call_count == 1


def test_unknown_survives_new_coordinator_and_terminal_live_status(guard, account):
    send = unknown_start(guard)
    account['updated_at'] = '2099-01-01T00:00:00Z'
    restarted = M3URefreshGuard('http://dispatcharr.invalid/', lambda _: account)
    with pytest.raises(RefreshStartBlocked, match='unresolved'):
        restarted.start(9, send)
    assert send.call_count == 1
    assert restarted.pending()[0]['state'] == 'unknown'


@pytest.mark.parametrize('status', ['idle', 'success', 'processing', 'fetching', 'error'])
def test_account_status_alone_never_replays_unknown_start(guard, account, status):
    send = unknown_start(guard)
    account['status'] = status
    with pytest.raises(RefreshStartBlocked):
        guard.start(9, send)
    assert send.call_count == 1


def test_write_ahead_claim_protects_crash_before_result_is_saved(guard):
    send = Mock(side_effect=KeyboardInterrupt)
    with pytest.raises(KeyboardInterrupt):
        guard.start(9, send)
    with pytest.raises(RefreshStartBlocked):
        guard.start(9, send)
    assert send.call_count == 1


def test_unavailable_status_prevents_new_start_without_leaving_false_uncertainty(account):
    guard = M3URefreshGuard('http://dispatcharr.invalid', lambda _: None)
    send = Mock(return_value=accepted())
    with pytest.raises(RefreshStartBlocked, match='Could not verify'):
        guard.start(9, send)
    send.assert_not_called()
    assert guard.repository.pending(guard.scope) == []


def test_unknown_remains_protected_when_status_cannot_be_read(guard):
    send = unknown_start(guard)
    guard.fetch_account = lambda _: None
    assert guard.pending()[0]['status_available'] is False
    attempt = guard.repository.pending(guard.scope)[0]
    with pytest.raises(RefreshStartBlocked):
        guard.resolve(9, attempt['attempt_id'], True)
    assert len(guard.repository.pending(guard.scope)) == 1
    assert send.call_count == 1


def test_operator_resolution_requires_confirmation_and_matching_attempt(guard):
    send = unknown_start(guard)
    attempt = guard.pending()[0]
    with pytest.raises(ValueError):
        guard.resolve(9, attempt['attempt_id'], False)
    with pytest.raises(RefreshStartBlocked):
        guard.resolve(9, 'stale-attempt', True)
    guard.resolve(9, attempt['attempt_id'], True)
    assert guard.pending() == []
    assert send.call_count == 1  # Resolving never starts another refresh itself.
    send.side_effect = None
    send.return_value = accepted()
    assert guard.start(9, send).status_code == 202
    assert send.call_count == 2


def test_running_account_cannot_be_resolved_even_with_operator_confirmation(guard, account):
    unknown_start(guard)
    attempt = guard.pending()[0]
    account['status'] = 'processing'
    with pytest.raises(RefreshStartBlocked, match='still reports'):
        guard.resolve(9, attempt['attempt_id'], True)
    assert len(guard.pending()) == 1


def test_recent_in_flight_request_cannot_be_resolved(guard):
    assert guard.repository.claim(guard.scope, 9, 'in-flight', datetime.now().timestamp())
    with pytest.raises(RefreshStartBlocked, match='in flight'):
        guard.resolve(9, 'in-flight', True)


def test_old_sending_claim_survives_restart_until_explicit_resolution(guard):
    assert guard.repository.claim(guard.scope, 9, 'crashed', 1)
    send = Mock(return_value=accepted())
    with pytest.raises(RefreshStartBlocked):
        guard.start(9, send)
    guard.resolve(9, 'crashed', True)
    send.assert_not_called()


def test_known_acceptance_and_busy_account_preserve_normal_behavior(guard, account):
    send = Mock(return_value=accepted())
    assert guard.start(9, send).status_code == 202
    assert guard.pending() == []
    account['status'] = 'fetching'
    assert guard.start(9, send).json()['already_running'] is True
    assert send.call_count == 1


def test_different_accounts_and_servers_are_independent(guard, account):
    unknown_start(guard)
    other = M3URefreshGuard('http://other-dispatcharr.invalid', lambda _: account)
    assert other.start(9, accepted).status_code == 202
    guard.fetch_account = lambda aid: {**account, 'id': aid}
    assert guard.start(10, accepted).status_code == 202
    assert [item['account_id'] for item in guard.pending()] == [9]


@pytest.mark.parametrize('code', [400, 401, 403, 404, 409, 429])
def test_explicit_rejection_allows_safe_later_attempt(guard, code):
    response = accepted()
    response.status_code = code
    send = Mock(side_effect=requests.exceptions.HTTPError(response=response))
    with pytest.raises(requests.exceptions.HTTPError):
        guard.start(9, send)
    assert guard.pending() == []


@pytest.mark.parametrize('error', [requests.exceptions.ConnectTimeout()])
def test_connection_establishment_failure_does_not_leave_uncertainty(guard, error):
    send = Mock(side_effect=error)
    with pytest.raises(type(error)):
        guard.start(9, send)
    assert guard.pending() == []


@pytest.mark.parametrize('code', [408, 500, 502, 503])
def test_ambiguous_server_error_stays_protected(guard, code):
    response = accepted()
    response.status_code = code
    send = Mock(side_effect=requests.exceptions.HTTPError(response=response))
    with pytest.raises(requests.exceptions.HTTPError):
        guard.start(9, send)
    assert guard.pending()[0]['state'] == 'unknown'


def test_bulk_refresh_cannot_bypass_pending_account_and_continues_other_accounts(monkeypatch, account):
    monkeypatch.setattr(api_utils, '_get_base_url', lambda: 'http://dispatcharr.invalid')
    accounts = [account, {**account, 'id': 10}]
    monkeypatch.setattr(api_utils, 'fetch_data_from_url', lambda url: accounts if url.endswith('/accounts/')
                        else accounts[1] if url.endswith('/10/') else account)
    guard = api_utils.get_m3u_refresh_guard()
    unknown_start(guard)
    send = Mock(return_value=accepted())
    monkeypatch.setattr(api_utils, 'post_request', send)
    with pytest.raises(requests.exceptions.RequestException, match='account IDs'):
        api_utils.refresh_m3u_playlists()
    assert send.call_count == 1
    assert send.call_args.args[0].endswith('/refresh/10/')


@pytest.mark.parametrize('target', [9, None])
def test_refresh_keeps_its_original_target_if_connection_settings_change(monkeypatch, account, target):
    base_url = Mock(side_effect=['http://original.invalid', 'http://changed.invalid'])
    monkeypatch.setattr(api_utils, '_get_base_url', base_url)
    accounts = [account, {**account, 'id': 10}]
    reads = []

    def fetch(url):
        reads.append(url)
        return accounts if url.endswith('/accounts/') else accounts[1] if url.endswith('/10/') else account

    monkeypatch.setattr(api_utils, 'fetch_data_from_url', fetch)
    send = Mock(return_value=accepted())
    monkeypatch.setattr(api_utils, 'post_request', send)
    assert api_utils.refresh_m3u_playlists(target).status_code == 202
    assert base_url.call_count == 1
    assert all(url.startswith('http://original.invalid/') for url in reads)
    assert all(call.args[0].startswith('http://original.invalid/') for call in send.call_args_list)


def test_concurrent_process_style_claims_use_database_atomicity(tmp_path, monkeypatch, account):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from apps.database import connection

    engine = create_engine(f'sqlite:///{tmp_path / "refresh.db"}', connect_args={'check_same_thread': False})
    connection.Base.metadata.create_all(engine)
    monkeypatch.setattr(connection, 'get_session', sessionmaker(bind=engine))
    first = M3URefreshGuard('http://dispatcharr.invalid', lambda _: account)
    second = M3URefreshGuard('http://dispatcharr.invalid', lambda _: account)
    entered, finish = threading.Event(), threading.Event()

    def send():
        entered.set()
        assert finish.wait(5)
        return accepted()

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            future = pool.submit(first.start, 9, send)
            assert entered.wait(5)
            try:
                with pytest.raises(RefreshStartBlocked):
                    second.start(9, Mock())
            finally:
                finish.set()
            assert future.result(timeout=5).status_code == 202
    finally:
        engine.dispose()
