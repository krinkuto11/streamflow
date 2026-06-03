import { describe, expect, it } from 'vitest'
import {
  formatRealViewerChannelCount,
  formatStreamRef,
  formatViewerClientCount,
  formatWatcherClientCount,
  formatWatcherOnlyChannelCount,
  getPlaybackBadgeLabel,
} from './viewer-activity-display.js'

describe('viewer activity display helpers', () => {
  it('labels real viewer and watcher channel counts clearly', () => {
    expect(formatRealViewerChannelCount(1)).toBe('1 real-viewer channel')
    expect(formatRealViewerChannelCount(2)).toBe('2 real-viewer channels')
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
})
