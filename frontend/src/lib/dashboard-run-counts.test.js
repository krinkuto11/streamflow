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
        dead_streams: 1,
        blank_streams: 2,
        freeze_streams: 3,
      },
    })

    expect(counts).toMatchObject({
      channels: 4,
      playlists: 2,
      matched: 3,
      checked: 4,
      dead: 1,
      blank: 2,
      freeze: 3,
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
    expect(counts.blank).toBe(1)
    expect(counts.freeze).toBe(2)
  })

  it('uses cumulative stream-checker queue problem counts while a batch is active', () => {
    const counts = getDashboardRunCounts({
      streamQueueActive: true,
      batchTotal: 4,
      completed: 2,
      streamCheckerStatus: {
        queue: {
          dead_streams_count: 3,
          blank_streams_count: 1,
          freeze_streams_count: 2,
        },
        progress: {
          streams_detail: [],
        },
      },
    })

    expect(counts.checked).toBe(2)
    expect(counts.dead).toBe(3)
    expect(counts.blank).toBe(1)
    expect(counts.freeze).toBe(2)
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
      },
      streamCheckerStatus: {
        queue: {
          state: 'completed',
          completed: 2,
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
      ['dead', 'Dead Streams', 1],
      ['blank', 'Blank Streams', 1],
      ['freeze', 'Frozen Streams', 0],
    ])
    expect(metrics.find(metric => metric.key === 'dead').description).toMatch(/last completed Stream Checker batch/i)
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
      ['dead', 'Dead Streams', 1],
      ['blank', 'Blank Streams', 1],
      ['freeze', 'Frozen Streams', 1],
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
      ['dead', 'Dead Streams', 1],
      ['blank', 'Blank Streams', 0],
      ['freeze', 'Frozen Streams', 1],
    ])
  })
})
