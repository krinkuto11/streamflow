import { useState, useEffect, useMemo, useRef } from 'react'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card.jsx'
import { Button } from '@/components/ui/button.jsx'
import { Badge } from '@/components/ui/badge.jsx'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table.jsx'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle, DialogTrigger } from '@/components/ui/dialog.jsx'
import { Input } from '@/components/ui/input.jsx'
import { Label } from '@/components/ui/label.jsx'
import { Switch } from '@/components/ui/switch.jsx'
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover.jsx'
import { Command, CommandEmpty, CommandGroup, CommandInput, CommandItem, CommandList } from '@/components/ui/command.jsx'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select.jsx'
import { Pagination, PaginationContent, PaginationItem, PaginationLink, PaginationNext, PaginationPrevious, PaginationEllipsis } from '@/components/ui/pagination.jsx'
import { useToast } from '@/hooks/use-toast.js'
import { schedulingAPI, channelsAPI, automationAPI } from '@/services/api.js'
import { Plus, Trash2, Clock, Calendar, RefreshCw, Loader2, Settings, ChevronsUpDown, Check, Edit, Download, Upload, FileJson } from 'lucide-react'
import { cn } from '@/lib/utils.js'

// Schedule type descriptions
const SCHEDULE_TYPE_INFO = {
  check: {
    title: 'Stream Check',
    description: 'Quick stream validation',
    details: 'Performs a quick validation of streams'
  },
  monitoring: {
    title: 'Monitoring Session',
    description: 'Continuous quality monitoring',
    details: 'Creates a monitoring session for continuous quality tracking',
    detailsPlural: 'Creates monitoring sessions for continuous quality tracking'
  }
}

