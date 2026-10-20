import { describe, expect, it } from 'vitest'
import { preflightAction, preflightOrderLabels, preflightScore, preflightStreamStatus } from './preflight-stream-details.js'

describe('preflight stream decisions', () => {
  it('keeps missing and zero scores distinct', () => {
    expect(preflightScore(0)).toBe('0.00')
    for (const value of [null, undefined, NaN, Infinity, '0.8']) expect(preflightScore(value)).toBe('Not scored')
  })
  it('does not equate channel rank with active playback or the highest score', () => {
    expect(preflightAction({ rank: 1, action: 'retained', score: 0.3 })).toBe('First in channel')
    expect(preflightAction({ rank: 2, action: 'retained', score: 0.9 })).toBe('Channel rank 2')
    expect(preflightAction({ rank: 1, action: 'order_not_applied' })).toBe('Order not applied')
    expect(preflightAction({ action: 'not_retained' })).toBe('Not retained in channel')
    expect(preflightOrderLabels.absolute).toMatch(/^Playlist priority/)
    expect(preflightOrderLabels.score_playlist).toMatch(/^Score/)
  })
  it('keeps skipped, incomplete and unknown checks distinct from dead streams', () => {
    for (const status of ['skipped', 'incomplete', 'unknown']) expect(preflightStreamStatus({ status }).label).not.toBe('Dead')
    expect(preflightStreamStatus({ status: 'dead' }).variant).toBe('destructive')
  })
})
