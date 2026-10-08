import { describe, expect, it } from 'vitest'
import { visibleRowRange } from './virtual-rows.js'

describe('visible stream row ranges', () => {
  it('uses measured heights, overscan, and padding without dropping scroll extent', () => {
    const offsets = [0, 20, 100, 130, 200, 230, 300]
    const result = visibleRowRange(offsets, 105, 25, 1)
    expect(result).toEqual({ start: 1, end: 5, before: 20, after: 70 })
    expect(result.before + offsets[result.end] - offsets[result.start] + result.after).toBe(300)
  })

  it('clamps empty lists and positions beyond the final row', () => {
    expect(visibleRowRange([0], 500, 100)).toEqual({ start: 0, end: 0, before: 0, after: 0 })
    expect(visibleRowRange([0, 50, 100], 500, 100, 1)).toEqual({ start: 1, end: 2, before: 50, after: 0 })
  })
})
