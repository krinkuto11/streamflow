const numberOrNull = (value) => {
  const numeric = Number(value)
  return Number.isFinite(numeric) ? numeric : null
}

const plural = (count, singular, pluralLabel = `${singular}s`) => (
  count === 1 ? singular : pluralLabel
)

export const getTeamarrEventHealthAlert = (event = {}, lastPreflightEvent = null, automaticCheckSummary = '') => {
  const stats = lastPreflightEvent?.details?.stats || {}
  const totalStreams = numberOrNull(stats.total_streams)
  const deadStreams = numberOrNull(stats.dead_streams)

  if (!totalStreams || deadStreams === null || deadStreams < totalStreams) {
    return null
  }

  const secondsToStart = numberOrNull(event.seconds_to_start ?? lastPreflightEvent?.seconds_to_start)
  const afterStart = secondsToStart !== null && secondsToStart < 0
  const timingText = afterStart ? 'after event start' : 'before event start'
  const scheduleText = automaticCheckSummary
    ? `Next automatic status: ${automaticCheckSummary}.`
    : afterStart
      ? 'No automatic check remains; Force Check can still run manually.'
      : 'No automatic check is currently scheduled; Force Check can still run manually.'

  return {
    severity: afterStart ? 'critical' : 'warning',
    label: 'No functional streams',
    detail: `All ${totalStreams} checked ${plural(totalStreams, 'stream')} failed the preflight quality/dead-stream check ${timingText}. ${scheduleText}`,
    totalStreams,
    deadStreams,
    afterStart,
  }
}
