import axios from 'axios';

// Create axios instance with default config
// Use /api path since both frontend and backend are served from the same origin
const baseURL = '/api';

export const api = axios.create({
  baseURL,
  timeout: 30000,
  headers: {
    'Content-Type': 'application/json',
  },
});

// Request interceptor
api.interceptors.request.use(
  (config) => {
    return config;
  },
  (error) => {
    return Promise.reject(error);
  }
);

// Response interceptor for error handling
api.interceptors.response.use(
  (response) => {
    return response;
  },
  (error) => {
    return Promise.reject(error);
  }
);

// API methods
export const automationAPI = {
  // Status and Control
  getStatus: () => api.get('/automation/status'),
  start: () => api.post('/automation/start'),
  stop: () => api.post('/automation/stop'),
  runCycle: (data) => api.post('/automation/trigger', data),  // Trigger immediate automation cycle
  trigger: () => api.post('/automation/trigger'),

  // Configuration
  getConfig: () => api.get('/automation/config'),
  updateConfig: (config) => api.put('/automation/config', config),

  // Global Settings
  getGlobalSettings: () => api.get('/settings/automation/global'),
  updateGlobalSettings: (settings) => api.put('/settings/automation/global', settings),

  // Profiles
  getProfiles: () => api.get('/automation/profiles'),
  createProfile: (profile) => api.post('/automation/profiles', profile),
  getProfile: (profileId) => api.get(`/automation/profiles/${profileId}`),
  updateProfile: (profileId, profile) => api.put(`/automation/profiles/${profileId}`, profile),
  deleteProfile: (profileId) => api.delete(`/automation/profiles/${profileId}`),
  bulkDeleteProfiles: (profileIds) => api.post('/automation/profiles/bulk-delete', { profile_ids: profileIds }),

  // Assignments
  assignChannel: (channelId, profileId) => api.post('/automation/assign/channel', { channel_id: channelId, profile_id: profileId }),
  assignChannels: (channelIds, profileId) => api.post('/automation/assign/channels', { channel_ids: channelIds, profile_id: profileId }),
  assignGroup: (groupId, profileId) => api.post('/automation/assign/group', { group_id: groupId, profile_id: profileId }),

  // Automation Periods
  getPeriods: () => api.get('/automation/periods'),
  createPeriod: (period) => api.post('/automation/periods', period),
  getPeriod: (periodId) => api.get(`/automation/periods/${periodId}`),
  updatePeriod: (periodId, period) => api.put(`/automation/periods/${periodId}`, period),
  deletePeriod: (periodId) => api.delete(`/automation/periods/${periodId}`),
  assignPeriodToChannels: (periodId, channelIds, profileId, replace = false) =>
    api.post(`/automation/periods/${periodId}/assign-channels`, { channel_ids: channelIds, profile_id: profileId, replace }),
  removePeriodFromChannels: (periodId, channelIds) =>
    api.post(`/automation/periods/${periodId}/remove-channels`, { channel_ids: channelIds }),
  getPeriodChannels: (periodId) => api.get(`/automation/periods/${periodId}/channels`),
  getChannelPeriods: (channelId) => api.get(`/channels/${channelId}/automation-periods`),
  batchAssignPeriods: (channelIds, periodAssignments, replace = false) =>
    api.post('/channels/batch/assign-periods', { channel_ids: channelIds, period_assignments: periodAssignments, replace }),
  assignPeriodToGroups: (periodId, groupIds, profileId, replace = false) =>
    api.post(`/automation/periods/${periodId}/assign-groups`, { group_ids: groupIds, profile_id: profileId, replace }),
  removePeriodFromGroups: (periodId, groupIds) =>
    api.post(`/automation/periods/${periodId}/remove-groups`, { group_ids: groupIds }),
  getGroupPeriods: (groupId) => api.get(`/channels/groups/${groupId}/automation-periods`),
  batchAssignPeriodsToGroups: (groupIds, periodAssignments, replace = false) =>
    api.post('/channels/groups/batch/assign-periods', { group_ids: groupIds, period_assignments: periodAssignments, replace }),

  getBatchPeriodUsage: (channelIds) =>
    api.post('/channels/batch/period-usage', { channel_ids: channelIds }),

  // Automation Events
  getUpcomingEvents: (hours = 24, maxEvents = 100, periodId = null, forceRefresh = false) => {
    const params = new URLSearchParams({ hours: hours.toString(), max_events: maxEvents.toString() })
    if (periodId) params.append('period_id', periodId)
    if (forceRefresh) params.append('force_refresh', 'true')
    return api.get(`/automation/events/upcoming?${params.toString()}`)
  },
  invalidateEventsCache: () => api.post('/automation/events/invalidate-cache'),
};

