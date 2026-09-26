// Setup is a configuration decision. Live connection diagnostics belong to the
// wizard's explicit connection test, not every application navigation/reload.
export function getSetupCompleteFromReadiness(data) {
  if (!data || typeof data !== 'object' || typeof data.ready !== 'boolean') {
    throw new Error('Invalid startup readiness response')
  }

  const config = data.checks?.dispatcharr_config
  if (config !== undefined) {
    if (!config || typeof config !== 'object' || config.reason === 'config_unavailable') {
      throw new Error('Dispatcharr configuration status is unavailable')
    }

    // Explicit configuration checks take priority over a contradictory global
    // ready flag. An unreadable configuration must not open a fresh setup form.
    if (config.reason === 'setup_required' && config.configured === false && config.ready === false) {
      return false
    }
    if (config.configured === false || config.ready === false || config.reason === 'setup_required') {
      throw new Error('Invalid Dispatcharr configuration status')
    }
    if (config.configured === true || config.ready === true) return true
    throw new Error('Missing Dispatcharr configuration status')
  }

  if (data.ready === true) return true
  throw new Error('Missing startup configuration status')
}
