export const TELEMETRY_RETENTION_DAYS = 7

export const TELEMETRY_DATE_RANGES = [
  { value: '1', label: 'Last 24 Hours' },
  { value: String(TELEMETRY_RETENTION_DAYS), label: `Last ${TELEMETRY_RETENTION_DAYS} Days` },
]
