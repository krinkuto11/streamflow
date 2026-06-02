import { describe, expect, it } from 'vitest'

import { getHardwareAnalysisPathDisplay } from './hardware-status-display'

describe('getHardwareAnalysisPathDisplay', () => {
  it('describes CPU-only analysis when hardware acceleration is disabled', () => {
    expect(getHardwareAnalysisPathDisplay({
      config: { enabled: false, mode: 'auto', allow_fallback: true },
      mode_supported: true,
    })).toMatchObject({
      label: 'CPU only',
      variant: 'secondary',
    })
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
    expect(getHardwareAnalysisPathDisplay({
      config: { enabled: true, mode: 'vaapi', allow_fallback: true },
      mode_supported: false,
    })).toMatchObject({
      label: 'Fallback ready',
      variant: 'secondary',
      description: expect.stringContaining('VAAPI is not reported available'),
    })
  })
})
