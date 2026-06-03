export const operatorHelpSections = [
  {
    id: 'startup-cache',
    title: 'Startup And Cache',
    summary: 'Dispatcharr data must be ready before automation or manual checks can make reliable decisions.',
    items: [
      'The startup screen appears only before the first usable Dispatcharr cache is available.',
      'Manual UDI reloads keep the app usable when an existing cache is present.',
      'Playlist refresh requests are accepted by Dispatcharr first; Cache Sync is where StreamFlow confirms the data it can see.',
      'Large playlists can take a few minutes; the dashboard unlocks automatically when startup is complete.',
    ],
    links: [
      { label: 'Dashboard', to: '/' },
      { label: 'Settings', to: '/settings' },
    ],
  },
  {
    id: 'profiles-periods',
    title: 'Profiles And Periods',
    summary: 'Profiles define what a run does; periods define when and where those rules run.',
    items: [
      'Use checking-only profiles for targeted quality passes without playlist refresh or stream matching.',
      'Playlist Priority Rank applies only when the selected stream priority mode uses rank.',
      'Startup catch-up is opt-in per period and only covers the safe first/no-last-run case.',
      'Missed-run grace keeps late automatic runs bounded; 0 keeps the existing schedule behavior.',
      'Catch-up cap and Maintenance window are global automatic-run policies; manual forced runs remain available.',
      'Teamarr event window can pause automatic runs around cached event starts; manual forced runs remain available.',
      'Teamarr post-start checks can intentionally run after event start when event channels appear at kickoff or a few minutes later.',
    ],
    links: [
      { label: 'Automation Settings', to: '/settings' },
      { label: 'Channels', to: '/channels' },
    ],
  },
  {
    id: 'stream-checker',
    title: 'Stream Checker',
    summary: 'Quality checks reserve provider/profile capacity and keep real viewers protected.',
    items: [
      'Queued channels wait their turn; higher waiting priority does not interrupt a channel already running.',
      'Check slots full means the checker-side capacity for that account or profile is currently occupied.',
      'Viewer-preempted probes are not counted as bad streams and can be checked again later.',
      'During active batches, dashboard Dead, Blank, and Frozen cards count cumulative stream results from the queue.',
    ],
    links: [
      { label: 'Stream Checker', to: '/stream-checker' },
    ],
  },
  {
    id: 'shadow-monitor',
    title: 'Shadow Monitor',
    summary: 'Continuous watching follows active viewer sessions and keeps exclusions as the normal control path.',
    items: [
      'Continuous mode watches active viewer channels without needing repeated Scan Now actions.',
      'Excluded channel IDs and UUIDs are the normal way to keep a viewer-visible channel out of watcher checks.',
      'Watched Now shows watcher continuity with anonymized watcher references and uptime.',
    ],
    links: [
      { label: 'Shadow Monitor', to: '/shadow-monitor' },
    ],
  },
  {
    id: 'hardware',
    title: 'GPU And Fallback',
    summary: 'Hardware acceleration is optional and should stay visible through the normal container management path.',
    items: [
      'CPU only is valid; hardware preferred with CPU fallback is the safer GPU mode for mixed providers.',
      'Hardware-only mode should be used carefully because failed hardware init cannot retry on CPU.',
      'Intel/DRI paths should show VAAPI, QSV, or DRI methods even when no NVIDIA runtime is present.',
      'Use the hardware status panel before changing runtime, device, or acceleration settings.',
    ],
    links: [
      { label: 'Stream Checker', to: '/stream-checker' },
      { label: 'Settings', to: '/settings' },
    ],
  },
  {
    id: 'troubleshooting',
    title: 'Troubleshooting',
    summary: 'Start with the live status panels, then narrow the problem by run type and active stage.',
    items: [
      'If the dashboard is idle, upcoming events and automation status should refresh without manual clicks.',
      'If a run is stopped, the dashboard should show an aborted state instead of completed or failed.',
      'For stream failures, check the displayed reason text before changing provider or profile settings.',
    ],
    links: [
      { label: 'Changelog', to: '/changelog' },
      { label: 'Analytics', to: '/stats' },
    ],
  },
]

export const operatorHelpQuickChecks = [
  'Cache ready before starting automation',
  'Profile mode matches intended priority behavior',
  'GPU path and fallback state are visible',
  'Stream Checker is idle or intentionally running',
  'Shadow Monitor excludes are intentional',
]
