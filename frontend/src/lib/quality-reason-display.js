const REASON_LABELS = {
  probe_exited_early: 'Probe exited early',
  blank_detected: 'Blank video detected',
  freeze_detected: 'Frozen video detected',
  zero_resolution: 'No video resolution',
  zero_resolution_dimension: 'Invalid video resolution',
  zero_bitrate: 'No bitrate detected',
  missing_bitrate: 'Needs bitrate recheck',
  resolution_width_below_threshold: 'Resolution width below threshold',
  resolution_height_below_threshold: 'Resolution height below threshold',
  bitrate_below_threshold: 'Bitrate below threshold',
  fps_below_threshold: 'FPS below threshold',
  score_below_threshold: 'Score below threshold',
  viewer_preempted: 'Viewer needed this profile',
  active_viewers: 'Active viewer protection',
  quota_consumed_by_active_viewers: 'Capacity used by active viewers',
  max_streams_reached: 'Provider stream limit reached',
  checking_capacity: 'Check slots full',
  global_worker_limit: 'Global workers full',
  provider_capacity: 'Provider capacity unavailable',
  provider_capacity_unavailable: 'Provider capacity unavailable',
  provider_wait_timeout: 'Provider wait timed out',
  connectivity_guard: 'Connectivity guard stopped this check',
  connectivity_timeout: 'Connectivity probe timed out',
  network_unreachable: 'Network endpoint unreachable',
  dns_resolution_failed: 'DNS resolution failed',
  dispatcharr_auth_failed: 'Dispatcharr authentication failed',
  endpoint_unhealthy: 'Connectivity endpoint unhealthy',
  connectivity_guard_error: 'Connectivity guard error',
  invalid_probe_endpoint: 'Invalid connectivity probe endpoint',
  timeout: 'Stream analysis timed out',
  Timeout: 'Stream analysis timed out',
  stream_timeout: 'Stream analysis timed out',
  probe_timeout: 'Stream analysis timed out',
  Error: 'Stream analysis failed',
  error: 'Stream analysis failed',
}

const STATUS_REASON_FALLBACKS = {
  viewer_preempted: 'viewer_preempted',
  provider_limit_wait_timeout: 'provider_wait_timeout',
  waiting_provider_limit: 'provider_capacity_unavailable',
  incomplete_bitrate: 'missing_bitrate',
  error: 'error',
}

const ACTIVE_STREAM_STATUSES = new Set(['checking', 'probing'])

const STATIC_REASON_DETAILS = {
  viewer_preempted: 'Real playback kept the profile slot; check again later',
  active_viewers: 'Viewer protection kept the stream untouched',
  quota_consumed_by_active_viewers: 'Active viewers are using the available capacity',
  max_streams_reached: 'The provider stream limit is already full',
  checking_capacity: 'The checker has no free slot for this account or profile yet',
  global_worker_limit: 'The global Stream Checker worker limit is full',
  provider_capacity: 'No provider or profile check slot is free',
  provider_capacity_unavailable: 'No provider or profile check slot is free',
  provider_wait_timeout: 'No provider or profile slot became free in time',
}

const formatNumber = (value, suffix = '') => {
  if (value === null || value === undefined || value === '') return null
  const numeric = Number(value)
  if (!Number.isFinite(numeric)) return `${value}${suffix}`
  const rendered = Number.isInteger(numeric) ? `${numeric}` : numeric.toFixed(1)
  return `${rendered}${suffix}`
}

const formatComparison = (context, suffix = '') => {
  const actual = formatNumber(context?.actual, suffix)
  const threshold = formatNumber(context?.threshold, suffix)
  if (!actual || !threshold) return null
  return `${actual} < ${threshold}`
}

const titleizeCode = (code) => {
  if (!code) return ''
  return String(code)
    .replace(/[_-]+/g, ' ')
    .replace(/\b\w/g, (char) => char.toUpperCase())
}

const uniqueParts = (parts) => {
  const seen = new Set()
  return parts.filter((part) => {
    if (!part || seen.has(part)) return false
    seen.add(part)
    return true
  })
}

const formatProbeLabel = (label) => titleizeCode(label).replace(/\bApi\b/g, 'API')

const pickConnectivityDetails = (context = {}) => {
  if (Array.isArray(context.attempts)) {
    return context.attempts[context.attempts.length - 1] || {}
  }
  if (context.dispatcharr_api) return pickConnectivityDetails(context.dispatcharr_api)
  if (context.internet) return pickConnectivityDetails(context.internet)
  if (context.details) return pickConnectivityDetails(context.details)
  return context
}

const formatAttempts = (context = {}) => {
  const attempts = formatNumber(context.attempts || context.attempt)
  const maxAttempts = formatNumber(context.max_attempts || context.total_attempts)
  if (attempts && maxAttempts) return `attempt ${attempts}/${maxAttempts}`
  if (attempts) return `${attempts} attempt${attempts === '1' ? '' : 's'}`
  return null
}

