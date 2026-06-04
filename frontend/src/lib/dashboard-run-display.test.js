import { describe, expect, it } from 'vitest'
import {
  getAbortedRunDisplay,
  getDashboardActionStates,
  getAutomationStageCards,
  getCacheSyncCardDetail,
  getRunHistoryBaseline,
  getM3uRefreshCardDetail,
  getRunDurationCardValue,
  getRunDurationValue,
  getSkippedRunDisplay,
  getStreamCheckerRunDisplay,
  isM3uRefreshSkipped,
  preferLiveRunSeconds,
  shouldShowAutomationRunCard,
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

  it('detects active single-channel checks even when no batch queue is exposed', () => {
    const display = getStreamCheckerRunDisplay({
      runState: 'idle',
      batchTotal: 0,
      completed: 0,
      now: Date.parse('2026-05-29T18:05:41Z'),
      streamCheckerStatus: {
        checking: true,
        stream_checking_mode: true,
        queue: {
          queue_size: 0,
          in_progress: 0,
        },
        progress: {
          is_single_channel_check: true,
          timestamp: '2026-05-29T18:04:41Z',
          channel_name: 'Single Channel',
        },
      },
    })

    expect(display.isProcessing).toBe(true)
    expect(display.streamCheckerOnlyActive).toBe(true)
    expect(display.streamQueueActive).toBe(false)
    expect(display.streamCheckerElapsedSeconds).toBe(60)
  })

  it('ignores completed queue history when the stream checker is idle', () => {
    const display = getStreamCheckerRunDisplay({
      runState: 'skipped',
      runStage: 'quality_checking',
      batchTotal: 5,
      completed: 5,
      now: Date.parse('2026-06-02T12:00:00Z'),
      streamCheckerStatus: {
        stream_checking_mode: false,
        queue: {
          state: 'completed',
          queue_size: 0,
          in_progress: 0,
          completed: 5,
          failed: 0,
          started_at: '2026-06-02T11:46:33Z',
        },
      },
    })

    expect(display.isProcessing).toBe(false)
    expect(display.streamCheckerOnlyActive).toBe(false)
    expect(display.streamQueueActive).toBe(false)
    expect(display.streamCheckerElapsedSeconds).toBeNull()
    expect(display.stageCards).toEqual([])
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

  it('keeps automation-only stages neutral for manual stream checker runs', () => {
    const cards = getAutomationStageCards({
      stages,
      runStatusStages: [
        { key: 'settings', status: 'completed' },
        { key: 'm3u_refresh', status: 'completed' },
        { key: 'stream_matching', status: 'completed' },
      ],
      displayRunStageId: 'quality_checking',
      displayRunningRun: true,
      streamRunActive: true,
    })

    expect(cards[0].status).toBe('pending')
    expect(cards[2].status).toBe('pending')
    expect(cards[4].status).toBe('pending')
    expect(cards[5].status).toBe('pending')
    expect(cards[6].status).toBe('running')
    expect(cards[7].status).toBe('pending')
  })

  it('keeps no-due skipped automation stages neutral', () => {
    const cards = getAutomationStageCards({
      stages,
      runStatusStages: [
        { key: 'settings', status: 'completed' },
        { key: 'period_discovery', status: 'pending' },
      ],
      displayRunStageId: 'skipped',
      displayRunningRun: false,
      neutralRun: true,
    })

    expect(cards.map(stage => stage.status)).toEqual([
      'pending',
      'pending',
      'pending',
      'pending',
      'pending',
      'pending',
      'pending',
      'pending',
    ])
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

  it('keeps manual stream checker predecessor durations neutral', () => {
    const value = getRunDurationValue({
      runDurations: { m3u_refresh_seconds: 0 },
      durationKey: 'm3u_refresh_seconds',
      stageId: 'm3u_refresh',
      displayRunStageId: 'quality_checking',
      streamRunActive: true,
      stages,
    })

    expect(value).toBeNull()
  })

  it('marks M3U refresh as skipped when no playlist refresh was requested', () => {
    expect(isM3uRefreshSkipped({
      runCounts: {
        playlists_to_refresh: 0,
        refreshed_playlists: 0,
      },
    })).toBe(true)
  })

  it('does not mark M3U refresh as skipped when playlists were requested', () => {
    expect(isM3uRefreshSkipped({
      runCounts: {
        playlists_to_refresh: 2,
        refreshed_playlists: 0,
      },
    })).toBe(false)
  })

  it('does not apply M3U skipped semantics to manual stream checker runs', () => {
    expect(isM3uRefreshSkipped({
      streamRunActive: true,
      runCounts: {
        playlists_to_refresh: 0,
        refreshed_playlists: 0,
      },
    })).toBe(false)
  })

  it('renders duration cards as skipped or formatted duration', () => {
    expect(getRunDurationCardValue({ skipped: true, seconds: 0 })).toBe('Skipped')
    expect(getRunDurationCardValue({ seconds: 61 })).toBe('1m 1s')
    expect(getRunDurationCardValue({ seconds: null })).toBe('N/A')
  })

  it('summarizes running M3U refresh request progress for the duration card', () => {
    expect(getM3uRefreshCardDetail({
      runCounts: {
        m3u_refresh_state: 'requesting',
        m3u_refresh_current: 1,
        m3u_refresh_total: 3,
      },
    })).toBe('Refreshing playlist 2/3')

    expect(getM3uRefreshCardDetail({
      runCounts: {
        m3u_refresh_state: 'accepted',
        m3u_refresh_current: 2,
        m3u_refresh_total: 3,
      },
    })).toBe('2/3 refresh requests accepted')

    expect(getM3uRefreshCardDetail({
      runCounts: {
        m3u_refresh_state: 'failed',
        m3u_refresh_current: 2,
        m3u_refresh_total: 3,
      },
    })).toBe('2/3 refresh requests failed')
  })

  it('keeps skipped and manual-check M3U refresh card details explicit', () => {
    expect(getM3uRefreshCardDetail({ skipped: true })).toBe('No playlist refresh requested')
    expect(getM3uRefreshCardDetail({ streamRunActive: true })).toBeNull()
  })

  it('summarizes cache sync step progress for the duration card', () => {
    expect(getCacheSyncCardDetail({
      runCounts: {
        cache_sync_state: 'completed',
        cache_sync_successful_steps: 2,
        cache_sync_total_steps: 2,
      },
    })).toBe('2/2 cache sync steps completed')

    expect(getCacheSyncCardDetail({
      runCounts: {
        cache_sync_state: 'warning',
        cache_sync_successful_steps: 1,
        cache_sync_total_steps: 2,
      },
    })).toBe('1/2 cache sync steps completed with warnings')

    expect(getCacheSyncCardDetail({ skipped: true })).toBe('No cache sync requested')
    expect(getCacheSyncCardDetail({ streamRunActive: true })).toBeNull()
  })

  it('normalizes recent automation run history for dashboard display', () => {
    expect(getRunHistoryBaseline({
      summary: {
        sample_count: 3,
        latest: {
          duration_seconds: 120,
          total_channels: 12,
        },
        typical_duration_seconds: 90,
        average_duration_seconds: 100,
        typical_seconds_per_channel: 7.5,
      },
    })).toEqual({
      available: true,
      sampleCount: 3,
      latest: {
        duration_seconds: 120,
        total_channels: 12,
      },
      typicalDurationSeconds: 90,
      averageDurationSeconds: 100,
      typicalSecondsPerChannel: 7.5,
    })
  })

  it('keeps empty automation run history quiet', () => {
    expect(getRunHistoryBaseline({ summary: { sample_count: 0 } })).toEqual({
      available: false,
      sampleCount: 0,
      latest: null,
      typicalDurationSeconds: null,
      averageDurationSeconds: null,
      typicalSecondsPerChannel: null,
    })
  })

  it('uses explicit idle wording for skipped no-due automation runs', () => {
    const display = getSkippedRunDisplay({
      skippedRun: true,
      runStatusMessage: 'No active automation periods were due',
    })

    expect(display).toEqual({
      badgeLabel: 'Idle',
      message: 'Waiting for next scheduled run',
      progressDetail: 'No active automation periods were due',
      stageLabel: 'No Due Periods',
    })
  })

  it('does not override skipped automation wording during manual stream checks', () => {
    const display = getSkippedRunDisplay({
      skippedRun: true,
      streamRunActive: true,
      streamQueueActive: true,
      runStatusMessage: 'No active automation periods were due',
    })

    expect(display.badgeLabel).toBeNull()
    expect(display.message).toBeNull()
    expect(display.progressDetail).toBeNull()
    expect(display.stageLabel).toBeNull()
  })

  it('uses terminal abort wording instead of stale stage progress', () => {
    const display = getAbortedRunDisplay({
      runState: 'aborted',
      runStatus: {
        last_error: 'Automation run was stopped by the user',
        message: 'Automation run was stopped by the user',
        progress: {
          current: 2,
          message: 'Syncing channel cache completed',
          percent: 100,
          total: 2,
        },
      },
    })

    expect(display).toEqual({
      message: 'Automation run was stopped by the user',
      progressDetail: 'Automation run was stopped by the user',
      progressPercent: 0,
    })
  })

  it('hides the automation run card for idle no-due runs', () => {
    const display = getSkippedRunDisplay({
      skippedRun: true,
      runStatusMessage: 'No active automation periods were due',
    })

    expect(shouldShowAutomationRunCard({
      showRunProgress: true,
      skippedRunDisplay: display,
    })).toBe(false)
  })

  it('keeps the automation run card visible for real progress', () => {
    expect(shouldShowAutomationRunCard({
      showRunProgress: true,
      skippedRunDisplay: {
        badgeLabel: null,
        stageLabel: null,
      },
    })).toBe(true)
  })

  it('keeps UDI reload available during stream checks but blocks conflicting automation starts', () => {
    const actions = getDashboardActionStates({
      actionLoading: '',
      isStreamCheckerProcessing: true,
      udiSyncing: false,
    })

    expect(actions.reloadUdi.disabled).toBe(false)
    expect(actions.runAutomation.disabled).toBe(true)
    expect(actions.runAutomation.reason).toMatch(/stream check/i)
  })

  it('blocks automation actions while Dispatcharr cache refresh is running', () => {
    const actions = getDashboardActionStates({
      actionLoading: '',
      isStreamCheckerProcessing: false,
      udiInitializing: true,
      udiSyncing: false,
    })

    expect(actions.reloadUdi.disabled).toBe(true)
    expect(actions.reloadUdi.reason).toMatch(/cache refresh/i)
    expect(actions.runAutomation.disabled).toBe(true)
    expect(actions.runAutomation.reason).toMatch(/cache is ready/i)
  })

  it('blocks dashboard actions while another action is already running', () => {
    const actions = getDashboardActionStates({
      actionLoading: 'udi',
      isStreamCheckerProcessing: false,
      udiSyncing: true,
    })

    expect(actions.reloadUdi.disabled).toBe(true)
    expect(actions.runAutomation.disabled).toBe(true)
  })
})
