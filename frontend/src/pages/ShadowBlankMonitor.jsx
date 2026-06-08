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
  formatViewerClientCount,
  formatWatcherClientCount,
  getProgramDisplayLabel,
} from '@/lib/viewer-activity-display.js'
import {
  shadowMonitorNumberFields,
  shadowMonitorThresholdFields,
} from '@/lib/shadow-monitor-config-fields.js'
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

const eventLabels = {
  probe_ok: 'Probe OK',
  blank_pending: 'Blank Pending',
  freeze_pending: 'Freeze Pending',
  garbled_audio_pending: 'Garbled Audio Pending',
  silent_audio_pending: 'Silent Audio Pending',
  offline_image_pending: 'Offline Image Pending',
  dry_run_switch: 'Dry Run Switch',
  switch_success: 'Switch Success',
  switch_failed: 'Switch Failed',
  no_alternative: 'No Alternative',
  cooldown: 'Cooldown',
  stale_stream_guard: 'Stale Stream',
  switch_rate_limited: 'Rate Limited',
  viewer_left: 'Viewer Left',
  quality_check_active: 'Quality Check Active',
  watcher_reconnecting: 'Watcher Reconnecting',
  watcher_recovered: 'Watcher Recovered',
  pre_probe_unavailable: 'Pre-Probe Unavailable',
  pre_probe_rejected: 'Pre-Probe Rejected',
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

const formatDuration = (seconds) => {
  const value = Number(seconds)
  if (!Number.isFinite(value) || value < 0) return null
  if (value < 60) return `${Math.floor(value)}s`
  const minutes = Math.floor(value / 60)
  const hours = Math.floor(minutes / 60)
  if (hours > 0) return `${hours}h ${minutes % 60}m`
  return `${minutes}m`
}

const formatEvent = (event) => eventLabels[event?.type] || event?.type || 'Unknown'

export default function ShadowBlankMonitor() {
  const [config, setConfig] = useState(null)
  const [editedConfig, setEditedConfig] = useState(null)
  const [status, setStatus] = useState(null)
  const [excludedIds, setExcludedIds] = useState('')
  const [excludedUuids, setExcludedUuids] = useState('')
  const [offlineImageHashes, setOfflineImageHashes] = useState('')
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
      setOfflineImageHashes((nextConfig.offline_image_reference_hashes || []).join(', '))
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
        offline_image_reference_hashes: parseCsv(offlineImageHashes),
        ...extra,
      }
      const response = await shadowBlankMonitorAPI.updateConfig(payload)
      const nextConfig = response.data || {}
      setConfig(nextConfig)
      setEditedConfig(nextConfig)
      setExcludedIds((nextConfig.excluded_channel_ids || []).join(', '))
      setExcludedUuids((nextConfig.excluded_channel_uuids || []).join(', '))
      setOfflineImageHashes((nextConfig.offline_image_reference_hashes || []).join(', '))
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
  const watchMode = editedConfig?.watch_mode || 'continuous'
  const hasKey = Boolean(config?.has_watcher_api_key)
  const configurationRequired = Boolean(status?.configuration_required) || !hasKey
  const configurationMessage = status?.configuration_message || 'Save a Watcher API Key before starting the monitor.'
  const canUseWatcher = actionLoading === '' && !configurationRequired
  const continuousWatcherActive = running && watchMode === 'continuous'

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
              disabled={!canUseWatcher}
            >
              {actionLoading === 'start' ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : <PlayCircle className="mr-2 h-4 w-4" />}
              Start
            </Button>
          )}
          {!continuousWatcherActive && (
            <Button
              variant="outline"
              onClick={() => runAction('scan', shadowBlankMonitorAPI.runOnce, 'Shadow monitor scan completed')}
              disabled={!canUseWatcher}
            >
              {actionLoading === 'scan' ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : <RefreshCw className="mr-2 h-4 w-4" />}
              Scan Now
            </Button>
          )}
        </div>
      </div>

      {configurationRequired ? (
        <div className="flex items-start gap-3 rounded-md border border-amber-500/40 bg-amber-500/10 p-4 text-sm text-amber-900 dark:text-amber-100">
          <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" />
          <div>
            <p className="font-medium">Watcher setup required</p>
            <p className="mt-1">{configurationMessage}</p>
          </div>
        </div>
      ) : null}

      <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-4">
        <Card>
          <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
            <CardTitle className="text-sm font-medium">Service</CardTitle>
            <Activity className="h-4 w-4 text-muted-foreground" />
          </CardHeader>
          <CardContent>
            <Badge
              variant={configurationRequired ? 'outline' : running ? 'default' : 'secondary'}
              className={running ? 'bg-green-500' : configurationRequired ? 'border-amber-500/50 text-amber-700 dark:text-amber-200' : ''}
            >
              {configurationRequired ? <AlertCircle className="mr-1 h-3 w-3" /> : running ? <CheckCircle2 className="mr-1 h-3 w-3" /> : null}
              {configurationRequired ? 'Setup required' : running ? 'Running' : 'Stopped'}
            </Badge>
            <p className="mt-2 text-xs text-muted-foreground">
              {configurationRequired ? configurationMessage : enabled ? 'Enabled' : 'Disabled'}
            </p>
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
            <CardTitle>Configuration</CardTitle>
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

              <div className="flex items-center justify-between rounded-md border p-3">
                <div>
                  <Label className="text-sm font-medium">Garbled Audio</Label>
                  <p className="text-xs text-muted-foreground">Treat repeated audio decode errors as a media fault</p>
                </div>
                <Switch
                  checked={Boolean(editedConfig.garbled_audio_detection_enabled)}
                  onCheckedChange={(value) => updateConfigValue('garbled_audio_detection_enabled', value)}
                />
              </div>

              <div className="flex items-center justify-between rounded-md border p-3">
                <div>
                  <Label className="text-sm font-medium">Silent Audio</Label>
                  <p className="text-xs text-muted-foreground">Treat long audio silence as a media fault</p>
                </div>
                <Switch
                  checked={Boolean(editedConfig.silent_audio_detection_enabled)}
                  onCheckedChange={(value) => updateConfigValue('silent_audio_detection_enabled', value)}
                />
              </div>

              <div className="flex items-center justify-between rounded-md border p-3 md:col-span-2">
                <div>
                  <Label className="text-sm font-medium">Offline Image</Label>
                  <p className="text-xs text-muted-foreground">Detect provider offline slates by reference pHash</p>
                </div>
                <Switch
                  checked={Boolean(editedConfig.offline_image_detection_enabled)}
                  onCheckedChange={(value) => updateConfigValue('offline_image_detection_enabled', value)}
                />
              </div>

              <div className="flex items-center justify-between rounded-md border p-3 md:col-span-2">
                <div>
                  <Label className="text-sm font-medium">Next Stream Pre-Probe</Label>
                  <p className="text-xs text-muted-foreground">Validate the next candidate before switching</p>
                </div>
                <Switch
                  checked={Boolean(editedConfig.next_stream_pre_probe_enabled)}
                  onCheckedChange={(value) => updateConfigValue('next_stream_pre_probe_enabled', value)}
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
                <div className="mt-3 rounded-md border border-border bg-muted/30 p-3 text-sm text-muted-foreground">
                  Continuous mode keeps watching active viewer sessions as they appear. Use excludes for channels the watcher should ignore; `Scan Now` is hidden while continuous watching is already active.
                </div>
              </div>
            </div>

            <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-3">
              {shadowMonitorNumberFields.map(field => (
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
                  {field.help ? (
                    <p className="text-xs text-muted-foreground">{field.help}</p>
                  ) : null}
                </div>
              ))}
            </div>

            <Separator />

            <div className="grid gap-4 md:grid-cols-3">
              {shadowMonitorThresholdFields.map(field => (
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
              <Label htmlFor="offline_image_reference_hashes">Offline Image Reference pHashes</Label>
              <Input
                id="offline_image_reference_hashes"
                placeholder="comma-separated pHash values"
                value={offlineImageHashes}
                onChange={(event) => setOfflineImageHashes(event.target.value)}
              />
              <p className="text-xs text-muted-foreground">
                Offline-image switching stays disabled unless at least one reference hash is configured.
              </p>
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
                {!hasKey ? (
                  <p className="text-xs font-medium text-destructive">
                    Required before Start or Scan Now can run.
                  </p>
                ) : null}
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
                <Label htmlFor="excluded_channel_ids">Exclude Channel IDs</Label>
                <Input
                  id="excluded_channel_ids"
                  placeholder="None"
                  value={excludedIds}
                  onChange={(event) => setExcludedIds(event.target.value)}
                />
              </div>
              <div className="space-y-2">
                <Label htmlFor="excluded_channel_uuids">Exclude Channel UUIDs</Label>
                <Input
                  id="excluded_channel_uuids"
                  placeholder="None"
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
              <CardTitle>Watched Now</CardTitle>
              <CardDescription>Channels with active non-watcher clients</CardDescription>
            </CardHeader>
            <CardContent>
              {watchedChannels.length === 0 ? (
                <p className="text-sm text-muted-foreground">No watched channels</p>
              ) : (
                <div className="space-y-3">
                  {watchedChannels.map(channel => {
                    const programLabel = getProgramDisplayLabel(channel.current_program)
                    return (
                      <div key={channel.channel_ref} className="rounded-md border p-3">
                        <div className="flex items-center justify-between gap-3">
                          <span className="font-mono text-xs">{channel.channel_ref}</span>
                          <div className="flex shrink-0 flex-wrap justify-end gap-2">
                            <Badge variant="outline">{formatViewerClientCount(channel.real_client_count)}</Badge>
                            {(channel.watcher_client_count || 0) > 0 && (
                              <Badge variant="secondary">{formatWatcherClientCount(channel.watcher_client_count)}</Badge>
                            )}
                          </div>
                        </div>
                        {programLabel && (
                          <div className="mt-2 min-w-0 text-sm font-medium">
                            {programLabel}
                          </div>
                        )}
                        <div className="mt-2 flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
                          <span>{channel.stream_ref}</span>
                          {!programLabel && channel.watcher_state === 'waiting' && (
                            <span>Waiting for shadow watcher</span>
                          )}
                          {channel.watcher_client_ref && <span>{channel.watcher_client_ref}</span>}
                          {formatDuration(channel.watcher_uptime_seconds) && (
                            <span>watching for {formatDuration(channel.watcher_uptime_seconds)}</span>
                          )}
                          {channel.watcher_state === 'reconnecting' && (
                            <Badge variant="outline">Watcher reconnecting</Badge>
                          )}
                          {channel.watcher_state === 'reconnecting' && formatDuration(channel.watcher_absent_seconds) && (
                            <span>missing for {formatDuration(channel.watcher_absent_seconds)}</span>
                          )}
                          {channel.last_event?.type === 'watcher_recovered' && (
                            <Badge variant="secondary">Watcher recovered</Badge>
                          )}
                          {channel.last_event && !['watcher_reconnecting', 'watcher_recovered'].includes(channel.last_event.type) && (
                            <Badge variant="secondary">{formatEvent(channel.last_event)}</Badge>
                          )}
                          {channel.last_probe?.freeze_detected && <Badge variant="outline">Frozen</Badge>}
                          {channel.last_probe?.blank_detected && <Badge variant="outline">Blank</Badge>}
                          {channel.last_probe?.garbled_audio_detected && <Badge variant="outline">Garbled Audio</Badge>}
                          {channel.last_probe?.silent_audio_detected && <Badge variant="outline">Silent Audio</Badge>}
                          {channel.last_probe?.offline_image_detected && <Badge variant="outline">Offline Image</Badge>}
                        </div>
                      </div>
                    )
                  })}
                </div>
              )}
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle>Recent Events</CardTitle>
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
                      <span className="shrink-0 text-xs text-muted-foreground">{formatViewerClientCount(event.real_client_count)}</span>
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