export const channelsAPI = {
  /**
   * Fetch channels with optional filtering, sorting, and pagination.
   *
   * @param {Object} [params]
   * @param {string}  [params.search]    - Filter by channel name (case-insensitive substring match).
   * @param {string}  [params.sort_by]   - Sort field: 'name' (default), 'channel_number', or 'id'.
   * @param {string}  [params.sort_dir]  - Sort direction: 'asc' (default) or 'desc'.
   * @param {number}  [params.page]      - Page number (1-based). Omit for full list.
   * @param {number}  [params.per_page]  - Items per page (default 50, max 500).
   */
  getChannels: (params = {}) => api.get('/channels', { params }),
  getGroups: () => api.get('/channels/groups'),
  getChannelStats: (channelId) => api.get(`/channels/${channelId}/stats`),
  getLogo: (logoId) => api.get(`/channels/logos/${logoId}`),
  getLogoCached: (logoId) => `/api/channels/logos/${logoId}/cache`,
};

export const channelOrderAPI = {
  getOrder: () => api.get('/channel-order'),
  setOrder: (order) => api.put('/channel-order', { order }),
  clearOrder: () => api.delete('/channel-order'),
};

export const regexAPI = {
  getPatterns: () => api.get('/regex-patterns'),
  addPattern: (pattern) => api.post('/regex-patterns', pattern),
  deletePattern: (channelId) => api.delete(`/regex-patterns/${channelId}`),
  getGroupConfig: (groupId) => api.get(`/channels/groups/${groupId}/regex-config`),
  saveGroupConfig: (groupId, config) => api.post(`/channels/groups/${groupId}/regex-config`, config),
  deleteGroupConfig: (groupId) => api.delete(`/channels/groups/${groupId}/regex-config`),
  testPattern: (data) => api.post('/test-regex', data),
  testPatternLive: (data) => api.post('/test-regex-live', data),
  /**
   * Import patterns from a canonical JSON object.
   * Fully replaces all existing patterns.
   */
  importPatterns: (patterns) => api.post('/regex-patterns/import', patterns),
  /**
   * Export all patterns as a JSON object in the canonical format.
   * The result can be passed directly to importPatterns for backup/restore.
   */
  exportPatterns: () => api.get('/regex-patterns/export'),
  bulkAddPatterns: (data) => api.post('/regex-patterns/bulk', data),
  bulkDeletePatterns: (data) => api.post('/regex-patterns/bulk-delete', data),
  getCommonPatterns: (data) => api.post('/regex-patterns/common', data),
  bulkEditPattern: (data) => api.post('/regex-patterns/bulk-edit', data),
  massEditPreview: (data) => api.post('/regex-patterns/mass-edit-preview', data),
  massEdit: (data) => api.post('/regex-patterns/mass-edit', data),
  updateMatchSettings: (channelId, settings) => api.post(`/channels/${channelId}/match-settings`, settings),
  updateGroupMatchSettings: (groupId, settings) => api.post(`/channels/groups/${groupId}/match-settings`, settings),
  testMatchLive: (data) => api.post('/test-match-live', data),
  updateBulkMatchSettings: (data) => api.post('/regex-patterns/bulk-settings', data),
};

export const streamAPI = {
  discoverStreams: () => api.post('/discover-streams'),
  refreshPlaylist: (accountId) => api.post('/refresh-playlist', accountId ? { account_id: accountId } : {}),
};

export const m3uAPI = {
  getAccounts: () => api.get('/m3u-accounts'),
  updateAccountPriority: (accountId, data) => api.patch(`/m3u-accounts/${accountId}/priority`, data),
  updateGlobalPriorityMode: (data) => api.put('/m3u-priority/global-mode', data),
};

export const streamCheckerAPI = {
  getStatus: () => api.get('/stream-checker/status'),
  start: () => api.post('/stream-checker/start'),
  stop: () => api.post('/stream-checker/stop'),
  getQueue: () => api.get('/stream-checker/queue'),
  addToQueue: (data) => api.post('/stream-checker/queue/add', data),
  clearQueue: () => api.post('/stream-checker/queue/clear'),
  getConfig: () => api.get('/stream-checker/config'),
  updateConfig: (config) => api.put('/stream-checker/config', config),
  getProgress: () => api.get('/stream-checker/progress'),
  checkChannel: (channelId) => api.post('/stream-checker/check-channel', { channel_id: channelId }),
  // Use longer timeout for single channel check as it can take time
  checkSingleChannel: (channelId) => api.post('/stream-checker/check-single-channel', { channel_id: channelId }, { timeout: 120000 }),
  markUpdated: (data) => api.post('/stream-checker/mark-updated', data),
  queueAllChannels: () => api.post('/stream-checker/queue-all'),
  triggerGlobalAction: () => api.post('/stream-checker/global-action'),
};

