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
  const data = responseData || {}
  const matches = finiteNumber(data.matches)
  const channelsTested = finiteNumber(data.channels_tested, selectedChannelCount)
  const channelsWithoutTvg = data.channels_without_tvg || []
  const channelsWithoutPrograms = data.channels_without_programs || []
  const channelsWithoutMatches = data.channels_without_matches || []
  const channelsWithMatches = data.channels_with_matches == null
    ? (matches > 0 ? Math.min(channelsTested, 1) : 0)
    : finiteNumber(data.channels_with_matches)

  if (data.no_tvg_id) {
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
    const reasonParts = []
    if (channelsWithoutPrograms.length > 0) {
      reasonParts.push(`${channelsWithoutPrograms.length} without EPG programs`)
    }
    if (channelsWithoutMatches.length > 0) {
      reasonParts.push(`${channelsWithoutMatches.length} with EPG titles that did not match`)
    }
    const reasonSuffix = reasonParts.length > 0 ? ` (${reasonParts.join(', ')})` : ''

    return {
      title: 'Partial Matches',
      description: `${matches} matching ${plural(matches, 'program')} found on ${channelsWithMatches}/${channelsTested} tested ${plural(channelsTested, 'channel')}${reasonSuffix}.`,
      variant: 'default',
    }
  }

  return null
}

export const getAutoCreateRuleTestDiagnostics = (responseData = {}) => {
  const data = responseData || {}
  const channelsWithoutTvg = data.channels_without_tvg || []
  const channelsWithoutPrograms = data.channels_without_programs || []
  const channelsWithoutMatches = data.channels_without_matches || []
  const diagnostics = []

  if (channelsWithoutTvg.length > 0) {
    diagnostics.push({
      key: 'no_tvg_id',
      label: 'No TVG-ID',
      count: channelsWithoutTvg.length,
      detail: 'EPG matching needs a TVG-ID on the source channel.',
      channels: channelsWithoutTvg,
    })
  }

  if (channelsWithoutPrograms.length > 0) {
    diagnostics.push({
      key: 'no_epg_programs',
      label: 'No EPG programs',
      count: channelsWithoutPrograms.length,
      detail: 'StreamFlow could not see any EPG programs for these TVG-IDs.',
      channels: channelsWithoutPrograms,
    })
  }

  if (channelsWithoutMatches.length > 0) {
    const sampleTitles = []
    channelsWithoutMatches.forEach((channel) => {
      ;(channel.sample_titles || []).forEach((title) => {
        if (title && sampleTitles.length < 4 && !sampleTitles.includes(title)) {
          sampleTitles.push(title)
        }
      })
    })
    diagnostics.push({
      key: 'regex_mismatch',
      label: 'Regex did not match',
      count: channelsWithoutMatches.length,
      detail: 'EPG programs exist, but their titles do not match this pattern.',
      sampleTitles,
      channels: channelsWithoutMatches,
    })
  }

  return diagnostics
}
