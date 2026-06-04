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
  const idText = slot.id != null ? `ID ${slot.id}` : null
  const activeViewers = Number(slot.active_viewers || 0)
  const checking = Number(slot.checking || 0)
  const used = Number(slot.used ?? (activeViewers + checking))
  const unlimited = Boolean(slot.unlimited)
  const limit = Number(slot.limit || 0)
  const available = slot.available == null ? null : Number(slot.available)

  const capacityText = unlimited ? 'open' : `${used}/${limit}`
  const availableValue = Number.isFinite(available) ? available : 0
  const freeText = unlimited
    ? 'unlimited capacity'
    : `${availableValue} free`
  const status = slot.full
    ? 'Full'
    : checking > 0
      ? 'Checking'
      : activeViewers > 0
        ? 'Viewer active'
        : 'Available'

  return {
    id: slot.id,
    name,
    text: `${name}: ${capacityText}`,
    title: [name, idText, `${activeViewers} viewer`, `${checking} checking`, freeText]
      .filter(Boolean)
      .join(', '),
    full: Boolean(slot.full),
    checking,
    activeViewers,
    used,
    limit,
    available: availableValue,
    unlimited,
    capacityText,
    freeText,
    status,
  }
}

export function getProfileSlotMatrixRows(providers = []) {
  return providers.flatMap((provider) => {
    const accountName = provider.name || 'Unknown'
    const accountId = provider.account_id ?? null
    return (provider.profile_slots || []).map((slot) => {
      const display = getProfileSlotDisplay(slot)
      return {
        ...display,
        accountName,
        accountId,
        key: `${accountId ?? accountName}:${display.id ?? display.name}`,
        limitText: display.unlimited ? 'unlimited' : String(display.limit),
        availableText: display.unlimited ? 'open' : String(display.available),
      }
    })
  })
}

export function getParallelProgressBadgeText(status = {}, providerSummary = {}) {
  if (!status?.parallel?.enabled) return null

  const maxWorkers = Number(status.parallel.max_workers || 0)
  if (maxWorkers > 0) {
    return `Parallel (${maxWorkers} workers)`
  }

  const activeWorkers = Number(providerSummary.checking_streams || 0)
  if (activeWorkers > 0) {
    return `Parallel (${activeWorkers} active)`
  }

  return 'Parallel'
}

export function getCheckerConcurrencyDisplay(streamCheckerStatus = {}) {
  const workers = Number(streamCheckerStatus?.parallel?.max_workers || 0)
  if (workers > 0) {
    return {
      text: `${workers} Workers`,
      active: true,
    }
  }

  return {
    text: 'Sequential',
    active: false,
  }
}
