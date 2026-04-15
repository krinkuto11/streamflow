import { useCallback, useEffect, useRef, useState } from 'react'
import { Button } from '@/components/ui/button.jsx'
import { Checkbox } from '@/components/ui/checkbox.jsx'
import { Badge } from '@/components/ui/badge.jsx'
import { Label } from '@/components/ui/label.jsx'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select.jsx'
import { Separator } from '@/components/ui/separator.jsx'
import { Switch } from '@/components/ui/switch.jsx'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip.jsx'
import { useToast } from '@/hooks/use-toast.js'
import { channelsAPI, automationAPI } from '@/services/api.js'
import { getCachedChannelLogoUrl, setCachedChannelLogoUrl } from '@/services/channelCache.js'
import { Plus, Trash2, Loader2, Eye, ChevronDown, Activity, Calendar, CalendarClock, Edit, Zap } from 'lucide-react'
import {
  REGEX_TABLE_GRID_COLS,
  normalizePatternData,
} from '@/components/channel-configuration/patternUtils.js'
import { AssignPeriodsDialog } from '@/components/channel-configuration/PeriodDialogs.jsx'

// ---------------------------------------------------------------------------
// Helper: format a period's schedule into a human-readable string
// ---------------------------------------------------------------------------
function formatSchedule(schedule) {
  if (!schedule) return 'Unknown schedule'
  if (schedule.type === 'interval') {
    const mins = Number(schedule.value)
    if (mins >= 60 && mins % 60 === 0) return `Every ${mins / 60}h`
    return `Every ${mins}m`
  }
  if (schedule.type === 'cron') return `Cron: ${schedule.value}`
  return String(schedule.value ?? '')
}

// ---------------------------------------------------------------------------
// ChannelGroupTooltip — commented out; cell now shows the same data inline.
// Kept in file for reference; uncomment if the inline approach is ever reverted.
// ---------------------------------------------------------------------------
/*
function ChannelGroupTooltip({ children, channel, group, groupsConfig }) {
  const [open, setOpen] = useState(false)
  const [activeProfile, setActiveProfile] = useState(null)
  const [loading, setLoading] = useState(false)
  const fetchedRef = useRef(false)

  const handleOpenChange = useCallback(
    async (nextOpen) => {
      setOpen(nextOpen)
      if (!nextOpen || fetchedRef.current) return
      fetchedRef.current = true
      setLoading(true)
      try {
        const res = await channelsAPI.getChannelActiveProfile(channel.id)
        setActiveProfile(res.data)
      } catch {
        setActiveProfile({ error: true })
      } finally {
        setLoading(false)
      }
    },
    [channel.id]
  )

  const channelGroupId = channel?.group_id ?? channel?.channel_group_id
  const isPeriodChannelOverride =
    channel?.automation_periods_source === 'channel' || Number(channel?.channel_periods_count || 0) > 0
  const isPeriodGroupBased =
    channel?.automation_periods_source === 'group' ||
    (!isPeriodChannelOverride && Number(channel?.group_periods_count || 0) > 0)
  const isEpgChannelOverride = Boolean(channel?.channel_epg_scheduled_profile_id)
  const isEpgGroupBased = !isEpgChannelOverride && Boolean(groupsConfig?.[channelGroupId]?.epg_profile_id)

  const automation = activeProfile?.automation
  const epgOverride = activeProfile?.epg_override

  return (
    <Tooltip open={open} onOpenChange={handleOpenChange}>
      <TooltipTrigger asChild>{children}</TooltipTrigger>
      <TooltipContent className="max-w-[280px] p-3 space-y-2">
        <p className="font-semibold text-sm">{group?.name || 'Ungrouped'}</p>
        <Separator className="my-1" />
        {loading && (
          <div className="flex items-center gap-2 text-xs text-muted-foreground py-1">
            <Loader2 className="h-3 w-3 animate-spin" />
            Resolving active profile…
          </div>
        )}
        <div className="space-y-0.5">
          <p className="text-[11px] font-medium uppercase tracking-wider text-muted-foreground flex items-center gap-1">
            <Zap className="h-3 w-3" />
            Active Automation Profile
          </p>
          {!loading && automation?.profile_name ? (
            <div className="rounded-md bg-muted/50 px-2 py-1.5">
              <p className="text-xs font-medium">{automation.profile_name}</p>
              {automation.period_name && (
                <p className="text-[11px] text-muted-foreground mt-0.5">via period: {automation.period_name}</p>
              )}
              {automation.source && (
                <Badge variant={automation.source === 'channel' ? 'default' : 'outline'} className="text-[10px] h-4 px-1 mt-1">
                  {automation.source === 'channel' ? 'Channel' : 'Group'}
                </Badge>
              )}
            </div>
          ) : !loading ? (
            <p className="text-xs text-muted-foreground italic">
              {activeProfile?.error ? 'Could not resolve' : 'No automation configured'}
            </p>
          ) : null}
        </div>
        <div className="space-y-0.5">
          <p className="text-[11px] font-medium uppercase tracking-wider text-muted-foreground flex items-center gap-1">
            <CalendarClock className="h-3 w-3" />
            EPG Override Profile
          </p>
          {!loading && epgOverride?.profile_name ? (
            <div className="rounded-md bg-muted/50 px-2 py-1.5">
              <p className="text-xs font-medium">{epgOverride.profile_name}</p>
              {epgOverride.source && (
                <Badge variant={epgOverride.source === 'channel' ? 'default' : 'outline'} className="text-[10px] h-4 px-1 mt-1">
                  {epgOverride.source === 'channel' ? 'Channel' : 'Group'}
                </Badge>
              )}
            </div>
          ) : !loading ? (
            <p className="text-xs text-muted-foreground italic">
              {activeProfile?.error ? 'Could not resolve' : 'None'}
            </p>
          ) : null}
        </div>
      </TooltipContent>
    </Tooltip>
  )
}
*/

