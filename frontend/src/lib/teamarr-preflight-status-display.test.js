import { describe, expect, it } from 'vitest'
import {
  getTeamarrActiveChecksDetail,
  getTeamarrConcurrentCheckLimit,
} from './teamarr-preflight-status-display.js'

describe('teamarr preflight status display', () => {
  it('falls back to status config while edited config is still loading', () => {
    expect(getTeamarrConcurrentCheckLimit({
      editedConfig: null,
      status: { config: { max_concurrent_checks: 4 } },
      config: null,
    })).toBe(4)
  })

  it('uses a safe default when no config is available during a transient render', () => {
    expect(getTeamarrActiveChecksDetail({
      editedConfig: null,
      status: null,
      config: null,
    })).toBe('Limit 1')
  })

  it('describes queue-active checks instead of the configured limit', () => {
    expect(getTeamarrActiveChecksDetail({
      directActiveChecksCount: 1,
      queueActiveChecksCount: 2,
      editedConfig: { max_concurrent_checks: 5 },
    })).toBe('1 direct, 2 from queue')
  })
})
