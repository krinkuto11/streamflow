import { describe, expect, it } from 'vitest'
import {
  getAutoCreateRuleTestDiagnostics,
  getAutoCreateRuleTestToast,
} from './scheduling-auto-create-display.js'

describe('getAutoCreateRuleTestToast', () => {
  it('explains when selected channels have no TVG-ID', () => {
    expect(getAutoCreateRuleTestToast({
      responseData: {
        no_tvg_id: true,
        channels_tested: 2,
      },
      selectedChannelCount: 2,
    })).toEqual({
      title: 'No TVG-ID Configured',
      description: 'None of the selected channels have a TVG-ID set. EPG matching requires TVG-IDs on the source channels.',
      variant: 'destructive',
    })
  })

  it('reports no matches across all tested group channels', () => {
    expect(getAutoCreateRuleTestToast({
      responseData: {
        matches: 0,
        channels_tested: 3,
      },
      selectedChannelCount: 1,
    })).toEqual({
      title: 'No Matches',
      description: 'The regex pattern did not match any EPG programs across 3 selected channels.',
      variant: 'default',
    })
  })

  it('reports partial matches so operators know why only some events will be created', () => {
    expect(getAutoCreateRuleTestToast({
      responseData: {
        matches: 2,
        channels_tested: 4,
        channels_with_matches: 1,
        channels_without_programs: [{ id: 2, name: 'No EPG' }],
        channels_without_matches: [{ id: 3, name: 'Wrong Title' }],
        programs: [
          { title: 'Friendly', channel_name: 'Match Channel' },
          { title: 'Friendly Extra', channel_name: 'Match Channel' },
        ],
      },
      selectedChannelCount: 0,
    })).toEqual({
      title: 'Partial Matches',
      description: '2 matching programs found on 1/4 tested channels (1 without EPG programs, 1 with EPG titles that did not match).',
      variant: 'default',
    })
  })

  it('does not show a warning when every tested channel has a match', () => {
    expect(getAutoCreateRuleTestToast({
      responseData: {
        matches: 2,
        channels_tested: 2,
        channels_with_matches: 2,
      },
      selectedChannelCount: 2,
    })).toBeNull()
  })

  it('treats null test data as an empty diagnostic snapshot', () => {
    expect(getAutoCreateRuleTestToast({
      responseData: null,
      selectedChannelCount: 3,
    })).toEqual({
      title: 'No Matches',
      description: 'The regex pattern did not match any EPG programs across 3 selected channels.',
      variant: 'default',
    })
    expect(getAutoCreateRuleTestDiagnostics(null)).toEqual([])
  })

  it('builds diagnostics for missing tvg, missing epg data, and regex mismatch', () => {
    expect(getAutoCreateRuleTestDiagnostics({
      channels_without_tvg: [{ id: 1, name: 'No TVG' }],
      channels_without_programs: [{ id: 2, name: 'No EPG' }],
      channels_without_matches: [{
        id: 3,
        name: 'Wrong Title',
        sample_titles: ['Pregame Baseball', 'Postgame Baseball'],
      }],
    })).toEqual([
      {
        key: 'no_tvg_id',
        label: 'No TVG-ID',
        count: 1,
        detail: 'EPG matching needs a TVG-ID on the source channel.',
        channels: [{ id: 1, name: 'No TVG' }],
      },
      {
        key: 'no_epg_programs',
        label: 'No EPG programs',
        count: 1,
        detail: 'StreamFlow could not see any EPG programs for these TVG-IDs.',
        channels: [{ id: 2, name: 'No EPG' }],
      },
      {
        key: 'regex_mismatch',
        label: 'Regex did not match',
        count: 1,
        detail: 'EPG programs exist, but their titles do not match this pattern.',
        sampleTitles: ['Pregame Baseball', 'Postgame Baseball'],
        channels: [{
          id: 3,
          name: 'Wrong Title',
          sample_titles: ['Pregame Baseball', 'Postgame Baseball'],
        }],
      },
    ])
  })
})
