"""Real SQLite/WAL archive contracts, fault recovery, scheduling and HTTP boundaries."""

import copy
import io
import json
import sqlite3
import threading
import time
import zipfile
from collections import deque
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from flask import Flask
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from apps.api.backup_handlers import create_backup_blueprint
from apps.backups import archive, restore
from apps.backups.history import capture_monitoring_history, restore_monitoring_history, validate_history
from apps.backups.schedule import next_run, validate_config
from apps.backups.service import BackupBusyError, BackupService
from apps.database.migrations import run_migrations
from apps.database.models import (Base, Channel, Stream, SystemSetting, ChannelRegexConfig,
    ChannelRegexPattern, AutomationProfile, Run, StreamTelemetry, PlaybackObservation, MonitoringSession)
from apps.stream.stream_session_manager import StreamMetrics


@pytest.fixture
def persistent(tmp_path):
    root = tmp_path / 'data'; root.mkdir()
    path = root / 'streamflow.db'
    engine = create_engine(f'sqlite:///{path}')
    Base.metadata.create_all(engine)
    run_migrations(engine, path)
    with Session(engine) as session:
        session.add_all([
            Channel(id=1, name='Sample Channel'), Stream(id=2, name='Sample source', url='https://example.invalid/stream'),
            SystemSetting(key='dispatcharr_config', value={'api_key': 'private-test-key', 'base_url': 'https://example.invalid'}),
            SystemSetting(key='wizard_complete', value=True),
            AutomationProfile(id=3, name='Full Check', extra_settings={'playback_stability_scoring': True}),
            ChannelRegexConfig(channel_id='1', name='Sample Channel', enabled=True, match_by_tvg_id=False),
            ChannelRegexPattern(channel_id='1', pattern=r'(?i)^Sample\s+Channel(?:\s+HD)?$', m3u_accounts=[5,9], step_order=1),
            Run(id=4, raw_details='{"sample":true}'),
            StreamTelemetry(run_id=4, channel_id=1, stream_id=2, resolution_height=2160, resolution_width=3840),
            PlaybackObservation(id='a'*32, channel_id=1, stream_id=2, source_fingerprint='b'*64, last_seen=1000, samples=15),
            MonitoringSession(session_id='sample', stream_id=2, status='active', pid=123, current_speed=1.1, raw_info={'name':'Sample source','url':'https://example.invalid/stream','channel_id':1}),
        ])
        session.commit()
    engine.dispose()
    (root / 'shadow_blank_monitor_config.json').write_text('{"enabled":false,"secret":"test"}')
    (root / 'teamarr_preflight_config.json').write_text('{"enabled":true}')
    (root / 'udi_data.json').write_text('{"cache_only":true}')
    yield root


def rows(path, table):
    with closing(sqlite3.connect(path)) as db:
        return db.execute(f'SELECT * FROM "{table}" ORDER BY 1').fetchall()


def make(root, **kwargs):
    metadata = archive.create_archive(root, root / 'backups', version='test', **kwargs)
    return root / 'backups' / metadata['name']


def rewrite(path, mutate):
    with zipfile.ZipFile(path) as source:
        data = {name: source.read(name) for name in source.namelist()}
    mutate(data)
    target = path.parent / 'streamflow-import-mutated.zip'
    with zipfile.ZipFile(target, 'w', zipfile.ZIP_DEFLATED) as output:
        for name, value in data.items(): output.writestr(name, value)
    return target


def history():
    from dataclasses import asdict
    return {'format_version':1, 'sessions':[{'id':'sample','streams':[{'id':2,'metrics':[asdict(StreamMetrics(timestamp=1000, speed=1.0, bitrate=5000, fps=50, is_alive=True))]}]}]}


def test_complete_archive_preserves_settings_regex_credentials_and_measurements(persistent, tmp_path):
    path = make(persistent, monitoring_history=history())
    destination = tmp_path / 'extract'
    metadata = archive.extract_validated(path, destination)
    assert metadata['schema_version'] == 2
    assert metadata['summary']['playback_observations'] == 1
    assert metadata['summary']['quality_measurements'] == 1
    for table in Base.metadata.tables:
        assert rows(destination / 'streamflow.db', table) == rows(persistent / 'streamflow.db', table)
    assert (destination / 'config/shadow_blank_monitor_config.json').read_bytes() == (persistent / 'shadow_blank_monitor_config.json').read_bytes()
    assert not (destination / 'config/udi_data.json').exists()
    assert json.loads((destination / 'monitoring-history.json').read_text()) == history()
    assert 'private-test-key' not in json.dumps(metadata)


