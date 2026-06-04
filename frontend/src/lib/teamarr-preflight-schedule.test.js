import { describe, expect, it } from 'vitest'

import { getTeamarrAutomaticCheck, getTeamarrNextAutomaticCheck } from './teamarr-preflight-schedule'

describe('Teamarr preflight schedule helpers', () => {
  const config = {
    preflight_offset_minutes: 20,
    pre_start_retry_count: 2,
    post_start_retry_count: 2,
    post_start_grace_minutes: 5,
  }

  it('shows the first pre-start bucket when an event is scheduled far in the future', () => {
    expect(getTeamarrNextAutomaticCheck({
      event_date: '2026-06-05T00:00:00Z',
      seconds_to_start: 35092,
      state: 'scheduled',
    }, config)).toEqual({
      label: 'Next auto check',
      bucket: '-20m',
      timestamp: '2026-06-04T23:40:00.000Z',
    })
  })

  it('shows the next retry bucket after an earlier bucket was already attempted', () => {
    expect(getTeamarrNextAutomaticCheck({
      event_date: '2026-06-05T00:00:00Z',
      seconds_to_start: 900,
      state: 'already_attempted',
    }, config)).toMatchObject({
      bucket: '-14m',
      timestamp: '2026-06-04T23:46:00.000Z',
    })
  })

  it('shows the next post-start bucket shortly after game start', () => {
    expect(getTeamarrNextAutomaticCheck({
      event_date: '2026-06-05T00:00:00Z',
      seconds_to_start: -60,
      state: 'scheduled',
    }, config)).toMatchObject({
      bucket: '+2m',
      timestamp: '2026-06-05T00:02:00.000Z',
    })
  })

  it('marks due events as due now', () => {
    expect(getTeamarrNextAutomaticCheck({
      event_date: '2026-06-05T00:00:00Z',
      seconds_to_start: 60,
      state: 'due',
      trigger_bucket: '3m',
    }, config)).toEqual({
      label: 'Due now',
      bucket: '3m',
      timestamp: null,
    })
  })

  it('prefers the backend-provided next automatic check when present', () => {
    const backendCheck = {
      label: 'Next auto check',
      bucket: '-20m',
      timestamp: '2026-06-04T23:40:00+00:00',
    }

    expect(getTeamarrAutomaticCheck({
      event_date: '2026-06-05T00:00:00Z',
      next_automatic_check: backendCheck,
      seconds_to_start: 35092,
      state: 'scheduled',
    }, config)).toBe(backendCheck)
  })
})
