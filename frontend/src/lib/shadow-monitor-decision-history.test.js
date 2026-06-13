import { describe, expect, it } from 'vitest'

import {
  filterShadowDecisionEvents,
  formatShadowEventReason,
  formatShadowEventType,
  formatShadowPreProbeStatus,
  getShadowEventDecisionGroup,
  getShadowEventDetailParts,
} from './shadow-monitor-decision-history.js'

describe('shadow monitor decision history helpers', () => {
  it('labels switch reasons and legacy event groups', () => {
    expect(formatShadowEventType({ type: 'no_decodable_frames_pending' })).toBe('Decoder Stall Pending')
    expect(formatShadowEventType({ type: 'loop_pending' })).toBe('Loop Pending')
    expect(formatShadowEventType({ type: 'loop_pre_probe_required' })).toBe('Loop Pre-Probe Required')
    expect(formatShadowEventReason('offline_image')).toBe('Offline Image')
    expect(formatShadowEventReason('loop')).toBe('Loop')
    expect(getShadowEventDecisionGroup({ type: 'dry_run_switch' })).toBe('switch')
    expect(getShadowEventDecisionGroup({ type: 'pre_probe_rejected' })).toBe('pre_probe')
    expect(getShadowEventDecisionGroup({ type: 'blank_pending' })).toBe('probe')
    expect(getShadowEventDecisionGroup({ type: 'loop_pre_probe_required' })).toBe('guard')
  })

  it('filters events by backend decision group', () => {
    const events = [
      { type: 'switch_success', decision_group: 'switch' },
      { type: 'pre_probe_rejected', decision_group: 'pre_probe' },
      { type: 'cooldown', decision_group: 'guard' },
    ]

    expect(filterShadowDecisionEvents(events, 'all')).toHaveLength(3)
    expect(filterShadowDecisionEvents(events, 'switch')).toEqual([events[0]])
    expect(filterShadowDecisionEvents(events, 'guard')).toEqual([events[2]])
  })

  it('summarizes switch context without raw provider data', () => {
    const parts = getShadowEventDetailParts({
      type: 'switch_success',
      trigger_reason: 'blank',
      details: {
        trigger_reason: 'blank',
        origin_stream_ref: 'stream-old',
        target_stream_ref: 'stream-new',
        detection: {
          reason: 'blank',
          confirmations: 2,
          required: 2,
          measurements: {
            blank_ratio: 1,
            blank_duration_secs: 8,
          },
          thresholds: {
            blank_ratio_threshold: 0.8,
            blank_min_duration_seconds: 2,
          },
        },
        pre_probe: {
          result: 'ok',
        },
        viewer_context: {
          real_client_count: 1,
        },
      },
    })

    expect(parts).toContain('Blank')
    expect(parts).toContain('blank 1/0.8, 8s/2s')
    expect(parts).toContain('2/2 confirmations')
    expect(parts).toContain('pre-probe ok')
    expect(parts).toContain('stream-old -> stream-new')
    expect(parts).toContain('1 real viewer')
    expect(parts.join(' ')).not.toMatch(/http|provider|account/i)
  })

  it('summarizes pre-probe rejection reasons', () => {
    expect(getShadowEventDetailParts({
      type: 'pre_probe_rejected',
      details: {
        reason: 'silent_audio',
        result: 'rejected',
        rejection_reason: 'provider_capacity',
        cooldown_seconds: 65,
        viewer_context: { real_client_count: 2 },
      },
    })).toEqual([
      'Silent Audio',
      'pre-probe rejected: Provider Slot',
      'cooldown 1m 5s',
      '2 real viewers',
    ])
  })

  it('summarizes loop detection context', () => {
    expect(getShadowEventDetailParts({
      type: 'loop_pending',
      details: {
        reason: 'loop',
        detection: {
          reason: 'loop',
          confirmations: 1,
          required: 2,
          measurements: {
            loop_duration_secs: 12.5,
            loop_frames_processed: 240,
          },
          thresholds: {
            loop_probe_duration_seconds: 180,
          },
        },
      },
    })).toEqual([
      'Loop',
      'loop 12.5s, probe 3m, 240 frames',
      '1/2 confirmations',
    ])
  })

  it('summarizes the loop pre-probe guard', () => {
    expect(getShadowEventDetailParts({
      type: 'loop_pre_probe_required',
      trigger_reason: 'loop',
      details: {
        trigger_reason: 'loop',
        operator_action: 'enable_next_stream_pre_probe',
        viewer_context: { real_client_count: 1 },
      },
    })).toEqual([
      'Loop',
      'next-stream pre-probe required',
      '1 real viewer',
    ])
  })

  it('formats last pre-probe status for the Shadow switch', () => {
    expect(formatShadowPreProbeStatus(null)).toBe('No pre-probe decisions')
    expect(formatShadowPreProbeStatus({
      metric: 'preprobe_skipped_provider_limit',
    })).toBe('Last pre-probe skipped: provider slot unavailable')
    expect(formatShadowPreProbeStatus({
      metric: 'preprobe_skipped_profile_limit',
    })).toBe('Last pre-probe skipped: profile slot unavailable')
    expect(formatShadowPreProbeStatus({
      metric: 'preprobe_rejected_media_fault',
      rejection_reason: 'blank',
    })).toBe('Last pre-probe rejected target: Blank')
    expect(formatShadowPreProbeStatus({
      metric: 'preprobe_success',
      elapsed_ms: 124,
    })).toBe('Last pre-probe passed in 124ms')
  })
})
