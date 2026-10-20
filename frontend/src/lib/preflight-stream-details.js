export const preflightOrderLabels = {
  absolute: 'Playlist priority → Resolution → Score',
  same_resolution: 'Resolution → Playlist priority → Score',
  playlist_score: 'Playlist priority → Score',
  score_playlist: 'Score → Playlist priority',
  equal: 'Resolution → Score',
  quality: 'Score',
}

export const preflightStreamStatus = (stream) => ({
  alive: { label: 'Alive', variant: 'default' },
  dead: { label: 'Dead', variant: 'destructive' },
  skipped: { label: 'Skipped', variant: 'secondary' },
  incomplete: { label: 'Incomplete', variant: 'secondary' },
  warning: { label: 'Warning', variant: 'secondary' },
}[stream?.status] || { label: 'Unknown', variant: 'outline' })

export const preflightScore = (value) => (
  typeof value === 'number' && Number.isFinite(value) ? value.toFixed(2) : 'Not scored'
)

export const preflightAction = (stream) => {
  if (stream.action === 'order_not_applied') return 'Order not applied'
  if (stream.action === 'not_retained') return 'Not retained in channel'
  if (stream.rank === 1) return 'First in channel'
  return stream.rank ? `Channel rank ${stream.rank}` : 'Rank unavailable'
}
