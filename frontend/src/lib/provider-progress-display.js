const WAIT_REASON_LABELS = {
  checking_capacity: 'Check slots full',
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

export function getProfileSlotDisplay(slot = {}) {
  const name = slot.name || (slot.id != null ? `Profile ${slot.id}` : 'Profile')
  const activeViewers = Number(slot.active_viewers || 0)
  const checking = Number(slot.checking || 0)
  const used = Number(slot.used ?? (activeViewers + checking))
  const unlimited = Boolean(slot.unlimited)
  const limit = Number(slot.limit || 0)
  const available = slot.available == null ? null : Number(slot.available)

  const capacityText = unlimited ? 'open' : `${used}/${limit}`
  const freeText = unlimited
    ? 'unlimited capacity'
    : `${Number.isFinite(available) ? available : 0} free`

  return {
    id: slot.id,
    name,
    text: `${name}: ${capacityText}`,
    title: `${name}: ${activeViewers} viewer, ${checking} checking, ${freeText}`,
    full: Boolean(slot.full),
    checking,
    activeViewers,
    used,
    unlimited,
  }
}
