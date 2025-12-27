import { useState, useEffect, useCallback, useRef } from 'react'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card.jsx'
import { Button } from '@/components/ui/button.jsx'
import { Input } from '@/components/ui/input.jsx'
import { Label } from '@/components/ui/label.jsx'
import { Badge } from '@/components/ui/badge.jsx'
import { Checkbox } from '@/components/ui/checkbox.jsx'
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle, DialogFooter } from '@/components/ui/dialog.jsx'
import { Accordion, AccordionContent, AccordionItem, AccordionTrigger } from '@/components/ui/accordion.jsx'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select.jsx'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs.jsx'
import { Alert, AlertDescription } from '@/components/ui/alert.jsx'
import { useToast } from '@/hooks/use-toast.js'
import { channelsAPI, regexAPI, streamCheckerAPI, channelSettingsAPI, channelOrderAPI, groupSettingsAPI, profileAPI, m3uAPI } from '@/services/api.js'
import { CheckCircle, Edit, Plus, Trash2, Loader2, Search, X, Download, Upload, GripVertical, Save, RotateCcw, ArrowUpDown, MoreVertical, Eye, ChevronDown, Info, Activity } from 'lucide-react'
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuLabel, DropdownMenuSeparator, DropdownMenuTrigger, DropdownMenuCheckboxItem } from '@/components/ui/dropdown-menu.jsx'
import { Switch } from '@/components/ui/switch.jsx'
import { Tooltip, TooltipContent, TooltipTrigger, TooltipProvider } from '@/components/ui/tooltip.jsx'
import ProfileManagement from '@/components/ProfileManagement.jsx'
import {
  DndContext,
  closestCenter,
  KeyboardSensor,
  PointerSensor,
  useSensor,
  useSensors,
} from '@dnd-kit/core'
import {
  arrayMove,
  SortableContext,
  sortableKeyboardCoordinates,
  useSortable,
  verticalListSortingStrategy,
} from '@dnd-kit/sortable'
import { CSS } from '@dnd-kit/utilities'

// Constants for localStorage keys
const CHANNEL_STATS_PREFIX = 'streamflow_channel_stats_'
const CHANNEL_LOGO_PREFIX = 'streamflow_channel_logo_'

// Constants for grid layout
const REGEX_TABLE_GRID_COLS = '50px 80px 80px 1fr 200px 150px 140px'

// Constants for stream checker priorities
const BULK_HEALTH_CHECK_PRIORITY = 10

// M3U account filtering - exclude 'custom' account as it's not a real source
const CUSTOM_ACCOUNT_NAME = 'custom'

function ChannelCard({ channel, patterns, onEditRegex, onDeletePattern, onCheckChannel, loading, channelSettings, onUpdateSettings }) {
  const [stats, setStats] = useState(null)
  const [loadingStats, setLoadingStats] = useState(true)
  const [expanded, setExpanded] = useState(false)
  const [logoUrl, setLogoUrl] = useState(null)
  const [logoError, setLogoError] = useState(false)
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
        title: "Success",
        description: "Matching mode updated successfully"
      })
    } catch (err) {
      toast({
        title: "Error",
        description: "Failed to update matching mode",
        variant: "destructive"
      })
    }
  }

  const handleCheckingModeChange = async (value) => {
    try {
      await onUpdateSettings(channel.id, { checking_mode: value })
      toast({
        title: "Success",
        description: "Checking mode updated successfully"
      })
    } catch (err) {
      toast({
        title: "Error",
        description: "Failed to update checking mode",
        variant: "destructive"
      })
    }
  }

  useEffect(() => {
    // Try to load stats from localStorage first for instant display
    const cachedStats = localStorage.getItem(`${CHANNEL_STATS_PREFIX}${channel.id}`)
    if (cachedStats) {
      try {
        const parsed = JSON.parse(cachedStats)
        setStats(parsed)
        setLoadingStats(false) // Show cached data immediately
      } catch (e) {
        console.error('Failed to parse cached stats:', e)
      }
    }
    // Always fetch fresh stats in background to keep data current
    loadStats()
  }, [channel.id])

  // Fetch logo when logo_id is available
  useEffect(() => {
    const loadLogo = () => {
      // Try cached logo first from localStorage
      const cachedLogo = localStorage.getItem(`${CHANNEL_LOGO_PREFIX}${channel.id}`)
      if (cachedLogo) {
        setLogoUrl(cachedLogo)
      }
      
      // Set logo URL if logo_id is available using the cached endpoint
      // This endpoint will serve cached logos or download them on first request
      if (channel.logo_id) {
        const logoUrl = channelsAPI.getLogoCached(channel.logo_id)
        setLogoUrl(logoUrl)
        localStorage.setItem(`${CHANNEL_LOGO_PREFIX}${channel.id}`, logoUrl)
      }
    }
    loadLogo()
  }, [channel.id, channel.logo_id])

  const loadStats = async () => {
    try {
      setLoadingStats(true)
      const response = await channelsAPI.getChannelStats(channel.id)
      setStats(response.data)
      // Cache stats in localStorage
      localStorage.setItem(`${CHANNEL_STATS_PREFIX}${channel.id}`, JSON.stringify(response.data))
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
          {/* Channel Logo */}
          <div className="w-24 h-12 flex-shrink-0 bg-muted rounded-md flex items-center justify-center overflow-hidden">
            {logoUrl && !logoError ? (
              <img 
                src={logoUrl} 
                alt={channel.name} 
                className="w-full h-full object-contain"
                onError={() => setLogoError(true)}
              />
            ) : (
              <span className="text-2xl font-bold text-muted-foreground">
                {channel.name?.charAt(0) || '?'}
              </span>
            )}
          </div>

          {/* Channel Info */}
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
                    <span className="font-medium">{stats.average_bitrate > 0 ? `${stats.average_bitrate} Kbps` : 'N/A'}</span>
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

          {/* Action Buttons */}
          <div className="flex gap-2 flex-shrink-0">
            <Button
              variant="outline"
              size="sm"
              onClick={() => setExpanded(!expanded)}
            >
              <Edit className="h-4 w-4 mr-2" />
              Edit Regex
            </Button>
            <Button
              size="sm"
              onClick={() => onCheckChannel(channel.id)}
              disabled={loading}
            >
              {loading ? (
                <Loader2 className="h-4 w-4 mr-2 animate-spin" />
              ) : (
                <CheckCircle className="h-4 w-4 mr-2" />
              )}
              Check Channel
            </Button>
          </div>
        </div>

        {/* Expandable Regex Section */}
        {expanded && (
          <div className="border-t p-4 bg-muted/50 space-y-4">
            {/* Channel Settings */}
            <div className="grid grid-cols-2 gap-4 pb-4 border-b">
              <div className="space-y-2">
                <Label htmlFor={`matching-mode-${channel.id}`} className="text-sm font-medium">
                  Stream Matching
                  {isMatchingInherited && (
                    <Badge variant="outline" className="ml-2 text-xs">From Group</Badge>
                  )}
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
                  {isCheckingInherited && (
                    <Badge variant="outline" className="ml-2 text-xs">From Group</Badge>
                  )}
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

            {/* Regex Patterns */}
            <div>
              <div className="flex justify-between items-center mb-3">
                <h4 className="font-medium text-sm">Regex Patterns</h4>
                <Button
                  size="sm"
                  variant="outline"
                  onClick={() => onEditRegex(channel.id, null)}
                >
                  <Plus className="h-4 w-4 mr-2" />
                  Add Pattern
                </Button>
              </div>
            
            {channelPatterns && channelPatterns.regex && channelPatterns.regex.length > 0 ? (
              <div className="space-y-2">
                {channelPatterns.regex.map((pattern, index) => (
                  <div key={index} className="flex items-center justify-between gap-2 p-2 bg-background rounded-md">
                    <code className="text-sm flex-1 break-all">{pattern}</code>
                    <div className="flex gap-1">
                      <Button
                        size="sm"
                        variant="ghost"
                        onClick={() => onEditRegex(channel.id, index)}
                      >
                        <Edit className="h-4 w-4" />
                      </Button>
                      <Button
                        size="sm"
                        variant="ghost"
                        onClick={() => onDeletePattern(channel.id, index)}
                      >
                        <Trash2 className="h-4 w-4" />
                      </Button>
                    </div>
                  </div>
                ))}
              </div>
            ) : (
              <p className="text-sm text-muted-foreground">No regex patterns configured</p>
            )}
            </div>
          </div>
        )}
      </CardContent>
    </Card>
  )
}

function SortableChannelItem({ channel }) {
  const [logoUrl, setLogoUrl] = useState(null)
  const [logoError, setLogoError] = useState(false)
  
  const {
    attributes,
    listeners,
    setNodeRef,
    transform,
    transition,
    isDragging,
  } = useSortable({ id: channel.id })

  useEffect(() => {
    const loadLogo = () => {
      // Try cached logo first from localStorage
      const cachedLogo = localStorage.getItem(`${CHANNEL_LOGO_PREFIX}${channel.id}`)
      if (cachedLogo) {
        setLogoUrl(cachedLogo)
        return // Use cached version, don't fetch again
      }
      
      // Only fetch from API if not cached and logo_id is available
      if (channel.logo_id) {
        const logoUrl = channelsAPI.getLogoCached(channel.logo_id)
        setLogoUrl(logoUrl)
        localStorage.setItem(`${CHANNEL_LOGO_PREFIX}${channel.id}`, logoUrl)
      }
    }
    loadLogo()
  }, [channel.id, channel.logo_id])

  const style = {
    transform: CSS.Transform.toString(transform),
    transition,
    opacity: isDragging ? 0.5 : 1,
  }

  return (
    <div
      ref={setNodeRef}
      style={style}
      className={`flex items-center gap-3 p-4 bg-card border rounded-lg ${
        isDragging ? 'shadow-lg' : 'shadow-sm'
      }`}
    >
      <div
        {...attributes}
        {...listeners}
        className="cursor-grab active:cursor-grabbing touch-none"
      >
        <GripVertical className="h-5 w-5 text-muted-foreground" />
      </div>
      
      {/* Channel Logo */}
      <div className="w-20 h-10 flex-shrink-0 bg-muted rounded-md flex items-center justify-center overflow-hidden">
        {logoUrl && !logoError ? (
          <img 
            src={logoUrl} 
            alt={channel.name} 
            className="w-full h-full object-contain"
            onError={() => setLogoError(true)}
          />
        ) : (
          <span className="text-xl font-bold text-muted-foreground">
            {channel.name?.charAt(0) || '?'}
          </span>
        )}
      </div>
      
      <div className="flex-1 flex items-center gap-4">
        <Badge variant="outline" className="font-mono">
          #{channel.channel_number || 'N/A'}
        </Badge>
        <div className="flex-1 min-w-0">
          <div className="font-medium truncate">{channel.name}</div>
          <div className="text-xs text-muted-foreground">ID: {channel.id}</div>
        </div>
      </div>
    </div>
  )
}

