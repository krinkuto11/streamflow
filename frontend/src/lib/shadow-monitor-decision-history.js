export const shadowEventLabels = {
  probe_ok: 'Probe OK',
  blank_pending: 'Blank Pending',
  freeze_pending: 'Freeze Pending',
  no_decodable_frames_pending: 'Decoder Stall Pending',
  garbled_audio_pending: 'Garbled Audio Pending',
  silent_audio_pending: 'Silent Audio Pending',
  offline_image_pending: 'Offline Image Pending',
  dry_run_switch: 'Dry Run Switch',
  switch_success: 'Switch Success',
  switch_failed: 'Switch Failed',
  no_alternative: 'No Alternative',
  cooldown: 'Cooldown',
  stale_stream_guard: 'Stale Stream',
  switch_rate_limited: 'Rate Limited',
  viewer_left: 'Viewer Left',
  quality_check_active: 'Quality Check Active',
  watcher_reconnecting: 'Watcher Reconnecting',
  watcher_recovered: 'Watcher Recovered',
  watcher_recovery_guard: 'Watcher Recovery Guard',
  watcher_recovery_observed: 'Watcher Recovery Observed',
  pre_probe_unavailable: 'Pre-Probe Unavailable',
  pre_probe_rejected: 'Pre-Probe Rejected',
  offline_image_learned: 'Offline Image Learned',
}

const preProbeMetricLabels = {
  preprobe_attempted: 'attempted',
  preprobe_success: 'passed',
  preprobe_rejected_media_fault: 'rejected target',
  preprobe_skipped_provider_limit: 'skipped',
  preprobe_skipped_profile_limit: 'skipped',
  preprobe_skipped_missing_url: 'unavailable',
  preprobe_timeout: 'timed out',
  switch_prevented_by_preprobe: 'prevented switch',
}

const reasonLabels = {
  blank: 'Blank',
  freeze: 'Freeze',
  no_decodable_frames: 'Decoder Stall',
  garbled_audio: 'Garbled Audio',
  silent_audio: 'Silent Audio',
  offline_image: 'Offline Image',
  manual: 'Manual',
  pre_probe: 'Pre-Probe',
  provider_capacity: 'Provider Slot',
  active_viewers: 'Provider Slot',
  checking_capacity: 'Profile Slot',
  timeout: 'Timeout',
  probe_ok: 'Probe OK',
  active_watcher_between_confirmations: 'Watcher Recovered',
}

export const shadowDecisionFilters = [
  { key: 'all', label: 'All' },
  { key: 'switch', label: 'Switches' },
  { key: 'probe', label: 'Probes' },
  { key: 'pre_probe', label: 'Pre-Probes' },
  { key: 'guard', label: 'Guards' },
  { key: 'skip', label: 'Skips' },
  { key: 'learn', label: 'Learns' },
]

const titleizeCode = (value) => String(value || '')
  .split('_')
  .filter(Boolean)
  .map(part => part.charAt(0).toUpperCase() + part.slice(1))
  .join(' ')

const finiteNumber = (value) => {
  const parsed = Number(value)
  return Number.isFinite(parsed) ? parsed : null
}

const formatSeconds = (value) => {
  const number = finiteNumber(value)
  if (number === null) return null
  if (number < 60) return `${Math.round(number * 10) / 10}s`
  const minutes = Math.floor(number / 60)
  const seconds = Math.round(number % 60)
  return seconds ? `${minutes}m ${seconds}s` : `${minutes}m`
}

const formatRatio = (value) => {
  const number = finiteNumber(value)
  if (number === null) return null
  return number.toFixed(number >= 10 ? 0 : 2).replace(/\.?0+$/, '')
}

export const formatShadowEventType = (event = {}) => (
  shadowEventLabels[event?.type] || titleizeCode(event?.type) || 'Unknown'
)

export const formatShadowEventReason = (reason) => (
  reasonLabels[reason] || titleizeCode(reason) || 'Unknown'
)

export const formatShadowPreProbeStatus = (last = null) => {
  if (!last?.metric) return 'No pre-probe decisions'
  const metric = last.metric
  const action = preProbeMetricLabels[metric] || titleizeCode(metric)
  if (metric === 'preprobe_skipped_provider_limit') {
    return 'Last pre-probe skipped: provider slot unavailable'
  }
  if (metric === 'preprobe_skipped_profile_limit') {
    return 'Last pre-probe skipped: profile slot unavailable'
  }
  if (metric === 'preprobe_rejected_media_fault') {
    return `Last pre-probe rejected target: ${formatShadowEventReason(last.rejection_reason)}`
  }
  if (metric === 'preprobe_timeout') {
    return 'Last pre-probe timed out'
  }
  if (metric === 'preprobe_success') {
    const elapsed = finiteNumber(last.elapsed_ms)
    return elapsed === null ? 'Last pre-probe passed' : `Last pre-probe passed in ${Math.round(elapsed)}ms`
  }
  if (metric === 'switch_prevented_by_preprobe') {
    return 'Last switch prevented by pre-probe'
  }
  return `Last pre-probe ${action}`
}

export const getShadowEventDecisionGroup = (event = {}) => {
  if (event?.decision_group) return event.decision_group
  const type = String(event?.type || '')
  if (['switch_success', 'switch_failed', 'dry_run_switch'].includes(type)) return 'switch'
  if (['pre_probe_unavailable', 'pre_probe_rejected'].includes(type)) return 'pre_probe'
  if (type.endsWith('_pending') || type === 'probe_ok') return 'probe'
  if (type === 'offline_image_learned') return 'learn'
  if ([
    'cooldown',
    'stale_stream_guard',
    'switch_rate_limited',
    'quality_check_active',
    'watcher_recovery_guard',
    'watcher_recovery_observed',
  ].includes(type)) return 'guard'
  if (type === 'no_alternative') return 'skip'
  if (['viewer_left', 'watcher_reconnecting', 'watcher_recovered'].includes(type)) return 'watcher'
  return 'other'
}

