const normalizeSearchValue = (value) => String(value || '').trim().toLowerCase()

const eventSearchFields = (event = {}) => {
  const details = event.details || {}
  const stats = details.stats || {}
  const lastPreflight = event.last_preflight_event || {}
  const lastDetails = lastPreflight.details || {}

  return [
    event.preflight_kind,
    event.event_name,
    event.team_name,
    event.team_abbrev,
    event.channel_name,
    event.sport,
    event.league,
    event.primary_league,
    event.team_status,
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

const DEFAULT_PREFLIGHT_OFFSET_MINUTES = 20
const DEFAULT_POLL_INTERVAL_SECONDS = 60
const staticTeamHiddenStates = new Set([
  'filtered',
  'incomplete_team',
  'no_dispatcharr_channel',
  'no_event_window',
  'no_live_window',
  'no_streams_yet',
  'past',
  'waiting_for_channel_sync',
])

const positiveNumberOrDefault = (value, fallback) => {
  const number = Number(value)
  return Number.isFinite(number) && number > 0 ? number : fallback
}

const staticTeamUpcomingWindowSeconds = (config = {}) => {
  const offsetMinutes = positiveNumberOrDefault(
    config.preflight_offset_minutes,
    DEFAULT_PREFLIGHT_OFFSET_MINUTES,
  )
  const pollSeconds = positiveNumberOrDefault(
    config.poll_interval_seconds,
    DEFAULT_POLL_INTERVAL_SECONDS,
  )
  return offsetMinutes * 60 + pollSeconds
}

const isStaticTeamDefaultUpcoming = (event = {}, config = {}) => {
  if (String(event?.preflight_kind || '') !== 'team') return true

  const state = String(event?.state || '')
  if (state === 'due' || state === 'already_attempted') return true
  if (staticTeamHiddenStates.has(state)) return false
  if (state !== 'scheduled') return false
  if (!event?.event_date || !event?.dispatcharr_channel_id) return false
  if (Number(event?.stream_count ?? 1) <= 0) return false
  if (String(event?.team_status || 'ready') !== 'ready') return false

  const seconds = Number(event.seconds_to_start)
  if (!Number.isFinite(seconds)) return false
  if (seconds < 0) return true
  return seconds <= staticTeamUpcomingWindowSeconds(config)
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

export const filterTeamarrEventsByView = (events = [], view = 'upcoming', config = {}) => {
  if (view === 'all') return events
  if (view === 'past') return events.filter(event => String(event?.state || '') === 'past')
  if (view === 'no_check') {
    return events.filter(event => (
      !event?.last_preflight_event
      && isStaticTeamDefaultUpcoming(event, config)
    ))
  }
  if (view === 'due') return events.filter(event => String(event?.state || '') === 'due')
  return events.filter(event => (
    String(event?.state || '') !== 'past'
    && isStaticTeamDefaultUpcoming(event, config)
  ))
}

export const paginateTeamarrEvents = (events = [], page = 1, pageSize = 10) => {
  const safePage = Math.max(1, Number(page) || 1)
  const safePageSize = Math.max(1, Number(pageSize) || 10)
  const start = (safePage - 1) * safePageSize
  return events.slice(start, start + safePageSize)
}
