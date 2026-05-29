import { describe, expect, it } from 'vitest'

import { cn } from './utils'

describe('cn', () => {
  it('merges class names and ignores falsey values', () => {
    expect(cn('flex', false && 'hidden', ['items-center', null], 'gap-2')).toBe(
      'flex items-center gap-2'
    )
  })
})
