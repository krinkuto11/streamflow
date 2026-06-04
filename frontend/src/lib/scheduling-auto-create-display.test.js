import { describe, expect, it } from 'vitest'
import { getAutoCreateRuleTestToast } from './scheduling-auto-create-display.js'

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
        programs: [
          { title: 'Friendly', channel_name: 'Match Channel' },
          { title: 'Friendly Extra', channel_name: 'Match Channel' },
        ],
      },
      selectedChannelCount: 0,
    })).toEqual({
      title: 'Partial Matches',
      description: '2 matching programs found on 1/4 tested channels. Channels without matching EPG programs will not create events until a future EPG refresh matches them.',
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
})
