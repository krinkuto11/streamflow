"""Identity reuse, mapping collisions and fail-closed restore history contracts."""

import copy
import json
import sqlite3
from contextlib import closing

import pytest

from .test_backups import persistent, rows, make
from apps.backups.inventory import normalize_inventory, INVENTORY_FILE
from apps.backups.review import build_report, apply_mappings
from apps.backups.review import preview_mappings, validate_mappings, load_source
from apps.backups.inventory import inventory_token, REVIEW_KEY
from .test_backups import service, finish


@pytest.fixture
def inventory(persistent):
    value = normalize_inventory(providers=[{'id':5,'name':'Provider A','server_url':'https://example.invalid/list/key'},
                                           {'id':9,'name':'Provider B','server_url':'https://example.invalid/second'}],
                                channels=[{'id':1,'name':'Sample Channel','uuid':'channel-a'}], groups=[],channel_profiles=[],
                                streams=[{'id':2,'name':'Sample source','url':'https://example.invalid/stream','m3u_account':5}])
    (persistent / INVENTORY_FILE).write_text(json.dumps(value))
    with closing(sqlite3.connect(persistent / 'streamflow.db')) as db:
        db.execute('UPDATE stream_telemetry SET provider_id=5')
        db.execute('UPDATE playback_observations SET source_fingerprint=?',(value['streams'][0]['identity'],));db.commit()
    return value


def plan():
    return {'providers':{'5':5,'9':9},'channels':{'1':1},'groups':{},'channel_profiles':{}}


def test_provider_rename_same_identity_keeps_all_regex_and_history(persistent,inventory):
    current=copy.deepcopy(inventory);current['providers'][0]['name']='Renamed provider'
    report=build_report(persistent,inventory,current,'a'*64)
    assert all(row['status']=='verified' for row in report['entities']['providers'])
    regex=rows(persistent/'streamflow.db','channel_regex_patterns')
    result=apply_mappings(persistent,inventory,current,plan())
    assert result['foreign'] is False
    assert result['history']['stream_telemetry']=={'kept':1,'removed':0}
    assert rows(persistent/'streamflow.db','channel_regex_patterns')==regex
    assert len(rows(persistent/'streamflow.db','playback_observations'))==1


def test_reused_identifiers_do_not_suggest_or_keep_wrong_measurements(persistent,inventory):
    current=copy.deepcopy(inventory)
    current['providers'][0].update(name='Different provider',identity='c'*64)
    current['channels'][0].update(name='Different channel',identity='d'*64,uuid='different')
    current['streams'][0]['identity']='e'*64
    report=build_report(persistent,inventory,current,'a'*64)
    assert report['entities']['providers'][0]['status']=='unresolved'
    assert report['entities']['channels'][0]['status']=='unresolved'
    result=apply_mappings(persistent,inventory,current,plan())
    assert result['foreign']
    assert result['history']['stream_telemetry']=={'kept':0,'removed':1}
    assert not rows(persistent/'streamflow.db','playback_observations')


def test_verified_changed_ids_remap_regex_and_quality_rows(persistent,inventory):
    current=copy.deepcopy(inventory)
    current['providers'][0]['id']=15;current['channels'][0]['id']=11
    current['streams'][0].update(id=12,provider_id=15)
    mappings=plan();mappings['providers']['5']=15;mappings['channels']['1']=11
    result=apply_mappings(persistent,inventory,current,mappings)
    assert result['foreign'] and result['history']['stream_telemetry']['kept']==1
    with closing(sqlite3.connect(persistent/'streamflow.db')) as db:
        assert db.execute('SELECT channel_id,stream_id,provider_id FROM stream_telemetry').fetchone()==(11,12,15)
        assert db.execute('SELECT channel_id,m3u_accounts,pattern,step_order FROM channel_regex_patterns').fetchone()==('11','[15, 9]',r'(?i)^Sample\s+Channel(?:\s+HD)?$',1)


def test_skipped_provider_restriction_never_becomes_all_providers(persistent,inventory):
    mappings=plan();mappings['providers']={'5':None,'9':None}
    apply_mappings(persistent,inventory,inventory,mappings)
    assert not rows(persistent/'streamflow.db','channel_regex_patterns')


@pytest.mark.parametrize('change', ['channel', 'provider', 'stream', 'playback_fingerprint'])
def test_preview_matches_applied_history_and_is_read_only(persistent, inventory, change):
    current = copy.deepcopy(inventory)
    if change == 'channel': current['channels'][0]['identity'] = 'c' * 64
    if change == 'provider': current['providers'][0]['id'] = 15
    if change == 'stream': current['streams'][0]['id'] = 12
    if change == 'playback_fingerprint':
        with closing(sqlite3.connect(persistent/'streamflow.db')) as db:
            db.execute("UPDATE playback_observations SET source_fingerprint=?", ('c'*64,)); db.commit()
    mappings = plan()
    if change == 'provider': mappings['providers']['5'] = 15
    before = (persistent/'streamflow.db').read_bytes()
    preview = preview_mappings(persistent, inventory, current, mappings)
    assert (persistent/'streamflow.db').read_bytes() == before
    result = apply_mappings(persistent, inventory, current, mappings)
    assert preview['history'] == result['history']
    assert preview['foreign'] == result['foreign']
    if change == 'stream': assert not preview['monitoring_history_kept']


