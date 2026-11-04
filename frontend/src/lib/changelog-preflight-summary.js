const asArray = value => Array.isArray(value) ? value : []

export function getChangelogPreflightSummary(entry = {}) {
  const details = entry.details || {}
  if ((details.run_mode || details.run_snapshot?.run_mode) !== 'teamarr_preflight') return null

  const channels = asArray(entry.subentries)
    .filter(group => group?.group === 'check')
    .flatMap(group => asArray(group.items))
  const channel = details.channel_id != null
    ? channels.find(item => String(item?.channel_id) === String(details.channel_id))
    : channels.length === 1 ? channels[0] : null
  const inspection = channel?.stats?.preflight_inspection
  const recorded = Array.isArray(inspection?.streams)
  const streams = recorded ? inspection.streams : asArray(channel?.stats?.stream_details)
  const rows = streams.filter(stream => stream && typeof stream === 'object').map(stream => ({
    ...stream,
    measurement: recorded && stream.reason === 'known_good_reused'
      && stream.measurement_source === 'previous_measurement' ? 'reused'
      : stream.measurement_source === 'current_check' ? 'checked'
        : recorded ? 'skipped' : 'unknown',
  }))
  return {
    channelName: details.channel_name || channel?.channel_name
      || (details.channel_id != null ? `Channel ${details.channel_id}` : 'Channel unavailable'),
    programName: details.program_name || null,
    recorded,
    total: recorded ? inspection.total_streams ?? rows.length : rows.length,
    truncated: recorded && (inspection.truncated === true || inspection.total_streams > rows.length),
    checked: rows.filter(row => row.measurement === 'checked').length,
    reused: rows.filter(row => row.measurement === 'reused').length,
    skipped: rows.filter(row => row.measurement === 'skipped').length,
    rows,
  }
}