// ---------------------------------------------------------------------------
// Tooltip: Nº of Periods column — lazy fetch on first hover
// ---------------------------------------------------------------------------
function PeriodsTooltip({ children, channel, profiles }) {
  const [open, setOpen] = useState(false)
  const [periods, setPeriods] = useState(null)
  const [loading, setLoading] = useState(false)
  const fetchedRef = useRef(false)

  const handleOpenChange = useCallback(
    async (nextOpen) => {
      setOpen(nextOpen)
      if (!nextOpen || fetchedRef.current) return
      fetchedRef.current = true
      setLoading(true)
      try {
        const res = await automationAPI.getChannelPeriods(channel.id)
        setPeriods(Array.isArray(res.data) ? res.data : [])
      } catch {
        setPeriods([])
      } finally {
        setLoading(false)
      }
    },
    [channel.id]
  )

  const periodCount = channel?.automation_periods_count ?? 0

  return (
    <Tooltip open={open} onOpenChange={handleOpenChange}>
      <TooltipTrigger asChild>{children}</TooltipTrigger>
      <TooltipContent className="max-w-[280px] p-3 space-y-2">
        <p className="font-semibold text-sm">
          {periodCount} Automation {periodCount === 1 ? 'Period' : 'Periods'}
        </p>
        {loading && (
          <div className="flex items-center gap-2 text-xs text-muted-foreground py-1">
            <Loader2 className="h-3 w-3 animate-spin" />
            Loading…
          </div>
        )}
        {!loading && periods !== null && periods.length === 0 && (
          <p className="text-xs text-muted-foreground italic">No periods assigned</p>
        )}
        {!loading && periods && periods.length > 0 && (
          <div className="space-y-1.5">
            {periods.map((period) => {
              const profileName =
                period.profile?.name ||
                period.profile_name ||
                (period.profile_id
                  ? profiles?.find(p => String(p.id) === String(period.profile_id))?.name
                  : null)
              return (
                <div key={period.id} className="rounded-md bg-muted/50 px-2 py-1.5 space-y-0.5">
                  <p className="text-xs font-medium leading-tight">{period.name}</p>
                  <p className="text-[11px] text-muted-foreground leading-tight">
                    {formatSchedule(period.schedule)}
                    {profileName && <span className="ml-1">· {profileName}</span>}
                  </p>
                </div>
              )
            })}
          </div>
        )}
      </TooltipContent>
    </Tooltip>
  )
}

