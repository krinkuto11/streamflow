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
