export const shadowMonitorNumberFields = [
  {
    key: 'viewer_left_grace_seconds',
    label: 'Viewer Grace',
    suffix: 'sec',
    min: 0,
    max: 10,
    help: 'How long Shadow keeps a channel in grace after the real viewer disappears before it releases the watcher. Use 3-5 seconds for frequent channel switching to protect provider/profile limits.',
  },
]

export const shadowMonitorThresholdFields = []
