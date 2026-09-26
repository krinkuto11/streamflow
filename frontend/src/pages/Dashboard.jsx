import { useState, useEffect, useRef } from 'react'
import { Link } from 'react-router-dom'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card.jsx'
import { Button } from '@/components/ui/button.jsx'
import { Badge } from '@/components/ui/badge.jsx'
import { Progress } from '@/components/ui/progress.jsx'
import { Alert, AlertDescription } from '@/components/ui/alert.jsx'
import { Label } from '@/components/ui/label.jsx'
import { Switch } from '@/components/ui/switch.jsx'
import { useToast } from '@/hooks/use-toast.js'
import { automationAPI, streamCheckerAPI, shadowBlankMonitorAPI, viewerActivityAPI, m3uAPI, dispatcharrAPI, environmentAPI } from '@/services/api.js'
import { getDashboardRunMetrics } from '@/lib/dashboard-run-counts.js'
import { getQueueEtaDisplay } from '@/lib/queue-eta-display.js'
import { getCheckerConcurrencyDisplay } from '@/lib/provider-progress-display.js'
import {
  getAbortedRunDisplay,
  getDashboardActionStates,
  getAutomationStageCards,
  getCacheSyncCardDetail,
  getRunHistoryBaseline,
  getM3uRefreshCardDetail,
  getRunDurationCardValue,
  getRunDurationValue,
  getSkippedRunDisplay,
  getStreamCheckerRunDisplay,
  isM3uRefreshSkipped,
  normalizeRunStageKey,
  preferLiveRunSeconds,
  shouldShowAutomationRunCard,
} from '@/lib/dashboard-run-display.js'
import { formatDuration as formatDurationValue, formatLatency as formatLatencyValue } from '@/lib/time-format.js'
import {
  PlayCircle, RefreshCw, Activity, CheckCircle2,
  Loader2, ChevronDown, Tv, Radio, Database, WifiOff,
  Clock3, AlertCircle, ListChecks, Timer, Eye, Users, StopCircle, History
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
import StreamFlowInitializingScreen from '@/components/Dashboard/StreamFlowInitializingScreen.jsx'
import {
  formatRealViewerChannelCount,
  formatStreamRef,
  formatViewerClientCount,
  formatWatcherClientCount,
  formatWatcherOnlyChannelCount,
  getPlaybackBadgeLabel,
  getViewerActivityDetailLabel,
} from '@/lib/viewer-activity-display.js'

const AUTOMATION_STAGES = [
  { id: 'settings', label: 'Preparing' },
  { id: 'period_discovery', label: 'Schedule' },
  { id: 'm3u_refresh', label: 'M3U Refresh' },
  { id: 'cache_sync', label: 'Cache Sync' },
  { id: 'stream_matching', label: 'Matching' },
  { id: 'quality_queueing', label: 'Queueing' },
  { id: 'quality_checking', label: 'Quality Check' },
  { id: 'finalizing', label: 'Finalizing' },
]

const LIVE_STATUS_POLL_MS = 1000
const IDLE_STATUS_POLL_MS = 5000
const BACKGROUND_DATA_POLL_MS = 30000

const formatDuration = (seconds) => {
  const formatted = formatDurationValue(seconds)
  return formatted || 'N/A'
}

const formatLatency = (seconds) => {
  const formatted = formatLatencyValue(seconds)
  return formatted || 'N/A'
}

const formatSecondsPerChannel = (seconds) => {
  const value = Number(seconds)
  if (!Number.isFinite(value)) return 'N/A'
  return `${value >= 10 ? Math.round(value) : value.toFixed(1)} sec`
}

const getSecondsPerChannelBaselineLabel = (baseline) => {
  if (baseline?.perChannelBaselineStable) {
    return formatSecondsPerChannel(baseline.typicalSecondsPerChannel)
  }
  if ((baseline?.perChannelSampleCount || 0) > 0) {
    return 'Mixed'
  }
  return 'N/A'
}

const formatTime = (value) => {
  if (!value) return 'N/A'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return 'N/A'
  return date.toLocaleTimeString()
}

const elapsedSecondsSince = (value, now = Date.now()) => {
  if (!value) return null
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return null
  return Math.max(0, (now - date.getTime()) / 1000)
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
  const [viewerActivityStatus, setViewerActivityStatus] = useState(null)
  const [playlists, setPlaylists] = useState([])
  const [loading, setLoading] = useState(true)
  const [actionLoading, setActionLoading] = useState('')
  const [togglingPlaylist, setTogglingPlaylist] = useState(null)
  const [periods, setPeriods] = useState([])
  const [udiStats, setUdiStats] = useState(null)
  const [udiSyncing, setUdiSyncing] = useState(false)
  const [dashboardNow, setDashboardNow] = useState(() => Date.now())
  const statusPollInFlight = useRef(false)
  const liveContextInFlight = useRef(false)
  const activeRunRef = useRef(false)
  // debug_mode gates the fault injection panel (Phase 5 — not yet built)
  const [debugMode, setDebugMode] = useState(false)
  const { toast } = useToast()

  useEffect(() => {
    setDashboardNow(Date.now())
    loadStatus()
    loadSecondaryStatus()
    loadLiveContext()
    loadPlaylists()
    loadPeriods()
    loadEnvironment()
    loadUdiStats()

    let statusTimer
    let stopped = false
    const scheduleStatus = (delayMs = activeRunRef.current ? LIVE_STATUS_POLL_MS : IDLE_STATUS_POLL_MS) => {
      if (stopped) return
      statusTimer = setTimeout(async () => {
        if (document.visibilityState === 'visible') {
          setDashboardNow(Date.now())
          await loadStatus()
        }
        scheduleStatus()
      }, delayMs)
    }
    scheduleStatus(LIVE_STATUS_POLL_MS)

    const onVisibilityChange = () => {
      if (document.visibilityState !== 'visible') return
      setDashboardNow(Date.now())
      loadStatus()
      loadSecondaryStatus()
      loadLiveContext()
      loadUdiStats()
    }
    document.addEventListener('visibilitychange', onVisibilityChange)

    const backgroundInterval = setInterval(() => {
      if (document.visibilityState !== 'visible') return
      loadSecondaryStatus()
      loadPlaylists()
      loadUdiStats()
    }, BACKGROUND_DATA_POLL_MS)
    const liveContextInterval = setInterval(() => {
      if (document.visibilityState === 'visible') loadLiveContext()
    }, 5000)

    return () => {
      stopped = true
      clearTimeout(statusTimer)
      clearInterval(backgroundInterval)
      clearInterval(liveContextInterval)
      document.removeEventListener('visibilitychange', onVisibilityChange)
    }
  }, [])

  const loadStatus = async () => {
    if (statusPollInFlight.current) {
      return
    }
    statusPollInFlight.current = true
    try {
      const [automationResult, streamCheckerResult] = await Promise.allSettled([
        automationAPI.getStatus(),
        streamCheckerAPI.getStatus(),
      ])

      if (automationResult.status === 'fulfilled') {
        setStatus(automationResult.value.data)
      }
      if (streamCheckerResult.status === 'fulfilled') {
        setStreamCheckerStatus(streamCheckerResult.value.data)
      }
      const automation = automationResult.status === 'fulfilled' ? automationResult.value.data : null
      const checker = streamCheckerResult.status === 'fulfilled' ? streamCheckerResult.value.data : null
      const activeNow = Boolean(
          ['running', 'queued'].includes(automation?.run_status?.state)
          || automation?.run_status?.active
          || checker?.checking || checker?.stream_checking_mode
          || Number(checker?.queue?.in_progress || 0) > 0
          || Number(checker?.queue?.queue_size || 0) > 0
        )
      if (activeNow || (automation && checker)) {
        activeRunRef.current = activeNow
      }

      const failedResults = [
        automationResult,
        streamCheckerResult,
      ].filter(result => result.status === 'rejected')

      if (failedResults.length > 0) {
        console.warn(
          'Dashboard status poll had partial failures:',
          failedResults.map(result => result.reason?.message || result.reason)
        )
      }
    } catch (err) {
      console.error('Failed to load status:', err)
    } finally {
      statusPollInFlight.current = false
      setLoading(false)
    }
  }

  const loadSecondaryStatus = async () => {
    try {
      const response = await automationAPI.getConfig()
      setAutomationConfig(response.data || {})
    } catch (error) {
      console.warn('Failed to load automation configuration:', error)
    }
  }

  const loadLiveContext = async () => {
    if (liveContextInFlight.current) return
    liveContextInFlight.current = true
    try {
      const [shadowResult, viewersResult] = await Promise.allSettled([
        shadowBlankMonitorAPI.getStatus(),
        viewerActivityAPI.getStatus(),
      ])
      if (shadowResult.status === 'fulfilled') setShadowMonitorStatus(shadowResult.value.data)
      if (viewersResult.status === 'fulfilled') setViewerActivityStatus(viewersResult.value.data)
    } finally {
      liveContextInFlight.current = false
    }
  }

  const loadUdiStats = async () => {
    try {
      const response = await dispatcharrAPI.getInitializationStatus()
      const data = response.data || {}
      const ec = data.entity_counts || {}
      const counts = {
        channels_count: ec.channels?.received ?? null,
        streams_count: ec.streams?.received ?? null,
        m3u_accounts_count: ec.m3u_accounts?.received ?? null,
      }
      const hasCounts = Object.values(counts).some(value => value != null)

      if (data.status || hasCounts) {
        setUdiStats({
          syncStatus: data.status || 'unknown',
          percentage: data.percentage ?? null,
          message: data.message || '',
          ...counts,
        })
      }
    } catch (err) {
      console.error('UDI status poll error:', err)
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
      const response = await automationAPI.getPeriods({ page: 1, per_page: 200 })
      const periodItems = Array.isArray(response.data) ? response.data : response.data?.items || []
      setPeriods(periodItems)
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
        const counts = {
          channels_count: ec.channels?.received ?? null,
          streams_count: ec.streams?.received ?? null,
          m3u_accounts_count: ec.m3u_accounts?.received ?? null,
        }
        const hasCounts = Object.values(counts).some(value => value != null)

        if (hasCounts || data.status === 'completed' || data.status === 'failed') {
          clearInterval(udiPollInterval)
          setUdiStats(prev => {
            if (prev !== null) return prev
            return {
              syncStatus: data.status || 'unknown',
              ...counts,
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

  const handleStopActiveRun = async () => {
    try {
      setActionLoading('stop-run')

      const activeStreamCheck = Boolean(
        streamCheckerStatus?.stream_checking_mode ||
        streamCheckerStatus?.checking ||
        (streamCheckerStatus?.queue?.queue_size || 0) > 0 ||
        (streamCheckerStatus?.queue?.in_progress || 0) > 0
      )
      const activeAutomationRun = Boolean(status?.running || status?.run_status?.active)

      if (activeStreamCheck) {
        await streamCheckerAPI.clearQueue()
      }
      if (activeAutomationRun) {
        await automationAPI.abortRun()
      }

      toast({
        title: "Stop Requested",
        description: "Active automation and stream-check work is being stopped.",
      })
      await loadStatus()
    } catch (err) {
      toast({
        title: "Error",
        description: err.response?.data?.error || "Failed to stop the active run",
        variant: "destructive",
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
      await loadSecondaryStatus()
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

  const schedulerRunning = status?.running || false
  const runStatus = status?.run_status || {}
  const schedulerRetry = runStatus?.scheduler_retry || status?.scheduler_retry || {}
  const schedulerRetryPeriods = Array.isArray(schedulerRetry?.periods)
    ? schedulerRetry.periods
    : []
  const runCounts = runStatus.counts || {}
  const runDurations = runStatus.durations || {}
  const udiStatus = status?.udi_status || {}
  const apiTiming = udiStatus?.api_timing || {}
  const runState = runStatus.state || 'idle'
  const runStage = runStatus.stage || 'idle'
  const runStageLabel = runStatus.stage_label || 'Idle'
  const runProgress = runStatus.progress || {}
  const runningRun = runState === 'running'
  const failedRun = runState === 'failed'
  const abortedRun = runState === 'aborted'
  const completedRun = runState === 'completed'
  const skippedRun = runState === 'skipped'
  const queuedRun = runState === 'queued'
  const queueSize     = streamCheckerStatus?.queue?.queue_size || 0
  const completed     = streamCheckerStatus?.queue?.completed  || 0
  const inProgress    = streamCheckerStatus?.queue?.in_progress || 0
  const queueState    = streamCheckerStatus?.queue?.state || 'idle'
  const streamCheckerEtaDisplay = getQueueEtaDisplay(streamCheckerStatus?.queue)
  const checkerConcurrencyDisplay = getCheckerConcurrencyDisplay(streamCheckerStatus)
  const totalProcessed = completed
  const batchTotal    = completed + inProgress + queueSize
  const queueProgress = batchTotal > 0 ? (completed / batchTotal) * 100 : 0
  const streamCheckerRunDisplay = getStreamCheckerRunDisplay({
    streamCheckerStatus,
    runState,
    runStage,
    batchTotal,
    completed,
    now: dashboardNow,
  })
  const isProcessing = streamCheckerRunDisplay.isProcessing
  const queueHistoryOnly = !isProcessing && queueState === 'completed' && totalProcessed > 0
  const totalProcessedLabel = queueHistoryOnly ? 'Last Batch:' : 'Total Processed:'
  const streamQueueActive = streamCheckerRunDisplay.streamQueueActive
  const streamCheckerOnlyActive = streamCheckerRunDisplay.streamCheckerOnlyActive
  const streamQueueHistory = queueHistoryOnly && ['idle', 'skipped'].includes(runState)
  const streamRunActive = streamCheckerOnlyActive
  const streamProgress = streamCheckerStatus?.progress || {}
  const singleStreamRunActive = streamRunActive && !streamQueueActive
  const skippedRunDisplay = getSkippedRunDisplay({
    skippedRun,
    streamRunActive,
    streamQueueActive,
    runProgressMessage: runProgress.message,
    runStatusMessage: runStatus.message,
  })
  const abortedRunDisplay = getAbortedRunDisplay({
    runState,
    runStatus,
  })
  const rawRunProgressPercent = Number(runProgress.percent)
  const runProgressPercent = abortedRunDisplay.progressPercent !== null
    ? abortedRunDisplay.progressPercent
    : streamQueueActive
    ? queueProgress
    : singleStreamRunActive && Number.isFinite(Number(streamProgress.percentage))
      ? Number(streamProgress.percentage)
    : Number.isFinite(rawRunProgressPercent)
      ? rawRunProgressPercent
      : 0
  const runProgressCurrent = streamQueueActive ? completed : (singleStreamRunActive ? null : runProgress.current)
  const runProgressTotal = streamQueueActive ? batchTotal : (singleStreamRunActive ? null : runProgress.total)
  const hasRunProgressTotal = runProgressTotal !== null && runProgressTotal !== undefined
  const runProgressDetail = abortedRunDisplay.progressDetail || (hasRunProgressTotal
    ? `${runProgressCurrent ?? 0} of ${runProgressTotal}`
    : singleStreamRunActive
      ? (streamProgress.channel_name || streamProgress.step || 'Single channel check in progress')
      : skippedRunDisplay.progressDetail || runProgress.message || runStatus.message || 'Waiting for progress')
  const showRunProgress = isProcessing || runState !== 'idle' || Object.keys(runProgress).length > 0
  const showAutomationRunCard = shouldShowAutomationRunCard({
    showRunProgress,
    skippedRunDisplay,
  })
  const streamRunDisplayMessage = streamRunActive && queuedRun
    ? `${streamCheckerRunDisplay.displayMessage}; automation queued`
    : streamCheckerRunDisplay.displayMessage
  const displayRunMessage = streamRunActive
    ? streamRunDisplayMessage
    : abortedRunDisplay.message || skippedRunDisplay.message || runProgress.message || runStatus.message || 'Automation run status'
  const displayRunStageId = normalizeRunStageKey(streamRunActive ? streamCheckerRunDisplay.displayStageId : runStage)
  const displayRunStageLabel = streamRunActive ? streamCheckerRunDisplay.displayStageLabel : runStageLabel
  const displayRunningRun = runningRun || streamRunActive
  const runDisplayStageLabel = skippedRunDisplay.stageLabel || displayRunStageLabel
  const runDisplayBadgeLabel = streamRunActive
    ? 'Running'
    : skippedRunDisplay.badgeLabel || (skippedRun
      ? 'Waiting'
      : runningRun
        ? 'Running'
        : queuedRun
          ? 'Queued'
        : completedRun
          ? 'Completed'
          : failedRun
            ? 'Failed'
            : abortedRun
              ? 'Aborted'
              : 'Idle')
  const streamCheckerElapsedSeconds = streamCheckerRunDisplay.streamCheckerElapsedSeconds
  const liveRunDurationSeconds = runningRun
    ? elapsedSecondsSince(runStatus.started_at, dashboardNow) ?? runStatus.duration_seconds
    : runStatus.duration_seconds
  const liveStageDurationSeconds = runningRun
    ? elapsedSecondsSince(runStatus.stage_started_at, dashboardNow) ?? runStatus.stage_duration_seconds
    : runStatus.stage_duration_seconds
  const displayRunUpdatedAt = streamRunActive
    ? (streamCheckerStatus?.progress?.timestamp || streamCheckerStatus?.queue?.started_at || runStatus.updated_at)
    : runStatus.updated_at
  const displayRunElapsedSeconds = streamRunActive
    ? streamCheckerElapsedSeconds
    : preferLiveRunSeconds({
        active: runningRun,
        reportedSeconds: runStatus.elapsed_seconds ?? runProgress.elapsed_seconds,
        liveSeconds: liveRunDurationSeconds,
      })
  const displayRunStageElapsedSeconds = streamRunActive
    ? streamCheckerElapsedSeconds
    : preferLiveRunSeconds({
        active: runningRun,
        reportedSeconds: runStatus.stage_elapsed_seconds ?? runProgress.stage_elapsed_seconds,
        liveSeconds: liveStageDurationSeconds,
      })
  const displayStageCards = getAutomationStageCards({
    stages: AUTOMATION_STAGES,
    runStatusStages: runStatus.stages,
    displayRunStageId,
    displayRunningRun,
    completedRun,
    neutralRun: Boolean(skippedRunDisplay.stageLabel),
    streamRunActive,
  })
  const m3uRefreshDuration = getRunDurationValue({
    runDurations,
    durationKey: 'm3u_refresh_seconds',
    stageId: 'm3u_refresh',
    displayRunStageId,
    displayRunningRun,
    streamRunActive,
    streamCheckerElapsedSeconds,
    displayRunStageElapsedSeconds,
    stages: AUTOMATION_STAGES,
  })
  const cacheSyncDuration = getRunDurationValue({
    runDurations,
    durationKey: 'udi_sync_seconds',
    stageId: 'cache_sync',
    displayRunStageId,
    displayRunningRun,
    streamRunActive,
    streamCheckerElapsedSeconds,
    displayRunStageElapsedSeconds,
    stages: AUTOMATION_STAGES,
  })
  const streamMatchingDuration = getRunDurationValue({
    runDurations,
    durationKey: 'stream_matching_seconds',
    stageId: 'stream_matching',
    displayRunStageId,
    displayRunningRun,
    streamRunActive,
    streamCheckerElapsedSeconds,
    displayRunStageElapsedSeconds,
    stages: AUTOMATION_STAGES,
  })
  const qualityCheckDuration = getRunDurationValue({
    runDurations,
    durationKey: 'quality_check_seconds',
    stageId: 'quality_checking',
    displayRunStageId,
    displayRunningRun,
    streamRunActive,
    streamCheckerElapsedSeconds,
    displayRunStageElapsedSeconds,
    stages: AUTOMATION_STAGES,
  })
  const m3uRefreshSkipped = isM3uRefreshSkipped({
    runCounts,
    streamRunActive,
  })
  const m3uRefreshDetail = getM3uRefreshCardDetail({
    runCounts,
    skipped: m3uRefreshSkipped,
    streamRunActive,
  })
  const cacheSyncSkipped = m3uRefreshSkipped && cacheSyncDuration == null
  const cacheSyncDetail = getCacheSyncCardDetail({
    runCounts,
    skipped: cacheSyncSkipped,
    streamRunActive,
  })
  const durationCards = [
    {
      label: 'M3U Refresh',
      value: getRunDurationCardValue({
        seconds: m3uRefreshDuration,
        skipped: m3uRefreshSkipped,
      }),
      detail: m3uRefreshDetail,
    },
    {
      label: 'Cache Sync',
      value: getRunDurationCardValue({
        seconds: cacheSyncDuration,
        skipped: cacheSyncSkipped,
      }),
      detail: cacheSyncDetail,
    },
    {
      label: 'Stream Matching',
      value: getRunDurationCardValue({ seconds: streamMatchingDuration }),
    },
    {
      label: 'Quality Check',
      value: getRunDurationCardValue({ seconds: qualityCheckDuration }),
    },
  ]
  const runHistoryBaseline = getRunHistoryBaseline({
    summary: status?.run_history_summary,
  })
  const displayRunMetrics = getDashboardRunMetrics({
    streamCheckerStatus,
    streamQueueActive,
    streamQueueHistory,
    streamCheckerOnlyActive,
    batchTotal,
    completed,
    runCounts,
  })
  const currentCheckingChannelName = isProcessing && streamProgress?.channel_name
    ? String(streamProgress.channel_name)
    : ''
  const syncStatus = udiStats?.syncStatus
  const udiInitProgress = status?.udi_status?.init_progress || {}
  const udiCacheHasCompleted = Boolean(
    status?.udi_status?.last_refresh_time ||
    udiInitProgress?.last_refresh_time
  )
  const udiBackendRefreshing = Boolean(
    status?.udi_status?.init_in_progress ||
    syncStatus === 'in_progress'
  )
  const udiRefreshing = Boolean(udiSyncing || udiBackendRefreshing)
  const udiBootstrapInitializing = Boolean(udiBackendRefreshing && !udiCacheHasCompleted)
  const udiInitialization = {
    inProgress: udiBootstrapInitializing,
    percentage: udiStats?.percentage ?? udiInitProgress?.percentage ?? 0,
    message: udiStats?.message || udiInitProgress?.message || '',
  }
  if (udiBootstrapInitializing) {
    return <StreamFlowInitializingScreen initialization={udiInitialization} />
  }

  const runBadgeClass = displayRunningRun
    ? 'bg-blue-600 text-white border-transparent'
    : failedRun
    ? 'bg-destructive text-destructive-foreground border-transparent'
    : abortedRun
      ? 'bg-amber-600 text-white border-transparent'
      : completedRun
      ? 'bg-green-600 text-white border-transparent'
      : ''
  const actionStates = getDashboardActionStates({
    actionLoading,
    isStreamCheckerProcessing: isProcessing,
    udiInitializing: udiRefreshing,
    udiSyncing,
  })
  const showStopRunAction = displayRunningRun || isProcessing
  const shadowWatchedCount = shadowMonitorStatus?.watched_count || shadowMonitorStatus?.watched_channels?.length || 0
  const shadowLastEvent = shadowMonitorStatus?.recent_events?.[0]
  const viewerChannels = viewerActivityStatus?.channels || []
  const realWatchedCount = viewerActivityStatus?.real_watched_count || 0
  const watcherOnlyCount = viewerActivityStatus?.watcher_only_count || 0
  const totalRealClients = viewerActivityStatus?.total_real_clients || 0
  const totalWatcherClients = viewerActivityStatus?.total_watcher_clients || 0
  const visibleViewerChannels = viewerChannels.slice(0, 6)
  const hiddenViewerChannelCount = Math.max(0, viewerChannels.length - visibleViewerChannels.length)
  const latestRecordedRun = runHistoryBaseline.latest
  const overviewMetrics = displayRunMetrics.filter(metric => ['checked', 'good', 'dead', 'hidden'].includes(metric.key))

  const syncBadgeClass =
    syncStatus === 'completed' ? 'bg-green-600 text-white border-transparent' :
    syncStatus === 'failed'    ? 'bg-destructive text-destructive-foreground border-transparent' :
    udiRefreshing              ? 'bg-blue-600 text-white border-transparent' :
    ''

  return (
    <div className="min-w-0 space-y-5">
      <div className="flex flex-col gap-4 sm:flex-row sm:items-start sm:justify-between">
        <div className="min-w-0">
          <div className="mb-2 text-[11px] font-bold uppercase tracking-[0.16em] text-primary">Overview</div>
          <h1 className="text-3xl font-bold tracking-tight">Dashboard</h1>
          <p className="mt-1 text-sm text-muted-foreground">Checks, playback and your next scheduled run.</p>
        </div>
        <div className="flex flex-col gap-2 sm:max-w-sm sm:items-end">
          <div className="flex flex-wrap gap-2">
            <Button variant="outline" className="min-h-11" asChild><Link to="/channels">Channels</Link></Button>
            <DropdownMenu>
              <DropdownMenuTrigger asChild>
                <Button
                  disabled={actionStates.runAutomation.disabled}
                  className="min-h-11"
                  aria-describedby={actionStates.runAutomation.reason ? 'run-automation-disabled-reason' : undefined}
                  title={actionStates.runAutomation.reason || 'Run automation for all periods or choose a specific period'}
                >
                  {actionLoading === 'automation' ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : <PlayCircle className="mr-2 h-4 w-4" />}
                  {actionLoading === 'automation' ? 'Starting...' : 'Run automation'}
                  <ChevronDown className="ml-2 h-4 w-4 opacity-60" />
                </Button>
              </DropdownMenuTrigger>
              <DropdownMenuContent align="end" className="max-h-80 w-64 max-w-[calc(100vw-2rem)] overflow-y-auto">
                <DropdownMenuLabel>Choose run mode</DropdownMenuLabel>
                <DropdownMenuSeparator />
                <DropdownMenuItem className="min-h-11" onClick={() => handleRunAutomation(null)}>Run all periods</DropdownMenuItem>
                {periods.length > 0 && (
                  <>
                    <DropdownMenuSeparator />
                    <DropdownMenuLabel className="text-xs text-muted-foreground">Specific periods</DropdownMenuLabel>
                    {periods.map(period => (
                      <DropdownMenuItem className="min-h-11 break-words" key={period.id} onClick={() => handleRunAutomation(period.id)}>
                        {period.name}
                      </DropdownMenuItem>
                    ))}
                  </>
                )}
              </DropdownMenuContent>
            </DropdownMenu>
          </div>
          {actionStates.runAutomation.reason && (
            <p id="run-automation-disabled-reason" className="text-xs text-muted-foreground">{actionStates.runAutomation.reason}</p>
          )}
        </div>
      </div>

      {!showAutomationRunCard && (
        <Card className="border-primary/20">
          <CardContent className="space-y-4 p-5">
            <div className="flex items-start justify-between gap-3">
              <div className="min-w-0 flex-1">
                <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Current activity</p>
                <h2 className="mt-1 text-xl font-semibold">{status && streamCheckerStatus ? 'No check running' : 'Status unavailable'}</h2>
                <p className="mt-1 text-sm text-muted-foreground">
                  {status && streamCheckerStatus ? 'Start a run above or let your scheduled periods take care of it.' : 'Waiting for current automation and checker status.'}
                </p>
              </div>
              <Badge variant="secondary" className="shrink-0 gap-1.5"><Clock3 className="h-3.5 w-3.5" />{status && streamCheckerStatus ? 'Idle' : 'Unknown'}</Badge>
            </div>
            <div className="flex flex-col gap-2 border-t pt-3 sm:flex-row sm:items-center sm:justify-between">
              <div className="min-w-0 text-sm">
                <span className="font-medium">Last recorded run</span>
                <p className="mt-1 text-xs text-muted-foreground">
                  {latestRecordedRun
                    ? `${latestRecordedRun.timestamp ? new Date(latestRecordedRun.timestamp).toLocaleString() : 'Time unavailable'} · ${formatDuration(latestRecordedRun.duration_seconds)} · ${latestRecordedRun.total_channels ?? 0} channels`
                    : 'No automation run history available yet.'}
                </p>
              </div>
              <Button variant="ghost" className="min-h-11 self-start sm:self-auto" asChild><Link to="/changelog"><History className="mr-2 h-4 w-4" />View history</Link></Button>
            </div>
          </CardContent>
        </Card>
      )}

      {showAutomationRunCard && (
        <Card className={failedRun && !displayRunningRun ? 'border-destructive/50' : 'border-primary/20'}>
          <CardHeader className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
            <div className="space-y-1">
              <CardTitle className="flex items-center gap-2 text-lg">
                <ListChecks className="h-5 w-5 text-muted-foreground" />
                {displayRunningRun ? 'Check in progress' : queuedRun ? 'Automation queued' : 'Latest run'}
              </CardTitle>
              <CardDescription>{displayRunMessage}</CardDescription>
            </div>
            <div className="flex flex-wrap items-center gap-2">
              {showStopRunAction && (
                <Button
                  type="button"
                  variant="destructive"
                  className="min-h-11"
                  onClick={handleStopActiveRun}
                  disabled={actionLoading === 'stop-run'}
                  title="Stop the active automation or stream-check run"
                >
                  {actionLoading === 'stop-run' ? (
                    <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                  ) : (
                    <StopCircle className="mr-2 h-4 w-4" />
                  )}
                  Stop Run
                </Button>
              )}
              <Badge variant="outline" className={`w-fit gap-1 ${runBadgeClass}`}>
                {displayRunningRun && <Loader2 className="h-3 w-3 animate-spin" />}
                {!displayRunningRun && failedRun && <AlertCircle className="h-3 w-3" />}
                {!displayRunningRun && abortedRun && <AlertCircle className="h-3 w-3" />}
                {!displayRunningRun && completedRun && <CheckCircle2 className="h-3 w-3" />}
                {runDisplayBadgeLabel}
              </Badge>
            </div>
          </CardHeader>
          <CardContent className="space-y-4">
            <div className="flex flex-wrap items-center gap-x-5 gap-y-2 text-sm">
              <span className="font-semibold">{runDisplayStageLabel}</span>
              <span className="flex items-center gap-1.5 text-muted-foreground"><Timer className="h-4 w-4" />{formatDuration(displayRunElapsedSeconds)} elapsed</span>
              <span className="text-xs text-muted-foreground">Updated {formatTime(displayRunUpdatedAt)}</span>
            </div>

            <div>
              <div className="mb-2 flex items-center justify-between gap-3 text-sm">
                <span className="text-muted-foreground">{runProgressDetail}</span>
                <span className="text-muted-foreground">{Math.round(runProgressPercent)}%</span>
              </div>
              <Progress value={runProgressPercent} className="h-2" />
              {currentCheckingChannelName && (
                <div
                  className="mt-2 flex min-w-0 items-center gap-2 rounded-md border border-border/60 bg-muted/30 px-3 py-2 text-xs text-muted-foreground"
                  title={currentCheckingChannelName}
                >
                  <Loader2 className="h-3.5 w-3.5 shrink-0 animate-spin text-primary" />
                  <span className="shrink-0 font-medium text-foreground/80">Checking now</span>
                  <span className="min-w-0 truncate">{currentCheckingChannelName}</span>
                </div>
              )}
            </div>

            {schedulerRetryPeriods.length > 0 && (
              <div className="rounded-md border border-amber-500/40 bg-amber-500/10 px-3 py-3 text-sm">
                <div className="flex items-center gap-2 font-medium text-amber-800 dark:text-amber-200">
                  <Clock3 className="h-4 w-4" />
                  Quality retry status
                </div>
                <div className="mt-2 space-y-1 text-muted-foreground">
                  {schedulerRetryPeriods.map((retry) => (
                    <div key={retry.period_id} className="flex flex-wrap gap-x-2 gap-y-1">
                      <span className="font-medium text-foreground">
                        {retry.period_name || `Period ${retry.period_id}`}
                      </span>
                      <span>{retry.pending_channels} channel(s) pending</span>
                      <span>
                        {retry.exhausted
                          ? 'Retry budget exhausted; waiting for the next regular schedule'
                          : `Retry ${retry.attempt}/${retry.max_attempts} at ${new Date(retry.next_retry_at).toLocaleString()}`}
                      </span>
                    </div>
                  ))}
                </div>
              </div>
            )}

            <div className="grid grid-cols-2 gap-3 border-t pt-4 sm:grid-cols-4">
              {overviewMetrics.map(metric => (
                <div key={metric.key} className="min-w-0" title={metric.description}>
                  <div className="text-xs text-muted-foreground">{metric.label}</div>
                  <div className="mt-1 text-xl font-semibold tabular-nums">{metric.value === null ? 'N/A' : metric.value}</div>
                </div>
              ))}
            </div>

            <details className="rounded-lg border bg-muted/20 p-3">
              <summary className="cursor-pointer text-sm font-semibold marker:text-primary">Stages and performance details</summary>
              <div className="mt-4 space-y-3">
                <div className="flex flex-wrap gap-x-5 gap-y-2 text-xs text-muted-foreground">
                  <span>Stage time: {formatDuration(displayRunStageElapsedSeconds)}</span>
                  <span>API p95 / p99: {apiTiming.p95_seconds != null ? formatLatency(apiTiming.p95_seconds) : 'N/A'} / {apiTiming.p99_seconds != null ? formatLatency(apiTiming.p99_seconds) : 'N/A'}</span>
                </div>
            <div className="grid grid-cols-2 gap-2 md:grid-cols-4 xl:grid-cols-8">
              {displayStageCards.map((stage) => {
                const isCurrent = stage.id === displayRunStageId && stage.status === 'running'
                const isDone = stage.status === 'completed'
                const isAborted = stage.status === 'aborted'
                const stageClass = isCurrent
                  ? 'border-primary bg-primary/10 text-primary'
                  : isDone
                    ? 'border-green-500/50 bg-green-500/10 text-green-600 dark:text-green-400'
                    : isAborted
                      ? 'border-amber-500/50 bg-amber-500/10 text-amber-700 dark:text-amber-400'
                      : 'border-border bg-background text-muted-foreground'
                return (
                  <div key={stage.id} className={`rounded-md border px-3 py-2 text-xs font-medium ${stageClass}`}>
                    <div className="flex items-center gap-2">
                      {isCurrent && displayRunningRun ? (
                        <Loader2 className="h-3.5 w-3.5 animate-spin" />
                      ) : isDone ? (
                        <CheckCircle2 className="h-3.5 w-3.5" />
                      ) : isAborted ? (
                        <AlertCircle className="h-3.5 w-3.5" />
                      ) : (
                        <Activity className="h-3.5 w-3.5" />
                      )}
                      <span className="truncate">{stage.label}</span>
                    </div>
                  </div>
                )
              })}
            </div>

            <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4 xl:grid-cols-5 2xl:grid-cols-10">
              {displayRunMetrics.map((metric) => (
                <div key={metric.key} className="rounded-md border p-3" title={metric.description}>
                  <div className="text-xs text-muted-foreground">{metric.label}</div>
                  <div className={`text-xl font-semibold ${metric.value === null ? 'text-muted-foreground' : ''}`}>
                    {metric.value === null ? 'N/A' : metric.value}
                  </div>
                </div>
              ))}
            </div>

            <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
              {durationCards.map((card) => (
                <div key={card.label} className="rounded-md border p-3">
                  <div className="text-xs text-muted-foreground">{card.label}</div>
                  <div className="text-base font-semibold">{card.value}</div>
                  {card.detail && (
                    <div className="mt-1 text-xs text-muted-foreground">{card.detail}</div>
                  )}
                </div>
              ))}
            </div>
              </div>
            </details>
          </CardContent>
        </Card>
      )}

      {/* Background services are separate from the actual check state above. */}
      <div className="flex flex-wrap items-center gap-x-5 gap-y-2 px-1 text-xs text-muted-foreground">
        <span className="flex items-center gap-1.5"><Activity className="h-3.5 w-3.5" />Scheduler: <span className="font-medium text-foreground">{status ? (schedulerRunning ? 'Active' : 'Stopped') : 'Unknown'}</span></span>
        <Link to="/shadow-monitor" className="flex flex-wrap items-center gap-1.5 rounded-sm hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">
          <Eye className="h-3.5 w-3.5" />Shadow monitor:
          <span className="font-medium text-foreground">{!shadowMonitorStatus ? 'Unknown' : shadowMonitorStatus.running ? `Watching ${shadowWatchedCount} channels` : shadowMonitorStatus.enabled ? 'Enabled' : 'Disabled'}</span>
          {shadowMonitorStatus?.dry_run && <span>(dry run)</span>}
        </Link>
        {status?.last_playlist_update && <span>Playlist update: {new Date(status.last_playlist_update).toLocaleString()}</span>}
      </div>

      {syncStatus === 'failed' && (
        <Alert variant="destructive">
          <WifiOff className="h-4 w-4" />
          <AlertDescription>Dispatcharr cache refresh failed. Open System and maintenance below to retry or review the connection in Settings.</AlertDescription>
        </Alert>
      )}

      <UpcomingAutomationEvents />

      <Card>
        <CardHeader className="flex flex-row flex-wrap items-center justify-between gap-2 space-y-0 pb-3">
          <CardTitle className="flex items-center gap-2 text-base"><Users className="h-4 w-4 text-muted-foreground" />Live playback</CardTitle>
          <Button variant="ghost" className="min-h-11" asChild><Link to="/stream-monitoring">Monitoring</Link></Button>
        </CardHeader>
        <CardContent className="space-y-3">
          <div className="flex flex-wrap gap-x-5 gap-y-2 text-sm">
            <span><strong className="tabular-nums">{viewerActivityStatus ? totalRealClients : 'N/A'}</strong> <span className="text-muted-foreground">viewer clients</span></span>
            <span><strong className="tabular-nums">{viewerActivityStatus ? totalWatcherClients : 'N/A'}</strong> <span className="text-muted-foreground">watcher clients</span></span>
            <span><strong className="tabular-nums">{viewerActivityStatus ? viewerChannels.length : 'N/A'}</strong> <span className="text-muted-foreground">active channels</span></span>
          </div>
          {viewerChannels.length > 0 && (
            <div className="flex flex-wrap gap-2">
              <Badge variant="secondary">{formatRealViewerChannelCount(realWatchedCount)}</Badge>
              <Badge variant="outline">{formatWatcherOnlyChannelCount(watcherOnlyCount)}</Badge>
            </div>
          )}
          {viewerChannels.length === 0 ? (
            <p className="text-sm text-muted-foreground">{viewerActivityStatus ? 'No active channel playback detected.' : 'Viewer activity is unavailable.'}</p>
          ) : (
            <div className="grid gap-2 lg:grid-cols-2 xl:grid-cols-3">
              {visibleViewerChannels.map((channel) => (
                <div
                  key={`${channel.channel_uuid || channel.channel_id}-${channel.stream_id || 'stream'}`}
                  className="rounded-md border bg-background p-3"
                >
                  <div className="flex items-start justify-between gap-3">
                    <div className="min-w-0">
                      <div className="truncate text-sm font-medium">{channel.channel_name || 'Unknown Channel'}</div>
                      <div className="text-xs text-muted-foreground">
                        {getViewerActivityDetailLabel(channel)}
                        {formatStreamRef(channel.stream_id)}
                      </div>
                    </div>
                    {channel.has_real_clients ? (
                      <Badge className="shrink-0 bg-green-600 text-white">{getPlaybackBadgeLabel(channel)}</Badge>
                    ) : (
                      <Badge variant="outline" className="shrink-0">{getPlaybackBadgeLabel(channel)}</Badge>
                    )}
                  </div>
                  <div className="mt-3 flex flex-wrap gap-2 text-xs">
                    <Badge variant="secondary">{formatViewerClientCount(channel.real_client_count)}</Badge>
                    <Badge variant="outline">{formatWatcherClientCount(channel.watcher_client_count)}</Badge>
                  </div>
                </div>
              ))}
            </div>
          )}

          {hiddenViewerChannelCount > 0 && (
            <p className="text-xs text-muted-foreground">
              {hiddenViewerChannelCount} more active channels are not shown in this summary
            </p>
          )}

          {viewerChannels.length > 0 && realWatchedCount === 0 && shadowMonitorStatus?.running && (
            <Alert>
              <Eye className="h-4 w-4" />
              <AlertDescription>
                Only watcher clients are active; no real viewer clients are currently detected.
              </AlertDescription>
            </Alert>
          )}
        </CardContent>
      </Card>

      {/* System Information */}
      <details className="rounded-xl border bg-card p-4">
        <summary className="cursor-pointer py-1 text-sm font-semibold marker:text-primary">System and maintenance</summary>
        <div className="mt-4 space-y-4">
      <Card>
        <CardHeader>
          <CardTitle className="text-base">Dispatcharr cache</CardTitle>
          <CardDescription>Refresh the cached channel, stream and playlist data.</CardDescription>
        </CardHeader>
        <CardContent>
          <div className="flex flex-col sm:flex-row gap-6">

            {/* Dispatcharr Cache Stats */}
            <div className="flex-1 border rounded-lg p-4 bg-muted/30 space-y-3">
              <div className="flex items-center justify-between">
                <span className="text-xs font-semibold uppercase tracking-wider text-muted-foreground">
                  Dispatcharr Cache
                </span>
                {udiRefreshing ? (
                  <Badge variant="outline" className="text-xs gap-1 border-blue-400 text-blue-400">
                    <Loader2 className="h-3 w-3 animate-spin" />Syncing
                  </Badge>
                ) : syncStatus ? (
                  <Badge variant="outline" className={`text-xs ${syncBadgeClass}`}>
                    {syncStatus === 'completed' && <CheckCircle2 className="h-3 w-3 mr-1" />}
                    {syncStatus === 'failed'    && <WifiOff      className="h-3 w-3 mr-1" />}
                    {syncStatus === 'completed' ? 'Synced' : syncStatus === 'failed' ? 'Failed' : syncStatus === 'idle' ? 'Idle' : 'Unknown'}
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
                {udiRefreshing
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
                disabled={actionStates.reloadUdi.disabled}
                variant="outline"
                className="min-h-11 w-full"
                title={actionStates.reloadUdi.reason || 'Reload Dispatcharr cache'}
              >
                {udiRefreshing
                  ? <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                  : <RefreshCw className="mr-2 h-4 w-4" />}
                {udiRefreshing ? 'Syncing...' : 'Reload UDI'}
              </Button>

            </div>
          </div>
        </CardContent>
      </Card>


      <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
        <Card>
          <CardHeader><CardTitle>Automation Configuration</CardTitle></CardHeader>
          <CardContent>
            <dl className="space-y-3 text-sm">
              {shadowLastEvent && <div className="flex flex-wrap justify-between gap-2"><dt className="text-muted-foreground">Last shadow event:</dt><dd>{formatShadowEvent(shadowLastEvent.type)}</dd></div>}
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
                  <Badge variant={checkerConcurrencyDisplay.active ? "outline" : "secondary"}>
                    {checkerConcurrencyDisplay.text}
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
                <dt className="text-muted-foreground">{totalProcessedLabel}</dt>
                <dd><Badge variant={queueHistoryOnly ? 'secondary' : 'outline'}>{totalProcessed}</Badge></dd>
              </div>
              {queueSize > 0 && (
                <div className="pt-2">
                  <div className="flex justify-between items-center mb-2">
                    <Label className="text-xs text-muted-foreground block">Processing Progress</Label>
                    {streamCheckerEtaDisplay.label ? (
                      <span
                        className={`text-xs text-muted-foreground ${streamCheckerEtaDisplay.pulse ? 'animate-pulse text-primary/70' : ''}`}
                        title={streamCheckerEtaDisplay.title}
                      >
                        {streamCheckerEtaDisplay.label}
                      </span>
                    ) : (
                      null
                    )}
                  </div>
                  <Progress value={queueProgress} className="h-2" />
                </div>
              )}
            </dl>
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <History className="h-4 w-4 text-muted-foreground" />
              Recent Run Baseline
            </CardTitle>
          </CardHeader>
          <CardContent>
            {runHistoryBaseline.available ? (
              <dl className="space-y-3 text-sm">
                <div className="flex items-center justify-between gap-3">
                  <dt className="text-muted-foreground">Typical Duration:</dt>
                  <dd><Badge variant="outline">{formatDuration(runHistoryBaseline.typicalDurationSeconds)}</Badge></dd>
                </div>
                <div className="flex items-center justify-between gap-3">
                  <dt className="text-muted-foreground">Seconds / Channel:</dt>
                  <dd><Badge variant="secondary">{getSecondsPerChannelBaselineLabel(runHistoryBaseline)}</Badge></dd>
                </div>
                <div className="flex items-center justify-between gap-3">
                  <dt className="text-muted-foreground">Samples:</dt>
                  <dd><Badge variant="secondary">{runHistoryBaseline.sampleCount}</Badge></dd>
                </div>
                <div className="flex items-center justify-between gap-3">
                  <dt className="text-muted-foreground">Last Run:</dt>
                  <dd className="text-right">
                    <div className="font-medium">{formatDuration(runHistoryBaseline.latest?.duration_seconds)}</div>
                    <div className="text-xs text-muted-foreground">{formatTime(runHistoryBaseline.latest?.timestamp)}</div>
                  </dd>
                </div>
              </dl>
            ) : (
              <p className="text-sm text-muted-foreground">No completed automation runs yet</p>
            )}
          </CardContent>
        </Card>
      </div>
        </div>
      </details>

      {/* Available Playlists */}
      <details className="rounded-xl border bg-card p-4">
        <summary className="cursor-pointer text-sm font-semibold marker:text-primary">Global playlist visibility</summary>
        <div className="mt-4">
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
                  <div key={playlist.id} className="flex min-w-0 items-center justify-between gap-3 rounded-lg border p-3">
                    <div className="min-w-0 flex-1">
                      <div className="flex flex-wrap items-center gap-2">
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
                      aria-label={`Include ${playlist.name} in automation`}
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
      </details>
    </div>
  )
}
