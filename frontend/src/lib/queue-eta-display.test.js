import { describe, expect, it } from 'vitest'

import { getQueueEtaDisplay } from './queue-eta-display'

describe('getQueueEtaDisplay', () => {
  it('formats a ready queue ETA', () => {
    expect(getQueueEtaDisplay({ eta_seconds: 125, completed: 12 })).toMatchObject({
      state: 'ready',
      label: '~2m 5s remaining',
      pulse: false,
    })
  })

  it('labels ETA as early while only a few channels have completed', () => {
    expect(getQueueEtaDisplay({ eta_seconds: 3600, completed: 2, failed: 1 })).toMatchObject({
      state: 'early',
      label: 'Early ETA: ~1h remaining',
      pulse: false,
    })
  })

  it('shows learning state when work exists before average timing is known', () => {
    expect(getQueueEtaDisplay({
      eta_seconds: 0,
      queue_size: 4,
      avg_stream_process_time_sec: 0,
    })).toMatchObject({
      state: 'learning',
      label: 'Learning ETA',
      pulse: true,
    })
  })

  it('stays quiet when the queue is idle', () => {
    expect(getQueueEtaDisplay({ eta_seconds: 0, queue_size: 0 })).toMatchObject({
      state: 'idle',
      label: '',
      pulse: false,
    })
  })
})
