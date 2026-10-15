"""Bounded Teamarr queue execution using independent per-channel run state."""

import logging
import threading

logger = logging.getLogger("apps.stream.stream_checker_service")


class CheckerParallelPreflightMixin:
    def can_queue_parallel_preflight(self) -> bool:
        """Distinguish sibling preflights from the unrelated work defer setting."""
        with self.lock:
            runs = self._parallel_preflight_runs
            if (
                self.checking or self._single_stream_check_active
                or self._single_channel_check_active or self._automation_cycle_active
                or self._sync_batch_execution_active or self.sync_batch_state.get("active")
                or set(self._active_queue_entry_executions) - set(runs)
            ):
                return False
            status = self.check_queue.get_status()
            entries = status.get("queued_entries", []) + status.get("in_progress_entries", [])
            return bool(status.get("entries_complete")) and all(
                (entry.get("metadata") or {}).get("source") == "teamarr_preflight"
                for entry in entries
            )

    def _dispatch_parallel_preflight_checks(self, queue_entry=None) -> bool:
        """Start at most one queued event; return whether the worker stays in this lane.

        Ordinary queue work and direct/automation owners remain exclusive. All
        preflight runs share the real queue and its execution-token fences.
        """
        if not hasattr(self, '_parallel_preflight_runs'):
            return False
        from apps.stream.teamarr_preflight_service import get_teamarr_preflight_service
        from apps.stream.checker.preflight_run import PreflightCheckRun

        try:
            limit = max(1, min(10, int(
                get_teamarr_preflight_service().get_config().get("max_concurrent_checks", 1)
            )))
        except Exception:
            limit = 1

        with self.lock:
            self._parallel_preflight_enabled = limit > 1
            runs = self._parallel_preflight_runs
            if (
                self._single_stream_check_active
                or self._single_channel_check_active
                or self._automation_cycle_active
                or self._sync_batch_execution_active
                or self.sync_batch_state.get("active")
                or self._specialized_queue_gate_active_locked()
                or self.check_queue.get_status().get("paused")
            ):
                return bool(runs)
            if runs and (len(runs) >= limit or self.abort_current_check.is_set()):
                return True
            if not runs:
                if limit == 1 or self._active_queue_entry_executions or self.checking:
                    return False
                if self.abort_current_check.is_set():
                    return False
            entry = queue_entry
            if entry is None:
                entry = self.check_queue.get_next_entry_for_metadata_sources(
                    {"teamarr_preflight"}, respect_pause=True,
                    excluded_channel_ids={key[0] for key in runs},
                )
            elif not self.check_queue.owns_in_progress(entry['channel_id'], entry['queue_entry_token']):
                return True  # Clear/requeue invalidated this blocking dequeue.
            if entry is None:
                return bool(runs)
            key = (entry["channel_id"], entry["queue_entry_token"])
            if not runs:
                probe_limit = (
                    self.config.get("concurrent_streams.global_limit", 10)
                    if self.config.get("concurrent_streams.enabled", True) else 1
                )
                self._preflight_probe_slots = threading.BoundedSemaphore(max(1, int(probe_limit)))
            run = PreflightCheckRun(self, key)
            self._active_queue_entry_executions[key] = {"cancelled": False}
            runs[key] = run
            pending, generation = self._queue_force_check_state(entry["channel_id"])

        def execute():
            try:
                run._run_specialized_queue_entry_owned(
                    entry, force_check_generation=generation if pending else None,
                )
            except Exception:
                logger.exception("Parallel Teamarr check failed for channel %s", key[0])
                run._fail_channel_check(key[0], "preflight_execution_failed", queue_entry_token=key[1])
            finally:
                try:
                    if pending:
                        self.update_tracker.clear_force_check(key[0], expected_generation=generation)
                except Exception:
                    logger.exception("Could not clear force flag for channel %s", key[0])
                finally:
                    # Retain ownership through reporting and force-flag cleanup.
                    with self.lock:
                        self._active_queue_entry_executions.pop(key, None)
                        runs.pop(key, None)
                        if not runs:
                            self._preflight_probe_slots = None
                        run.progress.clear()

        thread = threading.Thread(target=execute, daemon=True, name=f"TeamarrCheck-{key[0]}")
        try:
            thread.start()
        except Exception:
            with self.lock:
                self._active_queue_entry_executions.pop(key, None)
                runs.pop(key, None)
                if not runs:
                    self._preflight_probe_slots = None
                self.check_queue.mark_failed(key[0], "preflight_start_failed", entry_token=key[1])
            raise
        return True

    def _parallel_preflight_progress(self):
        with self.lock:
            return [
                {"channel_id": key[0], "queue_entry_token": key[1], "progress": run.progress.get()}
                for key, run in getattr(self, '_parallel_preflight_runs', {}).items()
            ]
