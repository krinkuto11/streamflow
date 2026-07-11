import { describe, expect, it, vi } from 'vitest'

import { createSequentialPoller } from './sequential-poller.js'

const flushPromises = () => new Promise((resolve) => setTimeout(resolve, 0))

describe('createSequentialPoller', () => {
  it('schedules the next poll only after the current request settles', async () => {
    let resolvePoll
    let active = 0
    let maximumActive = 0
    const scheduled = []
    const poller = createSequentialPoller({
      intervalMs: 3000,
      setTimer: (callback) => {
        scheduled.push(callback)
        return scheduled.length
      },
      clearTimer: vi.fn(),
      poll: async () => {
        active += 1
        maximumActive = Math.max(maximumActive, active)
        await new Promise((resolve) => { resolvePoll = resolve })
        active -= 1
        return true
      },
    })

    poller.start()
    await flushPromises()
    expect(scheduled).toHaveLength(0)

    resolvePoll()
    await flushPromises()
    expect(scheduled).toHaveLength(1)

    scheduled.shift()()
    await flushPromises()
    expect(maximumActive).toBe(1)
    poller.stop()
  })

  it('does not schedule again after a terminal result', async () => {
    const setTimer = vi.fn()
    const poller = createSequentialPoller({
      intervalMs: 3000,
      setTimer,
      clearTimer: vi.fn(),
      poll: vi.fn().mockResolvedValue(false),
    })

    poller.start()
    await flushPromises()

    expect(setTimer).not.toHaveBeenCalled()
  })

  it('aborts an in-flight poll when stopped', async () => {
    let observedSignal
    const poller = createSequentialPoller({
      intervalMs: 3000,
      poll: (signal) => {
        observedSignal = signal
        return new Promise(() => {})
      },
    })

    poller.start()
    await flushPromises()
    poller.stop()

    expect(observedSignal.aborted).toBe(true)
  })
})
