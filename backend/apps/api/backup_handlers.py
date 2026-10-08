"""HTTP adapters for portable backups; no credentials in status/preview responses."""

import logging
import sqlite3
import zipfile
from functools import wraps
from urllib.parse import urlsplit

from flask import Blueprint, request, send_file
from werkzeug.exceptions import RequestEntityTooLarge

from apps.api.schemas import BackupCreateRequest, BackupRestoreRequest
from apps.backups.archive import MAX_ARCHIVE_BYTES, archive_path
from apps.backups.service import BackupBusyError
from apps.core.api_responses import error_response, success_response
from apps.core.exceptions import ValidationError

logger = logging.getLogger(__name__)


def create_backup_blueprint(service_provider):
    blueprint = Blueprint('backups', __name__, url_prefix='/api/backups')

    @blueprint.before_request
    def require_same_origin():
        origin = request.headers.get('Origin')
        if origin and urlsplit(origin).netloc != request.host:
            return error_response('Backup requests must use the application origin', status_code=403)
        if request.content_length and request.content_length > MAX_ARCHIVE_BYTES + 1024 * 1024:
            return error_response('Backup exceeds the upload size limit', status_code=413)
        request.max_content_length = MAX_ARCHIVE_BYTES + 1024 * 1024

    def handled(fn):
        @wraps(fn)
        def run(*args, **kwargs):
            try:
                return fn(*args, **kwargs)
            except BackupBusyError as exc:
                return error_response(str(exc), status_code=409, code='backup_busy')
            except FileNotFoundError:
                return error_response('Backup not found', status_code=404)
            except RequestEntityTooLarge:
                return error_response('Backup exceeds the upload size limit', status_code=413)
            except (ValueError, ValidationError, zipfile.BadZipFile, sqlite3.DatabaseError, KeyError, TypeError) as exc:
                message = str(exc) if isinstance(exc, (ValueError, ValidationError)) else 'Invalid backup archive'
                return error_response(message, status_code=400)
            except Exception:
                logger.exception('Backup request failed')
                return error_response('Backup operation failed. Check storage permissions, available space and the server log.', status_code=500)
        return run

    @blueprint.get('')
    @handled
    def status():
        return success_response(service_provider().get_status())

    @blueprint.put('/config')
    @handled
    def config():
        return success_response(service_provider().update_config(request.get_json(silent=True)))

    @blueprint.post('')
    @handled
    def create():
        payload = BackupCreateRequest.from_payload(request.get_json(silent=True))
        return success_response(service_provider().create(payload.include_history), status_code=202)

    @blueprint.post('/upload')
    @handled
    def upload():
        if 'file' not in request.files:
            raise ValueError('Choose a StreamFlow backup ZIP file')
        return success_response(service_provider().import_backup(request.files['file'].stream), status_code=201)

    @blueprint.get('/<name>/inspect')
    @handled
    def inspect(name):
        return success_response(service_provider().inspect_backup(name))

    @blueprint.get('/<name>/download')
    @handled
    def download(name):
        path = archive_path(service_provider().backup_dir, name)
        return send_file(path, as_attachment=True, download_name=name, mimetype='application/zip', max_age=0)

    @blueprint.delete('/<name>')
    @handled
    def delete(name):
        BackupRestoreRequest.from_payload(request.get_json(silent=True))
        service_provider().delete(name)
        return success_response(message='Backup deleted')

    @blueprint.post('/<name>/restore')
    @handled
    def restore(name):
        BackupRestoreRequest.from_payload(request.get_json(silent=True))
        return success_response(service_provider().restore(name), status_code=202)

    @blueprint.after_request
    def private_response(response):
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        return response

    return blueprint
