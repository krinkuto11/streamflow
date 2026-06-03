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
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select.jsx'
import {
  Activity,
  CalendarCheck,
  ChevronDown,
  CheckCircle2,
  Clock,
  Loader2,
  PlayCircle,
  RefreshCw,
  Save,
  StopCircle,
} from 'lucide-react'

const numberFields = [
  { key: 'poll_interval_seconds', label: 'Poll Interval', suffix: 'sec', min: 15, max: 3600 },
  { key: 'preflight_offset_minutes', label: 'Preflight Offset', suffix: 'min', min: 1, max: 360 },
  { key: 'post_start_grace_minutes', label: 'Post Start Grace', suffix: 'min', min: 0, max: 120 },
  { key: 'max_concurrent_checks', label: 'Concurrent Checks', suffix: 'max', min: 1, max: 10 },
  { key: 'event_cooldown_minutes', label: 'Event Cooldown', suffix: 'min', min: 1, max: 10080 },
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
  waiting_for_channel_sync: 'Syncing',
  filtered: 'Filtered',
  past: 'Past',
}

const DEFAULT_PROFILE_SELECT_VALUE = '__teamarr_default_profile__'

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

const eventLabel = (type) => eventLabels[type] || type || 'Unknown'
const stateLabel = (state) => stateLabels[state] || state || 'Unknown'
const forceableStates = new Set(['due', 'scheduled', 'already_attempted', 'past'])

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

  if (details.bucket) parts.push(`Bucket ${details.bucket}`)
  if (details.reason) parts.push(reasonLabel(details.reason))
  if (details.error) parts.push(details.error)
  if (stats.total_streams !== undefined) parts.push(`${stats.total_streams} streams`)
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

