const countProgressStatus = (streams = [], status) => (
  streams.filter(stream => stream?.status === status).length
)

const numericOrNull = (value) => {
  const number = Number(value)
  return Number.isFinite(number) ? number : null
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
  const queueDead = numericOrNull(queueCounts.dead_streams_count)
  const queueBlank = numericOrNull(queueCounts.blank_streams_count)
  const queueFreeze = numericOrNull(queueCounts.freeze_streams_count)
  const queueCountsVisible = streamQueueActive || streamQueueHistory
  const qualityOnlyRun = streamCheckerOnlyActive || streamQueueHistory
  const singleQualityOnlyRun = qualityOnlyRun && !streamQueueActive
  const activeStreamCheckerRun = streamQueueActive || qualityOnlyRun

  return {
    channels: queueCountsVisible
      ? batchTotal
      : singleQualityOnlyRun
        ? (streamCheckerStatus?.progress?.channel_id ? 1 : 0)
        : (runCounts.channels_with_periods ?? 0),
    playlists: qualityOnlyRun ? null : (runCounts.refreshed_playlists ?? 0),
    matched: qualityOnlyRun ? null : (runCounts.assigned_channels ?? 0),
    checked: queueCountsVisible ? completed : (singleQualityOnlyRun ? 0 : (runCounts.quality_checked ?? 0)),
    dead: queueCountsVisible
      ? (queueDead ?? countProgressStatus(progressStreams, 'dead'))
      : (singleQualityOnlyRun ? countProgressStatus(progressStreams, 'dead') : (runCounts.dead_streams ?? 0)),
    blank: queueCountsVisible
      ? (queueBlank ?? countProgressStatus(progressStreams, 'blank'))
      : (singleQualityOnlyRun ? countProgressStatus(progressStreams, 'blank') : (runCounts.blank_streams ?? 0)),
    freeze: queueCountsVisible
      ? (queueFreeze ?? countProgressStatus(progressStreams, 'freeze'))
      : (singleQualityOnlyRun ? countProgressStatus(progressStreams, 'freeze') : (runCounts.freeze_streams ?? 0)),
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
  ]
}
