import { describe, expect, it } from 'vitest'

import {
  CUSTOM_WATCHER_USER_AGENT_VALUE,
  SHADOW_WATCHER_USER_AGENT_MARKER,
  defaultWatcherUserAgent,
  getWatcherUserAgentPreset,
  getWatcherUserAgentSelectValue,
  watcherUserAgentPresets,
} from './shadow-monitor-user-agent-presets.js'

describe('shadow monitor watcher user agent presets', () => {
  it('defaults to a TiviMate-like user agent with the unique Shadow marker', () => {
    expect(defaultWatcherUserAgent).toContain('TiviMate/')
    expect(defaultWatcherUserAgent).toContain(SHADOW_WATCHER_USER_AGENT_MARKER)
  })

  it('keeps every preset identifiable as a Shadow watcher', () => {
    expect(watcherUserAgentPresets.length).toBeGreaterThan(3)
    expect(watcherUserAgentPresets.every(preset => preset.value.includes(SHADOW_WATCHER_USER_AGENT_MARKER))).toBe(true)
  })

  it('selects custom mode for unknown user agents', () => {
    expect(getWatcherUserAgentSelectValue(defaultWatcherUserAgent)).toBe(defaultWatcherUserAgent)
    expect(getWatcherUserAgentSelectValue('MyPlayer/1.0')).toBe(CUSTOM_WATCHER_USER_AGENT_VALUE)
    expect(getWatcherUserAgentPreset('MyPlayer/1.0')).toBeNull()
  })
})
