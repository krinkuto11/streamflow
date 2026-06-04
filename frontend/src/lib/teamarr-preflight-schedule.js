const DEFAULT_PREFLIGHT_OFFSET_MINUTES = 20
const DEFAULT_POST_START_GRACE_MINUTES = 5

const positiveMinutes = (values = []) => (
  values
    .map(value => Number(value))
    .filter(value => Number.isFinite(value) && value > 0)
)

const uniquePositiveMinutes = (values = []) => [...new Set(positiveMinutes(values))]

const boundedCount = (value) => {
  const parsed = Number(value)
  if (!Number.isFinite(parsed)) return null
  return Math.max(0, Math.min(10, Math.floor(parsed)))
}

const distributedPreStartOffsets = (preflightOffset, retryCount) => {
  const offsets = [preflightOffset]
  if (retryCount <= 0) return offsets
  const denominator = retryCount + 1
  for (let index = 1; index <= retryCount; index += 1) {
    const offset = Math.max(1, Math.ceil((preflightOffset * (retryCount - index + 1)) / denominator))
    if (offset < preflightOffset) offsets.push(offset)
  }
  return [...new Set(offsets)].sort((a, b) => b - a)
}

const distributedPostStartOffsets = (graceMinutes, retryCount) => {
  if (graceMinutes <= 0 || retryCount <= 0) return []
  const denominator = retryCount + 1
  const offsets = []
  for (let index = 1; index <= retryCount; index += 1) {
    const offset = Math.max(1, Math.ceil((graceMinutes * index) / denominator))
    offsets.push(Math.min(graceMinutes, offset))
  }
  return [...new Set(offsets)].sort((a, b) => a - b)
}

const preStartOffsets = (config = {}) => {
  const rawPreflightOffset = Number(config.preflight_offset_minutes)
  const preflightOffset = Number.isFinite(rawPreflightOffset) && rawPreflightOffset > 0
    ? rawPreflightOffset
    : DEFAULT_PREFLIGHT_OFFSET_MINUTES
  const retryCount = boundedCount(config.pre_start_retry_count)
  if (retryCount !== null) return distributedPreStartOffsets(preflightOffset, retryCount)
  const retryOffsets = positiveMinutes(config.retry_offsets_minutes || [])
    .filter(offset => offset <= preflightOffset)
  return uniquePositiveMinutes([preflightOffset, ...retryOffsets]).sort((a, b) => b - a)
}

const postStartOffsets = (config = {}) => {
  const retryCount = boundedCount(config.post_start_retry_count)
  if (retryCount !== null) {
    const rawGrace = Number(config.post_start_grace_minutes)
    const grace = Number.isFinite(rawGrace) && rawGrace > 0 ? rawGrace : DEFAULT_POST_START_GRACE_MINUTES
    return distributedPostStartOffsets(grace, retryCount)
  }
  return uniquePositiveMinutes(config.post_start_offsets_minutes || []).sort((a, b) => a - b)
}

const eventSecondsToStart = (event, eventAt) => {
  const seconds = Number(event?.seconds_to_start)
  if (Number.isFinite(seconds)) return seconds
  return Math.floor((eventAt.getTime() - Date.now()) / 1000)
}

export const getTeamarrNextAutomaticCheck = (event = {}, config = {}) => {
  const state = String(event?.state || '').trim()
  if (state === 'due') {
    return {
      label: 'Due now',
      bucket: event.trigger_bucket || null,
      timestamp: null,
    }
  }
  if (!['scheduled', 'already_attempted'].includes(state)) return null

  const eventAt = new Date(event.event_date)
  if (Number.isNaN(eventAt.getTime())) return null

  const seconds = eventSecondsToStart(event, eventAt)
  if (seconds >= 0) {
    const nextOffset = preStartOffsets(config).find(offset => seconds > offset * 60)
    if (!nextOffset) return null
    return {
      label: 'Next auto check',
      bucket: `-${nextOffset}m`,
      timestamp: new Date(eventAt.getTime() - nextOffset * 60 * 1000).toISOString(),
    }
  }

  const elapsedSeconds = Math.abs(seconds)
  const nextOffset = postStartOffsets(config).find(offset => elapsedSeconds < offset * 60)
  if (!nextOffset) return null
  return {
    label: 'Next auto check',
    bucket: `+${nextOffset}m`,
    timestamp: new Date(eventAt.getTime() + nextOffset * 60 * 1000).toISOString(),
  }
}

export const getTeamarrAutomaticCheck = (event = {}, config = {}) => (
  event?.next_automatic_check || getTeamarrNextAutomaticCheck(event, config)
)
