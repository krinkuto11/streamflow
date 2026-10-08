import { describe, expect, it } from 'vitest'
import { getSetupCompleteFromReadiness } from './setup-bootstrap.js'
import { getInitializationStateFromStatus, isStartupGateActive } from './startup-gate-state.js'

describe('application setup from readiness', () => {
  it('accepts an operational application without a connection diagnostic', () => {
    expect(getSetupCompleteFromReadiness({ ready: true })).toBe(true)
    expect(getSetupCompleteFromReadiness({
      ready: true,
      checks: { dispatcharr_config: { configured: true, ready: true, reason: null } },
    })).toBe(true)
  })

  it('preserves startup gating when configured Dispatcharr is still initializing', () => {
    const data = {
      ready: false,
      checks: {
        dispatcharr_config: { configured: true, ready: true, reason: null },
        udi: { initialized: false, network_ready: false, ready: false, initialization_pending: true },
      },
      initialization: { status: 'in_progress', percentage: 20 },
    }
    expect(getSetupCompleteFromReadiness(data)).toBe(true)
    expect(isStartupGateActive({
      setupComplete: getSetupCompleteFromReadiness(data),
      initializationChecked: true,
      initialization: getInitializationStateFromStatus(data),
    })).toBe(true)
  })

  it('keeps database failures in the existing startup gate', () => {
    const data = {
      ready: false,
      checks: {
        dispatcharr_config: { configured: true, ready: true },
        database: { ready: false, reason: 'database_unavailable' },
      },
    }
    expect(getSetupCompleteFromReadiness(data)).toBe(true)
    expect(getInitializationStateFromStatus(data).inProgress).toBe(true)
  })

  it.each([false, true])('requires fresh setup despite global ready=%s when explicitly unconfigured', ready => {
    expect(getSetupCompleteFromReadiness({
      ready,
      checks: { dispatcharr_config: { configured: false, ready: false, reason: 'setup_required' } },
    })).toBe(false)
  })

  it.each([false, true])('rejects unreadable configuration despite global ready=%s', ready => {
    expect(() => getSetupCompleteFromReadiness({
      ready,
      checks: { dispatcharr_config: { configured: false, ready: false, reason: 'config_unavailable' } },
    })).toThrow('unavailable')
  })

  it.each([
    null,
    {},
    { ready: 'true' },
    { ready: false },
    { ready: true, checks: { dispatcharr_config: null } },
    { ready: true, checks: { dispatcharr_config: {} } },
    { ready: true, checks: { dispatcharr_config: { configured: true, ready: false } } },
    { ready: true, checks: { dispatcharr_config: { configured: false, ready: true } } },
  ])('rejects malformed or contradictory readiness: %j', data => {
    expect(() => getSetupCompleteFromReadiness(data)).toThrow()
  })
})