export const changelogAPI = {
  getChangelog: (days = 7, page = 1, limit = 10) => api.get(`/changelog`, { params: { days, page, limit } }),
};

export const deadStreamsAPI = {
  /**
   * Fetch dead streams with SQL-native pagination, sorting, and optional search.
   *
   * @param {number} [page=1]         - Page number (1-based).
   * @param {number} [per_page=20]    - Items per page.
   * @param {Object} [options]
   * @param {number} [options.page=1]
   * @param {number} [options.per_page=20]
   * @param {string} [options.sort_by='marked_dead_at'] - 'marked_dead_at', 'stream_name', 'url', 'reason'
   * @param {string} [options.sort_dir='desc']          - 'desc' or 'asc'
   * @param {string} [options.search='']               - case-insensitive substring filter
   */
  getDeadStreams: (options = {}) => {
    const {
      page = 1,
      per_page = 20,
      sort_by = 'marked_dead_at',
      sort_dir = 'desc',
      search = '',
    } = options;
    const safePage = typeof page === 'number' ? page : parseInt(page) || 1;
    const safePerPage = typeof per_page === 'number' ? per_page : parseInt(per_page) || 20;
    const params = { page: safePage, per_page: safePerPage, sort_by, sort_dir };
    if (search) params.search = search;
    return api.get('/dead-streams', { params });
  },
  reviveStream: (streamUrl) => api.post('/dead-streams/revive', { stream_url: streamUrl }),
  clearAllDeadStreams: () => api.post('/dead-streams/clear'),
};

export const setupAPI = {
  getStatus: () => api.get('/setup-wizard'),
  ensureConfig: () => api.post('/setup-wizard/ensure-config'),
};

export const dispatcharrAPI = {
  getConfig: () => api.get('/dispatcharr/config'),
  updateConfig: (config) => api.put('/dispatcharr/config', config),
  testConnection: (config) => api.post('/dispatcharr/test-connection', config),
  initializeUDI: () => api.post('/dispatcharr/initialize-udi'),
  getInitializationStatus: () => api.get('/dispatcharr/initialization-status'),
};

export const aceStreamOrchestratorAPI = {
  getConfig: () => api.get('/acestream-orchestrator/config'),
  updateConfig: (config) => api.put('/acestream-orchestrator/config', config),
};

export const sessionSettingsAPI = {
  getSettings: () => api.get('/settings/session'),
  updateSettings: (settings) => api.post('/settings/session', settings),
};

export const schedulingAPI = {
  getConfig: () => api.get('/scheduling/config'),
  updateConfig: (config) => api.put('/scheduling/config', config),
  getEPGGrid: (forceRefresh = false) => api.get('/scheduling/epg/grid', { params: { force_refresh: forceRefresh } }),
  getChannelPrograms: (channelId) => api.get(`/scheduling/epg/channel/${channelId}`),
  getEvents: () => api.get('/scheduling/events'),
  createEvent: (eventData) => api.post('/scheduling/events', eventData),
  deleteEvent: (eventId) => api.delete(`/scheduling/events/${eventId}`),
  getAutoCreateRules: () => api.get('/scheduling/auto-create-rules'),
  createAutoCreateRule: (ruleData) => api.post('/scheduling/auto-create-rules', ruleData),
  updateAutoCreateRule: (ruleId, ruleData) => api.put(`/scheduling/auto-create-rules/${ruleId}`, ruleData),
  deleteAutoCreateRule: (ruleId) => api.delete(`/scheduling/auto-create-rules/${ruleId}`),
  testAutoCreateRule: (testData) => api.post('/scheduling/auto-create-rules/test', testData),
  exportAutoCreateRules: () => api.get('/scheduling/auto-create-rules/export'),
  importAutoCreateRules: (rulesData) => api.post('/scheduling/auto-create-rules/import', rulesData),
};

export const versionAPI = {
  getVersion: () => api.get('/version'),
};

export const environmentAPI = {
  getEnvironment: () => api.get('/environment'),
};