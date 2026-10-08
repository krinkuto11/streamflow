"""Serialized read-only comparison and journaled remapping/restart workflow."""

import os
import shutil
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path

from apps.backups.archive import config_files, snapshot_database, create_archive, read_json
from apps.backups.inventory import REVIEW_KEY, INVENTORY_FILE, capture_inventory, fetch_inventory, inventory_token
from apps.backups.restore import PENDING, stage_restore
from apps.backups.review import load_source, build_report, apply_mappings
from apps.core.atomic_json import atomic_write_json


def pending(service):
    with service._lock:
        if service._review_pending is None:
            service._review_pending = service.db.get_system_setting(REVIEW_KEY, False) is True
        return service._review_pending


def connection_token():
    from apps.config.dispatcharr_config import get_dispatcharr_config
    from apps.backups.inventory import fingerprint
    config = get_dispatcharr_config()
    return fingerprint(str(config.get_base_url()) + str(config.get_auth_mode()) + str(config.get_api_key()) + str(config.get_username()) + str(config.get_password()))


def public_status(service):
    if not pending(service):
        return {'pending': False}
    from apps.config.dispatcharr_config import get_dispatcharr_config
    config = get_dispatcharr_config().get_config()
    return {'pending': True, 'report': getattr(service, '_review_report', None),
            'connection': config, 'base_url_managed_externally': bool(os.getenv('DISPATCHARR_BASE_URL'))}


def saved_inventory(service):
    if pending(service) and (service.config_dir / INVENTORY_FILE).is_file():
        return read_json(service.config_dir / INVENTORY_FILE)
    return service.capture_inventory()


def check(service):
    if not pending(service):
        raise ValueError('No restored assignments are awaiting review')
    def action():
        current = service.fetch_inventory()
        token = inventory_token(current, connection_token())
        source = load_source(service.config_dir)
        report = build_report(service.config_dir, source, current, token)
        with service._lock:
            service._review_report = report
        return {'comparison_ready': True}
    return service._start_job('restore_comparison', action)


def update_connection(service, settings):
    from apps.config.dispatcharr_config import get_dispatcharr_config
    from apps.core.auth import _clear_token_validation_cache
    from apps.backups.service import BackupBusyError
    with service._lock:
        if service._operation_lock.locked():
            raise BackupBusyError('A backup operation is in progress')
        if not pending(service):
            raise ValueError('Connection editing here is available only during restore review')
        if os.getenv('DISPATCHARR_BASE_URL') and 'base_url' in settings:
            raise ValueError('The target address is managed in the container environment')
        config = get_dispatcharr_config()
        if not config.update_config(**settings):
            raise RuntimeError('Target connection could not be saved')
        _clear_token_validation_cache()
        os.environ.pop('DISPATCHARR_TOKEN', None)
        service._review_report = None
    return {'saved': True}


def confirm(service, token, mappings):
    from apps.backups.service import BackupBusyError
    if not pending(service):
        raise ValueError('No restored assignments are awaiting review')
    if service.restart is None:
        raise BackupBusyError('Restart support is unavailable')
    report = getattr(service, '_review_report', None)
    if not report or token != report['token']:
        raise ValueError('Compare the current Dispatcharr inventory before confirming')
    if service.busy_reasons():
        raise BackupBusyError('Wait for active work to finish before applying assignments')

    def action():
        current = service.fetch_inventory()
        if inventory_token(current, connection_token()) != token:
            service._review_report = None
            raise ValueError('Dispatcharr inventory changed; compare again before confirming')
        source = load_source(service.config_dir)
        with tempfile.TemporaryDirectory(prefix='.remap-', dir=service.backup_dir) as directory:
            root = Path(directory)
            snapshot_database(service.config_dir / 'streamflow.db', root / 'streamflow.db')
            for path in config_files(service.config_dir):
                shutil.copyfile(path, root / path.name)
            if (service.config_dir / INVENTORY_FILE).exists():
                shutil.copyfile(service.config_dir / INVENTORY_FILE, root / INVENTORY_FILE)
            outcome = apply_mappings(root, source, current, mappings)
            history = None if outcome['foreign'] else service.capture_history()
            # The source metadata belongs to the newly confirmed target after
            # this transaction. A later imported backup must still enter review.
            archive = create_archive(root, root / 'archives', inventory=current, monitoring_history=history,
                                     version='restore-remapping')
            stage_restore(service.config_dir, root / 'archives' / archive['name'])
        journal = read_json(service.config_dir / PENDING)
        journal['review_approved'] = True
        atomic_write_json(service.config_dir / PENDING, journal, backup=False)
        service._restart_staged()
        return outcome
    return service._start_job('restore', action)
