const plural = (count, singular, pluralValue = `${singular}s`) => (
  count === 1 ? singular : pluralValue
)

const finiteNumber = (value, fallback = 0) => {
  const number = Number(value)
  return Number.isFinite(number) ? number : fallback
}

const compactReasonList = (parts) => parts.filter(Boolean).join(', ')

const getSchedulingBreakdown = (data = {}) => {
  const matches = finiteNumber(data.matches)
  const totalEpgMatches = data.total_epg_matches == null
    ? matches
    : finiteNumber(data.total_epg_matches)
  const futureMatches = finiteNumber(data.future_matches)
  const dueNowMatches = finiteNumber(data.due_now_matches)
  const endedMatches = finiteNumber(data.ended_matches)
  const alreadyCheckedMatches = finiteNumber(data.already_checked_matches)
  const missingTimeMatches = finiteNumber(data.missing_time_matches)
  const invalidTimeMatches = finiteNumber(data.invalid_time_matches)
  const guardrailBlockedMatches = finiteNumber(data.guardrail_blocked_matches)
  const unscheduledMatches = endedMatches + alreadyCheckedMatches + missingTimeMatches + invalidTimeMatches

  return {
    matches,
    totalEpgMatches,
    futureMatches,
    dueNowMatches,
    endedMatches,
    alreadyCheckedMatches,
    missingTimeMatches,
    invalidTimeMatches,
    guardrailBlockedMatches,
    unscheduledMatches,
    hasSchedulingSplit: totalEpgMatches !== matches || dueNowMatches > 0,
  }
}

export const getAutoCreateRuleTestToast = ({
  responseData = {},
  selectedChannelCount = 0,
} = {}) => {
  const data = responseData || {}
  const {
    matches,
    totalEpgMatches,
    futureMatches,
    dueNowMatches,
    endedMatches,
    alreadyCheckedMatches,
    missingTimeMatches,
    invalidTimeMatches,
    guardrailBlockedMatches,
    hasSchedulingSplit,
  } = getSchedulingBreakdown(data)
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

  if (data.guardrail?.blocked || guardrailBlockedMatches > 0) {
    const limit = data.guardrail?.limit
    return {
      title: 'Guardrail Blocked',
      description: `${guardrailBlockedMatches || totalEpgMatches} schedulable ${plural(guardrailBlockedMatches || totalEpgMatches, 'match', 'matches')} would exceed the configured max${limit ? ` of ${limit}` : ''}. Narrow the regex or raise the rule limit intentionally.`,
      variant: 'destructive',
    }
  }

  if (totalEpgMatches > 0 && matches === 0) {
    const reasonSuffix = compactReasonList([
      endedMatches > 0 ? `${endedMatches} already ended` : '',
      alreadyCheckedMatches > 0 ? `${alreadyCheckedMatches} already checked` : '',
      missingTimeMatches > 0 ? `${missingTimeMatches} missing start/end time` : '',
      invalidTimeMatches > 0 ? `${invalidTimeMatches} with invalid start/end time` : '',
    ])
    return {
      title: 'No Schedulable Matches',
      description: `${totalEpgMatches} EPG title ${plural(totalEpgMatches, 'match', 'matches')} found, but none can create a scheduled event${reasonSuffix ? ` (${reasonSuffix})` : ''}.`,
      variant: 'default',
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

  if (hasSchedulingSplit) {
    const reasonSuffix = compactReasonList([
      `${futureMatches} future ${plural(futureMatches, 'event')}`,
      dueNowMatches > 0 ? `${dueNowMatches} due now and will move to the queue` : '',
      endedMatches > 0 ? `${endedMatches} already ended` : '',
      alreadyCheckedMatches > 0 ? `${alreadyCheckedMatches} already checked` : '',
      missingTimeMatches > 0 ? `${missingTimeMatches} missing start/end time` : '',
      invalidTimeMatches > 0 ? `${invalidTimeMatches} with invalid start/end time` : '',
    ])
    return {
      title: 'Scheduling Breakdown',
      description: `${totalEpgMatches} EPG title ${plural(totalEpgMatches, 'match', 'matches')}: ${reasonSuffix}.`,
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
  const channelsWithUnscheduledMatches = data.channels_with_unscheduled_matches || []
  const {
    dueNowMatches,
    endedMatches,
    alreadyCheckedMatches,
    missingTimeMatches,
    invalidTimeMatches,
    guardrailBlockedMatches,
  } = getSchedulingBreakdown(data)
  const diagnostics = []

  if (data.guardrail?.blocked || guardrailBlockedMatches > 0) {
    const blockedPrograms = data.guardrail_blocked_programs || []
    diagnostics.push({
      key: 'guardrail_blocked',
      label: 'Guardrail blocked',
      count: guardrailBlockedMatches || data.guardrail?.candidate_count || 0,
      detail: `This rule would create too many checks${data.guardrail?.limit ? ` for the configured limit of ${data.guardrail.limit}` : ''}. Narrow the regex or raise the max checks value deliberately.`,
      sampleTitles: blockedPrograms.map(program => program.title).filter(Boolean).slice(0, 4),
      channels: blockedPrograms.map(program => ({
        id: program.channel_id,
        name: program.channel_name,
      })).filter(channel => channel.id || channel.name).slice(0, 5),
    })
  }

  if (dueNowMatches > 0) {
    diagnostics.push({
      key: 'due_now',
      label: 'Due now',
      count: dueNowMatches,
      detail: 'These matched programs are at or past their check time and will move to the Stream Checker queue when the rule refreshes.',
    })
  }

  if (endedMatches > 0 || alreadyCheckedMatches > 0 || missingTimeMatches > 0 || invalidTimeMatches > 0) {
    diagnostics.push({
      key: 'not_schedulable',
      label: 'Matched but not scheduled',
      count: endedMatches + alreadyCheckedMatches + missingTimeMatches + invalidTimeMatches,
      detail: compactReasonList([
        endedMatches > 0 ? `${endedMatches} already ended` : '',
        alreadyCheckedMatches > 0 ? `${alreadyCheckedMatches} already checked` : '',
        missingTimeMatches > 0 ? `${missingTimeMatches} missing start/end time` : '',
        invalidTimeMatches > 0 ? `${invalidTimeMatches} with invalid start/end time` : '',
      ]),
      channels: channelsWithUnscheduledMatches,
    })
  }

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
