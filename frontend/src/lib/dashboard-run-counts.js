const countProgressStatus = (streams = [], status) => (
  streams.filter(stream => (
    stream?.status === status ||
    (status === 'blank' && stream?.blank_detected === true) ||
    (status === 'freeze' && stream?.freeze_detected === true)
  )).length
)

const countGoodProgressStreams = (streams = []) => (
  streams.filter(stream => (
    stream?.status === 'completed' &&
    stream?.blank_detected !== true &&
    stream?.freeze_detected !== true &&
    !['blank', 'freeze', 'low_quality', 'offline', 'unstable'].includes(stream?.dead_reason) &&
    [undefined, null, '', 'none'].includes(stream?.quality_reason_detail)
  )).length
)

const numericOrNull = (value) => {
  const number = Number(value)
  return Number.isFinite(number) ? number : null
}

const hasOwn = (source, key) => (
  source != null && Object.prototype.hasOwnProperty.call(source, key)
)

const getVisibilityCountsFromSource = (source) => {
  if (!source || typeof source !== 'object') return null
  const hasHidden = hasOwn(source, 'channels_hidden') || hasOwn(source, 'channels_hidden_count')
  const hasReady = hasOwn(source, 'channels_ready') || hasOwn(source, 'channels_ready_count')
  if (!hasHidden && !hasReady) return null

  return {
    hidden: numericOrNull(source.channels_hidden ?? source.channels_hidden_count) ?? 0,
    ready: numericOrNull(source.channels_ready ?? source.channels_ready_count) ?? 0,
  }
}

const getStreamCheckerVisibilityCounts = (streamCheckerStatus = {}) => {
  const progress = streamCheckerStatus?.progress || {}
  const queue = streamCheckerStatus?.queue || {}
  const sources = [
    queue,
    queue?.result_summary,
    queue?.run_snapshot?.result_summary,
    progress,
    progress?.stats,
    progress?.result_summary,
    progress?.run_snapshot?.result_summary,
    streamCheckerStatus?.result_summary,
    streamCheckerStatus?.run_snapshot?.result_summary,
  ]
  const counts = sources
    .map(getVisibilityCountsFromSource)
    .filter(Boolean)

  if (!counts.length) return null

  return counts.reduce((summary, count) => ({
    hidden: Math.max(summary.hidden, count.hidden),
    ready: Math.max(summary.ready, count.ready),
  }), { hidden: 0, ready: 0 })
}

export const getDashboardRunCounts = ({
  streamCheckerStatus,
  streamQueueActive = false,
  streamQueueHistory = false,
  streamCheckerOnlyActive = false,
  batchTotal = 0,
  completed = 0,
  runCounts = {},
} = {}) => {
  const progressStreams = streamCheckerStatus?.progress?.streams_detail || []
  const queueCounts = streamCheckerStatus?.queue || {}
  const queueGood = numericOrNull(queueCounts.good_streams_count)
  const queueDead = numericOrNull(queueCounts.dead_streams_count)
  const queueBlank = numericOrNull(queueCounts.blank_streams_count)
  const queueFreeze = numericOrNull(queueCounts.freeze_streams_count)
  const progressGood = countGoodProgressStreams(progressStreams)
  const progressDead = countProgressStatus(progressStreams, 'dead')
  const progressBlank = countProgressStatus(progressStreams, 'blank')
  const progressFreeze = countProgressStatus(progressStreams, 'freeze')
  const queueCountsVisible = streamQueueActive || streamQueueHistory
  const qualityOnlyRun = streamCheckerOnlyActive || streamQueueHistory
  const singleQualityOnlyRun = qualityOnlyRun && !streamQueueActive
  const activeStreamCheckerRun = streamQueueActive || qualityOnlyRun
  const streamCheckerVisibilityCounts = activeStreamCheckerRun
    ? getStreamCheckerVisibilityCounts(streamCheckerStatus)
    : null
  const useRunVisibilityCounts = !streamCheckerOnlyActive

  return {
    channels: queueCountsVisible
      ? batchTotal
      : singleQualityOnlyRun
        ? (streamCheckerStatus?.progress?.channel_id ? 1 : 0)
        : (runCounts.channels_with_periods ?? 0),
    playlists: qualityOnlyRun ? null : (runCounts.refreshed_playlists ?? 0),
    matched: qualityOnlyRun ? null : (runCounts.assigned_channels ?? 0),
    checked: queueCountsVisible ? completed : (singleQualityOnlyRun ? 0 : (runCounts.quality_checked ?? 0)),
    good: queueCountsVisible
      ? Math.max(queueGood ?? 0, progressGood)
      : (singleQualityOnlyRun ? progressGood : (runCounts.good_streams ?? 0)),
    dead: queueCountsVisible
      ? Math.max(queueDead ?? 0, progressDead)
      : (singleQualityOnlyRun ? progressDead : (runCounts.dead_streams ?? 0)),
    blank: queueCountsVisible
      ? Math.max(queueBlank ?? 0, progressBlank)
      : (singleQualityOnlyRun ? progressBlank : (runCounts.blank_streams ?? 0)),
    freeze: queueCountsVisible
      ? Math.max(queueFreeze ?? 0, progressFreeze)
      : (singleQualityOnlyRun ? progressFreeze : (runCounts.freeze_streams ?? 0)),
    hidden: streamCheckerVisibilityCounts?.hidden
      ?? (useRunVisibilityCounts ? (runCounts.channels_hidden ?? 0) : 0),
    ready: streamCheckerVisibilityCounts?.ready
      ?? (useRunVisibilityCounts ? (runCounts.channels_ready ?? 0) : 0),
  }
}