def test_sqlite_snapshot_includes_committed_wal_excludes_uncommitted(persistent, tmp_path):
    with closing(sqlite3.connect(persistent / 'streamflow.db')) as db:
        db.execute('PRAGMA journal_mode=WAL')
        db.execute("INSERT INTO system_settings VALUES ('committed','true')"); db.commit()
        db.execute("INSERT INTO system_settings VALUES ('uncommitted','true')")
        path = make(persistent)
        archive.extract_validated(path, tmp_path / 'extract')
        keys = dict(rows(tmp_path / 'extract/streamflow.db', 'system_settings'))
        assert keys['committed'] == 'true' and 'uncommitted' not in keys
        db.rollback()


def test_history_can_be_omitted_without_changing_source(persistent, tmp_path):
    path = make(persistent, include_history=False)
    metadata = archive.extract_validated(path, tmp_path / 'extract')
    for table in archive.HISTORY_TABLES:
        assert rows(tmp_path / 'extract/streamflow.db', table) == []
    assert metadata['include_history'] is False
    assert len(rows(persistent / 'streamflow.db','playback_observations')) == 1
    assert dict(rows(tmp_path / 'extract/streamflow.db','system_settings'))['dispatcharr_config'].find('private-test-key') >= 0


@pytest.mark.parametrize('name', ['../escape.zip', '/tmp/escape.zip', 'streamflow-backup-x.zip/../x', 'x.zip', None])
def test_backup_names_cannot_escape_storage(tmp_path, name):
    with pytest.raises(ValueError): archive.archive_path(tmp_path, name)


@pytest.mark.parametrize('attack', ['extra','traversal','checksum','format','schema','history','summary','date','history_flag'])
def test_damaged_and_unsupported_archives_rejected(persistent, tmp_path, attack):
    original = make(persistent, monitoring_history=history())
    def mutate(data):
        manifest = json.loads(data['manifest.json'])
        if attack == 'extra': data['unexpected.txt'] = b'no'
        if attack == 'traversal':
            data['../outside.json'] = b'{}'; manifest['files']['../outside.json'] = {'size':2,'sha256':'0'*64}
        if attack == 'checksum': manifest['files']['streamflow.db']['sha256'] = '0'*64
        if attack == 'format': manifest['format_version'] = 999
        if attack == 'schema': manifest['schema_version'] = 999
        if attack == 'summary': manifest['summary']['settings'] = {'invalid':'preview'}
        if attack == 'date': manifest['created_at'] = 'invalid'
        if attack == 'history_flag': manifest['include_history'] = False
        if attack == 'history':
            import hashlib
            data['monitoring-history.json'] = b'{"format_version":1,"sessions":"bad"}'
            manifest['files']['monitoring-history.json'] = {'size':len(data['monitoring-history.json']), 'sha256':hashlib.sha256(data['monitoring-history.json']).hexdigest()}
        data['manifest.json'] = json.dumps(manifest).encode()
    damaged = rewrite(original, mutate)
    before = rows(persistent / 'streamflow.db','system_settings')
    with pytest.raises(ValueError): archive.extract_validated(damaged, tmp_path / 'extract')
    assert rows(persistent / 'streamflow.db','system_settings') == before
    assert not (tmp_path / 'outside.json').exists()


def test_duplicate_entries_and_symlink_entries_rejected(persistent, tmp_path):
    original = make(persistent)
    with zipfile.ZipFile(original,'a') as output:
        with pytest.warns(UserWarning): output.writestr('manifest.json', b'{}')
    with pytest.raises(ValueError): archive.extract_validated(original,tmp_path/'duplicate')
    original = make(persistent)
    with zipfile.ZipFile(original) as source:
        data = {name:source.read(name) for name in source.namelist()}
    target = tmp_path / 'symlink.zip'
    with zipfile.ZipFile(target,'w') as output:
        for name, content in data.items():
            info = zipfile.ZipInfo(name); info.create_system=3
            if name == 'streamflow.db': info.external_attr = (0o120777 << 16)
            output.writestr(info, content)
    with pytest.raises(ValueError, match='Unsupported archive entry'): archive.extract_validated(target,tmp_path/'symlink')


