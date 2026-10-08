import { describe, expect, it } from 'vitest'
import { getNavigationItem } from './Sidebar.jsx'

describe('navigation location labels', () => {
  it.each([
    ['/', 'Dashboard'],
    ['/dashboard', 'Dashboard'],
    ['/channels', 'Channels'],
    ['/stream-monitoring', 'Monitoring'],
    ['/stream-checker', 'Stream Checker'],
    ['/shadow-monitor', 'Shadow Monitor'],
    ['/teamarr-preflight', 'Teamarr Preflight'],
    ['/scheduling', 'Scheduling'],
    ['/stats', 'Analytics'],
    ['/changelog', 'Changelog'],
    ['/settings', 'Settings'],
    ['/automation/profiles/example', 'Settings'],
    ['/help', 'Help'],
    ['/help/scheduling', 'Help'],
  ])('labels %s as %s', (path, expected) => {
    expect(getNavigationItem(path).text).toBe(expected)
  })
})
