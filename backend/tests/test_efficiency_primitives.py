"""Concurrency, expiry, and conditional-response regression specifications."""

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from unittest.mock import Mock

import pytest
from flask import Flask, jsonify

from apps.api.status_etags import install_status_etags
from apps.core.metadata_cache import MetadataCache
from apps.core.request_coalescing import SingleFlight
from apps.stream.preflight_policy import latest_due_bucket, preflight_deadline


def test_single_flight_shares_only_pending_result():
    flight = SingleFlight()
    entered, release, waiter = threading.Event(), threading.Event(), threading.Event()
    calls = []

    def operation():
        calls.append(1)
        entered.set()
        assert release.wait(2)
        return {'value': len(calls)}

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(flight.run, 'channel', operation)
        assert entered.wait(2)
        pending = flight._pending['channel']
        original = pending.result
        pending.result = lambda: (waiter.set(), original())[1]
        second = pool.submit(flight.run, 'channel', operation)
        assert waiter.wait(2)
        release.set()
        assert first.result(2) == second.result(2) == {'value': 1}
    assert flight.run('channel', operation) == {'value': 2}


def test_failed_single_flight_can_be_retried():
    flight = SingleFlight()
    with pytest.raises(ValueError):
        flight.run('channel', lambda: (_ for _ in ()).throw(ValueError('unavailable')))
    assert flight.run('channel', lambda: 42) == 42


def test_metadata_cache_ttl_copy_and_failed_loader():
    now = [0.0]
    cache = MetadataCache(ttl_seconds=10, clock=lambda: now[0])
    loader = Mock(return_value={'sports': ['soccer']})
    first = cache.get('catalog', loader)
    first['sports'].append('changed locally')
    assert cache.get('catalog', loader) == {'sports': ['soccer']}
    assert loader.call_count == 1
    now[0] = 11
    cache.get('catalog', loader)
    assert loader.call_count == 2
    with pytest.raises(ValueError):
        cache.get('missing', lambda: (_ for _ in ()).throw(ValueError('offline')))
    assert cache.get('missing', lambda: ['available']) == ['available']


def test_catalog_clear_does_not_publish_an_old_inflight_read():
    cache = MetadataCache()
    entered, release = threading.Event(), threading.Event()

    def old_loader():
        entered.set()
        assert release.wait(2)
        return 'old'

    with ThreadPoolExecutor(max_workers=1) as pool:
        old = pool.submit(cache.get, 'catalog', old_loader)
        assert entered.wait(2)
        cache.clear()
        assert cache.get('catalog', lambda: 'new') == 'new'
        release.set()
        assert old.result(2) == 'old'
    assert cache.get('catalog', lambda: 'unexpected') == 'new'


def test_crossed_checkpoints_choose_newest_due_bucket():
    assert latest_due_bucket(2 * 60, [20, 10, 3], 'pre') == '3m'
    assert latest_due_bucket(4 * 60, [1, 2], 'post') == 'post+2m'
    assert latest_due_bucket(21 * 60, [20, 10, 3], 'pre') is None


def test_checkpoint_expiry_and_manual_checks():
    event_at = datetime(2026, 10, 2, 20, tzinfo=timezone.utc).timestamp()
    event = {'event_date': '2026-10-02T20:00:00Z', 'trigger_bucket': '20m'}
    config = {'preflight_offset_minutes': 20, 'retry_offsets_minutes': [10, 3],
              'post_start_offsets_minutes': [1, 2], 'post_start_grace_minutes': 5}
    assert preflight_deadline(event, config) == event_at - 600
    assert preflight_deadline({**event, 'trigger_bucket': '3m'}, config) == event_at + 60
    assert preflight_deadline({**event, 'trigger_bucket': 'post+2m'}, config) == event_at + 300
    assert preflight_deadline({**event, 'trigger_bucket': 'manual'}, config) is None


def test_status_etag_revalidates_all_fields_and_never_caches_errors():
    app = Flask(__name__)
    install_status_etags(app)
    state = {'queue': {'queued': 1}, 'error': False}

    @app.route('/api/stream-checker/status')
    def status():
        return jsonify(state), 503 if state['error'] else 200

    client = app.test_client()
    first = client.get('/api/stream-checker/status')
    etag = first.headers['ETag']
    unchanged = client.get('/api/stream-checker/status', headers={'If-None-Match': etag})
    assert unchanged.status_code == 304 and unchanged.data == b''
    assert 'private' in unchanged.headers['Cache-Control']
    state['queue']['queued'] = 2
    changed = client.get('/api/stream-checker/status', headers={'If-None-Match': etag})
    assert changed.status_code == 200 and changed.json['queue']['queued'] == 2
    state['error'] = True
    assert client.get('/api/stream-checker/status', headers={'If-None-Match': etag}).status_code == 503


def test_http_sessions_reuse_per_thread_without_retaining_cookies_or_headers(monkeypatch):
    from apps.core import http_transport
    import requests

    http_transport.close_thread_sessions()
    sessions = []
    real_session = requests.Session

    def session_factory():
        session = real_session()
        session.get = Mock(return_value='response')
        sessions.append(session)
        return session

    monkeypatch.setattr(http_transport.requests, 'Session', session_factory)
    try:
        first = http_transport.get_session('http://connector.invalid/one')
        first.cookies.set('old', 'cookie')
        assert http_transport.get('http://connector.invalid/two', headers={'Authorization': 'new'}, timeout=7) == 'response'
        assert http_transport.get_session('http://connector.invalid/three') is first
        assert not first.cookies
        first.get.assert_called_once_with('http://connector.invalid/two', headers={'Authorization': 'new'}, timeout=7)
        assert 'Authorization' not in first.headers
        assert first.adapters['http://'].max_retries.total == 0
        with ThreadPoolExecutor(max_workers=1) as pool:
            def other_thread():
                other = http_transport.get_session('http://connector.invalid/one')
                http_transport.close_thread_sessions()
                return other
            assert pool.submit(other_thread).result() is not first
        assert len(sessions) == 2
    finally:
        http_transport.close_thread_sessions()
