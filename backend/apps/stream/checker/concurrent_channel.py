"""Concurrent channel responsibilities for the shared StreamCheckerService instance."""

import logging
import json
import threading
from copy import deepcopy
from datetime import datetime
from typing import Any, Dict, List, Optional
from apps.core.log_sanitizer import audit_ref as _audit_ref, stream_context
from apps.stream.quality_report_fields import VISUAL_PROBE_REPORT_FIELDS
from apps.core.operation_timing import STREAM_OPERATION_TIMINGS
from apps.core.stream_stats_utils import extract_stream_stats, format_stream_stats_for_display
from apps.core.logging_config import log_function_call, log_function_return, log_state_change

from .concurrent_progress import create_concurrent_progress
from .bitrate_progress import create_bitrate_progress
from .heartbeat import create_status_heartbeat

logger = logging.getLogger("apps.stream.stream_checker_service")


class CheckerConcurrentChannelMixin:
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
        started_monotonic = time_module.monotonic()
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

        # A failed metadata read cannot fall through the profile fallback and
        # launch probes with stale assignments or checked-stream immunity.
        try:
            udi = self._checker_get_udi_manager()
            with STREAM_OPERATION_TIMINGS.measure('metadata_read'):
                metadata = udi.refresh_channel_metadata(channel_id)
            if not isinstance(metadata, dict) or not metadata.get('success'):
                raise RuntimeError('channel_metadata_unavailable')
            self.update_tracker.invalidate_checked_streams(channel_id, metadata.get('changed_stream_ids', []))
        except Exception as exc:
            logger.warning("Fresh metadata unavailable for channel %s: %s", channel_id, exc)
            self._fail_channel_check(channel_id, 'channel_metadata_unavailable', queue_entry_token=queue_entry_token)
            return {'success': False, 'skipped': True, 'skip_reason': 'channel_metadata_unavailable',
                    'channel_id': channel_id, 'dead_streams_count': 0, 'revived_streams_count': 0}

        try:
            automation_config = self._checker_get_automation_config_manager()
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

            udi = self._checker_get_udi_manager()
            base_url = self._checker_get_base_url()
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

            streams = self._checker_fetch_channel_streams(channel_id)
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

            _parallel_callbacks = create_concurrent_progress(
                self,
                _threshold_config=_threshold_config,
                analysis_params=analysis_params,
                channel_id=channel_id,
                channel_name=channel_name,
                completed_count=completed_count,
                get_account_limiter=get_account_limiter,
                last_published_stream_status_revision=last_published_stream_status_revision,
                priority_m3u_ids=priority_m3u_ids,
                priority_mode=priority_mode,
                profile_progress_context=profile_progress_context,
                profile_slot_account_ids=profile_slot_account_ids,
                scoring_weights=scoring_weights,
                stream_status_publish_lock=stream_status_publish_lock,
                stream_status_revision=stream_status_revision,
                stream_statuses=stream_statuses,
                stream_statuses_lock=stream_statuses_lock,
                streams_by_id=streams_by_id,
                total_streams=total_streams,
                update_run_progress=update_run_progress,
            )
            build_provider_profile_slots = _parallel_callbacks.build_provider_profile_slots
            capture_stream_statuses = _parallel_callbacks.capture_stream_statuses
            publish_stream_status_progress = _parallel_callbacks.publish_stream_status_progress
            apply_reserved_profile_progress = _parallel_callbacks.apply_reserved_profile_progress
            start_callback = _parallel_callbacks.start_callback
            apply_progress_callback = _parallel_callbacks.apply_progress_callback
            progress_callback = _parallel_callbacks.progress_callback
            defer_callback = _parallel_callbacks.defer_callback

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

                _heartbeat = create_status_heartbeat(
                    self,
                    _heartbeat_stop=_heartbeat_stop,
                    analysis_params=analysis_params,
                    capture_stream_statuses=capture_stream_statuses,
                    channel_id=channel_id,
                    channel_name=channel_name,
                    profile_progress_context=profile_progress_context,
                    publish_stream_status_progress=publish_stream_status_progress,
                    total_streams=total_streams,
                )

                _hb_thread = threading.Thread(target=_heartbeat, daemon=True, name='stream-checker-heartbeat')
                _hb_thread.start()

                bitrate_recheck_enabled = self._is_bitrate_recheck_enabled()
                try:
                    # Check streams in parallel with account-aware limits
                    results = smart_scheduler.check_streams_with_limits(
                        streams=streams_to_check,
                        check_function=self._checker_analyze_stream,
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
                        defer_missing_bitrate_retry=bitrate_recheck_enabled,
                        retry_missing_bitrate=bitrate_recheck_enabled,
                    )
                finally:
                    _heartbeat_stop.set()
                    _hb_thread.join(timeout=3)

                bitrate_recheck_progress_context = {'index': 0, 'total': 0}

                _bitrate_callbacks = create_bitrate_progress(
                    self,
                    _threshold_config=_threshold_config,
                    analysis_params=analysis_params,
                    apply_reserved_profile_progress=apply_reserved_profile_progress,
                    bitrate_recheck_progress_context=bitrate_recheck_progress_context,
                    capture_stream_statuses=capture_stream_statuses,
                    channel_id=channel_id,
                    channel_name=channel_name,
                    completed_count=completed_count,
                    priority_m3u_ids=priority_m3u_ids,
                    priority_mode=priority_mode,
                    profile_progress_context=profile_progress_context,
                    publish_stream_status_progress=publish_stream_status_progress,
                    scoring_weights=scoring_weights,
                    smart_scheduler=smart_scheduler,
                    stream_statuses=stream_statuses,
                    stream_statuses_lock=stream_statuses_lock,
                    total_streams=total_streams,
                )
                recheck_bitrate_stream = _bitrate_callbacks.recheck_bitrate_stream
                bitrate_recheck_started = _bitrate_callbacks.bitrate_recheck_started
                bitrate_recheck_start_callback = _bitrate_callbacks.bitrate_recheck_start_callback
                bitrate_recheck_defer_callback = _bitrate_callbacks.bitrate_recheck_defer_callback
                apply_bitrate_recheck_completed = _bitrate_callbacks.apply_bitrate_recheck_completed
                bitrate_recheck_completed = _bitrate_callbacks.bitrate_recheck_completed

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
                successful, failed = self._checker_batch_update_stream_stats(batch_stats_list, batch_size=batch_size)
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

            # Compare with the loaded cache IDs, not reordered_ids: the latter has
            # already been truncated by the profile stream limit.
            _uncached_ids = self._get_uncached_channel_stream_ids(
                assigned_stream_ids,
                set(current_stream_ids),
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
            reordered_ids = self._limit_write_back_stream_ids(
                reordered_ids,
                stream_limit,
                protected_active_stream_ids,
            )

            write_back_valid_stream_ids = self._build_write_back_valid_stream_ids(
                udi,
                dead_stream_removal_enabled,
            )

            if self._checker_get_session_manager().is_channel_in_active_session(channel_id):
                logger.info(
                    "Skipping channel %s write-back because monitoring now owns it",
                    channel_name,
                )
                return {
                    'success': True,
                    'skipped': True,
                    'reason': 'in_monitoring_session',
                    'channel_id': channel_id,
                    'channel_name': channel_name,
                }

            if not hasattr(self._checker_update_channel_streams, "mock_calls"):
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

            update_authorized, update_succeeded = self._run_channel_side_effect_if_authorized(
                channel_id,
                queue_entry_token,
                lambda: self._checker_update_channel_streams(
                    channel_id,
                    reordered_ids,
                    valid_stream_ids=write_back_valid_stream_ids,
                    allow_dead_streams=(not dead_stream_removal_enabled),
                    protected_stream_ids=protected_active_stream_ids,
                    expected_current_stream_ids=assigned_stream_ids,
                ),
            )
            if not update_authorized:
                return self._abort_channel_check(
                    channel_id,
                    channel_name,
                    queue_entry_token=queue_entry_token,
                )
            if not update_succeeded:
                raise RuntimeError(
                    f"Dispatcharr rejected stream assignment for channel {channel_id}"
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
                'success': False,
                'error': str(e)
            }

        finally:
            self.checking = False
            log_function_return(logger, "_check_channel_concurrent")

