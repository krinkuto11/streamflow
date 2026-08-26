import { describe, expect, it } from 'vitest'
import { getDashboardRunCounts, getDashboardRunMetrics } from './dashboard-run-counts.js'

describe('dashboard run counts', () => {
  it('includes freeze counts from completed automation run status', () => {
    const counts = getDashboardRunCounts({
      runCounts: {
        channels_with_periods: 4,
        refreshed_playlists: 2,
        assigned_channels: 3,
        quality_checked: 4,
        good_streams: 8,
        dead_streams: 1,
        blank_streams: 2,
        freeze_streams: 3,
        channels_hidden: 2,
        channels_ready: 1,
      },
    })

    expect(counts).toMatchObject({
      channels: 4,
      playlists: 2,
      matched: 3,
      checked: 4,
      good: 8,
      dead: 1,
      blank: 2,
      freeze: 3,
      hidden: 2,
      ready: 1,
    })
  })

  it('starts automation quality stream counters at zero before quality runs', () => {
    const counts = getDashboardRunCounts({
      runCounts: {
        channels_with_periods: 212,
        refreshed_playlists: 5,
        assigned_channels: 0,
        quality_checked: 0,
      },
    })

    expect(counts).toMatchObject({
      channels: 212,
      playlists: 5,
      matched: 0,
      checked: 0,
      good: 0,
      dead: 0,
      blank: 0,
      freeze: 0,
      hidden: 0,
      ready: 0,
    })
  })

  it('derives manual queue dead blank and freeze counts from stream progress details', () => {
    const counts = getDashboardRunCounts({
      streamQueueActive: true,
      batchTotal: 1,
      completed: 0,
      runCounts: {
        dead_streams: 99,
        blank_streams: 99,
        freeze_streams: 99,
      },
      streamCheckerStatus: {
        progress: {
          streams_detail: [
            { status: 'completed' },
            { status: 'dead' },
            { status: 'blank' },
            { status: 'freeze' },
            { status: 'freeze' },
          ],
        },
      },
    })

    expect(counts.dead).toBe(1)
    expect(counts.good).toBe(1)
    expect(counts.blank).toBe(1)
    expect(counts.freeze).toBe(2)
  })

  it('counts incomplete bitrate streams as playable during active quality checks', () => {
    const counts = getDashboardRunCounts({
      streamQueueActive: true,
      batchTotal: 1,
      completed: 0,
      streamCheckerStatus: {
        progress: {
          streams_detail: [
            { status: 'completed', quality_reason_detail: 'none' },
            { status: 'incomplete_bitrate', quality_reason_detail: 'missing_bitrate' },
            { status: 'completed', quality_reason_detail: 'bitrate_below_threshold' },
          ],
        },
      },
    })

    expect(counts.good).toBe(2)
    expect(counts.dead).toBe(0)
    expect(counts.blank).toBe(0)
    expect(counts.freeze).toBe(0)
  })

  it('uses cumulative stream-checker queue problem counts while a batch is active', () => {
    const counts = getDashboardRunCounts({
      streamQueueActive: true,
      batchTotal: 4,
      completed: 2,
      streamCheckerStatus: {
        queue: {
          dead_streams_count: 3,
          good_streams_count: 5,
          blank_streams_count: 1,
          freeze_streams_count: 2,
        },
        progress: {
          streams_detail: [],
        },
      },
    })

    expect(counts.checked).toBe(2)
    expect(counts.good).toBe(5)
    expect(counts.dead).toBe(3)
    expect(counts.blank).toBe(1)
    expect(counts.freeze).toBe(2)
  })

  it('uses stream-checker queue visibility counts while a batch is active', () => {
    const counts = getDashboardRunCounts({
      streamQueueActive: true,
      batchTotal: 4,
      completed: 2,
      runCounts: {
        channels_hidden: 1,
        channels_ready: 1,
      },
      streamCheckerStatus: {
        queue: {
          channels_hidden_count: 3,
          channels_ready_count: 2,
        },
      },
    })

    expect(counts.hidden).toBe(3)
    expect(counts.ready).toBe(2)
  })

  it('does not let stale zero queue problem counts hide active stream details', () => {
    const counts = getDashboardRunCounts({
      streamQueueActive: true,
      batchTotal: 1,
      completed: 0,
      streamCheckerStatus: {
        queue: {
          dead_streams_count: 0,
          good_streams_count: 0,
          blank_streams_count: 0,
          freeze_streams_count: 0,
        },
        progress: {
          streams_detail: [
            { status: 'completed', blank_detected: true },
            { status: 'completed', freeze_detected: true },
          ],
        },
      },
    })

    expect(counts.dead).toBe(0)
    expect(counts.good).toBe(0)
    expect(counts.blank).toBe(1)
    expect(counts.freeze).toBe(1)
  })

  it('keeps last completed stream-checker batch problem counts visible after the queue goes idle', () => {
    const metrics = getDashboardRunMetrics({
      streamQueueHistory: true,
      batchTotal: 2,
      completed: 2,
      runCounts: {
        channels_with_periods: 0,
        refreshed_playlists: 9,
        assigned_channels: 8,
        quality_checked: 2,
        dead_streams: 0,
        blank_streams: 0,
        freeze_streams: 0,
        channels_hidden: 2,
        channels_ready: 1,
      },
      streamCheckerStatus: {
        queue: {
          state: 'completed',
          completed: 2,
          good_streams_count: 7,
          dead_streams_count: 1,
          blank_streams_count: 1,
          freeze_streams_count: 0,
        },
      },
    })

    expect(metrics.map(metric => [metric.key, metric.label, metric.value])).toEqual([
      ['channels', 'Queued Channels', 2],
      ['playlists', 'Refresh Requests', null],
      ['matched', 'Stream Matching', null],
      ['checked', 'Channels Checked', 2],
      ['good', 'Good Streams', 7],
      ['dead', 'Dead Streams', 1],
      ['blank', 'Blank Streams', 1],
      ['freeze', 'Frozen Streams', 0],
      ['hidden', 'Channels Hidden', 2],
      ['ready', 'Channels Restored', 1],
    ])
    expect(metrics.find(metric => metric.key === 'dead').description).toMatch(/last completed Stream Checker batch/i)
  })

  it('uses active single-channel visibility counters without stale automation counts', () => {
    const metrics = getDashboardRunMetrics({
      streamQueueActive: false,
      streamCheckerOnlyActive: true,
      runCounts: {
        channels_hidden: 9,
        channels_ready: 8,
      },
      streamCheckerStatus: {
        progress: {
          is_single_channel_check: true,
          channel_id: 8442,
          channels_hidden: 1,
          channels_ready: 0,
          streams_detail: [],
        },
      },
    })

    expect(metrics.find(metric => metric.key === 'hidden')).toMatchObject({
      label: 'Channels Hidden',
      value: 1,
    })
    expect(metrics.find(metric => metric.key === 'ready')).toMatchObject({
      label: 'Channels Restored',
      value: 0,
    })
  })

  it('falls back to active single-channel snapshot visibility counters', () => {
    const counts = getDashboardRunCounts({
      streamCheckerOnlyActive: true,
      runCounts: {
        channels_hidden: 9,
        channels_ready: 8,
      },
      streamCheckerStatus: {
        progress: {
          is_single_channel_check: true,
          channel_id: 8442,
          run_snapshot: {
            result_summary: {
              channels_hidden: 0,
              channels_ready: 1,
            },
          },
        },
      },
    })

    expect(counts.hidden).toBe(0)
    expect(counts.ready).toBe(1)
  })

  it('does not carry playlist or matching counts into manual quality-only metrics', () => {
    const metrics = getDashboardRunMetrics({
      streamQueueActive: true,
      streamCheckerOnlyActive: true,
      batchTotal: 5,
      completed: 2,
      runCounts: {
        refreshed_playlists: 9,
        assigned_channels: 8,
        channels_hidden: 7,
        channels_ready: 6,
      },
      streamCheckerStatus: {
        progress: {
          streams_detail: [
            { status: 'completed' },
            { status: 'dead' },
            { status: 'blank' },
            { status: 'freeze' },
          ],
        },
      },
    })

    expect(metrics.map(metric => [metric.key, metric.label, metric.value])).toEqual([
      ['channels', 'Queued Channels', 5],
      ['playlists', 'Refresh Requests', null],
      ['matched', 'Stream Matching', null],
      ['checked', 'Channels Checked', 2],
      ['good', 'Good Streams', 1],
      ['dead', 'Dead Streams', 1],
      ['blank', 'Blank Streams', 1],
      ['freeze', 'Frozen Streams', 1],
      ['hidden', 'Channels Hidden', 0],
      ['ready', 'Channels Restored', 0],
    ])
  })

  it('labels automation assignment counts as updated channels while the automation quality queue is active', () => {
    const metrics = getDashboardRunMetrics({
      streamQueueActive: true,
      streamCheckerOnlyActive: false,
      batchTotal: 3,
      completed: 1,
      runCounts: {
        refreshed_playlists: 2,
        assigned_channels: 4,
      },
    })

    expect(metrics.find(metric => metric.key === 'playlists')).toMatchObject({
      label: 'Refresh Requests',
      value: 2,
      description: 'M3U account refresh requests accepted by Dispatcharr.',
    })
    expect(metrics.find(metric => metric.key === 'matched')).toMatchObject({
      label: 'Channels Updated',
      value: 4,
      description: 'Channels that received new stream assignments during matching.',
    })
  })

  it('labels active single-channel quality checks without stale automation counts', () => {
    const metrics = getDashboardRunMetrics({
      streamQueueActive: false,
      streamCheckerOnlyActive: true,
      runCounts: {
        channels_with_periods: 99,
        refreshed_playlists: 9,
        assigned_channels: 8,
        quality_checked: 7,
        dead_streams: 6,
      },
      streamCheckerStatus: {
        progress: {
          channel_id: 8442,
          streams_detail: [
            { status: 'completed' },
            { status: 'dead' },
            { status: 'freeze' },
          ],
        },
      },
    })

    expect(metrics.map(metric => [metric.key, metric.label, metric.value])).toEqual([
      ['channels', 'Active Channel', 1],
      ['playlists', 'Refresh Requests', null],
      ['matched', 'Stream Matching', null],
      ['checked', 'Channels Checked', 0],
      ['good', 'Good Streams', 1],
      ['dead', 'Dead Streams', 1],
      ['blank', 'Blank Streams', 0],
      ['freeze', 'Frozen Streams', 1],
      ['hidden', 'Channels Hidden', 0],
      ['ready', 'Channels Restored', 0],
    ])
  })
})
