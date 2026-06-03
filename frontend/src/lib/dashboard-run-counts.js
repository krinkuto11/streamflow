const countProgressStatus = (streams = [], status) => (
  streams.filter(stream => stream?.status === status).length
)

export const getDashboardRunCounts = ({
  streamCheckerStatus,
  streamQueueActive = false,
  streamCheckerOnlyActive = false,
  batchTotal = 0,
  completed = 0,
  runCounts = {},
} = {}) => {
  const progressStreams = streamCheckerStatus?.progress?.streams_detail || []
  const qualityOnlyRun = streamCheckerOnlyActive

  return {
    channels: streamQueueActive ? batchTotal : (runCounts.channels_with_periods ?? 0),
    playlists: qualityOnlyRun ? null : (runCounts.refreshed_playlists ?? 0),
    matched: qualityOnlyRun ? null : (runCounts.assigned_channels ?? 0),
    checked: streamQueueActive ? completed : (runCounts.quality_checked ?? 0),
    dead: streamQueueActive ? countProgressStatus(progressStreams, 'dead') : (runCounts.dead_streams ?? 0),
    blank: streamQueueActive ? countProgressStatus(progressStreams, 'blank') : (runCounts.blank_streams ?? 0),
    freeze: streamQueueActive ? countProgressStatus(progressStreams, 'freeze') : (runCounts.freeze_streams ?? 0),
  }
}

export const getDashboardRunMetrics = ({
  streamCheckerOnlyActive = false,
  streamQueueActive = false,
  ...countArgs
} = {}) => {
  const counts = getDashboardRunCounts({
    ...countArgs,
    streamCheckerOnlyActive,
    streamQueueActive,
  })
  const qualityOnlyRun = streamCheckerOnlyActive
  const activeQueue = streamQueueActive

  return [
    {
      key: 'channels',
      label: activeQueue ? 'Queued Channels' : 'Period Channels',
      value: counts.channels,
      description: activeQueue
        ? 'Channels in the active Stream Checker batch.'
        : 'Channel assignments selected by due automation periods.',
    },
    {
      key: 'playlists',
      label: 'Playlists Refreshed',
      value: counts.playlists,
      description: qualityOnlyRun
        ? 'Not part of a quality-only Stream Checker run.'
        : 'M3U accounts refreshed during this automation run.',
    },
    {
      key: 'matched',
      label: qualityOnlyRun ? 'Stream Matching' : 'Channels Matched',
      value: counts.matched,
      description: qualityOnlyRun
        ? 'Not part of a quality-only Stream Checker run.'
        : 'Channels whose stream assignments changed or were validated during matching.',
    },
    {
      key: 'checked',
      label: 'Channels Checked',
      value: counts.checked,
      description: activeQueue
        ? 'Channels completed by the active Stream Checker batch.'
        : 'Channels completed by the automation quality stage.',
    },
    {
      key: 'dead',
      label: 'Dead Streams',
      value: counts.dead,
      description: activeQueue
        ? 'Stream rows currently classified as dead in the active checker view.'
        : 'Streams classified as dead by the completed automation quality stage.',
    },
    {
      key: 'blank',
      label: 'Blank Streams',
      value: counts.blank,
      description: activeQueue
        ? 'Stream rows currently classified as blank in the active checker view.'
        : 'Streams classified as blank by the completed automation quality stage.',
    },
    {
      key: 'freeze',
      label: 'Frozen Streams',
      value: counts.freeze,
      description: activeQueue
        ? 'Stream rows currently classified as frozen in the active checker view.'
        : 'Streams classified as frozen by the completed automation quality stage.',
    },
  ]
}
