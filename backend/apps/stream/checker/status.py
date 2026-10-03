"""Status responsibilities for the shared StreamCheckerService instance."""

import logging
import math
from copy import deepcopy
from collections import Counter
from datetime import datetime
from typing import Any, Dict, Optional
from apps.stream.stream_check_utils import _stream_analysis_timeout
from apps.stream.stale_status_snapshot import external_m3u_account_risk, external_message_class
from apps.core.operation_timing import STREAM_OPERATION_TIMINGS

logger = logging.getLogger("apps.stream.stream_checker_service")


class CheckerStatusMixin:
    @staticmethod
    def _queue_number(value: Any) -> float:
        try:
            number = float(value or 0)
        except (TypeError, ValueError):
            return 0.0
        return number if number > 0 else 0.0


    @staticmethod
    def _elapsed_seconds_since(value: Any) -> float:
        if not value:
            return 0.0
        try:
            started_at = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        except (TypeError, ValueError):
            return 0.0
        now = datetime.now(started_at.tzinfo) if started_at.tzinfo else datetime.now()
        return max(0.0, (now - started_at).total_seconds())


    @staticmethod
    def _active_worker_context(progress: Optional[Dict[str, Any]]) -> Dict[str, int]:
        if not isinstance(progress, dict):
            return {'active': 0, 'waiting': 0}

        provider_summary = progress.get('provider_summary') or {}
        try:
            active = int(provider_summary.get('checking_streams') or 0)
        except (TypeError, ValueError):
            active = 0
        try:
            waiting = int(provider_summary.get('waiting_streams') or 0)
        except (TypeError, ValueError):
            waiting = 0

        if active <= 0 or waiting <= 0:
            streams_detail = progress.get('streams_detail') or []
            if isinstance(streams_detail, list):
                if active <= 0:
                    active = sum(
                        1
                        for stream in streams_detail
                        if isinstance(stream, dict)
                        and stream.get('status') in {'checking', 'probing'}
                    )
                if waiting <= 0:
                    waiting = sum(
                        1
                        for stream in streams_detail
                        if isinstance(stream, dict)
                        and stream.get('status') == 'waiting_provider_limit'
                    )

        return {'active': max(0, active), 'waiting': max(0, waiting)}


    def _current_progress_stale_after_seconds(self) -> int:
        analysis_duration = self._queue_number(self.config.get('stream_analysis.ffmpeg_duration', 30)) or 30.0
        analysis_timeout = self._queue_number(self.config.get('stream_analysis.timeout', 30)) or 30.0
        startup_buffer = self._queue_number(self.config.get('stream_analysis.stream_startup_buffer', 10)) or 10.0
        retries = self._queue_number(self.config.get('stream_analysis.retries', 1))
        retry_delay = self._queue_number(self.config.get('stream_analysis.retry_delay', 10)) or 10.0
        loop_duration = self._queue_number(self.config.get('stream_analysis.max_loop_duration', 120)) or 120.0
        provider_wait = self._queue_number(self.config.get('concurrent_streams.provider_wait_timeout', 180)) or 180.0

        attempts = max(1.0, retries + 1.0)
        probe_budget = attempts * _stream_analysis_timeout(
            analysis_timeout,
            analysis_duration,
            startup_buffer,
        )
        retry_budget = max(0.0, retries) * retry_delay
        loop_budget = loop_duration * 3.0
        stale_after = probe_budget + retry_budget + loop_budget + provider_wait + 60.0
        return int(max(300.0, min(stale_after, 1800.0)))


    def _current_progress_cleanup_after_seconds(self) -> int:
        return int(max(3600, min(self._current_progress_stale_after_seconds() * 6, 21600)))


    def _current_progress_age_seconds(self, progress: Optional[Dict[str, Any]]) -> Optional[int]:
        if not isinstance(progress, dict) or not progress.get('timestamp'):
            return None
        return int(self._elapsed_seconds_since(progress.get('timestamp')))


    def _current_progress_stale_gate(
        self,
        progress: Optional[Dict[str, Any]],
        *,
        worker_or_queue_active: bool,
    ) -> Optional[Dict[str, Any]]:
        if not isinstance(progress, dict) or not progress:
            return None
        if worker_or_queue_active:
            return None

        age_seconds = self._current_progress_age_seconds(progress)
        stale_after_seconds = self._current_progress_stale_after_seconds()
        is_single_channel = bool(progress.get('is_single_channel_check'))
        if is_single_channel and age_seconds is not None and age_seconds <= stale_after_seconds:
            return None

        reason = 'missing_progress_timestamp' if age_seconds is None else 'no_active_worker'
        if not is_single_channel:
            reason = 'idle_batch_progress'

        stale_state = {
            'stale': True,
            'reason': reason,
            'age_seconds': age_seconds,
            'stale_after_seconds': stale_after_seconds,
            'cleanup_after_seconds': self._current_progress_cleanup_after_seconds(),
            'detected_at': datetime.now().isoformat(),
            'channel_id': progress.get('channel_id'),
            'channel_name': progress.get('channel_name'),
            'is_single_channel_check': is_single_channel,
        }
        return stale_state


    @staticmethod
    def _external_message_class(message: Any) -> str:
        """Classify Dispatcharr status text without exposing the raw message."""
        return external_message_class(message)


    @staticmethod
    def _external_m3u_account_risk(account: Dict[str, Any]) -> Dict[str, Any]:
        """Return a UI-safe M3U account status summary and stale-risk classification."""
        return external_m3u_account_risk(account)


    def _build_external_stale_diagnostics(
        self,
        *,
        stream_checking_mode: bool,
        queue_status: Dict[str, Any],
        progress: Optional[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Build read-only diagnostics for external Dispatcharr stale-status risks.

        StreamFlow cannot safely inspect Dispatcharr's Celery/Redis/Postgres internals from
        inside this container, so unavailable signals are reported as unknown instead of
        being inferred. This method only reads StreamFlow's UDI cache/status and never
        mutates Dispatcharr state.
        """
        base = {
            "status": "unknown",
            "read_only": True,
            "generated_at": datetime.now().isoformat(),
            "stale_status_suspected": False,
            "operator_note": "External Dispatcharr stale-state diagnostics are read-only.",
            "m3u_accounts": {
                "available": False,
                "total": 0,
                "active": 0,
                "status_counts": {},
                "stale_suspected": [],
            },
            "streamflow_activity": {
                "stream_checking_mode": bool(stream_checking_mode),
                "queue_active": bool(
                    queue_status.get("queue_size", 0) > 0
                    or queue_status.get("in_progress", 0) > 0
                    or queue_status.get("current_channel") is not None
                ),
                "progress_present": isinstance(progress, dict) and bool(progress),
            },
            "external_checks": {
                "celery": {
                    "status": "unknown",
                    "operator_note": "Not available from the StreamFlow container; verify directly in Dispatcharr before treating active provider status as real work.",
                },
                "redis": {
                    "status": "unknown",
                    "operator_note": "Not available from the StreamFlow container; verify Dispatcharr refresh or M3U locks directly if needed.",
                },
                "postgres": {
                    "status": "unknown",
                    "operator_note": "Not available from the StreamFlow container; verify database locks directly if needed.",
                },
            },
            "actions": {
                "dispatcharr_mutated": False,
                "dispatcharr_restart_attempted": False,
                "repair_requires_operator_approval": True,
            },
        }

        try:
            udi = self._checker_get_udi_manager()
            observability = {}
            getter = getattr(udi, "get_observability_status", None)
            if callable(getter):
                observability = getter() or {}
            network_ready = bool(getattr(udi, "is_network_ready", lambda: False)())
            automation_busy = bool(getattr(udi, "is_automation_busy", lambda: False)())
            base["streamflow_activity"].update({
                "udi_network_ready": network_ready,
                "udi_init_in_progress": bool(observability.get("init_in_progress")),
                "udi_refresh_running": bool(observability.get("refresh_running")),
                "udi_automation_busy": automation_busy,
                "udi_last_refresh_time": observability.get("last_refresh_time"),
                "udi_last_refresh_age_seconds": observability.get("last_refresh_age_seconds"),
            })

            if not network_ready:
                base["status"] = "insufficient_evidence"
                base["operator_note"] = "UDI has not completed a live Dispatcharr refresh yet, so external stale-state diagnostics are incomplete."
                return base

            account_getter = getattr(udi, "get_m3u_accounts", None)
            accounts = account_getter() if callable(account_getter) else []
            if not isinstance(accounts, list):
                accounts = []

            status_counts: Counter = Counter()
            active_count = 0
            stale_suspected = []
            for account in accounts:
                if not isinstance(account, dict):
                    continue
                if account.get("is_active") is False:
                    continue
                active_count += 1
                risk = self._external_m3u_account_risk(account)
                status_counts[risk["status"]] += 1
                if risk["stale_status_suspected"]:
                    stale_suspected.append(risk)

            base["m3u_accounts"] = {
                "available": True,
                "total": len(accounts),
                "active": active_count,
                "status_counts": dict(sorted(status_counts.items())),
                "stale_suspected": stale_suspected[:10],
                "stale_suspected_count": len(stale_suspected),
            }

            if stale_suspected:
                base["status"] = "stale_risk"
                base["stale_status_suspected"] = True
                base["operator_note"] = (
                    "Dispatcharr has a provider status that differs from its latest completion message. "
                    "StreamFlow will keep checking and only reports this as an observed sync note."
                )
            else:
                base["status"] = "ok"
                base["operator_note"] = "No Dispatcharr provider status/message contradiction was detected in the current UDI cache."
        except Exception as exc:
            logger.debug("Could not build external stale diagnostics: %s", exc, exc_info=True)
            base["status"] = "error"
            base["operator_note"] = "External stale-state diagnostics could not be built."
            base["error"] = exc.__class__.__name__

        return base


    @staticmethod
    def _clear_active_stream_reason(stream_status: Dict) -> None:
        for key in (
            'reason_detail',
            'quality_reason',
            'quality_reason_detail',
            'quality_reason_context',
        ):
            stream_status.pop(key, None)


    def _calculate_queue_eta_seconds(self, queue_status: Dict) -> int:
        avg_seconds = self._queue_number(queue_status.get('avg_stream_process_time_sec'))
        analysis_config = self.config.get('stream_analysis', {}) or {}
        configured_stream_seconds = 0.0
        configured_timeout_floor_seconds = 0.0
        if isinstance(analysis_config, dict):
            configured_stream_seconds = self._queue_number(analysis_config.get('ffmpeg_duration'))
            configured_timeout = self._queue_number(analysis_config.get('timeout'))
            startup_buffer = self._queue_number(analysis_config.get('stream_startup_buffer'))
            if configured_timeout > 0 or startup_buffer > 0:
                configured_timeout_floor_seconds = _stream_analysis_timeout(
                    configured_timeout,
                    configured_stream_seconds,
                    startup_buffer,
                )
        effective_stream_seconds = max(
            avg_seconds,
            configured_stream_seconds,
            configured_timeout_floor_seconds,
        )
        remaining_streams = (
            self._queue_number(queue_status.get('queued_streams_count'))
            + self._queue_number(queue_status.get('in_progress_streams_count'))
        )

        configured_workers = 1
        if self.config.get('concurrent_streams.enabled', True):
            concurrent_config = self.config.get('concurrent_streams', {}) or {}
            max_workers = self.config.get('concurrent_streams.global_limit', None)
            if isinstance(concurrent_config, dict):
                max_workers = concurrent_config.get(
                    'global_limit',
                    concurrent_config.get('max_workers', max_workers),
                )
            try:
                configured_workers = int(max_workers or 1)
            except (TypeError, ValueError):
                configured_workers = 1
            if configured_workers <= 0:
                configured_workers = max(1, int(remaining_streams or 1))

        effective_workers = configured_workers
        observed_workers = self._queue_number(queue_status.get('eta_active_stream_workers'))
        provider_waiting = self._queue_number(queue_status.get('eta_provider_waiting_streams'))
        if provider_waiting > 0 and observed_workers > 0:
            effective_workers = max(1, min(configured_workers, int(observed_workers)))

        stream_eta_seconds = 0.0
        if effective_stream_seconds > 0 and remaining_streams > 0:
            if self.config.get('concurrent_streams.enabled', True):
                stream_eta_seconds = (effective_stream_seconds * remaining_streams) / effective_workers
            else:
                stream_eta_seconds = effective_stream_seconds * remaining_streams

        completed_channels = (
            self._queue_number(queue_status.get('completed'))
            + self._queue_number(queue_status.get('failed'))
        )
        queued_channels = self._queue_number(queue_status.get('queued'))
        if queued_channels <= 0:
            queued_channels = self._queue_number(queue_status.get('queue_size'))
        remaining_channels = queued_channels + self._queue_number(queue_status.get('in_progress'))

        total_channels = self._queue_number(queue_status.get('total_queued'))
        if total_channels > 0:
            remaining_channels = max(
                remaining_channels,
                total_channels - completed_channels,
            )

        avg_channel_seconds = self._queue_number(queue_status.get('avg_channel_process_time_sec'))
        if completed_channels > 0:
            elapsed_avg = self._elapsed_seconds_since(queue_status.get('started_at')) / completed_channels
            avg_channel_seconds = max(avg_channel_seconds, elapsed_avg)

        channel_floor_seconds = 0.0
        if effective_stream_seconds > 0 and remaining_channels > 0:
            if remaining_streams > 0:
                avg_streams_per_channel = remaining_streams / max(1.0, remaining_channels)
                channel_batches = max(1, math.ceil(avg_streams_per_channel / max(1, effective_workers)))
            else:
                channel_batches = 1
            channel_floor_seconds = effective_stream_seconds * channel_batches * remaining_channels

        channel_eta_seconds = 0.0
        if avg_channel_seconds > 0 and remaining_channels > 0:
            channel_eta_seconds = avg_channel_seconds * remaining_channels
        channel_eta_seconds = max(channel_eta_seconds, channel_floor_seconds)

        eta_seconds = max(stream_eta_seconds, channel_eta_seconds)
        queue_status['eta_stream_seconds'] = int(stream_eta_seconds)
        queue_status['eta_channel_seconds'] = int(channel_eta_seconds)
        queue_status['eta_stream_observed_seconds'] = int(avg_seconds)
        queue_status['eta_stream_floor_seconds'] = int(configured_stream_seconds)
        queue_status['eta_stream_timeout_floor_seconds'] = int(configured_timeout_floor_seconds)
        queue_status['eta_channel_floor_seconds'] = int(channel_floor_seconds)
        queue_status['eta_configured_workers'] = int(configured_workers)
        queue_status['eta_effective_workers'] = int(effective_workers)
        if channel_eta_seconds > stream_eta_seconds and channel_eta_seconds > 0:
            queue_status['eta_basis'] = 'channel'
            queue_status['eta_basis_detail'] = (
                'channel_floor'
                if channel_floor_seconds >= channel_eta_seconds and channel_floor_seconds > 0
                else 'observed_channel'
            )
        elif configured_timeout_floor_seconds > configured_stream_seconds and configured_timeout_floor_seconds >= avg_seconds:
            queue_status['eta_basis'] = 'stream_timeout_floor'
            queue_status['eta_basis_detail'] = 'stream_timeout_floor'
        elif configured_stream_seconds >= avg_seconds and configured_stream_seconds > 0:
            queue_status['eta_basis'] = 'stream_floor'
            queue_status['eta_basis_detail'] = 'stream_floor'
        else:
            queue_status['eta_basis'] = 'observed_stream'
            queue_status['eta_basis_detail'] = 'observed_stream'
        return int(eta_seconds)


    def _clear_progress_snapshot_if_idle(self, expected_progress: Optional[Dict]) -> bool:
        """Clear an observed progress row only while no operation can own it."""
        if not expected_progress:
            return False
        with self.lock:
            with self.check_queue.lock:
                active = bool(
                    self.checking
                    or getattr(self, '_single_stream_check_active', False)
                    or getattr(self, '_single_channel_check_active', False)
                    or getattr(self, '_automation_cycle_active', False)
                    or getattr(self, '_sync_batch_execution_active', False)
                    or (self.sync_batch_state or {}).get('active')
                    or getattr(self, '_active_queue_entry_executions', {})
                    or self.check_queue.queued
                    or self.check_queue.in_progress
                    or self.check_queue.stats.get('current_channel') is not None
                )
                if active:
                    return False
            return self.progress.clear_if_matches(expected_progress)


    def get_status(self) -> Dict:
        """Get current service status."""
        queue_status = self.check_queue.get_status()
        progress = self.progress.get()
        observed_progress = deepcopy(progress)
        worker_context = self._active_worker_context(progress)
        if worker_context['active'] > 0:
            queue_status['eta_active_stream_workers'] = worker_context['active']
        if worker_context['waiting'] > 0:
            queue_status['eta_provider_waiting_streams'] = worker_context['waiting']

        with self.lock:
            sync_state = dict(self.sync_batch_state)
            single_channel_check_active = bool(
                getattr(self, '_single_channel_check_active', False)
            )
            automation_cycle_active = bool(
                getattr(self, '_automation_cycle_active', False)
            )
            sync_execution_active = bool(
                getattr(self, '_sync_batch_execution_active', False)
            )
            queue_execution_active = bool(
                getattr(self, '_active_queue_entry_executions', {})
            )

        if sync_state.get('active'):
            # Override queue status with our synchronous batch status
            # When active, ONLY the sync batch progress should be displayed
            # The public aggregates below describe the synchronous batch, not
            # the background queue maps returned by StreamCheckQueue. Never
            # claim an exact entry snapshot across those two ownership domains.
            queue_status['entries_complete'] = False
            queue_status['entries_unavailable_reason'] = 'sync_batch_active'
            queue_status['queued_entries'] = []
            queue_status['in_progress_entries'] = []
            queue_status['completed_entries'] = []
            queue_status['failed_entries'] = []
            queue_status['completed_channel_ids'] = []
            queue_status['failed_channel_ids'] = []
            queued_channels = max(
                0,
                sync_state['total_channels']
                - sync_state['completed']
                - sync_state['failed']
                - sync_state['in_progress'],
            )
            queue_status['in_progress'] = sync_state['in_progress']
            queue_status['completed'] = sync_state['completed']
            queue_status['failed'] = sync_state['failed']
            queue_status['queued'] = queued_channels
            queue_status['total_queued'] = sync_state['total_channels']
            queue_status['total_completed'] = sync_state['completed']
            queue_status['total_failed'] = sync_state['failed']
            queue_status['queue_size'] = queue_status['queued']
            queue_status['started_at'] = sync_state.get('started_at')
            if queue_status['in_progress'] > 0:
                queue_status['state'] = 'checking'
            elif queue_status['queue_size'] > 0:
                queue_status['state'] = 'queued'
            elif queue_status['completed'] or queue_status['failed']:
                queue_status['state'] = 'completed'
            else:
                queue_status['state'] = 'idle'

            # Map tracking stream properties back over queue_status for calculations
            queue_status['queued_streams_count'] = sync_state.get('queued_streams_count', 0)
            queue_status['in_progress_streams_count'] = sync_state.get('in_progress_streams_count', 0)
            queue_status['good_streams_count'] = sync_state.get('good_streams_count', 0)
            queue_status['dead_streams_count'] = sync_state.get('dead_streams_count', 0)
            queue_status['blank_streams_count'] = sync_state.get('blank_streams_count', 0)
            queue_status['freeze_streams_count'] = sync_state.get('freeze_streams_count', 0)
            queue_status['channels_hidden'] = sync_state.get('channels_hidden', 0)
            queue_status['channels_ready'] = sync_state.get('channels_ready', 0)
            queue_status['channel_visibility_changed'] = sync_state.get('channel_visibility_changed', 0)

            # Use real queue averages if available, otherwise 0
            queue_snapshot = self.check_queue.get_status()
            queue_status['avg_stream_process_time_sec'] = queue_snapshot.get('avg_stream_process_time_sec', 0)
            queue_status['avg_channel_process_time_sec'] = queue_snapshot.get('avg_channel_process_time_sec', 0)

        queue_status['eta_seconds'] = self._calculate_queue_eta_seconds(queue_status)

        queued_waiting = queue_status.get('queue_size', 0) > 0
        queue_processing = bool(
            queue_status.get('in_progress', 0) > 0 or
            queue_status.get('current_channel') is not None
        )
        worker_or_queue_active = bool(
            self.checking or
            single_channel_check_active or
            automation_cycle_active or
            queue_execution_active or
            queue_processing or
            (self.running and queued_waiting) or
            sync_state.get('active', False) or
            sync_execution_active
        )
        progress_stale = self._current_progress_stale_gate(
            progress,
            worker_or_queue_active=worker_or_queue_active,
        )
        if progress_stale:
            progress = {
                **progress,
                'stale': True,
                'stale_reason': progress_stale.get('reason'),
                'stale_age_seconds': progress_stale.get('age_seconds'),
                'stale_after_seconds': progress_stale.get('stale_after_seconds'),
            }
            cleanup_after = progress_stale.get('cleanup_after_seconds')
            age_seconds = progress_stale.get('age_seconds')
            if age_seconds is None or (cleanup_after is not None and age_seconds >= cleanup_after):
                if self._clear_progress_snapshot_if_idle(observed_progress):
                    progress_stale['cleared'] = True
                    progress = None
                else:
                    progress = self.progress.get()

        single_channel_progress_active = bool(
            progress and
            progress.get('is_single_channel_check') and
            not progress.get('stale')
        )

        # Stream checking mode is active when:
        # - An individual channel is being checked or preparing to be checked, OR
        # - There are channels in the queue waiting to be checked
        stream_checking_mode = (
            worker_or_queue_active or
            single_channel_progress_active or
            sync_state.get('active', False)
        )

        if not stream_checking_mode and progress and not progress.get('stale'):
            if self._clear_progress_snapshot_if_idle(progress):
                progress = None

        self._maybe_refresh_stale_connectivity_guard(stream_checking_mode)
        connectivity_guard_status = dict(self.connectivity_guard_status or {})
        guard_failed = connectivity_guard_status.get('ok') is False
        connectivity_guard_status['active_failure'] = bool(guard_failed and stream_checking_mode)
        connectivity_guard_status['stale_failure'] = bool(guard_failed and not stream_checking_mode)
        external_stale_diagnostics = self._build_external_stale_diagnostics(
            stream_checking_mode=stream_checking_mode,
            queue_status=queue_status,
            progress=progress,
        )

        return {
            'running': self.running,
            'checking': bool(self.checking or queue_execution_active),
            'stream_checking_mode': stream_checking_mode,
            'queue_execution_active': queue_execution_active,
            'single_channel_check_active': bool(
                single_channel_check_active
            ),
            'automation_cycle_active': automation_cycle_active,
            'sync_batch_execution_active': sync_execution_active,
            'enabled': self.config.get('enabled', True),
            'operation_timings': STREAM_OPERATION_TIMINGS.snapshot(),
            'queue': queue_status,
            'progress': progress,
            'progress_stale': bool(progress_stale),
            'progress_stale_details': progress_stale or {},
            'connectivity_guard': connectivity_guard_status,
            'external_stale_diagnostics': external_stale_diagnostics,
            'last_global_check': self.update_tracker.get_last_global_check(),
            'config': {
                'automation_controls': self.config.get('automation_controls', {}),
                'check_interval': self.config.get('check_interval'),
                'global_check_schedule': self.config.get('global_check_schedule'),
                'queue_settings': self.config.get('queue'),
                'channel_visibility_automation': self.config.get('channel_visibility_automation', {})
            }
        }

