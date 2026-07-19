export const formatStartupDuration = (seconds) => {
  if (seconds === null || seconds === undefined || seconds === '') return null

  const value = Number(seconds)
  if (!Number.isFinite(value) || value < 0) return null

  const totalSeconds = Math.floor(value)
  const minutes = Math.floor(totalSeconds / 60)
  const remainingSeconds = totalSeconds % 60
  const hours = Math.floor(minutes / 60)

  if (hours > 0) {
    return `${hours}h ${minutes % 60}m`
  }
  if (minutes > 0) {
    return `${minutes}m ${remainingSeconds}s`
  }
  return `${remainingSeconds}s`
}

export const getStartupDurationDisplay = (initialization = {}) => {
  const hasElapsedValue = initialization.elapsed_seconds !== null
    && initialization.elapsed_seconds !== undefined
    && initialization.elapsed_seconds !== ''
  const lastDuration = Number(initialization.last_refresh_duration_seconds)
  const elapsed = hasElapsedValue ? Number(initialization.elapsed_seconds) : Number.NaN
  const hasLastDuration = Number.isFinite(lastDuration) && lastDuration > 0
  const hasElapsed = Number.isFinite(elapsed) && elapsed >= 0
  const completed = initialization.status === 'completed'
    || Number(initialization.percentage) >= 100
  const elapsedLabel = formatStartupDuration(
    completed && !hasElapsed && hasLastDuration ? lastDuration : initialization.elapsed_seconds,
  )

  if (completed) {
    return {
      elapsedLabel,
      remainingLabel: null,
      estimateLabel: 'Status',
      estimateValue: 'Complete',
      estimateHint: 'Opening the dashboard.',
      expectation: 'Startup complete. Opening the dashboard.',
    }
  }

  const remainingLabel = hasLastDuration && hasElapsed
    ? formatStartupDuration(Math.max(0, lastDuration - elapsed))
    : null
  const estimateLabel = remainingLabel ? 'Remaining' : 'Expected'
  const estimateValue = remainingLabel ? `About ${remainingLabel}` : '2-5 minutes'
  const estimateHint = remainingLabel
    ? 'Based on the last cache refresh.'
    : 'Large playlists can take a few minutes on first load.'

  return {
    elapsedLabel,
    remainingLabel,
    estimateLabel,
    estimateValue,
    estimateHint,
    expectation: remainingLabel
      ? `About ${remainingLabel} remaining based on the last cache refresh.`
      : 'Large playlists can take 2-5 minutes on first load.',
  }
}
