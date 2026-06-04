import { describe, expect, it } from 'vitest'

import {
  getTeamarrAutomaticCheck,
  getTeamarrNextAutomaticCheck,
  getTeamarrSchedulePreview,
  getTeamarrTimingWarnings,
} from './teamarr-preflight-schedule'

describe('Teamarr preflight schedule helpers', () => {
  const config = {
    preflight_offset_minutes: 20,
    retry_offsets_minutes: [10, 3],
    post_start_offsets_minutes: [2, 4],
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
      bucket: '-10m',
      timestamp: '2026-06-04T23:50:00.000Z',
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

  it('accepts a single pre-start retry offset value', () => {
    expect(getTeamarrNextAutomaticCheck({
      event_date: '2026-06-05T00:00:00Z',
      seconds_to_start: 181,
      state: 'already_attempted',
    }, {
      preflight_offset_minutes: 20,
      retry_offsets_minutes: '3',
      post_start_offsets_minutes: [],
    })).toMatchObject({
      bucket: '-3m',
      timestamp: '2026-06-04T23:57:00.000Z',
    })
  })

  it('accepts a single post-start check offset value', () => {
    expect(getTeamarrNextAutomaticCheck({
      event_date: '2026-06-05T00:00:00Z',
      seconds_to_start: -60,
      state: 'scheduled',
    }, {
      preflight_offset_minutes: 20,
      retry_offsets_minutes: [],
      post_start_offsets_minutes: '2',
    })).toMatchObject({
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

  it('builds a timing preview from all configured buckets', () => {
    const preview = getTeamarrSchedulePreview({
      event_date: '2026-06-05T00:00:00Z',
    }, {
      ...config,
      post_start_grace_minutes: 4,
    })

    expect(preview.items.map(item => [item.bucket, item.label])).toEqual([
      ['-20m', 'Preflight Offset'],
      ['-10m', 'Pre-start check'],
      ['-3m', 'Pre-start check'],
      ['+2m', 'Post-start check'],
      ['+4m', 'Post-start check'],
    ])
    expect(preview.items[0].timestamp).toBe('2026-06-04T23:40:00.000Z')
  })

  it('warns about ignored buckets and wide polling intervals', () => {
    const warnings = getTeamarrTimingWarnings({
      preflight_offset_minutes: 5,
      retry_offsets_minutes: [10, 3],
      post_start_offsets_minutes: [2, 6],
      post_start_grace_minutes: 4,
      poll_interval_seconds: 240,
    })

    expect(warnings.map(warning => warning.code)).toEqual([
      'retry_after_preflight_offset',
      'post_start_outside_grace',
      'poll_interval_wider_than_bucket_gap',
    ])
  })
})
