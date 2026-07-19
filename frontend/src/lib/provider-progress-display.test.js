import { describe, expect, it } from 'vitest'

import {
  getCheckerConcurrencyDisplay,
  getParallelProgressBadgeText,
  getProfileSlotDisplay,
  getProfileSlotMatrixRows,
  getProviderCapacityExplanationDisplay,
  getProviderWaitReasonDisplay,
} from './provider-progress-display'

describe('getProviderWaitReasonDisplay', () => {
  it('uses concise operator wording for checker-owned capacity waits', () => {
    expect(getProviderWaitReasonDisplay({
      dominant_wait_reason: 'checking_capacity',
      wait_reason_counts: { checking_capacity: 3 },
    })).toEqual({
      code: 'checking_capacity',
      text: 'Check slots full (3)',
      title: 'checking_capacity: 3',
    })
  })

  it('distinguishes viewer-owned capacity from provider capacity', () => {
    expect(getProviderWaitReasonDisplay({
      dominant_wait_reason: 'active_viewers',
      wait_reason_counts: { active_viewers: 1 },
    })).toMatchObject({
      code: 'active_viewers',
      text: 'Viewer slots',
    })
  })

  it('falls back to titleized unknown reason codes', () => {
    expect(getProviderWaitReasonDisplay({
      dominant_wait_reason: 'custom_reason',
      wait_reason_counts: { custom_reason: 2 },
    })).toMatchObject({
      code: 'custom_reason',
      text: 'Custom Reason (2)',
    })
  })

  it('returns null without a dominant wait reason', () => {
    expect(getProviderWaitReasonDisplay({})).toBeNull()
  })

  it('formats bounded profile slot usage', () => {
    expect(getProfileSlotDisplay({
      id: 50,
      name: 'Sibling',
      active_viewers: 1,
      checking: 1,
      used: 2,
      limit: 2,
      available: 0,
      full: true,
    })).toMatchObject({
      id: 50,
      text: 'Sibling: 2/2',
      title: 'Sibling, ID 50, 1 viewer, 1 checking, 0 free',
      full: true,
    })
  })

  it('formats unlimited profile slots as open', () => {
    expect(getProfileSlotDisplay({
      name: 'Default',
      unlimited: true,
      checking: 2,
      active_viewers: 0,
    })).toMatchObject({
      text: 'Default: open',
      title: 'Default, 0 viewer, 2 checking, unlimited capacity',
      unlimited: true,
      status: 'Checking',
    })
  })

  it('keeps Shadow watcher slots separate from real viewers', () => {
    expect(getProfileSlotDisplay({
      id: 52,
      name: 'Shadow',
      active_viewers: 1,
      real_viewers: 0,
      shadow_watchers: 1,
      checking: 0,
      used: 1,
      limit: 1,
      available: 0,
      full: false,
    })).toMatchObject({
      text: 'Shadow: 1/1',
      title: 'Shadow, ID 52, 0 real viewer, 1 shadow watcher, 0 checking, 0 free',
      realViewers: 0,
      shadowWatchers: 1,
      status: 'Shadow watcher',
    })
  })

  it('surfaces Teamarr and quality checker slot context', () => {
    expect(getProfileSlotDisplay({
      id: 53,
      name: 'Preflight',
      checking: 1,
      teamarr_preflight: 1,
      quality_checks: 0,
      used: 1,
      limit: 2,
      available: 1,
    })).toMatchObject({
      title: 'Preflight, ID 53, 0 viewer, 1 Teamarr preflight, 1 checking, 1 free',
      teamarrPreflight: 1,
      qualityChecks: 0,
      status: 'Teamarr Preflight',
    })
  })

  it('builds safe profile matrix rows from provider progress', () => {
    expect(getProfileSlotMatrixRows([
      {
        account_id: 7,
        name: 'Provider A',
        profile_slots: [
          {
            id: 70,
            name: 'Main',
            active_viewers: 1,
            real_viewers: 0,
            shadow_watchers: 1,
            checking: 0,
            used: 1,
            limit: 3,
            available: 2,
          },
          {
            id: 71,
            name: 'Backup',
            unlimited: true,
          },
        ],
      },
    ])).toEqual([
      expect.objectContaining({
        key: '7:70',
        accountName: 'Provider A',
        accountId: 7,
        id: 70,
        name: 'Main',
        limitText: '3',
        availableText: '2',
        realViewers: 0,
        shadowWatchers: 1,
        status: 'Shadow watcher',
        capacityCounted: true,
      }),
      expect.objectContaining({
        key: '7:71',
        accountName: 'Provider A',
        id: 71,
        limitText: 'unlimited',
        availableText: 'open',
        status: 'Available',
      }),
    ])
  })

  it('marks shared credential route aliases without hiding their profile rows', () => {
    const rows = getProfileSlotMatrixRows([
      {
        account_id: 7,
        name: 'Provider A',
        profile_slots: [
          {
            id: 70,
            name: 'Representative',
            shared_route: true,
            capacity_counted: true,
          },
          {
            id: 71,
            name: 'Default alias',
            shared_route: true,
            capacity_counted: false,
          },
        ],
      },
    ])

    expect(rows).toHaveLength(2)
    expect(rows[0]).toMatchObject({
      sharedRoute: true,
      capacityCounted: true,
      isSharedRouteAlias: false,
      sharedRouteLabel: 'Shared credential route',
    })
    expect(rows[1]).toMatchObject({
      sharedRoute: true,
      capacityCounted: false,
      isSharedRouteAlias: true,
      sharedRouteLabel: 'Shared credential route alias',
    })
  })

  it('keeps unusable routes visible without presenting capacity as available', () => {
    const rows = getProfileSlotMatrixRows([
      {
        account_id: 7,
        name: 'Provider A',
        profile_slots: [
          {
            id: 72,
            name: 'Invalid route',
            route_usable: false,
            capacity_counted: false,
            unlimited: true,
            used: 0,
            limit: 0,
            available: null,
            full: true,
          },
        ],
      },
    ])

    expect(rows).toHaveLength(1)
    expect(rows[0]).toMatchObject({
      id: 72,
      name: 'Invalid route',
      text: 'Invalid route: N/A',
      full: false,
      unlimited: false,
      capacityCounted: false,
      routeUsable: false,
      routeUnavailable: true,
      routeUnavailableHint: 'This profile cannot provide a usable credential route for StreamFlow checks.',
      status: 'Route unavailable',
      capacityText: 'N/A',
      freeText: 'capacity N/A',
      available: null,
      limitText: 'N/A',
      usageText: 'N/A',
      availableText: 'N/A',
    })
    expect(rows[0].title).toContain('This profile cannot provide a usable credential route')
    expect(rows[0].title).not.toContain('open')
    expect(rows[0].title).not.toContain('free')

    expect(getProfileSlotDisplay({
      route_usable: false,
      capacity_counted: true,
      shared_route: true,
    })).toMatchObject({
      routeUnavailable: true,
      capacityCounted: false,
      sharedRoute: false,
      isSharedRouteAlias: false,
      sharedRouteLabel: null,
      status: 'Route unavailable',
    })
  })

  it('counts missing capacity metadata defensively for older snapshots', () => {
    expect(getProfileSlotDisplay({ shared_route: true })).toMatchObject({
      sharedRoute: true,
      capacityCounted: true,
      isSharedRouteAlias: false,
      sharedRouteLabel: 'Shared credential route',
    })
  })

  it('formats provider capacity explanations with source and action details', () => {
    expect(getProviderCapacityExplanationDisplay({
      capacity_explanation: {
        state: 'viewer_protected',
        message: 'Real viewer capacity is protected before StreamFlow probes use the slot.',
        operator_action: 'wait_for_viewer_capacity',
        primary_reason: 'active_viewers',
        capacity_sources: ['real_viewers', 'provider_profile', 'streamflow_workers'],
        profile_slot_summary: {
          full: 1,
          open: 1,
          with_real_viewers: 1,
          with_streamflow_workers: 1,
        },
      },
    })).toEqual({
      state: 'viewer_protected',
      text: 'Real viewer capacity is protected before StreamFlow probes use the slot.',
      detail: 'Sources: Real viewers, Provider profile, StreamFlow probes | Wait for viewer capacity | Slots: 1 full, 1 open, 1 viewer, 1 probing',
      title: 'Reason: active_viewers | Sources: Real viewers, Provider profile, StreamFlow probes | Wait for viewer capacity | Slots: 1 full, 1 open, 1 viewer, 1 probing',
      sources: ['Real viewers', 'Provider profile', 'StreamFlow probes'],
      action: 'Wait for viewer capacity',
      slotParts: ['1 full', '1 open', '1 viewer', '1 probing'],
    })
  })

  it('formats Shadow and specialized checker capacity sources', () => {
    expect(getProviderCapacityExplanationDisplay({
      waiting: 1,
      capacity_explanation: {
        state: 'shadow_watcher_capacity',
        message: 'A Shadow Monitor watcher is using the provider profile slot without being counted as a real viewer.',
        operator_action: 'wait_for_shadow_watcher',
        primary_reason: 'shadow_watchers',
        capacity_sources: ['shadow_watchers', 'provider_profile', 'teamarr_preflight', 'quality_checks'],
        profile_slot_summary: {
          full: 1,
          open: 0,
          with_shadow_watchers: 1,
          with_streamflow_workers: 2,
          with_teamarr_preflight: 1,
          with_quality_checks: 1,
        },
      },
    })).toEqual(expect.objectContaining({
      state: 'shadow_watcher_capacity',
      sources: ['Shadow watchers', 'Provider profile', 'Teamarr Preflight', 'Quality checks'],
      action: 'Wait for Shadow watcher',
      slotParts: ['1 full', '1 shadow', '1 preflight', '1 quality'],
    }))
  })

  it('returns null for missing capacity explanations', () => {
    expect(getProviderCapacityExplanationDisplay({})).toBeNull()
  })

  it('does not render idle capacity explanations without active pressure', () => {
    expect(getProviderCapacityExplanationDisplay({
      checking: 0,
      waiting: 0,
      skipped: 0,
      capacity_explanation: {
        state: 'idle',
        message: 'No provider capacity wait is active.',
        operator_action: 'none',
        capacity_sources: [],
        profile_slot_summary: {},
      },
    })).toBeNull()
  })

  it('does not render a zero-worker parallel badge while active checks are visible', () => {
    expect(getParallelProgressBadgeText(
      { parallel: { enabled: true, max_workers: 0 } },
      { checking_streams: 4 },
    )).toBe('Parallel (4 active)')
  })

  it('renders configured parallel capacity when available', () => {
    expect(getParallelProgressBadgeText(
      { parallel: { enabled: true, max_workers: 10 } },
      { checking_streams: 4 },
    )).toBe('Parallel (10 workers)')
  })

  it('hides the parallel badge when parallel checking is disabled', () => {
    expect(getParallelProgressBadgeText(
      { parallel: { enabled: false, max_workers: 0 } },
      { checking_streams: 4 },
    )).toBeNull()
  })

  it('describes missing dashboard worker capacity as sequential', () => {
    expect(getCheckerConcurrencyDisplay({ parallel: { max_workers: 0 } })).toEqual({
      text: 'Sequential',
      active: false,
    })
  })

  it('describes disabled parallel mode as sequential despite its configured limit', () => {
    expect(getCheckerConcurrencyDisplay({
      parallel: { enabled: false, max_workers: 10, configured_max_workers: 10 },
    })).toEqual({
      text: 'Sequential',
      active: false,
    })
  })

  it('describes dashboard worker capacity when configured', () => {
    expect(getCheckerConcurrencyDisplay({ parallel: { max_workers: 6 } })).toEqual({
      text: '6 Workers',
      active: true,
    })
  })
})
