import { describe, expect, it } from 'vitest'
import {
  getChangelogRunContextBadges,
  getChangelogStaleWarnings,
  getChangelogVisibilityEvents,
  getChangelogVisibilityMetrics,
} from './changelog-run-summary.js'

describe('changelog run summary', () => {
  it('derives V7 context badges from the run snapshot', () => {
    const badges = getChangelogRunContextBadges({
      run_snapshot: {
        run_mode: 'manual_period_run',
        effective_profiles: [
          { profile_name: 'Prime Profile' },
        ],
        quality_rules: [
          { profile_name: 'Strict Quality', enabled: true },
        ],
        capacity_profile_context: {
          type: 'provider_account_profiles',
        },
      },
    })

    expect(badges).toEqual([
      { key: 'run-mode', label: 'Run Mode', value: 'Manual Period Run' },
      { key: 'run-profile', label: 'Run Profile', value: 'Prime Profile' },
      { key: 'quality-rules', label: 'Quality Rules', value: 'Strict Quality' },
      { key: 'capacity-profile', label: 'Capacity Profile', value: 'Provider Account Profiles' },
    ])
  })

  it('summarizes multi-profile and multi-rule automation runs compactly', () => {
    const badges = getChangelogRunContextBadges({
      run_snapshot: {
        run_mode: 'scheduler_run',
        effective_profiles: [
          { profile_name: 'Morning' },
          { profile_name: 'Evening' },
        ],
        quality_rules: [
          { profile_name: 'Morning', enabled: true },
          { profile_name: 'Evening', enabled: false },
        ],
      },
    })

    expect(badges).toEqual([
      { key: 'run-mode', label: 'Run Mode', value: 'Scheduler Run' },
      { key: 'run-profile', label: 'Run Profile', value: '2 profiles' },
      { key: 'quality-rules', label: 'Quality Rules', value: '1/2 enabled' },
    ])
  })

  it('uses explicit detail fields before snapshot fallbacks', () => {
    const badges = getChangelogRunContextBadges({
      run_mode: 'teamarr_preflight',
      run_profile_name: 'Event Profile',
      quality_profile_name: 'Event Quality',
      capacity_profile_name: 'Event Capacity',
      run_snapshot: {
        run_mode: 'scheduler_run',
        effective_profiles: [{ profile_name: 'Scheduled Profile' }],
        quality_rules: [{ profile_name: 'Scheduled Quality', enabled: true }],
        capacity_profile_context: { type: 'provider_account_profiles' },
      },
    })

    expect(badges).toEqual([
      { key: 'run-mode', label: 'Run Mode', value: 'Teamarr Preflight' },
      { key: 'run-profile', label: 'Run Profile', value: 'Event Profile' },
      { key: 'quality-rules', label: 'Quality Rules', value: 'Event Quality' },
      { key: 'capacity-profile', label: 'Capacity Profile', value: 'Event Capacity' },
    ])
  })

  it('keeps old changelog runs without V7 fields quiet', () => {
    expect(getChangelogRunContextBadges({ total_channels: 3 })).toEqual([])
    expect(getChangelogStaleWarnings({ total_channels: 3 })).toEqual([])
    expect(getChangelogVisibilityMetrics({ total_channels: 3 })).toEqual([])
  })

  it('derives stale warning badges from V7 run snapshots', () => {
    expect(getChangelogStaleWarnings({
      run_snapshot: {
        stale_warnings: [
          {
            type: 'dispatcharr_status_risk',
            count: 2,
            read_only: true,
          },
        ],
      },
    })).toEqual([
      {
        key: 'stale-warning-dispatcharr_status_risk',
        label: 'Dispatcharr Provider Sync',
        value: '2 provider status notes / observed only',
      },
    ])
  })

  it('keeps stale progress changelog badges neutral', () => {
    expect(getChangelogStaleWarnings({
      run_snapshot: {
        stale_warnings: [
          {
            type: 'progress_stale',
            status: 'idle_batch_progress',
          },
        ],
      },
    })).toEqual([
      {
        key: 'stale-warning-progress_stale',
        label: 'Previous Progress',
        value: 'Idle Batch Progress',
      },
    ])
  })

  it('falls back to dispatcharr stale summary when warning records are absent', () => {
    expect(getChangelogStaleWarnings({
      run_snapshot: {
        dispatcharr_status: {
          stale_status: {
            status: 'stale_risk',
            stale_status_suspected: true,
            stale_suspected_count: 1,
            read_only: true,
          },
        },
      },
    })).toEqual([
      {
        key: 'stale-warning-dispatcharr_status_risk',
        label: 'Dispatcharr Provider Sync',
        value: '1 provider status note / observed only',
      },
    ])
  })

  it('keeps zero hide and ready counts visible when the run recorded them', () => {
    expect(getChangelogVisibilityMetrics({
      channels_hidden: 0,
      channels_ready: 0,
    })).toEqual([
      { key: 'channels-hidden', label: 'Channels Hidden', value: 0, className: 'text-amber-500' },
      { key: 'channels-ready', label: 'Channels Restored', value: 0, className: 'text-green-500' },
    ])
  })

  it('maps visibility events back to channel names from automation periods', () => {
    expect(getChangelogVisibilityEvents({
      channel_visibility_events: [
        {
          action: 'hidden',
          channel_id: 8092,
          reason: 'all_failed',
          details: { good_streams_count: 0, dead_streams_count: 2, total_streams: 2 },
        },
        {
          action: 'unhidden',
          channel_id: 2154,
          reason: 'recovered',
          details: { good_streams_count: 1, dead_streams_count: 0, total_streams: 8 },
        },
      ],
      periods: [
        {
          channels: [
            { channel_id: 8092, channel_name: 'BBC News', logo_url: '/logos/bbc.png' },
            { channel_id: 2154, channel_name: 'TalkTV' },
          ],
        },
      ],
    })).toEqual([
      {
        key: 'hidden-8092',
        action: 'hidden',
        channel_id: '8092',
        channel_ref: null,
        channel_name: 'BBC News',
        logo_url: '/logos/bbc.png',
        logo_id: null,
        reason: 'all_failed',
        good_streams_count: 0,
        dead_streams_count: 2,
        total_streams: 2,
      },
      {
        key: 'unhidden-2154',
        action: 'unhidden',
        channel_id: '2154',
        channel_ref: null,
        channel_name: 'TalkTV',
        logo_url: null,
        logo_id: null,
        reason: 'recovered',
        good_streams_count: 1,
        dead_streams_count: 0,
        total_streams: 8,
      },
    ])
  })
})
