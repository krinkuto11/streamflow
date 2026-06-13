import { useEffect, useMemo, useState } from 'react'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card.jsx'
import { Button } from '@/components/ui/button.jsx'
import { Badge } from '@/components/ui/badge.jsx'
import { Input } from '@/components/ui/input.jsx'
import { Label } from '@/components/ui/label.jsx'
import { Switch } from '@/components/ui/switch.jsx'
import { Separator } from '@/components/ui/separator.jsx'
import { AlertDialog, AlertDialogAction, AlertDialogCancel, AlertDialogContent, AlertDialogDescription, AlertDialogFooter, AlertDialogHeader, AlertDialogTitle } from '@/components/ui/alert-dialog.jsx'
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from '@/components/ui/tooltip.jsx'
import { DropdownMenu, DropdownMenuCheckboxItem, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger } from '@/components/ui/dropdown-menu.jsx'
import { Accordion, AccordionContent, AccordionItem, AccordionTrigger } from '@/components/ui/accordion.jsx'
import { useToast } from '@/hooks/use-toast.js'
import { teamarrPreflightAPI, automationAPI } from '@/services/api.js'
import { collectTeamarrFilterOptions, parseFilterCsv, toggleFilterCsvTerm } from '@/lib/teamarr-preflight-filters.js'
import {
  filterTeamarrEventsBySearch,
  filterTeamarrEventsByView,
  paginateTeamarrEvents,
  sortTeamarrManagedEvents,
} from '@/lib/teamarr-preflight-event-search.js'
import { getTeamarrEventHealthAlert } from '@/lib/teamarr-preflight-event-health.js'
import { getTeamarrAutomaticCheck, getTeamarrSchedulePreview } from '@/lib/teamarr-preflight-schedule.js'
import { getTeamarrActiveChecksDetail } from '@/lib/teamarr-preflight-status-display.js'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select.jsx'
import {
  Activity,
  AlertCircle,
  CalendarCheck,
  ChevronDown,
  CheckCircle2,
  Clock,
  Info,
  ListChecks,
  Loader2,
  PlayCircle,
  RefreshCw,
  Save,
  Search,
  StopCircle,
  Wifi,
} from 'lucide-react'

const numberFields = [
  { key: 'poll_interval_seconds', label: 'Teamarr Poll Interval', suffix: 'sec', min: 15, max: 3600, description: 'How often StreamFlow reads Teamarr managed events. 30-60 sec keeps narrow event windows reliable.' },
  { key: 'preflight_offset_minutes', label: 'Preflight Offset', suffix: 'min', min: 1, max: 360, description: 'Main automatic check before start. 20 means the first check becomes due around -20 min.' },
  { key: 'post_start_grace_minutes', label: 'Post Start Grace', suffix: 'min', min: 0, max: 120, description: 'How long after start post-start offsets can still run.' },
  { key: 'max_concurrent_checks', label: 'Concurrent Checks', suffix: 'max', min: 1, max: 10, description: 'Maximum Teamarr event checks running at the same time.' },
  { key: 'event_cooldown_minutes', label: 'Event Cooldown', suffix: 'min', min: 1, max: 10080, description: 'Prevents the same event bucket from repeating after it already ran.' },
]

const eventLabels = {
  preflight_started: 'Started',
  preflight_completed: 'Completed',
  preflight_failed: 'Failed',
  preflight_deferred: 'Deferred',
  preflight_queued: 'Queued',
  deferred_automation_active: 'Deferred',
  manual_preflight_rejected: 'Rejected',
  no_streams_yet: 'No Streams',
  deferred_quality_check_active: 'Deferred',
  concurrency_limit: 'Limit',
  scan_failed: 'Scan Failed',
}

const reasonLabels = {
  active_viewers: 'Active viewers',
  connectivity_guard: 'Connectivity guard',
  max_streams_reached: 'Provider limit',
}

const stateLabels = {
  due: 'Due',
  scheduled: 'Scheduled',
  already_attempted: 'Attempted',
  no_dispatcharr_channel: 'No Channel',
  no_live_window: 'No Window',
  no_streams_yet: 'No Streams',
  incomplete_team: 'Incomplete',
  waiting_for_channel_sync: 'Syncing',
  filtered: 'Filtered',
  past: 'Past',
}

const DEFAULT_PROFILE_SELECT_VALUE = '__teamarr_default_profile__'
const MANAGED_EVENT_PAGE_SIZE = 10
const RECENT_EVENT_PAGE_SIZE = 10
const ACTIVE_CHECK_DISPLAY_LIMIT = 20
const DEFAULT_PREFLIGHT_STREAM_CHECKING = {
  enabled: true,
  remove_dead_streams: false,
  blank_check_enabled: false,
  treat_blank_as_dead: false,
  freeze_check_enabled: false,
  treat_freeze_as_dead: false,
  loop_check_enabled: false,
}
const DEFAULT_PREFLIGHT_VISIBILITY = {
  enabled: false,
  hide_on_no_regex: false,
  hide_on_no_streams: false,
  hide_on_all_failed: false,
  unhide_on_recovered: false,
}

const parseCsv = (value) => (
  String(value || '')
    .split(',')
    .map(item => item.trim())
    .filter(Boolean)
)

const normalizeProfiles = (payload) => {
  const items = Array.isArray(payload)
    ? payload
    : (Array.isArray(payload?.items) ? payload.items : [])
  return items
    .filter(profile => profile && profile.id !== undefined && profile.id !== null)
    .map(profile => ({ ...profile, id: String(profile.id) }))
}

const formatTimestamp = (timestamp) => {
  if (!timestamp) return 'Never'
  return new Date(timestamp * 1000).toLocaleTimeString()
}

const formatDateTime = (value) => {
  if (!value) return 'N/A'
  return new Date(value).toLocaleString()
}

const formatOffset = (seconds) => {
  if (seconds === null || seconds === undefined) return 'N/A'
  const sign = seconds < 0 ? '-' : ''
  const abs = Math.abs(Number(seconds))
  const hours = Math.floor(abs / 3600)
  const minutes = Math.floor((abs % 3600) / 60)
  const secs = Math.floor(abs % 60)
  if (hours) return `${sign}${hours}h ${minutes}m`
  if (minutes) return `${sign}${minutes}m ${secs}s`
  return `${sign}${secs}s`
}

const formatElapsedSince = (timestamp) => {
  if (!timestamp) return 'N/A'
  const elapsed = Math.max(0, Math.floor(Date.now() / 1000 - Number(timestamp)))
  return formatOffset(elapsed)
}

const eventLabel = (type) => eventLabels[type] || type || 'Unknown'
const stateLabel = (state) => stateLabels[state] || state || 'Unknown'
const forceableStates = new Set(['due', 'scheduled', 'already_attempted', 'past'])
const preflightKindLabel = (item) => (item?.preflight_kind === 'team' ? 'Teamarr Team' : 'Teamarr Event')
const preflightKindShortLabel = (item) => (item?.preflight_kind === 'team' ? 'Team' : 'Event')
const preflightItemTitle = (item) => item?.team_name || item?.event_name || item?.channel_name || 'Preflight Item'
const preflightItemChannel = (item) => item?.channel_name || `Channel ${item?.dispatcharr_channel_id || 'N/A'}`

const titleizeCode = (value) => (
  String(value || '')
    .replace(/[_-]+/g, ' ')
    .replace(/\s+/g, ' ')
    .trim()
    .replace(/\b\w/g, char => char.toUpperCase())
)

const reasonLabel = (reason) => reasonLabels[reason] || titleizeCode(reason)

const recentEventBadgeVariant = (type) => {
  if (type === 'preflight_completed') return 'default'
  if (['preflight_failed', 'manual_preflight_rejected', 'scan_failed'].includes(type)) return 'destructive'
  return 'secondary'
}

