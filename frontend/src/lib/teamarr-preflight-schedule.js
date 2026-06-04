const DEFAULT_PREFLIGHT_OFFSET_MINUTES = 20

const minuteValues = (values = []) => {
  if (Array.isArray(values)) return values
  if (values === null || values === undefined || values === '') return []
  return [values]
}

const positiveMinutes = (values = []) => (
  minuteValues(values)
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

export const getTeamarrConfiguredPreStartOffsets = preStartOffsets
export const getTeamarrConfiguredPostStartOffsets = postStartOffsets

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

export const getTeamarrTimingWarnings = (config = {}) => {
  const warnings = []
  const rawPreflightOffset = Number(config.preflight_offset_minutes)
  const preflightOffset = Number.isFinite(rawPreflightOffset) && rawPreflightOffset > 0
    ? rawPreflightOffset
    : DEFAULT_PREFLIGHT_OFFSET_MINUTES
  const retryOffsets = positiveMinutes(config.retry_offsets_minutes || [])
  const postOffsets = postStartOffsets(config)
  const rawPostGrace = Number(config.post_start_grace_minutes)
  const postGrace = Number.isFinite(rawPostGrace) && rawPostGrace >= 0 ? rawPostGrace : 0
  const pollSeconds = Number(config.poll_interval_seconds)

  const ignoredRetries = retryOffsets.filter(offset => offset > preflightOffset)
  if (ignoredRetries.length > 0) {
    warnings.push({
      code: 'retry_after_preflight_offset',
      text: `Pre-start value ${ignoredRetries.join(', ')} is later than the Preflight Offset and will be ignored.`,
    })
  }

  const outsideGrace = postOffsets.filter(offset => offset > postGrace)
  if (outsideGrace.length > 0) {
    warnings.push({
      code: 'post_start_outside_grace',
      text: `Post-start value ${outsideGrace.join(', ')} is outside Post Start Grace and will not run automatically.`,
    })
  }

  const bucketTimes = [
    ...preStartOffsets(config).map(offset => -offset),
    ...postOffsets.filter(offset => offset <= postGrace).map(offset => offset),
  ].sort((a, b) => a - b)
  const bucketGapsSeconds = []
  for (let index = 1; index < bucketTimes.length; index += 1) {
    bucketGapsSeconds.push(Math.abs(bucketTimes[index] - bucketTimes[index - 1]) * 60)
  }
  const narrowestGap = bucketGapsSeconds.length ? Math.min(...bucketGapsSeconds) : null
  if (Number.isFinite(pollSeconds) && narrowestGap && pollSeconds > narrowestGap) {
    warnings.push({
      code: 'poll_interval_wider_than_bucket_gap',
      text: `Poll interval ${pollSeconds}s is wider than the narrowest configured bucket gap (${narrowestGap}s).`,
    })
  }

  return warnings
}

export const getTeamarrSchedulePreview = (event = {}, config = {}) => {
  const eventAt = new Date(event?.event_date)
  if (Number.isNaN(eventAt.getTime())) {
    return {
      items: [],
      warnings: getTeamarrTimingWarnings(config),
    }
  }

  const rawPostGrace = Number(config.post_start_grace_minutes)
  const postGrace = Number.isFinite(rawPostGrace) && rawPostGrace >= 0 ? rawPostGrace : 0
  const preflightOffset = preStartOffsets(config)[0] || DEFAULT_PREFLIGHT_OFFSET_MINUTES
  const items = [
    ...preStartOffsets(config).map(offset => ({
      bucket: `-${offset}m`,
      label: offset === preflightOffset ? 'Preflight Offset' : 'Pre-start check',
      timestamp: new Date(eventAt.getTime() - offset * 60 * 1000).toISOString(),
    })),
    ...postStartOffsets(config).map(offset => ({
      bucket: `+${offset}m`,
      label: offset <= postGrace ? 'Post-start check' : 'Outside grace',
      timestamp: new Date(eventAt.getTime() + offset * 60 * 1000).toISOString(),
      disabled: offset > postGrace,
    })),
  ]

  return {
    items,
    warnings: getTeamarrTimingWarnings(config),
  }
}
