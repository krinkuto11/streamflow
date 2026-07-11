export const getInitializationStateFromStatus = (data = {}) => {
  const readiness = typeof data.ready === 'boolean' ? data : null
  const statusData = readiness?.initialization || data
  const hasCompletedCache = Boolean(statusData.last_refresh_time)

  return {
    inProgress: readiness
      ? !readiness.ready
      : statusData.status === 'in_progress' && !hasCompletedCache,
    status: readiness?.status || statusData.status || 'unknown',
    percentage: statusData.percentage ?? (readiness?.ready ? 100 : 0),
    message: statusData.message || '',
    started_at: statusData.started_at || null,
    elapsed_seconds: statusData.elapsed_seconds ?? null,
    last_refresh_duration_seconds: statusData.last_refresh_duration_seconds ?? null,
    checks: readiness?.checks || null,
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

export const shouldRedirectForStartupGate = ({
  setupComplete,
  initializationChecked,
  initialization,
  pathname,
}) => {
  if (!setupComplete || !initializationChecked) return false
  if (!initialization?.inProgress) return false
  return pathname !== '/' && pathname !== '/dashboard'
}
