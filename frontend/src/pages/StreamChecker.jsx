import { useState, useEffect } from 'react'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card.jsx'
import { Button } from '@/components/ui/button.jsx'
import { Badge } from '@/components/ui/badge.jsx'
import { Progress } from '@/components/ui/progress.jsx'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert.jsx'
import { Label } from '@/components/ui/label.jsx'
import { Input } from '@/components/ui/input.jsx'
import { Switch } from '@/components/ui/switch.jsx'
import { Separator } from '@/components/ui/separator.jsx'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs.jsx'
import { Pagination, PaginationContent, PaginationItem, PaginationLink, PaginationNext, PaginationPrevious } from '@/components/ui/pagination.jsx'
import { useToast } from '@/hooks/use-toast.js'
import { streamCheckerAPI, deadStreamsAPI } from '@/services/api.js'
import {
  Activity,
  CheckCircle2,
  Clock,
  PlayCircle,
  StopCircle,
  Loader2,
  Settings,
  Trash2,
  AlertCircle,
  RefreshCw,
  List
} from 'lucide-react'

// Pagination constants
const DEAD_STREAMS_PER_PAGE = 20
const PAGINATION_MAX_VISIBLE_PAGES = 5

export default function StreamChecker() {
  const [status, setStatus] = useState(null)
  const [progress, setProgress] = useState(null)
  const [config, setConfig] = useState(null)
  const [loading, setLoading] = useState(true)
  const [actionLoading, setActionLoading] = useState('')
  const [tick, setTick] = useState(0) // drives countdown re-renders — value never rendered
  const [configEditing, setConfigEditing] = useState(false)
  const [editedConfig, setEditedConfig] = useState(null)
  const [deadStreams, setDeadStreams] = useState([])
  const [deadStreamsLoading, setDeadStreamsLoading] = useState(false)
  const [deadStreamsPagination, setDeadStreamsPagination] = useState({
    page: 1,
    per_page: DEAD_STREAMS_PER_PAGE,
    total_pages: 0,
    has_next: false,
    has_prev: false
  })
  const [totalDeadStreams, setTotalDeadStreams] = useState(0)
  const { toast } = useToast()

  useEffect(() => {
    loadData()
    // Poll for updates - use shorter interval when checking is active
    const pollInterval = (status?.checking || (status?.queue?.queue_size > 0)) ? 1000 : 3000
    const interval = setInterval(() => {
      loadData()
    }, pollInterval)
    return () => clearInterval(interval)
  }, [status?.checking, status?.queue?.queue_size])

  // Tick every second to drive per-stream countdown cells
  // The tick value itself is never rendered — it triggers re-renders so
  // each countdown cell recalculates from Date.now() fresh each second.
  useEffect(() => {
    const timer = setInterval(() => setTick(t => t + 1), 1000)
    return () => clearInterval(timer)
  }, [])

  const loadData = async () => {
    try {
      const [statusResponse, progressResponse, configResponse] = await Promise.all([
        streamCheckerAPI.getStatus(),
        streamCheckerAPI.getProgress(),
        streamCheckerAPI.getConfig()
      ])
      setStatus(statusResponse.data)
      setProgress(progressResponse.data)
      setConfig(configResponse.data)
      if (!editedConfig && configResponse.data) {
        setEditedConfig(configResponse.data)
      }
    } catch (err) {
      console.error('Failed to load stream checker data:', err)
    } finally {
      setLoading(false)
    }
  }


  const handleClearQueue = async () => {
    try {
      setActionLoading('clear-queue')
      await streamCheckerAPI.clearQueue()
      toast({
        title: "Success",
        description: "Queue cleared successfully"
      })
      await loadData()
    } catch (err) {
      toast({
        title: "Error",
        description: "Failed to clear queue",
        variant: "destructive"
      })
    } finally {
      setActionLoading('')
    }
  }

  const handleSaveConfig = async () => {
    try {
      setActionLoading('save-config')
      await streamCheckerAPI.updateConfig(editedConfig)
      toast({
        title: "Success",
        description: "Configuration saved successfully"
      })
      setConfigEditing(false)
      await loadData()
    } catch (err) {
      toast({
        title: "Error",
        description: err.response?.data?.error || "Failed to save configuration",
        variant: "destructive"
      })
    } finally {
      setActionLoading('')
    }
  }

  const updateConfigValue = (path, value) => {
    setEditedConfig(prevConfig => {
      const newConfig = JSON.parse(JSON.stringify(prevConfig)) // Deep clone
      const keys = path.split('.')

      // Validate keys to prevent prototype pollution
      const safeKeys = keys.filter(key =>
        key !== '__proto__' &&
        key !== 'constructor' &&
        key !== 'prototype'
      )

      if (safeKeys.length === 0) {
        return prevConfig // Return unchanged if all keys were filtered
      }

      let current = newConfig
      for (let i = 0; i < safeKeys.length - 1; i++) {
        const key = safeKeys[i]
        if (!current[key] || typeof current[key] !== 'object' || Array.isArray(current[key])) {
          current[key] = {}
        }
        current = current[key]
      }
      current[safeKeys[safeKeys.length - 1]] = value
      return newConfig
    })
  }

  const loadDeadStreams = async (page = deadStreamsPagination.page) => {
    try {
      setDeadStreamsLoading(true)
      const response = await deadStreamsAPI.getDeadStreams(page, deadStreamsPagination.per_page)
      const deadStreamsData = response.data.dead_streams || []
      const paginationData = response.data.pagination || {}

      // Validate that backend returned the page we requested
      if (paginationData.page && paginationData.page !== page) {
        toast({
          title: "Warning",
          description: `Requested page ${page} but received page ${paginationData.page}`,
          variant: "default"
        })
      }

      setDeadStreams(deadStreamsData)
      setTotalDeadStreams(response.data.total_dead_streams || 0)
      setDeadStreamsPagination({
        page: paginationData.page || page,
        per_page: paginationData.per_page || deadStreamsPagination.per_page,
        total_pages: paginationData.total_pages || 0,
        has_next: paginationData.has_next || false,
        has_prev: paginationData.has_prev || false
      })
    } catch (err) {
      console.error('Failed to load dead streams:', err)
      toast({
        title: "Error",
        description: "Failed to load dead streams",
        variant: "destructive"
      })
    } finally {
      setDeadStreamsLoading(false)
    }
  }

  const handleReviveStream = async (streamUrl) => {
    try {
      setActionLoading(`revive-${streamUrl}`)
      await deadStreamsAPI.reviveStream(streamUrl)
      toast({
        title: "Success",
        description: "Stream revived successfully"
      })
      await loadDeadStreams()
    } catch (err) {
      toast({
        title: "Error",
        description: err.response?.data?.error || "Failed to revive stream",
        variant: "destructive"
      })
    } finally {
      setActionLoading('')
    }
  }

  const handleClearAllDeadStreams = async () => {
    try {
      setActionLoading('clear-all-dead')
      const response = await deadStreamsAPI.clearAllDeadStreams()
      toast({
        title: "Success",
        description: response.data.message || "All dead streams cleared"
      })
      await loadDeadStreams()
    } catch (err) {
      toast({
        title: "Error",
        description: err.response?.data?.error || "Failed to clear dead streams",
        variant: "destructive"
      })
    } finally {
      setActionLoading('')
    }
  }

  if (loading) {
    return (
      <div className="flex items-center justify-center h-64">
        <Loader2 className="h-8 w-8 animate-spin text-muted-foreground" />
      </div>
    )
  }

  const isChecking = status?.checking || (status?.queue?.queue_size > 0)
  const queueSize = status?.queue?.queue_size || 0
  const inProgress = status?.queue?.in_progress || 0
  const completed = status?.queue?.completed || 0
  const failed = status?.queue?.failed || 0
  const queued = status?.queue?.queued || 0
  const totalBatch = queued + inProgress + completed + failed
  const batchProgress = totalBatch > 0 ? ((completed + failed) / totalBatch) * 100 : 0

  return (
    <div className="space-y-6">
      <div className="flex justify-between items-start">
        <div>
          <h1 className="text-3xl font-bold tracking-tight">Stream Checker</h1>
          <p className="text-muted-foreground">
            Monitor and manage stream quality checking
          </p>
        </div>
        <div className="flex gap-2">
        </div>
      </div>

      {/* Status Overview */}
      <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-4">
        <Card>
          <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
            <CardTitle className="text-sm font-medium">Status</CardTitle>
            <Activity className="h-4 w-4 text-muted-foreground" />
          </CardHeader>
          <CardContent>
            <div className="flex items-center gap-2">
              <Badge variant={isChecking ? "default" : "secondary"}>
                {isChecking ? "Active" : "Idle"}
              </Badge>
            </div>
            <p className="text-xs text-muted-foreground mt-2">
              Mode: {status?.parallel?.mode || 'sequential'}
            </p>
          </CardContent>
        </Card>

        <Card>
          <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
            <CardTitle className="text-sm font-medium">Queue Size</CardTitle>
            <List className="h-4 w-4 text-muted-foreground" />
          </CardHeader>
          <CardContent>
            <div className="text-2xl font-bold">{queueSize}</div>
            <p className="text-xs text-muted-foreground">
              {inProgress > 0 ? `${inProgress} in progress` : 'No channels processing'}
            </p>
          </CardContent>
        </Card>

        <Card>
          <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
            <CardTitle className="text-sm font-medium">Completed</CardTitle>
            <CheckCircle2 className="h-4 w-4 text-muted-foreground" />
          </CardHeader>
          <CardContent>
            <div className="text-2xl font-bold">{completed}</div>
            <p className="text-xs text-muted-foreground">
              Channels checked this session
            </p>
          </CardContent>
        </Card>

        <Card>
          <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
            <CardTitle className="text-sm font-medium">Failed</CardTitle>
            <AlertCircle className="h-4 w-4 text-muted-foreground" />
          </CardHeader>
          <CardContent>
            <div className="text-2xl font-bold">{failed}</div>
            <p className="text-xs text-muted-foreground">
              Channels with errors
            </p>
          </CardContent>
        </Card>
      </div>

      {/* Batch Progress — hidden during single channel checks to avoid showing
           stale counters from the previous automation run */}
      {isChecking && totalBatch > 0 && !progress?.is_single_channel_check && (
        <Card>
          <CardHeader className="pb-2">
            <div className="flex justify-between items-center">
              <CardTitle>Batch Progress</CardTitle>
              {status?.queue?.eta_seconds > 0 ? (
                <span className="text-sm text-muted-foreground font-medium bg-secondary/50 px-2 py-1 rounded-md">
                  ~{status.queue.eta_seconds > 60
                    ? `${Math.floor(status.queue.eta_seconds / 60)}m ${status.queue.eta_seconds % 60}s`
                    : `${status.queue.eta_seconds}s`} remaining
                </span>
              ) : (
                <span className="text-sm text-muted-foreground font-medium bg-secondary/50 px-2 py-1 rounded-md animate-pulse">
                  Calculating ETA...
                </span>
              )}
            </div>
            <CardDescription>Checking {totalBatch} channels</CardDescription>
          </CardHeader>
          <CardContent className="space-y-4">
            <div className="space-y-2">
              <div className="flex justify-between text-sm">
                <span className="text-muted-foreground">{completed + failed} of {totalBatch} channels processed</span>
                <span className="font-medium">{Math.round(batchProgress)}%</span>
              </div>
              <Progress value={batchProgress} className="h-2" />
            </div>
          </CardContent>
        </Card>
      )}

      {/* Current Progress */}
      {progress && isChecking && (
        <Card>
          <CardHeader>
            <CardTitle>Current Progress</CardTitle>
            <CardDescription>
              {progress.channel_name || 'Processing...'}
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-4">
            <div className="space-y-2">
              <div className="flex justify-between text-sm">
                <span className="text-muted-foreground">{progress.step || 'Checking'}</span>
                <span className="font-medium">{progress.percentage || 0}%</span>
              </div>
              <Progress value={progress.percentage || 0} className="h-2" />
              <p className="text-xs text-muted-foreground">{progress.step_detail}</p>
            </div>

            <div className="flex items-center gap-2 text-sm pb-2 border-b">
              <Badge variant="outline">{progress.status}</Badge>
              {status?.parallel?.enabled && (
                <Badge variant="secondary">
                  Parallel ({status.parallel.max_workers} workers)
                </Badge>
              )}
            </div>

            {/* Streams Detail Progress List */}
            {progress.streams_detail && progress.streams_detail.length > 0 && (() => {
              // Phase-aware sort: loop testing phase floats probing streams to top;
              // normal analysis phase floats completed streams to top by score.
              const isLoopPhase = progress.step === 'Loop testing'
              const STATUS_ORDER = isLoopPhase
                ? { probing: 0, loop_detected: 1, completed: 2, checking: 3, pending: 4, error: 5, dead: 5 }
                : { completed: 0, checking: 1, pending: 2, error: 3, dead: 3 }

              // Dynamic height: sized to min(max_workers, stream count), floor 6 rows
              const maxWorkers = status?.parallel?.max_workers || 6
              const ROW_HEIGHT = 44   // px — double-line completed rows are taller
              const HEADER_HEIGHT = 32
              const visibleRows = Math.max(6, Math.min(maxWorkers, progress.streams_detail.length))
              const tableMaxHeight = visibleRows * ROW_HEIGHT + HEADER_HEIGHT

              const sortedStreams = [...progress.streams_detail].sort((a, b) => {
                const oa = STATUS_ORDER[a.status] ?? 99
                const ob = STATUS_ORDER[b.status] ?? 99
                if (oa !== ob) return oa - ob
                const sa = a.score != null ? a.score : -Infinity
                const sb = b.score != null ? b.score : -Infinity
                return sb - sa
              })

              return (
                <div className="mt-4">
                  <Label className="text-sm font-semibold mb-2 block">Stream Progress Tracking</Label>
                  <div className="rounded-md border overflow-y-auto w-full" style={{ maxHeight: `${tableMaxHeight}px` }}>
                    <table className="w-full text-sm text-left">
                      <thead className="bg-muted sticky top-0 z-10 text-xs text-muted-foreground uppercase h-8">
                        <tr>
                          <th className="px-3 py-1 font-medium">Stream</th>
                          <th className="px-3 py-1 font-medium">Account</th>
                          <th className="px-3 py-1 font-medium text-center">Status</th>
                          <th className="px-3 py-1 font-medium text-right">Countdown</th>
                          <th className="px-3 py-1 font-medium text-right">Specs</th>
                          <th className="px-3 py-1 font-medium text-right">Score</th>
                        </tr>
                      </thead>
                      <tbody className="divide-y">
                        {sortedStreams.map((stream) => {
                          // Per-stream countdown: counts down from stream_duration
                          // using only client-side time — no backend tracking needed.
                          const isActive = stream.status === 'checking' || stream.status === 'probing'
                          let countdownCell = <span className="text-muted-foreground">-</span>
                          if (isActive && stream.started_at && progress.stream_duration) {
                            const elapsed = Math.floor((Date.now() - new Date(stream.started_at).getTime()) / 1000)
                            const remaining = Math.max(0, progress.stream_duration - elapsed)
                            if (remaining === 0) {
                              countdownCell = <span className="text-muted-foreground/50">--</span>
                            } else {
                              const m = Math.floor(remaining / 60)
                              const s = remaining % 60
                              const timeStr = m > 0 ? `${m}m ${String(s).padStart(2, '0')}s` : `${s}s`
                              countdownCell = (
                                <span className={remaining <= 10 ? 'text-amber-500 font-mono text-xs' : 'text-muted-foreground font-mono text-xs'}>
                                  {timeStr}
                                </span>
                              )
                            }
                          }

                          return (
                            <tr key={stream.id} className="hover:bg-muted/50 transition-colors bg-card">
                              <td className="px-3 py-1.5 align-middle">
                                <div className="font-medium max-w-[200px] truncate" title={stream.name}>
                                  {stream.name}
                                </div>
                              </td>
                              <td className="px-3 py-1.5 align-middle">
                                <div className="text-xs text-muted-foreground max-w-[150px] truncate" title={stream.m3u_account}>
                                  {stream.m3u_account}
                                </div>
                              </td>
                              <td className="px-3 py-1.5 align-middle text-center">
                                {stream.status === 'pending' && <Badge variant="outline" className="text-[10px] text-muted-foreground">Pending</Badge>}
                                {stream.status === 'checking' && <Badge variant="secondary" className="text-[10px] bg-blue-100 text-blue-800 dark:bg-blue-900 dark:text-blue-300">Checking</Badge>}
                                {stream.status === 'completed' && <Badge variant="outline" className="text-[10px] bg-green-100 text-green-800 dark:bg-green-900/30 dark:text-green-400">Completed</Badge>}
                                {stream.status === 'error' && <Badge variant="destructive" className="text-[10px]">Error</Badge>}
                                {stream.status === 'dead' && <Badge variant="destructive" className="text-[10px]">Dead</Badge>}
                                {stream.status === 'probing' && <Badge variant="outline" className="text-[10px] bg-amber-100 text-amber-800 dark:bg-amber-900/30 dark:text-amber-400 animate-pulse">Probing</Badge>}
                                {stream.status === 'loop_detected' && <Badge variant="outline" className="text-[10px] bg-red-100 text-red-800 dark:bg-red-900/30 dark:text-red-400">⚠ {stream.loop_duration_secs ? `${stream.loop_duration_secs.toFixed(1)}s` : ''} Loop Found</Badge>}
                              </td>
                              <td className="px-3 py-1.5 align-middle text-right">
                                {countdownCell}
                              </td>
                              <td className="px-3 py-1.5 align-middle text-right text-xs text-muted-foreground whitespace-nowrap">
                                {stream.status === 'completed' || stream.status === 'loop_detected' ? (
                                  <div className="flex flex-col items-end gap-0.5">
                                    <span>{stream.video_codec || 'N/A'} • <span className="text-foreground">{stream.fps || 0} fps </span></span>
                                    {(stream.resolution || stream.bitrate) && (
                                      <span className="text-[10px] text-muted-foreground/80">
                                        {stream.resolution || 'Unknown'} {stream.bitrate ? `• ${Math.round(stream.bitrate)} kbps` : ''}
                                        {stream.hdr_format && stream.hdr_format !== 'SDR' && (
                                          <Badge variant="outline" className="ml-1 px-1 py-0 text-[8px] h-3 border-amber-500/30 text-amber-600 dark:text-amber-400">HDR</Badge>
                                        )}
                                      </span>
                                    )}
                                  </div>
                                ) : '-'}
                              </td>
                              <td className="px-3 py-1.5 align-middle text-right text-xs font-mono">
                                {(stream.status === 'completed' || stream.status === 'loop_detected') && stream.score !== undefined ? stream.score.toFixed(2) : '-'}
                              </td>
                            </tr>
                          )
                        })}
                      </tbody>
                    </table>
                  </div>
                </div>
              )
            })()}
          </CardContent>
        </Card>
      )}

      {/* Queue Information */}
      {queueSize > 0 && (
        <Card>
          <CardHeader className="flex flex-row items-center justify-between">
            <div>
              <CardTitle>Stream Queue</CardTitle>
              <CardDescription>
                {queueSize} channels waiting to be checked
              </CardDescription>
            </div>
            <Button
              variant="destructive"
              size="sm"
              onClick={handleClearQueue}
              disabled={actionLoading === 'clear-queue'}
            >
              {actionLoading === 'clear-queue' ? (
                <Loader2 className="mr-2 h-4 w-4 animate-spin" />
              ) : (
                <Trash2 className="mr-2 h-4 w-4" />
              )}
              Clear Queue
            </Button>
          </CardHeader>
        </Card>
      )}

      <Separator />

      {/* Configuration Section */}
      <Card>
        <CardHeader className="flex flex-row items-center justify-between">
          <div>
            <CardTitle>Stream Checker Configuration</CardTitle>
            <CardDescription>
              Configure stream analysis and checking parameters
            </CardDescription>
          </div>
          <Button
            variant="outline"
            size="sm"
            onClick={() => setConfigEditing(!configEditing)}
          >
            <Settings className="mr-2 h-4 w-4" />
            {configEditing ? 'Cancel' : 'Edit'}
          </Button>
        </CardHeader>
        <CardContent className="space-y-6">
          {config && (
            <>
              {/* Pipeline Mode - Read Only */}
              <div className="space-y-2">
                <Label>Pipeline Mode</Label>
                <div className="text-sm bg-muted p-3 rounded-md">
                  <span className="font-medium">{config.pipeline_mode}</span>
                  <p className="text-xs text-muted-foreground mt-1">
                    Pipeline mode is managed in Automation Settings
                  </p>
                </div>
              </div>

              {/* Tabs for Configuration Sections */}
              <Tabs defaultValue="analysis" className="w-full">
                <TabsList className="grid w-full grid-cols-3">
                  <TabsTrigger value="analysis">Stream Analysis</TabsTrigger>
                  <TabsTrigger value="concurrent">Concurrent Checking</TabsTrigger>
                  <TabsTrigger value="dead-streams">Dead Streams</TabsTrigger>
                </TabsList>

                {/* Stream Analysis Tab */}
                <TabsContent value="analysis" className="space-y-4">
                  <div className="grid gap-4 md:grid-cols-2">
                    <div className="space-y-2">
                      <Label htmlFor="ffmpeg_duration">FFmpeg Duration (seconds)</Label>
                      <Input
                        id="ffmpeg_duration"
                        type="number"
                        value={editedConfig?.stream_analysis?.ffmpeg_duration || 30}
                        onChange={(e) => updateConfigValue('stream_analysis.ffmpeg_duration', parseInt(e.target.value))}
                        disabled={!configEditing}
                        min={5}
                        max={120}
                      />
                      <p className="text-xs text-muted-foreground">
                        Duration to analyze each stream (5-120 seconds)
                      </p>
                    </div>

                    <div className="space-y-2">
                      <Label htmlFor="timeout">Timeout (seconds)</Label>
                      <Input
                        id="timeout"
                        type="number"
                        value={editedConfig?.stream_analysis?.timeout || 30}
                        onChange={(e) => updateConfigValue('stream_analysis.timeout', parseInt(e.target.value))}
                        disabled={!configEditing}
                        min={10}
                        max={300}
                      />
                      <p className="text-xs text-muted-foreground">
                        Base timeout for stream operations (does not include duration or startup buffer)
                      </p>
                    </div>

                    <div className="space-y-2">
                      <Label htmlFor="stream_startup_buffer">Stream Startup Buffer (seconds)</Label>
                      <Input
                        id="stream_startup_buffer"
                        type="number"
                        value={editedConfig?.stream_analysis?.stream_startup_buffer || 10}
                        onChange={(e) => updateConfigValue('stream_analysis.stream_startup_buffer', parseInt(e.target.value))}
                        disabled={!configEditing}
                        min={5}
                        max={120}
                      />
                      <p className="text-xs text-muted-foreground">
                        Maximum time to wait for stream to start (actual timeout = timeout + duration + buffer)
                      </p>
                    </div>

                    <div className="space-y-2">
                      <Label htmlFor="retries">Retry Attempts</Label>
                      <Input
                        id="retries"
                        type="number"
                        value={editedConfig?.stream_analysis?.retries ?? 1}
                        onChange={(e) => updateConfigValue('stream_analysis.retries', parseInt(e.target.value))}
                        disabled={!configEditing}
                        min={0}
                        max={5}
                      />
                      <p className="text-xs text-muted-foreground">
                        Number of retry attempts for failed streams
                      </p>
                    </div>

                    <div className="space-y-2">
                      <Label htmlFor="retry_delay">Retry Delay (seconds)</Label>
                      <Input
                        id="retry_delay"
                        type="number"
                        value={editedConfig?.stream_analysis?.retry_delay || 10}
                        onChange={(e) => updateConfigValue('stream_analysis.retry_delay', parseInt(e.target.value))}
                        disabled={!configEditing}
                        min={1}
                        max={60}
                      />
                      <p className="text-xs text-muted-foreground">
                        Delay between retry attempts
                      </p>
                    </div>

                    <div className="space-y-2">
                      <Label htmlFor="max_loop_duration">Loop Probe Duration (seconds)</Label>
                      <Input
                        id="max_loop_duration"
                        type="number"
                        value={editedConfig?.stream_analysis?.max_loop_duration || 120}
                        onChange={(e) => updateConfigValue('stream_analysis.max_loop_duration', Math.min(240, Math.max(20, parseInt(e.target.value) || 120)))}
                        disabled={!configEditing}
                        min={20}
                        max={240}
                      />
                      <p className="text-xs text-muted-foreground">
                        Maximum loop period to detect — probes each stream for 3× this value (20–240 seconds)
                      </p>
                    </div>

                    <div className="space-y-2 md:col-span-2">
                      <Label htmlFor="user_agent">FFmpeg/FFprobe User Agent</Label>
                      <Input
                        id="user_agent"
                        type="text"
                        value={editedConfig?.stream_analysis?.user_agent || 'VLC/3.0.14'}
                        onChange={(e) => updateConfigValue('stream_analysis.user_agent', e.target.value)}
                        disabled={!configEditing}
                        maxLength={200}
                      />
                      <p className="text-xs text-muted-foreground">
                        User agent string for ffmpeg/ffprobe (for strict stream providers)
                      </p>
                    </div>
                  </div>
                </TabsContent>

                {/* Concurrent Checking Tab */}
                <TabsContent value="concurrent" className="space-y-4">
                  <div className="flex items-center justify-between">
                    <div className="space-y-0.5">
                      <Label htmlFor="concurrent_enabled">Enable Concurrent Checking</Label>
                      <p className="text-xs text-muted-foreground">
                        Check multiple streams in parallel for faster processing
                      </p>
                    </div>
                    <Switch
                      id="concurrent_enabled"
                      checked={editedConfig?.concurrent_streams?.enabled !== false}
                      onCheckedChange={(checked) => updateConfigValue('concurrent_streams.enabled', checked)}
                      disabled={!configEditing}
                    />
                  </div>

                  <div className="space-y-2">
                    <Label htmlFor="global_limit">Global Concurrent Limit</Label>
                    <Input
                      id="global_limit"
                      type="number"
                      value={editedConfig?.concurrent_streams?.global_limit || 10}
                      onChange={(e) => updateConfigValue('concurrent_streams.global_limit', parseInt(e.target.value))}
                      disabled={!configEditing || !editedConfig?.concurrent_streams?.enabled}
                      min={1}
                      max={50}
                    />
                    <p className="text-xs text-muted-foreground">
                      Maximum number of streams to check simultaneously (1-50)
                    </p>
                  </div>

                  <div className="space-y-2">
                    <Label htmlFor="stagger_delay">Stagger Delay (seconds)</Label>
                    <Input
                      id="stagger_delay"
                      type="number"
                      step="0.1"
                      value={editedConfig?.concurrent_streams?.stagger_delay || 1.0}
                      onChange={(e) => updateConfigValue('concurrent_streams.stagger_delay', parseFloat(e.target.value))}
                      disabled={!configEditing || !editedConfig?.concurrent_streams?.enabled}
                      min={0}
                      max={10}
                    />
                    <p className="text-xs text-muted-foreground">
                      Delay between starting each concurrent check to prevent overload
                    </p>
                  </div>
                </TabsContent>


                {/* Dead Streams Tab */}
                <TabsContent value="dead-streams" className="space-y-4">
                  <p className="text-sm text-muted-foreground">
                    View and manage streams that have been marked as dead. Removal from channels during stream checks depends on each automation profile&apos;s Stream Checking settings.
                  </p>
                  <div className="space-y-4">

                    {/* Dead Streams List */}
                    <Separator className="my-6" />

                    <div className="space-y-4">
                      <div className="flex items-center justify-between">
                        <div>
                          <h4 className="font-medium">Dead Streams List</h4>
                          <p className="text-sm text-muted-foreground">
                            View and manage streams that have been marked as dead
                          </p>
                        </div>
                        <div className="flex gap-2">
                          <Button
                            variant="outline"
                            size="sm"
                            onClick={loadDeadStreams}
                            disabled={deadStreamsLoading}
                          >
                            {deadStreamsLoading ? (
                              <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                            ) : (
                              <RefreshCw className="mr-2 h-4 w-4" />
                            )}
                            Refresh
                          </Button>
                          {deadStreams.length > 0 && (
                            <Button
                              variant="destructive"
                              size="sm"
                              onClick={handleClearAllDeadStreams}
                              disabled={actionLoading === 'clear-all-dead'}
                            >
                              {actionLoading === 'clear-all-dead' ? (
                                <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                              ) : (
                                <Trash2 className="mr-2 h-4 w-4" />
                              )}
                              Clear All
                            </Button>
                          )}
                        </div>
                      </div>

                      {deadStreamsLoading ? (
                        <div className="flex items-center justify-center py-8">
                          <Loader2 className="h-6 w-6 animate-spin text-muted-foreground" />
                        </div>
                      ) : deadStreams.length === 0 ? (
                        <Alert>
                          <CheckCircle2 className="h-4 w-4" />
                          <AlertTitle>No Dead Streams</AlertTitle>
                          <AlertDescription>
                            No streams are currently marked as dead. This is good news!
                          </AlertDescription>
                        </Alert>
                      ) : (
                        <>
                          <div className="space-y-2">
                            {deadStreams.map((stream) => (
                              <Card key={stream.url} className="p-4">
                                <div className="flex items-start justify-between gap-4">
                                  <div className="flex-1 space-y-1">
                                    <div className="flex items-center gap-2">
                                      <Badge variant="destructive">Dead</Badge>
                                      <span className="font-medium">{stream.stream_name}</span>
                                    </div>
                                    <div className="text-sm text-muted-foreground space-y-1">
                                      <div className="flex items-center gap-2">
                                        <span className="font-mono text-xs">{stream.url}</span>
                                      </div>
                                      {stream.marked_dead_at && (
                                        <div className="flex items-center gap-2">
                                          <Clock className="h-3 w-3" />
                                          <span>Marked dead: {new Date(stream.marked_dead_at).toLocaleString()}</span>
                                        </div>
                                      )}
                                    </div>
                                  </div>
                                  <Button
                                    variant="outline"
                                    size="sm"
                                    onClick={() => handleReviveStream(stream.url)}
                                    disabled={actionLoading === `revive-${stream.url}`}
                                  >
                                    {actionLoading === `revive-${stream.url}` ? (
                                      <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                                    ) : (
                                      <CheckCircle2 className="mr-2 h-4 w-4" />
                                    )}
                                    Revive
                                  </Button>
                                </div>
                              </Card>
                            ))}
                          </div>

                          {/* Pagination */}
                          {deadStreamsPagination.total_pages > 1 && (
                            <div className="flex flex-col items-center gap-2 pt-4">
                              <div className="text-sm text-muted-foreground">
                                Showing page {deadStreamsPagination.page} of {deadStreamsPagination.total_pages} ({totalDeadStreams} total)
                              </div>
                              <Pagination>
                                <PaginationContent>
                                  <PaginationItem>
                                    <PaginationPrevious
                                      onClick={() => deadStreamsPagination.has_prev && loadDeadStreams(deadStreamsPagination.page - 1)}
                                      className={!deadStreamsPagination.has_prev ? 'pointer-events-none opacity-50' : 'cursor-pointer'}
                                    />
                                  </PaginationItem>

                                  {/* Show page numbers with smart windowing */}
                                  {(() => {
                                    const currentPage = deadStreamsPagination.page
                                    const totalPages = deadStreamsPagination.total_pages
                                    const maxVisiblePages = PAGINATION_MAX_VISIBLE_PAGES
                                    let startPage, endPage

                                    if (totalPages <= maxVisiblePages) {
                                      startPage = 1
                                      endPage = totalPages
                                    } else {
                                      const halfVisible = Math.floor(maxVisiblePages / 2)

                                      if (currentPage <= halfVisible + 1) {
                                        startPage = 1
                                        endPage = maxVisiblePages
                                      } else if (currentPage >= totalPages - halfVisible) {
                                        startPage = totalPages - maxVisiblePages + 1
                                        endPage = totalPages
                                      } else {
                                        startPage = currentPage - halfVisible
                                        endPage = currentPage + halfVisible
                                      }
                                    }

                                    return Array.from({ length: endPage - startPage + 1 }, (_, i) => {
                                      const pageNum = startPage + i
                                      return (
                                        <PaginationItem key={pageNum}>
                                          <PaginationLink
                                            onClick={() => loadDeadStreams(pageNum)}
                                            isActive={pageNum === currentPage}
                                            className="cursor-pointer"
                                          >
                                            {pageNum}
                                          </PaginationLink>
                                        </PaginationItem>
                                      )
                                    })
                                  })()}

                                  <PaginationItem>
                                    <PaginationNext
                                      onClick={() => deadStreamsPagination.has_next && loadDeadStreams(deadStreamsPagination.page + 1)}
                                      className={!deadStreamsPagination.has_next ? 'pointer-events-none opacity-50' : 'cursor-pointer'}
                                    />
                                  </PaginationItem>
                                </PaginationContent>
                              </Pagination>
                            </div>
                          )}
                        </>
                      )}
                    </div>
                  </div>
                </TabsContent>
              </Tabs>

              {configEditing && (
                <div className="flex justify-end gap-2 pt-4">
                  <Button
                    variant="outline"
                    onClick={() => {
                      setEditedConfig(config)
                      setConfigEditing(false)
                    }}
                  >
                    Cancel
                  </Button>
                  <Button
                    onClick={handleSaveConfig}
                    disabled={actionLoading === 'save-config'}
                  >
                    {actionLoading === 'save-config' ? (
                      <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                    ) : null}
                    Save Configuration
                  </Button>
                </div>
              )}
            </>
          )}
        </CardContent>
      </Card>
    </div>
  )
}
