import { describe, expect, it } from 'vitest'
import { getChangelogPreflightSummary } from './changelog-preflight-summary.js'

const preflightEntry = (inspection) => ({
  details: { run_mode: 'teamarr_preflight', channel_id: 8, channel_name: 'Sports 1', program_name: 'Team A vs Team B' },
  subentries: [{ group: 'check', items: [{ channel_id: 8, stats: { preflight_inspection: inspection } }] }],
})

describe('preflight changelog summary', () => {
  it('counts fresh failures and reuse separately from other skips, including removed sources', () => {
    const result = getChangelogPreflightSummary(preflightEntry({ total_streams: 4, streams: [
      { stream_id: 1, measurement_source: 'current_check', status: 'dead', action: 'not_retained' },
      { stream_id: 2, measurement_source: 'previous_measurement', reason: 'known_good_reused', measurement_checked_at: '2026-11-04T10:00:00Z' },
      { stream_id: 3, measurement_source: 'previous_measurement', reason: 'cached_measurement' },
      { stream_id: 4, measurement_source: 'not_measured', reason: 'active_viewer' },
    ] }))
    expect(result).toMatchObject({ channelName: 'Sports 1', programName: 'Team A vs Team B', checked: 1, reused: 1, skipped: 2, truncated: false })
    expect(result.rows[1].measurement_checked_at).toBe('2026-11-04T10:00:00Z')
    expect(result.rows[0].action).toBe('not_retained')
  })

  it('does not invent reuse evidence for historical entries', () => {
    const entry = preflightEntry(undefined)
    entry.subentries[0].items[0].stats.stream_details = [{ stream_id: 3, status: 'alive' }]
    expect(getChangelogPreflightSummary(entry)).toMatchObject({ recorded: false, checked: 0, reused: 0, rows: [{ measurement: 'unknown' }] })
  })

  it('keeps all-good and empty checks explicit', () => {
    expect(getChangelogPreflightSummary(preflightEntry({ streams: [
      { reason: 'known_good_reused', measurement_source: 'previous_measurement' },
    ] }))).toMatchObject({ recorded: true, checked: 0, reused: 1, skipped: 0 })
    expect(getChangelogPreflightSummary(preflightEntry({ streams: [] }))).toMatchObject({ recorded: true, checked: 0, reused: 0, total: 0 })
  })

  it('marks bounded snapshots so partial counts are not presented as full totals', () => {
    expect(getChangelogPreflightSummary(preflightEntry({ total_streams: 600, truncated: true, streams: [{ measurement_source: 'current_check' }] })))
      .toMatchObject({ total: 600, truncated: true, checked: 1 })
  })

  it('uses the matching channel and snapshot run mode without affecting other runs', () => {
    const entry = preflightEntry({ streams: [] })
    entry.subentries[0].items.unshift({ channel_id: 9, stats: { preflight_inspection: { streams: [{ measurement_source: 'current_check' }] } } })
    delete entry.details.run_mode
    entry.details.run_snapshot = { run_mode: 'teamarr_preflight' }
    expect(getChangelogPreflightSummary(entry).checked).toBe(0)
    entry.details.channel_id = 404
    expect(getChangelogPreflightSummary(entry)).toMatchObject({ recorded: false, rows: [] })
    expect(getChangelogPreflightSummary({ details: { run_mode: 'single_channel_check' } })).toBeNull()
    expect(getChangelogPreflightSummary()).toBeNull()
  })
})
