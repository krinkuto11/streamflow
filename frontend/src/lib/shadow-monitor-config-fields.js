export const shadowMonitorNumberFields = [
  {
    key: 'viewer_left_grace_seconds',
    label: 'Viewer Grace',
    suffix: 'sec',
    min: 0,
    max: 30,
    help: 'How long Shadow keeps a channel in grace after the real viewer disappears before it releases the watcher. Keep this short for frequent channel switching.',
  },
]

export const shadowMonitorThresholdFields = []
