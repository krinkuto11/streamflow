export const SETTINGS_SAVE_DEPENDENCIES = {
  scheduling: ['automation', 'scheduling'],
  monitoring: ['monitoring'],
  connection: ['connection'],
}

export async function saveSettingsSection(section, values, apis) {
  const operations = {
    automation: () => apis.automation.updateConfig(values.automation),
    scheduling: () => apis.scheduling.updateConfig(values.scheduling),
    monitoring: () => apis.monitoring.updateSettings(values.monitoring),
    connection: () => apis.connection.updateConfig(values.connection),
  }
  const names = SETTINGS_SAVE_DEPENDENCIES[section]
  if (!names) throw new Error(`Unknown settings section: ${section}`)

  const results = await Promise.allSettled(names.map(name => operations[name]()))
  return {
    saved: names.filter((_, index) => results[index].status === 'fulfilled'),
    failed: names.filter((_, index) => results[index].status === 'rejected'),
  }
}
