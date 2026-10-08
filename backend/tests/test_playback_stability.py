"""Playback evidence must be optional, bounded, persisted and conservative."""

import threading
from unittest.mock import Mock

import pytest
from flask import Flask

from apps.database.connection import get_session
from apps.database.models import PlaybackObservation
from apps.database.repositories.playback_stability_repository import PlaybackStabilityRepository
from apps.stream.playback_stability_tracker import PlaybackTracker
from apps.stream.playback_stability_service import PlaybackStabilityService, source_fingerprint
from apps.stream.checker.classification import CheckerClassificationMixin
from apps.api.playback_stability_handlers import playback_stability_config_response, playback_stability_status_response


def snapshot(stream_id=1, session='one', clients=('viewer',), total_bytes=100, channel_id=10, fingerprint='source'):
    return {'channel_uuid': 'uuid', 'channel_id': channel_id, 'stream_id': stream_id,
            'source_fingerprint': fingerprint, 'session': session, 'clients': frozenset(clients), 'total_bytes': total_bytes}


def observe(tracker, seconds, **kwargs):
    result = []
    for second in range(0, seconds + 1, 10):
        result.extend(tracker.consume([snapshot(total_bytes=100 + second, **kwargs)], 1000 + second, 10))
    return result


def test_normal_watch_persists_valid_duration_not_client_multiplier():
    tracker = PlaybackTracker()
    rows = observe(tracker, 60, clients=('one', 'two'))
    assert rows[-1]['observed_seconds'] == 60
    assert rows[-1]['samples'] == 6
    assert rows[-1]['stalls'] == rows[-1]['failovers'] == 0
    assert not any('clients' in row or 'session' in row or 'url' in row for row in rows)


@pytest.mark.parametrize('kind', ['end', 'new_session', 'viewer_changed', 'gap', 'counter_reset', 'channel_reused', 'url_changed'])
def test_normal_ends_resets_retunes_and_gaps_are_not_failures(kind):
    tracker = PlaybackTracker()
    rows = observe(tracker, 60)
    previous = tracker.legs['uuid']
    next_item = snapshot(total_bytes=200)
    now = 1070
    if kind == 'end':
        assert tracker.consume([], now, 10) == []
        assert not tracker.legs
        return
    if kind == 'new_session': next_item.update(session='two', stream_id=2)
    if kind == 'viewer_changed': next_item.update(clients=frozenset({'new'}), stream_id=2)
    if kind == 'gap': now = 1200; next_item['stream_id'] = 2
    if kind == 'counter_reset': next_item['total_bytes'] = 0
    if kind == 'channel_reused': next_item.update(channel_id=11, stream_id=2)
    if kind == 'url_changed': next_item['source_fingerprint'] = 'different'
    tracker.consume([next_item], now, 10)
    assert previous.failovers == 0
    assert tracker.legs['uuid'].observed_seconds == 0
    assert rows[-1]['stalled_seconds'] == 0


def test_continuous_source_switch_is_a_single_failover_after_initial_tuning():
    tracker = PlaybackTracker()
    observe(tracker, 60)
    changed = tracker.consume([snapshot(stream_id=2, total_bytes=200)], 1070, 10)
    assert len(changed) == 1
    assert changed[0]['stream_id'] == 1
    assert changed[0]['failovers'] == 1
    assert tracker.consume([snapshot(stream_id=2, total_bytes=300)], 1080, 10)[0]['failovers'] == 0
    tracker = PlaybackTracker()
    observe(tracker, 10)
    assert tracker.consume([snapshot(stream_id=2)], 1020, 10) == []


def test_sustained_stall_has_grace_and_counts_once_until_recovery():
    tracker = PlaybackTracker()
    observe(tracker, 60)
    for now in (1070, 1080):
        row = tracker.consume([snapshot(total_bytes=160)], now, 10)[0]
        assert row['stalls'] == row['stalled_seconds'] == 0
    row = tracker.consume([snapshot(total_bytes=160)], 1090, 10)[0]
    assert row['stalls'] == 1 and row['stalled_seconds'] == 30
    row = tracker.consume([snapshot(total_bytes=160)], 1100, 10)[0]
    assert row['stalls'] == 1 and row['stalled_seconds'] == 40
    row = tracker.consume([snapshot(total_bytes=300)], 1110, 10)[0]
    assert row['stalled_seconds'] == 40
    for now in (1120, 1130, 1140): row = tracker.consume([snapshot(total_bytes=300)], now, 10)[0]
    assert row['stalls'] == 2 and row['stalled_seconds'] == 70