def test_limits_and_invalid_json_leave_no_partial_archive(persistent, monkeypatch):
    (persistent / 'bad.json').write_text('{broken')
    with pytest.raises(ValueError): make(persistent)
    assert not list((persistent/'backups').glob('*.zip'))
    (persistent / 'bad.json').unlink()
    monkeypatch.setattr(archive,'MAX_ARCHIVE_BYTES',100)
    with pytest.raises(ValueError, match='size limit'): make(persistent)
    assert not list((persistent/'backups').glob('*.tmp'))


def test_restore_round_trip_replaces_json_recovery_copies_and_creates_safety(persistent):
    path = make(persistent, monitoring_history=history())
    before = rows(persistent/'streamflow.db','system_settings')
    with closing(sqlite3.connect(persistent/'streamflow.db')) as db:
        db.execute("UPDATE system_settings SET value='false' WHERE key='wizard_complete'"); db.commit()
    (persistent/'shadow_blank_monitor_config.json').write_text('{"changed":true}')
    (persistent/'shadow_blank_monitor_config.json.last-good').write_text('{"stale":true}')
    (persistent/'new_after_backup.json').write_text('{}')
    restore.stage_restore(persistent,path)
    result = restore.apply_pending_restore(persistent)
    assert result['status']=='restored'
    assert [row for row in rows(persistent/'streamflow.db','system_settings') if row[0]!='backup_restore_sql_authoritative'] == before
    with closing(sqlite3.connect(persistent/'streamflow.db')) as db:
        assert db.execute('SELECT pid FROM monitoring_sessions').fetchone()[0] is None
        assert json.loads(db.execute('SELECT raw_info FROM monitoring_sessions').fetchone()[0])['is_active'] is False
    assert (persistent/'shadow_blank_monitor_config.json').read_text() == '{"enabled":false,"secret":"test"}'
    assert (persistent/'shadow_blank_monitor_config.json.last-good').read_bytes() == (persistent/'shadow_blank_monitor_config.json').read_bytes()
    assert not (persistent/'new_after_backup.json').exists()
    assert json.loads((persistent/'monitoring_history.json').read_text()) == history()
    assert (persistent/'backups'/result['safety_backup']).is_file()
    assert not (persistent/restore.PENDING).exists()
    assert not list(persistent.glob('.restore-stage-*'))


@pytest.mark.parametrize('crash', [False,True])
def test_partial_restore_and_interrupted_boot_recover_previous_state(persistent, monkeypatch, crash):
    path = make(persistent)
    with closing(sqlite3.connect(persistent/'streamflow.db')) as db:
        db.execute("UPDATE system_settings SET value='false' WHERE key='wizard_complete'"); db.commit()
    (persistent/'teamarr_preflight_config.json').write_text('{"current":true}')
    previous = rows(persistent/'streamflow.db','system_settings')
    restore.stage_restore(persistent,path)
    original = restore._replace
    calls = []
    def fail(source,target):
        if target.parent == persistent and target.name == 'teamarr_preflight_config.json':
            calls.append(target)
            if len(calls)==1:
                raise SystemExit('power loss') if crash else OSError('disk failure')
        original(source,target)
    monkeypatch.setattr(restore,'_replace',fail)
    if crash:
        with pytest.raises(SystemExit): restore.apply_pending_restore(persistent)
        monkeypatch.setattr(restore,'_replace',original)
        result = restore.apply_pending_restore(persistent)
    else: result = restore.apply_pending_restore(persistent)
    assert result['status']=='rolled_back'
    assert rows(persistent/'streamflow.db','system_settings') == previous
    assert (persistent/'teamarr_preflight_config.json').read_text() == '{"current":true}'
    assert not (persistent/restore.PENDING).exists()


def test_restore_rejects_modified_staging_before_touching_live_state(persistent):
    path=make(persistent); before=rows(persistent/'streamflow.db','system_settings')
    restore.stage_restore(persistent,path)
    stage=restore._stage_path(persistent,json.loads((persistent/restore.PENDING).read_text())['stage'])
    (stage/'incoming/config/teamarr_preflight_config.json').write_text('{}')
    assert restore.apply_pending_restore(persistent)['status']=='failed'
    assert rows(persistent/'streamflow.db','system_settings')==before


