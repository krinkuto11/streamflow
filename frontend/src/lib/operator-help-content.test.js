import { describe, expect, it } from 'vitest'
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import {
  getOperatorHelpDetailTopic,
  operatorHelpDetailGuidePrinciples,
  operatorHelpDetailTopics,
  operatorHelpQuickChecks,
  operatorHelpSections,
} from './operator-help-content.js'

const __dirname = path.dirname(fileURLToPath(import.meta.url))

const getJpegDimensions = (assetPath) => {
  const buffer = fs.readFileSync(assetPath)
  let offset = 2

  while (offset < buffer.length) {
    if (buffer[offset] !== 0xff) {
      offset += 1
      continue
    }

    const marker = buffer[offset + 1]
    const segmentLength = buffer.readUInt16BE(offset + 2)
    if (marker >= 0xc0 && marker <= 0xcf && ![0xc4, 0xc8, 0xcc].includes(marker)) {
      return {
        height: buffer.readUInt16BE(offset + 5),
        width: buffer.readUInt16BE(offset + 7),
      }
    }
    offset += 2 + segmentLength
  }

  throw new Error(`Could not read JPEG dimensions for ${assetPath}`)
}

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
    expect(profilesPeriods.items.join(' ')).toMatch(/Run all due periods/)
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
    expect(troubleshooting.items.join(' ')).toMatch(/elapsed\/limit/)
    expect(troubleshooting.items.join(' ')).toMatch(/post-start checks/)
    expect(troubleshooting.links.map(link => link.to)).toContain('/help/troubleshooting')
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
      'troubleshooting',
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
        if (setting.controlType === 'Visible UI setting') {
          expect(setting.locationTo || topic.settingsLocationTo).toMatch(/^\//)
          expect(setting.location.split('->').map(part => part.trim()).filter(Boolean).length).toBeGreaterThanOrEqual(3)
        }
      }
    }

    const setup = getOperatorHelpDetailTopic('setup')
    expect(setup.settings.find(setting => setting.name === 'API_HOST').controlType).toBe('Container setting')
    expect(setup.settings.find(setting => setting.name === 'Startup cache readiness').controlType).toBe('Status/API')

    expect(getOperatorHelpDetailTopic('teamarr-preflight').settings.map(setting => setting.name)).toContain('Post-Start Retries')
    const teamarr = getOperatorHelpDetailTopic('teamarr-preflight')
    const preStartRetries = teamarr.settings.find(setting => setting.name === 'Pre-Start Retries')
    const postStartRetries = teamarr.settings.find(setting => setting.name === 'Post-Start Retries')
    const postStartGrace = teamarr.settings.find(setting => setting.name === 'Post-Start Grace')
    expect(teamarr.settings.find(setting => setting.name === 'Teamarr API Poll Interval').effect).toMatch(/reads Teamarr managed-event state/i)
    expect(preStartRetries.defaultValue).toBe('2 checks')
    expect(preStartRetries.effect).toMatch(/automatically distributed/i)
    expect(postStartRetries.defaultValue).toBe('2 checks')
    expect(postStartRetries.effect).toMatch(/inside Post Start Grace/i)
    expect(postStartGrace.useWhen).toMatch(/poll interval/i)
    expect(teamarr.steps.join(' ')).toMatch(/Post-Start Retries/i)
    expect(postStartRetries.location).toBe('Teamarr Preflight -> Configuration card -> Post-Start Retries')
    expect(teamarr.smokeChecks.join(' ')).toMatch(/post-start bucket/i)
    expect(teamarr.smokeChecks.join(' ')).toMatch(/4 minutes/i)
    expect(teamarr.smokeChecks.join(' ')).toMatch(/dead-stream removal off/i)
    expect(teamarr.steps.join(' ')).toMatch(/automation can defer/i)
    expect(teamarr.settings.find(setting => setting.name === 'Busy Handling').effect).toMatch(/priority queue/i)
    expect(teamarr.settings.find(setting => setting.name === 'Provider Limit Override').defaultValue).toBe('Off')
    expect(teamarr.settings.find(setting => setting.name === 'Provider Limit Override').risk).toMatch(/active-viewer protection/i)
    expect(teamarr.smokeChecks.join(' ')).toMatch(/queued or deferred event-check context/i)
    expect(teamarr.smokeChecks.join(' ')).toMatch(/active-viewer events/i)
    const automation = getOperatorHelpDetailTopic('automation-periods')
    expect(automation.settings.map(setting => setting.name)).toEqual(expect.arrayContaining([
      'Catch-up cap',
      'Run all due periods',
      'Maintenance window',
      'Teamarr event window',
    ]))
    expect(automation.settings.find(setting => setting.name === 'Maintenance window').location).toBe('Settings -> Scheduling tab -> Automation Run Policy -> Maintenance window/start/end')
    const streamChecker = getOperatorHelpDetailTopic('stream-checker')
    expect(streamChecker.settings.find(setting => setting.name === 'Check on update').controlType).toBe('Status/API')
    expect(streamChecker.settings.find(setting => setting.name === 'Global Concurrent Limit').location).toBe('Stream Checker -> Concurrent Checking tab -> Global Concurrent Limit')
    const shadowSettings = getOperatorHelpDetailTopic('shadow-monitor').settings
    expect(shadowSettings.map(setting => setting.name)).toEqual(expect.arrayContaining([
      'Watch Mode',
      'Dry Run',
      'Freeze Detection',
      'Probe Duration',
      'Channel Switch Limit',
    ]))
    expect(shadowSettings.find(setting => setting.name === 'Watch Mode').defaultValue).toBe('Continuous')
    expect(shadowSettings.find(setting => setting.name === 'Dry Run').defaultValue).toBe('Off')
    expect(shadowSettings.find(setting => setting.name === 'Freeze Detection').defaultValue).toBe('On')
    expect(shadowSettings.find(setting => setting.name === 'Probe Duration').defaultValue).toBe('60 seconds')
    expect(getOperatorHelpDetailTopic('hardware-fallback').settings.map(setting => setting.name)).toContain('CPU Fallback')
    expect(getOperatorHelpDetailTopic('automation-periods').settings.map(setting => setting.name)).toContain('Missed-run grace')
    const troubleshooting = getOperatorHelpDetailTopic('troubleshooting')
    expect(troubleshooting.settings.map(setting => setting.controlType)).toEqual(
      troubleshooting.settings.map(() => 'Status/API'),
    )
    expect(troubleshooting.settings.map(setting => setting.name)).toEqual(expect.arrayContaining([
      'Startup readiness',
      'Stream Checker status',
      'Quality reason details',
      'Hardware status',
      'Teamarr Preflight status',
      'Shadow Monitor status',
      'Changelog and logs',
    ]))
    expect(troubleshooting.settings.find(setting => setting.name === 'Quality reason details').effect).toMatch(/timeout, connectivity, endpoint/i)
    expect(troubleshooting.settings.find(setting => setting.name === 'Quality reason details').effect).toMatch(/elapsed\/limit/i)
    expect(troubleshooting.settings.find(setting => setting.name === 'Quality reason details').useWhen).toMatch(/timeout values/)
    expect(troubleshooting.steps.join(' ')).toMatch(/smallest manual check/i)
    expect(troubleshooting.steps.join(' ')).toMatch(/reason-detail fields/i)
    expect(troubleshooting.smokeChecks.join(' ')).toMatch(/post-start buckets/i)
    expect(getOperatorHelpDetailTopic('missing-topic')).toBeNull()

    const visibleText = JSON.stringify(operatorHelpDetailTopics)
    expect(visibleText).not.toMatch(new RegExp('un' + 'raid', 'i'))
    expect(visibleText).not.toMatch(/platform-specific/i)
  })

  it('uses small lazy visual references only for settings that benefit from a UI crop', () => {
    const references = operatorHelpDetailTopics.flatMap(topic =>
      topic.settings
        .filter(setting => setting.reference)
        .map(setting => ({ topicId: topic.id, settingName: setting.name, ...setting.reference })),
    )

    expect(references.map(reference => `${reference.topicId}:${reference.settingName}`)).toEqual([
      'automation-periods:Run all due periods',
      'automation-periods:Catch-up cap',
      'teamarr-preflight:Post-Start Retries',
      'teamarr-preflight:Busy Handling',
    ])

    for (const reference of references) {
      expect(reference.imageSrc).toMatch(/^\/help\/.+-dark\.jpg$/)
      expect(reference.triggerLabel).toBe('UI Screenshot')
      expect(reference.alt).toMatch(/Dark mode crop/i)
      expect(reference.caption.length).toBeGreaterThan(40)

      const assetPath = path.resolve(__dirname, '../../public', reference.imageSrc.replace(/^\//, ''))
      const stat = fs.statSync(assetPath)
      expect(stat.size).toBeGreaterThan(1000)
      expect(stat.size).toBeLessThan(80_000)

      const dimensions = getJpegDimensions(assetPath)
      expect(reference.width).toBe(dimensions.width)
      expect(reference.height).toBe(dimensions.height)
      expect(dimensions.width).toBeGreaterThanOrEqual(200)
      expect(dimensions.height).toBeGreaterThanOrEqual(120)
      expect(dimensions.width).toBeLessThanOrEqual(1200)
      expect(dimensions.height).toBeLessThanOrEqual(720)
    }
  })
})
