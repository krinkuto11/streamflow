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


from apps.stream.quality_report_fields import VISUAL_PROBE_REPORT_FIELDS, BITRATE_RECHECK_REPORT_FIELDS
from apps.stream.checker import CheckerOperationsMixin
from apps.core.operation_timing import STREAM_OPERATION_TIMINGS

_STREAM_STATUS_HEARTBEAT_INTERVAL_SECONDS = 2.0

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

from apps.stream.checker.constants import SPECIALIZED_QUEUE_SOURCES

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


from apps.stream.checker.refresh_scope import (
    _coerce_m3u_account_id,
    _dedupe_m3u_account_ids,
    _is_active_non_custom_m3u_account,
    _resolve_single_channel_m3u_refresh_scope,
    _sort_m3u_account_ids,
    _wait_for_udi_stream_count_stabilise,
)

class StreamCheckerService(CheckerOperationsMixin):
    """Main service for managing stream checking operations."""
    SINGLE_CHANNEL_RUN_SNAPSHOT_MAX_BYTES = 50 * 1024


    # Keep connector/media dependencies at the facade boundary. Extracted
    # behaviors call these adapters; tests and integrations can replace the
    # existing module bindings without copying or changing shared state.
    # Properties preserve the original callable identity and its attributes.

    @property
    def _checker_get_udi_manager(self):
        return get_udi_manager

    @property
    def _checker_fetch_channel_streams(self):
        return fetch_channel_streams

    @property
    def _checker_get_automation_config_manager(self):
        return get_automation_config_manager

    @property
    def _checker_get_session_manager(self):
        return get_session_manager

    @property
    def _checker_update_channel_streams(self):
        return update_channel_streams

    @property
    def _checker_get_base_url(self):
        return _get_base_url

    @property
    def _checker_batch_update_stream_stats(self):
        return batch_update_stream_stats

    @property
    def _checker_analyze_stream(self):
        return analyze_stream

    @property
    def _checker_wait_for_udi_stream_count_stabilise(self):
        return _wait_for_udi_stream_count_stabilise

    @property
    def _checker_heartbeat_interval_seconds(self):
        return _STREAM_STATUS_HEARTBEAT_INTERVAL_SECONDS

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






















    # Deprecated: _trigger_empty_channel_disabling and _trigger_channel_re_enabling
    # were removed as they relied on a missing module 'empty_channel_manager'
    # and obsolete Dispatcharr features.



    # Removed _refine_sorted_streams in favor of lexicographical Sort Keys.



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