const formatConnectivityDetail = (context = {}) => {
  const details = pickConnectivityDetails(context)
  const label = formatProbeLabel(details.label)
  const target = [label, details.host].filter(Boolean).join(' ')
  const status = details.status_code ? `HTTP ${details.status_code}` : null
  const attempts = formatAttempts(details)
  const timeout = formatNumber(details.timeout_seconds || context.timeout_seconds, 's')
  const authRefresh = details.auth_refresh_attempted
    ? `auth refresh ${details.auth_refresh_ok ? 'ok' : 'failed'}`
    : null

  return uniqueParts([
    context.message || details.message,
    target || null,
    status,
    attempts,
    timeout ? `timeout ${timeout}` : null,
    authRefresh,
  ]).join(', ') || null
}

const formatAnalysisFailureDetail = (context = {}) => {
  const elapsed = formatNumber(context.elapsed_seconds || context.elapsed_time, 's')
  const limit = formatNumber(
    context.timeout_seconds || context.analysis_timeout_seconds || context.timeout,
    's',
  )
  const operation = formatNumber(context.operation_timeout_seconds, 's')
  const window = formatNumber(context.ffmpeg_duration_seconds || context.ffmpeg_duration, 's')
  const startup = formatNumber(
    context.startup_buffer_seconds || context.stream_startup_buffer,
    's',
  )
  const attempts = formatAttempts(context)
  const message = context.message || context.error
  const timing = elapsed && limit ? `${elapsed} of ${limit}` : (limit ? `limit ${limit}` : null)
  const breakdown = [
    operation ? `base ${operation}` : null,
    window ? `window ${window}` : null,
    startup ? `startup ${startup}` : null,
  ].filter(Boolean).join(' + ')

  return uniqueParts([
    message,
    timing,
    breakdown || null,
    attempts,
  ]).join(', ') || null
}

export function getQualityReasonDisplay(stream = {}) {
  if (ACTIVE_STREAM_STATUSES.has(stream.status)) return null

  const code = stream.quality_reason_detail
    || stream.reason_detail
    || STATUS_REASON_FALLBACKS[stream.status]
  if (!code || code === 'none') return null

  const context = stream.quality_reason_context || {}
  const label = REASON_LABELS[code] || titleizeCode(code)
  let detail = null

  if (code === 'bitrate_below_threshold') {
    detail = formatComparison(context, ' kbps')
  } else if (code === 'fps_below_threshold') {
    detail = formatComparison(context, ' fps')
  } else if (code === 'score_below_threshold') {
    detail = formatComparison(context)
  } else if (
    code === 'resolution_width_below_threshold'
    || code === 'resolution_height_below_threshold'
  ) {
    detail = formatComparison(context, ' px')
  } else if (code === 'zero_resolution' && context.actual) {
    detail = String(context.actual)
  } else if (code === 'zero_resolution_dimension') {
    const width = formatNumber(context.actual_width, ' px')
    const height = formatNumber(context.actual_height, ' px')
    detail = width && height ? `${width} x ${height}` : null
  } else if (code === 'blank_detected') {
    const ratio = formatNumber(context.blank_ratio)
    const duration = formatNumber(context.blank_duration_secs, 's')
    detail = [ratio ? `ratio ${ratio}` : null, duration].filter(Boolean).join(', ') || null
  } else if (code === 'freeze_detected') {
    const ratio = formatNumber(context.freeze_ratio)
    const duration = formatNumber(context.freeze_duration_secs, 's')
    detail = [ratio ? `ratio ${ratio}` : null, duration].filter(Boolean).join(', ') || null
  } else if (code === 'probe_exited_early') {
    const actual = formatNumber(context.actual_seconds, 's')
    const expected = formatNumber(context.expected_seconds, 's')
    detail = actual && expected ? `${actual} of ${expected}` : null
  } else if (
    code === 'connectivity_guard'
    || code === 'connectivity_timeout'
    || code === 'network_unreachable'
    || code === 'dns_resolution_failed'
    || code === 'dispatcharr_auth_failed'
    || code === 'endpoint_unhealthy'
    || code === 'connectivity_guard_error'
    || code === 'invalid_probe_endpoint'
  ) {
    detail = formatConnectivityDetail(context)
  } else if (
    code === 'timeout'
    || code === 'Timeout'
    || code === 'stream_timeout'
    || code === 'probe_timeout'
  ) {
    detail = formatAnalysisFailureDetail(context)
  } else if (code === 'error' || code === 'Error') {
    detail = formatAnalysisFailureDetail(context) || context.message || context.error || null
  }

  if (!detail) {
    detail = STATIC_REASON_DETAILS[code] || null
  }

  const text = detail ? `${label}: ${detail}` : label
  return {
    code,
    text,
    title: detail ? `${code}: ${detail}` : code,
  }
}
