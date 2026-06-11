import { describe, expect, it } from 'vitest'

import {
  filterShadowDecisionEvents,
  formatShadowEventReason,
  formatShadowEventType,
  getShadowEventDecisionGroup,
  getShadowEventDetailParts,
} from './shadow-monitor-decision-history.js'

describe('shadow monitor decision history helpers', () => {
  it('labels switch reasons and legacy event groups', () => {
    expect(formatShadowEventType({ type: 'no_decodable_frames_pending' })).toBe('Decoder Stall Pending')
    expect(formatShadowEventReason('offline_image')).toBe('Offline Image')
    expect(getShadowEventDecisionGroup({ type: 'dry_run_switch' })).toBe('switch')
    expect(getShadowEventDecisionGroup({ type: 'pre_probe_rejected' })).toBe('pre_probe')
    expect(getShadowEventDecisionGroup({ type: 'blank_pending' })).toBe('probe')
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
      'pre-probe rejected: Provider Capacity',
      'cooldown 1m 5s',
      '2 real viewers',
    ])
  })
})
