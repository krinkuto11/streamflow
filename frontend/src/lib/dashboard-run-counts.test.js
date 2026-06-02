import { describe, expect, it } from 'vitest'
import { getDashboardRunCounts } from './dashboard-run-counts.js'

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
})
