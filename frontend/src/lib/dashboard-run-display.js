export const earliestStreamStart = (streams = []) => {
  const startedAtValues = streams
    .map(stream => Date.parse(stream?.started_at))
    .filter(timestamp => Number.isFinite(timestamp))

  return startedAtValues.length > 0
    ? Math.min(...startedAtValues)
    : null
}

const parseTimestamp = (value) => {
  const timestamp = Date.parse(value)
  return Number.isFinite(timestamp) ? timestamp : null
}

export const preferLiveRunSeconds = ({
  reportedSeconds,
  liveSeconds,
  active = false,
} = {}) => {
  if (active && Number.isFinite(liveSeconds) && liveSeconds > 0) {
    return liveSeconds
  }

  return reportedSeconds ?? liveSeconds
}

export const getStreamCheckerRunDisplay = ({
  streamCheckerStatus,
  runState = 'idle',
  runStage = 'idle',
  batchTotal = 0,
  completed = 0,
  now = Date.now(),
} = {}) => {
  const isProcessing = Boolean(streamCheckerStatus?.stream_checking_mode)
  const qualityStageActive = runStage === 'quality_checking' && batchTotal > 0
  const streamCheckerOnlyActive = isProcessing && runState !== 'running'
  const streamQueueActive = (qualityStageActive || streamCheckerOnlyActive) && batchTotal > 0
  const queueStartedAt = parseTimestamp(streamCheckerStatus?.queue?.started_at)
  const currentStreamStartedAt = earliestStreamStart(streamCheckerStatus?.progress?.streams_detail || [])
  const progressTimestamp = parseTimestamp(streamCheckerStatus?.progress?.timestamp)
  const streamCheckerStartedAt = queueStartedAt ?? currentStreamStartedAt ?? progressTimestamp
  const streamCheckerElapsedSeconds = streamCheckerStartedAt !== null
    ? Math.max(0, Math.floor((now - streamCheckerStartedAt) / 1000))
    : (isProcessing ? 0 : null)
  const currentStreamElapsedSeconds = currentStreamStartedAt !== null
    ? Math.max(0, Math.floor((now - currentStreamStartedAt) / 1000))
    : null

  return {
    currentStreamElapsedSeconds,
    isProcessing,
    qualityStageActive,
    stageCards: isProcessing
      ? [{
          key: 'quality_checking',
          label: 'Quality Check',
          status: 'running',
          current: completed,
          total: batchTotal,
        }]
      : [],
    streamCheckerElapsedSeconds,
    streamCheckerOnlyActive,
    streamQueueActive,
  }
}