function RegexTableRow({ channel, group, groups, patterns, channelSettings, selectedChannels, onToggleChannel, onEditRegex, onUpdateSettings, onDeletePattern, expandedRowId, onToggleExpanded, onCheckChannel, checkingChannel }) {
  const [logoUrl, setLogoUrl] = useState(null)
  const [logoError, setLogoError] = useState(false)
  const { toast } = useToast()
  
  // Use parent-controlled expanded state
  const expanded = expandedRowId === channel.id
  const isChecking = checkingChannel === channel.id
  
  const channelPatterns = patterns[channel.id] || patterns[String(channel.id)]
  const patternCount = channelPatterns?.regex?.length || 0
  const matchingMode = channelSettings?.matching_mode || 'enabled'
  const checkingMode = channelSettings?.checking_mode || 'enabled'
  const matchingModeSource = channelSettings?.matching_mode_source || 'default'
  const checkingModeSource = channelSettings?.checking_mode_source || 'default'
  const isMatchingInherited = matchingModeSource === 'group'
  const isCheckingInherited = checkingModeSource === 'group'
  
  // Load channel logo
  useEffect(() => {
    const loadLogo = () => {
      // Check localStorage cache first
      const cachedUrl = localStorage.getItem(`${CHANNEL_LOGO_PREFIX}${channel.id}`)
      if (cachedUrl) {
        setLogoUrl(cachedUrl)
        return
      }
      
      // Fetch logo if logo_id is available
      if (channel.logo_id) {
        const logoUrl = channelsAPI.getLogoCached(channel.logo_id)
        setLogoUrl(logoUrl)
        localStorage.setItem(`${CHANNEL_LOGO_PREFIX}${channel.id}`, logoUrl)
      }
    }
    loadLogo()
  }, [channel.id, channel.logo_id])
  
  const handleMatchingModeChange = async (value) => {
    try {
      await onUpdateSettings(channel.id, { matching_mode: value })
      toast({
        title: "Success",
        description: "Matching mode updated successfully"
      })
    } catch (err) {
      toast({
        title: "Error",
        description: "Failed to update matching mode",
        variant: "destructive"
      })
    }
  }
  
  const handleCheckingModeChange = async (value) => {
    try {
      await onUpdateSettings(channel.id, { checking_mode: value })
      toast({
        title: "Success",
        description: "Checking mode updated successfully"
      })
    } catch (err) {
      toast({
        title: "Error",
        description: "Failed to update checking mode",
        variant: "destructive"
      })
    }
  }
  
  return (
    <div key={channel.id}>
      <div className={`grid gap-4 p-4 hover:bg-muted/50 transition-colors`} style={{ gridTemplateColumns: REGEX_TABLE_GRID_COLS }}>
        <div className="flex items-center justify-center">
          <Checkbox
            checked={selectedChannels.has(channel.id)}
            onCheckedChange={() => onToggleChannel(channel.id)}
          />
        </div>
        <div className="flex items-center text-sm font-medium">
          {channel.channel_number || '-'}
        </div>
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
              <span className="text-lg font-bold text-muted-foreground">
                {channel.name?.charAt(0) || '?'}
              </span>
            )}
          </div>
        </div>
        <div className="flex items-center">
          <span className="font-medium truncate">{channel.name}</span>
        </div>
        <div className="flex items-center text-sm text-muted-foreground truncate">
          {group?.name || '-'}
        </div>
        <div className="flex items-center">
          {patternCount > 0 ? (
            <Badge variant="secondary">{patternCount} pattern{patternCount > 1 ? 's' : ''}</Badge>
          ) : (
            <span className="text-sm text-muted-foreground">No patterns</span>
          )}
        </div>
        <div className="flex items-center gap-2">
          <Tooltip>
            <TooltipTrigger asChild>
              <Button 
                variant="outline" 
                size="sm"
                onClick={() => onCheckChannel(channel.id)}
                disabled={isChecking}
                className="text-blue-600 dark:text-green-500 border-blue-600 dark:border-green-500 hover:bg-blue-50 dark:hover:bg-green-950"
              >
                {isChecking ? (
                  <Loader2 className="h-4 w-4 animate-spin" />
                ) : (
                  <Activity className="h-4 w-4" />
                )}
              </Button>
            </TooltipTrigger>
            <TooltipContent>
              <p>Health Check Channel</p>
            </TooltipContent>
          </Tooltip>
          <Button 
            variant="outline" 
            size="sm"
            onClick={() => onToggleExpanded(channel.id)}
          >
            <ChevronDown className={`h-4 w-4 transition-transform ${expanded ? 'rotate-180' : ''}`} />
          </Button>
        </div>
      </div>

      {/* Expanded Section */}
      {expanded && (
        <div className="border-t p-4 bg-muted/50 space-y-4">
          {/* Channel Settings */}
          <div className="grid grid-cols-2 gap-4 pb-4 border-b">
            <div className="space-y-2">
              <Label htmlFor={`matching-mode-${channel.id}`} className="text-sm font-medium">
                Stream Matching
                {isMatchingInherited && (
                  <Badge variant="outline" className="ml-2 text-xs">From Group</Badge>
                )}
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
                {isCheckingInherited && (
                  <Badge variant="outline" className="ml-2 text-xs">From Group</Badge>
                )}
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

          {/* Regex Patterns */}
          <div>
            <div className="flex justify-between items-center mb-3">
              <h4 className="font-medium text-sm">Regex Patterns</h4>
              <Button
                size="sm"
                variant="outline"
                onClick={() => onEditRegex(channel.id, null)}
              >
                <Plus className="h-4 w-4 mr-2" />
                Add Pattern
              </Button>
            </div>
          
            {channelPatterns && channelPatterns.regex && channelPatterns.regex.length > 0 ? (
              <div className="space-y-2">
                {channelPatterns.regex.map((pattern, index) => {
                  // Get M3U account names if m3u_accounts are specified
                  const accountNames = channelPatterns.m3u_accounts && channelPatterns.m3u_accounts.length > 0
                    ? channelPatterns.m3u_accounts.map(id => {
                        const acc = m3uAccounts?.find(account => account.id === id)
                        return acc ? acc.name : `Account ${id}`
                      }).join(', ')
                    : 'All M3U Accounts'
                  
                  return (
                    <div key={index} className="space-y-1">
                      <div className="flex items-center justify-between gap-2 p-2 bg-background rounded-md">
                        <div className="flex-1 space-y-1">
                          <code className="text-sm break-all">{pattern}</code>
                          <div className="text-xs text-muted-foreground">
                            M3U Sources: {accountNames}
                          </div>
                        </div>
                        <div className="flex gap-1">
                          <Button
                            size="sm"
                            variant="ghost"
                            onClick={() => onEditRegex(channel.id, index)}
                          >
                            <Edit className="h-4 w-4" />
                          </Button>
                          <Button
                            size="sm"
                            variant="ghost"
                            onClick={() => onDeletePattern(channel.id, index)}
                          >
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
            )}
          </div>
        </div>
      )}
    </div>
  )
}

function GroupCard({ group, channels, groupSettings, onUpdateSettings }) {
  const { toast } = useToast()
  const matchingMode = groupSettings?.matching_mode || 'enabled'
  const checkingMode = groupSettings?.checking_mode || 'enabled'
  
  // Count channels in this group
  const channelCount = channels.filter(ch => ch.channel_group_id === group.id).length

  const handleMatchingModeChange = async (value) => {
    try {
      await onUpdateSettings(group.id, { matching_mode: value })
      toast({
        title: "Success",
        description: "Group matching mode updated successfully"
      })
    } catch (err) {
      toast({
        title: "Error",
        description: "Failed to update matching mode",
        variant: "destructive"
      })
    }
  }

  const handleCheckingModeChange = async (value) => {
    try {
      await onUpdateSettings(group.id, { checking_mode: value })
      toast({
        title: "Success",
        description: "Group checking mode updated successfully"
      })
    } catch (err) {
      toast({
        title: "Error",
        description: "Failed to update checking mode",
        variant: "destructive"
      })
    }
  }

  const bothDisabled = matchingMode === 'disabled' && checkingMode === 'disabled'

  return (
    <Card className="w-full">
      <CardContent className="p-4">
        <div className="flex items-center gap-4">
          {/* Group Info */}
          <div className="flex-1 min-w-0">
            <h3 className="font-semibold text-base truncate">{group.name}</h3>
            <div className="flex flex-wrap gap-3 mt-1 text-sm text-muted-foreground">
              <div className="flex items-center gap-1">
                <span>Channels:</span>
                <span className="font-medium">{channelCount}</span>
              </div>
              <div className="flex items-center gap-1">
                <span>Group ID:</span>
                <span className="font-medium">{group.id}</span>
              </div>
            </div>
            {bothDisabled && (
              <p className="text-xs text-amber-600 mt-1">
                ⚠️ Channels from this group will not appear in Regex Configuration or Channel Ordering
              </p>
            )}
          </div>

          {/* Settings Controls */}
          <div className="grid grid-cols-2 gap-4 flex-shrink-0">
            <div className="space-y-2 min-w-[180px]">
              <Label htmlFor={`group-matching-${group.id}`} className="text-sm font-medium">
                Stream Matching
              </Label>
              <Select value={matchingMode} onValueChange={handleMatchingModeChange}>
                <SelectTrigger id={`group-matching-${group.id}`}>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="enabled">Enabled</SelectItem>
                  <SelectItem value="disabled">Disabled</SelectItem>
                </SelectContent>
              </Select>
            </div>
            <div className="space-y-2 min-w-[180px]">
              <Label htmlFor={`group-checking-${group.id}`} className="text-sm font-medium">
                Stream Checking
              </Label>
              <Select value={checkingMode} onValueChange={handleCheckingModeChange}>
                <SelectTrigger id={`group-checking-${group.id}`}>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="enabled">Enabled</SelectItem>
                  <SelectItem value="disabled">Disabled</SelectItem>
                </SelectContent>
              </Select>
            </div>
          </div>
        </div>
      </CardContent>
    </Card>
  )
}

// M3U Priority Management Component
function M3UPriorityManagement() {
  const [accounts, setAccounts] = useState([])
  const [globalPriorityMode, setGlobalPriorityMode] = useState('disabled')
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const { toast } = useToast()

  useEffect(() => {
    loadAccounts()
  }, [])

  const loadAccounts = async () => {
    try {
      setLoading(true)
      const response = await m3uAPI.getAccounts()
      // API returns { accounts: [], global_priority_mode: '' }
      if (response.data && Array.isArray(response.data.accounts)) {
        setAccounts(response.data.accounts)
        setGlobalPriorityMode(response.data.global_priority_mode || 'disabled')
      } else {
        // Fallback for backwards compatibility if API returns array directly
        setAccounts(Array.isArray(response.data) ? response.data : [])
      }
    } catch (err) {
      console.error('Failed to load M3U accounts:', err)
      toast({
        title: "Error",
        description: "Failed to load M3U accounts",
        variant: "destructive"
      })
    } finally {
      setLoading(false)
    }
  }

  const handlePriorityChange = async (accountId, field, value) => {
    try {
      setSaving(true)
      await m3uAPI.updateAccountPriority(accountId, { [field]: value })
      
      // Update local state
      setAccounts(prev => prev.map(acc => 
        acc.id === accountId ? { ...acc, [field]: value } : acc
      ))
      
      toast({
        title: "Success",
        description: "M3U account priority updated successfully"
      })
    } catch (err) {
      console.error('Failed to update priority:', err)
      toast({
        title: "Error",
        description: err.response?.data?.error || "Failed to update priority",
        variant: "destructive"
      })
    } finally {
      setSaving(false)
    }
  }

  const handleGlobalPriorityModeChange = async (value) => {
    try {
      setSaving(true)
      await m3uAPI.updateGlobalPriorityMode({ priority_mode: value })
      
      setGlobalPriorityMode(value)
      
      toast({
        title: "Success",
        description: "Global priority mode updated successfully"
      })
    } catch (err) {
      console.error('Failed to update global priority mode:', err)
      toast({
        title: "Error",
        description: err.response?.data?.error || "Failed to update global priority mode",
        variant: "destructive"
      })
    } finally {
      setSaving(false)
    }
  }

  if (loading) {
    return (
      <div className="flex items-center justify-center min-h-[400px]">
        <Loader2 className="h-8 w-8 animate-spin text-muted-foreground" />
      </div>
    )
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>M3U Account Priority System</CardTitle>
        <CardDescription>
          Configure stream selection priority for your M3U accounts. Higher priority accounts' streams will be preferred during stream matching.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-6">
        {accounts.length === 0 ? (
          <Alert>
            <AlertDescription>
              No enabled M3U accounts found. Add and enable M3U accounts in Dispatcharr to configure their priority.
            </AlertDescription>
          </Alert>
        ) : (
          <>
            {/* Global Priority Mode Selector */}
            <div className="space-y-3 rounded-lg border p-4 bg-muted/50">
              <Label htmlFor="global-priority-mode" className="text-base font-semibold">Global Priority Mode</Label>
              <Select
                value={globalPriorityMode}
                onValueChange={handleGlobalPriorityModeChange}
                disabled={saving}
              >
                <SelectTrigger id="global-priority-mode">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="disabled">Disabled</SelectItem>
                  <SelectItem value="same_resolution">Same Resolution Only</SelectItem>
                  <SelectItem value="all_streams">All Streams</SelectItem>
                </SelectContent>
              </Select>
              <p className="text-xs text-muted-foreground">
                {globalPriorityMode === 'disabled' && 'Priority system is disabled. Streams are selected based on quality only.'}
                {globalPriorityMode === 'same_resolution' && 'Priority applied within same resolution groups. Among streams with the same resolution, prefer streams from higher priority accounts.'}
                {globalPriorityMode === 'all_streams' && 'Priority applied to all streams. Always prefer streams from higher priority accounts, even if lower quality.'}
              </p>
            </div>

            {/* Account Priority Table */}
            <div className="space-y-4">
              <div className="rounded-lg border">
                <div className="grid grid-cols-2 gap-4 p-4 bg-muted font-medium text-sm">
                  <div>Account Name</div>
                  <div>Priority (0-100)</div>
                </div>
                {accounts.map((account) => (
                  <div key={account.id} className="grid grid-cols-2 gap-4 p-4 border-t items-center">
                    <div className="font-medium">{account.name}</div>
                    <div>
                      <Input
                        type="number"
                        min="0"
                        max="100"
                        value={account.priority || 0}
                        onChange={(e) => handlePriorityChange(account.id, 'priority', parseInt(e.target.value) || 0)}
                        disabled={saving || globalPriorityMode === 'disabled'}
                      />
                    </div>
                  </div>
                ))}
              </div>
            </div>

            <Alert>
              <AlertDescription>
                <strong>How it works:</strong>
                <ul className="list-disc list-inside mt-2 space-y-1">
                  <li><strong>Disabled:</strong> Priority values are ignored, streams are selected based on quality only</li>
                  <li><strong>Same Resolution Only:</strong> Among streams with the same resolution, prefer streams from higher priority accounts</li>
                  <li><strong>All Streams:</strong> Always prefer streams from higher priority accounts, even if lower quality</li>
                </ul>
                <p className="mt-2"><strong>Note:</strong> Only enabled M3U accounts are shown in this list.</p>
              </AlertDescription>
            </Alert>
          </>
        )}
      </CardContent>
    </Card>
  )
}

export default function ChannelConfiguration() {
  const [channels, setChannels] = useState([])
  const [patterns, setPatterns] = useState({})
  const [channelSettings, setChannelSettings] = useState({})
  const [loading, setLoading] = useState(true)
  const [checkingChannel, setCheckingChannel] = useState(null)
  const [searchQuery, setSearchQuery] = useState('')
  const [dialogOpen, setDialogOpen] = useState(false)
  const [editingChannelId, setEditingChannelId] = useState(null)
  const [editingPatternIndex, setEditingPatternIndex] = useState(null)
  const [newPattern, setNewPattern] = useState('')
  const [testingPattern, setTestingPattern] = useState(false)
  const [testResults, setTestResults] = useState(null)
  const testRequestIdRef = useRef(0)
  const { toast } = useToast()
  
  // Pagination state for Regex Configuration
  const [currentPage, setCurrentPage] = useState(1)
  const [itemsPerPage, setItemsPerPage] = useState(20)
  
  // Pagination state for Channel Order
  const [orderCurrentPage, setOrderCurrentPage] = useState(1)
  const [orderItemsPerPage, setOrderItemsPerPage] = useState(20)
  
  // Pagination state for Group Management
  const [groupCurrentPage, setGroupCurrentPage] = useState(1)
  const [groupItemsPerPage, setGroupItemsPerPage] = useState(20)
  const [groupSearchQuery, setGroupSearchQuery] = useState('')
  
  // Channel ordering state
  const [orderedChannels, setOrderedChannels] = useState([])
  const [originalChannelOrder, setOriginalChannelOrder] = useState([])
  const [hasOrderChanges, setHasOrderChanges] = useState(false)
  const [savingOrder, setSavingOrder] = useState(false)
  const [sortBy, setSortBy] = useState('custom')
  
  // Group management state
  const [groups, setGroups] = useState([])
  const [groupSettings, setGroupSettings] = useState({})
  const [pendingChanges, setPendingChanges] = useState({})
  const [activeTab, setActiveTab] = useState('regex')
  
  // Multi-select state for bulk regex assignment
  const [selectedChannels, setSelectedChannels] = useState(new Set())
  const [filterByGroup, setFilterByGroup] = useState('all')
  const [sortByGroup, setSortByGroup] = useState(false)
  const [bulkDialogOpen, setBulkDialogOpen] = useState(false)
  const [bulkPattern, setBulkPattern] = useState('')
  
  // M3U account filtering state for regex patterns
  const [m3uAccounts, setM3uAccounts] = useState([])  // All M3U accounts
  const [selectedM3uAccounts, setSelectedM3uAccounts] = useState([])  // For individual regex dialog
  const [bulkSelectedM3uAccounts, setBulkSelectedM3uAccounts] = useState([])  // For bulk regex dialog
  
  // Profile filter state
  const [profileFilterActive, setProfileFilterActive] = useState(false)
  const [profileFilterInfo, setProfileFilterInfo] = useState(null)
  
  // Expanded row state - to ensure only one action menu is open at a time
  const [expandedRowId, setExpandedRowId] = useState(null)
  
  // Bulk health check state
  const [bulkCheckingChannels, setBulkCheckingChannels] = useState(false)
  
  const sensors = useSensors(
    useSensor(PointerSensor),
    useSensor(KeyboardSensor, {
      coordinateGetter: sortableKeyboardCoordinates,
    })
  )

  useEffect(() => {
    loadData()
  }, [])

  const loadData = async () => {
    try {
      setLoading(true)
      
      // First, load profile configuration to see if we should filter channels
      const profileConfigResponse = await profileAPI.getConfig().catch((err) => {
        console.error('Failed to load profile configuration:', err)
        return { data: null }
      })
      const profileConfig = profileConfigResponse.data
      
      // Determine which channels to load
      let channelsToLoad = []
      let shouldFilterByProfile = false
      
      if (profileConfig?.use_profile && profileConfig?.selected_profile_id) {
        // User is using a specific profile - fetch only enabled channels from that profile
        shouldFilterByProfile = true
        try {
          // Include snapshot channels so disabled channels (emptied by Dispatcharr) are still shown
          // This allows users to force a channel check even if channels were auto-disabled
          const profileResponse = await profileAPI.getProfileChannels(profileConfig.selected_profile_id, true)
          const profileData = profileResponse.data
          
          // Get all channels first
          const allChannelsResponse = await channelsAPI.getChannels()
          const allChannels = allChannelsResponse.data || []
          
          // Filter to only include channels that are enabled in the profile OR in the snapshot
          // According to Dispatcharr API, profileData.channels is a list of channel IDs (integers)
          // Being in the profile means the channel is enabled (or in snapshot if include_snapshot=true)
          const enabledChannelIds = new Set()
          if (profileData && profileData.channels && Array.isArray(profileData.channels)) {
            for (const channelId of profileData.channels) {
              // Channels are just integers (channel IDs)
              if (typeof channelId === 'number') {
                enabledChannelIds.add(channelId)
              }
            }
          }
          
          channelsToLoad = allChannels.filter(ch => enabledChannelIds.has(ch.id))
          
          // Store profile filter information instead of showing toast
          if (channelsToLoad.length === 0 && allChannels.length > 0) {
            setProfileFilterActive(true)
            setProfileFilterInfo({
              profileName: profileConfig.selected_profile_name,
              channelCount: channelsToLoad.length,
              totalChannels: allChannels.length,
              isEmpty: true,
              message: `No enabled channels found in profile "${profileConfig.selected_profile_name}". Try refreshing profiles in the Profile Management section.`
            })
          } else {
            setProfileFilterActive(true)
            setProfileFilterInfo({
              profileName: profileConfig.selected_profile_name,
              channelCount: channelsToLoad.length,
              totalChannels: allChannels.length,
              isEmpty: false,
              message: `Showing ${channelsToLoad.length} of ${allChannels.length} channels from profile "${profileConfig.selected_profile_name}" (including snapshot)`
            })
          }
        } catch (err) {
          console.error('Failed to load profile channels:', err)
          setProfileFilterActive(false)
          setProfileFilterInfo(null)
          toast({
            title: "Warning",
            description: "Failed to filter by profile, showing all channels",
            variant: "destructive"
          })
          // Fall back to loading all channels
          const channelsResponse = await channelsAPI.getChannels()
          channelsToLoad = channelsResponse.data || []
        }
      } else {
        // Not using a specific profile - load all channels
        setProfileFilterActive(false)
        setProfileFilterInfo(null)
        const channelsResponse = await channelsAPI.getChannels()
        channelsToLoad = channelsResponse.data || []
      }
      
      const [patternsResponse, settingsResponse, groupsResponse, groupSettingsResponse, orderResponse, m3uAccountsResponse] = await Promise.all([
        regexAPI.getPatterns(),
        channelSettingsAPI.getAllSettings(),
        channelsAPI.getGroups(),
        groupSettingsAPI.getAllSettings(),
        channelOrderAPI.getOrder().catch(() => ({ data: { order: [] } })), // Handle case where no order is saved
        m3uAPI.getAccounts().catch(() => ({ data: { accounts: [] } })) // Load M3U accounts
      ])
      
      setChannels(channelsToLoad)
      setPatterns(patternsResponse.data.patterns || {})
      setChannelSettings(settingsResponse.data || {})
      setGroups(groupsResponse.data || [])
      setGroupSettings(groupSettingsResponse.data || {})
      
      // Set M3U accounts (filter out custom account as it's not a real source)
      const accounts = m3uAccountsResponse.data?.accounts || m3uAccountsResponse.data || []
      setM3uAccounts(accounts.filter(acc => acc.name?.toLowerCase() !== CUSTOM_ACCOUNT_NAME))
      
      // Initialize ordered channels
      const channelData = channelsToLoad
      const savedOrder = orderResponse.data?.order || []
      
      let orderedList = []
      
      if (savedOrder.length > 0) {
        // Apply saved custom order
        // Create a map for quick lookup
        const channelMap = new Map(channelData.map(ch => [ch.id, ch]))
        
        // First, add channels in the saved order (only if they're in our filtered list)
        orderedList = savedOrder
          .map(id => channelMap.get(id))
          .filter(ch => ch !== undefined) // Filter out any channels that don't exist or are filtered out
        
        // Then add any new channels that weren't in the saved order (sorted by channel number)
        const orderedIds = new Set(orderedList.map(ch => ch.id))
        const newChannels = channelData
          .filter(ch => !orderedIds.has(ch.id))
          .sort((a, b) => {
            const numA = parseFloat(a.channel_number) || 999999
            const numB = parseFloat(b.channel_number) || 999999
            return numA - numB
          })
        
        orderedList = [...orderedList, ...newChannels]
      } else {
        // No saved order, sort by channel_number
        orderedList = [...channelData].sort((a, b) => {
          const numA = parseFloat(a.channel_number) || 999999
          const numB = parseFloat(b.channel_number) || 999999
          return numA - numB
        })
      }
      
      setOrderedChannels(orderedList)
      setOriginalChannelOrder(orderedList)
      setHasOrderChanges(false)
    } catch (err) {
      console.error('Failed to load data:', err)
      toast({
        title: "Error",
        description: "Failed to load channel data",
        variant: "destructive"
      })
    } finally {
      setLoading(false)
    }
  }

  const handleUpdateSettings = async (channelId, settings) => {
    try {
      await channelSettingsAPI.updateSettings(channelId, settings)
      // Reload settings to get updated values
      const settingsResponse = await channelSettingsAPI.getAllSettings()
      setChannelSettings(settingsResponse.data || {})
    } catch (err) {
      throw err
    }
  }

  const handleUpdateGroupSettings = async (groupId, settings) => {
    try {
      await groupSettingsAPI.updateSettings(groupId, settings)
      // Reload both group and channel settings to get updated values
      // Channel settings need to be reloaded because they contain inherited values from groups
      const [groupSettingsResponse, channelSettingsResponse] = await Promise.all([
        groupSettingsAPI.getAllSettings(),
        channelSettingsAPI.getAllSettings()
      ])
      setGroupSettings(groupSettingsResponse.data || {})
      setChannelSettings(channelSettingsResponse.data || {})
    } catch (err) {
      throw err
    }
  }

  const reloadAllSettings = async () => {
    const [groupSettingsResponse, channelSettingsResponse] = await Promise.all([
      groupSettingsAPI.getAllSettings(),
      channelSettingsAPI.getAllSettings()
    ])
    setGroupSettings(groupSettingsResponse.data || {})
    setChannelSettings(channelSettingsResponse.data || {})
  }

  const handleBulkDisableMatching = async () => {
    try {
      const response = await groupSettingsAPI.bulkDisableMatching()
      toast({
        title: "Success",
        description: response.data.message || "Disabled matching for all groups",
      })
      // Reload both group and channel settings
      await reloadAllSettings()
    } catch (err) {
      console.error('Failed to bulk disable matching:', err)
      toast({
        title: "Error",
        description: err.response?.data?.error || "Failed to disable matching for all groups",
        variant: "destructive"
      })
    }
  }

  const handleBulkDisableChecking = async () => {
    try {
      const response = await groupSettingsAPI.bulkDisableChecking()
      toast({
        title: "Success",
        description: response.data.message || "Disabled checking for all groups",
      })
      // Reload both group and channel settings
      await reloadAllSettings()
    } catch (err) {
      console.error('Failed to bulk disable checking:', err)
      toast({
        title: "Error",
        description: err.response?.data?.error || "Failed to disable checking for all groups",
        variant: "destructive"
      })
    }
  }

  const handleCheckChannel = async (channelId) => {
    try {
      setCheckingChannel(channelId)
      
      // Show starting notification
      toast({
        title: "Channel Check Started",
        description: "Checking channel streams... This may take a few minutes.",
      })
      
      const response = await streamCheckerAPI.checkSingleChannel(channelId)
      
      if (response.data.success) {
        const stats = response.data.stats
        toast({
          title: "Channel Check Complete",
          description: `Checked ${stats.total_streams} streams. Dead: ${stats.dead_streams}. Avg Resolution: ${stats.avg_resolution}, Avg Bitrate: ${stats.avg_bitrate}`,
        })
        // Reload the channel data to show updated stats
        loadData()
      } else {
        toast({
          title: "Check Failed",
          description: response.data.error || "Failed to check channel",
          variant: "destructive"
        })
      }
    } catch (err) {
      console.error('Error checking channel:', err)
      // Check if it's a timeout error
      if (err.code === 'ECONNABORTED' || err.message?.includes('timeout')) {
        toast({
          title: "Check Taking Longer Than Expected",
          description: "The channel check is still running. Please check back in a few minutes.",
          variant: "default"
        })
      } else {
        toast({
          title: "Error",
          description: err.response?.data?.error || "Failed to check channel",
          variant: "destructive"
        })
      }
    } finally {
      setCheckingChannel(null)
    }
  }

  const handleBulkHealthCheck = async () => {
    if (selectedChannels.size === 0) {
      toast({
        title: "No Channels Selected",
        description: "Please select at least one channel",
        variant: "destructive"
      })
      return
    }
    
    try {
      setBulkCheckingChannels(true)
      
      // Show starting notification
      toast({
        title: "Bulk Health Check Started",
        description: `Queuing ${selectedChannels.size} channel${selectedChannels.size !== 1 ? 's' : ''} for checking...`,
      })
      
      const response = await streamCheckerAPI.addToQueue({
        channel_ids: Array.from(selectedChannels),
        priority: BULK_HEALTH_CHECK_PRIORITY,
        force_check: true  // Enable force check to bypass 2-hour immunity
      })
      
      toast({
        title: "Channels Queued",
        description: response.data.message || `${selectedChannels.size} channel${selectedChannels.size !== 1 ? 's' : ''} queued for health check`,
      })
    } catch (err) {
      console.error('Error queuing channels for health check:', err)
      toast({
        title: "Error",
        description: err.response?.data?.error || "Failed to queue channels for health check",
        variant: "destructive"
      })
    } finally {
      setBulkCheckingChannels(false)
    }
  }


  const handleEditRegex = (channelId, patternIndex) => {
    setEditingChannelId(channelId)
    setEditingPatternIndex(patternIndex)
    
    // If editing an existing pattern, load it
    if (patternIndex !== null) {
      const channelPatterns = patterns[channelId] || patterns[String(channelId)]
      if (channelPatterns && channelPatterns.regex && channelPatterns.regex[patternIndex]) {
        setNewPattern(channelPatterns.regex[patternIndex])
      }
      // Load existing M3U account selection (if any)
      if (channelPatterns && channelPatterns.m3u_accounts) {
        setSelectedM3uAccounts(channelPatterns.m3u_accounts)
      } else {
        setSelectedM3uAccounts([])  // Empty means all M3U accounts
      }
    } else {
      setNewPattern('')
      setSelectedM3uAccounts([])  // Default to all M3U accounts for new patterns
    }
    
    setTestResults(null)
    setDialogOpen(true)
  }

  const handleCloseDialog = () => {
    setDialogOpen(false)
    setEditingChannelId(null)
    setEditingPatternIndex(null)
    setNewPattern('')
    setTestResults(null)
    setSelectedM3uAccounts([])  // Reset M3U account selection
  }

  const handleTestPattern = useCallback(async () => {
    if (!newPattern.trim() || !editingChannelId) return
    
    // Increment request ID to track this request
    const requestId = ++testRequestIdRef.current
    
    try {
      setTestingPattern(true)
      const channel = channels.find(ch => ch.id === editingChannelId)
      const response = await regexAPI.testPatternLive({
        patterns: [{
          channel_id: editingChannelId,
          channel_name: channel?.name || '',
          regex: [newPattern],
          m3u_accounts: selectedM3uAccounts.length > 0 ? selectedM3uAccounts : undefined
        }],
        max_matches: 50
      })
      
      // Only update state if this is still the latest request
      if (requestId !== testRequestIdRef.current) return
      
      // Extract results for this channel
      const result = response.data.results?.[0]
      if (result) {
        setTestResults({
          valid: true,
          matches: result.matched_streams?.map(s => s.stream_name) || [],
          match_count: result.match_count || 0
        })
      } else {
        setTestResults({ valid: true, matches: [], match_count: 0 })
      }
    } catch (err) {
      // Only update state if this is still the latest request
      if (requestId !== testRequestIdRef.current) return
      
      // Check if it's a validation error
      if (err.response?.data?.error) {
        setTestResults({
          valid: false,
          error: err.response.data.error
        })
      } else {
        toast({
          title: "Error",
          description: "Failed to test pattern",
          variant: "destructive"
        })
      }
    } finally {
      // Only update loading state if this is still the latest request
      if (requestId === testRequestIdRef.current) {
        setTestingPattern(false)
      }
    }
  }, [newPattern, editingChannelId, channels, selectedM3uAccounts, toast])

  // Test pattern on every change with debouncing
  useEffect(() => {
    if (!newPattern || !editingChannelId || !dialogOpen) {
      setTestResults(null)
      return
    }

    const timer = setTimeout(() => {
      handleTestPattern()
    }, 500) // 500ms debounce
    
    return () => clearTimeout(timer)
    // Only depend on the actual values, not the function
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [newPattern, editingChannelId, dialogOpen, selectedM3uAccounts])

  const handleSavePattern = async () => {
    if (!newPattern.trim() || !editingChannelId) {
      toast({
        title: "Error",
        description: "Pattern cannot be empty",
        variant: "destructive"
      })
      return
    }

    try {
      const channelPatterns = patterns[editingChannelId] || patterns[String(editingChannelId)]
      const channel = channels.find(ch => ch.id === editingChannelId)
      
      let updatedRegex = []
      if (editingPatternIndex !== null && channelPatterns?.regex) {
        // Editing existing pattern
        updatedRegex = [...channelPatterns.regex]
        updatedRegex[editingPatternIndex] = newPattern
      } else {
        // Adding new pattern
        updatedRegex = channelPatterns?.regex ? [...channelPatterns.regex, newPattern] : [newPattern]
      }

      await regexAPI.addPattern({
        channel_id: editingChannelId,
        name: channel?.name || '',
        regex: updatedRegex,
        enabled: channelPatterns?.enabled !== false,
        m3u_accounts: selectedM3uAccounts.length > 0 ? selectedM3uAccounts : null  // null = all M3U accounts
      })

      toast({
        title: "Success",
        description: editingPatternIndex !== null ? "Pattern updated successfully" : "Pattern added successfully"
      })

      // Reload patterns
      await loadData()
      handleCloseDialog()
    } catch (err) {
      toast({
        title: "Error",
        description: "Failed to save pattern",
        variant: "destructive"
      })
    }
  }

  const handleDeletePattern = async (channelId, patternIndex) => {
    try {
      const channelPatterns = patterns[channelId] || patterns[String(channelId)]
      const channel = channels.find(ch => ch.id === channelId)
      
      if (!channelPatterns?.regex) return

      const updatedRegex = channelPatterns.regex.filter((_, index) => index !== patternIndex)
      
      if (updatedRegex.length === 0) {
        // If no patterns left, delete the entire pattern config
        await regexAPI.deletePattern(channelId)
      } else {
        // Update with remaining patterns, preserving playlist configuration
        await regexAPI.addPattern({
          channel_id: channelId,
          name: channel?.name || '',
          regex: updatedRegex,
          enabled: channelPatterns.enabled !== false,
          m3u_accounts: channelPatterns.m3u_accounts  // Preserve M3U account filtering
        })
      }

      toast({
        title: "Success",
        description: "Pattern deleted successfully"
      })

      await loadData()
    } catch (err) {
      toast({
        title: "Error",
        description: "Failed to delete pattern",
        variant: "destructive"
      })
    }
  }
  
  // Bulk assignment handlers
  const handleToggleChannel = (channelId) => {
    setSelectedChannels(prev => {
      const newSet = new Set(prev)
      if (newSet.has(channelId)) {
        newSet.delete(channelId)
      } else {
        newSet.add(channelId)
      }
      return newSet
    })
  }
  
  const handleSelectAll = () => {
    const visibleChannelIds = filteredChannels.map(ch => ch.id)
    setSelectedChannels(new Set(visibleChannelIds))
  }
  
  const handleDeselectAll = () => {
    setSelectedChannels(new Set())
  }
  
  const handleBulkAddPattern = async () => {
    if (selectedChannels.size === 0) {
      toast({
        title: "No Channels Selected",
        description: "Please select at least one channel",
        variant: "destructive"
      })
      return
    }
    
    if (!bulkPattern.trim()) {
      toast({
        title: "No Pattern Provided",
        description: "Please enter a regex pattern",
        variant: "destructive"
      })
      return
    }
    
    try {
      const response = await regexAPI.bulkAddPatterns({
        channel_ids: Array.from(selectedChannels),
        regex_patterns: [bulkPattern],
        m3u_accounts: bulkSelectedM3uAccounts.length > 0 ? bulkSelectedM3uAccounts : null  // null = all M3U accounts
      })
      
      toast({
        title: "Success",
        description: response.data.message || `Added pattern to ${response.data.success_count} channels`,
      })
      
      // Reload data and clear selection
      await loadData()
      setSelectedChannels(new Set())
      setBulkDialogOpen(false)
      setBulkPattern('')
      setBulkSelectedM3uAccounts([])  // Reset bulk M3U account selection
    } catch (err) {
      toast({
        title: "Error",
        description: err.response?.data?.error || "Failed to add patterns",
        variant: "destructive"
      })
    }
  }

  // Handler for toggling expanded row - ensures only one row is expanded at a time
  const handleToggleExpanded = (channelId) => {
    setExpandedRowId(prevId => prevId === channelId ? null : channelId)
  }

  // Helper function to check if channel should be visible based on group settings
  const isChannelVisibleByGroup = (channel) => {
    // If channel has no group, it's visible
    if (!channel.channel_group_id) return true
    
    // Get group settings
    const groupSetting = groupSettings[channel.channel_group_id]
    if (!groupSetting) return true // No settings means visible
    
    // Channel is hidden if both matching and checking are disabled for its group
    const matchingDisabled = groupSetting.matching_mode === 'disabled'
    const checkingDisabled = groupSetting.checking_mode === 'disabled'
    
    return !(matchingDisabled && checkingDisabled)
  }

  // Filter channels based on search query, group settings, and group filter
  // Use orderedChannels as the base to ensure consistent ordering between tabs
  const filteredChannels = orderedChannels.filter(channel => {
    // First check group visibility
    if (!isChannelVisibleByGroup(channel)) return false
    
    // Apply group filter
    if (filterByGroup !== 'all' && channel.channel_group_id !== parseInt(filterByGroup)) {
      return false
    }
    
    // Then apply search filter
    if (!searchQuery.trim()) return true
    
    const query = searchQuery.toLowerCase()
    const channelName = (channel.name || '').toLowerCase()
    const channelNumber = channel.channel_number ? String(channel.channel_number) : ''
    const channelId = String(channel.id)
    
    // Get group name for search
    const group = groups.find(g => g.id === channel.channel_group_id)
    const groupName = group ? group.name.toLowerCase() : ''
    
    return channelName.includes(query) || 
           channelNumber.includes(query) || 
           channelId.includes(query) ||
           groupName.includes(query)
  })
  
  // Sort by group if enabled
  const displayChannels = sortByGroup 
    ? [...filteredChannels].sort((a, b) => {
        const groupA = groups.find(g => g.id === a.channel_group_id)?.name || ''
        const groupB = groups.find(g => g.id === b.channel_group_id)?.name || ''
        return groupA.localeCompare(groupB) || (a.channel_number || 0) - (b.channel_number || 0)
      })
    : filteredChannels

  // Filter ordered channels based on group settings
  const visibleOrderedChannels = orderedChannels.filter(isChannelVisibleByGroup)

  // Calculate pagination for Regex Configuration
  const totalPages = Math.ceil(displayChannels.length / itemsPerPage)
  const startIndex = (currentPage - 1) * itemsPerPage
  const endIndex = startIndex + itemsPerPage
  const paginatedChannels = displayChannels.slice(startIndex, endIndex)
  
  // Calculate pagination for Channel Order
  const orderTotalPages = Math.ceil(visibleOrderedChannels.length / orderItemsPerPage)
  const orderStartIndex = (orderCurrentPage - 1) * orderItemsPerPage
  const orderEndIndex = orderStartIndex + orderItemsPerPage
  const paginatedOrderedChannels = visibleOrderedChannels.slice(orderStartIndex, orderEndIndex)
  
  // Filter groups for Group Management search
  const filteredGroups = groups.filter(group => {
    if (!groupSearchQuery.trim()) return true
    
    const query = groupSearchQuery.toLowerCase()
    const groupName = (group.name || '').toLowerCase()
    const groupId = String(group.id)
    
    return groupName.includes(query) || groupId.includes(query)
  })
  
  // Calculate pagination for Group Management
  const groupTotalPages = Math.ceil(filteredGroups.length / groupItemsPerPage)
  const groupStartIndex = (groupCurrentPage - 1) * groupItemsPerPage
  const groupEndIndex = groupStartIndex + groupItemsPerPage
  const paginatedGroups = filteredGroups.slice(groupStartIndex, groupEndIndex)

  // Reset to first page when search changes
  useEffect(() => {
    setCurrentPage(1)
  }, [searchQuery, filterByGroup, sortByGroup])

  // Reset to first page when group search changes
  useEffect(() => {
    setGroupCurrentPage(1)
  }, [groupSearchQuery])

  const clearSearch = () => {
    setSearchQuery('')
  }
  
  // Channel ordering handlers
  const handleDragEnd = (event) => {
    const { active, over } = event

    if (over && active.id !== over.id) {
      setOrderedChannels((items) => {
        const oldIndex = items.findIndex((item) => item.id === active.id)
        const newIndex = items.findIndex((item) => item.id === over.id)
        const newOrder = arrayMove(items, oldIndex, newIndex)
        setHasOrderChanges(true)
        return newOrder
      })
    }
  }

  const handleSort = (value) => {
    setSortBy(value)
    
    let sorted = [...orderedChannels]
    
    switch (value) {
      case 'channel_number':
        sorted.sort((a, b) => {
          const numA = parseFloat(a.channel_number) || 999999
          const numB = parseFloat(b.channel_number) || 999999
          return numA - numB
        })
        break
      case 'name':
        sorted.sort((a, b) => a.name.localeCompare(b.name))
        break
      case 'id':
        sorted.sort((a, b) => a.id - b.id)
        break
      case 'custom':
        // Keep current order
        return
    }
    
    setOrderedChannels(sorted)
    setHasOrderChanges(JSON.stringify(sorted) !== JSON.stringify(originalChannelOrder))
  }

  const handleSaveOrder = async () => {
    try {
      setSavingOrder(true)
      
      // Create order array with channel IDs
      const order = orderedChannels.map(ch => ch.id)
      
      await channelOrderAPI.setOrder(order)
      
      setOriginalChannelOrder([...orderedChannels])
      setHasOrderChanges(false)
      
      toast({
        title: "Success",
        description: "Channel order saved successfully"
      })
    } catch (err) {
      console.error('Failed to save order:', err)
      toast({
        title: "Error",
        description: "Failed to save channel order",
        variant: "destructive"
      })
    } finally {
      setSavingOrder(false)
    }
  }

  const handleResetOrder = () => {
    setOrderedChannels([...originalChannelOrder])
    setHasOrderChanges(false)
    setSortBy('custom')
    toast({
      title: "Reset",
      description: "Changes have been discarded"
    })
  }

  const handleExportPatterns = () => {
    try {
      // Create a JSON blob with the current patterns
      const dataStr = JSON.stringify({ patterns }, null, 2)
      const dataBlob = new Blob([dataStr], { type: 'application/json' })
      
      // Create download link
      const url = URL.createObjectURL(dataBlob)
      const link = document.createElement('a')
      link.href = url
      link.download = `channel_regex_patterns_${new Date().toISOString().split('T')[0]}.json`
      document.body.appendChild(link)
      link.click()
      document.body.removeChild(link)
      URL.revokeObjectURL(url)
      
      toast({
        title: "Success",
        description: "Regex patterns exported successfully"
      })
    } catch (err) {
      console.error('Export error:', err)
      toast({
        title: "Error",
        description: "Failed to export patterns",
        variant: "destructive"
      })
    }
  }

  const handleImportPatterns = (event) => {
    const file = event.target.files[0]
    if (!file) return
    
    const reader = new FileReader()
    reader.onload = async (e) => {
      try {
        const importedData = JSON.parse(e.target.result)
        
        // Validate the imported data structure
        if (!importedData.patterns || typeof importedData.patterns !== 'object') {
          throw new Error('Invalid file format: missing patterns object')
        }
        
        // Import the patterns using the API
        await regexAPI.importPatterns(importedData)
        
        toast({
          title: "Success",
          description: `Imported ${Object.keys(importedData.patterns).length} channel patterns`
        })
        
        // Reload data to show imported patterns
        await loadData()
      } catch (err) {
        console.error('Import error:', err)
        toast({
          title: "Error",
          description: err.response?.data?.error || "Failed to import patterns",
          variant: "destructive"
        })
      }
    }
    
    reader.onerror = () => {
      toast({
        title: "Error",
        description: "Failed to read file",
        variant: "destructive"
      })
    }
    
    reader.readAsText(file)
    
    // Reset the input so the same file can be imported again
    event.target.value = ''
  }

  if (loading) {
    return (
      <div className="flex items-center justify-center min-h-[400px]">
        <Loader2 className="h-8 w-8 animate-spin text-primary" />
      </div>
    )
  }

  return (
    <TooltipProvider>
      <div className="space-y-6">
        <div>
          <h1 className="text-3xl font-bold tracking-tight">Channel Configuration</h1>
          <p className="text-muted-foreground">
            View and manage channel regex patterns, settings, and ordering
          </p>
        </div>

      <Tabs value={activeTab} onValueChange={setActiveTab} className="w-full">
        <TabsList className="grid w-full grid-cols-5">
          <TabsTrigger value="regex">Regex Configuration</TabsTrigger>
          <TabsTrigger value="groups">Group Management</TabsTrigger>
          <TabsTrigger value="ordering">Channel Order</TabsTrigger>
          <TabsTrigger value="profiles">Profiles</TabsTrigger>
          <TabsTrigger value="m3u-priority">M3U Priority</TabsTrigger>
        </TabsList>
        
        <TabsContent value="regex" className="space-y-6">
          {/* Search Bar and Export/Import Buttons */}
          <div className="flex items-center gap-2">
            <div className="relative flex-1 max-w-md">
              <Search className="absolute left-3 top-1/2 transform -translate-y-1/2 h-4 w-4 text-muted-foreground" />
              <Input
                type="text"
                placeholder="Search channels by name, number, or ID..."
                value={searchQuery}
                onChange={(e) => setSearchQuery(e.target.value)}
                className="pl-10 pr-10"
              />
              {searchQuery && (
                <Button
                  variant="ghost"
                  size="sm"
                  className="absolute right-1 top-1/2 transform -translate-y-1/2 h-7 w-7 p-0"
                  onClick={clearSearch}
                >
                  <X className="h-4 w-4" />
                </Button>
              )}
            </div>
            {searchQuery && (
              <Badge variant="secondary">
                {filteredChannels.length} of {channels.length} channels
              </Badge>
            )}
            
            {/* Profile Filter Info */}
            {profileFilterActive && profileFilterInfo && !searchQuery && (
              <Tooltip>
                <TooltipTrigger asChild>
                  <Badge variant="outline" className="gap-1 cursor-help">
                    <Info className="h-3 w-3" />
                    {profileFilterInfo.channelCount} channels
                  </Badge>
                </TooltipTrigger>
                <TooltipContent className="max-w-xs">
                  <p className="font-semibold mb-1">Profile Filter Active</p>
                  <p className="text-sm">{profileFilterInfo.message}</p>
                  {profileFilterInfo.isEmpty && (
                    <p className="text-sm text-muted-foreground mt-1">
                      Go to the Profiles tab to refresh profile data.
                    </p>
                  )}
                </TooltipContent>
              </Tooltip>
            )}
            
            {/* Export/Import Buttons */}
            <div className="flex items-center gap-2 ml-auto">
              <Button
                variant="outline"
                size="sm"
                onClick={handleExportPatterns}
              >
                <Download className="h-4 w-4 mr-2" />
                Export Regex
              </Button>
              <Button
                variant="outline"
                size="sm"
                onClick={() => document.getElementById('import-file-input').click()}
              >
                <Upload className="h-4 w-4 mr-2" />
                Import Regex
              </Button>
              <input
                id="import-file-input"
                type="file"
                accept=".json"
                onChange={handleImportPatterns}
                style={{ display: 'none' }}
              />
            </div>
          </div>

          <div className="space-y-4">
            {/* Filter and Action Bar */}
            <Card>
              <CardContent className="p-4">
                <div className="flex items-center justify-between gap-4">
                  <div className="flex items-center gap-4 flex-1">
                    {/* Group Filter */}
                    <div className="flex items-center gap-2">
                      <Label htmlFor="group-filter" className="text-sm whitespace-nowrap">Filter Group:</Label>
                      <Select value={filterByGroup} onValueChange={setFilterByGroup}>
                        <SelectTrigger id="group-filter" className="h-9 w-[200px]">
                          <SelectValue />
                        </SelectTrigger>
                        <SelectContent>
                          <SelectItem value="all">All Groups</SelectItem>
                          {groups.map(group => (
                            <SelectItem key={group.id} value={String(group.id)}>
                              {group.name}
                            </SelectItem>
                          ))}
                        </SelectContent>
                      </Select>
                    </div>
                    
                    {/* Sort by Group */}
                    <div className="flex items-center gap-2">
                      <Checkbox
                        id="sort-by-group"
                        checked={sortByGroup}
                        onCheckedChange={setSortByGroup}
                      />
                      <Label htmlFor="sort-by-group" className="text-sm whitespace-nowrap cursor-pointer">
                        Sort by Group
                      </Label>
                    </div>
                  </div>
                  
                  {/* Selection Actions */}
                  <div className="flex items-center gap-2">
                    <Badge variant="secondary">
                      {selectedChannels.size} selected
                    </Badge>
                    <Button
                      variant="outline"
                      size="sm"
                      onClick={handleSelectAll}
                      disabled={displayChannels.length === 0}
                    >
                      Select All
                    </Button>
                    <Button
                      variant="outline"
                      size="sm"
                      onClick={handleDeselectAll}
                      disabled={selectedChannels.size === 0}
                    >
                      Deselect All
                    </Button>
                    <Button
                      size="sm"
                      onClick={() => setBulkDialogOpen(true)}
                      disabled={selectedChannels.size === 0}
                    >
                      <Plus className="h-4 w-4 mr-2" />
                      Add Regex to Selected
                    </Button>
                    <Button
                      size="sm"
                      onClick={handleBulkHealthCheck}
                      disabled={selectedChannels.size === 0 || bulkCheckingChannels}
                      className="text-blue-600 dark:text-green-500 border-blue-600 dark:border-green-500 hover:bg-blue-50 dark:hover:bg-green-950"
                      variant="outline"
                    >
                      {bulkCheckingChannels ? (
                        <Loader2 className="h-4 w-4 mr-2 animate-spin" />
                      ) : (
                        <Activity className="h-4 w-4 mr-2" />
                      )}
                      Health Check Selected
                    </Button>
                  </div>
                </div>
              </CardContent>
            </Card>
            
            {/* Pagination info and controls at top */}
            {displayChannels.length > 0 && (
              <div className="flex items-center justify-between">
                <div className="text-sm text-muted-foreground">
                  Showing {startIndex + 1}-{Math.min(endIndex, displayChannels.length)} of {displayChannels.length} channels
                </div>
                <div className="flex items-center gap-2">
                  <Label htmlFor="items-per-page" className="text-sm whitespace-nowrap">Items per page:</Label>
                  <Select
                    value={itemsPerPage.toString()}
                    onValueChange={(value) => {
                      setItemsPerPage(Number(value))
                      setCurrentPage(1)
                    }}
                  >
                    <SelectTrigger className="h-9 w-[100px]">
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value="10">10</SelectItem>
                      <SelectItem value="20">20</SelectItem>
                      <SelectItem value="50">50</SelectItem>
                      <SelectItem value="100">100</SelectItem>
                    </SelectContent>
                  </Select>
                </div>
              </div>
            )}

            {displayChannels.length === 0 ? (
              <Card>
                <CardContent className="p-8 text-center">
                  <p className="text-muted-foreground">
                    {searchQuery ? `No channels found matching "${searchQuery}"` : 'No channels available'}
                  </p>
                  {searchQuery && (
                    <Button
                      variant="outline"
                      size="sm"
                      className="mt-4"
                      onClick={clearSearch}
                    >
                      Clear search
                    </Button>
                  )}
                </CardContent>
              </Card>
            ) : (
              <>
                {/* Table Header */}
                <Card>
                  <CardContent className="p-0">
                    <div className="border-b bg-muted/50">
                      <div className={`gap-4 p-4 font-medium text-sm`} style={{ gridTemplateColumns: REGEX_TABLE_GRID_COLS, display: 'grid' }}>
                        <div className="flex items-center justify-center">
                          <Checkbox
                            checked={filteredChannels.length > 0 && filteredChannels.every(ch => selectedChannels.has(ch.id))}
                            onCheckedChange={(checked) => {
                              const newSet = new Set(selectedChannels)
                              filteredChannels.forEach(ch => {
                                if (checked) {
                                  newSet.add(ch.id)
                                } else {
                                  newSet.delete(ch.id)
                                }
                              })
                              setSelectedChannels(newSet)
                            }}
                          />
                        </div>
                        <div>#</div>
                        <div>Logo</div>
                        <div>Channel Name</div>
                        <div>Channel Group</div>
                        <div>Regex Patterns</div>
                        <div>Actions</div>
                      </div>
                    </div>
                    
                    {/* Table Rows */}
                    <div className="divide-y">
                      {paginatedChannels.map(channel => {
                        const group = groups.find(g => g.id === channel.channel_group_id)
                        const settings = channelSettings[channel.id]
                        
                        return (
                          <RegexTableRow 
                            key={channel.id}
                            channel={channel}
                            group={group}
                            groups={groups}
                            patterns={patterns}
                            channelSettings={settings}
                            selectedChannels={selectedChannels}
                            onToggleChannel={handleToggleChannel}
                            onEditRegex={handleEditRegex}
                            onUpdateSettings={handleUpdateSettings}
                            onDeletePattern={handleDeletePattern}
                            expandedRowId={expandedRowId}
                            onToggleExpanded={handleToggleExpanded}
                            onCheckChannel={handleCheckChannel}
                            checkingChannel={checkingChannel}
                          />
                        )
                      })}
                    </div>
                  </CardContent>
                </Card>
              </>
            )}

            {/* Pagination controls at bottom */}
            {totalPages > 1 && (
              <div className="flex items-center justify-center gap-2 pt-4">
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => setCurrentPage(1)}
                  disabled={currentPage === 1}
                >
                  First
                </Button>
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => setCurrentPage(currentPage - 1)}
                  disabled={currentPage === 1}
                >
                  Previous
                </Button>
                <div className="flex items-center gap-1">
                  {Array.from({ length: Math.min(5, totalPages) }, (_, i) => {
                    // Show pages around current page
                    let pageNum
                    if (totalPages <= 5) {
                      pageNum = i + 1
                    } else if (currentPage <= 3) {
                      pageNum = i + 1
                    } else if (currentPage >= totalPages - 2) {
                      pageNum = totalPages - 4 + i
                    } else {
                      pageNum = currentPage - 2 + i
                    }
                    
                    return (
                      <Button
                        key={pageNum}
                        variant={currentPage === pageNum ? "default" : "outline"}
                        size="sm"
                        onClick={() => setCurrentPage(pageNum)}
                        className="w-9"
                      >
                        {pageNum}
                      </Button>
                    )
                  })}
                </div>
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => setCurrentPage(currentPage + 1)}
                  disabled={currentPage === totalPages}
                >
                  Next
                </Button>
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => setCurrentPage(totalPages)}
                  disabled={currentPage === totalPages}
                >
                  Last
                </Button>
              </div>
            )}
          </div>
        </TabsContent>
        
        <TabsContent value="groups" className="space-y-6">
          {/* Search Bar for Groups */}
          <div className="flex items-center gap-2">
            <div className="relative flex-1 max-w-md">
              <Search className="absolute left-3 top-1/2 transform -translate-y-1/2 h-4 w-4 text-muted-foreground" />
              <Input
                type="text"
                placeholder="Search groups by name or ID..."
                value={groupSearchQuery}
                onChange={(e) => setGroupSearchQuery(e.target.value)}
                className="pl-10 pr-10"
              />
              {groupSearchQuery && (
                <Button
                  variant="ghost"
                  size="sm"
                  className="absolute right-1 top-1/2 transform -translate-y-1/2 h-7 w-7 p-0"
                  onClick={() => setGroupSearchQuery('')}
                >
                  <X className="h-4 w-4" />
                </Button>
              )}
            </div>
            {groupSearchQuery && (
              <Badge variant="secondary">
                {filteredGroups.length} of {groups.length} groups
              </Badge>
            )}
          </div>

          <Card>
            <CardHeader>
              <div className="flex items-start justify-between">
                <div className="flex-1">
                  <CardTitle>Channel Group Settings</CardTitle>
                  <CardDescription>
                    Manage stream matching and checking settings for entire channel groups. 
                    When both settings are disabled for a group, its channels will not appear in Regex Configuration or Channel Ordering.
                  </CardDescription>
                </div>
                <div className="flex gap-2 ml-4">
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={handleBulkDisableMatching}
                    className="whitespace-nowrap"
                  >
                    Disable Matching for All
                  </Button>
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={handleBulkDisableChecking}
                    className="whitespace-nowrap"
                  >
                    Disable Checking for All
                  </Button>
                </div>
              </div>
            </CardHeader>
            <CardContent className="space-y-4">
              {/* Pagination info and controls at top */}
              {filteredGroups.length > 0 && (
                <div className="flex items-center justify-between">
                  <div className="text-sm text-muted-foreground">
                    Showing {groupStartIndex + 1}-{Math.min(groupEndIndex, filteredGroups.length)} of {filteredGroups.length} groups
                  </div>
                  <div className="flex items-center gap-2">
                    <Label htmlFor="group-items-per-page" className="text-sm whitespace-nowrap">Items per page:</Label>
                    <Select
                      value={groupItemsPerPage.toString()}
                      onValueChange={(value) => {
                        setGroupItemsPerPage(Number(value))
                        setGroupCurrentPage(1)
                      }}
                    >
                      <SelectTrigger className="h-9 w-[100px]" id="group-items-per-page">
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent>
                        <SelectItem value="10">10</SelectItem>
                        <SelectItem value="20">20</SelectItem>
                        <SelectItem value="50">50</SelectItem>
                        <SelectItem value="100">100</SelectItem>
                      </SelectContent>
                    </Select>
                  </div>
                </div>
              )}

              {filteredGroups.length === 0 ? (
                <div className="text-center py-8 text-muted-foreground">
                  {groupSearchQuery ? `No groups found matching "${groupSearchQuery}"` : 'No channel groups available'}
                  {groupSearchQuery && (
                    <Button
                      variant="outline"
                      size="sm"
                      className="mt-4"
                      onClick={() => setGroupSearchQuery('')}
                    >
                      Clear search
                    </Button>
                  )}
                </div>
              ) : (
                <>
                  <div className="space-y-4">
                    {paginatedGroups.map(group => (
                      <GroupCard
                        key={group.id}
                        group={group}
                        channels={channels}
                        groupSettings={groupSettings[group.id]}
                        onUpdateSettings={handleUpdateGroupSettings}
                      />
                    ))}
                  </div>

                  {/* Pagination controls at bottom */}
                  {groupTotalPages > 1 && (
                    <div className="flex items-center justify-center gap-2 pt-4">
                      <Button
                        variant="outline"
                        size="sm"
                        onClick={() => setGroupCurrentPage(1)}
                        disabled={groupCurrentPage === 1}
                      >
                        First
                      </Button>
                      <Button
                        variant="outline"
                        size="sm"
                        onClick={() => setGroupCurrentPage(groupCurrentPage - 1)}
                        disabled={groupCurrentPage === 1}
                      >
                        Previous
                      </Button>
                      <div className="flex items-center gap-1">
                        {Array.from({ length: Math.min(5, groupTotalPages) }, (_, i) => {
                          let pageNum
                          if (groupTotalPages <= 5) {
                            pageNum = i + 1
                          } else if (groupCurrentPage <= 3) {
                            pageNum = i + 1
                          } else if (groupCurrentPage >= groupTotalPages - 2) {
                            pageNum = groupTotalPages - 4 + i
                          } else {
                            pageNum = groupCurrentPage - 2 + i
                          }
                          
                          return (
                            <Button
                              key={pageNum}
                              variant={groupCurrentPage === pageNum ? "default" : "outline"}
                              size="sm"
                              onClick={() => setGroupCurrentPage(pageNum)}
                              className="w-9"
                            >
                              {pageNum}
                            </Button>
                          )
                        })}
                      </div>
                      <Button
                        variant="outline"
                        size="sm"
                        onClick={() => setGroupCurrentPage(groupCurrentPage + 1)}
                        disabled={groupCurrentPage === groupTotalPages}
                      >
                        Next
                      </Button>
                      <Button
                        variant="outline"
                        size="sm"
                        onClick={() => setGroupCurrentPage(groupTotalPages)}
                        disabled={groupCurrentPage === groupTotalPages}
                      >
                        Last
                      </Button>
                    </div>
                  )}
                </>
              )}
            </CardContent>
          </Card>
        </TabsContent>
        
        <TabsContent value="ordering" className="space-y-6">
          {hasOrderChanges && (
            <Alert>
              <AlertDescription className="flex items-center justify-between">
                <span>You have unsaved changes</span>
                <div className="flex gap-2">
                  <Button size="sm" variant="outline" onClick={handleResetOrder}>
                    <RotateCcw className="h-4 w-4 mr-2" />
                    Reset
                  </Button>
                  <Button size="sm" onClick={handleSaveOrder} disabled={savingOrder}>
                    {savingOrder && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}
                    <Save className="h-4 w-4 mr-2" />
                    Save Changes
                  </Button>
                </div>
              </AlertDescription>
            </Alert>
          )}

          <Card>
            <CardHeader>
              <div className="flex items-center justify-between">
                <div>
                  <CardTitle>Channel List</CardTitle>
                  <CardDescription>
                    {visibleOrderedChannels.length} visible channels ({orderedChannels.length} total) - Drag and drop within the current page to reorder
                  </CardDescription>
                </div>
                <div className="flex items-center gap-2">
                  <ArrowUpDown className="h-4 w-4 text-muted-foreground" />
                  <Select value={sortBy} onValueChange={handleSort}>
                    <SelectTrigger className="w-[200px]">
                      <SelectValue placeholder="Sort by..." />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value="custom">Custom Order</SelectItem>
                      <SelectItem value="channel_number">Channel Number</SelectItem>
                      <SelectItem value="name">Name (A-Z)</SelectItem>
                      <SelectItem value="id">ID</SelectItem>
                    </SelectContent>
                  </Select>
                </div>
              </div>
            </CardHeader>
            <CardContent className="space-y-4">
              {/* Pagination info and controls at top */}
              {visibleOrderedChannels.length > 0 && (
                <div className="flex items-center justify-between">
                  <div className="text-sm text-muted-foreground">
                    Showing {orderStartIndex + 1}-{Math.min(orderEndIndex, visibleOrderedChannels.length)} of {visibleOrderedChannels.length} channels
                  </div>
                  <div className="flex items-center gap-2">
                    <Label htmlFor="order-items-per-page" className="text-sm whitespace-nowrap">Items per page:</Label>
                    <Select
                      value={orderItemsPerPage.toString()}
                      onValueChange={(value) => {
                        setOrderItemsPerPage(Number(value))
                        setOrderCurrentPage(1)
                      }}
                    >
                      <SelectTrigger className="h-9 w-[100px]" id="order-items-per-page">
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent>
                        <SelectItem value="10">10</SelectItem>
                        <SelectItem value="20">20</SelectItem>
                        <SelectItem value="50">50</SelectItem>
                        <SelectItem value="100">100</SelectItem>
                      </SelectContent>
                    </Select>
                  </div>
                </div>
              )}

              {visibleOrderedChannels.length === 0 ? (
                <div className="text-center py-8 text-muted-foreground">
                  No channels available
                </div>
              ) : (
                <>
                  <DndContext
                    sensors={sensors}
                    collisionDetection={closestCenter}
                    onDragEnd={handleDragEnd}
                  >
                    <SortableContext
                      items={paginatedOrderedChannels.map(ch => ch.id)}
                      strategy={verticalListSortingStrategy}
                    >
                      <div className="space-y-2">
                        {paginatedOrderedChannels.map((channel) => (
                          <SortableChannelItem key={channel.id} channel={channel} />
                        ))}
                      </div>
                    </SortableContext>
                  </DndContext>

                  {/* Pagination controls at bottom */}
                  {orderTotalPages > 1 && (
                    <div className="flex items-center justify-center gap-2 pt-4">
                      <Button
                        variant="outline"
                        size="sm"
                        onClick={() => setOrderCurrentPage(1)}
                        disabled={orderCurrentPage === 1}
                      >
                        First
                      </Button>
                      <Button
                        variant="outline"
                        size="sm"
                        onClick={() => setOrderCurrentPage(orderCurrentPage - 1)}
                        disabled={orderCurrentPage === 1}
                      >
                        Previous
                      </Button>
                      <div className="flex items-center gap-1">
                        {Array.from({ length: Math.min(5, orderTotalPages) }, (_, i) => {
                          let pageNum
                          if (orderTotalPages <= 5) {
                            pageNum = i + 1
                          } else if (orderCurrentPage <= 3) {
                            pageNum = i + 1
                          } else if (orderCurrentPage >= orderTotalPages - 2) {
                            pageNum = orderTotalPages - 4 + i
                          } else {
                            pageNum = orderCurrentPage - 2 + i
                          }
                          
                          return (
                            <Button
                              key={pageNum}
                              variant={orderCurrentPage === pageNum ? "default" : "outline"}
                              size="sm"
                              onClick={() => setOrderCurrentPage(pageNum)}
                              className="w-9"
                            >
                              {pageNum}
                            </Button>
                          )
                        })}
                      </div>
                      <Button
                        variant="outline"
                        size="sm"
                        onClick={() => setOrderCurrentPage(orderCurrentPage + 1)}
                        disabled={orderCurrentPage === orderTotalPages}
                      >
                        Next
                      </Button>
                      <Button
                        variant="outline"
                        size="sm"
                        onClick={() => setOrderCurrentPage(orderTotalPages)}
                        disabled={orderCurrentPage === orderTotalPages}
                      >
                        Last
                      </Button>
                    </div>
                  )}
                </>
              )}
            </CardContent>
          </Card>

          {hasOrderChanges && (
            <div className="flex justify-end gap-2">
              <Button variant="outline" onClick={handleResetOrder}>
                <RotateCcw className="h-4 w-4 mr-2" />
                Reset Changes
              </Button>
              <Button onClick={handleSaveOrder} disabled={savingOrder} size="lg">
                {savingOrder && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}
                <Save className="h-4 w-4 mr-2" />
                Save Order
              </Button>
            </div>
          )}
        </TabsContent>
        
        {/* Profiles Tab */}
        <TabsContent value="profiles" className="space-y-6">
          <ProfileManagement />
        </TabsContent>

        {/* M3U Priority Tab */}
        <TabsContent value="m3u-priority" className="space-y-6">
          <M3UPriorityManagement />
        </TabsContent>
      </Tabs>

      {/* Regex Pattern Dialog */}
      <Dialog open={dialogOpen} onOpenChange={setDialogOpen}>
        <DialogContent className="sm:max-w-[600px]">
          <DialogHeader>
            <DialogTitle>
              {editingPatternIndex !== null ? 'Edit' : 'Add'} Regex Pattern
            </DialogTitle>
            <DialogDescription>
              Enter a regex pattern to match streams for this channel. Use {'{CHANNEL_NAME}'} to insert the channel name.
            </DialogDescription>
          </DialogHeader>
          
          <div className="space-y-4 py-4">
            <div className="space-y-2">
              <Label htmlFor="pattern">Regex Pattern</Label>
              <Input
                id="pattern"
                placeholder="e.g., .*ESPN.*|.*Sports.*"
                value={newPattern}
                onChange={(e) => setNewPattern(e.target.value)}
                className="font-mono"
              />
            </div>

            {/* M3U Account Selection */}
            <div className="space-y-2">
              <Label>Apply to M3U Accounts (Sources)</Label>
              <div className="text-xs text-muted-foreground mb-2">
                Select which M3U account sources this regex should match streams from. Leave empty for all sources.
              </div>
              <div className="border rounded-md p-3 max-h-48 overflow-y-auto space-y-2">
                {m3uAccounts.length === 0 ? (
                  <div className="text-sm text-muted-foreground italic">No M3U accounts available</div>
                ) : (
                  <>
                    <div className="flex items-center space-x-2 pb-2 border-b">
                      <Checkbox
                        id="select-all-m3u-accounts"
                        checked={selectedM3uAccounts.length === 0}
                        onCheckedChange={(checked) => {
                          if (checked) {
                            setSelectedM3uAccounts([])  // Empty = all M3U accounts
                          } else {
                            // When unchecking "All", select first M3U account if available
                            if (m3uAccounts.length > 0) {
                              setSelectedM3uAccounts([m3uAccounts[0].id])
                            }
                          }
                        }}
                      />
                      <label htmlFor="select-all-m3u-accounts" className="text-sm font-medium cursor-pointer">
                        All M3U Accounts
                      </label>
                    </div>
                    {m3uAccounts.map(account => (
                      <div key={account.id} className="flex items-center space-x-2">
                        <Checkbox
                          id={`m3u-account-${account.id}`}
                          checked={selectedM3uAccounts.includes(account.id)}
                          disabled={selectedM3uAccounts.length === 0}  // Disabled when "All" is selected
                          onCheckedChange={(checked) => {
                            if (checked) {
                              setSelectedM3uAccounts([...selectedM3uAccounts, account.id])
                            } else {
                              setSelectedM3uAccounts(selectedM3uAccounts.filter(id => id !== account.id))
                            }
                          }}
                        />
                        <label htmlFor={`m3u-account-${account.id}`} className={`text-sm cursor-pointer ${selectedM3uAccounts.length === 0 ? 'text-muted-foreground' : ''}`}>
                          {account.name}
                        </label>
                      </div>
                    ))}
                  </>
                )}
              </div>
            </div>

            {/* Live Test Results */}
            {testingPattern && (
              <div className="flex items-center gap-2 text-sm text-muted-foreground animate-in fade-in duration-200">
                <Loader2 className="h-4 w-4 animate-spin" />
                Testing pattern...
              </div>
            )}

            {testResults && !testingPattern && (
              <div className="space-y-2 animate-in fade-in slide-in-from-top-2 duration-300">
                <Label>Test Results</Label>
                <div className="border rounded-md p-3 bg-muted/50 transition-all">
                  {testResults.valid ? (
                    <>
                      <div className="flex items-center gap-2 text-sm font-medium text-green-600 mb-2 animate-in fade-in duration-200">
                        <CheckCircle className="h-4 w-4" />
                        Valid pattern - {testResults.matches?.length || 0} matches found
                      </div>
                      {testResults.matches && testResults.matches.length > 0 && (
                        <div className="space-y-1 max-h-32 overflow-y-auto">
                          {testResults.matches.slice(0, 10).map((match, idx) => (
                            <div 
                              key={idx} 
                              className="text-xs text-muted-foreground truncate animate-in fade-in slide-in-from-left-1 duration-200"
                              style={{ animationDelay: `${idx * 20}ms` }}
                            >
                              • {match}
                            </div>
                          ))}
                          {testResults.matches.length > 10 && (
                            <div className="text-xs text-muted-foreground italic animate-in fade-in duration-200">
                              ... and {testResults.matches.length - 10} more
                            </div>
                          )}
                        </div>
                      )}
                    </>
                  ) : (
                    <div className="text-sm text-destructive">
                      {testResults.error || 'Invalid pattern'}
                    </div>
                  )}
                </div>
              </div>
            )}
          </div>

          <DialogFooter>
            <Button variant="outline" onClick={handleCloseDialog}>
              Cancel
            </Button>
            <Button onClick={handleSavePattern} disabled={!newPattern.trim()}>
              {editingPatternIndex !== null ? 'Update' : 'Add'} Pattern
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
      
      {/* Bulk Pattern Assignment Dialog */}
      <Dialog open={bulkDialogOpen} onOpenChange={setBulkDialogOpen}>
        <DialogContent className="sm:max-w-[600px]">
          <DialogHeader>
            <DialogTitle>Add Regex Pattern to Multiple Channels</DialogTitle>
            <DialogDescription>
              This pattern will be added to {selectedChannels.size} selected channel{selectedChannels.size !== 1 ? 's' : ''}. Use {'{CHANNEL_NAME}'} to insert each channel's name into the pattern.
            </DialogDescription>
          </DialogHeader>
          
          <div className="space-y-4 py-4">
            <div className="space-y-2">
              <Label htmlFor="bulk-pattern">Regex Pattern</Label>
              <Input
                id="bulk-pattern"
                placeholder="e.g., .*{CHANNEL_NAME}.*"
                value={bulkPattern}
                onChange={(e) => setBulkPattern(e.target.value)}
                className="font-mono"
              />
              <p className="text-xs text-muted-foreground">
                Use {'{CHANNEL_NAME}'} to create a pattern that works for all selected channels. The legacy CHANNEL_NAME format (without braces) is also supported.
              </p>
            </div>
            
            {/* M3U Account Selection for Bulk */}
            <div className="space-y-2">
              <Label>Apply to M3U Accounts (Sources)</Label>
              <div className="text-xs text-muted-foreground mb-2">
                Select which M3U account sources this regex should match streams from. Leave empty for all sources.
              </div>
              <div className="border rounded-md p-3 max-h-48 overflow-y-auto space-y-2">
                {m3uAccounts.length === 0 ? (
                  <div className="text-sm text-muted-foreground italic">No M3U accounts available</div>
                ) : (
                  <>
                    <div className="flex items-center space-x-2 pb-2 border-b">
                      <Checkbox
                        id="bulk-select-all-m3u-accounts"
                        checked={bulkSelectedM3uAccounts.length === 0}
                        onCheckedChange={(checked) => {
                          if (checked) {
                            setBulkSelectedM3uAccounts([])  // Empty = all M3U accounts
                          } else {
                            // When unchecking "All", select first M3U account if available
                            if (m3uAccounts.length > 0) {
                              setBulkSelectedM3uAccounts([m3uAccounts[0].id])
                            }
                          }
                        }}
                      />
                      <label htmlFor="bulk-select-all-m3u-accounts" className="text-sm font-medium cursor-pointer">
                        All M3U Accounts
                      </label>
                    </div>
                    {m3uAccounts.map(account => (
                      <div key={account.id} className="flex items-center space-x-2">
                        <Checkbox
                          id={`bulk-m3u-account-${account.id}`}
                          checked={bulkSelectedM3uAccounts.includes(account.id)}
                          disabled={bulkSelectedM3uAccounts.length === 0}  // Disabled when "All" is selected
                          onCheckedChange={(checked) => {
                            if (checked) {
                              setBulkSelectedM3uAccounts([...bulkSelectedM3uAccounts, account.id])
                            } else {
                              setBulkSelectedM3uAccounts(bulkSelectedM3uAccounts.filter(id => id !== account.id))
                            }
                          }}
                        />
                        <label htmlFor={`bulk-m3u-account-${account.id}`} className={`text-sm cursor-pointer ${bulkSelectedM3uAccounts.length === 0 ? 'text-muted-foreground' : ''}`}>
                          {account.name}
                        </label>
                      </div>
                    ))}
                  </>
                )}
              </div>
            </div>
            
            <div className="border rounded-md p-3 bg-muted/50">
              <div className="text-sm font-medium mb-2">Example:</div>
              <div className="text-xs text-muted-foreground space-y-1">
                <div>Pattern: <code className="bg-background px-1 rounded">.*{'{CHANNEL_NAME}'}.*</code></div>
                <div>For channel "ESPN", matches: <code className="bg-background px-1 rounded">.*ESPN.*</code></div>
                <div>For channel "CNN", matches: <code className="bg-background px-1 rounded">.*CNN.*</code></div>
              </div>
            </div>
          </div>

          <DialogFooter>
            <Button variant="outline" onClick={() => {
              setBulkDialogOpen(false)
              setBulkPattern('')
              setBulkSelectedM3uAccounts([])  // Reset bulk M3U account selection
            }}>
              Cancel
            </Button>
            <Button onClick={handleBulkAddPattern} disabled={!bulkPattern.trim()}>
              Add to {selectedChannels.size} Channel{selectedChannels.size !== 1 ? 's' : ''}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
    </TooltipProvider>
  )
}
