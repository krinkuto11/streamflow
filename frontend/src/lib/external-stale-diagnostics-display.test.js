import { describe, expect, it } from 'vitest'

import { getExternalStaleDiagnosticsDisplay } from './external-stale-diagnostics-display'

describe('getExternalStaleDiagnosticsDisplay', () => {
  it('returns null when no stale risk is reported', () => {
    expect(getExternalStaleDiagnosticsDisplay({ status: 'ok' })).toBeNull()
    expect(getExternalStaleDiagnosticsDisplay({ stale_status_suspected: false })).toBeNull()
  })

  it('formats read-only Dispatcharr status risk details without raw messages', () => {
    const display = getExternalStaleDiagnosticsDisplay({
      status: 'stale_risk',
      stale_status_suspected: true,
      operator_note: 'Dispatcharr provider status may be stale.',
      m3u_accounts: {
        stale_suspected_count: 1,
        stale_suspected: [
          {
            account_id: 5,
            account_name: 'Provider A',
            status: 'fetching',
            conflict: 'active_status_with_completed_message',
          },
        ],
      },
      external_checks: {
        celery: { status: 'unknown' },
        redis: { status: 'unknown' },
        postgres: { status: 'unknown' },
      },
    })

    expect(display).toEqual({
      title: 'Dispatcharr Status Risk',
      text: 'Provider status may be stale. No automatic repair.',
      detail: '1 provider status conflict. Celery, Redis, Postgres evidence is not available from StreamFlow.',
      accounts: ['Provider A: Fetching (active status with completed message)'],
      checks: ['Celery: Unknown', 'Redis: Unknown', 'Postgres: Unknown'],
    })
  })

  it('uses safe fallbacks and limits account rows', () => {
    const display = getExternalStaleDiagnosticsDisplay({
      stale_status_suspected: true,
      m3u_accounts: {
        stale_suspected_count: 4,
        stale_suspected: [
          { account_id: 1, status: 'processing', conflict: 'active_status_with_error_message' },
          { account_name: 'Two', status: 'success', conflict: 'success_status_with_error_message' },
          { account_name: 'Three', status: 'error', conflict: 'error_status_with_completed_message' },
          { account_name: 'Four', status: 'fetching', conflict: 'active_status_with_completed_message' },
        ],
      },
      external_checks: {
        celery: { status: 'unknown' },
      },
    })

    expect(display.detail).toBe('4 provider status conflicts. Celery, Redis, Postgres evidence is not available from StreamFlow.')
    expect(display.accounts).toHaveLength(3)
    expect(display.accounts[0]).toBe('Account 1: Processing (active status with error message)')
  })
})
