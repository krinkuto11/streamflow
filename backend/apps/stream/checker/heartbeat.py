"""Heartbeat callbacks sharing the caller-owned state and synchronization."""

def create_status_heartbeat(
    service,
    *,
    _heartbeat_stop,
    analysis_params,
    capture_stream_statuses,
    channel_id,
    channel_name,
    profile_progress_context,
    publish_stream_status_progress,
    total_streams,
):
    """Bind callbacks to existing run values; no copied state or additional locks."""
    self = service

    def _heartbeat():
        while (
            not self.abort_current_check.is_set()
            and not _heartbeat_stop.wait(
                self._checker_heartbeat_interval_seconds
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

    return _heartbeat
