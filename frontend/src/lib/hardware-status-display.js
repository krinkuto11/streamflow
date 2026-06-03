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

export function getHardwareOperatorNote(hardwareStatus) {
  const config = hardwareStatus?.config || {}
  const enabled = config.enabled === true
  const mode = config.mode || 'auto'
  const fallbackEnabled = config.allow_fallback !== false
  const modeSupported = hardwareStatus?.mode_supported === true

  if (!enabled) {
    return {
      variant: 'default',
      title: 'CPU Analysis Active',
      description: 'Hardware acceleration is off; checks run on CPU even if a GPU is visible.',
    }
  }

  if (modeSupported && fallbackEnabled) {
    return {
      variant: 'default',
      title: 'Hardware Preferred With CPU Fallback',
      description: `Stream analysis tries ${formatMode(mode)} first and retries on CPU if ffmpeg rejects hardware init.`,
    }
  }

  if (modeSupported) {
    return {
      variant: 'default',
      title: 'Hardware Only',
      description: `${formatMode(mode)} is available, but failed hardware init will not retry on CPU.`,
    }
  }

  if (fallbackEnabled) {
    return {
      variant: 'default',
      title: 'CPU Fallback Ready',
      description: `${formatMode(mode)} is not reported by ffmpeg; checks can continue on CPU while the container GPU path is adjusted.`,
    }
  }

  return {
    variant: 'destructive',
    title: 'Hardware Not Ready',
    description: `${formatMode(mode)} is not reported by ffmpeg and CPU fallback is disabled.`,
  }
}
