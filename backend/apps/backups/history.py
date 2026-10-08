"""Bounded, optional snapshots of the monitoring timeline held in memory."""

import logging
import math
import time
from collections import deque
from dataclasses import asdict, fields

from apps.backups.archive import read_json

logger = logging.getLogger(__name__)
MAX_PER_STREAM = 1000
MAX_TOTAL = 100000


def validate_history(payload):
    from apps.stream.stream_session_manager import StreamMetrics
    if not isinstance(payload, dict) or payload.get('format_version') != 1:
        raise ValueError('Invalid monitoring history format')
    sessions = payload.get('sessions')
    if not isinstance(sessions, list) or len(sessions) > 10000:
        raise ValueError('Invalid monitoring history sessions')
    allowed = {field.name for field in fields(StreamMetrics)}
    count, identities = 0, set()
    for session in sessions:
        if not isinstance(session, dict) or not isinstance(session.get('id'), str) or len(session['id']) > 256:
            raise ValueError('Invalid monitoring session identity')
        streams = session.get('streams')
        if not isinstance(streams, list) or len(streams) > 10000:
            raise ValueError('Invalid monitoring history streams')
        for stream in streams:
            if not isinstance(stream, dict) or type(stream.get('id')) is not int:
                raise ValueError('Invalid monitoring stream identity')
            identity = (session['id'], stream['id'])
            if identity in identities:
                raise ValueError('Duplicate monitoring history identity')
            identities.add(identity)
            metrics = stream.get('metrics')
            if not isinstance(metrics, list) or len(metrics) > MAX_PER_STREAM:
                raise ValueError('Monitoring history exceeds its sample limit')
            count += len(metrics)
            if count > MAX_TOTAL:
                raise ValueError('Monitoring history exceeds its sample limit')
            for metric in metrics:
                if not isinstance(metric, dict) or set(metric) - allowed:
                    raise ValueError('Invalid monitoring measurement')
                for key in ('timestamp', 'speed', 'bitrate', 'fps', 'reliability_score'):
                    value = metric.get(key, 50.0 if key == 'reliability_score' else None)
                    if type(value) not in (int, float) or not math.isfinite(value):
                        raise ValueError('Invalid monitoring measurement value')
                for key in ('is_alive', 'buffering'):
                    if type(metric.get(key, False)) is not bool:
                        raise ValueError('Invalid monitoring measurement flag')
                for key in ('status', 'status_reason', 'display_logo_status'):
                    if key in metric and metric[key] is not None and (not isinstance(metric[key], str) or len(metric[key]) > 1024):
                        raise ValueError('Invalid monitoring measurement label')
                if metric.get('rank') is not None and type(metric['rank']) is not int:
                    raise ValueError('Invalid monitoring measurement rank')
                if metric.get('loop_duration') is not None and (type(metric['loop_duration']) not in (int, float) or not math.isfinite(metric['loop_duration'])):
                    raise ValueError('Invalid monitoring loop duration')
                StreamMetrics(**metric)
    return payload


def capture_monitoring_history(manager=None):
    if manager is None:
        from apps.stream.stream_session_manager import get_session_manager
        manager = get_session_manager()
    if not manager._save_sessions(wait=True):
        raise RuntimeError('Monitoring state could not be flushed')
    payload = {'format_version': 1, 'captured_at': time.time(), 'sessions': []}
    remaining = MAX_TOTAL
    for session_id, session in list(manager.sessions.items()):
        lock = manager.session_locks.get(session_id)
        if lock is None:
            continue
        with lock:
            streams = []
            for stream_id, stream in list(session.streams.items()):
                metrics = list(stream.metrics_history)[-min(MAX_PER_STREAM, remaining):] if remaining else []
                if metrics:
                    streams.append({'id': stream_id, 'metrics': [asdict(metric) for metric in metrics]})
                    remaining -= len(metrics)
            if streams:
                payload['sessions'].append({'id': session_id, 'streams': streams})
    return validate_history(payload)


def restore_monitoring_history(manager, config_dir):
    path = config_dir / 'monitoring_history.json'
    if not path.is_file() or path.is_symlink():
        return
    try:
        from apps.stream.stream_session_manager import StreamMetrics
        payload = validate_history(read_json(path))
        for entry in payload['sessions']:
            session = manager.sessions.get(entry['id'])
            if session is None or session.is_active:
                continue
            for stream in entry['streams']:
                target = session.streams.get(stream['id'])
                if target is not None:
                    target.metrics_history = deque((StreamMetrics(**metric) for metric in stream['metrics']), maxlen=3600)
        # Timelines remain in memory as before. Consume the restore snapshot so
        # an ordinary later restart cannot inject measurements from an old run.
        path.unlink()
    except (ValueError, TypeError, OSError, KeyError):
        logger.warning('Ignoring an invalid restored monitoring timeline')
