import { describe, expect, it } from 'vitest'

import {
  formatStartupDuration,
  getStartupDurationDisplay,
} from './startup-duration-display.js'

describe('formatStartupDuration', () => {
  it('formats startup durations without pretending precision beyond seconds', () => {
    expect(formatStartupDuration(8)).toBe('8s')
    expect(formatStartupDuration(75)).toBe('1m 15s')
    expect(formatStartupDuration(3720)).toBe('1h 2m')
    expect(formatStartupDuration(null)).toBeNull()
  })
})

describe('getStartupDurationDisplay', () => {
  it('shows elapsed time and a cautious first-load expectation without prior timing', () => {
    expect(getStartupDurationDisplay({ elapsed_seconds: 91 })).toEqual({
      elapsedLabel: '1m 31s',
      remainingLabel: null,
      expectation: 'Large playlists can take 2-5 minutes on first load.',
    })
  })

  it('uses the previous successful refresh duration for a rough remaining estimate', () => {
    expect(getStartupDurationDisplay({
      elapsed_seconds: 45,
      last_refresh_duration_seconds: 120,
    })).toEqual({
      elapsedLabel: '45s',
      remainingLabel: '1m 15s',
      expectation: 'About 1m 15s remaining based on the last cache refresh.',
    })
  })
})
