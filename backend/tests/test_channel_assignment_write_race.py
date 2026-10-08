"""Deterministic channel assignment races against a fake Dispatcharr API."""

import threading
from types import SimpleNamespace
from unittest.mock import patch

from apps.core import api_utils


class FakeDispatcharr:
    def __init__(self, stream_ids):
        self.stream_ids = list(stream_ids)
        self.lock = threading.Lock()
        self.get_count = 0
        self.patch_count = 0
        self.apply_patch = True

    def get(self, url):
        with self.lock:
            self.get_count += 1
            if url.endswith('/api/channels/streams/ids/'):
                return [1, 2, 3, 4]
            assert url.endswith('/api/channels/channels/42/')
            return {'id': 42, 'streams': list(self.stream_ids)}

    def patch(self, url, payload):
        assert url.endswith('/api/channels/channels/42/')
        with self.lock:
            self.patch_count += 1
            if self.apply_patch:
                self.stream_ids = list(payload['streams'])
        return SimpleNamespace(status_code=204)


class FakeUDI:
    def __init__(self):
        self.channel = {'id': 42, 'streams': [1]}

    def get_valid_stream_ids(self):
        return {1, 2, 3, 4}

    def get_channel_by_id(self, channel_id):
        assert channel_id == 42
        return dict(self.channel)

    def update_channel(self, channel_id, channel):
        assert channel_id == 42
        self.channel = dict(channel)
        return True


def _mock_dispatcharr(dispatcharr, udi):
    return (
        patch.object(api_utils, 'fetch_data_from_url', side_effect=dispatcharr.get),
        patch.object(api_utils, 'patch_request', side_effect=dispatcharr.patch),
        patch.object(api_utils, '_get_base_url', return_value='http://fake-dispatcharr'),
        patch.object(api_utils, 'get_udi_manager', return_value=udi),
    )


def test_monitoring_stale_full_list_cannot_erase_matching_append():
    dispatcharr = FakeDispatcharr([1])
    udi = FakeUDI()
    monitor_ready = threading.Event()
    release_monitor = threading.Event()
    monitor_result = []

    def stale_monitor_write():
        # Monitoring calculated this desired order before matching adds stream 2.
        monitor_ready.set()
        assert release_monitor.wait(timeout=2)
        monitor_result.append(api_utils.update_channel_streams(
            42, [3, 1], valid_stream_ids={1, 2, 3, 4},
            allow_dead_streams=True, expected_current_stream_ids=[1],
        ))

    patches = _mock_dispatcharr(dispatcharr, udi)
    with patches[0], patches[1], patches[2], patches[3]:
        thread = threading.Thread(target=stale_monitor_write)
        thread.start()
        assert monitor_ready.wait(timeout=2)
        try:
            assert api_utils.add_streams_to_channel(
                42, [2], valid_stream_ids={1, 2, 3, 4}, allow_dead_streams=True,
            ) == 1
        finally:
            release_monitor.set()
            thread.join(timeout=2)

    assert not thread.is_alive()
    assert monitor_result == [False]
    assert dispatcharr.stream_ids == [1, 2]
    assert udi.channel['streams'] == [1, 2]
    assert dispatcharr.patch_count == 1
    assert dispatcharr.get_count == 4  # append read, preflight, readback; stale preflight


def test_matching_append_keeps_manual_addition_absent_from_udi_cache():
    dispatcharr = FakeDispatcharr([1, 4])
    udi = FakeUDI()  # stale cache still contains only stream 1
    patches = _mock_dispatcharr(dispatcharr, udi)

    with patches[0], patches[1], patches[2], patches[3]:
        added = api_utils.add_streams_to_channel(
            42, [2], valid_stream_ids={1, 2, 3, 4}, allow_dead_streams=True,
        )

    assert added == 1
    assert dispatcharr.stream_ids == [1, 4, 2]
    assert dispatcharr.patch_count == 1


def test_successful_http_patch_requires_authoritative_readback():
    dispatcharr = FakeDispatcharr([1])
    dispatcharr.apply_patch = False
    udi = FakeUDI()
    patches = _mock_dispatcharr(dispatcharr, udi)

    with patches[0], patches[1], patches[2], patches[3]:
        result = api_utils.update_channel_streams(
            42, [2, 1], valid_stream_ids={1, 2, 3, 4},
            allow_dead_streams=True, expected_current_stream_ids=[1],
        )

    assert result is False
    assert dispatcharr.stream_ids == [1]
    assert dispatcharr.patch_count == 1
    assert dispatcharr.get_count == 3  # preflight and two bounded readbacks
