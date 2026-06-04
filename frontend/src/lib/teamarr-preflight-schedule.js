const DEFAULT_PREFLIGHT_OFFSET_MINUTES = 20

const positiveMinutes = (values = []) => (
  values
    .map(value => Number(value))
    .filter(value => Number.isFinite(value) && value > 0)
)

const uniquePositiveMinutes = (values = []) => [...new Set(positiveMinutes(values))]

const preStartOffsets = (config = {}) => {
  const rawPreflightOffset = Number(config.preflight_offset_minutes)
  const preflightOffset = Number.isFinite(rawPreflightOffset) && rawPreflightOffset > 0
    ? rawPreflightOffset
    : DEFAULT_PREFLIGHT_OFFSET_MINUTES
  const retryOffsets = positiveMinutes(config.retry_offsets_minutes || [])
    .filter(offset => offset <= preflightOffset)
  return uniquePositiveMinutes([preflightOffset, ...retryOffsets]).sort((a, b) => b - a)
}

const postStartOffsets = (config = {}) => (
  uniquePositiveMinutes(config.post_start_offsets_minutes || []).sort((a, b) => a - b)
)

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
