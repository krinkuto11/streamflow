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
        status: 'Viewer active',
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

  it('describes dashboard worker capacity when configured', () => {
    expect(getCheckerConcurrencyDisplay({ parallel: { max_workers: 6 } })).toEqual({
      text: '6 Workers',
      active: true,
    })
  })
})
