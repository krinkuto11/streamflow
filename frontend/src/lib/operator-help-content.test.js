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
  it('covers the operator areas outside dedicated Teamarr details', () => {
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
    expect(profilesPeriods.items.join(' ')).toMatch(/does not delete channels/)

    const startupCache = operatorHelpSections.find(section => section.id === 'startup-cache')
    expect(startupCache.items.join(' ')).toMatch(/refresh requests are accepted by Dispatcharr/i)
    expect(startupCache.items.join(' ')).toMatch(/Cache Sync/)

    const streamChecker = operatorHelpSections.find(section => section.id === 'stream-checker')
    expect(streamChecker.items.join(' ')).toMatch(/Dead, Blank, and Frozen/)
    expect(streamChecker.items.join(' ')).toMatch(/cumulative stream results/)
    expect(streamChecker.items.join(' ')).toMatch(/direct stream check/i)

    const teamarr = operatorHelpSections.find(section => section.id === 'teamarr-preflight')
    expect(teamarr.items.join(' ')).toMatch(/Post-start checks/)
    expect(teamarr.items.join(' ')).toMatch(/static team channels/)
    expect(teamarr.items.join(' ')).toMatch(/Missing Channel/)
    expect(teamarr.items.join(' ')).toMatch(/No Game Window/)
    expect(teamarr.links.map(link => link.to)).toContain('/help/teamarr-preflight')

    const shadowMonitor = operatorHelpSections.find(section => section.id === 'shadow-monitor')
    expect(shadowMonitor.items.join(' ')).toMatch(/Timing, cooldown, and switch-budget values/)
    expect(shadowMonitor.items.join(' ')).toMatch(/internal Continuous safeguards/)
    expect(shadowMonitor.items.join(' ')).toMatch(/Silent Audio/)
    expect(shadowMonitor.items.join(' ')).toMatch(/fMP4/)
    expect(shadowMonitor.items.join(' ')).toMatch(/MPEGTS/)
    expect(shadowMonitor.items.join(' ')).toMatch(/Next Stream Pre-Probe/)
    expect(shadowMonitor.items.join(' ')).toMatch(/Loop Detection/)

    const hardware = operatorHelpSections.find(section => section.id === 'hardware')
    expect(hardware.title).toBe('Hardware And Fallback')
    expect(hardware.items.join(' ')).toMatch(/Intel\/DRI/)
    expect(hardware.items.join(' ')).toMatch(/VAAPI, QSV, or DRI/)

    const troubleshooting = operatorHelpSections.find(section => section.id === 'troubleshooting')
    expect(troubleshooting.items.join(' ')).toMatch(/After setup or image updates/)
    expect(troubleshooting.items.join(' ')).toMatch(/Channels Restored/)
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
        expect([
          'Visible UI setting',
          'Visible UI action',
          'Container setting',
          'Status/API',
          'Backend/API only',
        ]).toContain(setting.controlType)
        if (['Visible UI setting', 'Visible UI action'].includes(setting.controlType)) {
          expect(setting.locationTo || topic.settingsLocationTo).toMatch(/^\//)
          expect(setting.location.split('->').map(part => part.trim()).filter(Boolean).length).toBeGreaterThanOrEqual(3)
        }
      }
    }

    const setup = getOperatorHelpDetailTopic('setup')
    expect(setup.settings.find(setting => setting.name === 'API_HOST').controlType).toBe('Container setting')
    expect(setup.settings.find(setting => setting.name === 'Startup cache readiness').controlType).toBe('Status/API')

    expect(getOperatorHelpDetailTopic('teamarr-preflight').settings.map(setting => setting.name)).toContain('Post-Start Checks')
    const teamarr = getOperatorHelpDetailTopic('teamarr-preflight')
    expect(teamarr.settings.map(setting => setting.name)).toContain('Static Teams')
    expect(teamarr.settings.map(setting => setting.name)).toContain('Scan & Queue Due Checks')
    expect(teamarr.settings.find(setting => setting.name === 'Scan & Queue Due Checks').risk).toMatch(/not a read-only refresh/i)
    expect(teamarr.settings.find(setting => setting.name === 'Quality Profile').effect).toMatch(/static-team preflight checks/i)
    expect(teamarr.settings.find(setting => setting.name === 'Static Teams').effect).toMatch(/mapped persistent Dispatcharr team channel/i)
    expect(teamarr.settings.find(setting => setting.name === 'Static Teams').risk).toMatch(/Missing Channel/i)
    const preStartRetries = teamarr.settings.find(setting => setting.name === 'Pre-Start Retries')
    const postStartGrace = teamarr.settings.find(setting => setting.name === 'Post-Start Grace')
    expect(teamarr.settings.find(setting => setting.name === 'Teamarr Poll Interval').effect).toMatch(/reads Teamarr internal managed-event endpoints/i)
    expect(preStartRetries.defaultValue).toBe('10 min before start; 3 min before start')
    expect(preStartRetries.effect).toMatch(/single value like 3/i)
    expect(preStartRetries.effect).toMatch(/10, 3/i)
    expect(postStartGrace.useWhen).toMatch(/poll interval/i)
    expect(teamarr.steps.join(' ')).toMatch(/2 and 4 minutes/i)
    expect(teamarr.settings.find(setting => setting.name === 'Post-Start Checks').defaultValue).toBe('2 min after start; 4 min after start')
    expect(teamarr.settings.find(setting => setting.name === 'Post-Start Checks').location).toBe('Teamarr Preflight -> Configuration card -> Post-Start Checks')
    expect(teamarr.settings.find(setting => setting.name === 'Post-Start Checks').effect).toMatch(/single value like 2/i)
    expect(teamarr.smokeChecks.join(' ')).toMatch(/2-minute post-start check/i)
    expect(teamarr.smokeChecks.join(' ')).toMatch(/4 minutes/i)
    expect(teamarr.smokeChecks.join(' ')).toMatch(/dead-stream removal off/i)
    expect(teamarr.steps.join(' ')).toMatch(/priority queue during Automation or Stream Checker runs/i)
    expect(teamarr.settings.find(setting => setting.name === 'Queue Events During Active Checks').defaultValue).toBe('On')
    expect(teamarr.settings.find(setting => setting.name === 'Queue Events During Active Checks').effect).toMatch(/does not queue new event checks/i)
    expect(teamarr.settings.find(setting => setting.name === 'Event Priority Queue').location).toBe('Teamarr Preflight -> Active Preflight Checks and Stream Checker -> Stream Checker Configuration -> Queue')
    expect(teamarr.settings.find(setting => setting.name === 'Provider Limit Override')).toBeUndefined()
    expect(teamarr.smokeChecks.join(' ')).toMatch(/queued or deferred event-check context/i)
    expect(teamarr.smokeChecks.join(' ')).toMatch(/No Game Evidence/i)
    const automation = getOperatorHelpDetailTopic('automation-periods')
    expect(automation.settings.map(setting => setting.name)).toEqual(expect.arrayContaining([
      'Catch-up cap',
      'Run all due periods',
      'Maintenance window',
      'Teamarr event window',
      'Retry failed M3U providers',
      'Case Sensitive Regex Matching',
    ]))
    expect(automation.settings.find(setting => setting.name === 'Maintenance window').location).toBe('Settings -> Scheduling tab -> Automation Run Policy -> Maintenance window/start/end')
    expect(automation.settings.find(setting => setting.name === 'Retry failed M3U providers').effect).toMatch(/only providers/i)
    expect(automation.settings.find(setting => setting.name === 'Channel Visibility').location).toBe('Settings -> Profiles tab -> Edit profile -> Channel Visibility')
    expect(automation.settings.find(setting => setting.name === 'Channel Visibility').risk).toMatch(/never deletes channels/i)
    expect(automation.settings.find(setting => setting.name === 'Case Sensitive Regex Matching').location).toBe('Settings -> Profiles tab -> Global Automation Settings -> Case Sensitive Regex Matching')
    expect(automation.settings.find(setting => setting.name === 'Case Sensitive Regex Matching').effect).toMatch(/case-insensitive regex evaluation/i)
    const streamChecker = getOperatorHelpDetailTopic('stream-checker')
    expect(streamChecker.settings.find(setting => setting.name === 'Direct Stream Check').effect).toMatch(/without requiring that stream to be assigned to a channel/i)
    expect(streamChecker.settings.find(setting => setting.name === 'Direct Stream Check').risk).toMatch(/must be idle.*HTTP 409/i)
    expect(streamChecker.settings.find(setting => setting.name === 'Check on update').controlType).toBe('Status/API')
    expect(streamChecker.settings.find(setting => setting.name === 'Global Concurrent Limit').location).toBe('Stream Checker -> Stream Checker Configuration -> Edit -> Concurrent Checking tab -> Global Concurrent Limit')
    expect(streamChecker.settings.find(setting => setting.name === 'Max channels per run').location).toBe('Stream Checker -> Stream Checker Configuration -> Edit -> Queue tab -> Max Channels Per Run')
    expect(streamChecker.settings.find(setting => setting.name === 'CPU Fallback').location).toBe('Stream Checker -> Stream Checker Configuration -> Edit -> Stream Analysis tab -> Hardware Acceleration -> CPU Fallback')
    const accountLimit = streamChecker.settings.find(setting => setting.name === 'M3U account Max Streams')
    expect(accountLimit.controlType).toBe('Backend/API only')
    expect(accountLimit.location).toMatch(/GET \/api\/m3u-accounts -> accounts\[\]\.max_streams/i)
    expect(accountLimit.location).toMatch(/backend\/API.*not editable in the StreamFlow UI/i)
    expect(accountLimit.location).not.toMatch(/Profile Matrix/i)
    expect(accountLimit.defaultValue).toMatch(/fallback when no active provider profile credentials exist/i)
    expect(accountLimit.effect).toMatch(/fallback account capacity only when no active provider profiles exist/i)
    expect(accountLimit.effect).toMatch(/does not cap the aggregate while usable profiles are active/i)
    expect(accountLimit.risk).toMatch(/active profiles.*none can resolve a usable route.*fails closed/i)
    const profileLimits = streamChecker.settings.find(setting => setting.name === 'Provider profile limits')
    expect(profileLimits.controlType).toBe('Status/API')
    expect(profileLimits.location).toBe('Stream Checker -> Current Progress (active run) -> Profile Matrix (expand); API GET /api/stream-checker/progress -> provider_progress[].profile_slots[]')
    expect(profileLimits.effect).toMatch(/independent provider credentials/i)
    expect(profileLimits.effect).toMatch(/finite route limits sum to the account aggregate/i)
    expect(profileLimits.effect).toMatch(/unlimited distinct route makes the aggregate unlimited/i)
    expect(profileLimits.effect).toMatch(/same credential target.*default aliases.*share capacity/i)
    expect(profileLimits.risk).toMatch(/probe URL must remain bound to the profile actually reserved/i)
    expect(profileLimits.risk).toMatch(/non-default profile must produce its own valid credential rewrite or fail closed/i)
    expect(profileLimits.risk).toMatch(/read-only status\/API.*not an editable/i)
    const capacityAuthority = streamChecker.settings.find(setting => setting.name === 'Provider capacity authority')
    expect(capacityAuthority.controlType).toBe('Status/API')
    expect(capacityAuthority.effect).toMatch(/provider_profile_unavailable.*provider_usage_unavailable/i)
    expect(capacityAuthority.risk).toMatch(/never treat either unavailable state as zero usage/i)
    const reservedProfile = streamChecker.settings.find(setting => setting.name === 'Reserved probe profile')
    expect(reservedProfile.controlType).toBe('Status/API')
    expect(reservedProfile.location).toMatch(/Stream Progress Tracking -> Account.*hover for ID and Limit/i)
    expect(reservedProfile.location).toMatch(/streams_detail\[\]\.reserved_profile_id\|reserved_profile_name\|reserved_profile_limit/i)
    expect(reservedProfile.effect).toMatch(/safe ID, name, and actually enforced limit/i)
    expect(reservedProfile.effect).toMatch(/strict shared-route limit/i)
    expect(reservedProfile.effect).toMatch(/profile A with profile B/i)
    expect(reservedProfile.useWhen).toMatch(/without exposing its URL or credentials/i)
    expect(reservedProfile.risk).toMatch(/clears on capacity wait or viewer preemption/i)
    expect(reservedProfile.risk).toMatch(/not retained as Changelog history/i)
    const bitrateRecheck = streamChecker.settings.find(setting => setting.name === 'Bitrate Recheck')
    expect(bitrateRecheck.controlType).toBe('Status/API')
    expect(bitrateRecheck.location).toBe('Stream Checker -> Current Progress (active run) -> Stream Progress Tracking -> Status -> Bitrate Recheck')
    expect(bitrateRecheck.effect).toMatch(/one at a time/i)
    expect(bitrateRecheck.risk).toMatch(/current result remains N\/A/i)
    const bitrateHistory = streamChecker.settings.find(setting => setting.name === 'Bitrate Recheck history')
    expect(bitrateHistory.location).toMatch(/Action filter: Automation Runs.*Analyzed Streams -> Reason/i)
    expect(bitrateHistory.locationTo).toBe('/changelog')
    expect(streamChecker.smokeChecks.join(' ')).toMatch(/Capacity deferred.*provider\/profile slot.*authority\/usage could not be proved/i)
    const hardware = getOperatorHelpDetailTopic('hardware-fallback')
    for (const name of ['Hardware Acceleration', 'Mode', 'Device', 'CPU Fallback']) {
      expect(hardware.settings.find(setting => setting.name === name).location).toMatch(/^Stream Checker -> Stream Checker Configuration -> Edit -> Stream Analysis tab -> Hardware Acceleration/)
    }
    expect(getOperatorHelpDetailTopic('troubleshooting').settings.find(setting => setting.name === 'Effective visual probe duration').location).toMatch(/^Stream Checker -> Stream Checker Configuration -> Edit -> Stream Analysis tab -> FFmpeg Duration/)
    const shadowSettings = getOperatorHelpDetailTopic('shadow-monitor').settings
    expect(shadowSettings.map(setting => setting.name)).toEqual(expect.arrayContaining([
      'Continuous Monitoring',
      'Viewer Output Format',
      'Watcher API Key',
      'Watcher User Agent',
      'Viewer Grace',
      'Dry Run',
      'Freeze Detection',
      'Garbled Audio',
      'Silent Audio',
      'Offline Image',
      'Next Stream Pre-Probe',
      'Configuration revision guard',
      'Loop Detection',
      'Loop Probe Duration',
      'Probe Duration',
      'Healthy Probe Interval',
      'Channel Switch Limit',
    ]))
    expect(shadowSettings.find(setting => setting.name === 'Continuous Monitoring').defaultValue).toBe('Continuous')
    expect(shadowSettings.find(setting => setting.name === 'Continuous Monitoring').effect).toMatch(/Legacy periodic config is normalized/i)
    expect(shadowSettings.find(setting => setting.name === 'Viewer Output Format').effect).toMatch(/fMP4 or MPEGTS/i)
    expect(shadowSettings.find(setting => setting.name === 'Viewer Output Format').location).toMatch(/backend-only/i)
    expect(shadowSettings.find(setting => setting.name === 'Viewer Output Format').location).not.toMatch(/Watched Now/i)
    expect(shadowSettings.find(setting => setting.name === 'Watcher API Key').location).toBe('Shadow Monitor -> Configuration card -> Watcher API Key')
    expect(shadowSettings.find(setting => setting.name === 'Watcher API Key').risk).toMatch(/administrator or primary account key/i)
    expect(shadowSettings.find(setting => setting.name === 'Watcher API Key').risk).toMatch(/separate ordinary playback identity/i)
    expect(shadowSettings.find(setting => setting.name === 'Watcher API Key').effect).toMatch(/never returned/i)
    expect(shadowSettings.find(setting => setting.name === 'Watcher API Key').useWhen).toMatch(/reserved for Shadow/i)
    expect(shadowSettings.find(setting => setting.name === 'Watcher User Agent').defaultValue).toMatch(/TiviMate/i)
    expect(shadowSettings.find(setting => setting.name === 'Watcher User Agent').effect).toMatch(/unique marker/i)
    expect(shadowSettings.find(setting => setting.name === 'Viewer Grace').defaultValue).toBe('5 seconds')
    expect(shadowSettings.find(setting => setting.name === 'Viewer Grace').effect).toMatch(/real viewer disappears/i)
    expect(shadowSettings.find(setting => setting.name === 'Viewer Grace').risk).toMatch(/provider\/profile capacity/i)
    expect(shadowSettings.find(setting => setting.name === 'Healthy Probe Interval').defaultValue).toBe('120 seconds')
    expect(shadowSettings.find(setting => setting.name === 'Healthy Probe Interval').effect).toMatch(/constant extra viewer/i)
    expect(shadowSettings.find(setting => setting.name === 'Healthy Probe Interval').effect).toMatch(/Probe active.*Last probe.*Next probe/i)
    expect(shadowSettings.find(setting => setting.name === 'Dry Run').defaultValue).toBe('Off')
    expect(shadowSettings.find(setting => setting.name === 'Freeze Detection').defaultValue).toBe('On')
    expect(shadowSettings.find(setting => setting.name === 'Garbled Audio').defaultValue).toBe('Off')
    expect(shadowSettings.find(setting => setting.name === 'Silent Audio').defaultValue).toBe('Off')
    expect(shadowSettings.find(setting => setting.name === 'Silent Audio').effect).toMatch(/no usable audio stream/i)
    expect(shadowSettings.find(setting => setting.name === 'Offline Image').defaultValue).toBe('Off')
    expect(shadowSettings.find(setting => setting.name === 'Offline Image').useWhen).toMatch(/do not turn detection on/i)
    expect(shadowSettings.find(setting => setting.name === 'Next Stream Pre-Probe').defaultValue).toBe('Off')
    expect(shadowSettings.find(setting => setting.name === 'Next Stream Pre-Probe').risk).toMatch(/loop-triggered live switches are blocked/i)
    expect(shadowSettings.find(setting => setting.name === 'Configuration revision guard').controlType).toBe('Status/API')
    expect(shadowSettings.find(setting => setting.name === 'Configuration revision guard').defaultValue).toBe('Applied by UI saves')
    expect(shadowSettings.find(setting => setting.name === 'Configuration revision guard').location).toBe('Shadow Monitor -> Save -> "Configuration changed" notice')
    expect(shadowSettings.find(setting => setting.name === 'Configuration revision guard').effect).toMatch(/cannot silently overwrite/i)
    expect(shadowSettings.find(setting => setting.name === 'Configuration revision guard').risk).toMatch(/operator or API client/i)
    expect(shadowSettings.find(setting => setting.name === 'Loop Detection').risk).toMatch(/real viewers/i)
    expect(shadowSettings.find(setting => setting.name === 'Loop Detection').risk).toMatch(/next-stream pre-probe/i)
    expect(shadowSettings.find(setting => setting.name === 'Loop Probe Duration').defaultValue).toBe('360 seconds')
    expect(shadowSettings.find(setting => setting.name === 'Probe Duration').defaultValue).toBe('60 seconds')
    expect(getOperatorHelpDetailTopic('hardware-fallback').settings.map(setting => setting.name)).toContain('CPU Fallback')
    expect(getOperatorHelpDetailTopic('automation-periods').settings.map(setting => setting.name)).toContain('Missed-run grace')
    const troubleshooting = getOperatorHelpDetailTopic('troubleshooting')
    const troubleshootingStatusSettings = troubleshooting.settings.filter(
      setting => setting.name !== 'Analytics history range',
    )
    expect(troubleshootingStatusSettings.map(setting => setting.controlType)).toEqual(
      troubleshootingStatusSettings.map(() => 'Status/API'),
    )
    expect(troubleshooting.settings.map(setting => setting.name)).toEqual(expect.arrayContaining([
      'Startup readiness',
      'Stream Checker status',
      'Previous progress hidden',
      'Dispatcharr status notice',
      'Dashboard and Changelog counters',
      'Analytics history range',
      'Quality reason details',
      'Hardware status',
      'Teamarr Preflight status',
      'Shadow Monitor status',
      'Changelog and logs',
    ]))
    expect(troubleshooting.settings.find(setting => setting.name === 'Quality reason details').effect).toMatch(/timeout, connectivity, endpoint/i)
    expect(troubleshooting.settings.find(setting => setting.name === 'Previous progress hidden').effect).toMatch(/not a failed stream check/i)
    expect(troubleshooting.settings.find(setting => setting.name === 'Dispatcharr status notice').effect).toMatch(/not automatically a quality-check failure/i)
    expect(troubleshooting.settings.find(setting => setting.name === 'Dashboard and Changelog counters').effect).toMatch(/Channels Restored/)
    expect(troubleshooting.settings.find(setting => setting.name === 'Dashboard and Changelog counters').effect).toMatch(/not the total number of visible channels/)
    expect(troubleshooting.settings.find(setting => setting.name === 'Analytics history range').location).toBe('Analytics -> System Analytics header -> Date Range')
    expect(troubleshooting.settings.find(setting => setting.name === 'Analytics history range').controlType).toBe('Visible UI setting')
    expect(troubleshooting.settings.find(setting => setting.name === 'Analytics history range').effect).toMatch(/retained seven-day telemetry history/i)
    expect(troubleshooting.settings.find(setting => setting.name === 'Quality reason details').effect).toMatch(/elapsed\/limit/i)
    expect(troubleshooting.settings.find(setting => setting.name === 'Quality reason details').useWhen).toMatch(/timeout values/)
    const needsRecheck = troubleshooting.settings.find(setting => setting.name === 'Needs Recheck bitrate status')
    expect(needsRecheck.location).toBe('Stream Checker -> Current Progress (active run) -> Stream Progress Tracking -> Status -> Needs Recheck')
    expect(needsRecheck.effect).toMatch(/serial bitrate recheck after the channel initial probes finish/i)
    const needsRecheckHistory = troubleshooting.settings.find(setting => setting.name === 'Needs Recheck bitrate history')
    expect(needsRecheckHistory.location).toMatch(/Action filter: Automation Runs.*Analyzed Streams -> Reason/i)
    expect(needsRecheckHistory.locationTo).toBe('/changelog')
    expect(troubleshooting.settings.find(setting => setting.name === 'Changelog and logs').effect).toMatch(/full run or only dead\/blank\/freeze\/failed/i)
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
      'teamarr-preflight:Post-Start Checks',
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
