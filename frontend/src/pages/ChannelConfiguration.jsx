
import { useState, useEffect, useCallback, useRef } from 'react'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card.jsx'
import { Button } from '@/components/ui/button.jsx'
import { Input } from '@/components/ui/input.jsx'
import { Label } from '@/components/ui/label.jsx'
import { Badge } from '@/components/ui/badge.jsx'
import { Checkbox } from '@/components/ui/checkbox.jsx'
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle, DialogFooter } from '@/components/ui/dialog.jsx'
import { AlertDialog, AlertDialogAction, AlertDialogCancel, AlertDialogContent, AlertDialogDescription, AlertDialogFooter, AlertDialogHeader, AlertDialogTitle } from '@/components/ui/alert-dialog.jsx'
import { Accordion, AccordionContent, AccordionItem, AccordionTrigger } from '@/components/ui/accordion.jsx'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select.jsx'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs.jsx'
import { Alert, AlertDescription } from '@/components/ui/alert.jsx'
import { Separator } from '@/components/ui/separator.jsx'
import { useToast } from '@/hooks/use-toast.js'
import { channelsAPI, regexAPI, streamCheckerAPI, channelOrderAPI, automationAPI, m3uAPI } from '@/services/api.js'
import { CheckCircle, Edit, Plus, Trash2, Loader2, Search, X, Download, Upload, GripVertical, Save, RotateCcw, ArrowUpDown, MoreVertical, Eye, ChevronDown, Info, Activity, Edit2, ArrowRight, Clock, Calendar, CalendarClock } from 'lucide-react'
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuLabel, DropdownMenuSeparator, DropdownMenuTrigger, DropdownMenuCheckboxItem } from '@/components/ui/dropdown-menu.jsx'
import { Switch } from '@/components/ui/switch.jsx'
import { Tooltip, TooltipContent, TooltipTrigger, TooltipProvider } from '@/components/ui/tooltip.jsx'
import { RegexTableRow } from '@/components/channel-configuration/RegexTableRow.jsx'
import { SortableChannelItem } from '@/components/channel-configuration/SortableChannelItem.jsx'
import {
  BatchAssignPeriodsDialog,
  BatchPeriodEditDialog,
} from '@/components/channel-configuration/PeriodDialogs.jsx'
import { MatchPreviewDialog, MatchResultsList } from '@/components/channel-configuration/MatchPreviewDialog.jsx'
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
  verticalListSortingStrategy,
} from '@dnd-kit/sortable'

// Constants for localStorage keys
const CHANNEL_STATS_PREFIX = 'streamflow_channel_stats_'
const CHANNEL_LOGO_PREFIX = 'streamflow_channel_logo_'

// Constants for grid layout
const REGEX_TABLE_GRID_COLS = '50px 80px 80px 1fr 180px 120px 150px 140px'

// Constants for stream checker priorities

// M3U account filtering - exclude 'custom' account as it's not a real source
const CUSTOM_ACCOUNT_NAME = 'custom'

// Helper function to normalize pattern data (supports both old and new formats)
const normalizePatternData = (channelPatterns) => {
  if (!channelPatterns) return []

  // New format: regex_patterns is array of {pattern, m3u_accounts}
  if (channelPatterns.regex_patterns && Array.isArray(channelPatterns.regex_patterns)) {
    return channelPatterns.regex_patterns.map(p => ({
      pattern: p.pattern || p,
      m3u_accounts: p.m3u_accounts || null
    }))
  }

  // Old format: regex is array of strings, m3u_accounts is channel-level
  if (channelPatterns.regex && Array.isArray(channelPatterns.regex)) {
    const channelM3uAccounts = channelPatterns.m3u_accounts || null
    return channelPatterns.regex.map(pattern => ({
      pattern: pattern,
      m3u_accounts: channelM3uAccounts
    }))
  }

  return []
}

