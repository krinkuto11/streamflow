"""Opt-in passive Dispatcharr playback history and channel-scoped scoring evidence."""

import hashlib
import logging
import math
import threading
import time

from apps.database.manager import get_db_manager
from apps.database.repositories.playback_stability_repository import PlaybackStabilityRepository
from apps.stream.playback_stability_tracker import PlaybackTracker

logger = logging.getLogger(__name__)
DEFAULT_CONFIG = {'enabled': False, 'poll_interval_seconds': 10, 'retention_days': 14}


def source_fingerprint(stream):
    if not isinstance(stream, dict) or not stream.get('url'):
        return None
    return hashlib.sha256(str(stream['url']).encode()).hexdigest()


def validate_config(payload, current):
    if not isinstance(payload, dict) or set(payload) - set(DEFAULT_CONFIG):
        raise ValueError('Unknown Playback Stability setting')
    result = {**current, **payload}
    if not isinstance(result['enabled'], bool):
        raise ValueError('enabled must be a boolean')
    for name, low, high in (('poll_interval_seconds', 5, 60), ('retention_days', 1, 30)):
        value = result[name]
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            raise ValueError(f'{name} must be an integer between {low} and {high}')
    return result


class PlaybackStabilityService:
    def __init__(self, *, db=None, repository=None, get_udi=None, clock=time.time):
        self.db = db or get_db_manager()
        self.repository = repository or PlaybackStabilityRepository()
        if get_udi is None:
            from apps.udi import get_udi_manager
            get_udi = get_udi_manager
        self.get_udi = get_udi
        self.clock = clock
        self._config = validate_config(self.db.get_system_setting('playback_stability', {}) or {}, DEFAULT_CONFIG)
        self._lock = threading.RLock()
        self._poll_lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread = None
        self._revision = 0
        self.tracker = PlaybackTracker()
        self._ready = False
        self._last_success = None
        self._error = None

    def get_config(self):
        with self._lock:
            return dict(self._config)

    def update_config(self, payload):
        with self._lock:
            updated = validate_config(payload, self._config)
            if not self.db.set_system_setting('playback_stability', updated):
                raise RuntimeError('Playback Stability settings could not be saved')
            self._config = updated
            self._revision += 1
            self._ready = False
            self.tracker.reset()
        if updated['enabled']:
            self.start()
        self._wake.set()
        return self.get_config()

    def start(self):
        with self._lock:
            if not self._config['enabled']:
                return False
            if self._thread and self._thread.is_alive():
                return True
            self._stop.clear()
            self._thread = threading.Thread(target=self._worker, name='PlaybackStability', daemon=True)
            self._thread.start()
            return True

    def stop(self):
        self._stop.set()
        self._wake.set()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=5)
        with self._lock:
            self._ready = False
            self.tracker.reset()

    def _worker(self):
        from apps.core import http_transport
        try:
            while not self._stop.is_set():
                self._wake.clear()
                config = self.get_config()
                if config['enabled']:
                    self.run_once()
                self._wake.wait(config['poll_interval_seconds'] if config['enabled'] else None)
        finally:
            http_transport.close_thread_sessions()

    @staticmethod
    def _snapshots(statuses, channels, udi):
        by_uuid = {str(c['uuid']): c for c in channels if c.get('uuid')}
        marker = udi._shadow_watcher_user_agent().lower()
        snapshots = []
        if not isinstance(statuses, dict):
            raise ValueError('Invalid proxy snapshot')
        for channel_uuid, status in statuses.items():
            if not isinstance(status, dict):
                raise ValueError('Invalid proxy channel snapshot')
            channel = by_uuid.get(str(channel_uuid))
            if not channel:
                continue
            clients = status.get('clients')
            if isinstance(clients, dict):
                clients = list(clients.values())
            # Capacity accounting intentionally has broader protective fallbacks.
            # Playback evidence requires confirmed clients and a byte counter.
            if not isinstance(clients, list):
                continue
            confirmed = frozenset(
                str(c['client_id']) for c in clients if isinstance(c, dict) and c.get('client_id')
                and c.get('user_agent') and 'streamflow' not in str(c['user_agent']).lower()
                and (not marker or marker not in str(c['user_agent']).lower()))
            if not confirmed:
                continue
            try:
                stream_id = int(status['stream_id'])
                total = status['total_bytes']
                started = status['started_at']
                if isinstance(total, bool) or not isinstance(total, (int, float)) or not math.isfinite(total) or total < 0:
                    continue
                if isinstance(started, bool) or not isinstance(started, (int, float)) or not math.isfinite(started) or started <= 0:
                    continue
            except (KeyError, ValueError, TypeError):
                continue
            fingerprint = source_fingerprint(udi.get_stream_by_id(stream_id))
            if not fingerprint:
                continue
            snapshots.append({'channel_uuid': str(channel_uuid), 'channel_id': int(channel['id']),
                              'stream_id': stream_id, 'source_fingerprint': fingerprint,
                              'session': str(started), 'clients': confirmed, 'total_bytes': int(total)})
        return snapshots

    def run_once(self):
        with self._poll_lock:
            with self._lock:
                config = dict(self._config)
                revision = self._revision
            if not config['enabled'] or self._stop.is_set():
                return False
            try:
                udi = self.get_udi()
                channels = udi.get_channels()
                # Reuse single-flight status reads without changing capacity freshness.
                statuses = udi._get_proxy_status(require_authoritative=True)
                snapshots = self._snapshots(statuses, channels, udi)
                now = self.clock()
                with self._lock:
                    if revision != self._revision or self._stop.is_set():
                        return False
                    checkpoints = self.tracker.consume(snapshots, now, config['poll_interval_seconds'])
                    self.repository.save(checkpoints, now - config['retention_days'] * 86400)
                    self._last_success = now
                    self._error = None
                    self._ready = True
                return True
            except Exception:
                with self._lock:
                    self.tracker.reset()
                    self._ready = False
                    self._error = 'Playback data unavailable; recording paused.'
                logger.warning('Playback Stability poll unavailable; no failure attributed to streams')
                return False

    def scoring_snapshot(self, channel_id):
        with self._lock:
            if not self._config['enabled'] or not self._ready:
                return {}
            if self._last_success is None or not 0 <= self.clock() - self._last_success <= self._config['poll_interval_seconds'] * 2.5:
                return {}
            cutoff = self.clock() - self._config['retention_days'] * 86400
        udi = self.get_udi()
        channel = udi.get_channel_by_id(channel_id) or {}
        stream_ids = [s.get('id') if isinstance(s, dict) else s for s in channel.get('streams', [])]
        if not stream_ids:
            return {}
        rows = self.repository.summaries(cutoff, stream_ids=stream_ids, aggregate_streams=True)
        return {row['stream_id']: row['score'] for row in rows if row['eligible'] and
                row['source_fingerprint'] == source_fingerprint(udi.get_stream_by_id(row['stream_id']))}

    def get_status(self):
        with self._lock:
            enabled = self._config['enabled']
            fresh = self._ready and self._last_success is not None and 0 <= self.clock() - self._last_success <= self._config['poll_interval_seconds'] * 2.5
            status = {'enabled': enabled, 'recording': enabled and fresh,
                      'last_success': self._last_success, 'error': self._error,
                      'active_playbacks': len(self.tracker.legs),
                      'minimum_observed_seconds': 600, 'minimum_samples': 60}
            cutoff = self.clock() - self._config['retention_days'] * 86400
        rows = self.repository.summaries(cutoff, aggregate_streams=True) if enabled else []
        # A reused Dispatcharr ID must not inherit evidence for a different URL.
        udi = self.get_udi() if rows else None
        streams = []
        for row in rows:
            stream = udi.get_stream_by_id(row['stream_id'])
            if row['source_fingerprint'] != source_fingerprint(stream):
                continue
            streams.append({**{k: v for k, v in row.items() if k not in {'source_fingerprint', 'channel_id'}},
                            'stream_name': stream.get('name') or f"Stream {row['stream_id']}"})
        status['total_streams'] = len(streams)
        status['streams'] = sorted(streams, key=lambda s: s['last_seen'], reverse=True)[:200]
        return status


_instance = None
_instance_lock = threading.Lock()


def get_playback_stability_service():
    global _instance
    with _instance_lock:
        if _instance is None:
            _instance = PlaybackStabilityService()
        return _instance