def test_monitoring_snapshot_is_bounded_and_only_hydrates_stopped_sessions(tmp_path):
    metrics=deque(StreamMetrics(timestamp=float(i),speed=1.0,bitrate=5000,fps=50,is_alive=True) for i in range(1200))
    stream=SimpleNamespace(metrics_history=metrics)
    session=SimpleNamespace(streams={2:stream},is_active=False)
    manager=SimpleNamespace(sessions={'sample':session},session_locks={'sample':threading.Lock()},_save_sessions=lambda **kw:True)
    payload=capture_monitoring_history(manager)
    assert len(payload['sessions'][0]['streams'][0]['metrics'])==1000
    (tmp_path/'monitoring_history.json').write_text(json.dumps(payload))
    stream.metrics_history.clear(); restore_monitoring_history(manager,tmp_path)
    assert len(stream.metrics_history)==1000 and stream.metrics_history[0].timestamp==200
    stream.metrics_history.clear(); session.is_active=True
    (tmp_path/'monitoring_history.json').write_text(json.dumps(payload))
    restore_monitoring_history(manager,tmp_path); assert not stream.metrics_history


@pytest.mark.parametrize('value',[float('nan'),float('inf'),'1.0',True])
def test_invalid_measurement_values_rejected(value):
    payload=history(); payload['sessions'][0]['streams'][0]['metrics'][0]['speed']=value
    with pytest.raises(ValueError): validate_history(payload)


def stamp(value): return datetime.fromisoformat(value).replace(tzinfo=timezone.utc).timestamp()


@pytest.mark.parametrize('settings,now,expected',[
    ({'frequency':'daily','time':'03:00','timezone':'Europe/Berlin'},'2026-10-08T00:00:00','2026-10-08T01:00:00'),
    ({'frequency':'weekly','weekday':0,'time':'03:00','timezone':'Europe/Berlin'},'2026-10-08T00:00:00','2026-10-12T01:00:00'),
    ({'frequency':'interval','interval_hours':3},'2026-10-08T00:00:00','2026-10-08T03:00:00'),
    ({'time':'02:30','timezone':'Europe/Berlin'},'2026-03-29T00:00:00','2026-03-30T00:30:00'),
    ({'time':'02:30','timezone':'Europe/Berlin'},'2026-10-25T00:31:00','2026-10-26T01:30:00'),
])
def test_schedule_timezone_weekly_interval_and_dst(settings,now,expected):
    assert next_run(validate_config(settings),stamp(now))==stamp(expected)


@pytest.mark.parametrize('settings',[{'enabled':'true'},{'retention':0},{'retention':True},{'interval_hours':169},{'weekday':7},{'time':'9:00'},{'time':'24:00'},{'timezone':'Fake/Zone'},{'unknown':1}])
def test_schedule_validation(settings):
    with pytest.raises(ValueError): validate_config(settings)


class MemoryDB:
    def __init__(self): self.values={}; self.fail=False
    def get_system_setting(self,key,default=None): return copy.deepcopy(self.values.get(key,default))
    def set_system_setting(self,key,value):
        if self.fail: return False
        self.values[key]=copy.deepcopy(value); return True
    def set_system_settings_multi(self,values):
        if self.fail: return False
        self.values.update(copy.deepcopy(values)); return True


def service(root,**kwargs): return BackupService(config_dir=root,db=MemoryDB(),**kwargs)


def finish(svc):
    deadline=time.monotonic()+5
    while svc._operation_lock.locked() and time.monotonic()<deadline: time.sleep(.01)
    assert not svc._operation_lock.locked()
    return svc.operation


def test_manual_backup_retention_and_concurrent_requests(persistent):
    waiting=threading.Event(); release=threading.Event()
    def capture(): waiting.set(); assert release.wait(3); return history()
    svc=service(persistent,capture_history=capture)
    svc.update_config({'retention':2})
    svc.create(); assert waiting.wait(2)
    with pytest.raises(BackupBusyError): svc.create()
    with pytest.raises(BackupBusyError): svc.update_config({'retention':3})
    release.set(); assert finish(svc)['state']=='completed'
    svc.create(False); finish(svc); svc.create(False); finish(svc)
    assert len(svc.list_backups())==2
    assert all('private-test-key' not in json.dumps(item) for item in svc.list_backups())


def test_disabled_schedule_catchup_and_failure_backoff(persistent,monkeypatch):
    now=[1000.0]; svc=service(persistent,clock=lambda:now[0])
    svc.tick(); assert svc.operation['state']=='idle'
    svc.update_config({'enabled':True,'frequency':'interval','interval_hours':1})
    assert svc.db.values['backup_schedule_state']['next_run_at']==4600
    now[0]=8000; svc.tick(); assert finish(svc)['state']=='completed'
    assert svc.db.values['backup_schedule_state']['next_run_at']==11600
    def fail(*args): raise OSError('test disk failure')
    monkeypatch.setattr(svc,'_create',fail)
    now[0]=11601; svc.tick(); assert finish(svc)['state']=='failed'
    operation=copy.deepcopy(svc.operation); svc.tick(); assert svc.operation==operation
    assert svc.db.values['backup_schedule_state']['retry_after']==11901


