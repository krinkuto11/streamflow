"""Apply staged restores before opening the application database.

The journal makes an interrupted multi-file restore recoverable on the next boot.
"""

from __future__ import annotations

import logging
import json
import os
import shutil
import sqlite3
import tempfile
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from apps.core.atomic_json import atomic_write_json
from apps.backups.archive import config_files, create_archive, extract_validated, read_json, snapshot_database, sync_directory

logger = logging.getLogger(__name__)
PENDING = '.restore-pending.json'
RESULT = '.restore-result.json'


def _stage_path(root, name):
    root = Path(root).resolve()
    if not isinstance(name, str) or not name.startswith('.restore-stage-') or Path(name).name != name:
        raise ValueError('Invalid restore staging directory')
    path = root / name
    if path.is_symlink() or path.resolve().parent != root:
        raise ValueError('Invalid restore staging path')
    return path


def _replace(source, target):
    if target.is_symlink():
        raise ValueError('Configuration symlinks cannot be restored')
    descriptor, temporary = tempfile.mkstemp(prefix='.restore-file-', dir=target.parent)
    os.close(descriptor)
    try:
        shutil.copyfile(source, temporary)
        os.chmod(temporary, 0o600)
        with open(temporary, 'r+b') as handle:
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        sync_directory(target.parent)
    finally:
        Path(temporary).unlink(missing_ok=True)


def stage_restore(config_dir, archive):
    root = Path(config_dir).resolve()
    if (root / PENDING).exists():
        raise ValueError('A restore is already pending')
    stage = Path(tempfile.mkdtemp(prefix='.restore-stage-', dir=root))
    _stage_path(root, stage.name)
    os.chmod(stage, 0o700)
    try:
        metadata = extract_validated(archive, stage / 'incoming')
        atomic_write_json(root / PENDING, {'stage': stage.name, 'phase': 'prepared',
                          'backup': Path(archive).name, 'metadata': metadata}, backup=False)
        return metadata
    except Exception:
        shutil.rmtree(_stage_path(root, stage.name))
        raise


def _safe_file(root, name):
    if not isinstance(name, str) or Path(name).name != name or name.startswith('.'):
        raise ValueError('Invalid restore target')
    target = root / name
    if target.is_symlink() or target.resolve().parent != root:
        raise ValueError('Invalid restore target path')
    return target


def _rollback(root, stage, journal):
    for name in journal['targets']:
        target = _safe_file(root, name)
        saved = stage / 'rollback' / name
        if saved.exists():
            _replace(saved, target)
        else:
            target.unlink(missing_ok=True)
    for suffix in ('-wal', '-shm'):
        (root / ('streamflow.db' + suffix)).unlink(missing_ok=True)


def apply_pending_restore(config_dir, backup_dir=None):
    """Called only before runtime services/migrations open any database handles."""
    root = Path(config_dir).resolve()
    pending = root / PENDING
    if not pending.exists():
        return None
    journal = read_json(pending)
    stage = _stage_path(root, journal['stage'])
    result = {'backup': journal.get('backup'), 'finished_at': datetime.now(timezone.utc).isoformat()}
    phase = journal.get('phase')
    try:
        if phase == 'applying':
            _rollback(root, stage, journal)
            result.update(status='rolled_back', message='Interrupted restore rolled back to the previous state')
        elif phase == 'committed':
            result.update(status='restored', message='Backup restored successfully')
        elif phase == 'prepared':
            incoming = stage / 'incoming'
            # Verify staged files again after the restart, before saving/replacing anything.
            from apps.backups.archive import digest, database_summary
            for relative, expected in journal['metadata']['files'].items():
                source = incoming / relative
                if not source.is_file() or source.is_symlink() or digest(source) != expected['sha256']:
                    raise ValueError('Staged backup checksum check failed')
            database_summary(incoming / 'streamflow.db')
            backup_dir = Path(backup_dir or os.environ.get('BACKUP_DIR', str(root / 'backups'))).resolve()
            history_path = root / 'monitoring_history.json'
            version_file = Path(__file__).resolve().parents[2] / 'version.txt'
            safety = create_archive(root, backup_dir, kind='safety',
                                    version=version_file.read_text().strip() if version_file.is_file() else 'unknown',
                                    monitoring_history=read_json(history_path) if history_path.is_file() else None)
            result['safety_backup'] = safety['name']
            safety_files = sorted(backup_dir.glob('streamflow-safety-*.zip'), reverse=True)
            for obsolete in safety_files[3:]:
                if not obsolete.is_symlink():
                    obsolete.unlink()
            rollback = stage / 'rollback'
            rollback.mkdir()
            originals = {p.name: p for p in config_files(root)}
            # Preserve the offline monitoring snapshot and JSON recovery copies as well.
            for path in [root / 'monitoring_history.json', *root.glob('*.json.last-good')]:
                if path.is_file() and not path.is_symlink():
                    originals[path.name] = path
            # The backup can be created during live monitoring. Restore its data,
            # but never resume historical process IDs or active-session flags.
            restored_db = stage / 'restored.db'
            snapshot_database(incoming / 'streamflow.db', restored_db)
            with closing(sqlite3.connect(restored_db)) as db:
                for identity, raw in db.execute('SELECT session_id, raw_info FROM monitoring_sessions').fetchall():
                    content = json.loads(raw) if raw else {}
                    content['is_active'] = False
                    db.execute('UPDATE monitoring_sessions SET pid=NULL, raw_info=? WHERE session_id=?', (json.dumps(content), identity))
                db.execute("UPDATE monitoring_sessions SET status='stopped' WHERE stream_id IS NULL")
                db.commit()
            new_files = {'streamflow.db': restored_db}
            for path in (incoming / 'config').glob('*.json'):
                new_files[path.name] = path
                new_files[path.name + '.last-good'] = path
            if (incoming / 'monitoring-history.json').exists():
                new_files['monitoring_history.json'] = incoming / 'monitoring-history.json'
            targets = sorted({'streamflow.db', 'monitoring_history.json', *originals, *new_files})
            for name in targets:
                _safe_file(root, name)
            for name, source in originals.items():
                _replace(source, rollback / name)
            snapshot_database(root / 'streamflow.db', rollback / 'streamflow.db')
            journal.update(phase='applying', targets=targets)
            atomic_write_json(pending, journal, backup=False)
            try:
                for suffix in ('-wal', '-shm'):
                    (root / ('streamflow.db' + suffix)).unlink(missing_ok=True)
                for name in targets:
                    target = _safe_file(root, name)
                    if name in new_files:
                        _replace(new_files[name], target)
                    else:
                        target.unlink(missing_ok=True)
                journal['phase'] = 'committed'
                atomic_write_json(pending, journal, backup=False)
            except Exception:
                _rollback(root, stage, journal)
                result.update(status='rolled_back', message='Restore failed; previous state recovered')
                logger.exception('Restore failed and was rolled back')
            else:
                result.update(status='restored', message='Backup restored successfully')
        else:
            raise ValueError('Invalid restore journal phase')
    except Exception:
        if phase == 'applying' or journal.get('phase') == 'applying':
            # Do not start services on mixed state. Keep the journal for boot recovery.
            logger.exception('Restore recovery failed; journal retained')
            raise
        logger.exception('Restore rejected before live state was changed')
        result.update(status='failed', message='Restore rejected; previous state retained')
    atomic_write_json(root / RESULT, result, backup=False)
    pending.unlink()
    shutil.rmtree(_stage_path(root, stage.name))
    return result
