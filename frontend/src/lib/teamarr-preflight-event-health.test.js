import { describe, expect, it } from 'vitest'

import { getTeamarrEventHealthAlert } from './teamarr-preflight-event-health'

describe('Teamarr event health alert helpers', () => {
  const completedAllDead = {
    type: 'preflight_completed',
    details: {
      stats: {
        total_streams: 1,
        dead_streams: 1,
      },
    },
  }

  it('warns before event start when all checked streams are dead', () => {
    expect(getTeamarrEventHealthAlert(
      { seconds_to_start: 600 },
      completedAllDead,
      'Next auto check: 6/4/2026, 9:57:00 PM (-3m)',
    )).toMatchObject({
      severity: 'warning',
      label: 'No functional streams',
      afterStart: false,
    })
  })

  it('explains static team game windows that cannot run without a Dispatcharr channel', () => {
    const alert = getTeamarrEventHealthAlert({
      preflight_kind: 'team',
      state: 'no_dispatcharr_channel',
      event_date: '2026-06-14T16:15:00Z',
      team_channel_id: 'MiamiMarlins.mlb',
    })

    expect(alert).toMatchObject({
      severity: 'warning',
      label: 'Team channel missing',
    })
    expect(alert.detail).toMatch(/MiamiMarlins\.mlb/)
    expect(alert.detail).toMatch(/before StreamFlow can run/i)
  })

  it('marks all-dead results as critical after event start', () => {
    const alert = getTeamarrEventHealthAlert(
      { seconds_to_start: -600 },
      completedAllDead,
      '',
    )

    expect(alert).toMatchObject({
      severity: 'critical',
      afterStart: true,
      totalStreams: 1,
      deadStreams: 1,
    })
    expect(alert.detail).toMatch(/quality\/dead-stream check/i)
    expect(alert.detail).toMatch(/No automatic check remains/i)
  })

  it('does not alert when at least one checked stream is functional', () => {
    expect(getTeamarrEventHealthAlert(
      { seconds_to_start: -600 },
      {
        details: {
          stats: {
            total_streams: 2,
            dead_streams: 1,
          },
        },
      },
      '',
    )).toBeNull()
  })
})
