import { describe, expect, it } from 'vitest'

import {
  getHardwareAnalysisPathDisplay,
  getHardwareOperatorNote,
  getHardwareRuntimeDeviceLabel,
} from './hardware-status-display'

describe('getHardwareRuntimeDeviceLabel', () => {
  it('reports visible NVIDIA devices when the runtime exposes them', () => {
    expect(getHardwareRuntimeDeviceLabel({ nvidia_gpu_count: 1 })).toBe('1 NVIDIA detected')
    expect(getHardwareRuntimeDeviceLabel({ nvidia_gpu_count: 2 })).toBe('2 NVIDIA detected')
  })

  it('reports DRI methods for Intel or VAAPI/QSV paths without requiring NVIDIA', () => {
    expect(getHardwareRuntimeDeviceLabel({
      dri_available: true,
      dri_hwaccels: ['drm', 'qsv', 'vaapi'],
      nvidia_checked: false,
      nvidia_gpu_count: 0,
    })).toBe('DRI/VAAPI/QSV reported (drm, qsv, vaapi)')
  })

  it('keeps NVIDIA absence specific when NVIDIA was explicitly checked', () => {
    expect(getHardwareRuntimeDeviceLabel({
      nvidia_checked: true,
      nvidia_gpu_count: 0,
      dri_available: false,
    })).toBe('No NVIDIA GPU reported')
  })

  it('falls back to generic ffmpeg methods when hardware is enabled but no runtime path is reported', () => {
    expect(getHardwareRuntimeDeviceLabel({
      config: { enabled: true },
      nvidia_checked: false,
      nvidia_gpu_count: 0,
      dri_available: false,
    })).toBe('FFmpeg methods only')
  })
})

describe('getHardwareAnalysisPathDisplay', () => {
  it('describes CPU-only analysis when hardware acceleration is disabled', () => {
    const display = getHardwareAnalysisPathDisplay({
      config: { enabled: false, mode: 'auto', allow_fallback: true },
      mode_supported: true,
    })
    const note = getHardwareOperatorNote({
      config: { enabled: false, mode: 'auto', allow_fallback: true },
      mode_supported: true,
    })

    expect(display).toMatchObject({
      label: 'CPU only',
      variant: 'secondary',
    })
    expect(note.description).toContain('hardware device')
    expect(note.description).not.toContain('GPU')
  })

  it('describes supported hardware with fallback as preferred hardware', () => {
    expect(getHardwareAnalysisPathDisplay({
      config: { enabled: true, mode: 'cuda', allow_fallback: true },
      mode_supported: true,
    })).toMatchObject({
      label: 'Hardware preferred',
      variant: 'default',
      description: expect.stringContaining('CUDA is available'),
    })
  })

  it('warns when unsupported hardware has no CPU fallback', () => {
    expect(getHardwareAnalysisPathDisplay({
      config: { enabled: true, mode: 'cuda', allow_fallback: false },
      mode_supported: false,
    })).toMatchObject({
      label: 'Hardware risk',
      variant: 'destructive',
    })
  })

  it('describes unsupported hardware with fallback as fallback ready', () => {
    const display = getHardwareAnalysisPathDisplay({
      config: { enabled: true, mode: 'vaapi', allow_fallback: true },
      mode_supported: false,
    })
    const note = getHardwareOperatorNote({
      config: { enabled: true, mode: 'vaapi', allow_fallback: true },
      mode_supported: false,
    })

    expect(display).toMatchObject({
      label: 'Fallback ready',
      variant: 'secondary',
      description: expect.stringContaining('VAAPI is not reported available'),
    })
    expect(note.description).toContain('hardware path')
    expect(note.description).not.toContain(['G', 'PU path'].join(''))
  })

  it('summarizes the live hardware path with fallback for operators', () => {
    expect(getHardwareOperatorNote({
      config: { enabled: true, mode: 'auto', allow_fallback: true },
      mode_supported: true,
    })).toMatchObject({
      title: 'Hardware Preferred With CPU Fallback',
      variant: 'default',
      description: expect.stringContaining('AUTO first'),
    })
  })

  it('warns operators when hardware is not ready and fallback is disabled', () => {
    expect(getHardwareOperatorNote({
      config: { enabled: true, mode: 'cuda', allow_fallback: false },
      mode_supported: false,
    })).toMatchObject({
      title: 'Hardware Not Ready',
      variant: 'destructive',
      description: expect.stringContaining('CPU fallback is disabled'),
    })
  })
})
