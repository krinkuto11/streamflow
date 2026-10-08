export function createVisiblePoller({ poll, intervalMs, document: doc = document, onError = () => {}, setTimer = setTimeout, clearTimer = clearTimeout }) {
  let stopped = true
  let timer = null
  let controller = null
  let refreshPending = false
  const visible = () => doc.visibilityState !== 'hidden'
  const schedule = delay => {
    if (stopped || !visible()) return
    if (timer !== null) clearTimer(timer)
    timer = setTimer(run, delay)
  }
  const run = async () => {
    timer = null
    if (stopped || !visible() || controller) return
    controller = new AbortController()
    try {
      await poll(controller.signal)
    } catch (error) {
      if (!controller.signal.aborted) onError(error)
    } finally {
      controller = null
      const immediate = refreshPending
      refreshPending = false
      schedule(immediate ? 0 : typeof intervalMs === 'function' ? intervalMs() : intervalMs)
    }
  }
  const visibilityChanged = () => {
    if (timer !== null) clearTimer(timer)
    timer = null
    if (!visible()) {
      controller?.abort()
    } else if (controller) {
      refreshPending = true
    } else {
      schedule(0)
    }
  }
  return {
    start() {
      if (!stopped) return
      stopped = false
      doc.addEventListener('visibilitychange', visibilityChanged)
      schedule(0)
    },
    stop() {
      stopped = true
      doc.removeEventListener('visibilitychange', visibilityChanged)
      if (timer !== null) clearTimer(timer)
      timer = null
      controller?.abort()
    },
  }
}
