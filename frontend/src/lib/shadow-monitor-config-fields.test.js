import { describe, expect, it } from 'vitest'

import {
  shadowMonitorNumberFields,
  shadowMonitorThresholdFields,
} from './shadow-monitor-config-fields.js'

describe('shadowMonitorNumberFields', () => {
  it('makes per-channel switch throttling explicit', () => {
    const cooldown = shadowMonitorNumberFields.find(field => field.key === 'channel_cooldown_seconds')
    const switchLimit = shadowMonitorNumberFields.find(field => field.key === 'max_switches_per_hour')

    expect(cooldown).toMatchObject({
      label: 'Channel Cooldown',
      suffix: 'sec',
    })
    expect(cooldown.help).toMatch(/same channel/i)

    expect(switchLimit).toMatchObject({
      label: 'Channel Switch Limit',
      suffix: '/ hour',
      min: 1,
      max: 20,
    })
    expect(switchLimit.help).toMatch(/one channel/i)
    expect(switchLimit.help).toMatch(/rolling hour/i)
  })

  it('keeps all numeric and threshold config keys available to the page', () => {
    expect(shadowMonitorNumberFields.map(field => field.key)).toEqual([
      'poll_interval_seconds',
      'watch_gap_seconds',
      'probe_duration_seconds',
      'confirmation_count',
      'channel_cooldown_seconds',
      'max_switches_per_hour',
      'max_concurrent_watchers',
    ])

    expect(shadowMonitorThresholdFields.map(field => field.key)).toEqual([
      'blank_min_duration_seconds',
      'blank_pixel_threshold',
      'blank_ratio_threshold',
      'freeze_min_duration_seconds',
      'freeze_noise_threshold',
      'freeze_ratio_threshold',
      'no_decodable_frames_min_duration_seconds',
    ])
  })
})
