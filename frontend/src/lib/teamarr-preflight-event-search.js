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