const recentEventDetailParts = (event) => {
  const details = event?.details || {}
  const stats = details.stats || {}
  const parts = []
  const unit = (count, singular, plural = `${singular}s`) => `${count} ${count === 1 ? singular : plural}`

  if (details.bucket) parts.push(`Bucket ${details.bucket}`)
  if (details.reason) parts.push(reasonLabel(details.reason))
  if (details.error) parts.push(details.error)
  if (stats.total_streams !== undefined) parts.push(unit(Number(stats.total_streams), 'stream'))
  if (stats.dead_streams !== undefined) parts.push(`${stats.dead_streams} dead`)
  if (stats.duration || stats.duration_seconds !== undefined) parts.push(stats.duration || `${stats.duration_seconds}s`)
  if (stats.avg_resolution) parts.push(stats.avg_resolution)
  if (stats.avg_fps) parts.push(stats.avg_fps)

  return parts.filter(Boolean)
}

const canForceEvent = (event) => (
  Boolean(event?.identity)
  && Boolean(event?.dispatcharr_channel_id)
  && forceableStates.has(String(event?.state || ''))
)

const eventCheckSummary = (event, lastPreflightEvent) => {
  if (lastPreflightEvent) return `Latest check: ${eventLabel(lastPreflightEvent.type)}`
  if (event?.state === 'already_attempted') return 'Latest check: attempted in current cooldown'
  return 'Latest check: not recorded'
}

const eventAutomaticCheckSummary = (event, config) => {
  const nextCheck = getTeamarrAutomaticCheck(event, config)
  if (!nextCheck) return ''
  const bucket = nextCheck.bucket ? ` (${nextCheck.bucket})` : ''
  if (!nextCheck.timestamp) return `${nextCheck.label}${bucket}`
  return `${nextCheck.label}: ${formatDateTime(nextCheck.timestamp)}${bucket}`
}

const connectorStatusDisplay = (connector = {}) => {
  const state = String(connector?.state || '').trim()
  if (state === 'connected') {
    return {
      label: connector.label || 'Connected',
      detail: connector.detail || 'Teamarr managed event channels are available.',
      variant: 'default',
      className: 'bg-green-500',
      icon: CheckCircle2,
    }
  }
  if (state === 'empty' || state === 'filtered' || state === 'pending') {
    return {
      label: connector.label || (state === 'pending' ? 'Waiting for scan' : 'No events'),
      detail: connector.detail || 'Teamarr is reachable, but no matching managed events are available.',
      variant: 'secondary',
      className: '',
      icon: Info,
    }
  }
  if (state === 'error') {
    return {
      label: connector.label || 'Scan error',
      detail: connector.detail || 'Teamarr managed event endpoint did not complete the last scan.',
      variant: 'destructive',
      className: '',
      icon: AlertCircle,
    }
  }
  return {
    label: connector.label || 'Not configured',
    detail: connector.detail || 'Set the Teamarr base URL to read managed event channels.',
    variant: 'secondary',
    className: '',
    icon: Wifi,
  }
}

const eventScheduleDiagnosticParts = (event, automaticCheckSummary, config) => {
  const parts = []
  const seconds = Number(event?.seconds_to_start)
  parts.push({ label: 'State', value: stateLabel(event?.state) })
  if (event?.trigger_bucket) parts.push({ label: 'Bucket', value: event.trigger_bucket })
  if (automaticCheckSummary) parts.push({ label: 'Schedule', value: automaticCheckSummary })
  if (!automaticCheckSummary && event?.state === 'past') parts.push({ label: 'Schedule', value: 'Outside automatic window' })
  if (!automaticCheckSummary && event?.state === 'filtered') parts.push({ label: 'Schedule', value: 'Hidden by filters' })
  if (Number.isFinite(seconds)) {
    parts.push({
      label: seconds < 0 ? 'Since start' : 'Until start',
      value: formatOffset(seconds),
    })
  }
  if (event?.dispatcharr_channel_id) parts.push({ label: 'Channel ID', value: event.dispatcharr_channel_id })
  if (config?.poll_interval_seconds) parts.push({ label: 'Poll', value: `${config.poll_interval_seconds}s` })
  return parts.filter(part => part.value !== undefined && part.value !== null && part.value !== '')
}

const forceEventTooltip = (event) => {
  if (!event?.dispatcharr_channel_id) return 'No Dispatcharr channel'
  if (event?.state === 'no_live_window') return 'No live window'
  if (event?.state === 'no_streams_yet') return 'No channel streams'
  if (event?.state === 'incomplete_team') return 'Incomplete team status'
  if (event?.state === 'waiting_for_channel_sync') return 'Channel syncing'
  if (event?.state === 'filtered') return 'Filtered'
  if (event?.state === 'past') return 'Run manual check'
  if (!forceableStates.has(String(event?.state || ''))) return 'Unavailable'
  return event?.preflight_kind === 'team' ? 'Run team check' : 'Run event check'
}

const normalizeFilterOption = (option) => {
  if (option && typeof option === 'object') {
    const rawValue = option.value ?? option.slug ?? option.id ?? option.name ?? option.label
    const value = String(rawValue || '').trim().toLowerCase()
    if (!value) return null
    const label = String(option.label ?? option.name ?? option.league_alias ?? rawValue ?? value).trim()
    return {
      value,
      label: label || titleizeCode(value),
      sport: option.sport ? String(option.sport).trim().toLowerCase() : undefined,
    }
  }

  const value = String(option || '').trim().toLowerCase()
  if (!value) return null
  return {
    value,
    label: titleizeCode(value),
  }
}

const mergeFilterOptions = (primaryOptions = [], fallbackOptions = [], selectedValues = []) => {
  const byValue = new Map()
  const addOption = (option) => {
    const normalized = normalizeFilterOption(option)
    if (normalized && !byValue.has(normalized.value)) {
      byValue.set(normalized.value, normalized)
    }
  }

  primaryOptions.forEach(addOption)
  fallbackOptions.forEach(addOption)
  selectedValues.flatMap(parseFilterCsv).forEach(value => addOption(value))

  return [...byValue.values()].sort((a, b) => a.label.localeCompare(b.label))
}

const filterLabelForValue = (value, options) => (
  options.find(option => option.value === value)?.label || titleizeCode(value)
)

