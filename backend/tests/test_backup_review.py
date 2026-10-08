"""Identity reuse, mapping collisions and fail-closed restore history contracts."""

import copy
import json
import sqlite3
from contextlib import closing

import pytest

from .test_backups import persistent, rows, make
from apps.backups.inventory import normalize_inventory, INVENTORY_FILE
from apps.backups.review import build_report, apply_mappings


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
