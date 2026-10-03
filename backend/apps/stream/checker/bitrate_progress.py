"""Bitrate progress callbacks sharing the caller-owned state and synchronization."""

import logging
from typing import Any, Callable, NamedTuple
from datetime import datetime

logger = logging.getLogger("apps.stream.stream_checker_service")


class BitrateProgressCallbacks(NamedTuple):
    recheck_bitrate_stream: Callable[..., Any]
    bitrate_recheck_started: Callable[..., Any]
    bitrate_recheck_start_callback: Callable[..., Any]
    bitrate_recheck_defer_callback: Callable[..., Any]
    apply_bitrate_recheck_completed: Callable[..., Any]
    bitrate_recheck_completed: Callable[..., Any]


def create_bitrate_progress(
    service,
    *,
    _threshold_config,
    analysis_params,
    apply_reserved_profile_progress,
    bitrate_recheck_progress_context,
    capture_stream_statuses,
    channel_id,
    channel_name,
    completed_count,
    priority_m3u_ids,
    priority_mode,
    profile_progress_context,
    publish_stream_status_progress,
    scoring_weights,
    smart_scheduler,
    stream_statuses,
    stream_statuses_lock,
    total_streams,
):
    """Bind callbacks to existing run values; no copied state or additional locks."""
    self = service

    def recheck_bitrate_stream(stream, _initial):
        recheck_results = smart_scheduler.check_streams_with_limits(
            streams=[stream],
            check_function=self._checker_analyze_stream,
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

    return BitrateProgressCallbacks(
        recheck_bitrate_stream,
        bitrate_recheck_started,
        bitrate_recheck_start_callback,
        bitrate_recheck_defer_callback,
        apply_bitrate_recheck_completed,
        bitrate_recheck_completed,
    )
