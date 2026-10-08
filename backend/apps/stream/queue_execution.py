"""Specialized queue execution, validation, and terminal ownership."""

import logging
import time
from typing import Any, Dict, Optional
from apps.core.operation_timing import STREAM_OPERATION_TIMINGS

logger = logging.getLogger(__name__)


class StreamCheckQueueExecutionMixin:
    def _run_specialized_queue_entry_owned(
        self,
        queue_entry: Dict[str, Any],
        *,
        force_check_generation: Optional[int] = None,
    ) -> None:
        channel_id = queue_entry.get('channel_id')
        queue_entry_token = queue_entry.get('queue_entry_token')
        queue_metadata = queue_entry.get('metadata') or {}
        forced_profile_id = queue_metadata.get('forced_profile_id')
        single_check_kwargs = {
            'program_name': queue_metadata.get('program_name'),
            'is_epg_scheduled': bool(queue_metadata.get('is_epg_scheduled')),
            'forced_profile_id': forced_profile_id,
        }
        if queue_metadata.get('source') == 'teamarr_preflight':
            single_check_kwargs['run_mode'] = 'teamarr_preflight'
        if queue_metadata.get('provider_limit_override'):
            single_check_kwargs['provider_limit_override'] = True

        with self.lock:
            abort_was_set = self.abort_current_check.is_set()
            external_abort_generation = int(
                getattr(self, '_external_abort_generation', 0)
            )
            owned_sync_generation = (
                getattr(self, '_sync_batch_execution_generation', None)
                if getattr(self, '_sync_batch_execution_active', False)
                else None
            )
        queued_at = queue_metadata.get('queued_monotonic')
        if queued_at is not None:
            STREAM_OPERATION_TIMINGS.record('queue_wait', time.monotonic() - queued_at)
        result = None
        if queue_metadata.get('source') == 'teamarr_preflight':
            from apps.stream.teamarr_preflight_service import get_teamarr_preflight_service
            result = get_teamarr_preflight_service().validate_queued_check(queue_metadata)
        if result is None:
            result = self.check_single_channel(
                channel_id,
                _operation_already_reserved=True,
                _queue_force_check_generation=force_check_generation,
                _queue_entry_token=queue_entry_token,
                **single_check_kwargs,
            )

        def record_teamarr_result() -> None:
            if queue_metadata.get('source') != 'teamarr_preflight':
                return
            try:
                from apps.stream.teamarr_preflight_service import get_teamarr_preflight_service

                get_teamarr_preflight_service().record_queued_check_result(
                    queue_metadata,
                    result,
                )
            except Exception as exc:
                logger.warning(
                    "Failed to record queued Teamarr preflight result for channel %s: %s",
                    channel_id,
                    exc,
                )
        with self.lock:
            external_abort_unchanged = (
                int(getattr(self, '_external_abort_generation', 0))
                == external_abort_generation
            )
            sync_owner_unchanged = bool(
                owned_sync_generation is None
                or (
                    getattr(self, '_sync_batch_execution_active', False)
                    and getattr(self, '_sync_batch_execution_generation', None)
                    == owned_sync_generation
                    and (self.sync_batch_state or {}).get('active')
                    and (self.sync_batch_state or {}).get('generation')
                    == owned_sync_generation
                )
            )
            isolate_connectivity_abort = (
                self._should_isolate_teamarr_connectivity_abort(
                    queue_metadata,
                    result,
                    abort_was_set,
                    external_abort_unchanged=external_abort_unchanged,
                    sync_owner_unchanged=sync_owner_unchanged,
                )
            )
            if isolate_connectivity_abort:
                self.abort_current_check.clear()
                self._cancel_queueing = False
        if isolate_connectivity_abort:
            logger.info(
                "Isolating Teamarr queued connectivity abort from synchronous batch "
                "channel_id=%s source=%s",
                channel_id,
                queue_metadata.get('source'),
            )
        if isinstance(result, dict) and result.get('success') is False:
            self._fail_channel_check(
                channel_id,
                result.get('error') or result.get('reason') or 'single channel check failed',
                record_teamarr_result,
                queue_entry_token=queue_entry_token,
                allow_already_failed_side_effects=True,
            )
        else:
            self._complete_channel_check(
                channel_id,
                record_teamarr_result,
                queue_entry_token=queue_entry_token,
                allow_already_completed_side_effects=True,
            )