export default function TeamarrPreflight() {
  const [config, setConfig] = useState(null)
  const [editedConfig, setEditedConfig] = useState(null)
  const [status, setStatus] = useState(null)
  const [retryOffsets, setRetryOffsets] = useState('')
  const [postStartOffsets, setPostStartOffsets] = useState('')
  const [includeSports, setIncludeSports] = useState('')
  const [excludeSports, setExcludeSports] = useState('')
  const [includeLeagues, setIncludeLeagues] = useState('')
  const [excludeLeagues, setExcludeLeagues] = useState('')
  const [profiles, setProfiles] = useState([])
  const [loading, setLoading] = useState(true)
  const [actionLoading, setActionLoading] = useState('')
  const [forceEvent, setForceEvent] = useState(null)
  const [eventSearch, setEventSearch] = useState('')
  const [sourceView, setSourceView] = useState('all')
  const [managedEventView, setManagedEventView] = useState('upcoming')
  const [managedEventPage, setManagedEventPage] = useState(1)
  const [recentEventPage, setRecentEventPage] = useState(1)
  const { toast } = useToast()

  useEffect(() => {
    loadData()
    const interval = setInterval(loadStatus, 5000)
    return () => clearInterval(interval)
  }, [])

  useEffect(() => {
    setManagedEventPage(1)
    setRecentEventPage(1)
  }, [eventSearch, managedEventView, sourceView])

  const upcomingEvents = status?.upcoming_events || []
  const upcomingTeams = status?.upcoming_teams || []
  const preflightItems = status?.preflight_items?.length
    ? status.preflight_items
    : [...upcomingEvents, ...upcomingTeams]
  const recentEvents = status?.recent_events || []
  const activeChecks = status?.active_checks || []
  const queuedChecks = status?.queued_checks || []
  const queueActiveChecks = status?.queue_active_checks || []
  const queuedChecksCount = Number(status?.queued_checks_count ?? queuedChecks.length)
  const queueActiveChecksCount = Number(status?.queue_active_checks_count ?? queueActiveChecks.length)
  const allActiveChecks = useMemo(
    () => [
      ...activeChecks.map(check => ({ ...check, run_source: check.run_source || 'direct' })),
      ...queueActiveChecks.map(check => ({ ...check, run_source: check.run_source || 'queue' })),
    ],
    [activeChecks, queueActiveChecks]
  )
  const sourceFilteredPreflightItems = useMemo(
    () => preflightItems.filter(item => (
      sourceView === 'all'
      || (sourceView === 'events' && item?.preflight_kind !== 'team')
      || (sourceView === 'teams' && item?.preflight_kind === 'team')
    )),
    [preflightItems, sourceView]
  )
  const sourceFilteredRecentEvents = useMemo(
    () => recentEvents.filter(item => (
      sourceView === 'all'
      || (sourceView === 'events' && item?.preflight_kind !== 'team')
      || (sourceView === 'teams' && item?.preflight_kind === 'team')
    )),
    [recentEvents, sourceView]
  )
  const sourceFilteredActiveChecks = useMemo(
    () => allActiveChecks.filter(item => (
      sourceView === 'all'
      || (sourceView === 'events' && item?.preflight_kind !== 'team')
      || (sourceView === 'teams' && item?.preflight_kind === 'team')
    )),
    [allActiveChecks, sourceView]
  )
  const sortedUpcomingEvents = useMemo(
    () => sortTeamarrManagedEvents(sourceFilteredPreflightItems),
    [sourceFilteredPreflightItems]
  )
  const effectivePreflightConfig = status?.config || config || editedConfig || null
  const upcomingViewEvents = useMemo(
    () => filterTeamarrEventsByView(sortedUpcomingEvents, 'upcoming', effectivePreflightConfig || {}),
    [sortedUpcomingEvents, effectivePreflightConfig]
  )
  const viewFilteredUpcomingEvents = useMemo(
    () => filterTeamarrEventsByView(sortedUpcomingEvents, managedEventView, effectivePreflightConfig || {}),
    [sortedUpcomingEvents, managedEventView, effectivePreflightConfig]
  )
  const filteredUpcomingEvents = useMemo(
    () => filterTeamarrEventsBySearch(viewFilteredUpcomingEvents, eventSearch),
    [viewFilteredUpcomingEvents, eventSearch]
  )
  const filteredRecentEvents = useMemo(
    () => filterTeamarrEventsBySearch(sourceFilteredRecentEvents, eventSearch),
    [sourceFilteredRecentEvents, eventSearch]
  )
  const filteredActiveChecks = useMemo(
    () => filterTeamarrEventsBySearch(sourceFilteredActiveChecks, eventSearch),
    [sourceFilteredActiveChecks, eventSearch]
  )
  const managedEventPageCount = Math.max(1, Math.ceil(filteredUpcomingEvents.length / MANAGED_EVENT_PAGE_SIZE))
  const safeManagedEventPage = Math.min(managedEventPage, managedEventPageCount)
  const displayedUpcomingEvents = paginateTeamarrEvents(
    filteredUpcomingEvents,
    safeManagedEventPage,
    MANAGED_EVENT_PAGE_SIZE,
  )
  const recentEventPageCount = Math.max(1, Math.ceil(filteredRecentEvents.length / RECENT_EVENT_PAGE_SIZE))
  const safeRecentEventPage = Math.min(recentEventPage, recentEventPageCount)
  const displayedRecentEvents = paginateTeamarrEvents(
    filteredRecentEvents,
    safeRecentEventPage,
    RECENT_EVENT_PAGE_SIZE,
  )
  const displayedActiveChecks = filteredActiveChecks.slice(0, ACTIVE_CHECK_DISPLAY_LIMIT)
  const activeChecksCount = allActiveChecks.length
  const sourceActiveChecksCount = sourceFilteredActiveChecks.length
  const directActiveChecksCount = activeChecks.length
  const upcomingViewCount = upcomingViewEvents.length
  const allManagedEventsCount = upcomingEvents.length
  const allStaticTeamsCount = upcomingTeams.length
  const allPreflightItemsCount = preflightItems.length
  const pastEventsCount = preflightItems.filter(item => String(item?.state || '') === 'past').length
  const teamStatus = status?.team_status || {}
  const managedCandidates = Number(status?.managed_candidates ?? upcomingEvents.length)
  const managedEventsSeen = Number(status?.managed_events_seen ?? managedCandidates)
  const managedEventsReturned = Number(status?.managed_events_returned ?? upcomingEvents.length)
  const managedEventsTruncated = Boolean(status?.managed_events_truncated)
  const nextEvent = useMemo(() => upcomingViewEvents[0] || null, [upcomingViewEvents])
  const connectorDisplay = connectorStatusDisplay(status?.teamarr_connector || {})
  const ConnectorIcon = connectorDisplay.icon
  const queuedChecksDetail = queuedChecksCount > 0
    ? (queueActiveChecksCount > 0 ? `${queueActiveChecksCount} running from queue` : 'Waiting for Stream Checker')
    : (queueActiveChecksCount > 0 ? `${queueActiveChecksCount} running from queue` : 'No queued events')
  const activeChecksDetail = getTeamarrActiveChecksDetail({
    directActiveChecksCount,
    queueActiveChecksCount,
    editedConfig,
    status,
    config,
  })
  const managedEventViewLabels = {
    upcoming: 'upcoming',
    due: 'due',
    no_check: 'without checks',
    past: 'past',
    all: 'all',
  }
  const managedViewLabel = managedEventViewLabels[managedEventView] || 'managed'
  const managedEventSummary = eventSearch.trim()
    ? `${filteredUpcomingEvents.length} of ${viewFilteredUpcomingEvents.length} ${managedViewLabel} items (${allPreflightItemsCount} all)`
    : `${viewFilteredUpcomingEvents.length} ${managedViewLabel} items (${allPreflightItemsCount} all)`
  const previewConfig = useMemo(() => ({
    ...(editedConfig || {}),
    retry_offsets_minutes: parseCsv(retryOffsets).map(item => Number(item)).filter(Number.isFinite),
    post_start_offsets_minutes: parseCsv(postStartOffsets).map(item => Number(item)).filter(Number.isFinite),
  }), [editedConfig, retryOffsets, postStartOffsets])
  const previewEvent = useMemo(() => (
    nextEvent || {
      event_name: 'Example event',
      event_date: new Date(Date.now() + Number(previewConfig.preflight_offset_minutes || 20) * 60 * 1000).toISOString(),
    }
  ), [nextEvent, previewConfig.preflight_offset_minutes])
  const timingPreview = useMemo(
    () => getTeamarrSchedulePreview(previewEvent, previewConfig),
    [previewEvent, previewConfig]
  )
  const eventFilterOptions = useMemo(
    () => collectTeamarrFilterOptions([...preflightItems, ...recentEvents]),
    [preflightItems, recentEvents]
  )
  const filterOptions = useMemo(() => {
    const serverOptions = status?.filter_options || {}
    return {
      sports: mergeFilterOptions(
        serverOptions.sports || [],
        eventFilterOptions.sports || [],
        [includeSports, excludeSports],
      ),
      leagues: mergeFilterOptions(
        serverOptions.leagues || [],
        eventFilterOptions.leagues || [],
        [includeLeagues, excludeLeagues],
      ),
    }
  }, [status?.filter_options, eventFilterOptions, includeSports, excludeSports, includeLeagues, excludeLeagues])
  const profileOptions = useMemo(() => {
    const items = [...profiles]
    const defaultProfileId = editedConfig?.default_profile_id ? String(editedConfig.default_profile_id) : ''
    if (defaultProfileId && !items.some(profile => String(profile.id) === defaultProfileId)) {
      items.unshift({
        id: defaultProfileId,
        name: editedConfig.default_profile_name || 'Teamarr Event Preflight',
        description: 'Default Teamarr preflight profile',
      })
    }
    return items
  }, [profiles, editedConfig])
  const selectedProfileValue = useMemo(() => {
    const profileId = editedConfig?.forced_profile_id || editedConfig?.default_profile_id || ''
    return profileId ? String(profileId) : DEFAULT_PROFILE_SELECT_VALUE
  }, [editedConfig])
  const selectedQualityProfile = useMemo(
    () => profileOptions.find(profile => String(profile.id) === String(selectedProfileValue)) || null,
    [profileOptions, selectedProfileValue]
  )
  const selectedProfileUsesDefaultRules = selectedProfileValue === String(editedConfig?.default_profile_id || '')
  const selectedStreamChecking = selectedQualityProfile?.stream_checking
    || (selectedProfileUsesDefaultRules ? DEFAULT_PREFLIGHT_STREAM_CHECKING : null)
  const selectedVisibility = selectedQualityProfile?.channel_visibility_automation
    || (selectedProfileUsesDefaultRules ? DEFAULT_PREFLIGHT_VISIBILITY : null)
  const qualityRuleSummary = useMemo(() => {
    if (!selectedStreamChecking) return []
    const blankEnabled = Boolean(selectedStreamChecking.blank_check_enabled)
    const freezeEnabled = Boolean(selectedStreamChecking.freeze_check_enabled)
    const visibilityEnabled = selectedVisibility?.enabled === true
    const visibilityHideRulesEnabled = Boolean(
      selectedVisibility?.hide_on_no_regex
      || selectedVisibility?.hide_on_no_streams
      || selectedVisibility?.hide_on_all_failed
    )
    return [
      {
        label: 'Quality check',
        value: selectedStreamChecking.enabled === false ? 'Disabled' : 'Enabled',
        description: selectedStreamChecking.enabled === false
          ? 'Event checks use schedule matching only.'
          : 'Runs stream quality checks before event promotion.',
        variant: selectedStreamChecking.enabled === false ? 'secondary' : 'default',
      },
      {
        label: 'Dead removal',
        value: selectedStreamChecking.remove_dead_streams ? 'Remove' : 'Keep',
        description: selectedStreamChecking.remove_dead_streams
          ? 'Dead streams are removed from event channels.'
          : 'Dead streams stay available for manual review.',
        variant: selectedStreamChecking.remove_dead_streams ? 'destructive' : 'secondary',
      },
      {
        label: 'Blank detection',
        value: blankEnabled ? (selectedStreamChecking.treat_blank_as_dead ? 'Dead' : 'Detect') : 'Off',
        description: blankEnabled
          ? (selectedStreamChecking.treat_blank_as_dead
            ? 'Blank video is treated as dead.'
            : 'Blank video is reported only.')
          : 'Blank-frame checks are skipped.',
        variant: blankEnabled ? 'destructive' : 'secondary',
      },
      {
        label: 'Freeze detection',
        value: freezeEnabled ? (selectedStreamChecking.treat_freeze_as_dead ? 'Dead' : 'Detect') : 'Off',
        description: freezeEnabled
          ? (selectedStreamChecking.treat_freeze_as_dead
            ? 'Frozen video is treated as dead.'
            : 'Frozen video is reported only.')
          : 'Freeze checks are skipped.',
        variant: freezeEnabled ? 'destructive' : 'secondary',
      },
      {
        label: 'Loop check',
        value: selectedStreamChecking.loop_check_enabled ? 'On' : 'Off',
        description: selectedStreamChecking.loop_check_enabled
          ? 'Looping content is checked during preflight.'
          : 'Loop detection is skipped.',
        variant: selectedStreamChecking.loop_check_enabled ? 'destructive' : 'secondary',
      },
      {
        label: 'Visibility',
        value: visibilityEnabled ? 'Profile' : 'Off',
        description: visibilityEnabled
          ? (visibilityHideRulesEnabled
            ? 'Profile-specific hide/recover rules apply.'
            : 'Profile can recover managed channels without hide rules.')
          : 'This profile does not hide or unhide channels.',
        variant: visibilityEnabled && visibilityHideRulesEnabled ? 'destructive' : 'secondary',
      },
    ]
  }, [selectedStreamChecking, selectedVisibility])

  const hydrateInputs = (nextConfig) => {
    setRetryOffsets((nextConfig.retry_offsets_minutes || []).join(', '))
    setPostStartOffsets((nextConfig.post_start_offsets_minutes || []).join(', '))
    setIncludeSports((nextConfig.include_sports || []).join(', '))
    setExcludeSports((nextConfig.exclude_sports || []).join(', '))
    setIncludeLeagues((nextConfig.include_leagues || []).join(', '))
    setExcludeLeagues((nextConfig.exclude_leagues || []).join(', '))
  }

  const loadData = async () => {
    try {
      const configResponse = await teamarrPreflightAPI.getConfig()
      const [statusResponse, profilesResponse] = await Promise.all([
        teamarrPreflightAPI.getStatus(),
        automationAPI.getProfiles(),
      ])
      const nextConfig = configResponse.data || {}
      setConfig(nextConfig)
      setEditedConfig(nextConfig)
      hydrateInputs(nextConfig)
      setStatus(statusResponse.data || {})
      setProfiles(normalizeProfiles(profilesResponse.data))
    } catch (err) {
      console.error('Failed to load Teamarr preflight data:', err)
      toast({
        title: 'Error',
        description: 'Failed to load Teamarr preflight status',
        variant: 'destructive',
      })
    } finally {
      setLoading(false)
    }
  }

  const loadStatus = async () => {
    try {
      const response = await teamarrPreflightAPI.getStatus()
      setStatus(response.data || {})
    } catch (err) {
      console.error('Failed to load Teamarr preflight status:', err)
    }
  }

  const updateConfigValue = (field, value) => {
    setEditedConfig(prev => ({
      ...(prev || {}),
      [field]: value,
    }))
  }

  const updateSelectedProfile = (value) => {
    const nextProfileId = value === DEFAULT_PROFILE_SELECT_VALUE
      ? (editedConfig?.default_profile_id || '')
      : value
    updateConfigValue('forced_profile_id', nextProfileId)
  }

  const saveConfig = async (extra = {}) => {
    try {
      setActionLoading('save')
      const payload = {
        ...(editedConfig || {}),
        retry_offsets_minutes: parseCsv(retryOffsets).map(item => Number(item)).filter(Number.isFinite),
        post_start_offsets_minutes: parseCsv(postStartOffsets).map(item => Number(item)).filter(Number.isFinite),
        include_sports: parseFilterCsv(includeSports),
        exclude_sports: parseFilterCsv(excludeSports),
        include_leagues: parseFilterCsv(includeLeagues),
        exclude_leagues: parseFilterCsv(excludeLeagues),
        ...extra,
      }
      const response = await teamarrPreflightAPI.updateConfig(payload)
      const nextConfig = response.data || {}
      setConfig(nextConfig)
      setEditedConfig(nextConfig)
      hydrateInputs(nextConfig)
      await loadStatus()
      toast({ title: 'Saved', description: 'Teamarr preflight configuration updated' })
    } catch (err) {
      toast({
        title: 'Error',
        description: err.response?.data?.error || 'Failed to save Teamarr preflight configuration',
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
        description: err.response?.data?.error || 'Teamarr preflight action failed',
        variant: 'destructive',
      })
    } finally {
      setActionLoading('')
    }
  }

  const forceEventCheck = async (event) => {
    if (!event) return
    try {
      setActionLoading(`force:${event.identity}`)
      const response = await teamarrPreflightAPI.forceEventCheck(event.identity)
      await loadData()
      toast({
        title: response.data?.launched ? 'Started' : 'Requested',
        description: response.data?.launched
          ? 'Manual preflight check started'
          : 'Manual preflight check request was recorded',
      })
    } catch (err) {
      toast({
        title: 'Error',
        description: err.response?.data?.error || 'Manual event check failed',
        variant: 'destructive',
      })
    } finally {
      setActionLoading('')
    }
  }

  if (loading || !editedConfig) {
    return (
      <div className="flex h-64 items-center justify-center">
        <Loader2 className="h-8 w-8 animate-spin text-muted-foreground" />
      </div>
    )
  }

  const running = Boolean(status?.running)
  const enabled = Boolean(editedConfig?.enabled)
  const renderFilterDropdown = (label, value, setValue, options, emptyLabel = 'Any') => {
    const selected = new Set(parseFilterCsv(value))
    const selectedValues = [...selected]
    const buttonLabel = selectedValues.length === 0
      ? emptyLabel
      : `${selectedValues.length} selected`

    return (
      <div className="space-y-2">
        <Label>{label}</Label>
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <Button type="button" variant="outline" className="h-10 w-full justify-between font-normal">
              <span className="truncate">{buttonLabel}</span>
              <ChevronDown className="ml-2 h-4 w-4 shrink-0 opacity-50" />
            </Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="start" className="max-h-72 w-[--radix-dropdown-menu-trigger-width] overflow-y-auto">
            {options.length === 0 ? (
              <DropdownMenuItem disabled>No options</DropdownMenuItem>
            ) : (
              options.map(option => (
                <DropdownMenuCheckboxItem
                  key={option.value}
                  checked={selected.has(option.value)}
                  onCheckedChange={() => setValue(toggleFilterCsvTerm(value, option.value))}
                  onSelect={(event) => event.preventDefault()}
                >
                  <span className="truncate">{option.label}</span>
                </DropdownMenuCheckboxItem>
              ))
            )}
          </DropdownMenuContent>
        </DropdownMenu>
        {selectedValues.length > 0 ? (
          <div className="flex flex-wrap gap-2">
            {selectedValues.map(optionValue => (
              <Button
                key={optionValue}
                type="button"
                size="sm"
                variant="outline"
                className="h-8 max-w-full px-2 text-xs"
                onClick={() => setValue(toggleFilterCsvTerm(value, optionValue))}
              >
                <span className="truncate">{filterLabelForValue(optionValue, options)}</span>
              </Button>
            ))}
          </div>
        ) : null}
      </div>
    )
  }

  return (
    <div className="space-y-6">
      <div className="flex flex-col gap-4 md:flex-row md:items-start md:justify-between">
        <div>
          <h1 className="text-3xl font-bold tracking-tight">Teamarr Preflight</h1>
          <p className="text-muted-foreground">Managed event and static team quality checks</p>
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
              onClick={() => runAction('stop', teamarrPreflightAPI.stop, 'Teamarr preflight stopped')}
              disabled={actionLoading !== ''}
            >
              {actionLoading === 'stop' ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : <StopCircle className="mr-2 h-4 w-4" />}
              Stop
            </Button>
          ) : (
            <Button
              variant="outline"
              onClick={() => runAction('start', teamarrPreflightAPI.start, 'Teamarr preflight started')}
              disabled={actionLoading !== ''}
            >
              {actionLoading === 'start' ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : <PlayCircle className="mr-2 h-4 w-4" />}
              Start
            </Button>
          )}
          <Button
            variant="outline"
            onClick={() => runAction('scan', teamarrPreflightAPI.runOnce, 'Teamarr preflight scan completed')}
            disabled={actionLoading !== ''}
          >
            {actionLoading === 'scan' ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : <RefreshCw className="mr-2 h-4 w-4" />}
            Scan Now
          </Button>
        </div>
      </div>

      <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-7">
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
            <CardTitle className="text-sm font-medium">Teamarr</CardTitle>
            <ConnectorIcon className="h-4 w-4 text-muted-foreground" />
          </CardHeader>
          <CardContent>
            <Badge variant={connectorDisplay.variant} className={connectorDisplay.className}>
              {connectorDisplay.label}
            </Badge>
            <p className="mt-2 line-clamp-2 text-xs text-muted-foreground">{connectorDisplay.detail}</p>
          </CardContent>
        </Card>

        <Card>
          <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
            <CardTitle className="text-sm font-medium">Upcoming Items</CardTitle>
            <CalendarCheck className="h-4 w-4 text-muted-foreground" />
          </CardHeader>
          <CardContent>
            <div className="text-2xl font-bold">{upcomingViewCount}</div>
            <p className="mt-2 text-xs text-muted-foreground">
              {nextEvent ? formatOffset(nextEvent.seconds_to_start) : 'No upcoming'}
            </p>
            <p className="mt-1 text-xs text-muted-foreground">
              {allManagedEventsCount} events, {allStaticTeamsCount} teams{pastEventsCount > 0 ? `, ${pastEventsCount} past` : ''}
            </p>
          </CardContent>
        </Card>

        <Card>
          <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
            <CardTitle className="text-sm font-medium">Static Teams</CardTitle>
            <ListChecks className="h-4 w-4 text-muted-foreground" />
          </CardHeader>
          <CardContent>
            <div className="text-2xl font-bold">{Number(teamStatus.ready || 0)}</div>
            <p className="mt-2 text-xs text-muted-foreground">
              {teamStatus.enabled ? `${Number(teamStatus.seen || 0)} seen, ${Number(teamStatus.queueable || 0)} queueable` : 'Disabled'}
            </p>
            <p className="mt-1 line-clamp-1 text-xs text-muted-foreground">
              {teamStatus.last_error || `${Number(teamStatus.incomplete || 0)} incomplete`}
            </p>
          </CardContent>
        </Card>

        <Card>
          <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
            <CardTitle className="text-sm font-medium">Active Checks</CardTitle>
            <CheckCircle2 className="h-4 w-4 text-muted-foreground" />
          </CardHeader>
          <CardContent>
            <div className="text-2xl font-bold">{activeChecksCount}</div>
            <p className="mt-2 text-xs text-muted-foreground">{activeChecksDetail}</p>
          </CardContent>
        </Card>

        <Card>
          <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
            <CardTitle className="text-sm font-medium">Queued Checks</CardTitle>
            <ListChecks className="h-4 w-4 text-muted-foreground" />
          </CardHeader>
          <CardContent>
            <div className="text-2xl font-bold">{queuedChecksCount}</div>
            <p className="mt-2 text-xs text-muted-foreground">{queuedChecksDetail}</p>
          </CardContent>
        </Card>

        <Card>
          <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
            <CardTitle className="text-sm font-medium">Last Scan</CardTitle>
            <Clock className="h-4 w-4 text-muted-foreground" />
          </CardHeader>
          <CardContent>
            <div className="text-2xl font-bold">{formatTimestamp(status?.last_scan_at)}</div>
            <p className="mt-2 text-xs text-muted-foreground">{status?.last_error || 'No errors'}</p>
          </CardContent>
        </Card>
      </div>

      <div className="grid gap-6 xl:grid-cols-[minmax(0,1.05fr)_minmax(0,0.95fr)]">
        <div className="space-y-6">
          <Card>
            <CardHeader>
              <CardTitle>Active Preflight Checks</CardTitle>
              <CardDescription>
                {eventSearch.trim()
                  ? `${filteredActiveChecks.length} of ${sourceActiveChecksCount} running checks`
                  : `${sourceActiveChecksCount} running checks`}
              </CardDescription>
            </CardHeader>
            <CardContent>
              {sourceActiveChecksCount === 0 ? (
                <p className="text-sm text-muted-foreground">No active preflight checks</p>
              ) : filteredActiveChecks.length === 0 ? (
                <p className="text-sm text-muted-foreground">No active checks match this search</p>
              ) : (
                <div className="space-y-3">
                  {displayedActiveChecks.map((check, index) => (
                    <div key={`${check.identity || check.dispatcharr_channel_id || index}-${check.bucket || 'active'}`} className="rounded-md border border-border p-3">
                      <div className="flex flex-wrap items-start justify-between gap-2">
                        <div className="min-w-0">
                          <p className="truncate font-medium">{preflightItemTitle(check)}</p>
                          <p className="text-sm text-muted-foreground">{preflightItemChannel(check)}</p>
                        </div>
                        <div className="flex shrink-0 flex-wrap justify-end gap-1.5">
                          {check.run_source === 'queue' ? <Badge variant="outline">Queued Runner</Badge> : null}
                          <Badge variant="outline">{preflightKindShortLabel(check)}</Badge>
                          <Badge variant="secondary">{check.bucket || check.trigger_bucket || 'manual'}</Badge>
                        </div>
                      </div>
                      <p className="mt-2 text-xs text-muted-foreground">
                        {check.started_at
                          ? `Started ${formatTimestamp(check.started_at)} - running ${formatElapsedSince(check.started_at)}`
                          : 'Running from stream-checker queue'}
                      </p>
                    </div>
                  ))}
                  {filteredActiveChecks.length > displayedActiveChecks.length ? (
                    <p className="text-xs text-muted-foreground">
                      Showing {displayedActiveChecks.length} of {filteredActiveChecks.length} matching active checks
                    </p>
                  ) : null}
                </div>
              )}
            </CardContent>
          </Card>

          <Card>
          <CardHeader>
            <CardTitle>Configuration</CardTitle>
            <CardDescription>Connector, timing, profile, and filters</CardDescription>
          </CardHeader>
          <CardContent className="space-y-6">
            <div className="grid gap-4 lg:grid-cols-2">
              <div className="flex min-h-[116px] items-start justify-between gap-5 rounded-md border border-border p-4">
                <div className="min-w-0 space-y-1">
                  <Label className="text-base">Enabled</Label>
                  <p className="max-w-[22rem] text-sm leading-snug text-muted-foreground">Auto-starts with the backend</p>
                </div>
                <Switch className="mt-1 shrink-0" checked={enabled} onCheckedChange={(value) => updateConfigValue('enabled', value)} />
              </div>
              <div className="flex min-h-[116px] items-start justify-between gap-5 rounded-md border border-border p-4">
                <div className="min-w-0 space-y-1">
                  <Label className="text-base">Managed Events</Label>
                  <p className="max-w-[28rem] text-sm leading-snug text-muted-foreground">Reads Teamarr managed event channels and queues targeted event checks</p>
                </div>
                <Switch
                  className="mt-1 shrink-0"
                  checked={editedConfig.managed_event_preflight_enabled !== false}
                  onCheckedChange={(value) => updateConfigValue('managed_event_preflight_enabled', value)}
                />
              </div>
              <div className="flex min-h-[116px] items-start justify-between gap-5 rounded-md border border-border p-4">
                <div className="min-w-0 space-y-1">
                  <Label className="text-base">Static Teams</Label>
                  <p className="max-w-[28rem] text-sm leading-snug text-muted-foreground">Reads active Teamarr teams and queues only ready single-channel team checks</p>
                </div>
                <Switch
                  className="mt-1 shrink-0"
                  checked={editedConfig.static_team_preflight_enabled === true}
                  onCheckedChange={(value) => updateConfigValue('static_team_preflight_enabled', value)}
                />
              </div>
              <div className="flex min-h-[116px] items-start justify-between gap-5 rounded-md border border-border p-4">
                <div className="min-w-0 space-y-1">
                  <Label className="text-base">Queue Events During Active Checks</Label>
                  <p className="max-w-[28rem] text-sm leading-snug text-muted-foreground">
                    On queues due Teamarr event checks during Automation or Stream Checker runs; off waits and does not queue new event checks until active work is done
                  </p>
                </div>
                <Switch
                  className="mt-1 shrink-0"
                  checked={Boolean(editedConfig.queue_during_active_checks ?? !(editedConfig.defer_during_active_checks ?? editedConfig.skip_during_quality_check))}
                  onCheckedChange={(value) => updateConfigValue('queue_during_active_checks', value)}
                />
              </div>
            </div>

            <div className="grid gap-4 md:grid-cols-2">
              <div className="space-y-2 md:col-span-2">
                <Label>Teamarr Base URL</Label>
                <Input
                  value={editedConfig.teamarr_base_url || ''}
                  onChange={(event) => updateConfigValue('teamarr_base_url', event.target.value)}
                  placeholder="http://teamarr:9195"
                />
                <p className="text-xs text-muted-foreground">
                  StreamFlow reads Teamarr internal managed-event endpoints. No separate official Teamarr API key is required.
                </p>
              </div>
              <div className="space-y-2 md:col-span-2">
                <Label>Quality Profile</Label>
                <Select
                  value={selectedProfileValue}
                  onValueChange={updateSelectedProfile}
                >
                  <SelectTrigger>
                    <SelectValue placeholder="Select profile" />
                  </SelectTrigger>
                  <SelectContent>
                    {profileOptions.length === 0 ? (
                      <SelectItem value={DEFAULT_PROFILE_SELECT_VALUE}>Teamarr Event Preflight</SelectItem>
                    ) : (
                      profileOptions.map(profile => (
                        <SelectItem key={profile.id} value={profile.id}>
                          {profile.name || `Profile ${profile.id}`}
                        </SelectItem>
                      ))
                    )}
                  </SelectContent>
                </Select>
                <p className="text-xs text-muted-foreground">
                  Create or edit profiles in Automation Settings; Save stores the selection here.
                </p>
                {qualityRuleSummary.length > 0 && (
                  <div className="grid gap-2 pt-2 xl:grid-cols-2">
                    {qualityRuleSummary.map(rule => (
                      <div key={rule.label} className="flex min-h-[72px] items-start justify-between gap-3 rounded-md border px-3 py-2.5">
                        <div className="min-w-0 space-y-1">
                          <span className="block text-sm font-medium leading-snug text-foreground">{rule.label}</span>
                          <span className="block text-xs leading-snug text-muted-foreground">{rule.description}</span>
                        </div>
                        <Badge variant={rule.variant} className="shrink-0 whitespace-nowrap text-[10px]">
                          {rule.value}
                        </Badge>
                      </div>
                    ))}
                  </div>
                )}
              </div>
            </div>

            <Separator />

            <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
              {numberFields.map(field => (
                <div key={field.key} className="space-y-2">
                  <Label>{field.label}</Label>
                  <div className="flex items-center gap-2">
                    <Input
                      type="number"
                      min={field.min}
                      max={field.max}
                      value={editedConfig[field.key] ?? ''}
                      onChange={(event) => updateConfigValue(field.key, Number(event.target.value))}
                    />
                    <span className="w-14 text-sm text-muted-foreground">{field.suffix}</span>
                  </div>
                  <p className="text-xs leading-snug text-muted-foreground">{field.description}</p>
                </div>
              ))}
              <div className="space-y-2">
                <Label>Pre-Start Retries</Label>
                <Input value={retryOffsets} onChange={(event) => setRetryOffsets(event.target.value)} />
                <p className="text-xs leading-snug text-muted-foreground">
                  One or more minutes before start, for example 3 or 10, 3. Each value is one extra check bucket and should be less than or equal to Preflight Offset.
                </p>
              </div>
              <div className="space-y-2">
                <Label>Post-Start Checks</Label>
                <Input value={postStartOffsets} onChange={(event) => setPostStartOffsets(event.target.value)} />
                <p className="text-xs leading-snug text-muted-foreground">
                  One or more minutes after start, for example 2 or 2, 4. These only run while the event is still inside Post Start Grace.
                </p>
              </div>
            </div>

            <div className="rounded-md border border-border p-3">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <div>
                  <p className="text-sm font-medium">Timing Preview</p>
                  <p className="text-xs text-muted-foreground">
                    {nextEvent
                      ? `${nextEvent.event_name || 'Next event'} at ${formatDateTime(nextEvent.event_date)}`
                      : 'Example event using the current timing fields'}
                  </p>
                </div>
                <Badge variant="outline" className="text-[10px]">
                  {timingPreview.items.filter(item => !item.disabled).length} automatic buckets
                </Badge>
              </div>
              {timingPreview.items.length > 0 ? (
                <div className="mt-3 grid gap-2 sm:grid-cols-2 xl:grid-cols-3">
                  {timingPreview.items.map(item => (
                    <div
                      key={`${item.bucket}-${item.timestamp}`}
                      className={`rounded-md border px-2.5 py-2 text-xs ${
                        item.disabled ? 'border-dashed text-muted-foreground opacity-70' : 'text-foreground'
                      }`}
                    >
                      <div className="flex items-center justify-between gap-2">
                        <span className="font-medium">{item.bucket}</span>
                        <Badge variant={item.disabled ? 'secondary' : 'outline'} className="text-[10px]">
                          {item.label}
                        </Badge>
                      </div>
                      <p className="mt-1 text-muted-foreground">{formatDateTime(item.timestamp)}</p>
                    </div>
                  ))}
                </div>
              ) : (
                <p className="mt-3 text-xs text-muted-foreground">No preview is available until a valid event date exists.</p>
              )}
              {timingPreview.warnings.length > 0 ? (
                <div className="mt-3 space-y-1">
                  {timingPreview.warnings.map(warning => (
                    <div key={warning.code} className="flex items-start gap-2 text-xs text-amber-600 dark:text-amber-300">
                      <AlertCircle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
                      <span>{warning.text}</span>
                    </div>
                  ))}
                </div>
              ) : null}
            </div>

            <Separator />

            <div className="grid gap-4 md:grid-cols-2">
              {renderFilterDropdown('Exclude Sports', excludeSports, setExcludeSports, filterOptions.sports, 'None')}
              {renderFilterDropdown('Exclude Leagues', excludeLeagues, setExcludeLeagues, filterOptions.leagues, 'None')}
            </div>

            <Accordion
              type="multiple"
              defaultValue={parseFilterCsv(includeSports).length || parseFilterCsv(includeLeagues).length ? ['include-filters'] : []}
              className="rounded-md border border-border px-3"
            >
              <AccordionItem value="operational-notes">
                <AccordionTrigger className="py-3 text-sm hover:no-underline">
                  Operational Notes
                </AccordionTrigger>
                <AccordionContent>
                  <div className="grid gap-3 pb-3 text-sm text-muted-foreground md:grid-cols-2 xl:grid-cols-4">
                    <div>
                      <p className="font-medium text-foreground">Queue Events During Active Checks</p>
                      <p>When on, due Teamarr event checks enter the server-side priority queue during Automation or Stream Checker runs. When off, new event checks are not queued until active work is done.</p>
                    </div>
                    <div>
                      <p className="font-medium text-foreground">Timing Fields</p>
                      <p>Offset fields are check times, not continuous monitoring. Poll interval controls how often Teamarr is read.</p>
                    </div>
                    <div>
                      <p className="font-medium text-foreground">Post-Start Checks</p>
                      <p>Use one post-start offset such as 2 minutes, or multiple offsets such as 2 and 4 minutes, when event channels appear at kickoff or shortly after.</p>
                    </div>
                    <div>
                      <p className="font-medium text-foreground">Event Status</p>
                      <p>Managed Events show both schedule state and the latest preflight result, so completed or deferred checks are visible in the event list.</p>
                    </div>
                    <div>
                      <p className="font-medium text-foreground">Manual Checks</p>
                      <p>Past events can still be checked manually. The selected profile controls the check rules used for that run.</p>
                    </div>
                  </div>
                </AccordionContent>
              </AccordionItem>
              <AccordionItem value="include-filters" className="border-b-0">
                <AccordionTrigger className="py-3 text-sm hover:no-underline">
                  Advanced Include Filters
                </AccordionTrigger>
                <AccordionContent>
                  <div className="grid gap-4 md:grid-cols-2">
                    {renderFilterDropdown('Include Sports', includeSports, setIncludeSports, filterOptions.sports, 'All Teamarr')}
                    {renderFilterDropdown('Include Leagues', includeLeagues, setIncludeLeagues, filterOptions.leagues, 'All Teamarr')}
                  </div>
                </AccordionContent>
              </AccordionItem>
            </Accordion>
          </CardContent>
          </Card>
        </div>

        <div className="space-y-6">
          <div className="grid gap-3 sm:grid-cols-[minmax(0,1fr)_10rem_12rem]">
            <div className="relative">
              <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
              <Input
                value={eventSearch}
                onChange={(event) => setEventSearch(event.target.value)}
                className="pl-9"
                placeholder="Search preflight items"
              />
            </div>
            <Select value={sourceView} onValueChange={setSourceView}>
              <SelectTrigger>
                <SelectValue placeholder="Source" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="all">All</SelectItem>
                <SelectItem value="events">Events</SelectItem>
                <SelectItem value="teams">Teams</SelectItem>
              </SelectContent>
            </Select>
            <Select value={managedEventView} onValueChange={setManagedEventView}>
              <SelectTrigger>
                <SelectValue placeholder="Event view" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="upcoming">Upcoming</SelectItem>
                <SelectItem value="due">Due</SelectItem>
                <SelectItem value="no_check">No Check</SelectItem>
                <SelectItem value="past">Past</SelectItem>
                <SelectItem value="all">All</SelectItem>
              </SelectContent>
            </Select>
          </div>

          <Card>
            <CardHeader>
              <CardTitle>Preflight Items</CardTitle>
              <CardDescription>
                {managedEventSummary}
                {managedEventsSeen !== managedCandidates ? ` from ${managedEventsSeen} managed records` : ''}
              </CardDescription>
            </CardHeader>
            <CardContent>
              {preflightItems.length === 0 ? (
                <p className="text-sm text-muted-foreground">No preflight items found</p>
              ) : filteredUpcomingEvents.length === 0 ? (
                <p className="text-sm text-muted-foreground">No preflight items match this search</p>
              ) : (
                <TooltipProvider delayDuration={200}>
                  <div className="space-y-3">
                  {displayedUpcomingEvents.map(event => {
                    const lastPreflightEvent = event.last_preflight_event || null
                    const lastPreflightDetails = recentEventDetailParts(lastPreflightEvent)
                    const checkSummary = eventCheckSummary(event, lastPreflightEvent)
                    const automaticCheckSummary = eventAutomaticCheckSummary(event, status?.config || config || editedConfig || {})
                    const eventDiagnostics = eventScheduleDiagnosticParts(event, automaticCheckSummary, status?.config || config || editedConfig || {})
                    const healthAlert = getTeamarrEventHealthAlert(event, lastPreflightEvent, automaticCheckSummary)
                    return (
                      <div key={`${event.identity}-${event.trigger_bucket || 'none'}`} className="rounded-md border border-border p-3">
                        <div className="flex flex-wrap items-start justify-between gap-2">
                          <div className="min-w-0">
                            <p className="truncate font-medium">{preflightItemTitle(event)}</p>
                            <p className="text-sm text-muted-foreground">{event.event_date ? formatDateTime(event.event_date) : 'No live window'}</p>
                          </div>
                          <div className="flex shrink-0 items-center gap-2">
                            <Badge variant="outline">
                              {preflightKindShortLabel(event)}
                            </Badge>
                            <Badge variant={event.state === 'due' ? 'default' : 'secondary'}>
                              {stateLabel(event.state)}
                            </Badge>
                            {lastPreflightEvent ? (
                              <Badge variant={recentEventBadgeVariant(lastPreflightEvent.type)}>
                                {eventLabel(lastPreflightEvent.type)}
                              </Badge>
                            ) : (
                              <Badge variant="outline">No Check</Badge>
                            )}
                            <Tooltip>
                              <TooltipTrigger asChild>
                                <span className="inline-flex">
                                  <Button
                                    type="button"
                                    variant="outline"
                                    size="sm"
                                    className="h-8 gap-1.5 px-2"
                                    disabled={!canForceEvent(event) || actionLoading !== ''}
                                    onClick={() => setForceEvent(event)}
                                    aria-label={`Run preflight check for ${preflightItemTitle(event)}`}
                                  >
                                    {actionLoading === `force:${event.identity}` ? (
                                      <Loader2 className="h-4 w-4 animate-spin" />
                                    ) : (
                                      <PlayCircle className="h-4 w-4" />
                                    )}
                                    <span className="whitespace-nowrap text-xs font-medium">Force Check</span>
                                  </Button>
                                </span>
                              </TooltipTrigger>
                              <TooltipContent>{forceEventTooltip(event)}</TooltipContent>
                            </Tooltip>
                          </div>
                        </div>
                        <div className="mt-3 grid gap-2 text-sm text-muted-foreground sm:grid-cols-3">
                          <span>{preflightItemChannel(event)}</span>
                          <span>{event.sport || 'Sport N/A'}</span>
                          <span>{event.league || 'League N/A'}</span>
                        </div>
                        <div className="mt-3 flex flex-wrap items-center gap-1.5">
                          {automaticCheckSummary ? (
                            <span className="basis-full inline-flex items-center gap-1 text-xs text-muted-foreground">
                              <Clock className="h-3.5 w-3.5" />
                              {automaticCheckSummary}
                            </span>
                          ) : null}
                          <span className="mr-1 text-xs text-muted-foreground">
                            {lastPreflightEvent
                              ? `${checkSummary} at ${formatTimestamp(lastPreflightEvent.timestamp)}`
                              : checkSummary}
                          </span>
                          {lastPreflightEvent ? (
                            <>
                            {lastPreflightDetails.map(part => (
                              <Badge key={part} variant="outline" className="text-[10px] font-medium text-muted-foreground">
                                {part}
                              </Badge>
                            ))}
                            </>
                          ) : null}
                        </div>
                        {healthAlert ? (
                          <div
                            className={`mt-3 flex items-start gap-2 rounded-md border px-3 py-2 text-sm ${
                              healthAlert.severity === 'critical'
                                ? 'border-red-500/40 bg-red-500/10 text-red-700 dark:text-red-300'
                                : 'border-amber-500/40 bg-amber-500/10 text-amber-700 dark:text-amber-300'
                            }`}
                          >
                            <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" />
                            <div className="min-w-0">
                              <p className="font-medium">{healthAlert.label}</p>
                              <p className="text-xs leading-snug opacity-90">{healthAlert.detail}</p>
                            </div>
                          </div>
                        ) : null}
                        {eventDiagnostics.length > 0 ? (
                          <div className="mt-3 border-t border-border pt-2">
                            <div className="grid gap-2 text-xs text-muted-foreground sm:grid-cols-2 xl:grid-cols-3">
                              {eventDiagnostics.map(part => (
                                <div key={`${part.label}-${part.value}`} className="min-w-0">
                                  <span className="text-muted-foreground/80">{part.label}: </span>
                                  <span className="text-foreground/90">{part.value}</span>
                                </div>
                              ))}
                            </div>
                          </div>
                        ) : null}
                      </div>
                    )
                  })}
                  {filteredUpcomingEvents.length > MANAGED_EVENT_PAGE_SIZE ? (
                    <div className="flex flex-wrap items-center justify-between gap-2 text-xs text-muted-foreground">
                      <span>
                        Page {safeManagedEventPage} of {managedEventPageCount} - showing {displayedUpcomingEvents.length} of {filteredUpcomingEvents.length} matching preflight items
                      </span>
                      <div className="flex items-center gap-2">
                        <Button
                          type="button"
                          variant="outline"
                          size="sm"
                          className="h-8"
                          disabled={safeManagedEventPage <= 1}
                          onClick={() => setManagedEventPage(page => Math.max(1, page - 1))}
                        >
                          Previous
                        </Button>
                        <Button
                          type="button"
                          variant="outline"
                          size="sm"
                          className="h-8"
                          disabled={safeManagedEventPage >= managedEventPageCount}
                          onClick={() => setManagedEventPage(page => Math.min(managedEventPageCount, page + 1))}
                        >
                          Next
                        </Button>
                      </div>
                    </div>
                  ) : null}
                  {managedEventsTruncated ? (
                    <p className="text-xs text-muted-foreground">
                      Teamarr returned {managedEventsSeen} managed records; StreamFlow kept the first {managedEventsReturned} event records by start time.
                    </p>
                  ) : null}
                  </div>
                </TooltipProvider>
              )}
            </CardContent>
          </Card>

        </div>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>Recent Activity</CardTitle>
          <CardDescription>
            {eventSearch.trim()
              ? `${filteredRecentEvents.length} of ${sourceFilteredRecentEvents.length} latest connector decisions`
              : `${sourceFilteredRecentEvents.length} latest connector decisions`}
          </CardDescription>
        </CardHeader>
        <CardContent>
          {sourceFilteredRecentEvents.length === 0 ? (
            <p className="text-sm text-muted-foreground">No activity recorded</p>
          ) : filteredRecentEvents.length === 0 ? (
            <p className="text-sm text-muted-foreground">No recent activity matches this search</p>
          ) : (
            <div className="space-y-3">
              {displayedRecentEvents.map((event, index) => {
                const detailParts = recentEventDetailParts(event)
                return (
                  <div key={`${event.timestamp}-${index}`} className="grid gap-3 border-b border-border pb-3 last:border-b-0 last:pb-0 sm:grid-cols-[minmax(0,1fr)_4.75rem]">
                    <div className="min-w-0 space-y-2">
                      <div className="flex flex-wrap items-center gap-2">
                        <Badge variant="outline" className="shrink-0">
                          {preflightKindLabel(event)}
                        </Badge>
                        <Badge variant={recentEventBadgeVariant(event.type)} className="shrink-0">
                          {eventLabel(event.type)}
                        </Badge>
                        <p className="min-w-0 truncate font-medium">{preflightItemTitle(event)}</p>
                      </div>
                      <div className="grid gap-1 text-sm text-muted-foreground sm:grid-cols-2">
                        <span className="truncate">{preflightItemChannel(event)}</span>
                        <span className="truncate">{[event.sport, event.league].filter(Boolean).join(' / ') || 'Metadata N/A'}</span>
                      </div>
                      {detailParts.length > 0 ? (
                        <div className="flex flex-wrap gap-1.5">
                          {detailParts.map(part => (
                            <Badge key={part} variant="outline" className="text-[10px] font-medium text-muted-foreground">
                              {part}
                            </Badge>
                          ))}
                        </div>
                      ) : null}
                    </div>
                    <span className="shrink-0 justify-self-start text-sm text-muted-foreground sm:justify-self-end">{formatTimestamp(event.timestamp)}</span>
                  </div>
                )
              })}
              {filteredRecentEvents.length > RECENT_EVENT_PAGE_SIZE ? (
                <div className="flex flex-wrap items-center justify-between gap-2 text-xs text-muted-foreground">
                  <span>
                    Page {safeRecentEventPage} of {recentEventPageCount} - showing {displayedRecentEvents.length} of {filteredRecentEvents.length} matching recent events
                  </span>
                  <div className="flex items-center gap-2">
                    <Button
                      type="button"
                      variant="outline"
                      size="sm"
                      className="h-8"
                      disabled={safeRecentEventPage <= 1}
                      onClick={() => setRecentEventPage(page => Math.max(1, page - 1))}
                    >
                      Previous
                    </Button>
                    <Button
                      type="button"
                      variant="outline"
                      size="sm"
                      className="h-8"
                      disabled={safeRecentEventPage >= recentEventPageCount}
                      onClick={() => setRecentEventPage(page => Math.min(recentEventPageCount, page + 1))}
                    >
                      Next
                    </Button>
                  </div>
                </div>
              ) : null}
            </div>
          )}
        </CardContent>
      </Card>

      <AlertDialog open={Boolean(forceEvent)} onOpenChange={(open) => {
        if (!open) setForceEvent(null)
      }}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Run Preflight Check</AlertDialogTitle>
            <AlertDialogDescription>
              Run the Teamarr profile now for {preflightItemTitle(forceEvent)}. Automation,
              Stream Checker activity, concurrency, and stream availability guards still apply.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <AlertDialogAction
              onClick={() => {
                const selectedEvent = forceEvent
                setForceEvent(null)
                forceEventCheck(selectedEvent)
              }}
            >
              Run Check
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  )
}
