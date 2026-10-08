import { useEffect, useRef } from 'react'
import { createVisiblePoller } from '@/lib/visible-poller.js'

export function useVisiblePolling(poll, intervalMs, enabled = true) {
  const latest = useRef({ poll, intervalMs })
  latest.current = { poll, intervalMs }
  useEffect(() => {
    if (!enabled) return undefined
    const poller = createVisiblePoller({
      poll: signal => latest.current.poll(signal),
      intervalMs: () => typeof latest.current.intervalMs === 'function' ? latest.current.intervalMs() : latest.current.intervalMs,
    })
    poller.start()
    return () => poller.stop()
  }, [enabled])
}
