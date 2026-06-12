import { describe, expect, it } from 'vitest'

import {
  filterTeamarrEventsBySearch,
  filterTeamarrEventsByView,
  paginateTeamarrEvents,
  sortTeamarrManagedEvents,
} from './teamarr-preflight-event-search'

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
      bucket: 'manual',
      details: { reason: 'active_viewers' },
    },
    {
      preflight_kind: 'team',
      event_name: 'Static Team Row',
      team_name: 'San Jose Sharks',
      team_abbrev: 'SJS',
      channel_name: 'Team Channel',
      sport: 'hockey',
      league: 'nhl',
      team_status: 'ready',
      state: 'due',
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
    expect(filterTeamarrEventsBySearch(events, 'manual')).toEqual([events[1]])
    expect(filterTeamarrEventsBySearch(events, 'sharks')).toEqual([events[2]])
    expect(filterTeamarrEventsBySearch(events, 'sjs')).toEqual([events[2]])
    expect(filterTeamarrEventsBySearch(events, 'hockey')).toEqual([events[2]])
  })

  it('matches latest preflight and decision detail fields', () => {
    expect(filterTeamarrEventsBySearch(events, 'completed')).toEqual([events[0]])
    expect(filterTeamarrEventsBySearch(events, 'active_viewers')).toEqual([events[1]])
  })

  it('sorts current and upcoming managed events before past events', () => {
    const sorted = sortTeamarrManagedEvents([
      { event_name: 'Oldest past', state: 'past', seconds_to_start: -7200 },
      { event_name: 'Upcoming later', state: 'scheduled', seconds_to_start: 3600 },
      { event_name: 'Due now', state: 'due', seconds_to_start: -30 },
      { event_name: 'Recent past', state: 'past', seconds_to_start: -300 },
    ])

    expect(sorted.map(event => event.event_name)).toEqual([
      'Due now',
      'Upcoming later',
      'Recent past',
      'Oldest past',
    ])
  })

  it('filters managed events by operator view', () => {
    expect(filterTeamarrEventsByView(events, 'upcoming')).toEqual([events[0], events[2]])
    expect(filterTeamarrEventsByView(events, 'past')).toEqual([events[1]])
    expect(filterTeamarrEventsByView(events, 'no_check')).toEqual([events[1], events[2]])
    expect(filterTeamarrEventsByView(events, 'all')).toEqual(events)
  })

  it('paginates managed event lists', () => {
    expect(paginateTeamarrEvents([1, 2, 3, 4, 5], 2, 2)).toEqual([3, 4])
    expect(paginateTeamarrEvents([1, 2, 3], 0, 2)).toEqual([1, 2])
  })
})
