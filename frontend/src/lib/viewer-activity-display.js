const toCount = (value) => {
  const count = Number(value || 0)
  return Number.isFinite(count) ? Math.max(0, count) : 0
}

const plural = (count, singular, pluralLabel = `${singular}s`) => (
  count === 1 ? singular : pluralLabel
)

export const formatViewerClientCount = (value) => {
  const count = toCount(value)
  return `${count} real ${plural(count, 'viewer')}`
}

export const formatWatcherClientCount = (value) => {
  const count = toCount(value)
  return `${count} shadow ${plural(count, 'watcher')}`
}

export const formatRealViewerChannelCount = (value) => {
  const count = toCount(value)
  return `${count} real viewer ${plural(count, 'channel')}`
}

export const formatWatcherOnlyChannelCount = (value) => {
  const count = toCount(value)
  return `${count} watcher-only ${plural(count, 'channel')}`
}

export const getPlaybackBadgeLabel = (channel = {}) => (
  channel.has_real_clients ? 'Real viewer active' : 'Watcher only'
)

export const getProgramDisplayLabel = (program = {}) => {
  const title = String(program?.title || '').trim()
  if (!title) return null
  const prefix = program?.state === 'upcoming' ? 'Next' : 'Now'
  return `${prefix}: ${title}`
}

const stateLabels = {
  active: 'Active playback',
  waiting_for_clients: 'Waiting for clients',
  idle: 'Idle',
}

export const getViewerActivityDetailLabel = (channel = {}) => {
  const programLabel = getProgramDisplayLabel(channel.current_program)
  if (programLabel) return programLabel
  if (channel.has_real_clients && Number(channel.watcher_client_count || 0) <= 0) {
    return 'Waiting for shadow watcher'
  }
  const state = String(channel.state || 'active').trim()
  return stateLabels[state] || state.replace(/_/g, ' ')
}

export const formatStreamRef = (streamId) => (
  streamId ? ` - Stream ${streamId}` : ''
)
