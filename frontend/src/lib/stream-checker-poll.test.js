import { describe, expect, it, vi } from 'vitest'
import { loadStreamCheckerPoll } from './stream-checker-poll'

describe('loadStreamCheckerPoll', () => {
  it('reuses progress in status instead of making a second active-run request', async () => {
    const progress = { channel_id: 42, streams_detail: [{ stream_id: 101, status: 'checking' }] }
    const api = {
      getStatus: vi.fn().mockResolvedValue({ data: { checking: true, progress } }),
      getProgress: vi.fn(),
      getConfig: vi.fn(),
      getHardwareStatus: vi.fn(),
    }

    const result = await loadStreamCheckerPoll(api, false)

    expect(api.getStatus).toHaveBeenCalledTimes(1)
    expect(api.getProgress).not.toHaveBeenCalled()
    expect(result.progressResult.value.data).toBe(progress)
    expect(result.configResult).toBeUndefined()
    expect(result.hardwareResult).toBeUndefined()
  })

  it('retains progress when a status response lacks the progress field', async () => {
    const progress = { channel_id: 42, percentage: 75 }
    const api = {
      getStatus: vi.fn().mockResolvedValue({ data: { checking: true } }),
      getProgress: vi.fn().mockResolvedValue({ data: progress }),
      getConfig: vi.fn().mockResolvedValue({ data: { enabled: true } }),
      getHardwareStatus: vi.fn().mockResolvedValue({ data: { available: true } }),
    }

    const result = await loadStreamCheckerPoll(api)

    expect(api.getProgress).toHaveBeenCalledTimes(1)
    expect(result.progressResult.value.data).toBe(progress)
    expect(result.configResult.value.data.enabled).toBe(true)
    expect(result.hardwareResult.value.data.available).toBe(true)
  })
})
