"""Ownership responsibilities for the shared StreamCheckerService instance."""

import logging
import threading
from typing import Any, Callable, Dict, Optional, Tuple

logger = logging.getLogger("apps.stream.stream_checker_service")


class CheckerOwnershipMixin:
    def _capture_operation_progress_generation(
        self,
        *,
        expected_progress_generation: Optional[int] = None,
    ) -> Tuple[bool, Optional[int]]:
        """Atomically bind one active operation to its progress generation."""
        def capture_locked() -> Tuple[bool, Optional[int]]:
            abort_event = getattr(self, 'abort_current_check', None)
            abort_is_set = getattr(abort_event, 'is_set', None)
            if callable(abort_is_set) and abort_is_set():
                return False, None

            supplied_generation = (
                expected_progress_generation
                if isinstance(expected_progress_generation, int)
                and not isinstance(expected_progress_generation, bool)
                else None
            )
            progress = getattr(self, 'progress', None)
            generation_guard_capable = (
                getattr(
                    type(progress),
                    'GENERATION_GUARD_CAPABLE',
                    False,
                )
                is True
            )
            # Compatibility belongs to explicit legacy/test-double types, not
            # dynamic attributes synthesized by MagicMock instances. A real
            # generation-aware store must fail closed if its contract breaks.
            if not generation_guard_capable:
                return True, supplied_generation

            generation_getter = getattr(progress, 'get_generation', None)
            if not callable(generation_getter):
                logger.error(
                    "Aborting progress publication owner capture because the "
                    "generation-aware progress store has no callable getter"
                )
                return False, None
            try:
                raw_generation = generation_getter()
            except Exception as exc:
                logger.error(
                    "Aborting progress publication owner capture because the "
                    "generation getter failed: %s",
                    exc,
                )
                return False, None
            generation = (
                raw_generation
                if isinstance(raw_generation, int)
                and not isinstance(raw_generation, bool)
                else None
            )
            if generation is None:
                logger.error(
                    "Aborting progress publication owner capture because the "
                    "generation getter returned a non-integer value"
                )
                return False, None
            return True, (
                supplied_generation
                if supplied_generation is not None
                else generation
            )

        service_lock = getattr(self, 'lock', None)
        if service_lock is None:
            return capture_locked()
        # clear_queue uses this same service -> progress lock order. Whichever
        # transaction wins either binds the old generation first or observes the
        # abort after clear; an old owner can never adopt the new generation.
        with service_lock:
            return capture_locked()


    def _complete_channel_check(
        self,
        channel_id: int,
        on_completed=None,
        *,
        queue_entry_token: Optional[int] = None,
        allow_already_completed_side_effects: bool = False,
    ) -> bool:
        """Complete a queued channel and run side effects only if it is still active."""
        lock = getattr(self, 'lock', None)

        def complete_locked() -> Tuple[bool, bool]:
            cancelled = self.abort_current_check.is_set()
            exact_execution_active = False
            if queue_entry_token is not None:
                executions = getattr(
                    self,
                    '_active_queue_entry_executions',
                    None,
                )
                if executions is not None:
                    execution = executions.get((channel_id, queue_entry_token))
                    exact_execution_active = bool(
                        execution is not None
                        and not execution.get('cancelled')
                    )
                    cancelled = bool(
                        cancelled
                        or not exact_execution_active
                    )
                if cancelled:
                    self.check_queue.mark_failed(
                        channel_id,
                        'aborted',
                        entry_token=queue_entry_token,
                    )
                    return False, False

            accepted = self.check_queue.mark_completed(
                channel_id,
                entry_token=queue_entry_token,
            )
            already_completed = bool(
                queue_entry_token is not None
                and allow_already_completed_side_effects
                and not accepted
                and exact_execution_active
            )
            run_side_effects = bool(
                accepted
                or already_completed
                or (
                    queue_entry_token is None
                    and not cancelled
                )
            )
            if run_side_effects and on_completed:
                on_completed()
            return accepted, run_side_effects

        if lock is None:
            accepted, run_side_effects = complete_locked()
        else:
            # request_abort()/clear_queue() use the same service -> queue lock
            # order. Whichever transaction wins here owns the terminal result.
            with lock:
                accepted, run_side_effects = complete_locked()

        if run_side_effects:
            return accepted

        logger.info(
            f"Skipping completion side effects for channel {channel_id}; "
            "the queue entry was already cleared or aborted"
        )
        return False


    def _fail_channel_check(
        self,
        channel_id: int,
        error: str,
        on_failed=None,
        *,
        queue_entry_token: Optional[int] = None,
        allow_already_failed_side_effects: bool = False,
    ) -> bool:
        """Fail an exact queue execution without publishing stale callbacks."""
        lock = getattr(self, 'lock', None)

        def fail_locked() -> bool:
            cancelled = self.abort_current_check.is_set()
            exact_execution_active = False
            if queue_entry_token is not None:
                executions = getattr(
                    self,
                    '_active_queue_entry_executions',
                    None,
                )
                if executions is not None:
                    execution = executions.get((channel_id, queue_entry_token))
                    exact_execution_active = bool(
                        execution is not None
                        and not execution.get('cancelled')
                    )
                    cancelled = bool(cancelled or not exact_execution_active)
                if cancelled:
                    self.check_queue.mark_failed(
                        channel_id,
                        'aborted',
                        entry_token=queue_entry_token,
                    )
                    return False

            accepted = self.check_queue.mark_failed(
                channel_id,
                error,
                entry_token=queue_entry_token,
            )
            terminal_authorized = bool(
                accepted
                or (
                    queue_entry_token is not None
                    and allow_already_failed_side_effects
                    and exact_execution_active
                )
            )
            if terminal_authorized and on_failed:
                on_failed()
            return terminal_authorized

        if lock is None:
            return fail_locked()
        with lock:
            return fail_locked()


    def _abort_channel_check(
        self,
        channel_id: int,
        channel_name: Optional[str] = None,
        *,
        queue_entry_token: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Finish an aborted channel check without writing completion state."""
        if channel_name:
            logger.info(f"Channel check aborted for {channel_name} (channel {channel_id})")
        else:
            logger.info(f"Channel check aborted for channel {channel_id}")

        self.check_queue.mark_failed(
            channel_id,
            'aborted',
            entry_token=queue_entry_token,
        )
        if queue_entry_token is not None:
            self._clear_queue_entry_progress(
                channel_id,
                queue_entry_token,
            )
        else:
            self.progress.clear()
        return {
            'success': False,
            'error': 'aborted',
            'dead_streams_count': 0,
            'revived_streams_count': 0,
            'checked_streams': [],
            'skipped': True,
            'skip_reason': 'aborted',
            'aborted': True
        }


    def _abort_channel_check_if_requested(
        self,
        channel_id: int,
        channel_name: Optional[str] = None,
        *,
        queue_entry_token: Optional[int] = None,
    ) -> Optional[Dict[str, Any]]:
        """Return an aborted result when a clear/abort request is pending."""
        queue_execution_cancelled = False
        if queue_entry_token is not None:
            lock = getattr(self, 'lock', None)
            if lock is None:
                executions = getattr(
                    self,
                    '_active_queue_entry_executions',
                    None,
                )
                if executions is None:
                    queue_execution_cancelled = not self.check_queue.owns_in_progress(
                        channel_id,
                        queue_entry_token,
                    )
                else:
                    execution = executions.get((channel_id, queue_entry_token))
                    queue_execution_cancelled = bool(
                        execution is None or execution.get('cancelled')
                    )
            else:
                with lock:
                    executions = getattr(
                        self,
                        '_active_queue_entry_executions',
                        None,
                    )
                    if executions is None:
                        queue_execution_cancelled = not self.check_queue.owns_in_progress(
                            channel_id,
                            queue_entry_token,
                        )
                    else:
                        execution = executions.get((channel_id, queue_entry_token))
                        queue_execution_cancelled = bool(
                            execution is None or execution.get('cancelled')
                        )
        if queue_execution_cancelled or self.abort_current_check.is_set():
            return self._abort_channel_check(
                channel_id,
                channel_name,
                queue_entry_token=queue_entry_token,
            )
        return None


    def _run_channel_side_effect_if_authorized(
        self,
        channel_id: int,
        queue_entry_token: Optional[int],
        action: Callable[[], Any],
    ) -> Tuple[bool, Any]:
        """Linearize an irreversible channel write against clear/request_abort."""
        def authorized_locked() -> bool:
            if self.abort_current_check.is_set():
                return False
            if queue_entry_token is None:
                return True

            executions = getattr(
                self,
                '_active_queue_entry_executions',
                None,
            )
            if executions is not None:
                execution = executions.get((channel_id, queue_entry_token))
                return bool(
                    execution is not None
                    and not execution.get('cancelled')
                )

            owns_in_progress = getattr(
                self.check_queue,
                'owns_in_progress',
                None,
            )
            return bool(
                callable(owns_in_progress)
                and owns_in_progress(channel_id, queue_entry_token)
            )

        lock = getattr(self, 'lock', None)
        if lock is None:
            if not authorized_locked():
                return False, None
            return True, action()
        with lock:
            if not authorized_locked():
                return False, None
            # Clear/request_abort use this same service lock. If this commit
            # wins, they cannot return until its irreversible write finishes;
            # if they win, authorization above fails before the write starts.
            return True, action()


    def begin_automation_cycle_operation(self) -> bool:
        """Atomically reserve Stream Checker ownership for a full automation cycle."""
        owner_thread_id = threading.get_ident()
        with self.lock:
            with self.check_queue.lock:
                if getattr(self, '_automation_cycle_active', False):
                    return False
                sync_active = bool((self.sync_batch_state or {}).get('active'))
                queue_active = bool(
                    self.check_queue.queued
                    or self.check_queue.in_progress
                    or self.check_queue.stats.get('current_channel') is not None
                    or getattr(self, '_active_queue_entry_executions', {})
                )
                if (
                    self.checking
                    or getattr(self, '_single_stream_check_active', False)
                    or getattr(self, '_single_channel_check_active', False)
                    or sync_active
                    or getattr(self, '_sync_batch_execution_active', False)
                    or queue_active
                ):
                    return False

                self._automation_cycle_previous_queue_paused = bool(
                    self.check_queue.paused
                )
                self.check_queue.paused = True
                self.abort_current_check.clear()
                self._automation_cycle_active = True
                self._automation_cycle_owner_thread_id = owner_thread_id
                self._automation_cycle_abort_generation = int(
                    getattr(self, '_external_abort_generation', 0)
                )
                progress = getattr(self, 'progress', None)
                if progress is not None:
                    progress.clear()
                return True


    def end_automation_cycle_operation(self) -> bool:
        """Release a full-automation reservation owned by the calling thread."""
        owner_thread_id = threading.get_ident()
        with self.lock:
            with self.check_queue.lock:
                if not getattr(self, '_automation_cycle_active', False):
                    return False
                if getattr(self, '_automation_cycle_owner_thread_id', None) != owner_thread_id:
                    logger.warning(
                        "Ignoring automation checker release from a non-owner thread"
                    )
                    return False
                self.check_queue.paused = bool(
                    self._automation_cycle_previous_queue_paused
                )
                self._automation_cycle_previous_queue_paused = False
                self._automation_cycle_active = False
                self._automation_cycle_owner_thread_id = None
                self._automation_cycle_abort_generation = None
                return True


    def _begin_single_channel_check_operation(self) -> bool:
        """Atomically reserve an immediate channel check outside owned queue work."""
        with self.lock:
            with self.check_queue.lock:
                sync_active = bool((self.sync_batch_state or {}).get('active'))
                sync_execution_active = bool(
                    getattr(self, '_sync_batch_execution_active', False)
                )
                queue_active = bool(
                    self.check_queue.queued
                    or self.check_queue.in_progress
                    or self.check_queue.stats.get('current_channel') is not None
                    or getattr(self, '_active_queue_entry_executions', {})
                )
                if (
                    self.checking
                    or getattr(self, '_single_stream_check_active', False)
                    or getattr(self, '_single_channel_check_active', False)
                    or getattr(self, '_automation_cycle_active', False)
                    or sync_active
                    or sync_execution_active
                    or queue_active
                ):
                    return False

                self.abort_current_check.clear()
                self._single_channel_previous_queue_paused = bool(
                    self.check_queue.paused
                )
                self.check_queue.paused = True
                self._single_channel_check_active = True
                self.checking = True
                return True


    def _end_single_channel_check_operation(self) -> None:
        """Release an immediate channel reservation and restore queue consumption."""
        with self.lock:
            with self.check_queue.lock:
                self.check_queue.paused = bool(
                    self._single_channel_previous_queue_paused
                )
                self._single_channel_previous_queue_paused = False
                self._single_channel_check_active = False
                self.checking = False


    def _begin_single_stream_check_operation(self) -> bool:
        """Atomically reserve the checker and pause background queue starts."""
        with self.lock:
            queue_lock = self.check_queue.lock
            with queue_lock:
                sync_active = bool((self.sync_batch_state or {}).get('active'))
                sync_execution_active = bool(
                    getattr(self, '_sync_batch_execution_active', False)
                )
                queue_active = bool(
                    self.check_queue.queued
                    or self.check_queue.in_progress
                    or self.check_queue.stats.get('current_channel') is not None
                    or getattr(self, '_active_queue_entry_executions', {})
                )
                if (
                    self.checking
                    or self._single_stream_check_active
                    or getattr(self, '_single_channel_check_active', False)
                    or getattr(self, '_automation_cycle_active', False)
                    or sync_active
                    or sync_execution_active
                    or queue_active
                ):
                    return False

                self.abort_current_check.clear()
                self._single_stream_previous_queue_paused = bool(self.check_queue.paused)
                self.check_queue.paused = True
                self._single_stream_check_active = True
                self.checking = True
                return True


    def _end_single_stream_check_operation(self) -> None:
        """Release a direct-check reservation and restore queue consumption."""
        with self.lock:
            with self.check_queue.lock:
                self.check_queue.paused = bool(
                    self._single_stream_previous_queue_paused
                )
                self._single_stream_previous_queue_paused = False
                self._single_stream_check_active = False
                self.checking = False

