const formatMode = (mode) => String(mode || 'auto').toUpperCase()

export function getHardwareRuntimeDeviceLabel(hardwareStatus) {
  const detectedGpuCount = Number.isFinite(Number(hardwareStatus?.nvidia_gpu_count))
    ? Number(hardwareStatus?.nvidia_gpu_count)
    : 0
  const driMethodsLabel = Array.isArray(hardwareStatus?.dri_hwaccels) && hardwareStatus.dri_hwaccels.length > 0
    ? hardwareStatus.dri_hwaccels.join(', ')
    : ''

  if (detectedGpuCount > 0) {
    return `${detectedGpuCount} NVIDIA detected`
  }

  if (hardwareStatus?.dri_available) {
    return `DRI methods reported${driMethodsLabel ? ` (${driMethodsLabel})` : ''}`
  }

  if (hardwareStatus?.nvidia_checked) {
    return 'No NVIDIA GPU reported'
  }

  if (hardwareStatus?.config?.enabled) {
    return 'FFmpeg methods only'
  }

  return 'Not checked'
}

export function getHardwareAnalysisPathDisplay(hardwareStatus) {
  const config = hardwareStatus?.config || {}
  const enabled = config.enabled === true
  const mode = config.mode || 'auto'
  const fallbackEnabled = config.allow_fallback !== false
  const modeSupported = hardwareStatus?.mode_supported === true
  const qsvOnDri = enabled && mode === 'qsv' && hardwareStatus?.dri_available

  if (!enabled) {
    return {
      label: 'CPU only',
      variant: 'secondary',
      description: 'Hardware acceleration is disabled; stream analysis uses CPU probes.',
    }
  }

  if (qsvOnDri && !fallbackEnabled) {
    return {
      label: 'Hardware risk',
      variant: 'destructive',
      description: 'QSV is reported by FFmpeg, but DRI hosts may still fail device init; Auto or VAAPI is safer when CPU fallback is disabled.',
    }
  }

  if (qsvOnDri) {
    return {
      label: 'QSV reported',
      variant: 'secondary',
      description: 'QSV is reported by FFmpeg, but device init is only proven by a probe; Auto or VAAPI is usually the safer DRI path.',
    }
  }

  if (modeSupported && fallbackEnabled) {
    return {
      label: 'Hardware preferred',
      variant: 'default',
      description: `${formatMode(mode)} is reported by FFmpeg; CPU fallback stays enabled for hardware init failures.`,
    }
  }

  if (modeSupported) {
    return {
      label: 'Hardware only',
      variant: 'secondary',
      description: `${formatMode(mode)} is reported by FFmpeg, but CPU fallback is disabled.`,
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
  const qsvOnDri = enabled && mode === 'qsv' && hardwareStatus?.dri_available

  if (!enabled) {
    return {
      variant: 'default',
      title: 'CPU Analysis Active',
      description: 'Hardware acceleration is off; checks run on CPU even if a hardware device is visible.',
    }
  }

  if (qsvOnDri && !fallbackEnabled) {
    return {
      variant: 'destructive',
      title: 'QSV Init Risk',
      description: 'FFmpeg reports QSV, but that does not guarantee the DRI render node can initialize it. Use Auto/VAAPI or enable CPU fallback while testing QSV.',
    }
  }

  if (qsvOnDri) {
    return {
      variant: 'default',
      title: 'QSV Reported, Validate With Probe',
      description: 'QSV is visible in FFmpeg methods, but VAAPI is the safer DRI default unless a blank/freeze probe proves QSV initializes on this host.',
    }
  }

  if (modeSupported && fallbackEnabled) {
    return {
      variant: 'default',
      title: 'Hardware Preferred With CPU Fallback',
      description: `Stream analysis tries reported ${formatMode(mode)} first and retries on CPU if ffmpeg rejects hardware init.`,
    }
  }

  if (modeSupported) {
    return {
      variant: 'default',
      title: 'Hardware Only',
      description: `${formatMode(mode)} is reported by FFmpeg, but failed hardware init will not retry on CPU.`,
    }
  }

  if (fallbackEnabled) {
    return {
      variant: 'default',
      title: 'CPU Fallback Ready',
      description: `${formatMode(mode)} is not reported by ffmpeg; checks can continue on CPU while the hardware path is adjusted.`,
    }
  }

  return {
    variant: 'destructive',
    title: 'Hardware Not Ready',
    description: `${formatMode(mode)} is not reported by ffmpeg and CPU fallback is disabled.`,
  }
}