// ---------------------------------------------------------------------------
// Tooltip: Regex Patterns column — zero fetching, pure props
// ---------------------------------------------------------------------------
function RegexPatternsTooltip({ children, channelPatterns, matchByTvgId, isTvgInherited, channel }) {
  const normalizedPatterns = normalizePatternData(channelPatterns)

  return (
    <Tooltip>
      <TooltipTrigger asChild>{children}</TooltipTrigger>
      <TooltipContent className="max-w-[320px] p-3 space-y-2">
        <p className="font-semibold text-sm">Regex Patterns</p>
        {matchByTvgId && (
          <div className="rounded-md bg-muted/50 px-2 py-1.5">
            <p className="text-xs font-medium flex items-center gap-1">
              TVG-ID matching
              {isTvgInherited && (
                <Badge variant="outline" className="text-[10px] h-4 px-1 ml-1">Group</Badge>
              )}
            </p>
            {channel?.tvg_id ? (
              <p className="text-[11px] text-muted-foreground font-mono mt-0.5 break-all">
                {channel.tvg_id}
              </p>
            ) : (
              <p className="text-[11px] text-muted-foreground italic mt-0.5">No TVG-ID set</p>
            )}
          </div>
        )}
        {normalizedPatterns.length > 0 ? (
          <div className="space-y-1.5">
            {normalizedPatterns.map((p, i) => (
              <div key={i} className="rounded-md bg-muted/50 px-2 py-1.5 space-y-0.5">
                <p className="text-xs font-mono break-all leading-snug">{p.pattern}</p>
                {p.m3u_accounts && p.m3u_accounts.length > 0 && (
                  <p className="text-[11px] text-muted-foreground">
                    M3U filter: {p.m3u_accounts.join(', ')}
                  </p>
                )}
              </div>
            ))}
          </div>
        ) : !matchByTvgId ? (
          <p className="text-xs text-muted-foreground italic">No patterns configured</p>
        ) : null}
      </TooltipContent>
    </Tooltip>
  )
}

