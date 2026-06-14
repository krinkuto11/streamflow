const RUN_MODE_LABELS = {
  scheduler_run: 'Scheduler Run',
  manual_full_run: 'Manual Full Run',
  manual_period_run: 'Manual Period Run',
  automation_quality_check: 'Automation Quality Check',
  stream_checker: 'Stream Checker',
  single_channel_check: 'Single Channel Check',
  teamarr_preflight: 'Teamarr Preflight',
}

const isPresent = (value) => (
  value !== undefined &&
  value !== null &&
  String(value).trim() !== ''
)

const firstPresent = (...values) => {
  for (const value of values) {
    if (isPresent(value)) return String(value).trim()
  }
  return null
}

const formatEnumLabel = (value) => (
  firstPresent(value)
    ?.replace(/[_-]+/g, ' ')
    .replace(/\s+/g, ' ')
    .trim()
    .replace(/\b\w/g, char => char.toUpperCase()) || null
)

const asArray = (value) => (Array.isArray(value) ? value : [])

const getSnapshot = (details = {}) => {
  const snapshot = details?.run_snapshot
  return snapshot && typeof snapshot === 'object' && !Array.isArray(snapshot) ? snapshot : {}
}

const summarizeProfiles = (profiles = []) => {
  const validProfiles = asArray(profiles).filter(profile => profile && typeof profile === 'object')
  if (validProfiles.length === 0) return null
  if (validProfiles.length === 1) {
    return firstPresent(validProfiles[0].profile_name, validProfiles[0].name)
  }
  return `${validProfiles.length} profiles`
}

const summarizeQualityRules = (rules = []) => {
  const validRules = asArray(rules).filter(rule => rule && typeof rule === 'object')
  if (validRules.length === 0) return null
  if (validRules.length === 1) {
    const rule = validRules[0]
    return firstPresent(rule.profile_name, rule.name, rule.quality_rules_name) || (rule.enabled ? 'Enabled' : 'Disabled')
  }
  const enabledCount = validRules.filter(rule => rule.enabled === true).length
  return `${enabledCount}/${validRules.length} enabled`
}

const summarizeCapacityProfile = (details = {}, snapshot = {}) => {
  const capacityContext = snapshot?.capacity_profile_context
  return firstPresent(
    details.capacity_profile_name,
    details.capacity_profile_source && formatEnumLabel(details.capacity_profile_source),
    capacityContext?.name,
    capacityContext?.type && formatEnumLabel(capacityContext.type)
  )
}

export function getChangelogRunContextBadges(details = {}) {
  const snapshot = getSnapshot(details)
  const runMode = firstPresent(details.run_mode, snapshot.run_mode)
  const runProfile = firstPresent(
    details.run_profile_name,
    details.automation_profile_name,
    details.profile_name,
    summarizeProfiles(snapshot.effective_profiles)
  )
  const qualityRules = firstPresent(
    details.quality_profile_name,
    details.quality_rules_name,
    summarizeQualityRules(snapshot.quality_rules)
  )
  const capacityProfile = summarizeCapacityProfile(details, snapshot)

  return [
    {
      key: 'run-mode',
      label: 'Run Mode',
      value: runMode ? (RUN_MODE_LABELS[runMode] || formatEnumLabel(runMode)) : null,
    },
    {
      key: 'run-profile',
      label: 'Run Profile',
      value: runProfile,
    },
    {
      key: 'quality-rules',
      label: 'Quality Rules',
      value: qualityRules,
    },
    {
      key: 'capacity-profile',
      label: 'Capacity Profile',
      value: capacityProfile,
    },
  ].filter(item => isPresent(item.value))
}

const STALE_WARNING_LABELS = {
  dispatcharr_status_risk: 'Dispatcharr Provider Notice',
  progress_stale: 'Previous Progress',
}

const staleWarningValue = (warning = {}) => {
  if (warning.type === 'dispatcharr_status_risk') {
    const count = Number(warning.count)
    const conflictText = Number.isFinite(count) && count > 0
      ? `${count} provider status ${count === 1 ? 'mismatch' : 'mismatches'}`
      : 'Detected'
    return warning.read_only === false ? conflictText : `${conflictText} / observed only`
  }
  return formatEnumLabel(warning.status || warning.type) || 'Detected'
}

export function getChangelogStaleWarnings(details = {}) {
  const snapshot = getSnapshot(details)
  const snapshotWarnings = asArray(snapshot.stale_warnings)
    .filter(warning => warning && typeof warning === 'object')

  const fallbackWarnings = []
  const dispatcharrStale = snapshot?.dispatcharr_status?.stale_status
  if (
    snapshotWarnings.length === 0 &&
    dispatcharrStale?.stale_status_suspected === true
  ) {
    fallbackWarnings.push({
      type: 'dispatcharr_status_risk',
      status: dispatcharrStale.status,
      count: dispatcharrStale.stale_suspected_count,
      read_only: dispatcharrStale.read_only,
    })
  }

  return [...snapshotWarnings, ...fallbackWarnings].map((warning, index) => ({
    key: `stale-warning-${warning.type || index}`,
    label: STALE_WARNING_LABELS[warning.type] || formatEnumLabel(warning.type) || 'Stale Warning',
    value: staleWarningValue(warning),
  })).filter(item => isPresent(item.value))
}

const hasOwn = (object, key) => Object.prototype.hasOwnProperty.call(object || {}, key)

const numericCount = (value) => {
  const number = Number(value)
  return Number.isFinite(number) ? number : 0
}

export function getChangelogVisibilityMetrics(details = {}) {
  const metrics = []
  if (hasOwn(details, 'channels_hidden')) {
    metrics.push({
      key: 'channels-hidden',
      label: 'Channels Hidden',
      value: numericCount(details.channels_hidden),
      className: 'text-amber-500',
    })
  }
  if (hasOwn(details, 'channels_ready')) {
    metrics.push({
      key: 'channels-ready',
      label: 'Channels Ready',
      value: numericCount(details.channels_ready),
      className: 'text-green-500',
    })
  }
  return metrics
}
