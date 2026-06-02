const formatMode = (mode) => String(mode || 'auto').toUpperCase()

export function getHardwareAnalysisPathDisplay(hardwareStatus) {
  const config = hardwareStatus?.config || {}
  const enabled = config.enabled === true
  const mode = config.mode || 'auto'
  const fallbackEnabled = config.allow_fallback !== false
  const modeSupported = hardwareStatus?.mode_supported === true

  if (!enabled) {
    return {
      label: 'CPU only',
      variant: 'secondary',
      description: 'Hardware acceleration is disabled; stream analysis uses CPU probes.',
    }
  }

  if (modeSupported && fallbackEnabled) {
    return {
      label: 'Hardware preferred',
      variant: 'default',
      description: `${formatMode(mode)} is available; CPU fallback stays enabled for hardware init failures.`,
    }
  }

  if (modeSupported) {
    return {
      label: 'Hardware only',
      variant: 'secondary',
      description: `${formatMode(mode)} is available, but CPU fallback is disabled.`,
    }
  }

  if (fallbackEnabled) {
    return {
      label: 'Fallback ready',
      variant: 'secondary',
      description: `${formatMode(mode)} is not reported available; hardware init failures retry on CPU.`,
    }
  }

  return {
    label: 'Hardware risk',
    variant: 'destructive',
    description: `${formatMode(mode)} is not reported available and CPU fallback is disabled.`,
  }
}
