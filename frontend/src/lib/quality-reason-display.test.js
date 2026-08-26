import { describe, expect, it } from 'vitest'

import {
  getIncompleteBitrateBadgeLabel,
  getQualityReasonDisplay,
  getVisualProbeLabel,
} from './quality-reason-display'

describe('getVisualProbeLabel', () => {
  it.each([
    ['exit_146', 'Incomplete: Connection timed out (FFmpeg 146)'],
    ['exit_155', 'Incomplete: Network unreachable (FFmpeg 155)'],
    ['exit_183', 'Incomplete: Invalid media data (FFmpeg 183)'],
    ['exit_234', 'Incomplete: Invalid input or argument (FFmpeg 234)'],
    ['exit_251', 'Incomplete: Input/output error (FFmpeg 251)'],
    ['exit_8', 'Incomplete: FFmpeg could not open or process the input (code 8)'],
    ['timeout', 'Incomplete: Probe timed out'],
    ['preempted', 'Incomplete: Viewer needed probe capacity'],
  ])('explains visual-probe reason %s while retaining useful raw evidence', (reason, label) => {
    expect(getVisualProbeLabel({
      visual_probe_incomplete: true,
      visual_probe_incomplete_reason: reason,
    })).toBe(label)
  })

  it('keeps an unknown FFmpeg exit code visible without inventing a meaning', () => {
    expect(getVisualProbeLabel({
      visual_probe_incomplete: true,
      visual_probe_incomplete_reason: 'exit_77',
    })).toBe('Incomplete: FFmpeg failed (code 77)')
  })

  it('preserves completed and pending probe labels', () => {
    expect(getVisualProbeLabel({
      visual_probe_completed: true,
      visual_probe_duration_seconds: 10,
      visual_probe_duration_adjusted: true,
    })).toBe('10s adjusted')
    expect(getVisualProbeLabel({})).toBe('Pending')
  })
})

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

  it('explains incomplete bitrate rows from status fallback', () => {
    expect(getQualityReasonDisplay({
      status: 'incomplete_bitrate',
    })).toEqual({
      code: 'missing_bitrate',
      text: 'Needs bitrate recheck',
      title: 'missing_bitrate',
    })
  })

  it('explains incomplete bitrate rows even when the backend sends the reason detail explicitly', () => {
    expect(getQualityReasonDisplay({
      status: 'incomplete_bitrate',
      quality_reason_detail: 'missing_bitrate',
    })).toEqual({
      code: 'missing_bitrate',
      text: 'Needs bitrate recheck',
      title: 'missing_bitrate',
    })
  })

  it('distinguishes bitrate that remains unavailable after the serial recheck', () => {
    expect(getQualityReasonDisplay({
      status: 'incomplete_bitrate',
      quality_reason_detail: 'missing_bitrate_after_recheck',
    })).toEqual({
      code: 'missing_bitrate_after_recheck',
      text: 'Bitrate unavailable after recheck',
      title: 'missing_bitrate_after_recheck',
    })
    expect(getIncompleteBitrateBadgeLabel({
      status: 'incomplete_bitrate',
      quality_reason_detail: 'missing_bitrate_after_recheck',
      bitrate_recheck_outcome: 'unavailable',
    })).toBe('Unavailable after recheck')
  })

  it('surfaces a capacity-deferred recheck instead of claiming it ran', () => {
    const stream = {
      status: 'incomplete_bitrate',
      quality_reason_detail: 'missing_bitrate',
      measurement_incomplete_context: {
        bitrate_recheck_outcome: 'provider_capacity_unavailable',
      },
    }
    expect(getQualityReasonDisplay(stream)).toEqual({
      code: 'provider_capacity_unavailable',
      text: 'Provider capacity unavailable: No provider or profile check slot is free',
      title: 'provider_capacity_unavailable: No provider or profile check slot is free',
    })
    expect(getIncompleteBitrateBadgeLabel(stream)).toBe('Capacity deferred')
  })

  it('does not hide a stronger visual failure behind deferred recheck capacity', () => {
    expect(getQualityReasonDisplay({
      status: 'blank',
      quality_reason_detail: 'blank_detected',
      quality_reason_context: { blank_ratio: 0.98 },
      bitrate_recheck_outcome: 'provider_capacity_unavailable',
    })).toEqual({
      code: 'blank_detected',
      text: 'Blank video detected: ratio 1.0',
      title: 'blank_detected: ratio 1.0',
    })
    expect(getQualityReasonDisplay({
      status: 'incomplete_bitrate',
      quality_reason_detail: 'freeze_detected',
      bitrate_recheck_outcome: 'provider_capacity_unavailable',
    })).toMatchObject({
      code: 'freeze_detected',
      text: 'Frozen video detected',
    })
  })

  it('records a successful bitrate recovery as visible completed-run evidence', () => {
    expect(getQualityReasonDisplay({
      status: 'completed',
      quality_reason_detail: 'none',
      bitrate_recheck_outcome: 'recovered',
    })).toEqual({
      code: 'bitrate_recheck_recovered',
      text: 'Bitrate recovered after recheck',
      title: 'bitrate_recheck_recovered',
    })
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

  it('uses explicit checker-capacity wording for account/profile slot waits', () => {
    expect(getQualityReasonDisplay({
      reason_detail: 'checking_capacity',
    })).toEqual({
      code: 'checking_capacity',
      text: 'Check slots full: The checker has no free slot for this account or profile yet',
      title: 'checking_capacity: The checker has no free slot for this account or profile yet',
    })
  })

  it('hides stale wait reasons while a stream is actively checking', () => {
    expect(getQualityReasonDisplay({
      status: 'checking',
      reason_detail: 'checking_capacity',
    })).toBeNull()
    expect(getQualityReasonDisplay({
      status: 'probing',
      quality_reason_detail: 'global_worker_limit',
    })).toBeNull()
    expect(getQualityReasonDisplay({
      status: 'rechecking_bitrate',
      quality_reason_detail: 'missing_bitrate',
    })).toBeNull()
  })

  it('uses explicit global-worker wording for queue capacity waits', () => {
    expect(getQualityReasonDisplay({
      reason_detail: 'global_worker_limit',
    })).toEqual({
      code: 'global_worker_limit',
      text: 'Global workers full: The global Stream Checker worker limit is full',
      title: 'global_worker_limit: The global Stream Checker worker limit is full',
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
