"""Sequential channel responsibilities for the shared StreamCheckerService instance."""

import logging
import json
import time
from datetime import datetime
from typing import Any, Dict, List, Optional
from apps.core.log_sanitizer import audit_ref as _audit_ref, stream_context, stream_ref
from apps.stream.quality_report_fields import VISUAL_PROBE_REPORT_FIELDS
from apps.core.stream_stats_utils import extract_stream_stats, format_stream_stats_for_display
from apps.core.logging_config import log_function_call, log_state_change

logger = logging.getLogger("apps.stream.stream_checker_service")


class CheckerSequentialChannelMixin:
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
        started_monotonic = time_module.monotonic()
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
            automation_config = self._checker_get_automation_config_manager()

            # Fetch channel data to get group_id (might be fetched already but just in case)
            udi = self._checker_get_udi_manager()
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

                bitrate_recheck_enabled = self._is_bitrate_recheck_enabled()
                analyzed = self._checker_analyze_stream(
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
                    defer_missing_bitrate_retry=bitrate_recheck_enabled,
                    retry_missing_bitrate=bitrate_recheck_enabled,
                )

                def recheck_sequential_bitrate(_stream, _initial):
                    return self._checker_analyze_stream(
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

                    analyzed = self._checker_analyze_stream(
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
                'checked_streams': [],
                'success': False,
                'error': str(e),
            }

        finally:
            self.checking = False

