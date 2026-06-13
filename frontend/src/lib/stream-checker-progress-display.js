export const formatProgressMode = (mode) => {
  const labels = {
    automation_quality_check: 'Automation Quality Check',
    manual_full_run: 'Manual Full Run',
    manual_period_run: 'Manual Period Run',
    scheduler_run: 'Scheduler Run',
    single_channel_check: 'Single Channel Check',
    stream_checker: 'Stream Checker',
    teamarr_preflight: 'Teamarr Preflight',
  }
  if (!mode) return null
  return labels[mode] || String(mode).replace(/[_-]+/g, ' ').replace(/\b\w/g, (char) => char.toUpperCase())
}

export function getCurrentProgressDisplay(status, progress) {
  const queueSize = status?.queue?.queue_size || 0
  const inProgress = status?.queue?.in_progress || 0
  const progressStale = status?.progress_stale === true || progress?.stale === true
  const progressStaleDetails = status?.progress_stale_details || {}
  const progressStaleAge = progressStaleDetails.age_seconds ?? progress?.stale_age_seconds
  const isChecking = !progressStale && Boolean(
    status?.stream_checking_mode ||
    status?.checking ||
    queueSize > 0 ||
    inProgress > 0 ||
    (status?.queue?.current_channel !== null && status?.queue?.current_channel !== undefined)
  )

  const runProfileName = progress?.run_profile_name || progress?.automation_profile_name || null
  const runProfileSource = progress?.run_profile_source || progress?.automation_profile_source || null
  const qualityProfileName = progress?.quality_profile_name || progress?.automation_profile_name || null
  const qualityProfileSource = progress?.quality_profile_source || progress?.automation_profile_source || null
  const capacityProfileName = progress?.capacity_profile_name || null

  return {
    queueSize,
    inProgress,
    progressStale,
    progressStaleAge,
    isChecking,
    progressRunMode: formatProgressMode(progress?.run_mode),
    runProfileName,
    runProfileSource,
    qualityProfileName,
    qualityProfileSource,
    capacityProfileName,
    showQualityRules: Boolean(qualityProfileName && qualityProfileName !== runProfileName),
    showCurrentProgress: Boolean(progress && isChecking && !progressStale),
  }
}
