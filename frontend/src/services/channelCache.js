const CHANNEL_STATS_PREFIX = 'streamflow_channel_stats_'
const CHANNEL_LOGO_PREFIX = 'streamflow_channel_logo_'

function canUseStorage() {
  return typeof window !== 'undefined' && typeof window.localStorage !== 'undefined'
}

function read(key) {
  if (!canUseStorage()) return null
  try {
    return window.localStorage.getItem(key)
  } catch (error) {
    return null
  }
}

function write(key, value) {
  if (!canUseStorage()) return
  try {
    window.localStorage.setItem(key, value)
  } catch (error) {
    // Ignore quota/privacy mode write failures to keep UI responsive.
  }
}

export function getCachedChannelStats(channelId) {
  const raw = read(`${CHANNEL_STATS_PREFIX}${channelId}`)
  if (!raw) return null
  try {
    return JSON.parse(raw)
  } catch (error) {
    return null
  }
}

export function setCachedChannelStats(channelId, stats) {
  write(`${CHANNEL_STATS_PREFIX}${channelId}`, JSON.stringify(stats))
}

export function getCachedChannelLogoUrl(channelId) {
  return read(`${CHANNEL_LOGO_PREFIX}${channelId}`)
}

export function setCachedChannelLogoUrl(channelId, logoUrl) {
  if (!logoUrl) return
  write(`${CHANNEL_LOGO_PREFIX}${channelId}`, logoUrl)
}
