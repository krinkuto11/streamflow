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
      { label: 'Detailed Guide', to: '/help/setup' },
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
      { label: 'Detailed Guide', to: '/help/automation-periods' },
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
      { label: 'Detailed Guide', to: '/help/stream-checker' },
      { label: 'Stream Checker', to: '/stream-checker' },
    ],
  },
  {
    id: 'teamarr-preflight',
    title: 'Teamarr Preflight',
    summary: 'Event checks protect channels that appear near kickoff and keep selected profile rules visible.',
    items: [
      'The selected quality profile defines the actual check rules for event channels.',
      'Post-start checks intentionally run after kickoff when event streams appear late.',
      'Busy stream-checker work is queued instead of silently dropping the event check.',
      'Include and exclude filters should stay broad unless an operator needs a temporary override.',
    ],
    links: [
      { label: 'Detailed Guide', to: '/help/teamarr-preflight' },
      { label: 'Teamarr Preflight', to: '/teamarr-preflight' },
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
      'Channel Switch Limit is a per-channel rolling-hour guard; Channel Cooldown is only the same-channel wait between switch attempts.',
    ],
    links: [
      { label: 'Detailed Guide', to: '/help/shadow-monitor' },
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
      { label: 'Detailed Guide', to: '/help/hardware-fallback' },
      { label: 'Stream Checker', to: '/stream-checker' },
      { label: 'Settings', to: '/settings' },
    ],
  },
  {
    id: 'troubleshooting',
    title: 'Troubleshooting',
    summary: 'Start with the live status panels, then narrow the problem by run type and active stage.',
    items: [
      'After setup or image updates, confirm startup/cache readiness, Stream Checker hardware status, and Teamarr Preflight status before starting automation.',
      'If the dashboard is idle, upcoming events and automation status should refresh without manual clicks.',
      'If a run is stopped, the dashboard should show an aborted state instead of completed or failed.',
      'For stream failures, check the displayed reason text before changing provider or profile settings.',
      'For event channels that appear at kickoff, use post-start checks instead of letting early failed checks demote streams.',
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
  'Teamarr post-start window is intentional',
  'GPU path and fallback state are visible',
  'Stream Checker is idle or intentionally running',
  'Shadow Monitor excludes are intentional',
]

export const operatorHelpDetailGuidePrinciples = [
  'Detailed guides stay platform neutral and avoid host-specific setup assumptions.',
  'Each settings reference explains effect, defaults, when to use it, tradeoffs, risks, and the smoke checks that confirm it works.',
]

export const operatorHelpDetailTopics = [
  {
    id: 'setup',
    title: 'Setup And First Smoke',
    summary: 'Use this after first setup, image updates, or backend connection changes.',
    visual: {
      title: 'Healthy startup path',
      steps: ['Connect backend', 'Build first cache', 'Confirm live status', 'Start automation deliberately'],
    },
    steps: [
      'Open Dashboard and wait until the startup/cache state is complete before running automation.',
      'Confirm Health, Stream Checker hardware status, Teamarr Preflight status, and Shadow Monitor status all answer without errors.',
      'Keep image tag, persistent data path, API host, and API port aligned with the container manager UI.',
      'Run the smallest useful manual check first; scale up only after the live status panels agree.',
    ],
    settings: [
      {
        name: 'API_HOST',
        defaultValue: '0.0.0.0',
        effect: 'Controls which network interface the backend listens on inside the container.',
        useWhen: 'Keep the default for normal container networking.',
        risk: 'A narrow bind can make the UI or healthcheck unreachable from outside the container.',
      },
      {
        name: 'API_PORT',
        defaultValue: '5000 or the configured application port',
        effect: 'Defines the backend port used by the UI, API, and healthcheck.',
        useWhen: 'Change only when the host mapping and healthcheck are changed together.',
        risk: 'A mismatch can make the container look unhealthy even when the process started.',
      },
      {
        name: 'CONFIG_DIR',
        defaultValue: '/app/data',
        effect: 'Stores persistent config, caches, profiles, and runtime state.',
        useWhen: 'Point it at the mounted persistent data directory.',
        risk: 'A temporary path can make settings disappear after an update.',
      },
      {
        name: 'Startup cache readiness',
        defaultValue: 'Wait for completion',
        effect: 'Prevents automation and manual checks from using partial Dispatcharr data.',
        useWhen: 'Use after container start, image update, or manual UDI reload.',
        risk: 'Starting runs too early can make stream counts, event lists, or channel mappings look incomplete.',
      },
    ],
    smokeChecks: [
      '/api/health returns healthy.',
      'Initialization status reaches completed with expected stream counts.',
      'Dashboard, Stream Checker, Teamarr Preflight, and Shadow Monitor status panels show no current error.',
    ],
    links: [
      { label: 'Dashboard', to: '/' },
      { label: 'Settings', to: '/settings' },
    ],
  },
  {
    id: 'automation-periods',
    title: 'Automation Profiles And Periods',
    summary: 'Profiles decide what a run does; periods decide when and which channels run.',
    visual: {
      title: 'Automation control flow',
      steps: ['Profile rules', 'Period schedule', 'Run policy', 'Stage progress'],
    },
    steps: [
      'Choose or create the profile first so refresh, matching, and quality behavior are explicit.',
      'Attach channels or groups through the period or channel assignment screens.',
      'Set the schedule and missed-run policy based on how late automatic runs are still useful.',
      'Watch Dashboard stage progress; skipped stages should read as skipped, not failed.',
    ],
    settings: [
      {
        name: 'Quality Check',
        defaultValue: 'Profile-specific',
        effect: 'Controls whether streams are analyzed for dead, blank, freeze, loop, or quality outcomes.',
        useWhen: 'Enable for maintenance and event-preflight profiles that should score or protect streams.',
        risk: 'Disabling it skips stream analysis and leaves quality state unchanged.',
      },
      {
        name: 'M3U Refresh',
        defaultValue: 'Profile-specific',
        effect: 'Sends playlist refresh requests before matching or checking.',
        useWhen: 'Use for scheduled provider refresh windows, not every small quality-only pass.',
        risk: 'Unneeded refreshes can make runs slower and hide whether a quality issue is real.',
      },
      {
        name: 'Startup catch-up',
        defaultValue: 'Off',
        effect: 'Allows a period with no previous run timestamp to run once after startup.',
        useWhen: 'Use only when the first automatic pass after setup is expected.',
        risk: 'Leaving it on broadly can start work immediately after a restart.',
      },
      {
        name: 'Missed-run grace',
        defaultValue: '0 minutes',
        effect: 'Limits how late an automatic missed run may still be caught up.',
        useWhen: 'Use a positive window when old runs should be skipped instead of replayed.',
        risk: 'Too small can skip useful maintenance; too large can create stale catch-up work.',
      },
      {
        name: 'Maintenance window',
        defaultValue: 'Configured globally, disabled by default',
        effect: 'Pauses automatic runs during a daily time window.',
        useWhen: 'Use around provider maintenance, backups, or known busy viewer windows.',
        risk: 'Manual forced runs bypass it, so operators still need to choose deliberately.',
      },
    ],
    smokeChecks: [
      'Automation status shows the expected run policy and no stale active run.',
      'A small manual run shows only the stages enabled by the profile.',
      'Periods list shows last skip or next run context when a policy blocks automatic work.',
    ],
    links: [
      { label: 'Automation Settings', to: '/settings' },
      { label: 'Dashboard', to: '/' },
    ],
  },
  {
    id: 'stream-checker',
    title: 'Stream Checker',
    summary: 'Use Stream Checker for controlled quality analysis without stealing capacity from real viewers.',
    visual: {
      title: 'Quality-check flow',
      steps: ['Queue channel', 'Reserve profile slot', 'Analyze stream', 'Record reason'],
    },
    steps: [
      'Start with one channel or a small queue when validating profile or hardware changes.',
      'Check provider/profile slots before increasing parallelism.',
      'Read the displayed reason before changing stream order or deleting streams.',
      'Use Dead, Blank, and Frozen dashboard counters as active batch totals, not only current stream rows.',
    ],
    settings: [
      {
        name: 'Check on update',
        defaultValue: 'On',
        effect: 'Queues checks after updates when the profile calls for quality validation.',
        useWhen: 'Keep on for normal maintenance profiles.',
        risk: 'Turning it off can leave newly matched streams unverified.',
      },
      {
        name: 'Max channels per run',
        defaultValue: '50',
        effect: 'Caps how much channel work a single automatic run can enqueue.',
        useWhen: 'Lower it when providers have tight limits or live viewers need more headroom.',
        risk: 'Too high can keep profiles busy for a long time.',
      },
      {
        name: 'Parallel workers',
        defaultValue: 'Configured in Stream Checker',
        effect: 'Controls how many stream probes can run concurrently.',
        useWhen: 'Increase only when provider/profile limits and hardware can handle it.',
        risk: 'Too high can cause provider throttling, viewer contention, or noisy failures.',
      },
      {
        name: 'CPU Fallback',
        defaultValue: 'On for safer hardware mode',
        effect: 'Retries analysis on CPU if the selected hardware path is unavailable or rejected.',
        useWhen: 'Use for mixed providers and uncertain hardware support.',
        risk: 'Turning it off makes hardware setup errors fail the check instead of falling back.',
      },
    ],
    smokeChecks: [
      'Stream Checker status is idle before manual queue testing.',
      'A small queue reports active provider/profile slots and final reason labels.',
      'Dead, Blank, and Frozen counters update from queue totals during active batches.',
    ],
    links: [
      { label: 'Stream Checker', to: '/stream-checker' },
      { label: 'Dashboard', to: '/' },
    ],
  },
  {
    id: 'teamarr-preflight',
    title: 'Teamarr Preflight',
    summary: 'Preflight protects event channels that may appear exactly at start time or shortly after.',
    visual: {
      title: 'Event-check timing',
      steps: ['Read event', 'Pre-start check', 'Post-start check', 'Queue or complete'],
    },
    steps: [
      'Select the quality profile that represents event-channel check rules.',
      'Keep pre-start retries separate from post-start checks so early failures do not block kickoff checks.',
      'Use exclude filters for temporary suppression; leave include filters broad unless a narrow whitelist is intended.',
      'Confirm active event checks and recent events before changing scoring rules.',
    ],
    settings: [
      {
        name: 'Preflight Offset',
        defaultValue: '20 minutes',
        effect: 'Runs the main pre-start check before event start.',
        useWhen: 'Use a smaller value when providers name event channels close to kickoff.',
        risk: 'Too early can check placeholder or missing streams.',
      },
      {
        name: 'Pre-Start Retries',
        defaultValue: '10 and 3 minutes',
        effect: 'Schedules additional checks before start without changing the main offset.',
        useWhen: 'Use when event channels appear shortly before start.',
        risk: 'Too many retries can occupy checker capacity before the stream exists.',
      },
      {
        name: 'Post-Start Checks',
        defaultValue: '2 minutes',
        effect: 'Runs after kickoff so channels that appear at start are not unfairly demoted.',
        useWhen: 'Use for event providers that rename or publish streams at kickoff.',
        risk: 'Too late can miss early viewer protection for short events.',
      },
      {
        name: 'Post-Start Grace',
        defaultValue: '5 minutes',
        effect: 'Limits how long after start post-start checks are still considered due.',
        useWhen: 'Use a short window for sports events with fast channel turnover.',
        risk: 'Too narrow can skip late-published streams; too wide can check stale events.',
      },
      {
        name: 'Skip during quality check',
        defaultValue: 'On',
        effect: 'Defers or queues event checks when normal checker work is already active.',
        useWhen: 'Keep on to avoid interrupting current stream work.',
        risk: 'Turning it off can overload provider/profile capacity.',
      },
    ],
    smokeChecks: [
      'Teamarr Preflight status shows running and no last error.',
      'Upcoming events show scheduled, due, past, or filtered state with clear latest-check context.',
      'A post-start event can be classified due even if an earlier pre-start bucket already ran.',
    ],
    links: [
      { label: 'Teamarr Preflight', to: '/teamarr-preflight' },
      { label: 'Stream Checker', to: '/stream-checker' },
    ],
  },
  {
    id: 'shadow-monitor',
    title: 'Shadow Monitor',
    summary: 'Shadow Monitor follows real viewer sessions and switches only when confirmed bad playback is observed.',
    visual: {
      title: 'Viewer protection loop',
      steps: ['Real viewer active', 'Shadow watcher joins', 'Confirm blank/freeze', 'Switch with guardrails'],
    },
    steps: [
      'Use Continuous mode for normal viewer-following behavior.',
      'Use Exclude Channel IDs or UUIDs to keep specific viewer-visible channels out of watcher checks.',
      'Watch Watched Now for real viewer and shadow watcher counts before changing thresholds.',
      'Treat Channel Switch Limit as a flapping guard; tune it only after reading recent events.',
    ],
    settings: [
      {
        name: 'Poll Interval',
        defaultValue: '5 seconds',
        effect: 'Controls how often periodic discovery checks active viewer sessions.',
        useWhen: 'Use default for responsive watcher discovery.',
        risk: 'Too low can add API noise; too high can notice viewer sessions late.',
      },
      {
        name: 'Watch Gap',
        defaultValue: '1 second',
        effect: 'Controls the short gap between continuous watcher loops.',
        useWhen: 'Keep low for long-lived viewer protection.',
        risk: 'Too high can create avoidable watcher gaps.',
      },
      {
        name: 'Confirmations',
        defaultValue: '2 hits',
        effect: 'Requires repeated bad detections before switching.',
        useWhen: 'Use default to reduce false positives from short video glitches.',
        risk: 'Too low can switch on transient noise; too high can leave bad playback visible longer.',
      },
      {
        name: 'Channel Cooldown',
        defaultValue: '300 seconds',
        effect: 'Waits before another switch attempt on the same channel.',
        useWhen: 'Use to prevent rapid repeat switches on unstable channels.',
        risk: 'Too long can delay recovery if the replacement stream is also bad.',
      },
      {
        name: 'Channel Switch Limit',
        defaultValue: '3 per channel per rolling hour',
        effect: 'Limits successful stream switches for one channel in a rolling hour.',
        useWhen: 'Use as a flapping guard when a channel alternates between bad streams.',
        risk: 'Too low can stop recovery attempts; too high can churn through streams.',
      },
    ],
    smokeChecks: [
      'Shadow Monitor status shows running and no last error.',
      'Watched Now separates real viewer count from shadow watcher count.',
      'Recent Events explain pending, cooldown, recovered, rate-limited, or switch outcomes.',
    ],
    links: [
      { label: 'Shadow Monitor', to: '/shadow-monitor' },
    ],
  },
  {
    id: 'hardware-fallback',
    title: 'Hardware And Fallback',
    summary: 'Hardware acceleration is optional; the UI should show whether CPU, hardware, or fallback is active.',
    visual: {
      title: 'Analysis path decision',
      steps: ['Configured mode', 'Runtime device', 'FFmpeg methods', 'Fallback result'],
    },
    steps: [
      'Check Runtime Device and FFmpeg Methods before changing acceleration mode.',
      'Use Auto first unless the hardware path needs an explicit VAAPI, QSV, or CUDA mode.',
      'Keep CPU Fallback on unless a hardware-only failure should stop the run.',
      'After container runtime or device changes, smoke the hardware-status API before running a large queue.',
    ],
    settings: [
      {
        name: 'Hardware Acceleration',
        defaultValue: 'Off or operator-enabled',
        effect: 'Enables FFmpeg hardware methods for stream analysis.',
        useWhen: 'Use when the runtime exposes a supported device and CPU savings matter.',
        risk: 'Misconfigured hardware can fail probes or hide that CPU would have worked.',
      },
      {
        name: 'Mode',
        defaultValue: 'auto',
        effect: 'Chooses which FFmpeg hardware method StreamFlow requests.',
        useWhen: 'Use Auto for general setups; use VAAPI or QSV for DRI devices when needed.',
        risk: 'A mode not reported by FFmpeg can fail without fallback.',
      },
      {
        name: 'Device',
        defaultValue: 'Blank or device path',
        effect: 'Optional FFmpeg device path or index used by the selected mode.',
        useWhen: 'Use for DRI devices or multi-device systems that need a specific path.',
        risk: 'Wrong paths can make hardware appear configured but unusable.',
      },
      {
        name: 'CPU Fallback',
        defaultValue: 'On',
        effect: 'Retries analysis without hardware if FFmpeg rejects the hardware path.',
        useWhen: 'Use for safer production checks.',
        risk: 'Turning it off makes hardware issues fail checks immediately.',
      },
    ],
    smokeChecks: [
      'Hardware status reports FFmpeg available and lists hardware methods when present.',
      'Runtime Device reports NVIDIA, DRI/VAAPI/QSV, or a generic FFmpeg-method state instead of misleading GPU-only wording.',
      'Analysis Path shows CPU only, hardware preferred, hardware only, fallback ready, or hardware risk.',
    ],
    links: [
      { label: 'Stream Checker', to: '/stream-checker' },
      { label: 'Settings', to: '/settings' },
    ],
  },
]

export const getOperatorHelpDetailTopic = (topicId) => {
  return operatorHelpDetailTopics.find(topic => topic.id === topicId) || null
}
