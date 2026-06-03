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

  it('explains connectivity guard aborts with the backend message when present', () => {
    expect(getQualityReasonDisplay({
      quality_reason_detail: 'connectivity_timeout',
      quality_reason_context: {
        message: 'Dispatcharr API connectivity probe timed out',
      },
    })).toEqual({
      code: 'connectivity_timeout',
      text: 'Connectivity probe timed out: Dispatcharr API connectivity probe timed out',
      title: 'connectivity_timeout: Dispatcharr API connectivity probe timed out',
    })
  })

  it('formats stream timeout context without exposing a raw code first', () => {
    expect(getQualityReasonDisplay({
      reason_detail: 'stream_timeout',
      quality_reason_context: {
        elapsed_seconds: 65,
        timeout_seconds: 65,
      },
    })).toMatchObject({
      code: 'stream_timeout',
      text: 'Stream analysis timed out: 65s of 65s',
    })
  })

  it('falls back to readable generic error wording', () => {
    expect(getQualityReasonDisplay({
      status: 'error',
    })).toMatchObject({
      code: 'error',
      text: 'Stream analysis failed',
    })
  })
})
