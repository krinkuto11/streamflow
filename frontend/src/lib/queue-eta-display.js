import { formatDuration } from './time-format'

export function getQueueEtaDisplay(queue) {
  const etaSeconds = Number(queue?.eta_seconds)
  if (Number.isFinite(etaSeconds) && etaSeconds > 0) {
    return {
      state: 'ready',
      label: `~${formatDuration(etaSeconds)} remaining`,
      pulse: false,
    }
  }

  const hasWork = [
    queue?.queue_size,
    queue?.queued,
    queue?.in_progress,
    queue?.queued_streams_count,
    queue?.in_progress_streams_count,
  ].some(value => Number(value) > 0)
  const avgSeconds = Number(queue?.avg_stream_process_time_sec)

  if (hasWork && (!Number.isFinite(avgSeconds) || avgSeconds <= 0)) {
    return {
      state: 'learning',
      label: 'Learning ETA',
      pulse: true,
    }
  }

  if (hasWork) {
    return {
      state: 'pending',
      label: 'ETA pending',
      pulse: true,
    }
  }

  return {
    state: 'idle',
    label: '',
    pulse: false,
  }
}
