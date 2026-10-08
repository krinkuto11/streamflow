import { describe, expect, it, vi } from 'vitest'
import { createVisiblePoller } from './visible-poller.js'

describe('visible serial polling', () => {
  it('aborts on hide, waits for the old request, and immediately refreshes on return', async () => {
    const listeners = new Map()
    const doc = { visibilityState: 'visible', addEventListener: (key, fn) => listeners.set(key, fn), removeEventListener: key => listeners.delete(key) }
    const scheduled = []
    let resolve
    let signal
    const poll = vi.fn(nextSignal => {
      signal = nextSignal
      return new Promise(done => { resolve = done })
    })
    const poller = createVisiblePoller({ document: doc, poll, intervalMs: 5000,
      setTimer: (fn, delay) => { scheduled.push({ fn, delay }); return scheduled.length }, clearTimer: vi.fn() })
    poller.start()
    const running = scheduled.shift().fn()
    doc.visibilityState = 'hidden'
    listeners.get('visibilitychange')()
    expect(signal.aborted).toBe(true)
    doc.visibilityState = 'visible'
    listeners.get('visibilitychange')()
    expect(poll).toHaveBeenCalledTimes(1)
    expect(scheduled).toHaveLength(0)
    resolve()
    await running
    expect(scheduled[0].delay).toBe(0)
    poller.stop()
    expect(listeners.size).toBe(0)
  })

  it('does not schedule while hidden', async () => {
    const doc = { visibilityState: 'hidden', addEventListener: vi.fn(), removeEventListener: vi.fn() }
    const setTimer = vi.fn()
    const poller = createVisiblePoller({ document: doc, poll: vi.fn(), intervalMs: 1000, setTimer })
    poller.start()
    expect(setTimer).not.toHaveBeenCalled()
    poller.stop()
  })
  it('schedules another attempt after a transient error', async () => {
    const scheduled = []
    const onError = vi.fn()
    const doc = { visibilityState: 'visible', addEventListener: vi.fn(), removeEventListener: vi.fn() }
    const poller = createVisiblePoller({ document: doc, poll: vi.fn().mockRejectedValue(new Error('offline')),
      intervalMs: 1000, onError, setTimer: (fn, delay) => { scheduled.push({ fn, delay }); return scheduled.length }, clearTimer: vi.fn() })
    poller.start()
    await scheduled.shift().fn()
    expect(onError).toHaveBeenCalledTimes(1)
    expect(scheduled[0].delay).toBe(1000)
    poller.stop()
  })

})