// Dialog for assigning automation periods to a group
function GroupAssignPeriodsDialog({ open, onOpenChange, group, initialPeriods, onSave, saving }) {
  const [allPeriods, setAllPeriods] = useState([])
  const [allProfiles, setAllProfiles] = useState([])
  const [periodAssignments, setPeriodAssignments] = useState({})  // {period_id: profile_id}
  const [loading, setLoading] = useState(false)
  const { toast } = useToast()

  useEffect(() => {
    if (open) {
      loadData()
    }
  }, [open])

  const loadData = async () => {
    try {
      setLoading(true)
      const [periodsResponse, profilesResponse] = await Promise.all([
        automationAPI.getPeriods(),
        automationAPI.getProfiles()
      ])
      setAllPeriods(periodsResponse.data || [])
      setAllProfiles(profilesResponse.data || [])
      // Pre-populate with existing group period assignments
      const existing = {}
      if (Array.isArray(initialPeriods)) {
        initialPeriods.forEach((p) => {
          if (p.id && p.profile_id) {
            existing[p.id] = p.profile_id
          }
        })
      }
      setPeriodAssignments(existing)
    } catch (err) {
      console.error('Failed to load data:', err)
      toast({
        title: "Error",
        description: "Failed to load periods and profiles",
        variant: "destructive"
      })
    } finally {
      setLoading(false)
    }
  }

  const togglePeriod = (periodId) => {
    setPeriodAssignments(prev => {
      const newAssignments = { ...prev }
      if (periodId in newAssignments) {
        delete newAssignments[periodId]
      } else {
        if (allProfiles.length > 0) {
          newAssignments[periodId] = allProfiles[0].id
        }
      }
      return newAssignments
    })
  }

  const updateProfile = (periodId, profileId) => {
    setPeriodAssignments(prev => ({
      ...prev,
      [periodId]: profileId
    }))
  }

  const handleSave = () => {
    const selectedPeriods = Object.entries(periodAssignments).filter(([_, profileId]) => profileId && profileId !== '')

    if (selectedPeriods.length === 0) {
      toast({
        title: "No Periods Selected",
        description: "Please select at least one period and assign a profile",
        variant: "destructive"
      })
      return
    }

    const assignments = selectedPeriods.map(([periodId, profileId]) => ({
      period_id: periodId,
      profile_id: profileId
    }))

    onSave(assignments, false)
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-3xl max-h-[80vh] overflow-hidden flex flex-col">
        <DialogHeader>
          <DialogTitle>Assign Automation Periods to Group</DialogTitle>
          <DialogDescription>
            Assign automation periods with profiles to group &quot;{group?.name}&quot;. Channels in this group will inherit these periods.
          </DialogDescription>
        </DialogHeader>

        {loading ? (
          <div className="flex items-center justify-center py-8">
            <Loader2 className="h-8 w-8 animate-spin text-muted-foreground" />
          </div>
        ) : allPeriods.length === 0 ? (
          <div className="text-center py-8">
            <Calendar className="h-12 w-12 mx-auto mb-4 opacity-50 text-muted-foreground" />
            <p className="text-muted-foreground mb-4">No automation periods available</p>
            <p className="text-sm text-muted-foreground">
              Create automation periods in Settings → Automation → Periods first
            </p>
          </div>
        ) : (
          <div className="space-y-3 overflow-y-auto flex-1">
            {allPeriods.map((period) => {
              const isSelected = period.id in periodAssignments
              const selectedProfile = periodAssignments[period.id]

              return (
                <div
                  key={period.id}
                  className={`p-3 border rounded-lg transition-colors ${isSelected ? 'border-primary bg-primary/5' : 'border-border'}`}
                >
                  <div className="flex items-start gap-3">
                    <Checkbox
                      checked={isSelected}
                      onCheckedChange={() => togglePeriod(period.id)}
                      className="mt-1"
                    />
                    <div className="flex-1 space-y-2">
                      <div>
                        <div className="flex items-center gap-2 mb-1">
                          <span className="font-medium">{period.name}</span>
                        </div>
                        <div className="text-sm text-muted-foreground">
                          {period.schedule?.type === 'interval'
                            ? `Every ${period.schedule.value} minutes`
                            : `Cron: ${period.schedule?.value || 'Not configured'}`}
                        </div>
                      </div>

                      {isSelected && (
                        <div className="pt-2 border-t">
                          <Label className="text-xs text-muted-foreground mb-1 block">
                            Profile for this period:
                          </Label>
                          <Select
                            value={selectedProfile}
                            onValueChange={(value) => updateProfile(period.id, value)}
                          >
                            <SelectTrigger className="h-8">
                              <SelectValue />
                            </SelectTrigger>
                            <SelectContent>
                              {allProfiles.map((profile) => (
                                <SelectItem key={profile.id} value={profile.id}>
                                  {profile.name}
                                </SelectItem>
                              ))}
                            </SelectContent>
                          </Select>
                        </div>
                      )}
                    </div>
                  </div>
                </div>
              )
            })}
          </div>
        )}

        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button
            onClick={handleSave}
            disabled={saving || loading || Object.keys(periodAssignments).length === 0}
          >
            {saving && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}
            Assign to Group
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

// Dialog for assigning regex and TVG-ID matching to a group
function GroupAssignMatchingDialog({ open, onOpenChange, group, initialConfig, onSave, onDelete, saving }) {
  const [enabled, setEnabled] = useState(true)
  const [matchByTvgId, setMatchByTvgId] = useState(false)
  const [patterns, setPatterns] = useState([])
  const [newPattern, setNewPattern] = useState('')
  const { toast } = useToast()

  useEffect(() => {
    if (!open) return
    const cfg = initialConfig || {}
    setEnabled(cfg.enabled ?? true)
    setMatchByTvgId(Boolean(cfg.match_by_tvg_id))

    const regexPatterns = cfg.regex_patterns || []
    const normalized = regexPatterns
      .map((p) => (typeof p === 'string' ? p : p?.pattern))
      .filter(Boolean)
    setPatterns(normalized)
    setNewPattern('')
  }, [open, initialConfig])

  const handleAddPattern = () => {
    const trimmed = (newPattern || '').trim()
    if (!trimmed) return
    if (patterns.includes(trimmed)) {
      toast({
        title: 'Duplicate Pattern',
        description: 'This regex pattern already exists for this group.',
        variant: 'destructive'
      })
      return
    }
    setPatterns((prev) => [...prev, trimmed])
    setNewPattern('')
  }

  const handleRemovePattern = (idx) => {
    setPatterns((prev) => prev.filter((_, i) => i !== idx))
  }

  const handleSave = () => {
    onSave({
      name: group?.name || '',
      enabled,
      match_by_tvg_id: matchByTvgId,
      regex_patterns: patterns.map((pattern) => ({ pattern }))
    })
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-2xl max-h-[80vh] overflow-hidden flex flex-col">
        <DialogHeader>
          <DialogTitle>Configure Group Matching</DialogTitle>
          <DialogDescription>
            Configure regex and TVG-ID matching for group &quot;{group?.name}&quot;. Channels in this group inherit these settings unless they have channel-specific matching config.
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-4 overflow-y-auto flex-1 py-1">
          <div className="flex items-center justify-between p-3 rounded-md border bg-muted/40">
            <div>
              <p className="font-medium text-sm">Enable Group Regex Matching</p>
              <p className="text-xs text-muted-foreground">When enabled, group regex patterns can match streams for channels in this group.</p>
            </div>
            <Switch checked={enabled} onCheckedChange={setEnabled} />
          </div>

          <div className="flex items-center justify-between p-3 rounded-md border bg-muted/40">
            <div>
              <p className="font-medium text-sm">Match by TVG-ID</p>
              <p className="text-xs text-muted-foreground">Use TVG-ID matching as a group fallback when channel-level settings are not defined.</p>
            </div>
            <Switch checked={matchByTvgId} onCheckedChange={setMatchByTvgId} />
          </div>

          <div className="space-y-2">
            <Label>Regex Patterns</Label>
            <div className="flex gap-2">
              <Input
                value={newPattern}
                onChange={(e) => setNewPattern(e.target.value)}
                placeholder="e.g. .*SPORT.*|.*ESPN.*"
                className="font-mono"
              />
              <Button onClick={handleAddPattern} type="button" variant="outline">
                <Plus className="h-4 w-4 mr-1" />
                Add
              </Button>
            </div>

            {patterns.length === 0 ? (
              <p className="text-sm text-muted-foreground italic">No regex patterns configured for this group</p>
            ) : (
              <div className="space-y-2">
                {patterns.map((pattern, idx) => (
                  <div key={`${pattern}-${idx}`} className="flex items-center justify-between gap-2 p-2 rounded border bg-background">
                    <code className="text-xs flex-1 truncate">{pattern}</code>
                    <Button
                      variant="ghost"
                      size="sm"
                      className="text-destructive hover:text-destructive"
                      onClick={() => handleRemovePattern(idx)}
                    >
                      <Trash2 className="h-4 w-4" />
                    </Button>
                  </div>
                ))}
              </div>
            )}
          </div>
        </div>

        <DialogFooter className="justify-between">
          <Button variant="destructive" onClick={onDelete} disabled={saving}>
            Clear Group Matching
          </Button>
          <div className="flex gap-2">
            <Button variant="outline" onClick={() => onOpenChange(false)}>
              Cancel
            </Button>
            <Button onClick={handleSave} disabled={saving}>
              {saving && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}
              Save Matching
            </Button>
          </div>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}


export default function ChannelConfiguration() {
  const [channels, setChannels] = useState([])
  const [patterns, setPatterns] = useState({})

  const [loading, setLoading] = useState(true)
  const [checkingChannel, setCheckingChannel] = useState(null)
  const [searchQuery, setSearchQuery] = useState('')
  const [dialogOpen, setDialogOpen] = useState(false)
  const [editingChannelId, setEditingChannelId] = useState(null)
  const [editingPatternIndex, setEditingPatternIndex] = useState(null)
  const [newPattern, setNewPattern] = useState('')
  const [testingPattern, setTestingPattern] = useState(false)
  const [testResults, setTestResults] = useState(null)
  const [testRequestIdRef] = useState({ current: 0 })
  const [previewResultsOpen, setPreviewResultsOpen] = useState(false)
  const [previewTitle, setPreviewTitle] = useState('')
  const { toast } = useToast()

  // Pagination state for Regex Configuration
  const [currentPage, setCurrentPage] = useState(1)
  const [itemsPerPage, setItemsPerPage] = useState(20)

  // Pagination state for Channel Order
  const [orderCurrentPage, setOrderCurrentPage] = useState(1)
  const [orderItemsPerPage, setOrderItemsPerPage] = useState(20)

  // Channel ordering state
  const [orderedChannels, setOrderedChannels] = useState([])
  const [originalChannelOrder, setOriginalChannelOrder] = useState([])
  const [hasOrderChanges, setHasOrderChanges] = useState(false)
  const [savingOrder, setSavingOrder] = useState(false)
  const [sortBy, setSortBy] = useState('custom')

  const [groups, setGroups] = useState([])

  const [pendingChanges, setPendingChanges] = useState({})
  const [activeTab, setActiveTab] = useState('regex')
  const [matchCounts, setMatchCounts] = useState({})        // {channelId: matchCount}
  const [totalStreamCount, setTotalStreamCount] = useState(null)

  // Multi-select state for bulk regex assignment
  const [selectedChannels, setSelectedChannels] = useState(new Set())
  const [filterByGroup, setFilterByGroup] = useState('all')
  const [sortByGroup, setSortByGroup] = useState(false)
  const [bulkDialogOpen, setBulkDialogOpen] = useState(false)
  const [bulkPeriodEditOpen, setBulkPeriodEditOpen] = useState(false)
  const [bulkPattern, setBulkPattern] = useState('')

  // Automation Profile state
  const [profiles, setProfiles] = useState([])
  const [assignEpgProfileId, setAssignEpgProfileId] = useState('')

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

  // Delete confirmation dialog state
  const [deleteDialogOpen, setDeleteDialogOpen] = useState(false)

  // Batch automation period assignment state
  const [batchPeriodsDialogOpen, setBatchPeriodsDialogOpen] = useState(false)

  // Group Configuration state
  const [groupsConfig, setGroupsConfig] = useState({}) // {groupId: {profileId, periods: [{id, profile_id}]}}
  const [loadingGroupsConfig, setLoadingGroupsConfig] = useState(false)
  const [groupAssignProfileDialogOpen, setGroupAssignProfileDialogOpen] = useState(false)
  const [groupAssignPeriodsDialogOpen, setGroupAssignPeriodsDialogOpen] = useState(false)
  const [groupAssignMatchingDialogOpen, setGroupAssignMatchingDialogOpen] = useState(false)
  const [groupAssignEpgProfileDialogOpen, setGroupAssignEpgProfileDialogOpen] = useState(false)
  const [selectedGroupForConfig, setSelectedGroupForConfig] = useState(null)
  const [groupProfileId, setGroupProfileId] = useState('')
  const [groupPeriodAssignments, setGroupPeriodAssignments] = useState([]) // [{period_id, profile_id}]
  const [savingGroupProfile, setSavingGroupProfile] = useState(false)
  const [savingGroupPeriods, setSavingGroupPeriods] = useState(false)
  const [savingGroupMatching, setSavingGroupMatching] = useState(false)
  const [groupEpgProfileId, setGroupEpgProfileId] = useState('')
  const [savingGroupEpgProfile, setSavingGroupEpgProfile] = useState(false)

  // Edit common regex dialog state
  const [editCommonDialogOpen, setEditCommonDialogOpen] = useState(false)
  const [commonPatterns, setCommonPatterns] = useState([])
  const [loadingCommonPatterns, setLoadingCommonPatterns] = useState(false)
  const [editingCommonPattern, setEditingCommonPattern] = useState(null)
  const [newCommonPattern, setNewCommonPattern] = useState('')
  const [newCommonPatternM3uAccounts, setNewCommonPatternM3uAccounts] = useState(null) // null = all playlists, array = selected playlists
  const [selectedCommonPatterns, setSelectedCommonPatterns] = useState(new Set())
  const [commonPatternsSearch, setCommonPatternsSearch] = useState('')

  // Mass edit state
  const [massEditMode, setMassEditMode] = useState(false)
  const [massEditFindPattern, setMassEditFindPattern] = useState('')
  const [massEditReplacePattern, setMassEditReplacePattern] = useState('')
  const [massEditUseRegex, setMassEditUseRegex] = useState(false)
  const [massEditM3uAccounts, setMassEditM3uAccounts] = useState(null) // null = keep existing, array = update to selected
  const [massEditPreview, setMassEditPreview] = useState(null)
  const [loadingMassEditPreview, setLoadingMassEditPreview] = useState(false)

  // Update settings only mode (for playlist/priority changes without find & replace)
  const [updateOnlyMode, setUpdateOnlyMode] = useState(false)

  const sensors = useSensors(
    useSensor(PointerSensor),
    useSensor(KeyboardSensor, {
      coordinateGetter: sortableKeyboardCoordinates,
    })
  )

  useEffect(() => {
    loadData()
  }, [])

  useEffect(() => {
    if (activeTab === 'groups' && groups.length > 0) {
      loadGroupsConfig()
    }
  }, [activeTab, groups])

  // Bulk match count — fires when Regex tab is active, on page change, pattern edits,
  // or after groupsConfig populates (so group-inherited channels get their counts too).
  useEffect(() => {
    if (activeTab !== 'regex' || channels.length === 0) return

    const fetchMatchCounts = async () => {
      // Build effective config for each channel on the current page,
      // mirroring the global mode logic in handleTestPattern.
      const channelConfigs = []

      for (const channel of paginatedChannels) {
        const channelId = channel.id
        const channelPatternConfig = patterns[channelId] || patterns[String(channelId)] || {}
        const channelGroupId = channel?.group_id ?? channel?.channel_group_id
        const groupMatchingConfig = channelGroupId
          ? ((groupsConfig[channelGroupId] || {}).matching || {})
          : {}

        const hasChannelRegexPatterns =
          (Array.isArray(channelPatternConfig.regex_patterns) && channelPatternConfig.regex_patterns.length > 0) ||
          (Array.isArray(channelPatternConfig.regex) && channelPatternConfig.regex.length > 0)
        const hasExplicitChannelOverride =
          hasChannelRegexPatterns ||
          channelPatternConfig.match_by_tvg_id === true ||
          channelPatternConfig.enabled === false

        const effectivePatternConfig = hasExplicitChannelOverride ? channelPatternConfig : groupMatchingConfig

        // Build regex patterns array in the normalised format
        let regexPatterns = []
        if (effectivePatternConfig.regex_patterns) {
          regexPatterns = effectivePatternConfig.regex_patterns
        } else if (effectivePatternConfig.regex) {
          regexPatterns = effectivePatternConfig.regex.map(p => ({
            pattern: p,
            m3u_accounts: effectivePatternConfig.m3u_accounts || null,
          }))
        }

        const matchByTvgId = effectivePatternConfig.match_by_tvg_id || false
        const hasTvg = matchByTvgId && Boolean(channel.tvg_id)
        const hasRegex = regexPatterns.length > 0

        // Skip channels with nothing to evaluate — display will show '— / Y'
        if (!hasRegex && !hasTvg) continue

        channelConfigs.push({
          channel_id: channelId,
          channel_name: channel.name || '',
          tvg_id: channel.tvg_id || null,
          match_by_tvg_id: matchByTvgId,
          regex_patterns: regexPatterns,
          match_priority_order: ['tvg', 'regex'],
          case_sensitive: true,
        })
      }

      if (channelConfigs.length === 0) return

      try {
        const response = await regexAPI.bulkMatchCounts({ channels: channelConfigs })
        const { counts, total_streams } = response.data
        setMatchCounts(prev => ({ ...prev, ...counts }))
        if (total_streams != null) setTotalStreamCount(total_streams)
      } catch (err) {
        console.error('Failed to fetch bulk match counts:', err)
      }
    }

    fetchMatchCounts()
  // paginatedChannels is derived — listing its deps individually avoids stale closure
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeTab, currentPage, patterns, groupsConfig, channels])

  const loadData = async () => {
    try {
      setLoading(true)

      // Load all channels directly
      const channelsResponse = await channelsAPI.getChannels()
      const channelsToLoad = channelsResponse.data || []

      const [patternsResponse, groupsResponse, orderResponse, m3uAccountsResponse, profilesResponse] = await Promise.all([
        regexAPI.getPatterns(),
        channelsAPI.getGroups(),
        channelOrderAPI.getOrder().catch(() => ({ data: { order: [] } })), // Handle case where no order is saved
        m3uAPI.getAccounts().catch(() => ({ data: { accounts: [] } })), // Load M3U accounts
        automationAPI.getProfiles().catch(() => ({ data: [] }))
      ])

      setChannels(channelsToLoad)
      setPatterns(patternsResponse.data.patterns || {})
      setGroups(groupsResponse.data || [])
      setProfiles(profilesResponse.data || [])


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

  const handleBatchAssignEpgProfile = async (profileId) => {
    if (selectedChannels.size === 0) {
      toast({
        title: "No Channels Selected",
        description: "Please select at least one channel",
        variant: "destructive"
      })
      return
    }

    try {
      setLoading(true)
      const channelIds = Array.from(selectedChannels)
      const targetProfileId = profileId === 'none' ? null : profileId

      await automationAPI.assignEpgChannels(channelIds, targetProfileId)

      toast({
        title: "Success",
        description: `EPG scheduled profile ${targetProfileId ? 'assigned to' : 'removed from'} ${channelIds.length} channels`,
      })

      loadData()
    } catch (err) {
      console.error('Failed to batch assign EPG scheduled profile:', err)
      toast({
        title: "Error",
        description: "Failed to assign EPG scheduled profile to channels",
        variant: "destructive"
      })
    } finally {
      setLoading(false)
      setAssignEpgProfileId('')
    }
  }

  const handleBatchAssignPeriods = () => {
    if (selectedChannels.size === 0) {
      toast({
        title: "No Channels Selected",
        description: "Please select at least one channel",
        variant: "destructive"
      })
      return
    }
    setBatchPeriodsDialogOpen(true)
  }

  // --- Group Configuration Handlers ---

  const loadGroupsConfig = async () => {
    if (groups.length === 0) return
    try {
      setLoadingGroupsConfig(true)
      const configMap = {}
      const [, epgAssignmentsRes, profileAssignmentsRes] = await Promise.all([
        Promise.all(groups.map(async (group) => {
          try {
            const [periodsRes, regexCfgRes] = await Promise.all([
              automationAPI.getGroupPeriods(group.id),
              regexAPI.getGroupConfig(group.id).catch(() => ({ data: {} }))
            ])
            configMap[group.id] = {
              periods: periodsRes.data || [],
              matching: regexCfgRes.data || {}
            }
          } catch {
            configMap[group.id] = { periods: [], matching: {} }
          }
        })),
        automationAPI.getGroupEpgAssignments().catch(() => ({ data: {} })),
        automationAPI.getGroupAssignments().catch(() => ({ data: {} }))
      ])
      const epgAssignments = epgAssignmentsRes.data || {}
      const profileAssignments = profileAssignmentsRes.data || {}
      // Merge EPG and automation profile assignments into configMap
      groups.forEach((group) => {
        if (!configMap[group.id]) {
          configMap[group.id] = { periods: [], matching: {} }
        }
        configMap[group.id].epg_profile_id = epgAssignments[String(group.id)] || null
        configMap[group.id].profile_id = profileAssignments[String(group.id)] || null
      })
      setGroupsConfig(configMap)
    } catch (err) {
      console.error('Failed to load groups config:', err)
    } finally {
      setLoadingGroupsConfig(false)
    }
  }

  const handleOpenGroupAssignProfile = (group) => {
    setSelectedGroupForConfig(group)
    const currentAssignment = (groupsConfig[group.id] || {}).profile_id || ''
    setGroupProfileId(currentAssignment)
    setGroupAssignProfileDialogOpen(true)
  }

  const handleSaveGroupProfile = async () => {
    if (!selectedGroupForConfig) return
    try {
      setSavingGroupProfile(true)
      await automationAPI.assignGroup(selectedGroupForConfig.id, groupProfileId || null)
      toast({
        title: "Success",
        description: groupProfileId
          ? `Automation profile assigned to group "${selectedGroupForConfig.name}"`
          : `Automation profile removed from group "${selectedGroupForConfig.name}"`
      })
      setGroupAssignProfileDialogOpen(false)
      await loadData()
      await loadGroupsConfig()
    } catch (err) {
      toast({
        title: "Error",
        description: err.response?.data?.error || "Failed to assign profile to group",
        variant: "destructive"
      })
    } finally {
      setSavingGroupProfile(false)
    }
  }

  const handleOpenGroupAssignPeriods = (group) => {
    setSelectedGroupForConfig(group)
    setGroupPeriodAssignments([])
    setGroupAssignPeriodsDialogOpen(true)
  }

  const handleOpenGroupAssignEpgProfile = (group) => {
    setSelectedGroupForConfig(group)
    const currentAssignment = (groupsConfig[group.id] || {}).epg_profile_id || ''
    setGroupEpgProfileId(currentAssignment)
    setGroupAssignEpgProfileDialogOpen(true)
  }

  const handleSaveGroupEpgProfile = async () => {
    if (!selectedGroupForConfig) return
    try {
      setSavingGroupEpgProfile(true)
      await automationAPI.assignEpgGroup(selectedGroupForConfig.id, groupEpgProfileId || null)
      toast({
        title: "Success",
        description: groupEpgProfileId
          ? `EPG scheduled profile assigned to group "${selectedGroupForConfig.name}"`
          : `EPG scheduled profile removed from group "${selectedGroupForConfig.name}"`
      })
      setGroupAssignEpgProfileDialogOpen(false)
      await loadData()
      await loadGroupsConfig()
    } catch (err) {
      toast({
        title: "Error",
        description: err.response?.data?.error || "Failed to assign EPG scheduled profile to group",
        variant: "destructive"
      })
    } finally {
      setSavingGroupEpgProfile(false)
    }
  }

  const handleOpenGroupAssignMatching = (group) => {
    setSelectedGroupForConfig(group)
    setGroupAssignMatchingDialogOpen(true)
  }

  const handleSaveGroupPeriods = async (assignments, replace = false) => {
    if (!selectedGroupForConfig || assignments.length === 0) return
    try {
      setSavingGroupPeriods(true)
      await automationAPI.batchAssignPeriodsToGroups(
        [selectedGroupForConfig.id],
        assignments,
        replace
      )
      toast({
        title: "Success",
        description: `Assigned ${assignments.length} period${assignments.length !== 1 ? 's' : ''} to group "${selectedGroupForConfig.name}"`
      })
      setGroupAssignPeriodsDialogOpen(false)
      await loadGroupsConfig()
    } catch (err) {
      toast({
        title: "Error",
        description: err.response?.data?.error || "Failed to assign periods to group",
        variant: "destructive"
      })
    } finally {
      setSavingGroupPeriods(false)
    }
  }

  const handleRemoveGroupPeriod = async (group, periodId) => {
    try {
      await automationAPI.removePeriodFromGroups(periodId, [group.id])
      toast({
        title: "Success",
        description: `Period removed from group "${group.name}"`
      })
      await loadGroupsConfig()
    } catch (err) {
      toast({
        title: "Error",
        description: err.response?.data?.error || "Failed to remove period from group",
        variant: "destructive"
      })
    }
  }

  const handleSaveGroupMatching = async (payload) => {
    if (!selectedGroupForConfig) return
    try {
      setSavingGroupMatching(true)
      await regexAPI.saveGroupConfig(selectedGroupForConfig.id, payload)
      toast({
        title: 'Success',
        description: `Group matching updated for "${selectedGroupForConfig.name}"`
      })
      setGroupAssignMatchingDialogOpen(false)
      await loadGroupsConfig()
    } catch (err) {
      toast({
        title: 'Error',
        description: err.response?.data?.error || 'Failed to update group matching',
        variant: 'destructive'
      })
    } finally {
      setSavingGroupMatching(false)
    }
  }

  const handleDeleteGroupMatching = async () => {
    if (!selectedGroupForConfig) return
    try {
      setSavingGroupMatching(true)
      await regexAPI.deleteGroupConfig(selectedGroupForConfig.id)
      toast({
        title: 'Success',
        description: `Group matching cleared for "${selectedGroupForConfig.name}"`
      })
      setGroupAssignMatchingDialogOpen(false)
      await loadGroupsConfig()
    } catch (err) {
      toast({
        title: 'Error',
        description: err.response?.data?.error || 'Failed to clear group matching',
        variant: 'destructive'
      })
    } finally {
      setSavingGroupMatching(false)
    }
  }

  const handleUpdateMatchSettings = async (channelId, settings) => {
    try {
      await regexAPI.updateMatchSettings(channelId, settings)

      // Update patterns state locally to reflect change immediately
      setPatterns(prev => {
        const channelPattern = prev[channelId] || prev[String(channelId)] || {}
        return {
          ...prev,
          [channelId]: {
            ...channelPattern,
            ...settings
          }
        }
      })

      toast({
        title: "Success",
        description: "Match settings updated successfully"
      })
    } catch (err) {
      console.error('Failed to update match settings:', err)
      toast({
        title: "Error",
        description: "Failed to update match settings",
        variant: "destructive"
      })
    }
  }

  const handleAssignEpgProfile = async (channelId, profileId) => {
    try {
      await automationAPI.assignEpgChannel(channelId, profileId)
      // Update channel state locally so the dropdown reflects the change immediately
      setChannels(prev => prev.map(ch =>
        ch.id === channelId
          ? { ...ch, channel_epg_scheduled_profile_id: profileId || null, epg_scheduled_profile_id: profileId || null }
          : ch
      ))
      toast({
        title: "Success",
        description: profileId
          ? "EPG scheduled profile assigned to channel"
          : "EPG scheduled profile removed from channel"
      })
    } catch (err) {
      console.error('Failed to assign EPG scheduled profile:', err)
      toast({
        title: "Error",
        description: "Failed to assign EPG scheduled profile",
        variant: "destructive"
      })
    }
  }




  const handleEditRegex = (channelId, patternIndex) => {
    setEditingChannelId(channelId)
    setEditingPatternIndex(patternIndex)

    // If editing an existing pattern, load it
    if (patternIndex !== null) {
      const channelPatterns = patterns[channelId] || patterns[String(channelId)]

      // Support both new and old format
      if (channelPatterns?.regex_patterns && channelPatterns.regex_patterns[patternIndex]) {
        // New format with per-pattern m3u_accounts and priority
        const patternObj = channelPatterns.regex_patterns[patternIndex]
        setNewPattern(patternObj.pattern || '')
        setSelectedM3uAccounts(patternObj.m3u_accounts || [])
      } else if (channelPatterns && channelPatterns.regex && channelPatterns.regex[patternIndex]) {
        // Old format - pattern is a string, m3u_accounts is channel-level
        setNewPattern(channelPatterns.regex[patternIndex])
        // Load channel-level M3U account selection (if any)
        if (channelPatterns.m3u_accounts) {
          setSelectedM3uAccounts(channelPatterns.m3u_accounts)
        } else {
          setSelectedM3uAccounts([])  // Empty means all M3U accounts
        }
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

  const handleTestPattern = useCallback(async (mode = 'regex_only', loadMore = false, channelId = null) => {
    // Determine context based on mode and optional channelId
    let contextChannelId = channelId || editingChannelId
    let contextPattern = newPattern
    let contextM3uAccounts = selectedM3uAccounts

    // If testing from outside match dialog (e.g. TVG preview or Global preview), we rely on passed channelId
    if (!contextChannelId) return

    // Increment request ID to track this request
    const requestId = ++testRequestIdRef.current
    const currentMaxMatches = loadMore && testResults ? (testResults.max_matches || 50) + 50 : 50

    try {
      setTestingPattern(true)
      const channel = channels.find(ch => ch.id === contextChannelId)
      const profile = profiles.find(p => p.id === channel?.automation_profile_id)
      const channelPatternConfig = patterns[contextChannelId] || patterns[String(contextChannelId)] || {}
      const channelGroupId = channel?.group_id ?? channel?.channel_group_id
      let groupMatchingConfig = channelGroupId ? ((groupsConfig[channelGroupId] || {}).matching || {}) : {}

      // Global preview can be opened from the Regex tab before group config is loaded.
      // Fetch it on demand so inherited TVG-ID/group regex settings are applied.
      if (mode === 'global' && channelGroupId && Object.keys(groupMatchingConfig).length === 0) {
        try {
          const groupCfgResponse = await regexAPI.getGroupConfig(channelGroupId)
          // Merge response data with safe defaults so downstream .length and boolean
          // checks never throw even if the API returns an unexpected shape.
          groupMatchingConfig = {
            regex_patterns: [],
            match_by_tvg_id: false,
            ...(groupCfgResponse?.data || {}),
          }
          setGroupsConfig(prev => ({
            ...prev,
            [channelGroupId]: {
              periods: [],
              ...(prev[channelGroupId] || {}),
              matching: groupMatchingConfig,
            },
          }))
        } catch (err) {
          console.warn(`Failed to load group matching config for group ${channelGroupId}:`, err)
        }
      }

      // Treat placeholder channel configs (empty patterns + TVG disabled + enabled=true) as no override,
      // so group-level matching can still be inherited.
      const hasChannelRegexPatterns =
        (Array.isArray(channelPatternConfig.regex_patterns) && channelPatternConfig.regex_patterns.length > 0) ||
        (Array.isArray(channelPatternConfig.regex) && channelPatternConfig.regex.length > 0)
      const hasExplicitChannelOverride =
        hasChannelRegexPatterns ||
        channelPatternConfig.match_by_tvg_id === true ||
        channelPatternConfig.enabled === false

      const effectivePatternConfig = hasExplicitChannelOverride ? channelPatternConfig : groupMatchingConfig

      // Prepare request data based on mode
      let requestData = {
        channel_name: channel?.name || '',
        tvg_id: channel?.tvg_id,
        max_matches: currentMaxMatches
      }

      if (mode === 'tvg_only') {
        requestData.match_by_tvg_id = true
        requestData.regex_patterns = []
      } else if (mode === 'regex_only') {
        // Test ONLY the current pattern being edited
        requestData.match_by_tvg_id = false
        requestData.regex_patterns = [{
          pattern: contextPattern,
          m3u_accounts: contextM3uAccounts.length > 0 ? contextM3uAccounts : undefined
        }]
      } else if (mode === 'global') {
        // Full channel configuration test
        requestData.match_by_tvg_id = effectivePatternConfig.match_by_tvg_id || false

        // Reconstruct regex patterns from stored config
        let regexPatterns = []
        if (effectivePatternConfig.regex_patterns) {
          regexPatterns = effectivePatternConfig.regex_patterns
        } else if (effectivePatternConfig.regex) {
          regexPatterns = effectivePatternConfig.regex.map(p => ({
            pattern: p,
            m3u_accounts: effectivePatternConfig.m3u_accounts
          }))
        }

        requestData.regex_patterns = regexPatterns
      }

      const response = await regexAPI.testMatchLive(requestData)

      // Only update state if this is still the latest request
      if (requestId !== testRequestIdRef.current) return

      const matches = response.data.matches || []

      setTestResults({
        valid: true,
        matches: matches,
        match_count: matches.length,
        total_matches: response.data.total_matches,
        max_matches: currentMaxMatches,
        has_more: matches.length >= currentMaxMatches,
        mode: mode, // Store mode to know how to load more
        channelId: contextChannelId // Store channel ID for load more context
      })

    } catch (err) {
      if (requestId !== testRequestIdRef.current) return

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
      if (requestId === testRequestIdRef.current) {
        setTestingPattern(false)
      }
    }
  }, [newPattern, editingChannelId, channels, selectedM3uAccounts, toast, patterns, profiles, testResults, groupsConfig])

  const handlePreviewMatch = useCallback((channelId, mode) => {
    setTestResults(null)

    let title = "Match Preview"
    if (mode === 'tvg_only') title = "TVG-ID Match Preview"
    else if (mode === 'global') title = "Global Channel Match Preview"

    setPreviewTitle(title)
    setPreviewResultsOpen(true)

    // Trigger test immediately
    handleTestPattern(mode, false, channelId)
  }, [handleTestPattern])

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

      // Build regex_patterns array in new format with per-pattern m3u_accounts
      let updatedRegexPatterns = []

      // Get existing patterns in new format
      if (channelPatterns?.regex_patterns) {
        // Already in new format
        updatedRegexPatterns = [...channelPatterns.regex_patterns]
      } else if (channelPatterns?.regex) {
        // Convert from old format
        const oldM3uAccounts = channelPatterns.m3u_accounts
        updatedRegexPatterns = channelPatterns.regex.map(p => ({
          pattern: p,
          m3u_accounts: oldM3uAccounts
        }))
      }

      if (editingPatternIndex !== null) {
        // Editing existing pattern
        updatedRegexPatterns[editingPatternIndex] = {
          pattern: newPattern,
          m3u_accounts: selectedM3uAccounts.length > 0 ? selectedM3uAccounts : null
        }
      } else {
        // Adding new pattern
        updatedRegexPatterns.push({
          pattern: newPattern,
          m3u_accounts: selectedM3uAccounts.length > 0 ? selectedM3uAccounts : null
        })
      }

      // Send in new format
      await regexAPI.addPattern({
        channel_id: editingChannelId,
        name: channel?.name || '',
        regex: updatedRegexPatterns,  // Send array of objects with per-pattern m3u_accounts
        enabled: channelPatterns?.enabled !== false
      })

      toast({
        title: "Success",
        description: editingPatternIndex !== null ? "Pattern updated successfully" : "Pattern added successfully"
      })

      // Update patterns state directly instead of reloading the entire page
      setPatterns(prevPatterns => ({
        ...prevPatterns,
        [editingChannelId]: {
          ...channelPatterns,
          regex_patterns: updatedRegexPatterns,
          enabled: channelPatterns?.enabled !== false
        }
      }))

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

      // Get regex patterns in the appropriate format
      let regexPatterns = []
      if (channelPatterns?.regex_patterns) {
        regexPatterns = channelPatterns.regex_patterns
      } else if (channelPatterns?.regex) {
        // Convert old format
        const oldM3uAccounts = channelPatterns.m3u_accounts
        regexPatterns = channelPatterns.regex.map(p => ({
          pattern: p,
          m3u_accounts: oldM3uAccounts
        }))
      } else {
        return // No patterns to delete
      }

      const updatedRegexPatterns = regexPatterns.filter((_, index) => index !== patternIndex)

      if (updatedRegexPatterns.length === 0) {
        // If no patterns left, delete the entire pattern config
        await regexAPI.deletePattern(channelId)

        // Update state to remove patterns
        setPatterns(prevPatterns => {
          const newPatterns = { ...prevPatterns }
          delete newPatterns[channelId]
          delete newPatterns[String(channelId)]
          return newPatterns
        })
      } else {
        // Update with remaining patterns
        await regexAPI.addPattern({
          channel_id: channelId,
          name: channel?.name || '',
          regex: updatedRegexPatterns,  // Send array of objects
          enabled: channelPatterns.enabled !== false
        })

        // Update patterns state directly
        setPatterns(prevPatterns => ({
          ...prevPatterns,
          [channelId]: {
            ...channelPatterns,
            regex_patterns: updatedRegexPatterns,
            enabled: channelPatterns.enabled !== false
          }
        }))
      }

      toast({
        title: "Success",
        description: "Pattern deleted successfully"
      })
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

  const handleBulkDelete = async () => {
    if (selectedChannels.size === 0) {
      toast({
        title: "No Channels Selected",
        description: "Please select at least one channel",
        variant: "destructive"
      })
      return
    }

    try {
      const response = await regexAPI.bulkDeletePatterns({
        channel_ids: Array.from(selectedChannels)
      })

      toast({
        title: "Success",
        description: response.data.message || `Deleted patterns from ${response.data.success_count} channels`,
      })

      // Reload data and close dialog
      await loadData()
      setDeleteDialogOpen(false)
    } catch (err) {
      toast({
        title: "Error",
        description: err.response?.data?.error || "Failed to delete patterns",
        variant: "destructive"
      })
    }
  }

  const handleOpenEditCommon = async () => {
    if (selectedChannels.size === 0) {
      toast({
        title: "No Channels Selected",
        description: "Please select at least one channel",
        variant: "destructive"
      })
      return
    }

    try {
      setLoadingCommonPatterns(true)
      setEditCommonDialogOpen(true)

      const response = await regexAPI.getCommonPatterns({
        channel_ids: Array.from(selectedChannels)
      })

      setCommonPatterns(response.data.patterns || [])
    } catch (err) {
      toast({
        title: "Error",
        description: err.response?.data?.error || "Failed to load common patterns",
        variant: "destructive"
      })
      setEditCommonDialogOpen(false)
    } finally {
      setLoadingCommonPatterns(false)
    }
  }

  const handleEditCommonPattern = async () => {
    if (!editingCommonPattern || !newCommonPattern.trim()) {
      toast({
        title: "Error",
        description: "Please enter a new pattern",
        variant: "destructive"
      })
      return
    }

    try {
      // Filter to only include channels that are currently selected
      // This ensures we only edit patterns in the selected channels, even if the pattern
      // exists in other channels that were not selected
      const selectedChannelIdsSet = new Set(Array.from(selectedChannels).map(id => String(id)))
      const channelsToEdit = editingCommonPattern.channel_ids.filter(id =>
        selectedChannelIdsSet.has(String(id))
      )

      if (channelsToEdit.length === 0) {
        toast({
          title: "Error",
          description: "No selected channels have this pattern",
          variant: "destructive"
        })
        return
      }

      const response = await regexAPI.bulkEditPattern({
        channel_ids: channelsToEdit,
        old_pattern: editingCommonPattern.pattern,
        new_pattern: newCommonPattern,
        new_m3u_accounts: newCommonPatternM3uAccounts  // Include playlist selection
      })

      toast({
        title: "Success",
        description: response.data.message || `Updated pattern in ${response.data.success_count} channels`,
      })

      // Reload data and common patterns
      await loadData()

      // Reload common patterns to show updated list
      const patternsResponse = await regexAPI.getCommonPatterns({
        channel_ids: Array.from(selectedChannels)
      })
      setCommonPatterns(patternsResponse.data.patterns || [])

      // Close edit mode
      setEditingCommonPattern(null)
      setNewCommonPattern('')
      setNewCommonPatternM3uAccounts(null)
    } catch (err) {
      toast({
        title: "Error",
        description: err.response?.data?.error || "Failed to edit pattern",
        variant: "destructive"
      })
    }
  }

  const handleBulkMatchSettings = async (enabled) => {
    if (selectedChannels.size === 0) return

    try {
      const response = await regexAPI.updateBulkMatchSettings({
        channel_ids: Array.from(selectedChannels),
        settings: { match_by_tvg_id: enabled }
      })

      toast({
        title: "Success",
        description: response.data.message || `Updated settings for ${response.data.success_count} channels`
      })

      // Reload to reflect changes
      loadData()
    } catch (err) {
      toast({
        title: "Error",
        description: err.response?.data?.error || "Failed to update match settings",
        variant: "destructive"
      })
    }
  }

  const handleDeleteSingleCommonPattern = async (patternInfo) => {
    try {
      // Delete this pattern from all selected channels
      const selectedChannelIdsSet = new Set(Array.from(selectedChannels).map(id => String(id)))
      const channelsToDelete = patternInfo.channel_ids.filter(id =>
        selectedChannelIdsSet.has(String(id))
      )

      if (channelsToDelete.length === 0) {
        toast({
          title: "Error",
          description: "No selected channels have this pattern",
          variant: "destructive"
        })
        return
      }

      // Delete pattern from each channel
      let successCount = 0
      for (const channelId of channelsToDelete) {
        try {
          const channelPatterns = patterns[channelId] || patterns[String(channelId)]
          const channel = channels.find(ch => ch.id === channelId || ch.id === String(channelId))

          // Normalize to new format
          let regexPatterns = normalizePatternData(channelPatterns)

          // Remove the pattern
          const updatedPatterns = regexPatterns.filter(p => p.pattern !== patternInfo.pattern)

          if (updatedPatterns.length === 0) {
            // Delete entire channel pattern config
            await regexAPI.deletePattern(channelId)
          } else {
            // Update with remaining patterns
            await regexAPI.addPattern({
              channel_id: channelId,
              name: channel?.name || '',
              regex: updatedPatterns,
              enabled: channelPatterns?.enabled !== false
            })
          }
          successCount++
        } catch (err) {
          console.error(`Failed to delete pattern from channel ${channelId}:`, err)
        }
      }

      toast({
        title: "Success",
        description: `Deleted pattern from ${successCount} channel${successCount !== 1 ? 's' : ''}`
      })

      // Reload data and common patterns
      await loadData()
      const patternsResponse = await regexAPI.getCommonPatterns({
        channel_ids: Array.from(selectedChannels)
      })
      setCommonPatterns(patternsResponse.data.patterns || [])
    } catch (err) {
      toast({
        title: "Error",
        description: "Failed to delete pattern",
        variant: "destructive"
      })
    }
  }

  const handleDeleteSelectedCommonPatterns = async () => {
    if (selectedCommonPatterns.size === 0) {
      toast({
        title: "No Patterns Selected",
        description: "Please select at least one pattern to delete",
        variant: "destructive"
      })
      return
    }

    try {
      const patternsToDelete = commonPatterns.filter((_, idx) => selectedCommonPatterns.has(idx))
      let totalSuccess = 0

      for (const patternInfo of patternsToDelete) {
        const selectedChannelIdsSet = new Set(Array.from(selectedChannels).map(id => String(id)))
        const channelsToDelete = patternInfo.channel_ids.filter(id =>
          selectedChannelIdsSet.has(String(id))
        )

        for (const channelId of channelsToDelete) {
          try {
            const channelPatterns = patterns[channelId] || patterns[String(channelId)]
            const channel = channels.find(ch => ch.id === channelId || ch.id === String(channelId))

            let regexPatterns = normalizePatternData(channelPatterns)
            const updatedPatterns = regexPatterns.filter(p => p.pattern !== patternInfo.pattern)

            if (updatedPatterns.length === 0) {
              await regexAPI.deletePattern(channelId)
            } else {
              await regexAPI.addPattern({
                channel_id: channelId,
                name: channel?.name || '',
                regex: updatedPatterns,
                enabled: channelPatterns?.enabled !== false
              })
            }
            totalSuccess++
          } catch (err) {
            console.error(`Failed to delete pattern from channel ${channelId}:`, err)
          }
        }
      }

      toast({
        title: "Success",
        description: `Deleted ${selectedCommonPatterns.size} pattern${selectedCommonPatterns.size !== 1 ? 's' : ''} from ${totalSuccess} channel instance${totalSuccess !== 1 ? 's' : ''}`
      })

      // Clear selection and reload
      setSelectedCommonPatterns(new Set())
      await loadData()
      const patternsResponse = await regexAPI.getCommonPatterns({
        channel_ids: Array.from(selectedChannels)
      })
      setCommonPatterns(patternsResponse.data.patterns || [])
    } catch (err) {
      toast({
        title: "Error",
        description: "Failed to delete patterns",
        variant: "destructive"
      })
    }
  }

  // Mass edit handlers
  const handleMassEditPreview = async () => {
    if (!massEditFindPattern.trim()) {
      toast({
        title: "Error",
        description: "Please enter a find pattern",
        variant: "destructive"
      })
      return
    }

    try {
      setLoadingMassEditPreview(true)
      const response = await regexAPI.massEditPreview({
        channel_ids: Array.from(selectedChannels),
        find_pattern: massEditFindPattern,
        replace_pattern: massEditReplacePattern,
        use_regex: massEditUseRegex
      })

      setMassEditPreview(response.data)

      if (response.data.total_patterns_affected === 0) {
        toast({
          title: "No Changes",
          description: "No patterns will be affected by this find/replace operation",
        })
      }
    } catch (err) {
      toast({
        title: "Error",
        description: err.response?.data?.error || "Failed to preview mass edit",
        variant: "destructive"
      })
      setMassEditPreview(null)
    } finally {
      setLoadingMassEditPreview(false)
    }
  }

  const handleApplyMassEdit = async () => {
    if (!massEditFindPattern.trim()) {
      toast({
        title: "Error",
        description: "Please enter a find pattern",
        variant: "destructive"
      })
      return
    }

    try {
      const response = await regexAPI.massEdit({
        channel_ids: Array.from(selectedChannels),
        find_pattern: massEditFindPattern,
        replace_pattern: massEditReplacePattern,
        use_regex: massEditUseRegex,
        new_m3u_accounts: massEditM3uAccounts
      })

      toast({
        title: "Success",
        description: response.data.message || `Updated ${response.data.success_count} channels`,
      })

      // Reset mass edit mode
      setMassEditMode(false)
      setMassEditFindPattern('')
      setMassEditReplacePattern('')
      setMassEditUseRegex(false)
      setMassEditM3uAccounts(null)
      setMassEditPreview(null)

      // Reload data and common patterns
      await loadData()
      const patternsResponse = await regexAPI.getCommonPatterns({
        channel_ids: Array.from(selectedChannels)
      })
      setCommonPatterns(patternsResponse.data.patterns || [])
    } catch (err) {
      toast({
        title: "Error",
        description: err.response?.data?.error || "Failed to apply mass edit",
        variant: "destructive"
      })
    }
  }

  // Handler for toggling expanded row - ensures only one row is expanded at a time
  const handleToggleExpanded = (channelId) => {
    setExpandedRowId(prevId => prevId === channelId ? null : channelId)
  }

  // Filter channels based on search query and group filter
  // Use orderedChannels as the base to ensure consistent ordering between tabs
  const filteredChannels = orderedChannels.filter(channel => {
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

  // Filter ordered channels - simplify since group visibility is removed
  const visibleOrderedChannels = orderedChannels

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


  // Reset to first page when search changes
  useEffect(() => {
    setCurrentPage(1)
  }, [searchQuery, filterByGroup, sortByGroup])


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
          <TabsList className="grid w-full grid-cols-3">
            <TabsTrigger value="regex">Regex Configuration</TabsTrigger>
            <TabsTrigger value="ordering">Channel Order</TabsTrigger>
            <TabsTrigger value="groups">Group Configuration</TabsTrigger>
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
                  <div className="flex flex-wrap items-center gap-6">
                    {/* Section: Sorting */}
                    <div className="flex items-center gap-4">
                      <div className="text-[10px] font-bold text-muted-foreground uppercase tracking-wider">Sorting</div>
                      <div className="flex items-center gap-2">
                        <Select value={filterByGroup} onValueChange={setFilterByGroup}>
                          <SelectTrigger id="group-filter" className="h-8 w-[140px]">
                            <SelectValue placeholder="Group" />
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

                      <div className="flex items-center gap-2">
                        <Checkbox
                          id="sort-by-group"
                          checked={sortByGroup}
                          onCheckedChange={setSortByGroup}
                        />
                        <Label htmlFor="sort-by-group" className="text-xs whitespace-nowrap cursor-pointer">
                          Sort by Group
                        </Label>
                      </div>
                    </div>

                    <Separator orientation="vertical" className="h-6 hidden lg:block" />

                    {/* Section: Matching */}
                    <div className="flex items-center gap-3">
                      <div className="text-[10px] font-bold text-muted-foreground uppercase tracking-wider">Matching</div>
                      <Tooltip>
                        <TooltipTrigger asChild>
                          <Button
                            size="sm"
                            variant="outline"
                            onClick={() => setBulkDialogOpen(true)}
                            disabled={selectedChannels.size === 0}
                            className="h-8 w-8 p-0"
                          >
                            <Plus className="h-4 w-4" />
                          </Button>
                        </TooltipTrigger>
                        <TooltipContent><p>Add Regex</p></TooltipContent>
                      </Tooltip>
                      <Tooltip>
                        <TooltipTrigger asChild>
                          <Button
                            size="sm"
                            variant="outline"
                            onClick={handleBulkHealthCheck}
                            disabled={selectedChannels.size === 0 || bulkCheckingChannels}
                            className="h-8 w-8 p-0 text-blue-600 dark:text-green-500 border-blue-600 dark:border-green-500"
                          >
                            {bulkCheckingChannels ? <Loader2 className="h-4 w-4 animate-spin" /> : <Activity className="h-4 w-4" />}
                          </Button>
                        </TooltipTrigger>
                        <TooltipContent><p>Health Check</p></TooltipContent>
                      </Tooltip>
                      <Tooltip>
                        <TooltipTrigger asChild>
                          <Button
                            size="sm"
                            variant="outline"
                            onClick={handleOpenEditCommon}
                            disabled={selectedChannels.size === 0}
                            className="h-8 w-8 p-0"
                          >
                            <Edit2 className="h-4 w-4" />
                          </Button>
                        </TooltipTrigger>
                        <TooltipContent><p>Mass Regex Edit</p></TooltipContent>
                      </Tooltip>

                      <DropdownMenu>
                        <Tooltip>
                          <TooltipTrigger asChild>
                            <DropdownMenuTrigger asChild>
                              <Button variant="outline" size="sm" disabled={selectedChannels.size === 0} className="h-8 w-10 px-0 font-bold text-xs ring-offset-background transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2">
                                ID
                              </Button>
                            </DropdownMenuTrigger>
                          </TooltipTrigger>
                          <TooltipContent>Match Settings</TooltipContent>
                        </Tooltip>
                        <DropdownMenuContent>
                          <DropdownMenuLabel>Match by TVG-ID</DropdownMenuLabel>
                          <DropdownMenuSeparator />
                          <DropdownMenuItem onClick={() => handleBulkMatchSettings(true)}>Enable</DropdownMenuItem>
                          <DropdownMenuItem onClick={() => handleBulkMatchSettings(false)}>Disable</DropdownMenuItem>
                        </DropdownMenuContent>
                      </DropdownMenu>
                    </div>

                    <Separator orientation="vertical" className="h-6 hidden lg:block" />

                    {/* Section: Periods */}
                    <div className="flex items-center gap-3">
                      <div className="text-[10px] font-bold text-muted-foreground uppercase tracking-wider">Periods</div>
                      <Tooltip>
                        <TooltipTrigger asChild>
                          <Button
                            variant="outline"
                            size="sm"
                            onClick={handleBatchAssignPeriods}
                            disabled={selectedChannels.size === 0}
                            className="h-8 px-2"
                          >
                            <div className="flex items-center gap-0.5">
                              <Clock className="h-4 w-4" />
                              <Plus className="h-3 w-3" />
                            </div>
                          </Button>
                        </TooltipTrigger>
                        <TooltipContent>Assign Periods</TooltipContent>
                      </Tooltip>

                      <Tooltip>
                        <TooltipTrigger asChild>
                          <Button
                            variant="outline"
                            size="sm"
                            onClick={() => setBulkPeriodEditOpen(true)}
                            disabled={selectedChannels.size === 0}
                            className="h-8 w-8 p-0"
                          >
                            <Edit className="h-4 w-4" />
                          </Button>
                        </TooltipTrigger>
                        <TooltipContent>Mass Period Edit</TooltipContent>
                      </Tooltip>
                    </div>

                    <Separator orientation="vertical" className="h-6 hidden lg:block" />

                    {/* Section: EPG Profile */}
                    <div className="flex items-center gap-3">
                      <div className="text-[10px] font-bold text-muted-foreground uppercase tracking-wider">EPG Profile</div>
                      <Select
                        value={assignEpgProfileId}
                        onValueChange={(v) => {
                          setAssignEpgProfileId(v)
                          handleBatchAssignEpgProfile(v)
                        }}
                        disabled={selectedChannels.size === 0}
                      >
                        <SelectTrigger className="h-8 w-[160px] text-xs">
                          <SelectValue placeholder="Assign EPG profile…" />
                        </SelectTrigger>
                        <SelectContent>
                          <SelectItem value="none">— Remove EPG profile —</SelectItem>
                          {profiles.map((profile) => (
                            <SelectItem key={profile.id} value={profile.id}>{profile.name}</SelectItem>
                          ))}
                        </SelectContent>
                      </Select>
                    </div>

                    <Separator orientation="vertical" className="h-6 hidden lg:block" />

                    <div className="flex items-center gap-2 ml-auto">
                      <Badge variant="secondary" className="whitespace-nowrap text-[10px]">
                        {selectedChannels.size} selected
                      </Badge>
                      <Tooltip>
                        <TooltipTrigger asChild>
                          <Button
                            size="sm"
                            variant="destructive"
                            onClick={() => setDeleteDialogOpen(true)}
                            disabled={selectedChannels.size === 0}
                            className="h-8 w-8 p-0"
                          >
                            <Trash2 className="h-4 w-4" />
                          </Button>
                        </TooltipTrigger>
                        <TooltipContent><p>Delete Selected</p></TooltipContent>
                      </Tooltip>
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
                          <div>Nº of Periods</div>
                          <div>Regex Patterns</div>
                          <div>Actions</div>
                        </div>
                      </div>

                      {/* Table Rows */}
                      <div className="divide-y">
                        {paginatedChannels.map(channel => {
                          const group = groups.find(g => g.id === channel.channel_group_id)


                          return (
                            <RegexTableRow
                              key={channel.id}
                              channel={channel}
                              group={group}
                              groups={groups}
                              groupsConfig={groupsConfig}
                              profiles={profiles}
                              patterns={patterns}
                              selectedChannels={selectedChannels}
                              onToggleChannel={handleToggleChannel}
                              onEditRegex={handleEditRegex}
                              onDeletePattern={handleDeletePattern}
                              expandedRowId={expandedRowId}
                              onToggleExpanded={handleToggleExpanded}
                              onCheckChannel={handleCheckChannel}
                              checkingChannel={checkingChannel}
                              m3uAccounts={m3uAccounts}
                              onUpdateMatchSettings={handleUpdateMatchSettings}
                              onPreviewMatch={handlePreviewMatch}
                              onRefresh={loadData}
                              onAssignEpgProfile={handleAssignEpgProfile}
                              matchCount={matchCounts[String(channel.id)]}
                              totalStreamCount={totalStreamCount}
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

          <TabsContent value="groups" className="space-y-6">
            <Card>
              <CardHeader>
                <div className="flex items-center justify-between">
                  <div>
                    <CardTitle>Group Configuration</CardTitle>
                    <CardDescription>
                      Assign automation profiles and periods to channel groups. New channels added to a group will inherit these settings.
                    </CardDescription>
                  </div>
                  <Button variant="outline" size="sm" onClick={loadGroupsConfig} disabled={loadingGroupsConfig}>
                    {loadingGroupsConfig ? <Loader2 className="h-4 w-4 animate-spin" /> : <RotateCcw className="h-4 w-4" />}
                  </Button>
                </div>
              </CardHeader>
              <CardContent>
                {groups.length === 0 ? (
                  <div className="text-center py-8 text-muted-foreground">
                    No channel groups found
                  </div>
                ) : loadingGroupsConfig ? (
                  <div className="flex items-center justify-center py-8">
                    <Loader2 className="h-8 w-8 animate-spin text-muted-foreground" />
                  </div>
                ) : (
                  <div className="space-y-3">
                    {groups.map((group) => {
                      const config = groupsConfig[group.id] || { periods: [], matching: {} }
                      const matching = config.matching || {}
                      const matchingPatterns = Array.isArray(matching.regex_patterns)
                        ? matching.regex_patterns
                        : []
                      const matchingPatternCount = matchingPatterns.length
                      const groupTvgEnabled = Boolean(matching.match_by_tvg_id)
                      const groupEpgProfile = config.epg_profile_id
                        ? (profiles || []).find(p => String(p.id) === String(config.epg_profile_id))
                        : null
                      return (
                        <Card key={group.id} className="border">
                          <CardContent className="p-4">
                            <div className="flex items-start justify-between gap-4">
                              <div className="flex-1 min-w-0">
                                <div className="flex items-center gap-2 mb-2">
                                  <h3 className="font-semibold text-base">{group.name}</h3>
                                  <Badge variant="secondary" className="text-xs">
                                    ID: {group.id}
                                  </Badge>
                                </div>

                                {/* Periods section */}
                                <div className="space-y-1">
                                  <div className="text-xs text-muted-foreground font-medium uppercase tracking-wider mb-1">
                                    Automation Periods ({config.periods.length})
                                  </div>
                                  {config.periods.length === 0 ? (
                                    <p className="text-sm text-muted-foreground italic">No periods assigned</p>
                                  ) : (
                                    <div className="flex flex-wrap gap-2">
                                      {config.periods.map((period) => (
                                        <Badge key={period.id} variant="outline" className="gap-1 pr-1">
                                          <Clock className="h-3 w-3" />
                                          <span>{period.name}</span>
                                          {period.profile_name && (
                                            <span className="text-muted-foreground">· {period.profile_name}</span>
                                          )}
                                          <Button
                                            variant="ghost"
                                            size="sm"
                                            className="h-4 w-4 p-0 ml-1 hover:bg-destructive hover:text-destructive-foreground rounded-full"
                                            onClick={() => handleRemoveGroupPeriod(group, period.id)}
                                          >
                                            <X className="h-3 w-3" />
                                          </Button>
                                        </Badge>
                                      ))}
                                    </div>
                                  )}
                                </div>

                                {/* Matching section */}
                                <div className="space-y-1 mt-4">
                                  <div className="text-xs text-muted-foreground font-medium uppercase tracking-wider mb-1">
                                    Matching
                                  </div>
                                  <div className="flex flex-wrap gap-2">
                                    <Badge variant="outline" className="text-xs">
                                      {matchingPatternCount} regex pattern{matchingPatternCount !== 1 ? 's' : ''}
                                    </Badge>
                                    <Badge variant={groupTvgEnabled ? 'default' : 'secondary'} className="text-xs">
                                      TVG-ID {groupTvgEnabled ? 'On' : 'Off'}
                                    </Badge>
                                  </div>
                                </div>

                                {/* EPG Profile section */}
                                <div className="space-y-1 mt-4">
                                  <div className="text-xs text-muted-foreground font-medium uppercase tracking-wider mb-1">
                                    EPG Profile
                                  </div>
                                  {groupEpgProfile ? (
                                    <Badge variant="outline" className="gap-1 text-xs">
                                      <CalendarClock className="h-3 w-3" />
                                      {groupEpgProfile.name}
                                    </Badge>
                                  ) : (
                                    <p className="text-sm text-muted-foreground italic">No profile assigned</p>
                                  )}
                                </div>
                              </div>

                              {/* Action buttons */}
                              <div className="flex gap-2 flex-shrink-0 flex-wrap">
                                <Tooltip>
                                  <TooltipTrigger asChild>
                                    <Button
                                      variant="outline"
                                      size="sm"
                                      onClick={() => handleOpenGroupAssignPeriods(group)}
                                      className="h-8"
                                    >
                                      <Clock className="h-4 w-4 mr-1" />
                                      Periods
                                    </Button>
                                  </TooltipTrigger>
                                  <TooltipContent>Assign automation periods and profiles to group</TooltipContent>
                                </Tooltip>
                                <Tooltip>
                                  <TooltipTrigger asChild>
                                    <Button
                                      variant="outline"
                                      size="sm"
                                      onClick={() => handleOpenGroupAssignMatching(group)}
                                      className="h-8"
                                    >
                                      <Edit2 className="h-4 w-4 mr-1" />
                                      Matching
                                    </Button>
                                  </TooltipTrigger>
                                  <TooltipContent>Configure group regex and TVG-ID matching</TooltipContent>
                                </Tooltip>
                                <Tooltip>
                                  <TooltipTrigger asChild>
                                    <Button
                                      variant="outline"
                                      size="sm"
                                      onClick={() => handleOpenGroupAssignEpgProfile(group)}
                                      className="h-8"
                                    >
                                      <CalendarClock className="h-4 w-4 mr-1" />
                                      EPG Profile
                                    </Button>
                                  </TooltipTrigger>
                                  <TooltipContent>Assign EPG scheduled profile to group</TooltipContent>
                                </Tooltip>
                              </div>
                            </div>
                          </CardContent>
                        </Card>
                      )
                    })}
                  </div>
                )}
              </CardContent>
            </Card>
          </TabsContent>
        </Tabs>

        {/* Group Assign Periods Dialog */}
        {selectedGroupForConfig && (
          <GroupAssignPeriodsDialog
            open={groupAssignPeriodsDialogOpen}
            onOpenChange={setGroupAssignPeriodsDialogOpen}
            group={selectedGroupForConfig}
            initialPeriods={(groupsConfig[selectedGroupForConfig.id] || {}).periods || []}
            onSave={handleSaveGroupPeriods}
            saving={savingGroupPeriods}
          />
        )}

        {/* Group Matching Dialog */}
        {selectedGroupForConfig && (
          <GroupAssignMatchingDialog
            open={groupAssignMatchingDialogOpen}
            onOpenChange={setGroupAssignMatchingDialogOpen}
            group={selectedGroupForConfig}
            initialConfig={(groupsConfig[selectedGroupForConfig.id] || {}).matching || {}}
            onSave={handleSaveGroupMatching}
            onDelete={handleDeleteGroupMatching}
            saving={savingGroupMatching}
          />
        )}

        {/* Group Assign EPG Profile Dialog */}
        <Dialog open={groupAssignEpgProfileDialogOpen} onOpenChange={setGroupAssignEpgProfileDialogOpen}>
          <DialogContent className="sm:max-w-[450px]">
            <DialogHeader>
              <DialogTitle>Assign EPG Scheduled Profile to Group</DialogTitle>
              <DialogDescription>
                {selectedGroupForConfig
                  ? `Assign an EPG scheduled profile to group "${selectedGroupForConfig.name}". When an EPG scheduled check fires, channels in this group will use this profile instead of their automation period profile.`
                  : ''}
              </DialogDescription>
            </DialogHeader>
            <div className="space-y-4 py-4">
              <div className="space-y-2">
                <Label>EPG Scheduled Profile</Label>
                <Select
                  value={groupEpgProfileId}
                  onValueChange={(v) => setGroupEpgProfileId(v === 'none' ? '' : v)}
                >
                  <SelectTrigger>
                    <SelectValue placeholder="Select a profile (or clear to remove)" />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="none">— Remove EPG profile assignment —</SelectItem>
                    {profiles.map((profile) => (
                      <SelectItem key={profile.id} value={profile.id}>
                        {profile.name}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
                <p className="text-xs text-muted-foreground">
                  Select a profile to use for EPG scheduled stream checks, or choose the empty option to remove the current assignment.
                </p>
              </div>
            </div>
            <DialogFooter>
              <Button variant="outline" onClick={() => setGroupAssignEpgProfileDialogOpen(false)}>
                Cancel
              </Button>
              <Button onClick={handleSaveGroupEpgProfile} disabled={savingGroupEpgProfile}>
                {savingGroupEpgProfile && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}
                Save
              </Button>
            </DialogFooter>
          </DialogContent>
        </Dialog>

        {/* Regex Pattern Dialog */}
        <Dialog open={dialogOpen} onOpenChange={setDialogOpen}>
          <DialogContent className="sm:max-w-[600px]">
            <DialogHeader>
              <DialogTitle>
                {editingPatternIndex !== null ? 'Edit' : 'Add'} Regex Pattern
              </DialogTitle>
              <DialogDescription>
                Enter a regex pattern to match streams for this channel. Use CHANNEL_NAME to insert the channel name.
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
                            onCheckedChange={(checked) => {
                              if (checked) {
                                setSelectedM3uAccounts(prev =>
                                  prev.length === 0 ? [account.id] : [...prev, account.id]
                                )
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
              <MatchResultsList
                results={testResults}
                loading={testingPattern}
                onLoadMore={() => handleTestPattern(testResults?.mode || 'regex_only', true, testResults?.channelId)}
                maxHeight="max-h-60"
              />
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
                This pattern will be added to {selectedChannels.size} selected channel{selectedChannels.size !== 1 ? 's' : ''}. Use CHANNEL_NAME to insert each channel's name into the pattern.
              </DialogDescription>
            </DialogHeader>

            <div className="space-y-4 py-4">
              <div className="space-y-2">
                <Label htmlFor="bulk-pattern">Regex Pattern</Label>
                <Input
                  id="bulk-pattern"
                  placeholder="e.g., .*CHANNEL_NAME.*"
                  value={bulkPattern}
                  onChange={(e) => setBulkPattern(e.target.value)}
                  className="font-mono"
                />
                <p className="text-xs text-muted-foreground">
                  Use CHANNEL_NAME to create a pattern that works for all selected channels
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
                            onCheckedChange={(checked) => {
                              if (checked) {
                                setBulkSelectedM3uAccounts(prev =>
                                  prev.length === 0 ? [account.id] : [...prev, account.id]
                                )
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
                  <div>Pattern: <code className="bg-background px-1 rounded">.*CHANNEL_NAME.*</code></div>
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

        {/* Delete Confirmation Dialog */}
        <AlertDialog open={deleteDialogOpen} onOpenChange={setDeleteDialogOpen}>
          <AlertDialogContent>
            <AlertDialogHeader>
              <AlertDialogTitle>Delete Regex Patterns</AlertDialogTitle>
              <AlertDialogDescription>
                Are you sure you want to delete all regex patterns from {selectedChannels.size} selected channel{selectedChannels.size !== 1 ? 's' : ''}? This action cannot be undone.
              </AlertDialogDescription>
            </AlertDialogHeader>
            <AlertDialogFooter>
              <AlertDialogCancel>Cancel</AlertDialogCancel>
              <AlertDialogAction onClick={handleBulkDelete} className="bg-destructive text-destructive-foreground hover:bg-destructive/90">
                Delete
              </AlertDialogAction>
            </AlertDialogFooter>
          </AlertDialogContent>
        </AlertDialog>

        {/* Batch Period Edit Dialog */}
        <BatchPeriodEditDialog
          open={bulkPeriodEditOpen}
          onOpenChange={setBulkPeriodEditOpen}
          selectedChannelIds={Array.from(selectedChannels)}
          profiles={profiles}
          onSuccess={loadData}
        />

        {/* Edit Common Regex Dialog */}
        <Dialog open={editCommonDialogOpen} onOpenChange={(open) => {
          setEditCommonDialogOpen(open)
          if (!open) {
            // Reset state when closing
            setSelectedCommonPatterns(new Set())
            setCommonPatternsSearch('')
          }
        }}>
          <DialogContent className="sm:max-w-[90vw] lg:max-w-[1200px] max-h-[90vh] overflow-y-auto">
            <DialogHeader>
              <DialogTitle>Edit Common Regex Patterns</DialogTitle>
              <DialogDescription>
                These are the regex patterns shared across the {selectedChannels.size} selected channel{selectedChannels.size !== 1 ? 's' : ''}. Editing or deleting a pattern will affect it ONLY in the selected channels.
              </DialogDescription>
            </DialogHeader>

            <div className="space-y-4 py-4">
              {/* Search bar */}
              {commonPatterns.length > 0 && (
                <div className="flex items-center gap-2">
                  <div className="relative flex-1">
                    <Search className="absolute left-3 top-1/2 transform -translate-y-1/2 h-4 w-4 text-muted-foreground" />
                    <Input
                      placeholder="Search patterns..."
                      value={commonPatternsSearch}
                      onChange={(e) => setCommonPatternsSearch(e.target.value)}
                      className="pl-9"
                    />
                  </div>
                  {selectedCommonPatterns.size > 0 && (
                    <div className="flex gap-2">
                      <Button
                        variant="outline"
                        size="sm"
                        onClick={() => setMassEditMode(!massEditMode)}
                      >
                        <Edit className="h-4 w-4 mr-2" />
                        Edit Selected ({selectedCommonPatterns.size})
                      </Button>
                      <Button
                        variant="destructive"
                        size="sm"
                        onClick={handleDeleteSelectedCommonPatterns}
                      >
                        <Trash2 className="h-4 w-4 mr-2" />
                        Delete Selected ({selectedCommonPatterns.size})
                      </Button>
                    </div>
                  )}
                </div>
              )}

              {/* Mass Edit Section */}
              {massEditMode && selectedCommonPatterns.size > 0 && (
                <Card className="border-blue-200 dark:border-blue-800 bg-blue-50 dark:bg-blue-950">
                  <CardHeader className="pb-3">
                    <div className="flex items-center justify-between">
                      <CardTitle className="text-lg">Mass Find & Replace</CardTitle>
                      <Button
                        variant="ghost"
                        size="sm"
                        onClick={() => {
                          setMassEditMode(false)
                          setMassEditFindPattern('')
                          setMassEditReplacePattern('')
                          setMassEditUseRegex(false)
                          setMassEditM3uAccounts(null)
                          setMassEditPreview(null)
                        }}
                      >
                        <X className="h-4 w-4" />
                      </Button>
                    </div>
                  </CardHeader>
                  <CardContent className="space-y-4">
                    {/* Find/Replace Inputs */}
                    <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                      <div className="space-y-2">
                        <Label htmlFor="mass-edit-find">Find Pattern</Label>
                        <Input
                          id="mass-edit-find"
                          value={massEditFindPattern}
                          onChange={(e) => setMassEditFindPattern(e.target.value)}
                          placeholder="Enter text or regex to find"
                          className="font-mono"
                        />
                      </div>
                      <div className="space-y-2">
                        <Label htmlFor="mass-edit-replace">Replace With</Label>
                        <Input
                          id="mass-edit-replace"
                          value={massEditReplacePattern}
                          onChange={(e) => setMassEditReplacePattern(e.target.value)}
                          placeholder="Enter replacement text"
                          className="font-mono"
                        />
                      </div>
                    </div>

                    {/* Options */}
                    <div className="space-y-3">
                      <div className="flex items-center gap-2">
                        <Checkbox
                          id="mass-edit-use-regex"
                          checked={massEditUseRegex}
                          onCheckedChange={setMassEditUseRegex}
                        />
                        <label htmlFor="mass-edit-use-regex" className="text-sm cursor-pointer">
                          Use Regular Expression
                        </label>
                      </div>
                      {massEditUseRegex && (
                        <Alert className="bg-blue-50 dark:bg-blue-950/50 border-blue-200 dark:border-blue-800">
                          <Info className="h-4 w-4" />
                          <AlertDescription className="text-xs">
                            <strong>Regex replacement supports backreferences:</strong>
                            <ul className="list-disc list-inside mt-1 space-y-0.5">
                              <li><code className="bg-background px-1 rounded">{`\\g<0>`}</code> - Full match (equivalent to $0)</li>
                              <li><code className="bg-background px-1 rounded">{`\\1, \\2, ...`}</code> - Capture groups</li>
                              <li><code className="bg-background px-1 rounded">{`\\g<name>`}</code> - Named groups</li>
                            </ul>
                            <div className="mt-2">
                              <strong>Example:</strong> Find: <code className="bg-background px-1 rounded">{`(\\w+)_HD`}</code>, Replace: <code className="bg-background px-1 rounded">{`\\1_4K`}</code> → Changes ESPN_HD to ESPN_4K
                            </div>
                          </AlertDescription>
                        </Alert>
                      )}
                    </div>

                    {/* M3U Account Selection */}
                    <div className="space-y-2">
                      <Label>Update Playlists (Optional)</Label>
                      <div className="space-y-2 border rounded-lg p-3">
                        <div className="flex items-center gap-2">
                          <Checkbox
                            id="mass-edit-keep-playlists"
                            checked={massEditM3uAccounts === null}
                            onCheckedChange={(checked) => {
                              setMassEditM3uAccounts(checked ? null : [])
                            }}
                          />
                          <label htmlFor="mass-edit-keep-playlists" className="text-sm cursor-pointer">
                            Keep Existing Playlists
                          </label>
                        </div>

                        {massEditM3uAccounts !== null && (
                          <>
                            <Separator />
                            <div className="flex items-center gap-2">
                              <Checkbox
                                id="mass-edit-all-playlists"
                                checked={massEditM3uAccounts.length === 0}
                                onCheckedChange={(checked) => {
                                  if (checked) {
                                    setMassEditM3uAccounts([])
                                  } else {
                                    // Select first available playlist when unchecking "All"
                                    const availablePlaylists = m3uAccounts.filter(acc => acc.id !== 'custom')
                                    setMassEditM3uAccounts(availablePlaylists.length > 0 ? [availablePlaylists[0].id] : [])
                                  }
                                }}
                              />
                              <label htmlFor="mass-edit-all-playlists" className="text-sm cursor-pointer">
                                All Playlists
                              </label>
                            </div>

                            {m3uAccounts.filter(acc => acc.id !== 'custom').map(account => (
                              <div key={account.id} className="flex items-center gap-2">
                                <Checkbox
                                  id={`mass-edit-playlist-${account.id}`}
                                  checked={massEditM3uAccounts.length > 0 && massEditM3uAccounts.includes(account.id)}
                                  onCheckedChange={(checked) => {
                                    if (checked) {
                                      setMassEditM3uAccounts(prev => [...prev, account.id])
                                    } else {
                                      setMassEditM3uAccounts(prev => prev.filter(id => id !== account.id))
                                    }
                                  }}
                                />
                                <label htmlFor={`mass-edit-playlist-${account.id}`} className="text-sm cursor-pointer">
                                  {account.name}
                                </label>
                              </div>
                            ))}
                          </>
                        )}
                      </div>
                    </div>

                    {/* Preview Button */}
                    <div className="flex gap-2">
                      <Button
                        variant="outline"
                        onClick={handleMassEditPreview}
                        disabled={!massEditFindPattern.trim() || loadingMassEditPreview}
                      >
                        {loadingMassEditPreview ? (
                          <>
                            <Loader2 className="h-4 w-4 mr-2 animate-spin" />
                            Loading Preview...
                          </>
                        ) : (
                          <>
                            <Eye className="h-4 w-4 mr-2" />
                            Preview Changes
                          </>
                        )}
                      </Button>
                      <Button
                        onClick={handleApplyMassEdit}
                        disabled={!massEditFindPattern.trim() || !massEditPreview || massEditPreview.total_patterns_affected === 0}
                      >
                        <Save className="h-4 w-4 mr-2" />
                        Apply Changes
                      </Button>
                    </div>

                    {/* Preview Results */}
                    {massEditPreview && (
                      <div className="space-y-3 border rounded-lg p-4 bg-background">
                        <div className="flex items-center justify-between">
                          <h4 className="font-semibold">Preview Results</h4>
                          <div className="text-sm text-muted-foreground">
                            {massEditPreview.total_patterns_affected} pattern{massEditPreview.total_patterns_affected !== 1 ? 's' : ''} in {massEditPreview.total_channels_affected} channel{massEditPreview.total_channels_affected !== 1 ? 's' : ''}
                          </div>
                        </div>

                        <div className="max-h-[300px] overflow-y-auto space-y-2">
                          {massEditPreview.affected_channels.map(channelInfo => (
                            <div key={channelInfo.channel_id} className="border rounded p-3 space-y-2">
                              <div className="font-medium text-sm">
                                {channelInfo.channel_name}
                                <span className="text-muted-foreground ml-2">({channelInfo.total_affected} pattern{channelInfo.total_affected !== 1 ? 's' : ''})</span>
                              </div>
                              <div className="space-y-1">
                                {channelInfo.affected_patterns.map((patternChange, idx) => (
                                  <div key={idx} className="text-xs font-mono bg-muted p-2 rounded space-y-1">
                                    <div className="flex items-center gap-2">
                                      <span className="text-red-600 dark:text-red-400 line-through">{patternChange.old_pattern}</span>
                                    </div>
                                    <div className="flex items-center gap-2">
                                      <ArrowRight className="h-3 w-3" />
                                      <span className="text-green-600 dark:text-green-400">{patternChange.new_pattern}</span>
                                    </div>
                                  </div>
                                ))}
                              </div>
                            </div>
                          ))}
                        </div>
                      </div>
                    )}
                  </CardContent>
                </Card>
              )}

              <div className="max-h-[500px] overflow-y-auto">
                {loadingCommonPatterns ? (
                  <div className="flex items-center justify-center py-8">
                    <Loader2 className="h-6 w-6 animate-spin text-muted-foreground" />
                  </div>
                ) : commonPatterns.length === 0 ? (
                  <div className="text-center py-8 text-muted-foreground">
                    No common patterns found across selected channels
                  </div>
                ) : (
                  <>
                    {/* Select All Checkbox */}
                    {(() => {
                      const filterPattern = (p) =>
                        p.pattern.toLowerCase().includes(commonPatternsSearch.toLowerCase())

                      const filteredData = commonPatterns
                        .map((p, idx) => ({ pattern: p, index: idx }))
                        .filter(({ pattern }) => filterPattern(pattern))

                      const filteredPatterns = filteredData.map(({ pattern }) => pattern)
                      const filteredIndices = filteredData.map(({ index }) => index)

                      const allFilteredSelected = filteredIndices.length > 0 &&
                        filteredIndices.every(idx => selectedCommonPatterns.has(idx))

                      return filteredPatterns.length > 0 && (
                        <>
                          <div className="flex items-center space-x-2 pb-3 border-b mb-3">
                            <Checkbox
                              id="select-all-common-patterns"
                              checked={allFilteredSelected}
                              onCheckedChange={(checked) => {
                                if (checked) {
                                  // Select all filtered patterns
                                  setSelectedCommonPatterns(new Set(filteredIndices))
                                } else {
                                  // Deselect all
                                  setSelectedCommonPatterns(new Set())
                                }
                              }}
                            />
                            <label htmlFor="select-all-common-patterns" className="text-sm font-medium cursor-pointer">
                              Select All {commonPatternsSearch ? `(${filteredPatterns.length} filtered)` : `(${commonPatterns.length})`}
                            </label>
                          </div>

                          <div className="space-y-3">
                            {filteredData.map(({ pattern: patternInfo, index: actualIndex }) => {
                              return (
                                <div key={actualIndex} className="border rounded-lg p-4 space-y-3">
                                  {editingCommonPattern && editingCommonPattern.pattern === patternInfo.pattern ? (
                                    // Edit mode
                                    <div className="space-y-3">
                                      <div className="space-y-2">
                                        <Label>New Pattern</Label>
                                        <Input
                                          value={newCommonPattern}
                                          onChange={(e) => setNewCommonPattern(e.target.value)}
                                          className="font-mono"
                                          placeholder="Enter new pattern"
                                        />
                                      </div>

                                      {/* M3U Account filter */}
                                      <div className="space-y-2">
                                        <Label>Playlists (M3U Accounts)</Label>
                                        <div className="space-y-2 border rounded-lg p-3">
                                          <div className="flex items-center gap-2">
                                            <Checkbox
                                              id="all-playlists-edit"
                                              checked={newCommonPatternM3uAccounts === null}
                                              onCheckedChange={(checked) => {
                                                if (checked) {
                                                  setNewCommonPatternM3uAccounts(null)
                                                } else {
                                                  // When unchecking "All", select first M3U account if available
                                                  const availableAccounts = m3uAccounts.filter(acc => acc.id !== 'custom')
                                                  if (availableAccounts.length > 0) {
                                                    setNewCommonPatternM3uAccounts([availableAccounts[0].id])
                                                  }
                                                }
                                              }}
                                            />
                                            <label
                                              htmlFor="all-playlists-edit"
                                              className="text-sm font-medium leading-none peer-disabled:cursor-not-allowed peer-disabled:opacity-70 cursor-pointer"
                                            >
                                              All Playlists
                                            </label>
                                          </div>

                                          {m3uAccounts.filter(acc => acc.id !== 'custom').map(account => (
                                            <div key={account.id} className="flex items-center gap-2">
                                              <Checkbox
                                                id={`playlist-edit-${account.id}`}
                                                checked={newCommonPatternM3uAccounts !== null && newCommonPatternM3uAccounts.includes(account.id)}
                                                onCheckedChange={(checked) => {
                                                  if (checked) {
                                                    setNewCommonPatternM3uAccounts(prev =>
                                                      prev === null ? [account.id] : [...prev, account.id]
                                                    )
                                                  } else {
                                                    setNewCommonPatternM3uAccounts(prev => {
                                                      if (prev === null) return []
                                                      const updated = prev.filter(id => id !== account.id)
                                                      // Return null when all unchecked to mean "all playlists" (backend convention)
                                                      return updated.length === 0 ? null : updated
                                                    })
                                                  }
                                                }}
                                              />
                                              <label
                                                htmlFor={`playlist-edit-${account.id}`}
                                                className={`text-sm leading-none cursor-pointer ${newCommonPatternM3uAccounts === null ? 'text-muted-foreground' : ''}`}
                                              >
                                                {account.name}
                                              </label>
                                            </div>
                                          ))}
                                        </div>
                                      </div>

                                      <div className="flex gap-2 justify-end">
                                        <Button
                                          size="sm"
                                          variant="outline"
                                          onClick={() => {
                                            setEditingCommonPattern(null)
                                            setNewCommonPattern('')
                                            setNewCommonPatternM3uAccounts(null)
                                          }}
                                        >
                                          Cancel
                                        </Button>
                                        <Button
                                          size="sm"
                                          onClick={handleEditCommonPattern}
                                          disabled={!newCommonPattern.trim()}
                                        >
                                          Save
                                        </Button>
                                      </div>
                                    </div>
                                  ) : (
                                    // View mode
                                    <div className="flex items-start gap-3">
                                      <Checkbox
                                        checked={selectedCommonPatterns.has(actualIndex)}
                                        onCheckedChange={(checked) => {
                                          setSelectedCommonPatterns(prev => {
                                            const newSet = new Set(prev)
                                            if (checked) {
                                              newSet.add(actualIndex)
                                            } else {
                                              newSet.delete(actualIndex)
                                            }
                                            return newSet
                                          })
                                        }}
                                      />
                                      <div className="flex-1 min-w-0">
                                        <code className="text-sm break-all block">{patternInfo.pattern}</code>
                                        <div className="flex items-center gap-3 mt-2 text-xs text-muted-foreground">
                                          <span>Used in {patternInfo.count} of {selectedChannels.size} channels ({patternInfo.percentage}%)</span>
                                        </div>
                                      </div>
                                      <div className="flex gap-1">
                                        <Button
                                          size="sm"
                                          variant="outline"
                                          onClick={() => {
                                            setEditingCommonPattern(patternInfo)
                                            setNewCommonPattern(patternInfo.pattern)
                                          }}
                                        >
                                          <Edit2 className="h-4 w-4" />
                                        </Button>
                                        <Button
                                          size="sm"
                                          variant="destructive"
                                          onClick={() => handleDeleteSingleCommonPattern(patternInfo)}
                                        >
                                          <Trash2 className="h-4 w-4" />
                                        </Button>
                                      </div>
                                    </div>
                                  )}
                                </div>
                              )
                            })}
                          </div>
                        </>
                      )
                    })()}

                    {commonPatterns.filter(p =>
                      p.pattern.toLowerCase().includes(commonPatternsSearch.toLowerCase())
                    ).length === 0 && commonPatternsSearch && (
                        <div className="text-center py-8 text-muted-foreground">
                          No patterns match your search
                        </div>
                      )}
                  </>
                )}
              </div>
            </div>

            <DialogFooter>
              <Button variant="outline" onClick={() => {
                setEditCommonDialogOpen(false)
                setEditingCommonPattern(null)
                setNewCommonPattern('')
                setSelectedCommonPatterns(new Set())
                setCommonPatternsSearch('')
              }}>
                Close
              </Button>
            </DialogFooter>
          </DialogContent>
        </Dialog>

        {/* Batch Assign Automation Periods Dialog */}
        <BatchAssignPeriodsDialog
          open={batchPeriodsDialogOpen}
          onOpenChange={setBatchPeriodsDialogOpen}
          selectedChannelIds={Array.from(selectedChannels)}
          channelsData={channels}
          onSuccess={() => {
            loadData()
            setBatchPeriodsDialogOpen(false)
          }}
        />

        <MatchPreviewDialog
          open={previewResultsOpen}
          onOpenChange={setPreviewResultsOpen}
          title={previewTitle}
          results={testResults}
          loading={testingPattern}
          onLoadMore={() => handleTestPattern(testResults?.mode, true, testResults?.channelId)}
        />
      </div>
    </TooltipProvider >
  )
}
