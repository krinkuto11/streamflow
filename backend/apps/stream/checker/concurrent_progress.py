"""Concurrent progress callbacks sharing the caller-owned state and synchronization."""

import logging
from typing import Any, Callable, NamedTuple
from copy import deepcopy
from datetime import datetime

logger = logging.getLogger("apps.stream.stream_checker_service")


class ConcurrentProgressCallbacks(NamedTuple):
    build_provider_profile_slots: Callable[..., Any]
    capture_stream_statuses: Callable[..., Any]
    publish_stream_status_progress: Callable[..., Any]
    apply_reserved_profile_progress: Callable[..., Any]
    start_callback: Callable[..., Any]
    apply_progress_callback: Callable[..., Any]
    progress_callback: Callable[..., Any]
    defer_callback: Callable[..., Any]


def create_concurrent_progress(
    service,
    *,
    _threshold_config,
    analysis_params,
    channel_id,
    channel_name,
    completed_count,
    get_account_limiter,
    last_published_stream_status_revision,
    priority_m3u_ids,
    priority_mode,
    profile_progress_context,
    profile_slot_account_ids,
    scoring_weights,
    stream_status_publish_lock,
    stream_status_revision,
    stream_statuses,
    stream_statuses_lock,
    streams_by_id,
    total_streams,
    update_run_progress,
):
    """Bind callbacks to existing run values; no copied state or additional locks."""
    self = service

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

    return ConcurrentProgressCallbacks(
        build_provider_profile_slots,
        capture_stream_statuses,
        publish_stream_status_progress,
        apply_reserved_profile_progress,
        start_callback,
        apply_progress_callback,
        progress_callback,
        defer_callback,
    )
