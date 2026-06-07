import { formatDuration } from './time-format.js'

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

const SINGLE_CHANNEL_STATUS_STAGE = {
  starting: 'settings',
  preparing: 'settings',
  m3u_refresh: 'm3u_refresh',
  cache_sync: 'cache_sync',
  stream_matching: 'stream_matching',
  matching: 'stream_matching',
  quality_checking: 'quality_checking',
  checking: 'quality_checking',
  finalizing: 'finalizing',
  complete: 'finalizing',
  completed: 'finalizing',
}

const SINGLE_CHANNEL_STAGE_LABELS = {
  settings: 'Preparing',
  m3u_refresh: 'M3U Refresh',
  cache_sync: 'Cache Sync',
  stream_matching: 'Matching',
  quality_checking: 'Quality Check',
  finalizing: 'Finalizing',
}

const getSingleChannelStageId = (progress = {}) => {
  const statusStage = SINGLE_CHANNEL_STATUS_STAGE[String(progress?.status || '').toLowerCase()]
  if (statusStage) {
    return statusStage
  }

  const text = `${progress?.step || ''} ${progress?.step_detail || ''}`.toLowerCase()
  if (text.includes('cache') || text.includes('udi')) {
    return 'cache_sync'
  }
  if (text.includes('m3u') || text.includes('playlist') || text.includes('provider')) {
    return 'm3u_refresh'
  }
  if (text.includes('match') || text.includes('validating')) {
    return 'stream_matching'
  }
  if (text.includes('quality') || text.includes('checking streams')) {
    return 'quality_checking'
  }
  if (text.includes('final')) {
    return 'finalizing'
  }
  return 'settings'
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

export const getAutomationStageCards = ({
  stages = [],
  runStatusStages = [],
  displayRunStageId = 'idle',
  displayRunningRun = false,
  completedRun = false,
  neutralRun = false,
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

    if (neutralRun && !streamRunActive) {
      status = 'pending'
    } else if (streamRunActive) {
      status = stage.id === normalizedDisplayStageId
        ? (displayRunningRun ? 'running' : 'completed')
        : 'pending'
    } else if (!status || status === 'pending') {
      if (completedRun) {
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
  const normalizedStageId = normalizeRunStageKey(stageId)
  const normalizedDisplayStageId = normalizeRunStageKey(displayRunStageId)

  if (streamRunActive) {
    if (normalizedStageId === normalizedDisplayStageId) {
      return displayRunStageElapsedSeconds ?? streamCheckerElapsedSeconds
    }

    return null
  }

  const reportedSeconds = runDurations?.[durationKey]
  if (reportedSeconds !== null && reportedSeconds !== undefined) {
    return reportedSeconds
  }

  if (displayRunningRun && normalizedStageId === normalizedDisplayStageId) {
    return displayRunStageElapsedSeconds
  }

  return reportedSeconds
}

export const isM3uRefreshSkipped = ({
  runCounts = {},
  streamRunActive = false,
} = {}) => {
  if (streamRunActive) {
    return false
  }

  const playlistsToRefresh = runCounts.playlists_to_refresh
  const refreshedPlaylists = runCounts.refreshed_playlists ?? 0

  return refreshedPlaylists === 0 && (
    playlistsToRefresh === 0 ||
    playlistsToRefresh === '0' ||
    playlistsToRefresh === false
  )
}

export const getRunDurationCardValue = ({
  seconds = null,
  skipped = false,
} = {}) => {
  if (skipped) {
    return 'Skipped'
  }

  return formatDuration(seconds) || 'N/A'
}

const finiteNumber = (value) => {
  const number = Number(value)
  return Number.isFinite(number) ? number : null
}

export const getM3uRefreshCardDetail = ({
  runCounts = {},
  skipped = false,
  streamRunActive = false,
} = {}) => {
  if (streamRunActive) {
    return null
  }

  if (skipped) {
    return 'No playlist refresh requested'
  }

  const state = runCounts.m3u_refresh_state
  const current = finiteNumber(runCounts.m3u_refresh_current)
  const total = finiteNumber(runCounts.m3u_refresh_total)
  const refreshed = finiteNumber(runCounts.refreshed_playlists)
  const playlistsToRefresh = runCounts.playlists_to_refresh

  if (state === 'skipped') {
    return 'No active playlists matched'
  }

  if (state === 'planned' && total !== null && total > 0) {
    return `Preparing ${total} playlist refresh request${total === 1 ? '' : 's'}`
  }

  if (state === 'requesting' && total !== null && total > 0) {
    const active = current !== null ? Math.min(current + 1, total) : 1
    return `Refreshing playlist ${active}/${total}`
  }

  if (state === 'already_running' && total !== null && total > 0) {
    return `${current ?? total}/${total} refresh request${total === 1 ? '' : 's'} already running`
  }

  if (state === 'retrying_failed') {
    const retryCount = finiteNumber(runCounts.m3u_refresh_wait_retry_count)
    const failedCount = finiteNumber(runCounts.m3u_refresh_wait_failed_accounts)
    if (failedCount !== null) {
      return `Retrying failed providers${retryCount !== null ? ` (${retryCount}/${failedCount} accepted)` : ` (${failedCount})`}`
    }
    return 'Retrying failed providers'
  }

  if (state === 'waiting') {
    const elapsed = finiteNumber(runCounts.m3u_refresh_wait_elapsed_seconds)
    const streamsSeen = finiteNumber(runCounts.m3u_refresh_wait_streams_seen)
    if (streamsSeen !== null) {
      return `Waiting for Dispatcharr to settle (${streamsSeen} streams${elapsed !== null ? `, ${elapsed}s` : ''})`
    }
    return `Waiting for Dispatcharr to settle${elapsed !== null ? ` (${elapsed}s)` : ''}`
  }

  if (state === 'settled') {
    const streamsSeen = finiteNumber(runCounts.m3u_refresh_wait_streams_seen)
    return streamsSeen !== null
      ? `Playlist refresh settled (${streamsSeen} streams)`
      : 'Playlist refresh settled'
  }

  if (state === 'partial') {
    const failedCount = finiteNumber(runCounts.m3u_refresh_wait_failed_accounts)
    if (failedCount !== null && failedCount > 0) {
      return `Playlist refresh settled with ${failedCount} failed provider${failedCount === 1 ? '' : 's'}`
    }
    return 'Playlist refresh partially accepted'
  }

  if (state === 'timeout') {
    return 'Playlist refresh wait timed out'
  }

  if (['accepted', 'completed'].includes(state) && current !== null && total !== null && total > 0) {
    return `${current}/${total} refresh request${total === 1 ? '' : 's'} accepted`
  }

  if (state === 'failed' && current !== null && total !== null && total > 0) {
    return `${current}/${total} refresh request${total === 1 ? '' : 's'} failed`
  }

  if (refreshed !== null && refreshed > 0) {
    return `${refreshed} refresh request${refreshed === 1 ? '' : 's'} accepted`
  }

  if (playlistsToRefresh === 'all') {
    return 'All playlists selected'
  }

  return null
}

export const getCacheSyncCardDetail = ({
  runCounts = {},
  skipped = false,
  streamRunActive = false,
} = {}) => {
  if (streamRunActive) {
    return null
  }

  if (skipped) {
    return 'No cache sync requested'
  }

  const state = runCounts.cache_sync_state
  const current = finiteNumber(runCounts.cache_sync_successful_steps)
  const total = finiteNumber(runCounts.cache_sync_total_steps)

  if (current !== null && total !== null && total > 0) {
    const suffix = state === 'warning' ? 'completed with warnings' : 'completed'
    return `${current}/${total} cache sync steps ${suffix}`
  }

  if (state === 'warning') {
    return 'Cache sync reported warnings'
  }

  if (state === 'completed') {
    return 'Cache sync completed'
  }

  return null
}

export const getRunHistoryBaseline = ({
  summary = {},
} = {}) => {
  const sampleCount = finiteNumber(summary?.sample_count) ?? 0
  if (sampleCount <= 0) {
    return {
      available: false,
      sampleCount: 0,
      latest: null,
      typicalDurationSeconds: null,
      averageDurationSeconds: null,
      typicalSecondsPerChannel: null,
      perChannelSampleCount: 0,
      perChannelBaselineStable: false,
    }
  }

  return {
    available: true,
    sampleCount,
    latest: summary.latest || null,
    typicalDurationSeconds: finiteNumber(summary.typical_duration_seconds),
    averageDurationSeconds: finiteNumber(summary.average_duration_seconds),
    typicalSecondsPerChannel: finiteNumber(summary.typical_seconds_per_channel),
    perChannelSampleCount: finiteNumber(summary.per_channel_sample_count) ?? 0,
    perChannelBaselineStable: Boolean(summary.per_channel_baseline_stable),
  }
}

export const getStreamCheckerRunDisplay = ({
  streamCheckerStatus,
  runState = 'idle',
  runStage = 'idle',
  batchTotal = 0,
  completed = 0,
  now = Date.now(),
} = {}) => {
  const queue = streamCheckerStatus?.queue || {}
  const progress = streamCheckerStatus?.progress || {}
  const queueActive = Number(queue?.queue_size || 0) > 0
    || Number(queue?.in_progress || 0) > 0
    || (queue?.current_channel !== null && queue?.current_channel !== undefined)
  const singleChannelProgressActive = Boolean(progress?.is_single_channel_check)
  const isProcessing = Boolean(
    streamCheckerStatus?.stream_checking_mode
    || streamCheckerStatus?.checking
    || queueActive
    || singleChannelProgressActive
  )
  const singleChannelStageId = singleChannelProgressActive ? getSingleChannelStageId(progress) : null
  const activeBatchTotal = isProcessing && !singleChannelProgressActive ? batchTotal : 0
  const qualityStageActive = runStage === 'quality_checking' && activeBatchTotal > 0
  const streamCheckerOnlyActive = isProcessing && runState !== 'running'
  const streamQueueActive = (qualityStageActive || streamCheckerOnlyActive) && activeBatchTotal > 0
  const queueStartedAt = isProcessing && !singleChannelProgressActive
    ? parseTimestamp(streamCheckerStatus?.queue?.started_at)
    : null
  const currentStreamStartedAt = isProcessing
    ? earliestStreamStart(streamCheckerStatus?.progress?.streams_detail || [])
    : null
  const progressTimestamp = isProcessing ? parseTimestamp(streamCheckerStatus?.progress?.timestamp) : null
  const streamCheckerStartedAt = queueStartedAt ?? currentStreamStartedAt ?? progressTimestamp
  const streamCheckerElapsedSeconds = streamCheckerStartedAt !== null
    ? Math.max(0, Math.floor((now - streamCheckerStartedAt) / 1000))
    : (isProcessing ? 0 : null)
  const currentStreamElapsedSeconds = currentStreamStartedAt !== null
    ? Math.max(0, Math.floor((now - currentStreamStartedAt) / 1000))
    : null

  return {
    currentStreamElapsedSeconds,
    displayMessage: singleChannelProgressActive
      ? 'Running single channel check'
      : 'Running manual quality checks',
    displayStageId: singleChannelStageId || 'quality_checking',
    displayStageLabel: singleChannelStageId
      ? SINGLE_CHANNEL_STAGE_LABELS[singleChannelStageId] || 'Single Channel Check'
      : 'Quality Checking',
    isProcessing,
    qualityStageActive,
    singleChannelProgressActive,
    stageCards: isProcessing
      ? [{
          key: singleChannelStageId || 'quality_checking',
          label: singleChannelStageId
            ? SINGLE_CHANNEL_STAGE_LABELS[singleChannelStageId] || 'Single Channel Check'
            : 'Quality Check',
          status: 'running',
          current: singleChannelProgressActive ? Number(progress?.current_stream || 0) : completed,
          total: singleChannelProgressActive ? Number(progress?.total_streams || 1) : batchTotal,
        }]
      : [],
    streamCheckerElapsedSeconds,
    streamCheckerOnlyActive,
    streamQueueActive,
  }
}

export const getSkippedRunDisplay = ({
  skippedRun = false,
  streamRunActive = false,
  streamQueueActive = false,
  runProgressMessage = '',
  runStatusMessage = '',
} = {}) => {
  const noDueRun = skippedRun && !streamRunActive && !streamQueueActive
  if (!noDueRun) {
    return {
      badgeLabel: null,
      message: null,
      progressDetail: null,
      stageLabel: null,
    }
  }

  return {
    badgeLabel: 'Idle',
    message: 'Waiting for next scheduled run',
    progressDetail: runProgressMessage || runStatusMessage || 'No active automation periods are due',
    stageLabel: 'No Due Periods',
  }
}

export const getAbortedRunDisplay = ({
  runState = 'idle',
  runStatus = {},
} = {}) => {
  const aborted = runState === 'aborted'
    || runStatus?.status === 'aborted'
    || runStatus?.stage === 'aborted'

  if (!aborted) {
    return {
      message: null,
      progressDetail: null,
      progressPercent: null,
    }
  }

  const message = runStatus?.last_error
    || runStatus?.message
    || 'Automation run was stopped by the user'

  return {
    message,
    progressDetail: message,
    progressPercent: 0,
  }
}

export const shouldShowAutomationRunCard = ({
  showRunProgress = false,
  skippedRunDisplay = {},
} = {}) => {
  const idleNoDueRun = skippedRunDisplay?.badgeLabel === 'Idle'
    && skippedRunDisplay?.stageLabel === 'No Due Periods'

  return Boolean(showRunProgress && !idleNoDueRun)
}

export const getDashboardActionStates = ({
  actionLoading = '',
  isStreamCheckerProcessing = false,
  udiInitializing = false,
  udiSyncing = false,
} = {}) => {
  const actionBusy = actionLoading !== ''
  const reloadUdiReason = udiSyncing
    ? 'UDI sync is already running.'
    : udiInitializing
      ? 'Dispatcharr cache refresh is still running.'
    : actionBusy
      ? 'Another dashboard action is running.'
      : null
  const runAutomationReason = udiInitializing
    ? 'Automation can start after the Dispatcharr cache is ready.'
    : isStreamCheckerProcessing
    ? 'Automation cannot start while a stream check is already active.'
    : actionBusy
      ? 'Another dashboard action is running.'
      : null

  return {
    reloadUdi: {
      disabled: udiSyncing || udiInitializing || actionBusy,
      reason: reloadUdiReason,
    },
    runAutomation: {
      disabled: udiInitializing || isStreamCheckerProcessing || actionBusy,
      reason: runAutomationReason,
    },
  }
}
