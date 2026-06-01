import { useEffect, useMemo, useState } from 'react'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card.jsx'
import { Button } from '@/components/ui/button.jsx'
import { Badge } from '@/components/ui/badge.jsx'
import { Input } from '@/components/ui/input.jsx'
import { Label } from '@/components/ui/label.jsx'
import { Switch } from '@/components/ui/switch.jsx'
import { Separator } from '@/components/ui/separator.jsx'
import { useToast } from '@/hooks/use-toast.js'
import { shadowBlankMonitorAPI } from '@/services/api.js'
import {
  Activity,
  AlertCircle,
  CheckCircle2,
  Eye,
  Loader2,
  PlayCircle,
  RefreshCw,
  Save,
  Shield,
  StopCircle,
} from 'lucide-react'

const numberFields = [
  { key: 'poll_interval_seconds', label: 'Poll Interval', suffix: 'sec', min: 5, max: 3600 },
  { key: 'watch_gap_seconds', label: 'Watch Gap', suffix: 'sec', min: 1, max: 300 },
  { key: 'probe_duration_seconds', label: 'Probe Duration', suffix: 'sec', min: 3, max: 120 },
  { key: 'confirmation_count', label: 'Confirmations', suffix: 'hits', min: 1, max: 5 },
  { key: 'channel_cooldown_seconds', label: 'Cooldown', suffix: 'sec', min: 30, max: 86400 },
  { key: 'max_switches_per_hour', label: 'Switch Limit', suffix: 'per hour', min: 1, max: 20 },
  { key: 'max_concurrent_watchers', label: 'Watchers', suffix: 'max', min: 1, max: 10 },
]

const thresholdFields = [
  { key: 'blank_min_duration_seconds', label: 'Blank Duration', step: '0.5', min: 0.5, max: 30 },
  { key: 'blank_pixel_threshold', label: 'Pixel Threshold', step: '0.01', min: 0, max: 1 },
  { key: 'blank_ratio_threshold', label: 'Blank Ratio', step: '0.01', min: 0.1, max: 1 },
  { key: 'freeze_min_duration_seconds', label: 'Freeze Duration', step: '0.5', min: 1, max: 120 },
  { key: 'freeze_noise_threshold', label: 'Freeze Noise', step: '0.001', min: 0, max: 1 },
  { key: 'freeze_ratio_threshold', label: 'Freeze Ratio', step: '0.01', min: 0.1, max: 1 },
]

const eventLabels = {
  probe_ok: 'Probe OK',
  blank_pending: 'Blank Pending',
  freeze_pending: 'Freeze Pending',
  dry_run_switch: 'Dry Run Switch',
  switch_success: 'Switch Success',
  switch_failed: 'Switch Failed',
  no_alternative: 'No Alternative',
  cooldown: 'Cooldown',
  stale_stream_guard: 'Stale Stream',
  switch_rate_limited: 'Rate Limited',
  viewer_left: 'Viewer Left',
  quality_check_active: 'Quality Check Active',
}

const parseCsv = (value, numeric = false) => {
  return String(value || '')
    .split(',')
    .map(item => item.trim())
    .filter(Boolean)
    .map(item => numeric ? Number(item) : item)
    .filter(item => numeric ? Number.isFinite(item) : true)
}

const formatTime = (timestamp) => {
  if (!timestamp) return 'Never'
  return new Date(timestamp * 1000).toLocaleTimeString()
}

const formatEvent = (event) => eventLabels[event?.type] || event?.type || 'Unknown'

