const plural = (count, singular, pluralValue = `${singular}s`) => (
  count === 1 ? singular : pluralValue
)

const finiteNumber = (value, fallback = 0) => {
  const number = Number(value)
  return Number.isFinite(number) ? number : fallback
}

export const getAutoCreateRuleTestToast = ({
  responseData = {},
  selectedChannelCount = 0,
} = {}) => {
  const matches = finiteNumber(responseData.matches)
  const channelsTested = finiteNumber(responseData.channels_tested, selectedChannelCount)
  const channelsWithoutTvg = responseData.channels_without_tvg || []
  const channelsWithMatches = responseData.channels_with_matches == null
    ? (matches > 0 ? Math.min(channelsTested, 1) : 0)
    : finiteNumber(responseData.channels_with_matches)

  if (responseData.no_tvg_id) {
    return {
      title: 'No TVG-ID Configured',
      description: channelsTested > 1
        ? 'None of the selected channels have a TVG-ID set. EPG matching requires TVG-IDs on the source channels.'
        : 'This channel has no TVG-ID set. EPG matching requires a TVG-ID.',
      variant: 'destructive',
    }
  }

  if (matches === 0) {
    return {
      title: 'No Matches',
      description: `The regex pattern did not match any EPG programs across ${channelsTested} selected ${plural(channelsTested, 'channel')}.`,
      variant: 'default',
    }
  }

  if (channelsWithoutTvg.length > 0) {
    return {
      title: 'Partial TVG-ID Coverage',
      description: `${channelsWithoutTvg.length} selected ${plural(channelsWithoutTvg.length, 'channel')} had no TVG-ID and could not be tested.`,
      variant: 'default',
    }
  }

  if (channelsTested > 1 && channelsWithMatches < channelsTested) {
    return {
      title: 'Partial Matches',
      description: `${matches} matching ${plural(matches, 'program')} found on ${channelsWithMatches}/${channelsTested} tested ${plural(channelsTested, 'channel')}. Channels without matching EPG programs will not create events until a future EPG refresh matches them.`,
      variant: 'default',
    }
  }

  return null
}
