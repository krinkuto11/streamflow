// The status response already contains progress. Fetch it separately only when
// an older or unavailable status endpoint cannot provide that field.
export async function loadStreamCheckerPoll(streamCheckerAPI, includeSettings = true) {
  const [statusResult, configResult, hardwareResult] = await Promise.allSettled([
    streamCheckerAPI.getStatus(),
    ...(includeSettings ? [streamCheckerAPI.getConfig(), streamCheckerAPI.getHardwareStatus()] : []),
  ])

  let progressResult
  const statusData = statusResult.status === 'fulfilled' ? statusResult.value?.data : null
  if (statusData && Object.prototype.hasOwnProperty.call(statusData, 'progress')) {
    progressResult = { status: 'fulfilled', value: { data: statusData.progress } }
  } else {
    try {
      progressResult = { status: 'fulfilled', value: await streamCheckerAPI.getProgress() }
    } catch (reason) {
      progressResult = { status: 'rejected', reason }
    }
  }

  return { statusResult, progressResult, configResult, hardwareResult }
}
