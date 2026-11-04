import { describe, expect, it } from 'vitest'
import { renderToStaticMarkup } from 'react-dom/server'
import { ChangelogEntry } from './Changelog.jsx'

const entry = (details = {}, stats = {}) => ({
  timestamp: '2026-11-04T20:00:00Z', action: 'single_channel_check',
  details: { channel_id: 8, channel_name: 'Sports <1>', program_name: 'Team A vs Team B', ...details },
  subentries: [{ group: 'check', items: [{ channel_id: 8, channel_name: 'Legacy channel', stats: {
    total_streams: 1, stream_details: [{ stream_id: 1, stream_name: 'Feed 1', score: 1 }], ...stats,
  } }] }],
})

describe('single-channel changelog layout', () => {
  it('shows the channel/event in the heading and exposes the list with one collapsed control', () => {
    const html = renderToStaticMarkup(<ChangelogEntry entry={entry()} />)
    expect(html).toMatch(/<h3[^>]*>Sports &lt;1&gt;<\/h3>/)
    expect(html).toContain('Team A vs Team B')
    expect(html).toContain('Stream results (1)')
    expect(html.match(/aria-expanded="false"/g)).toHaveLength(1)
    expect(html).not.toContain('Single Channel Check Details')
    expect(html).not.toContain('Checked Streams. Checked:')
  })

  it('keeps preflight reuse results and detailed metrics directly accessible without duplicate names', () => {
    const html = renderToStaticMarkup(<ChangelogEntry entry={entry({ run_mode: 'teamarr_preflight' }, {
      preflight_inspection: { streams: [{ stream_id: 1, measurement_source: 'previous_measurement', reason: 'known_good_reused' }] },
    })} />)
    expect(html).toContain('Known good reused: 1')
    expect(html).toContain('Stream results (1)')
    expect(html).toContain('Additional stream metrics (1)')
    expect(html.match(/Sports &lt;1&gt;/g)).toHaveLength(1)
    expect(html.match(/Team A vs Team B/g)).toHaveLength(1)
    expect(html).not.toContain('Single Channel Check Details')
  })

  it('uses historical subentry names when details lack a name and handles absent results', () => {
    const historical = entry({ channel_name: undefined, program_name: undefined })
    expect(renderToStaticMarkup(<ChangelogEntry entry={historical} />)).toMatch(/<h3[^>]*>Legacy channel<\/h3>/)
    historical.subentries = []
    const html = renderToStaticMarkup(<ChangelogEntry entry={historical} />)
    expect(html).toMatch(/<h3[^>]*>Channel 8<\/h3>/)
    expect(html).toContain('No stream results were recorded for this check.')
  })

  it('preserves grouped detail navigation for multi-channel batch runs', () => {
    const batch = { ...entry(), action: 'batch_stream_check' }
    const html = renderToStaticMarkup(<ChangelogEntry entry={batch} />)
    expect(html).toContain('Batch Stream Check Details')
    expect(html).not.toContain('Stream results (1)')
    expect(html).not.toMatch(/<h3[^>]*>Sports/)
  })
})
