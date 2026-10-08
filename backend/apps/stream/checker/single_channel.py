"""Single channel responsibilities for the shared StreamCheckerService instance."""

import logging
import json
import threading
from datetime import datetime
from typing import Dict, List, Optional
from apps.core.log_sanitizer import stream_ref
from apps.stream.quality_report_fields import VISUAL_PROBE_REPORT_FIELDS
from apps.core.stream_stats_utils import extract_stream_stats, format_stream_stats_for_display, calculate_channel_averages
from .refresh_scope import _resolve_single_channel_m3u_refresh_scope

logger = logging.getLogger("apps.stream.stream_checker_service")


class CheckerSingleChannelMixin:
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
        started_monotonic = time_module.monotonic()
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
            udi = self._checker_get_udi_manager()
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
            session_manager = self._checker_get_session_manager()
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
            automation_config = self._checker_get_automation_config_manager()

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
            current_streams = self._checker_fetch_channel_streams(channel_id)
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
                self._checker_wait_for_udi_stream_count_stabilise(
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
                    if not isinstance(validation_results, dict) or (
                        validation_results.get('success') is False
                        or validation_results.get('aborted')
                        or validation_results.get('error')
                    ):
                        clear_operation_progress()
                        if isinstance(validation_results, dict) and validation_results.get('aborted'):
                            logger.info("Stream validation aborted for channel %s", channel_name)
                            return {
                                'success': False,
                                'error': 'aborted',
                                'aborted': True,
                                'channel_id': channel_id,
                                'channel_name': channel_name,
                            }
                        validation_error = (
                            validation_results.get('error')
                            if isinstance(validation_results, dict) else None
                        )
                        logger.error(
                            "Stream validation failed for channel %s: %s",
                            channel_name,
                            validation_error,
                        )
                        return {
                            'success': False,
                            'error': validation_error or 'stream_validation_failed',
                            'channel_id': channel_id,
                            'channel_name': channel_name,
                        }
                    if validation_results.get("streams_removed", 0) > 0:
                        logger.info(f"✓ Removed {validation_results['streams_removed']} non-matching streams")
                    else:
                        logger.info("✓ No non-matching streams found to remove")
                except Exception:
                    logger.error("Failed to validate streams for channel %s", channel_name, exc_info=True)
                    clear_operation_progress()
                    return {
                        'success': False,
                        'error': 'stream_validation_failed',
                        'channel_id': channel_id,
                        'channel_name': channel_name,
                    }
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
                    if isinstance(assignments, dict) and (
                        assignments.get('success') is False or assignments.get('aborted')
                    ):
                        clear_operation_progress()
                        if assignments.get('aborted'):
                            logger.info("Stream matching aborted for channel %s", channel_name)
                            return {
                                'success': False,
                                'error': 'aborted',
                                'aborted': True,
                                'channel_id': channel_id,
                                'channel_name': channel_name,
                            }
                        logger.error(
                            "Stream matching failed for channel %s: %s",
                            channel_name,
                            assignments.get('error'),
                        )
                        return {
                            'success': False,
                            'error': assignments.get('error') or 'stream_matching_failed',
                            'channel_id': channel_id,
                            'channel_name': channel_name,
                        }
                    if assignments:
                        logger.info(f"✓ Stream matching completed")
                    else:
                        logger.info("✓ No new stream assignments")
                except Exception:
                    logger.error("Failed to match streams for channel %s", channel_name, exc_info=True)
                    clear_operation_progress()
                    return {
                        'success': False,
                        'error': 'stream_matching_failed',
                        'channel_id': channel_id,
                        'channel_name': channel_name,
                    }
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
                    logger.error("Quality check returned no valid result for channel %s", channel_name)
                    clear_operation_progress()
                    return {
                        'success': False,
                        'error': 'channel_check_failed',
                        'channel_id': channel_id,
                        'channel_name': channel_name,
                    }
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
                if check_result.get('success') is False or check_result.get('error'):
                    clear_operation_progress()
                    return {
                        **check_result,
                        'success': False,
                        'error': check_result.get('error') or 'channel_check_failed',
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
            streams = self._checker_fetch_channel_streams(channel_id)
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
            duration_seconds = int(time_module.monotonic() - started_monotonic)

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
                        _udi = self._checker_get_udi_manager()
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

