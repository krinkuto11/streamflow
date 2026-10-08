import React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'
import { SessionCard } from './StreamMonitoring'

describe('monitoring session access', () => {
  it('exposes native selection and named details, stop and delete buttons', () => {
    const html = renderToStaticMarkup(<SessionCard session={{
      session_id: 'sample', channel_name: 'Sample channel', created_at: 1,
      is_active: true, stable_count: 2, review_count: 1, quarantined_count: 1,
    }} selected={false} />)
    expect(html).toMatch(/<input[^>]+type="checkbox"[^>]+aria-label="Select Sample channel"/)
    for (const name of ['View details for', 'Stop', 'Delete']) {
      expect(html).toMatch(new RegExp(`<button[^>]+aria-label="${name} Sample channel"`))
    }
    expect(html).toContain('Quarantined sources')
    expect(html).toContain('1 in review')
  })

  it('exposes a start action for inactive sessions without claiming unmeasured sources are healthy', () => {
    const html = renderToStaticMarkup(<SessionCard session={{
      session_id: 'empty', channel_name: 'Empty channel', created_at: 1, is_active: false,
    }} selected={false} />)
    expect(html).toMatch(/<button[^>]+aria-label="Start Empty channel"/)
    expect(html).toContain('No source results yet')
    expect(html).not.toContain('Stable sources')
  })
})
