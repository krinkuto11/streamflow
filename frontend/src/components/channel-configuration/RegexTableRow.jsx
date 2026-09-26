import { useCallback, useEffect, useState } from 'react'
import { Button } from '@/components/ui/button.jsx'
import { Checkbox } from '@/components/ui/checkbox.jsx'
import { Badge } from '@/components/ui/badge.jsx'
import { Label } from '@/components/ui/label.jsx'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select.jsx'
import { Separator } from '@/components/ui/separator.jsx'
import { Switch } from '@/components/ui/switch.jsx'
import { useToast } from '@/hooks/use-toast.js'
import { channelsAPI, automationAPI } from '@/services/api.js'
import { getCachedChannelLogoUrl, setCachedChannelLogoUrl } from '@/services/channelCache.js'
import { Plus, Trash2, Loader2, Eye, ChevronDown, Activity, Calendar, CalendarClock, Edit } from 'lucide-react'
import {
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

export function ActiveProfileSummary({ activeProfile, details = false }) {
  const loading = activeProfile == null
  const failed = Boolean(activeProfile?.error)
  const automation = activeProfile?.automation
  const profileName = loading ? 'Loading profile...' : failed ? 'Profile load failed' : automation?.profile_name || 'No profile configured'
  return (
    <div className="min-w-0 space-y-1" aria-busy={loading}>
      <p className={`break-words text-sm ${loading || failed || !automation?.profile_name ? 'text-muted-foreground' : 'font-medium'}`}>{profileName}</p>
      {details && !loading && !failed && (
        <>
          <p className="text-xs text-muted-foreground">
            {automation?.period_name && <span>{automation.period_name} / </span>}
            {automation?.source === 'channel' ? 'Channel configuration' : automation?.source === 'group' ? 'Inherited from group' : 'No automation period assigned'}
          </p>
          <p className="text-xs text-muted-foreground">EPG override: <span className="text-foreground">{activeProfile?.epg_override?.profile_name || 'Use period profile'}</span></p>
        </>
      )}
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
  matchCountState,
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
  const hasChannelMatchingConfig = normalizePatternData(channelPatterns).length > 0 ||
    channelPatterns?.match_by_tvg_id === true || channelPatterns?.enabled === false
  const effectiveMatchingConfig = hasChannelMatchingConfig ? channelPatterns : groupMatchingConfig
  const matchByTvgId = Boolean(effectiveMatchingConfig?.match_by_tvg_id)
  const isTvgInherited = !hasChannelMatchingConfig && Boolean(channelGroupId)
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
      <div className="grid grid-cols-[24px_minmax(0,1fr)_minmax(0,1fr)] items-start gap-x-3 gap-y-2 p-3 transition-colors hover:bg-muted/20 xl:items-center xl:gap-3 xl:p-4 xl:[grid-template-columns:24px_minmax(0,1.4fr)_112px_minmax(0,1fr)_180px]">
        <Checkbox
          aria-label={`Select ${channel.name}`}
          checked={selectedChannels?.has(channel.id)}
          onCheckedChange={() => onToggleChannel?.(channel.id)}
          className="mt-2 xl:mt-0"
        />
        <div className="col-span-2 flex min-w-0 items-center gap-3 xl:col-span-1">
          <div className="flex h-9 w-9 shrink-0 items-center justify-center overflow-hidden rounded-md bg-muted">
            {logoUrl && !logoError ? (
              <img src={logoUrl} alt="" className="h-full w-full object-contain" onError={() => setLogoError(true)} />
            ) : <span className="text-sm font-bold text-muted-foreground">{channel.name?.charAt(0) || '?'}</span>}
          </div>
          <div className="min-w-0">
            <p className="break-words text-sm font-semibold">{channel.name}</p>
            <p className="truncate text-xs text-muted-foreground">#{channel.channel_number || '-'} / {group?.name || 'Ungrouped'}</p>
          </div>
        </div>
        <div className="col-start-2 text-sm xl:col-auto">
          <span className={channel.streams?.length ? 'font-medium tabular-nums' : 'font-medium text-amber-600 dark:text-amber-400'}>{channel.streams?.length ?? 0}</span>
          <span className="ml-1 text-xs text-muted-foreground">assigned</span>
          <p className="text-xs text-muted-foreground">{matchCount !== undefined ? `${matchCount} matching` : matchCountState === 'not_configured' ? 'No matching rules' : matchCountState === 'unavailable' ? 'Matches unavailable' : 'Loading matches...'}</p>
        </div>
        <div className="col-start-3 min-w-0 xl:col-auto">
          <span className="sr-only">Effective profile: </span>
          <ActiveProfileSummary activeProfile={activeProfile} />
        </div>
        <div className="col-span-3 flex items-center gap-2 xl:col-span-1 xl:justify-end">
          <Button variant="outline" size="sm" className="min-h-11 flex-1 xl:flex-none" onClick={() => onCheckChannel?.(channel.id)} disabled={isChecking} aria-label={`Check ${channel.name}`}>
            {isChecking ? <Loader2 className="mr-1.5 h-4 w-4 animate-spin" /> : <Activity className="mr-1.5 h-4 w-4" />}{isChecking ? 'Checking' : 'Check'}
          </Button>
          <Button variant="ghost" size="sm" className="min-h-11 flex-1 xl:flex-none" onClick={() => onToggleExpanded?.(channel.id)} aria-expanded={expanded} aria-controls={`channel-details-${channel.id}`} aria-label={`Details for ${channel.name}`}>
            Details<ChevronDown className={`ml-1.5 h-4 w-4 transition-transform ${expanded ? 'rotate-180' : ''}`} />
          </Button>
        </div>
      </div>

      {/* Advanced configuration remains available within each channel. */}
      {expanded && (
        <div id={`channel-details-${channel.id}`} className="border-t bg-muted/20 px-4 pb-4 space-y-4">
          <div className="flex flex-col gap-3 pt-4 sm:flex-row sm:items-start sm:justify-between">
            <div>
              <p className="mb-1 text-xs font-medium text-muted-foreground">Effective automation profile</p>
              <ActiveProfileSummary activeProfile={activeProfile} details />
              {activeProfile?.error && <Button variant="link" size="sm" className="h-auto p-0" onClick={onRefresh}>Retry profile loading</Button>}
            </div>
            <Button variant="outline" size="sm" className="min-h-11" onClick={() => onPreviewMatch?.(channel.id, 'global')}>
              <Eye className="mr-1.5 h-4 w-4" />Preview stream matches
            </Button>
          </div>
          <p className="text-xs text-muted-foreground">
            {channel.streams?.length ?? 0} assigned streams{matchCount !== undefined ? ` / ${matchCount} potential matches` : ''} / {channel.automation_periods_count || 0} automation periods
            {matchByTvgId ? ` / TVG-ID matching${isTvgInherited ? ' from group' : ''}` : ''}
          </p>
          <div className="pt-4">
            <div className="flex flex-wrap items-center justify-between gap-2 mb-3">
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
                        aria-label={`Edit pattern ${idx + 1} for ${channel.name}`} onClick={() => onEditRegex?.(channel.id, idx)}>
                        <Edit className="h-3 w-3" />
                      </Button>
                      <Button size="sm" variant="ghost"
                        className="h-7 w-7 p-0 text-destructive hover:text-destructive"
                        aria-label={`Delete pattern ${idx + 1} for ${channel.name}`} onClick={() => onDeletePattern?.(channel.id, idx)}>
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
            <div className="flex items-center justify-between gap-3">
              <div>
                <Label className="text-sm font-medium">TVG-ID Matching</Label>
                <p className="text-xs text-muted-foreground">Match streams using the channel's TVG-ID</p>
              </div>
              <Switch
                aria-label={`TVG-ID matching for ${channel.name}`}
                checked={matchByTvgId}
                onCheckedChange={(checked) =>
                  onUpdateMatchSettings?.(channel.id, { match_by_tvg_id: checked })
                }
              />
            </div>
          )}

          <Separator />

          <div>
            <div className="flex flex-wrap items-center justify-between gap-2 mb-3">
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
                        aria-label={`Remove ${period.name} from ${channel.name}`} onClick={() => handleRemovePeriod(period.id)}
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
                <SelectTrigger aria-label={`EPG scheduled profile for ${channel.name}`} className="min-h-11 text-xs">
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
