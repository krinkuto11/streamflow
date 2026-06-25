export const SHADOW_WATCHER_USER_AGENT_MARKER = 'StreamFlow-Shadow-Blank-Monitor/1.0'

export const CUSTOM_WATCHER_USER_AGENT_VALUE = 'custom'

export const CUSTOM_WATCHER_USER_AGENT_TEMPLATE = SHADOW_WATCHER_USER_AGENT_MARKER

export const watcherUserAgentPresets = [
  {
    label: 'TiviMate',
    value: `TiviMate/5.1.6 ${SHADOW_WATCHER_USER_AGENT_MARKER}`,
    description: 'Default. TiviMate-like playback with a unique Shadow marker.',
  },
  {
    label: 'VLC',
    value: `VLC/3.0.20 LibVLC/3.0.20 ${SHADOW_WATCHER_USER_AGENT_MARKER}`,
    description: 'Desktop-style fallback for providers that prefer VLC clients.',
  },
  {
    label: 'OTT Navigator',
    value: `OTT Navigator/1.7.0 ${SHADOW_WATCHER_USER_AGENT_MARKER}`,
    description: 'Android IPTV-style fallback.',
  },
  {
    label: 'Kodi',
    value: `Kodi/21.0 ${SHADOW_WATCHER_USER_AGENT_MARKER}`,
    description: 'Kodi-style fallback.',
  },
  {
    label: 'FFmpeg',
    value: `Lavf/60.16.100 ${SHADOW_WATCHER_USER_AGENT_MARKER}`,
    description: 'Generic FFmpeg fallback.',
  },
]

export const defaultWatcherUserAgent = watcherUserAgentPresets[0].value

export const getWatcherUserAgentSelectValue = (userAgent) => {
  const value = String(userAgent || '').trim()
  if (watcherUserAgentPresets.some(preset => preset.value === value)) {
    return value
  }
  return CUSTOM_WATCHER_USER_AGENT_VALUE
}

export const getWatcherUserAgentPreset = (userAgent) => {
  const value = String(userAgent || '').trim()
  return watcherUserAgentPresets.find(preset => preset.value === value) || null
}
