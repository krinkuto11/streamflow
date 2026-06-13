import { describe, expect, it } from 'vitest'

import {
  getShadowMonitorDisplayState,
  syncShadowMonitorConfigFromStatus,
} from './shadow-monitor-status.js'

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
        loop_detection_gates: {
          next_stream_pre_probe_required: true,
          next_stream_pre_probe_enabled: true,
          switch_gate_satisfied: true,
        },
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
    expect(state.loopSwitchRequiresPreProbe).toBe(true)
    expect(state.nextStreamPreProbeEnabled).toBe(true)
    expect(state.loopSwitchGateSatisfied).toBe(true)
  })

  it('marks enabled loop detection as switch-gated until next-stream pre-probe is enabled', () => {
    const state = getShadowMonitorDisplayState({
      status: {
        enabled: true,
        running: true,
        loop_detection_enabled: true,
        loop_detection_gates: {
          next_stream_pre_probe_required: true,
          next_stream_pre_probe_enabled: false,
          switch_gate_satisfied: false,
        },
      },
      config: { has_watcher_api_key: true },
      editedConfig: {
        loop_detection_enabled: true,
        next_stream_pre_probe_enabled: false,
      },
    })

    expect(state.loopDetectionEnabled).toBe(true)
    expect(state.loopSwitchRequiresPreProbe).toBe(true)
    expect(state.nextStreamPreProbeEnabled).toBe(false)
    expect(state.loopSwitchGateSatisfied).toBe(false)
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

  it('syncs service status fields into clean config and form state', () => {
    const config = {
      enabled: false,
      dry_run: true,
      watch_mode: 'periodic',
      loop_detection_enabled: false,
      loop_probe_duration_seconds: 120,
      next_stream_pre_probe_enabled: false,
      confirmation_count: 2,
    }
    const editedConfig = { ...config }

    const result = syncShadowMonitorConfigFromStatus({
      config,
      editedConfig,
      status: {
        enabled: true,
        dry_run: false,
        watch_mode: 'continuous',
        loop_detection_enabled: true,
        loop_probe_duration_seconds: 180,
        loop_detection_gates: {
          next_stream_pre_probe_enabled: true,
        },
      },
    })

    expect(result.config).toMatchObject({
      enabled: true,
      dry_run: false,
      watch_mode: 'continuous',
      loop_detection_enabled: true,
      loop_probe_duration_seconds: 180,
      next_stream_pre_probe_enabled: true,
      confirmation_count: 2,
    })
    expect(result.editedConfig).toMatchObject(result.config)
    expect(result.changedConfig).toBe(true)
    expect(result.changedEditedConfig).toBe(true)
  })

  it('does not overwrite unsaved dirty form fields while syncing saved service state', () => {
    const result = syncShadowMonitorConfigFromStatus({
      config: {
        enabled: false,
        dry_run: true,
        watch_mode: 'periodic',
      },
      editedConfig: {
        enabled: false,
        dry_run: true,
        watch_mode: 'periodic',
      },
      dirtyFields: new Set(['enabled']),
      status: {
        enabled: true,
        dry_run: false,
        watch_mode: 'continuous',
      },
    })

    expect(result.config).toMatchObject({
      enabled: true,
      dry_run: false,
      watch_mode: 'continuous',
    })
    expect(result.editedConfig).toMatchObject({
      enabled: false,
      dry_run: false,
      watch_mode: 'continuous',
    })
  })
})
