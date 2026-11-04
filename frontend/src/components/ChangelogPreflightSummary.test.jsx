import { describe, expect, it } from 'vitest'
import { renderToStaticMarkup } from 'react-dom/server'
import { ChangelogPreflightSummary, ChangelogPreflightStreams } from './ChangelogPreflightSummary.jsx'
import { getChangelogPreflightSummary } from '@/lib/changelog-preflight-summary.js'

describe('preflight changelog cards', () => {
  it('shows channel, event and zero fresh probes before opening the stream results', () => {
    const summary = getChangelogPreflightSummary({
      details: { channel_id: 1, channel_name: 'Sports <1>', program_name: 'A vs B', run_mode: 'teamarr_preflight' },
      subentries: [{ group: 'check', items: [{ channel_id: 1, stats: { preflight_inspection: { streams: [
        { stream_id: 7, reason: 'known_good_reused', measurement_source: 'previous_measurement' },
      ] } } }] }],
    })
    const html = renderToStaticMarkup(<ChangelogPreflightSummary summary={summary} />)
    expect(html).toContain('Sports &lt;1&gt;')
    expect(html).toContain('A vs B')
    expect(html).toContain('Freshly checked: 0')
    expect(html).toContain('Known good reused: 1')
    expect(html).toContain('Stream results (1)')
    expect(html).toContain('data-state="closed"')
  })

  it('identifies reused results and their original time without claiming a fresh success', () => {
    const html = renderToStaticMarkup(<ChangelogPreflightStreams rows={[{
      stream_id: 7, stream_name: 'Feed 7', provider_name: 'Provider 2', measurement: 'reused',
      measurement_checked_at: '2026-11-04T10:00:00Z', status: 'skipped',
    }]} />)
    expect(html).toContain('Feed 7')
    expect(html).toContain('Provider 2')
    expect(html).toContain('Known good reused')
    expect(html).toContain('Previous successful check:')
    expect(html).toContain('Passed previously')
  })
})
