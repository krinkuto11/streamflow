import { formatDuration } from './time-format'

const EARLY_ETA_SAMPLE_FLOOR = 10
const CONSERVATIVE_BASIS = new Set([
  'channel',
  'channel_floor',
  'stream_floor',
  'stream_timeout_floor',
])

function numericQueueValue(value) {
  const number = Number(value)
  return Number.isFinite(number) ? number : 0
}

export function getQueueEtaDisplay(queue) {
  const etaSeconds = Number(queue?.eta_seconds)
  if (Number.isFinite(etaSeconds) && etaSeconds > 0) {
    const processedCount = numericQueueValue(queue?.completed) + numericQueueValue(queue?.failed)
    const basis = queue?.eta_basis_detail || queue?.eta_basis
    const effectiveWorkers = numericQueueValue(queue?.eta_effective_workers)
    const configuredWorkers = numericQueueValue(queue?.eta_configured_workers)
    const conservative = CONSERVATIVE_BASIS.has(basis)
      || (effectiveWorkers > 0 && configuredWorkers > 0 && effectiveWorkers < configuredWorkers)
    const prefix = processedCount < EARLY_ETA_SAMPLE_FLOOR
      ? (conservative ? 'Early conservative ETA' : 'Early ETA')
      : (conservative ? 'Conservative ETA' : null)
    const label = prefix
      ? `${prefix}: ~${formatDuration(etaSeconds)} remaining`
      : `~${formatDuration(etaSeconds)} remaining`

    if (processedCount < EARLY_ETA_SAMPLE_FLOOR) {
      return {
        state: 'early',
        label,
        pulse: false,
      }
    }

    return {
      state: 'ready',
      label,
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
