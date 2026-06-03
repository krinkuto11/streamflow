import { describe, expect, it } from 'vitest'

import { getProfileSlotDisplay, getProviderWaitReasonDisplay } from './provider-progress-display'

describe('getProviderWaitReasonDisplay', () => {
  it('uses concise operator wording for checker-owned capacity waits', () => {
    expect(getProviderWaitReasonDisplay({
      dominant_wait_reason: 'checking_capacity',
      wait_reason_counts: { checking_capacity: 3 },
    })).toEqual({
      code: 'checking_capacity',
      text: 'Check slots full (3)',
      title: 'checking_capacity: 3',
    })
  })

  it('distinguishes viewer-owned capacity from provider capacity', () => {
    expect(getProviderWaitReasonDisplay({
      dominant_wait_reason: 'active_viewers',
      wait_reason_counts: { active_viewers: 1 },
    })).toMatchObject({
      code: 'active_viewers',
      text: 'Viewer slots',
    })
  })

  it('falls back to titleized unknown reason codes', () => {
    expect(getProviderWaitReasonDisplay({
      dominant_wait_reason: 'custom_reason',
      wait_reason_counts: { custom_reason: 2 },
    })).toMatchObject({
      code: 'custom_reason',
      text: 'Custom Reason (2)',
    })
  })

  it('returns null without a dominant wait reason', () => {
    expect(getProviderWaitReasonDisplay({})).toBeNull()
  })

  it('formats bounded profile slot usage', () => {
    expect(getProfileSlotDisplay({
      id: 50,
      name: 'Sibling',
      active_viewers: 1,
      checking: 1,
      used: 2,
      limit: 2,
      available: 0,
      full: true,
    })).toMatchObject({
      id: 50,
      text: 'Sibling: 2/2',
      title: 'Sibling: 1 viewer, 1 checking, 0 free',
      full: true,
    })
  })

  it('formats unlimited profile slots as open', () => {
    expect(getProfileSlotDisplay({
      name: 'Default',
      unlimited: true,
      checking: 2,
      active_viewers: 0,
    })).toMatchObject({
      text: 'Default: open',
      title: 'Default: 0 viewer, 2 checking, unlimited capacity',
      unlimited: true,
    })
  })
})
