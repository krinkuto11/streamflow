export const parseFilterCsv = (value) => (
  String(value || '')
    .split(',')
    .map(item => item.trim().toLowerCase())
    .filter(Boolean)
)

export const toggleFilterCsvTerm = (value, term) => {
  const normalized = String(term || '').trim().toLowerCase()
  if (!normalized) return value || ''

  const current = parseFilterCsv(value)
  const next = current.includes(normalized)
    ? current.filter(item => item !== normalized)
    : [...current, normalized]

  return next.join(', ')
}

export const collectTeamarrFilterOptions = (events = []) => {
  const sports = new Set()
  const leagues = new Set()

  for (const event of events) {
    const sport = String(event?.sport || '').trim().toLowerCase()
    const league = String(event?.league || '').trim().toLowerCase()
    if (sport) sports.add(sport)
    if (league) leagues.add(league)
  }

  return {
    sports: [...sports].sort((a, b) => a.localeCompare(b)),
    leagues: [...leagues].sort((a, b) => a.localeCompare(b)),
  }
}
