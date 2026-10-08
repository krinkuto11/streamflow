// The status response already contains progress. Fetch it separately only when
// an older or unavailable status endpoint cannot provide that field.
export async function loadStreamCheckerPoll(streamCheckerAPI, includeSettings = true, requestOptions = undefined) {
  const [statusResult, configResult, hardwareResult] = await Promise.allSettled([
    streamCheckerAPI.getStatus(requestOptions),
    ...(includeSettings ? [streamCheckerAPI.getConfig(requestOptions), streamCheckerAPI.getHardwareStatus(requestOptions)] : []),
  ])

  if (requestOptions?.signal?.aborted) return { statusResult, configResult, hardwareResult }

  let progressResult
  const statusData = statusResult.status === 'fulfilled' ? statusResult.value?.data : null
  if (statusData && Object.prototype.hasOwnProperty.call(statusData, 'progress')) {
    progressResult = { status: 'fulfilled', value: { data: statusData.progress } }
  } else {
    try {
      progressResult = { status: 'fulfilled', value: await streamCheckerAPI.getProgress(requestOptions) }
    } catch (reason) {
      progressResult = { status: 'rejected', reason }
    }
  }

  return { statusResult, progressResult, configResult, hardwareResult }
}
