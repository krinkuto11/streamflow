"""Queue responsibilities for the shared StreamCheckerService instance."""

import logging
from copy import deepcopy
from typing import Any, Dict, List, Optional, Set, Tuple
from apps.stream.queue_start import order_channels_for_queue_start
from apps.automation.automation_config_manager import get_automation_config_manager
from .constants import SPECIALIZED_QUEUE_SOURCES

logger = logging.getLogger("apps.stream.stream_checker_service")


class CheckerQueueMixin:
    def _is_specialized_queue_metadata(self, metadata: Dict[str, Any]) -> bool:
        if not isinstance(metadata, dict):
            return False
        return bool(
            metadata.get('is_epg_scheduled')
            or metadata.get('source') in SPECIALIZED_QUEUE_SOURCES
        )


    @staticmethod
    def _should_isolate_teamarr_connectivity_abort(
        queue_metadata: Dict[str, Any],
        result: Any,
        abort_was_set: bool,
        external_abort_unchanged: bool = True,
        sync_owner_unchanged: bool = True,
    ) -> bool:
        if abort_was_set or not external_abort_unchanged or not sync_owner_unchanged:
            return False
        if not isinstance(queue_metadata, dict) or queue_metadata.get('source') != 'teamarr_preflight':
            return False
        if not isinstance(result, dict) or not result.get('aborted'):
            return False
        for key in ('skip_reason', 'error', 'reason', 'quality_reason_detail'):
            if str(result.get(key) or '').lower() == 'connectivity_guard':
                return True
        return False


    def _sync_batch_active(self) -> bool:
        try:
            with self.lock:
                return bool(
                    self.sync_batch_state.get('active')
                    and self.sync_batch_state.get('generation') == self._sync_batch_generation
                )
        except Exception:
            return False


    def _specialized_queue_gate_active_locked(self) -> bool:
        sync_batch_state = getattr(self, 'sync_batch_state', {}) or {}
        return bool(
            getattr(self, '_specialized_queue_gates', set())
            or (
                sync_batch_state.get('active')
                and sync_batch_state.get('generation') == getattr(self, '_sync_batch_generation', None)
            )
        )


    def _apply_specialized_queue_deferral(self) -> None:
        defer_metadata_sources = getattr(self.check_queue, 'defer_metadata_sources', None)
        if not callable(defer_metadata_sources):
            return
        lock = getattr(self, 'lock', None)
        if lock is None:
            should_defer = self._specialized_queue_gate_active_locked()
            defer_metadata_sources(
                SPECIALIZED_QUEUE_SOURCES if should_defer else set()
            )
        else:
            with lock:
                should_defer = self._specialized_queue_gate_active_locked()
                defer_metadata_sources(
                    SPECIALIZED_QUEUE_SOURCES if should_defer else set()
                )


    def set_specialized_queue_gate(self, gate_name: str, active: bool) -> None:
        """Pause event-style queue entries while an external event check runs."""
        gate = str(gate_name or '').strip()
        if not gate:
            return
        with self.lock:
            if not hasattr(self, '_specialized_queue_gates'):
                self._specialized_queue_gates = set()
            if active:
                self._specialized_queue_gates.add(gate)
            else:
                self._specialized_queue_gates.discard(gate)
            self.check_queue.defer_metadata_sources(
                SPECIALIZED_QUEUE_SOURCES
                if self._specialized_queue_gate_active_locked()
                else set()
            )


    def _queue_force_check_state(
        self,
        channel_id: int,
    ) -> Tuple[bool, Optional[int]]:
        """Snapshot the force intent owned by a queue entry."""
        tracker = getattr(self, 'update_tracker', None)
        if tracker is None:
            return False, None
        getter = getattr(tracker, 'get_force_check_state', None)
        if not callable(getter):
            return False, None
        state = getter(channel_id)
        if not isinstance(state, (tuple, list)) or len(state) != 2:
            return False, None
        pending, generation = state
        try:
            generation = int(generation) if generation is not None else None
        except (TypeError, ValueError):
            generation = None
        return bool(pending), generation


    def _claim_queue_entry_execution(
        self,
        channel_id: int,
        queue_entry_token: Optional[int],
    ) -> bool:
        """Reserve a popped queue call until its worker stack has fully exited."""
        lock = getattr(self, 'lock', None)
        if lock is None:
            owns_entry = self.check_queue.owns_in_progress(
                channel_id,
                queue_entry_token,
            )
            if not owns_entry:
                return False
            executions = getattr(self, '_active_queue_entry_executions', None)
            if executions is None:
                executions = {}
                self._active_queue_entry_executions = executions
            executions[(channel_id, queue_entry_token)] = {'cancelled': False}
            return True

        with lock:
            if not self.check_queue.owns_in_progress(
                channel_id,
                queue_entry_token,
            ):
                return False
            if not hasattr(self, '_active_queue_entry_executions'):
                self._active_queue_entry_executions = {}
            self._active_queue_entry_executions[
                (channel_id, queue_entry_token)
            ] = {'cancelled': False}
            return True


    def _release_queue_entry_execution(
        self,
        channel_id: int,
        queue_entry_token: Optional[int],
    ) -> None:
        """Release only the exact worker execution reservation."""
        execution_key = (channel_id, queue_entry_token)
        lock = getattr(self, 'lock', None)
        if lock is None:
            getattr(self, '_active_queue_entry_executions', {}).pop(
                execution_key,
                None,
            )
            return
        with lock:
            executions = getattr(self, '_active_queue_entry_executions', {})
            execution = executions.get(execution_key)
            if execution is None:
                return
            if execution.get('cancelled'):
                expected_progress = self.progress.get()
                if (
                    expected_progress
                    and str(expected_progress.get('channel_id')) == str(channel_id)
                ):
                    self.progress.clear_if_matches(expected_progress)
            executions.pop(execution_key, None)


    def _cancel_active_queue_entry_executions_locked(self) -> None:
        """Cancel worker reservations without releasing their ownership fence."""
        for execution in getattr(
            self,
            '_active_queue_entry_executions',
            {},
        ).values():
            execution['cancelled'] = True


    def _clear_queue_entry_progress(
        self,
        channel_id: int,
        queue_entry_token: Optional[int],
    ) -> bool:
        """Clear progress only while the exact queue execution still owns cleanup."""
        lock = getattr(self, 'lock', None)
        execution_key = (channel_id, queue_entry_token)
        if lock is None:
            execution = getattr(
                self,
                '_active_queue_entry_executions',
                {},
            ).get(execution_key)
            if execution is None:
                return False
            expected_progress = self.progress.get()
            if (
                not expected_progress
                or str(expected_progress.get('channel_id')) != str(channel_id)
            ):
                return False
            return self.progress.clear_if_matches(expected_progress)

        with lock:
            execution = getattr(
                self,
                '_active_queue_entry_executions',
                {},
            ).get(execution_key)
            if execution is None:
                return False
            expected_progress = self.progress.get()
            if (
                not expected_progress
                or str(expected_progress.get('channel_id')) != str(channel_id)
            ):
                return False
            return self.progress.clear_if_matches(expected_progress)


    def _run_specialized_queue_entry(
        self,
        queue_entry: Dict[str, Any],
        *,
        force_check_generation: Optional[int] = None,
    ) -> None:
        """Run a specialized entry while retaining its post-clear tombstone."""
        channel_id = queue_entry.get('channel_id')
        queue_entry_token = queue_entry.get('queue_entry_token')
        if not self._claim_queue_entry_execution(
            channel_id,
            queue_entry_token,
        ):
            logger.info(
                "Skipping specialized queue entry %s because its activation "
                "was already cleared",
                channel_id,
            )
            return
        try:
            return self._run_specialized_queue_entry_owned(
                queue_entry,
                force_check_generation=force_check_generation,
            )
        finally:
            self._release_queue_entry_execution(
                channel_id,
                queue_entry_token,
            )


    def _drain_specialized_queue_entries(self, *, max_entries: int = 25) -> int:
        drained = 0
        for _ in range(max(0, int(max_entries))):
            if self.abort_current_check.is_set():
                break
            queue_entry = self.check_queue.get_next_entry_for_metadata_sources(
                SPECIALIZED_QUEUE_SOURCES
            )
            if queue_entry is None:
                break
            logger.info(
                "Running specialized queued check serially during synchronous batch "
                "channel_id=%s source=%s",
                queue_entry.get('channel_id'),
                (queue_entry.get('metadata') or {}).get('source'),
            )
            channel_id = queue_entry.get('channel_id')
            force_pending, force_generation = self._queue_force_check_state(
                channel_id
            )
            try:
                self._run_specialized_queue_entry(
                    queue_entry,
                    force_check_generation=(
                        force_generation if force_pending else None
                    ),
                )
            except Exception as entry_error:
                self.check_queue.mark_failed(
                    channel_id,
                    str(entry_error),
                    entry_token=queue_entry.get('queue_entry_token'),
                )
                raise
            finally:
                if force_pending:
                    self.update_tracker.clear_force_check(
                        channel_id,
                        expected_generation=force_generation,
                    )
            drained += 1
        return drained


    def _queue_updated_channels(self):
        """Queue channels that have received M3U updates.

        This respects automation_controls and queues channels only when
        automatic quality checking is enabled.
        """
        # Do not queue channels before the startup network UDI refresh completes.
        # is_initialized() alone is True from SQL storage load (potentially empty cache).
        if not self._checker_get_udi_manager().is_network_ready():
            logger.debug("Skipping channel queueing — UDI network refresh not yet complete")
            return

        # Check if auto quality checking is enabled (considers both pipeline mode and individual controls)
        if not self.config.is_auto_quality_checking_enabled():
            logger.info("Skipping channel queueing - automatic quality checking is disabled")
            return

        max_channels = self.config.get('queue.max_channels_per_run', 50)

        with self.lock:
            if getattr(self, '_cancel_queueing', False):
                logger.info("Skipping updated-channel queueing after cancellation")
                return
            # Selection, needs-check consumption, and queue commit form one
            # transaction with manual clear_queue().
            channels_to_queue = self.update_tracker.get_and_clear_channels_needing_check(
                max_channels
            )

            if channels_to_queue:
                for channel_id in channels_to_queue:
                    self.check_queue.remove_from_completed(channel_id)

                added = self.check_queue.add_channels(
                    channels_to_queue,
                    priority=10,
                )
                logger.info(
                    f"Queued {added}/{len(channels_to_queue)} updated channels for checking"
                )
            else:
                logger.debug("No channels need checking")


    def _queue_all_channels(self, force_check: bool = False):
        """Queue all channels for checking (global check).

        Args:
            force_check: If True, marks channels for force checking which bypasses 2-hour immunity
        """
        with self.lock:
            self._cancel_queueing = False
        try:
            udi = self._checker_get_udi_manager()

            # Do not queue channels before the startup network UDI refresh completes.
            if not udi.is_network_ready():
                logger.debug("Skipping global channel queue — UDI network refresh not yet complete")
                return

            channels = udi.get_channels()

            if channels:
                channel_ids = [ch['id'] for ch in channels if isinstance(ch, dict) and 'id' in ch]

                # Filter by profile if one is selected
                # Filter channels using Automation Profiles
                from apps.automation.automation_config_manager import get_automation_config_manager
                automation_config = get_automation_config_manager()

                filtered_channels = []

                for ch in channels:
                    if not isinstance(ch, dict) or 'id' not in ch:
                        continue

                    cid = ch['id']
                    channel_group_id = ch.get('channel_group_id')

                    # Get effective profile
                    # Get effective profile via configuration
                    config = automation_config.get_effective_configuration(cid, channel_group_id)
                    profile = config.get('profile') if config else None

                    # Check if stream checking is enabled in the profile
                    if profile and profile.get('stream_checking', {}).get('enabled', False):
                        filtered_channels.append(ch)

                filtered_channel_ids = [ch['id'] for ch in filtered_channels]

                excluded_count = len(channel_ids) - len(filtered_channel_ids)

                if excluded_count > 0:
                    logger.info(f"Excluding {excluded_count} channel(s) with checking disabled (channel or group level)")

                if not filtered_channel_ids:
                    logger.info("No channels with checking enabled to queue for global check")
                    return

                start_mode = self.config.get('queue.start_mode', 'first')
                start_channel_id = self.config.get('queue.start_channel_id', None)
                try:
                    ordered_channels, start_meta = order_channels_for_queue_start(
                        filtered_channels,
                        start_mode=start_mode,
                        start_channel_id=start_channel_id,
                    )
                except ValueError as exc:
                    logger.warning(
                        "Invalid saved queue start selection for global check (%s); falling back to first channel",
                        exc,
                    )
                    ordered_channels, start_meta = order_channels_for_queue_start(
                        filtered_channels,
                        start_mode='first',
                    )
                filtered_channel_ids = [ch['id'] for ch in ordered_channels]
                logger.info(
                    "Global check starts at %s (mode=%s)",
                    start_meta.get('start_channel_name', start_meta.get('start_channel_id')),
                    start_meta.get('mode', 'first'),
                )

                max_channels = self.config.get('queue.max_channels_per_run', 50)

                # Queue in batches with higher priority for global checks
                total_added = 0
                for i in range(0, len(filtered_channel_ids), max_channels):
                    batch = filtered_channel_ids[i:i+max_channels]
                    with self.lock:
                        if getattr(self, '_cancel_queueing', False):
                            logger.info("Aborting channel queueing loop due to cancel flag")
                            break
                        for channel_id in batch:
                            self.check_queue.remove_from_completed(channel_id)
                        added = self.check_queue.add_channels(
                            batch,
                            priority=5,
                            on_accepted=(
                                self.update_tracker.mark_channel_for_force_check
                                if force_check
                                else None
                            ),
                        )
                    total_added += added

                logger.info(f"Queued {total_added}/{len(filtered_channel_ids)} channels for global check (force_check={force_check})")
        except Exception as e:
            logger.error(f"Failed to queue all channels: {e}")


    def queue_channel(
        self,
        channel_id: int,
        priority: int = 10,
        force_check: bool = False,
        metadata: Optional[Dict[str, Any]] = None,
        immutable_metadata_keys: Optional[Set[str]] = None,
    ) -> bool:
        """Manually queue a channel for checking.

        Args:
            channel_id: ID of the channel to queue
            priority: Priority for queue ordering (higher = earlier)
            force_check: If True, marks channel for force checking (bypasses 2-hour immunity)
            metadata: Optional queue metadata used by specialized callers

        Returns:
            True if channel was successfully queued, False otherwise
        """
        # Look up stream count accurately to assist in ETA tracking calculations
        udi = self._checker_get_udi_manager()
        channel = udi.get_channel_by_id(channel_id)
        stream_count = len(channel.get('streams', [])) if channel else 1

        with self.lock:
            self._cancel_queueing = False
            # Ensure we can re-queue if it was completed (manual check overrides completion state)
            self.check_queue.remove_from_completed(channel_id)
            return self.check_queue.add_channel(
                channel_id,
                priority,
                stream_count=stream_count,
                metadata=metadata,
                immutable_metadata_keys=immutable_metadata_keys,
                on_accepted=(
                    (lambda: self.update_tracker.mark_channel_for_force_check(channel_id))
                    if force_check
                    else None
                ),
            )


    def queue_channels(
        self,
        channel_ids: List[int],
        priority: int = 10,
        force_check: bool = False,
        metadata: Optional[Dict[str, Any]] = None,
        immutable_metadata_keys: Optional[Set[str]] = None,
    ) -> int:
        """Manually queue multiple channels for checking.

        Args:
            channel_ids: List of channel IDs to queue
            priority: Priority for queue ordering (higher = earlier)
            force_check: If True, marks all channels for force checking (bypasses 2-hour immunity)
            metadata: Optional shared queue ownership metadata

        Returns:
            Number of channels successfully queued
        """
        with self.lock:
            self._cancel_queueing = False
            for channel_id in channel_ids:
                self.check_queue.remove_from_completed(channel_id)

            added = self.check_queue.add_channels(
                channel_ids,
                priority,
                on_accepted=(
                    self.update_tracker.mark_channel_for_force_check
                    if force_check
                    else None
                ),
                metadata=metadata,
                immutable_metadata_keys=immutable_metadata_keys,
            )
        if force_check:
            logger.info(
                "Accepted %s/%s channels for force checking (bypasses immunity)",
                added,
                len(channel_ids),
            )
        return added


    def clear_queue(
        self,
        expected_queue_snapshot: Optional[Dict[str, Any]] = None,
    ):
        """Clear the checking queue, optionally behind an exact snapshot guard."""
        with self.lock:
            sync_active = self.sync_batch_state.get('active', False)
            direct_check_active = bool(
                getattr(self, "_single_stream_check_active", False)
            )
            single_channel_check_active = bool(
                getattr(self, '_single_channel_check_active', False)
            )
            sync_execution_active = bool(
                getattr(self, '_sync_batch_execution_active', False)
            )
            automation_cycle_active = bool(
                getattr(self, '_automation_cycle_active', False)
            )
            guard_matched = None
            guard_current = None
            if expected_queue_snapshot is not None:
                expected_active_identities = {
                    (entry['channel_id'], entry['entry_token'])
                    for entry in expected_queue_snapshot['in_progress_entries']
                }
                active_queue_executions = getattr(
                    self,
                    '_active_queue_entry_executions',
                    {},
                ) or {}
                unrepresented_queue_execution = any(
                    identity not in expected_active_identities
                    or bool((state or {}).get('cancelled'))
                    for identity, state in active_queue_executions.items()
                )
                guard_blocked_by_active_owner = bool(
                    direct_check_active
                    or single_channel_check_active
                    or sync_active
                    or sync_execution_active
                    or automation_cycle_active
                    or unrepresented_queue_execution
                )
                if guard_blocked_by_active_owner:
                    queue_status = self.check_queue.get_status()
                    guard_current = {
                        'entries_complete': bool(
                            queue_status.get('entries_complete')
                        ),
                        'admission_epoch': queue_status.get('admission_epoch'),
                        'admission_revision': queue_status.get(
                            'admission_revision'
                        ),
                        'paused': queue_status.get('paused'),
                        'queued_entries': [
                            {
                                'channel_id': entry.get('channel_id'),
                                'entry_token': entry.get('entry_token'),
                                'metadata': deepcopy(entry.get('metadata') or {}),
                            }
                            for entry in queue_status.get('queued_entries', [])
                        ],
                        'in_progress_entries': [
                            {
                                'channel_id': entry.get('channel_id'),
                                'entry_token': entry.get('entry_token'),
                                'metadata': deepcopy(entry.get('metadata') or {}),
                            }
                            for entry in queue_status.get(
                                'in_progress_entries',
                                [],
                            )
                        ],
                        'completed_entries': [
                            {
                                'channel_id': entry.get('channel_id'),
                                'entry_token': entry.get('entry_token'),
                                'metadata': deepcopy(entry.get('metadata') or {}),
                            }
                            for entry in queue_status.get(
                                'completed_entries',
                                [],
                            )
                        ],
                        'failed_entries': [
                            {
                                'channel_id': entry.get('channel_id'),
                                'entry_token': entry.get('entry_token'),
                                'metadata': deepcopy(entry.get('metadata') or {}),
                            }
                            for entry in queue_status.get('failed_entries', [])
                        ],
                        'completed_channel_ids': list(
                            queue_status.get('completed_channel_ids', [])
                        ),
                        'failed_channel_ids': list(
                            queue_status.get('failed_channel_ids', [])
                        ),
                    }
                    logger.info(
                        "Checking queue guarded clear rejected because an "
                        "unrepresented operation owner is active"
                    )
                    return {
                        'guard_matched': False,
                        'guard_failure_reason': 'active_owner_not_in_snapshot',
                        'abort_requested': False,
                        'cleared': None,
                        'current': guard_current,
                        'batch_changelog_finalized': False,
                        'batch_changelog_detached': False,
                    }
                guard_result = self.check_queue.clear_if_entries_match(
                    expected_admission_epoch=expected_queue_snapshot[
                        'admission_epoch'
                    ],
                    expected_admission_revision=expected_queue_snapshot[
                        'admission_revision'
                    ],
                    expected_queued_entries=expected_queue_snapshot[
                        'queued_entries'
                    ],
                    expected_in_progress_entries=expected_queue_snapshot[
                        'in_progress_entries'
                    ],
                    expected_completed_entries=expected_queue_snapshot[
                        'completed_entries'
                    ],
                    expected_failed_entries=expected_queue_snapshot[
                        'failed_entries'
                    ],
                    expected_completed_channel_ids=expected_queue_snapshot[
                        'completed_channel_ids'
                    ],
                    expected_failed_channel_ids=expected_queue_snapshot[
                        'failed_channel_ids'
                    ],
                    expected_paused=expected_queue_snapshot['paused'],
                    reason='manual_clear',
                )
                guard_matched = bool(guard_result.get('guard_matched'))
                guard_current = guard_result.get('current')
                if not guard_matched:
                    logger.info(
                        "Checking queue clear rejected because the exact "
                        "snapshot guard no longer matches"
                    )
                    return {
                        'guard_matched': False,
                        'abort_requested': False,
                        'cleared': None,
                        'current': guard_current,
                        'batch_changelog_finalized': False,
                        'batch_changelog_detached': False,
                    }
                cleared = guard_result['cleared']

            self._external_abort_generation = (
                int(getattr(self, '_external_abort_generation', 0)) + 1
            )
            if expected_queue_snapshot is None:
                cleared = self.check_queue.clear(
                    reason='manual_clear',
                    preserve_paused=(
                        direct_check_active
                        or single_channel_check_active
                        or sync_execution_active
                        or automation_cycle_active
                    ),
                )
            self._cancel_active_queue_entry_executions_locked()
            has_active_check = bool(
                self.checking
                or direct_check_active
                or single_channel_check_active
                or sync_active
                or sync_execution_active
                or automation_cycle_active
                or getattr(self, '_active_queue_entry_executions', {})
                or cleared.get('in_progress', 0) > 0
            )

            self._cancel_queueing = True
            if has_active_check:
                # Publish the abort before waiting for batch_lock. This closes
                # the pop-before-claim window: a worker blocked on the same lock
                # will fail its require_not_aborted claim after it wakes.
                self.abort_current_check.set()
            else:
                self.abort_current_check.clear()

            batch_changelog_finalized = False
            batch_changelog_detached = False
            batch_lock = getattr(self, 'batch_lock', None)
            batch_was_started = getattr(self, 'batch_start_time', None) is not None
            if has_active_check:
                # Linearize batch invalidation against both append and finalize.
                # Calls which already carry the old generation cannot affect a
                # later batch even if they finish after the next claim.
                if batch_lock is not None:
                    with batch_lock:
                        batch_changelog_detached = bool(
                            self.batch_start_time is not None
                            or self.batch_changelog_entries
                        )
                        self.batch_start_time = None
                        self.batch_changelog_entries = []
                        self._active_batch_changelog_generation = None
            elif batch_was_started:
                # Clear is a hard batch boundary even after the final channel
                # became terminal but before the worker's idle finalizer ran.
                # Persist a completed non-empty batch (or normalize an empty
                # one), then detach it before another queue claim can start.
                if batch_lock is not None:
                    batch_changelog_finalized = bool(
                        self._finalize_batch_changelog()
                    )
                    batch_changelog_detached = True
                else:
                    self.batch_start_time = None
                    self.batch_changelog_entries = []
                    self._active_batch_changelog_generation = None
                    batch_changelog_detached = True

            tracker = getattr(self, 'update_tracker', None)
            if tracker is not None:
                tracker.clear_force_checks(cleared.get('channel_ids', []))

            if sync_active:
                self._sync_batch_generation = getattr(self, '_sync_batch_generation', 0) + 1
                self.sync_batch_state = {
                    'active': False,
                    'total_channels': 0,
                    'completed': 0,
                    'failed': 0,
                    'in_progress': 0,
                    'queued_streams_count': 0,
                    'in_progress_streams_count': 0,
                    'good_streams_count': 0,
                    'dead_streams_count': 0,
                    'blank_streams_count': 0,
                    'freeze_streams_count': 0,
                    'channels_hidden': 0,
                    'channels_ready': 0,
                    'channel_visibility_changed': 0,
                    'generation': self._sync_batch_generation,
                }
                self.checking = False
            # Keep progress cleanup inside the operation-state transaction. A
            # new direct/synchronous owner cannot publish fresh progress and
            # then have it erased by this older clear request.
            self.progress.clear()
        logger.info(
            "Checking queue cleared%s",
            " and current check abort requested" if has_active_check else ""
        )
        return {
            'guard_matched': guard_matched,
            'abort_requested': has_active_check,
            'cleared': cleared,
            'current': guard_current,
            'batch_changelog_finalized': batch_changelog_finalized,
            'batch_changelog_detached': batch_changelog_detached,
        }


    def request_abort(self, reason: str = 'external') -> None:
        """Request an external abort that connectivity cleanup must not clear."""
        with self.lock:
            self._external_abort_generation = (
                int(getattr(self, '_external_abort_generation', 0)) + 1
            )
            self._cancel_queueing = True
            self._cancel_active_queue_entry_executions_locked()
            self.abort_current_check.set()
        logger.info("Stream Checker abort requested: %s", reason)


    def trigger_check_updated_channels(self):
        """Trigger immediate check of channels with M3U updates.

        This method signals the scheduler to immediately process any channels
        that have been marked as updated, instead of waiting for the next
        scheduled check interval.
        """
        if self.running:
            logger.info("Triggering immediate check for updated channels")
            with self.lock:
                self._cancel_queueing = False
            self.check_trigger.set()
        else:
            logger.warning("Cannot trigger check - service is not running")

