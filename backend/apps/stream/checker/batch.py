"""Batch responsibilities for the shared StreamCheckerService instance."""

import logging
import threading
import time
from datetime import datetime
from typing import Callable, Dict, List, Optional
from apps.udi import get_udi_manager

logger = logging.getLogger("apps.stream.stream_checker_service")


class CheckerBatchMixin:
    @staticmethod
    def _count_checked_stream_status(result: Dict, status: str) -> int:
        checked_streams = result.get('checked_streams', []) if isinstance(result, dict) else []
        if not isinstance(checked_streams, list):
            return 0
        detected_key = f"{status}_detected"
        return sum(
            1
            for stream in checked_streams
            if isinstance(stream, dict)
            and (
                stream.get('status') == status
                or stream.get(detected_key) is True
            )
        )


    @staticmethod
    def _count_good_checked_streams(result: Dict) -> int:
        checked_streams = result.get('checked_streams', []) if isinstance(result, dict) else []
        if not isinstance(checked_streams, list):
            return 0
        bad_reasons = {'blank', 'freeze', 'low_quality', 'offline', 'unstable'}
        good_statuses = {None, '', 'completed', 'revived', 'incomplete_bitrate'}
        return sum(
            1
            for stream in checked_streams
            if isinstance(stream, dict)
            and stream.get('status') in good_statuses
            and stream.get('blank_detected') is not True
            and stream.get('freeze_detected') is not True
            and stream.get('dead_reason') not in bad_reasons
            and (
                stream.get('status') == 'incomplete_bitrate'
                or stream.get('quality_reason_detail') in {None, '', 'none'}
            )
        )


    @staticmethod
    def _count_failed_checked_streams(result: Dict) -> int:
        checked_streams = result.get('checked_streams', []) if isinstance(result, dict) else []
        if not isinstance(checked_streams, list):
            return 0
        bad_statuses = {'blank', 'freeze', 'low_quality', 'dead', 'offline', 'error', 'failed', 'timeout', 'unstable'}
        bad_reasons = bad_statuses
        return sum(
            1
            for stream in checked_streams
            if isinstance(stream, dict)
            and (
                stream.get('status') in bad_statuses
                or stream.get('blank_detected') is True
                or stream.get('freeze_detected') is True
                or stream.get('dead_reason') in bad_reasons
                or stream.get('reason') in bad_reasons
                or stream.get('reason_detail') in bad_reasons
                or stream.get('quality_reason') in bad_reasons
                or stream.get('quality_reason_detail') in bad_reasons
            )
        )


    @classmethod
    def _result_good_streams_count(cls, result: Dict) -> int:
        if not isinstance(result, dict):
            return 0
        fallback_count = cls._count_good_checked_streams(result)
        try:
            raw = result.get('good_streams_count')
            if raw is not None:
                return max(0, int(raw or 0), fallback_count)
        except (TypeError, ValueError):
            pass
        return fallback_count


    @classmethod
    def _result_count(cls, result: Dict, key: str, fallback_status: Optional[str] = None) -> int:
        if not isinstance(result, dict):
            return 0
        fallback_count = cls._count_checked_stream_status(result, fallback_status) if fallback_status else 0
        try:
            raw = result.get(key)
            if raw is not None:
                return max(0, int(raw or 0), fallback_count)
        except (TypeError, ValueError):
            pass
        return fallback_count


    def check_channels_synchronously(
        self,
        channel_ids: List[int],
        force_check: bool = False,
        target_stream_ids: Optional[Dict[int, List[str]]] = None,
        progress_callback: Optional[Callable[[int, int, Dict], None]] = None,
        run_mode: Optional[str] = None,
    ) -> Dict[int, Dict]:
        """Check multiple channels synchronously and return results.

        Using this method bypasses the normal worker/scheduler for the batch
        channels. Event-triggered queue entries are still drained serially
        between batch channels so preflight/auto-create checks can run without
        opening parallel provider streams next to the synchronous batch.

        Args:
            channel_ids: List of channel IDs to check
            force_check: If True, marks channels for force checking
            target_stream_ids: Optional dict mapping channel_id -> list of stream_ids.
                               If provided, only these specific streams will be evaluated.
                               Any stream not in the list will be skipped and its existing
                               stats cached. Used by automation for newly matched streams.
            progress_callback: Optional callback invoked after each channel completes
                               with (completed_count, total_channels, channel_result).

        Returns:
            Dict mapping channel_id to result dict (containing dead/revived streams)
        """
        results = {}

        # Fast lookup precise stream counts
        from apps.udi import get_udi_manager
        udi = get_udi_manager()

        channel_streams = {}
        total_streams = 0
        for channel_id in channel_ids:
            channel = udi.get_channel_by_id(channel_id)
            stream_count = len(channel.get('streams', [])) if channel else 1
            channel_streams[channel_id] = stream_count
            total_streams += stream_count

        with self.lock:
            with self.check_queue.lock:
                sync_state_active = bool((self.sync_batch_state or {}).get('active'))
                sync_execution_active = bool(
                    getattr(self, '_sync_batch_execution_active', False)
                )
                queue_in_progress = bool(
                    self.check_queue.in_progress
                    or self.check_queue.stats.get('current_channel') is not None
                    or getattr(self, '_active_queue_entry_executions', {})
                )
                automation_cycle_conflict = bool(
                    getattr(self, '_automation_cycle_active', False)
                    and getattr(self, '_automation_cycle_owner_thread_id', None)
                    != threading.get_ident()
                )
                automation_cycle_owned = bool(
                    getattr(self, '_automation_cycle_active', False)
                    and getattr(self, '_automation_cycle_owner_thread_id', None)
                    == threading.get_ident()
                )
                automation_abort_changed = bool(
                    automation_cycle_owned
                    and (
                        int(getattr(self, '_external_abort_generation', 0))
                        != int(
                            getattr(
                                self,
                                '_automation_cycle_abort_generation',
                                getattr(self, '_external_abort_generation', 0),
                            )
                        )
                        or self.abort_current_check.is_set()
                    )
                )
                if automation_abort_changed:
                    logger.info(
                        "Synchronous quality batch skipped because the owning "
                        "automation cycle was aborted"
                    )
                    return {
                        channel_id: {
                            'success': False,
                            'error': 'aborted',
                            'skip_reason': 'aborted',
                            'message': 'Automation run was aborted before quality checking',
                            'aborted': True,
                            'channel_id': channel_id,
                        }
                        for channel_id in channel_ids
                    }
                if (
                    self.checking
                    or getattr(self, '_single_stream_check_active', False)
                    or getattr(self, '_single_channel_check_active', False)
                    or automation_cycle_conflict
                    or sync_state_active
                    or sync_execution_active
                    or queue_in_progress
                ):
                    logger.warning(
                        "Cannot start synchronous quality batch while Stream Checker work is active"
                    )
                    return {
                        channel_id: {
                            'success': False,
                            'error': 'stream_checker_active',
                            'skip_reason': 'stream_checker_active',
                            'message': 'Stream Checker work is already active',
                            'aborted': True,
                            'channel_id': channel_id,
                        }
                        for channel_id in channel_ids
                    }

                previous_queue_paused = bool(self.check_queue.paused)
                self.check_queue.paused = True
                self.abort_current_check.clear()
                self._sync_batch_generation = getattr(self, '_sync_batch_generation', 0) + 1
                sync_generation = self._sync_batch_generation
                self._sync_batch_execution_active = True
                self._sync_batch_execution_generation = sync_generation
                self.sync_batch_state = {
                    'active': True,
                    'total_channels': len(channel_ids),
                    'completed': 0,
                    'failed': 0,
                    'in_progress': 0,
                    'queued_streams_count': total_streams,
                    'in_progress_streams_count': 0,
                    'good_streams_count': 0,
                    'dead_streams_count': 0,
                    'blank_streams_count': 0,
                    'freeze_streams_count': 0,
                    'channels_hidden': 0,
                    'channels_ready': 0,
                    'channel_visibility_changed': 0,
                    'started_at': datetime.now().isoformat(),
                    'generation': sync_generation,
                }
                self.checking = True

        try:
            # Connectivity can set the shared abort event, so it must run only
            # after this batch owns the checker reservation.
            failed_connectivity = self._require_quality_check_connectivity(
                phase='sync_batch_preflight',
                update_progress=False,
            )
            if failed_connectivity is not None:
                return {
                    channel_id: self._connectivity_abort_payload(
                        failed_connectivity,
                        channel_id=channel_id,
                    )
                    for channel_id in channel_ids
                }

            self._apply_specialized_queue_deferral()

            # Process each channel
            for channel_id in channel_ids:
                if self.abort_current_check.is_set():
                    logger.info("Synchronous channel batch aborted before next channel")
                    break

                self._drain_specialized_queue_entries()
                if self.abort_current_check.is_set():
                    logger.info("Synchronous channel batch aborted after specialized queue drain")
                    break

                stream_count = channel_streams.get(channel_id, 1)

                with self.lock:
                    if self.sync_batch_state.get('generation') != sync_generation or not self.sync_batch_state.get('active'):
                        logger.info("Synchronous channel batch was cleared; stopping remaining checks")
                        break
                    self.sync_batch_state['in_progress'] = 1
                    self.sync_batch_state['queued_streams_count'] = max(0, self.sync_batch_state['queued_streams_count'] - stream_count)
                    self.sync_batch_state['in_progress_streams_count'] = stream_count

                channel_started_monotonic = time.monotonic()
                try:
                    # Both modes use the smart scheduler. Sequential mode limits
                    # it to one active basis probe so capacity reservations and
                    # deferred bitrate rechecks keep the same contract.
                    concurrent_enabled = self.config.get('concurrent_streams.enabled', True)

                    if target_stream_ids and channel_id in target_stream_ids:
                        stream_id_whitelist = target_stream_ids[channel_id]
                    else:
                        stream_id_whitelist = None

                    if self._checker_get_session_manager().is_channel_in_active_session(channel_id):
                        logger.info(
                            "Skipping synchronous quality check for monitored channel %s",
                            channel_id,
                        )
                        channel_result = {
                            'success': True,
                            'skipped': True,
                            'reason': 'in_monitoring_session',
                            'channel_id': channel_id,
                        }
                    elif concurrent_enabled:
                        channel_result = self._check_channel_concurrent(
                            channel_id,
                            skip_batch_changelog=True,
                            target_stream_ids=stream_id_whitelist,
                            run_mode=run_mode,
                            force_check_override=force_check,
                        )
                    else:
                        channel_result = self._check_channel_concurrent(
                            channel_id,
                            skip_batch_changelog=True,
                            target_stream_ids=stream_id_whitelist,
                            run_mode=run_mode,
                            global_limit_override=1,
                            force_check_override=force_check,
                        )

                    results[channel_id] = channel_result
                    with self.lock:
                        if self.sync_batch_state.get('generation') == sync_generation and self.sync_batch_state.get('active'):
                            if (
                                not isinstance(channel_result, dict)
                                or channel_result.get('aborted')
                                or channel_result.get('success') is False
                                or channel_result.get('error')
                            ):
                                self.sync_batch_state['failed'] += 1
                            else:
                                self.sync_batch_state['completed'] += 1
                            if isinstance(channel_result, dict):
                                self.sync_batch_state['good_streams_count'] += self._result_good_streams_count(channel_result)
                                self.sync_batch_state['dead_streams_count'] += self._result_count(channel_result, 'dead_streams_count')
                                self.sync_batch_state['blank_streams_count'] += self._result_count(
                                    channel_result,
                                    'blank_streams_count',
                                    fallback_status='blank',
                                )
                                self.sync_batch_state['freeze_streams_count'] += self._result_count(
                                    channel_result,
                                    'freeze_streams_count',
                                    fallback_status='freeze',
                                )
                                visibility_summary = self._single_channel_visibility_summary(
                                    channel_result.get('channel_visibility')
                                )
                                self.sync_batch_state['channels_hidden'] += visibility_summary.get('channels_hidden', 0)
                                self.sync_batch_state['channels_ready'] += visibility_summary.get('channels_ready', 0)
                                self.sync_batch_state['channel_visibility_changed'] += visibility_summary.get(
                                    'channel_visibility_changed',
                                    0,
                                )

                    if isinstance(channel_result, dict) and channel_result.get('aborted'):
                        logger.info("Synchronous channel batch aborted; stopping remaining checks")
                        break
                except Exception as e:
                    logger.error(f"Error checking channel {channel_id} synchronously: {e}")
                    results[channel_id] = {'error': str(e)}
                    with self.lock:
                        if self.sync_batch_state.get('generation') == sync_generation and self.sync_batch_state.get('active'):
                            self.sync_batch_state['failed'] += 1
                finally:
                    if progress_callback is not None:
                        try:
                            with self.lock:
                                completed_count = (
                                    int(self.sync_batch_state.get('completed', 0) or 0)
                                    + int(self.sync_batch_state.get('failed', 0) or 0)
                                )
                            progress_callback(completed_count, len(channel_ids), results.get(channel_id, {}))
                        except Exception as progress_error:
                            logger.debug("Synchronous channel progress callback failed: %s", progress_error)

                    duration_sec = time.monotonic() - channel_started_monotonic
                    with self.lock:
                        sync_still_active = bool(
                            self.sync_batch_state.get('generation') == sync_generation
                            and self.sync_batch_state.get('active')
                        )
                        if sync_still_active and stream_count > 0:
                            time_per_stream = duration_sec / stream_count
                            with self.check_queue.lock:
                                self.check_queue.channel_processing_times.append(
                                    duration_sec
                                )
                                self.check_queue.stream_processing_times.append(
                                    time_per_stream
                                )
                        if sync_still_active:
                            self.sync_batch_state['in_progress'] = 0
                            self.sync_batch_state['in_progress_streams_count'] = 0
            if not self.abort_current_check.is_set():
                self._drain_specialized_queue_entries()
        finally:
            with self.lock:
                with self.check_queue.lock:
                    if self.sync_batch_state.get('generation') == sync_generation:
                        self.sync_batch_state['active'] = False
                    if (
                        getattr(self, '_sync_batch_execution_generation', None)
                        == sync_generation
                    ):
                        self._sync_batch_execution_active = False
                        self._sync_batch_execution_generation = None
                        self.check_queue.paused = previous_queue_paused
                        # This execution no longer owns checking state. Queued
                        # work blocks new reservations by queue membership and
                        # the worker sets checking when it actually starts.
                        self.checking = False
            self._apply_specialized_queue_deferral()

        return results

