export const getTeamarrConcurrentCheckLimit = ({ editedConfig, status, config } = {}) => {
  const candidates = [
    editedConfig?.max_concurrent_checks,
    status?.config?.max_concurrent_checks,
    config?.max_concurrent_checks,
  ]
  const limit = candidates
    .map(value => Number(value))
    .find(value => Number.isFinite(value) && value > 0)
  return limit || 1
}

export const getTeamarrActiveChecksDetail = ({
  directActiveChecksCount = 0,
  queueActiveChecksCount = 0,
  editedConfig,
  status,
  config,
} = {}) => {
  if (queueActiveChecksCount > 0) {
    return `${directActiveChecksCount} direct, ${queueActiveChecksCount} from queue`
  }
  return `Limit ${getTeamarrConcurrentCheckLimit({ editedConfig, status, config })}`
}
