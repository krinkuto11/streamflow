import { describe, expect, it } from 'vitest'

import {
  getInitializationStateFromStatus,
  getInitializationStateFromStatusError,
  isStartupGateActive,
  shouldRedirectForStartupGate,
} from './startup-gate-state.js'

describe('startup gate state', () => {
  it('keeps the startup gate active until the readiness endpoint is ready', () => {
    expect(getInitializationStateFromStatus({
      status: 'not_ready',
      ready: false,
      checks: { database: { ready: true }, udi: { ready: false } },
      initialization: {
        status: 'in_progress',
        percentage: 60,
        message: 'Fetching data',
      },
    })).toMatchObject({
      inProgress: true,
      status: 'not_ready',
      percentage: 60,
      message: 'Fetching data',
      checks: { database: { ready: true }, udi: { ready: false } },
    })

    expect(getInitializationStateFromStatus({
      status: 'ready',
      ready: true,
      initialization: { status: 'completed', percentage: 100 },
    })).toMatchObject({
      inProgress: false,
      status: 'ready',
      percentage: 100,
    })
  })

  it('opens the dashboard when initialization is complete but a runtime service is stopped', () => {
    expect(getInitializationStateFromStatus({
      status: 'not_ready',
      ready: false,
      checks: {
        database: { ready: true },
        dispatcharr_config: { ready: true },
        udi: { ready: true, initialization_pending: false },
        services: {
          ready: false,
          items: {
            automation: { required: true, ready: false, state: 'stopped' },
          },
        },
      },
      initialization: {
        status: 'completed',
        percentage: 100,
        message: 'Initialization complete',
        last_refresh_time: '2026-07-12T22:03:20Z',
      },
    })).toMatchObject({
      inProgress: false,
      status: 'not_ready',
      percentage: 100,
      message: 'Initialization complete',
    })
  })

  it('keeps blocking for core startup checks even if a stale completed cache is reported', () => {
    expect(getInitializationStateFromStatus({
      status: 'not_ready',
      ready: false,
      checks: {
        database: { ready: false },
        dispatcharr_config: { ready: true },
        udi: { ready: true },
      },
      initialization: {
        status: 'completed',
        percentage: 100,
        last_refresh_time: '2026-07-12T22:03:20Z',
      },
    }).inProgress).toBe(true)
  })

  it('does not treat an in-progress status as startup when a usable cache exists', () => {
    expect(getInitializationStateFromStatus({
      status: 'in_progress',
      last_refresh_time: '2026-06-04T09:05:46Z',
      percentage: 40,
    })).toMatchObject({
      inProgress: false,
      status: 'in_progress',
      percentage: 40,
    })
  })

  it('preserves the ready state when a later status poll fails', () => {
    const previous = {
      inProgress: false,
      status: 'complete',
      percentage: 100,
      message: 'Ready',
    }

    expect(getInitializationStateFromStatusError(previous)).toEqual(previous)
  })

  it('uses the startup gate only before the first ready status or during real initialization', () => {
    expect(isStartupGateActive({
      setupComplete: true,
      initializationChecked: false,
      initialization: null,
    })).toBe(true)

    expect(isStartupGateActive({
      setupComplete: true,
      initializationChecked: true,
      initialization: { inProgress: false },
    })).toBe(false)
  })

  it('does not redirect deep links until startup status is confirmed active', () => {
    expect(shouldRedirectForStartupGate({
      setupComplete: true,
      initializationChecked: false,
      initialization: null,
      pathname: '/settings',
    })).toBe(false)

    expect(shouldRedirectForStartupGate({
      setupComplete: true,
      initializationChecked: true,
      initialization: { inProgress: false },
      pathname: '/settings',
    })).toBe(false)

    expect(shouldRedirectForStartupGate({
      setupComplete: true,
      initializationChecked: true,
      initialization: { inProgress: true },
      pathname: '/settings',
    })).toBe(true)

    expect(shouldRedirectForStartupGate({
      setupComplete: true,
      initializationChecked: true,
      initialization: { inProgress: true },
      pathname: '/dashboard',
    })).toBe(false)
  })
})
