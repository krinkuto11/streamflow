import { describe, expect, it } from 'vitest'
import { formatDuration, parseDurationSeconds } from './time-format.js'

describe('time formatting', () => {
  it('formats seconds, minutes, and hours', () => {
    expect(formatDuration(44)).toBe('44s')
    expect(formatDuration(104)).toBe('1m 44s')
    expect(formatDuration(1667)).toBe('27m 47s')
    expect(formatDuration(9167)).toBe('2h 32m 47s')
  })

  it('normalizes existing duration strings', () => {
    expect(formatDuration('1667s')).toBe('27m 47s')
    expect(formatDuration('1m 44s')).toBe('1m 44s')
    expect(formatDuration('02:32:47')).toBe('2h 32m 47s')
  })

  it('returns unknown strings unchanged', () => {
    expect(parseDurationSeconds('N/A')).toBeNull()
    expect(formatDuration('N/A')).toBe('N/A')
  })
})
