import { describe, expect, it } from 'vitest'

import {
  buildShadowMonitorConfigPayload,
  isShadowConfigRevisionConflict,
  parseShadowMonitorCsv,
} from './shadow-monitor-config-payload.js'

describe('shadow monitor config payload helpers', () => {
  it('builds an include/exclude scope PUT with the loaded CAS revision', () => {
    expect(buildShadowMonitorConfigPayload({
      sourceConfig: {
        config_revision: 17,
        enabled: true,
        watcher_api_key: '',
      },
      configRevision: 17,
      includedIds: ' 10, 20, invalid ',
      includedUuids: ' uuid-a, uuid-b ',
      excludedIds: '30',
      excludedUuids: 'uuid-z',
      offlineImageHashes: '0123456789abcdef, fedcba9876543210',
      extra: { dry_run: false },
    })).toEqual({
      config_revision: 17,
      enabled: true,
      watcher_api_key: '',
      expected_config_revision: 17,
      included_channel_ids: [10, 20],
      included_channel_uuids: ['uuid-a', 'uuid-b'],
      excluded_channel_ids: [30],
      excluded_channel_uuids: ['uuid-z'],
      offline_image_reference_hashes: ['0123456789abcdef', 'fedcba9876543210'],
      dry_run: false,
    })
  })

  it('keeps empty include fields as the explicit all-channel scope', () => {
    const payload = buildShadowMonitorConfigPayload({
      sourceConfig: { enabled: false },
      configRevision: 3,
    })

    expect(payload.expected_config_revision).toBe(3)
    expect(payload.included_channel_ids).toEqual([])
    expect(payload.included_channel_uuids).toEqual([])
  })

  it('does not let extra fields override the loaded CAS revision or explicit scope', () => {
    const payload = buildShadowMonitorConfigPayload({
      configRevision: 9,
      includedIds: '10',
      extra: {
        expected_config_revision: 8,
        included_channel_ids: [99],
      },
    })

    expect(payload.expected_config_revision).toBe(9)
    expect(payload.included_channel_ids).toEqual([10])
  })

  it('recognizes only the exact stale-revision response as a reload conflict', () => {
    expect(isShadowConfigRevisionConflict({
      response: {
        status: 409,
        data: { code: 'shadow_config_revision_conflict' },
      },
    })).toBe(true)
    expect(isShadowConfigRevisionConflict({
      response: { status: 409, data: { code: 'another_conflict' } },
    })).toBe(false)
    expect(isShadowConfigRevisionConflict({
      response: { status: 400, data: { code: 'shadow_config_revision_conflict' } },
    })).toBe(false)
  })

  it('drops non-finite numeric CSV values', () => {
    expect(parseShadowMonitorCsv('1, Infinity, NaN, 2', true)).toEqual([1, 2])
  })
})
