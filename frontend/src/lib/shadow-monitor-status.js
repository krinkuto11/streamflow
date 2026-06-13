export const getShadowMonitorDisplayState = ({
  status = {},
  config = {},
  editedConfig = {},
  actionLoading = '',
} = {}) => {
  const serviceEnabled = Boolean(status?.enabled ?? config?.enabled)
  const backendRunning = Boolean(status?.running)
  const running = backendRunning && serviceEnabled
  const formEnabled = Boolean(editedConfig?.enabled)
  const formDryRun = Boolean(editedConfig?.dry_run)
  const serviceDryRun = Boolean(status?.dry_run ?? config?.dry_run ?? formDryRun)
  const watchMode = status?.watch_mode || config?.watch_mode || editedConfig?.watch_mode || 'continuous'
  const loopDetectionEnabled = Boolean(
    status?.loop_detection_enabled
      ?? config?.loop_detection_enabled
      ?? editedConfig?.loop_detection_enabled,
  )
  const loopProbeDurationSeconds = Number(
    status?.loop_probe_duration_seconds
      ?? config?.loop_probe_duration_seconds
      ?? editedConfig?.loop_probe_duration_seconds
      ?? 120,
  )
  const loopDetectionGates = status?.loop_detection_gates || {}
  const nextStreamPreProbeEnabled = Boolean(
    loopDetectionGates?.next_stream_pre_probe_enabled
      ?? config?.next_stream_pre_probe_enabled
      ?? editedConfig?.next_stream_pre_probe_enabled,
  )
  const loopSwitchRequiresPreProbe = Boolean(
    loopDetectionGates?.next_stream_pre_probe_required
      ?? status?.loop_switch_requires_pre_probe
      ?? true,
  )
  const loopSwitchGateSatisfied = Boolean(
    loopDetectionGates?.switch_gate_satisfied
      ?? status?.loop_switch_gate_satisfied
      ?? (!loopDetectionEnabled || !loopSwitchRequiresPreProbe || nextStreamPreProbeEnabled),
  )
  const hasKey = Boolean(config?.has_watcher_api_key ?? status?.has_watcher_api_key)
  const configurationRequired = Boolean(status?.configuration_required) || !hasKey
  const configurationMessage = status?.configuration_message || 'Save a Watcher API Key before starting the monitor.'
  const canUseWatcher = actionLoading === '' && !configurationRequired && serviceEnabled
  const canStopWatcher = actionLoading === '' && backendRunning
  const continuousWatcherActive = running && watchMode === 'continuous'
  const staleRunning = backendRunning && !serviceEnabled

  return {
    backendRunning,
    running,
    serviceEnabled,
    formEnabled,
    formDryRun,
    serviceDryRun,
    watchMode,
    loopDetectionEnabled,
    loopProbeDurationSeconds: Number.isFinite(loopProbeDurationSeconds)
      ? loopProbeDurationSeconds
      : 120,
    nextStreamPreProbeEnabled,
    loopSwitchRequiresPreProbe,
    loopSwitchGateSatisfied,
    hasKey,
    configurationRequired,
    configurationMessage,
    canUseWatcher,
    canStopWatcher,
    continuousWatcherActive,
    staleRunning,
    serviceLabel: configurationRequired ? 'Setup required' : running ? 'Running' : 'Stopped',
    serviceDescription: configurationRequired
      ? configurationMessage
      : serviceEnabled ? 'Enabled' : 'Disabled',
  }
}

const hasOwn = (value, key) => Object.prototype.hasOwnProperty.call(value || {}, key)

const statusServiceFields = [
  ['enabled', status => hasOwn(status, 'enabled') ? Boolean(status.enabled) : undefined],
  ['dry_run', status => hasOwn(status, 'dry_run') ? Boolean(status.dry_run) : undefined],
  ['watch_mode', status => status?.watch_mode || undefined],
  [
    'loop_detection_enabled',
    status => hasOwn(status, 'loop_detection_enabled') ? Boolean(status.loop_detection_enabled) : undefined,
  ],
  [
    'loop_probe_duration_seconds',
    status => Number.isFinite(Number(status?.loop_probe_duration_seconds))
      ? Number(status.loop_probe_duration_seconds)
      : undefined,
  ],
  [
    'next_stream_pre_probe_enabled',
    status => hasOwn(status?.loop_detection_gates, 'next_stream_pre_probe_enabled')
      ? Boolean(status.loop_detection_gates.next_stream_pre_probe_enabled)
      : undefined,
  ],
]

export const syncShadowMonitorConfigFromStatus = ({
  config = {},
  editedConfig = {},
  status = {},
  dirtyFields = [],
} = {}) => {
  const dirty = dirtyFields instanceof Set ? dirtyFields : new Set(dirtyFields || [])
  let nextConfig = config || {}
  let nextEditedConfig = editedConfig || {}

  const ensureConfigCopy = () => {
    if (nextConfig === config) nextConfig = { ...nextConfig }
  }

  const ensureEditedCopy = () => {
    if (nextEditedConfig === editedConfig) nextEditedConfig = { ...nextEditedConfig }
  }

  statusServiceFields.forEach(([field, pickValue]) => {
    const value = pickValue(status)
    if (value === undefined) return

    if (nextConfig?.[field] !== value) {
      ensureConfigCopy()
      nextConfig[field] = value
    }

    if (!dirty.has(field) && nextEditedConfig?.[field] !== value) {
      ensureEditedCopy()
      nextEditedConfig[field] = value
    }
  })

  return {
    config: nextConfig,
    editedConfig: nextEditedConfig,
    changedConfig: nextConfig !== config,
    changedEditedConfig: nextEditedConfig !== editedConfig,
  }
}
