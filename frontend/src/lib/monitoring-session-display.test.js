import { describe, expect, it } from 'vitest'
import { getSessionHealth, sortMonitoringSessions, toggleVisibleSessionSelection } from './monitoring-session-display'

describe('monitoring session summaries', () => {
  it('keeps session activity distinct from quality and never marks unmeasured sources healthy', () => {
    expect(getSessionHealth({ is_active: true }).tone).toBe('unknown')
    expect(getSessionHealth({ stable_count: 4, quarantined_count: 1 }).tone).toBe('warning')
    expect(getSessionHealth({ stable_count: 4, review_count: 1 }).tone).toBe('review')
    expect(getSessionHealth({ stable_count: 4 }).tone).toBe('stable')
  })

  it('shows active sessions first and quarantined sources before other active sessions', () => {
    const sessions = [
      { session_id: 'history', is_active: false, quarantined_count: 3, created_at: 300 },
      { session_id: 'stable', is_active: true, stable_count: 2, created_at: 200 },
      { session_id: 'warning', is_active: true, quarantined_count: 1, created_at: 100 },
    ]
    expect(sortMonitoringSessions(sessions).map(session => session.session_id)).toEqual(['warning', 'stable', 'history'])
    expect(sessions[0].session_id).toBe('history')
  })

  it('uses visible membership rather than selection size and preserves other selected sessions', () => {
    const visible = [{ session_id: 'a' }, { session_id: 'b' }]
    const selected = new Set(['a', 'history'])
    const all = toggleVisibleSessionSelection(selected, visible)
    expect([...all]).toEqual(['a', 'history', 'b'])
    expect([...toggleVisibleSessionSelection(all, visible)]).toEqual(['history'])
    expect([...selected]).toEqual(['a', 'history'])
  })
})
