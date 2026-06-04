import { describe, expect, it } from 'vitest'
import {
  getOperatorHelpDetailTopic,
  operatorHelpDetailGuidePrinciples,
  operatorHelpDetailTopics,
  operatorHelpQuickChecks,
  operatorHelpSections,
} from './operator-help-content.js'

describe('operatorHelpSections', () => {
  it('covers the non-Teamarr V3 operator areas', () => {
    expect(operatorHelpSections.map(section => section.id)).toEqual([
      'startup-cache',
      'profiles-periods',
      'stream-checker',
      'teamarr-preflight',
      'shadow-monitor',
      'hardware',
      'troubleshooting',
    ])
    expect(operatorHelpQuickChecks).toHaveLength(6)
    expect(operatorHelpDetailGuidePrinciples.join(' ')).toMatch(/platform neutral/i)
    expect(operatorHelpDetailGuidePrinciples.join(' ')).toMatch(/settings reference/)

    const profilesPeriods = operatorHelpSections.find(section => section.id === 'profiles-periods')
    expect(profilesPeriods.items.join(' ')).toMatch(/Missed-run grace/)
    expect(profilesPeriods.items.join(' ')).toMatch(/Catch-up cap/)
    expect(profilesPeriods.items.join(' ')).toMatch(/Maintenance window/)
    expect(profilesPeriods.items.join(' ')).toMatch(/Teamarr event window/)
    expect(profilesPeriods.items.join(' ')).toMatch(/post-start checks/)

    const startupCache = operatorHelpSections.find(section => section.id === 'startup-cache')
    expect(startupCache.items.join(' ')).toMatch(/refresh requests are accepted by Dispatcharr/i)
    expect(startupCache.items.join(' ')).toMatch(/Cache Sync/)

    const streamChecker = operatorHelpSections.find(section => section.id === 'stream-checker')
    expect(streamChecker.items.join(' ')).toMatch(/Dead, Blank, and Frozen/)
    expect(streamChecker.items.join(' ')).toMatch(/cumulative stream results/)

    const teamarr = operatorHelpSections.find(section => section.id === 'teamarr-preflight')
    expect(teamarr.items.join(' ')).toMatch(/Post-start checks/)
    expect(teamarr.links.map(link => link.to)).toContain('/help/teamarr-preflight')

    const shadowMonitor = operatorHelpSections.find(section => section.id === 'shadow-monitor')
    expect(shadowMonitor.items.join(' ')).toMatch(/Channel Switch Limit/)
    expect(shadowMonitor.items.join(' ')).toMatch(/per-channel rolling-hour guard/)

    const hardware = operatorHelpSections.find(section => section.id === 'hardware')
    expect(hardware.title).toBe('Hardware And Fallback')
    expect(hardware.items.join(' ')).toMatch(/Intel\/DRI/)
    expect(hardware.items.join(' ')).toMatch(/VAAPI, QSV, or DRI/)

    const troubleshooting = operatorHelpSections.find(section => section.id === 'troubleshooting')
    expect(troubleshooting.items.join(' ')).toMatch(/After setup or image updates/)
    expect(troubleshooting.items.join(' ')).toMatch(/post-start checks/)
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

  it('provides platform-neutral detailed guide topics with settings explanations', () => {
    expect(operatorHelpDetailTopics.map(topic => topic.id)).toEqual([
      'setup',
      'automation-periods',
      'stream-checker',
      'teamarr-preflight',
      'shadow-monitor',
      'hardware-fallback',
    ])

    for (const topic of operatorHelpDetailTopics) {
      expect(topic.visual.steps.length).toBeGreaterThanOrEqual(4)
      expect(topic.steps.length).toBeGreaterThanOrEqual(4)
      expect(topic.settings.length).toBeGreaterThanOrEqual(4)
      expect(topic.smokeChecks.length).toBeGreaterThanOrEqual(3)
      expect(topic.links.length).toBeGreaterThanOrEqual(1)

      for (const setting of topic.settings) {
        expect(setting).toEqual(expect.objectContaining({
          name: expect.any(String),
          controlType: expect.any(String),
          defaultValue: expect.any(String),
          location: expect.any(String),
          effect: expect.any(String),
          useWhen: expect.any(String),
          risk: expect.any(String),
        }))
        expect(['Visible UI setting', 'Container setting', 'Status/API']).toContain(setting.controlType)
      }
    }

    const setup = getOperatorHelpDetailTopic('setup')
    expect(setup.settings.find(setting => setting.name === 'API_HOST').controlType).toBe('Container setting')
    expect(setup.settings.find(setting => setting.name === 'Startup cache readiness').controlType).toBe('Status/API')

    expect(getOperatorHelpDetailTopic('teamarr-preflight').settings.map(setting => setting.name)).toContain('Post-Start Checks')
    const teamarr = getOperatorHelpDetailTopic('teamarr-preflight')
    const preStartRetries = teamarr.settings.find(setting => setting.name === 'Pre-Start Retries')
    const postStartGrace = teamarr.settings.find(setting => setting.name === 'Post-Start Grace')
    expect(preStartRetries.defaultValue).toBe('10 min before start; 3 min before start')
    expect(preStartRetries.effect).toMatch(/two separate pre-start retry buckets/i)
    expect(postStartGrace.useWhen).toMatch(/at least as large as the largest post-start check/i)
    expect(teamarr.steps.join(' ')).toMatch(/2 minutes and 4 minutes after start/i)
    expect(teamarr.settings.find(setting => setting.name === 'Post-Start Checks').defaultValue).toBe('2 min after start; 4 min after start')
    expect(teamarr.settings.find(setting => setting.name === 'Post-Start Checks').location).toBe('Teamarr Preflight -> Configuration')
    expect(teamarr.smokeChecks.join(' ')).toMatch(/2-minute post-start check/i)
    expect(teamarr.smokeChecks.join(' ')).toMatch(/4 minutes/i)
    expect(teamarr.smokeChecks.join(' ')).toMatch(/dead-stream removal off/i)
    const automation = getOperatorHelpDetailTopic('automation-periods')
    expect(automation.settings.map(setting => setting.name)).toEqual(expect.arrayContaining([
      'Catch-up cap',
      'Maintenance window',
      'Teamarr event window',
    ]))
    expect(automation.settings.find(setting => setting.name === 'Maintenance window').location).toBe('Settings -> Scheduling -> Automation Run Policy')
    expect(getOperatorHelpDetailTopic('shadow-monitor').settings.map(setting => setting.name)).toContain('Channel Switch Limit')
    expect(getOperatorHelpDetailTopic('hardware-fallback').settings.map(setting => setting.name)).toContain('CPU Fallback')
    expect(getOperatorHelpDetailTopic('automation-periods').settings.map(setting => setting.name)).toContain('Missed-run grace')
    expect(getOperatorHelpDetailTopic('missing-topic')).toBeNull()

    const visibleText = JSON.stringify(operatorHelpDetailTopics)
    expect(visibleText).not.toMatch(new RegExp('un' + 'raid', 'i'))
    expect(visibleText).not.toMatch(/platform-specific/i)
  })
})
