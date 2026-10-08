import { describe, expect, it } from 'vitest'
import { renderToStaticMarkup } from 'react-dom/server'
import { ActiveProfileSummary } from './RegexTableRow.jsx'
import { ChannelPagination } from './ChannelPagination.jsx'

describe('effective channel profile states', () => {
  it('distinguishes a pending request from a resolved empty configuration', () => {
    const pending = renderToStaticMarkup(<ActiveProfileSummary activeProfile={null} />)
    const empty = renderToStaticMarkup(<ActiveProfileSummary activeProfile={{ automation: {} }} />)
    expect(pending).toContain('Loading profile...')
    expect(pending).toContain('aria-busy="true"')
    expect(pending).not.toContain('No profile configured')
    expect(empty).toContain('No profile configured')
    expect(empty).toContain('aria-busy="false"')
  })

  it('does not present a failed lookup as no profile or a default EPG assignment', () => {
    const failed = renderToStaticMarkup(<ActiveProfileSummary activeProfile={{ error: true }} details />)
    expect(failed).toContain('Profile load failed')
    expect(failed).not.toContain('No profile configured')
    expect(failed).not.toContain('Use period profile')
  })

  it('shows the effective inherited profile and the default EPG behavior', () => {
    const html = renderToStaticMarkup(<ActiveProfileSummary activeProfile={{
      automation: { profile_name: 'Full Check', source: 'group', period_name: 'Daily' },
      epg_override: { profile_name: null },
    }} details />)
    expect(html).toContain('Full Check')
    expect(html).toContain('Daily')
    expect(html).toContain('Inherited from group')
    expect(html).toContain('Use period profile')
  })

  it('preserves a configured EPG override in details', () => {
    const html = renderToStaticMarkup(<ActiveProfileSummary activeProfile={{
      automation: { profile_name: 'Full Check', source: 'channel' },
      epg_override: { profile_name: 'Event Check' },
    }} details />)
    expect(html).toContain('Event Check')
    expect(html).not.toContain('Use period profile')
  })
})

describe('channel pagination', () => {
  it('does not offer navigation for an empty or single page', () => {
    expect(ChannelPagination({ page: 1, totalPages: 0 })).toBeNull()
    expect(ChannelPagination({ page: 1, totalPages: 1 })).toBeNull()
  })

  it('disables backward navigation on the first page and forward navigation on the last', () => {
    const first = ChannelPagination({ page: 1, totalPages: 12 }).props.children
    const last = ChannelPagination({ page: 12, totalPages: 12 }).props.children
    expect(first[0].props.disabled).toBe(true)
    expect(first[1].props.disabled).toBe(true)
    expect(first[3].props.disabled).toBe(false)
    expect(last[1].props.disabled).toBe(false)
    expect(last[3].props.disabled).toBe(true)
    expect(last[4].props.disabled).toBe(true)
  })
})