def test_long_watch_rotates_checkpoints_without_breaking_continuity():
    tracker = PlaybackTracker()
    rows = observe(tracker, 1250)
    final = {row['id']: row for row in rows}
    assert len(final) == 3
    assert sum(r['observed_seconds'] for r in final.values()) == 1250
    assert max(r['observed_seconds'] for r in final.values()) <= 600
    assert sum(r['samples'] for r in final.values()) == 125


def fake_udi():
    udi = Mock()
    udi.get_channels.return_value = [{'id': 10, 'uuid': 'uuid', 'auto_created_by_name': 'Teamarr'}]
    udi.get_channel_by_id.return_value = {'id': 10, 'streams': [1, 2]}
    udi.get_stream_by_id.side_effect = lambda sid: {'id': sid, 'name': f'Source {sid}', 'url': f'https://provider.invalid/{sid}'}
    udi._shadow_watcher_user_agent.return_value = 'CustomWatcher'
    udi._get_proxy_status.return_value = {'uuid': {'stream_id': 1, 'started_at': 900,
        'total_bytes': 100, 'clients': [{'client_id': 'viewer', 'user_agent': 'VLC'}]}}
    return udi


def service(enabled=True, repository=None):
    db = Mock()
    db.get_system_setting.return_value = {'enabled': enabled}
    db.set_system_setting.return_value = True
    clock = Mock(return_value=1000)
    udi = fake_udi()
    result = PlaybackStabilityService(db=db, repository=repository or PlaybackStabilityRepository(), get_udi=lambda: udi, clock=clock)
    return result, udi, clock


def test_defaults_and_disabled_upgrades_do_no_polling_or_writes():
    disabled, udi, _ = service(enabled=False)
    assert disabled.get_config()['enabled'] is False
    assert not disabled.start() and disabled._thread is None
    assert not disabled.run_once()
    assert disabled.scoring_snapshot(10) == {}
    assert disabled.get_status()['streams'] == []
    udi._get_proxy_status.assert_not_called()
    with get_session() as session:
        assert session.query(PlaybackObservation).count() == 0


@pytest.mark.parametrize('override', [
    {'clients': []}, {'clients': [{'client_id': 'watcher', 'user_agent': 'CustomWatcher/1.0'}]},
    {'clients': [{'client_id': 'probe', 'user_agent': 'StreamFlow/1.0'}]},
    {'clients': [{'client_id': 'unknown'}]}, {'clients': None, 'client_count': 1},
    {'total_bytes': None}, {'total_bytes': -1}, {'total_bytes': float('nan')},
    {'started_at': None}, {'started_at': float('inf')}, {'stream_id': None},
])
def test_technical_unknown_or_malformed_playbacks_are_not_evidence(override):
    s, udi, _ = service()
    udi._get_proxy_status.return_value['uuid'].update(override)
    assert s.run_once()
    assert not s.tracker.legs


def test_teamarr_and_regular_streams_follow_identical_opt_in_rules():
    s, udi, clock = service()
    for offset in range(0, 611, 10):
        clock.return_value = 1000 + offset
        udi._get_proxy_status.return_value['uuid']['total_bytes'] = 100 + offset
        assert s.run_once()
    assert s.scoring_snapshot(10) == {1: 1.0}
    udi._get_proxy_status.assert_called_with(require_authoritative=True)
    status = s.get_status()
    assert status['streams'][0]['observed_seconds'] == 610
    assert status['streams'][0]['eligible'] is True
    assert 'source_fingerprint' not in status['streams'][0]
    assert 'clients' not in str(status['streams']) and 'provider.invalid' not in str(status)


def test_api_failure_and_resume_never_bridge_the_unknown_interval():
    s, udi, clock = service()
    assert s.run_once()
    clock.return_value = 1010
    udi._get_proxy_status.return_value['uuid']['total_bytes'] = 200
    assert s.run_once()
    udi._get_proxy_status.side_effect = RuntimeError('credentials must not leak')
    clock.return_value = 1020
    assert not s.run_once()
    assert s.scoring_snapshot(10) == {} and not s.tracker.legs
    assert 'credentials' not in str(s.get_status())
    udi._get_proxy_status.side_effect = None
    clock.return_value = 1030
    assert s.run_once()
    assert s.tracker.legs['uuid'].observed_seconds == 0
    assert s.get_status()['streams'][0]['observed_seconds'] == 10


def test_disable_during_a_poll_fences_all_pending_writes():
    repository = Mock()
    s, udi, _ = service(repository=repository)
    entered = threading.Event()
    finish = threading.Event()
    def delayed(**kwargs):
        entered.set()
        assert finish.wait(2)
        return fake_udi()._get_proxy_status.return_value
    udi._get_proxy_status.side_effect = delayed
    thread = threading.Thread(target=s.run_once)
    thread.start()
    assert entered.wait(2)
    s.update_config({'enabled': False})
    finish.set()
    thread.join(2)
    assert not thread.is_alive()
    repository.save.assert_not_called()


