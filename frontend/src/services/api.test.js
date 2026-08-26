import { afterEach, describe, expect, it, vi } from 'vitest'

import { api, streamCheckerAPI } from './api'

describe('streamCheckerAPI direct checks', () => {
  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('keeps the backend reservation request unbounded while forwarding cancellation', async () => {
    const controller = new AbortController()
    const post = vi.spyOn(api, 'post').mockResolvedValue({ data: { success: true } })

    await streamCheckerAPI.checkStream(
      42,
      { blank_check_enabled: true },
      { signal: controller.signal, timeout: 1 },
    )

    expect(post).toHaveBeenCalledWith(
      '/stream-checker/check-stream',
      { stream_id: 42, blank_check_enabled: true },
      { signal: controller.signal, timeout: 0 },
    )
  })

  it('forwards cancellation for the path-based endpoint too', async () => {
    const controller = new AbortController()
    const post = vi.spyOn(api, 'post').mockResolvedValue({ data: { success: true } })

    await streamCheckerAPI.checkStreamById(
      42,
      { freeze_check_enabled: true },
      { signal: controller.signal },
    )

    expect(post).toHaveBeenCalledWith(
      '/stream-checker/streams/42/check',
      { freeze_check_enabled: true },
      { signal: controller.signal, timeout: 0 },
    )
  })

  it('treats the second object-signature argument as request config', async () => {
    const controller = new AbortController()
    const post = vi.spyOn(api, 'post').mockResolvedValue({ data: { success: true } })

    await streamCheckerAPI.checkStream(
      { stream_id: 42, loop_check_enabled: true },
      { signal: controller.signal, timeout: 1 },
    )

    expect(post).toHaveBeenCalledWith(
      '/stream-checker/check-stream',
      { stream_id: 42, loop_check_enabled: true },
      { signal: controller.signal, timeout: 0 },
    )
  })
})
