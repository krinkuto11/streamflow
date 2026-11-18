"""stream_matching.preserve_custom_streams in the stream checker.

Drives the complete checker through both entry points (concurrent and the legacy
sequential wrapper) with real scoring, limit selection and write-back, mocking
only external I/O.
"""
from unittest.mock import Mock

import pytest

from apps.stream.checker.inventory import CheckerInventoryMixin
from tests.test_concurrent_limiter_integration import (
    _configure_bitrate_runtime_service,
    _make_bitrate_runtime_udi,
)

pytestmark = pytest.mark.integration

CHANNEL_ID = 555
SCORES = {900: .99, 1: .8, 2: .5}
CUSTOM_URL = 'http://127.0.0.1:9999/slate.ts'


class TestCustomStreamHelpers:
    def test_custom_stream_ids_picks_only_custom_in_order(self):
        streams = [
            {'id': 1, 'is_custom': False},
            {'id': 900, 'is_custom': True},
            {'id': 2},
            {'id': '901', 'is_custom': True},
            {'id': None, 'is_custom': True},
            'junk',
        ]
        assert CheckerInventoryMixin._custom_stream_ids(streams) == [900, 901]

    def test_custom_stream_ids_empty(self):
        assert CheckerInventoryMixin._custom_stream_ids(None) == []
        assert CheckerInventoryMixin._custom_stream_ids([]) == []

    def test_append_custom_moves_custom_last(self):
        assert CheckerInventoryMixin._append_custom_stream_ids([900, 3, 1, 2], [900]) == [3, 1, 2, 900]

    def test_append_custom_restores_missing_custom(self):
        assert CheckerInventoryMixin._append_custom_stream_ids([3, 1], [900]) == [3, 1, 900]

    def test_append_custom_noop_without_custom(self):
        assert CheckerInventoryMixin._append_custom_stream_ids([3, 1], []) == [3, 1]


def _streams(include_real=(1, 2)):
    rows = [{'id': sid, 'name': f'Feed {sid}', 'url': f'http://provider.invalid/{sid}',
             'm3u_account': 1, 'stream_stats': {}} for sid in include_real]
    rows.append({'id': 900, 'name': 'Could Not Dispatch', 'url': CUSTOM_URL,
                 'm3u_account': None, 'is_custom': True, 'stream_stats': {}})
    return rows


def _run(monkeypatch, *, entry, preserve, stream_limit=1, custom_dead=False,
         remove_dead=True, stale_dead_marker=False, real_ids=(1, 2)):
    from apps.stream.stream_checker_service import StreamCheckerService

    streams = _streams(real_ids)
    udi = _make_bitrate_runtime_udi(CHANNEL_ID, streams)
    profile = {
        'id': 'custom', 'name': 'Custom',
        'stream_matching': {'enabled': True, 'preserve_custom_streams': preserve},
        'stream_checking': {
            'enabled': True, 'stream_limit': stream_limit, 'provider_stream_limit': 0,
            'm3u_priority_mode': 'quality', 'grace_period': False,
            'remove_dead_streams': remove_dead, 'allow_revive': True,
        },
    }
    automation = Mock()
    automation.get_profile.return_value = profile
    automation.get_effective_configuration.return_value = {'profile': profile, 'periods': []}
    service = StreamCheckerService()
    _configure_bitrate_runtime_service(service, {'channels': {}})
    service.dead_streams_tracker.is_dead.side_effect = lambda url: stale_dead_marker and url == CUSTOM_URL
    monkeypatch.setattr('apps.stream.stream_checker_service.get_udi_manager', lambda: udi)
    monkeypatch.setattr('apps.stream.stream_checker_service.get_automation_config_manager', lambda: automation)
    monkeypatch.setattr('apps.stream.stream_checker_service.fetch_channel_streams', lambda cid: streams)
    monkeypatch.setattr('apps.stream.stream_checker_service._get_base_url', lambda: 'http://dispatcharr.invalid')
    monkeypatch.setattr('apps.stream.stream_checker_service.get_session_manager', lambda: Mock(
        is_channel_in_active_session=Mock(return_value=False)))
    writer = Mock(return_value=True)
    monkeypatch.setattr('apps.stream.stream_checker_service.update_channel_streams', writer)
    monkeypatch.setattr('apps.stream.stream_checker_service.batch_update_stream_stats',
                        Mock(return_value=(len(streams), 0)))
    service._require_quality_check_connectivity = Mock(return_value=None)
    service._check_channel_limits = Mock(return_value=None)
    service._get_active_viewer_protected_stream_ids = Mock(return_value=set())
    service._run_deferred_bitrate_rechecks = Mock()
    service._calculate_stream_score = lambda row, *a, **kw: SCORES[row['stream_id']]
    service._is_stream_dead = lambda row, *a, **kw: (
        (custom_dead and row['stream_id'] == 900), 'timeout' if custom_dead and row['stream_id'] == 900 else '')

    probed = []

    def probe(streams, **kwargs):
        probed.extend(item['id'] for item in streams)
        return [{'stream_id': item['id'], 'stream_name': item['name'], 'stream_url': item['url'],
                 'resolution': '1920x1080', 'fps': 50, 'bitrate_kbps': 5000,
                 'video_codec': 'h264', 'audio_codec': 'aac'} for item in streams]

    scheduler = Mock()
    scheduler.check_streams_with_limits.side_effect = probe
    monkeypatch.setattr('apps.stream.concurrent_stream_limiter.get_smart_scheduler', lambda **kw: scheduler)

    operation = getattr(service, entry)
    result = operation(CHANNEL_ID, forced_profile_id='custom', force_check_override=True)
    assert not result.get('error'), result
    return service, writer, probed


