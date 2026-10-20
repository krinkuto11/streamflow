import json
import threading
from collections import deque
from copy import deepcopy
from unittest.mock import Mock

import pytest
from flask import Flask

from apps.api.teamarr_preflight_handlers import get_teamarr_preflight_inspection_response
from apps.stream.preflight_inspection import build_inspection, public_inspection, MAX_INSPECTION_BYTES
from apps.stream.teamarr_preflight_service import TeamarrPreflightService, MAX_EVENTS


def snapshot(**overrides):
    values = dict(
        streams=[{'id': sid, 'name': f'Feed {sid}', 'm3u_account': sid} for sid in range(1, 7)],
        analyzed=[
            {'stream_id': 1, 'resolution': '1920x1080', 'fps': 50, 'bitrate_kbps': 6000,
             'video_codec': 'h264', 'audio_codec': 'aac', 'score': .6},
            {'stream_id': 2, 'resolution': '3840x2160', 'score': .9},
            {'stream_id': 3, 'score': 0, 'dead_reason': 'timeout', 'dead_reason_detail': 'HTTP 503'},
            {'stream_id': 4, 'provider_limit_skipped': True, 'reason_detail': 'provider_capacity'},
            {'stream_id': 5, 'status': 'cached', 'score': .8},
            {'stream_id': 6, 'measurement_incomplete': True, 'measurement_incomplete_reason': 'missing_bitrate'},
        ],
        skipped=[], checked_ids={1, 2, 3, 4, 6}, dead_ids={3}, ordered_ids=[1, 5],
        profile={'id': 'profile', 'name': 'Preflight'}, priority_mode='absolute',
        priority_ids=[1, 2], provider_names={sid: f'Provider {sid}' for sid in range(1, 7)},
    )
    values.update(overrides)
    return build_inspection(**values)


def test_removed_and_limited_streams_remain_in_the_original_check():
    data = snapshot()
    rows = {row['stream_id']: row for row in data['streams']}
    assert len(rows) == 6
    assert rows[1]['rank'] == 1 and rows[1]['score'] < rows[2]['score']
    assert rows[1]['audio_codec'] == 'aac'
    assert rows[2]['status'] == 'alive' and rows[2]['action'] == 'not_retained'
    assert rows[3]['status'] == 'dead' and 'HTTP 503' in rows[3]['reason']
    assert rows[3]['action'] == 'not_retained'
    assert rows[4]['status'] == 'skipped' and rows[4]['reason'] == 'provider_capacity'
    assert rows[5]['status'] == 'skipped' and rows[5]['measurement_source'] == 'previous_measurement'
    assert rows[6]['status'] == 'incomplete'
    assert snapshot(ordered_ids=None)['ordering_status'] == 'not_applied'
    assert all(row['action'] == 'order_not_applied' for row in snapshot(ordered_ids=None)['streams'])


def test_unsafe_unknown_fields_and_nonfinite_scores_are_not_exposed():
    raw = {'streams': [{'stream_id': 3, 'stream_name': 'Feed https://host/user/password',
                       'reason': 'token=SECRET HTTP 503', 'score': float('nan'),
                       'url': 'https://SECRET', 'headers': {'Authorization': 'SECRET'}}],
           'profile_name': 'password=SECRET', 'exception': 'SECRET'}
    data = public_inspection(raw)
    assert 'SECRET' not in json.dumps(data)
    assert 'https://' not in json.dumps(data)
    assert 'score' not in data['streams'][0]
    assert data['streams'][0]['reason'].endswith('HTTP 503')


def test_tracked_dead_state_does_not_invent_a_current_offline_measurement():
    rows = {row['stream_id']: row for row in snapshot(dead_ids={1})['streams']}
    assert rows[1]['status'] == 'dead' and rows[1]['reason'] == 'marked_dead'
    rows = {row['stream_id']: row for row in snapshot(dead_ids={1}, decision_reasons={1: 'revival_disabled'})['streams']}
    assert rows[1]['reason'] == 'revival_disabled'


def test_large_results_are_explicitly_bounded():
    raw = {'streams': [dict(snapshot()['streams'][0], stream_id=sid, reason='a' * 10000)
                       for sid in range(800)]}
    data = public_inspection(raw)
    assert data['total_streams'] == 800 and data['truncated']
    assert len(data['streams']) <= 500
    assert len(json.dumps(data, ensure_ascii=False).encode()) <= MAX_INSPECTION_BYTES


def service_harness(result):
    service = TeamarrPreflightService.__new__(TeamarrPreflightService)
    service._lock = threading.RLock()
    service._events = deque(maxlen=MAX_EVENTS)
    service._inspections = {}
    service.clock = lambda: 12345678
    service.get_config = lambda **kw: {}
    service._resolve_profile_id = lambda value: None
    service._quality_profile_details = lambda value: {}
    service._finish_active_check = Mock()
    checker = Mock()
    checker.check_single_channel.return_value = result
    service.stream_checker_provider = lambda: checker
    return service, checker


@pytest.mark.parametrize('queued', [False, True])
def test_direct_and_queued_results_reference_immutable_lazy_details(queued):
    result = {'success': True, 'stats': {'dead_streams': 1}, 'preflight_inspection': snapshot()}
    service, checker = service_harness(result)
    event = {'identity': 'event:1', 'dispatcharr_channel_id': 77}
    if queued:
        service.record_queued_check_result({'event': event}, result)
    else:
        service._run_check('attempt', event, {})
    record = service._events[0]
    assert record['type'] == 'preflight_completed'
    details = record['details']
    assert details['stats'] == {'dead_streams': 1}
    assert 'inspection' not in details and 'streams' not in details
    inspection_id = details['inspection_id']
    original = deepcopy(service.get_inspection(inspection_id))
    result['preflight_inspection']['streams'][0]['stream_name'] = 'Changed after check'
    data = service.get_inspection(inspection_id)
    data['streams'].clear()
    assert service.get_inspection(inspection_id) == original
    calls = checker.mock_calls[:]
    app = Flask(__name__)
    with app.app_context():
        response, status = get_teamarr_preflight_inspection_response(
            inspection_id=inspection_id, get_service=lambda: service)
    assert status == 200 and response.get_json() == original
    assert checker.mock_calls == calls


def test_result_ids_do_not_collide_and_expired_details_return_404():
    service, _ = service_harness({})
    ids = []
    for _ in range(MAX_EVENTS + 1):
        service._record_event('preflight_completed', {}, {'inspection': snapshot()})
        ids.append(service._events[0]['details']['inspection_id'])
    assert len(set(ids)) == MAX_EVENTS + 1
    assert len(service._inspections) == MAX_EVENTS
    assert service.get_inspection(ids[0]) is None
    with Flask(__name__).app_context():
        response, status = get_teamarr_preflight_inspection_response(
            inspection_id=ids[0], get_service=lambda: service)
    assert status == 404 and response.get_json()['code'] == 'inspection_not_found'
