import { describe, expect, it } from 'vitest'

import {
  formatProgressMode,
  getCurrentProgressDisplay,
} from './stream-checker-progress-display.js'

describe('formatProgressMode', () => {
  it('uses explicit V7 labels for known run modes', () => {
    expect(formatProgressMode('manual_full_run')).toBe('Manual Full Run')
    expect(formatProgressMode('single_channel_check')).toBe('Single Channel Check')
    expect(formatProgressMode('teamarr_preflight')).toBe('Teamarr Preflight')
  })

  it('titleizes unknown run modes without hiding them', () => {
    expect(formatProgressMode('custom-event_run')).toBe('Custom Event Run')
  })
})

describe('getCurrentProgressDisplay', () => {
  it('exposes V7 Current Progress context badges while a run is active', () => {
    const display = getCurrentProgressDisplay(
      {
        stream_checking_mode: true,
        queue: { queue_size: 0, in_progress: 1, current_channel: 42 },
      },
      {
        run_mode: 'manual_period_run',
        run_profile_name: 'Prime Time',
        run_profile_source: 'period_assignment',
        quality_profile_name: 'Strict Quality',
        quality_profile_source: 'quality_profile',
        capacity_profile_name: 'Provider account profiles',
      },
    )

    expect(display).toMatchObject({
      isChecking: true,
      showCurrentProgress: true,
      progressRunMode: 'Manual Period Run',
      runProfileName: 'Prime Time',
      runProfileSource: 'period_assignment',
      qualityProfileName: 'Strict Quality',
      qualityProfileSource: 'quality_profile',
      capacityProfileName: 'Provider account profiles',
      showQualityRules: true,
    })
  })

  it('keeps legacy automation profile fallback compact', () => {
    const display = getCurrentProgressDisplay(
      {
        checking: true,
        queue: { queue_size: 0, in_progress: 0, current_channel: null },
      },
      {
        automation_profile_name: 'Legacy Full Check',
        automation_profile_source: 'default',
      },
    )

    expect(display.isChecking).toBe(true)
    expect(display.showCurrentProgress).toBe(true)
    expect(display.runProfileName).toBe('Legacy Full Check')
    expect(display.qualityProfileName).toBe('Legacy Full Check')
    expect(display.showQualityRules).toBe(false)
  })

  it('keeps live single-channel capacity progress visible with idle queue counters', () => {
    const display = getCurrentProgressDisplay(
      {
        stream_checking_mode: true,
        queue: { queue_size: 0, in_progress: 0, current_channel: null },
      },
      {
        run_mode: 'single_channel_check',
        is_single_channel_check: true,
        run_profile_name: 'Teamarr Event Preflight',
        run_profile_source: 'forced',
        quality_profile_name: 'Teamarr Event Preflight',
        quality_profile_source: 'forced',
        capacity_profile_name: 'Provider account profiles',
        capacity_profile_source: 'm3u_account_profiles',
        provider_progress: [
          {
            account_id: 1,
            name: 'Provider account',
            total: 2,
            checking: 1,
          },
        ],
        provider_summary: {
          total_providers: 1,
          checking_streams: 1,
        },
      },
    )

    expect(display).toMatchObject({
      isChecking: true,
      showCurrentProgress: true,
      progressRunMode: 'Single Channel Check',
      runProfileName: 'Teamarr Event Preflight',
      runProfileSource: 'forced',
      qualityProfileName: 'Teamarr Event Preflight',
      qualityProfileSource: 'forced',
      capacityProfileName: 'Provider account profiles',
      showQualityRules: false,
    })
  })

  it('uses status stale details and hides Current Progress for stale payloads', () => {
    const display = getCurrentProgressDisplay(
      {
        progress_stale: true,
        progress_stale_details: { age_seconds: 95 },
        stream_checking_mode: true,
        queue: { queue_size: 4, in_progress: 1, current_channel: 99 },
      },
      {
        stale_age_seconds: 44,
        run_mode: 'single_channel_check',
        run_profile_name: 'Single Check',
      },
    )

    expect(display.progressStale).toBe(true)
    expect(display.progressStaleAge).toBe(95)
    expect(display.isChecking).toBe(false)
    expect(display.statusLabel).toBe('Idle')
    expect(display.staleNoticeTitle).toBe('Previous progress hidden')
    expect(display.staleNoticeText).toContain('inactive checker run')
    expect(display.showCurrentProgress).toBe(false)
    expect(display.progressRunMode).toBe('Single Channel Check')
    expect([
      display.statusLabel,
      display.staleNoticeTitle,
      display.staleNoticeText,
    ].join(' ')).not.toContain('Stale Progress')
  })

  it('falls back to progress stale age when status details are absent', () => {
    const display = getCurrentProgressDisplay(
      {
        queue: { queue_size: 0, in_progress: 0, current_channel: null },
      },
      {
        stale: true,
        stale_age_seconds: 31,
      },
    )

    expect(display.progressStale).toBe(true)
    expect(display.progressStaleAge).toBe(31)
    expect(display.showCurrentProgress).toBe(false)
  })
})
