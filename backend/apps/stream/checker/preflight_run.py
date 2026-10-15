"""Execution-only checker context: no service threads or independent limiters."""

import threading
import logging

from apps.stream.stream_checker_service import StreamCheckerService
from apps.stream.stream_checker_components import StreamCheckerProgress


class PreflightCheckRun(StreamCheckerService):
    def __init__(self, owner, execution_key):
        # Share only explicit infrastructure. Never shallow-copy the singleton:
        # new mutable service state must not silently become shared run state.
        for name in (
            "config", "hardware_acceleration_diagnostics", "update_tracker",
            "check_queue", "dead_streams_tracker", "connectivity_guard",
            "channel_visibility_automation", "changelog", "lock",
            "_active_queue_entry_executions", "_preflight_probe_slots",
            "_preflight_inventory_lock",
        ):
            setattr(self, name, getattr(owner, name))
        self._execution_key = execution_key
        self.progress = StreamCheckerProgress(persist=False)
        self.abort_current_check = threading.Event()
        self.connectivity_guard_status = {}
        self._last_connectivity_guard_recovery_probe_at = 0.0
        self.checking = False
        self._cancel_queueing = False
        self._external_abort_generation = 0
        self._sync_batch_execution_active = False
        self._sync_batch_execution_generation = None
        self.sync_batch_state = {}
        self.batch_lock = threading.Lock()
        self.batch_changelog_entries = []
        self.batch_start_time = None
        self._batch_changelog_generation = 0
        self._active_batch_changelog_generation = None

    def _cancel_active_queue_entry_executions_locked(self):
        # A local connectivity failure must not invalidate sibling executions.
        execution = self._active_queue_entry_executions.get(self._execution_key)
        if execution is not None:
            execution["cancelled"] = True

    def start(self):
        raise RuntimeError("A preflight run cannot start service threads")

    def _initialize_provider_probe_account_inventory(
        self, *, udi, limiter, initialize_account_limits, operation_label,
    ):
        # Refreshes by sibling runs must not briefly revoke a healthy active
        # reservation. The limiter publishes the complete inventory atomically
        # and revalidates current UDI authority at every reservation/preemption.
        # Failed reads still revoke authority, rather than reuse stale limits.
        with self._preflight_inventory_lock:
            try:
                accounts = udi.get_m3u_accounts()
                if not isinstance(accounts, list) or any(not isinstance(item, dict) for item in accounts):
                    raise ValueError("Invalid provider inventory")
                limiter.udi_manager = udi
                if initialize_account_limits(accounts) is False:
                    raise ValueError("Provider inventory rejected")
                return True
            except Exception:
                limiter.invalidate_account_inventory()
                logging.getLogger(__name__).exception("%s could not refresh provider inventory", operation_label)
                return False
