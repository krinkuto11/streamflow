import { describe, expect, it } from 'vitest'

import {
  getInitializationStateFromStatus,
  getInitializationStateFromStatusError,
  isStartupGateActive,
  shouldRedirectForStartupGate,
} from './startup-gate-state.js'

describe('startup gate state', () => {
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
