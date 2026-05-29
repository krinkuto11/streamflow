import { useState, useEffect } from 'react'
import { Link } from 'react-router-dom'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card.jsx'
import { Button } from '@/components/ui/button.jsx'
import { Badge } from '@/components/ui/badge.jsx'
import { Progress } from '@/components/ui/progress.jsx'
import { Alert, AlertDescription } from '@/components/ui/alert.jsx'
import { Label } from '@/components/ui/label.jsx'
import { Switch } from '@/components/ui/switch.jsx'
import { useToast } from '@/hooks/use-toast.js'
import { automationAPI, streamCheckerAPI, shadowBlankMonitorAPI, m3uAPI, dispatcharrAPI, environmentAPI } from '@/services/api.js'
import { formatDuration as formatDurationValue } from '@/lib/time-format.js'
import {
  PlayCircle, RefreshCw, Activity, CheckCircle2,
  Loader2, ChevronDown, Tv, Radio, Database, WifiOff, Eye
} from 'lucide-react'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
  DropdownMenuSeparator,
  DropdownMenuLabel,
} from '@/components/ui/dropdown-menu.jsx'
import UpcomingAutomationEvents from '@/components/Dashboard/UpcomingAutomationEvents.jsx'

const AUTOMATION_STAGES = [
  { id: 'settings', label: 'Preparing' },
  { id: 'period_discovery', label: 'Schedule' },
  { id: 'm3u_refresh', label: 'M3U Refresh' },
  { id: 'udi_sync', label: 'Cache Sync' },
  { id: 'stream_matching', label: 'Matching' },
  { id: 'quality_queueing', label: 'Queueing' },
  { id: 'quality_checking', label: 'Quality Check' },
  { id: 'finalizing', label: 'Finalizing' },
]

const formatDuration = (seconds) => {
  const formatted = formatDurationValue(seconds)
  return formatted || 'N/A'
}

const formatShadowEvent = (eventType) => {
  if (!eventType) return ''
  return eventType
    .split('_')
    .map(part => part.charAt(0).toUpperCase() + part.slice(1))
    .join(' ')
}

