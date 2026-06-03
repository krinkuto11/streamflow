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
      ['playlists', 'Playlists Refreshed', null],
      ['matched', 'Stream Matching', null],
      ['checked', 'Channels Checked', 2],
      ['dead', 'Dead Streams', 1],
      ['blank', 'Blank Streams', 1],
      ['freeze', 'Frozen Streams', 1],
    ])
  })

  it('keeps automation matching and playlist metrics while the automation quality queue is active', () => {
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
      label: 'Playlists Refreshed',
      value: 2,
    })
    expect(metrics.find(metric => metric.key === 'matched')).toMatchObject({
      label: 'Channels Matched',
      value: 4,
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
      ['playlists', 'Playlists Refreshed', null],
      ['matched', 'Stream Matching', null],
      ['checked', 'Channels Checked', 0],
      ['dead', 'Dead Streams', 1],
      ['blank', 'Blank Streams', 0],
      ['freeze', 'Frozen Streams', 1],
    ])
  })
})
