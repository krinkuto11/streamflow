import { describe, expect, it } from 'vitest'

import { TELEMETRY_DATE_RANGES, TELEMETRY_RETENTION_DAYS } from './telemetry-retention.js'

describe('telemetry retention controls', () => {
  it('never offers a range beyond retained history', () => {
    expect(TELEMETRY_RETENTION_DAYS).toBe(7)
    expect(TELEMETRY_DATE_RANGES.map((range) => range.value)).toEqual(['1', '7'])
    expect(TELEMETRY_DATE_RANGES.every((range) => Number(range.value) <= TELEMETRY_RETENTION_DAYS)).toBe(true)
  })
})