export default function Dashboard() {
  const [status, setStatus] = useState(null)
  const [automationConfig, setAutomationConfig] = useState(null)
  const [streamCheckerStatus, setStreamCheckerStatus] = useState(null)
  const [shadowMonitorStatus, setShadowMonitorStatus] = useState(null)
  const [playlists, setPlaylists] = useState([])
  const [loading, setLoading] = useState(true)
  const [actionLoading, setActionLoading] = useState('')
  const [togglingPlaylist, setTogglingPlaylist] = useState(null)
  const [periods, setPeriods] = useState([])
  const [udiStats, setUdiStats] = useState(null)
  const [udiSyncing, setUdiSyncing] = useState(false)
  // debug_mode gates the fault injection panel (Phase 5 — not yet built)
  const [debugMode, setDebugMode] = useState(false)
  const { toast } = useToast()

  useEffect(() => {
    loadStatus()
    loadPlaylists()
    loadPeriods()
    loadEnvironment()
    const interval = setInterval(() => {
      loadStatus()
      loadPlaylists()
    }, 30000)
    return () => clearInterval(interval)
  }, [])

  const loadStatus = async () => {
    try {
      const [automationResponse, streamCheckerResponse, automationConfigResponse, shadowMonitorResponse] = await Promise.all([
        automationAPI.getStatus(),
        streamCheckerAPI.getStatus(),
        automationAPI.getConfig(),
        shadowBlankMonitorAPI.getStatus().catch(() => ({ data: null })),
      ])
      setStatus(automationResponse.data)
      setStreamCheckerStatus(streamCheckerResponse.data)
      setAutomationConfig(automationConfigResponse.data || {})
      setShadowMonitorStatus(shadowMonitorResponse.data)
    } catch (err) {
      console.error('Failed to load status:', err)
      toast({
        title: "Error",
        description: "Failed to load automation status",
        variant: "destructive"
      })
    } finally {
      setLoading(false)
    }
  }

  const loadPlaylists = async () => {
    try {
      const response = await m3uAPI.getAccounts()
      setPlaylists(response.data.accounts || [])
    } catch (err) {
      console.error('Failed to load playlists:', err)
    }
  }

  const loadPeriods = async () => {
    try {
      const response = await automationAPI.getPeriods()
      setPeriods(response.data || [])
    } catch (err) {
      console.error('Failed to load periods:', err)
    }
  }

  const loadEnvironment = async () => {
    try {
      const response = await environmentAPI.getEnvironment()
      setDebugMode(response.data?.debug_mode === true)
    } catch (err) {
      console.error('Failed to load environment:', err)
    }
  }

  // On mount, poll getInitializationStatus until the UDI reports completed or
  // failed.  This handles the common case where the page loads while the
  // background startup refresh is still running — counts populate automatically
  // once the refresh finishes rather than requiring a manual Reload UDI.
  // Also handles navigating away and back — if already completed, the first
  // poll resolves immediately and the interval is cleared.
  useEffect(() => {
    let udiPollInterval = null

    const pollUdiStatus = async () => {
      // Don't poll if a manual reload already populated stats
      if (udiStats !== null) {
        clearInterval(udiPollInterval)
        return
      }
      try {
        const res = await dispatcharrAPI.getInitializationStatus()
        const data = res.data || {}
        const ec = data.entity_counts || {}

        if (data.status === 'completed' || data.status === 'failed') {
          clearInterval(udiPollInterval)
          setUdiStats(prev => {
            if (prev !== null) return prev
            return {
              syncStatus:         data.status,
              channels_count:     ec.channels?.received     ?? null,
              streams_count:      ec.streams?.received      ?? null,
              m3u_accounts_count: ec.m3u_accounts?.received ?? null,
            }
          })
        }
        // status is 'in_progress' or 'idle' — keep polling
      } catch (err) {
        console.error('UDI status poll error:', err)
      }
    }

    // Poll immediately, then every 3 seconds until resolved
    pollUdiStatus()
    udiPollInterval = setInterval(pollUdiStatus, 3000)

    return () => clearInterval(udiPollInterval)
  }, []) // eslint-disable-line react-hooks/exhaustive-deps

  // Reload UDI: the POST blocks until the full refresh completes and returns
  // real entity counts.  We use those counts directly — no timer, no polling.
  const handleReloadUDI = async () => {
    try {
      setActionLoading('udi')
      setUdiSyncing(true)

      const res = await dispatcharrAPI.initializeUDI()
      const counts = res.data?.data || {}

      const countParts = [
        counts.channels_count     != null && `${counts.channels_count.toLocaleString()} channels`,
        counts.streams_count      != null && `${counts.streams_count.toLocaleString()} streams`,
        counts.m3u_accounts_count != null && `${counts.m3u_accounts_count} playlists`,
      ].filter(Boolean)

      setUdiStats({
        syncStatus:        'completed',
        channels_count:    counts.channels_count,
        streams_count:     counts.streams_count,
        m3u_accounts_count: counts.m3u_accounts_count,
      })

      toast({
        title: "UDI Synced",
        description: countParts.length > 0
          ? countParts.join(' · ')
          : "Dispatcharr data refreshed successfully",
      })

      await loadStatus()
      await loadPlaylists()

    } catch (err) {
      // The request may have timed out while the backend completed successfully.
      // Poll the status endpoint to recover whatever counts the backend loaded,
      // so the tiles show real numbers rather than staying at '—'.
      try {
        const statusRes = await dispatcharrAPI.getInitializationStatus()
        const statusData = statusRes.data || {}
        const ec = statusData.entity_counts || {}

        setUdiStats({
          syncStatus:         statusData.status || 'failed',
          channels_count:     ec.channels?.received     ?? null,
          streams_count:      ec.streams?.received      ?? null,
          m3u_accounts_count: ec.m3u_accounts?.received ?? null,
        })
      } catch (_) { /* ignore secondary error — tiles stay at '—' */ }

      toast({
        title: "UDI Sync Failed",
        description: err.response?.data?.error || "Check logs for details",
        variant: "destructive",
      })
    } finally {
      setUdiSyncing(false)
      setActionLoading('')
    }
  }

  const handleRunAutomation = async (periodId = null) => {
    try {
      setActionLoading('automation')
      await automationAPI.runCycle({ period_id: periodId })
      toast({
        title: "Success",
        description: periodId
          ? `Automation cycle for "${periods.find(p => p.id === periodId)?.name}" triggered successfully`
          : "Full automation cycle triggered successfully"
      })
      await loadStatus()
    } catch (err) {
      toast({
        title: "Error",
        description: err.response?.data?.error || "Failed to run automation cycle",
        variant: "destructive"
      })
    } finally {
      setActionLoading('')
    }
  }

  const handleTogglePlaylist = async (playlistId, currentlyEnabled) => {
    try {
      setTogglingPlaylist(playlistId)
      const normalizedPlaylistId = Number(playlistId)

      const currentEnabledAccounts = (automationConfig?.enabled_m3u_accounts || [])
        .map(id => Number(id))
        .filter(Number.isFinite)
      let newEnabledAccounts

      if (currentEnabledAccounts.length === 0) {
        if (currentlyEnabled) {
          newEnabledAccounts = playlists
            .map(p => Number(p.id))
            .filter(Number.isFinite)
            .filter(id => id !== normalizedPlaylistId)
        } else {
          newEnabledAccounts = []
        }
      } else {
        if (currentlyEnabled) {
          newEnabledAccounts = currentEnabledAccounts.filter(id => id !== normalizedPlaylistId)
        } else {
          newEnabledAccounts = [...currentEnabledAccounts, normalizedPlaylistId]
          if (newEnabledAccounts.length === playlists.length) {
            newEnabledAccounts = []
          }
        }
      }

      await automationAPI.updateConfig({ enabled_m3u_accounts: newEnabledAccounts })
      toast({ title: "Success", description: `Playlist ${currentlyEnabled ? 'disabled' : 'enabled'} successfully` })
      await loadStatus()
      await loadPlaylists()
    } catch (err) {
      toast({ title: "Error", description: "Failed to toggle playlist", variant: "destructive" })
    } finally {
      setTogglingPlaylist(null)
    }
  }

  if (loading) {
    return (
      <div className="flex items-center justify-center h-64">
        <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-primary"></div>
      </div>
    )
  }

  const isAutomationRunning = status?.running || false
  const runStatus = status?.run_status || {}
  const runCounts = runStatus.counts || {}
  const runDurations = runStatus.durations || {}
  const udiStatus = status?.udi_status || {}
  const apiTiming = udiStatus?.api_timing || {}
  const runState = runStatus.state || 'idle'
  const runStage = runStatus.stage || 'idle'
  const runStageLabel = runStatus.stage_label || 'Idle'
  const currentStageIndex = AUTOMATION_STAGES.findIndex((stage) => stage.id === runStage)
  const runProgress = runStatus.progress || {}
  const runningRun = runState === 'running'
  const failedRun = runState === 'failed'
  const completedRun = runState === 'completed'
  const skippedRun = runState === 'skipped'
  const runBadgeClass = failedRun
    ? 'bg-destructive text-destructive-foreground border-transparent'
    : completedRun
      ? 'bg-green-600 text-white border-transparent'
      : runningRun
        ? 'bg-blue-600 text-white border-transparent'
        : ''
  const queueSize     = streamCheckerStatus?.queue?.queue_size || 0
  const completed     = streamCheckerStatus?.queue?.completed  || 0
  const inProgress    = streamCheckerStatus?.queue?.in_progress || 0
  const totalProcessed = completed
  const batchTotal    = completed + inProgress + queueSize
  const queueProgress = batchTotal > 0 ? (completed / batchTotal) * 100 : 0
  const isProcessing  = streamCheckerStatus?.stream_checking_mode || false
  const qualityStageActive = runStage === 'quality_checking' && batchTotal > 0
  const rawRunProgressPercent = Number(runProgress.percent)
  const runStageProgress = qualityStageActive
    ? queueProgress
    : Number.isFinite(rawRunProgressPercent)
      ? rawRunProgressPercent
      : 0
  const runProgressCurrent = qualityStageActive ? completed : runProgress.current
  const runProgressTotal = qualityStageActive ? batchTotal : runProgress.total
  const hasRunProgressTotal = runProgressTotal !== null && runProgressTotal !== undefined
  const runProgressDetail = hasRunProgressTotal
    ? `${runProgressCurrent ?? 0} of ${runProgressTotal}`
    : runProgress.message || runStatus.message || 'Waiting for progress'
  const showRunProgress = runState !== 'idle' || Object.keys(runProgress).length > 0
  const qualityCheckedCount = qualityStageActive
    ? completed
    : (runCounts.quality_checked ?? 0)
  const shouldDisableActions = isProcessing || actionLoading !== ''
  const shadowWatchedCount = shadowMonitorStatus?.watched_count || shadowMonitorStatus?.watched_channels?.length || 0
  const shadowLastEvent = shadowMonitorStatus?.recent_events?.[0]

  const syncStatus = udiStats?.syncStatus
  const syncBadgeClass =
    syncStatus === 'completed' ? 'bg-green-600 text-white border-transparent' :
    syncStatus === 'failed'    ? 'bg-destructive text-destructive-foreground border-transparent' :
    ''

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-3xl font-bold tracking-tight">Dashboard</h1>
        <p className="text-muted-foreground">Monitor and control your stream automation</p>
      </div>

      {/* Active Operations Alert */}
      {isProcessing && (
        <Alert className="border-blue-500 bg-blue-50 dark:bg-blue-950">
          <Loader2 className="h-4 w-4 animate-spin text-blue-600" />
          <AlertDescription className="text-blue-900 dark:text-blue-100">
            <div className="font-medium mb-1">Stream checker is actively processing</div>
            <div className="text-sm">
              {completed} of {batchTotal} channels completed
              {inProgress > 0 && ` (${inProgress} in progress, ${queueSize} in queue)`}
            </div>
            <Progress value={queueProgress} className="mt-2 h-2" />
            <div className="text-xs mt-1 text-muted-foreground">Quick actions are temporarily disabled</div>
          </AlertDescription>
        </Alert>
      )}

      {showRunProgress && (
        <Card>
          <CardHeader className="space-y-1">
            <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
              <div>
                <CardTitle>Automation Run</CardTitle>
                <CardDescription>{runProgress.message || 'Automation run status'}</CardDescription>
              </div>
              <Badge
                variant={runProgress.status === 'completed' ? 'default' : runProgress.status === 'failed' ? 'destructive' : 'outline'}
                className="w-fit"
              >
                {runProgress.status}
              </Badge>
            </div>
          </CardHeader>
          <CardContent className="space-y-4">
            <div className="grid gap-3 md:grid-cols-4">
              <div className="rounded-md border p-3">
                <Label className="text-xs text-muted-foreground">Current Stage</Label>
                <div className="mt-1 text-xl font-semibold">{runProgress.stage_label || 'Idle'}</div>
              </div>
              <div className="rounded-md border p-3">
                <Label className="text-xs text-muted-foreground">Elapsed</Label>
                <div className="mt-1 text-xl font-semibold">{formatDuration(runProgress.elapsed_seconds)}</div>
              </div>
              <div className="rounded-md border p-3">
                <Label className="text-xs text-muted-foreground">Stage Time</Label>
                <div className="mt-1 text-xl font-semibold">{formatDuration(runProgress.stage_elapsed_seconds)}</div>
              </div>
              <div className="rounded-md border p-3">
                <Label className="text-xs text-muted-foreground">Progress</Label>
                <div className="mt-1 text-xl font-semibold">{Math.round(runStageProgress)}%</div>
              </div>
            </div>

            <div>
              <div className="mb-2 flex items-center justify-between gap-3 text-sm">
                <span className="text-muted-foreground">{runProgressDetail}</span>
                {runStage === 'quality_checking' && batchTotal > 0 && (
                  <span className="text-muted-foreground">
                    {completed} of {batchTotal} quality checks
                  </span>
                )}
              </div>
              <Progress value={runStageProgress} className="h-2" />
            </div>

            <div className="grid gap-2 md:grid-cols-4 xl:grid-cols-7">
              {(runProgress.stages || []).map(stage => (
                <div
                  key={stage.key}
                  className={`rounded-md border px-3 py-2 ${
                    stage.status === 'running'
                      ? 'border-primary bg-primary/10'
                      : stage.status === 'completed'
                        ? 'border-green-500/60 bg-green-500/10'
                        : stage.status === 'failed'
                          ? 'border-destructive/70 bg-destructive/10'
                          : ''
                  }`}
                >
                  <div className="flex items-center justify-between gap-2">
                    <span className="text-sm font-medium">{stage.label}</span>
                    {stage.status === 'running' && <Loader2 className="h-3.5 w-3.5 animate-spin text-primary" />}
                    {stage.status === 'completed' && <CheckCircle2 className="h-3.5 w-3.5 text-green-500" />}
                  </div>
                  <div className="mt-1 text-xs text-muted-foreground">
                    {stage.total ? `${stage.current || 0}/${stage.total}` : stage.status}
                  </div>
                </div>
              ))}
            </div>
          </CardContent>
        </Card>
      )}

      {/* Status Cards */}
      <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-4">
        <Card>
          <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
            <CardTitle className="text-sm font-medium">Automation Status</CardTitle>
            <Activity className="h-4 w-4 text-muted-foreground" />
          </CardHeader>
          <CardContent>
            <div className="flex items-center gap-2">
              {isAutomationRunning ? (
                <Badge variant="default" className="bg-green-500">
                  <CheckCircle2 className="h-3 w-3 mr-1" />Running
                </Badge>
              ) : (
                <Badge variant="secondary">Stopped</Badge>
              )}
            </div>
            <p className="text-xs text-muted-foreground mt-2">Background automation service</p>
          </CardContent>
        </Card>

        <Card>
          <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
            <CardTitle className="text-sm font-medium">Stream Checker</CardTitle>
            <Activity className="h-4 w-4 text-muted-foreground" />
          </CardHeader>
          <CardContent>
            <div className="flex items-center gap-2">
              {streamCheckerStatus?.checking || (streamCheckerStatus?.queue?.in_progress > 0) ? (
                <Badge variant="default" className="bg-green-500">
                  <CheckCircle2 className="h-3 w-3 mr-1" />Normal Check
                </Badge>
              ) : (
                <Badge variant="secondary">Idle</Badge>
              )}
            </div>
            <p className="text-xs text-muted-foreground mt-2">Quality checking service</p>
          </CardContent>
        </Card>

        <Card>
          <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
            <CardTitle className="text-sm font-medium">Shadow Monitor</CardTitle>
            <Eye className="h-4 w-4 text-muted-foreground" />
          </CardHeader>
          <CardContent>
            <div className="flex flex-wrap items-center gap-2">
              {shadowMonitorStatus?.running ? (
                <Badge variant="default" className="bg-green-500">
                  <CheckCircle2 className="h-3 w-3 mr-1" />Watching
                </Badge>
              ) : shadowMonitorStatus?.enabled ? (
                <Badge variant="outline">Enabled</Badge>
              ) : (
                <Badge variant="secondary">Disabled</Badge>
              )}
              {shadowMonitorStatus?.dry_run && <Badge variant="outline">Dry Run</Badge>}
            </div>
            <p className="text-xs text-muted-foreground mt-2">
              <Link to="/shadow-monitor" className="hover:underline">
                {shadowWatchedCount} active channels
                {shadowLastEvent ? `, last ${formatShadowEvent(shadowLastEvent.type)}` : ''}
              </Link>
            </p>
          </CardContent>
        </Card>

        <Card>
          <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
            <CardTitle className="text-sm font-medium">Last Update</CardTitle>
            <RefreshCw className="h-4 w-4 text-muted-foreground" />
          </CardHeader>
          <CardContent>
            <div className="text-2xl font-bold">
              {status?.last_playlist_update
                ? new Date(status.last_playlist_update).toLocaleTimeString()
                : 'N/A'}
            </div>
            <p className="text-xs text-muted-foreground">Most recent activity</p>
          </CardContent>
        </Card>
      </div>

      {/* Quick Actions */}
      <Card>
        <CardHeader>
          <CardTitle>Quick Actions</CardTitle>
          <CardDescription>Perform common operations on your stream management system</CardDescription>
        </CardHeader>
        <CardContent>
          <div className="flex flex-col sm:flex-row gap-6">

            {/* Dispatcharr Cache Stats */}
            <div className="flex-1 border rounded-lg p-4 bg-muted/30 space-y-3">
              <div className="flex items-center justify-between">
                <span className="text-xs font-semibold uppercase tracking-wider text-muted-foreground">
                  Dispatcharr Cache
                </span>
                {udiSyncing ? (
                  <Badge variant="outline" className="text-xs gap-1 border-blue-400 text-blue-400">
                    <Loader2 className="h-3 w-3 animate-spin" />Syncing
                  </Badge>
                ) : syncStatus ? (
                  <Badge variant="outline" className={`text-xs ${syncBadgeClass}`}>
                    {syncStatus === 'completed' && <CheckCircle2 className="h-3 w-3 mr-1" />}
                    {syncStatus === 'failed'    && <WifiOff      className="h-3 w-3 mr-1" />}
                    {syncStatus === 'completed' ? 'Synced' : 'Failed'}
                  </Badge>
                ) : null}
              </div>

              <div className="grid grid-cols-3 gap-3">
                <div className="flex flex-col items-center justify-center rounded-md bg-background border p-3 gap-1">
                  <Tv className="h-4 w-4 text-muted-foreground mb-0.5" />
                  <span className="text-xl font-bold leading-none">
                    {udiStats?.channels_count != null
                      ? udiStats.channels_count.toLocaleString()
                      : <span className="text-muted-foreground text-base">—</span>}
                  </span>
                  <span className="text-[10px] text-muted-foreground uppercase tracking-wide">Channels</span>
                </div>

                <div className="flex flex-col items-center justify-center rounded-md bg-background border p-3 gap-1">
                  <Radio className="h-4 w-4 text-muted-foreground mb-0.5" />
                  <span className="text-xl font-bold leading-none">
                    {udiStats?.streams_count != null
                      ? udiStats.streams_count.toLocaleString()
                      : <span className="text-muted-foreground text-base">—</span>}
                  </span>
                  <span className="text-[10px] text-muted-foreground uppercase tracking-wide">Streams</span>
                </div>

                <div className="flex flex-col items-center justify-center rounded-md bg-background border p-3 gap-1">
                  <Database className="h-4 w-4 text-muted-foreground mb-0.5" />
                  <span className="text-xl font-bold leading-none">
                    {udiStats?.m3u_accounts_count != null
                      ? udiStats.m3u_accounts_count
                      : playlists.length > 0
                        ? playlists.length
                        : <span className="text-muted-foreground text-base">—</span>}
                  </span>
                  <span className="text-[10px] text-muted-foreground uppercase tracking-wide">Playlists</span>
                </div>
              </div>

              <p className="text-[11px] text-muted-foreground">
                {udiSyncing
                  ? 'Fetching data from Dispatcharr...'
                  : udiStats
                    ? 'Counts reflect the last completed sync'
                    : 'Reload UDI to populate channel and stream counts'}
              </p>
            </div>

            {/* Action Buttons */}
            <div className="flex flex-col justify-center gap-3 sm:min-w-[180px]">
              <Button
                onClick={handleReloadUDI}
                disabled={shouldDisableActions || udiSyncing}
                className="w-full"
              >
                {udiSyncing
                  ? <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                  : <RefreshCw className="mr-2 h-4 w-4" />}
                {udiSyncing ? 'Syncing...' : 'Reload UDI'}
              </Button>

              <DropdownMenu>
                <DropdownMenuTrigger asChild>
                  <Button disabled={shouldDisableActions} variant="outline" className="w-full">
                    <PlayCircle className="mr-2 h-4 w-4" />
                    {actionLoading === 'automation' ? 'Running...' : 'Run Automation'}
                    <ChevronDown className="ml-2 h-4 w-4 opacity-50" />
                  </Button>
                </DropdownMenuTrigger>
                <DropdownMenuContent align="start" className="w-[200px]">
                  <DropdownMenuLabel>Choose Run Mode</DropdownMenuLabel>
                  <DropdownMenuSeparator />
                  <DropdownMenuItem onClick={() => handleRunAutomation(null)}>Run All Periods</DropdownMenuItem>
                  {periods.length > 0 && (
                    <>
                      <DropdownMenuSeparator />
                      <DropdownMenuLabel className="text-[10px] uppercase text-muted-foreground">
                        Specific Periods
                      </DropdownMenuLabel>
                      {periods.map(period => (
                        <DropdownMenuItem key={period.id} onClick={() => handleRunAutomation(period.id)}>
                          {period.name}
                        </DropdownMenuItem>
                      ))}
                    </>
                  )}
                </DropdownMenuContent>
              </DropdownMenu>
            </div>
          </div>
        </CardContent>
      </Card>

      {/* System Information */}
      <div className="grid gap-4 md:grid-cols-2">
        <Card>
          <CardHeader><CardTitle>Automation Configuration</CardTitle></CardHeader>
          <CardContent>
            <dl className="space-y-3 text-sm">
              <div className="flex justify-between items-center">
                <dt className="text-muted-foreground">Active Profiles:</dt>
                <dd><Badge variant="secondary">{status?.profiles_count || 0}</Badge></dd>
              </div>
              <div className="flex justify-between items-center">
                <dt className="text-muted-foreground">Scheduled Periods:</dt>
                <dd><Badge variant="outline">{periods.length || 0}</Badge></dd>
              </div>
              <div className="flex justify-between items-center">
                <dt className="text-muted-foreground">Stream Checking:</dt>
                <dd>
                  <Badge variant={status?.stream_checking_enabled ? "default" : "secondary"}>
                    {status?.stream_checking_enabled ? "Enabled" : "Disabled"}
                  </Badge>
                </dd>
              </div>
              <div className="flex justify-between items-center">
                <dt className="text-muted-foreground">Checker Concurrency:</dt>
                <dd>
                  <Badge variant={(streamCheckerStatus?.parallel?.max_workers || 0) > 0 ? "outline" : "secondary"}>
                    {streamCheckerStatus?.parallel?.max_workers || 0} Workers
                  </Badge>
                </dd>
              </div>
            </dl>
          </CardContent>
        </Card>

        <Card>
          <CardHeader><CardTitle>Stream Checker Status</CardTitle></CardHeader>
          <CardContent>
            <dl className="space-y-3 text-sm">
              <div className="flex justify-between items-center">
                <dt className="text-muted-foreground">Queue Size:</dt>
                <dd><Badge variant={queueSize > 0 ? "default" : "secondary"}>{queueSize}</Badge></dd>
              </div>
              <div className="flex justify-between items-center">
                <dt className="text-muted-foreground">Total Processed:</dt>
                <dd><Badge variant="outline">{totalProcessed}</Badge></dd>
              </div>
              {queueSize > 0 && (
                <div className="pt-2">
                  <div className="flex justify-between items-center mb-2">
                    <Label className="text-xs text-muted-foreground block">Processing Progress</Label>
                    {streamCheckerStatus?.queue?.eta_seconds > 0 ? (
                      <span className="text-xs text-muted-foreground">
                        ~{formatDuration(streamCheckerStatus.queue.eta_seconds)} remaining
                      </span>
                    ) : (
                      <span className="text-xs text-muted-foreground animate-pulse text-primary/70">
                        Calculating ETA...
                      </span>
                    )}
                  </div>
                  <Progress value={queueProgress} className="h-2" />
                </div>
              )}
            </dl>
          </CardContent>
        </Card>
      </div>

      {/* Upcoming Automation Events */}
      <UpcomingAutomationEvents />

      {/* Available Playlists */}
      <Card>
        <CardHeader>
          <CardTitle>Global Playlist Visibility</CardTitle>
          <CardDescription>Toggle global extraction pooling for upstream API connections.</CardDescription>
        </CardHeader>
        <CardContent>
          {playlists.length === 0 ? (
            <p className="text-sm text-muted-foreground">No playlists available</p>
          ) : (
            <div className="space-y-3">
              {playlists.map((playlist) => {
                const enabledAccounts = (automationConfig?.enabled_m3u_accounts || [])
                  .map(id => Number(id)).filter(Number.isFinite)
                const playlistId = Number(playlist.id)
                const isEnabled = enabledAccounts.length === 0 || enabledAccounts.includes(playlistId)
                return (
                  <div key={playlist.id} className="flex items-center justify-between p-3 border rounded-lg">
                    <div className="flex-1">
                      <div className="flex items-center gap-2">
                        <h4 className="font-medium">{playlist.name}</h4>
                        <Badge variant={isEnabled ? "default" : "secondary"}>
                          {isEnabled ? "Enabled" : "Disabled"}
                        </Badge>
                      </div>
                      {playlist.url && (
                        <p className="text-xs text-muted-foreground mt-1 truncate max-w-md">{playlist.url}</p>
                      )}
                    </div>
                    <Switch
                      checked={isEnabled}
                      onCheckedChange={() => handleTogglePlaylist(playlist.id, isEnabled)}
                      disabled={togglingPlaylist === playlist.id}
                    />
                  </div>
                )
              })}
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  )
}
