
import { useCallback, useEffect, useState } from 'react'
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
import { Plus, Trash2, Loader2, Eye, ChevronDown, Activity, Calendar, CalendarClock, Edit } from 'lucide-react'
import {
  REGEX_TABLE_GRID_COLS,
  normalizePatternData,
} from '@/components/channel-configuration/patternUtils.js'
import { AssignPeriodsDialog } from '@/components/channel-configuration/PeriodDialogs.jsx'

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
  const isPeriodChannelOverride =
    channel?.automation_periods_source === 'channel' || Number(channel?.channel_periods_count || 0) > 0
  const isPeriodGroupBased =
    channel?.automation_periods_source === 'group' || (!isPeriodChannelOverride && Number(channel?.group_periods_count || 0) > 0)
  const isEpgChannelOverride = Boolean(channel?.channel_epg_scheduled_profile_id)
  const isEpgGroupBased = !isEpgChannelOverride && Boolean(groupsConfig?.[channelGroupId]?.epg_profile_id)
  const groupMatchingPatternCount = Array.isArray(groupMatchingConfig?.regex_patterns)
    ? groupMatchingConfig.regex_patterns.length
    : 0
  const isRegexChannelOverride = patternCount > 0
  const isRegexGroupBased = !isRegexChannelOverride && groupMatchingPatternCount > 0

  const loadChannelPeriods = useCallback(async () => {
    try {
      setLoadingPeriods(true)
      const response = await automationAPI.getChannelPeriods(channel.id)
      setChannelPeriods(response.data || [])
    } catch (err) {
      console.error('Failed to load channel periods:', err)
    } finally {
      setLoadingPeriods(false)
    }
  }, [channel.id])

  useEffect(() => {
    if (expanded) {
      loadChannelPeriods()
    }
  }, [expanded, loadChannelPeriods])

  const handleRemovePeriod = async (periodId) => {
    try {
      await automationAPI.removePeriodFromChannels(periodId, [channel.id])
      toast({
        title: 'Success',
        description: 'Automation period removed from channel',
      })
      loadChannelPeriods()
      if (onRefresh) {
        onRefresh()
      }
    } catch (err) {
      console.error('Failed to remove period:', err)
      toast({
        title: 'Error',
        description: 'Failed to remove automation period',
        variant: 'destructive',
      })
    }
  }

  useEffect(() => {
    const loadLogo = () => {
      const cachedUrl = getCachedChannelLogoUrl(channel.id)
      if (cachedUrl) {
        setLogoUrl(cachedUrl)
        return
      }

      if (channel.logo_id) {
        const channelLogoUrl = channelsAPI.getLogoCached(channel.logo_id)
        setLogoUrl(channelLogoUrl)
        setCachedChannelLogoUrl(channel.id, channelLogoUrl)
      }
    }
    loadLogo()
  }, [channel.id, channel.logo_id])

  return (
    <div key={channel.id}>
      <div className="grid gap-4 p-4 hover:bg-muted/50 transition-colors" style={{ gridTemplateColumns: REGEX_TABLE_GRID_COLS }}>
        <div className="flex items-center justify-center">
          <Checkbox checked={selectedChannels.has(channel.id)} onCheckedChange={() => onToggleChannel(channel.id)} />
        </div>
        <div className="flex items-center text-sm font-medium">{channel.channel_number || '-'}</div>
        <div className="flex items-center">
          <div className="w-16 h-10 flex-shrink-0 bg-muted rounded-md flex items-center justify-center overflow-hidden">
            {logoUrl && !logoError ? (
              <img
                src={logoUrl}
                alt={channel.name}
                className="w-full h-full object-contain"
                onError={() => setLogoError(true)}
              />
            ) : (
              <span className="text-lg font-bold text-muted-foreground">{channel.name?.charAt(0) || '?'}</span>
            )}
          </div>
        </div>
        <div className="flex items-center">
          <span className="font-medium truncate">{channel.name}</span>
        </div>
        <div className="flex items-center text-sm text-muted-foreground">
          <div className="min-w-0">
            <div className="truncate">{group?.name || '-'}</div>
            <div className="flex items-center gap-1 mt-1 flex-wrap">
              <Badge variant={isPeriodChannelOverride ? 'default' : 'outline'} className="text-[10px] h-5 px-1.5">
                Automation: {isPeriodChannelOverride ? 'Override' : isPeriodGroupBased ? 'Group' : 'None'}
              </Badge>
              <Badge variant={isEpgChannelOverride ? 'default' : 'outline'} className="text-[10px] h-5 px-1.5">
                EPG Profile: {isEpgChannelOverride ? 'Override' : isEpgGroupBased ? 'Group' : 'None'}
              </Badge>
            </div>
          </div>
        </div>
        <div className="flex items-center">
          <Badge variant="outline" className="text-xs font-normal">
            {channel.automation_periods_count || 0} period{channel.automation_periods_count !== 1 ? 's' : ''}
          </Badge>
        </div>
        <div className="flex items-center gap-2 flex-wrap">
          {patternCount > 0 ? (
            <Badge variant="secondary">{patternCount} pattern{patternCount > 1 ? 's' : ''}</Badge>
          ) : (
            <span className="text-sm text-muted-foreground">No patterns</span>
          )}
          {matchByTvgId && (
            <>
              <Badge variant="default" className="text-xs">
                TVG-ID
              </Badge>
              {isTvgInherited && (
                <Badge variant="outline" className="text-xs">
                  From Group
                </Badge>
              )}
            </>
          )}
          <Tooltip>
            <TooltipTrigger asChild>
              <span className="text-xs font-mono tabular-nums cursor-default select-none">
                <span className={matchCount !== undefined ? 'text-green-500' : 'text-muted-foreground'}>
                  {matchCount !== undefined ? matchCount : '—'}
                </span>
                <span className="text-muted-foreground">{' / '}</span>
                <span className="text-muted-foreground">{(channel.streams?.length ?? 0)}</span>
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
        <div className="flex items-center gap-2">
          <Tooltip>
            <TooltipTrigger asChild>
              <Button variant="ghost" size="sm" onClick={() => onPreviewMatch(channel.id, 'global')}>
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
                onClick={() => onCheckChannel(channel.id)}
                disabled={isChecking}
                className="text-blue-600 dark:text-green-500 border-blue-600 dark:border-green-500 hover:bg-blue-50 dark:hover:bg-green-950"
              >
                {isChecking ? <Loader2 className="h-4 w-4 animate-spin" /> : <Activity className="h-4 w-4" />}
              </Button>
            </TooltipTrigger>
            <TooltipContent>
              <p>Health Check Channel</p>
            </TooltipContent>
          </Tooltip>
          <Button variant="outline" size="sm" onClick={() => onToggleExpanded(channel.id)}>
            <ChevronDown className={`h-4 w-4 transition-transform ${expanded ? 'rotate-180' : ''}`} />
          </Button>
        </div>
      </div>

      {expanded && (
        <div className="border-t p-4 bg-muted/50 space-y-6">
          <div>
            <div className="flex justify-between items-center mb-3">
              <h4 className="font-medium text-sm flex items-center gap-2">
                <Calendar className="h-4 w-4" />
                Automation Periods
                {isPeriodChannelOverride ? (
                  <Badge variant="default" className="text-[10px]">Override</Badge>
                ) : isPeriodGroupBased ? (
                  <Badge variant="outline" className="text-[10px]">From Group</Badge>
                ) : null}
              </h4>
              <Button size="sm" variant="outline" onClick={() => setAssignDialogOpen(true)}>
                <Plus className="h-4 w-4 mr-2" />
                Assign to Period
              </Button>
            </div>

            {loadingPeriods ? (
              <div className="flex items-center justify-center py-4">
                <Loader2 className="h-5 w-5 animate-spin text-muted-foreground" />
              </div>
            ) : channelPeriods.length > 0 ? (
              <div className="grid gap-2 grid-cols-1 md:grid-cols-2">
                {channelPeriods.map((period) => (
                  <div key={period.id} className="flex items-center justify-between p-3 bg-background border rounded-lg">
                    <div className="min-w-0">
                      <div className="font-medium text-sm truncate">{period.name}</div>
                      <div className="flex items-center gap-2 mt-1">
                        <Badge variant="secondary" className="text-[10px] font-normal">
                          {period.profile_name || 'No Profile'}
                        </Badge>
                        <span className="text-[10px] text-muted-foreground">
                          {period.schedule?.type === 'interval' ? `${period.schedule.value}m` : 'Cron'}
                        </span>
                      </div>
                    </div>
                    <Button
                      size="sm"
                      variant="ghost"
                      onClick={() => handleRemovePeriod(period.id)}
                      className="text-destructive hover:text-destructive hover:bg-destructive/10 h-7 w-7 p-0"
                    >
                      <Trash2 className="h-4 w-4" />
                    </Button>
                  </div>
                ))}
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
                if (onRefresh) {
                  onRefresh()
                }
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
                onValueChange={(v) => onAssignEpgProfile(channel.id, v === 'none' ? null : v)}
              >
                <SelectTrigger className="h-8 text-xs">
                  <SelectValue placeholder="Use period profile (default)" />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="none">— Use period profile (default) —</SelectItem>
                  {profiles.map((p) => (
                    <SelectItem key={p.id} value={p.id}>
                      {p.name}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
              <p className="text-xs text-muted-foreground mt-1">
                When set, this profile overrides the automation period profile for EPG scheduled stream checks.
              </p>
            </div>
          )}

          <Separator />

          <div>
            <div className="flex items-center justify-between mb-4 p-3 bg-muted rounded-md">
              <div className="flex items-center space-x-2">
                <Switch
                  id={`tvg-match-${channel.id}`}
                  checked={matchByTvgId}
                  onCheckedChange={(checked) => onUpdateMatchSettings(channel.id, { match_by_tvg_id: checked })}
                />
                <Label htmlFor={`tvg-match-${channel.id}`} className="flex flex-col cursor-pointer">
                  <span className="font-medium flex items-center gap-2">
                    Match by TVG-ID
                    {isTvgInherited && (
                      <Badge variant="outline" className="text-[10px]">
                        From Group
                      </Badge>
                    )}
                  </span>
                  <span className="font-normal text-xs text-muted-foreground">
                    Automatically match streams with TVG-ID "{channel.tvg_id || 'N/A'}"
                  </span>
                </Label>
              </div>
              <Button
                size="sm"
                variant="outline"
                onClick={() => onPreviewMatch(channel.id, 'tvg_only')}
                disabled={!channel.tvg_id}
                title={!channel.tvg_id ? 'No TVG-ID set' : 'Preview only TVG-ID matches (ignoring profile/regex)'}
              >
                <Eye className="h-4 w-4 mr-2" />
                Preview Results
              </Button>
            </div>

            <div className="flex justify-between items-center mb-3">
              <h4 className="font-medium text-sm flex items-center gap-2">
                Regex Patterns
                {isRegexChannelOverride ? (
                  <Badge variant="default" className="text-[10px]">Override</Badge>
                ) : isRegexGroupBased ? (
                  <Badge variant="outline" className="text-[10px]">From Group</Badge>
                ) : null}
              </h4>
              <Button size="sm" variant="outline" onClick={() => onEditRegex(channel.id, null)}>
                <Plus className="h-4 w-4 mr-2" />
                Add Pattern
              </Button>
            </div>

            {(() => {
              const normalizedPatterns = normalizePatternData(channelPatterns)
              return normalizedPatterns.length > 0 ? (
                <div className="space-y-2">
                  {normalizedPatterns.map((patternObj, index) => {
                    const accountNames =
                      patternObj.m3u_accounts && patternObj.m3u_accounts.length > 0
                        ? patternObj.m3u_accounts
                            .map((id) => {
                              const account = m3uAccounts?.find((item) => item.id === id)
                              return account ? account.name : `Account ${id}`
                            })
                            .join(', ')
                        : 'All M3U Accounts'

                    return (
                      <div key={index} className="space-y-1">
                        <div className="flex items-center justify-between gap-2 p-2 bg-background rounded-md">
                          <div className="flex-1 space-y-1">
                            <code className="text-sm break-all">{patternObj.pattern}</code>
                            <div className="text-xs text-muted-foreground">M3U Sources: {accountNames}</div>
                          </div>
                          <div className="flex gap-1">
                            <Button size="sm" variant="ghost" onClick={() => onEditRegex(channel.id, index)}>
                              <Edit className="h-4 w-4" />
                            </Button>
                            <Button size="sm" variant="ghost" onClick={() => onDeletePattern(channel.id, index)}>
                              <Trash2 className="h-4 w-4" />
                            </Button>
                          </div>
                        </div>
                      </div>
                    )
                  })}
                </div>
              ) : (
                <p className="text-sm text-muted-foreground">No regex patterns configured</p>
              )
            })()}
          </div>
        </div>
      )}
    </div>
  )
}
