import { describe, expect, it } from 'vitest'
import {
  formatRealViewerChannelCount,
  formatStreamRef,
  formatViewerClientCount,
  formatWatcherClientCount,
  formatWatcherOnlyChannelCount,
  getPlaybackBadgeLabel,
  getProgramDisplayLabel,
  getViewerActivityDetailLabel,
} from './viewer-activity-display.js'

describe('viewer activity display helpers', () => {
  it('labels real viewer and watcher channel counts clearly', () => {
    expect(formatRealViewerChannelCount(1)).toBe('1 real viewer channel')
    expect(formatRealViewerChannelCount(2)).toBe('2 real viewer channels')
    expect(formatWatcherOnlyChannelCount(0)).toBe('0 watcher-only channels')
    expect(formatWatcherOnlyChannelCount(1)).toBe('1 watcher-only channel')
  })

  it('labels client counts without mixing real viewers and watcher probes', () => {
    expect(formatViewerClientCount(1)).toBe('1 real viewer')
    expect(formatViewerClientCount(3)).toBe('3 real viewers')
    expect(formatWatcherClientCount(1)).toBe('1 shadow watcher')
    expect(formatWatcherClientCount(2)).toBe('2 shadow watchers')
  })

  it('uses stable playback badges and ASCII stream separators', () => {
    expect(getPlaybackBadgeLabel({ has_real_clients: true })).toBe('Real viewer active')
    expect(getPlaybackBadgeLabel({ has_real_clients: false })).toBe('Watcher only')
    expect(formatStreamRef(91)).toBe(' - Stream 91')
    expect(formatStreamRef(null)).toBe('')
  })

  it('uses EPG program context before raw proxy state', () => {
    expect(getProgramDisplayLabel({ title: 'Live: MLB', state: 'current' })).toBe('Now: Live: MLB')
    expect(getProgramDisplayLabel({ title: 'Later Game', state: 'upcoming' })).toBe('Next: Later Game')
    expect(getViewerActivityDetailLabel({
      state: 'waiting_for_clients',
      current_program: { title: 'Live: MLB', state: 'current' },
      has_real_clients: true,
      watcher_client_count: 0,
    })).toBe('Now: Live: MLB')
  })

  it('shows transient shadow watcher reconnects before normal playback context', () => {
    expect(getViewerActivityDetailLabel({
      current_program: { title: 'Live: MLB', state: 'current' },
      has_real_clients: true,
      watcher_client_count: 0,
      watcher_state: 'reconnecting',
    })).toBe('Shadow watcher reconnecting')
    expect(getViewerActivityDetailLabel({
      current_program: { title: 'Live: MLB', state: 'current' },
      has_real_clients: true,
      viewer_left_grace_active: true,
    })).toBe('Shadow watcher holding during reconnect')
  })

  it('hides waiting_for_clients behind an operator fallback when no EPG is known', () => {
    expect(getViewerActivityDetailLabel({
      state: 'waiting_for_clients',
      has_real_clients: true,
      watcher_client_count: 0,
    })).toBe('Waiting for shadow watcher')
    expect(getViewerActivityDetailLabel({ state: 'active' })).toBe('Active playback')
  })
})
