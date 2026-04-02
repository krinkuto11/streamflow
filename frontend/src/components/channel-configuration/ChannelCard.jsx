import { useEffect, useState } from 'react'
import { Card, CardContent } from '@/components/ui/card.jsx'
import { Button } from '@/components/ui/button.jsx'
import { Label } from '@/components/ui/label.jsx'
import { Badge } from '@/components/ui/badge.jsx'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select.jsx'
import { useToast } from '@/hooks/use-toast.js'
import { channelsAPI, automationAPI } from '@/services/api.js'
import {
  getCachedChannelLogoUrl,
  getCachedChannelStats,
  setCachedChannelLogoUrl,
  setCachedChannelStats,
} from '@/services/channelCache.js'
import { CheckCircle, Edit, Plus, Trash2, Loader2, X, Activity } from 'lucide-react'
import { normalizePatternData } from '@/components/channel-configuration/patternUtils.js'
import { AssignPeriodsDialog } from '@/components/channel-configuration/PeriodDialogs.jsx'

export function ChannelCard({
  channel,
  patterns,
  onEditRegex,
  onDeletePattern,
  onCheckChannel,
  loading,
  channelSettings,
  onUpdateSettings,
}) {
  const [stats, setStats] = useState(null)
  const [loadingStats, setLoadingStats] = useState(true)
  const [expanded, setExpanded] = useState(false)
  const [logoUrl, setLogoUrl] = useState(null)
  const [logoError, setLogoError] = useState(false)
  const [automationPeriods, setAutomationPeriods] = useState([])
  const [loadingPeriods, setLoadingPeriods] = useState(false)
  const [assignPeriodsDialogOpen, setAssignPeriodsDialogOpen] = useState(false)
  const { toast } = useToast()

  const matchingMode = channelSettings?.matching_mode || 'enabled'
  const checkingMode = channelSettings?.checking_mode || 'enabled'
  const matchingModeSource = channelSettings?.matching_mode_source || 'default'
  const checkingModeSource = channelSettings?.checking_mode_source || 'default'
  const isMatchingInherited = matchingModeSource === 'group'
  const isCheckingInherited = checkingModeSource === 'group'

  const handleMatchingModeChange = async (value) => {
    try {
      await onUpdateSettings(channel.id, { matching_mode: value })
      toast({
        title: 'Success',
        description: 'Matching mode updated successfully',
      })
    } catch {
      toast({
        title: 'Error',
        description: 'Failed to update matching mode',
        variant: 'destructive',
      })
    }
  }

  const handleCheckingModeChange = async (value) => {
    try {
      await onUpdateSettings(channel.id, { checking_mode: value })
      toast({
        title: 'Success',
        description: 'Checking mode updated successfully',
      })
    } catch {
      toast({
        title: 'Error',
        description: 'Failed to update checking mode',
        variant: 'destructive',
      })
    }
  }

  useEffect(() => {
    const cachedStats = getCachedChannelStats(channel.id)
    if (cachedStats) {
      setStats(cachedStats)
      setLoadingStats(false)
    }
    loadStats()
  }, [channel.id])

  useEffect(() => {
    const loadLogo = () => {
      const cachedLogo = getCachedChannelLogoUrl(channel.id)
      if (cachedLogo) {
        setLogoUrl(cachedLogo)
      }

      if (channel.logo_id) {
        const channelLogoUrl = channelsAPI.getLogoCached(channel.logo_id)
        setLogoUrl(channelLogoUrl)
        setCachedChannelLogoUrl(channel.id, channelLogoUrl)
      }
    }
    loadLogo()
  }, [channel.id, channel.logo_id])

  useEffect(() => {
    if (expanded) {
      loadAutomationPeriods()
    }
  }, [expanded, channel.id])

  const loadAutomationPeriods = async () => {
    try {
      setLoadingPeriods(true)
      const response = await automationAPI.getChannelPeriods(channel.id)
      const periodToProfile = response.data || {}

      const periodsResponse = await automationAPI.getPeriods()
      const profilesResponse = await automationAPI.getProfiles()

      const allPeriods = periodsResponse.data || []
      const allProfiles = profilesResponse.data || []

      const enrichedPeriods = Object.entries(periodToProfile).map(([periodId, profileId]) => {
        const period = allPeriods.find((p) => p.id === String(periodId))
        const profile = allProfiles.find((p) => p.id === String(profileId))
        return {
          id: String(periodId),
          name: period?.name || 'Unknown Period',
          schedule: period?.schedule || {},
          profile_id: String(profileId),
          profile_name: profile?.name || 'Unknown Profile',
        }
      })

      setAutomationPeriods(enrichedPeriods)
    } catch (err) {
      console.error('Failed to load automation periods:', err)
    } finally {
      setLoadingPeriods(false)
    }
  }

  const loadStats = async () => {
    try {
      setLoadingStats(true)
      const response = await channelsAPI.getChannelStats(channel.id)
      setStats(response.data)
      setCachedChannelStats(channel.id, response.data)
    } catch (err) {
      console.error('Failed to load channel stats:', err)
    } finally {
      setLoadingStats(false)
    }
  }

  const channelPatterns = patterns[channel.id] || patterns[String(channel.id)]

  return (
    <Card className="w-full">
      <CardContent className="p-0">
        <div className="flex items-center gap-3 p-3">
          <div className="w-24 h-12 flex-shrink-0 bg-muted rounded-md flex items-center justify-center overflow-hidden">
            {logoUrl && !logoError ? (
              <img
                src={logoUrl}
                alt={channel.name}
                className="w-full h-full object-contain"
                onError={() => setLogoError(true)}
              />
            ) : (
              <span className="text-2xl font-bold text-muted-foreground">{channel.name?.charAt(0) || '?'}</span>
            )}
          </div>

          <div className="flex-1 min-w-0">
            <h3 className="font-semibold text-base truncate">{channel.name}</h3>
            <div className="flex flex-wrap gap-3 mt-1 text-sm">
              {loadingStats ? (
                <span className="text-muted-foreground">Loading stats...</span>
              ) : stats ? (
                <>
                  <div className="flex items-center gap-1">
                    <span className="text-muted-foreground">Streams:</span>
                    <span className="font-medium">{stats.total_streams ?? 0}</span>
                  </div>
                  <div className="flex items-center gap-1">
                    <span className="text-muted-foreground">Dead:</span>
                    <span className="font-medium text-destructive">{stats.dead_streams ?? 0}</span>
                  </div>
                  <div className="flex items-center gap-1">
                    <span className="text-muted-foreground">Avg.Resolution:</span>
                    <span className="font-medium">{stats.most_common_resolution || 'Unknown'}</span>
                  </div>
                  <div className="flex items-center gap-1">
                    <span className="text-muted-foreground">Avg Bitrate:</span>
                    <span className="font-medium">
                      {stats.average_bitrate > 0 ? `${stats.average_bitrate} Kbps` : 'N/A'}
                    </span>
                  </div>
                </>
              ) : (
                <>
                  <div className="flex items-center gap-1">
                    <span className="text-muted-foreground">Streams:</span>
                    <span className="font-medium text-muted-foreground">--</span>
                  </div>
                  <div className="flex items-center gap-1">
                    <span className="text-muted-foreground">Dead:</span>
                    <span className="font-medium text-muted-foreground">--</span>
                  </div>
                  <div className="flex items-center gap-1">
                    <span className="text-muted-foreground">Avg.Resolution:</span>
                    <span className="font-medium text-muted-foreground">--</span>
                  </div>
                  <div className="flex items-center gap-1">
                    <span className="text-muted-foreground">Avg Bitrate:</span>
                    <span className="font-medium text-muted-foreground">--</span>
                  </div>
                </>
              )}
            </div>
          </div>

          <div className="flex gap-2 flex-shrink-0">
            <Button variant="outline" size="sm" onClick={() => setExpanded(!expanded)}>
              <Edit className="h-4 w-4 mr-2" />
              Edit Regex
            </Button>
            <Button size="sm" onClick={() => onCheckChannel(channel.id)} disabled={loading}>
              {loading ? <Loader2 className="h-4 w-4 mr-2 animate-spin" /> : <CheckCircle className="h-4 w-4 mr-2" />}
              Check Channel
            </Button>
          </div>
        </div>

        {expanded && (
          <div className="border-t p-4 bg-muted/50 space-y-4">
            <div className="grid grid-cols-2 gap-4 pb-4 border-b">
              <div className="space-y-2">
                <Label htmlFor={`matching-mode-${channel.id}`} className="text-sm font-medium">
                  Stream Matching
                  {isMatchingInherited && <Badge variant="outline" className="ml-2 text-xs">From Group</Badge>}
                </Label>
                <Select value={matchingMode} onValueChange={handleMatchingModeChange}>
                  <SelectTrigger id={`matching-mode-${channel.id}`}>
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="enabled">Enabled</SelectItem>
                    <SelectItem value="disabled">Disabled</SelectItem>
                  </SelectContent>
                </Select>
                <p className="text-xs text-muted-foreground">
                  {matchingMode === 'enabled'
                    ? 'Channel will be included in stream matching'
                    : 'Channel will be excluded from stream matching'}
                  {isMatchingInherited && ' (inherited from group)'}
                </p>
              </div>
              <div className="space-y-2">
                <Label htmlFor={`checking-mode-${channel.id}`} className="text-sm font-medium">
                  Stream Checking
                  {isCheckingInherited && <Badge variant="outline" className="ml-2 text-xs">From Group</Badge>}
                </Label>
                <Select value={checkingMode} onValueChange={handleCheckingModeChange}>
                  <SelectTrigger id={`checking-mode-${channel.id}`}>
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="enabled">Enabled</SelectItem>
                    <SelectItem value="disabled">Disabled</SelectItem>
                  </SelectContent>
                </Select>
                <p className="text-xs text-muted-foreground">
                  {checkingMode === 'enabled'
                    ? 'Channel streams will be quality checked'
                    : 'Channel streams will not be quality checked'}
                  {isCheckingInherited && ' (inherited from group)'}
                </p>
              </div>
            </div>

            <div className="space-y-3 pb-4 border-b">
              <div className="flex justify-between items-center">
                <div>
                  <h4 className="font-medium text-sm">Automation Periods</h4>
                  <p className="text-xs text-muted-foreground">Assigned automation periods for this channel</p>
                </div>
                <Button size="sm" variant="outline" onClick={() => setAssignPeriodsDialogOpen(true)}>
                  <Plus className="h-4 w-4 mr-2" />
                  Assign Periods
                </Button>
              </div>

              {loadingPeriods ? (
                <div className="flex items-center justify-center py-4">
                  <Loader2 className="h-4 w-4 animate-spin text-muted-foreground" />
                </div>
              ) : automationPeriods.length > 0 ? (
                <div className="space-y-2">
                  {automationPeriods.map((period) => (
                    <div key={period.id} className="flex items-center justify-between p-2 bg-background rounded-md border">
                      <div className="flex-1">
                        <div className="flex items-center gap-2">
                          <span className="font-medium text-sm">{period.name}</span>
                          <Badge variant="secondary" className="text-xs">
                            {period.profile_name || 'Unknown Profile'}
                          </Badge>
                        </div>
                        <p className="text-xs text-muted-foreground mt-0.5">
                          {period.schedule?.type === 'interval'
                            ? `Every ${period.schedule.value} minutes`
                            : `Cron: ${period.schedule?.value || 'Not configured'}`}
                        </p>
                      </div>
                      <Button
                        size="sm"
                        variant="ghost"
                        onClick={async () => {
                          try {
                            await automationAPI.removePeriodFromChannels(period.id, [channel.id])
                            toast({
                              title: 'Success',
                              description: 'Period removed from channel',
                            })
                            loadAutomationPeriods()
                          } catch {
                            toast({
                              title: 'Error',
                              description: 'Failed to remove period',
                              variant: 'destructive',
                            })
                          }
                        }}
                      >
                        <X className="h-4 w-4" />
                      </Button>
                    </div>
                  ))}
                </div>
              ) : (
                <div className="text-center py-6 border rounded-lg bg-muted/50">
                  <Activity className="h-8 w-8 mx-auto mb-2 opacity-50 text-muted-foreground" />
                  <p className="text-sm text-muted-foreground mb-2">No automation periods assigned</p>
                  <p className="text-xs text-muted-foreground mb-3">This channel will not participate in automation</p>
                  <Button size="sm" variant="outline" onClick={() => setAssignPeriodsDialogOpen(true)}>
                    <Plus className="h-4 w-4 mr-2" />
                    Assign Periods
                  </Button>
                </div>
              )}
            </div>

            <div>
              <div className="flex justify-between items-center mb-3">
                <h4 className="font-medium text-sm">Regex Patterns</h4>
                <Button size="sm" variant="outline" onClick={() => onEditRegex(channel.id, null)}>
                  <Plus className="h-4 w-4 mr-2" />
                  Add Pattern
                </Button>
              </div>

              {(() => {
                const normalizedPatterns = normalizePatternData(channelPatterns)
                return normalizedPatterns.length > 0 ? (
                  <div className="space-y-2">
                    {normalizedPatterns.map((patternObj, index) => (
                      <div key={index} className="flex items-center justify-between gap-2 p-2 bg-background rounded-md">
                        <code className="text-sm flex-1 break-all">{patternObj.pattern}</code>
                        <div className="flex gap-1">
                          <Button size="sm" variant="ghost" onClick={() => onEditRegex(channel.id, index)}>
                            <Edit className="h-4 w-4" />
                          </Button>
                          <Button size="sm" variant="ghost" onClick={() => onDeletePattern(channel.id, index)}>
                            <Trash2 className="h-4 w-4" />
                          </Button>
                        </div>
                      </div>
                    ))}
                  </div>
                ) : (
                  <p className="text-sm text-muted-foreground">No regex patterns configured</p>
                )
              })()}
            </div>
          </div>
        )}
      </CardContent>

      <AssignPeriodsDialog
        open={assignPeriodsDialogOpen}
        onOpenChange={setAssignPeriodsDialogOpen}
        channelId={channel.id}
        channelName={channel.name}
        onSuccess={loadAutomationPeriods}
      />
    </Card>
  )
}
