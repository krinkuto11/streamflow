export function selectParallelCheckProgress(runs, ordinaryProgress, selectedChannelId) {
  if (!runs?.length) return ordinaryProgress
  const selected = runs.find(run => String(run.channel_id) === String(selectedChannelId))
  return selected?.progress || runs.find(run => run.progress)?.progress || null
}