def test_legacy_backup_requires_manual_configuration_and_removes_unverified_history(persistent, inventory):
    (persistent/INVENTORY_FILE).unlink()
    source = load_source(persistent)
    report = build_report(persistent, source, inventory, 'a'*64)
    assert report['legacy_inventory']
    assert report['entities']['channels'][0]['status'] == 'review'
    result = apply_mappings(persistent, source, inventory, plan())
    assert result['history']['stream_telemetry'] == {'kept':0, 'removed':1}
    assert rows(persistent/'streamflow.db', 'channel_regex_patterns')


@pytest.mark.parametrize('invalid', ['missing_category', 'missing_entry', 'nonexistent', 'boolean', 'extra'])
def test_mapping_requires_explicit_complete_valid_targets(persistent, inventory, invalid):
    mappings = plan()
    if invalid == 'missing_category': mappings.pop('groups')
    if invalid == 'missing_entry': mappings['providers'].pop('9')
    if invalid == 'nonexistent': mappings['channels']['1'] = 9999
    if invalid == 'boolean': mappings['channels']['1'] = True
    if invalid == 'extra': mappings['channels']['555'] = 1
    with pytest.raises(ValueError): validate_mappings(persistent, inventory, inventory, mappings)


def test_channel_target_collisions_rejected_before_writes(persistent, inventory):
    inventory['channels'].append({**inventory['channels'][0], 'id':3, 'uuid':'other', 'identity':'c'*64})
    with closing(sqlite3.connect(persistent/'streamflow.db')) as db:
        db.execute("INSERT INTO channel_regex_configs VALUES ('3','Other',1,0)"); db.commit()
    mappings = plan(); mappings['channels']['3'] = 1
    before = (persistent/'streamflow.db').read_bytes()
    with pytest.raises(ValueError): apply_mappings(persistent, inventory, inventory, mappings)
    assert (persistent/'streamflow.db').read_bytes() == before


def test_duplicate_urls_are_not_merged_without_unique_owner_and_identity(persistent, inventory):
    current = copy.deepcopy(inventory)
    current['streams'] = [{**current['streams'][0], 'id':12}, {**current['streams'][0], 'id':13}]
    result = apply_mappings(persistent, inventory, current, plan())
    assert result['history']['stream_telemetry']['removed'] == 1


def test_skip_scopes_disables_rules_and_preserves_local_profile_ids(persistent, inventory):
    with closing(sqlite3.connect(persistent/'streamflow.db')) as db:
        settings = {'enabled':True, 'channel_ids':[1], 'profile_id':3, 'queue':{'start_mode':'channel','start_channel_id':1}}
        db.execute('INSERT INTO system_settings VALUES (?,?)', ('stream_checker_config', json.dumps(settings)))
        db.execute('UPDATE automation_profiles SET extra_settings=?', (json.dumps({'profile_id':3, 'm3u_accounts':[5], 'included_channel_ids':[1]}),))
        db.commit()
    mappings = plan(); mappings['channels']['1'] = None
    apply_mappings(persistent, inventory, inventory, mappings)
    settings = dict(rows(persistent/'streamflow.db', 'system_settings'))
    rewritten = json.loads(settings['stream_checker_config'])
    assert rewritten['enabled'] is False
    assert rewritten['profile_id'] == 3 and rewritten['queue']['start_mode'] == 'first'
    with closing(sqlite3.connect(persistent/'streamflow.db')) as db:
        enabled, raw = db.execute('SELECT enabled,extra_settings FROM automation_profiles').fetchone()
        assert not enabled and json.loads(raw)['profile_id'] == 3


def test_skipping_all_global_provider_filter_rejected(persistent, inventory):
    with closing(sqlite3.connect(persistent/'streamflow.db')) as db:
        db.execute('INSERT INTO system_settings VALUES (?,?)', ('enabled_m3u_accounts','[5,9]')); db.commit()
    mappings = plan(); mappings['providers'] = {'5':None,'9':None}
    before = (persistent/'streamflow.db').read_bytes()
    with pytest.raises(ValueError, match='broaden'): apply_mappings(persistent, inventory, inventory, mappings)
    assert (persistent/'streamflow.db').read_bytes() == before


def test_token_ignores_list_order_but_rejects_identity_or_connection_changes(inventory):
    current = copy.deepcopy(inventory); current['providers'].reverse()
    assert inventory_token(current, 'connection') == inventory_token(inventory, 'connection')
    assert inventory_token(current, 'connection2') != inventory_token(inventory, 'connection')
    current['streams'][0]['identity'] = 'c'*64
    assert inventory_token(current, 'connection') != inventory_token(inventory, 'connection')