export default function Scheduling() {
  const [events, setEvents] = useState([])
  const [channels, setChannels] = useState([])
  const [channelGroups, setChannelGroups] = useState([])  // Add channel groups state
  const [programs, setPrograms] = useState([])
  const [config, setConfig] = useState(null)
  const [loading, setLoading] = useState(true)
  const [loadingPrograms, setLoadingPrograms] = useState(false)
  const [dialogOpen, setDialogOpen] = useState(false)
  const [channelComboboxOpen, setChannelComboboxOpen] = useState(false)
  const [selectedChannel, setSelectedChannel] = useState(null)
  const [selectedProgram, setSelectedProgram] = useState(null)
  const [minutesBefore, setMinutesBefore] = useState(5)
  const [scheduleType, setScheduleType] = useState('check')  // 'check' or 'monitoring'

  // Pagination state for scheduled events
  const [currentPage, setCurrentPage] = useState(1)
  const [eventsPerPage, setEventsPerPage] = useState(10)
  const [channelFilter, setChannelFilter] = useState('all')
  const [channelFilterOpen, setChannelFilterOpen] = useState(false)

  // Auto-create rules state
  const [autoCreateRules, setAutoCreateRules] = useState([])
  const [ruleDialogOpen, setRuleDialogOpen] = useState(false)
  const [ruleChannelComboboxOpen, setRuleChannelComboboxOpen] = useState(false)
  const [ruleSelectedChannels, setRuleSelectedChannels] = useState([])  // Changed to array
  const [ruleSelectedChannelGroups, setRuleSelectedChannelGroups] = useState([])  // Add channel groups state
  const [ruleChannelGroupComboboxOpen, setRuleChannelGroupComboboxOpen] = useState(false)
  const [ruleName, setRuleName] = useState('')
  const [ruleRegexPattern, setRuleRegexPattern] = useState('')
  const [ruleMinutesBefore, setRuleMinutesBefore] = useState(5)
  const [ruleScheduleType, setRuleScheduleType] = useState('check')  // 'check' or 'monitoring'
  const [ruleEnableLoopDetection, setRuleEnableLoopDetection] = useState(false)
  const [ruleEnableLoopingDetection, setRuleEnableLoopingDetection] = useState(true)
  const [ruleEnableLogoDetection, setRuleEnableLogoDetection] = useState(true)
  
  // Single Event AceStream Config
  const [enableLoopDetection, setEnableLoopDetection] = useState(false)
  const [sessionType, setSessionType] = useState('standard')
  const [intervalS, setIntervalS] = useState(1.0)
  const [runSeconds, setRunSeconds] = useState(0)
  const [perSampleTimeoutS, setPerSampleTimeoutS] = useState(1.0)
  const [engineContainerId, setEngineContainerId] = useState('')

  // Rule AceStream Config
  const [ruleSessionType, setRuleSessionType] = useState('standard')
  const [ruleIntervalS, setRuleIntervalS] = useState(1.0)
  const [ruleRunSeconds, setRuleRunSeconds] = useState(0)
  const [rulePerSampleTimeoutS, setRulePerSampleTimeoutS] = useState(1.0)
  const [ruleEngineContainerId, setRuleEngineContainerId] = useState('')

  const [testingRegex, setTestingRegex] = useState(false)
  const [regexMatches, setRegexMatches] = useState([])
  const [deleteRuleDialogOpen, setDeleteRuleDialogOpen] = useState(false)
  const [ruleToDelete, setRuleToDelete] = useState(null)
  const [editingRuleId, setEditingRuleId] = useState(null)

  // File input refs for import
  const fileInputRef = useRef(null)
  const wizardFileInputRef = useRef(null)

  const { toast } = useToast()

  // Calculate paginated events with useMemo for performance
  const paginationData = useMemo(() => {
    // Apply channel filter first
    const filteredEvents = channelFilter === 'all'
      ? events
      : events.filter(event => event.channel_id === parseInt(channelFilter))

    const totalPages = Math.ceil(filteredEvents.length / eventsPerPage)
    const startIndex = (currentPage - 1) * eventsPerPage
    const endIndex = startIndex + eventsPerPage
    const paginatedEvents = filteredEvents.slice(startIndex, endIndex)

    return {
      totalPages,
      startIndex,
      endIndex,
      paginatedEvents,
      filteredEvents,
      showingStart: startIndex + 1,
      showingEnd: Math.min(endIndex, filteredEvents.length)
    }
  }, [events, currentPage, eventsPerPage, channelFilter])

  useEffect(() => {
    loadData()
  }, [])

  const loadData = async () => {
    try {
      setLoading(true)
      const [eventsResponse, channelsResponse, channelGroupsResponse, rulesResponse] = await Promise.all([
        schedulingAPI.getEvents(),
        channelsAPI.getChannels(),
        channelsAPI.getGroups(),
        schedulingAPI.getAutoCreateRules()
      ])

      setEvents(eventsResponse.data || [])
      setChannels(channelsResponse.data || [])
      setChannelGroups(channelGroupsResponse.data || [])
      setAutoCreateRules(rulesResponse.data || [])
    } catch (err) {
      console.error('Failed to load scheduling data:', err)
      toast({
        title: "Error",
        description: "Failed to load scheduling data",
        variant: "destructive"
      })
    } finally {
      setLoading(false)
    }
  }

  const loadPrograms = async (channelId) => {
    if (!channelId) return

    try {
      setLoadingPrograms(true)
      const response = await schedulingAPI.getChannelPrograms(channelId)
      setPrograms(response.data || [])
    } catch (err) {
      console.error('Failed to load programs:', err)
      toast({
        title: "Error",
        description: "Failed to load programs for channel",
        variant: "destructive"
      })
      setPrograms([])
    } finally {
      setLoadingPrograms(false)
    }
  }

  const handleChannelSelect = (channelId) => {
    const channel = channels.find(c => c.id === parseInt(channelId))
    setSelectedChannel(channel)
    setSelectedProgram(null)
    setPrograms([])
    setChannelComboboxOpen(false)
    if (channel) {
      loadPrograms(channel.id)
    }
  }

  const handleCreateEvent = async () => {
    if (!selectedChannel || !selectedProgram) {
      toast({
        title: "Validation Error",
        description: "Please select a channel and program",
        variant: "destructive"
      })
      return
    }

    const minutesBeforeValue = parseInt(minutesBefore)
    if (isNaN(minutesBeforeValue) || minutesBeforeValue < 0) {
      toast({
        title: "Validation Error",
        description: "Please enter a valid number of minutes (0 or greater)",
        variant: "destructive"
      })
      return
    }

    try {
      const eventData = {
        channel_id: selectedChannel.id,
        program_start_time: selectedProgram.start_time,
        program_end_time: selectedProgram.end_time,
        program_title: selectedProgram.title,
        minutes_before: minutesBeforeValue,
        schedule_type: scheduleType,
        session_type: sessionType,
        interval_s: intervalS,
        run_seconds: runSeconds,
        per_sample_timeout_s: perSampleTimeoutS,
        engine_container_id: engineContainerId,
        enable_loop_detection: enableLoopDetection,
      }

      await schedulingAPI.createEvent(eventData)

      toast({
        title: "Success",
        description: "Scheduled event created successfully"
      })

      setDialogOpen(false)
      setSelectedChannel(null)
      setSelectedProgram(null)
      setPrograms([])
      setChannelComboboxOpen(false)
      setMinutesBefore(5)
      setScheduleType('check')
      setSessionType('standard')
      setIntervalS(1.0)
      setRunSeconds(0)
      setPerSampleTimeoutS(1.0)
      setEngineContainerId('')
      setEnableLoopDetection(false)
      await loadData()
    } catch (err) {
      console.error('Failed to create event:', err)
      toast({
        title: "Error",
        description: err.response?.data?.error || "Failed to create scheduled event",
        variant: "destructive"
      })
    }
  }

  const [deleteDialogOpen, setDeleteDialogOpen] = useState(false)
  const [eventToDelete, setEventToDelete] = useState(null)

  const validateMinutesBefore = (value) => {
    const minutesValue = parseInt(value)
    return !isNaN(minutesValue) && minutesValue >= 0
  }

  const handleRuleChannelSelect = (channelId) => {
    const channel = channels.find(c => c.id === parseInt(channelId))
    if (!channel) return

    // Toggle channel selection
    const isSelected = ruleSelectedChannels.some(c => c.id === channel.id)
    if (isSelected) {
      setRuleSelectedChannels(ruleSelectedChannels.filter(c => c.id !== channel.id))
    } else {
      setRuleSelectedChannels([...ruleSelectedChannels, channel])
    }

    // Clear regex matches when channels change
    setRegexMatches([])
  }

  const handleRuleChannelGroupSelect = (groupId) => {
    const group = channelGroups.find(g => g.id === parseInt(groupId))
    if (!group) return

    // Toggle group selection
    const isSelected = ruleSelectedChannelGroups.some(g => g.id === group.id)
    if (isSelected) {
      setRuleSelectedChannelGroups(ruleSelectedChannelGroups.filter(g => g.id !== group.id))
    } else {
      setRuleSelectedChannelGroups([...ruleSelectedChannelGroups, group])
    }

    // Clear regex matches when groups change
    setRegexMatches([])
  }

  const handleTestRegex = async () => {
    if ((ruleSelectedChannels.length === 0 && ruleSelectedChannelGroups.length === 0) || !ruleRegexPattern) {
      toast({
        title: "Validation Error",
        description: "Please select at least one channel or channel group and enter a regex pattern",
        variant: "destructive"
      })
      return
    }

    try {
      setTestingRegex(true)
      // Test regex against first selected channel or first channel from first selected group
      let testChannelId = null
      if (ruleSelectedChannels.length > 0) {
        testChannelId = ruleSelectedChannels[0].id
      } else if (ruleSelectedChannelGroups.length > 0) {
        // Find first channel in the first selected group
        const firstGroup = ruleSelectedChannelGroups[0]
        const groupChannels = channels.filter(c => c.channel_group_id === firstGroup.id)
        if (groupChannels.length > 0) {
          testChannelId = groupChannels[0].id
        }
      }

      if (!testChannelId) {
        toast({
          title: "No Channels",
          description: "No channels found in selected groups",
          variant: "destructive"
        })
        return
      }

      const response = await schedulingAPI.testAutoCreateRule({
        channel_id: testChannelId,
        regex_pattern: ruleRegexPattern
      })

      setRegexMatches(response.data.programs || [])

      // SCH-002: backend signals the channel has no TVG-ID configured
      if (response.data.no_tvg_id) {
        toast({
          title: "No TVG-ID Configured",
          description: "This channel has no TVG-ID set. EPG matching requires a TVG-ID — open the channel in Dispatcharr and click \"Use EPG TVG-ID\" to populate it.",
          variant: "destructive"
        })
      } else if (response.data.matches === 0) {
        toast({
          title: "No Matches",
          description: "The regex pattern didn't match any programs in the EPG (tested on first available channel)",
          variant: "default"
        })
      }
    } catch (err) {
      console.error('Failed to test regex:', err)
      toast({
        title: "Error",
        description: err.response?.data?.error || "Failed to test regex pattern",
        variant: "destructive"
      })
      setRegexMatches([])
    } finally {
      setTestingRegex(false)
    }
  }

  const handleCreateRule = async () => {
    if (!ruleName || (ruleSelectedChannels.length === 0 && ruleSelectedChannelGroups.length === 0) || !ruleRegexPattern) {
      toast({
        title: "Validation Error",
        description: "Please fill in all required fields and select at least one channel or channel group",
        variant: "destructive"
      })
      return
    }

    // Parse once and validate
    const minutesBeforeValue = parseInt(ruleMinutesBefore)
    if (!validateMinutesBefore(ruleMinutesBefore)) {
      toast({
        title: "Validation Error",
        description: "Please enter a valid number of minutes (0 or greater)",
        variant: "destructive"
      })
      return
    }

    try {
      const ruleData = {
        name: ruleName,
        channel_ids: ruleSelectedChannels.map(c => c.id),
        channel_group_ids: ruleSelectedChannelGroups.map(g => g.id),
        regex_pattern: ruleRegexPattern,
        minutes_before: minutesBeforeValue,
        schedule_type: ruleScheduleType,
        session_type: ruleSessionType,
        interval_s: ruleIntervalS,
        run_seconds: ruleRunSeconds,
        per_sample_timeout_s: rulePerSampleTimeoutS,
        engine_container_id: ruleEngineContainerId,
        enable_loop_detection: ruleEnableLoopDetection,
        enable_looping_detection: ruleEnableLoopingDetection,
        enable_logo_detection: ruleEnableLogoDetection
      }

      if (editingRuleId) {
        // Update existing rule
        await schedulingAPI.updateAutoCreateRule(editingRuleId, ruleData)
        toast({
          title: "Success",
          description: "Auto-create rule updated successfully. Events will be recreated automatically."
        })
      } else {
        // Create new rule
        await schedulingAPI.createAutoCreateRule(ruleData)
        toast({
          title: "Success",
          description: "Auto-create rule created successfully. Events will be created automatically when EPG is refreshed."
        })
      }

      setRuleDialogOpen(false)
      setEditingRuleId(null)
      setRuleName('')
      setRuleSelectedChannels([])
      setRuleSelectedChannelGroups([])
      setRuleRegexPattern('')
      setRuleMinutesBefore(5)
      setRuleScheduleType('check')
      setRuleSessionType('standard')
      setRuleIntervalS(1.0)
      setRuleRunSeconds(0)
      setRulePerSampleTimeoutS(1.0)
      setRuleEngineContainerId('')
      setRuleEnableLoopDetection(false)
      setRuleEnableLoopingDetection(true)
      setRuleEnableLogoDetection(true)
      setRegexMatches([])
      setRuleChannelComboboxOpen(false)
      setRuleChannelGroupComboboxOpen(false)
      await loadData()
    } catch (err) {
      console.error('Failed to save rule:', err)
      toast({
        title: "Error",
        description: err.response?.data?.error || "Failed to create auto-create rule",
        variant: "destructive"
      })
    }
  }

  const handleDeleteRule = async (ruleId) => {
    try {
      await schedulingAPI.deleteAutoCreateRule(ruleId)
      toast({
        title: "Success",
        description: "Auto-create rule deleted"
      })
      await loadData()
      setDeleteRuleDialogOpen(false)
      setRuleToDelete(null)
    } catch (err) {
      console.error('Failed to delete rule:', err)
      toast({
        title: "Error",
        description: "Failed to delete auto-create rule",
        variant: "destructive"
      })
    }
  }

  const handleEditRule = (rule) => {
    // Populate form with rule data
    setEditingRuleId(rule.id)
    setRuleName(rule.name)
    setRuleRegexPattern(rule.regex_pattern)
    setRuleMinutesBefore(rule.minutes_before)
    setRuleScheduleType(rule.schedule_type || 'check')  // Default to 'check' for backward compatibility
    setRuleSessionType(rule.session_type || 'standard')
    setRuleIntervalS(rule.interval_s || 1.0)
    setRuleRunSeconds(rule.run_seconds || 0)
    setRulePerSampleTimeoutS(rule.per_sample_timeout_s || 1.0)
    setRuleEngineContainerId(rule.engine_container_id || '')
    setRuleEnableLoopDetection(rule.enable_loop_detection || false)
    setRuleEnableLoopingDetection(rule.enable_looping_detection !== false)
    setRuleEnableLogoDetection(rule.enable_logo_detection !== false)

    // Find and set the channels - support both old (channel_id) and new (channel_ids) format
    const selectedChannels = []

    if (rule.channel_ids && Array.isArray(rule.channel_ids)) {
      // New multi-channel format
      rule.channel_ids.forEach(channelId => {
        const channel = channels.find(c => c.id === channelId)
        if (channel) {
          selectedChannels.push(channel)
        }
      })
    } else if (rule.channel_id) {
      // Old single-channel format (backward compatibility)
      const channel = channels.find(c => c.id === rule.channel_id)
      if (channel) {
        selectedChannels.push(channel)
      }
    }

    setRuleSelectedChannels(selectedChannels)

    // Find and set channel groups
    const selectedGroups = []
    if (rule.channel_group_ids && Array.isArray(rule.channel_group_ids)) {
      rule.channel_group_ids.forEach(groupId => {
        const group = channelGroups.find(g => g.id === groupId)
        if (group) {
          selectedGroups.push(group)
        }
      })
    }

    setRuleSelectedChannelGroups(selectedGroups)

    // Clear previous test results
    setRegexMatches([])

    // Open dialog
    setRuleDialogOpen(true)
  }

  const handleDeleteEvent = async (eventId) => {
    try {
      await schedulingAPI.deleteEvent(eventId)
      toast({
        title: "Success",
        description: "Scheduled event deleted"
      })
      await loadData()
      setDeleteDialogOpen(false)
      setEventToDelete(null)
    } catch (err) {
      console.error('Failed to delete event:', err)
      toast({
        title: "Error",
        description: "Failed to delete scheduled event",
        variant: "destructive"
      })
    }
  }

  const handleExportRules = async () => {
    try {
      const response = await schedulingAPI.exportAutoCreateRules()
      const rulesData = response.data

      // Create a blob and download
      const blob = new Blob([JSON.stringify(rulesData, null, 2)], { type: 'application/json' })
      const url = URL.createObjectURL(blob)
      const link = document.createElement('a')
      link.href = url
      link.download = `auto-create-rules-${new Date().toISOString().split('T')[0]}.json`
      document.body.appendChild(link)
      link.click()
      document.body.removeChild(link)
      URL.revokeObjectURL(url)

      toast({
        title: "Success",
        description: `Exported ${rulesData.length} rule(s)`
      })
    } catch (err) {
      console.error('Failed to export rules:', err)
      toast({
        title: "Error",
        description: "Failed to export auto-create rules",
        variant: "destructive"
      })
    }
  }

  const handleImportRules = async (event, fromWizard = false) => {
    const file = event.target.files?.[0]
    if (!file) return

    try {
      const text = await file.text()
      const rulesData = JSON.parse(text)

      if (!Array.isArray(rulesData)) {
        toast({
          title: "Error",
          description: "Invalid file format. Expected a JSON array of rules.",
          variant: "destructive"
        })
        return
      }

      const response = await schedulingAPI.importAutoCreateRules(rulesData)
      const result = response.data

      if (result.imported > 0) {
        toast({
          title: "Success",
          description: `Imported ${result.imported} rule(s). ${result.failed > 0 ? `${result.failed} failed.` : ''}`
        })
        await loadData()
      } else {
        toast({
          title: "Import Failed",
          description: result.errors?.[0] || "No rules were imported",
          variant: "destructive"
        })
      }
    } catch (err) {
      console.error('Failed to import rules:', err)
      toast({
        title: "Error",
        description: err.response?.data?.error || "Failed to import rules. Please check the file format.",
        variant: "destructive"
      })
    } finally {
      // Reset file input
      if (event.target) {
        event.target.value = ''
      }
    }
  }

  const handleImportIntoWizard = async (event) => {
    const file = event.target.files?.[0]
    if (!file) return

    try {
      const text = await file.text()
      const rulesData = JSON.parse(text)

      // If it's a single rule object, wrap it in an array
      const rules = Array.isArray(rulesData) ? rulesData : [rulesData]

      if (rules.length === 0) {
        toast({
          title: "Error",
          description: "No rules found in the file",
          variant: "destructive"
        })
        return
      }

      // Import the first rule into the wizard form
      const rule = rules[0]

      // Populate form fields
      setRuleName(rule.name || '')
      setRuleRegexPattern(rule.regex_pattern || '')
      setRuleMinutesBefore(rule.minutes_before || 5)

      // Handle channel selection
      const channelIds = rule.channel_ids || (rule.channel_id ? [rule.channel_id] : [])
      const selectedChannels = channelIds
        .map(id => channels.find(c => c.id === id))
        .filter(Boolean)

      setRuleSelectedChannels(selectedChannels)

      if (rules.length > 1) {
        toast({
          title: "Info",
          description: `Loaded first rule from file. File contains ${rules.length} rules total. Import all rules using the main Import button.`
        })
      } else {
        toast({
          title: "Success",
          description: "Rule loaded into form"
        })
      }
    } catch (err) {
      console.error('Failed to load rule:', err)
      toast({
        title: "Error",
        description: "Failed to load rule. Please check the file format.",
        variant: "destructive"
      })
    } finally {
      // Reset file input
      if (event.target) {
        event.target.value = ''
      }
    }
  }

  const formatDateTime = (dateStr) => {
    if (!dateStr) return 'N/A'
    try {
      const date = new Date(dateStr)
      return date.toLocaleString()
    } catch {
      return dateStr
    }
  }

  const formatTime = (dateStr) => {
    if (!dateStr) return 'N/A'
    try {
      const date = new Date(dateStr)
      return date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
    } catch {
      return dateStr
    }
  }

  if (loading) {
    return (
      <div className="flex items-center justify-center h-64">
        <Loader2 className="h-8 w-8 animate-spin text-primary" />
      </div>
    )
  }

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-3xl font-bold">Scheduling</h1>
          <p className="text-muted-foreground mt-1">
            Schedule channel checks before EPG events
          </p>
        </div>
        <div className="flex gap-2">
          <Button variant="outline" size="sm" onClick={() => loadData()}>
            <RefreshCw className={cn("h-4 w-4 mr-2", loading && "animate-spin")} />
            Refresh
          </Button>
          <Dialog open={dialogOpen} onOpenChange={setDialogOpen}>
            <DialogTrigger asChild>
              <Button>
                <Plus className="h-4 w-4 mr-2" />
                Add Event Check
              </Button>
            </DialogTrigger>
            <DialogContent className="max-w-3xl max-h-[90vh] overflow-y-auto">
              <DialogHeader>
                <DialogTitle>Schedule Channel Check</DialogTitle>
                <DialogDescription>
                  Select a channel, program, and how many minutes before the program starts you want the check to happen
                </DialogDescription>
              </DialogHeader>

              <div className="space-y-4 py-4">
                {/* Channel Selection */}
                <div className="space-y-2">
                  <Label htmlFor="channel-select">Channel</Label>
                  <Popover open={channelComboboxOpen} onOpenChange={setChannelComboboxOpen}>
                    <PopoverTrigger asChild>
                      <Button
                        variant="outline"
                        role="combobox"
                        aria-expanded={channelComboboxOpen}
                        className="w-full justify-between"
                      >
                        {selectedChannel
                          ? `${selectedChannel.channel_number ? `${selectedChannel.channel_number} - ` : ''}${selectedChannel.name}`
                          : "Search and select a channel..."}
                        <ChevronsUpDown className="ml-2 h-4 w-4 shrink-0 opacity-50" />
                      </Button>
                    </PopoverTrigger>
                    <PopoverContent className="w-[500px] p-0" align="start">
                      <Command>
                        <CommandInput placeholder="Search channels..." className="h-9" />
                        <CommandList>
                          <CommandEmpty>No channel found.</CommandEmpty>
                          <CommandGroup>
                            {channels.map((channel) => {
                              const channelNumber = channel.channel_number ? `${channel.channel_number} ` : '';
                              const searchValue = `${channelNumber}${channel.name}`.toLowerCase().trim();
                              return (
                                <CommandItem
                                  key={channel.id}
                                  value={searchValue}
                                  onSelect={() => handleChannelSelect(channel.id)}
                                >
                                  {channel.channel_number ? `${channel.channel_number} - ` : ''}{channel.name}
                                  <Check
                                    className={cn(
                                      "ml-auto h-4 w-4",
                                      selectedChannel?.id === channel.id ? "opacity-100" : "opacity-0"
                                    )}
                                  />
                                </CommandItem>
                              );
                            })}
                          </CommandGroup>
                        </CommandList>
                      </Command>
                    </PopoverContent>
                  </Popover>
                </div>

                {/* Programs List */}
                {selectedChannel && (
                  <div className="space-y-2">
                    <Label>Programs</Label>
                    {loadingPrograms ? (
                      <div className="flex items-center justify-center p-8">
                        <Loader2 className="h-6 w-6 animate-spin text-primary" />
                      </div>
                    ) : programs.length === 0 ? (
                      <div className="text-center text-muted-foreground p-8 border rounded-lg">
                        No programs available for this channel
                      </div>
                    ) : (
                      <div className="border rounded-lg max-h-64 overflow-y-auto">
                        {programs.map((program) => (
                          <div
                            key={program.id || `${program.start_time}-${program.title}`}
                            className={`p-3 border-b last:border-b-0 cursor-pointer hover:bg-muted/50 transition-colors ${selectedProgram?.id === program.id
                              ? 'bg-primary/10 border-2 border-green-500/50 dark:border-green-400/50'
                              : ''
                              }`}
                            onClick={() => setSelectedProgram(program)}
                          >
                            <div className="flex items-start justify-between">
                              <div className="flex-1">
                                <div className="font-medium">{program.title}</div>
                                <div className="text-sm text-muted-foreground mt-1">
                                  {formatTime(program.start_time)} - {formatTime(program.end_time)}
                                </div>
                                {program.description && (
                                  <div className="text-sm text-muted-foreground mt-1 line-clamp-2">
                                    {program.description}
                                  </div>
                                )}
                              </div>
                            </div>
                          </div>
                        ))}
                      </div>
                    )}
                  </div>
                )}

                {/* Minutes Before Input */}
                {selectedProgram && (
                  <div className="space-y-2">
                    <Label htmlFor="minutes-before">Minutes Before Program Start</Label>
                    <Input
                      id="minutes-before"
                      type="number"
                      min="0"
                      max="120"
                      value={minutesBefore}
                      onChange={(e) => setMinutesBefore(e.target.value)}
                      placeholder="5"
                    />
                    <p className="text-sm text-muted-foreground">
                      The channel check will run {minutesBefore || 0} minutes before the program starts
                    </p>
                  </div>
                )}

                {/* Schedule Type Selection */}
                {selectedProgram && (
                  <div className="space-y-2">
                    <Label htmlFor="schedule-type">Schedule Type</Label>
                    <Select
                      value={scheduleType}
                      onValueChange={(value) => setScheduleType(value)}
                    >
                      <SelectTrigger id="schedule-type">
                        <SelectValue placeholder="Select schedule type" />
                      </SelectTrigger>
                      <SelectContent>
                        <SelectItem value="check">
                          <div className="flex flex-col">
                            <span className="font-medium">{SCHEDULE_TYPE_INFO.check.title}</span>
                            <span className="text-xs text-muted-foreground">{SCHEDULE_TYPE_INFO.check.description}</span>
                          </div>
                        </SelectItem>
                        <SelectItem value="monitoring">
                          <div className="flex flex-col">
                            <span className="font-medium">{SCHEDULE_TYPE_INFO.monitoring.title}</span>
                            <span className="text-xs text-muted-foreground">{SCHEDULE_TYPE_INFO.monitoring.description}</span>
                          </div>
                        </SelectItem>
                      </SelectContent>
                    </Select>
                    <p className="text-sm text-muted-foreground">
                      {SCHEDULE_TYPE_INFO[scheduleType].details}
                    </p>
                  </div>
                )}

                {/* Session Type (Monitoring only) */}
                {selectedProgram && scheduleType === 'monitoring' && (
                  <div className="space-y-4 border rounded-lg p-4">
                    <h4 className="text-sm font-medium">Monitoring Settings</h4>
                    <div className="space-y-2">
                      <Label htmlFor="session-type">Session Type</Label>
                      <Select
                        value={sessionType}
                        onValueChange={(value) => setSessionType(value)}
                      >
                        <SelectTrigger id="session-type">
                          <SelectValue placeholder="Select session type" />
                        </SelectTrigger>
                        <SelectContent>
                          <SelectItem value="standard">Standard (FFmpeg)</SelectItem>
                          <SelectItem value="acestream">AceStream</SelectItem>
                        </SelectContent>
                      </Select>
                      <p className="text-sm text-muted-foreground">
                        {sessionType === 'standard'
                          ? 'Creates a standard monitoring session using ffmpeg'
                          : 'Creates an AceStream quality monitoring session'}
                      </p>
                    </div>

                    {sessionType === 'acestream' && (
                      <div className="grid grid-cols-2 gap-4">
                        <div className="space-y-2">
                          <Label htmlFor="interval-s">Interval (s)</Label>
                          <Input
                            id="interval-s"
                            type="number"
                            step="0.1"
                            min="0.1"
                            value={intervalS}
                            onChange={(e) => setIntervalS(parseFloat(e.target.value) || 1.0)}
                          />
                        </div>
                        <div className="space-y-2">
                          <Label htmlFor="run-seconds">Run Seconds</Label>
                          <Input
                            id="run-seconds"
                            type="number"
                            min="0"
                            value={runSeconds}
                            onChange={(e) => setRunSeconds(parseInt(e.target.value) || 0)}
                          />
                        </div>
                        <div className="space-y-2">
                          <Label htmlFor="per-sample-timeout">Sample Timeout (s)</Label>
                          <Input
                            id="per-sample-timeout"
                            type="number"
                            step="0.1"
                            min="0.1"
                            value={perSampleTimeoutS}
                            onChange={(e) => setPerSampleTimeoutS(parseFloat(e.target.value) || 1.0)}
                          />
                        </div>
                        <div className="space-y-2">
                          <Label htmlFor="engine-container">Engine Container ID</Label>
                          <Input
                            id="engine-container"
                            placeholder="Optional"
                            value={engineContainerId}
                            onChange={(e) => setEngineContainerId(e.target.value)}
                          />
                        </div>
                        <div className="flex items-center justify-between col-span-2 border-t pt-2">
                          <div className="space-y-0.5">
                            <Label htmlFor="ace-loop-detection" className="text-sm">
                              Enable Zero-Decode Loop Detection
                            </Label>
                            <p className="text-[11px] text-muted-foreground mr-4">
                              Monitors packet metadata to detect if the stream is a looping VOD file. (Negligible CPU impact)
                            </p>
                          </div>
                          <Switch
                            id="ace-loop-detection"
                            checked={enableLoopDetection}
                            onCheckedChange={setEnableLoopDetection}
                          />
                        </div>
                      </div>
                    )}
                  </div>
                )}
              </div>

              <DialogFooter>
                <Button variant="outline" onClick={() => {
                  setDialogOpen(false)
                  setSelectedChannel(null)
                  setSelectedProgram(null)
                  setPrograms([])
                  setChannelComboboxOpen(false)
                }}>
                  Cancel
                </Button>
                <Button
                  onClick={handleCreateEvent}
                  disabled={!selectedChannel || !selectedProgram}
                >
                  Schedule Check
                </Button>
              </DialogFooter>
            </DialogContent>
          </Dialog>
        </div>
      </div>

      {/* Scheduled Events Table */}
      <Card>
        <CardHeader>
          <div className="flex items-center justify-between">
            <div>
              <CardTitle>Scheduled Events</CardTitle>
              <CardDescription>
                Channel checks scheduled before EPG program events
              </CardDescription>
            </div>
            {events.length > 0 && (
              <div className="flex items-center gap-4">
                {/* Channel Filter */}
                <div className="flex items-center gap-2">
                  <Label htmlFor="channel-filter" className="text-sm whitespace-nowrap">
                    Filter by channel:
                  </Label>
                  <Popover open={channelFilterOpen} onOpenChange={setChannelFilterOpen}>
                    <PopoverTrigger asChild>
                      <Button
                        id="channel-filter"
                        variant="outline"
                        role="combobox"
                        aria-expanded={channelFilterOpen}
                        className="w-[200px] justify-between"
                      >
                        {channelFilter === 'all'
                          ? "All Channels"
                          : channels.find(c => c.id === parseInt(channelFilter))?.name || "Select..."}
                        <ChevronsUpDown className="ml-2 h-4 w-4 shrink-0 opacity-50" />
                      </Button>
                    </PopoverTrigger>
                    <PopoverContent className="w-[200px] p-0">
                      <Command>
                        <CommandInput placeholder="Search channel..." />
                        <CommandList>
                          <CommandEmpty>No channel found.</CommandEmpty>
                          <CommandGroup>
                            <CommandItem
                              value="all"
                              onSelect={() => {
                                setChannelFilter('all')
                                setCurrentPage(1)
                                setChannelFilterOpen(false)
                              }}
                            >
                              <Check
                                className={cn(
                                  "mr-2 h-4 w-4",
                                  channelFilter === 'all' ? "opacity-100" : "opacity-0"
                                )}
                              />
                              All Channels
                            </CommandItem>
                            {/* Get unique channels from events */}
                            {[...new Set(events.map(e => e.channel_id))].map(channelId => {
                              const channel = channels.find(c => c.id === channelId)
                              return channel ? (
                                <CommandItem
                                  key={channelId}
                                  value={`${channel.name}-${channelId}`}
                                  onSelect={() => {
                                    setChannelFilter(channelId.toString())
                                    setCurrentPage(1)
                                    setChannelFilterOpen(false)
                                  }}
                                >
                                  <Check
                                    className={cn(
                                      "mr-2 h-4 w-4",
                                      channelFilter === channelId.toString() ? "opacity-100" : "opacity-0"
                                    )}
                                  />
                                  {channel.name}
                                </CommandItem>
                              ) : null
                            })}
                          </CommandGroup>
                        </CommandList>
                      </Command>
                    </PopoverContent>
                  </Popover>
                </div>

                {/* Events per page selector */}
                <div className="flex items-center gap-2">
                  <Label htmlFor="events-per-page" className="text-sm whitespace-nowrap">
                    Events per page:
                  </Label>
                  <Select
                    value={eventsPerPage.toString()}
                    onValueChange={(value) => {
                      setEventsPerPage(parseInt(value))
                      setCurrentPage(1) // Reset to first page when changing page size
                    }}
                  >
                    <SelectTrigger id="events-per-page" className="w-[100px]">
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value="10">10</SelectItem>
                      <SelectItem value="25">25</SelectItem>
                      <SelectItem value="50">50</SelectItem>
                      <SelectItem value="100">100</SelectItem>
                    </SelectContent>
                  </Select>
                </div>
              </div>
            )}
          </div>
        </CardHeader>
        <CardContent>
          {events.length === 0 ? (
            <div className="text-center text-muted-foreground py-12">
              <Calendar className="h-12 w-12 mx-auto mb-4 opacity-50" />
              <p className="text-lg font-medium mb-2">No scheduled events yet</p>
              <p>Click "Add Event Check" to schedule a channel check before a program</p>
            </div>
          ) : (
            <>
              <div className="rounded-md border">
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead>Channel</TableHead>
                      <TableHead>Program</TableHead>
                      <TableHead>Program Time</TableHead>
                      <TableHead>Check Time</TableHead>
                      <TableHead>Minutes Before</TableHead>
                      <TableHead className="text-right">Actions</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {paginationData.paginatedEvents.map((event) => (
                      <TableRow key={event.id}>
                        <TableCell>
                          <div className="flex items-center gap-2">
                            {event.channel_logo_url && (
                              <img
                                src={event.channel_logo_url}
                                alt={event.channel_name}
                                className="h-8 w-8 object-contain rounded"
                                onError={(e) => { e.target.style.display = 'none' }}
                              />
                            )}
                            <span className="font-medium">{event.channel_name}</span>
                          </div>
                        </TableCell>
                        <TableCell>{event.program_title}</TableCell>
                        <TableCell>
                          <div className="flex items-center gap-1 text-sm">
                            <Clock className="h-3 w-3" />
                            {formatTime(event.program_start_time)}
                          </div>
                        </TableCell>
                        <TableCell>
                          <Badge variant="outline">
                            {formatDateTime(event.check_time)}
                          </Badge>
                        </TableCell>
                        <TableCell>
                          {event.minutes_before} min
                        </TableCell>
                        <TableCell className="text-right">
                          <Button
                            variant="ghost"
                            size="sm"
                            onClick={() => {
                              setEventToDelete(event.id)
                              setDeleteDialogOpen(true)
                            }}
                          >
                            <Trash2 className="h-4 w-4 text-destructive" />
                          </Button>
                        </TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              </div>

              {/* Pagination Controls */}
              {paginationData.totalPages > 1 && (
                <div className="flex items-center justify-between mt-4">
                  <div className="text-sm text-muted-foreground">
                    Showing {paginationData.showingStart} to {paginationData.showingEnd} of {paginationData.filteredEvents.length} events
                    {channelFilter !== 'all' && ` (filtered from ${events.length} total)`}
                  </div>
                  <Pagination>
                    <PaginationContent>
                      <PaginationItem>
                        <PaginationPrevious
                          onClick={() => setCurrentPage(prev => Math.max(1, prev - 1))}
                          className={currentPage === 1 ? 'pointer-events-none opacity-50' : 'cursor-pointer'}
                        />
                      </PaginationItem>

                      {(() => {
                        const { totalPages } = paginationData
                        const pages = []

                        // For 2 or fewer pages, show all pages
                        if (totalPages <= 2) {
                          for (let i = 1; i <= totalPages; i++) {
                            pages.push(
                              <PaginationItem key={i}>
                                <PaginationLink
                                  onClick={() => setCurrentPage(i)}
                                  isActive={currentPage === i}
                                  className="cursor-pointer"
                                >
                                  {i}
                                </PaginationLink>
                              </PaginationItem>
                            )
                          }
                        } else {
                          // Always show first page
                          pages.push(
                            <PaginationItem key={1}>
                              <PaginationLink
                                onClick={() => setCurrentPage(1)}
                                isActive={currentPage === 1}
                                className="cursor-pointer"
                              >
                                1
                              </PaginationLink>
                            </PaginationItem>
                          )

                          // Show ellipsis if needed
                          if (currentPage > 3) {
                            pages.push(
                              <PaginationItem key="ellipsis-1">
                                <PaginationEllipsis />
                              </PaginationItem>
                            )
                          }

                          // Show pages around current page
                          const startPage = Math.max(2, currentPage - 1)
                          const endPage = Math.min(totalPages - 1, currentPage + 1)

                          for (let i = startPage; i <= endPage; i++) {
                            pages.push(
                              <PaginationItem key={i}>
                                <PaginationLink
                                  onClick={() => setCurrentPage(i)}
                                  isActive={currentPage === i}
                                  className="cursor-pointer"
                                >
                                  {i}
                                </PaginationLink>
                              </PaginationItem>
                            )
                          }

                          // Show ellipsis if needed
                          if (currentPage < totalPages - 2) {
                            pages.push(
                              <PaginationItem key="ellipsis-2">
                                <PaginationEllipsis />
                              </PaginationItem>
                            )
                          }

                          // Always show last page
                          pages.push(
                            <PaginationItem key={totalPages}>
                              <PaginationLink
                                onClick={() => setCurrentPage(totalPages)}
                                isActive={currentPage === totalPages}
                                className="cursor-pointer"
                              >
                                {totalPages}
                              </PaginationLink>
                            </PaginationItem>
                          )
                        }

                        return pages
                      })()}

                      <PaginationItem>
                        <PaginationNext
                          onClick={() => setCurrentPage(prev => Math.min(paginationData.totalPages, prev + 1))}
                          className={currentPage === paginationData.totalPages ? 'pointer-events-none opacity-50' : 'cursor-pointer'}
                        />
                      </PaginationItem>
                    </PaginationContent>
                  </Pagination>
                </div>
              )}
            </>
          )}
        </CardContent>
      </Card>

      {/* Auto-Create Rules Card */}
      <Card>
        <CardHeader>
          <div className="flex items-center justify-between">
            <div>
              <CardTitle>Auto-Create Rules</CardTitle>
              <CardDescription>
                Automatically create scheduled events based on regex patterns matching EPG program names
              </CardDescription>
            </div>
            <div className="flex gap-2">
              {/* Export Button */}
              <Button
                variant="outline"
                size="sm"
                onClick={handleExportRules}
                disabled={autoCreateRules.length === 0}
              >
                <Download className="h-4 w-4 mr-2" />
                Export
              </Button>

              {/* Import Button */}
              <Button
                variant="outline"
                size="sm"
                onClick={() => fileInputRef.current?.click()}
              >
                <Upload className="h-4 w-4 mr-2" />
                Import
              </Button>
              <input
                ref={fileInputRef}
                type="file"
                accept=".json"
                onChange={handleImportRules}
                style={{ display: 'none' }}
              />

              {/* Add Rule Dialog */}
              <Dialog open={ruleDialogOpen} onOpenChange={(open) => {
                setRuleDialogOpen(open)
                if (!open) {
                  // Clear form when dialog closes
                  setEditingRuleId(null)
                  setRuleName('')
                  setRuleSelectedChannels([])
                  setRuleRegexPattern('')
                  setRuleMinutesBefore(5)
                  setRuleEnableLoopDetection(false)
                  setRuleEnableLoopingDetection(true)
                  setRuleEnableLogoDetection(true)
                  setRegexMatches([])
                }
              }}>
                <DialogTrigger asChild>
                  <Button size="sm" onClick={() => {
                    // Clear editing state when opening to create new rule
                    setEditingRuleId(null)
                    setRuleName('')
                    setRuleSelectedChannels([])
                    setRuleRegexPattern('')
                    setRuleMinutesBefore(5)
                    setRuleScheduleType('check')
                    setRuleSessionType('standard')
                    setRuleIntervalS(1.0)
                    setRuleRunSeconds(0)
                    setRulePerSampleTimeoutS(1.0)
                    setRuleEngineContainerId('')
                    setRuleEnableLoopDetection(false)
                    setRuleEnableLoopingDetection(true)
                    setRuleEnableLogoDetection(true)
                    setRegexMatches([])
                  }}>
                    <Plus className="h-4 w-4 mr-2" />
                    Add Rule
                  </Button>
                </DialogTrigger>
                <DialogContent className="max-w-4xl max-h-[90vh] overflow-y-auto">
                  <DialogHeader>
                    <DialogTitle>
                      <div className="flex items-center justify-between">
                        <span>{editingRuleId ? 'Edit Auto-Create Rule' : 'Create Auto-Create Rule'}</span>
                        {!editingRuleId && (
                          <Button
                            variant="outline"
                            size="sm"
                            onClick={() => wizardFileInputRef.current?.click()}
                          >
                            <FileJson className="h-4 w-4 mr-2" />
                            Import JSON
                          </Button>
                        )}
                      </div>
                    </DialogTitle>
                    <DialogDescription>
                      Define a regex pattern to automatically create scheduled checks for matching programs
                    </DialogDescription>
                  </DialogHeader>
                  <input
                    ref={wizardFileInputRef}
                    type="file"
                    accept=".json"
                    onChange={handleImportIntoWizard}
                    style={{ display: 'none' }}
                  />

                  <div className="space-y-4 py-4">
                    {/* Rule Name */}
                    <div className="space-y-2">
                      <Label htmlFor="rule-name">Rule Name</Label>
                      <Input
                        id="rule-name"
                        placeholder="e.g., Breaking News Alert"
                        value={ruleName}
                        onChange={(e) => setRuleName(e.target.value)}
                      />
                    </div>

                    {/* Channel Selection - Multi-select */}
                    <div className="space-y-2">
                      <Label htmlFor="rule-channel-select">Channels (Individual)</Label>
                      <Popover open={ruleChannelComboboxOpen} onOpenChange={setRuleChannelComboboxOpen}>
                        <PopoverTrigger asChild>
                          <Button
                            variant="outline"
                            role="combobox"
                            aria-expanded={ruleChannelComboboxOpen}
                            className="w-full justify-between"
                          >
                            {ruleSelectedChannels.length > 0
                              ? `${ruleSelectedChannels.length} channel${ruleSelectedChannels.length > 1 ? 's' : ''} selected`
                              : "Search and select channels..."}
                            <ChevronsUpDown className="ml-2 h-4 w-4 shrink-0 opacity-50" />
                          </Button>
                        </PopoverTrigger>
                        <PopoverContent className="w-[600px] p-0" align="start">
                          <Command>
                            <CommandInput placeholder="Search channels..." className="h-9" />
                            <CommandList>
                              <CommandEmpty>No channel found.</CommandEmpty>
                              <CommandGroup>
                                {channels.map((channel) => {
                                  const channelNumber = channel.channel_number ? `${channel.channel_number} ` : '';
                                  const searchValue = `${channelNumber}${channel.name}`.toLowerCase().trim();
                                  const isSelected = ruleSelectedChannels.some(c => c.id === channel.id);
                                  return (
                                    <CommandItem
                                      key={channel.id}
                                      value={searchValue}
                                      onSelect={() => handleRuleChannelSelect(channel.id)}
                                    >
                                      {channel.channel_number ? `${channel.channel_number} - ` : ''}{channel.name}
                                      <Check
                                        className={cn(
                                          "ml-auto h-4 w-4",
                                          isSelected ? "opacity-100" : "opacity-0"
                                        )}
                                      />
                                    </CommandItem>
                                  );
                                })}
                              </CommandGroup>
                            </CommandList>
                          </Command>
                        </PopoverContent>
                      </Popover>
                      {ruleSelectedChannels.length > 0 && (
                        <div className="flex flex-wrap gap-2 mt-2">
                          {ruleSelectedChannels.map((channel) => (
                            <Badge key={channel.id} variant="secondary" className="flex items-center gap-1">
                              {channel.channel_number ? `${channel.channel_number} - ` : ''}{channel.name}
                              <button
                                type="button"
                                onClick={() => handleRuleChannelSelect(channel.id)}
                                onKeyDown={(e) => {
                                  if (e.key === 'Enter' || e.key === ' ') {
                                    e.preventDefault();
                                    handleRuleChannelSelect(channel.id);
                                  }
                                }}
                                className="ml-1 hover:text-destructive"
                                aria-label={`Remove ${channel.name}`}
                                tabIndex={0}
                              >
                                ×
                              </button>
                            </Badge>
                          ))}
                        </div>
                      )}
                    </div>

                    {/* Channel Group Selection - Multi-select */}
                    <div className="space-y-2">
                      <Label htmlFor="rule-channel-group-select">Channel Groups</Label>
                      <Popover open={ruleChannelGroupComboboxOpen} onOpenChange={setRuleChannelGroupComboboxOpen}>
                        <PopoverTrigger asChild>
                          <Button
                            variant="outline"
                            role="combobox"
                            aria-expanded={ruleChannelGroupComboboxOpen}
                            className="w-full justify-between"
                          >
                            {ruleSelectedChannelGroups.length > 0
                              ? `${ruleSelectedChannelGroups.length} group${ruleSelectedChannelGroups.length > 1 ? 's' : ''} selected`
                              : "Search and select channel groups..."}
                            <ChevronsUpDown className="ml-2 h-4 w-4 shrink-0 opacity-50" />
                          </Button>
                        </PopoverTrigger>
                        <PopoverContent className="w-[600px] p-0" align="start">
                          <Command>
                            <CommandInput placeholder="Search channel groups..." className="h-9" />
                            <CommandList>
                              <CommandEmpty>No channel group found.</CommandEmpty>
                              <CommandGroup>
                                {channelGroups.map((group) => {
                                  const isSelected = ruleSelectedChannelGroups.some(g => g.id === group.id);
                                  return (
                                    <CommandItem
                                      key={group.id}
                                      value={group.name.toLowerCase()}
                                      onSelect={() => handleRuleChannelGroupSelect(group.id)}
                                    >
                                      {group.name} ({group.channel_count || 0} channels)
                                      <Check
                                        className={cn(
                                          "ml-auto h-4 w-4",
                                          isSelected ? "opacity-100" : "opacity-0"
                                        )}
                                      />
                                    </CommandItem>
                                  );
                                })}
                              </CommandGroup>
                            </CommandList>
                          </Command>
                        </PopoverContent>
                      </Popover>
                      {ruleSelectedChannelGroups.length > 0 && (
                        <div className="flex flex-wrap gap-2 mt-2">
                          {ruleSelectedChannelGroups.map((group) => (
                            <Badge key={group.id} variant="outline" className="flex items-center gap-1">
                              {group.name} ({group.channel_count || 0})
                              <button
                                type="button"
                                onClick={() => handleRuleChannelGroupSelect(group.id)}
                                onKeyDown={(e) => {
                                  if (e.key === 'Enter' || e.key === ' ') {
                                    e.preventDefault();
                                    handleRuleChannelGroupSelect(group.id);
                                  }
                                }}
                                className="ml-1 hover:text-destructive"
                                aria-label={`Remove ${group.name}`}
                                tabIndex={0}
                              >
                                ×
                              </button>
                            </Badge>
                          ))}
                        </div>
                      )}
                      <p className="text-sm text-muted-foreground">
                        Selected groups will automatically include current and future channels in those groups
                      </p>
                    </div>

                    {/* Regex Pattern */}
                    {(ruleSelectedChannels.length > 0 || ruleSelectedChannelGroups.length > 0) && (
                      <>
                        <div className="space-y-2">
                          <div className="flex items-center justify-between">
                            <Label htmlFor="rule-regex">Regex Pattern</Label>
                            <Button
                              variant="outline"
                              size="sm"
                              onClick={handleTestRegex}
                              disabled={!ruleRegexPattern || testingRegex}
                            >
                              {testingRegex ? <Loader2 className="h-4 w-4 animate-spin mr-2" /> : <RefreshCw className="h-4 w-4 mr-2" />}
                              Test Pattern
                            </Button>
                          </div>
                          <Input
                            id="rule-regex"
                            placeholder="e.g., ^Breaking News|^Special Report"
                            value={ruleRegexPattern}
                            onChange={(e) => {
                              setRuleRegexPattern(e.target.value)
                              setRegexMatches([])  // Clear matches when pattern changes
                            }}
                          />
                          <p className="text-sm text-muted-foreground">
                            Use regex syntax to match program titles. Click "Test Pattern" to see live results{ruleSelectedChannels.length > 1 ? ' (tested on first selected channel)' : ''}.
                          </p>
                        </div>

                        {/* Live Regex Results */}
                        {regexMatches.length > 0 && (
                          <div className="space-y-2">
                            <Label>Matching Programs ({regexMatches.length}){ruleSelectedChannels.length > 1 && ` on ${ruleSelectedChannels[0].name}`}</Label>
                            <div className="border rounded-lg max-h-48 overflow-y-auto">
                              {regexMatches.map((program, idx) => (
                                <div
                                  key={idx}
                                  className="p-2 border-b last:border-b-0 text-sm"
                                >
                                  <div className="font-medium">{program.title}</div>
                                  <div className="text-muted-foreground text-xs mt-1">
                                    {formatTime(program.start_time)} - {formatTime(program.end_time)}
                                  </div>
                                </div>
                              ))}
                            </div>
                          </div>
                        )}

                        {/* Minutes Before Input */}
                        <div className="space-y-2">
                          <Label htmlFor="rule-minutes-before">Minutes Before Program Start</Label>
                          <Input
                            id="rule-minutes-before"
                            type="number"
                            min="0"
                            max="120"
                            value={ruleMinutesBefore}
                            onChange={(e) => setRuleMinutesBefore(e.target.value)}
                          />
                          <p className="text-sm text-muted-foreground">
                            Channel checks will run {ruleMinutesBefore || 0} minutes before matching programs start
                          </p>
                        </div>

                        {/* Schedule Type Selection */}
                        <div className="space-y-2">
                          <Label htmlFor="rule-schedule-type">Schedule Type</Label>
                          <Select
                            value={ruleScheduleType}
                            onValueChange={(value) => setRuleScheduleType(value)}
                          >
                            <SelectTrigger id="rule-schedule-type">
                              <SelectValue placeholder="Select schedule type" />
                            </SelectTrigger>
                            <SelectContent>
                              <SelectItem value="check">
                                <div className="flex flex-col">
                                  <span className="font-medium">{SCHEDULE_TYPE_INFO.check.title}</span>
                                  <span className="text-xs text-muted-foreground">{SCHEDULE_TYPE_INFO.check.description}</span>
                                </div>
                              </SelectItem>
                              <SelectItem value="monitoring">
                                <div className="flex flex-col">
                                  <span className="font-medium">{SCHEDULE_TYPE_INFO.monitoring.title}</span>
                                  <span className="text-xs text-muted-foreground">{SCHEDULE_TYPE_INFO.monitoring.description}</span>
                                </div>
                              </SelectItem>
                            </SelectContent>
                          </Select>
                          <p className="text-sm text-muted-foreground">
                            {ruleScheduleType === 'check'
                              ? SCHEDULE_TYPE_INFO.check.details
                              : SCHEDULE_TYPE_INFO.monitoring.detailsPlural}
                          </p>
                        </div>

                        {/* Monitoring Toggles */}
                        {ruleScheduleType === 'monitoring' && ruleSessionType !== 'acestream' && (
                          <div className="border rounded-lg p-3 space-y-3">
                            <h4 className="text-sm font-medium">Monitoring Features</h4>

                            <div className="flex items-center justify-between">
                              <div className="space-y-0.5">
                                <Label htmlFor="rule-looping-detection" className="text-sm">
                                  Looping Detection
                                </Label>
                                <p className="text-xs text-muted-foreground">
                                  Identify and penalize looping streams
                                </p>
                              </div>
                              <Switch
                                id="rule-looping-detection"
                                checked={ruleEnableLoopingDetection}
                                onCheckedChange={(checked) => setRuleEnableLoopingDetection(checked)}
                              />
                            </div>

                            <div className="flex items-center justify-between border-t pt-3">
                              <div className="space-y-0.5">
                                <Label htmlFor="rule-logo-detection" className="text-sm">
                                  Logo Verification
                                </Label>
                                <p className="text-xs text-muted-foreground">
                                  Verify stream logo against reference
                                </p>
                              </div>
                              <Switch
                                id="rule-logo-detection"
                                checked={ruleEnableLogoDetection}
                                onCheckedChange={(checked) => setRuleEnableLogoDetection(checked)}
                              />
                            </div>
                          </div>
                        )}
                        
                        {/* Session Type Settings Group */}
                        {ruleScheduleType === 'monitoring' && (
                          <div className="border rounded-lg p-3 space-y-3">
                            <h4 className="text-sm font-medium">Session Type</h4>
                            <Select
                              value={ruleSessionType}
                              onValueChange={(value) => setRuleSessionType(value)}
                            >
                              <SelectTrigger id="rule-session-type">
                                <SelectValue placeholder="Select session type" />
                              </SelectTrigger>
                              <SelectContent>
                                <SelectItem value="standard">Standard (FFmpeg)</SelectItem>
                                <SelectItem value="acestream">AceStream</SelectItem>
                              </SelectContent>
                            </Select>

                            {ruleSessionType === 'acestream' && (
                              <div className="grid grid-cols-2 gap-4 pt-2">
                                <div className="space-y-2">
                                  <Label htmlFor="rule-interval-s">Interval (s)</Label>
                                  <Input
                                    id="rule-interval-s"
                                    type="number"
                                    step="0.1"
                                    min="0.1"
                                    value={ruleIntervalS}
                                    onChange={(e) => setRuleIntervalS(parseFloat(e.target.value) || 1.0)}
                                  />
                                </div>
                                <div className="space-y-2">
                                  <Label htmlFor="rule-run-seconds">Run Seconds</Label>
                                  <Input
                                    id="rule-run-seconds"
                                    type="number"
                                    min="0"
                                    value={ruleRunSeconds}
                                    onChange={(e) => setRuleRunSeconds(parseInt(e.target.value) || 0)}
                                  />
                                </div>
                                <div className="space-y-2">
                                  <Label htmlFor="rule-per-sample-timeout">Sample Timeout (s)</Label>
                                  <Input
                                    id="rule-per-sample-timeout"
                                    type="number"
                                    step="0.1"
                                    min="0.1"
                                    value={rulePerSampleTimeoutS}
                                    onChange={(e) => setRulePerSampleTimeoutS(parseFloat(e.target.value) || 1.0)}
                                  />
                                </div>
                                <div className="space-y-2">
                                  <Label htmlFor="rule-engine-container">Engine Container ID</Label>
                                  <Input
                                    id="rule-engine-container"
                                    placeholder="Optional"
                                    value={ruleEngineContainerId}
                                    onChange={(e) => setRuleEngineContainerId(e.target.value)}
                                  />
                                </div>
                                <div className="flex items-center justify-between col-span-2 border-t pt-2">
                                  <div className="space-y-0.5">
                                    <Label htmlFor="rule-ace-loop" className="text-sm">
                                      Enable Zero-Decode Loop Detection
                                    </Label>
                                    <p className="text-[11px] text-muted-foreground mr-4">
                                      Monitors packet metadata to detect if the stream is a looping VOD file. (Negligible CPU impact)
                                    </p>
                                  </div>
                                  <Switch
                                    id="rule-ace-loop"
                                    checked={ruleEnableLoopDetection}
                                    onCheckedChange={setRuleEnableLoopDetection}
                                  />
                                </div>
                              </div>
                            )}
                          </div>
                        )}
                      </>
                    )}
                  </div>

                  <DialogFooter>
                    <Button variant="outline" onClick={() => {
                      setRuleDialogOpen(false)
                      setEditingRuleId(null)
                      setRuleName('')
                      setRuleSelectedChannels([])
                      setRuleSelectedChannelGroups([])
                      setRuleRegexPattern('')
                      setRuleMinutesBefore(5)
                      setRuleScheduleType('check')
                      setRuleSessionType('standard')
                      setRuleIntervalS(1.0)
                      setRuleRunSeconds(0)
                      setRulePerSampleTimeoutS(1.0)
                      setRuleEngineContainerId('')
                      setRuleEnableLoopDetection(false)
                      setRuleEnableLoopingDetection(true)
                      setRuleEnableLogoDetection(true)
                      setRegexMatches([])
                      setRuleChannelComboboxOpen(false)
                      setRuleChannelGroupComboboxOpen(false)
                    }}>
                      Cancel
                    </Button>
                    <Button
                      onClick={handleCreateRule}
                      disabled={!ruleName || (ruleSelectedChannels.length === 0 && ruleSelectedChannelGroups.length === 0) || !ruleRegexPattern}
                    >
                      {editingRuleId ? 'Update Rule' : 'Create Rule'}
                    </Button>
                  </DialogFooter>
                </DialogContent>
              </Dialog>
            </div>
          </div>
        </CardHeader>
        <CardContent>
          {autoCreateRules.length === 0 ? (
            <div className="text-center text-muted-foreground py-8">
              <Settings className="h-10 w-10 mx-auto mb-3 opacity-50" />
              <p className="text-base font-medium mb-1">No auto-create rules yet</p>
              <p className="text-sm">Click "Add Rule" to automatically create events based on program names</p>
            </div>
          ) : (
            <div className="rounded-md border">
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Rule Name</TableHead>
                    <TableHead>Channels</TableHead>
                    <TableHead>Regex Pattern</TableHead>
                    <TableHead>Minutes Before</TableHead>
                    <TableHead className="text-right">Actions</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {autoCreateRules.map((rule) => {
                    // Support both old (single channel) and new (multiple channels + groups) format
                    const channelsInfo = rule.channels_info ||
                      (rule.channel_id ? [{
                        id: rule.channel_id,
                        name: rule.channel_name,
                        logo_url: rule.channel_logo_url
                      }] : []);

                    const channelGroupsInfo = rule.channel_groups_info || [];
                    const hasIndividualChannels = (rule.channel_ids && rule.channel_ids.length > 0);
                    const hasGroups = (channelGroupsInfo.length > 0);

                    return (
                      <TableRow key={rule.id}>
                        <TableCell className="font-medium">{rule.name}</TableCell>
                        <TableCell>
                          <div className="flex flex-col gap-2">
                            {/* Individual channels */}
                            {hasIndividualChannels && (
                              <div>
                                {rule.channel_ids.length === 1 ? (
                                  <div className="flex items-center gap-2">
                                    {channelsInfo[0]?.logo_url && (
                                      <img
                                        src={channelsInfo[0].logo_url}
                                        alt={channelsInfo[0].name}
                                        className="h-6 w-6 object-contain rounded"
                                        onError={(e) => { e.target.style.display = 'none' }}
                                      />
                                    )}
                                    <span>{channelsInfo[0]?.name}</span>
                                  </div>
                                ) : (
                                  <div className="flex flex-col gap-1">
                                    <span className="text-sm font-medium">{rule.channel_ids.length} individual channels</span>
                                    <div className="flex flex-wrap gap-1">
                                      {channelsInfo.slice(0, 3).map((ch, idx) => (
                                        <Badge key={idx} variant="secondary" className="text-xs">
                                          {ch.name}
                                        </Badge>
                                      ))}
                                      {channelsInfo.length > 3 && (
                                        <Badge variant="secondary" className="text-xs">
                                          +{channelsInfo.length - 3} more
                                        </Badge>
                                      )}
                                    </div>
                                  </div>
                                )}
                              </div>
                            )}

                            {/* Channel groups */}
                            {hasGroups && (
                              <div className="flex flex-col gap-1">
                                <span className="text-sm font-medium">{channelGroupsInfo.length} group{channelGroupsInfo.length > 1 ? 's' : ''}</span>
                                <div className="flex flex-wrap gap-1">
                                  {channelGroupsInfo.map((group, idx) => (
                                    <Badge key={idx} variant="outline" className="text-xs">
                                      {group.name} ({group.channel_count || 0})
                                    </Badge>
                                  ))}
                                </div>
                              </div>
                            )}

                            {/* Total channel count - show only for expanded display */}
                            {channelsInfo.length > 1 && (
                              <span className="text-xs text-muted-foreground">
                                Applied to: {channelsInfo.length} channel{channelsInfo.length !== 1 ? 's' : ''}
                              </span>
                            )}
                          </div>
                        </TableCell>
                        <TableCell>
                          <code className="text-xs bg-muted px-2 py-1 rounded">{rule.regex_pattern}</code>
                        </TableCell>
                        <TableCell>{rule.minutes_before} min</TableCell>
                        <TableCell className="text-right">
                          <div className="flex items-center justify-end gap-1">
                            <Button
                              variant="ghost"
                              size="sm"
                              onClick={() => handleEditRule(rule)}
                            >
                              <Edit className="h-4 w-4" />
                            </Button>
                            <Button
                              variant="ghost"
                              size="sm"
                              onClick={() => {
                                setRuleToDelete(rule.id)
                                setDeleteRuleDialogOpen(true)
                              }}
                            >
                              <Trash2 className="h-4 w-4 text-destructive" />
                            </Button>
                          </div>
                        </TableCell>
                      </TableRow>
                    )
                  })}
                </TableBody>
              </Table>
            </div>
          )}
        </CardContent>
      </Card>

      {/* Info Card */}
      <Card>
        <CardHeader>
          <CardTitle>How It Works</CardTitle>
        </CardHeader>
        <CardContent className="space-y-2 text-sm text-muted-foreground">
          <p>
            • EPG data is fetched from Dispatcharr every {config?.epg_refresh_interval_minutes || 60} minutes
          </p>
          <p>
            • Schedule channel checks to happen before important programs
          </p>
          <p>
            • A playlist update will also happen right before the scheduled check
          </p>
          <p>
            • This ensures channels have the freshest streams for optimal viewing experience
          </p>
        </CardContent>
      </Card>

      {/* Delete Confirmation Dialog */}
      <Dialog open={deleteDialogOpen} onOpenChange={setDeleteDialogOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Delete Scheduled Event</DialogTitle>
            <DialogDescription>
              Are you sure you want to delete this scheduled event? This action cannot be undone.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button
              variant="outline"
              onClick={() => {
                setDeleteDialogOpen(false)
                setEventToDelete(null)
              }}
            >
              Cancel
            </Button>
            <Button
              variant="destructive"
              onClick={() => handleDeleteEvent(eventToDelete)}
            >
              Delete
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Delete Rule Confirmation Dialog */}
      <Dialog open={deleteRuleDialogOpen} onOpenChange={setDeleteRuleDialogOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Delete Auto-Create Rule</DialogTitle>
            <DialogDescription>
              Are you sure you want to delete this rule? This will also delete all scheduled events that were automatically created by this rule.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button
              variant="outline"
              onClick={() => {
                setDeleteRuleDialogOpen(false)
                setRuleToDelete(null)
              }}
            >
              Cancel
            </Button>
            <Button
              variant="destructive"
              onClick={() => handleDeleteRule(ruleToDelete)}
            >
              Delete
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div >
  )
}