ENTRY_POINTS = pytest.mark.parametrize('entry', ['_check_channel_concurrent', '_check_channel_sequential'])


@ENTRY_POINTS
def test_high_scoring_custom_stream_does_not_consume_the_only_real_slot(monkeypatch, entry):
    """Reviewer repro: limit 1, real 1/2 and custom 900 must write [1, 900], not [900]."""
    _service, writer, probed = _run(monkeypatch, entry=entry, preserve=True, stream_limit=1)

    writer.assert_called_once()
    assert writer.call_args.args[1] == [1, 900]
    assert 900 not in probed
    assert 900 in writer.call_args.kwargs['protected_stream_ids']


@ENTRY_POINTS
def test_without_the_option_the_custom_stream_still_competes(monkeypatch, entry):
    """Control: disabled preservation keeps the previous behaviour."""
    _service, writer, probed = _run(monkeypatch, entry=entry, preserve=False, stream_limit=1)

    assert writer.call_args.args[1] == [900]
    assert 900 in probed


@ENTRY_POINTS
def test_offline_custom_stream_is_not_probed_marked_dead_or_removed(monkeypatch, entry):
    service, writer, probed = _run(monkeypatch, entry=entry, preserve=True, stream_limit=0,
                                   custom_dead=True)

    assert writer.call_args.args[1] == [1, 2, 900]
    assert 900 not in probed
    for call in service.dead_streams_tracker.mark_as_dead.call_args_list:
        assert call.args[1] != 900
    assert 900 in writer.call_args.kwargs['protected_stream_ids']


@ENTRY_POINTS
def test_stale_dead_marker_on_custom_stream_is_cleared(monkeypatch, entry):
    service, writer, probed = _run(monkeypatch, entry=entry, preserve=True, stream_limit=0,
                                    stale_dead_marker=True)

    # Cleared up front, not as a side effect of probing the stream.
    assert 900 not in probed
    service.dead_streams_tracker.mark_as_alive.assert_any_call(CUSTOM_URL)
    assert writer.call_args.args[1] == [1, 2, 900]


@ENTRY_POINTS
def test_custom_stream_last_when_every_real_stream_is_dead_removed(monkeypatch, entry):
    """Real streams limited out still leave the custom stream as the last entry."""
    _service, writer, _probed = _run(monkeypatch, entry=entry, preserve=True, stream_limit=2)

    ids = writer.call_args.args[1]
    assert ids[-1] == 900
    assert ids[:-1] == [1, 2]


@ENTRY_POINTS
def test_channel_with_only_a_custom_stream_is_left_alone(monkeypatch, entry):
    _service, writer, probed = _run(monkeypatch, entry=entry, preserve=True, real_ids=())

    writer.assert_not_called()
    assert probed == []