def test_failed_settings_transaction_retains_config(persistent):
    svc=service(persistent); svc.update_config({'retention':5}); before=copy.deepcopy(svc.db.values)
    svc.db.fail=True
    with pytest.raises(RuntimeError): svc.update_config({'enabled':True,'retention':2})
    assert svc.db.values==before


def test_invalid_upload_releases_lock_and_leaves_no_archive(persistent):
    svc=service(persistent)
    with pytest.raises(zipfile.BadZipFile): svc.import_backup(io.BytesIO(b'invalid'))
    assert not svc._operation_lock.locked()
    assert not list(svc.backup_dir.glob('*.zip'))
    svc.create(False); assert finish(svc)['state']=='completed'


def test_restore_busy_guard_and_failed_restart_do_not_leave_pending_state(persistent):
    path=make(persistent); reasons=['stream checks']
    def fail(): raise OSError('exec failed')
    svc=service(persistent,busy_reasons=lambda:reasons,restart=fail)
    with pytest.raises(BackupBusyError): svc.restore(path.name)
    assert not (persistent/restore.PENDING).exists()
    reasons.clear(); svc.restore(path.name)
    assert finish(svc)['state']=='failed' and not (persistent/restore.PENDING).exists()
    assert svc.maintenance is False


def test_http_upload_download_preview_confirmation_and_private_metadata(persistent):
    svc=service(persistent,restart=lambda:None)
    app=Flask(__name__); app.register_blueprint(create_backup_blueprint(lambda:svc)); client=app.test_client()
    assert client.get('/api/backups').json['data']['config']['enabled'] is False
    assert client.post('/api/backups',json={'include_history':'true'}).status_code==400
    assert client.post('/api/backups',json={},headers={'Origin':'https://untrusted.invalid'}).status_code==403
    assert client.put('/api/backups/config',json={'enabled':False,'retention':3}).status_code==200
    path=make(persistent)
    response=client.post('/api/backups/upload',data={'file':(io.BytesIO(path.read_bytes()),'backup.zip')})
    assert response.status_code==201; name=response.json['data']['name']
    preview=client.get(f'/api/backups/{name}/inspect')
    assert preview.status_code==200 and 'private-test-key' not in preview.text
    download=client.get(f'/api/backups/{name}/download')
    assert download.status_code==200 and download.data==path.read_bytes()
    assert download.headers['Cache-Control']=='no-store'
    download.close()
    assert client.post(f'/api/backups/{name}/restore',json={'confirm':'true'}).status_code==400
    assert client.delete(f'/api/backups/{name}',json={}).status_code==400
    assert client.delete(f'/api/backups/{name}',json={'confirm':True}).status_code==200
    assert client.get(f'/api/backups/{name}/inspect').status_code==404
    assert client.post('/api/backups/upload',data={'file':(io.BytesIO(b'bad'),'bad.zip')}).status_code==400
    assert client.post('/api/backups',json={'include_history':False}).status_code==202
    assert finish(svc)['state']=='completed'


def test_failed_rollback_keeps_journal_and_blocks_startup(persistent, monkeypatch):
    path=make(persistent); restore.stage_restore(persistent,path)
    original=restore._replace
    def fail(source,target):
        if target.parent==persistent: raise OSError('storage unavailable')
        original(source,target)
    monkeypatch.setattr(restore,'_replace',fail)
    with pytest.raises(OSError): restore.apply_pending_restore(persistent)
    assert json.loads((persistent/restore.PENDING).read_text())['phase']=='applying'
    monkeypatch.setattr(restore,'_replace',original)
    assert restore.apply_pending_restore(persistent)['status']=='rolled_back'


def test_newer_database_schema_rejected_even_with_valid_checksums(persistent, tmp_path):
    with closing(sqlite3.connect(persistent/'streamflow.db')) as db:
        db.execute("INSERT INTO schema_migrations (version,name,applied_at) VALUES (999,'future','2026-10-08')");db.commit()
    with pytest.raises(ValueError,match='newer StreamFlow'): make(persistent)


