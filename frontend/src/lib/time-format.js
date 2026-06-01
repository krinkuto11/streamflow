export function parseDurationSeconds(value) {
  if (value === null || value === undefined || value === '') {
    return null
  }

  if (typeof value === 'number') {
    return Number.isFinite(value) ? Math.max(0, value) : null
  }

  const text = String(value).trim()
  if (!text) {
    return null
  }

  if (/^\d+(?:\.\d+)?$/.test(text)) {
    return Math.max(0, Number(text))
  }

  const colonMatch = text.match(/^(\d+):([0-5]\d)(?::([0-5]\d))?$/)
  if (colonMatch) {
    const first = Number(colonMatch[1])
    const second = Number(colonMatch[2])
    const third = colonMatch[3] !== undefined ? Number(colonMatch[3]) : null
    return third === null
      ? first * 60 + second
      : first * 3600 + second * 60 + third
  }

  const unitRegex = /(\d+(?:\.\d+)?)\s*([hms])/gi
  let total = 0
  let matched = false
  for (const match of text.matchAll(unitRegex)) {
    matched = true
    const amount = Number(match[1])
    const unit = match[2].toLowerCase()
    if (unit === 'h') total += amount * 3600
    if (unit === 'm') total += amount * 60
    if (unit === 's') total += amount
  }

  return matched ? Math.max(0, total) : null
}

export function formatDuration(value) {
  const secondsValue = parseDurationSeconds(value)
  if (secondsValue === null) {
    return value === null || value === undefined ? '' : String(value)
  }

  const totalSeconds = Math.round(secondsValue)
  if (totalSeconds < 60) {
    return `${totalSeconds}s`
  }

  const hours = Math.floor(totalSeconds / 3600)
  const minutes = Math.floor((totalSeconds % 3600) / 60)
  const seconds = totalSeconds % 60

  if (hours > 0) {
    const parts = [`${hours}h`]
    if (minutes > 0 || seconds > 0) {
      parts.push(`${minutes}m`)
    }
    if (seconds > 0) {
      parts.push(`${seconds}s`)
    }
    return parts.join(' ')
  }

  return seconds > 0 ? `${minutes}m ${seconds}s` : `${minutes}m`
}

export function formatLatency(value) {
  const secondsValue = parseDurationSeconds(value)
  if (secondsValue === null) {
    return value === null || value === undefined ? '' : String(value)
  }

  if (secondsValue < 1) {
    const milliseconds = Math.round(secondsValue * 1000)
    return `${milliseconds}ms`
  }

  return formatDuration(secondsValue)
}