export const getDashboardRunMetrics = ({
  streamCheckerOnlyActive = false,
  streamQueueActive = false,
  streamQueueHistory = false,
  ...countArgs
} = {}) => {
  const counts = getDashboardRunCounts({
    ...countArgs,
    streamCheckerOnlyActive,
    streamQueueActive,
    streamQueueHistory,
  })
  const qualityOnlyRun = streamCheckerOnlyActive || streamQueueHistory
  const activeQueue = streamQueueActive || streamQueueHistory
  const singleQualityOnlyRun = streamCheckerOnlyActive && !streamQueueActive
  const activeStreamCheckerRun = activeQueue || qualityOnlyRun
  const streamProblemDescription = streamQueueHistory
    ? 'Streams classified by the last completed Stream Checker batch.'
    : 'Stream rows currently classified in the active checker view.'

  return [
    {
      key: 'channels',
      label: singleQualityOnlyRun
        ? 'Active Channel'
        : activeQueue
          ? 'Queued Channels'
          : 'Period Channels',
      value: counts.channels,
      description: singleQualityOnlyRun
        ? 'Channel currently being checked by Stream Checker.'
        : (
            activeQueue
              ? (streamQueueHistory ? 'Channels from the last completed Stream Checker batch.' : 'Channels in the active Stream Checker batch.')
              : 'Channel assignments selected by due automation periods.'
          ),
    },
    {
      key: 'playlists',
      label: 'Refresh Requests',
      value: counts.playlists,
      description: qualityOnlyRun || streamQueueHistory
        ? 'Not part of a quality-only Stream Checker run.'
        : 'M3U account refresh requests accepted by Dispatcharr.',
    },
    {
      key: 'matched',
      label: qualityOnlyRun || streamQueueHistory ? 'Stream Matching' : 'Channels Updated',
      value: counts.matched,
      description: qualityOnlyRun || streamQueueHistory
        ? 'Not part of a quality-only Stream Checker run.'
        : 'Channels that received new stream assignments during matching.',
    },
    {
      key: 'checked',
      label: 'Channels Checked',
      value: counts.checked,
      description: activeStreamCheckerRun
        ? (streamQueueHistory ? 'Channels completed by the last Stream Checker batch.' : 'Channels completed by the active Stream Checker batch.')
        : 'Channels completed by the automation quality stage.',
    },
    {
      key: 'good',
      label: 'Good Streams',
      value: counts.good,
      description: activeStreamCheckerRun
        ? (streamQueueHistory ? 'Clean streams from the last completed Stream Checker batch.' : 'Clean streams seen by the active Stream Checker batch.')
        : 'Streams that completed the automation quality stage without problem flags.',
    },
    {
      key: 'dead',
      label: 'Dead Streams',
      value: counts.dead,
      description: activeStreamCheckerRun
        ? streamProblemDescription
        : 'Streams classified as dead by the completed automation quality stage.',
    },
    {
      key: 'blank',
      label: 'Blank Streams',
      value: counts.blank,
      description: activeStreamCheckerRun
        ? streamProblemDescription
        : 'Streams classified as blank by the completed automation quality stage.',
    },
    {
      key: 'freeze',
      label: 'Frozen Streams',
      value: counts.freeze,
      description: activeStreamCheckerRun
        ? streamProblemDescription
        : 'Streams classified as frozen by the completed automation quality stage.',
    },
    {
      key: 'hidden',
      label: 'Channels Hidden',
      value: counts.hidden,
      description: activeStreamCheckerRun
        ? 'Channels hidden by visibility automation during this Stream Checker batch.'
        : 'Channels hidden by visibility automation during the automation run.',
    },
    {
      key: 'ready',
      label: 'Channels Ready',
      value: counts.ready,
      description: activeStreamCheckerRun
        ? 'Channels restored by visibility automation during this Stream Checker batch.'
        : 'Channels restored by visibility automation during the automation run.',
    },
  ]
}