const forceEventTooltip = (event) => {
  if (!event?.dispatcharr_channel_id) return 'No Dispatcharr channel'
  if (event?.state === 'waiting_for_channel_sync') return 'Channel syncing'
  if (event?.state === 'filtered') return 'Filtered'
  if (event?.state === 'past') return 'Run manual check'
  if (!forceableStates.has(String(event?.state || ''))) return 'Unavailable'
  return 'Run event check'
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
  const [includeSports, setIncludeSports] = useState('')
  const [excludeSports, setExcludeSports] = useState('')
  const [includeLeagues, setIncludeLeagues] = useState('')
  const [excludeLeagues, setExcludeLeagues] = useState('')
  const [profiles, setProfiles] = useState([])
  const [loading, setLoading] = useState(true)
  const [actionLoading, setActionLoading] = useState('')
  const [forceEvent, setForceEvent] = useState(null)
  const { toast } = useToast()

  useEffect(() => {
    loadData()
    const interval = setInterval(loadStatus, 5000)
    return () => clearInterval(interval)
  }, [])

  const upcomingEvents = status?.upcoming_events || []
  const recentEvents = status?.recent_events || []
  const activeChecks = status?.active_checks || []
  const nextEvent = useMemo(() => upcomingEvents.find(event => event.state !== 'past') || null, [upcomingEvents])
  const eventFilterOptions = useMemo(
    () => collectTeamarrFilterOptions([...upcomingEvents, ...recentEvents]),
    [upcomingEvents, recentEvents]
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

  const hydrateInputs = (nextConfig) => {
    setRetryOffsets((nextConfig.retry_offsets_minutes || []).join(', '))
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
          ? 'Manual event check started'
          : 'Manual event check request was recorded',
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
          <p className="text-muted-foreground">Managed event quality checks</p>
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

      <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-4">
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
            <CardTitle className="text-sm font-medium">Upcoming</CardTitle>
            <CalendarCheck className="h-4 w-4 text-muted-foreground" />
          </CardHeader>
          <CardContent>
            <div className="text-2xl font-bold">{upcomingEvents.length}</div>
            <p className="mt-2 text-xs text-muted-foreground">
              {nextEvent ? formatOffset(nextEvent.seconds_to_start) : 'No events'}
            </p>
          </CardContent>
        </Card>

        <Card>
          <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
            <CardTitle className="text-sm font-medium">Active Checks</CardTitle>
            <CheckCircle2 className="h-4 w-4 text-muted-foreground" />
          </CardHeader>
          <CardContent>
            <div className="text-2xl font-bold">{activeChecks.length}</div>
            <p className="mt-2 text-xs text-muted-foreground">Limit {editedConfig.max_concurrent_checks}</p>
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
        <Card>
          <CardHeader>
            <CardTitle>Configuration</CardTitle>
            <CardDescription>Connector, timing, profile, and filters</CardDescription>
          </CardHeader>
          <CardContent className="space-y-6">
            <div className="grid gap-4 md:grid-cols-2">
              <div className="flex items-center justify-between rounded-md border border-border p-4">
                <div>
                  <Label className="text-base">Enabled</Label>
                  <p className="text-sm text-muted-foreground">Auto-starts with the backend</p>
                </div>
                <Switch checked={enabled} onCheckedChange={(value) => updateConfigValue('enabled', value)} />
              </div>
              <div className="flex items-center justify-between rounded-md border border-border p-4">
                <div>
                  <Label className="text-base">Busy Handling</Label>
                  <p className="text-sm text-muted-foreground">Defers during automation; queues Stream Checker conflicts so events still get checked</p>
                </div>
                <Switch
                  checked={Boolean(editedConfig.skip_during_quality_check)}
                  onCheckedChange={(value) => updateConfigValue('skip_during_quality_check', value)}
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
              </div>
              <div className="space-y-2">
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
                </div>
              ))}
              <div className="space-y-2">
                <Label>Retry Offsets</Label>
                <Input value={retryOffsets} onChange={(event) => setRetryOffsets(event.target.value)} />
              </div>
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
                  <div className="grid gap-3 pb-3 text-sm text-muted-foreground md:grid-cols-3">
                    <div>
                      <p className="font-medium text-foreground">Busy Handling</p>
                      <p>Automation phases defer event checks; Stream Checker conflicts enter the server-side priority queue and continue after the current channel.</p>
                    </div>
                    <div>
                      <p className="font-medium text-foreground">Event Status</p>
                      <p>Managed Events show both schedule state and the latest preflight result, so completed or deferred checks are visible in the event list.</p>
                    </div>
                    <div>
                      <p className="font-medium text-foreground">Manual Checks</p>
                      <p>Past events can still be checked manually. The selected profile controls the check rules; Teamarr priority is not a user setting.</p>
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

        <div className="space-y-6">
          <Card>
            <CardHeader>
              <CardTitle>Managed Events</CardTitle>
              <CardDescription>Upcoming Teamarr event channels</CardDescription>
            </CardHeader>
            <CardContent>
              {upcomingEvents.length === 0 ? (
                <p className="text-sm text-muted-foreground">No events found</p>
              ) : (
                <TooltipProvider delayDuration={200}>
                  <div className="space-y-3">
                  {upcomingEvents.slice(0, 12).map(event => {
                    const lastPreflightEvent = event.last_preflight_event || null
                    const lastPreflightDetails = recentEventDetailParts(lastPreflightEvent)
                    const checkSummary = eventCheckSummary(event, lastPreflightEvent)
                    return (
                      <div key={`${event.identity}-${event.trigger_bucket || 'none'}`} className="rounded-md border border-border p-3">
                        <div className="flex flex-wrap items-start justify-between gap-2">
                          <div className="min-w-0">
                            <p className="truncate font-medium">{event.event_name}</p>
                            <p className="text-sm text-muted-foreground">{formatDateTime(event.event_date)}</p>
                          </div>
                          <div className="flex shrink-0 items-center gap-2">
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
                                    size="icon"
                                    className="h-8 w-8"
                                    disabled={!canForceEvent(event) || actionLoading !== ''}
                                    onClick={() => setForceEvent(event)}
                                    aria-label={`Run event check for ${event.event_name || 'managed event'}`}
                                  >
                                    {actionLoading === `force:${event.identity}` ? (
                                      <Loader2 className="h-4 w-4 animate-spin" />
                                    ) : (
                                      <PlayCircle className="h-4 w-4" />
                                    )}
                                  </Button>
                                </span>
                              </TooltipTrigger>
                              <TooltipContent>{forceEventTooltip(event)}</TooltipContent>
                            </Tooltip>
                          </div>
                        </div>
                        <div className="mt-3 grid gap-2 text-sm text-muted-foreground sm:grid-cols-3">
                          <span>{event.channel_name || `Channel ${event.dispatcharr_channel_id || 'N/A'}`}</span>
                          <span>{event.sport || 'Sport N/A'}</span>
                          <span>{event.league || 'League N/A'}</span>
                        </div>
                        <div className="mt-3 flex flex-wrap items-center gap-1.5">
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
                      </div>
                    )
                  })}
                  </div>
                </TooltipProvider>
              )}
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle>Recent Events</CardTitle>
              <CardDescription>Latest connector decisions</CardDescription>
            </CardHeader>
            <CardContent>
              {recentEvents.length === 0 ? (
                <p className="text-sm text-muted-foreground">No events recorded</p>
              ) : (
                <div className="space-y-3">
                  {recentEvents.slice(0, 10).map((event, index) => {
                    const detailParts = recentEventDetailParts(event)
                    return (
                      <div key={`${event.timestamp}-${index}`} className="grid gap-3 border-b border-border pb-3 last:border-b-0 last:pb-0 sm:grid-cols-[minmax(0,1fr)_4.75rem]">
                        <div className="min-w-0 space-y-2">
                          <div className="flex flex-wrap items-center gap-2">
                            <Badge variant={recentEventBadgeVariant(event.type)} className="shrink-0">
                              {eventLabel(event.type)}
                            </Badge>
                            <p className="min-w-0 truncate font-medium">{event.event_name || event.channel_name || 'Managed Event'}</p>
                          </div>
                          <div className="grid gap-1 text-sm text-muted-foreground sm:grid-cols-2">
                            <span className="truncate">{event.channel_name || `Channel ${event.dispatcharr_channel_id || 'N/A'}`}</span>
                            <span className="truncate">{[event.sport, event.league].filter(Boolean).join(' / ') || 'Event metadata N/A'}</span>
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
                </div>
              )}
            </CardContent>
          </Card>
        </div>
      </div>

      <AlertDialog open={Boolean(forceEvent)} onOpenChange={(open) => {
        if (!open) setForceEvent(null)
      }}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Run Event Check</AlertDialogTitle>
            <AlertDialogDescription>
              Run the Teamarr event profile now for {forceEvent?.event_name || 'this managed event'}. Automation,
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
