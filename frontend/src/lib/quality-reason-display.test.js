import { describe, expect, it } from 'vitest'

import { getQualityReasonDisplay } from './quality-reason-display'

describe('getQualityReasonDisplay', () => {
  it('formats threshold comparisons with machine-readable code retained', () => {
    expect(getQualityReasonDisplay({
      quality_reason_detail: 'bitrate_below_threshold',
      quality_reason_context: { actual: 742.4, threshold: 1500 },
    })).toEqual({
      code: 'bitrate_below_threshold',
      text: 'Bitrate below threshold: 742.4 kbps < 1500 kbps',
      title: 'bitrate_below_threshold: 742.4 kbps < 1500 kbps',
    })
  })

  it('returns null for clean streams', () => {
    expect(getQualityReasonDisplay({ quality_reason_detail: 'none' })).toBeNull()
  })

  it('uses provider-capacity wording without leaking raw detail as primary text', () => {
    expect(getQualityReasonDisplay({
      reason_detail: 'provider_capacity_unavailable',
    })).toMatchObject({
      code: 'provider_capacity_unavailable',
      text: 'Provider capacity unavailable',
    })
  })

  it('falls back to viewer-preempted status wording when no detail exists', () => {
    expect(getQualityReasonDisplay({
      status: 'viewer_preempted',
    })).toMatchObject({
      code: 'viewer_preempted',
      text: 'Viewer needed this profile',
    })
  })

  it('falls back to provider wait timeout status wording when no detail exists', () => {
    expect(getQualityReasonDisplay({
      status: 'provider_limit_wait_timeout',
    })).toMatchObject({
      code: 'provider_wait_timeout',
      text: 'Provider wait timed out',
    })
  })
})
