import { describe, expect, it } from 'vitest'

import { filterTeamarrEventsBySearch } from './teamarr-preflight-event-search'

describe('Teamarr preflight event search helpers', () => {
  const events = [
    {
      event_name: 'Netherlands vs. Algeria',
      channel_name: 'DE Soccer PPV 101',
      sport: 'soccer',
      league: 'fifa.friendly',
      state: 'scheduled',
      last_preflight_event: { type: 'preflight_completed' },
    },
    {
      event_name: 'Detroit Tigers at Boston Red Sox',
      channel_name: 'MLB Event 5',
      sport: 'baseball',
      league: 'mlb',
      state: 'past',
      details: { reason: 'active_viewers' },
    },
  ]

  it('returns all events when search is empty', () => {
    expect(filterTeamarrEventsBySearch(events, '')).toBe(events)
  })

  it('matches event names, channels, sport, league, and state', () => {
    expect(filterTeamarrEventsBySearch(events, 'algeria')).toEqual([events[0]])
    expect(filterTeamarrEventsBySearch(events, 'soccer ppv')).toEqual([events[0]])
    expect(filterTeamarrEventsBySearch(events, 'baseball')).toEqual([events[1]])
    expect(filterTeamarrEventsBySearch(events, 'past')).toEqual([events[1]])
  })

  it('matches latest preflight and decision detail fields', () => {
    expect(filterTeamarrEventsBySearch(events, 'completed')).toEqual([events[0]])
    expect(filterTeamarrEventsBySearch(events, 'active_viewers')).toEqual([events[1]])
  })
})
