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
  return `${count} real-viewer ${plural(count, 'channel')}`
}

export const formatWatcherOnlyChannelCount = (value) => {
  const count = toCount(value)
  return `${count} watcher-only ${plural(count, 'channel')}`
}

export const getPlaybackBadgeLabel = (channel = {}) => (
  channel.has_real_clients ? 'Real viewer active' : 'Watcher only'
)

export const formatStreamRef = (streamId) => (
  streamId ? ` - Stream ${streamId}` : ''
)
