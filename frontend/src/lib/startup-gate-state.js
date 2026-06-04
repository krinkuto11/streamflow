export const getInitializationStateFromStatus = (data = {}) => {
  const hasCompletedCache = Boolean(data.last_refresh_time)

  return {
    inProgress: data.status === 'in_progress' && !hasCompletedCache,
    status: data.status || 'unknown',
    percentage: data.percentage ?? 0,
    message: data.message || '',
    started_at: data.started_at || null,
    elapsed_seconds: data.elapsed_seconds ?? null,
    last_refresh_duration_seconds: data.last_refresh_duration_seconds ?? null,
  }
}

export const getInitializationStateFromStatusError = (previousState = null) => {
  if (previousState && previousState.inProgress === false) {
    return {
      ...previousState,
      status: previousState.status || 'ready',
      message: previousState.message || 'Startup complete',
    }
  }

  return {
    inProgress: true,
    status: 'pending',
    percentage: 0,
    message: 'Checking startup status...',
    started_at: null,
    elapsed_seconds: null,
    last_refresh_duration_seconds: null,
  }
}

export const isStartupGateActive = ({
  setupComplete,
  initializationChecked,
  initialization,
}) => {
  if (!setupComplete) return false
  if (!initializationChecked) return true
  return Boolean(initialization?.inProgress)
}
