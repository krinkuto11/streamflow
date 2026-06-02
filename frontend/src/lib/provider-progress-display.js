const WAIT_REASON_LABELS = {
  checking_capacity: 'Checker slots',
  global_worker_limit: 'Global workers',
  provider_capacity: 'Provider capacity',
  provider_capacity_unavailable: 'Provider capacity',
  max_streams_reached: 'Provider limit',
  active_viewers: 'Viewer slots',
  quota_consumed_by_active_viewers: 'Viewer slots',
  viewer_preempted: 'Viewer needed slot',
}

const titleizeCode = (code) => {
  if (!code) return ''
  return String(code)
    .replace(/[_-]+/g, ' ')
    .replace(/\b\w/g, (char) => char.toUpperCase())
}

export function getProviderWaitReasonDisplay(provider = {}) {
  const code = provider.dominant_wait_reason
  if (!code) return null

  const count = provider.wait_reason_counts?.[code]
  const label = WAIT_REASON_LABELS[code] || titleizeCode(code)
  const suffix = Number.isFinite(Number(count)) && Number(count) > 1 ? ` (${count})` : ''

  return {
    code,
    text: `${label}${suffix}`,
    title: count ? `${code}: ${count}` : code,
  }
}