export const filterShadowDecisionEvents = (events = [], filter = 'all') => (
  (events || []).filter(event => filter === 'all' || getShadowEventDecisionGroup(event) === filter)
)

const detectionParts = (detection = {}) => {
  const measurements = detection.measurements || {}
  const thresholds = detection.thresholds || {}
  const reason = detection.reason

  if (reason === 'blank') {
    const ratio = formatRatio(measurements.blank_ratio)
    const threshold = formatRatio(thresholds.blank_ratio_threshold)
    const duration = formatSeconds(measurements.blank_duration_secs)
    const minimum = formatSeconds(thresholds.blank_min_duration_seconds)
    return [
      ratio && threshold ? `blank ${ratio}/${threshold}` : null,
      duration && minimum ? `${duration}/${minimum}` : duration,
    ].filter(Boolean)
  }

  if (reason === 'freeze') {
    const ratio = formatRatio(measurements.freeze_ratio)
    const threshold = formatRatio(thresholds.freeze_ratio_threshold)
    const duration = formatSeconds(measurements.freeze_duration_secs)
    const minimum = formatSeconds(thresholds.freeze_min_duration_seconds)
    return [
      ratio && threshold ? `freeze ${ratio}/${threshold}` : null,
      duration && minimum ? `${duration}/${minimum}` : duration,
    ].filter(Boolean)
  }

  if (reason === 'no_decodable_frames') {
    const duration = formatSeconds(measurements.no_decodable_frames_duration_secs)
    const minimum = formatSeconds(thresholds.no_decodable_frames_min_duration_seconds)
    return [
      duration && minimum ? `${duration}/${minimum}` : duration,
      measurements.no_decodable_frames_error,
    ].filter(Boolean)
  }

  if (reason === 'garbled_audio') {
    const count = finiteNumber(measurements.garbled_audio_error_count)
    const threshold = finiteNumber(thresholds.garbled_audio_error_threshold)
    return [
      count !== null && threshold !== null ? `${count}/${threshold} audio errors` : null,
      measurements.garbled_audio_error,
    ].filter(Boolean)
  }

  if (reason === 'silent_audio') {
    const duration = formatSeconds(measurements.silent_audio_duration_secs)
    const minimum = formatSeconds(thresholds.silent_audio_min_duration_seconds)
    return [
      duration && minimum ? `${duration}/${minimum} silence` : duration,
      measurements.silent_audio_noise_db !== undefined ? `${measurements.silent_audio_noise_db} dB` : null,
    ].filter(Boolean)
  }

  if (reason === 'offline_image') {
    return [
      measurements.offline_image_distance !== undefined ? `pHash gap ${measurements.offline_image_distance}` : null,
    ].filter(Boolean)
  }

  return []
}

export const getShadowEventDetailParts = (event = {}) => {
  const details = event.details || {}
  const parts = []
  const reason = details.trigger_reason || event.trigger_reason || details.reason
  if (reason && reason !== 'probe_ok') {
    parts.push(formatShadowEventReason(reason))
  }
  if (details.pre_probe_metric && !details.pre_probe) {
    const metricLabel = preProbeMetricLabels[details.pre_probe_metric] || titleizeCode(details.pre_probe_metric)
    parts.push(`pre-probe ${metricLabel}`)
  }
  if (event.type === 'offline_image_learned') {
    parts.push(details.deduplicated ? 'deduplicated' : 'reference added')
    if (details.offline_image_distance !== undefined && details.offline_image_distance !== null) {
      parts.push(`nearest pHash gap ${details.offline_image_distance}`)
    }
    if (details.reference_count !== undefined) {
      parts.push(`${details.reference_count} references`)
    }
  }

  const detection = details.detection
  if (detection) {
    const compact = detectionParts(detection)
    if (compact.length) parts.push(compact.join(', '))
    if (detection.confirmations && detection.required) {
      parts.push(`${detection.confirmations}/${detection.required} confirmations`)
    }
  }

  if (details.pre_probe) {
    const result = details.pre_probe.result || 'unknown'
    const rejection = details.pre_probe.rejection_reason
    parts.push(rejection ? `pre-probe ${result}: ${formatShadowEventReason(rejection)}` : `pre-probe ${result}`)
  } else if (getShadowEventDecisionGroup(event) === 'pre_probe') {
    const result = details.result || 'unknown'
    const rejection = details.rejection_reason
    parts.push(rejection ? `pre-probe ${result}: ${formatShadowEventReason(rejection)}` : `pre-probe ${result}`)
  }

  if (details.origin_stream_ref && details.target_stream_ref) {
    parts.push(`${details.origin_stream_ref} -> ${details.target_stream_ref}`)
  } else if (details.target_stream_ref) {
    parts.push(`target ${details.target_stream_ref}`)
  }

  if (details.cooldown_seconds !== undefined) {
    const cooldown = formatSeconds(details.cooldown_seconds)
    if (cooldown) parts.push(`cooldown ${cooldown}`)
  }

  const viewers = details.viewer_context
  if (viewers?.real_client_count !== undefined) {
    parts.push(`${viewers.real_client_count} real viewer${Number(viewers.real_client_count) === 1 ? '' : 's'}`)
  }

  return parts.filter(Boolean)
}
