const countProgressStatus = (streams = [], status) => (
  streams.filter(stream => stream?.status === status).length
)

export const getDashboardRunCounts = ({
  streamCheckerStatus,
  streamQueueActive = false,
  batchTotal = 0,
  completed = 0,
  runCounts = {},
} = {}) => {
  const progressStreams = streamCheckerStatus?.progress?.streams_detail || []

  return {
    channels: streamQueueActive ? batchTotal : (runCounts.channels_with_periods ?? 0),
    playlists: runCounts.refreshed_playlists ?? 0,
    matched: runCounts.assigned_channels ?? 0,
    checked: streamQueueActive ? completed : (runCounts.quality_checked ?? 0),
    dead: streamQueueActive ? countProgressStatus(progressStreams, 'dead') : (runCounts.dead_streams ?? 0),
    blank: streamQueueActive ? countProgressStatus(progressStreams, 'blank') : (runCounts.blank_streams ?? 0),
    freeze: streamQueueActive ? countProgressStatus(progressStreams, 'freeze') : (runCounts.freeze_streams ?? 0),
  }
}
