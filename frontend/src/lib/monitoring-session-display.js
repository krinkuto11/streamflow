const count = (value) => Number.isFinite(value) && value >= 0 ? value : 0

// List responses contain category counts, not bitrate or per-source errors.
// Do not infer an all-clear from an empty session or invent quality measurements.
export function getSessionHealth(session) {
  const stable = count(session.stable_count)
  const review = count(session.review_count)
  const quarantined = count(session.quarantined_count)
  if (quarantined > 0) return { label: 'Quarantined sources', tone: 'warning', stable, review, quarantined }
  if (review > 0) return { label: 'Sources under review', tone: 'review', stable, review, quarantined }
  if (stable > 0) return { label: 'Stable sources', tone: 'stable', stable, review, quarantined }
  return { label: 'No source results yet', tone: 'unknown', stable, review, quarantined }
}

export function sortMonitoringSessions(sessions) {
  return [...sessions].sort((a, b) =>
    Number(Boolean(b.is_active)) - Number(Boolean(a.is_active)) ||
    Number(count(b.quarantined_count) > 0) - Number(count(a.quarantined_count) > 0) ||
    (b.created_at || 0) - (a.created_at || 0)
  )
}

export function toggleVisibleSessionSelection(selected, sessions) {
  const next = new Set(selected)
  const allSelected = sessions.length > 0 && sessions.every(session => next.has(session.session_id))
  sessions.forEach(session => {
    if (allSelected) next.delete(session.session_id)
    else next.add(session.session_id)
  })
  return next
}
