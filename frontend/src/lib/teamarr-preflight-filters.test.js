import { describe, expect, it } from 'vitest'

import {
  collectTeamarrFilterOptions,
  parseFilterCsv,
  toggleFilterCsvTerm,
} from './teamarr-preflight-filters'

describe('Teamarr preflight filter helpers', () => {
  it('normalizes CSV filter values', () => {
    expect(parseFilterCsv(' Soccer, NHL ,,  MLB ')).toEqual(['soccer', 'nhl', 'mlb'])
  })

  it('toggles a term in a CSV filter', () => {
    expect(toggleFilterCsvTerm('hockey', 'mlb')).toBe('hockey, mlb')
    expect(toggleFilterCsvTerm('hockey, mlb', 'Hockey')).toBe('mlb')
  })

  it('collects unique sport and league options from events', () => {
    expect(collectTeamarrFilterOptions([
      { sport: 'Hockey', league: 'NHL' },
      { sport: 'baseball', league: 'mlb' },
      { sport: 'hockey', league: 'nhl' },
      { sport: '', league: null },
    ])).toEqual({
      sports: ['baseball', 'hockey'],
      leagues: ['mlb', 'nhl'],
    })
  })
})
