#!/usr/bin/env python3
"""
Stream Checker Service for Dispatcharr.

This service manages stream quality checking, rating, and ordering for
Dispatcharr channels. It implements a comprehensive system for maintaining
optimal stream quality across all channels.

Features:
    - Queue-based channel checking with priority support
    - Tracking of M3U playlist update events
    - Scheduled global checks during configurable off-peak hours
    - Progressive stream rating and automatic ordering
    - Real-time progress reporting via web API
    - Thread-safe operations with proper synchronization

The service runs continuously in the background, monitoring for channel
updates and maintaining a queue of channels that need checking. It
integrates with the stream_check_utils.py module for stream analysis.
"""

import json
import logging
import math
import os
import threading
import time
from copy import deepcopy
from collections import defaultdict, deque, Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple
import queue

from apps.core.api_utils import (
    fetch_channel_streams,
    update_channel_streams,
    _get_base_url,
    _get_auth_headers,
    patch_request,
    batch_update_stream_stats,
    _stream_stats_update_lock,
)
from apps.core.log_sanitizer import audit_ref as _audit_ref, scrub_urls, stream_context, stream_ref

# Import UDI for direct data access
from apps.udi import get_udi_manager

# Import dead streams tracker
from apps.stream.dead_streams_tracker import DeadStreamsTracker
from apps.stream.stream_check_utils import analyze_stream, _stream_analysis_timeout
from apps.stream.queue_start import order_channels_for_queue_start
from apps.stream.connectivity_guard import ConnectivityCheckResult, StreamConnectivityGuard
from apps.stream.stream_session_manager import get_session_manager
from apps.stream.stale_status_snapshot import (
    build_dispatcharr_stale_snapshot,
    build_stale_warnings,
    external_m3u_account_risk,
    external_message_class,
)
from apps.automation.automation_config_manager import get_automation_config_manager
from apps.automation.channel_visibility_automation import (
    ChannelVisibilityAutomation,
    resolve_channel_visibility_config,
)
from apps.core.auth import _refresh_token


VISUAL_PROBE_REPORT_FIELDS = (
    "visual_probe_ran",
    "visual_probe_completed",
    "visual_probe_incomplete",
    "visual_probe_incomplete_reason",
    "visual_probe_requested_duration_seconds",
    "visual_probe_minimum_duration_seconds",
    "visual_probe_duration_seconds",
    "visual_probe_duration_adjusted",
    "visual_probe_duration_adjustment_reason",
    "visual_probe_elapsed_time",
)

_STREAM_STATUS_HEARTBEAT_INTERVAL_SECONDS = 2.0

BITRATE_RECHECK_REPORT_FIELDS = (
    "measurement_incomplete",
    "measurement_incomplete_reason",
    "measurement_incomplete_context",
    "bitrate_recheck_required",
    "bitrate_recheck_attempted",
    "bitrate_recheck_outcome",
)

# Import channel settings manager
# Import channel settings manager - DEPRECATED/REMOVED
# from channel_settings_manager import get_channel_settings_manager

# Import profile config
# Import profile config - DEPRECATED/REMOVED
# from profile_config import get_profile_config

# Import centralized stream stats utilities
from apps.core.stream_stats_utils import (
    parse_bitrate_value,
    format_bitrate,
    parse_fps_value,
    format_fps,
    extract_stream_stats,
    format_stream_stats_for_display,
    calculate_channel_averages,
    is_stream_dead as utils_is_stream_dead
)

# Import changelog manager
try:
    from apps.automation.automated_stream_manager import ChangelogManager
    CHANGELOG_AVAILABLE = True
except ImportError:
    CHANGELOG_AVAILABLE = False

SPECIALIZED_QUEUE_SOURCES = {"teamarr_preflight", "auto_create"}

# Import croniter for cron expression validation
try:
    from croniter import croniter
    CRONITER_AVAILABLE = True
except ImportError:
    CRONITER_AVAILABLE = False

# Setup centralized logging
from apps.core.logging_config import setup_logging, log_function_call, log_function_return, log_exception, log_state_change

logger = setup_logging(__name__)

# Configuration directory
CONFIG_DIR = Path(os.environ.get('CONFIG_DIR', '/app/data'))


from apps.stream.stream_checker_components import (
    StreamCheckConfig,
    ChannelUpdateTracker,
    StreamCheckQueue,
    StreamCheckerProgress,
)

def _wait_for_udi_stream_count_stabilise(
    udi,
    pre_count: int,
    timeout: int = 60,
    poll_interval: int = 5,
    abort_event: Optional[threading.Event] = None,
) -> bool:
    """Poll UDI stream count after triggering a Dispatcharr playlist refresh.

    Dispatcharr processes M3U playlists asynchronously. The refresh API call
    returns as soon as the job is *enqueued*, not when it completes. Immediately
    syncing the UDI cache after the call often returns pre-refresh data.

    This helper polls the UDI stream count until it changes from pre_count
    (indicating Dispatcharr has finished processing) or until timeout elapses.
    It reuses the same poll-and-confirm pattern used in the startup sequence.

    Args:
        udi: Initialised UDI manager instance.
        pre_count: Stream count captured before refresh_m3u_playlists() was called.
        timeout: Maximum seconds to wait before giving up (default 60).
        poll_interval: Seconds between each poll attempt (default 5).

    Returns:
        True  — stream count changed; refresh appears to have taken effect.
        False — timed out with no change; downstream steps proceed on
                potentially stale data (logged as a warning).
    """
    elapsed = 0
    while elapsed < timeout:
        if abort_event is not None:
            if abort_event.wait(poll_interval):
                logger.info("Post-refresh UDI wait aborted")
                return False
        else:
            time.sleep(poll_interval)
        elapsed += poll_interval
        try:
            current_count = udi.get_stream_count()
            if current_count != pre_count:
                logger.info(
                    f"UDI stream count changed after playlist refresh: "
                    f"{pre_count} → {current_count} ({elapsed}s elapsed)"
                )
                return True
        except Exception as _e:
            logger.warning(
                f"Error polling UDI stream count during post-refresh wait: {_e}"
            )
    logger.warning(
        f"UDI stream count unchanged after {timeout}s (still {pre_count} streams). "
        "Proceeding with potentially stale data. "
        "Consider setting post_refresh_delay_seconds in config if this recurs."
    )
    return False


def _coerce_m3u_account_id(value: Any) -> Optional[int]:
    try:
        if value is None or isinstance(value, bool):
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _dedupe_m3u_account_ids(values: List[Any]) -> List[int]:
    account_ids: List[int] = []
    seen: Set[int] = set()
    for value in values:
        account_id = _coerce_m3u_account_id(value)
        if account_id is None or account_id in seen:
            continue
        seen.add(account_id)
        account_ids.append(account_id)
    return account_ids


def _is_active_non_custom_m3u_account(account: Dict[str, Any]) -> bool:
    name = str(account.get("name") or "").strip().lower()
    raw_id = str(account.get("id") or "").strip().lower()
    if name == "custom" or raw_id == "custom":
        return False

    for key in ("is_active", "active", "enabled"):
        if key in account:
            return bool(account.get(key))
    return True


def _sort_m3u_account_ids(values: Set[int]) -> List[int]:
    return sorted(values, key=lambda value: (str(type(value)), value))


def _resolve_single_channel_m3u_refresh_scope(
    *,
    profile: Dict[str, Any],
    channel_account_ids: Set[int],
    udi: Any,
) -> Tuple[List[int], str]:
    """Resolve the provider-fetch scope for a single-channel check.

    V6 semantics:
      - explicit profile m3u_update.playlists: refresh exactly those accounts
      - empty profile playlist list: refresh every active, non-custom account

    If account discovery is unavailable, fall back to the current channel
    accounts to preserve the pre-V6 behavior instead of silently doing nothing.
    """
    m3u_update = profile.get("m3u_update") if isinstance(profile, dict) else {}
    if not isinstance(m3u_update, dict):
        m3u_update = {}

    explicit_playlist_ids = _dedupe_m3u_account_ids(m3u_update.get("playlists") or [])
    if explicit_playlist_ids:
        return explicit_playlist_ids, "profile_playlists"

    all_accounts = []
    try:
        get_accounts = getattr(udi, "get_m3u_accounts", None)
        if callable(get_accounts):
            fetched_accounts = get_accounts()
            if isinstance(fetched_accounts, list):
                all_accounts = fetched_accounts
    except Exception as exc:
        logger.warning("Could not resolve active M3U accounts for single-channel refresh: %s", exc)

    active_account_ids = _dedupe_m3u_account_ids(
        [
            account.get("id")
            for account in all_accounts
            if isinstance(account, dict) and _is_active_non_custom_m3u_account(account)
        ]
    )
    if active_account_ids:
        return active_account_ids, "all_active_non_custom"

    fallback_ids = _sort_m3u_account_ids(channel_account_ids)
    if fallback_ids:
        logger.warning(
            "Falling back to channel-attached M3U accounts for single-channel refresh "
            "because active account discovery returned no usable accounts"
        )
        return fallback_ids, "channel_accounts_fallback"

    return [], "none"


class StreamCheckerService:
    """Main service for managing stream checking operations."""
    SINGLE_CHANNEL_RUN_SNAPSHOT_MAX_BYTES = 50 * 1024
    
    def __init__(self):
        log_function_call(logger, "__init__")
        logger.debug("Initializing StreamCheckerService components...")
        
        self.config = StreamCheckConfig()
        logger.debug("Config loaded")
        self.hardware_acceleration_diagnostics = {}
        self._refresh_hardware_acceleration_diagnostics(log_startup=True)
        
        self.update_tracker = ChannelUpdateTracker()
        logger.debug("Update tracker initialized")
        
        self.check_queue = StreamCheckQueue(
            max_size=self.config.get('queue.max_size', 1000)
        )
        logger.debug(f"Check queue initialized with max_size={self.config.get('queue.max_size', 1000)}")
        
        self.progress = StreamCheckerProgress()
        logger.debug("Progress tracker initialized")
        
        self.dead_streams_tracker = DeadStreamsTracker()
        logger.debug("Dead streams tracker initialized")

        self.connectivity_guard = StreamConnectivityGuard()
        self.connectivity_guard_status = {
            'ok': True,
            'reason': 'not_checked',
            'message': 'Connectivity guard has not run yet',
            'details': {},
        }
        self._last_connectivity_guard_recovery_probe_at = 0.0
        logger.debug("Connectivity guard initialized")

        self.channel_visibility_automation = ChannelVisibilityAutomation()
        logger.debug("Channel visibility automation initialized")
        
        # Initialize changelog manager
        self.changelog = None
        if CHANGELOG_AVAILABLE:
            try:
                self.changelog = ChangelogManager(changelog_file=CONFIG_DIR / "stream_checker_changelog.json")
                logger.info("Stream checker changelog manager initialized")
            except Exception as e:
                log_exception(logger, e, "changelog initialization")
                logger.warning(f"Failed to initialize changelog manager: {e}")
        
        # Batch changelog tracking
        self.batch_changelog_entries = []
        self.batch_start_time = None
        self.batch_lock = threading.Lock()
        self._batch_changelog_generation = 0
        self._active_batch_changelog_generation = None
        
        self.running = False
        self.checking = False
        self.start_time = datetime.now()
        self.worker_thread = None
        self.scheduler_thread = None
        self.lock = threading.Lock()
        self._single_stream_check_active = False
        self._single_stream_previous_queue_paused = False
        self._single_channel_check_active = False
        self._single_channel_previous_queue_paused = False
        self._automation_cycle_active = False
        self._automation_cycle_owner_thread_id = None
        self._automation_cycle_previous_queue_paused = False
        self._automation_cycle_abort_generation = None
        self._external_abort_generation = 0
        self._sync_batch_execution_active = False
        self._sync_batch_execution_generation = None
        # Queue.clear() deliberately removes public in-progress state
        # immediately, but the popped worker call may still be unwinding. Keep
        # a separate execution reservation until that exact call returns so a
        # new direct/synchronous owner cannot clear its abort or overlap writes.
        self._active_queue_entry_executions = {}
        self._cancel_queueing = False
        self._sync_batch_generation = 0
        self._specialized_queue_gates = set()
        
        self.sync_batch_state = {
            'active': False,
            'total_channels': 0,
            'completed': 0,
            'failed': 0,
            'in_progress': 0,
            'good_streams_count': 0,
            'dead_streams_count': 0,
            'blank_streams_count': 0,
            'freeze_streams_count': 0,
            'channels_hidden': 0,
            'channels_ready': 0,
            'channel_visibility_changed': 0,
        }
        
        # Event for immediate triggering of updated channels check
        self.check_trigger = threading.Event()
        logger.debug("Check trigger event created")
        
        # Event for immediate config change notification
        self.config_changed = threading.Event()
        logger.debug("Config changed event created")
        
        # Event for aborting current channel check
        self.abort_current_check = threading.Event()
        logger.debug("Abort current check event created")
        
        logger.info("Stream Checker Service initialized")
        log_function_return(logger, "__init__")
    
    def start(self):
        """Start the stream checker service."""
        log_function_call(logger, "start")
        with self.lock:
            if self.running:
                logger.warning("Stream checker service is already running")
                return
            
            log_state_change(logger, "stream_checker_service", "stopped", "starting")
            self.running = True
            
            # Start worker thread for processing queue
            self.worker_thread = threading.Thread(target=self._worker_loop, daemon=True)
            self.worker_thread.start()
            logger.debug(f"Worker thread started (id: {self.worker_thread.ident})")
            
            # Start scheduler thread for periodic checks
            self.scheduler_thread = threading.Thread(target=self._scheduler_loop, daemon=True)
            self.scheduler_thread.start()
            logger.debug(f"Scheduler thread started (id: {self.scheduler_thread.ident})")
            
            log_state_change(logger, "stream_checker_service", "starting", "running")
            logger.info("Stream checker service started")
            log_function_return(logger, "start")
    
    def stop(self):
        """Stop the stream checker service."""
        with self.lock:
            if not self.running:
                logger.warning("Stream checker service is not running")
                return
            
            self.running = False
            logger.info("Stream checker service stopping...")
        
        # Wait for threads to finish
        if self.worker_thread and self.worker_thread.is_alive():
            self.worker_thread.join(timeout=5)
        if self.scheduler_thread and self.scheduler_thread.is_alive():
            self.scheduler_thread.join(timeout=5)
        
        self.progress.clear()
        logger.info("Stream checker service stopped")
    
    def _worker_loop(self):
        """Main worker loop for processing the check queue."""
        log_function_call(logger, "_worker_loop")
        logger.info("Stream checker worker started")
        batch_changelog_generation = None
        
        while self.running:
            owned_queue_execution = None
            try:
                # Clear stale aborts before waiting for the next queue item. Do
                # not clear this after a channel is pulled: a manual queue clear
                # can legitimately request abort in that narrow handoff window.
                # The active-operation check and clear share the service lock so
                # a direct check cannot reserve the checker between them. Its
                # abort belongs to that direct caller until the reservation ends.
                service_lock = getattr(self, 'lock', None)
                if service_lock is None:
                    direct_check_active = bool(
                        getattr(self, '_single_stream_check_active', False)
                    )
                    single_channel_check_active = bool(
                        getattr(self, '_single_channel_check_active', False)
                    )
                    sync_batch_execution_active = bool(
                        getattr(self, '_sync_batch_execution_active', False)
                    )
                    automation_cycle_active = bool(
                        getattr(self, '_automation_cycle_active', False)
                    )
                    sync_batch_active = self._sync_batch_active()
                    if (
                        not direct_check_active
                        and not single_channel_check_active
                        and not sync_batch_active
                        and not sync_batch_execution_active
                        and not automation_cycle_active
                        and not getattr(self, '_active_queue_entry_executions', {})
                    ):
                        self.abort_current_check.clear()
                else:
                    with service_lock:
                        direct_check_active = bool(
                            getattr(self, '_single_stream_check_active', False)
                        )
                        single_channel_check_active = bool(
                            getattr(self, '_single_channel_check_active', False)
                        )
                        sync_batch_execution_active = bool(
                            getattr(self, '_sync_batch_execution_active', False)
                        )
                        automation_cycle_active = bool(
                            getattr(self, '_automation_cycle_active', False)
                        )
                        sync_batch_state = getattr(self, 'sync_batch_state', {}) or {}
                        sync_batch_active = bool(
                            sync_batch_state.get('active')
                            and sync_batch_state.get('generation')
                            == getattr(self, '_sync_batch_generation', None)
                        )
                        if (
                            not direct_check_active
                            and not single_channel_check_active
                            and not sync_batch_active
                            and not sync_batch_execution_active
                            and not automation_cycle_active
                            and not getattr(self, '_active_queue_entry_executions', {})
                        ):
                            self.abort_current_check.clear()

                if direct_check_active:
                    logger.debug("Worker paused while direct stream check is active")
                    time.sleep(1)
                    continue
                if automation_cycle_active:
                    logger.debug("Worker paused while full automation cycle owns the checker")
                    time.sleep(1)
                    continue
                if single_channel_check_active:
                    logger.debug("Worker paused while immediate single-channel check is active")
                    time.sleep(1)
                    continue
                if sync_batch_active or sync_batch_execution_active:
                    logger.debug("Worker paused while synchronous quality batch is active")
                    time.sleep(1)
                    continue
                self._apply_specialized_queue_deferral()
                logger.debug("Worker waiting for next channel from queue...")
                queue_entry = self.check_queue.get_next_entry(timeout=1.0)
                if queue_entry is None:
                    # No channel in queue - check if we should finalize a batch
                    if batch_changelog_generation is not None:
                        # Queue is empty and we have an active batch - finalize it
                        self._finalize_batch_changelog(
                            batch_generation=batch_changelog_generation,
                        )
                        batch_changelog_generation = None
                    logger.debug("No channel in queue (timeout)")
                    continue
                channel_id = queue_entry.get('channel_id')
                queue_entry_token = queue_entry.get('queue_entry_token')
                queue_metadata = queue_entry.get('metadata') or {}
                
                single_check_metadata = self._is_specialized_queue_metadata(queue_metadata)

                if not single_check_metadata:
                    if not self._claim_queue_entry_execution(
                        channel_id,
                        queue_entry_token,
                    ):
                        logger.info(
                            "Skipping queue entry %s because its activation was cleared "
                            "before the worker execution claim",
                            channel_id,
                        )
                        continue
                    owned_queue_execution = (channel_id, queue_entry_token)

                # Start a new batch if not already started. Specialized single-channel
                # queue entries keep their own changelog path and should not create an
                # otherwise empty batch.
                if not single_check_metadata:
                    if self.abort_current_check.is_set():
                        if not self.check_queue.owns_in_progress(
                            channel_id,
                            queue_entry_token,
                        ):
                            logger.info(
                                "Skipping cleared queue entry %s before batch claim",
                                channel_id,
                            )
                            continue
                        # request_abort() deliberately leaves queue ownership in
                        # place. Let _check_channel observe the abort so it can
                        # mark that logical entry terminal instead of stranding
                        # it in_progress. Do not open a changelog batch solely
                        # for this already-aborted channel.
                    else:
                        claimed_generation = self._start_batch_changelog(
                            require_not_aborted=True,
                        )
                        if claimed_generation is None:
                            if not self.check_queue.owns_in_progress(
                                channel_id,
                                queue_entry_token,
                            ):
                                logger.info(
                                    "Skipping cleared queue entry %s during batch claim",
                                    channel_id,
                                )
                                continue
                            logger.info(
                                "Processing externally aborted queue entry %s "
                                "without a changelog batch",
                                channel_id,
                            )
                        else:
                            batch_changelog_generation = claimed_generation
                        if (
                            self.abort_current_check.is_set()
                            and not self.check_queue.owns_in_progress(
                                channel_id,
                                queue_entry_token,
                            )
                        ):
                            logger.info(
                                "Skipping cleared queue entry %s after batch claim",
                                channel_id,
                            )
                            continue
                
                logger.debug(f"Worker processing channel {channel_id}")
                # Check this channel
                forced_profile_id = queue_metadata.get('forced_profile_id')
                force_pending, force_generation = self._queue_force_check_state(
                    channel_id
                )
                try:
                    if single_check_metadata:
                        self._run_specialized_queue_entry(
                            queue_entry,
                            force_check_generation=(
                                force_generation if force_pending else None
                            ),
                        )
                    else:
                        check_kwargs = {
                            'force_check_override': force_pending,
                            'force_check_generation': (
                                force_generation if force_pending else None
                            ),
                            'batch_changelog_generation': (
                                batch_changelog_generation
                            ),
                            'queue_entry_token': queue_entry_token,
                        }
                        if forced_profile_id:
                            check_kwargs['forced_profile_id'] = forced_profile_id
                        self._check_channel(channel_id, **check_kwargs)
                except Exception as entry_error:
                    self.check_queue.mark_failed(
                        channel_id,
                        str(entry_error),
                        entry_token=queue_entry_token,
                    )
                    raise
                finally:
                    if force_pending:
                        self.update_tracker.clear_force_check(
                            channel_id,
                            expected_generation=force_generation,
                        )
                logger.debug(f"Worker completed channel {channel_id}")
                
            except Exception as e:
                log_exception(logger, e, "worker loop")
                logger.error(f"Error in worker loop: {e}", exc_info=True)
            finally:
                if owned_queue_execution is not None:
                    self._release_queue_entry_execution(*owned_queue_execution)
        
        # Finalize any remaining batch before stopping
        if batch_changelog_generation is not None:
            self._finalize_batch_changelog(
                batch_generation=batch_changelog_generation,
            )
        
        logger.info("Stream checker worker stopped")
        log_function_return(logger, "_worker_loop")
    
    def _scheduler_loop(self):
        """Scheduler loop for M3U update-triggered and scheduled checks."""
        logger.info("Stream checker scheduler started")
        
        while self.running:
            try:
                # Wait for either a trigger event or timeout (60 seconds for global check monitoring)
                triggered = self.check_trigger.wait(timeout=60)
                
                # Handle trigger for M3U updates
                if triggered:
                    self.check_trigger.clear()
                    # Only process channel queueing if this was a real M3U update trigger
                    # (not a config change wake-up)
                    if not self.config_changed.is_set():
                        # Call _queue_updated_channels() directly - it handles pipeline mode checking internally
                        self._queue_updated_channels()
                
                # Check if config was changed
                if self.config_changed.is_set():
                    self.config_changed.clear()
                    logger.info("Configuration change detected, applying new settings immediately")
                
            except Exception as e:
                logger.error(f"Error in scheduler loop: {e}", exc_info=True)
        
        logger.info("Stream checker scheduler stopped")

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
        if not get_udi_manager().is_network_ready():
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
            udi = get_udi_manager()

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
    
    def _build_threshold_config_from_profile(self, stream_checking: Dict[str, Any]) -> Dict[str, Any]:
        """Build a threshold config dict from an already-resolved stream_checking block.

        Called once per channel run so the per-stream _is_stream_dead() calls
        don't have to re-resolve the profile from the database.  This also
        ensures that a forced_profile_id (e.g. from the multi-period picker)
        is honoured — previously _is_stream_dead() re-derived the profile from
        the channel's period assignments, ignoring any explicit picker selection.

        Args:
            stream_checking: The stream_checking sub-dict from the resolved profile.

        Returns:
            A config dict suitable for passing to utils_is_stream_dead() as the
            second argument.  Only non-zero thresholds are included.
        """
        config: Dict[str, Any] = {}
        min_res = stream_checking.get('min_resolution', 'any')
        if min_res in ('2160p', '4k'):
            config['min_resolution_width'], config['min_resolution_height'] = 3840, 2160
        elif min_res == '1080p':
            config['min_resolution_width'], config['min_resolution_height'] = 1920, 1080
        elif min_res == '720p':
            config['min_resolution_width'], config['min_resolution_height'] = 1280, 720
        elif min_res == '480p':
            config['min_resolution_width'], config['min_resolution_height'] = 854, 480
        elif min_res == '360p':
            config['min_resolution_width'], config['min_resolution_height'] = 640, 360

        min_bitrate = stream_checking.get('min_bitrate', 0)
        if min_bitrate and min_bitrate > 0:
            config['min_bitrate_kbps'] = min_bitrate

        min_fps = stream_checking.get('min_fps', 0)
        if min_fps and min_fps > 0:
            config['min_fps'] = min_fps

        # A single profile switch controls blank handling: when blank checks run,
        # detected blank streams are treated as dead. The legacy
        # treat_blank_as_dead value is intentionally ignored so older profiles
        # with it set to False do not silently keep blanks alive.
        config['treat_blank_as_dead'] = stream_checking.get('blank_check_enabled') is True
        config['treat_freeze_as_dead'] = stream_checking.get('freeze_check_enabled') is True

        return config

    @staticmethod
    def _coerce_stream_id_list(raw_stream_ids: Any) -> List[int]:
        """Return stream IDs as ints, accepting Dispatcharr int or object lists."""
        if not isinstance(raw_stream_ids, (list, tuple)):
            return []

        coerced: List[int] = []
        seen = set()
        for raw_stream_id in raw_stream_ids:
            if isinstance(raw_stream_id, dict):
                raw_stream_id = raw_stream_id.get('id')
            try:
                stream_id = int(raw_stream_id)
            except (TypeError, ValueError):
                continue
            if stream_id in seen:
                continue
            seen.add(stream_id)
            coerced.append(stream_id)
        return coerced

    def _get_channel_assignment_stream_ids(
        self,
        channel_id: int,
        channel_data: Optional[Dict[str, Any]],
        udi: Any,
        fallback_stream_ids: Optional[List[int]] = None,
        refresh_from_dispatcharr: bool = False,
    ) -> List[int]:
        """Return the best available full Dispatcharr channel assignment list.

        When dead-stream removal is disabled, the checker still rewrites the
        channel for ordering. A stale UDI stream cache must not make that write
        shrink the user's existing channel assignment list.
        """
        if refresh_from_dispatcharr:
            try:
                fetcher = getattr(udi, 'fetcher', None)
                fetch_channel_by_id = getattr(fetcher, 'fetch_channel_by_id', None)
                if callable(fetch_channel_by_id):
                    fresh_channel = fetch_channel_by_id(channel_id)
                    if isinstance(fresh_channel, dict) and 'streams' in fresh_channel:
                        try:
                            update_channel = getattr(udi, 'update_channel', None)
                            if callable(update_channel):
                                update_channel(channel_id, fresh_channel)
                        except Exception as cache_err:
                            logger.debug(
                                "Could not refresh cached channel assignment for %s: %s",
                                channel_id,
                                cache_err,
                            )
                        return self._coerce_stream_id_list(fresh_channel.get('streams'))
            except Exception as exc:
                logger.warning(
                    "Could not fetch fresh channel assignment for %s before write-back: %s",
                    channel_id,
                    exc,
                )

        assignment_ids: List[int] = []
        if isinstance(channel_data, dict):
            assignment_ids.extend(self._coerce_stream_id_list(channel_data.get('streams')))
        assignment_ids.extend(self._coerce_stream_id_list(fallback_stream_ids or []))
        return self._coerce_stream_id_list(assignment_ids)

    @staticmethod
    def _build_write_back_valid_stream_ids(
        udi: Any,
        assignment_stream_ids: List[int],
        dead_stream_removal_enabled: bool,
    ) -> Optional[set]:
        """Return valid IDs for write-back without losing assigned cache misses."""
        if dead_stream_removal_enabled:
            return None

        valid_stream_ids = set()
        try:
            get_valid_stream_ids = getattr(udi, 'get_valid_stream_ids', None)
            if callable(get_valid_stream_ids):
                valid_stream_ids.update(get_valid_stream_ids() or set())
        except Exception as exc:
            logger.warning("Could not read UDI valid stream IDs before write-back: %s", exc)
        valid_stream_ids.update(assignment_stream_ids or [])
        return valid_stream_ids

    @staticmethod
    def _get_uncached_channel_stream_ids(
        raw_channel_stream_ids: List[int],
        cached_stream_id_set: set,
        dead_stream_removal_enabled: bool,
        dead_stream_ids: set,
    ) -> List[int]:
        """Return stream IDs present in the channel's raw Dispatcharr assignment list
        but absent from the UDI stream cache (i.e. not in cached_stream_id_set).

        These IDs need to be preserved in the write-back to Dispatcharr to avoid
        accidentally dropping streams that are validly assigned but not yet indexed
        in the UDI cache (stale cache scenario).

        When dead_stream_removal_enabled is True, IDs that are known dead (in
        dead_stream_ids) are excluded so they are still removed as intended.
        """
        return [
            sid for sid in raw_channel_stream_ids
            if sid not in cached_stream_id_set
            and (not dead_stream_removal_enabled or sid not in dead_stream_ids)
        ]

    def _is_stream_dead(self, stream_data: Dict[str, Any], channel_id: Optional[int] = None, threshold_config: Optional[Dict[str, Any]] = None) -> Tuple[bool, str]:
        """
        Check if a stream should be considered dead based on profile or global settings.

        This method uses categorization logic:
        - 'offline': truly dead (0x0 resolution, 0 bitrate)
        - 'low_quality': dead based on quality thresholds

        Args:
            stream_data: Dictionary containing stream statistics
            channel_id: Optional channel ID to look up profile-specific thresholds.
                        Ignored when threshold_config is provided.
            threshold_config: Pre-built threshold dict from _build_threshold_config_from_profile().
                              When supplied, profile re-resolution is skipped entirely.
                              This ensures forced_profile_id selections are honoured.

        Returns:
            Tuple of (is_dead: bool, reason: str).
            reason values: 'offline', 'low_quality', 'unstable', 'none'.
        """
        # Default configuration
        dead_stream_config = self.config.get('dead_stream_handling', {})
        profile_config = {}

        # Fast path: caller already resolved the profile and built the threshold dict.
        # Skip the expensive per-stream profile re-resolution entirely.
        if threshold_config is not None:
            check_config = threshold_config
        # Slow path: resolve profile from channel_id (legacy / external callers).
        elif channel_id is not None:
            try:
                from apps.automation.automation_config_manager import get_automation_config_manager
                automation_config = get_automation_config_manager()
                
                # Get effective profile
                udi = get_udi_manager()
                channel = udi.get_channel_by_id(channel_id)
                group_id = channel.get('channel_group_id') if channel else None
                config = automation_config.get_effective_configuration(channel_id, group_id)
                profile = config.get('profile') if config else None

                if profile:
                    stream_checking = profile.get('stream_checking', {})
                    if stream_checking.get('enabled', False):
                        # Construct config from profile settings
                        # Convert min_res string (e.g., '1080p') to dimensions
                        min_res = stream_checking.get('min_resolution', '0x0')
                        if min_res == '2160p' or min_res == '4k':
                            profile_config['min_resolution_width'], profile_config['min_resolution_height'] = 3840, 2160
                        elif min_res == '1080p':
                            profile_config['min_resolution_width'], profile_config['min_resolution_height'] = 1920, 1080
                        elif min_res == '720p':
                            profile_config['min_resolution_width'], profile_config['min_resolution_height'] = 1280, 720
                        elif min_res == '480p':
                            profile_config['min_resolution_width'], profile_config['min_resolution_height'] = 854, 480
                        elif min_res == '360p':
                            profile_config['min_resolution_width'], profile_config['min_resolution_height'] = 640, 360
                        
                        if 'min_bitrate' in stream_checking:
                            profile_config['min_bitrate_kbps'] = stream_checking['min_bitrate']
                        
                        if 'min_fps' in stream_checking:
                            profile_config['min_fps'] = stream_checking['min_fps']
                        
                        # Use profile config if available
                        check_config = profile_config
                    else:
                        # Stream matching enabled but checker disabled in profile?
                        # Fallback to global or basic
                        check_config = dead_stream_config if dead_stream_config.get('enabled', True) else {'min_resolution_width': 0, 'min_resolution_height': 0, 'min_bitrate_kbps': 0}
                else:
                    check_config = dead_stream_config
            except Exception as e:
                logger.warning(f"Error fetching profile for dead stream check: {e}")
                check_config = dead_stream_config
        else:
            check_config = dead_stream_config

        # If global handling is disabled and no profile was found, use basic check (absolute failures only)
        if not check_config.get('enabled', True) and not profile_config:
            check_config = {
                'min_resolution_width': 0,
                'min_resolution_height': 0,
                'min_bitrate_kbps': 0,
                'min_score': 0
            }
        
        # Use centralized utility for the check
        return utils_is_stream_dead(stream_data, check_config)

    @staticmethod
    def _apply_quality_classification(stream_data: Dict[str, Any], result: Any) -> None:
        """Stamp machine-readable quality classification details onto a stream."""
        reason = getattr(result, 'reason', result[1] if isinstance(result, tuple) and len(result) > 1 else 'none')
        reason_detail = getattr(result, 'reason_detail', reason)
        details = getattr(result, 'details', {}) or {}

        stream_data['quality_reason'] = reason
        stream_data['quality_reason_detail'] = reason_detail
        stream_data['quality_reason_context'] = details
        if reason == 'none' and stream_data.get('measurement_incomplete_reason') in {
            'missing_bitrate',
            'missing_bitrate_after_recheck',
        }:
            incomplete_reason = stream_data['measurement_incomplete_reason']
            incomplete_context = stream_data.get('measurement_incomplete_context') or {}
            stream_data['quality_reason'] = incomplete_reason
            stream_data['quality_reason_detail'] = incomplete_reason
            stream_data['quality_reason_context'] = incomplete_context
        if result and reason != 'none':
            stream_data['dead_reason'] = reason
            stream_data['dead_reason_detail'] = reason_detail
            stream_data['dead_reason_context'] = details
    
    def _calculate_channel_averages(self, analyzed_streams: List[Dict], dead_stream_ids: set) -> Dict[str, str]:
        """Calculate channel-level average statistics from analyzed streams.
        
        Uses centralized utility function for consistent average calculation.
        
        Args:
            analyzed_streams: List of analyzed stream dictionaries
            dead_stream_ids: Set of stream IDs that are marked as dead
            
        Returns:
            Dictionary with avg_resolution, avg_bitrate, and avg_fps
        """
        return calculate_channel_averages(analyzed_streams, dead_stream_ids)

    def _log_blank_detection_summary(
        self,
        channel_id: int,
        _channel_name: str,
        analyzed_streams: List[Dict],
        dead_stream_ids: Optional[Set[int]] = None,
        dead_stream_removal_enabled: Optional[bool] = None,
    ) -> None:
        """Log a URL-free blank-detection summary for post-run audits."""
        probed_streams = [
            stream for stream in analyzed_streams
            if stream.get('blank_probe_ran') and stream.get('status') != 'cached'
        ]
        if not probed_streams:
            return

        blank_streams = [stream for stream in probed_streams if stream.get('blank_detected')]
        clean_count = len(probed_streams) - len(blank_streams)

        def _metric(stream: Dict, key: str) -> float:
            try:
                return float(stream.get(key) or 0.0)
            except (TypeError, ValueError):
                return 0.0

        max_ratio_stream = max(probed_streams, key=lambda stream: _metric(stream, 'blank_ratio'))
        channel_ref = _audit_ref('channel', channel_id)
        logger.info(
            f"[blank-detect] Channel summary: channel_ref={channel_ref}, "
            f"probed={len(probed_streams)}, clean={clean_count}, "
            f"blank={len(blank_streams)}, "
            f"max_ratio={_metric(max_ratio_stream, 'blank_ratio'):.3f}, "
            f"max_blank_duration={_metric(max_ratio_stream, 'blank_duration_secs'):.1f}s"
        )

        dead_stream_ids = dead_stream_ids or set()
        removal_enabled = bool(dead_stream_removal_enabled)

        for stream in blank_streams:
            stream_id = stream.get('stream_id')
            marked_dead = stream_id in dead_stream_ids
            dead_reason = stream.get('dead_reason') or ('blank' if marked_dead else 'none')
            action = 'remove' if marked_dead and removal_enabled else 'retain'
            logger.warning(
                f"[blank-detect] Blank candidate: channel_ref={channel_ref}, "
                f"stream_ref={_audit_ref('stream', stream_id)}, "
                f"duration={_metric(stream, 'blank_duration_secs'):.1f}s, "
                f"ratio={_metric(stream, 'blank_ratio'):.3f}, "
                f"segments={len(stream.get('blank_segments') or [])}, "
                f"marked_dead={marked_dead}, reason={dead_reason}, "
                f"removal_enabled={removal_enabled}, action={action}"
            )

    def _log_freeze_detection_summary(
        self,
        channel_id: int,
        _channel_name: str,
        analyzed_streams: List[Dict],
        dead_stream_ids: Optional[Set[int]] = None,
        dead_stream_removal_enabled: Optional[bool] = None,
    ) -> None:
        """Log a URL-free freeze-detection summary for post-run audits."""
        probed_streams = [
            stream for stream in analyzed_streams
            if stream.get('freeze_probe_ran') and stream.get('status') != 'cached'
        ]
        if not probed_streams:
            return

        frozen_streams = [stream for stream in probed_streams if stream.get('freeze_detected')]
        clean_count = len(probed_streams) - len(frozen_streams)

        def _metric(stream: Dict, key: str) -> float:
            try:
                return float(stream.get(key) or 0.0)
            except (TypeError, ValueError):
                return 0.0

        max_ratio_stream = max(probed_streams, key=lambda stream: _metric(stream, 'freeze_ratio'))
        channel_ref = _audit_ref('channel', channel_id)
        logger.info(
            f"[freeze-detect] Channel summary: channel_ref={channel_ref}, "
            f"probed={len(probed_streams)}, clean={clean_count}, "
            f"frozen={len(frozen_streams)}, "
            f"max_ratio={_metric(max_ratio_stream, 'freeze_ratio'):.3f}, "
            f"max_freeze_duration={_metric(max_ratio_stream, 'freeze_duration_secs'):.1f}s"
        )

        dead_stream_ids = dead_stream_ids or set()
        removal_enabled = bool(dead_stream_removal_enabled)

        for stream in frozen_streams:
            stream_id = stream.get('stream_id')
            marked_dead = stream_id in dead_stream_ids
            dead_reason = stream.get('dead_reason') or ('freeze' if marked_dead else 'none')
            action = 'remove' if marked_dead and removal_enabled else 'retain'
            logger.warning(
                f"[freeze-detect] Frozen candidate: channel_ref={channel_ref}, "
                f"stream_ref={_audit_ref('stream', stream_id)}, "
                f"duration={_metric(stream, 'freeze_duration_secs'):.1f}s, "
                f"ratio={_metric(stream, 'freeze_ratio'):.3f}, "
                f"segments={len(stream.get('freeze_segments') or [])}, "
                f"marked_dead={marked_dead}, reason={dead_reason}, "
                f"removal_enabled={removal_enabled}, action={action}"
            )

    def _refresh_dead_stream_reason_if_needed(
        self,
        stream_url: str,
        stream_id: int,
        stream_name: str,
        channel_id: int,
        reason: str,
        blank_detected: bool = False,
        freeze_detected: bool = False,
    ) -> bool:
        """Refresh stale dead-stream reasons after a checked stream gets a newer verdict."""
        if not stream_url or not reason or reason == 'none':
            return False

        try:
            current_reason = None
            if hasattr(self.dead_streams_tracker, 'get_dead_reason'):
                current_reason = self.dead_streams_tracker.get_dead_reason(stream_url)
            if current_reason == reason:
                return False

            update_reason = getattr(self.dead_streams_tracker, 'update_dead_reason', None)
            if not callable(update_reason):
                return False
            updated = update_reason(stream_url, reason, channel_id=channel_id)

            if updated and (blank_detected or freeze_detected):
                detection_label = 'blank' if blank_detected else 'freeze'
                logger.warning(
                    f"[{detection_label}-detect] Stream dead reason updated: "
                    f"channel_ref={_audit_ref('channel', channel_id)}, "
                    f"stream_ref={_audit_ref('stream', stream_id)}, "
                    f"reason={reason}"
                )
            return bool(updated)
        except Exception as exc:
            logger.warning("Failed to refresh dead stream reason for stream %s: %s", stream_id, exc)
            return False
    
    @staticmethod
    def _get_stream_m3u_account_id(stream: Dict) -> Optional[Any]:
        """Return a stream's M3U account id across legacy and SQL payloads."""
        if not isinstance(stream, dict):
            return None
        account_id = stream.get('m3u_account_id')
        if account_id in (None, ''):
            account_id = stream.get('m3u_account')
        try:
            return int(account_id) if account_id not in (None, '') else None
        except (TypeError, ValueError):
            return account_id

    @staticmethod
    def _get_priority_account_rank(account_id: Any, priority_m3u_ids: Optional[List[Any]]) -> Optional[int]:
        """Return account priority rank using type-stable id comparison."""
        if account_id in (None, '') or not priority_m3u_ids:
            return None
        account_key = str(account_id)
        for index, priority_id in enumerate(priority_m3u_ids):
            if str(priority_id) == account_key:
                return index
        return None

    def _get_m3u_account_name(self, stream_id: int, udi=None) -> Optional[str]:
        """Get the M3U account name for a stream.
        
        Args:
            stream_id: The stream ID to look up
            udi: Optional UDI manager instance (will fetch if not provided)
            
        Returns:
            M3U account name or None if not found
        """
        try:
            if udi is None:
                udi = get_udi_manager()
            
            stream_data = udi.get_stream_by_id(stream_id)
            if not stream_data:
                return None
            
            m3u_account_id = self._get_stream_m3u_account_id(stream_data)
            if not m3u_account_id:
                return None
            
            m3u_account = udi.get_m3u_account_by_id(m3u_account_id)
            if not m3u_account:
                return None
            
            return m3u_account.get('name', 'Unknown')
        except Exception as e:
            logger.debug(f"Could not fetch M3U account for stream {stream_id}: {e}")
            return None
    
    
    def _update_stream_stats(self, stream_data: Dict) -> bool:
        """Update stream stats for a single stream on the server and sync with UDI cache.
        
        This method:
        1. Constructs the stats payload from analyzed stream data
        2. Merges with existing stats on Dispatcharr
        3. PATCHes the updated stats to Dispatcharr
        4. Updates the UDI cache to keep it in sync
        
        This ensures that the UDI cache always reflects the latest stats written to Dispatcharr,
        preventing inconsistencies between changelog data and actual Dispatcharr data.
        """
        base_url = _get_base_url()
        if not base_url:
            logger.error("DISPATCHARR_BASE_URL not set.")
            return False
        
        stream_id = stream_data.get("stream_id")
        if not stream_id:
            logger.warning("No stream_id in stream data. Skipping stats update.")
            return False
        
        bitrate_value = self._bitrate_payload_value(stream_data.get("bitrate_kbps"))
        preserve_existing_bitrate = self._should_preserve_existing_bitrate(stream_data)

        # Construct the stream stats payload from the analyzed stream data
        stream_stats_payload = {
            "resolution": stream_data.get("resolution"),
            "source_fps": stream_data.get("fps"),
            "video_codec": stream_data.get("video_codec"),
            "audio_codec": stream_data.get("audio_codec"),
            "hdr_format": stream_data.get("hdr_format"),
            "pixel_format": stream_data.get("pixel_format"),
            "audio_sample_rate": stream_data.get("audio_sample_rate"),
            "audio_channels": stream_data.get("audio_channels"),
            "channel_layout": stream_data.get("channel_layout"),
            "audio_bitrate": stream_data.get("audio_bitrate"),
            "ffmpeg_output_bitrate": bitrate_value,
            "bitrate_source": stream_data.get("bitrate_source"),
            "quality_score": stream_data.get("score"),
            "quality_reason": stream_data.get("quality_reason"),
            "quality_reason_detail": stream_data.get("quality_reason_detail"),
            "quality_reason_context": stream_data.get("quality_reason_context"),
            "measurement_incomplete": bool(stream_data.get("measurement_incomplete")),
            "measurement_incomplete_reason": stream_data.get("measurement_incomplete_reason") or "none",
            "measurement_incomplete_context": stream_data.get("measurement_incomplete_context") or {},
            "bitrate_recheck_required": bool(stream_data.get("bitrate_recheck_required")),
            "bitrate_recheck_attempted": bool(stream_data.get("bitrate_recheck_attempted")),
            "bitrate_recheck_outcome": stream_data.get("bitrate_recheck_outcome") or "not_needed",
            "visual_probe_ran": True if stream_data.get("visual_probe_ran") else (False if "visual_probe_ran" in stream_data else None),
            "visual_probe_completed": stream_data.get("visual_probe_completed") if "visual_probe_completed" in stream_data else None,
            "visual_probe_incomplete": stream_data.get("visual_probe_incomplete") if "visual_probe_incomplete" in stream_data else None,
            "visual_probe_incomplete_reason": (
                stream_data.get("visual_probe_incomplete_reason") or "none"
                if "visual_probe_ran" in stream_data or "visual_probe_incomplete_reason" in stream_data
                else None
            ),
            "visual_probe_requested_duration_seconds": stream_data.get(
                "visual_probe_requested_duration_seconds"
            ),
            "visual_probe_minimum_duration_seconds": stream_data.get(
                "visual_probe_minimum_duration_seconds"
            ),
            "visual_probe_duration_seconds": stream_data.get("visual_probe_duration_seconds"),
            "visual_probe_duration_adjusted": stream_data.get("visual_probe_duration_adjusted"),
            "visual_probe_duration_adjustment_reason": stream_data.get(
                "visual_probe_duration_adjustment_reason"
            ),
            "visual_probe_elapsed_time": stream_data.get("visual_probe_elapsed_time"),
            # PRESERVE_FALSE: emit False (not None) so the None-filter below keeps these
            # fields in the payload even when the probe ran but found no loop.
            # Without this, Dispatcharr's PATCH merge leaves a stale loop_probe_ran: true
            # from a previous run in place for streams not probed in the current run.
            "loop_probe_ran": True if stream_data.get("loop_probe_ran") else (False if "loop_probe_ran" in stream_data else None),
            "loop_detected": stream_data.get("loop_detected") if stream_data.get("loop_probe_ran") else (False if "loop_detected" in stream_data else None),
            "loop_duration_secs": stream_data.get("loop_duration_secs") if stream_data.get("loop_detected") else None,
            "loop_score_penalty": stream_data.get("loop_score_penalty"),
            "blank_probe_ran": True if stream_data.get("blank_probe_ran") else (False if "blank_probe_ran" in stream_data else None),
            "blank_detected": stream_data.get("blank_detected") if stream_data.get("blank_probe_ran") else (False if "blank_detected" in stream_data else None),
            "blank_duration_secs": stream_data.get("blank_duration_secs") if stream_data.get("blank_probe_ran") else None,
            "blank_ratio": stream_data.get("blank_ratio") if stream_data.get("blank_probe_ran") else None,
            "freeze_probe_ran": True if stream_data.get("freeze_probe_ran") else (False if "freeze_probe_ran" in stream_data else None),
            "freeze_detected": stream_data.get("freeze_detected") if stream_data.get("freeze_probe_ran") else (False if "freeze_detected" in stream_data else None),
            "freeze_duration_secs": stream_data.get("freeze_duration_secs") if stream_data.get("freeze_probe_ran") else None,
            "freeze_ratio": stream_data.get("freeze_ratio") if stream_data.get("freeze_probe_ran") else None,
        }
        
        # Clean up the payload, removing None and N/A values.
        # PRESERVE_FALSE: keep False values for boolean loop fields so they
        # explicitly clear stale True values in Dispatcharr on PATCH merge.
        PRESERVE_FALSE = {
            "loop_probe_ran",
            "loop_detected",
            "blank_probe_ran",
            "blank_detected",
            "freeze_probe_ran",
            "freeze_detected",
            "visual_probe_ran",
            "visual_probe_completed",
            "visual_probe_incomplete",
            "visual_probe_duration_adjusted",
            "measurement_incomplete",
            "bitrate_recheck_required",
            "bitrate_recheck_attempted",
        }
        PRESERVE_NULL = set()
        if not preserve_existing_bitrate:
            PRESERVE_NULL.add("ffmpeg_output_bitrate")
        stream_stats_payload = {
            k: v for k, v in stream_stats_payload.items()
            if v not in [None, "N/A"] or (v is None and k in PRESERVE_NULL)
        }
        for k in PRESERVE_FALSE:
            if k in stream_stats_payload or stream_data.get(k) is False:
                stream_stats_payload[k] = stream_data.get(k) if stream_data.get(k) is not None else False
        
        if not stream_stats_payload:
            logger.debug(f"No data to update for stream {stream_id}. Skipping.")
            return False
        
        # Construct the URL for the specific stream
        stream_url = f"{base_url}/api/channels/streams/{int(stream_id)}/"

        _stream_stats_update_lock.acquire()
        try:
            # Fetch the existing stream data from UDI
            udi = get_udi_manager()
            existing_stream_data = udi.get_stream_by_id(int(stream_id))
            if not existing_stream_data:
                logger.warning(f"Could not fetch existing data for stream {stream_id}. Skipping stats update.")
                return False
            
            # Get the existing stream_stats or an empty dict
            existing_stats = existing_stream_data.get("stream_stats") or {}
            if isinstance(existing_stats, str):
                try:
                    existing_stats = json.loads(existing_stats)
                except json.JSONDecodeError:
                    existing_stats = {}
            
            # Merge the existing stats with the new payload
            updated_stats = {**existing_stats, **stream_stats_payload}
            
            # Send the PATCH request with the updated stream_stats
            patch_payload = {"stream_stats": updated_stats}
            logger.info(f"Updating stream {stream_id} stats with: {stream_stats_payload}")
            patch_request(stream_url, patch_payload)
            
            # Update UDI cache with the new stats to keep it in sync with Dispatcharr
            # This ensures changelog and verification read the correct, up-to-date data
            updated_stream_data = existing_stream_data.copy()
            updated_stream_data['stream_stats'] = updated_stats
            udi.update_stream(int(stream_id), updated_stream_data)
            logger.debug(f"Updated UDI cache for stream {stream_id} with new stats")
            
            return True
        
        except Exception as e:
            logger.error(f"Error updating stats for stream {stream_id}: {e}")
            return False
        finally:
            _stream_stats_update_lock.release()
    
    def _prepare_stream_stats_for_batch(self, stream_data: Dict) -> Optional[Dict[str, Any]]:
        """
        Prepare stream stats for batch update.
        
        This method extracts and formats stream stats from analyzed stream data
        for use in batch update operations.
        
        Parameters:
            stream_data (Dict): Analyzed stream data with resolution, fps, codecs, bitrate
            
        Returns:
            Optional[Dict[str, Any]]: Dict with 'stream_id' and 'stream_stats' keys,
                                     or None if no valid stats to update
        """
        stream_id = stream_data.get("stream_id")
        if not stream_id:
            logger.warning("No stream_id in stream data. Skipping stats preparation.")
            return None
        
        bitrate_value = self._bitrate_payload_value(stream_data.get("bitrate_kbps"))
        preserve_existing_bitrate = self._should_preserve_existing_bitrate(stream_data)

        # Construct the stream stats payload from the analyzed stream data
        stream_stats_payload = {
            "resolution": stream_data.get("resolution"),
            "source_fps": stream_data.get("fps"),
            "video_codec": stream_data.get("video_codec"),
            "audio_codec": stream_data.get("audio_codec"),
            "hdr_format": stream_data.get("hdr_format"),
            "pixel_format": stream_data.get("pixel_format"),
            "audio_sample_rate": stream_data.get("audio_sample_rate"),
            "audio_channels": stream_data.get("audio_channels"),
            "channel_layout": stream_data.get("channel_layout"),
            "audio_bitrate": stream_data.get("audio_bitrate"),
            "ffmpeg_output_bitrate": bitrate_value,
            "bitrate_source": stream_data.get("bitrate_source"),
            "quality_score": stream_data.get("score"),
            "quality_reason": stream_data.get("quality_reason"),
            "quality_reason_detail": stream_data.get("quality_reason_detail"),
            "quality_reason_context": stream_data.get("quality_reason_context"),
            "measurement_incomplete": bool(stream_data.get("measurement_incomplete")),
            "measurement_incomplete_reason": stream_data.get("measurement_incomplete_reason") or "none",
            "measurement_incomplete_context": stream_data.get("measurement_incomplete_context") or {},
            "bitrate_recheck_required": bool(stream_data.get("bitrate_recheck_required")),
            "bitrate_recheck_attempted": bool(stream_data.get("bitrate_recheck_attempted")),
            "bitrate_recheck_outcome": stream_data.get("bitrate_recheck_outcome") or "not_needed",
            "visual_probe_ran": True if stream_data.get("visual_probe_ran") else False,
            "visual_probe_completed": stream_data.get("visual_probe_completed") if "visual_probe_completed" in stream_data else False,
            "visual_probe_incomplete": stream_data.get("visual_probe_incomplete") if "visual_probe_incomplete" in stream_data else False,
            "visual_probe_incomplete_reason": (
                stream_data.get("visual_probe_incomplete_reason") or "none"
                if "visual_probe_ran" in stream_data or "visual_probe_incomplete_reason" in stream_data
                else None
            ),
            "visual_probe_requested_duration_seconds": stream_data.get(
                "visual_probe_requested_duration_seconds"
            ),
            "visual_probe_minimum_duration_seconds": stream_data.get(
                "visual_probe_minimum_duration_seconds"
            ),
            "visual_probe_duration_seconds": stream_data.get("visual_probe_duration_seconds"),
            "visual_probe_duration_adjusted": stream_data.get("visual_probe_duration_adjusted"),
            "visual_probe_duration_adjustment_reason": stream_data.get(
                "visual_probe_duration_adjustment_reason"
            ),
            "visual_probe_elapsed_time": stream_data.get("visual_probe_elapsed_time"),
            # PRESERVE_FALSE: emit False (not None) so the None-filter below keeps these
            # fields in the payload even when the probe ran but found no loop.
            # Without this, Dispatcharr's PATCH merge leaves a stale loop_probe_ran: true
            # from a previous run in place for streams not probed in the current run.
            "loop_probe_ran": True if stream_data.get("loop_probe_ran") else False,
            "loop_detected": stream_data.get("loop_detected") if stream_data.get("loop_probe_ran") else False,
            "loop_duration_secs": stream_data.get("loop_duration_secs") if stream_data.get("loop_detected") else None,
            "loop_score_penalty": stream_data.get("loop_score_penalty"),
            "blank_probe_ran": True if stream_data.get("blank_probe_ran") else False,
            "blank_detected": stream_data.get("blank_detected") if stream_data.get("blank_probe_ran") else False,
            "blank_duration_secs": stream_data.get("blank_duration_secs") if stream_data.get("blank_probe_ran") else None,
            "blank_ratio": stream_data.get("blank_ratio") if stream_data.get("blank_probe_ran") else None,
            "freeze_probe_ran": True if stream_data.get("freeze_probe_ran") else False,
            "freeze_detected": stream_data.get("freeze_detected") if stream_data.get("freeze_probe_ran") else False,
            "freeze_duration_secs": stream_data.get("freeze_duration_secs") if stream_data.get("freeze_probe_ran") else None,
            "freeze_ratio": stream_data.get("freeze_ratio") if stream_data.get("freeze_probe_ran") else None,
        }
        
        # Clean up the payload, removing None and N/A values.
        # PRESERVE_FALSE: keep False values for boolean loop fields so they
        # explicitly clear stale True values in Dispatcharr on PATCH merge.
        PRESERVE_FALSE = {
            "loop_probe_ran",
            "loop_detected",
            "blank_probe_ran",
            "blank_detected",
            "freeze_probe_ran",
            "freeze_detected",
            "visual_probe_ran",
            "visual_probe_completed",
            "visual_probe_incomplete",
            "visual_probe_duration_adjusted",
            "measurement_incomplete",
            "bitrate_recheck_required",
            "bitrate_recheck_attempted",
        }
        QUALITY_FIELDS = {
            "quality_reason",
            "quality_reason_detail",
            "quality_reason_context",
        }
        stream_stats_payload = {
            k: v for k, v in stream_stats_payload.items()
            if (
                v not in [None, "N/A"]
                or (
                    v is None
                    and k not in PRESERVE_FALSE
                    and k not in QUALITY_FIELDS
                    and not (k == "ffmpeg_output_bitrate" and preserve_existing_bitrate)
                )
            )
        }
        for k in PRESERVE_FALSE:
            if k in stream_stats_payload or stream_data.get(k) is False:
                stream_stats_payload[k] = stream_data.get(k) if stream_data.get(k) is not None else False
        
        if not stream_stats_payload:
            logger.debug(f"No data to update for stream {stream_id}. Skipping.")
            return None
        
        return {
            'stream_id': stream_id,
            'stream_stats': stream_stats_payload
        }

    @staticmethod
    def _bitrate_payload_value(value: Any) -> Optional[int]:
        parsed = parse_bitrate_value(value)
        if parsed is None or parsed <= 0:
            return None
        return int(parsed)

    @staticmethod
    def _load_stream_stats_blob(stream_data: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        if not isinstance(stream_data, dict):
            return {}
        stream_stats = stream_data.get("stream_stats") or {}
        if isinstance(stream_stats, str):
            try:
                stream_stats = json.loads(stream_stats) or {}
            except json.JSONDecodeError:
                stream_stats = {}
        return stream_stats if isinstance(stream_stats, dict) else {}

    @classmethod
    def _previous_stream_bitrate(cls, stream_data: Optional[Dict[str, Any]]) -> Optional[int]:
        stream_stats = cls._load_stream_stats_blob(stream_data)
        return cls._bitrate_payload_value(
            stream_stats.get("ffmpeg_output_bitrate")
            or stream_stats.get("bitrate_kbps")
            or stream_stats.get("bitrate")
        )

    @classmethod
    def _should_preserve_existing_bitrate(cls, stream_data: Dict[str, Any]) -> bool:
        if cls._bitrate_payload_value(stream_data.get("bitrate_kbps")) is not None:
            return False
        return bool(
            stream_data.get("measurement_incomplete")
            or stream_data.get("bitrate_recheck_required")
        )

    @classmethod
    def _apply_previous_bitrate_fallback(
        cls,
        analyzed: Dict[str, Any],
        existing_stream: Optional[Dict[str, Any]],
    ) -> None:
        if not cls._should_preserve_existing_bitrate(analyzed):
            return
        previous_bitrate = cls._previous_stream_bitrate(existing_stream)
        if previous_bitrate is None:
            return
        analyzed["scoring_bitrate_kbps"] = previous_bitrate
        analyzed["bitrate_preserved_from_previous_measurement"] = True
        analyzed["preserved_bitrate_kbps"] = previous_bitrate
        analyzed["preserved_bitrate_source"] = "previous_stream_stats"
        context = analyzed.get("measurement_incomplete_context")
        if not isinstance(context, dict):
            context = {}
        context.setdefault("preserved_bitrate_kbps", previous_bitrate)
        context.setdefault("preserved_bitrate_source", "previous_stream_stats")
        analyzed["measurement_incomplete_context"] = context

    @staticmethod
    def _current_probe_stats_source(
        stream_data: Optional[Dict[str, Any]],
        analyzed: Optional[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Return current probe stats when present, avoiding stale cache display."""
        if isinstance(analyzed, dict):
            return analyzed
        return stream_data if isinstance(stream_data, dict) else {}

    @classmethod
    def _has_incomplete_bitrate_measurement(cls, stream_data: Optional[Dict[str, Any]]) -> bool:
        if not isinstance(stream_data, dict):
            return False
        if cls._bitrate_payload_value(stream_data.get("bitrate_kbps")) is not None:
            return False
        return bool(
            stream_data.get("measurement_incomplete_reason") in {
                "missing_bitrate",
                "missing_bitrate_after_recheck",
            }
            or stream_data.get("bitrate_recheck_required")
        )

    @classmethod
    def _apply_incomplete_bitrate_status(
        cls,
        target: Dict[str, Any],
        source: Optional[Dict[str, Any]],
    ) -> None:
        if not cls._has_incomplete_bitrate_measurement(source):
            return
        context = {}
        if isinstance(source, dict) and isinstance(source.get("measurement_incomplete_context"), dict):
            context = source.get("measurement_incomplete_context") or {}
        reason = "missing_bitrate"
        if isinstance(source, dict) and source.get("measurement_incomplete_reason") in {
            "missing_bitrate",
            "missing_bitrate_after_recheck",
        }:
            reason = source.get("measurement_incomplete_reason")
        target["status"] = "incomplete_bitrate"
        target["reason"] = reason
        target["reason_detail"] = reason
        target["quality_reason"] = reason
        target["quality_reason_detail"] = reason
        target["quality_reason_context"] = context
        target["measurement_incomplete"] = True
        target["measurement_incomplete_reason"] = reason
        target["measurement_incomplete_context"] = context
        target["bitrate_recheck_required"] = True
        if isinstance(source, dict):
            target["bitrate_recheck_attempted"] = bool(
                source.get("bitrate_recheck_attempted")
            )
            target["bitrate_recheck_outcome"] = (
                source.get("bitrate_recheck_outcome") or "not_needed"
            )

    @staticmethod
    def _copy_bitrate_recheck_report_fields(
        target: Dict[str, Any],
        source: Optional[Dict[str, Any]],
    ) -> None:
        """Keep final bitrate-recheck evidence in live and Changelog rows."""
        if not isinstance(source, dict):
            return
        for field in BITRATE_RECHECK_REPORT_FIELDS:
            if field in source:
                target[field] = source.get(field)

    @classmethod
    def _merge_deferred_bitrate_recheck(
        cls,
        initial: Dict[str, Any],
        recheck: Optional[Dict[str, Any]],
    ) -> str:
        """Merge only bitrate evidence from a deferred basis probe.

        The initial result remains authoritative for media identity and visual
        detections. A lightweight bitrate recheck must never erase a valid
        blank/freeze result or turn a playable stream into an offline stream.
        """
        if not isinstance(recheck, dict):
            initial["bitrate_recheck_attempted"] = False
            initial["bitrate_recheck_outcome"] = "not_run"
            return "not_run"

        if recheck.get("provider_limit_skipped"):
            initial["bitrate_recheck_attempted"] = False
            initial["bitrate_recheck_outcome"] = "provider_capacity_unavailable"
            context = initial.get("measurement_incomplete_context")
            if not isinstance(context, dict):
                context = {}
            context["bitrate_recheck_outcome"] = "provider_capacity_unavailable"
            context["bitrate_recheck_reason"] = (
                recheck.get("skipped_reason")
                or recheck.get("reason_detail")
                or "provider_capacity_unavailable"
            )
            initial["measurement_incomplete_context"] = context
            return "provider_capacity_unavailable"

        initial["bitrate_recheck_attempted"] = True
        bitrate = cls._bitrate_payload_value(recheck.get("bitrate_kbps"))
        recheck_status = str(recheck.get("status") or "unknown")

        if bitrate is not None and recheck_status.lower() == "ok":
            initial["bitrate_kbps"] = recheck.get("bitrate_kbps")
            initial["bitrate_source"] = recheck.get("bitrate_source")
            initial["measurement_incomplete"] = False
            initial["measurement_incomplete_reason"] = "none"
            initial["measurement_incomplete_context"] = {}
            initial["bitrate_recheck_required"] = False
            initial["bitrate_recheck_outcome"] = "recovered"
            initial["quality_reason"] = "none"
            initial["quality_reason_detail"] = "none"
            initial["quality_reason_context"] = {}
            for key in (
                "scoring_bitrate_kbps",
                "bitrate_preserved_from_previous_measurement",
                "preserved_bitrate_kbps",
                "preserved_bitrate_source",
            ):
                initial.pop(key, None)
            return "recovered"

        context = initial.get("measurement_incomplete_context")
        if not isinstance(context, dict):
            context = {}
        context.update({
            key: value
            for key, value in {
                "bitrate_recheck_outcome": "unavailable",
                "bitrate_recheck_status": recheck_status,
                "bitrate_recheck_source": recheck.get("bitrate_source"),
                "bitrate_recheck_elapsed_seconds": recheck.get("elapsed_time"),
                "bitrate_recheck_ffprobe_fallback_reason": recheck.get(
                    "ffprobe_fallback_reason"
                ),
            }.items()
            if value not in (None, "", [], {})
        })
        initial["measurement_incomplete"] = True
        initial["measurement_incomplete_reason"] = "missing_bitrate_after_recheck"
        initial["measurement_incomplete_context"] = context
        initial["bitrate_recheck_required"] = True
        initial["bitrate_recheck_outcome"] = "unavailable"
        initial["quality_reason"] = "missing_bitrate_after_recheck"
        initial["quality_reason_detail"] = "missing_bitrate_after_recheck"
        initial["quality_reason_context"] = context
        return "unavailable"

    def _run_deferred_bitrate_rechecks(
        self,
        results: List[Dict[str, Any]],
        streams_by_id: Dict[Any, Dict[str, Any]],
        recheck_stream: Callable[[Dict[str, Any], Dict[str, Any]], Optional[Dict[str, Any]]],
        *,
        abort_event: Optional[threading.Event] = None,
        on_start: Optional[Callable[[Dict[str, Any], int, int], None]] = None,
        on_complete: Optional[
            Callable[[Dict[str, Any], str, int, int], None]
        ] = None,
    ) -> List[Dict[str, Any]]:
        """Recheck missing bitrates one at a time after initial channel analysis."""
        result_by_id = {
            str(result.get("stream_id")): result
            for result in results
            if isinstance(result, dict) and result.get("stream_id") is not None
        }
        candidates = []
        for stream_id, stream in streams_by_id.items():
            initial = result_by_id.get(str(stream_id))
            if (
                initial is not None
                and not initial.get("provider_limit_skipped")
                and str(initial.get("status") or "").lower() == "ok"
                and self._has_incomplete_bitrate_measurement(initial)
            ):
                candidates.append((stream, initial))

        if not candidates:
            return results

        logger.info(
            "Starting %s deferred bitrate recheck(s) sequentially after the "
            "initial channel scan",
            len(candidates),
        )
        total = len(candidates)
        for index, (stream, initial) in enumerate(candidates, 1):
            if abort_event is not None and abort_event.is_set():
                logger.info("Abort requested while running deferred bitrate rechecks")
                break
            if on_start:
                on_start(initial, index, total)
            try:
                recheck = recheck_stream(stream, initial)
            except Exception as exc:
                logger.warning(
                    "Deferred bitrate recheck raised %s for stream_ref=%s",
                    type(exc).__name__,
                    _audit_ref("stream", initial.get("stream_id")),
                )
                recheck = {
                    "status": "Error",
                    "bitrate_kbps": None,
                    "elapsed_time": 0,
                }
            if abort_event is not None and abort_event.is_set():
                logger.info(
                    "Abort requested after deferred bitrate probe for stream_ref=%s; "
                    "discarding its outcome",
                    _audit_ref("stream", initial.get("stream_id")),
                )
                break
            outcome = self._merge_deferred_bitrate_recheck(initial, recheck)
            logger.info(
                "Deferred bitrate recheck %s/%s finished for stream_ref=%s: %s",
                index,
                total,
                _audit_ref("stream", initial.get("stream_id")),
                outcome,
            )
            if on_complete:
                on_complete(initial, outcome, index, total)

        return results

    @staticmethod
    def _initialize_provider_probe_account_inventory(
        *,
        udi: Any,
        limiter: Any,
        initialize_account_limits: Callable[[List[Dict[str, Any]]], Any],
        operation_label: str,
    ) -> bool:
        """Publish fresh provider authority before any quality probe starts.

        Inventory invalidation deliberately precedes the UDI fetch. A missing,
        failed, or malformed fetch therefore cannot reuse limits from an older
        successful run. An empty list is a valid authoritative snapshot and is
        still published. It permits explicitly custom streams to use the shared
        scheduler while the limiter rejects every provider-account stream.
        """
        invalidate_inventory = getattr(
            limiter,
            'invalidate_account_inventory',
            None,
        )
        if not callable(invalidate_inventory):
            logger.error(
                "%s aborted because the account limiter cannot invalidate "
                "stale provider inventory",
                operation_label,
            )
            return False
        try:
            invalidate_inventory()
        except Exception as exc:
            logger.error(
                "%s aborted because stale provider inventory invalidation "
                "failed: %s",
                operation_label,
                exc,
            )
            return False

        try:
            limiter.udi_manager = udi
        except Exception as exc:
            logger.error(
                "%s aborted because the account limiter could not bind the "
                "current UDI snapshot: %s",
                operation_label,
                exc,
            )
            return False

        get_accounts = getattr(udi, 'get_m3u_accounts', None)
        if not callable(get_accounts):
            logger.error(
                "%s aborted because the UDI account inventory getter is "
                "unavailable",
                operation_label,
            )
            return False
        try:
            accounts = get_accounts()
        except Exception as exc:
            logger.error(
                "%s aborted because the UDI account inventory fetch failed: %s",
                operation_label,
                exc,
            )
            return False

        if not isinstance(accounts, list) or any(
            not isinstance(account, dict)
            for account in accounts
        ):
            logger.error(
                "%s aborted because the UDI account inventory is malformed",
                operation_label,
            )
            return False

        try:
            # Empty is authoritative too: publishing it clears any prior
            # account routes instead of leaving stale capacity available.
            publication_result = initialize_account_limits(accounts)
        except Exception as exc:
            logger.error(
                "%s aborted because provider account inventory publication "
                "failed: %s",
                operation_label,
                exc,
            )
            return False

        # The limiter performs the authoritative deep validation (IDs, limits,
        # profiles, duplicates) while publishing atomically.  Preserve support
        # for legacy initializers that returned None, but an explicit rejection
        # must never open the scheduler for a non-empty malformed snapshot.
        if publication_result is False:
            logger.error(
                "%s aborted because the provider account inventory was "
                "rejected during publication",
                operation_label,
            )
            return False

        if not accounts:
            logger.info(
                "%s continuing with an authoritative empty provider account "
                "inventory; only explicit custom streams remain eligible",
                operation_label,
            )
        return True

    def _run_capacity_limited_stream_probes(
        self,
        streams: List[Dict[str, Any]],
        *,
        udi: Any,
        **analysis_params: Any,
    ) -> List[Dict[str, Any]]:
        """Run probes through the shared provider/profile-aware scheduler.

        One-off checks use this path too, so they cannot bypass account,
        profile, global-worker, viewer-preemption, URL-transformation, or abort
        behavior that applies to channel quality checks.
        """
        from apps.stream.concurrent_stream_limiter import (
            get_account_limiter,
            get_smart_scheduler,
            initialize_account_limits,
        )

        limiter = get_account_limiter()
        if not self._initialize_provider_probe_account_inventory(
            udi=udi,
            limiter=limiter,
            initialize_account_limits=initialize_account_limits,
            operation_label='One-off stream probe',
        ):
            return []

        concurrent_enabled = bool(self.config.get("concurrent_streams.enabled", True))
        configured_limit = self.config.get("concurrent_streams.global_limit", 10)
        try:
            global_limit = max(1, int(configured_limit)) if concurrent_enabled else 1
        except (TypeError, ValueError):
            global_limit = 10 if concurrent_enabled else 1

        scheduler = get_smart_scheduler(global_limit=global_limit)
        return scheduler.check_streams_with_limits(
            streams=streams,
            check_function=analyze_stream,
            stagger_delay=0,
            abort_event=self.abort_current_check,
            provider_wait_timeout=self.config.get(
                "concurrent_streams.provider_wait_timeout",
                300,
            ),
            **analysis_params,
        )
    
    def _start_batch_changelog(
        self,
        *,
        require_not_aborted: bool = False,
    ) -> Optional[int]:
        """Start a new batch for changelog entries.

        Queue workers use ``require_not_aborted`` to linearize the narrow
        interval between popping an entry and claiming its changelog batch.
        ``clear_queue`` publishes the abort before taking ``batch_lock``; the
        clear therefore either discards a batch started first or prevents a
        cleared entry from starting one afterward.
        """
        with self.batch_lock:
            if require_not_aborted and self.abort_current_check.is_set():
                return None

            active_generation = getattr(
                self,
                '_active_batch_changelog_generation',
                None,
            )
            if self.batch_start_time is not None:
                # Backward-compatible recovery for tests or restored objects
                # which predate generation tracking but already own a batch.
                if active_generation is None:
                    self._batch_changelog_generation = (
                        int(getattr(self, '_batch_changelog_generation', 0)) + 1
                    )
                    active_generation = self._batch_changelog_generation
                    self._active_batch_changelog_generation = active_generation
                return active_generation

            self._batch_changelog_generation = (
                int(getattr(self, '_batch_changelog_generation', 0)) + 1
            )
            active_generation = self._batch_changelog_generation
            self._active_batch_changelog_generation = active_generation
            self.batch_start_time = datetime.now().isoformat()
            self.batch_changelog_entries = []
            logger.debug(
                "Started changelog batch generation %s",
                active_generation,
            )
            return active_generation
    
    def _add_to_batch_changelog(
        self,
        channel_entry: Dict[str, Any],
        *,
        batch_generation: Optional[int] = None,
    ) -> bool:
        """Add a channel check result to the current batch.
        
        Args:
            channel_entry: Dictionary containing channel check results
            batch_generation: Optional generation token returned by
                ``_start_batch_changelog``. Queue-worker calls always provide
                it; omission remains supported for direct legacy callers.
        """
        with self.batch_lock:
            active_generation = getattr(
                self,
                '_active_batch_changelog_generation',
                None,
            )
            if (
                batch_generation is not None
                and batch_generation != active_generation
            ):
                logger.debug(
                    "Ignoring stale changelog add for batch generation %s "
                    "(active: %s)",
                    batch_generation,
                    active_generation,
                )
                return False
            if self.batch_start_time is not None:
                self.batch_changelog_entries.append(channel_entry)
                logger.debug(f"Added channel entry to batch (total: {len(self.batch_changelog_entries)})")
                return True
            return False

    def _build_batch_changelog_entry(
        self,
        *,
        channel_id: int,
        channel_name: str,
        logo_url: Optional[str],
        total_streams: int,
        stream_stats: List[Dict[str, Any]],
        averages: Dict[str, Any],
        skipped_streams: Optional[List[Dict[str, Any]]] = None,
        channel_visibility: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Build the canonical, complete per-channel batch changelog payload."""
        complete_stats = deepcopy([
            item for item in stream_stats
            if isinstance(item, dict)
        ])
        checked = {"checked_streams": complete_stats}
        entry = {
            "channel_id": channel_id,
            "channel_name": channel_name,
            "logo_url": logo_url,
            "total_streams": total_streams,
            "streams_analyzed": len(complete_stats),
            "dead_streams_detected": self._count_failed_checked_streams(checked),
            "blank_streams_detected": self._count_checked_stream_status(checked, "blank"),
            "freeze_streams_detected": self._count_checked_stream_status(checked, "freeze"),
            "streams_revived": sum(
                1 for item in complete_stats if item.get("status") == "revived"
            ),
            "incomplete_bitrate_streams": sum(
                1 for item in complete_stats if item.get("status") == "incomplete_bitrate"
            ),
            "avg_resolution": averages.get("avg_resolution", "N/A"),
            "avg_bitrate": averages.get("avg_bitrate", "N/A"),
            "avg_fps": averages.get("avg_fps", "N/A"),
            "success": True,
            "stream_stats": complete_stats,
            "skipped_streams": deepcopy(skipped_streams or []),
        }
        visibility_changelog = self._visibility_changelog_result(channel_visibility)
        if visibility_changelog:
            entry["channel_visibility"] = visibility_changelog
        return entry
    
    def _finalize_batch_changelog(
        self,
        *,
        batch_generation: Optional[int] = None,
    ) -> bool:
        """Finalize the current batch and create a consolidated changelog entry."""
        with self.batch_lock:
            active_generation = getattr(
                self,
                '_active_batch_changelog_generation',
                None,
            )
            if (
                batch_generation is not None
                and batch_generation != active_generation
            ):
                logger.debug(
                    "Ignoring stale changelog finalizer for batch generation %s "
                    "(active: %s)",
                    batch_generation,
                    active_generation,
                )
                return False
            if self.batch_start_time is None or len(self.batch_changelog_entries) == 0:
                logger.debug("No batch to finalize")
                # A started batch can be left empty when its active queue entry
                # is aborted before producing a changelog row. Normalize both
                # fields here so the next queue batch always gets a fresh start
                # timestamp instead of inheriting this stale lifecycle state.
                self.batch_start_time = None
                self.batch_changelog_entries = []
                self._active_batch_changelog_generation = None
                return False
            
            if not self.changelog:
                logger.debug("Changelog not available, skipping batch finalization")
                self.batch_start_time = None
                self.batch_changelog_entries = []
                self._active_batch_changelog_generation = None
                return False
            
            try:
                # Calculate duration
                start_dt = datetime.fromisoformat(self.batch_start_time)
                end_dt = datetime.now()
                duration_seconds = int((end_dt - start_dt).total_seconds())
                
                # Format duration as human-readable string
                if duration_seconds < 60:
                    duration_str = f"{duration_seconds}s"
                elif duration_seconds < 3600:
                    minutes = duration_seconds // 60
                    seconds = duration_seconds % 60
                    duration_str = f"{minutes}m {seconds}s"
                else:
                    hours = duration_seconds // 3600
                    minutes = (duration_seconds % 3600) // 60
                    duration_str = f"{hours}h {minutes}m"
                
                # Calculate aggregate stats
                total_channels = len(self.batch_changelog_entries)
                total_streams = sum(entry.get('total_streams', 0) for entry in self.batch_changelog_entries)
                streams_analyzed = sum(entry.get('streams_analyzed', 0) for entry in self.batch_changelog_entries)
                dead_streams = sum(entry.get('dead_streams_detected', 0) for entry in self.batch_changelog_entries)
                blank_streams = sum(entry.get('blank_streams_detected', 0) for entry in self.batch_changelog_entries)
                freeze_streams = sum(entry.get('freeze_streams_detected', 0) for entry in self.batch_changelog_entries)
                streams_revived = sum(entry.get('streams_revived', 0) for entry in self.batch_changelog_entries)
                incomplete_bitrate_streams = sum(
                    entry.get('incomplete_bitrate_streams', 0)
                    for entry in self.batch_changelog_entries
                )
                successful_checks = sum(1 for entry in self.batch_changelog_entries if entry.get('success', False))
                failed_checks = total_channels - successful_checks
                
                # Prepare subentries in the format expected by the UI
                subentries = [{
                    "group": "check",
                    "items": [
                        {
                            "channel_id": entry.get('channel_id'),
                            "channel_name": entry.get('channel_name'),
                            "logo_url": entry.get('logo_url'),
                            "stats": {
                                "total_streams": entry.get('total_streams', 0),
                                "streams_analyzed": entry.get('streams_analyzed', 0),
                                "dead_streams": entry.get('dead_streams_detected', 0),
                                "blank_streams": entry.get('blank_streams_detected', 0),
                                "freeze_streams": entry.get('freeze_streams_detected', 0),
                                "streams_revived": entry.get('streams_revived', 0),
                                "incomplete_bitrate_streams": entry.get('incomplete_bitrate_streams', 0),
                                "avg_resolution": entry.get('avg_resolution', 'N/A'),
                                "avg_bitrate": entry.get('avg_bitrate', 'N/A'),
                                "avg_fps": entry.get('avg_fps', 'N/A'),
                                "stream_details": entry.get('stream_stats', []),
                                "skipped_streams": entry.get('skipped_streams', []),
                            }
                        }
                        for entry in self.batch_changelog_entries
                    ]
                }]
                
                # Create consolidated changelog entry
                self.changelog.add_entry(
                    action='batch_stream_check',
                    details={
                        'total_channels': total_channels,
                        'successful_checks': successful_checks,
                        'failed_checks': failed_checks,
                        'total_streams': total_streams,
                        'streams_analyzed': streams_analyzed,
                        'dead_streams': dead_streams,
                        'blank_streams': blank_streams,
                        'freeze_streams': freeze_streams,
                        'streams_revived': streams_revived,
                        'incomplete_bitrate_streams': incomplete_bitrate_streams,
                        'duration': duration_str,
                        'duration_seconds': duration_seconds
                    },
                    timestamp=self.batch_start_time,
                    subentries=subentries
                )
                
                logger.info(f"Finalized batch changelog: {total_channels} channels, {streams_analyzed} streams analyzed in {duration_str}")
                
                # Note: trigger_channel_re_enabling and trigger_empty_channel_disabling 
                # have been deprecated as they relied on Dispatcharr channel profiles 
                # which have been removed.
                
                return True
            except Exception as e:
                logger.error(f"Failed to finalize batch changelog: {e}", exc_info=True)
                return False
            finally:
                # Reset batch tracking
                self.batch_start_time = None
                self.batch_changelog_entries = []
                self._active_batch_changelog_generation = None
    
    # Deprecated: _trigger_empty_channel_disabling and _trigger_channel_re_enabling
    # were removed as they relied on a missing module 'empty_channel_manager'
    # and obsolete Dispatcharr features.
    
    def _check_channel_limits(
        self,
        channel_id: int,
        channel_name: str,
        streams: List[Dict],
        provider_limit_override: bool = False,
    ) -> Optional[Dict]:
        """Check if a channel can be checked based on viewer and playlist limits.
        
        This method now uses profile-aware checking. Instead of just checking account-level
        max_streams, it verifies that at least one stream has an available profile slot.
        
        Args:
            channel_id: ID of the channel
            channel_name: Name of the channel
            streams: List of streams for the channel
            provider_limit_override: If True, bypass provider/profile capacity
                skips. Active viewer streams are protected by the channel
                checker so other streams can still be analyzed.
            
        Returns:
            None if check can proceed, or a result dict if check should be skipped
        """
        udi = get_udi_manager()
        
        if provider_limit_override:
            logger.info(
                "Provider/profile slot guard override enabled for channel %s; "
                "active-viewer stream protection is handled before analysis",
                channel_name,
            )
            return None
        
        # Check if at least one stream can run (has an available profile)
        # This replaces the old account-level checking with profile-aware logic
        has_available_slot = False
        blocked_reasons = []
        
        for stream in streams:
            m3u_account = self._get_stream_m3u_account_id(stream)
            if not m3u_account:
                # Custom stream without M3U account - can always check
                has_available_slot = True
                break
            
            # Check if this stream can run using profile-aware checking.
            # If the helper is unavailable in a mocked environment, default to allowing checks.
            check_stream_can_run = getattr(udi, 'check_stream_can_run', None)
            if not callable(check_stream_can_run):
                has_available_slot = True
                break

            can_run_result = check_stream_can_run(stream)
            if not (isinstance(can_run_result, tuple) and len(can_run_result) == 2):
                has_available_slot = True
                break

            can_run, reason = can_run_result
            if can_run:
                has_available_slot = True
                break
            else:
                if reason and reason not in blocked_reasons:
                    blocked_reasons.append(reason)
        
        # If no stream has an available slot, skip the check
        if not has_available_slot:
            reason_str = "; ".join(blocked_reasons) if blocked_reasons else "All M3U account profiles are at capacity"
            logger.warning(f"Cannot check channel {channel_name}: {reason_str}")
            return {
                'dead_streams_count': 0,
                'revived_streams_count': 0,
                'skipped': True,
                'skip_reason': 'max_streams_reached',
                'reason_detail': reason_str
            }
        
        # At least one stream has an available slot, check can proceed
        return None
    
    def _get_resolution_product(self, stream_data: Dict) -> int:
        """Get resolution product (width * height) from stream data."""
        res = stream_data.get('resolution', '')
        if 'x' in str(res):
            try:
                width, height = map(int, str(res).split('x'))
                return width * height
            except: pass
        return 0

    # Removed _refine_sorted_streams in favor of lexicographical Sort Keys.

    def _run_connectivity_guard(
        self,
        phase: str,
        *,
        operation: str = 'destructive_write',
        channel_id: Optional[int] = None,
        channel_name: Optional[str] = None,
    ) -> ConnectivityCheckResult:
        """Run and record the fail-closed connectivity guard."""
        try:
            result = self.connectivity_guard.check(
                config=self.config.get('connectivity_guard', {}),
                dispatcharr_base_url=_get_base_url(),
                dispatcharr_headers_provider=_get_auth_headers,
                dispatcharr_auth_refresh_provider=_refresh_token,
                operation=operation,
            )
        except Exception as exc:
            logger.warning("Connectivity guard failed unexpectedly during %s: %s", phase, exc)
            result = ConnectivityCheckResult(
                ok=False,
                reason='connectivity_guard_error',
                message='Connectivity could not be verified',
                details={'phase': phase},
            )

        status = result.to_dict()
        status['phase'] = phase
        status['operation'] = operation
        if channel_id is not None:
            status['channel_id'] = channel_id
        if channel_name:
            status['channel_name'] = channel_name
        status['checked_at'] = datetime.now().isoformat()
        self.connectivity_guard_status = status
        return result

    def _maybe_refresh_stale_connectivity_guard(self, stream_checking_mode: bool) -> None:
        """Recheck old idle guard failures so recovered systems stop showing stale errors."""
        if stream_checking_mode:
            return

        current_status = self.connectivity_guard_status or {}
        if current_status.get('ok') is not False:
            return

        config = self.config.get('connectivity_guard', {}) or {}
        if config.get('enabled', True) is False:
            return

        checked_at = current_status.get('checked_at')
        if not checked_at:
            return

        try:
            last_checked = datetime.fromisoformat(checked_at)
        except (TypeError, ValueError):
            return

        interval = self._bounded_float(
            config.get('stale_recheck_interval_seconds', 60),
            default=60.0,
            minimum=10.0,
            maximum=3600.0,
        )
        if (datetime.now() - last_checked).total_seconds() < interval:
            return

        now = time.time()
        if now - self._last_connectivity_guard_recovery_probe_at < interval:
            return

        self._last_connectivity_guard_recovery_probe_at = now
        logger.info("Rechecking stale connectivity guard failure after %.0fs", interval)
        self._run_connectivity_guard(
            'stale_failure_recovery',
            operation='analysis',
        )

    @staticmethod
    def _bounded_float(value: Any, *, default: float, minimum: float, maximum: float) -> float:
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            numeric = default
        return max(minimum, min(maximum, numeric))

    def _connectivity_abort_payload(
        self,
        result: ConnectivityCheckResult,
        *,
        channel_id: Optional[int] = None,
        channel_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        payload = {
            'success': False,
            'error': 'connectivity_guard',
            'aborted': True,
            'skip_reason': 'connectivity_guard',
            'message': result.message,
            'connectivity_guard': result.to_dict(),
        }
        if channel_id is not None:
            payload['channel_id'] = channel_id
        if channel_name is not None:
            payload['channel_name'] = channel_name
        return payload

    def _fail_channel_for_connectivity(
        self,
        result: ConnectivityCheckResult,
        *,
        channel_id: int,
        channel_name: Optional[str] = None,
        queue_entry_token: Optional[int] = None,
    ) -> Dict[str, Any]:
        self.check_queue.mark_failed(
            channel_id,
            result.message,
            entry_token=queue_entry_token,
        )
        return self._connectivity_abort_payload(
            result,
            channel_id=channel_id,
            channel_name=channel_name,
        )

    def _require_quality_check_connectivity(
        self,
        *,
        phase: str,
        channel_id: Optional[int] = None,
        channel_name: Optional[str] = None,
        update_progress: bool = True,
        progress_context: Optional[Dict[str, Any]] = None,
    ) -> Optional[ConnectivityCheckResult]:
        """Return a failed result when the requested quality operation must abort."""
        destructive_phases = {
            'mark_dead_stream',
            'keep_dead_stream_marked',
            'channel_stream_update',
            'single_channel_validation_removal',
            'single_channel_matching_update',
            'automation_quality_preflight',
        }
        operation = (
            'destructive_write'
            if phase in destructive_phases
            else 'analysis'
        )
        result = self._run_connectivity_guard(
            phase,
            operation=operation,
            channel_id=channel_id,
            channel_name=channel_name,
        )
        if result.ok:
            return None
        progress_context = dict(progress_context or {})

        recoverable_phases = destructive_phases
        config = self.config.get('connectivity_guard', {}) or {}
        recovery_wait_seconds = self._bounded_float(
            config.get('recovery_wait_seconds', 240),
            default=240.0,
            minimum=0.0,
            maximum=600.0,
        )
        recovery_poll_seconds = self._bounded_float(
            config.get('recovery_poll_seconds', 10),
            default=10.0,
            minimum=1.0,
            maximum=120.0,
        )
        if phase in recoverable_phases and recovery_wait_seconds > 0:
            safe_channel_name = channel_name or (
                f"Channel {channel_id}" if channel_id is not None else "Quality check"
            )
            deadline = time.time() + recovery_wait_seconds
            first_failure = {
                'reason': result.reason,
                'message': result.message,
                'checked_at': datetime.now().isoformat(),
            }
            recovery_attempts = 0
            logger.warning(
                "Connectivity guard failed at %s for %s: %s; waiting up to %.0fs for recovery",
                phase,
                safe_channel_name,
                result.message,
                recovery_wait_seconds,
            )
            while time.time() < deadline and not self.abort_current_check.is_set():
                remaining = max(0.0, deadline - time.time())
                sleep_for = min(recovery_poll_seconds, remaining)
                recovery_attempts += 1
                self.connectivity_guard_status = {
                    **dict(self.connectivity_guard_status or {}),
                    'recovery': {
                        'active': True,
                        'first_failure': first_failure,
                        'attempts': recovery_attempts,
                        'remaining_seconds': round(remaining, 1),
                        'channel_id': channel_id,
                        'channel_name': safe_channel_name,
                    },
                }
                if update_progress and channel_id is not None:
                    try:
                        self.progress.update(
                            channel_id=channel_id,
                            channel_name=safe_channel_name,
                            current=0,
                            total=0,
                            status='waiting_connectivity',
                            step='Waiting for Dispatcharr API',
                            step_detail=(
                                f"{result.message}; retrying for up to {int(remaining)}s"
                            ),
                            **progress_context,
                        )
                    except Exception as exc:
                        logger.debug("Failed to publish connectivity recovery progress: %s", exc)
                if sleep_for > 0:
                    time.sleep(sleep_for)
                recovery_result = self._run_connectivity_guard(
                    f"{phase}_recovery",
                    operation=operation,
                    channel_id=channel_id,
                    channel_name=safe_channel_name,
                )
                if recovery_result.ok:
                    self.connectivity_guard_status['recovery'] = {
                        'active': False,
                        'first_failure': first_failure,
                        'attempts': recovery_attempts,
                        'remaining_seconds': round(max(0.0, deadline - time.time()), 1),
                        'channel_id': channel_id,
                        'channel_name': safe_channel_name,
                        'recovered': True,
                    }
                    logger.info(
                        "Connectivity guard recovered at %s for %s; continuing quality work",
                        phase,
                        safe_channel_name,
                    )
                    return None
                result = recovery_result
            self.connectivity_guard_status['recovery'] = {
                'active': False,
                'first_failure': first_failure,
                'attempts': recovery_attempts,
                'remaining_seconds': 0.0,
                'channel_id': channel_id,
                'channel_name': safe_channel_name,
                'recovered': False,
                'exhausted': not self.abort_current_check.is_set(),
            }

        self.abort_current_check.set()
        self._cancel_queueing = True
        safe_channel_name = channel_name or (f"Channel {channel_id}" if channel_id is not None else "Quality check")
        logger.error("Aborting quality check at %s: %s", phase, result.message)

        if update_progress and channel_id is not None:
            try:
                self.progress.update(
                    channel_id=channel_id,
                    channel_name=safe_channel_name,
                    current=0,
                    total=0,
                    status='aborted',
                    step='Connectivity check failed',
                    step_detail=result.message,
                    **progress_context,
                )
            except Exception as exc:
                logger.debug("Failed to publish connectivity abort progress: %s", exc)

        return result

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
    
    def _check_channel(
        self,
        channel_id: int,
        skip_batch_changelog: bool = False,
        forced_profile_id: Optional[str] = None,
        provider_limit_override: bool = False,
        run_mode: Optional[str] = None,
        is_single_channel_check: bool = False,
        force_check_override: Optional[bool] = None,
        force_check_generation: Optional[int] = None,
        batch_changelog_generation: Optional[int] = None,
        queue_entry_token: Optional[int] = None,
        expected_progress_generation: Optional[int] = None,
    ):
        """Check and reorder streams for a specific channel.
        
        Routes to either concurrent or sequential checking based on configuration.
        
        Args:
            channel_id: ID of the channel to check
            skip_batch_changelog: If True, don't add this check to the batch changelog
            provider_limit_override: If True, bypass provider/profile capacity
                skips while still protecting active viewers.
            run_mode: Optional progress context label for specialized callers.
            is_single_channel_check: If True, preserve single-channel progress
                semantics through the internal quality-analysis phases.
            force_check_override: Explicit force intent owned by this direct
                operation. ``None`` consumes the persistent worker-queue flag.
            batch_changelog_generation: Queue batch token used to reject
                changelog writes after that batch has been cleared.
            queue_entry_token: Exact queue activation identity used to reject
                stale completion after clear/requeue of the same channel.
            expected_progress_generation: Progress ownership captured by an
                outer single-channel operation before any long-running work.
        """
        progress_owner_active, progress_generation = (
            self._capture_operation_progress_generation(
                expected_progress_generation=expected_progress_generation,
            )
        )
        if not progress_owner_active:
            return self._abort_channel_check(
                channel_id,
                queue_entry_token=queue_entry_token,
            )

        connectivity_progress_context: Dict[str, Any] = {}
        if run_mode:
            connectivity_progress_context['run_mode'] = run_mode
        elif is_single_channel_check:
            connectivity_progress_context['run_mode'] = 'single_channel_check'
        if is_single_channel_check:
            connectivity_progress_context['is_single_channel_check'] = True
        if progress_generation is not None:
            connectivity_progress_context['expected_generation'] = (
                progress_generation
            )

        failed_connectivity = self._require_quality_check_connectivity(
            phase='quality_check_preflight',
            channel_id=channel_id,
            progress_context=connectivity_progress_context,
        )
        if failed_connectivity is not None:
            return self._fail_channel_for_connectivity(
                failed_connectivity,
                channel_id=channel_id,
                queue_entry_token=queue_entry_token,
            )

        concurrent_enabled = self.config.get('concurrent_streams.enabled', True)
        
        if concurrent_enabled:
            return self._check_channel_concurrent(
                channel_id,
                skip_batch_changelog=skip_batch_changelog,
                forced_profile_id=forced_profile_id,
                provider_limit_override=provider_limit_override,
                run_mode=run_mode,
                is_single_channel_check=is_single_channel_check,
                force_check_override=force_check_override,
                force_check_generation=force_check_generation,
                batch_changelog_generation=batch_changelog_generation,
                queue_entry_token=queue_entry_token,
                expected_progress_generation=progress_generation,
            )
        else:
            # Keep the user-visible sequential mode while retaining the same
            # provider/profile reservations and deferred bitrate-recheck flow
            # as the parallel checker. A single global probe slot guarantees
            # that every basis probe runs one at a time.
            return self._check_channel_concurrent(
                channel_id,
                skip_batch_changelog=skip_batch_changelog,
                forced_profile_id=forced_profile_id,
                provider_limit_override=provider_limit_override,
                run_mode=run_mode,
                is_single_channel_check=is_single_channel_check,
                global_limit_override=1,
                force_check_override=force_check_override,
                force_check_generation=force_check_generation,
                batch_changelog_generation=batch_changelog_generation,
                queue_entry_token=queue_entry_token,
                expected_progress_generation=progress_generation,
            )

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

    def _get_active_viewer_protected_stream_ids(
        self,
        channel_id: int,
        streams: List[Dict[str, Any]],
        udi: Any,
    ) -> set:
        """Resolve watched stream IDs for this channel and intersect with assigned streams."""
        assigned_ids = set()
        for stream in streams:
            if not isinstance(stream, dict) or stream.get('id') is None:
                continue
            try:
                assigned_ids.add(int(stream.get('id')))
            except (TypeError, ValueError):
                continue
        if not assigned_ids:
            return set()

        protected_ids = set()
        try:
            get_active = getattr(udi, 'get_active_stream_ids_for_channel', None)
            if callable(get_active):
                protected_ids.update(get_active(channel_id) or set())
        except Exception as exc:
            logger.debug("Could not resolve active stream IDs for channel %s: %s", channel_id, exc)

        if not protected_ids:
            try:
                active_status = getattr(udi, 'is_channel_active', lambda *_args: False)(channel_id)
                get_playing = getattr(udi, 'get_playing_stream_ids', None)
                if active_status is True and callable(get_playing):
                    protected_ids.update(get_playing() or set())
            except Exception as exc:
                logger.debug("Could not fall back to playing stream IDs for channel %s: %s", channel_id, exc)

        coerced = set()
        for stream_id in protected_ids:
            try:
                coerced.add(int(stream_id))
            except (TypeError, ValueError):
                continue
        return coerced.intersection(assigned_ids)

    @staticmethod
    def _merge_protected_stream_order(
        original_stream_ids: List[int],
        reordered_ids: List[int],
        protected_stream_ids: set,
    ) -> List[int]:
        """Keep protected stream IDs at their original indexes while reordering the rest."""
        protected_stream_ids = set(protected_stream_ids or set())
        if not protected_stream_ids:
            return reordered_ids

        remaining = [
            stream_id
            for stream_id in reordered_ids
            if stream_id not in protected_stream_ids
        ]
        result: List[int] = []
        used = set()

        for original_id in original_stream_ids:
            if original_id in protected_stream_ids:
                if original_id not in used:
                    result.append(original_id)
                    used.add(original_id)
                continue
            while remaining and remaining[0] in used:
                remaining.pop(0)
            if remaining:
                next_id = remaining.pop(0)
                result.append(next_id)
                used.add(next_id)

        for stream_id in remaining:
            if stream_id not in used:
                result.append(stream_id)
                used.add(stream_id)

        for original_id in original_stream_ids:
            if original_id in protected_stream_ids and original_id not in used:
                result.append(original_id)
                used.add(original_id)

        return result

    def _active_viewer_skipped_streams(
        self,
        streams: List[Dict[str, Any]],
        protected_stream_ids: set,
    ) -> List[Dict[str, Any]]:
        skipped = []
        for stream in streams:
            stream_id = stream.get('id') if isinstance(stream, dict) else None
            try:
                protected_id = int(stream_id)
            except (TypeError, ValueError):
                continue
            if protected_id not in protected_stream_ids:
                continue
            skipped.append({
                'id': protected_id,
                'stream_id': protected_id,
                'name': stream.get('name', f"Stream {protected_id}"),
                'stream_name': stream.get('name', f"Stream {protected_id}"),
                'skip_reason': 'active_viewer_protected',
                'reason_detail': 'active_viewer_protected',
                'status': 'active_viewer_protected',
            })
        return skipped
    
    def _check_channel_concurrent(
        self,
        channel_id: int,
        skip_batch_changelog: bool = False,
        target_stream_ids: Optional[List[str]] = None,
        forced_profile_id: Optional[str] = None,
        provider_limit_override: bool = False,
        run_mode: Optional[str] = None,
        is_single_channel_check: bool = False,
        global_limit_override: Optional[int] = None,
        force_check_override: Optional[bool] = None,
        force_check_generation: Optional[int] = None,
        batch_changelog_generation: Optional[int] = None,
        queue_entry_token: Optional[int] = None,
        expected_progress_generation: Optional[int] = None,
    ):
        """Check and reorder streams for a specific channel using parallel thread pool.
        
        Args:
            channel_id: ID of the channel to check
            skip_batch_changelog: If True, don't add this check to the batch changelog
            target_stream_ids: Optional list of stream IDs. If provided, ONLY these
                               streams will be checked, bypassing all other logic.
            provider_limit_override: If True, bypass provider/profile capacity
                                     skips while still protecting active viewers.
            run_mode: Optional progress context label for specialized callers.
            is_single_channel_check: If True, keep Current Progress in
                                     single-channel mode for every phase update.
            global_limit_override: Optional per-channel probe limit. Sequential
                                   mode uses one while retaining smart capacity
                                   reservations and the two-pass bitrate flow.
            force_check_override: Explicit force intent owned by this direct
                                  operation. ``None`` consumes the persistent
                                  worker-queue flag.
            batch_changelog_generation: Queue batch token used to reject
                                  changelog writes after that batch is cleared.
            queue_entry_token: Exact queue activation identity used to reject
                                  stale terminal writes after clear/requeue.
            expected_progress_generation: Progress ownership captured by an
                                  outer entry before connectivity preflight.
        """
        import time as time_module
        from apps.stream.concurrent_stream_limiter import get_smart_scheduler, get_account_limiter, initialize_account_limits

        # One concurrent channel check owns the progress generation that was
        # current when it started.  A clear is an ownership boundary: once an
        # operator or scheduler clears this run, none of its early, heartbeat,
        # stream-detail, or late phase publications may recreate stale progress.
        progress_owner_active, progress_generation = (
            self._capture_operation_progress_generation(
                expected_progress_generation=expected_progress_generation,
            )
        )
        if not progress_owner_active:
            return self._abort_channel_check(
                channel_id,
                queue_entry_token=queue_entry_token,
            )

        def update_run_progress(**progress_fields):
            if progress_generation is not None:
                progress_fields['expected_generation'] = progress_generation
            return self.progress.update(**progress_fields)
        
        start_time = time_module.time()
        log_function_call(logger, "_check_channel_concurrent", channel_id=channel_id)
        
        log_state_change(logger, f"channel_{channel_id}", "queued", "checking")
        logger.info(f"=" * 80)
        logger.info(f"Checking channel {channel_id} (parallel mode)")
        logger.info(f"=" * 80)
        
        # Default to False (safe: do not remove) until the profile is resolved below.
        # If profile resolution fails, streams are left in place rather than silently removed.
        dead_stream_removal_enabled = False

        # Get effective profile for this channel
        stream_limit = 0
        allow_revive = True
        grace_period = False
        loop_check_enabled = False
        blank_check_enabled = False
        freeze_check_enabled = False
        loop_penalty = 0.0
        priority_m3u_ids = []
        priority_mode = 'absolute'
        scoring_weights = None
        batch_config = self.config.get('batch_operations', {})
        batch_enabled = batch_config.get('enabled', True)
        batch_size = batch_config.get('batch_size', 10)
        batch_stats_list = []
        # Initialised here; built from the resolved profile below so every
        # _is_stream_dead() call uses the correct profile including forced_profile_id.
        _threshold_config: Dict[str, Any] = {}
        profile: Optional[Dict[str, Any]] = None

        try:
            automation_config = get_automation_config_manager()

            # Fetch channel data to get group_id (might be fetched already but just in case)
            udi = get_udi_manager()
            channel = udi.get_channel_by_id(channel_id)
            group_id = channel.get('channel_group_id') if channel else None

            # If a profile was explicitly selected (via ProfilePickerDialog), use it
            # directly so all checking parameters (weights, limits, revive, loop detection)
            # reflect the user's intent rather than whichever period is currently active.
            if forced_profile_id:
                profile = automation_config.get_profile(forced_profile_id)
                if not profile:
                    logger.warning(
                        f"forced_profile_id={forced_profile_id!r} not found in _check_channel "
                        f"— falling back to active period resolution"
                    )
            if not forced_profile_id or not profile:
                config = automation_config.get_effective_configuration(channel_id, group_id)
                profile = config.get('profile') if config else None
            if profile:
                profile_stream_checking = profile.get('stream_checking', {})
                stream_limit = profile_stream_checking.get('stream_limit', 0)
                allow_revive = profile_stream_checking.get('allow_revive', True)
                priority_m3u_ids = profile_stream_checking.get('m3u_priority', [])
                priority_mode = profile_stream_checking.get('m3u_priority_mode', 'absolute')
                grace_period = profile_stream_checking.get('grace_period', False)
                loop_check_enabled = profile_stream_checking.get('loop_check_enabled', False)
                blank_check_enabled = profile_stream_checking.get('blank_check_enabled', False)
                freeze_check_enabled = profile_stream_checking.get('freeze_check_enabled', False)
                profile_remove_dead_streams = profile_stream_checking.get('remove_dead_streams')
                if isinstance(profile_remove_dead_streams, bool):
                    dead_stream_removal_enabled = profile_remove_dead_streams
                elif profile_remove_dead_streams is not None:
                    logger.warning(
                        "Ignoring non-boolean stream_checking.remove_dead_streams for channel %s",
                        channel_id,
                    )
                scoring_weights = profile.get('scoring_weights', None)
                loop_penalty = float(
                    (scoring_weights or {}).get('loop_penalty', 0.0)
                )
                # Clamp to valid range: -0.25 to 0.0
                loop_penalty = max(-0.25, min(0.0, loop_penalty))

                # Build threshold config once from the resolved profile so every
                # _is_stream_dead() call uses the correct profile — including
                # when a forced_profile_id was selected via the picker.
                _threshold_config = self._build_threshold_config_from_profile(profile_stream_checking)
                logger.debug(f"Threshold config for channel {channel_id}: {_threshold_config}")

                # Also check if checking is enabled at all for this profile
                if not profile_stream_checking.get('enabled', False):
                    logger.info(f"Stream checking disabled by profile for channel {channel_id}")
                    self._complete_channel_check(
                        channel_id,
                        queue_entry_token=queue_entry_token,
                    )
                    return {
                        'dead_streams_count': 0,
                        'revived_streams_count': 0,
                        'skipped': True,
                        'skip_reason': 'profile_disabled'
                    }
        except Exception as e:
            logger.warning(f"Failed to load profile settings for channel {channel_id}: {e}")
            _threshold_config = {}
        profile_progress_context = self._automation_profile_progress_context(
            profile,
            forced_profile_id=forced_profile_id,
        )
        profile_progress_context['run_mode'] = run_mode or (
            'single_channel_check' if is_single_channel_check else 'stream_checker'
        )
        if is_single_channel_check:
            profile_progress_context['is_single_channel_check'] = True
        if progress_generation is not None:
            # Nested connectivity and loop-probe publishers receive this same
            # ownership fence through their copied progress context.
            profile_progress_context['expected_generation'] = progress_generation

        self.checking = True
        try:
            # Get channel information from UDI
            logger.debug(f"Updating progress for channel {channel_id} initialization")
            update_run_progress(
                channel_id=channel_id,
                channel_name='Loading...',
                current=0,
                total=0,
                status='initializing',
                step='Fetching channel info',
                step_detail='Retrieving channel data from UDI',
                **profile_progress_context,
            )
            
            udi = get_udi_manager()
            base_url = _get_base_url()
            logger.debug(f"Fetching channel data for channel {channel_id} from UDI")
            channel_data = udi.get_channel_by_id(channel_id)
            if not channel_data:
                logger.error(f"UDI returned None for channel {channel_id}")
                raise Exception(f"Could not fetch channel {channel_id}")
            
            channel_name = channel_data.get('name', f'Channel {channel_id}')
            abort_result = self._abort_channel_check_if_requested(
                channel_id,
                channel_name,
                queue_entry_token=queue_entry_token,
            )
            if abort_result:
                return abort_result
            
            # Get streams for this channel
            update_run_progress(
                channel_id=channel_id,
                channel_name=channel_name,
                current=0,
                total=0,
                status='initializing',
                step='Fetching streams',
                step_detail=f'Loading streams for {channel_name}',
                **profile_progress_context,
            )
            
            streams = fetch_channel_streams(channel_id)
            abort_result = self._abort_channel_check_if_requested(
                channel_id,
                channel_name,
                queue_entry_token=queue_entry_token,
            )
            if abort_result:
                return abort_result

            if not streams or len(streams) == 0:
                logger.info(f"No streams found for channel {channel_name}")
                visibility_authorized, visibility_result = (
                    self._run_channel_side_effect_if_authorized(
                        channel_id,
                        queue_entry_token,
                        lambda: self._apply_channel_visibility_after_check(
                            channel_data,
                            good_streams_count=0,
                            dead_streams_count=0,
                            revived_streams_count=0,
                            total_streams=0,
                            profile=profile,
                        ),
                    )
                )
                if not visibility_authorized:
                    return self._abort_channel_check(
                        channel_id,
                        channel_name,
                        queue_entry_token=queue_entry_token,
                    )
                if self.changelog and not skip_batch_changelog:
                    batch_entry = {
                        'channel_id': channel_id,
                        'channel_name': channel_name,
                        'logo_url': f"/api/logos/{channel_data.get('logo_id')}" if channel_data.get('logo_id') else None,
                        'total_streams': 0,
                        'streams_analyzed': 0,
                        'dead_streams_detected': 0,
                        'streams_revived': 0,
                        'avg_resolution': 'N/A',
                        'avg_bitrate': 'N/A',
                        'avg_fps': 'N/A',
                        'success': True,
                        'stream_stats': [],
                    }
                    visibility_changelog = self._visibility_changelog_result(visibility_result)
                    if visibility_changelog:
                        batch_entry['channel_visibility'] = visibility_changelog
                    self._add_to_batch_changelog(
                        batch_entry,
                        batch_generation=batch_changelog_generation,
                    )
                self._complete_channel_check(
                    channel_id,
                    lambda: self.update_tracker.mark_channel_checked(
                        channel_id,
                        stream_count=0,
                        checked_stream_ids=[],
                    ),
                    queue_entry_token=queue_entry_token,
                )
                return {
                    'dead_streams_count': 0,
                    'revived_streams_count': 0,
                    'channel_visibility': visibility_result,
                }
            
            logger.info(f"Found {len(streams)} streams for channel {channel_name}")
            
            # Check if channel has active viewers or if its playlist has reached max concurrent streams
            limit_check_result = self._check_channel_limits(
                channel_id,
                channel_name,
                streams,
                provider_limit_override=provider_limit_override,
            )
            if limit_check_result is not None:
                self._complete_channel_check(
                    channel_id,
                    lambda: self.update_tracker.mark_channel_checked(channel_id),
                    queue_entry_token=queue_entry_token,
                )
                return limit_check_result
            
            # Check if this is a force check (bypasses 2-hour immunity)
            if force_check_override is None:
                force_check, owned_force_generation = (
                    self.update_tracker.get_force_check_state(channel_id)
                )
            else:
                force_check = bool(force_check_override)
                owned_force_generation = force_check_generation
            # NOTE: force_check controls immunity bypass ONLY (all streams are re-analyzed).
            # It no longer overrides allow_revive — the profile flag is the sole authority
            # for whether a previously-dead stream can be promoted back to active (Bug 5 fix).
            
            # Get list of already checked streams to avoid re-analyzing
            checked_stream_info = self.update_tracker.updates.get('channels', {}).get(str(channel_id), {})
            checked_stream_ids = checked_stream_info.get('checked_stream_ids', [])
            last_check_str = checked_stream_info.get('last_check')
            
            # Check if immunity period (2 hours) has expired
            immunity_expired = False
            if last_check_str and grace_period:
                try:
                    last_check_time = datetime.fromisoformat(last_check_str)
                    if (datetime.now() - last_check_time).total_seconds() > 7200:
                        immunity_expired = True
                        logger.info(f"Immunity period (2 hours) expired for channel {channel_name} - will re-analyze all streams")
                except Exception as e:
                    logger.warning(f"Failed to parse last_check timestamp for channel {channel_id}: {e}")
            
            current_stream_ids = [s['id'] for s in streams]
            assigned_stream_ids = self._get_channel_assignment_stream_ids(
                channel_id,
                channel_data,
                udi,
                fallback_stream_ids=current_stream_ids,
                refresh_from_dispatcharr=not dead_stream_removal_enabled,
            )
            protected_active_stream_ids = self._get_active_viewer_protected_stream_ids(
                channel_id,
                streams,
                udi,
            )
            active_viewer_skipped_streams = self._active_viewer_skipped_streams(
                streams,
                protected_active_stream_ids,
            )
            if protected_active_stream_ids:
                logger.info(
                    "Channel %s has %s active viewer-protected stream(s); "
                    "their slots will be preserved while other streams are checked",
                    channel_name,
                    len(protected_active_stream_ids),
                )
            
            # Identify which streams need analysis (new or unchecked)
            
            if target_stream_ids is not None:
                # Targeted check mode: Evaluates newly assigned streams ONLY
                streams_to_check = [
                    s for s in streams
                    if str(s['id']) in [str(ts) for ts in target_stream_ids]
                    and s.get('id') not in protected_active_stream_ids
                ]
                streams_already_checked = [
                    s for s in streams
                    if str(s['id']) not in [str(ts) for ts in target_stream_ids]
                    and s.get('id') not in protected_active_stream_ids
                ]
                logger.info(f"Targeted stream check: evaluating {len(streams_to_check)} specific newly assigned streams")
                
            elif force_check or (grace_period and immunity_expired) or (not grace_period and not force_check):
                # If grace period is DISABLED, we check everything every time unless it's a "needs_check" trigger?
                # Actually, if grace_period is False, users probably expect regular checks.
                # However, we only get here if the worker picked up the channel.
                # If it's a force check or immunity expired, check all.
                # If grace period is OFF, we also check everything if we are running.
                streams_to_check = [
                    s for s in streams
                    if s.get('id') not in protected_active_stream_ids
                ]
                streams_already_checked = []
                
                if force_check:
                    logger.info(f"Force check enabled: analyzing all {len(streams)} streams (bypassing 2-hour immunity)")
                    if force_check_override is None or owned_force_generation is not None:
                        self.update_tracker.clear_force_check(
                            channel_id,
                            expected_generation=owned_force_generation,
                        )
                elif grace_period and immunity_expired:
                    logger.info(f"Grace period (2h) expired: re-analyzing {len(streams)} streams for {channel_name}")
                elif not grace_period:
                    logger.info(f"Grace period disabled for profile: analyzing all {len(streams)} streams")
            else:
                # Normal incremental check: only analyze new streams
                streams_to_check = [
                    s for s in streams
                    if s['id'] not in checked_stream_ids
                    and s.get('id') not in protected_active_stream_ids
                ]
                streams_already_checked = [
                    s for s in streams
                    if s['id'] in checked_stream_ids
                    and s.get('id') not in protected_active_stream_ids
                ]
                
                if streams_to_check:
                    logger.info(f"Found {len(streams_to_check)} new/unchecked streams (out of {len(streams)} total)")
                else:
                    logger.info(f"All {len(streams)} streams have been recently checked (within 2h immunity), using cached scores")
                    
                    # Optimization: Skip check entirely if all conditions are met:
                    # 1. No new streams to analyze (all have been checked)
                    # 2. Stream count matches previous check (no additions/deletions)
                    # 3. Set of stream IDs is identical (no stream replacements)
                    previous_stream_count = len(checked_stream_ids)
                    current_stream_count = len(current_stream_ids)
                    
                    if (current_stream_count == previous_stream_count and 
                        set(current_stream_ids) == set(checked_stream_ids)):
                        logger.info(f"Channel {channel_name} unchanged since last check - skipping reorder")
                        # Update timestamp but keep existing checked_stream_ids
                        self._complete_channel_check(
                            channel_id,
                            lambda: self.update_tracker.mark_channel_checked(
                                channel_id,
                                stream_count=current_stream_count,
                                checked_stream_ids=checked_stream_ids
                            ),
                            queue_entry_token=queue_entry_token,
                        )
                        # Best effort to reconstruct stats for skipped/cached streams
                        cached_stats = []
                        for s in streams_already_checked:
                            # Try to find existing stats if available in stream object
                            # Otherwise use placeholders
                            extracted_stats = extract_stream_stats(s)
                            formatted_stats = format_stream_stats_for_display(extracted_stats)
                            
                            cached_for_score = {
                                'stream_id': s.get('id'),
                                'stream_name': s.get('name'),
                                'stream_url': s.get('url'),
                                'bitrate_kbps': extracted_stats.get('bitrate_kbps'),
                                'scoring_bitrate_kbps': (
                                    self._previous_stream_bitrate(s)
                                    if extracted_stats.get('bitrate_kbps') is None
                                    else None
                                ),
                                'resolution': extracted_stats.get('resolution'),
                                'fps': extracted_stats.get('fps'),
                                'video_codec': extracted_stats.get('video_codec'),
                                'audio_codec': extracted_stats.get('audio_codec'),
                                'hdr_format': extracted_stats.get('hdr_format'),
                                'status': 'cached'
                            }
                            
                            temp_score = self._calculate_stream_score(cached_for_score, priority_m3u_ids, priority_mode, scoring_weights)

                            stat = {
                                'stream_id': s.get('id'),
                                'stream_name': s.get('name'),
                                'resolution': formatted_stats['resolution'],
                                'fps': formatted_stats['fps'],
                                'video_codec': formatted_stats['video_codec'],
                                'bitrate': formatted_stats['bitrate'],
                                'm3u_account': self._get_m3u_account_name(s.get('id'), udi) if hasattr(self, '_get_m3u_account_name') else 'N/A',
                                'score': temp_score
                            }
                            cached_stats.append(stat)

                        return {
                            'dead_streams_count': 0,
                            'revived_streams_count': 0,
                            'dead_streams': [],
                            'revived_streams': [],
                            'skipped_streams_count': len(streams_already_checked) + len(active_viewer_skipped_streams),
                            'skipped_streams': (
                                [{'id': s['id'], 'name': s.get('name', f"Stream {s['id']}")} for s in streams_already_checked]
                                + active_viewer_skipped_streams
                            ),
                            'checked_streams': cached_stats
                        }
                    else:
                        logger.info(f"Channel composition changed (prev: {previous_stream_count}, curr: {current_stream_count}) - will reorder")
            
            # Streams that are actively analyzed in this pass. Used to gate
            # dead_stream_ids mutations — only streams checked in THIS pass may
            # be added to dead_stream_ids. Unchecked streams retain their tracker
            # state unchanged until a future run evaluates them directly.
            checked_stream_id_set = {s['id'] for s in streams_to_check}

            # Get configuration for analysis
            analysis_params = self.config.get('stream_analysis', {})
            configured_global_limit = self.config.get('concurrent_streams.global_limit', 10)
            global_limit = (
                max(1, int(global_limit_override))
                if global_limit_override is not None
                else configured_global_limit
            )
            stagger_delay = (
                0
                if global_limit_override is not None
                else self.config.get('concurrent_streams.stagger_delay', 1.0)
            )
            
            # Invalidate old provider authority before fetching a fresh UDI
            # inventory. Empty or malformed snapshots must stop this channel
            # before the smart scheduler can invoke an analyzer.
            account_limiter = get_account_limiter()
            if not self._initialize_provider_probe_account_inventory(
                udi=udi,
                limiter=account_limiter,
                initialize_account_limits=initialize_account_limits,
                operation_label='Concurrent channel stream probe',
            ):
                raise RuntimeError(
                    'Provider account inventory unavailable for channel probes'
                )
            
            # Initialize smart scheduler with account-aware limiting
            smart_scheduler = get_smart_scheduler(global_limit=global_limit)
            
            # Prepare for concurrent execution
            analyzed_streams = []
            dead_stream_ids = set()  # Use set for O(1) lookups
            revived_stream_ids = []
            preempted_stream_ids = set()
            total_streams = len(streams_to_check)
            completed_count = [0]  # Use list for mutable closure
            
            # Dict to keep track of the stream details throughout the analysis
            stream_statuses = {
                s['id']: {
                    'id': s['id'],
                    'name': s.get('name', f"Stream {s['id']}"),
                    'status': 'pending',
                    'm3u_account_id': self._get_stream_m3u_account_id(s),
                    'm3u_account': self._get_m3u_account_name(s.get('id'), udi) if hasattr(self, '_get_m3u_account_name') else 'N/A'
                }
                for s in streams_to_check
            }
            # Worker callbacks and the heartbeat publish this shared mapping
            # concurrently. Every transition is committed under this lock and
            # every publication receives a deep snapshot, so a released or
            # partially populated profile reservation can never escape.
            stream_statuses_lock = threading.RLock()
            stream_status_revision = [0]
            last_published_stream_status_revision = [0]
            stream_status_publish_lock = threading.Lock()
            streams_by_id = {
                s.get('id'): s
                for s in streams_to_check
                if s.get('id') is not None
            }

            profile_slot_account_ids = sorted({
                account_id
                for account_id in (self._get_stream_m3u_account_id(s) for s in streams_to_check)
                if account_id not in (None, '')
            }, key=lambda value: str(value))

            def build_provider_profile_slots():
                snapshots = {}
                run_mode_name = str(profile_progress_context.get('run_mode') or '').lower()
                checking_context_key = (
                    'teamarr_preflight'
                    if run_mode_name == 'teamarr_preflight'
                    else 'quality_checks'
                )
                limiter = get_account_limiter()
                for account_id in profile_slot_account_ids:
                    try:
                        slots = limiter.get_profile_slot_snapshot(account_id)
                        for slot in slots:
                            try:
                                checking_count = int(slot.get('checking') or 0)
                            except (TypeError, ValueError):
                                checking_count = 0
                            if checking_count > 0 and checking_context_key not in slot:
                                slot[checking_context_key] = checking_count
                    except Exception as exc:
                        logger.debug(
                            "Could not build profile slot snapshot for account %s: %s",
                            account_id,
                            exc,
                        )
                        slots = []
                    if slots:
                        snapshots[str(account_id)] = slots
                return snapshots

            def capture_stream_statuses():
                with stream_statuses_lock:
                    stream_status_revision[0] += 1
                    return (
                        stream_status_revision[0],
                        deepcopy(list(stream_statuses.values())),
                    )

            def publish_stream_status_progress(
                revision,
                streams_snapshot,
                **progress_fields,
            ):
                # Snapshot creation and database publication happen on different
                # threads. Serialize writes and reject a snapshot overtaken by a
                # newer transition so an old Profile A row cannot overwrite a
                # later wait/clear or Profile B row.
                # The status lock is also the scheduler's capacity-transition
                # boundary. Take it before the publication lock so heartbeat and
                # worker publications cannot invert the scheduler callback order.
                with stream_statuses_lock:
                    if self.abort_current_check.is_set():
                        return False
                    with stream_status_publish_lock:
                        if (
                            revision != stream_status_revision[0]
                            or revision <= last_published_stream_status_revision[0]
                        ):
                            return False
                        last_published_stream_status_revision[0] = revision
                        return bool(update_run_progress(
                            streams_detail=streams_snapshot,
                            provider_profile_slots=build_provider_profile_slots(),
                            **progress_fields,
                        ))

            def apply_reserved_profile_progress(stream_status, profile):
                updated_status = dict(stream_status)
                for field in (
                    'reserved_profile_id',
                    'reserved_profile_name',
                    'reserved_profile_limit',
                ):
                    updated_status.pop(field, None)
                if isinstance(profile, dict):
                    raw_effective_limit = getattr(profile, 'effective_limit', None)
                    if raw_effective_limit is None:
                        raw_effective_limit = profile.get('max_streams', 0)
                    try:
                        effective_limit = max(0, int(raw_effective_limit or 0))
                    except (TypeError, ValueError):
                        effective_limit = 0
                    updated_status['reserved_profile_id'] = profile.get('id')
                    updated_status['reserved_profile_name'] = (
                        profile.get('name') or f"Profile {profile.get('id')}"
                    )
                    updated_status['reserved_profile_limit'] = effective_limit
                return updated_status
            
            # Start callback for parallel checker
            def start_callback(stream, profile=None):
                stream_id = stream.get('id')
                with stream_statuses_lock:
                    if stream_id not in stream_statuses:
                        return
                    updated_status = dict(stream_statuses[stream_id])
                    updated_status['status'] = 'checking'
                    updated_status['started_at'] = datetime.now().isoformat()
                    self._clear_active_stream_reason(updated_status)
                    stream_statuses[stream_id] = apply_reserved_profile_progress(
                        updated_status,
                        profile,
                    )
                    status_revision, streams_snapshot = capture_stream_statuses()
                    current_completed = completed_count[0]
                publish_stream_status_progress(
                    status_revision,
                    streams_snapshot,
                    channel_id=channel_id,
                    channel_name=channel_name,
                    current=current_completed,
                    total=total_streams,
                    current_stream=stream.get('name', 'Unknown'),
                    status='analyzing',
                    step='Analyzing streams with account limits',
                    step_detail=f'Started checking {stream.get("name", "Unknown")}',
                    stream_duration=analysis_params.get('ffmpeg_duration', 30),
                    **profile_progress_context,
                )
            
            def apply_progress_callback(completed, total, result):
                stream_name = result.get('stream_name', 'Unknown')
                stream_id = result.get('stream_id')

                with stream_statuses_lock:
                    completed_count[0] = completed
                    if stream_id in stream_statuses:
                        stream_status = dict(stream_statuses[stream_id])
                        if result.get('provider_limit_skipped'):
                            reason_detail = result.get('reason_detail')
                            skipped_reason = result.get('skipped_reason') or reason_detail
                            stream_status['status'] = (
                                'viewer_preempted'
                                if reason_detail == 'viewer_preempted'
                                else 'provider_limit_wait_timeout'
                            )
                            stream_status['reason_detail'] = skipped_reason
                            stream_status['quality_reason'] = 'provider_capacity'
                            stream_status['quality_reason_detail'] = skipped_reason
                            stream_status['quality_reason_context'] = {}
                            stream_status['score'] = None
                        elif result.get('status') == 'ERROR':
                            stream_status['status'] = 'error'
                            stream_status['score'] = 0.0
                            stream_status['reason_detail'] = result.get('quality_reason_detail') or 'error'
                            stream_status['quality_reason'] = result.get('quality_reason') or 'offline'
                            stream_status['quality_reason_detail'] = result.get('quality_reason_detail') or 'error'
                            stream_status['quality_reason_context'] = result.get('quality_reason_context') or {
                                'stage': 'stream analysis',
                                'message': result.get('error_message') or 'Stream analysis worker returned no result',
                            }
                        else:
                            self._apply_previous_bitrate_fallback(
                                result,
                                streams_by_id.get(stream_id),
                            )
                            temp_score = self._calculate_stream_score(
                                result,
                                priority_m3u_ids,
                                priority_mode,
                                scoring_weights,
                            )
                            dead_result = self._is_stream_dead(
                                result,
                                channel_id,
                                threshold_config=_threshold_config,
                            )
                            self._apply_quality_classification(result, dead_result)
                            is_dead, dead_reason = dead_result
                            dead_reason_detail = getattr(
                                dead_result,
                                'reason_detail',
                                dead_reason,
                            )
                            dead_reason_context = getattr(
                                dead_result,
                                'details',
                                {},
                            ) or {}

                            if is_dead:
                                stream_status['status'] = (
                                    dead_reason
                                    if dead_reason in ('low_quality', 'blank', 'freeze')
                                    else 'dead'
                                )
                                stream_status['score'] = 0.0
                                stream_status['reason_detail'] = dead_reason_detail
                                stream_status['quality_reason'] = dead_reason
                                stream_status['quality_reason_detail'] = dead_reason_detail
                                stream_status['quality_reason_context'] = dead_reason_context
                                stream_status['resolution'] = result.get('resolution', '0x0')
                                stream_status['video_codec'] = result.get('video_codec', 'N/A')
                                stream_status['fps'] = result.get('fps', 0)
                                stream_status['bitrate'] = result.get('bitrate_kbps')
                                stream_status['hdr_format'] = result.get('hdr_format')
                            else:
                                if self._has_incomplete_bitrate_measurement(result):
                                    self._apply_incomplete_bitrate_status(
                                        stream_status,
                                        result,
                                    )
                                else:
                                    stream_status['status'] = 'completed'
                                    stream_status['quality_reason'] = 'none'
                                    stream_status['quality_reason_detail'] = 'none'
                                    stream_status['quality_reason_context'] = {}
                                stream_status['score'] = temp_score
                                stream_status['resolution'] = result.get('resolution', '0x0')
                                stream_status['video_codec'] = result.get('video_codec', 'N/A')
                                stream_status['fps'] = result.get('fps', 0)
                                stream_status['bitrate'] = result.get('bitrate_kbps')
                                stream_status['hdr_format'] = result.get('hdr_format')
                        stream_statuses[stream_id] = stream_status
                    status_revision, streams_snapshot = capture_stream_statuses()
                
                # Update progress
                publish_stream_status_progress(
                    status_revision,
                    streams_snapshot,
                    channel_id=channel_id,
                    channel_name=channel_name,
                    current=completed,
                    total=total,
                    current_stream=stream_name,
                    status='analyzing',
                    step='Analyzing streams with account limits',
                    step_detail=f'Completed {completed}/{total}',
                    stream_duration=analysis_params.get('ffmpeg_duration', 30),
                    **profile_progress_context,
                )

            def progress_callback(completed, total, result):
                try:
                    return apply_progress_callback(completed, total, result)
                except Exception:
                    # Capacity has already been released when this callback runs.
                    # Even if scoring/classification or its first publication
                    # fails, commit a non-active row and a fresh revision so no
                    # later heartbeat can revive the old checking snapshot.
                    stream_id = result.get('stream_id')
                    stream_name = result.get('stream_name', 'Unknown')
                    logger.exception(
                        "Failing stream %s closed after progress callback error",
                        stream_id,
                    )
                    fallback_applied = False
                    with stream_statuses_lock:
                        completed_count[0] = completed
                        if stream_id in stream_statuses:
                            stream_status = dict(stream_statuses[stream_id])
                            if stream_status.get('status') not in {
                                'completed',
                                'incomplete_bitrate',
                                'provider_limit_wait_timeout',
                                'viewer_preempted',
                                'error',
                                'dead',
                                'blank',
                                'freeze',
                                'low_quality',
                                'loop_detected',
                            }:
                                fallback_applied = True
                                stream_status['status'] = 'error'
                                stream_status['score'] = 0.0
                                stream_status['reason_detail'] = (
                                    'progress_callback_error'
                                )
                                stream_status['quality_reason'] = 'offline'
                                stream_status['quality_reason_detail'] = 'error'
                                stream_status['quality_reason_context'] = {
                                    'stage': 'stream progress',
                                    'message': (
                                        'Stream progress finalization failed'
                                    ),
                                }
                            stream_statuses[stream_id] = stream_status
                        status_revision, streams_snapshot = capture_stream_statuses()

                    return publish_stream_status_progress(
                        status_revision,
                        streams_snapshot,
                        channel_id=channel_id,
                        channel_name=channel_name,
                        current=completed,
                        total=total,
                        current_stream=stream_name,
                        status='analyzing',
                        step='Analyzing streams with account limits',
                        step_detail=(
                            f'Closed failed progress {completed}/{total}'
                            if fallback_applied
                            else f'Republished terminal progress {completed}/{total}'
                        ),
                        stream_duration=analysis_params.get('ffmpeg_duration', 30),
                        **profile_progress_context,
                    )

            def defer_callback(stream, reason):
                stream_id = stream.get('id')
                with stream_statuses_lock:
                    if stream_id not in stream_statuses:
                        return
                    updated_status = dict(stream_statuses[stream_id])
                    updated_status['status'] = 'waiting_provider_limit'
                    updated_status['reason_detail'] = reason
                    stream_statuses[stream_id] = apply_reserved_profile_progress(
                        updated_status,
                        None,
                    )
                    status_revision, streams_snapshot = capture_stream_statuses()
                    current_completed = completed_count[0]
                publish_stream_status_progress(
                    status_revision,
                    streams_snapshot,
                    channel_id=channel_id,
                    channel_name=channel_name,
                    current=current_completed,
                    total=total_streams,
                    current_stream=stream.get('name', 'Unknown'),
                    status='analyzing',
                    step='Analyzing streams with account limits',
                    step_detail=f'Waiting for provider capacity: {stream.get("name", "Unknown")}',
                    stream_duration=analysis_params.get('ffmpeg_duration', 30),
                    **profile_progress_context,
                )
            
            if streams_to_check:
                logger.info(f"Starting smart parallel analysis of {total_streams} streams with {global_limit} global workers")

                update_run_progress(
                    channel_id=channel_id,
                    channel_name=channel_name,
                    current=0,
                    total=total_streams,
                    status='analyzing',
                    step='Analyzing streams with account limits',
                    step_detail=f'Using smart scheduler with per-account limits',
                    **profile_progress_context,
                )

                # Heartbeat thread: pushes current stream_statuses to the frontend
                # every 2 seconds while check_streams_with_limits is running.
                # Ensures the live grid stays responsive during stagger delays and
                # between completion events regardless of stream count.
                _heartbeat_stop = threading.Event()

                def _heartbeat():
                    while (
                        not self.abort_current_check.is_set()
                        and not _heartbeat_stop.wait(
                            _STREAM_STATUS_HEARTBEAT_INTERVAL_SECONDS
                        )
                    ):
                        try:
                            status_revision, streams_snapshot = (
                                capture_stream_statuses()
                            )
                            heartbeat_completed = sum(
                                1
                                for stream_status in streams_snapshot
                                if stream_status.get('status') in (
                                    'completed',
                                    'dead',
                                    'error',
                                    'loop_detected',
                                    'blank',
                                    'freeze',
                                    'viewer_preempted',
                                )
                            )
                            publish_stream_status_progress(
                                status_revision,
                                streams_snapshot,
                                channel_id=channel_id,
                                channel_name=channel_name,
                                current=heartbeat_completed,
                                total=total_streams,
                                status='analyzing',
                                step='Analyzing streams with account limits',
                                step_detail='Checking streams...',
                                stream_duration=analysis_params.get(
                                    'ffmpeg_duration',
                                    30,
                                ),
                                **profile_progress_context,
                            )
                        except Exception:
                            pass  # never let the heartbeat crash the check

                _hb_thread = threading.Thread(target=_heartbeat, daemon=True, name='stream-checker-heartbeat')
                _hb_thread.start()

                try:
                    # Check streams in parallel with account-aware limits
                    results = smart_scheduler.check_streams_with_limits(
                        streams=streams_to_check,
                        check_function=analyze_stream,
                        progress_callback=progress_callback,
                        start_callback=start_callback,
                        defer_callback=defer_callback,
                        stagger_delay=stagger_delay,
                        abort_event=self.abort_current_check,
                        provider_wait_timeout=self.config.get('concurrent_streams.provider_wait_timeout', 300),
                        capacity_transition_lock=stream_statuses_lock,
                        ffmpeg_duration=analysis_params.get('ffmpeg_duration', 30),
                        timeout=analysis_params.get('timeout', 30),
                        retries=analysis_params.get('retries', 1),
                        retry_delay=analysis_params.get('retry_delay', 10),
                        user_agent=analysis_params.get('user_agent', 'VLC/3.0.14'),
                        stream_startup_buffer=analysis_params.get('stream_startup_buffer', 10),
                        blank_check_enabled=blank_check_enabled,
                        blank_check_min_duration=analysis_params.get('blank_check_min_duration', 2.0),
                        blank_check_pixel_threshold=analysis_params.get('blank_check_pixel_threshold', 0.10),
                        blank_check_ratio_threshold=analysis_params.get('blank_check_ratio_threshold', 0.80),
                        freeze_check_enabled=freeze_check_enabled,
                        freeze_check_min_duration=analysis_params.get('freeze_check_min_duration', 5.0),
                        freeze_check_noise_threshold=analysis_params.get('freeze_check_noise_threshold', 0.001),
                        freeze_check_ratio_threshold=analysis_params.get('freeze_check_ratio_threshold', 0.80),
                        hardware_acceleration=analysis_params.get('hardware_acceleration'),
                        defer_missing_bitrate_retry=True,
                    )
                finally:
                    _heartbeat_stop.set()
                    _hb_thread.join(timeout=3)

                bitrate_recheck_progress_context = {'index': 0, 'total': 0}

                def recheck_bitrate_stream(stream, _initial):
                    recheck_results = smart_scheduler.check_streams_with_limits(
                        streams=[stream],
                        check_function=analyze_stream,
                        start_callback=bitrate_recheck_start_callback,
                        defer_callback=bitrate_recheck_defer_callback,
                        stagger_delay=0,
                        abort_event=self.abort_current_check,
                        provider_wait_timeout=self.config.get(
                            'concurrent_streams.provider_wait_timeout',
                            300,
                        ),
                        capacity_transition_lock=stream_statuses_lock,
                        ffmpeg_duration=analysis_params.get('ffmpeg_duration', 30),
                        timeout=analysis_params.get('timeout', 30),
                        retries=0,
                        retry_delay=0,
                        user_agent=analysis_params.get('user_agent', 'VLC/3.0.14'),
                        stream_startup_buffer=analysis_params.get('stream_startup_buffer', 10),
                        blank_check_enabled=False,
                        freeze_check_enabled=False,
                        hardware_acceleration=analysis_params.get('hardware_acceleration'),
                        defer_missing_bitrate_retry=False,
                    )
                    return recheck_results[0] if recheck_results else None

                def bitrate_recheck_started(initial, index, total):
                    stream_id = initial.get('stream_id')
                    bitrate_recheck_progress_context['index'] = index
                    bitrate_recheck_progress_context['total'] = total
                    streams_snapshot = None
                    with stream_statuses_lock:
                        if stream_id in stream_statuses:
                            updated_status = dict(stream_statuses[stream_id])
                            updated_status['reason_detail'] = 'missing_bitrate'
                            # The initial probe reservation has already been
                            # released. Do not advertise it as the serial recheck
                            # reservation.
                            stream_statuses[stream_id] = apply_reserved_profile_progress(
                                updated_status,
                                None,
                            )
                            status_revision, streams_snapshot = (
                                capture_stream_statuses()
                            )
                            current_completed = completed_count[0]
                    if streams_snapshot is not None:
                        publish_stream_status_progress(
                            status_revision,
                            streams_snapshot,
                            channel_id=channel_id,
                            channel_name=channel_name,
                            current=current_completed,
                            total=total_streams,
                            current_stream=initial.get('stream_name', 'Unknown'),
                            status='analyzing',
                            step='Preparing bitrate recheck',
                            step_detail=f'Preparing serial bitrate recheck {index}/{total}',
                            stream_duration=analysis_params.get(
                                'ffmpeg_duration',
                                30,
                            ),
                            **profile_progress_context,
                        )

                def bitrate_recheck_start_callback(stream, profile=None):
                    stream_id = stream.get('id')
                    with stream_statuses_lock:
                        if stream_id not in stream_statuses:
                            return
                        updated_status = dict(stream_statuses[stream_id])
                        updated_status['status'] = 'rechecking_bitrate'
                        updated_status['reason_detail'] = 'missing_bitrate'
                        updated_status['started_at'] = datetime.now().isoformat()
                        stream_statuses[stream_id] = apply_reserved_profile_progress(
                            updated_status,
                            profile,
                        )
                        status_revision, streams_snapshot = capture_stream_statuses()
                        current_completed = completed_count[0]
                    publish_stream_status_progress(
                        status_revision,
                        streams_snapshot,
                        channel_id=channel_id,
                        channel_name=channel_name,
                        current=current_completed,
                        total=total_streams,
                        current_stream=stream.get('name', 'Unknown'),
                        status='analyzing',
                        step='Rechecking missing bitrate',
                        step_detail=(
                            'Serial bitrate recheck '
                            f"{bitrate_recheck_progress_context['index']}/"
                            f"{bitrate_recheck_progress_context['total']}"
                        ),
                        stream_duration=analysis_params.get('ffmpeg_duration', 30),
                        **profile_progress_context,
                    )

                def bitrate_recheck_defer_callback(stream, reason):
                    stream_id = stream.get('id')
                    with stream_statuses_lock:
                        if stream_id not in stream_statuses:
                            return
                        updated_status = dict(stream_statuses[stream_id])
                        updated_status['status'] = 'waiting_provider_limit'
                        updated_status['reason_detail'] = reason
                        stream_statuses[stream_id] = apply_reserved_profile_progress(
                            updated_status,
                            None,
                        )
                        status_revision, streams_snapshot = capture_stream_statuses()
                        current_completed = completed_count[0]
                    publish_stream_status_progress(
                        status_revision,
                        streams_snapshot,
                        channel_id=channel_id,
                        channel_name=channel_name,
                        current=current_completed,
                        total=total_streams,
                        current_stream=stream.get('name', 'Unknown'),
                        status='analyzing',
                        step='Waiting for provider capacity',
                        step_detail=(
                            'Waiting to start serial bitrate recheck '
                            f"{bitrate_recheck_progress_context['index']}/"
                            f"{bitrate_recheck_progress_context['total']}: {reason}"
                        ),
                        stream_duration=analysis_params.get('ffmpeg_duration', 30),
                        **profile_progress_context,
                    )

                def apply_bitrate_recheck_completed(initial, outcome, index, total):
                    stream_id = initial.get('stream_id')
                    with stream_statuses_lock:
                        stream_status = (
                            dict(stream_statuses[stream_id])
                            if stream_id in stream_statuses
                            else None
                        )
                    if stream_status is not None:
                        dead_result = self._is_stream_dead(
                            initial,
                            channel_id,
                            threshold_config=_threshold_config,
                        )
                        self._apply_quality_classification(initial, dead_result)
                        is_dead, dead_reason = dead_result
                        dead_reason_detail = getattr(dead_result, 'reason_detail', dead_reason)
                        dead_reason_context = getattr(dead_result, 'details', {}) or {}
                        if is_dead:
                            stream_status['status'] = (
                                dead_reason
                                if dead_reason in ('low_quality', 'blank', 'freeze')
                                else 'dead'
                            )
                            stream_status['reason_detail'] = dead_reason_detail
                            stream_status['quality_reason'] = dead_reason
                            stream_status['quality_reason_detail'] = dead_reason_detail
                            stream_status['quality_reason_context'] = dead_reason_context
                        elif outcome == 'recovered':
                            recovered_score = self._calculate_stream_score(
                                initial,
                                priority_m3u_ids,
                                priority_mode,
                                scoring_weights,
                            )
                            initial['score'] = recovered_score
                            stream_status['status'] = 'completed'
                            stream_status['score'] = round(
                                recovered_score,
                                2,
                            )
                            stream_status['reason_detail'] = 'none'
                            stream_status['reason'] = 'none'
                            stream_status['quality_reason'] = 'none'
                            stream_status['quality_reason_detail'] = 'none'
                            stream_status['quality_reason_context'] = {}
                            stream_status['bitrate'] = initial.get('bitrate_kbps')
                        else:
                            self._apply_incomplete_bitrate_status(
                                stream_status,
                                initial,
                            )
                        self._copy_bitrate_recheck_report_fields(
                            stream_status,
                            initial,
                        )
                    with stream_statuses_lock:
                        if stream_status is not None:
                            stream_statuses[stream_id] = stream_status
                        status_revision, streams_snapshot = capture_stream_statuses()
                        current_completed = completed_count[0]
                    publish_stream_status_progress(
                        status_revision,
                        streams_snapshot,
                        channel_id=channel_id,
                        channel_name=channel_name,
                        current=current_completed,
                        total=total_streams,
                        current_stream=initial.get('stream_name', 'Unknown'),
                        status='analyzing',
                        step='Rechecking missing bitrate',
                        step_detail=f'Completed serial bitrate recheck {index}/{total}',
                        stream_duration=analysis_params.get('ffmpeg_duration', 30),
                        **profile_progress_context,
                    )

                def bitrate_recheck_completed(initial, outcome, index, total):
                    try:
                        return apply_bitrate_recheck_completed(
                            initial,
                            outcome,
                            index,
                            total,
                        )
                    except Exception:
                        stream_id = initial.get('stream_id')
                        stream_name = initial.get('stream_name', 'Unknown')
                        logger.exception(
                            "Failing bitrate recheck progress closed for stream %s",
                            stream_id,
                        )
                        fallback_applied = False
                        with stream_statuses_lock:
                            if stream_id in stream_statuses:
                                stream_status = dict(stream_statuses[stream_id])
                                if stream_status.get('status') not in {
                                    'completed',
                                    'incomplete_bitrate',
                                    'provider_limit_wait_timeout',
                                    'viewer_preempted',
                                    'error',
                                    'dead',
                                    'blank',
                                    'freeze',
                                    'low_quality',
                                    'loop_detected',
                                }:
                                    fallback_applied = True
                                    stream_status['status'] = 'error'
                                    stream_status['score'] = 0.0
                                    stream_status['reason_detail'] = (
                                        'bitrate_recheck_progress_error'
                                    )
                                    stream_status['quality_reason'] = 'offline'
                                    stream_status['quality_reason_detail'] = 'error'
                                    stream_status['quality_reason_context'] = {
                                        'stage': 'bitrate recheck progress',
                                        'message': (
                                            'Bitrate recheck progress finalization failed'
                                        ),
                                    }
                                    stream_statuses[stream_id] = stream_status
                            status_revision, streams_snapshot = (
                                capture_stream_statuses()
                            )
                            current_completed = completed_count[0]

                        try:
                            return publish_stream_status_progress(
                                status_revision,
                                streams_snapshot,
                                channel_id=channel_id,
                                channel_name=channel_name,
                                current=current_completed,
                                total=total_streams,
                                current_stream=stream_name,
                                status='analyzing',
                                step='Rechecking missing bitrate',
                                step_detail=(
                                    'Closed failed serial bitrate recheck '
                                    f'{index}/{total}'
                                    if fallback_applied
                                    else 'Republished terminal serial bitrate recheck '
                                    f'{index}/{total}'
                                ),
                                stream_duration=analysis_params.get(
                                    'ffmpeg_duration',
                                    30,
                                ),
                                **profile_progress_context,
                            )
                        except Exception:
                            logger.exception(
                                "Could not republish closed bitrate recheck progress "
                                "for stream %s",
                                stream_id,
                            )
                            return False

                self._run_deferred_bitrate_rechecks(
                    results,
                    streams_by_id,
                    recheck_bitrate_stream,
                    abort_event=self.abort_current_check,
                    on_start=bitrate_recheck_started,
                    on_complete=bitrate_recheck_completed,
                )

                abort_result = self._abort_channel_check_if_requested(
                    channel_id,
                    channel_name,
                    queue_entry_token=queue_entry_token,
                )
                if abort_result:
                    return abort_result
                
                # Process results - ALL checks are complete at this point
                # Collect stats for batch update to minimize API calls
                batch_stats_list = []
                
                for analyzed in results:
                    if analyzed.get('provider_limit_skipped'):
                        if analyzed.get('reason_detail') == 'viewer_preempted':
                            preempted_stream_ids.add(analyzed.get('stream_id'))
                        logger.warning(
                            "Stream check deferred until provider capacity timed out; preserving existing stream state: "
                            f"{stream_context(stream_id=analyzed.get('stream_id'), stream_url=analyzed.get('stream_url'), channel_id=channel_id)}"
                        )
                        if not analyzed.get('cached'):
                            logger.warning(
                                "Stream check deferred without cached quality stats; excluding from this channel update "
                                "so unchecked newly assigned streams are not promoted: "
                                f"{stream_context(stream_id=analyzed.get('stream_id'), stream_url=analyzed.get('stream_url'), channel_id=channel_id)}"
                            )
                            continue
                        score = self._calculate_stream_score(analyzed, priority_m3u_ids, priority_mode, scoring_weights)
                        analyzed['score'] = score
                        analyzed['channel_id'] = channel_id
                        analyzed['channel_name'] = channel_name
                        analyzed_streams.append(analyzed)
                        continue

                    # Check if stream is dead using pre-resolved threshold config
                    # so forced_profile_id selections are honoured.
                    self._apply_previous_bitrate_fallback(
                        analyzed,
                        streams_by_id.get(analyzed.get('stream_id')),
                    )
                    dead_result = self._is_stream_dead(analyzed, channel_id, threshold_config=_threshold_config)
                    self._apply_quality_classification(analyzed, dead_result)
                    is_dead, dead_reason = dead_result
                    stream_id = analyzed.get('stream_id')
                    stream_url = analyzed.get('stream_url', '')
                    stream_name = analyzed.get('stream_name', 'Unknown')
                    was_dead = self.dead_streams_tracker.is_dead(stream_url)

                    # Prepare stats for batch update after classification so
                    # quality reason fields are persisted with the probe stats.
                    if batch_enabled:
                        stats_item = self._prepare_stream_stats_for_batch(analyzed)
                        if stats_item:
                            batch_stats_list.append(stats_item)
                    else:
                        # Fall back to individual updates if batching is disabled
                        self._update_stream_stats(analyzed)
                    
                    if is_dead and not was_dead:
                        failed_connectivity = self._require_quality_check_connectivity(
                            phase='mark_dead_stream',
                            channel_id=channel_id,
                            channel_name=channel_name,
                            progress_context=profile_progress_context,
                        )
                        if failed_connectivity is not None:
                            return self._fail_channel_for_connectivity(
                                failed_connectivity,
                                channel_id=channel_id,
                                channel_name=channel_name,
                                queue_entry_token=queue_entry_token,
                            )
                        if self.dead_streams_tracker.mark_as_dead(stream_url, stream_id, stream_name, channel_id, reason=dead_reason):
                            dead_stream_ids.add(stream_id)
                            if analyzed.get('blank_detected') or analyzed.get('freeze_detected'):
                                detection_label = 'blank' if analyzed.get('blank_detected') else 'freeze'
                                logger.warning(
                                    f"[{detection_label}-detect] Stream marked dead: "
                                    f"channel_ref={_audit_ref('channel', channel_id)}, "
                                    f"stream_ref={_audit_ref('stream', stream_id)}, "
                                    f"reason={dead_reason}"
                                )
                            else:
                                logger.warning(
                                    f"Stream detected as dead: "
                                    f"{stream_context(stream_id=stream_id, stream_url=stream_url, channel_id=channel_id, reason=dead_reason)}"
                                )
                        else:
                            logger.error(f"Failed to mark stream {stream_id} as dead in tracker")
                    elif not is_dead and was_dead:
                        if allow_revive:
                            if self.dead_streams_tracker.mark_as_alive(stream_url):
                                revived_stream_ids.append(stream_id)
                                logger.info(
                                    f"Stream revived: "
                                    f"{stream_context(stream_id=stream_id, stream_url=stream_url, channel_id=channel_id)}"
                                )
                        else:
                            # Not allowed to revive, treat as still dead
                            dead_stream_ids.add(stream_id)
                            logger.info(
                                f"Stream is alive but revival is disabled by profile: "
                                f"{stream_context(stream_id=stream_id, stream_url=stream_url, channel_id=channel_id)}"
                            )
                    elif is_dead and was_dead:
                        logger.debug(f"Stream {stream_id} remains dead (already marked)")
                        # Only act on stale dead state if this stream was part of the current
                        # check pass. Unchecked streams must not be culled based on prior-run
                        # tracker state — their status will be re-evaluated in the next full check.
                        if stream_id in checked_stream_id_set:
                            failed_connectivity = self._require_quality_check_connectivity(
                                phase='keep_dead_stream_marked',
                                channel_id=channel_id,
                                channel_name=channel_name,
                                progress_context=profile_progress_context,
                            )
                            if failed_connectivity is not None:
                                return self._fail_channel_for_connectivity(
                                    failed_connectivity,
                                    channel_id=channel_id,
                                    channel_name=channel_name,
                                    queue_entry_token=queue_entry_token,
                                )
                            self._refresh_dead_stream_reason_if_needed(
                                stream_url,
                                stream_id,
                                stream_name,
                                channel_id,
                                dead_reason,
                                blank_detected=bool(analyzed.get('blank_detected')),
                                freeze_detected=bool(analyzed.get('freeze_detected')),
                            )
                            dead_stream_ids.add(stream_id)
                        else:
                            logger.debug(
                                f"Stream {stream_id} skipped dead accumulation "
                                f"(not in current check pass)"
                            )
                    
                    # Calculate score using per-profile scoring weights
                    score = self._calculate_stream_score(analyzed, priority_m3u_ids, priority_mode, scoring_weights)
                    analyzed['score'] = score
                    analyzed['channel_id'] = channel_id
                    analyzed['channel_name'] = channel_name
                    analyzed_streams.append(analyzed)
                
                
                # --- MERGE CACHED STREAMS FOR CORRECT SORTING AND LIMITING ---
                # Retrieve "cached" streams that weren't analyzed (because they are within immunity period)
                # We need to include them in the sorting and limiting process to ensure we keep the absolute best streams
                if streams_already_checked:
                    cached_analyzed_streams = []
                    logger.info(f"Re-integrating {len(streams_already_checked)} cached streams for global sorting/limiting")
                    
                    for stream in streams_already_checked:
                        stream_id = stream['id']
                        # Reconstruct a minimal 'analyzed' object from stored stats
                        # This allows standard scoring and sorting logic to work
                        stream_stats = stream.get('stream_stats')
                        if stream_stats is None:
                            stream_stats = {}
                        elif isinstance(stream_stats, str):
                            try:
                                stream_stats = json.loads(stream_stats)
                            except:
                                stream_stats = {}
                        
                        extracted_cached_stats = extract_stream_stats(stream)
                        current_cached_bitrate = extracted_cached_stats.get(
                            'bitrate_kbps'
                        )

                        # Map stored stats back to analysis keys. A bitrate kept
                        # in Dispatcharr beside an incomplete marker is ranking
                        # history only, never the current cached measurement.
                        cached_analyzed = {
                            'stream_id': stream_id,
                            'stream_url': stream.get('url'),
                            'stream_name': stream.get('name'),
                            'bitrate_kbps': current_cached_bitrate,
                            'scoring_bitrate_kbps': (
                                self._previous_stream_bitrate(stream)
                                if current_cached_bitrate is None
                                else None
                            ),
                            'resolution': stream_stats.get('resolution', 'N/A'),
                            'fps': stream_stats.get('source_fps', 0),
                            'video_codec': stream_stats.get('video_codec', 'N/A'),
                            'audio_codec': stream_stats.get('audio_codec', 'N/A'),
                            'hdr_format': stream_stats.get('hdr_format'),
                            'blank_probe_ran': stream_stats.get('blank_probe_ran', False),
                            'blank_detected': stream_stats.get('blank_detected', False),
                            'blank_duration_secs': stream_stats.get('blank_duration_secs'),
                            'blank_ratio': stream_stats.get('blank_ratio'),
                            'freeze_probe_ran': stream_stats.get('freeze_probe_ran', False),
                            'freeze_detected': stream_stats.get('freeze_detected', False),
                            'freeze_duration_secs': stream_stats.get('freeze_duration_secs'),
                            'freeze_ratio': stream_stats.get('freeze_ratio'),
                            'status': 'cached',
                            'channel_id': channel_id,
                            'channel_name': channel_name,
                            'score': 0.0 # Will be calculated below
                        }
                        for field in (
                            'quality_reason',
                            'quality_reason_detail',
                            'quality_reason_context',
                        ):
                            if field in stream_stats:
                                cached_analyzed[field] = stream_stats.get(field)
                        self._copy_bitrate_recheck_report_fields(
                            cached_analyzed,
                            stream_stats,
                        )
                        
                        # Calculate score using CURRENT profile weights
                        score = self._calculate_stream_score(cached_analyzed, priority_m3u_ids, priority_mode, scoring_weights)
                        cached_analyzed['score'] = score
                        cached_analyzed_streams.append(cached_analyzed)
                    
                    # Merge cached streams with newly analyzed streams
                    analyzed_streams.extend(cached_analyzed_streams)
                    logger.info(f"Merged {len(cached_analyzed_streams)} cached streams with {len(results)} new results. Total candidates: {len(analyzed_streams)}")

                logger.info(f"Completed smart parallel analysis of {len(results)} streams with account-aware limits")

            abort_result = self._abort_channel_check_if_requested(
                channel_id,
                channel_name,
                queue_entry_token=queue_entry_token,
            )
            if abort_result:
                return abort_result

            self._log_blank_detection_summary(
                channel_id,
                channel_name,
                analyzed_streams,
                dead_stream_ids=dead_stream_ids,
                dead_stream_removal_enabled=dead_stream_removal_enabled,
            )
            self._log_freeze_detection_summary(
                channel_id,
                channel_name,
                analyzed_streams,
                dead_stream_ids=dead_stream_ids,
                dead_stream_removal_enabled=dead_stream_removal_enabled,
            )

            # Run loop probes on eligible streams (top 25% scoring >= 0.5).
            # Called after all streams are scored and analyzed_streams is fully
            # assembled so the complete score distribution is available.
            # Gated on the per-profile loop_check_enabled flag.
            if loop_check_enabled:
                analysis_params_lp = self.config.get('stream_analysis', {})
                with stream_statuses_lock:
                    loop_streams_snapshot = deepcopy(
                        list(stream_statuses.values())
                    )
                self._run_loop_probes(
                    analyzed_streams,
                    user_agent=analysis_params_lp.get('user_agent', 'VLC/3.0.14'),
                    loop_penalty=loop_penalty,
                    probe_duration=analysis_params_lp.get('max_loop_duration', 120) * 3,
                    hardware_acceleration=analysis_params_lp.get('hardware_acceleration'),
                    channel_id=channel_id,
                    channel_name=channel_name,
                    streams_detail=loop_streams_snapshot,
                    profile_progress_context=profile_progress_context,
                    global_limit_override=global_limit_override,
                )
            else:
                logger.debug("[loop-probe] Loop checking disabled by profile — skipping")

            # Batch stats write after probes so the persisted score and loop
            # fields reflect the penalised score from this run.
            abort_result = self._abort_channel_check_if_requested(
                channel_id,
                channel_name,
                queue_entry_token=queue_entry_token,
            )
            if abort_result:
                return abort_result

            if batch_enabled and batch_stats_list:
                # Rebuild batch list with updated scores post-penalty
                batch_stats_list = []
                for analyzed in analyzed_streams:
                    stats_item = self._prepare_stream_stats_for_batch(analyzed)
                    if stats_item:
                        batch_stats_list.append(stats_item)
            if batch_enabled and batch_stats_list:
                logger.info(f"Batch updating stats for {len(batch_stats_list)} streams (batch_size={batch_size})")
                successful, failed = batch_update_stream_stats(batch_stats_list, batch_size=batch_size)
                logger.info(f"Batch update complete: {successful} successful, {failed} failed")

            # Sort streams by score (highest first)
            update_run_progress(
                channel_id=channel_id,
                channel_name=channel_name,
                current=len(streams),
                total=len(streams),
                status='processing',
                step='Calculating scores',
                step_detail='Sorting streams by quality score',
                **profile_progress_context,
            )
            # Sort streams using tiered sort keys (lexicographical ranking)
            for analyzed in analyzed_streams:
                analyzed['sort_key'] = self._generate_stream_sort_key(analyzed, priority_m3u_ids, priority_mode)
                
            analyzed_streams.sort(key=lambda x: x['sort_key'])
            
            # Apply stream limit if configured in profile
            if stream_limit > 0 and len(analyzed_streams) > stream_limit:
                removed_count = len(analyzed_streams) - stream_limit
                logger.info(f"Applying profile stream limit: Keeping top {stream_limit} streams, removing {removed_count}")
                analyzed_streams = analyzed_streams[:stream_limit]

            report_analyzed_streams = list(analyzed_streams)
            
            # Remove dead streams from the channel (if enabled in config)
            # Dead streams are checked during all channel checks (normal and global)
            # If they're still dead, they're removed; if revived, they remain
            if dead_stream_ids:
                if dead_stream_removal_enabled:
                    logger.warning(f"🔴 Removing {len(dead_stream_ids)} dead streams from channel {channel_name}")
                    analyzed_streams = [s for s in analyzed_streams if s.get('stream_id') not in dead_stream_ids]
                else:
                    logger.info(f"⚠️ Found {len(dead_stream_ids)} dead streams in channel {channel_name}, but removal is disabled in config")
            
            if revived_stream_ids:
                logger.info(f"{len(revived_stream_ids)} streams were revived in channel {channel_name}")

            abort_result = self._abort_channel_check_if_requested(
                channel_id,
                channel_name,
                queue_entry_token=queue_entry_token,
            )
            if abort_result:
                return abort_result
            
            # Update channel with reordered streams
            update_run_progress(
                channel_id=channel_id,
                channel_name=channel_name,
                current=len(streams),
                total=len(streams),
                status='updating',
                step='Reordering streams',
                step_detail='Applying new stream order to channel',
                **profile_progress_context,
            )
            reordered_ids = [s.get('stream_id') for s in analyzed_streams if s.get('stream_id') is not None]
            reordered_ids = self._merge_protected_stream_order(
                current_stream_ids,
                reordered_ids,
                protected_active_stream_ids,
            )
            # Dead streams have already been filtered from analyzed_streams if removal is enabled
            # If removal is disabled, allow them to remain in the channel
            
            # Preserve any stream IDs that are assigned to the channel in Dispatcharr but
            # were not returned by get_channel_streams() due to a stale UDI stream cache.
            # Without this guard, a stale cache causes those streams to be silently dropped
            # when the checker PATCHes the channel's stream list back to Dispatcharr.
            _uncached_ids = self._get_uncached_channel_stream_ids(
                assigned_stream_ids,
                set(reordered_ids),
                dead_stream_removal_enabled,
                dead_stream_ids,
            )
            if _uncached_ids:
                logger.warning(
                    f"Channel {channel_name}: {len(_uncached_ids)} stream ID(s) were assigned "
                    f"to the channel but absent from the UDI stream cache (stale cache?). "
                    f"Preserving in write-back to avoid accidental removal: "
                    f"{_uncached_ids[:5]}{'...' if len(_uncached_ids) > 5 else ''}"
                )
                reordered_ids.extend(_uncached_ids)

            write_back_valid_stream_ids = self._build_write_back_valid_stream_ids(
                udi,
                assigned_stream_ids,
                dead_stream_removal_enabled,
            )

            if not hasattr(update_channel_streams, "mock_calls"):
                failed_connectivity = self._require_quality_check_connectivity(
                    phase='channel_stream_update',
                    channel_id=channel_id,
                    channel_name=channel_name,
                    progress_context=profile_progress_context,
                )
                if failed_connectivity is not None:
                    return self._fail_channel_for_connectivity(
                        failed_connectivity,
                        channel_id=channel_id,
                        channel_name=channel_name,
                        queue_entry_token=queue_entry_token,
                    )

            update_authorized, _ = self._run_channel_side_effect_if_authorized(
                channel_id,
                queue_entry_token,
                lambda: update_channel_streams(
                    channel_id,
                    reordered_ids,
                    valid_stream_ids=write_back_valid_stream_ids,
                    allow_dead_streams=(not dead_stream_removal_enabled),
                    protected_stream_ids=protected_active_stream_ids,
                ),
            )
            if not update_authorized:
                return self._abort_channel_check(
                    channel_id,
                    channel_name,
                    queue_entry_token=queue_entry_token,
                )
            
            # Verify the update
            update_run_progress(
                channel_id=channel_id,
                channel_name=channel_name,
                current=len(streams),
                total=len(streams),
                status='verifying',
                step='Verifying update',
                step_detail='Confirming stream order was applied',
                **profile_progress_context,
            )
            
            # Only verify if enabled in configuration
            batch_config = self.config.get('batch_operations', {})
            verify_updates = batch_config.get('verify_updates', False)
            
            if verify_updates:
                time_module.sleep(0.5)
                udi.refresh_channel_by_id(channel_id)
                logger.debug(f"Verified channel {channel_name} update via UDI refresh")
            else:
                logger.debug(f"Skipped verification for channel {channel_name} (disabled in config)")
            
            logger.info(f"✓ Channel {channel_name} checked and streams reordered (parallel mode)")
            
            # Generate detailed stream stats for return value and changelog
            try:
                # Get channel logo URL
                logo_url = None
                logo_id = channel_data.get('logo_id')
                if logo_id:
                    logo_url = f"/api/logos/{logo_id}"
                
                # Calculate channel-level averages from analyzed streams
                averages = self._calculate_channel_averages(report_analyzed_streams, dead_stream_ids)
                
                stream_stats = []
                # Use all analyzed streams for stats, including dead streams
                # removed from the channel so cause counters stay accurate.
                for analyzed in report_analyzed_streams:
                    stream_id = analyzed.get('stream_id')
                    is_dead = stream_id in dead_stream_ids
                    is_revived = stream_id in revived_stream_ids
                    
                    # Extract and format stats using centralized utilities
                    extracted_stats = extract_stream_stats(analyzed)
                    formatted_stats = format_stream_stats_for_display(extracted_stats)
                    
                    # Get M3U account name for this stream using helper method
                    m3u_account_name = self._get_m3u_account_name(stream_id, udi)

                    # Stamp onto the analyzed dict so analyzed_lookup (used by
                    # check_single_channel) can read it without a separate UDI call.
                    analyzed['m3u_account'] = m3u_account_name
                    
                    stream_stat = {
                        'stream_id': stream_id,
                        'stream_name': analyzed.get('stream_name'),
                        'resolution': formatted_stats['resolution'],
                        'fps': formatted_stats['fps'],
                        'video_codec': formatted_stats['video_codec'],
                        'audio_codec': formatted_stats.get('audio_codec', 'N/A'),
                        'bitrate': formatted_stats['bitrate'],
                        'm3u_account': m3u_account_name,
                        'hdr_format': extracted_stats.get('hdr_format')
                    }
                    
                    # Mark dead streams as "dead" instead of showing score:0
                    if is_dead:
                        stream_stat['status'] = analyzed.get('dead_reason') if analyzed.get('dead_reason') in ('blank', 'freeze', 'low_quality') else 'dead'
                    elif is_revived:
                        stream_stat['status'] = 'revived'
                        stream_stat['score'] = round(analyzed.get('score', 0), 2)
                    elif analyzed.get('reason_detail') == 'viewer_preempted':
                        stream_stat['status'] = 'viewer_preempted'
                    elif self._has_incomplete_bitrate_measurement(analyzed):
                        self._apply_incomplete_bitrate_status(stream_stat, analyzed)
                        stream_stat['score'] = round(analyzed.get('score', 0), 2)
                    else:
                        stream_stat['status'] = 'completed'
                        stream_stat['score'] = round(analyzed.get('score', 0), 2)

                    if analyzed.get('quality_reason') and analyzed.get('quality_reason') != 'none':
                        stream_stat['quality_reason'] = analyzed.get('quality_reason')
                        stream_stat['quality_reason_detail'] = analyzed.get('quality_reason_detail')
                        stream_stat['quality_reason_context'] = analyzed.get('quality_reason_context')

                    for field in VISUAL_PROBE_REPORT_FIELDS:
                        if field in analyzed:
                            stream_stat[field] = analyzed.get(field)
                    self._copy_bitrate_recheck_report_fields(stream_stat, analyzed)

                    # Include loop detection results if the probe ran
                    if analyzed.get('loop_probe_ran'):
                        stream_stat['loop_probe_ran']      = True
                        stream_stat['loop_detected']       = analyzed.get('loop_detected')
                        stream_stat['loop_duration_secs']  = analyzed.get('loop_duration_secs')
                    if analyzed.get('blank_probe_ran'):
                        stream_stat['blank_probe_ran']     = True
                        stream_stat['blank_detected']      = analyzed.get('blank_detected')
                        stream_stat['blank_duration_secs'] = analyzed.get('blank_duration_secs')
                        stream_stat['blank_ratio']         = analyzed.get('blank_ratio')
                    if analyzed.get('freeze_probe_ran'):
                        stream_stat['freeze_probe_ran']     = True
                        stream_stat['freeze_detected']      = analyzed.get('freeze_detected')
                        stream_stat['freeze_duration_secs'] = analyzed.get('freeze_duration_secs')
                        stream_stat['freeze_ratio']         = analyzed.get('freeze_ratio')

                    # Clean up N/A values for cleaner JSON
                    cleaned_stat = {k: v for k, v in stream_stat.items() if v not in [None]}
                    stream_stats.append(cleaned_stat)

            except Exception as e:
                logger.error(f"Error generating stream stats: {e}")
                stream_stats = []
                averages = {'avg_resolution': 'N/A', 'avg_bitrate': 'N/A', 'avg_fps': 'N/A'}
                logo_url = None

            visibility_good_streams_count = (
                self._count_good_checked_streams({'checked_streams': stream_stats})
                + len(protected_active_stream_ids)
            )
            visibility_failed_streams_count = max(
                len(dead_stream_ids),
                self._count_failed_checked_streams({'checked_streams': stream_stats}),
            )
            visibility_authorized, visibility_result = (
                self._run_channel_side_effect_if_authorized(
                    channel_id,
                    queue_entry_token,
                    lambda: self._apply_channel_visibility_after_check(
                        channel_data,
                        good_streams_count=visibility_good_streams_count,
                        dead_streams_count=len(dead_stream_ids),
                        failed_streams_count=visibility_failed_streams_count,
                        revived_streams_count=len(revived_stream_ids),
                        total_streams=len(streams),
                        profile=profile,
                    ),
                )
            )
            if not visibility_authorized:
                return self._abort_channel_check(
                    channel_id,
                    channel_name,
                    queue_entry_token=queue_entry_token,
                )

            # Add to batch changelog instead of creating individual entry
            if self.changelog:
                try:
                    
                    # Add to batch instead of creating individual changelog entry
                    
                    # Add to batch instead of creating individual changelog entry
                    # Only add to batch if not explicitly skipped (e.g., when called from check_single_channel)
                    if not skip_batch_changelog:
                        batch_entry = self._build_batch_changelog_entry(
                            channel_id=channel_id,
                            channel_name=channel_name,
                            logo_url=logo_url,
                            total_streams=len(streams),
                            stream_stats=stream_stats,
                            averages=averages,
                            skipped_streams=active_viewer_skipped_streams,
                            channel_visibility=visibility_result,
                        )
                        self._add_to_batch_changelog(
                            batch_entry,
                            batch_generation=batch_changelog_generation,
                        )
                except Exception as e:
                    logger.warning(f"Failed to add to batch changelog: {e}")
            
            # Update current_stream_ids to exclude dead streams that were removed
            # This prevents dead stream IDs from being saved in checked_stream_ids
            # which would cause them to be skipped by 2-hour immunity even after revival
            # Note: Using list comprehension instead of set operations to preserve order
            # Only exclude dead streams if removal is enabled
            if dead_stream_removal_enabled:
                final_stream_ids = [sid for sid in current_stream_ids if sid not in dead_stream_ids]
            else:
                final_stream_ids = current_stream_ids  # Keep all streams if removal is disabled
            if preempted_stream_ids:
                final_stream_ids = [sid for sid in final_stream_ids if sid not in preempted_stream_ids]
            if protected_active_stream_ids:
                final_stream_ids = [
                    sid
                    for sid in current_stream_ids
                    if (
                        sid in protected_active_stream_ids
                        or (
                            (not dead_stream_removal_enabled or sid not in dead_stream_ids)
                            and sid not in preempted_stream_ids
                        )
                    )
                ]
            self._complete_channel_check(
                channel_id,
                lambda: self.update_tracker.mark_channel_checked(
                    channel_id,
                    stream_count=len(streams),
                    checked_stream_ids=final_stream_ids
                ),
                queue_entry_token=queue_entry_token,
            )
            
            blank_streams_count = self._count_checked_stream_status(
                {'checked_streams': stream_stats},
                'blank',
            )
            freeze_streams_count = self._count_checked_stream_status(
                {'checked_streams': stream_stats},
                'freeze',
            )
            good_streams_count = (
                self._count_good_checked_streams({'checked_streams': stream_stats})
                + len(protected_active_stream_ids)
            )

            # Return statistics for callers that need them
            return {
                'good_streams_count': good_streams_count,
                'dead_streams_count': len(dead_stream_ids),
                'blank_streams_count': blank_streams_count,
                'freeze_streams_count': freeze_streams_count,
                'revived_streams_count': len(revived_stream_ids),
                'dead_streams': [{
                    'id': s, 
                    'name': next((st.get('name') for st in streams if st['id'] == s), f'Stream {s}'),
                    'm3u_account': next((self._get_stream_m3u_account_id(st) for st in streams if st['id'] == s), None)
                } for s in dead_stream_ids],
                'revived_streams': [{
                    'id': s, 
                    'name': next((st.get('name') for st in streams if st['id'] == s), f'Stream {s}'),
                    'm3u_account': next((self._get_stream_m3u_account_id(st) for st in streams if st['id'] == s), None)
                } for s in revived_stream_ids],
                'preempted_streams': [{
                    'id': s,
                    'name': next((st.get('name') for st in streams if st['id'] == s), f'Stream {s}'),
                    'm3u_account': next((self._get_stream_m3u_account_id(st) for st in streams if st['id'] == s), None)
                } for s in preempted_stream_ids],
                'skipped_streams': (
                    [{'id': s['id'], 'name': s.get('name', f"Stream {s['id']}")} for s in streams_already_checked]
                    + active_viewer_skipped_streams
                ),
                'checked_streams': stream_stats,
                'channel_visibility': visibility_result,
                # In-memory analyzed_streams: authoritative source for loop results
                # and m3u_account names. Used by check_single_channel to build its
                # changelog entry without depending on a potentially stale UDI refresh.
                'analyzed_streams': analyzed_streams,
            }

            
        except Exception as e:
            logger.error(f"Error checking channel {channel_id}: {e}", exc_info=True)
            self.check_queue.mark_failed(
                channel_id,
                str(e),
                entry_token=queue_entry_token,
            )
            
            # Only add to batch changelog if not explicitly skipped
            if self.changelog and not skip_batch_changelog:
                try:
                    try:
                        channel_name = channel_data.get('name', f'Channel {channel_id}')
                    except:
                        channel_name = f'Channel {channel_id}'
                    
                    # Add failed check to batch
                    self._add_to_batch_changelog(
                        {
                            'channel_id': channel_id,
                            'channel_name': channel_name,
                            'total_streams': 0,
                            'streams_analyzed': 0,
                            'dead_streams_detected': 0,
                            'streams_revived': 0,
                            'success': False,
                            'error': str(e),
                            'stream_stats': []
                        },
                        batch_generation=batch_changelog_generation,
                    )
                except Exception as changelog_error:
                    logger.warning(f"Failed to add to batch changelog: {changelog_error}")
            
            # Return empty stats on error
            return {
                'dead_streams_count': 0,
                'revived_streams_count': 0,
                'checked_streams': [],
                'error': str(e)
            }
        
        finally:
            self.checking = False
            log_function_return(logger, "_check_channel_concurrent")

    
    def _check_channel_sequential(
        self,
        channel_id: int,
        skip_batch_changelog: bool = False,
        target_stream_ids: Optional[List[str]] = None,
        forced_profile_id: Optional[str] = None,
        provider_limit_override: bool = False,
        run_mode: Optional[str] = None,
        is_single_channel_check: bool = False,
        force_check_override: Optional[bool] = None,
        force_check_generation: Optional[int] = None,
        batch_changelog_generation: Optional[int] = None,
        queue_entry_token: Optional[int] = None,
    ):
        """Check and reorder streams for a specific channel using sequential checking.
        
        Args:
            channel_id: ID of the channel to check
            skip_batch_changelog: If True, don't add this check to the batch changelog
            target_stream_ids: Optional list of stream IDs. If provided, ONLY these
                               streams will be checked, bypassing all other logic.
            provider_limit_override: If True, bypass provider/profile capacity
                                     skips while still protecting active viewers.
            run_mode: Optional progress context label for specialized callers.
            is_single_channel_check: If True, keep Current Progress in
                                     single-channel mode for every phase update.
            force_check_override: Explicit force intent owned by this direct
                                  operation. ``None`` consumes the persistent
                                  worker-queue flag.
            batch_changelog_generation: Queue batch token used to reject
                                  changelog writes after that batch is cleared.
            queue_entry_token: Exact queue activation identity used to reject
                                  stale terminal writes after clear/requeue.
        """
        # Keep this legacy entry point fail-closed by routing it through the same
        # profile reservation and exact-URL scheduler as normal checks. The
        # single global worker preserves sequential behavior without reviving the
        # historical auto-profile transform paths retained below for compatibility
        # archaeology.
        return self._check_channel_concurrent(
            channel_id,
            skip_batch_changelog=skip_batch_changelog,
            target_stream_ids=target_stream_ids,
            forced_profile_id=forced_profile_id,
            provider_limit_override=provider_limit_override,
            run_mode=run_mode,
            is_single_channel_check=is_single_channel_check,
            global_limit_override=1,
            force_check_override=force_check_override,
            force_check_generation=force_check_generation,
            batch_changelog_generation=batch_changelog_generation,
            queue_entry_token=queue_entry_token,
        )

        import time as time_module
        start_time = time_module.time()
        log_function_call(logger, "_check_channel_sequential", channel_id=channel_id)
        
        log_state_change(logger, f"channel_{channel_id}", "queued", "checking")
        logger.info(f"=" * 80)
        logger.info(f"Checking channel {channel_id} (sequential mode)")
        logger.info(f"=" * 80)
        
        # Default to False (safe: do not remove) until the profile is resolved below.
        # If profile resolution fails, streams are left in place rather than silently removed.
        dead_stream_removal_enabled = False

        # Get effective profile for this channel
        stream_limit = 0
        allow_revive = True
        grace_period = False
        loop_check_enabled = False
        blank_check_enabled = False
        freeze_check_enabled = False
        loop_penalty = 0.0
        priority_m3u_ids = []
        priority_mode = 'absolute'
        scoring_weights = None
        # Initialised here; built from the resolved profile below.
        _threshold_config: Dict[str, Any] = {}
        profile: Optional[Dict[str, Any]] = None

        try:
            automation_config = get_automation_config_manager()

            # Fetch channel data to get group_id (might be fetched already but just in case)
            udi = get_udi_manager()
            channel = udi.get_channel_by_id(channel_id)
            group_id = channel.get('channel_group_id') if channel else None

            # If a profile was explicitly selected (via ProfilePickerDialog), use it
            # directly so all checking parameters (weights, limits, revive, loop detection)
            # reflect the user's intent rather than whichever period is currently active.
            if forced_profile_id:
                profile = automation_config.get_profile(forced_profile_id)
                if not profile:
                    logger.warning(
                        f"forced_profile_id={forced_profile_id!r} not found in _check_channel "
                        f"— falling back to active period resolution"
                    )
            if not forced_profile_id or not profile:
                config = automation_config.get_effective_configuration(channel_id, group_id)
                profile = config.get('profile') if config else None
            if profile:
                profile_stream_checking = profile.get('stream_checking', {})
                stream_limit = profile_stream_checking.get('stream_limit', 0)
                allow_revive = profile_stream_checking.get('allow_revive', True)
                priority_m3u_ids = profile_stream_checking.get('m3u_priority', [])
                priority_mode = profile_stream_checking.get('m3u_priority_mode', 'absolute')
                grace_period = profile_stream_checking.get('grace_period', False)
                loop_check_enabled = profile_stream_checking.get('loop_check_enabled', False)
                blank_check_enabled = profile_stream_checking.get('blank_check_enabled', False)
                freeze_check_enabled = profile_stream_checking.get('freeze_check_enabled', False)
                profile_remove_dead_streams = profile_stream_checking.get('remove_dead_streams')
                if isinstance(profile_remove_dead_streams, bool):
                    dead_stream_removal_enabled = profile_remove_dead_streams
                elif profile_remove_dead_streams is not None:
                    logger.warning(
                        "Ignoring non-boolean stream_checking.remove_dead_streams for channel %s",
                        channel_id,
                    )
                scoring_weights = profile.get('scoring_weights', None)
                loop_penalty = float(
                    (scoring_weights or {}).get('loop_penalty', 0.0)
                )
                # Clamp to valid range: -0.25 to 0.0
                loop_penalty = max(-0.25, min(0.0, loop_penalty))

                # Build threshold config once from the resolved profile.
                _threshold_config = self._build_threshold_config_from_profile(profile_stream_checking)
                logger.debug(f"Threshold config for channel {channel_id}: {_threshold_config}")

                # Also check if checking is enabled at all for this profile
                if not profile_stream_checking.get('enabled', False):
                    logger.info(f"Stream checking disabled by profile for channel {channel_id}")
                    self._complete_channel_check(
                        channel_id,
                        queue_entry_token=queue_entry_token,
                    )
                    return {
                        'dead_streams_count': 0,
                        'revived_streams_count': 0,
                        'skipped': True,
                        'skip_reason': 'profile_disabled'
                    }
        except Exception as e:
            logger.warning(f"Failed to load profile settings for channel {channel_id}: {e}")
            _threshold_config = {}
        profile_progress_context = self._automation_profile_progress_context(
            profile,
            forced_profile_id=forced_profile_id,
        )
        profile_progress_context['run_mode'] = run_mode or (
            'single_channel_check' if is_single_channel_check else 'stream_checker'
        )
        if is_single_channel_check:
            profile_progress_context['is_single_channel_check'] = True

        self.checking = True
        try:
            # Get channel information from UDI
            logger.debug(f"Updating progress for channel {channel_id} initialization")
            self.progress.update(
                channel_id=channel_id,
                channel_name='Loading...',
                current=0,
                total=0,
                status='initializing',
                step='Fetching channel info',
                step_detail='Retrieving channel data from UDI',
                **profile_progress_context,
            )
            
            udi = get_udi_manager()
            base_url = _get_base_url()
            logger.debug(f"Fetching channel data for channel {channel_id} from UDI")
            channel_data = udi.get_channel_by_id(channel_id)
            if not channel_data:
                logger.error(f"UDI returned None for channel {channel_id}")
                raise Exception(f"Could not fetch channel {channel_id}")
            
            channel_name = channel_data.get('name', f'Channel {channel_id}')
            abort_result = self._abort_channel_check_if_requested(
                channel_id,
                channel_name,
                queue_entry_token=queue_entry_token,
            )
            if abort_result:
                return abort_result
            
            # Get streams for this channel
            self.progress.update(
                channel_id=channel_id,
                channel_name=channel_name,
                current=0,
                total=0,
                status='initializing',
                step='Fetching streams',
                step_detail=f'Loading streams for {channel_name}',
                **profile_progress_context,
            )
            
            streams = fetch_channel_streams(channel_id)
            abort_result = self._abort_channel_check_if_requested(
                channel_id,
                channel_name,
                queue_entry_token=queue_entry_token,
            )
            if abort_result:
                return abort_result

            if not streams or len(streams) == 0:
                logger.info(f"No streams found for channel {channel_name}")
                visibility_authorized, visibility_result = (
                    self._run_channel_side_effect_if_authorized(
                        channel_id,
                        queue_entry_token,
                        lambda: self._apply_channel_visibility_after_check(
                            channel_data,
                            good_streams_count=0,
                            dead_streams_count=0,
                            revived_streams_count=0,
                            total_streams=0,
                            profile=profile,
                        ),
                    )
                )
                if not visibility_authorized:
                    return self._abort_channel_check(
                        channel_id,
                        channel_name,
                        queue_entry_token=queue_entry_token,
                    )
                if self.changelog and not skip_batch_changelog:
                    batch_entry = {
                        'channel_id': channel_id,
                        'channel_name': channel_name,
                        'logo_url': f"/api/logos/{channel_data.get('logo_id')}" if channel_data.get('logo_id') else None,
                        'total_streams': 0,
                        'streams_analyzed': 0,
                        'dead_streams_detected': 0,
                        'streams_revived': 0,
                        'avg_resolution': 'N/A',
                        'avg_bitrate': 'N/A',
                        'avg_fps': 'N/A',
                        'success': True,
                        'stream_stats': [],
                    }
                    visibility_changelog = self._visibility_changelog_result(visibility_result)
                    if visibility_changelog:
                        batch_entry['channel_visibility'] = visibility_changelog
                    self._add_to_batch_changelog(
                        batch_entry,
                        batch_generation=batch_changelog_generation,
                    )
                self._complete_channel_check(
                    channel_id,
                    lambda: self.update_tracker.mark_channel_checked(
                        channel_id,
                        stream_count=0,
                        checked_stream_ids=[],
                    ),
                    queue_entry_token=queue_entry_token,
                )
                return {
                    'dead_streams_count': 0,
                    'revived_streams_count': 0,
                    'channel_visibility': visibility_result,
                }
            
            logger.info(f"Found {len(streams)} streams for channel {channel_name}")
            
            # Check if channel has active viewers or if its playlist has reached max concurrent streams
            limit_check_result = self._check_channel_limits(
                channel_id,
                channel_name,
                streams,
                provider_limit_override=provider_limit_override,
            )
            if limit_check_result is not None:
                self._complete_channel_check(
                    channel_id,
                    lambda: self.update_tracker.mark_channel_checked(channel_id),
                    queue_entry_token=queue_entry_token,
                )
                return limit_check_result
            
            # Check if this is a force check (bypasses 2-hour immunity)
            if force_check_override is None:
                force_check, owned_force_generation = (
                    self.update_tracker.get_force_check_state(channel_id)
                )
            else:
                force_check = bool(force_check_override)
                owned_force_generation = force_check_generation
            # NOTE: force_check controls immunity bypass ONLY (all streams are re-analyzed).
            # It no longer overrides allow_revive — the profile flag is the sole authority
            # for whether a previously-dead stream can be promoted back to active (Bug 5 fix).
            
            # Get list of already checked streams to avoid re-analyzing
            checked_stream_info = self.update_tracker.updates.get('channels', {}).get(str(channel_id), {})
            checked_stream_ids = checked_stream_info.get('checked_stream_ids', [])
            last_check_str = checked_stream_info.get('last_check')
            
            # Check if immunity period (2 hours) has expired
            immunity_expired = False
            if last_check_str and grace_period:
                try:
                    last_check_time = datetime.fromisoformat(last_check_str)
                    if (datetime.now() - last_check_time).total_seconds() > 7200:
                        immunity_expired = True
                        logger.info(f"Immunity period (2 hours) expired for channel {channel_name} - will re-analyze all streams")
                except Exception as e:
                    logger.warning(f"Failed to parse last_check timestamp for channel {channel_id}: {e}")
            
            current_stream_ids = [s['id'] for s in streams]
            assigned_stream_ids = self._get_channel_assignment_stream_ids(
                channel_id,
                channel_data,
                udi,
                fallback_stream_ids=current_stream_ids,
                refresh_from_dispatcharr=not dead_stream_removal_enabled,
            )
            protected_active_stream_ids = self._get_active_viewer_protected_stream_ids(
                channel_id,
                streams,
                udi,
            )
            active_viewer_skipped_streams = self._active_viewer_skipped_streams(
                streams,
                protected_active_stream_ids,
            )
            if protected_active_stream_ids:
                logger.info(
                    "Channel %s has %s active viewer-protected stream(s); "
                    "their slots will be preserved while other streams are checked",
                    channel_name,
                    len(protected_active_stream_ids),
                )
            
            # Identify which streams need analysis (new or unchecked)
            
            if target_stream_ids is not None:
                # Targeted check mode: Evaluates newly assigned streams ONLY
                streams_to_check = [
                    s for s in streams
                    if str(s['id']) in [str(ts) for ts in target_stream_ids]
                    and s.get('id') not in protected_active_stream_ids
                ]
                streams_already_checked = [
                    s for s in streams
                    if str(s['id']) not in [str(ts) for ts in target_stream_ids]
                    and s.get('id') not in protected_active_stream_ids
                ]
                logger.info(f"Targeted stream check: evaluating {len(streams_to_check)} specific newly assigned streams")
                
            elif force_check or (grace_period and immunity_expired) or (not grace_period and not force_check):
                streams_to_check = [
                    s for s in streams
                    if s.get('id') not in protected_active_stream_ids
                ]
                streams_already_checked = []
                
                if force_check:
                    logger.info(f"Force check enabled: analyzing all {len(streams)} streams (bypassing 2-hour immunity)")
                    if force_check_override is None or owned_force_generation is not None:
                        self.update_tracker.clear_force_check(
                            channel_id,
                            expected_generation=owned_force_generation,
                        )
                elif grace_period and immunity_expired:
                    logger.info(f"Grace period (2h) expired: re-analyzing {len(streams)} streams for {channel_name}")
                elif not grace_period:
                    logger.info(f"Grace period disabled for profile: analyzing all {len(streams)} streams")
            else:
                # Normal incremental check: only analyze new streams
                streams_to_check = [
                    s for s in streams
                    if s['id'] not in checked_stream_ids
                    and s.get('id') not in protected_active_stream_ids
                ]
                streams_already_checked = [
                    s for s in streams
                    if s['id'] in checked_stream_ids
                    and s.get('id') not in protected_active_stream_ids
                ]
                
                if streams_to_check:
                    logger.info(f"Found {len(streams_to_check)} new/unchecked streams (out of {len(streams)} total)")
                else:
                    logger.info(f"All {len(streams)} streams have been recently checked (within 2h immunity), using cached scores")
                    
                    # Optimization: Skip check entirely if all conditions are met:
                    # 1. No new streams to analyze (all have been checked)
                    # 2. Stream count matches previous check (no additions/deletions)
                    # 3. Set of stream IDs is identical (no stream replacements)
                    previous_stream_count = len(checked_stream_ids)
                    current_stream_count = len(current_stream_ids)
                    
                    if (current_stream_count == previous_stream_count and 
                        set(current_stream_ids) == set(checked_stream_ids)):
                        logger.info(f"Channel {channel_name} unchanged since last check - skipping reorder")
                        # Update timestamp but keep existing checked_stream_ids
                        self._complete_channel_check(
                            channel_id,
                            lambda: self.update_tracker.mark_channel_checked(
                                channel_id,
                                stream_count=current_stream_count,
                                checked_stream_ids=checked_stream_ids
                            ),
                            queue_entry_token=queue_entry_token,
                        )
                        return {
                            'dead_streams_count': 0,
                            'revived_streams_count': 0,
                            'dead_streams': [],
                            'revived_streams': [],
                            'skipped_streams_count': len(streams_already_checked) + len(active_viewer_skipped_streams),
                            'skipped_streams': (
                                [{'id': s['id'], 'name': s.get('name', f"Stream {s['id']}")} for s in streams_already_checked]
                                + active_viewer_skipped_streams
                            ),
                            'checked_streams': [],
                        }
                    else:
                        logger.info(f"Channel composition changed (prev: {previous_stream_count}, curr: {current_stream_count}) - will reorder")
            
            # Streams that are actively analyzed in this pass. Used to gate
            # dead_stream_ids mutations — only streams checked in THIS pass may
            # be added to dead_stream_ids. Unchecked streams retain their tracker
            # state unchanged until a future run evaluates them directly.
            checked_stream_id_set = {s['id'] for s in streams_to_check}

            # Analyze new/unchecked streams
            analyzed_streams = []
            dead_stream_ids = set()  # Use set for O(1) lookups
            revived_stream_ids = []
            total_streams = len(streams_to_check)
            
            # Dict to keep track of the stream details throughout the analysis
            stream_statuses = {
                s['id']: {
                    'id': s['id'],
                    'name': s.get('name', f"Stream {s['id']}"),
                    'status': 'pending',
                    'm3u_account': self._get_m3u_account_name(s.get('id'), udi) if hasattr(self, '_get_m3u_account_name') else 'N/A'
                }
                for s in streams_to_check
            }
            
            for idx, stream in enumerate(streams_to_check, 1):
                if self.abort_current_check.is_set():
                    logger.info("Abort requested, stopping sequential stream checks")
                    break
                    
                self.progress.update(
                    channel_id=channel_id,
                    channel_name=channel_name,
                    current=idx,
                    total=total_streams,
                    current_stream=stream.get('name', 'Unknown'),
                    status='analyzing',
                    step='Analyzing stream quality',
                    step_detail=f'Checking bitrate, resolution, codec ({idx}/{total_streams})',
                    streams_detail=list(stream_statuses.values()),
                    **profile_progress_context,
                )
                
                if stream['id'] in stream_statuses:
                    stream_statuses[stream['id']]['status'] = 'checking'
                    stream_statuses[stream['id']]['started_at'] = datetime.now().isoformat()
                    self._clear_active_stream_reason(stream_statuses[stream['id']])
                
                # Analyze stream
                analysis_params = self.config.get('stream_analysis', {})

                # Push checking status + started_at to frontend before analyze_stream blocks
                self.progress.update(
                    channel_id=channel_id,
                    channel_name=channel_name,
                    current=idx,
                    total=total_streams,
                    current_stream=stream.get('name', 'Unknown'),
                    status='analyzing',
                    step='Analyzing stream quality',
                    step_detail=f'Checking bitrate, resolution, codec ({idx}/{total_streams})',
                    streams_detail=list(stream_statuses.values()),
                    stream_duration=analysis_params.get('ffmpeg_duration', 20),
                    **profile_progress_context,
                )
                
                # Apply URL transformation if using M3U profile with search/replace patterns
                stream_url = stream.get('url', '')
                if udi:
                    stream_url = udi.apply_profile_url_transformation(stream)
                
                analyzed = analyze_stream(
                    stream_url=stream_url,
                    stream_id=stream['id'],
                    stream_name=stream.get('name', 'Unknown'),
                    ffmpeg_duration=analysis_params.get('ffmpeg_duration', 20),
                    timeout=analysis_params.get('timeout', 30),
                    retries=analysis_params.get('retries', 1),
                    retry_delay=analysis_params.get('retry_delay', 10),
                    user_agent=analysis_params.get('user_agent', 'VLC/3.0.14'),
                    stream_startup_buffer=analysis_params.get('stream_startup_buffer', 10),
                    blank_check_enabled=blank_check_enabled,
                    blank_check_min_duration=analysis_params.get('blank_check_min_duration', 2.0),
                    blank_check_pixel_threshold=analysis_params.get('blank_check_pixel_threshold', 0.10),
                    blank_check_ratio_threshold=analysis_params.get('blank_check_ratio_threshold', 0.80),
                    freeze_check_enabled=freeze_check_enabled,
                    freeze_check_min_duration=analysis_params.get('freeze_check_min_duration', 5.0),
                    freeze_check_noise_threshold=analysis_params.get('freeze_check_noise_threshold', 0.001),
                    freeze_check_ratio_threshold=analysis_params.get('freeze_check_ratio_threshold', 0.80),
                    hardware_acceleration=analysis_params.get('hardware_acceleration'),
                    defer_missing_bitrate_retry=True,
                )

                def recheck_sequential_bitrate(_stream, _initial):
                    return analyze_stream(
                        stream_url=stream_url,
                        stream_id=stream['id'],
                        stream_name=stream.get('name', 'Unknown'),
                        ffmpeg_duration=analysis_params.get('ffmpeg_duration', 20),
                        timeout=analysis_params.get('timeout', 30),
                        retries=0,
                        retry_delay=0,
                        user_agent=analysis_params.get('user_agent', 'VLC/3.0.14'),
                        stream_startup_buffer=analysis_params.get('stream_startup_buffer', 10),
                        blank_check_enabled=False,
                        freeze_check_enabled=False,
                        hardware_acceleration=analysis_params.get('hardware_acceleration'),
                        defer_missing_bitrate_retry=False,
                    )

                def sequential_recheck_started(initial, recheck_index, recheck_total):
                    if stream['id'] in stream_statuses:
                        stream_statuses[stream['id']]['status'] = 'rechecking_bitrate'
                        stream_statuses[stream['id']]['reason_detail'] = 'missing_bitrate'
                    self.progress.update(
                        channel_id=channel_id,
                        channel_name=channel_name,
                        current=idx,
                        total=total_streams,
                        current_stream=initial.get('stream_name', 'Unknown'),
                        status='analyzing',
                        step='Rechecking missing bitrate',
                        step_detail=(
                            f'Serial bitrate recheck {recheck_index}/{recheck_total} '
                            f'for stream {idx}/{total_streams}'
                        ),
                        streams_detail=list(stream_statuses.values()),
                        stream_duration=analysis_params.get('ffmpeg_duration', 20),
                        **profile_progress_context,
                    )

                self._run_deferred_bitrate_rechecks(
                    [analyzed],
                    {stream['id']: stream},
                    recheck_sequential_bitrate,
                    abort_event=self.abort_current_check,
                    on_start=sequential_recheck_started,
                )
                self._apply_previous_bitrate_fallback(analyzed, stream)
                
                # Check if stream is dead using pre-resolved threshold config
                dead_result = self._is_stream_dead(analyzed, channel_id, threshold_config=_threshold_config)
                self._apply_quality_classification(analyzed, dead_result)
                is_dead, dead_reason = dead_result

                # Update stream stats on dispatcharr with ffmpeg-extracted data
                self._update_stream_stats(analyzed)

                stream_url = stream.get('url', '')
                stream_name = stream.get('name', 'Unknown')
                was_dead = self.dead_streams_tracker.is_dead(stream_url)
                
                if is_dead and not was_dead:
                    failed_connectivity = self._require_quality_check_connectivity(
                        phase='mark_dead_stream',
                        channel_id=channel_id,
                        channel_name=channel_name,
                        progress_context=profile_progress_context,
                    )
                    if failed_connectivity is not None:
                        return self._fail_channel_for_connectivity(
                            failed_connectivity,
                            channel_id=channel_id,
                            channel_name=channel_name,
                            queue_entry_token=queue_entry_token,
                        )
                    # Mark as dead in tracker
                    if self.dead_streams_tracker.mark_as_dead(stream_url, stream['id'], stream_name, channel_id, reason=dead_reason):
                        dead_stream_ids.add(stream['id'])
                        if analyzed.get('blank_detected') or analyzed.get('freeze_detected'):
                            detection_label = 'blank' if analyzed.get('blank_detected') else 'freeze'
                            logger.warning(
                                f"[{detection_label}-detect] Stream marked dead: "
                                f"channel_ref={_audit_ref('channel', channel_id)}, "
                                f"stream_ref={_audit_ref('stream', stream['id'])}, "
                                f"reason={dead_reason}"
                            )
                        else:
                            logger.warning(
                                f"Stream detected as dead: "
                                f"{stream_context(stream_id=stream['id'], stream_url=stream_url, channel_id=channel_id, reason=dead_reason)}"
                            )
                    else:
                        logger.error(f"Failed to mark stream {stream['id']} as DEAD, will not remove from channel")
                elif not is_dead and was_dead:
                    # Stream was revived!
                    if allow_revive:
                        if self.dead_streams_tracker.mark_as_alive(stream_url):
                            revived_stream_ids.append(stream['id'])
                            logger.info(
                                f"Stream revived: "
                                f"{stream_context(stream_id=stream['id'], stream_url=stream_url, channel_id=channel_id)}"
                            )
                    else:
                        dead_stream_ids.add(stream['id'])
                        logger.info(
                            f"Stream is alive but revival is disabled by profile: "
                            f"{stream_context(stream_id=stream['id'], stream_url=stream_url, channel_id=channel_id)}"
                        )
                elif is_dead and was_dead:
                    # Stream remains dead. Guard is redundant here — this loop
                    # iterates streams_to_check by definition — but kept for
                    # symmetry with the concurrent method and to make the scope
                    # constraint explicit at review time.
                    if stream['id'] in checked_stream_id_set:
                        failed_connectivity = self._require_quality_check_connectivity(
                            phase='keep_dead_stream_marked',
                            channel_id=channel_id,
                            channel_name=channel_name,
                            progress_context=profile_progress_context,
                        )
                        if failed_connectivity is not None:
                            return self._fail_channel_for_connectivity(
                                failed_connectivity,
                                channel_id=channel_id,
                                channel_name=channel_name,
                                queue_entry_token=queue_entry_token,
                            )
                        self._refresh_dead_stream_reason_if_needed(
                            stream_url,
                            stream['id'],
                            stream_name,
                            channel_id,
                            dead_reason,
                            blank_detected=bool(analyzed.get('blank_detected')),
                            freeze_detected=bool(analyzed.get('freeze_detected')),
                        )
                        dead_stream_ids.add(stream['id'])

                # Calculate score
                score = self._calculate_stream_score(analyzed, priority_m3u_ids, priority_mode, scoring_weights)
                analyzed['score'] = score
                analyzed_streams.append(analyzed)
                
                # Update stream status for progress display
                if stream['id'] in stream_statuses:
                    if analyzed.get('status') == 'ERROR':
                        stream_statuses[stream['id']]['status'] = 'error'
                        stream_statuses[stream['id']]['score'] = 0.0
                        stream_statuses[stream['id']]['reason_detail'] = analyzed.get('quality_reason_detail') or 'error'
                        stream_statuses[stream['id']]['quality_reason'] = analyzed.get('quality_reason') or 'offline'
                        stream_statuses[stream['id']]['quality_reason_detail'] = analyzed.get('quality_reason_detail') or 'error'
                        stream_statuses[stream['id']]['quality_reason_context'] = analyzed.get('quality_reason_context') or {
                            'stage': 'stream analysis',
                            'message': analyzed.get('error_message') or 'Stream analysis worker returned no result',
                        }
                    elif is_dead:
                        stream_statuses[stream['id']]['status'] = dead_reason if dead_reason in ('low_quality', 'blank', 'freeze') else 'dead'
                        stream_statuses[stream['id']]['score'] = 0.0
                        stream_statuses[stream['id']]['reason_detail'] = analyzed.get('quality_reason_detail')
                        stream_statuses[stream['id']]['quality_reason'] = analyzed.get('quality_reason')
                        stream_statuses[stream['id']]['quality_reason_detail'] = analyzed.get('quality_reason_detail')
                        stream_statuses[stream['id']]['quality_reason_context'] = analyzed.get('quality_reason_context')
                        stream_statuses[stream['id']]['resolution'] = analyzed.get('resolution', '0x0')
                        stream_statuses[stream['id']]['video_codec'] = analyzed.get('video_codec', 'N/A')
                        stream_statuses[stream['id']]['fps'] = analyzed.get('fps', 0)
                        stream_statuses[stream['id']]['bitrate'] = analyzed.get('bitrate_kbps')
                        stream_statuses[stream['id']]['hdr_format'] = analyzed.get('hdr_format')
                    else:
                        stream_statuses[stream['id']]['score'] = score
                        if self._has_incomplete_bitrate_measurement(analyzed):
                            self._apply_incomplete_bitrate_status(stream_statuses[stream['id']], analyzed)
                        else:
                            stream_statuses[stream['id']]['status'] = 'completed'
                            stream_statuses[stream['id']]['quality_reason'] = 'none'
                            stream_statuses[stream['id']]['quality_reason_detail'] = 'none'
                            stream_statuses[stream['id']]['quality_reason_context'] = {}
                        stream_statuses[stream['id']]['resolution'] = analyzed.get('resolution', '0x0')
                        stream_statuses[stream['id']]['video_codec'] = analyzed.get('video_codec', 'N/A')
                        stream_statuses[stream['id']]['fps'] = analyzed.get('fps', 0)
                        stream_statuses[stream['id']]['bitrate'] = analyzed.get('bitrate_kbps')
                
                logger.info(f"Stream {idx}/{total_streams}: {stream.get('name')} - Score: {score:.2f}")

            abort_result = self._abort_channel_check_if_requested(
                channel_id,
                channel_name,
                queue_entry_token=queue_entry_token,
            )
            if abort_result:
                return abort_result
            
            # For already-checked streams, retrieve their cached data from UDI
            for stream in streams_already_checked:
                stream_data = udi.get_stream_by_id(stream['id'])
                if stream_data:
                    stream_stats = stream_data.get('stream_stats', {})
                    # Handle None case explicitly
                    if stream_stats is None:
                        stream_stats = {}
                    if isinstance(stream_stats, str):
                        try:
                            stream_stats = json.loads(stream_stats)
                            # Handle case where JSON string is "null"
                            if stream_stats is None:
                                stream_stats = {}
                        except json.JSONDecodeError:
                            stream_stats = {}
                    
                    # Reconstruct analyzed format from stored stats
                    # Use "0x0" for resolution, 0 for FPS and bitrate when not available
                    extracted_cached_stats = extract_stream_stats(stream_data)
                    current_cached_bitrate = extracted_cached_stats.get(
                        'bitrate_kbps'
                    )
                    analyzed = {
                        'channel_id': channel_id,
                        'channel_name': channel_name,
                        'stream_id': stream['id'],
                        'stream_name': stream.get('name', 'Unknown'),
                        'stream_url': stream.get('url', ''),
                        'resolution': stream_stats.get('resolution', '0x0'),
                        'fps': stream_stats.get('source_fps', 0),
                        'video_codec': stream_stats.get('video_codec', 'N/A'),
                        'audio_codec': stream_stats.get('audio_codec', 'N/A'),
                        'hdr_format': stream_stats.get('hdr_format'),
                        'bitrate_kbps': current_cached_bitrate,
                        'scoring_bitrate_kbps': (
                            self._previous_stream_bitrate(stream_data)
                            if current_cached_bitrate is None
                            else None
                        ),
                        'blank_probe_ran': stream_stats.get('blank_probe_ran', False),
                        'blank_detected': stream_stats.get('blank_detected', False),
                        'blank_duration_secs': stream_stats.get('blank_duration_secs'),
                        'blank_ratio': stream_stats.get('blank_ratio'),
                        'freeze_probe_ran': stream_stats.get('freeze_probe_ran', False),
                        'freeze_detected': stream_stats.get('freeze_detected', False),
                        'freeze_duration_secs': stream_stats.get('freeze_duration_secs'),
                        'freeze_ratio': stream_stats.get('freeze_ratio'),
                        'status': 'OK'  # Assume OK for previously checked streams
                    }
                    for field in (
                        'quality_reason',
                        'quality_reason_detail',
                        'quality_reason_context',
                    ):
                        if field in stream_stats:
                            analyzed[field] = stream_stats.get(field)
                    self._copy_bitrate_recheck_report_fields(analyzed, stream_stats)
                    
                    # TARGETED MODE GUARD: Dead-state transitions for streams in
                    # streams_already_checked are intentionally suppressed. These streams
                    # were NOT analyzed in this pass — their dead/alive determination is
                    # based on reconstructed cached stats, which are not authoritative.
                    # Acting on cached stats here causes every previously-flagged-dead
                    # stream in the channel to be culled during targeted checks even when
                    # zero new assignments were made (see: Change Block F, spec v1.0).
                    #
                    # All three state transitions are blocked:
                    #   is_dead and not was_dead  → no mark_as_dead on cached stats
                    #   not is_dead and was_dead  → no revival on cached stats
                    #   is_dead and was_dead      → no dead_stream_ids accumulation
                    #
                    # Dead-state transitions require live ffmpeg analysis to be
                    # authoritative. Leave all state changes for the next full check pass.
                    #
                    # NOTE: The variables below are commented out rather than deleted so
                    # the original logic remains readable alongside the guard explanation.
                    # stream_url = stream.get('url', '')
                    # stream_name = stream.get('name', 'Unknown')
                    # is_dead, dead_reason = self._is_stream_dead(analyzed, channel_id, threshold_config=_threshold_config)
                    # was_dead = self.dead_streams_tracker.is_dead(stream_url)
                    #
                    # if is_dead and not was_dead:
                    #     if self.dead_streams_tracker.mark_as_dead(stream_url, stream['id'], stream_name, channel_id, reason=dead_reason):
                    #         dead_stream_ids.add(stream['id'])
                    #         logger.warning(f"Cached stream {stream['id']} detected as DEAD: {stream_name} (reason={dead_reason})")
                    #     else:
                    #         logger.error(f"Failed to mark cached stream {stream['id']} as DEAD, will not remove from channel")
                    # elif not is_dead and was_dead:
                    #     if allow_revive:
                    #         if self.dead_streams_tracker.mark_as_alive(stream_url):
                    #             revived_stream_ids.append(stream['id'])
                    #             logger.info(f"Cached stream {stream['id']} REVIVED: {stream_name}")
                    #     else:
                    #         dead_stream_ids.add(stream['id'])
                    #         logger.info(f"Cached stream {stream['id']} is alive but revival disabled by profile: {stream_name}")
                    # elif is_dead and was_dead:
                    #     logger.debug(f"Cached stream {stream['id']} remains dead (already marked)")
                    #     dead_stream_ids.add(stream['id'])
                    
                    # Calculate score using stored stats and CURRENT profile weights
                    score = self._calculate_stream_score(analyzed, priority_m3u_ids, priority_mode, scoring_weights)
                    analyzed['score'] = score
                    analyzed_streams.append(analyzed)
                    logger.debug(f"Using cached data for stream {stream['id']}: {stream.get('name')} - Score: {score:.2f}")
                else:
                    # If we can't fetch cached data, analyze this stream
                    logger.warning(f"Could not fetch cached data for stream {stream['id']}, will analyze")
                    analysis_params = self.config.get('stream_analysis', {})
                    
                    # Apply URL transformation if using M3U profile with search/replace patterns
                    stream_url = stream.get('url', '')
                    if udi:
                        stream_url = udi.apply_profile_url_transformation(stream)
                    
                    analyzed = analyze_stream(
                        stream_url=stream_url,
                        stream_id=stream['id'],
                        stream_name=stream.get('name', 'Unknown'),
                        ffmpeg_duration=analysis_params.get('ffmpeg_duration', 20),
                        timeout=analysis_params.get('timeout', 30),
                        retries=analysis_params.get('retries', 1),
                        retry_delay=analysis_params.get('retry_delay', 10),
                        user_agent=analysis_params.get('user_agent', 'VLC/3.0.14'),
                        stream_startup_buffer=analysis_params.get('stream_startup_buffer', 10),
                        blank_check_enabled=blank_check_enabled,
                        blank_check_min_duration=analysis_params.get('blank_check_min_duration', 2.0),
                        blank_check_pixel_threshold=analysis_params.get('blank_check_pixel_threshold', 0.10),
                        blank_check_ratio_threshold=analysis_params.get('blank_check_ratio_threshold', 0.80),
                        freeze_check_enabled=freeze_check_enabled,
                        freeze_check_min_duration=analysis_params.get('freeze_check_min_duration', 5.0),
                        freeze_check_noise_threshold=analysis_params.get('freeze_check_noise_threshold', 0.001),
                        freeze_check_ratio_threshold=analysis_params.get('freeze_check_ratio_threshold', 0.80),
                        hardware_acceleration=analysis_params.get('hardware_acceleration')
                    )
                    self._update_stream_stats(analyzed)
                    score = self._calculate_stream_score(analyzed, priority_m3u_ids, priority_mode)
                    analyzed['score'] = score
                    analyzed_streams.append(analyzed)

            abort_result = self._abort_channel_check_if_requested(
                channel_id,
                channel_name,
                queue_entry_token=queue_entry_token,
            )
            if abort_result:
                return abort_result

            self._log_blank_detection_summary(
                channel_id,
                channel_name,
                analyzed_streams,
                dead_stream_ids=dead_stream_ids,
                dead_stream_removal_enabled=dead_stream_removal_enabled,
            )
            self._log_freeze_detection_summary(
                channel_id,
                channel_name,
                analyzed_streams,
                dead_stream_ids=dead_stream_ids,
                dead_stream_removal_enabled=dead_stream_removal_enabled,
            )

            # Run loop probes on eligible streams — all streams scored, full
            # distribution known for top-percentile calculation.
            # Gated on the per-profile loop_check_enabled flag.
            if loop_check_enabled:
                analysis_params_lp = self.config.get('stream_analysis', {})
                self._run_loop_probes(
                    analyzed_streams,
                    user_agent=analysis_params_lp.get('user_agent', 'VLC/3.0.14'),
                    loop_penalty=loop_penalty,
                    probe_duration=analysis_params_lp.get('max_loop_duration', 120) * 3,
                    hardware_acceleration=analysis_params_lp.get('hardware_acceleration'),
                    channel_id=channel_id,
                    channel_name=channel_name,
                    streams_detail=list(stream_statuses.values()),
                    profile_progress_context=profile_progress_context,
                )
                # Write stats for all probed streams so loop fields
                # (loop_probe_ran, loop_detected, loop_duration_secs) are
                # persisted to the database regardless of whether a penalty
                # was applied. Streams with a penalty get their updated score
                # persisted here too.
                for analyzed in analyzed_streams:
                    if analyzed.get('loop_probe_ran'):
                        self._update_stream_stats(analyzed)
            else:
                logger.debug("[loop-probe] Loop checking disabled by profile — skipping")

            abort_result = self._abort_channel_check_if_requested(
                channel_id,
                channel_name,
                queue_entry_token=queue_entry_token,
            )
            if abort_result:
                return abort_result

            # Sort streams by score (highest first)
            self.progress.update(
                channel_id=channel_id,
                channel_name=channel_name,
                current=len(streams),
                total=len(streams),
                status='processing',
                step='Calculating scores',
                step_detail='Sorting streams by quality score',
                **profile_progress_context,
            )
            # Sort streams using tiered sort keys (lexicographical ranking)
            for analyzed in analyzed_streams:
                analyzed['sort_key'] = self._generate_stream_sort_key(analyzed, priority_m3u_ids, priority_mode)
                
            analyzed_streams.sort(key=lambda x: x['sort_key'])
            
            # Apply stream limit if configured in profile
            if stream_limit > 0 and len(analyzed_streams) > stream_limit:
                removed_count = len(analyzed_streams) - stream_limit
                logger.info(f"Applying profile stream limit: Keeping top {stream_limit} streams, removing {removed_count}")
                analyzed_streams = analyzed_streams[:stream_limit]

            report_analyzed_streams = list(analyzed_streams)
            
            # Remove dead streams from the channel (if enabled in config)
            # Dead streams are checked during all channel checks (normal and global)
            # If they're still dead, they're removed; if revived, they remain
            if dead_stream_ids:
                if dead_stream_removal_enabled:
                    logger.warning(f"🔴 Removing {len(dead_stream_ids)} dead streams from channel {channel_name}")
                    # Log which streams are being removed
                    for stream_id in dead_stream_ids:
                        dead_stream = next((s for s in analyzed_streams if s.get('stream_id') == stream_id), None)
                        if dead_stream:
                            logger.info(f"  - Removing dead stream {stream_ref(stream_id, dead_stream.get('stream_url'))}")
                    analyzed_streams = [s for s in analyzed_streams if s.get('stream_id') not in dead_stream_ids]
                else:
                    logger.info(f"⚠️ Found {len(dead_stream_ids)} dead streams in channel {channel_name}, but removal is disabled in config")
            
            if revived_stream_ids:
                logger.info(f"{len(revived_stream_ids)} streams were revived in channel {channel_name}")

            abort_result = self._abort_channel_check_if_requested(
                channel_id,
                channel_name,
                queue_entry_token=queue_entry_token,
            )
            if abort_result:
                return abort_result
            
            # Update channel with reordered streams
            self.progress.update(
                channel_id=channel_id,
                channel_name=channel_name,
                current=len(streams),
                total=len(streams),
                status='updating',
                step='Reordering streams',
                step_detail='Applying new stream order to channel',
                **profile_progress_context,
            )
            reordered_ids = [s.get('stream_id') for s in analyzed_streams if s.get('stream_id') is not None]
            reordered_ids = self._merge_protected_stream_order(
                current_stream_ids,
                reordered_ids,
                protected_active_stream_ids,
            )
            # Dead streams have already been filtered from analyzed_streams if removal is enabled
            # If removal is disabled, allow them to remain in the channel

            # Preserve any stream IDs that are assigned to the channel in Dispatcharr but
            # were not returned by get_channel_streams() due to a stale UDI stream cache.
            # Without this guard, a stale cache causes those streams to be silently dropped
            # when the checker PATCHes the channel's stream list back to Dispatcharr.
            _uncached_ids = self._get_uncached_channel_stream_ids(
                assigned_stream_ids,
                set(reordered_ids),
                dead_stream_removal_enabled,
                dead_stream_ids,
            )
            if _uncached_ids:
                logger.warning(
                    f"Channel {channel_name}: {len(_uncached_ids)} stream ID(s) were assigned "
                    f"to the channel but absent from the UDI stream cache (stale cache?). "
                    f"Preserving in write-back to avoid accidental removal: "
                    f"{_uncached_ids[:5]}{'...' if len(_uncached_ids) > 5 else ''}"
                )
                reordered_ids.extend(_uncached_ids)

            write_back_valid_stream_ids = self._build_write_back_valid_stream_ids(
                udi,
                assigned_stream_ids,
                dead_stream_removal_enabled,
            )

            if not hasattr(update_channel_streams, "mock_calls"):
                failed_connectivity = self._require_quality_check_connectivity(
                    phase='channel_stream_update',
                    channel_id=channel_id,
                    channel_name=channel_name,
                    progress_context=profile_progress_context,
                )
                if failed_connectivity is not None:
                    return self._fail_channel_for_connectivity(
                        failed_connectivity,
                        channel_id=channel_id,
                        channel_name=channel_name,
                        queue_entry_token=queue_entry_token,
                    )

            update_authorized, _ = self._run_channel_side_effect_if_authorized(
                channel_id,
                queue_entry_token,
                lambda: update_channel_streams(
                    channel_id,
                    reordered_ids,
                    valid_stream_ids=write_back_valid_stream_ids,
                    allow_dead_streams=(not dead_stream_removal_enabled),
                    protected_stream_ids=protected_active_stream_ids,
                ),
            )
            if not update_authorized:
                return self._abort_channel_check(
                    channel_id,
                    channel_name,
                    queue_entry_token=queue_entry_token,
                )
            
            # Verify the update was applied correctly
            self.progress.update(
                channel_id=channel_id,
                channel_name=channel_name,
                current=len(streams),
                total=len(streams),
                status='verifying',
                step='Verifying update',
                step_detail='Confirming stream order was applied',
                **profile_progress_context,
            )
            
            # Only verify if enabled in configuration
            batch_config = self.config.get('batch_operations', {})
            verify_updates = batch_config.get('verify_updates', False)
            
            if verify_updates:
                time.sleep(0.5)  # Brief delay to ensure API has processed the update
                # Refresh this specific channel in UDI to get updated data after write
                udi.refresh_channel_by_id(channel_id)
                updated_channel_data = udi.get_channel_by_id(channel_id)
                if updated_channel_data:
                    updated_stream_ids = updated_channel_data.get('streams', [])
                    if updated_stream_ids == reordered_ids:
                        logger.info(f"✓ Verified: Channel {channel_name} streams reordered correctly")
                    else:
                        logger.warning(f"⚠ Verification failed: Stream order mismatch for channel {channel_name}")
                        logger.warning(f"Expected: {reordered_ids[:5]}... Got: {updated_stream_ids[:5]}...")
                else:
                    logger.warning(f"⚠ Could not verify channel {channel_name}: channel data not found after refresh")
            else:
                logger.debug(f"Skipped verification for channel {channel_name} (disabled in config)")
            
            logger.info(f"✓ Channel {channel_name} checked and streams reordered")

            # Build stream_stats unconditionally so the return dict can always
            # carry 'checked_streams' (mirrors _check_channel_concurrent behaviour).
            # The automation changelog builder reads c_result.get('checked_streams', [])
            # for every channel regardless of whether sequential or concurrent mode is
            # active — without this, sequential runs produce an empty Quality Check table.
            stream_stats = []
            try:
                averages = self._calculate_channel_averages(report_analyzed_streams, dead_stream_ids)
                for analyzed in report_analyzed_streams:
                    stream_id = analyzed.get('stream_id')
                    is_dead = stream_id in dead_stream_ids
                    is_revived = stream_id in revived_stream_ids

                    extracted_stats = extract_stream_stats(analyzed)
                    formatted_stats = format_stream_stats_for_display(extracted_stats)
                    m3u_account_name = self._get_m3u_account_name(stream_id, udi)

                    # Stamp onto analyzed dict so analyzed_lookup in check_single_channel
                    # carries the resolved name without a separate UDI call.
                    analyzed['m3u_account'] = m3u_account_name

                    stream_stat = {
                        'stream_id': stream_id,
                        'stream_name': analyzed.get('stream_name'),
                        'resolution': formatted_stats['resolution'],
                        'fps': formatted_stats['fps'],
                        'video_codec': formatted_stats['video_codec'],
                        'audio_codec': formatted_stats['audio_codec'],
                        'bitrate': formatted_stats['bitrate'],
                        'm3u_account': m3u_account_name,
                        'hdr_format': extracted_stats.get('hdr_format')
                    }

                    if is_dead:
                        stream_stat['status'] = analyzed.get('dead_reason') if analyzed.get('dead_reason') in ('blank', 'freeze', 'low_quality') else 'dead'
                    elif is_revived:
                        stream_stat['status'] = 'revived'
                        stream_stat['score'] = round(analyzed.get('score', 0), 2)
                    elif self._has_incomplete_bitrate_measurement(analyzed):
                        self._apply_incomplete_bitrate_status(stream_stat, analyzed)
                        stream_stat['score'] = round(analyzed.get('score', 0), 2)
                    else:
                        stream_stat['status'] = 'completed'
                        stream_stat['score'] = round(analyzed.get('score', 0), 2)
                        if 'status' in analyzed:
                            stream_stat['analysis_status'] = analyzed.get('status')

                    if analyzed.get('quality_reason') and analyzed.get('quality_reason') != 'none':
                        stream_stat['quality_reason'] = analyzed.get('quality_reason')
                        stream_stat['quality_reason_detail'] = analyzed.get('quality_reason_detail')
                        stream_stat['quality_reason_context'] = analyzed.get('quality_reason_context')

                    for field in VISUAL_PROBE_REPORT_FIELDS:
                        if field in analyzed:
                            stream_stat[field] = analyzed.get(field)
                    self._copy_bitrate_recheck_report_fields(stream_stat, analyzed)

                    # Include loop detection results if the probe ran
                    if analyzed.get('loop_probe_ran'):
                        stream_stat['loop_probe_ran']     = True
                        stream_stat['loop_detected']      = analyzed.get('loop_detected')
                        stream_stat['loop_duration_secs'] = analyzed.get('loop_duration_secs')
                    if analyzed.get('blank_probe_ran'):
                        stream_stat['blank_probe_ran']     = True
                        stream_stat['blank_detected']      = analyzed.get('blank_detected')
                        stream_stat['blank_duration_secs'] = analyzed.get('blank_duration_secs')
                        stream_stat['blank_ratio']         = analyzed.get('blank_ratio')
                    if analyzed.get('freeze_probe_ran'):
                        stream_stat['freeze_probe_ran']     = True
                        stream_stat['freeze_detected']      = analyzed.get('freeze_detected')
                        stream_stat['freeze_duration_secs'] = analyzed.get('freeze_duration_secs')
                        stream_stat['freeze_ratio']         = analyzed.get('freeze_ratio')

                    stream_stat = {k: v for k, v in stream_stat.items() if v not in [None, "N/A"]}
                    stream_stats.append(stream_stat)
            except Exception as e:
                logger.warning(f"Failed to build stream_stats for sequential return: {e}")
                averages = {'avg_resolution': 'N/A', 'avg_bitrate': 'N/A', 'avg_fps': 'N/A'}

            visibility_good_streams_count = (
                self._count_good_checked_streams({'checked_streams': stream_stats})
                + len(protected_active_stream_ids)
            )
            visibility_failed_streams_count = max(
                len(dead_stream_ids),
                self._count_failed_checked_streams({'checked_streams': stream_stats}),
            )
            visibility_authorized, visibility_result = (
                self._run_channel_side_effect_if_authorized(
                    channel_id,
                    queue_entry_token,
                    lambda: self._apply_channel_visibility_after_check(
                        channel_data,
                        good_streams_count=visibility_good_streams_count,
                        dead_streams_count=len(dead_stream_ids),
                        failed_streams_count=visibility_failed_streams_count,
                        revived_streams_count=len(revived_stream_ids),
                        total_streams=len(streams),
                        profile=profile,
                    ),
                )
            )
            if not visibility_authorized:
                return self._abort_channel_check(
                    channel_id,
                    channel_name,
                    queue_entry_token=queue_entry_token,
                )

            # Add changelog entry with stream stats
            if self.changelog:
                try:
                    # Get channel logo URL
                    logo_url = None
                    logo_id = channel_data.get('logo_id')
                    if logo_id:
                        logo_url = f"/api/logos/{logo_id}"
                    
                    # Add to batch changelog instead of creating individual entry
                    # Only add to batch if not explicitly skipped (e.g., when called from check_single_channel)
                    if not skip_batch_changelog:
                        batch_entry = self._build_batch_changelog_entry(
                            channel_id=channel_id,
                            channel_name=channel_name,
                            logo_url=logo_url,
                            total_streams=len(streams),
                            stream_stats=stream_stats,
                            averages=averages,
                            skipped_streams=active_viewer_skipped_streams,
                            channel_visibility=visibility_result,
                        )
                        self._add_to_batch_changelog(
                            batch_entry,
                            batch_generation=batch_changelog_generation,
                        )
                        logger.info(f"Added channel {channel_name} to batch changelog")
                except Exception as e:
                    logger.warning(f"Failed to add to batch changelog: {e}")
            
            # Update current_stream_ids to exclude dead streams that were removed
            # This prevents dead stream IDs from being saved in checked_stream_ids
            # which would cause them to be skipped by 2-hour immunity even after revival
            # Note: Using list comprehension instead of set operations to preserve order
            # Only exclude dead streams if removal is enabled
            if dead_stream_removal_enabled:
                final_stream_ids = [sid for sid in current_stream_ids if sid not in dead_stream_ids]
            else:
                final_stream_ids = current_stream_ids  # Keep all streams if removal is disabled
            if protected_active_stream_ids:
                final_stream_ids = [
                    sid
                    for sid in current_stream_ids
                    if (
                        sid in protected_active_stream_ids
                        or (not dead_stream_removal_enabled or sid not in dead_stream_ids)
                    )
                ]
            self._complete_channel_check(
                channel_id,
                lambda: self.update_tracker.mark_channel_checked(
                    channel_id,
                    stream_count=len(streams),
                    checked_stream_ids=final_stream_ids
                ),
                queue_entry_token=queue_entry_token,
            )
            
            blank_streams_count = self._count_checked_stream_status(
                {'checked_streams': stream_stats},
                'blank',
            )
            freeze_streams_count = self._count_checked_stream_status(
                {'checked_streams': stream_stats},
                'freeze',
            )
            good_streams_count = (
                self._count_good_checked_streams({'checked_streams': stream_stats})
                + len(protected_active_stream_ids)
            )

            # Return statistics for callers that need them
            return {
                'good_streams_count': good_streams_count,
                'dead_streams_count': len(dead_stream_ids),
                'blank_streams_count': blank_streams_count,
                'freeze_streams_count': freeze_streams_count,
                'revived_streams_count': len(revived_stream_ids),
                'dead_streams': [{
                    'id': s, 
                    'name': next((st.get('name') for st in streams if st['id'] == s), f'Stream {s}'),
                    'm3u_account': next((self._get_stream_m3u_account_id(st) for st in streams if st['id'] == s), None)
                } for s in dead_stream_ids],
                'revived_streams': [{
                    'id': s, 
                    'name': next((st.get('name') for st in streams if st['id'] == s), f'Stream {s}'),
                    'm3u_account': next((self._get_stream_m3u_account_id(st) for st in streams if st['id'] == s), None)
                } for s in revived_stream_ids],
                'skipped_streams': (
                    [{'id': s['id'], 'name': s.get('name', f"Stream {s['id']}")} for s in streams_already_checked]
                    + active_viewer_skipped_streams
                ),
                'checked_streams': stream_stats,
                'channel_visibility': visibility_result,
                'analyzed_streams': analyzed_streams,
            }
        except Exception as e:
            logger.error(f"Error checking channel {channel_id}: {e}", exc_info=True)
            self.check_queue.mark_failed(
                channel_id,
                str(e),
                entry_token=queue_entry_token,
            )
            
            # Add failed check to batch changelog
            # Only add to batch if not explicitly skipped
            if self.changelog and not skip_batch_changelog:
                try:
                    # Try to get channel name if available
                    try:
                        channel_name = channel_data.get('name', f'Channel {channel_id}')
                    except:
                        channel_name = f'Channel {channel_id}'
                    
                    self._add_to_batch_changelog(
                        {
                            'channel_id': channel_id,
                            'channel_name': channel_name,
                            'total_streams': 0,
                            'streams_analyzed': 0,
                            'dead_streams_detected': 0,
                            'streams_revived': 0,
                            'success': False,
                            'error': str(e),
                            'stream_stats': []
                        },
                        batch_generation=batch_changelog_generation,
                    )
                except Exception as changelog_error:
                    logger.warning(f"Failed to add to batch changelog: {changelog_error}")
            
            # Return empty stats on error
            return {
                'dead_streams_count': 0,
                'revived_streams_count': 0,
                'checked_streams': []
            }
        
        finally:
            self.checking = False
    
    def _run_loop_probes(self, analyzed_streams: list, user_agent: str = 'VLC/3.0.14', loop_penalty: float = 0.0,
                         probe_duration: int = 360, hardware_acceleration: Optional[dict] = None,
                         channel_id: int = 0, channel_name: str = '',
                         streams_detail: Optional[list] = None,
                         profile_progress_context: Optional[Dict[str, Any]] = None,
                         global_limit_override: Optional[int] = None) -> None:
        """
        Run loop detection probes on eligible streams in parallel with
        per-account concurrent limits, then write results back into each
        stream's analyzed dict.

        Eligibility criteria (both must be met):
          1. score >= LOOP_PROBE_SCORE_THRESHOLD (stream is healthy)
          2. stream is in the top LOOP_PROBE_TOP_PERCENTILE of all scored streams

        Dead streams (score == 0) and cached streams are never probed.

        Parallelism uses AccountStreamLimiter directly rather than
        SmartStreamScheduler to avoid:
          - Progress/start callback conflicts with the quality analysis UI
          - Result-shape mismatch (probe returns a tuple, not a dict)

        Each long-running probe reserves both its aggregate account slot and a
        concrete profile slot. The URL is rebuilt from the raw UDI stream with
        that reserved profile, and the probe remains preemptible when a real
        viewer needs either capacity limit.

        Account ID comes from the UDI stream record ('m3u_account_id' column,
        mapped to 'm3u_account' integer expected by AccountStreamLimiter).

        Results written into each analyzed dict (always present after this call):
          analyzed['loop_detected']      True / False / None (not probed / error)
          analyzed['loop_duration_secs'] float or None
          analyzed['loop_probe_ran']     True / False

        After all probes complete, applies loop_penalty to the score of any
        confirmed looping stream (loop_detected is True). Score is floored at
        0.0 — a looping stream is still better than no stream.

        Args:
            analyzed_streams: List of analyzed stream dicts, each with 'score' set.
            user_agent:       HTTP User-Agent forwarded to FFmpeg.
            loop_penalty:     Negative float (e.g. -0.25) subtracted from score
                              of looping streams. 0.0 = no penalty.
            probe_duration:   Seconds to run each FFmpeg probe. Derived from the
                              global max_loop_duration * 3. Clamped by
                              _probe_stream_for_loops to [60, 720]. Default 360.
            channel_id:       Channel being checked — used for progress reporting.
            channel_name:     Channel name — used for progress reporting.
            streams_detail:   Snapshot of stream_statuses values from the calling
                              check method. Used to build probe_detail for live
                              frontend grid updates. None on paths where
                              stream_statuses is not available.
            profile_progress_context: Optional Current Progress context to
                              preserve across loop-probe UI updates.
            global_limit_override: Optional per-channel worker limit inherited
                              from the quality-analysis path. Legacy sequential
                              checks pass one; normal concurrent checks pass None.
        """
        import threading
        from concurrent.futures import ThreadPoolExecutor, as_completed
        from apps.stream.stream_check_utils import _probe_stream_for_loops
        from apps.stream.concurrent_stream_limiter import (
            get_account_limiter,
            initialize_account_limits,
        )

        LOOP_PROBE_SCORE_THRESHOLD = 0.5
        LOOP_PROBE_TOP_PERCENTILE  = 0.25   # top 25%

        # Initialise loop fields on every stream so callers can always read them
        for s in analyzed_streams:
            s.setdefault('loop_detected', None)
            s.setdefault('loop_duration_secs', None)
            s.setdefault('loop_probe_ran', False)

        # Build candidate pool: alive, scored at or above threshold, not cached
        candidates = [
            s for s in analyzed_streams
            if s.get('score', 0) >= LOOP_PROBE_SCORE_THRESHOLD
            and s.get('status') != 'cached'
        ]

        if not candidates:
            logger.info("[loop-probe] No streams meet eligibility criteria — skipping all probes")
            return

        # Rank by score descending, take top percentile (minimum 1)
        candidates_sorted = sorted(candidates, key=lambda s: s.get('score', 0), reverse=True)
        cutoff  = max(1, int(len(candidates_sorted) * LOOP_PROBE_TOP_PERCENTILE))
        eligible = candidates_sorted[:cutoff]

        total = len(eligible)
        logger.info(
            f"[loop-probe] {total} stream(s) eligible for loop probe "
            f"(top {int(LOOP_PROBE_TOP_PERCENTILE * 100)}% of {len(candidates_sorted)} "
            f"scoring >= {LOOP_PROBE_SCORE_THRESHOLD}) — running in parallel"
        )

        account_limiter = get_account_limiter()
        udi = get_udi_manager()
        if not self._initialize_provider_probe_account_inventory(
            udi=udi,
            limiter=account_limiter,
            initialize_account_limits=initialize_account_limits,
            operation_label='Loop probe',
        ):
            logger.warning(
                "[loop-probe] Provider account inventory unavailable; "
                "skipping every loop candidate"
            )
            return

        if global_limit_override is not None:
            global_limit = max(1, int(global_limit_override))
        else:
            concurrent_enabled = bool(self.config.get('concurrent_streams.enabled', True))
            configured_global_limit = self.config.get('concurrent_streams.global_limit', 10)
            try:
                global_limit = max(1, int(configured_global_limit)) if concurrent_enabled else 1
            except (TypeError, ValueError):
                global_limit = 10 if concurrent_enabled else 1

        results_lock = threading.Lock()
        completed = [0]
        progress_context = dict(profile_progress_context or {})
        abort_event = getattr(self, 'abort_current_check', None)

        def abort_requested() -> bool:
            is_set = getattr(abort_event, 'is_set', None)
            return bool(is_set()) if callable(is_set) else False

        # Build probe_detail from the streams_detail snapshot passed in by the
        # calling check method. Keyed by integer stream id (same as stream_statuses).
        # Only built when streams_detail is available — the grid stays frozen
        # otherwise but the probes still run correctly.
        probe_detail: dict = {}
        if streams_detail:
            for entry in streams_detail:
                sid = entry.get('id') or entry.get('stream_id')
                if sid is not None:
                    probe_detail[sid] = dict(entry)  # shallow copy

        # Mark eligible streams as 'probing' and stamp started_at
        eligible_ids = {s.get('stream_id') for s in eligible}
        probe_start = datetime.now().isoformat()
        for sid, entry in probe_detail.items():
            if sid in eligible_ids:
                entry['status'] = 'probing'
                entry['started_at'] = probe_start

        # Emit phase-entry progress update so frontend transitions to loop phase
        if channel_id and probe_detail:
            self.progress.update(
                channel_id=channel_id,
                channel_name=channel_name,
                current=0,
                total=total,
                status='analyzing',
                step='Loop testing',
                step_detail=f'Probing {total} stream(s) for looping content',
                streams_detail=list(probe_detail.values()),
                stream_duration=probe_duration,
                **progress_context,
            )

        def finish_probe(stream: dict, completion_status: str) -> None:
            """Finalize one eligible item so no Progress row remains probing."""
            stream_id = stream.get('stream_id')
            stream_name = stream.get('stream_name', 'Unknown')
            stream_audit_ref = stream_ref(stream_id, stream.get('stream_url', ''))
            with results_lock:
                completed[0] += 1
                if probe_detail and stream_id in probe_detail:
                    loop_result = stream.get('loop_detected')
                    probe_detail[stream_id]['status'] = (
                        'loop_detected' if loop_result is True else completion_status
                    )
                if channel_id and probe_detail:
                    self.progress.update(
                        channel_id=channel_id,
                        channel_name=channel_name,
                        current=completed[0],
                        total=total,
                        status='analyzing',
                        step='Loop testing',
                        step_detail=f'Completed {completed[0]}/{total}: {stream_name}',
                        streams_detail=list(probe_detail.values()),
                        stream_duration=probe_duration,
                        **progress_context,
                    )
                logger.info(
                    f"[loop-probe] Completed {completed[0]}/{total}: {stream_audit_ref}"
                )

        def acquire_account_with_abort(account_id: Optional[int]) -> tuple[bool, str]:
            """Poll account capacity for up to 60s while honoring manual abort."""
            deadline = time.monotonic() + 60.0
            last_reason = 'timeout'
            while True:
                if abort_requested():
                    return False, 'aborted'
                acquired, reason = account_limiter.acquire(account_id, timeout=0)
                if acquired:
                    return True, reason
                last_reason = reason
                if reason == 'provider_profile_unavailable':
                    # Missing provider authority cannot recover during this
                    # operation. Do not turn an authoritative empty inventory
                    # into a 60-second wait; custom streams use account_id=None
                    # and still pass through the normal reservation path.
                    return False, last_reason
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False, last_reason
                wait_seconds = min(0.5, remaining)
                wait_for_abort = getattr(abort_event, 'wait', None)
                if callable(wait_for_abort):
                    if wait_for_abort(wait_seconds):
                        return False, 'aborted'
                else:
                    time.sleep(wait_seconds)

        def _probe_one(stream: dict) -> None:
            """Run one viewer-preemptible loop probe with account/profile reservations."""
            stream_url  = stream.get('stream_url', '')
            stream_name = stream.get('stream_name', 'Unknown')
            stream_id   = stream.get('stream_id')
            score       = stream.get('score', 0)
            stream_audit_ref = stream_ref(stream_id, stream_url)
            completion_status = 'skipped'

            if abort_requested():
                finish_probe(stream, 'aborted')
                return

            # Resolve numeric account ID from UDI.
            # analyzed dicts carry stream_id from quality analysis — use that
            # to look up the raw stream record which has m3u_account_id.
            account_id = None
            raw_stream = None
            try:
                raw_stream = udi.get_stream_by_id(int(stream_id)) if stream_id else None
                if raw_stream:
                    # SQL storage uses m3u_account_id; AccountStreamLimiter
                    # expects the integer under the key 'm3u_account'
                    account_id = raw_stream.get('m3u_account_id') or raw_stream.get('m3u_account')
            except Exception as e:
                logger.warning(f"[loop-probe:{stream_audit_ref}] Raw UDI lookup failed: {e}")

            if not isinstance(raw_stream, dict):
                logger.warning(
                    f"[loop-probe:{stream_audit_ref}] Skipping stream - raw UDI record unavailable"
                )
                finish_probe(stream, completion_status)
                return

            tag = stream_audit_ref

            reservation_stream = dict(raw_stream)
            if account_id not in (None, ''):
                reservation_stream.setdefault('m3u_account_id', account_id)
                reservation_stream.setdefault('m3u_account', account_id)

            # Acquire account slot — same mechanism used by quality analysis.
            # Timeout of 60s: if the account is saturated (e.g. live viewers
            # consuming all slots) we skip rather than block indefinitely.
            try:
                acquired_account, reason = acquire_account_with_abort(account_id)
            except Exception as e:
                logger.error(f"[loop-probe:{tag}] Account reservation failed: {e}")
                finish_probe(stream, 'error')
                return
            if not acquired_account:
                completion_status = 'aborted' if reason == 'aborted' else 'skipped'
                logger.info(
                    f"[loop-probe:{tag}] Skipping stream - "
                    f"account slot unavailable ({reason})"
                )
                finish_probe(stream, completion_status)
                return

            acquired_profile = None
            preempted_for_viewer = threading.Event()
            manual_abort_observed = threading.Event()
            preemption_token = object()
            preemption_claimed = False
            try:
                if abort_requested():
                    completion_status = 'aborted'
                    return
                profile_acquired, reason, acquired_profile, probe_url = (
                    account_limiter.reserve_profile_for_stream_with_url(reservation_stream)
                )
                if not profile_acquired:
                    logger.info(
                        f"[loop-probe:{tag}] Skipping stream - "
                        f"profile slot unavailable ({reason})"
                    )
                    return

                if not isinstance(probe_url, str) or not probe_url:
                    logger.warning(
                        f"[loop-probe:{tag}] Skipping stream - reserved profile has no usable URL"
                    )
                    return

                def should_abort_for_viewer() -> bool:
                    nonlocal preemption_claimed
                    if abort_requested():
                        manual_abort_observed.set()
                        return True
                    try:
                        should_preempt = account_limiter.should_preempt_profile_for_viewer(
                            acquired_profile,
                            account_id=account_id,
                            reservation_token=preemption_token,
                        )
                        if should_preempt:
                            preemption_claimed = True
                    except Exception as e:
                        # Fail safe: an unknown capacity state must not keep a
                        # 60-720 second provider probe alive ahead of viewers.
                        logger.warning(
                            f"[loop-probe:{tag}] Viewer preemption check failed: {e}"
                        )
                        should_preempt = True
                    if should_preempt:
                        preempted_for_viewer.set()
                    return should_preempt

                logger.info(
                    f"[loop-probe:{tag}] Probing stream "
                    f"(score: {score:.2f})"
                )
                loop_detected, loop_duration, _frames = _probe_stream_for_loops(
                    url=probe_url,
                    stream_tag=tag,
                    probe_duration=probe_duration,
                    user_agent=user_agent,
                    hardware_acceleration=hardware_acceleration,
                    should_abort=should_abort_for_viewer,
                )
                if manual_abort_observed.is_set() or abort_requested():
                    completion_status = 'aborted'
                    logger.info(f"[loop-probe:{tag}] Probe stopped by manual abort")
                    return
                if preempted_for_viewer.is_set():
                    completion_status = 'viewer_preempted'
                    logger.info(
                        f"[loop-probe:{tag}] Probe preempted because real viewer capacity is needed"
                    )
                    return
                stream['loop_detected']      = loop_detected
                stream['loop_duration_secs'] = loop_duration
                stream['loop_probe_ran']     = True
                completion_status = 'completed'

            except Exception as e:
                completion_status = 'error'
                logger.error(
                    f"[loop-probe:{tag}] Probe failed: {scrub_urls(e)}"
                )
                # loop_detected remains None — distinguishable from clean (False)
                # or detected (True)
            finally:
                if acquired_profile is not None:
                    try:
                        account_limiter.release_profile(acquired_profile)
                    except Exception as e:
                        logger.warning(f"[loop-probe:{tag}] Profile release failed: {e}")
                try:
                    account_limiter.release(account_id)
                except Exception as e:
                    logger.warning(f"[loop-probe:{tag}] Account release failed: {e}")
                if preemption_claimed:
                    try:
                        account_limiter.release_viewer_preemption_claim(preemption_token)
                    except Exception as e:
                        logger.warning(f"[loop-probe:{tag}] Preemption claim release failed: {e}")
                finish_probe(stream, completion_status)

        with ThreadPoolExecutor(max_workers=global_limit) as executor:
            futures = {}
            for stream in eligible:
                if abort_requested():
                    finish_probe(stream, 'aborted')
                    continue
                futures[executor.submit(_probe_one, stream)] = stream
            for future in as_completed(futures):
                try:
                    future.result()
                except Exception as e:
                    stream = futures[future]
                    logger.error(
                        f"[loop-probe] Unhandled error for stream "
                        f"{stream_ref(stream.get('stream_id'), stream.get('stream_url'))}: {scrub_urls(e)}"
                    )

        logger.info(
            f"[loop-probe] Probe phase complete — {completed[0]}/{total} eligible items finalized"
        )

        # Apply score penalty to confirmed looping streams.
        # Only fires when loop_penalty is non-zero and loop_detected is True.
        # Score is floored at 0.0 — looping is bad but the stream still exists.
        if loop_penalty < 0.0:
            penalised = 0
            for stream in analyzed_streams:
                if stream.get('loop_detected') is True:
                    original = stream.get('score', 0.0)
                    stream['score'] = round(max(0.0, original + loop_penalty), 2)
                    stream['loop_score_penalty'] = loop_penalty
                    penalty_ref = stream_ref(stream.get('stream_id'), stream.get('stream_url'))
                    logger.info(
                        f"[loop-probe] Penalty applied to {penalty_ref}: "
                        f"{original:.2f} → {stream['score']:.2f} (penalty={loop_penalty:+.2f})"
                    )
                    penalised += 1
            if penalised:
                logger.info(f"[loop-probe] Score penalty applied to {penalised} looping stream(s)")

    def _calculate_stream_score(self, stream_data: Dict, priority_m3u_ids: List[int] = None, priority_mode: str = 'absolute', scoring_weights: Dict = None) -> float:
        """Calculate a quality score for a stream based on analysis.
        
        Applies M3U account priority matching the order in the channel's Automation Profile.
        
        Args:
            stream_data: Dictionary of stream analysis data
            priority_m3u_ids: List of M3U account IDs in priority order (highest first)
            priority_mode: stream ordering mode; score calculation stays quality-based.
            scoring_weights: Optional per-profile scoring weights. Falls back to global config if not provided.
        """
        # Dead streams always get a score of 0
        _dead, _ = self._is_stream_dead(stream_data)
        if _dead:
            return 0.0
        
        # Use per-profile weights if provided, otherwise fall back to global config
        if scoring_weights is None:
            weights = self.config.get('scoring.weights', {})
            prefer_h265 = self.config.get('scoring.prefer_h265', True)
        else:
            weights = {
                'bitrate': scoring_weights.get('bitrate', 0.35),
                'resolution': scoring_weights.get('resolution', 0.30),
                'fps': scoring_weights.get('fps', 0.15),
                'codec': scoring_weights.get('codec', 0.10),
                'hdr': scoring_weights.get('hdr', 0.10)
            }
            prefer_h265 = scoring_weights.get('prefer_h265', True)
        
        score = 0.0
        
        # Bitrate score (0-1, normalized to typical range 1000-8000 kbps)
        bitrate = stream_data.get('bitrate_kbps', 0)
        if self._bitrate_payload_value(bitrate) is None:
            bitrate = stream_data.get('scoring_bitrate_kbps', 0)
        if isinstance(bitrate, (int, float)) and bitrate > 0:
            bitrate_score = min(bitrate / 8000, 1.0)
            score += bitrate_score * weights.get('bitrate', 0.40)
        
        # Resolution score (0-1)
        resolution = stream_data.get('resolution', 'N/A')
        resolution_score = 0.0
        if 'x' in str(resolution):
            try:
                width, height = map(int, resolution.split('x'))
                # Score based on vertical resolution
                if height >= 2160:
                    resolution_score = 1.0
                elif height >= 1080:
                    resolution_score = 0.85
                elif height >= 720:
                    resolution_score = 0.7
                elif height >= 576:
                    resolution_score = 0.5
                else:
                    resolution_score = 0.3
            except (ValueError, AttributeError):
                pass
        score += resolution_score * weights.get('resolution', 0.35)
        
        # FPS score (0-1)
        fps = stream_data.get('fps', 0)
        if isinstance(fps, (int, float)) and fps > 0:
            fps_score = min(fps / 60, 1.0)
            score += fps_score * weights.get('fps', 0.15)
        
        # Codec score (0-1)
        codec = str(stream_data.get('video_codec') or '').lower()
        codec_score = 0.0
        if codec:
            if 'h265' in codec or 'hevc' in codec:
                codec_score = 1.0 if prefer_h265 else 0.8
            elif 'h264' in codec or 'avc' in codec:
                codec_score = 0.8 if prefer_h265 else 1.0
            elif codec != 'n/a':
                codec_score = 0.5
        score += codec_score * weights.get('codec', 0.10)
        
        # HDR score (0-1)
        # Give full score for HDR10 or HLG, zero for SDR
        hdr_format = stream_data.get('hdr_format')
        hdr_score = 1.0 if hdr_format in ['HDR10', 'HLG'] else 0.0
        score += hdr_score * weights.get('hdr', 0.10)
        
        return round(score, 2)
    
    def _get_priority_boost(self, stream_id: int, stream_data: Dict, priority_m3u_ids: List[int] = None, priority_mode: str = 'absolute') -> float:
        """Calculate priority boost for a stream based on its M3U account priority.
        
        Args:
            stream_id: The stream ID
            stream_data: Stream data dictionary containing resolution and other info
            priority_m3u_ids: List of M3U account IDs in priority order (highest first)
            priority_mode: legacy boost mode, retained for compatibility.
            
        Returns:
            Priority boost value
        """
        try:
            if not priority_m3u_ids:
                return 0.0
                
            # Get stream from UDI to find its M3U account
            udi = get_udi_manager()
            stream = udi.get_stream_by_id(stream_id)
            if not stream:
                return 0.0
            
            m3u_account_id = self._get_stream_m3u_account_id(stream)
            if not m3u_account_id:
                return 0.0
            
            priority_rank = self._get_priority_account_rank(m3u_account_id, priority_m3u_ids)
            # Check if this account is in the priority list
            if priority_rank is not None:
                # Calculate boost based on position (index)
                # Lower index = higher priority
                index = priority_rank
                total_accounts = len(priority_m3u_ids)
                
                if priority_mode == 'equal':
                    # Equal Mode
                    # No priority boost, ranking matches quality exactly
                    boost = 0.0
                    logger.debug(f"Applying equal priority (no boost) to stream {stream_id}")
                elif priority_mode == 'same_resolution':
                    # Same Resolution (Tie-breaker) Mode
                    # Small boost (0.05 per step) to break ties in quality
                    # Example separation: 0.15 max (less than resolution tier gap)
                    boost = 0.05 * (total_accounts - index)
                    logger.debug(f"Applying tie-breaker priority boost of {boost} to stream {stream_id}")
                else:
                    # Absolute Mode (Default)
                    # Massive boost to override quality differences
                    # Boost formula: Base 10 + (inverted index count)
                    boost = 10.0 + (total_accounts - index)
                    logger.debug(f"Applying absolute priority boost of {boost} to stream {stream_id}")
                
                return boost
            
            return 0.0
        except Exception as e:
            logger.error(f"Error calculating priority boost for stream {stream_id}: {e}")
            return 0.0
    

    def _get_resolution_tier(self, resolution: str) -> int:
        """Map resolution string to a numeric tier (0-5, lower is better)."""
        if not resolution or 'x' not in str(resolution):
            return 5 # Unknown/N/A
            
        try:
            # Handle list/tuple format if resolution was already parsed elsewhere
            if isinstance(resolution, (list, tuple)):
                height = int(resolution[1])
            else:
                width, height = map(int, str(resolution).split('x'))
                
            if height >= 2160: return 0 # 4K
            if height >= 1080: return 1 # 1080p
            if height >= 720:  return 2 # 720p
            if height >= 576:  return 3 # 576p/SD
            return 4 # Low resolution
        except (ValueError, AttributeError, IndexError):
            return 5

    def _generate_stream_sort_key(self, stream_data: Dict, priority_m3u_ids: List[int] = None, priority_mode: str = 'absolute') -> Tuple:
        """Generate a lexicographical sort key for a stream based on priority tiers.
        
        The sort key is a tuple used for ascending sort (lower is better).
        
        Modes:
        - absolute: AccountRank, ResolutionTier, QualityScore
        - same_resolution: ResolutionTier, AccountRank, QualityScore
        - playlist_score: AccountRank, QualityScore
        - score_playlist: QualityScore, AccountRank
        """
        # 1. Account Rank (0 = highest)
        account_rank = 100
        stream_id = stream_data.get('stream_id')
        if priority_m3u_ids and stream_id:
            udi = get_udi_manager()
            stream = udi.get_stream_by_id(stream_id)
            if stream:
                m3u_id = self._get_stream_m3u_account_id(stream)
                priority_rank = self._get_priority_account_rank(m3u_id, priority_m3u_ids)
                if priority_rank is not None:
                    account_rank = priority_rank
        
        # 2. Resolution Tier (0 = highest)
        res_tier = self._get_resolution_tier(stream_data.get('resolution'))
        
        
        # 4. Quality Score (lower is better, so negate the 0-1 scale)
        quality_score = -stream_data.get('score', 0.0)
        
        if priority_mode == 'same_resolution':
            return (res_tier, account_rank, quality_score)
        elif priority_mode == 'playlist_score':
            # Playlist rank first, then quality score within the same playlist/account.
            return (account_rank, quality_score)
        elif priority_mode == 'score_playlist':
            # Score first, playlist rank only breaks equal-score ties.
            return (quality_score, account_rank)
        elif priority_mode == 'equal':
            # In 'equal' mode, resolution and quality matter, but not M3U account priority
            return (res_tier, quality_score)
        elif priority_mode == 'quality':
            # Score-only mode: sort purely by quality score, ignoring account rank and resolution tier
            return (quality_score,)
        else: # 'absolute' mode
            return (account_rank, res_tier, quality_score)
    
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
            udi = get_udi_manager()
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

    def _visibility_changelog_result(self, result: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        if not result:
            return None
        if result.get('action') in {'disabled', 'no_visibility_change', 'visible_unmanaged'}:
            return None
        return result

    @staticmethod
    def _automation_profile_progress_context(
        profile: Optional[Dict[str, Any]],
        *,
        forced_profile_id: Optional[str] = None,
    ) -> Dict[str, str]:
        if not isinstance(profile, dict):
            return {}
        profile_id = profile.get('id') or forced_profile_id
        profile_name = profile.get('name')
        context: Dict[str, str] = {}
        if profile_id not in (None, ''):
            context['automation_profile_id'] = str(profile_id)
        if profile_name:
            context['automation_profile_name'] = str(profile_name)
        context['automation_profile_source'] = 'forced' if forced_profile_id else 'resolved'
        context['run_profile_id'] = context.get('automation_profile_id')
        context['run_profile_name'] = context.get('automation_profile_name')
        context['run_profile_source'] = context.get('automation_profile_source')
        context['quality_profile_id'] = context.get('automation_profile_id')
        context['quality_profile_name'] = context.get('automation_profile_name')
        context['quality_profile_source'] = context.get('automation_profile_source')
        context['capacity_profile_name'] = 'Provider account profiles'
        context['capacity_profile_source'] = 'm3u_account_profiles'
        return context

    @staticmethod
    def _single_channel_snapshot_size_bytes(snapshot: Dict[str, Any]) -> int:
        try:
            return len(json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode("utf-8"))
        except Exception:
            return 0

    def _bound_single_channel_run_snapshot(self, snapshot: Dict[str, Any]) -> Dict[str, Any]:
        bounded = dict(snapshot or {})
        if self._single_channel_snapshot_size_bytes(bounded) <= self.SINGLE_CHANNEL_RUN_SNAPSHOT_MAX_BYTES:
            bounded["snapshot_size_bytes"] = self._single_channel_snapshot_size_bytes(bounded)
            bounded["snapshot_truncated"] = False
            return bounded

        for key in ("effective_profiles", "quality_rules", "feature_flags", "dispatcharr_status", "result_summary", "stale_warnings"):
            value = bounded.get(key)
            if isinstance(value, list):
                bounded[f"{key}_omitted_count"] = max(0, len(value) - 3)
                bounded[key] = value[:3]
            elif isinstance(value, dict):
                bounded[f"{key}_omitted"] = True
                bounded[key] = {}
            bounded["snapshot_truncated"] = True
            if self._single_channel_snapshot_size_bytes(bounded) <= self.SINGLE_CHANNEL_RUN_SNAPSHOT_MAX_BYTES:
                bounded["snapshot_size_bytes"] = self._single_channel_snapshot_size_bytes(bounded)
                return bounded

        minimal = {
            "schema_version": bounded.get("schema_version", 1),
            "run_id": bounded.get("run_id"),
            "run_mode": bounded.get("run_mode"),
            "start_source": bounded.get("start_source"),
            "started_at": bounded.get("started_at"),
            "completed_at": bounded.get("completed_at"),
            "streamflow_version": bounded.get("streamflow_version"),
            "streamflow_commit": bounded.get("streamflow_commit"),
            "snapshot_truncated": True,
            "snapshot_omitted_reason": "max_bytes_exceeded",
        }
        minimal["snapshot_size_bytes"] = self._single_channel_snapshot_size_bytes(minimal)
        return minimal

    @staticmethod
    def _single_channel_visibility_summary(visibility_result: Optional[Dict[str, Any]]) -> Dict[str, int]:
        if not isinstance(visibility_result, dict) or not visibility_result.get("changed"):
            return {
                "channels_hidden": 0,
                "channels_ready": 0,
                "channel_visibility_changed": 0,
            }
        action = visibility_result.get("action")
        return {
            "channels_hidden": 1 if action == "hidden" else 0,
            "channels_ready": 1 if action == "unhidden" else 0,
            "channel_visibility_changed": 1,
        }

    @staticmethod
    def _streamflow_version_context() -> Dict[str, Optional[str]]:
        version = os.getenv("STREAMFLOW_VERSION")
        if not version:
            current_file = Path(__file__)
            for version_file in (
                current_file.parent / "version.txt",
                current_file.parents[2] / "version.txt",
                current_file.parents[2] / "static" / "version.txt",
            ):
                try:
                    if version_file.exists():
                        value = version_file.read_text(encoding="utf-8").strip()
                        if value:
                            version = value
                            break
                except Exception:
                    continue
        commit = (
            os.getenv("STREAMFLOW_COMMIT")
            or os.getenv("STREAMFLOW_REVISION")
            or os.getenv("GITHUB_SHA")
        )
        return {
            "version": version or "dev-unknown",
            "commit": commit or None,
        }

    def _build_single_channel_run_snapshot(
        self,
        *,
        channel_id: int,
        channel_name: str,
        start_time: float,
        completed_at: datetime,
        duration_seconds: int,
        profile: Optional[Dict[str, Any]],
        profile_progress_context: Dict[str, Any],
        check_stats: Dict[str, Any],
        visibility_summary: Dict[str, int],
        checking_enabled: bool,
        matching_enabled: bool,
        m3u_update_enabled: bool,
        forced_profile_id: Optional[str],
        force_check: bool,
        provider_limit_override: bool,
        is_epg_scheduled: bool,
        m3u_refresh_scope: str,
        m3u_refresh_account_count: int,
        udi: Optional[Any] = None,
    ) -> Dict[str, Any]:
        run_mode = profile_progress_context.get("run_mode") or "single_channel_check"
        if run_mode == "teamarr_preflight":
            start_source = "teamarr_preflight"
        elif is_epg_scheduled:
            start_source = "epg_scheduled"
        elif forced_profile_id:
            start_source = "manual_forced_profile"
        else:
            start_source = "manual"

        version_context = self._streamflow_version_context()
        stream_checking = profile.get("stream_checking", {}) if isinstance(profile, dict) else {}
        dispatcharr_status: Dict[str, Any] = {}
        network_ready: Optional[bool] = None
        m3u_accounts: Optional[List[Dict[str, Any]]] = None
        try:
            if udi and hasattr(udi, "is_network_ready"):
                network_ready = bool(udi.is_network_ready())
                dispatcharr_status["network_ready"] = network_ready
        except Exception as exc:
            dispatcharr_status["network_ready_error"] = type(exc).__name__
        try:
            account_getter = getattr(udi, "get_m3u_accounts", None)
            if callable(account_getter):
                candidate_accounts = account_getter()
                if isinstance(candidate_accounts, list):
                    m3u_accounts = candidate_accounts
        except Exception as exc:
            dispatcharr_status["m3u_accounts_error"] = type(exc).__name__
        dispatcharr_status["stale_status"] = build_dispatcharr_stale_snapshot(
            network_ready=network_ready,
            accounts=m3u_accounts,
        )
        stale_warnings = build_stale_warnings(dispatcharr_stale=dispatcharr_status["stale_status"])

        profile_id = profile_progress_context.get("run_profile_id")
        profile_name = profile_progress_context.get("run_profile_name")
        snapshot = {
            "schema_version": 1,
            "run_id": f"{run_mode}-{channel_id}-{int(start_time)}",
            "run_mode": run_mode,
            "start_source": start_source,
            "started_at": datetime.fromtimestamp(start_time).isoformat(),
            "completed_at": completed_at.isoformat(),
            "duration_seconds": duration_seconds,
            "streamflow_version": version_context["version"],
            "streamflow_commit": version_context["commit"],
            "channel_id": channel_id,
            "channel_name": channel_name,
            "forced_profile_id": str(forced_profile_id) if forced_profile_id else None,
            "force_check": bool(force_check),
            "provider_limit_override": bool(provider_limit_override),
            "is_epg_scheduled": bool(is_epg_scheduled),
            "effective_profiles": [{
                "profile_id": profile_id,
                "profile_name": profile_name,
                "profile_source": profile_progress_context.get("run_profile_source"),
                "channel_count": 1,
                "quality_rules_enabled": bool(checking_enabled),
                "check_all_streams": bool(stream_checking.get("check_all_streams", False)),
                "stream_limit": stream_checking.get("stream_limit", 0),
            }],
            "effective_profile_count": 1 if profile_name or profile_id else 0,
            "channel_count": 1,
            "quality_rules": [{
                "profile_id": profile_progress_context.get("quality_profile_id"),
                "profile_name": profile_progress_context.get("quality_profile_name"),
                "enabled": bool(checking_enabled),
                "check_all_streams": bool(stream_checking.get("check_all_streams", False)),
                "stream_limit": stream_checking.get("stream_limit", 0),
            }],
            "capacity_profile_context": {
                "type": "provider_account_profiles",
                "description": "Capacity is enforced by account limits and active provider profiles.",
                "profile_limited": not bool(provider_limit_override),
            },
            "feature_flags": {
                "single_channel_checking": True,
                "m3u_update_enabled": bool(m3u_update_enabled),
                "stream_matching_enabled": bool(matching_enabled),
                "stream_checking_enabled": bool(checking_enabled),
                "force_check": bool(force_check),
                "provider_limit_override": bool(provider_limit_override),
            },
            "dispatcharr_status": dispatcharr_status,
            "stale_warnings": stale_warnings,
            "teamarr_status": {
                "preflight_context": run_mode == "teamarr_preflight",
            },
            "m3u_refresh": {
                "scope": m3u_refresh_scope,
                "account_count": int(m3u_refresh_account_count or 0),
            },
            "result_summary": {
                "total_streams": check_stats.get("total_streams", 0),
                "dead_streams": check_stats.get("dead_streams", 0),
                "avg_resolution": check_stats.get("avg_resolution"),
                "avg_bitrate": check_stats.get("avg_bitrate"),
                "avg_fps": check_stats.get("avg_fps"),
                "channels_hidden": visibility_summary.get("channels_hidden", 0),
                "channels_ready": visibility_summary.get("channels_ready", 0),
                "channel_visibility_changed": visibility_summary.get("channel_visibility_changed", 0),
            },
            "limits": {
                "max_bytes": self.SINGLE_CHANNEL_RUN_SNAPSHOT_MAX_BYTES,
            },
        }
        return self._bound_single_channel_run_snapshot(snapshot)

    def _apply_channel_visibility_after_check(
        self,
        channel_data: Dict[str, Any],
        *,
        good_streams_count: int,
        dead_streams_count: int,
        failed_streams_count: Optional[int] = None,
        revived_streams_count: int,
        total_streams: int,
        profile: Optional[Dict[str, Any]] = None,
        visibility_config: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        try:
            config = visibility_config
            if config is None:
                config = resolve_channel_visibility_config(
                    self.config.get('channel_visibility_automation', {}),
                    profile,
                )
            result = self.channel_visibility_automation.handle_quality_result(
                channel_data,
                good_streams_count=good_streams_count,
                dead_streams_count=dead_streams_count,
                failed_streams_count=failed_streams_count,
                revived_streams_count=revived_streams_count,
                config=config,
                details={
                    'total_streams': total_streams,
                    'good_streams_count': good_streams_count,
                    'dead_streams_count': dead_streams_count,
                    'failed_streams_count': failed_streams_count,
                    'revived_streams_count': revived_streams_count,
                },
            )
            if self._visibility_changelog_result(result):
                logger.info(
                    "Channel visibility automation action=%s reason=%s channel_id=%s",
                    result.get('action'),
                    result.get('reason'),
                    result.get('channel_id'),
                )
            return result
        except Exception as exc:
            logger.warning("Channel visibility automation failed after check: %s", exc)
            return {
                'action': 'visibility_error',
                'changed': False,
                'reason': 'quality_result',
                'details': {'error': 'channel_visibility_failed'},
            }
    
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
        udi = get_udi_manager()
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
                    
                channel_start_time = datetime.now()
                try:
                    # Both modes use the smart scheduler. Sequential mode limits
                    # it to one active basis probe so capacity reservations and
                    # deferred bitrate rechecks keep the same contract.
                    concurrent_enabled = self.config.get('concurrent_streams.enabled', True)
                    
                    if target_stream_ids and channel_id in target_stream_ids:
                        stream_id_whitelist = target_stream_ids[channel_id]
                    else:
                        stream_id_whitelist = None
                        
                    if concurrent_enabled:
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
                            if isinstance(channel_result, dict) and channel_result.get('aborted'):
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

                    duration_sec = (datetime.now() - channel_start_time).total_seconds()
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

    def check_single_channel(
        self,
        channel_id: int,
        program_name: Optional[str] = None,
        is_epg_scheduled: bool = False,
        forced_profile_id: Optional[str] = None,
        force_check: bool = False,
        provider_limit_override: bool = False,
        run_mode: Optional[str] = None,
        _operation_already_reserved: bool = False,
        _queue_force_check_generation: Optional[int] = None,
        _queue_entry_token: Optional[int] = None,
    ) -> Dict:
        """Check a single channel immediately and return results.
        
        This performs a targeted channel refresh for a single channel:
        - Identifies M3U accounts used by the channel
        - Refreshes playlists for accounts associated with the channel
        - Clears dead streams for the specified channel to give them a second chance
        - Re-matches and assigns streams (including previously dead ones) if matching_mode is enabled
        - Checks streams according to profile grace period/immunity settings if checking_mode is enabled
        - Detects newly dead streams and marks them (if checking is enabled)
        - Detects revived streams and marks them as alive (if checking is enabled)
        - Removes dead streams from the channel (if checking is enabled)
        
        Note: This now works like Global Action but only for the specified channel.
        Dead streams for other channels are not affected.
        
        Channel settings (matching_mode and checking_mode) are respected:
        - If matching_mode is disabled, stream matching is skipped
        - If checking_mode is disabled, stream quality checking is skipped
        
        Args:
            channel_id: ID of the channel to check
            program_name: Optional program name if this is a scheduled EPG check
            is_epg_scheduled: If True, prefer the channel's EPG scheduled profile over the period profile
            force_check: If True, bypass stream-check immunity and re-analyze all streams
            provider_limit_override: If True, bypass provider/profile capacity
                skips while still protecting active viewers.
            run_mode: Optional progress context label for specialized callers.
            
        Returns:
            Dict with check results and statistics
        """
        import time as time_module
        operation_reserved_here = False
        if not _operation_already_reserved:
            if not self._begin_single_channel_check_operation():
                return {
                    'success': False,
                    'error': 'stream_checker_active',
                    'message': 'Stream Checker work is already active',
                    'channel_id': channel_id,
                }
            operation_reserved_here = True
        if _queue_force_check_generation is not None:
            # Specialized worker entries own the snapshotted persistent queue
            # marker, not a potentially newer request for the same channel.
            force_check = True
        start_time = time_module.time()
        udi = None
        operation_progress_generation = None

        def clear_operation_progress() -> None:
            if _queue_entry_token is None:
                self.progress.clear()
                return
            self._clear_queue_entry_progress(
                channel_id,
                _queue_entry_token,
            )
        
        try:
            progress_owner_active, operation_progress_generation = (
                self._capture_operation_progress_generation()
            )
            if not progress_owner_active:
                return self._abort_channel_check(
                    channel_id,
                    queue_entry_token=_queue_entry_token,
                )
            abort_result = self._abort_channel_check_if_requested(
                channel_id,
                queue_entry_token=_queue_entry_token,
            )
            if abort_result:
                return abort_result
            logger.info(f"Starting single channel check for channel {channel_id}")
            
            # Get channel info from UDI
            udi = get_udi_manager()
            udi.set_automation_busy()
            channel = udi.get_channel_by_id(channel_id)
            if not channel:
                error_msg = f"Channel {channel_id} not found"
                logger.error(error_msg)
                return {'success': False, 'error': error_msg}
            
            channel_name = channel.get('name', f'Channel {channel_id}')
            abort_result = self._abort_channel_check_if_requested(
                channel_id,
                channel_name,
                queue_entry_token=_queue_entry_token,
            )
            if abort_result:
                return abort_result
            
            # Check if channel is in active monitoring session (coordination with monitoring system)
            session_manager = get_session_manager()
            channels_in_monitoring = session_manager.get_channels_in_active_sessions()
            
            if channel_id in channels_in_monitoring:
                logger.info(f"⏸ Skipping channel {channel_name} (ID: {channel_id}) - currently in active monitoring session")
                return {
                    'success': False,
                    'skipped': True,
                    'reason': 'in_monitoring_session',
                    'message': f'Channel {channel_name} is in an active monitoring session and cannot be checked by automation',
                    'channel_id': channel_id,
                    'channel_name': channel_name
                }
            
            # Check channel settings for matching and checking modes
            # Check channel settings for matching and checking modes via Automation Profiles
            automation_config = get_automation_config_manager()
            
            # channel dict is available in local scope
            channel_group_id = channel.get('channel_group_id')

            # Resolve the automation profile that governs this check.
            #
            # Resolution order:
            #   0. Explicitly forced profile (from ProfilePickerDialog, multi-period channels).
            #   1. EPG scheduled profile override (channel-level then group-level).
            #      Only consulted when is_epg_scheduled=True.
            #   2. Active period-based profile via get_effective_configuration.
            #   3. Hard halt — no fallback to global automation controls.
            #
            # Rationale: the opt-in model requires an explicit profile. Without one
            # the system cannot know the user's intent (matching flags, checking flags,
            # scoring weights, loop detection, minimum thresholds, etc.) and must not
            # act. Global automation controls are a system-wide on/off switch, not a
            # per-channel configuration, and are never an appropriate fallback here.

            # Step 0: Explicitly chosen profile (from ProfilePickerDialog, multi-period channels).
            # When the user selected a specific profile via the picker we honour it
            # directly, skipping the schedule-based resolution entirely.
            profile = None
            legacy_default_profile = False
            if forced_profile_id:
                profile = automation_config.get_profile(forced_profile_id)
                if profile:
                    logger.info(
                        f"Channel {channel_name}: using explicitly selected profile "
                        f"'{profile.get('name')}' (id={forced_profile_id})"
                    )
                else:
                    logger.warning(
                        f"Channel {channel_name}: forced_profile_id={forced_profile_id!r} "
                        f"not found — falling back to standard resolution"
                    )

            # Step 1: EPG scheduled profile override (EPG-triggered checks only)
            if profile is None and is_epg_scheduled:
                epg_profile = automation_config.get_effective_epg_scheduled_profile(channel_id, channel_group_id)
                if epg_profile:
                    profile = epg_profile
                    logger.info(f"Channel {channel_name}: using EPG scheduled profile '{epg_profile.get('name')}'")

            # Step 2: Active period-based profile
            if profile is None:
                config = automation_config.get_effective_configuration(channel_id, channel_group_id)
                profile = config.get('profile') if config else None

            # Step 3: Hard halt — no profile means no check.
            # This replaces the former global-controls fallback which silently ran
            # checks with system-wide defaults, ignoring per-channel intent entirely.
            if profile is None:
                try:
                    existing_profiles = automation_config.get_all_profiles(include_inactive=True)
                except TypeError:
                    existing_profiles = automation_config.get_all_profiles()
                except Exception:
                    existing_profiles = []

                if not existing_profiles:
                    logger.info(
                        f"Channel {channel_name}: using legacy single-channel default profile "
                        "because no automation profiles are configured"
                    )
                    profile = {
                        'name': 'Legacy Single Channel Default',
                        'm3u_update': {'enabled': True},
                        'stream_matching': {'enabled': True},
                        'stream_checking': {
                            'enabled': True,
                            'grace_period': False,
                            'allow_revive': False,
                        },
                    }
                    legacy_default_profile = True

            if profile is None:
                logger.warning(
                    f"⛔ Channel {channel_name} (ID: {channel_id}) has no automation "
                    f"profile assigned. Health check cannot proceed without an explicit "
                    f"profile. Assign an automation period, or for EPG checks an EPG "
                    f"scheduled profile override."
                )
                return {
                    'success': False,
                    'error': 'no_profile',
                    'message': (
                        f"Channel {channel_name} has no automation profile assigned. "
                        f"Assign an automation period with a profile before running "
                        f"a health check."
                    ),
                    'channel_id': channel_id,
                    'channel_name': channel_name,
                }

            m3u_update_enabled = profile.get('m3u_update', {}).get('enabled', False)
            matching_enabled   = profile.get('stream_matching', {}).get('enabled', False)
            checking_enabled   = profile.get('stream_checking', {}).get('enabled', False)
            _effective_profile_id_for_context = forced_profile_id or (profile.get('id') if profile else None)
            profile_progress_context = self._automation_profile_progress_context(
                profile,
                forced_profile_id=_effective_profile_id_for_context if forced_profile_id else None,
            )
            profile_progress_context['run_mode'] = run_mode or 'single_channel_check'
            if operation_progress_generation is not None:
                profile_progress_context['expected_generation'] = (
                    operation_progress_generation
                )
            m3u_refresh_scope = "disabled"
            m3u_refresh_account_ids: List[int] = []

            logger.info(
                f"Channel {channel_name} profile flags: "
                f"m3u_update={m3u_update_enabled}, "
                f"matching={matching_enabled}, "
                f"checking={checking_enabled}"
            )
            logger.info(f"UDI cache {udi.get_cache_age_description()}")

            if not legacy_default_profile:
                failed_connectivity = self._require_quality_check_connectivity(
                    phase='single_channel_preflight',
                    channel_id=channel_id,
                    channel_name=channel_name,
                    progress_context=profile_progress_context,
                )
                if failed_connectivity is not None:
                    return self._connectivity_abort_payload(
                        failed_connectivity,
                        channel_id=channel_id,
                        channel_name=channel_name,
                    )

            # Signal to the frontend that this is a single channel check so the
            # stale batch progress card from the previous automation run is suppressed.
            self.progress.update(
                channel_id=channel_id,
                channel_name=channel_name,
                current=0,
                total=1,
                status='starting',
                step='Starting single channel check',
                step_detail=f'Preparing check for {channel_name}',
                is_single_channel_check=True,
                **profile_progress_context,
            )

            def update_single_channel_progress(
                current_step: int,
                total_steps: int,
                status: str,
                step: str,
                detail: str = "",
            ):
                self.progress.update(
                    channel_id=channel_id,
                    channel_name=channel_name,
                    current=current_step,
                    total=total_steps,
                    status=status,
                    step=step,
                    step_detail=detail or step,
                    is_single_channel_check=True,
                    **profile_progress_context,
                )

            # Check if channel has active viewers or if its playlist has reached max concurrent streams
            current_streams = fetch_channel_streams(channel_id)
            if current_streams:
                limit_check_result = self._check_channel_limits(
                    channel_id,
                    channel_name,
                    current_streams,
                    provider_limit_override=provider_limit_override,
                )
                if limit_check_result is not None:
                    # A limit guard skip is an intentional no-op, not a failed
                    # single-channel check. Returning success keeps callers such
                    # as the dashboard and managed-event preflight from treating
                    # viewer/provider protection as an internal error.
                    skip_reason = limit_check_result.get('skip_reason', 'limits reached')
                    clear_operation_progress()
                    return {
                        'success': True,
                        'skipped': True,
                        'message': f"Channel check skipped: {skip_reason}",
                        'reason': skip_reason,
                        'channel_id': channel_id,
                        'channel_name': channel_name,
                        'details': limit_check_result
                    }
            
            # Step 1: Identify M3U accounts for channel (reusing current_streams from limit check above)
            logger.info(f"Step 1/6: Identifying M3U accounts for channel {channel_name}...")
            update_single_channel_progress(
                1,
                6,
                "preparing",
                "Identifying provider accounts",
                f"Finding provider accounts for {channel_name}",
            )
            account_ids = set()
            if current_streams:
                for stream in current_streams:
                    m3u_account = self._get_stream_m3u_account_id(stream)
                    if m3u_account:
                        account_ids.add(m3u_account)
            
            # Also check dead streams for this channel to find M3U accounts
            # This fixes the bug where channels with all dead streams couldn't refresh their playlists
            dead_streams = self.dead_streams_tracker.get_dead_streams_for_channel(channel_id)
            for dead_url, dead_info in dead_streams.items():
                # Try to get the stream from UDI to find its m3u_account
                stream_id = dead_info.get('stream_id')
                if stream_id:
                    stream = udi.get_stream_by_id(stream_id)
                    if stream:
                        m3u_account = self._get_stream_m3u_account_id(stream)
                        if m3u_account:
                            account_ids.add(m3u_account)
                            logger.info(
                                f"Found M3U account {m3u_account} from dead stream "
                                f"{stream_ref(stream_id, dead_url)}"
                            )
            
            # Step 2a: Provider fetch — only if m3u_update is enabled in the profile.
            #
            if m3u_update_enabled:
                m3u_refresh_account_ids, m3u_refresh_scope = _resolve_single_channel_m3u_refresh_scope(
                    profile=profile,
                    channel_account_ids=account_ids,
                    udi=udi,
                )
                logger.info(
                    "Single-channel M3U refresh scope resolved: %s account(s), scope=%s, "
                    "channel_attached_accounts=%s",
                    len(m3u_refresh_account_ids),
                    m3u_refresh_scope,
                    len(account_ids),
                )

            if legacy_default_profile:
                abort_result = self._abort_channel_check_if_requested(
                    channel_id,
                    channel_name,
                    queue_entry_token=_queue_entry_token,
                )
                if abort_result:
                    return abort_result
                logger.info(
                    f"Step 1b/6: Legacy single-channel mode - clearing dead tracker "
                    f"entries for channel {channel_name} before provider refresh..."
                )
                self.dead_streams_tracker.remove_dead_streams_by_channel_id(channel_id)

            # IMPORTANT DISTINCTION:
            #   m3u_update.enabled = True  → tell Dispatcharr to re-pull from the M3U
            #                                 provider URL (two-hop: StreamFlow → Dispatcharr
            #                                 → provider). The cache is confirmed current
            #                                 before proceeding to subsequent steps.
            #   m3u_update.enabled = False → no provider fetch is triggered. All steps
            #                                 operate on the existing cache as-is, which
            #                                 reflects the last completed cycle or refresh.
            #
            # Dispatcharr processes M3U refreshes asynchronously. After triggering the
            # refresh we poll the UDI stream count until it changes (confirming Dispatcharr
            # has finished processing) before proceeding.
            if m3u_update_enabled and m3u_refresh_account_ids:
                abort_result = self._abort_channel_check_if_requested(
                    channel_id,
                    channel_name,
                    queue_entry_token=_queue_entry_token,
                )
                if abort_result:
                    return abort_result
                logger.info(
                    f"Step 2a/6: Refreshing playlists for {len(m3u_refresh_account_ids)} M3U account(s) "
                    f"(m3u_update enabled in profile)..."
                )
                update_single_channel_progress(
                    2,
                    6,
                    "m3u_refresh",
                    "Refreshing M3U playlists",
                    f"Refreshing {len(m3u_refresh_account_ids)} provider playlist(s)",
                )
                # Capture stream count before triggering refresh so we can detect completion.
                pre_refresh_stream_count = udi.get_stream_count()

                # Import here to allow better test mocking
                from apps.core.api_utils import refresh_m3u_playlists
                for account_id in m3u_refresh_account_ids:
                    abort_result = self._abort_channel_check_if_requested(
                        channel_id,
                        channel_name,
                        queue_entry_token=_queue_entry_token,
                    )
                    if abort_result:
                        return abort_result
                    logger.info(f"Refreshing M3U account {account_id}")
                    refresh_m3u_playlists(account_id=account_id)

                logger.info(
                    "✓ Playlist refresh triggered — waiting for Dispatcharr to process..."
                )
                # Calibrate poll timeout to 115% of the last known refresh_all()
                # duration, with a floor of 5s for single channel checks (which
                # are user-triggered and must feel responsive) or 60s for
                # automation cycles (which run unattended and can afford to wait).
                _known_duration = udi.get_last_refresh_duration()
                if not isinstance(_known_duration, (int, float)):
                    _known_duration = 0
                _floor = 5
                _poll_timeout = max(_floor, int(_known_duration * 1.15)) if _known_duration > 0 else _floor
                logger.debug(
                    f"Post-refresh poll timeout: {_poll_timeout}s "
                    f"(115% of last refresh duration {_known_duration:.0f}s, floor {_floor}s)"
                )
                _wait_for_udi_stream_count_stabilise(
                    udi,
                    pre_refresh_stream_count,
                    timeout=_poll_timeout,
                    abort_event=self.abort_current_check,
                )
                abort_result = self._abort_channel_check_if_requested(
                    channel_id,
                    channel_name,
                    queue_entry_token=_queue_entry_token,
                )
                if abort_result:
                    return abort_result

                # Sync UDI cache from Dispatcharr's now-updated stream pool.
                #
                # The provider fetch above caused Dispatcharr to update its internal
                # stream database — potentially replacing stream IDs if the provider
                # rotated them. The UDI cache is now stale relative to Dispatcharr.
                # Syncing here ensures Steps 3-6 operate on current stream IDs,
                # preventing Invalid pk errors when matching writes assignments back.
                logger.info(
                    "Step 2a/6: Syncing UDI cache after provider refresh..."
                )
                update_single_channel_progress(
                    3,
                    6,
                    "cache_sync",
                    "Syncing UDI cache",
                    "Reading refreshed Dispatcharr streams into StreamFlow cache",
                )
                udi.refresh_streams()
                udi.refresh_channels()
                logger.info("✓ UDI cache synced — Steps 3-6 will use current stream IDs")
            elif m3u_update_enabled and not m3u_refresh_account_ids:
                logger.info(
                    "Step 2a/6: m3u_update enabled but no M3U accounts matched "
                    "the profile refresh scope; skipping provider fetch."
                )
                update_single_channel_progress(
                    2,
                    6,
                    "m3u_refresh",
                    "Skipping M3U refresh",
                    "No provider accounts were found for this channel",
                )
            else:
                logger.info(
                    "Step 2a/6: Skipping provider fetch (m3u_update disabled in profile). "
                    "Subsequent steps will use the current UDI cache state."
                )
                update_single_channel_progress(
                    2,
                    6,
                    "m3u_refresh",
                    "Skipping M3U refresh",
                    "M3U refresh is disabled by the selected profile",
                )

            # NOTE: No mid-pipeline UDI sync occurs here except when m3u_update=True.
            #
            # The UDI cache is the contract for all reads during this check. When
            # m3u_update.enabled = False, the existing cache is used as-is —
            # reflecting the last completed cycle, provider refresh, or startup init.
            #
            # When m3u_update.enabled = True, Step 2a fired a provider fetch that
            # caused Dispatcharr to update its stream database. The UDI cache was
            # synced immediately after the poll helper confirmed completion (above),
            # so all subsequent steps see current stream IDs.
            #
            # All writes (assignments, quality scores, stream ordering) go to Dispatcharr
            # in real time during Steps 4-6. A background UDI sync fires after this
            # function returns to pull those writes back into the cache for the next run.
            
            # Step 3: Remove stale dead-stream tracker entries whose URLs no longer exist
            # in the current playlist. This handles the URL-rotation case where a
            # provider assigns new stream IDs/URLs to the same logical streams after a
            # refresh — old dead-URL entries would otherwise block those streams from
            # ever being re-matched or re-checked.
            #
            # Dead status for URLs that ARE still present is intentionally preserved
            # so that allow_revive and remove_dead_streams toggles operate correctly
            # in Step 6 (_check_channel). Clearing all dead state here (the previous
            # behaviour) made both profile flags permanently ineffective on this path.
            abort_result = self._abort_channel_check_if_requested(
                channel_id,
                channel_name,
                queue_entry_token=_queue_entry_token,
            )
            if abort_result:
                return abort_result
            logger.info(f"Step 3/6: Cleaning stale dead stream tracker entries for channel {channel_name}...")
            update_single_channel_progress(
                3,
                6,
                "preparing",
                "Cleaning stale dead-stream entries",
                f"Cleaning stale stream URLs for {channel_name}",
            )
            try:
                # Build the set of stream URLs currently visible in the UDI cache.
                # After Step 2a this reflects the post-refresh state; when m3u_update
                # is disabled it reflects the last known cache state — either way it
                # is the correct boundary for stale-URL detection.
                if legacy_default_profile:
                    _ch_streams_for_step3 = []
                else:
                    _ch_streams_for_step3 = udi.get_channel_streams(channel_id) or []
                    if not isinstance(_ch_streams_for_step3, (list, tuple)):
                        _ch_streams_for_step3 = []
                current_stream_urls_step3 = {
                    s.get('url', '') for s in _ch_streams_for_step3
                    if isinstance(s, dict) and s.get('url')
                }
                current_stream_urls_step3.discard('')

                cleaned_count = 0 if legacy_default_profile else self.dead_streams_tracker.cleanup_removed_streams(
                    current_stream_urls_step3,
                    channel_id=channel_id,
                )
                if cleaned_count > 0:
                    logger.info(
                        f"✓ Removed {cleaned_count} stale dead stream URL(s) no longer in playlist — "
                        f"dead status for remaining URLs preserved for profile toggle evaluation"
                    )
                else:
                    logger.debug("Step 3/6: No stale dead stream URLs to clean")
            except Exception as e:
                logger.error(f"✗ Failed to clean stale dead streams: {e}", exc_info=True)
            
            # Step 4: Validate existing streams against regex patterns (if matching is enabled)
            if matching_enabled:
                logger.info(f"Step 4/6: Validating existing streams for channel {channel_name}...")
                update_single_channel_progress(
                    4,
                    6,
                    "stream_matching",
                    "Validating existing stream matches",
                    f"Checking current assignments for {channel_name}",
                )
                if not legacy_default_profile:
                    failed_connectivity = self._require_quality_check_connectivity(
                        phase='single_channel_validation_removal',
                        channel_id=channel_id,
                        channel_name=channel_name,
                        progress_context=profile_progress_context,
                    )
                    if failed_connectivity is not None:
                        clear_operation_progress()
                        return self._connectivity_abort_payload(
                            failed_connectivity,
                            channel_id=channel_id,
                            channel_name=channel_name,
                        )
                try:
                    from apps.automation.automated_stream_manager import AutomatedStreamManager
                    automation_manager = AutomatedStreamManager()
                    abort_result = self._abort_channel_check_if_requested(
                        channel_id,
                        channel_name,
                        queue_entry_token=_queue_entry_token,
                    )
                    if abort_result:
                        return abort_result
                    
                    # Run validation scoped to this channel only
                    validation_results = automation_manager.validate_and_remove_non_matching_streams(channel_id=channel_id)
                    if validation_results.get("streams_removed", 0) > 0:
                        logger.info(f"✓ Removed {validation_results['streams_removed']} non-matching streams")
                    else:
                        logger.info("✓ No non-matching streams found to remove")
                except Exception as e:
                    logger.error(f"✗ Failed to validate streams: {e}")
            else:
                logger.info(f"Step 4/6: Skipping stream validation (matching is disabled for this channel)")
                update_single_channel_progress(
                    4,
                    6,
                    "stream_matching",
                    "Skipping stream validation",
                    "Stream matching is disabled by the selected profile",
                )
            
            # Step 5: Re-match and assign streams for this specific channel (if matching is enabled)
            # With stale dead-stream URLs cleaned, streams with new URLs can be re-matched.
            #
            # Resolve dead_stream_removal_enabled from the profile so the matching step
            # respects the same policy as the checking step (Bug 3 fix). Previously,
            # discover_and_assign_streams derived this from the global StreamCheckConfig
            # which could disagree with the per-profile remove_dead_streams setting.
            _profile_sc = profile.get('stream_checking', {}) if profile else {}
            _profile_remove = _profile_sc.get('remove_dead_streams')
            if isinstance(_profile_remove, bool):
                _step5_dead_stream_removal_enabled = _profile_remove
            else:
                # No per-profile override: default to False (safe: do not remove).
                _step5_dead_stream_removal_enabled = False
            _step5_allow_dead_streams = not _step5_dead_stream_removal_enabled

            if matching_enabled:
                logger.info(f"Step 5/6: Re-matching streams for channel {channel_name}...")
                update_single_channel_progress(
                    5,
                    6,
                    "stream_matching",
                    "Matching streams",
                    f"Matching provider streams for {channel_name}",
                )
                if not legacy_default_profile:
                    failed_connectivity = self._require_quality_check_connectivity(
                        phase='single_channel_matching_update',
                        channel_id=channel_id,
                        channel_name=channel_name,
                        progress_context=profile_progress_context,
                    )
                    if failed_connectivity is not None:
                        clear_operation_progress()
                        return self._connectivity_abort_payload(
                            failed_connectivity,
                            channel_id=channel_id,
                            channel_name=channel_name,
                        )
                try:
                    # Import here to allow better test mocking
                    from apps.automation.automated_stream_manager import AutomatedStreamManager
                    automation_manager = AutomatedStreamManager()
                    abort_result = self._abort_channel_check_if_requested(
                        channel_id,
                        channel_name,
                        queue_entry_token=_queue_entry_token,
                    )
                    if abort_result:
                        return abort_result
                    
                    # Run discovery scoped to this channel only.
                    # Pass allow_dead_streams so the matching step honours the same
                    # dead-stream policy as the checking step (Bug 3 fix).
                    # Skip automatic check trigger since we'll perform the check explicitly in Step 6.
                    assignments = automation_manager.discover_and_assign_streams(
                        force=True,
                        skip_check_trigger=True,
                        channel_id=channel_id,
                        allow_dead_streams=_step5_allow_dead_streams,
                    )
                    if assignments:
                        logger.info(f"✓ Stream matching completed")
                    else:
                        logger.info("✓ No new stream assignments")
                except Exception as e:
                    logger.error(f"✗ Failed to match streams: {e}")
            else:
                logger.info(f"Step 5/6: Skipping stream matching (matching is disabled for this channel)")
                update_single_channel_progress(
                    5,
                    6,
                    "stream_matching",
                    "Skipping stream matching",
                    "Stream matching is disabled by the selected profile",
                )
            
            # After matching writes new assignments to Dispatcharr, refresh only this
            # channel's cache entry so Step 6 sees the updated stream list.
            # This is a targeted single-channel read — not a full stream pool fetch.
            if matching_enabled:
                abort_result = self._abort_channel_check_if_requested(
                    channel_id,
                    channel_name,
                    queue_entry_token=_queue_entry_token,
                )
                if abort_result:
                    return abort_result
                udi.refresh_channel_by_id(channel_id)
                logger.debug("✓ Channel cache entry updated with latest stream assignments")
            
            # Step 6: Perform the stream check (if checking is enabled)
            #
            # Resolve the profile ID to pass to _check_channel. When check_single_channel
            # was called without a forced_profile_id (e.g. EPG-triggered checks via
            # execute_scheduled_check), the correct profile was resolved above into
            # `profile` but forced_profile_id is still None. Without passing the resolved
            # ID here, _check_channel re-resolves the profile independently and falls
            # back to the active automation period — ignoring the EPG profile entirely.
            # This caused profile flags like loop_check_enabled, grace_period, allow_revive,
            # and scoring_weights to be read from the wrong profile on EPG-triggered runs.
            _effective_profile_id = forced_profile_id or (profile.get('id') if profile else None)

            dead_count = 0
            dead_stream_lookup = {}
            abort_result = self._abort_channel_check_if_requested(
                channel_id,
                channel_name,
                queue_entry_token=_queue_entry_token,
            )
            if abort_result:
                return abort_result
            if checking_enabled:
                logger.info(
                    f"Step 6/6: Checking streams for channel {channel_name} "
                    f"({'force checking all streams' if force_check else 'respecting profile grace period settings'})..."
                )
                update_single_channel_progress(
                    6,
                    6,
                    "quality_checking",
                    "Quality checking streams",
                    f"Checking stream quality for {channel_name}",
                )
                
                # Perform the check using normal profile logic.
                # Returns dict with dead_streams_count and revived_streams_count
                # Skip batch changelog since this is a single channel check
                _check_kwargs = {
                    'skip_batch_changelog': True,
                    'run_mode': profile_progress_context.get('run_mode') or 'single_channel_check',
                    'is_single_channel_check': True,
                    'expected_progress_generation': (
                        operation_progress_generation
                    ),
                }
                if _effective_profile_id:
                    _check_kwargs['forced_profile_id'] = _effective_profile_id
                if provider_limit_override:
                    _check_kwargs['provider_limit_override'] = True
                _check_kwargs['force_check_override'] = (
                    force_check
                )
                _check_kwargs['force_check_generation'] = (
                    _queue_force_check_generation
                )
                if _queue_entry_token is not None:
                    _check_kwargs['queue_entry_token'] = _queue_entry_token
                check_result = self._check_channel(channel_id, **_check_kwargs)
                if not check_result or not isinstance(check_result, dict):
                    # This should not happen with updated methods, but provide safe fallback
                    logger.warning(f"_check_channel did not return expected result dict, using defaults")
                    check_result = {'dead_streams_count': 0, 'revived_streams_count': 0}
                if check_result.get('aborted') or check_result.get('error') == 'aborted':
                    clear_operation_progress()
                    return {
                        **check_result,
                        'success': False,
                        'error': 'aborted',
                        'aborted': True,
                        'channel_id': channel_id,
                        'channel_name': channel_name,
                    }
                abort_result = self._abort_channel_check_if_requested(
                    channel_id,
                    channel_name,
                    queue_entry_token=_queue_entry_token,
                )
                if abort_result:
                    return abort_result
                
                # Get the count of dead streams that were removed during the check
                dead_count = check_result.get('dead_streams_count', 0)

                # Build an in-memory lookup keyed by stream_id from the authoritative
                # analyzed_streams list. This carries correct loop results and m3u_account
                # names without depending on UDI refresh timing or Dispatcharr staleness.
                analyzed_lookup = {
                    a.get('stream_id'): a
                    for a in check_result.get('analyzed_streams', [])
                    if a.get('stream_id') is not None
                }
                for dead_entry in check_result.get('dead_streams', []) or []:
                    if not isinstance(dead_entry, dict):
                        continue
                    dead_stream_id = dead_entry.get('stream_id', dead_entry.get('id'))
                    if dead_stream_id is not None:
                        dead_stream_lookup[dead_stream_id] = dead_entry
            else:
                logger.info(f"Step 6/6: Skipping stream checking (checking is disabled for this channel)")
                update_single_channel_progress(
                    6,
                    6,
                    "finalizing",
                    "Skipping quality check",
                    "Stream quality checking is disabled by the selected profile",
                )
                analyzed_lookup = {}
            
            # Gather statistics after check using cached channel data.
            #
            # fetch_channel_streams reads from the UDI in-memory cache — no network call.
            # For streams that were probed in this run, analyzed_lookup carries authoritative
            # scores and loop results (written to Dispatcharr during Step 6 and held in
            # memory). The background UDI sync that fires after this function returns will
            # pull those written values back into the cache for the next invocation.
            streams = fetch_channel_streams(channel_id)
            abort_result = self._abort_channel_check_if_requested(
                channel_id,
                channel_name,
                queue_entry_token=_queue_entry_token,
            )
            if abort_result:
                return abort_result
            total_streams = len(streams)
            
            # Calculate channel averages using centralized function
            channel_averages = calculate_channel_averages(streams, dead_stream_ids=set())
            
            check_stats = {
                'total_streams': total_streams,
                'dead_streams': dead_count,
                'avg_resolution': channel_averages['avg_resolution'],
                'avg_bitrate': channel_averages['avg_bitrate'],
                'avg_fps': channel_averages['avg_fps'],
                'profile_id': profile_progress_context.get('automation_profile_id'),
                'profile_name': profile_progress_context.get('automation_profile_name'),
                'automation_profile_id': profile_progress_context.get('automation_profile_id'),
                'automation_profile_name': profile_progress_context.get('automation_profile_name'),
                'automation_profile_source': profile_progress_context.get('automation_profile_source'),
                'm3u_refresh_scope': m3u_refresh_scope,
                'm3u_refresh_account_count': len(m3u_refresh_account_ids),
                'stream_details': [],
                'skipped_streams': check_result.get('skipped_streams', []) if checking_enabled else [],
            }
            
            # Sort streams by persisted quality_score descending so the
            # highest-ranked streams (including any that were loop-probed)
            # appear first. No arbitrary cap — all streams are included so
            # loop results are never hidden by a slice.
            streams_sorted = sorted(
                streams,
                key=lambda s: (s.get('stream_stats') or {}).get('quality_score') or 0,
                reverse=True
            )
            for stream in streams_sorted:
                analyzed = analyzed_lookup.get(stream.get('id'))

                # Extract stats using centralized utility
                detail_stats_source = self._current_probe_stats_source(stream, analyzed)
                extracted_stats = extract_stream_stats(detail_stats_source)
                formatted_stats = format_stream_stats_for_display(extracted_stats)
                
                # Calculate score for this stream using its stats
                # The score needs to be calculated from the stream_stats data stored in Dispatcharr
                stream_stats = stream.get('stream_stats', {})
                if stream_stats is None:
                    stream_stats = {}
                if isinstance(stream_stats, str):
                    try:
                        stream_stats = json.loads(stream_stats)
                        if stream_stats is None:
                            stream_stats = {}
                    except json.JSONDecodeError:
                        stream_stats = {}
                
                # Build stream data dict for score calculation
                score_data = {
                    'stream_id': stream.get('id'),
                    'stream_name': stream.get('name', 'Unknown'),
                    'stream_url': stream.get('url', ''),
                    'resolution': stream_stats.get('resolution', '0x0'),
                    'fps': stream_stats.get('source_fps', 0),
                    'video_codec': stream_stats.get('video_codec', 'N/A'),
                    'bitrate_kbps': stream_stats.get('ffmpeg_output_bitrate', 0),
                    'blank_probe_ran': stream_stats.get('blank_probe_ran', False),
                    'blank_detected': stream_stats.get('blank_detected', False),
                    'freeze_probe_ran': stream_stats.get('freeze_probe_ran', False),
                    'freeze_detected': stream_stats.get('freeze_detected', False),
                }
                
                # Calculate score — prefer the in-memory score from the check run
                # which already reflects loop penalties, priority weights, and profile
                # settings at the time of the check. Recalculate only as a fallback
                # for streams not present in the lookup (e.g. pre-existing streams
                # not re-analyzed in this run).
                if analyzed and analyzed.get('score') is not None:
                    score = analyzed.get('score')
                else:
                    score = self._calculate_stream_score(score_data)

                # M3U account: use the name already resolved during the check
                if analyzed and analyzed.get('m3u_account'):
                    m3u_account_name = analyzed.get('m3u_account')
                else:
                    m3u_account_name = None
                    m3u_account_id = self._get_stream_m3u_account_id(stream)
                    if m3u_account_id:
                        m3u_account_name = self._get_m3u_account_name(stream.get('id'), udi)
                
                # Build stream detail dict — include loop results if persisted
                stream_detail = {
                    'stream_id': stream.get('id'),
                    'stream_name': stream.get('name', 'Unknown'),
                    'resolution': formatted_stats['resolution'],
                    'bitrate': formatted_stats['bitrate'],
                    'video_codec': formatted_stats['video_codec'],
                    'fps': formatted_stats['fps'],
                    'score': score,
                    'm3u_account': m3u_account_name,
                    'hdr_format': extracted_stats.get('hdr_format')
                }

                quality_reason = None
                quality_reason_detail = None
                if analyzed:
                    quality_reason = analyzed.get('quality_reason')
                    if quality_reason == 'none':
                        quality_reason = None
                    quality_reason_detail = analyzed.get('quality_reason_detail')
                    if quality_reason_detail == 'none':
                        quality_reason_detail = None

                dead_reason = None
                dead_reason_detail = None
                if analyzed:
                    dead_reason = analyzed.get('dead_reason') or None
                    dead_reason_detail = analyzed.get('dead_reason_detail') or quality_reason_detail
                dead_entry = dead_stream_lookup.get(stream.get('id'))
                if dead_entry:
                    dead_reason = dead_reason or dead_entry.get('reason') or dead_entry.get('dead_reason')
                    dead_reason_detail = dead_reason_detail or dead_entry.get('reason_detail') or dead_entry.get('dead_reason_detail')

                visual_source = (
                    analyzed
                    if analyzed and 'visual_probe_ran' in analyzed
                    else stream_stats
                )
                for field in VISUAL_PROBE_REPORT_FIELDS:
                    if field in visual_source:
                        stream_detail[field] = visual_source.get(field)
                self._copy_bitrate_recheck_report_fields(stream_detail, analyzed)

                # Loop detection: prefer in-memory analyzed dict (authoritative, always
                # current) over Dispatcharr stream_stats (may lag UDI refresh timing).
                if analyzed and analyzed.get('loop_probe_ran'):
                    stream_detail['loop_probe_ran']     = True
                    stream_detail['loop_detected']      = analyzed.get('loop_detected')
                    stream_detail['loop_duration_secs'] = analyzed.get('loop_duration_secs')
                elif stream_stats.get('loop_probe_ran'):
                    # Fallback: stream not in analyzed_lookup but has persisted loop data
                    stream_detail['loop_probe_ran']     = True
                    stream_detail['loop_detected']      = stream_stats.get('loop_detected')
                    stream_detail['loop_duration_secs'] = stream_stats.get('loop_duration_secs')
                if analyzed and analyzed.get('blank_probe_ran'):
                    stream_detail['blank_probe_ran']     = True
                    stream_detail['blank_detected']      = analyzed.get('blank_detected')
                    stream_detail['blank_duration_secs'] = analyzed.get('blank_duration_secs')
                    stream_detail['blank_ratio']         = analyzed.get('blank_ratio')
                elif stream_stats.get('blank_probe_ran'):
                    stream_detail['blank_probe_ran']     = True
                    stream_detail['blank_detected']      = stream_stats.get('blank_detected')
                    stream_detail['blank_duration_secs'] = stream_stats.get('blank_duration_secs')
                    stream_detail['blank_ratio']         = stream_stats.get('blank_ratio')
                if analyzed and analyzed.get('freeze_probe_ran'):
                    stream_detail['freeze_probe_ran']     = True
                    stream_detail['freeze_detected']      = analyzed.get('freeze_detected')
                    stream_detail['freeze_duration_secs'] = analyzed.get('freeze_duration_secs')
                    stream_detail['freeze_ratio']         = analyzed.get('freeze_ratio')
                elif stream_stats.get('freeze_probe_ran'):
                    stream_detail['freeze_probe_ran']     = True
                    stream_detail['freeze_detected']      = stream_stats.get('freeze_detected')
                    stream_detail['freeze_duration_secs'] = stream_stats.get('freeze_duration_secs')
                    stream_detail['freeze_ratio']         = stream_stats.get('freeze_ratio')

                bad_quality_reasons = {'blank', 'freeze', 'low_quality', 'offline', 'error', 'failed', 'timeout'}
                if analyzed and analyzed.get('reason_detail') == 'viewer_preempted':
                    stream_detail['status'] = 'viewer_preempted'
                    stream_detail['reason'] = 'viewer_preempted'
                    stream_detail['reason_detail'] = analyzed.get('reason_detail')
                elif dead_reason:
                    stream_detail['status'] = dead_reason if dead_reason in {'blank', 'freeze', 'low_quality'} else 'dead'
                    stream_detail['reason'] = dead_reason
                    stream_detail['reason_detail'] = dead_reason_detail or dead_reason
                    stream_detail['quality_reason'] = quality_reason or dead_reason
                    stream_detail['quality_reason_detail'] = quality_reason_detail or dead_reason_detail or dead_reason
                elif quality_reason in bad_quality_reasons:
                    stream_detail['status'] = quality_reason if quality_reason in {'blank', 'freeze', 'low_quality'} else 'dead'
                    stream_detail['reason'] = quality_reason
                    stream_detail['reason_detail'] = quality_reason_detail or quality_reason
                    stream_detail['quality_reason'] = quality_reason
                    stream_detail['quality_reason_detail'] = quality_reason_detail or quality_reason
                elif stream_detail.get('blank_detected') is True:
                    stream_detail['status'] = 'blank'
                    stream_detail['reason'] = 'blank'
                    stream_detail['reason_detail'] = 'blank'
                    stream_detail['quality_reason'] = 'blank'
                    stream_detail['quality_reason_detail'] = 'blank'
                elif stream_detail.get('freeze_detected') is True:
                    stream_detail['status'] = 'freeze'
                    stream_detail['reason'] = 'freeze'
                    stream_detail['reason_detail'] = 'freeze'
                    stream_detail['quality_reason'] = 'freeze'
                    stream_detail['quality_reason_detail'] = 'freeze'
                elif self._has_incomplete_bitrate_measurement(analyzed):
                    self._apply_incomplete_bitrate_status(stream_detail, analyzed)
                else:
                    stream_detail['status'] = 'completed'
                    if quality_reason:
                        stream_detail['quality_reason'] = quality_reason
                        stream_detail['quality_reason_detail'] = quality_reason_detail

                check_stats['stream_details'].append(stream_detail)

            abort_result = self._abort_channel_check_if_requested(
                channel_id,
                channel_name,
                queue_entry_token=_queue_entry_token,
            )
            if abort_result:
                return abort_result

            # Calculate duration
            update_single_channel_progress(
                6,
                6,
                "finalizing",
                "Finalizing single channel check",
                f"Writing results for {channel_name}",
            )
            end_time = time_module.time()
            duration_seconds = int(end_time - start_time)
            
            # Format duration as human-readable string
            if duration_seconds < 60:
                duration_str = f"{duration_seconds}s"
            elif duration_seconds < 3600:
                minutes = duration_seconds // 60
                seconds = duration_seconds % 60
                duration_str = f"{minutes}m {seconds}s"
            else:
                hours = duration_seconds // 3600
                minutes = (duration_seconds % 3600) // 60
                duration_str = f"{hours}h {minutes}m"
            
            # Add duration to check stats
            check_stats['duration'] = duration_str
            check_stats['duration_seconds'] = duration_seconds
            visibility_summary = self._single_channel_visibility_summary(
                check_result.get('channel_visibility') if checking_enabled else None
            )
            check_stats.update({
                'run_mode': profile_progress_context.get('run_mode') or 'single_channel_check',
                'run_profile_id': profile_progress_context.get('run_profile_id'),
                'run_profile_name': profile_progress_context.get('run_profile_name'),
                'run_profile_source': profile_progress_context.get('run_profile_source'),
                'quality_profile_id': profile_progress_context.get('quality_profile_id'),
                'quality_profile_name': profile_progress_context.get('quality_profile_name'),
                'quality_profile_source': profile_progress_context.get('quality_profile_source'),
                'capacity_profile_name': profile_progress_context.get('capacity_profile_name'),
                'capacity_profile_source': profile_progress_context.get('capacity_profile_source'),
                'channels_hidden': visibility_summary['channels_hidden'],
                'channels_ready': visibility_summary['channels_ready'],
                'channel_visibility_changed': visibility_summary['channel_visibility_changed'],
            })
            completed_at = datetime.now()
            check_stats['run_snapshot'] = self._build_single_channel_run_snapshot(
                channel_id=channel_id,
                channel_name=channel_name,
                start_time=start_time,
                completed_at=completed_at,
                duration_seconds=duration_seconds,
                profile=profile,
                profile_progress_context=profile_progress_context,
                check_stats=check_stats,
                visibility_summary=visibility_summary,
                checking_enabled=checking_enabled,
                matching_enabled=matching_enabled,
                m3u_update_enabled=m3u_update_enabled,
                forced_profile_id=forced_profile_id,
                force_check=force_check,
                provider_limit_override=provider_limit_override,
                is_epg_scheduled=is_epg_scheduled,
                m3u_refresh_scope=m3u_refresh_scope,
                m3u_refresh_account_count=len(m3u_refresh_account_ids),
                udi=udi,
            )

            abort_result = self._abort_channel_check_if_requested(
                channel_id,
                channel_name,
                queue_entry_token=_queue_entry_token,
            )
            if abort_result:
                return abort_result

            # Add changelog entry
            if self.changelog:
                try:
                    # Get logo URL for the channel
                    logo_url = None
                    logo_id = channel.get('logo_id')
                    if logo_id:
                        logo_url = f"/api/logos/{logo_id}"
                    
                    self.changelog.add_single_channel_check_entry(
                        channel_id=channel_id,
                        channel_name=channel_name,
                        check_stats=check_stats,
                        logo_url=logo_url,
                        program_name=program_name
                    )
                except Exception as e:
                    logger.warning(f"Failed to add changelog entry: {e}")
            
            logger.info(f"✓ Single channel check completed for {channel_name} in {duration_str}")
            
            abort_result = self._abort_channel_check_if_requested(
                channel_id,
                channel_name,
                queue_entry_token=_queue_entry_token,
            )
            if abort_result:
                return abort_result

            # Note: _trigger_channel_re_enabling and _trigger_empty_channel_disabling
            # have been deprecated as they relied on obsolete Dispatcharr features

            # Clear progress so the frontend stops showing the single channel check UI
            clear_operation_progress()

            # Linearize completion against clear_queue()/request_abort(). Once
            # this lock-protected check succeeds, a later abort belongs to work
            # that starts after this already completed channel result.
            with self.lock:
                result_committed = not self.abort_current_check.is_set()
            if not result_committed:
                return self._abort_channel_check(
                    channel_id,
                    channel_name,
                    queue_entry_token=_queue_entry_token,
                )

            # Background UDI sync — pull all writes from this check back into cache.
            # Runs in a daemon thread so it does not block the response to the caller.
            # Guarded on is_network_ready() to avoid firing before startup network
            # refresh completes (is_initialized() alone is True from SQL storage load).
            if udi.is_network_ready() is True:
                def _background_udi_sync(ch_id: int, ch_name: str):
                    try:
                        _udi = get_udi_manager()
                        _udi.refresh_streams()
                        _udi.refresh_channel_by_id(ch_id)
                        logger.debug(
                            f"Background UDI sync completed for {ch_name} "
                            f"(channel {ch_id})"
                        )
                    except Exception as _e:
                        logger.warning(
                            f"Background UDI sync failed for {ch_name}: {_e}"
                        )

                threading.Thread(
                    target=_background_udi_sync,
                    args=(channel_id, channel_name),
                    daemon=True,
                    name=f"udi-sync-ch{channel_id}",
                ).start()

            return {
                'success': True,
                'channel_id': channel_id,
                'channel_name': channel_name,
                'automation_profile_id': profile_progress_context.get('automation_profile_id'),
                'automation_profile_name': profile_progress_context.get('automation_profile_name'),
                'automation_profile_source': profile_progress_context.get('automation_profile_source'),
                'run_mode': check_stats.get('run_mode'),
                'run_snapshot': check_stats.get('run_snapshot'),
                'channels_hidden': check_stats.get('channels_hidden'),
                'channels_ready': check_stats.get('channels_ready'),
                'channel_visibility_changed': check_stats.get('channel_visibility_changed'),
                'stats': check_stats
            }
            
        except Exception:
            logger.error(
                "Error checking single channel %s",
                channel_id,
                exc_info=True,
            )
            clear_operation_progress()
            return {
                'success': False,
                'error': 'single_channel_check_failed',
                'channel_id': channel_id,
            }
        finally:
            if udi is not None:
                udi.clear_automation_busy()
            if operation_reserved_here:
                self._end_single_channel_check_operation()

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

    def check_single_stream(
        self,
        stream_id: int,
        *,
        persist: bool = True,
        blank_check_enabled: bool = False,
        freeze_check_enabled: bool = False,
        loop_check_enabled: bool = False,
    ) -> Dict[str, Any]:
        """Reserve and run one synchronous, capacity-limited stream probe."""
        if not self._begin_single_stream_check_operation():
            return {
                'success': False,
                'error': 'stream_checker_active',
                'message': 'Stream Checker work is already active',
                'stream_id': stream_id,
                'run_mode': 'single_stream_check',
                'persisted': False,
            }
        try:
            return self._check_single_stream_reserved(
                stream_id,
                persist=persist,
                blank_check_enabled=blank_check_enabled,
                freeze_check_enabled=freeze_check_enabled,
                loop_check_enabled=loop_check_enabled,
            )
        finally:
            self._end_single_stream_check_operation()

    def _check_single_stream_reserved(
        self,
        stream_id: int,
        *,
        persist: bool = True,
        blank_check_enabled: bool = False,
        freeze_check_enabled: bool = False,
        loop_check_enabled: bool = False,
    ) -> Dict[str, Any]:
        """Run an already-reserved one-off quality probe.

        This intentionally does not require the stream to be assigned to a
        channel. It measures and optionally persists stream_stats only; it does
        not run regex matching, channel reordering, channel visibility changes,
        or dead-stream removal.
        """
        import time as time_module

        start_time = time_module.time()

        try:
            try:
                stream_id_int = int(stream_id)
            except (TypeError, ValueError):
                return {
                    'success': False,
                    'error': 'invalid_stream_id',
                    'message': 'stream_id must be an integer',
                }

            failed_connectivity = self._require_quality_check_connectivity(
                phase='single_stream_preflight',
                update_progress=False,
            )
            if failed_connectivity is not None:
                payload = self._connectivity_abort_payload(failed_connectivity)
                payload.update({
                    'success': False,
                    'stream_id': stream_id_int,
                    'run_mode': 'single_stream_check',
                })
                return payload

            udi = get_udi_manager()
            stream_data = udi.get_stream_by_id(stream_id_int)
            if not stream_data:
                return {
                    'success': False,
                    'error': 'stream_not_found',
                    'message': f'Stream {stream_id_int} was not found in the Dispatcharr stream cache',
                    'stream_id': stream_id_int,
                }

            stream_url = stream_data.get('url') or stream_data.get('stream_url')
            if not stream_url:
                return {
                    'success': False,
                    'error': 'stream_missing_url',
                    'message': f'Stream {stream_id_int} has no URL to probe',
                    'stream_id': stream_id_int,
                }

            stream_name = stream_data.get('name') or stream_data.get('stream_name') or f'Stream {stream_id_int}'
            analysis_params = self.config.get('stream_analysis', {}) or {}

            logger.info(
                "Starting one-off stream check for stream_ref=%s (persist=%s, blank=%s, freeze=%s, loop=%s)",
                _audit_ref('stream', stream_id_int),
                bool(persist),
                bool(blank_check_enabled),
                bool(freeze_check_enabled),
                bool(loop_check_enabled),
            )

            initial_results = self._run_capacity_limited_stream_probes(
                [stream_data],
                udi=udi,
                ffmpeg_duration=analysis_params.get('ffmpeg_duration', 30),
                timeout=analysis_params.get('timeout', 30),
                retries=analysis_params.get('retries', 1),
                retry_delay=analysis_params.get('retry_delay', 10),
                user_agent=analysis_params.get('user_agent', 'VLC/3.0.14'),
                stream_startup_buffer=analysis_params.get('stream_startup_buffer', 10),
                blank_check_enabled=bool(blank_check_enabled),
                blank_check_min_duration=analysis_params.get('blank_check_min_duration', 2.0),
                blank_check_pixel_threshold=analysis_params.get('blank_check_pixel_threshold', 0.10),
                blank_check_ratio_threshold=analysis_params.get('blank_check_ratio_threshold', 0.80),
                freeze_check_enabled=bool(freeze_check_enabled),
                freeze_check_min_duration=analysis_params.get('freeze_check_min_duration', 5.0),
                freeze_check_noise_threshold=analysis_params.get('freeze_check_noise_threshold', 0.001),
                freeze_check_ratio_threshold=analysis_params.get('freeze_check_ratio_threshold', 0.80),
                hardware_acceleration=analysis_params.get('hardware_acceleration'),
                defer_missing_bitrate_retry=True,
            )

            if self.abort_current_check.is_set():
                return {
                    'success': False,
                    'error': 'aborted',
                    'aborted': True,
                    'stream_id': stream_id_int,
                    'run_mode': 'single_stream_check',
                    'persisted': False,
                }
            if not initial_results:
                return {
                    'success': False,
                    'error': 'stream_analysis_no_result',
                    'stream_id': stream_id_int,
                    'run_mode': 'single_stream_check',
                    'persisted': False,
                }

            analyzed = initial_results[0]
            if analyzed.get('provider_limit_skipped'):
                return {
                    'success': False,
                    'error': 'provider_capacity_unavailable',
                    'reason_detail': (
                        analyzed.get('skipped_reason')
                        or analyzed.get('reason_detail')
                        or 'provider_capacity_unavailable'
                    ),
                    'stream_id': stream_id_int,
                    'stream_name': stream_name,
                    'run_mode': 'single_stream_check',
                    'persisted': False,
                    'analysis': analyzed,
                }

            def recheck_single_stream_bitrate(_stream, _initial):
                recheck_results = self._run_capacity_limited_stream_probes(
                    [stream_data],
                    udi=udi,
                    ffmpeg_duration=analysis_params.get('ffmpeg_duration', 30),
                    timeout=analysis_params.get('timeout', 30),
                    retries=0,
                    retry_delay=0,
                    user_agent=analysis_params.get('user_agent', 'VLC/3.0.14'),
                    stream_startup_buffer=analysis_params.get('stream_startup_buffer', 10),
                    blank_check_enabled=False,
                    freeze_check_enabled=False,
                    hardware_acceleration=analysis_params.get('hardware_acceleration'),
                    defer_missing_bitrate_retry=False,
                )
                return recheck_results[0] if recheck_results else None

            self._run_deferred_bitrate_rechecks(
                [analyzed],
                {stream_id_int: stream_data},
                recheck_single_stream_bitrate,
                abort_event=self.abort_current_check,
            )
            if self.abort_current_check.is_set():
                return {
                    'success': False,
                    'error': 'aborted',
                    'aborted': True,
                    'stream_id': stream_id_int,
                    'run_mode': 'single_stream_check',
                    'persisted': False,
                }
            self._apply_previous_bitrate_fallback(analyzed, stream_data)

            analyzed['m3u_account_id'] = (
                stream_data.get('m3u_account_id')
                or stream_data.get('m3u_account')
                or stream_data.get('m3u_account_id_id')
            )
            analyzed['run_mode'] = 'single_stream_check'

            threshold_config = dict(self.config.get('dead_stream_handling', {}) or {})
            if blank_check_enabled:
                threshold_config['treat_blank_as_dead'] = True
            if freeze_check_enabled:
                threshold_config['treat_freeze_as_dead'] = True

            dead_result = self._is_stream_dead(analyzed, threshold_config=threshold_config)
            self._apply_quality_classification(analyzed, dead_result)
            is_dead, dead_reason = dead_result
            analyzed['score'] = self._calculate_stream_score(analyzed)

            if loop_check_enabled:
                analysis_params_lp = self.config.get('stream_analysis', {}) or {}
                self._run_loop_probes(
                    [analyzed],
                    user_agent=analysis_params_lp.get('user_agent', 'VLC/3.0.14'),
                    loop_penalty=0.0,
                    probe_duration=analysis_params_lp.get('max_loop_duration', 120) * 3,
                    hardware_acceleration=analysis_params_lp.get('hardware_acceleration'),
                )

            if self.abort_current_check.is_set():
                return {
                    'success': False,
                    'error': 'aborted',
                    'aborted': True,
                    'stream_id': stream_id_int,
                    'run_mode': 'single_stream_check',
                    'persisted': False,
                }

            stats_payload = self._prepare_stream_stats_for_batch(analyzed)
            # Linearize the final persistence decision against clear/abort.
            # Holding the service lock through the optional write makes an
            # abort either win before persistence or belong to later work.
            with self.lock:
                commit_aborted = self.abort_current_check.is_set()
                persisted = False
                if not commit_aborted and persist:
                    persisted = self._update_stream_stats(analyzed)
            if commit_aborted:
                return {
                    'success': False,
                    'error': 'aborted',
                    'aborted': True,
                    'stream_id': stream_id_int,
                    'run_mode': 'single_stream_check',
                    'persisted': False,
                }

            duration_seconds = round(time_module.time() - start_time, 2)
            return {
                'success': True,
                'stream_id': stream_id_int,
                'stream_name': stream_name,
                'run_mode': 'single_stream_check',
                'persisted': bool(persisted),
                'persist_requested': bool(persist),
                'duration_seconds': duration_seconds,
                'dead': bool(is_dead),
                'dead_reason': dead_reason,
                'stats_payload': stats_payload.get('stream_stats', {}) if stats_payload else {},
                'analysis': analyzed,
            }

        except Exception as exc:
            logger.error("Error checking single stream %s: %s", stream_id, exc, exc_info=True)
            return {
                'success': False,
                'error': 'single_stream_check_failed',
                'stream_id': stream_id,
            }
    
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
    
    def update_config(self, updates: Dict):
        """Update service configuration and apply changes immediately."""
        # Sanitize user_agent if present
        if 'stream_analysis' in updates and 'user_agent' in updates['stream_analysis']:
            user_agent = updates['stream_analysis']['user_agent']
            # Sanitize user agent: allow alphanumeric, spaces, dots, slashes, dashes, underscores, parentheses
            import re
            sanitized = re.sub(r'[^a-zA-Z0-9 ./_\-()]+', '', str(user_agent))
            # Limit length to 200 characters
            sanitized = sanitized[:200].strip()
            if not sanitized:
                sanitized = 'VLC/3.0.14'  # Default fallback
            updates['stream_analysis']['user_agent'] = sanitized
            if sanitized != user_agent:
                logger.warning(f"User agent sanitized from '{user_agent}' to '{sanitized}'")

        if 'stream_analysis' in updates and 'hardware_acceleration' in updates['stream_analysis']:
            from apps.stream.stream_check_utils import normalize_hardware_acceleration_config
            updates['stream_analysis']['hardware_acceleration'] = normalize_hardware_acceleration_config(
                updates['stream_analysis'].get('hardware_acceleration')
            )
        
        # Log what's being updated
        config_changes = []
        if 'automation_controls' in updates:
            old_controls = self.config.get('automation_controls', {})
            new_controls = updates['automation_controls']
            for key, value in new_controls.items():
                old_value = old_controls.get(key, False)
                if old_value != value:
                    config_changes.append(f"Automation control '{key}': {old_value} → {value}")
        
        if 'global_check_schedule' in updates:
            schedule_changes = []
            schedule = updates['global_check_schedule']
            if 'hour' in schedule or 'minute' in schedule:
                old_hour = self.config.get('global_check_schedule.hour', 3)
                old_minute = self.config.get('global_check_schedule.minute', 0)
                new_hour = schedule.get('hour', old_hour)
                new_minute = schedule.get('minute', old_minute)
                if old_hour != new_hour or old_minute != new_minute:
                    schedule_changes.append(f"Time: {old_hour:02d}:{old_minute:02d} → {new_hour:02d}:{new_minute:02d}")
            if 'frequency' in schedule:
                old_freq = self.config.get('global_check_schedule.frequency', 'daily')
                new_freq = schedule['frequency']
                if old_freq != new_freq:
                    schedule_changes.append(f"Frequency: {old_freq} → {new_freq}")
            if 'enabled' in schedule:
                old_enabled = self.config.get('global_check_schedule.enabled', True)
                new_enabled = schedule['enabled']
                if old_enabled != new_enabled:
                    schedule_changes.append(f"Enabled: {old_enabled} → {new_enabled}")
            if schedule_changes:
                config_changes.append(f"Global check schedule: {', '.join(schedule_changes)}")
        
        # Apply the configuration update
        self.config.update(updates)
        if 'stream_analysis' in updates and 'hardware_acceleration' in updates['stream_analysis']:
            self._refresh_hardware_acceleration_diagnostics(log_startup=True)
        
        # Log the changes
        if config_changes:
            logger.info(f"Configuration updated: {'; '.join(config_changes)}")
        else:
            logger.info("Configuration updated")
        
        # Signal that config has changed for immediate application
        if self.running:
            self.config_changed.set()
            # Wake up the scheduler immediately by setting the trigger
            # The scheduler will check config_changed and skip channel queueing
            self.check_trigger.set()
            logger.info("Configuration changes will be applied immediately")
        
        # Reload queue max size if changed
        if 'queue' in updates and 'max_size' in updates['queue']:
            # Can't resize existing queue, but will apply on next restart
            logger.info("Queue max size updated, will apply on next restart")

    def _refresh_hardware_acceleration_diagnostics(self, *, log_startup: bool = False) -> Dict:
        """Refresh cached optional hardware acceleration diagnostics."""
        try:
            from apps.stream.stream_check_utils import (
                collect_hardware_acceleration_diagnostics,
                log_hardware_acceleration_startup_diagnostics,
            )
            config = self.config.get('stream_analysis.hardware_acceleration', {})
            diagnostics = (
                log_hardware_acceleration_startup_diagnostics(config)
                if log_startup
                else collect_hardware_acceleration_diagnostics(config)
            )
            self.hardware_acceleration_diagnostics = diagnostics
            return diagnostics
        except Exception as e:
            logger.warning(f"Unable to refresh hardware acceleration diagnostics: {e}")
            self.hardware_acceleration_diagnostics = {
                'config': self.config.get('stream_analysis.hardware_acceleration', {}),
                'error': str(e),
            }
            return self.hardware_acceleration_diagnostics

    def get_hardware_acceleration_status(self) -> Dict:
        """Return cached startup diagnostics for the current hardware config."""
        diagnostics = getattr(self, 'hardware_acceleration_diagnostics', None)
        if not diagnostics:
            diagnostics = self._refresh_hardware_acceleration_diagnostics(log_startup=False)
        return diagnostics


# Global service instance
_service_instance = None
_service_lock = threading.Lock()

def get_stream_checker_service() -> StreamCheckerService:
    """Get or create the global stream checker service instance."""
    global _service_instance
    with _service_lock:
        if _service_instance is None:
            _service_instance = StreamCheckerService()
        return _service_instance