// ---------------------------------------------------------------------------
// ActiveProfileLines — renders the two inline lines in the Channel Group cell.
// Data comes from the activeProfile prop (fetched eagerly at page level and
// cached in ChannelConfiguration.jsx). Starts muted while loading.
// ---------------------------------------------------------------------------
function ActiveProfileLines({ activeProfile }) {
  // activeProfile is one of:
  //   undefined  — fetch not yet initiated or in flight
  //   null       — fetch in flight (explicit loading state)
  //   { error }  — fetch failed
  //   { automation: {...}, epg_override: {...} } — resolved

  const loading = activeProfile === null || activeProfile === undefined

  const auto = activeProfile?.automation
  const epg  = activeProfile?.epg_override

  const renderAutomation = () => {
    if (loading) return <span className="text-muted-foreground/40">—</span>
    if (activeProfile?.error) return <span className="text-muted-foreground/60 italic">error</span>
    if (!auto?.profile_name) return <span className="text-muted-foreground/40">—</span>
    const source = auto.source === 'channel' ? 'Channel' : auto.source === 'group' ? 'Group' : null
    return (
      <span className="text-foreground">
        {auto.profile_name}
        {auto.period_name && (
          <span className="text-muted-foreground"> · {auto.period_name}</span>
        )}
        {source && (
          <span className="text-muted-foreground"> · {source}</span>
        )}
      </span>
    )
  }

  const renderEpg = () => {
    if (loading) return <span className="text-muted-foreground/40">—</span>
    if (activeProfile?.error) return <span className="text-muted-foreground/60 italic">error</span>
    if (!epg?.profile_name) return <span className="text-muted-foreground/40">—</span>
    const source = epg.source === 'channel' ? 'Channel' : epg.source === 'group' ? 'Group' : null
    return (
      <span className="text-foreground">
        {epg.profile_name}
        {source && (
          <span className="text-muted-foreground"> · {source}</span>
        )}
      </span>
    )
  }

  return (
    <div className="space-y-0.5 min-w-0">
      <p className="text-[11px] leading-snug">
        <span className="text-muted-foreground">Automation Profile: </span>
        {renderAutomation()}
      </p>
      <p className="text-[11px] leading-snug">
        <span className="text-muted-foreground">EPG Profile: </span>
        {renderEpg()}
      </p>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Main component
// ---------------------------------------------------------------------------
export function RegexTableRow({
  channel,
  group,
  groupsConfig,
  profiles,
  patterns,
  selectedChannels,
  onToggleChannel,
  onEditRegex,
  onDeletePattern,
  expandedRowId,
  onToggleExpanded,
  onCheckChannel,
  checkingChannel,
  m3uAccounts,
  onUpdateMatchSettings,
  onPreviewMatch,
  onRefresh,
  onAssignEpgProfile,
  matchCount,
  totalStreamCount,
  activeProfile,   // { automation, epg_override } | { error } | null | undefined
}) {
  const [logoUrl, setLogoUrl] = useState(null)
  const [logoError, setLogoError] = useState(false)
  const [channelPeriods, setChannelPeriods] = useState([])
  const [loadingPeriods, setLoadingPeriods] = useState(false)
  const [assignDialogOpen, setAssignDialogOpen] = useState(false)
  const { toast } = useToast()

  const expanded = expandedRowId === channel.id
  const isChecking = checkingChannel === channel.id

  const channelPatterns = patterns[channel.id] || patterns[String(channel.id)]
  const channelGroupId = channel?.group_id ?? channel?.channel_group_id
  const groupMatchingConfig = channelGroupId ? ((groupsConfig?.[channelGroupId] || {}).matching || {}) : {}
  const hasChannelMatchingConfig = Boolean(channelPatterns) && Object.keys(channelPatterns || {}).length > 0
  const effectiveMatchingConfig = hasChannelMatchingConfig ? channelPatterns : groupMatchingConfig
  const matchByTvgId = Boolean(effectiveMatchingConfig?.match_by_tvg_id)
  const isTvgInherited = !hasChannelMatchingConfig && Boolean(channelGroupId)
  const patternCount = normalizePatternData(channelPatterns).length
  const isEpgChannelOverride = Boolean(channel?.channel_epg_scheduled_profile_id)
  const isEpgGroupBased = !isEpgChannelOverride && Boolean(groupsConfig?.[channelGroupId]?.epg_profile_id)
  const groupMatchingPatternCount = Array.isArray(groupMatchingConfig?.regex_patterns)
    ? groupMatchingConfig.regex_patterns.length
    : 0

  // Logo
  useEffect(() => {
    const cached = getCachedChannelLogoUrl(channel.id)
    if (cached) { setLogoUrl(cached); return }
    if (channel.logo_id) {
      channelsAPI.getChannelLogo(channel.logo_id)
        .then(res => {
          const url = res.data?.url || res.data
          if (url) { setLogoUrl(url); setCachedChannelLogoUrl(channel.id, url) }
        })
        .catch(() => setLogoError(true))
    }
  }, [channel.id, channel.logo_id])

  // Expanded panel period fetch
  const loadChannelPeriods = useCallback(async () => {
    setLoadingPeriods(true)
    try {
      const res = await automationAPI.getChannelPeriods(channel.id)
      setChannelPeriods(Array.isArray(res.data) ? res.data : [])
    } catch {
      setChannelPeriods([])
    } finally {
      setLoadingPeriods(false)
    }
  }, [channel.id])

  useEffect(() => {
    if (expanded) loadChannelPeriods()
  }, [expanded, loadChannelPeriods])

  const handleRemovePeriod = async (periodId) => {
    try {
      await automationAPI.removePeriodFromChannels(periodId, [channel.id])
      toast({ title: 'Success', description: 'Period removed' })
      loadChannelPeriods()
      if (onRefresh) onRefresh()
    } catch {
      toast({ title: 'Error', description: 'Failed to remove period', variant: 'destructive' })
    }
  }

  return (
    <div className="border-b last:border-b-0">
      {/* ── Collapsed row ── */}
      <div
        className="grid items-center gap-2 px-3 py-3 hover:bg-muted/30 transition-colors"
        style={{ gridTemplateColumns: REGEX_TABLE_GRID_COLS }}
      >
        {/* Checkbox */}
        <div className="flex items-center">
          <Checkbox
            checked={selectedChannels?.has(channel.id)}
            onCheckedChange={() => onToggleChannel?.(channel.id)}
          />
        </div>

        {/* Channel number */}
        <div className="text-sm font-mono text-muted-foreground">
          {channel.channel_number || '—'}
        </div>

        {/* Logo */}
        <div className="flex items-center justify-center">
          <div className="w-8 h-8 rounded bg-muted flex items-center justify-center overflow-hidden">
            {logoUrl && !logoError ? (
              <img
                src={logoUrl}
                alt={channel.name}
                className="w-full h-full object-contain"
                onError={() => setLogoError(true)}
              />
            ) : (
              <span className="text-xs font-bold text-muted-foreground">
                {channel.name?.charAt(0) || '?'}
              </span>
            )}
          </div>
        </div>

        {/* Channel name */}
        <div className="flex items-center">
          <span className="font-medium">{channel.name}</span>
        </div>

        {/* ── Channel Group column — inline active profile lines, no tooltip ── */}
        <div className="flex items-start text-sm min-w-0">
          <div className="min-w-0 w-full">
            <div className="text-xs text-muted-foreground truncate mb-1">{group?.name || '-'}</div>
            <ActiveProfileLines activeProfile={activeProfile} />
          </div>
        </div>

        {/* ── Nº of Periods ── */}
        <div className="flex items-center">
          <PeriodsTooltip channel={channel} profiles={profiles}>
            <div className="cursor-default">
              <Badge variant="outline" className="text-xs font-normal">
                {channel.automation_periods_count || 0}{' '}
                {channel.automation_periods_count !== 1 ? 'periods' : 'period'}
              </Badge>
            </div>
          </PeriodsTooltip>
        </div>

        {/* ── Regex Patterns ── */}
        <div className="flex flex-col gap-1 items-center justify-center">
          <RegexPatternsTooltip
            channelPatterns={channelPatterns}
            matchByTvgId={matchByTvgId}
            isTvgInherited={isTvgInherited}
            channel={channel}
          >
            <div className="flex items-center gap-1 flex-wrap cursor-default">
              {patternCount > 0 ? (
                <Badge variant="secondary">
                  {patternCount} pattern{patternCount > 1 ? 's' : ''}
                </Badge>
              ) : groupMatchingPatternCount > 0 ? (
                <Badge variant="outline" className="text-[10px]">
                  {groupMatchingPatternCount} (group)
                </Badge>
              ) : (
                <span className="text-xs text-muted-foreground">No patterns</span>
              )}
              {matchByTvgId && (
                <>
                  <Badge variant="default" className="text-xs">TVG-ID</Badge>
                  {isTvgInherited && (
                    <Badge variant="outline" className="text-xs">From Group</Badge>
                  )}
                </>
              )}
            </div>
          </RegexPatternsTooltip>

          {/* Stream match count — stacked below pattern badge */}
          <Tooltip>
            <TooltipTrigger asChild>
              <span className="text-xs font-mono tabular-nums cursor-default select-none">
                <span className={matchCount !== undefined ? 'text-green-500' : 'text-muted-foreground'}>
                  {matchCount !== undefined ? matchCount : '—'}
                </span>
                <span className="text-muted-foreground">{' / '}</span>
                <span className="text-muted-foreground">{channel.streams?.length ?? 0}</span>
              </span>
            </TooltipTrigger>
            <TooltipContent className="max-w-xs">
              <p className="font-medium mb-1">Stream Match Counts</p>
              <p className="text-xs">
                <span className="text-green-400">{matchCount !== undefined ? matchCount : '—'}</span>
                {' '}streams currently match your regex configuration (potential)
              </p>
              <p className="text-xs mt-1">
                <span className="text-foreground">{channel.streams?.length ?? 0}</span>
                {' '}streams Dispatcharr has assigned from the last check pass
              </p>
              {matchCount !== undefined && (
                <p className="text-xs mt-1 text-muted-foreground">
                  The difference represents streams culled by health checking
                </p>
              )}
              {matchCount === undefined && (
                <p className="text-xs mt-1 text-muted-foreground">
                  Potential match count loading or unavailable
                </p>
              )}
            </TooltipContent>
          </Tooltip>
        </div>

        {/* Actions */}
        <div className="flex items-center gap-2">
          <Tooltip>
            <TooltipTrigger asChild>
              <Button variant="ghost" size="sm" onClick={() => onPreviewMatch?.(channel.id, 'global')}>
                <Eye className="h-4 w-4 text-muted-foreground" />
              </Button>
            </TooltipTrigger>
            <TooltipContent>Global Channel Preview</TooltipContent>
          </Tooltip>
          <Tooltip>
            <TooltipTrigger asChild>
              <Button
                variant="outline"
                size="sm"
                onClick={() => onCheckChannel?.(channel.id)}
                disabled={isChecking}
                className="text-blue-600 dark:text-green-500 border-blue-600 dark:border-green-500 hover:bg-blue-50 dark:hover:bg-green-950"
              >
                {isChecking
                  ? <Loader2 className="h-4 w-4 animate-spin" />
                  : <Activity className="h-4 w-4" />}
              </Button>
            </TooltipTrigger>
            <TooltipContent>Health Check Channel</TooltipContent>
          </Tooltip>
          <Button variant="outline" size="sm" onClick={() => onToggleExpanded?.(channel.id)}>
            <ChevronDown className={`h-4 w-4 transition-transform ${expanded ? 'rotate-180' : ''}`} />
          </Button>
        </div>
      </div>

      {/* ── Expanded panel (unchanged) ── */}
      {expanded && (
        <div className="px-4 pb-4 space-y-4 bg-muted/20 border-t">
          <div className="pt-4">
            <div className="flex items-center justify-between mb-3">
              <h4 className="font-medium text-sm flex items-center gap-2">
                <Edit className="h-4 w-4" />
                Regex Patterns
                {!hasChannelMatchingConfig && channelGroupId && groupMatchingPatternCount > 0 && (
                  <Badge variant="outline" className="text-[10px]">Inherited from group</Badge>
                )}
              </h4>
              <Button size="sm" variant="outline" className="h-7 text-xs"
                onClick={() => onEditRegex?.(channel.id, null)}>
                <Plus className="h-3 w-3 mr-1" />
                Add Pattern
              </Button>
            </div>

            {normalizePatternData(channelPatterns).length > 0 ? (
              <div className="space-y-2">
                {normalizePatternData(channelPatterns).map((p, idx) => (
                  <div key={idx}
                    className="flex items-center justify-between gap-2 rounded-md border bg-background px-3 py-2">
                    <div className="flex-1 min-w-0">
                      <p className="text-xs font-mono break-all">{p.pattern}</p>
                      {p.m3u_accounts && p.m3u_accounts.length > 0 && (
                        <p className="text-[11px] text-muted-foreground mt-0.5">
                          M3U: {p.m3u_accounts.join(', ')}
                        </p>
                      )}
                    </div>
                    <div className="flex gap-1 shrink-0">
                      <Button size="sm" variant="ghost" className="h-7 w-7 p-0"
                        onClick={() => onEditRegex?.(channel.id, idx)}>
                        <Edit className="h-3 w-3" />
                      </Button>
                      <Button size="sm" variant="ghost"
                        className="h-7 w-7 p-0 text-destructive hover:text-destructive"
                        onClick={() => onDeletePattern?.(channel.id, idx)}>
                        <Trash2 className="h-3 w-3" />
                      </Button>
                    </div>
                  </div>
                ))}
              </div>
            ) : (
              <p className="text-sm text-muted-foreground bg-background p-4 rounded-lg border border-dashed text-center">
                {groupMatchingPatternCount > 0
                  ? `Using ${groupMatchingPatternCount} pattern(s) inherited from group`
                  : 'No patterns configured for this channel'}
              </p>
            )}
          </div>

          <Separator />

          {onUpdateMatchSettings && (
            <div className="flex items-center justify-between">
              <div>
                <Label className="text-sm font-medium">TVG-ID Matching</Label>
                <p className="text-xs text-muted-foreground">Match streams using the channel's TVG-ID</p>
              </div>
              <Switch
                checked={matchByTvgId}
                onCheckedChange={(checked) =>
                  onUpdateMatchSettings?.(channel.id, { match_by_tvg_id: checked })
                }
              />
            </div>
          )}

          <Separator />

          <div>
            <div className="flex items-center justify-between mb-3">
              <h4 className="font-medium text-sm flex items-center gap-2">
                <Calendar className="h-4 w-4" />
                Automation Periods
              </h4>
              <Button size="sm" variant="outline" className="h-7 text-xs"
                onClick={() => setAssignDialogOpen(true)}>
                <Plus className="h-3 w-3 mr-1" />
                Assign Period
              </Button>
            </div>

            {loadingPeriods ? (
              <div className="flex items-center gap-2 text-xs text-muted-foreground p-2">
                <Loader2 className="h-3 w-3 animate-spin" />
                Loading periods…
              </div>
            ) : channelPeriods.length > 0 ? (
              <div className="space-y-2">
                {channelPeriods.map((period) => {
                  const profileName =
                    period.profile?.name ||
                    period.profile_name ||
                    (period.profile_id
                      ? profiles?.find(p => String(p.id) === String(period.profile_id))?.name
                      : null)
                  return (
                    <div key={period.id}
                      className="flex items-center justify-between gap-2 rounded-md border bg-background px-3 py-2">
                      <div className="flex-1 min-w-0">
                        <p className="text-xs font-medium">{period.name}</p>
                        <p className="text-[11px] text-muted-foreground mt-0.5">
                          {formatSchedule(period.schedule)}
                          {profileName && <span className="ml-1">· {profileName}</span>}
                        </p>
                      </div>
                      <Button size="sm" variant="ghost"
                        onClick={() => handleRemovePeriod(period.id)}
                        className="text-destructive hover:text-destructive hover:bg-destructive/10 h-7 w-7 p-0">
                        <Trash2 className="h-4 w-4" />
                      </Button>
                    </div>
                  )
                })}
              </div>
            ) : (
              <p className="text-sm text-muted-foreground bg-background p-4 rounded-lg border border-dashed text-center">
                No automation periods assigned to this channel
              </p>
            )}

            <AssignPeriodsDialog
              open={assignDialogOpen}
              onOpenChange={setAssignDialogOpen}
              channelId={channel.id}
              channelName={channel.name}
              onSuccess={() => {
                loadChannelPeriods()
                if (onRefresh) onRefresh()
              }}
            />
          </div>

          <Separator />

          {onAssignEpgProfile && (
            <div>
              <h4 className="font-medium text-sm flex items-center gap-2 mb-2">
                <CalendarClock className="h-4 w-4" />
                EPG Scheduled Profile
                {isEpgChannelOverride ? (
                  <Badge variant="default" className="text-[10px]">Override</Badge>
                ) : isEpgGroupBased ? (
                  <Badge variant="outline" className="text-[10px]">From Group</Badge>
                ) : null}
              </h4>
              <Select
                value={channel.channel_epg_scheduled_profile_id || ''}
                onValueChange={(v) => onAssignEpgProfile?.(channel.id, v === 'none' ? null : v)}
              >
                <SelectTrigger className="h-8 text-xs">
                  <SelectValue placeholder="Use period profile (default)" />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="none">— Use period profile (default) —</SelectItem>
                  {profiles?.map((p) => (
                    <SelectItem key={p.id} value={p.id}>{p.name}</SelectItem>
                  ))}
                </SelectContent>
              </Select>
              <p className="text-xs text-muted-foreground mt-1">
                When set, this profile overrides the automation period profile for EPG scheduled stream checks.
              </p>
            </div>
          )}
        </div>
      )}
    </div>
  )
}
