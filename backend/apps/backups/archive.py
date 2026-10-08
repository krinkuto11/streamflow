"""Portable, bounded ZIP snapshots. Never copy a live SQLite/WAL file."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import tempfile
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path

FORMAT_VERSION = 1
MAX_ARCHIVE_BYTES = 1024 ** 3
MAX_EXTRACTED_BYTES = 4 * 1024 ** 3
MAX_JSON_BYTES = 64 * 1024 ** 2
NAME_RE = re.compile(r'streamflow-(backup|safety|import)-[A-Za-z0-9_-]+\.zip\Z')
JSON_NAME_RE = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]*\.json\Z')
EXCLUDED_JSON = {'udi_data.json', 'monitoring_history.json'}
HISTORY_TABLES = ('channel_health', 'stream_telemetry', 'runs', 'playback_observations')
SUMMARY_TABLES = {
    'settings': 'system_settings', 'profiles': 'automation_profiles',
    'periods': 'automation_periods', 'regex_channels': 'channel_regex_configs',
    'regex_patterns': 'channel_regex_patterns', 'monitoring_rows': 'monitoring_sessions',
    'runs': 'runs', 'quality_measurements': 'stream_telemetry',
    'playback_observations': 'playback_observations',
}


def read_json(path):
    if Path(path).stat().st_size > MAX_JSON_BYTES:
        raise ValueError('A configuration file exceeds the backup size limit')
    return json.loads(Path(path).read_text(encoding='utf-8'), parse_constant=_invalid_constant)


def _invalid_constant(value):
    raise ValueError('Non-finite JSON values are not supported')


def digest(path):
    result = hashlib.sha256()
    with Path(path).open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            result.update(chunk)
    return result.hexdigest()


def config_files(config_dir):
    return sorted(path for path in Path(config_dir).glob('*.json')
                  if JSON_NAME_RE.fullmatch(path.name) and path.name not in EXCLUDED_JSON
                  and path.is_file() and not path.is_symlink())


def database_summary(path):
    with sqlite3.connect(path.as_uri() + '?mode=ro&immutable=1', uri=True) as db:
        if db.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
            raise ValueError('The backup database failed its integrity check')
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        required = {'system_settings', 'automation_profiles', 'automation_periods',
                    'channel_regex_configs', 'channel_regex_patterns', 'monitoring_sessions'}
        if not required <= tables:
            raise ValueError('This is not a supported StreamFlow database')
        if db.execute("SELECT 1 FROM sqlite_master WHERE type IN ('trigger','view') LIMIT 1").fetchone():
            raise ValueError('Unsupported database objects in backup')
        version = max((row[0] for row in db.execute('SELECT version FROM schema_migrations')), default=0) if 'schema_migrations' in tables else 0
        from apps.database.migrations import MIGRATIONS
        if version > max(m.version for m in MIGRATIONS):
            raise ValueError('The backup needs a newer StreamFlow database version')
        counts = {label: db.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
                  for label, table in SUMMARY_TABLES.items() if table in tables}
    return version, counts


def snapshot_database(source, target, *, include_history=True):
    if not source.is_file() or source.is_symlink():
        raise ValueError('The StreamFlow database is unavailable')
    with sqlite3.connect(source.as_uri() + '?mode=ro', uri=True, timeout=30) as src:
        with sqlite3.connect(target) as dst:
            src.backup(dst, pages=256, sleep=0.01)
            if not include_history:
                tables = {r[0] for r in dst.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                for table in HISTORY_TABLES:
                    if table in tables:
                        dst.execute(f'DELETE FROM "{table}"')
                dst.commit()
            dst.execute('PRAGMA journal_mode=DELETE')
    os.chmod(target, 0o600)


def archive_path(directory, name):
    if not isinstance(name, str) or not NAME_RE.fullmatch(name):
        raise ValueError('Invalid backup name')
    directory = Path(directory).resolve()
    path = directory / name
    if path.is_symlink() or path.resolve().parent != directory:
        raise ValueError('Invalid backup path')
    return path


def create_archive(config_dir, backup_dir, *, include_history=True, monitoring_history=None,
                   version='unknown', kind='backup'):
    config_dir, backup_dir = Path(config_dir).resolve(), Path(backup_dir).resolve()
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc)
    name = f'streamflow-{kind}-{stamp.strftime("%Y%m%dT%H%M%S%fZ")}-{uuid.uuid4().hex[:8]}.zip'
    final = archive_path(backup_dir, name)
    temporary = backup_dir / ('.' + name + '.tmp')
    try:
        with tempfile.TemporaryDirectory(prefix='.snapshot-', dir=backup_dir) as tmp:
            stage = Path(tmp)
            for attempt in range(3):
                originals = {p.name: p.read_bytes() for p in config_files(config_dir)}
                for content in originals.values():
                    if len(content) > MAX_JSON_BYTES:
                        raise ValueError('A configuration file exceeds the backup size limit')
                    json.loads(content, parse_constant=_invalid_constant)
                snapshot_database(config_dir / 'streamflow.db', stage / 'streamflow.db', include_history=include_history)
                if originals == {p.name: p.read_bytes() for p in config_files(config_dir)}:
                    break
            else:
                raise ValueError('Configuration changed during backup; try again')
            for filename, content in originals.items():
                path = stage / 'config' / filename
                path.parent.mkdir(exist_ok=True)
                path.write_bytes(content)
            if include_history and monitoring_history is not None:
                (stage / 'monitoring-history.json').write_text(json.dumps(monitoring_history, allow_nan=False), encoding='utf-8')
            schema, counts = database_summary(stage / 'streamflow.db')
            files = {p.relative_to(stage).as_posix(): {'size': p.stat().st_size, 'sha256': digest(p)}
                     for p in sorted(stage.rglob('*')) if p.is_file()}
            if sum(item['size'] for item in files.values()) > MAX_EXTRACTED_BYTES:
                raise ValueError('Snapshot exceeds the backup size limit')
            manifest = {'application': 'StreamFlow', 'format_version': FORMAT_VERSION,
                        'created_at': stamp.isoformat(), 'version': version, 'schema_version': schema,
                        'include_history': include_history, 'summary': counts, 'files': files}
            with zipfile.ZipFile(temporary, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=3) as archive:
                archive.writestr('manifest.json', json.dumps(manifest, allow_nan=False))
                for relative in files:
                    archive.write(stage / relative, relative)
            if temporary.stat().st_size > MAX_ARCHIVE_BYTES:
                raise ValueError('Archive exceeds the backup size limit')
            os.chmod(temporary, 0o600)
            with temporary.open('rb') as handle:
                os.fsync(handle.fileno())
            os.replace(temporary, final)
        return {'name': name, 'size': final.stat().st_size, **manifest}
    finally:
        temporary.unlink(missing_ok=True)


def manifest_metadata(path):
    with zipfile.ZipFile(path) as archive:
        info = archive.getinfo('manifest.json')
        if info.file_size > 1024 * 1024:
            raise ValueError('Invalid backup manifest')
        data = json.loads(archive.read(info), parse_constant=_invalid_constant)
    if not isinstance(data, dict) or data.get('application') != 'StreamFlow' or data.get('format_version') != FORMAT_VERSION:
        raise ValueError('Unsupported backup format')
    return data


def extract_validated(path, destination):
    """Validate every entry and checksum before any live state is touched."""
    path, destination = Path(path), Path(destination)
    if path.stat().st_size > MAX_ARCHIVE_BYTES:
        raise ValueError('Archive exceeds the backup size limit')
    metadata = manifest_metadata(path)
    files = metadata.get('files')
    if not isinstance(files, dict) or 'streamflow.db' not in files or len(files) > 128:
        raise ValueError('Invalid backup file list')
    with zipfile.ZipFile(path) as archive:
        entries = archive.infolist()
        names = [entry.filename for entry in entries]
        if len(names) != len(set(names)) or set(names) != {'manifest.json', *files}:
            raise ValueError('Unexpected or duplicate archive entries')
        if sum(entry.file_size for entry in entries) > MAX_EXTRACTED_BYTES:
            raise ValueError('Expanded archive exceeds the backup size limit')
        for relative, expected in files.items():
            allowed = relative in {'streamflow.db', 'monitoring-history.json'} or (
                relative.startswith('config/') and JSON_NAME_RE.fullmatch(relative[7:])
                and relative[7:] not in EXCLUDED_JSON)
            if not allowed or not isinstance(expected, dict):
                raise ValueError('Unsupported backup file')
            entry = archive.getinfo(relative)
            if entry.flag_bits & 1 or stat.S_ISLNK(entry.external_attr >> 16):
                raise ValueError('Unsupported archive entry')
            if entry.file_size != expected.get('size'):
                raise ValueError('Backup size check failed')
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(entry) as src, target.open('wb') as dst:
                shutil.copyfileobj(src, dst, length=1024 * 1024)
            os.chmod(target, 0o600)
            if digest(target) != expected.get('sha256'):
                raise ValueError('Backup checksum check failed')
            if relative.endswith('.json'):
                read_json(target)
        schema, _ = database_summary(destination / 'streamflow.db')
        if schema != metadata.get('schema_version'):
            raise ValueError('Backup schema check failed')
    return metadata
