export const REGEX_TABLE_GRID_COLS = '50px 80px 80px 1fr 180px 120px 150px 140px'

// M3U account filtering - exclude 'custom' account as it's not a real source
export const CUSTOM_ACCOUNT_NAME = 'custom'

// Supports both legacy and current regex pattern formats.
export const normalizePatternData = (channelPatterns) => {
  if (!channelPatterns) return []

  if (channelPatterns.regex_patterns && Array.isArray(channelPatterns.regex_patterns)) {
    return channelPatterns.regex_patterns.map((item) => ({
      pattern: item.pattern || item,
      m3u_accounts: item.m3u_accounts || null,
    }))
  }

  if (channelPatterns.regex && Array.isArray(channelPatterns.regex)) {
    const channelM3uAccounts = channelPatterns.m3u_accounts || null
    return channelPatterns.regex.map((pattern) => ({
      pattern,
      m3u_accounts: channelM3uAccounts,
    }))
  }

  return []
}
