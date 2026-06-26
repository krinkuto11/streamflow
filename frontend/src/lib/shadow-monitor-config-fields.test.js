import { describe, expect, it } from 'vitest'

import {
  shadowMonitorNumberFields,
  shadowMonitorThresholdFields,
} from './shadow-monitor-config-fields.js'

describe('shadowMonitorNumberFields', () => {
  it('keeps only viewer release timing visible in the normal UI', () => {
    const viewerGrace = shadowMonitorNumberFields.find(field => field.key === 'viewer_left_grace_seconds')
    const cooldown = shadowMonitorNumberFields.find(field => field.key === 'channel_cooldown_seconds')
    const switchLimit = shadowMonitorNumberFields.find(field => field.key === 'max_switches_per_hour')

    expect(viewerGrace).toMatchObject({
      label: 'Viewer Grace',
      suffix: 'sec',
      min: 0,
      max: 10,
    })
    expect(viewerGrace.help).toMatch(/real viewer disappears/i)
    expect(viewerGrace.help).toMatch(/frequent channel switching/i)
    expect(viewerGrace.help).toMatch(/provider\/profile limits/i)
    expect(cooldown).toBeUndefined()
    expect(switchLimit).toBeUndefined()
  })

  it('hides low-level probe and threshold tuning from the normal page', () => {
    expect(shadowMonitorNumberFields.map(field => field.key)).toEqual([
      'viewer_left_grace_seconds',
    ])

    expect(shadowMonitorThresholdFields).toEqual([])
  })
})
