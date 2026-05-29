import { describe, expect, it } from 'vitest'
import { getStreamCheckerRunDisplay } from './dashboard-run-display.js'

describe('dashboard stream checker run display', () => {
  it('uses queue progress and stream timing when no automation run is active', () => {
    const display = getStreamCheckerRunDisplay({
      runState: 'idle',
      runStage: 'idle',
      batchTotal: 30,
      completed: 1,
      now: Date.parse('2026-05-29T18:04:41Z'),
      streamCheckerStatus: {
        stream_checking_mode: true,
        queue: {
          started_at: '2026-05-29T18:02:41Z',
        },
        progress: {
          streams_detail: [
            { started_at: '2026-05-29T18:03:41Z' },
            { started_at: '2026-05-29T18:03:55Z' },
          ],
        },
      },
    })

    expect(display.isProcessing).toBe(true)
    expect(display.streamCheckerOnlyActive).toBe(true)
    expect(display.streamQueueActive).toBe(true)
    expect(display.currentStreamElapsedSeconds).toBe(60)
    expect(display.streamCheckerElapsedSeconds).toBe(120)
    expect(display.stageCards).toEqual([
      {
        key: 'quality_checking',
        label: 'Quality Check',
        status: 'running',
        current: 1,
        total: 30,
      },
    ])
  })

  it('lets manual stream checker progress override a skipped automation run', () => {
    const display = getStreamCheckerRunDisplay({
      runState: 'skipped',
      runStage: 'skipped',
      batchTotal: 3,
      completed: 1,
      streamCheckerStatus: {
        stream_checking_mode: true,
        queue: {
          started_at: '2026-05-29T18:02:41Z',
        },
      },
    })

    expect(display.streamCheckerOnlyActive).toBe(true)
    expect(display.streamQueueActive).toBe(true)
    expect(display.stageCards[0]).toMatchObject({
      key: 'quality_checking',
      status: 'running',
      current: 1,
      total: 3,
    })
  })

  it('falls back to zero seconds until stream starts are available', () => {
    const display = getStreamCheckerRunDisplay({
      runState: 'idle',
      batchTotal: 30,
      completed: 1,
      streamCheckerStatus: {
        stream_checking_mode: true,
        progress: { streams_detail: [] },
      },
    })

    expect(display.streamCheckerElapsedSeconds).toBe(0)
  })

  it('uses progress timestamp when older queue payloads do not expose a start time', () => {
    const display = getStreamCheckerRunDisplay({
      runState: 'idle',
      batchTotal: 30,
      completed: 1,
      now: Date.parse('2026-05-29T18:05:41Z'),
      streamCheckerStatus: {
        stream_checking_mode: true,
        progress: {
          timestamp: '2026-05-29T18:04:41Z',
        },
      },
    })

    expect(display.streamCheckerElapsedSeconds).toBe(60)
  })
})