def test_restart_keeps_only_confirmed_history_and_never_invents_a_failure():
    s, udi, clock = service()
    assert s.run_once()
    clock.return_value = 1010
    udi._get_proxy_status.return_value['uuid']['total_bytes'] = 200
    assert s.run_once()
    restarted, _, later = service()
    later.return_value = 1050
    assert restarted.run_once()
    assert restarted.get_status()['streams'][0]['observed_seconds'] == 10
    assert restarted.get_status()['streams'][0]['failovers'] == 0


def test_old_and_changed_sources_cannot_inherit_a_stability_score():
    s, udi, clock = service()
    assert s.run_once()
    clock.return_value = 1010
    udi._get_proxy_status.return_value['uuid']['total_bytes'] = 200
    assert s.run_once()
    udi.get_stream_by_id.side_effect = lambda sid: {'url': 'https://different.invalid/new'}
    assert s.scoring_snapshot(10) == {} and s.get_status()['streams'] == []
    clock.return_value = 1000 + 15 * 86400
    assert s.run_once()
    with get_session() as session:
        assert session.query(PlaybackObservation).count() == 0


@pytest.mark.parametrize('payload', [{'enabled': 'true'}, {'enabled': 1}, {'retention_days': 31}, {'retention_days': True},
                                      {'poll_interval_seconds': 1}, {'poll_interval_seconds': 5.5}, {'unknown': True}, None])
def test_invalid_settings_cannot_overwrite_config(payload):
    s, _, _ = service(enabled=False)
    with pytest.raises(ValueError): s.update_config(payload)
    s.db.set_system_setting.assert_not_called()
    assert not s.get_config()['enabled']


def test_cross_channel_history_aggregates_only_the_same_source():
    repository = PlaybackStabilityRepository()
    fingerprint = source_fingerprint(fake_udi().get_stream_by_id(1))
    rows = [dict(id=str(cid), channel_id=cid, stream_id=1, source_fingerprint=fingerprint,
                 last_seen=1000, observed_seconds=300, stalled_seconds=30, samples=30, stalls=1, failovers=0) for cid in (10, 11)]
    repository.save(rows, 0)
    s, _, _ = service(repository=repository)
    assert s.run_once()
    assert s.scoring_snapshot(10) == {1: 0.9}
    assert s.get_status()['streams'][0]['eligible'] is True


class Scorer(CheckerClassificationMixin):
    config = {'scoring.weights': {'bitrate': .4, 'resolution': .35, 'fps': .15, 'codec': .1, 'hdr': 0}}
    def _is_stream_dead(self, data): return data.get('dead', False), 'offline'
    def _bitrate_payload_value(self, value): return value


def quality():
    return {'stream_id': 1, 'bitrate_kbps': 8000, 'resolution': '3840x2160', 'fps': 60, 'video_codec': 'hevc'}


@pytest.mark.parametrize('weights', [None, {}, {'use_playback_stability': False}, {'use_playback_stability': True},
    {'use_playback_stability': True, '_playback_stability': {2: 0}},
    {'use_playback_stability': True, '_playback_stability': {1: None}},
    {'use_playback_stability': True, '_playback_stability': {1: 1}},
    {'use_playback_stability': True, '_playback_stability': {1: 0}, 'playback_stability_weight': 0}])
def test_absent_disabled_unobserved_or_stable_history_is_exactly_neutral(weights):
    scorer = Scorer()
    base = scorer._calculate_stream_score(quality(), scoring_weights=None if weights is None else {})
    assert scorer._calculate_stream_score(quality(), scoring_weights=weights) == base


def test_unstable_history_deduction_is_bounded_and_dead_streams_stay_dead():
    scorer = Scorer()
    weights = {'use_playback_stability': True, 'playback_stability_weight': .2, '_playback_stability': {1: 0}}
    base = scorer._calculate_stream_score(quality(), scoring_weights={})
    assert scorer._calculate_stream_score(quality(), scoring_weights=weights) == round(base * .8, 2)
    assert scorer._calculate_stream_score({**quality(), 'dead': True}, scoring_weights=weights) == 0


