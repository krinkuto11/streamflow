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

const STAGE_KEY_ALIASES = {
  preparing: 'settings',
  schedule: 'period_discovery',
  udi_sync: 'cache_sync',
  matching: 'stream_matching',
  quality_check: 'quality_checking',
}

export const normalizeRunStageKey = (key) => STAGE_KEY_ALIASES[key] || key

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

export const getAutomationStageCards = ({
  stages = [],
  runStatusStages = [],
  displayRunStageId = 'idle',
  displayRunningRun = false,
  completedRun = false,
  streamRunActive = false,
} = {}) => {
  const normalizedDisplayStageId = normalizeRunStageKey(displayRunStageId)
  const displayCurrentStageIndex = stages.findIndex(stage => stage.id === normalizedDisplayStageId)
  const backendStages = new Map(
    (runStatusStages || []).map(stage => [normalizeRunStageKey(stage?.key), stage])
  )

  return stages.map((stage, index) => {
    const backendStage = backendStages.get(stage.id)
    let status = backendStage?.status

    if (!status || status === 'pending') {
      if (streamRunActive && displayCurrentStageIndex >= 0) {
        if (index < displayCurrentStageIndex) {
          status = 'completed'
        } else if (index === displayCurrentStageIndex) {
          status = displayRunningRun ? 'running' : 'completed'
        } else {
          status = 'pending'
        }
      } else if (completedRun) {
        status = 'completed'
      } else if (displayRunningRun && displayCurrentStageIndex >= 0 && index < displayCurrentStageIndex) {
        status = 'completed'
      } else if (displayRunningRun && index === displayCurrentStageIndex) {
        status = 'running'
      } else {
        status = 'pending'
      }
    }

    return {
      ...stage,
      ...backendStage,
      id: stage.id,
      label: stage.label,
      status,
    }
  })
}

export const getRunDurationValue = ({
  runDurations = {},
  durationKey,
  stageId,
  displayRunStageId,
  displayRunningRun = false,
  streamRunActive = false,
  streamCheckerElapsedSeconds = null,
  displayRunStageElapsedSeconds = null,
  stages = [],
} = {}) => {
  const reportedSeconds = runDurations?.[durationKey]
  if (reportedSeconds !== null && reportedSeconds !== undefined) {
    return reportedSeconds
  }

  const normalizedStageId = normalizeRunStageKey(stageId)
  const normalizedDisplayStageId = normalizeRunStageKey(displayRunStageId)

  if (streamRunActive) {
    if (normalizedStageId === 'quality_checking') {
      return streamCheckerElapsedSeconds
    }

    const stageIndex = stages.findIndex(stage => stage.id === normalizedStageId)
    const currentIndex = stages.findIndex(stage => stage.id === normalizedDisplayStageId)
    if (stageIndex >= 0 && currentIndex >= 0 && stageIndex < currentIndex) {
      return 0
    }
  }

  if (displayRunningRun && normalizedStageId === normalizedDisplayStageId) {
    return displayRunStageElapsedSeconds
  }

  return reportedSeconds
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

export const getDashboardActionStates = ({
  actionLoading = '',
  isStreamCheckerProcessing = false,
  udiSyncing = false,
} = {}) => {
  const actionBusy = actionLoading !== ''
  const reloadUdiReason = udiSyncing
    ? 'UDI sync is already running.'
    : actionBusy
      ? 'Another dashboard action is running.'
      : null
  const runAutomationReason = isStreamCheckerProcessing
    ? 'Automation cannot start while a stream check is already active.'
    : actionBusy
      ? 'Another dashboard action is running.'
      : null

  return {
    reloadUdi: {
      disabled: udiSyncing || actionBusy,
      reason: reloadUdiReason,
    },
    runAutomation: {
      disabled: isStreamCheckerProcessing || actionBusy,
      reason: runAutomationReason,
    },
  }
}
