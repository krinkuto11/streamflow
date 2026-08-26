export const parseShadowMonitorCsv = (value, numeric = false) => (
  String(value || '')
    .split(',')
    .map(item => item.trim())
    .filter(Boolean)
    .map(item => numeric ? Number(item) : item)
    .filter(item => numeric ? Number.isFinite(item) : true)
)

export const buildShadowMonitorConfigPayload = ({
  sourceConfig = {},
  configRevision,
  includedIds = '',
  includedUuids = '',
  excludedIds = '',
  excludedUuids = '',
  offlineImageHashes = '',
  extra = {},
} = {}) => ({
  ...sourceConfig,
  ...extra,
  expected_config_revision: configRevision,
  included_channel_ids: parseShadowMonitorCsv(includedIds, true),
  included_channel_uuids: parseShadowMonitorCsv(includedUuids),
  excluded_channel_ids: parseShadowMonitorCsv(excludedIds, true),
  excluded_channel_uuids: parseShadowMonitorCsv(excludedUuids),
  offline_image_reference_hashes: parseShadowMonitorCsv(offlineImageHashes),
})

export const isShadowConfigRevisionConflict = error => (
  error?.response?.status === 409
  && error?.response?.data?.code === 'shadow_config_revision_conflict'
)