def review_service(persistent, inventory, monkeypatch, **kwargs):
    from apps.backups import review_workflow
    monkeypatch.setattr(review_workflow, 'connection_token', lambda:'connection')
    svc = service(persistent, fetch_inventory=lambda:copy.deepcopy(inventory), restart=lambda:None, **kwargs)
    svc.db.values[REVIEW_KEY] = True
    svc.backup_dir.mkdir(exist_ok=True)
    svc.check_review(); assert finish(svc)['state'] == 'completed'
    return svc


def test_stale_inventory_remains_paused_without_pending_stage(persistent, inventory, monkeypatch):
    from apps.backups.restore import PENDING
    svc = review_service(persistent, inventory, monkeypatch)
    token = svc._review_report['token']
    inventory['providers'][0]['name'] = 'Changed after comparison'
    svc.confirm_review(token, plan())
    assert finish(svc)['state'] == 'failed'
    assert svc.review_pending() and svc._review_report is None
    assert not (persistent/PENDING).exists()


def test_failed_approval_journal_is_cancelled_without_restart(persistent, inventory, monkeypatch):
    from apps.backups import review_workflow
    from apps.backups.restore import PENDING
    svc = review_service(persistent, inventory, monkeypatch)
    restarted = []; svc.restart = lambda:restarted.append(True)
    monkeypatch.setattr(review_workflow, 'atomic_write_json', lambda *a,**kw:(_ for _ in ()).throw(OSError('disk full')))
    svc.confirm_review(svc._review_report['token'], plan())
    assert finish(svc)['state'] == 'failed' and not restarted
    assert not (persistent/PENDING).exists() and not list(persistent.glob('.restore-stage-*'))
    assert svc.review_pending()


def test_approved_restore_unpauses_only_from_local_journal(persistent, inventory, monkeypatch):
    from apps.backups.restore import PENDING, apply_pending_restore, stage_restore
    # A forged archived setting cannot suppress the initial review.
    with closing(sqlite3.connect(persistent/'streamflow.db')) as db:
        db.execute('INSERT INTO system_settings VALUES (?,?)', (REVIEW_KEY, 'false')); db.commit()
    path = make(persistent, inventory=inventory)
    stage_restore(persistent, path); apply_pending_restore(persistent)
    assert dict(rows(persistent/'streamflow.db','system_settings'))[REVIEW_KEY] == 'true'
    assert (persistent/INVENTORY_FILE).exists()
    svc = review_service(persistent, inventory, monkeypatch)
    svc.confirm_review(svc._review_report['token'], plan())
    assert finish(svc)['state'] == 'completed'
    assert json.loads((persistent/PENDING).read_text())['review_approved'] is True
    apply_pending_restore(persistent)
    assert dict(rows(persistent/'streamflow.db','system_settings'))[REVIEW_KEY] == 'false'
    assert len(rows(persistent/'streamflow.db','stream_telemetry')) == 1


@pytest.mark.parametrize('payload', [None, {}, {'token':'a'*64,'mappings':{}}, {'confirm':True,'token':'bad','mappings':plan()}, {'confirm':False,'token':'a'*64,'mappings':plan()}])
def test_http_rejects_incomplete_confirm_payload(persistent, payload):
    from flask import Flask
    from apps.api.backup_handlers import create_backup_blueprint
    svc = service(persistent)
    app = Flask(__name__); app.register_blueprint(create_backup_blueprint(lambda:svc))
    assert app.test_client().post('/api/backups/review/confirm', json=payload).status_code == 400


def test_review_preview_api_uses_explicit_mapping_without_confirmation_or_writes(persistent, inventory, monkeypatch):
    from flask import Flask
    from apps.api.backup_handlers import create_backup_blueprint
    svc = review_service(persistent, inventory, monkeypatch)
    app = Flask(__name__); app.register_blueprint(create_backup_blueprint(lambda:svc)); client = app.test_client()
    before = (persistent/'streamflow.db').read_bytes()
    response = client.post('/api/backups/review/preview', json={'token':svc._review_report['token'], 'mappings':plan()})
    assert response.status_code == 200 and response.json['data']['history']['stream_telemetry']['kept'] == 1
    assert (persistent/'streamflow.db').read_bytes() == before
    assert client.post('/api/backups/review/preview', json={'token':'b'*64, 'mappings':plan()}).status_code == 400


@pytest.mark.parametrize('response', [None, {}, {'results':[]}, {'results':'bad','count':0}, {'results':[], 'count':1}])
def test_incomplete_dispatcharr_fetch_fails_closed(monkeypatch, response):
    from apps.udi.fetcher import UDIFetcher
    from apps.config import dispatcharr_config
    from apps.backups.inventory import fetch_inventory
    from types import SimpleNamespace
    monkeypatch.setattr(dispatcharr_config, 'get_dispatcharr_config', lambda:SimpleNamespace(is_configured=lambda:True))
    monkeypatch.setattr(UDIFetcher, '__init__', lambda self:setattr(self, 'base_url', 'https://example.invalid'))
    monkeypatch.setattr(UDIFetcher, '_fetch_url', lambda self,url:response if '/channels/channels/' in url else [])
    with pytest.raises(ValueError): fetch_inventory()
