"""Existing authoritative SQL must survive removal of restore functionality."""

import importlib.util
import json
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from apps.database.models import Base, SystemSetting, ChannelRegexConfig, ChannelRegexPattern


def test_legacy_json_does_not_overwrite_existing_authoritative_sql(tmp_path, monkeypatch):
    engine = create_engine(f'sqlite:///{tmp_path / "streamflow.db"}')
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add_all([
            SystemSetting(key='backup_restore_sql_authoritative', value=True),
            SystemSetting(key='dispatcharr_config', value={'api_key':'sample-current-key'}),
            ChannelRegexConfig(channel_id='1', name='Sample Channel', enabled=True),
            ChannelRegexPattern(channel_id='1', pattern='^Sample Channel$', m3u_accounts=[5], step_order=0),
        ])
        session.commit()
    (tmp_path/'dispatcharr_config.json').write_text(json.dumps({'api_key':'sample-stale-key'}))
    (tmp_path/'channel_regex_config.json').write_text(json.dumps({'patterns':{'1':{'name':'Stale','regex_patterns':[]}}}))
    spec = importlib.util.spec_from_file_location('sql_authority_migration', Path(__file__).parents[1]/'scripts/migrate_to_sql.py')
    migration = importlib.util.module_from_spec(spec); spec.loader.exec_module(migration)
    monkeypatch.setattr(migration,'init_db',lambda:None)
    monkeypatch.setattr(migration,'get_session',sessionmaker(bind=engine))
    monkeypatch.setattr(migration,'CONFIG_DIR',tmp_path)
    migration.main()
    with Session(engine) as session:
        assert session.get(SystemSetting,'dispatcharr_config').value['api_key']=='sample-current-key'
        assert session.get(ChannelRegexConfig,'1').name=='Sample Channel'
        assert session.query(ChannelRegexPattern).one().pattern=='^Sample Channel$'
    engine.dispose()
