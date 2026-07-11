export function createSequentialPoller({
  poll,
  intervalMs,
  setTimer = setTimeout,
  clearTimer = clearTimeout,
}) {
  let stopped = false
  let timer = null
  let controller = null

  const run = async () => {
    if (stopped) return

    controller = new AbortController()
    let continuePolling = false
    try {
      continuePolling = (await poll(controller.signal)) !== false
    } finally {
      controller = null
      if (!stopped && continuePolling) {
        timer = setTimer(run, intervalMs)
      }
    }
  }

  return {
    start() {
      void run()
    },
    stop() {
      stopped = true
      if (timer !== null) clearTimer(timer)
      timer = null
      controller?.abort()
    },
  }
}
