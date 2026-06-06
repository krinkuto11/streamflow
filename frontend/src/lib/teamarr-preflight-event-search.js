const normalizeSearchValue = (value) => String(value || '').trim().toLowerCase()

const eventSearchFields = (event = {}) => {
  const details = event.details || {}
  const stats = details.stats || {}
  const lastPreflight = event.last_preflight_event || {}
  const lastDetails = lastPreflight.details || {}

  return [
    event.event_name,
    event.channel_name,
    event.sport,
    event.league,
    event.state,
    event.bucket,
    event.trigger_bucket,
    event.identity,
    event.type,
    details.bucket,
    details.reason,
    details.error,
    stats.avg_resolution,
    stats.avg_fps,
    lastPreflight.type,
    lastDetails.bucket,
    lastDetails.reason,
    lastDetails.error,
  ]
}

export const filterTeamarrEventsBySearch = (events = [], search = '') => {
  const query = normalizeSearchValue(search)
  if (!query) return events

  return events.filter(event => (
    eventSearchFields(event)
      .map(normalizeSearchValue)
      .some(value => value.includes(query))
  ))
}

const numericSecondsToStart = (event = {}) => {
  const seconds = Number(event.seconds_to_start)
  return Number.isFinite(seconds) ? seconds : Number.MAX_SAFE_INTEGER
}

export const sortTeamarrManagedEvents = (events = []) => (
  [...events].sort((a, b) => {
    const aSeconds = numericSecondsToStart(a)
    const bSeconds = numericSecondsToStart(b)
    const aState = String(a?.state || '')
    const bState = String(b?.state || '')
    const aRank = aState === 'past' ? 2 : (aSeconds < 0 && !['due', 'already_attempted'].includes(aState) ? 1 : 0)
    const bRank = bState === 'past' ? 2 : (bSeconds < 0 && !['due', 'already_attempted'].includes(bState) ? 1 : 0)

    if (aRank !== bRank) return aRank - bRank
    if (aRank === 2 && aSeconds !== bSeconds) return bSeconds - aSeconds
    if (aSeconds !== bSeconds) return aSeconds - bSeconds
    return String(a?.event_name || '').localeCompare(String(b?.event_name || ''))
  })
)

export const filterTeamarrEventsByView = (events = [], view = 'upcoming') => {
  if (view === 'all') return events
  if (view === 'past') return events.filter(event => String(event?.state || '') === 'past')
  if (view === 'no_check') return events.filter(event => !event?.last_preflight_event)
  if (view === 'due') return events.filter(event => String(event?.state || '') === 'due')
  return events.filter(event => String(event?.state || '') !== 'past')
}

export const paginateTeamarrEvents = (events = [], page = 1, pageSize = 10) => {
  const safePage = Math.max(1, Number(page) || 1)
  const safePageSize = Math.max(1, Number(pageSize) || 10)
  const start = (safePage - 1) * safePageSize
  return events.slice(start, start + safePageSize)
}
