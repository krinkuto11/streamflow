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
    })).toEqual({
      code: 'provider_capacity_unavailable',
      text: 'Provider capacity unavailable: No provider or profile check slot is free',
      title: 'provider_capacity_unavailable: No provider or profile check slot is free',
    })
  })

  it('falls back to viewer-preempted status wording when no detail exists', () => {
    expect(getQualityReasonDisplay({
      status: 'viewer_preempted',
    })).toEqual({
      code: 'viewer_preempted',
      text: 'Viewer needed this profile: Real playback kept the profile slot; check again later',
      title: 'viewer_preempted: Real playback kept the profile slot; check again later',
    })
  })

  it('falls back to provider wait timeout status wording when no detail exists', () => {
    expect(getQualityReasonDisplay({
      status: 'provider_limit_wait_timeout',
    })).toEqual({
      code: 'provider_wait_timeout',
      text: 'Provider wait timed out: No provider or profile slot became free in time',
      title: 'provider_wait_timeout: No provider or profile slot became free in time',
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

  it('formats connectivity probe context without exposing raw URLs', () => {
    expect(getQualityReasonDisplay({
      quality_reason_detail: 'endpoint_unhealthy',
      quality_reason_context: {
        dispatcharr_api: {
          label: 'dispatcharr_api',
          host: 'dispatcharr.local',
          status_code: 503,
          attempts: 3,
          max_attempts: 3,
          timeout_seconds: 1,
        },
      },
    })).toEqual({
      code: 'endpoint_unhealthy',
      text: 'Connectivity endpoint unhealthy: Dispatcharr API dispatcharr.local, HTTP 503, attempt 3/3, timeout 1s',
      title: 'endpoint_unhealthy: Dispatcharr API dispatcharr.local, HTTP 503, attempt 3/3, timeout 1s',
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

  it('formats stream timeout breakdown and retry context', () => {
    expect(getQualityReasonDisplay({
      reason_detail: 'stream_timeout',
      quality_reason_context: {
        elapsed_seconds: 65,
        timeout_seconds: 65,
        operation_timeout_seconds: 30,
        ffmpeg_duration_seconds: 30,
        startup_buffer_seconds: 5,
        attempt: 2,
        max_attempts: 2,
      },
    })).toMatchObject({
      code: 'stream_timeout',
      text: 'Stream analysis timed out: 65s of 65s, base 30s + window 30s + startup 5s, attempt 2/2',
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
