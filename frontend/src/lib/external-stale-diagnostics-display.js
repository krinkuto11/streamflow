const CHECK_LABELS = {
  celery: 'Celery',
  redis: 'Redis',
  postgres: 'Postgres',
}

const CONFLICT_LABELS = {
  active_status_with_completed_message: 'last message already says completed',
  active_status_with_error_message: 'last message contains an error',
  success_status_with_error_message: 'success status with an error message',
  error_status_with_completed_message: 'error status with a completed message',
}

const titleizeStatus = (value) => {
  const text = String(value || 'unknown').replace(/[_-]+/g, ' ').trim()
  return text ? text.replace(/\b\w/g, (char) => char.toUpperCase()) : 'Unknown'
}

const formatAccount = (account) => {
  const name = account?.account_name || (account?.account_id != null ? `Account ${account.account_id}` : 'Account')
  const status = titleizeStatus(account?.status)
  const conflict = CONFLICT_LABELS[account?.conflict] || titleizeStatus(account?.conflict)
  return `${name}: ${status} (${conflict})`
}

export function getExternalStaleDiagnosticsDisplay(diagnostics = {}) {
  const staleRisk = diagnostics?.status === 'stale_risk' || diagnostics?.stale_status_suspected === true
  if (!staleRisk) {
    return null
  }

  const m3uAccounts = diagnostics?.m3u_accounts || {}
  const staleSuspected = Array.isArray(m3uAccounts.stale_suspected)
    ? m3uAccounts.stale_suspected
    : []
  const suspectCount = Number.isFinite(Number(m3uAccounts.stale_suspected_count))
    ? Number(m3uAccounts.stale_suspected_count)
    : staleSuspected.length
  const conflictLabel = suspectCount === 1 ? 'provider status note' : 'provider status notes'

  const externalChecks = diagnostics?.external_checks || {}
  const checkStatuses = Object.entries(CHECK_LABELS).map(([key, label]) => ({
    key,
    label,
    status: titleizeStatus(externalChecks?.[key]?.status),
  }))
  const unknownChecks = checkStatuses
    .filter((check) => check.status.toLowerCase() === 'unknown')
    .map((check) => check.label)

  return {
    title: 'Dispatcharr sync is still settling',
    text: 'Checks continue normally while Dispatcharr refreshes its provider status after the latest completion message.',
    detail: unknownChecks.length > 0
      ? `${suspectCount} ${conflictLabel}. Observed only; no automatic StreamFlow action is needed. ${unknownChecks.join(', ')} internals are not visible from here.`
      : `${suspectCount} ${conflictLabel}.`,
    accounts: staleSuspected.slice(0, 3).map(formatAccount),
    checks: checkStatuses.map((check) => `${check.label}: ${check.status}`),
  }
}
