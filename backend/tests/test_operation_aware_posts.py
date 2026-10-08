from unittest.mock import Mock

import pytest
import requests

from apps.core import api_utils


@pytest.fixture
def transport(monkeypatch):
    post = Mock()
    monkeypatch.setattr(api_utils.http_transport, 'post', post)
    monkeypatch.setattr(api_utils, '_get_auth_headers', lambda: {})
    monkeypatch.setattr(api_utils.time, 'sleep', lambda _: None)
    return post


def response(code=201):
    result = requests.Response()
    result.status_code = code
    result._content = b'{"id":42}'
    return result


def test_read_timeout_does_not_replay_creation(transport):
    transport.side_effect = requests.exceptions.ReadTimeout('accepted outcome unknown')
    with pytest.raises(api_utils.UncertainPostResult):
        api_utils.post_request('http://dispatcharr.invalid/create', {})
    assert transport.call_count == 1


def test_unknown_creation_can_be_reconciled_without_replay(transport):
    transport.side_effect = requests.exceptions.ReadTimeout()
    accepted = response()
    reconcile = Mock(return_value=accepted)
    assert api_utils.post_request('http://dispatcharr.invalid/create', {}, reconcile=reconcile) is accepted
    assert transport.call_count == reconcile.call_count == 1


def test_connect_timeout_can_retry(transport):
    accepted = response()
    transport.side_effect = [requests.exceptions.ConnectTimeout(), accepted]
    assert api_utils.post_request('http://dispatcharr.invalid/create', {}) is accepted
    assert transport.call_count == 2


def test_patch_retries_rate_limit_but_stops_on_permission_failure(monkeypatch):
    patch = Mock(side_effect=[response(429), response(204)])
    delays = []
    monkeypatch.setattr(api_utils.http_transport, 'patch', patch)
    monkeypatch.setattr(api_utils, '_get_auth_headers', lambda: {})
    monkeypatch.setattr(api_utils.time, 'sleep', delays.append)
    assert api_utils.patch_request('http://dispatcharr.invalid/streams/9', {}).status_code == 204
    assert patch.call_count == 2 and delays == [2.0]
    patch.reset_mock()
    patch.side_effect = [response(403)]
    with pytest.raises(requests.exceptions.HTTPError):
        api_utils.patch_request('http://dispatcharr.invalid/streams/9', {})
    assert patch.call_count == 1


def test_auth_rejection_refreshes_once_without_replaying_accepted_request(transport, monkeypatch):
    refresh = Mock(return_value=True)
    monkeypatch.setattr(api_utils, '_refresh_token', refresh)
    accepted = response()
    transport.side_effect = [response(401), accepted]
    assert api_utils.post_request('http://dispatcharr.invalid/create', {}) is accepted
    assert transport.call_count == 2 and refresh.call_count == 1


def test_creation_reconciliation_requires_unique_new_matching_channel(monkeypatch):
    fetcher = Mock()
    fetcher.fetch_channel_ids.side_effect = [{1}, {1, 2}]
    fetcher.fetch_channel_by_id.return_value = {'id': 2, 'streams': [9], 'name': 'Event', 'channel_number': 70}
    monkeypatch.setattr(api_utils, 'get_udi_manager', lambda: Mock(fetcher=fetcher))
    monkeypatch.setattr(api_utils, '_get_base_url', lambda: 'http://dispatcharr.invalid')
    monkeypatch.setattr(api_utils, 'post_request', lambda *args, **kwargs: kwargs['reconcile']())
    result = api_utils.create_channel_from_stream(9, channel_number=70, name='Event')
    assert result.status_code == 201 and result.json()['id'] == 2
    fetcher.fetch_channel_ids.side_effect = [{1}, {1, 2, 3}]
    assert api_utils.create_channel_from_stream(9, channel_number=70, name='Event') is None


def test_statistics_partial_failure_publishes_only_acknowledged_records(monkeypatch):
    streams = {9: {'id': 9, 'stream_stats': {'quality_score': 1}},
               10: {'id': 10, 'stream_stats': {'quality_score': 2}}}
    udi = Mock()
    udi.get_stream_by_id.side_effect = streams.get
    udi.update_stream.side_effect = lambda sid, data: streams.update({sid: data}) or True
    monkeypatch.setattr(api_utils, 'get_udi_manager', lambda: udi)
    monkeypatch.setattr(api_utils, '_get_base_url', lambda: 'http://dispatcharr.invalid')
    monkeypatch.setattr(api_utils, 'patch_request', Mock(side_effect=[response(204), requests.exceptions.ReadTimeout()]))
    result = api_utils.batch_update_stream_stats([
        {'stream_id': 9, 'stream_stats': {'quality_score': 8, 'blank_detected': False}},
        {'stream_id': 10, 'stream_stats': {'quality_score': 9}},
    ])
    assert result == (1, 1)
    assert streams[9]['stream_stats'] == {'quality_score': 8, 'blank_detected': False}
    assert streams[10]['stream_stats'] == {'quality_score': 2}
