"""Throttle hints, serialized credential cooldowns, and bounded UDI retries."""

from datetime import datetime, timezone
from email.utils import format_datetime
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import Mock

import pytest
import requests

from apps.core import auth
from apps.core.retry_after import retry_after_seconds
from apps.udi import fetcher as fetcher_module


def response(status, payload=None, headers=None):
    result = requests.Response()
    result.status_code = status
    result.url = 'http://dispatcharr.test/api/test/'
    result._content = json.dumps(payload or {}).encode()
    result.headers.update(headers or {})
    return result


@pytest.mark.parametrize('headers,payload,expected', [
    ({'Retry-After': '46'}, {}, 46),
    ({'Retry-After': '0'}, {}, 0),
    ({'Retry-After': '1.25'}, {}, 2),
    ({}, {'detail': 'Request was throttled. Expected available in 46 seconds.'}, 46),
    ({}, {'detail': 'Expected available in 0.25 seconds.'}, 1),
    ({'Retry-After': '2'}, {'detail': 'Expected available in 46 seconds.'}, 46),
    ({'Retry-After': 'invalid'}, {'detail': 'Expected available in 17 seconds.'}, 17),
    ({'Retry-After': '-1'}, {}, None),
    ({'Retry-After': 'NaN'}, {}, None),
    ({'Retry-After': 'inf'}, {}, None),
    ({}, {'detail': 'Invalid password 46.'}, None),
    ({}, {'detail': ['Expected available in 46 seconds.']}, None),
    ({}, {}, None),
])
def test_retry_hint_formats(headers, payload, expected):
    assert retry_after_seconds(response(429, payload, headers), now=1000) == expected


def test_http_date_and_past_date():
    date = format_datetime(datetime.fromtimestamp(1046, timezone.utc), usegmt=True)
    assert retry_after_seconds(response(503, headers={'Retry-After': date}), now=1000) == 46
    assert retry_after_seconds(response(503, headers={'Retry-After': date}), now=1100) == 0


def test_non_json_body_does_not_hide_valid_header():
    result = response(429, headers={'Retry-After': '46'})
    result._content = b'<html>throttled</html>'
    assert retry_after_seconds(result) == 46


@pytest.fixture
def credentials(monkeypatch, tmp_path):
    config = Mock()
    config.get_auth_mode.return_value = 'credentials'
    config.get_base_url.return_value = 'http://dispatcharr.test'
    config.get_username.return_value = 'test-user'
    config.get_password.return_value = 'test-password'
    monkeypatch.setattr(auth, 'get_dispatcharr_config', lambda: config)
    monkeypatch.setattr(auth, 'env_path', tmp_path / 'absent.env')
    monkeypatch.setattr(auth, '_login_retry_key', None)
    monkeypatch.setattr(auth, '_login_retry_deadline', 0.0)
    monkeypatch.setattr(auth, '_login_generation', 0)
    monkeypatch.delenv('DISPATCHARR_TOKEN', raising=False)
    return config


@pytest.fixture
def clock(monkeypatch):
    state = {'now': 1000.0, 'sleeps': []}
    def sleep(delay):
        state['sleeps'].append(delay)
        state['now'] += delay
    monkeypatch.setattr(auth.time, 'monotonic', lambda: state['now'])
    monkeypatch.setattr(auth.time, 'sleep', sleep)
    return state


@pytest.mark.parametrize('hint,expected', [({'Retry-After': '46'}, 46), ({}, 46)])
def test_login_cooldown_honors_header_or_body(monkeypatch, credentials, clock, hint, expected):
    attempts = []
    results = iter([response(429, {'detail': 'Expected available in 46 seconds.'}, hint), response(200, {'access': 'new-token'})])
    def post(*args, **kwargs):
        attempts.append(clock['now'])
        return next(results)
    monkeypatch.setattr(auth.requests, 'post', post)
    assert auth._login() is False
    assert auth._login() is True
    assert attempts == [1000, 1000 + expected]
    assert clock['sleeps'] == [expected]
    assert auth._get_auth_headers()['Authorization'] == 'Bearer new-token'
    assert auth._login_retry_deadline == 0


def test_throttled_initial_login_burst_uses_one_shared_retry(monkeypatch, credentials, clock):
    attempts = []
    def post(*args, **kwargs):
        attempts.append(clock['now'])
        return response(429, {'detail': 'Expected available in 46 seconds.'}) if len(attempts) == 1 else response(200, {'access': 'new-token'})
    monkeypatch.setattr(auth.requests, 'post', post)
    barrier = threading.Barrier(8)
    results, errors = [], []
    def worker():
        barrier.wait(timeout=3)
        try:
            results.append(auth._get_auth_headers()['Authorization'])
        except RuntimeError as error:
            errors.append(error)
    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=3)
        assert not thread.is_alive()
    assert attempts == [1000, 1046]
    assert results == ['Bearer new-token'] * 7
    assert len(errors) == 1  # The original throttled attempt retains its failure result.


def test_missing_hint_uses_cooldown_and_changed_credentials_do_not_inherit_it(monkeypatch, credentials, clock):
    post = Mock(side_effect=[response(429), response(200, {'access': 'new-token'})])
    monkeypatch.setattr(auth.requests, 'post', post)
    assert auth._login() is False
    assert auth._login_retry_deadline == 1002
    credentials.get_password.return_value = 'changed-password'
    assert auth._login() is True
    assert clock['sleeps'] == []


