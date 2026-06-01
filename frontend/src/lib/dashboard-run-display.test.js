import { describe, expect, it } from 'vitest'
import {
  getAutomationStageCards,
  getRunDurationValue,
  getStreamCheckerRunDisplay,
  preferLiveRunSeconds,
} from './dashboard-run-display.js'

const stages = [
  { id: 'settings', label: 'Preparing' },
  { id: 'period_discovery', label: 'Schedule' },
  { id: 'm3u_refresh', label: 'M3U Refresh' },
  { id: 'cache_sync', label: 'Cache Sync' },
  { id: 'stream_matching', label: 'Matching' },
  { id: 'quality_queueing', label: 'Queueing' },
  { id: 'quality_checking', label: 'Quality Check' },
  { id: 'finalizing', label: 'Finalizing' },
]

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

  it('uses live stage timing while an automation stage is active', () => {
    expect(preferLiveRunSeconds({
      active: true,
      reportedSeconds: 0,
      liveSeconds: 713,
    })).toBe(713)
  })

  it('keeps reported timing for inactive completed stages', () => {
    expect(preferLiveRunSeconds({
      active: false,
      reportedSeconds: 0,
      liveSeconds: 713,
    })).toBe(0)
  })

  it('normalizes backend stage keys and marks active automation predecessors as done', () => {
    const cards = getAutomationStageCards({
      stages,
      runStatusStages: [
        { key: 'preparing', status: 'completed' },
        { key: 'schedule', status: 'completed' },
        { key: 'm3u_refresh', status: 'completed' },
        { key: 'cache_sync', status: 'completed' },
        { key: 'matching', status: 'completed' },
        { key: 'quality_check', status: 'running' },
      ],
      displayRunStageId: 'quality_checking',
      displayRunningRun: true,
    })

    expect(cards.map(stage => stage.status)).toEqual([
      'completed',
      'completed',
      'completed',
      'completed',
      'completed',
      'completed',
      'running',
      'pending',
    ])
  })

  it('preserves aborted backend stages for dashboard rendering', () => {
    const cards = getAutomationStageCards({
      stages,
      runStatusStages: [
        { key: 'settings', status: 'completed' },
        { key: 'm3u_refresh', status: 'completed' },
        { key: 'quality_checking', status: 'aborted' },
      ],
      displayRunStageId: 'aborted',
      displayRunningRun: false,
    })

    expect(cards.find(stage => stage.id === 'quality_checking').status).toBe('aborted')
  })

  it('synthesizes completed predecessors for manual stream checker runs', () => {
    const cards = getAutomationStageCards({
      stages,
      displayRunStageId: 'quality_checking',
      displayRunningRun: true,
      streamRunActive: true,
    })

    expect(cards[0].status).toBe('completed')
    expect(cards[5].status).toBe('completed')
    expect(cards[6].status).toBe('running')
    expect(cards[7].status).toBe('pending')
  })

  it('uses live stage timing while a duration card is actively running', () => {
    const value = getRunDurationValue({
      runDurations: {},
      durationKey: 'stream_matching_seconds',
      stageId: 'stream_matching',
      displayRunStageId: 'stream_matching',
      displayRunningRun: true,
      displayRunStageElapsedSeconds: 91,
      stages,
    })

    expect(value).toBe(91)
  })

  it('uses manual stream checker elapsed time for the quality duration card', () => {
    const value = getRunDurationValue({
      runDurations: {},
      durationKey: 'quality_check_seconds',
      stageId: 'quality_checking',
      displayRunStageId: 'quality_checking',
      streamRunActive: true,
      streamCheckerElapsedSeconds: 128,
      stages,
    })

    expect(value).toBe(128)
  })

  it('shows zero-second placeholders for synthetic manual predecessors', () => {
    const value = getRunDurationValue({
      runDurations: {},
      durationKey: 'm3u_refresh_seconds',
      stageId: 'm3u_refresh',
      displayRunStageId: 'quality_checking',
      streamRunActive: true,
      stages,
    })

    expect(value).toBe(0)
  })
})
