import { describe, expect, it } from 'vitest'

import { getShadowMonitorDisplayState } from './shadow-monitor-status.js'

describe('shadow monitor display state', () => {
  it('keeps unsaved form enablement separate from backend service state', () => {
    const state = getShadowMonitorDisplayState({
      status: { enabled: false, running: false },
      config: { enabled: false, has_watcher_api_key: true },
      editedConfig: { enabled: true },
    })

    expect(state.formEnabled).toBe(true)
    expect(state.serviceEnabled).toBe(false)
    expect(state.running).toBe(false)
    expect(state.serviceDescription).toBe('Disabled')
    expect(state.canUseWatcher).toBe(false)
  })

  it('treats a running backend with disabled config as stale instead of healthy running', () => {
    const state = getShadowMonitorDisplayState({
      status: { enabled: false, running: true },
      config: { enabled: false, has_watcher_api_key: true },
      editedConfig: { enabled: false },
    })

    expect(state.backendRunning).toBe(true)
    expect(state.running).toBe(false)
    expect(state.staleRunning).toBe(true)
    expect(state.serviceLabel).toBe('Stopped')
    expect(state.canStopWatcher).toBe(true)
  })

  it('uses backend loop status ahead of unsaved form values', () => {
    const state = getShadowMonitorDisplayState({
      status: {
        enabled: true,
        running: true,
        dry_run: false,
        watch_mode: 'periodic',
        loop_detection_enabled: true,
        loop_probe_duration_seconds: 180,
      },
      config: { has_watcher_api_key: true },
      editedConfig: {
        dry_run: true,
        watch_mode: 'continuous',
        loop_detection_enabled: false,
        loop_probe_duration_seconds: 60,
      },
    })

    expect(state.running).toBe(true)
    expect(state.serviceDryRun).toBe(false)
    expect(state.watchMode).toBe('periodic')
    expect(state.loopDetectionEnabled).toBe(true)
    expect(state.loopProbeDurationSeconds).toBe(180)
  })

  it('requires watcher configuration before start or scan actions', () => {
    const state = getShadowMonitorDisplayState({
      status: { enabled: true, running: false, configuration_required: true },
      config: { enabled: true, has_watcher_api_key: false },
      editedConfig: { enabled: true },
      actionLoading: '',
    })

    expect(state.configurationRequired).toBe(true)
    expect(state.canUseWatcher).toBe(false)
    expect(state.serviceLabel).toBe('Setup required')
  })
})
