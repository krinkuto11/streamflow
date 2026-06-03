import { describe, expect, it } from 'vitest'
import { operatorHelpQuickChecks, operatorHelpSections } from './operator-help-content.js'

describe('operatorHelpSections', () => {
  it('covers the non-Teamarr V3 operator areas', () => {
    expect(operatorHelpSections.map(section => section.id)).toEqual([
      'startup-cache',
      'profiles-periods',
      'stream-checker',
      'shadow-monitor',
      'hardware',
      'troubleshooting',
    ])
    expect(operatorHelpQuickChecks).toHaveLength(5)

    const profilesPeriods = operatorHelpSections.find(section => section.id === 'profiles-periods')
    expect(profilesPeriods.items.join(' ')).toMatch(/Missed-run grace/)
    expect(profilesPeriods.items.join(' ')).toMatch(/Catch-up cap/)
    expect(profilesPeriods.items.join(' ')).toMatch(/Maintenance window/)
    expect(profilesPeriods.items.join(' ')).toMatch(/Teamarr event window/)

    const startupCache = operatorHelpSections.find(section => section.id === 'startup-cache')
    expect(startupCache.items.join(' ')).toMatch(/refresh requests are accepted by Dispatcharr/i)
    expect(startupCache.items.join(' ')).toMatch(/Cache Sync/)

    const hardware = operatorHelpSections.find(section => section.id === 'hardware')
    expect(hardware.items.join(' ')).toMatch(/Intel\/DRI/)
    expect(hardware.items.join(' ')).toMatch(/VAAPI, QSV, or DRI/)
  })

  it('does not expose internal planning or priority wording', () => {
    const visibleText = JSON.stringify({ operatorHelpSections, operatorHelpQuickChecks })

    expect(visibleText).not.toMatch(/Out of Codex V3 scope/i)
    expect(visibleText).not.toMatch(/V3-9 Release/i)
    expect(visibleText).not.toMatch(/V3-12 Parallel/i)
    expect(visibleText).not.toMatch(/Teamarr priority/i)
    expect(visibleText).not.toMatch(/not a user setting/i)
    expect(visibleText).not.toMatch(/normal users/i)
    expect(visibleText).not.toMatch(new RegExp('un' + 'raid', 'i'))
    expect(visibleText).not.toMatch(/playlist refresh completed/i)
  })
})
