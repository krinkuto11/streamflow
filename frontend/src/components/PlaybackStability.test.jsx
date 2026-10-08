import React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'
import { PlaybackStabilityHistory, PlaybackStabilityScoring } from './PlaybackStability.jsx'

describe('Playback Stability controls and evidence', () => {
  it('keeps profile scoring off by default and gates it on saved recording settings', () => {
    const html = renderToStaticMarkup(<PlaybackStabilityScoring weights={{}} onChange={() => {}} recordingEnabled={false} />)
    expect(html).toContain('data-state="unchecked"')
    expect(html).toContain('disabled=""')
    expect(html).toContain('Enable Record Playback Stability in Settings')
    expect(html).toContain('0.15 limits the deduction to 15%')
  })
  it('preserves an enabled profile setting when recording is temporarily disabled', () => {
    const html = renderToStaticMarkup(<PlaybackStabilityScoring weights={{ use_playback_stability: true }} onChange={() => {}} recordingEnabled={false} />)
    expect(html).toContain('data-state="checked"')
    expect(html).toContain('disabled=""')
  })
  it('does not silently enable profile scoring when recording is enabled', () => {
    const html = renderToStaticMarkup(<PlaybackStabilityScoring weights={{}} onChange={() => {}} recordingEnabled />)
    expect(html).toContain('data-state="unchecked"')
    expect(html).toContain('Streams without sufficient history keep their existing quality score')
  })
  it('reports settings failures instead of presenting failed loads as disabled settings', () => {
    const html = renderToStaticMarkup(<PlaybackStabilityScoring weights={{}} onChange={() => {}} recordingEnabled={false} loadError />)
    expect(html).toContain('Recording settings could not load')
    expect(html).toContain('disabled=""')
  })
  it('never shows an unobserved source as zero percent stability', () => {
    const html = renderToStaticMarkup(<PlaybackStabilityHistory status={{ enabled: true, recording: true, active_playbacks: 1,
      streams: [{ stream_id: 1, stream_name: 'Sample', observed_seconds: 60, stalled_seconds: 0, failovers: 0, score: null, eligible: false }] }} />)
    expect(html).toContain('Not enough data')
    expect(html).not.toContain('0%')
    expect(html).toContain('Sample')
  })
  it('shows measured zero percent distinctly from missing data', () => {
    const html = renderToStaticMarkup(<PlaybackStabilityHistory status={{ enabled: true, recording: true, active_playbacks: 1,
      streams: [{ stream_id: 1, observed_seconds: 600, stalled_seconds: 600, failovers: 1, score: 0, eligible: true }] }} />)
    expect(html).toContain('0%')
    expect(html).not.toContain('Not enough data')
  })
  it('shows disabled collection and unavailable playback data clearly', () => {
    expect(renderToStaticMarkup(<PlaybackStabilityHistory status={{ enabled: false }} />)).toContain('Recording is off')
    expect(renderToStaticMarkup(<PlaybackStabilityHistory status={{ enabled: true, error: 'Playback data unavailable', streams: [] }} />)).toContain('role="alert"')
  })
})
