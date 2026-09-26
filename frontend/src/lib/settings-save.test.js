import { describe, expect, it, vi } from 'vitest'
import { saveSettingsSection } from './settings-save.js'

function clients() {
  return {
    automation: { updateConfig: vi.fn().mockResolvedValue({}) },
    scheduling: { updateConfig: vi.fn().mockResolvedValue({}) },
    monitoring: { updateSettings: vi.fn().mockResolvedValue({}) },
    connection: { updateConfig: vi.fn().mockResolvedValue({}) },
  }
}

const values = {
  automation: { run_all_due_periods: true },
  scheduling: { enabled: true },
  monitoring: { review_duration: 60 },
  connection: { base_url: 'http://dispatcharr' },
}

describe('settings saves', () => {
  it('writes only the settings shown on the scheduling tab', async () => {
    const apis = clients()
    expect(await saveSettingsSection('scheduling', values, apis)).toEqual({
      saved: ['automation', 'scheduling'],
      failed: [],
    })
    expect(apis.automation.updateConfig).toHaveBeenCalledWith(values.automation)
    expect(apis.scheduling.updateConfig).toHaveBeenCalledWith(values.scheduling)
    expect(apis.monitoring.updateSettings).not.toHaveBeenCalled()
    expect(apis.connection.updateConfig).not.toHaveBeenCalled()
  })

  it('reports a partial save without claiming all settings succeeded', async () => {
    const apis = clients()
    apis.scheduling.updateConfig.mockRejectedValue(new Error('offline'))
    expect(await saveSettingsSection('scheduling', values, apis)).toEqual({
      saved: ['automation'],
      failed: ['scheduling'],
    })
  })

  it('keeps monitoring and connection saves separate', async () => {
    const apis = clients()
    expect(await saveSettingsSection('monitoring', values, apis)).toEqual({
      saved: ['monitoring'],
      failed: [],
    })
    expect(apis.connection.updateConfig).not.toHaveBeenCalled()
    expect(await saveSettingsSection('connection', values, apis)).toEqual({
      saved: ['connection'],
      failed: [],
    })
    expect(apis.monitoring.updateSettings).toHaveBeenCalledTimes(1)
    expect(apis.connection.updateConfig).toHaveBeenCalledTimes(1)
  })
})
