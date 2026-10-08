"""Manual/automatic backups and serialized archive operations."""

import logging
import os
import shutil
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from apps.backups.archive import (MAX_ARCHIVE_BYTES, NAME_RE, archive_path, create_archive,
                                 extract_validated, manifest_metadata, read_json)
from apps.backups.restore import PENDING, RESULT, _stage_path, stage_restore
from apps.backups.schedule import next_run, validate_config
from apps.core.atomic_json import atomic_write_json

logger = logging.getLogger(__name__)


class BackupBusyError(ValueError):
    pass


class BackupService:
    def __init__(self, *, config_dir, db, backup_dir=None, clock=time.time,
                 capture_history=None, capture_inventory=None, fetch_inventory=None,
                 busy_reasons=None, stop_runtime=None, restart=None):
        self.config_dir = Path(config_dir).resolve()
        self.backup_dir = Path(backup_dir or os.environ.get('BACKUP_DIR', str(self.config_dir / 'backups'))).resolve()
        self.db, self.clock = db, clock
        self.capture_history = capture_history or (lambda: {'format_version': 1, 'sessions': []})
        self.capture_inventory = capture_inventory or (lambda: None)
        from apps.backups.inventory import fetch_inventory as fetch_current
        self.fetch_inventory = fetch_inventory or fetch_current
        self.busy_reasons = busy_reasons or (lambda: [])
        self.stop_runtime, self.restart = stop_runtime, restart
        self._lock = threading.RLock()
        self._operation_lock = threading.Lock()
        self._wake, self._stop = threading.Event(), threading.Event()
        self._thread = None
        self.maintenance = False
        self.operation = {'state': 'idle'}
        self._review_report = None
        self._review_pending = None

    def review_pending(self):
        from apps.backups.review_workflow import pending
        return pending(self)

    def check_review(self):
        from apps.backups.review_workflow import check
        return check(self)

    def confirm_review(self, token, mappings):
        from apps.backups.review_workflow import confirm
        return confirm(self, token, mappings)

    def preview_review(self, token, mappings):
        from apps.backups.review_workflow import preview
        return preview(self, token, mappings)

    def update_review_connection(self, settings):
        from apps.backups.review_workflow import update_connection
        return update_connection(self, settings)

    def get_config(self):
        return validate_config(self.db.get_system_setting('backup_config', {}) or {})

    def update_config(self, payload):
        with self._lock:
            if self._operation_lock.locked():
                raise BackupBusyError('A backup operation is in progress')
            config = validate_config(payload, self.get_config())
            previous = self.get_config()
            values = {'backup_config': config}
            if any(config[key] != previous[key] for key in ('enabled','frequency','time','timezone','weekday','interval_hours')):
                state = self.db.get_system_setting('backup_schedule_state', {}) or {}
                state['next_run_at'] = next_run(config, self.clock()) if config['enabled'] else None
                state.pop('retry_after', None)
                state.pop('last_error', None)
                values['backup_schedule_state'] = state
            if not self.db.set_system_settings_multi(values):
                raise RuntimeError('Backup settings could not be saved')
            self._wake.set()
            return config

    def _public(self, path, metadata):
        fields = ('created_at', 'version', 'schema_version', 'include_history', 'summary')
        return {'name': path.name, 'size': path.stat().st_size,
                'kind': NAME_RE.fullmatch(path.name).group(1), 'valid': True,
                **{key: metadata.get(key) for key in fields}}

    def list_backups(self):
        if not self.backup_dir.exists():
            return []
        result = []
        for path in sorted(self.backup_dir.glob('streamflow-*.zip'), reverse=True):
            if not NAME_RE.fullmatch(path.name) or path.is_symlink() or not path.is_file():
                continue
            try:
                result.append(self._public(path, manifest_metadata(path)))
            except FileNotFoundError:
                continue
            except Exception:
                # A damaged file must not hide other backups or crash the page.
                try:
                    result.append({'name': path.name, 'size': path.stat().st_size, 'valid': False})
                except FileNotFoundError:
                    continue
        return result

    def get_status(self):
        from apps.backups.review_workflow import public_status
        with self._lock:
            result = {'config': self.get_config(), 'directory': str(self.backup_dir),
                      'operation': dict(self.operation), 'restart_supported': self.restart is not None,
                      'schedule': self.db.get_system_setting('backup_schedule_state', {}) or {},
                      'backups': self.list_backups(), 'restore_review': public_status(self)}
            if (self.config_dir / RESULT).is_file():
                result['restore_result'] = read_json(self.config_dir / RESULT)
            return result

    def _prune(self, retention):
        paths = sorted((p for p in self.backup_dir.glob('streamflow-*.zip')
                        if NAME_RE.fullmatch(p.name) and not p.is_symlink()
                        and not p.name.startswith('streamflow-safety-')), key=lambda p: p.stat().st_mtime_ns, reverse=True)
        for path in paths[retention:]:
            path.unlink()

    def _create(self, include_history):
        from apps.backups.review_workflow import saved_inventory
        history = self.capture_history() if include_history else None
        version_file = Path(__file__).resolve().parents[2] / 'version.txt'
        version = version_file.read_text().strip() if version_file.is_file() else 'unknown'
        metadata = create_archive(self.config_dir, self.backup_dir, include_history=include_history,
                                  monitoring_history=history, inventory=saved_inventory(self), version=version)
        self._prune(self.get_config()['retention'])
        return self._public(self.backup_dir / metadata['name'], metadata)

    def _start_job(self, kind, action):
        with self._lock:
            if not self._operation_lock.acquire(blocking=False):
                raise BackupBusyError('A backup operation is already in progress')
            self.operation = {'state': 'running', 'kind': kind, 'id': uuid.uuid4().hex,
                              'started_at': self.clock()}
            if kind == 'restore':
                self.maintenance = True
            def run():
                try:
                    result = action()
                    with self._lock:
                        self.operation.update(state='completed', result=result)
                except Exception as exc:
                    logger.exception('Backup operation failed (%s)', kind)
                    with self._lock:
                        self.operation.update(state='failed', error=str(exc) if isinstance(exc, ValueError) else 'Backup operation failed. Check storage permissions, available space and the server log.')
                finally:
                    with self._lock:
                        self.operation['finished_at'] = self.clock()
                        self.maintenance = False
                        self._operation_lock.release()
            threading.Thread(target=run, name='StreamFlow-Backup', daemon=True).start()
            return dict(self.operation)

    def create(self, include_history=None):
        include_history = self.get_config()['include_history'] if include_history is None else include_history
        if type(include_history) is not bool:
            raise ValueError('include_history must be a boolean')
        return self._start_job('backup', lambda: self._create(include_history))

    def inspect_backup(self, name):
        path = archive_path(self.backup_dir, name)
        with tempfile.TemporaryDirectory(prefix='.verify-', dir=self.backup_dir) as stage:
            metadata = extract_validated(path, Path(stage))
        return self._public(path, metadata)

    def import_backup(self, stream):
        if not self._operation_lock.acquire(blocking=False):
            raise BackupBusyError('A backup operation is already in progress')
        name = f'streamflow-import-{uuid.uuid4().hex}.zip'
        temporary = self.backup_dir / ('.' + name + '.tmp')
        try:
            self.backup_dir.mkdir(parents=True, exist_ok=True)
            total = 0
            with temporary.open('xb') as target:
                os.chmod(temporary, 0o600)
                for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                    total += len(chunk)
                    if total > MAX_ARCHIVE_BYTES:
                        raise ValueError('Archive exceeds the upload size limit')
                    target.write(chunk)
            with tempfile.TemporaryDirectory(prefix='.verify-', dir=self.backup_dir) as stage:
                metadata = extract_validated(temporary, Path(stage))
            final = archive_path(self.backup_dir, name)
            os.replace(temporary, final)
            self._prune(self.get_config()['retention'])
            return self._public(final, metadata)
        finally:
            temporary.unlink(missing_ok=True)
            self._operation_lock.release()

    def delete(self, name):
        if not self._operation_lock.acquire(blocking=False):
            raise BackupBusyError('A backup operation is already in progress')
        try:
            archive_path(self.backup_dir, name).unlink()
        finally:
            self._operation_lock.release()

    def restore(self, name):
        if self.restart is None:
            raise BackupBusyError('Restart support is unavailable')
        reasons = self.busy_reasons()
        if reasons:
            raise BackupBusyError('Wait for active work to finish: ' + ', '.join(reasons))
        self.inspect_backup(name)
        def action():
            path = archive_path(self.backup_dir, name)
            stage_restore(self.config_dir, path)
            self._restart_staged()
            return {'restart_required': True}
        return self._start_job('restore', action)

    def _restart_staged(self):
        try:
            if self.stop_runtime:
                self.stop_runtime()
            if self.busy_reasons():
                raise BackupBusyError('Work started while preparing the restore')
        except Exception:
            journal = read_json(self.config_dir / PENDING)
            (self.config_dir / PENDING).unlink()
            shutil.rmtree(_stage_path(self.config_dir, journal['stage']))
            atomic_write_json(self.config_dir / RESULT, {'status': 'failed',
                              'message': 'Restore cancelled; previous configuration retained',
                              'finished_at': datetime.now(timezone.utc).isoformat()}, backup=False)
            self.restart()  # Resume the previous services without a pending restore.
            raise
        with self._lock:
            self.operation['state'] = 'restarting'
        # The restart callback executes only after the HTTP response has been sent.
        try:
            self.restart()
        except Exception:
            journal = read_json(self.config_dir / PENDING)
            (self.config_dir / PENDING).unlink()
            shutil.rmtree(_stage_path(self.config_dir, journal['stage']))
            raise

    def tick(self):
        with self._lock:
            config = self.get_config()
            if not config['enabled'] or self._operation_lock.locked():
                return
            state = self.db.get_system_setting('backup_schedule_state', {}) or {}
            now = self.clock()
            if now < state.get('retry_after', 0):
                return
            due = state.get('next_run_at')
            if due is None:
                state['next_run_at'] = next_run(config, now)
                if not self.db.set_system_setting('backup_schedule_state', state):
                    raise RuntimeError('Backup schedule could not be initialized')
                return
            if now < due:
                return
            def action():
                try:
                    result = self._create(config['include_history'])
                except Exception:
                    state.update(retry_after=self.clock() + 300, last_error='Scheduled backup failed; retrying in five minutes')
                    self.db.set_system_setting('backup_schedule_state', state)
                    raise
                completed = self.clock()
                new_state = {'next_run_at': next_run(config, completed), 'last_success_at': completed,
                             'last_backup': result['name']}
                if not self.db.set_system_setting('backup_schedule_state', new_state):
                    raise RuntimeError('Backup schedule result could not be saved')
                return result
            self._start_job('scheduled_backup', action)

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        def run():
            while not self._stop.is_set():
                try:
                    self.tick()
                except Exception:
                    logger.exception('Backup scheduler failed')
                self._wake.wait(30)
                self._wake.clear()
        self._thread = threading.Thread(target=run, name='StreamFlow-Backup-Scheduler', daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=5)


_service = None
_service_lock = threading.Lock()


def get_backup_service():
    global _service
    with _service_lock:
        if _service is None:
            from apps.database.connection import CONFIG_DIR
            from apps.database.manager import get_db_manager
            from apps.backups.history import capture_monitoring_history
            from apps.backups.inventory import capture_inventory
            _service = BackupService(config_dir=CONFIG_DIR, db=get_db_manager(),
                                     capture_history=capture_monitoring_history, capture_inventory=capture_inventory)
        return _service
