import { describe, expect, it } from 'vitest'

import { getProviderWaitReasonDisplay } from './provider-progress-display'

describe('getProviderWaitReasonDisplay', () => {
  it('uses concise operator wording for checker-owned capacity waits', () => {
    expect(getProviderWaitReasonDisplay({
      dominant_wait_reason: 'checking_capacity',
      wait_reason_counts: { checking_capacity: 3 },
    })).toEqual({
      code: 'checking_capacity',
      text: 'Checker slots (3)',
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
})