def test_existing_bearer_and_api_key_do_not_wait_for_login_cooldown(monkeypatch, credentials, clock):
    monkeypatch.setattr(auth, '_login_retry_deadline', 1046)
    post = Mock()
    monkeypatch.setattr(auth.requests, 'post', post)
    monkeypatch.setenv('DISPATCHARR_TOKEN', 'saved-token')
    assert auth._get_auth_headers()['Authorization'] == 'Bearer saved-token'
    credentials.get_auth_mode.return_value = 'api_key'
    credentials.get_api_key.return_value = 'test-key'
    assert auth._get_auth_headers()['Authorization'] == 'ApiKey test-key'
    assert auth._refresh_token() is False
    assert clock['sleeps'] == []
    post.assert_not_called()


def _fetcher(monkeypatch, results, clock):
    monkeypatch.setattr(fetcher_module, '_get_base_url', lambda: 'http://dispatcharr.test')
    monkeypatch.setattr(fetcher_module, '_get_auth_headers', lambda: {'Authorization': 'Bearer token'})
    request = Mock(side_effect=results)
    monkeypatch.setattr(fetcher_module.http_transport, 'get', request)
    return fetcher_module.UDIFetcher(), request


@pytest.mark.parametrize('status', [429, 503])
def test_udi_get_uses_server_wait_without_short_cap(monkeypatch, clock, status):
    fetcher, request = _fetcher(monkeypatch, [response(status, headers={'Retry-After': '46'}), response(200, {'ok': True})], clock)
    assert fetcher._fetch_url('http://dispatcharr.test/api/test/') == {'ok': True}
    assert clock['sleeps'] == [46]
    assert request.call_count == 2


def test_udi_get_honors_body_hint_after_401_refresh(monkeypatch, clock):
    fetcher, request = _fetcher(monkeypatch, [response(401), response(429, {'detail': 'Expected available in 46 seconds.'}), response(200, {'ok': True})], clock)
    refresh = Mock(return_value=True)
    monkeypatch.setattr(fetcher_module, '_refresh_token', refresh)
    assert fetcher._fetch_url('http://dispatcharr.test/api/test/') == {'ok': True}
    assert clock['sleeps'] == [46]
    assert request.call_count == 3
    refresh.assert_called_once()


@pytest.mark.parametrize('status', [429, 500])
def test_retry_limit_and_fallback_cadence_are_preserved(monkeypatch, clock, status):
    fetcher, request = _fetcher(monkeypatch, [response(status)] * 3, clock)
    assert fetcher._fetch_url('http://dispatcharr.test/api/test/') is None
    assert request.call_count == 3
    assert clock['sleeps'] == [2, 4]


def test_non_retryable_error_and_unsafe_post_do_not_get_extra_retries(monkeypatch, clock):
    fetcher, get = _fetcher(monkeypatch, [response(404)], clock)
    assert fetcher._fetch_url('http://dispatcharr.test/api/test/') is None
    get.assert_called_once()
    post = Mock(return_value=response(429, headers={'Retry-After': '46'}))
    monkeypatch.setattr(fetcher_module.http_transport, 'post', post)
    assert fetcher._post_url('http://dispatcharr.test/api/test/', {}) is None
    post.assert_called_once()
    assert clock['sleeps'] == []


@pytest.fixture
def throttle_server():
    class Handler(BaseHTTPRequestHandler):
        attempts = {'GET': [], 'POST': []}
        lock = threading.Lock()
        def log_message(self, *args):
            pass
        def respond(self, method):
            if method == 'POST':
                self.rfile.read(int(self.headers.get('Content-Length', '0')))
            with self.lock:
                self.attempts[method].append(time.monotonic())
                first = len(self.attempts[method]) == 1
            payload = {'detail': 'Expected available in 1 second.'} if first else {'access': 'live-test-token', 'ok': True}
            data = json.dumps(payload).encode()
            self.send_response(429 if first else 200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(data)))
            if first:
                self.send_header('Retry-After', '1')
            self.end_headers()
            self.wfile.write(data)
        def do_GET(self):
            self.respond('GET')
        def do_POST(self):
            self.respond('POST')
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    yield f'http://127.0.0.1:{server.server_port}', Handler.attempts
    server.shutdown()
    server.server_close()
    worker.join(timeout=3)


@pytest.mark.integration
def test_real_http_initial_login_burst_waits_and_reuses_token(credentials, throttle_server):
    base_url, attempts = throttle_server
    credentials.get_base_url.return_value = base_url
    barrier = threading.Barrier(8)
    results, errors = [], []
    def worker():
        barrier.wait(timeout=3)
        try:
            results.append(auth._get_auth_headers()['Authorization'])
        except RuntimeError as error:
            errors.append(error)
    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
        assert not thread.is_alive()
    assert len(attempts['POST']) == 2
    assert attempts['POST'][1] - attempts['POST'][0] >= 1
    assert results == ['Bearer live-test-token'] * 7
    assert len(errors) == 1


@pytest.mark.integration
def test_real_http_udi_get_waits_for_retry_after(monkeypatch, throttle_server):
    base_url, attempts = throttle_server
    monkeypatch.setattr(fetcher_module, '_get_base_url', lambda: base_url)
    monkeypatch.setattr(fetcher_module, '_get_auth_headers', lambda: {'Authorization': 'ApiKey test-key'})
    fetcher = fetcher_module.UDIFetcher()
    assert fetcher._fetch_url(base_url + '/api/test/')['ok'] is True
    assert len(attempts['GET']) == 2
    assert attempts['GET'][1] - attempts['GET'][0] >= 1