export default function ShadowBlankMonitor() {
  const [config, setConfig] = useState(null)
  const [editedConfig, setEditedConfig] = useState(null)
  const [status, setStatus] = useState(null)
  const [excludedIds, setExcludedIds] = useState('')
  const [excludedUuids, setExcludedUuids] = useState('')
  const [loading, setLoading] = useState(true)
  const [actionLoading, setActionLoading] = useState('')
  const { toast } = useToast()

  useEffect(() => {
    loadData()
    const interval = setInterval(loadStatus, 5000)
    return () => clearInterval(interval)
  }, [])

  const watchedChannels = status?.watched_channels || []
  const recentEvents = status?.recent_events || []
  const cooldownCount = status?.cooldowns?.length || 0

  const lastEvent = useMemo(() => recentEvents[0] || null, [recentEvents])

  const loadData = async () => {
    try {
      const [configResponse, statusResponse] = await Promise.all([
        shadowBlankMonitorAPI.getConfig(),
        shadowBlankMonitorAPI.getStatus(),
      ])
      const nextConfig = configResponse.data || {}
      setConfig(nextConfig)
      setEditedConfig(nextConfig)
      setExcludedIds((nextConfig.excluded_channel_ids || []).join(', '))
      setExcludedUuids((nextConfig.excluded_channel_uuids || []).join(', '))
      setStatus(statusResponse.data || {})
    } catch (err) {
      console.error('Failed to load shadow monitor data:', err)
      toast({
        title: 'Error',
        description: 'Failed to load shadow monitor status',
        variant: 'destructive',
      })
    } finally {
      setLoading(false)
    }
  }

  const loadStatus = async () => {
    try {
      const response = await shadowBlankMonitorAPI.getStatus()
      setStatus(response.data || {})
    } catch (err) {
      console.error('Failed to load shadow monitor status:', err)
    }
  }

  const updateConfigValue = (field, value) => {
    setEditedConfig(prev => ({
      ...(prev || {}),
      [field]: value,
    }))
  }

  const saveConfig = async (extra = {}) => {
    try {
      setActionLoading('save')
      const payload = {
        ...(editedConfig || {}),
        excluded_channel_ids: parseCsv(excludedIds, true),
        excluded_channel_uuids: parseCsv(excludedUuids),
        ...extra,
      }
      const response = await shadowBlankMonitorAPI.updateConfig(payload)
      const nextConfig = response.data || {}
      setConfig(nextConfig)
      setEditedConfig(nextConfig)
      setExcludedIds((nextConfig.excluded_channel_ids || []).join(', '))
      setExcludedUuids((nextConfig.excluded_channel_uuids || []).join(', '))
      await loadStatus()
      toast({ title: 'Saved', description: 'Shadow monitor configuration updated' })
    } catch (err) {
      toast({
        title: 'Error',
        description: err.response?.data?.error || 'Failed to save shadow monitor configuration',
        variant: 'destructive',
      })
    } finally {
      setActionLoading('')
    }
  }

  const runAction = async (name, action, success) => {
    try {
      setActionLoading(name)
      await action()
      await loadData()
      toast({ title: 'Success', description: success })
    } catch (err) {
      toast({
        title: 'Error',
        description: err.response?.data?.error || 'Shadow monitor action failed',
        variant: 'destructive',
      })
    } finally {
      setActionLoading('')
    }
  }

  if (loading || !editedConfig) {
    return (
      <div className="flex items-center justify-center h-64">
        <Loader2 className="h-8 w-8 animate-spin text-muted-foreground" />
      </div>
    )
  }

  const running = Boolean(status?.running)
  const enabled = Boolean(editedConfig?.enabled)
  const dryRun = Boolean(editedConfig?.dry_run)
  const watchMode = editedConfig?.watch_mode || 'periodic'
  const hasKey = Boolean(config?.has_watcher_api_key)

  return (
    <div className="space-y-6">
      <div className="flex flex-col gap-4 md:flex-row md:items-start md:justify-between">
        <div>
          <h1 className="text-3xl font-bold tracking-tight">Shadow Monitor</h1>
          <p className="text-muted-foreground">Active viewer blank detection and stream switching</p>
        </div>
        <div className="flex flex-wrap gap-2">
          <Button
            onClick={() => saveConfig()}
            disabled={actionLoading !== ''}
          >
            {actionLoading === 'save' ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : <Save className="mr-2 h-4 w-4" />}
            Save
          </Button>
          {running ? (
            <Button
              variant="outline"
              onClick={() => runAction('stop', shadowBlankMonitorAPI.stop, 'Shadow monitor stopped')}
              disabled={actionLoading !== ''}
            >
              {actionLoading === 'stop' ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : <StopCircle className="mr-2 h-4 w-4" />}
              Stop
            </Button>
          ) : (
            <Button
              variant="outline"
              onClick={() => runAction('start', shadowBlankMonitorAPI.start, 'Shadow monitor started')}
              disabled={actionLoading !== ''}
            >
              {actionLoading === 'start' ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : <PlayCircle className="mr-2 h-4 w-4" />}
              Start
            </Button>
          )}
          <Button
            variant="outline"
            onClick={() => runAction('scan', shadowBlankMonitorAPI.runOnce, 'Shadow monitor scan completed')}
            disabled={actionLoading !== ''}
          >
            {actionLoading === 'scan' ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : <RefreshCw className="mr-2 h-4 w-4" />}
            Scan Now
          </Button>
        </div>
      </div>

      <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-4">
        <Card>
          <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
            <CardTitle className="text-sm font-medium">Service</CardTitle>
            <Activity className="h-4 w-4 text-muted-foreground" />
          </CardHeader>
          <CardContent>
            <Badge variant={running ? 'default' : 'secondary'} className={running ? 'bg-green-500' : ''}>
              {running ? <CheckCircle2 className="mr-1 h-3 w-3" /> : null}
              {running ? 'Running' : 'Stopped'}
            </Badge>
            <p className="mt-2 text-xs text-muted-foreground">{enabled ? 'Enabled' : 'Disabled'}</p>
          </CardContent>
        </Card>

        <Card>
          <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
            <CardTitle className="text-sm font-medium">Watched Channels</CardTitle>
            <Eye className="h-4 w-4 text-muted-foreground" />
          </CardHeader>
          <CardContent>
            <div className="text-2xl font-bold">{status?.watched_count || watchedChannels.length}</div>
            <p className="text-xs text-muted-foreground">Active channels with viewers</p>
          </CardContent>
        </Card>

        <Card>
          <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
            <CardTitle className="text-sm font-medium">Switching</CardTitle>
            <Shield className="h-4 w-4 text-muted-foreground" />
          </CardHeader>
          <CardContent>
            <Badge variant={dryRun ? 'outline' : 'default'}>{dryRun ? 'Dry Run' : 'Live'}</Badge>
            <p className="mt-2 text-xs text-muted-foreground">{cooldownCount} channel cooldowns</p>
          </CardContent>
        </Card>

        <Card>
          <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
            <CardTitle className="text-sm font-medium">Last Scan</CardTitle>
            {status?.last_error ? (
              <AlertCircle className="h-4 w-4 text-destructive" />
            ) : (
              <RefreshCw className="h-4 w-4 text-muted-foreground" />
            )}
          </CardHeader>
          <CardContent>
            <div className="text-2xl font-bold">{formatTime(status?.last_scan_at)}</div>
            <p className="text-xs text-muted-foreground truncate">
              {status?.last_error || (lastEvent ? formatEvent(lastEvent) : 'No events')}
            </p>
          </CardContent>
        </Card>
      </div>

      <div className="grid gap-4 lg:grid-cols-[minmax(0,1.1fr)_minmax(0,0.9fr)]">
        <Card>
          <CardHeader>
            <CardTitle className="text-lg">Configuration</CardTitle>
            <CardDescription>Detection, switching, and watcher identity</CardDescription>
          </CardHeader>
          <CardContent className="space-y-6">
            <div className="grid gap-4 md:grid-cols-2">
              <div className="flex items-center justify-between rounded-md border p-3">
                <div>
                  <Label className="text-sm font-medium">Enabled</Label>
                  <p className="text-xs text-muted-foreground">Auto-starts with the backend</p>
                </div>
                <Switch checked={enabled} onCheckedChange={(value) => updateConfigValue('enabled', value)} />
              </div>

              <div className="flex items-center justify-between rounded-md border p-3">
                <div>
                  <Label className="text-sm font-medium">Dry Run</Label>
                  <p className="text-xs text-muted-foreground">Records intended switches only</p>
                </div>
                <Switch checked={dryRun} onCheckedChange={(value) => updateConfigValue('dry_run', value)} />
              </div>

              <div className="flex items-center justify-between rounded-md border p-3 md:col-span-2">
                <div>
                  <Label className="text-sm font-medium">Freeze Detection</Label>
                  <p className="text-xs text-muted-foreground">Switch when the active picture is stuck but not black</p>
                </div>
                <Switch
                  checked={Boolean(editedConfig.freeze_detection_enabled)}
                  onCheckedChange={(value) => updateConfigValue('freeze_detection_enabled', value)}
                />
              </div>

              <div className="rounded-md border p-3 md:col-span-2">
                <Label className="text-sm font-medium">Watch Mode</Label>
                <div className="mt-3 grid gap-2 sm:grid-cols-2">
                  <Button
                    type="button"
                    variant={watchMode === 'periodic' ? 'default' : 'outline'}
                    onClick={() => updateConfigValue('watch_mode', 'periodic')}
                  >
                    Periodic
                  </Button>
                  <Button
                    type="button"
                    variant={watchMode === 'continuous' ? 'default' : 'outline'}
                    onClick={() => updateConfigValue('watch_mode', 'continuous')}
                  >
                    Continuous
                  </Button>
                </div>
              </div>
            </div>

            <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-3">
              {numberFields.map(field => (
                <div key={field.key} className="space-y-2">
                  <Label htmlFor={field.key}>{field.label}</Label>
                  <div className="flex items-center gap-2">
                    <Input
                      id={field.key}
                      type="number"
                      min={field.min}
                      max={field.max}
                      value={editedConfig[field.key] ?? ''}
                      onChange={(event) => updateConfigValue(field.key, Number(event.target.value))}
                    />
                    <span className="w-16 shrink-0 text-xs text-muted-foreground">{field.suffix}</span>
                  </div>
                </div>
              ))}
            </div>

            <Separator />

            <div className="grid gap-4 md:grid-cols-3">
              {thresholdFields.map(field => (
                <div key={field.key} className="space-y-2">
                  <Label htmlFor={field.key}>{field.label}</Label>
                  <Input
                    id={field.key}
                    type="number"
                    step={field.step}
                    min={field.min}
                    max={field.max}
                    value={editedConfig[field.key] ?? ''}
                    onChange={(event) => updateConfigValue(field.key, Number(event.target.value))}
                  />
                </div>
              ))}
            </div>

            <Separator />

            <div className="space-y-2">
              <Label htmlFor="watcher_user_agent">Watcher User Agent</Label>
              <Input
                id="watcher_user_agent"
                value={editedConfig.watcher_user_agent || ''}
                onChange={(event) => updateConfigValue('watcher_user_agent', event.target.value)}
              />
            </div>

            <div className="grid gap-4 md:grid-cols-[minmax(0,1fr)_auto] md:items-end">
              <div className="space-y-2">
                <Label htmlFor="watcher_api_key">Watcher API Key</Label>
                <Input
                  id="watcher_api_key"
                  type="password"
                  value={editedConfig.watcher_api_key || ''}
                  placeholder={hasKey ? 'Configured' : ''}
                  onChange={(event) => updateConfigValue('watcher_api_key', event.target.value)}
                />
                <p className="text-xs text-muted-foreground">
                  Use a dedicated watcher or playback user key here, not an admin or primary account key.
                </p>
              </div>
              <Button
                variant="outline"
                onClick={() => saveConfig({ watcher_api_key: '', clear_watcher_api_key: true })}
                disabled={actionLoading !== '' || !hasKey}
              >
                Clear Key
              </Button>
            </div>

            <div className="grid gap-4 md:grid-cols-2">
              <div className="space-y-2">
                <Label htmlFor="excluded_channel_ids">Excluded Channel IDs</Label>
                <Input
                  id="excluded_channel_ids"
                  value={excludedIds}
                  onChange={(event) => setExcludedIds(event.target.value)}
                />
              </div>
              <div className="space-y-2">
                <Label htmlFor="excluded_channel_uuids">Excluded Channel UUIDs</Label>
                <Input
                  id="excluded_channel_uuids"
                  value={excludedUuids}
                  onChange={(event) => setExcludedUuids(event.target.value)}
                />
              </div>
            </div>
          </CardContent>
        </Card>

        <div className="space-y-4">
          <Card>
            <CardHeader>
              <CardTitle className="text-lg">Watched Now</CardTitle>
              <CardDescription>Channels with active non-watcher clients</CardDescription>
            </CardHeader>
            <CardContent>
              {watchedChannels.length === 0 ? (
                <p className="text-sm text-muted-foreground">No watched channels</p>
              ) : (
                <div className="space-y-3">
                  {watchedChannels.map(channel => (
                    <div key={channel.channel_ref} className="rounded-md border p-3">
                      <div className="flex items-center justify-between gap-3">
                        <span className="font-mono text-xs">{channel.channel_ref}</span>
                        <Badge variant="outline">{channel.real_client_count || 0} viewers</Badge>
                      </div>
                      <div className="mt-2 flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
                        <span>{channel.stream_ref}</span>
                        {channel.last_event && <Badge variant="secondary">{formatEvent(channel.last_event)}</Badge>}
                        {channel.last_probe?.freeze_detected && <Badge variant="outline">Frozen</Badge>}
                        {channel.last_probe?.blank_detected && <Badge variant="outline">Blank</Badge>}
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle className="text-lg">Recent Events</CardTitle>
              <CardDescription>Latest monitor decisions</CardDescription>
            </CardHeader>
            <CardContent>
              {recentEvents.length === 0 ? (
                <p className="text-sm text-muted-foreground">No events recorded</p>
              ) : (
                <div className="space-y-3">
                  {recentEvents.slice(0, 8).map((event, index) => (
                    <div key={`${event.timestamp}-${index}`} className="flex items-start justify-between gap-3 rounded-md border p-3">
                      <div className="min-w-0">
                        <div className="flex items-center gap-2">
                          <Badge variant={event.type === 'switch_success' ? 'default' : 'secondary'}>
                            {formatEvent(event)}
                          </Badge>
                          <span className="text-xs text-muted-foreground">{formatTime(event.timestamp)}</span>
                        </div>
                        <p className="mt-2 truncate font-mono text-xs text-muted-foreground">
                          {event.channel_ref} / {event.stream_ref}
                        </p>
                      </div>
                      <span className="shrink-0 text-xs text-muted-foreground">{event.real_client_count || 0} viewers</span>
                    </div>
                  ))}
                </div>
              )}
            </CardContent>
          </Card>
        </div>
      </div>
    </div>
  )
}
