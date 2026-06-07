export const shadowMonitorNumberFields = [
  { key: 'poll_interval_seconds', label: 'Poll Interval', suffix: 'sec', min: 5, max: 3600 },
  { key: 'watch_gap_seconds', label: 'Watch Gap', suffix: 'sec', min: 1, max: 300 },
  { key: 'probe_duration_seconds', label: 'Probe Duration', suffix: 'sec', min: 3, max: 120 },
  { key: 'confirmation_count', label: 'Confirmations', suffix: 'hits', min: 1, max: 5 },
  {
    key: 'channel_cooldown_seconds',
    label: 'Channel Cooldown',
    suffix: 'sec',
    min: 30,
    max: 86400,
    help: 'Wait before another switch attempt on the same channel.',
  },
  {
    key: 'max_switches_per_hour',
    label: 'Channel Switch Limit',
    suffix: '/ hour',
    min: 1,
    max: 20,
    help: 'Maximum successful stream switches for one channel in a rolling hour.',
  },
  { key: 'max_concurrent_watchers', label: 'Watchers', suffix: 'max', min: 1, max: 10 },
]

export const shadowMonitorThresholdFields = [
  { key: 'blank_min_duration_seconds', label: 'Blank Duration', step: '0.5', min: 0.5, max: 30 },
  { key: 'blank_pixel_threshold', label: 'Pixel Threshold', step: '0.01', min: 0, max: 1 },
  { key: 'blank_ratio_threshold', label: 'Blank Ratio', step: '0.01', min: 0.1, max: 1 },
  { key: 'freeze_min_duration_seconds', label: 'Freeze Duration', step: '0.5', min: 1, max: 120 },
  { key: 'freeze_noise_threshold', label: 'Freeze Noise', step: '0.001', min: 0, max: 1 },
  { key: 'freeze_ratio_threshold', label: 'Freeze Ratio', step: '0.01', min: 0.1, max: 1 },
  { key: 'no_decodable_frames_min_duration_seconds', label: 'Decoder Stall Duration', step: '0.5', min: 3, max: 60 },
]
