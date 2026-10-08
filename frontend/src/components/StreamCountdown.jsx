import { memo, useState } from 'react'
import { useVisiblePolling } from '@/hooks/use-visible-polling.js'
import { formatDuration } from '@/lib/time-format.js'

export const StreamCountdown = memo(function StreamCountdown({ active, startedAt, duration }) {
  const [now, setNow] = useState(Date.now)
  useVisiblePolling(() => setNow(Date.now()), 1000, Boolean(active && startedAt && duration))
  if (!active || !startedAt || !duration) return <span className="text-muted-foreground">-</span>
  const started = new Date(startedAt).getTime()
  if (!Number.isFinite(started)) return <span className="text-muted-foreground">-</span>
  const remaining = Math.max(0, duration - Math.floor((now - started) / 1000))
  if (remaining === 0) return <span className="text-muted-foreground/50">--</span>
  return <span className={remaining <= 10 ? 'text-amber-500 font-mono text-xs' : 'text-muted-foreground font-mono text-xs'}>{formatDuration(remaining)}</span>
})