def test_scoring_loads_one_snapshot_and_drops_injected_profile_evidence(monkeypatch):
    from apps.stream import playback_stability_service as module
    s = Mock()
    s.scoring_snapshot.return_value = {1: .5}
    monkeypatch.setattr(module, 'get_playback_stability_service', lambda: s)
    weights = {'use_playback_stability': True, '_playback_stability': {1: 0}}
    prepared = Scorer()._prepare_playback_scoring(weights, 10)
    for _ in range(10): Scorer()._calculate_stream_score(quality(), scoring_weights=prepared)
    s.scoring_snapshot.assert_called_once_with(10)
    assert prepared['_playback_stability'] == {1: .5}
    assert weights['_playback_stability'] == {1: 0}


def test_api_returns_errors_without_secrets_and_validates_payloads():
    s, _, _ = service(enabled=False)
    with Flask(__name__).app_context():
        response, status = playback_stability_config_response(method='PUT', payload={'enabled': 'yes'}, get_service=lambda: s)
        assert status == 400
        response, status = playback_stability_config_response(method='GET', get_service=lambda: s)
        assert status == 200 and response.json['enabled'] is False
        response, status = playback_stability_status_response(get_service=lambda: s)
        assert status == 200 and response.json['streams'] == []


def test_profile_roundtrip_keeps_scoring_opt_in_and_legacy_profiles_unchanged():
    from apps.automation.automation_config_manager import AutomationConfigManager
    manager = AutomationConfigManager()
    legacy = manager.create_profile({'name': 'Legacy', 'scoring_weights': {'bitrate': .4}})
    assert 'use_playback_stability' not in manager.get_profile(legacy)['scoring_weights']
    weights = {'bitrate': .4, 'use_playback_stability': True, 'playback_stability_weight': .2}
    assert manager.update_profile(legacy, {'scoring_weights': weights})
    assert manager.get_profile(legacy)['scoring_weights'] == weights


def test_settings_persist_across_service_reconstruction_and_stop_stays_off(monkeypatch):
    from apps.database.manager import get_db_manager
    monkeypatch.setattr(PlaybackStabilityService, 'start', lambda self: True)
    s = PlaybackStabilityService(get_udi=fake_udi)
    assert s.update_config({'enabled': True, 'retention_days': 7})['enabled'] is True
    assert PlaybackStabilityService(get_udi=fake_udi).get_config()['retention_days'] == 7
    s.update_config({'enabled': False})
    s.stop()
    assert get_db_manager().get_system_setting('playback_stability')['enabled'] is False


def test_real_flask_routes_cover_configuration_validation_and_history(monkeypatch):
    import apps.api.web_api as web
    s, _, _ = service(enabled=False)
    monkeypatch.setattr(web, 'get_playback_stability_service', lambda: s)
    client = web.app.test_client()
    assert client.get('/api/playback-stability/config').json['enabled'] is False
    assert client.put('/api/playback-stability/config', json={'enabled': 'invalid'}).status_code == 400
    assert client.put('/api/playback-stability/config', json={'retention_days': 7}).json['retention_days'] == 7
    assert client.get('/api/playback-stability/status').json['streams'] == []


def test_stale_polls_cannot_supply_scoring_evidence():
    s, _, clock = service()
    assert s.run_once()
    clock.return_value = 1030
    assert s.scoring_snapshot(10) == {}
    assert s.get_status()['recording'] is False


def test_score_labels_are_cleared_when_stability_is_disabled():
    data = {**quality(), 'playback_stability_score': 0}
    Scorer()._calculate_stream_score(data)
    assert 'playback_stability_score' not in data


def test_history_sample_count_and_duration_are_both_required():
    repository = PlaybackStabilityRepository()
    fingerprint = source_fingerprint(fake_udi().get_stream_by_id(1))
    for duration, samples in ((600, 59), (599, 60), (600, 60)):
        repository.save([dict(id='one', channel_id=10, stream_id=1, source_fingerprint=fingerprint,
                             last_seen=1000, observed_seconds=duration, stalled_seconds=0,
                             samples=samples, stalls=0, failovers=0)], 0)
        row = repository.summaries(0, aggregate_streams=True)[0]
        assert row['eligible'] == (duration >= 600 and samples >= 60)


def test_legacy_retry_baseline_is_neutral_without_source_evidence():
    # Retry classification historically uses global weights even in a profile.
    # Its passive deduction must not accidentally replace those weights.
    baseline = Scorer()._calculate_stream_score(quality())
    weights = {'bitrate': 0, 'resolution': 0, 'fps': 0, 'codec': 0,
               'use_playback_stability': True, '_playback_stability': {2: 0}}
    assert Scorer()._apply_playback_stability_score(baseline, quality(), weights) == baseline
    weights['_playback_stability'] = {1: 0}
    assert Scorer()._apply_playback_stability_score(baseline, quality(), weights) == baseline * .85