def test_configuration_change_during_snapshot_is_retried(persistent, monkeypatch, tmp_path):
    original=archive.snapshot_database; calls=[]
    def changed(*args,**kwargs):
        original(*args,**kwargs);calls.append(1)
        if len(calls)==1: (persistent/'teamarr_preflight_config.json').write_text('{"changed":true}')
    monkeypatch.setattr(archive,'snapshot_database',changed)
    path=make(persistent)
    archive.extract_validated(path,tmp_path/'extract')
    assert len(calls)==2
    assert json.loads((tmp_path/'extract/config/teamarr_preflight_config.json').read_text())=={'changed':True}


def test_busy_race_cancels_restore_and_restarts_previous_state(persistent):
    path=make(persistent); reasons=[]; restarted=[]
    svc=service(persistent,busy_reasons=lambda:reasons,
                stop_runtime=lambda:reasons.append('stream checks'),restart=lambda:restarted.append(True))
    svc.restore(path.name)
    assert finish(svc)['state']=='failed' and restarted==[True]
    assert not (persistent/restore.PENDING).exists()
    assert json.loads((persistent/restore.RESULT).read_text())['finished_at']


def test_storage_failure_releases_import_lock(persistent, monkeypatch):
    svc=service(persistent)
    original=Path.mkdir
    def fail(path,*args,**kwargs):
        if path==svc.backup_dir: raise PermissionError('read only')
        return original(path,*args,**kwargs)
    monkeypatch.setattr(Path,'mkdir',fail)
    with pytest.raises(PermissionError): svc.import_backup(io.BytesIO(b'anything'))
    assert not svc._operation_lock.locked()


def test_restore_safety_retention_is_separate(persistent):
    path=make(persistent)
    for _ in range(4):
        restore.stage_restore(persistent,path)
        assert restore.apply_pending_restore(persistent)['status']=='restored'
    assert len(list((persistent/'backups').glob('streamflow-safety-*.zip')))==3
    assert path.exists()


def test_api_maintenance_gate_blocks_mutations_without_hiding_status(persistent,monkeypatch):
    from apps.api.web_api import app
    from apps.backups import service as module
    svc=service(persistent);svc.maintenance=True
    monkeypatch.setattr(module,'_service',svc)
    client=app.test_client()
    assert client.post('/api/automation/trigger',json={}).status_code==503
    assert client.get('/api/backups').status_code==200


def test_related_database_settings_commit_atomically(clean_test_db,monkeypatch):
    from apps.database.manager import get_db_manager
    manager=get_db_manager();assert manager.set_system_setting('backup_config',{'retention':7})
    real=manager._get_session
    def broken():
        session=real()
        monkeypatch.setattr(session,'commit',lambda:(_ for _ in ()).throw(RuntimeError('test failure')))
        return session
    monkeypatch.setattr(manager,'_get_session',broken)
    assert manager.set_system_settings_multi({'backup_config':{'retention':2},'backup_schedule_state':{'next_run_at':5}}) is False
    monkeypatch.setattr(manager,'_get_session',real)
    assert manager.get_system_setting('backup_config')=={'retention':7}
    assert manager.get_system_setting('backup_schedule_state') is None


def test_legacy_json_import_cannot_overwrite_restored_sql(persistent,monkeypatch):
    import importlib.util
    from sqlalchemy.orm import sessionmaker
    spec=importlib.util.spec_from_file_location('backup_migration_test',Path(__file__).parents[1]/'scripts/migrate_to_sql.py')
    migration=importlib.util.module_from_spec(spec);spec.loader.exec_module(migration)
    path=make(persistent);restore.stage_restore(persistent,path)
    assert restore.apply_pending_restore(persistent)['status']=='restored'
    # Stale files can remain for compatibility, but they must not replace SQL.
    (persistent/'dispatcharr_config.json').write_text('{"api_key":"stale-key"}')
    (persistent/'channel_regex_config.json').write_text('{"patterns":{"1":{"name":"wrong","regex_patterns":[]}}}')
    engine=create_engine(f'sqlite:///{persistent / "streamflow.db"}')
    monkeypatch.setattr(migration,'init_db',lambda:None)
    monkeypatch.setattr(migration,'get_session',sessionmaker(bind=engine))
    monkeypatch.setattr(migration,'CONFIG_DIR',persistent)
    migration.main();engine.dispose()
    assert 'private-test-key' in dict(rows(persistent/'streamflow.db','system_settings'))['dispatcharr_config']
    assert len(rows(persistent/'streamflow.db','channel_regex_patterns'))==1
