import { describe, it, expect } from 'vitest'
import { selectParallelCheckProgress } from './parallel-check-progress.js'

describe('parallel check progress selection', () => {
  const first = { channel_id: 1, step: 'Loop testing' }
  const second = { channel_id: 2, step: 'Analyzing streams' }
  const runs = [{ channel_id: 1, progress: first }, { channel_id: 2, progress: second }]

  it('shows the selected game instead of another game or an old manual check', () => {
    expect(selectParallelCheckProgress(runs, { channel_id: 9 }, '2')).toEqual(second)
  })
  it('moves to a remaining game when the selected one finishes', () => {
    expect(selectParallelCheckProgress([runs[1]], first, '1')).toEqual(second)
  })
  it('does not show unrelated progress while a new game is starting', () => {
    expect(selectParallelCheckProgress([{ channel_id: 1, progress: null }], second, '1')).toBeNull()
  })
  it('preserves ordinary progress when no parallel games are running', () => {
    expect(selectParallelCheckProgress([], first, null)).toEqual(first)
  })
})
