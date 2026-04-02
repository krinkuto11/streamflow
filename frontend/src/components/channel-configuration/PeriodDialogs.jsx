import { useEffect, useState } from 'react'
import { Loader2, Calendar, Clock, Trash2 } from 'lucide-react'

import { Button } from '@/components/ui/button.jsx'
import { Label } from '@/components/ui/label.jsx'
import { Badge } from '@/components/ui/badge.jsx'
import { Checkbox } from '@/components/ui/checkbox.jsx'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog.jsx'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select.jsx'
import { useToast } from '@/hooks/use-toast.js'
import { automationAPI } from '@/services/api.js'

export function AssignPeriodsDialog({ open, onOpenChange, channelId, channelName, onSuccess }) {
  const [allPeriods, setAllPeriods] = useState([])
  const [allProfiles, setAllProfiles] = useState([])
  const [periodAssignments, setPeriodAssignments] = useState({})
  const [loading, setLoading] = useState(false)
  const [saving, setSaving] = useState(false)
  const { toast } = useToast()

  useEffect(() => {
    if (open) {
      loadData()
    }
  }, [open, channelId])

  const loadData = async () => {
    try {
      setLoading(true)
      const [periodsResponse, profilesResponse, assignedResponse] = await Promise.all([
        automationAPI.getPeriods(),
        automationAPI.getProfiles(),
        automationAPI.getChannelPeriods(channelId),
      ])
      setAllPeriods(periodsResponse.data)
      setAllProfiles(profilesResponse.data)

      const assignments = {}
      if (Array.isArray(assignedResponse.data)) {
        assignedResponse.data.forEach((item) => {
          assignments[item.id] = item.profile_id
        })
      }
      setPeriodAssignments(assignments)
    } catch (err) {
      console.error('Failed to load data:', err)
      toast({
        title: 'Error',
        description: 'Failed to load periods and profiles',
        variant: 'destructive',
      })
    } finally {
      setLoading(false)
    }
  }

  const handleSave = async () => {
    try {
      setSaving(true)

      const currentResponse = await automationAPI.getChannelPeriods(channelId)
      const currentPeriodsData = currentResponse.data || []
      const currentAssignments = {}
      if (Array.isArray(currentPeriodsData)) {
        currentPeriodsData.forEach((item) => {
          currentAssignments[item.id] = item.profile_id
        })
      }

      const newPeriods = Object.keys(periodAssignments)
      const currentPeriods = Object.keys(currentAssignments)
      const toRemove = currentPeriods.filter((id) => !newPeriods.includes(id))

      for (const periodId of toRemove) {
        await automationAPI.removePeriodFromChannels(periodId, [channelId])
      }

      for (const [periodId, profileId] of Object.entries(periodAssignments)) {
        if (profileId) {
          await automationAPI.assignPeriodToChannels(periodId, [channelId], profileId, false)
        }
      }

      toast({
        title: 'Success',
        description: 'Automation periods updated',
      })

      onOpenChange(false)
      if (onSuccess) onSuccess()
    } catch (err) {
      console.error('Failed to save:', err)
      toast({
        title: 'Error',
        description: 'Failed to save periods',
        variant: 'destructive',
      })
    } finally {
      setSaving(false)
    }
  }

  const togglePeriod = (periodId) => {
    setPeriodAssignments((prev) => {
      const next = { ...prev }
      if (periodId in next) {
        delete next[periodId]
      } else if (allProfiles.length > 0) {
        next[periodId] = allProfiles[0].id
      }
      return next
    })
  }

  const updateProfile = (periodId, profileId) => {
    setPeriodAssignments((prev) => ({
      ...prev,
      [periodId]: profileId,
    }))
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-3xl max-h-[80vh] overflow-hidden flex flex-col">
        <DialogHeader>
          <DialogTitle>Assign Automation Periods with Profiles</DialogTitle>
          <DialogDescription>
            Select periods and choose which profile to use for {channelName}
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
              Create automation periods in Settings {'->'} Automation {'->'} Periods first
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
                  className={`p-3 border rounded-lg transition-colors ${
                    isSelected ? 'border-primary bg-primary/5' : 'border-border'
                  }`}
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
                          <Badge variant="outline" className="text-xs">
                            {period.channel_count || 0} channel{period.channel_count !== 1 ? 's' : ''}
                          </Badge>
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
            disabled={saving || loading || allPeriods.length === 0 || Object.keys(periodAssignments).length === 0}
          >
            {saving && <Loader2 className="h-4 w-4 mr-2 animate-spin" />}
            Save
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

export function BatchAssignPeriodsDialog({ open, onOpenChange, selectedChannelIds, channelsData, onSuccess }) {
  const [allPeriods, setAllPeriods] = useState([])
  const [allProfiles, setAllProfiles] = useState([])
  const [periodAssignments, setPeriodAssignments] = useState({})
  const [loading, setLoading] = useState(false)
  const [saving, setSaving] = useState(false)
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
        automationAPI.getProfiles(),
      ])
      setAllPeriods(periodsResponse.data || [])
      setAllProfiles(profilesResponse.data || [])
      setPeriodAssignments({})
    } catch (err) {
      console.error('Failed to load data:', err)
      toast({
        title: 'Error',
        description: 'Failed to load periods and profiles',
        variant: 'destructive',
      })
    } finally {
      setLoading(false)
    }
  }

  const handleSave = async () => {
    const selectedPeriods = Object.entries(periodAssignments).filter(
      ([, profileId]) => profileId && profileId !== ''
    )

    if (selectedPeriods.length === 0) {
      toast({
        title: 'No Periods Selected',
        description: 'Please select at least one period and assign a profile',
        variant: 'destructive',
      })
      return
    }

    try {
      setSaving(true)
      const assignments = selectedPeriods.map(([periodId, profileId]) => ({
        period_id: periodId,
        profile_id: profileId,
      }))
      await automationAPI.batchAssignPeriods(selectedChannelIds, assignments, false)

      toast({
        title: 'Success',
        description: `Assigned ${selectedPeriods.length} period${selectedPeriods.length > 1 ? 's' : ''} to ${selectedChannelIds.length} channel${selectedChannelIds.length > 1 ? 's' : ''}`,
      })

      if (onSuccess) onSuccess()
    } catch (err) {
      console.error('Failed to save:', err)
      toast({
        title: 'Error',
        description: err.response?.data?.error || 'Failed to assign periods',
        variant: 'destructive',
      })
    } finally {
      setSaving(false)
    }
  }

  const togglePeriod = (periodId) => {
    setPeriodAssignments((prev) => {
      const next = { ...prev }
      if (periodId in next) {
        delete next[periodId]
      } else if (allProfiles.length > 0) {
        next[periodId] = allProfiles[0].id
      }
      return next
    })
  }

  const updateProfile = (periodId, profileId) => {
    setPeriodAssignments((prev) => ({
      ...prev,
      [periodId]: profileId,
    }))
  }

  const selectedChannelNames = channelsData
    .filter((channel) => selectedChannelIds.includes(channel.id))
    .map((channel) => channel.name)
    .slice(0, 3)
  const remainingCount = selectedChannelIds.length - 3

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-3xl max-h-[80vh] overflow-hidden flex flex-col">
        <DialogHeader>
          <DialogTitle>Batch Assign Automation Periods</DialogTitle>
          <DialogDescription>
            Assign automation periods with profiles to {selectedChannelIds.length} selected channel
            {selectedChannelIds.length > 1 ? 's' : ''}
            {selectedChannelNames.length > 0 && (
              <span className="block mt-1 text-xs">
                ({selectedChannelNames.join(', ')}
                {remainingCount > 0 ? ` and ${remainingCount} more` : ''})
              </span>
            )}
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
              Create automation periods in Settings {'->'} Automation {'->'} Periods first
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
                  className={`p-3 border rounded-lg transition-colors ${
                    isSelected ? 'border-primary bg-primary/5' : 'border-border'
                  }`}
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
                          <Badge variant="outline" className="text-xs">
                            {period.channel_count || 0} channel{period.channel_count !== 1 ? 's' : ''}
                          </Badge>
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
          <Button onClick={handleSave} disabled={saving || loading || Object.keys(periodAssignments).length === 0}>
            {saving && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}
            Assign to Channels
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

export function BatchPeriodEditDialog({ open, onOpenChange, selectedChannelIds, profiles, onSuccess }) {
  const [usage, setUsage] = useState(null)
  const [loading, setLoading] = useState(false)
  const [updating, setUpdating] = useState(false)
  const [removing, setRemoving] = useState(false)
  const { toast } = useToast()

  useEffect(() => {
    if (open) {
      loadUsage()
    }
  }, [open, selectedChannelIds])

  const loadUsage = async () => {
    try {
      setLoading(true)
      const response = await automationAPI.getBatchPeriodUsage(selectedChannelIds)
      setUsage(response.data)
    } catch (err) {
      console.error('Failed to load period usage:', err)
      toast({
        title: 'Error',
        description: 'Failed to load automation period usage',
        variant: 'destructive',
      })
    } finally {
      setLoading(false)
    }
  }

  const handleUpdateProfile = async (periodId, profileId) => {
    try {
      setUpdating(periodId)
      await automationAPI.batchAssignPeriods(
        selectedChannelIds,
        [
          {
            period_id: periodId,
            profile_id: profileId,
          },
        ],
        false
      )

      toast({
        title: 'Success',
        description: 'Profile updated for selected channels',
      })
      loadUsage()
      if (onSuccess) onSuccess()
    } catch (err) {
      console.error('Failed to update profile:', err)
      toast({
        title: 'Error',
        description: 'Failed to update profile',
        variant: 'destructive',
      })
    } finally {
      setUpdating(false)
    }
  }

  const handleRemovePeriod = async (periodId) => {
    try {
      setRemoving(periodId)
      await automationAPI.removePeriodFromChannels(periodId, selectedChannelIds)
      toast({
        title: 'Success',
        description: 'Period removed from selected channels',
      })
      loadUsage()
      if (onSuccess) onSuccess()
    } catch (err) {
      console.error('Failed to remove period:', err)
      toast({
        title: 'Error',
        description: 'Failed to remove period',
        variant: 'destructive',
      })
    } finally {
      setRemoving(false)
    }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-4xl max-h-[85vh] overflow-hidden flex flex-col">
        <DialogHeader>
          <DialogTitle>Edit Common Automation Periods</DialogTitle>
          <DialogDescription>
            These are automation periods assigned to one or more of the {selectedChannelIds.length} selected channels.
          </DialogDescription>
        </DialogHeader>

        {loading ? (
          <div className="flex items-center justify-center py-12">
            <Loader2 className="h-8 w-8 animate-spin text-muted-foreground" />
          </div>
        ) : !usage || usage.periods.length === 0 ? (
          <div className="text-center py-12">
            <Clock className="h-12 w-12 mx-auto mb-4 opacity-50 text-muted-foreground" />
            <p className="text-muted-foreground">No automation periods assigned to selected channels</p>
          </div>
        ) : (
          <div className="flex-1 overflow-y-auto pr-2">
            <div className="space-y-4">
              {usage.periods.map((item) => (
                <div key={item.id} className="p-4 border rounded-lg bg-card">
                  <div className="flex items-start justify-between gap-4 mb-4">
                    <div>
                      <div className="flex items-center gap-2 mb-1">
                        <span className="font-semibold">{item.name}</span>
                        <Badge variant="secondary">
                          Used in {item.count} channel{item.count !== 1 ? 's' : ''} ({item.percentage}%)
                        </Badge>
                      </div>
                      <div className="text-sm text-muted-foreground">
                        Manage this period's profile or remove it from all {selectedChannelIds.length} channels.
                      </div>
                    </div>
                    <Button
                      variant="outline"
                      size="sm"
                      className="text-destructive border-destructive hover:bg-destructive/10"
                      disabled={removing === item.id}
                      onClick={() => handleRemovePeriod(item.id)}
                    >
                      {removing === item.id ? (
                        <Loader2 className="h-4 w-4 animate-spin" />
                      ) : (
                        <Trash2 className="h-4 w-4 mr-2" />
                      )}
                      Remove from All
                    </Button>
                  </div>

                  <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                    {item.profiles.map((profile) => (
                      <div
                        key={profile.id}
                        className="p-3 bg-muted/50 rounded-md border flex items-center justify-between gap-3"
                      >
                        <div className="min-w-0">
                          <div className="text-xs text-muted-foreground mb-0.5">Profile:</div>
                          <div className="font-medium text-sm truncate">{profile.name}</div>
                          <div className="text-[10px] text-muted-foreground">
                            {profile.channel_ids.length} channel{profile.channel_ids.length !== 1 ? 's' : ''}
                          </div>
                        </div>
                        <div className="flex-shrink-0">
                          <Select disabled={updating === item.id} onValueChange={(value) => handleUpdateProfile(item.id, value)}>
                            <SelectTrigger className="h-8 w-[140px] text-xs">
                              <SelectValue placeholder="Change Profile" />
                            </SelectTrigger>
                            <SelectContent>
                              {profiles.map((itemProfile) => (
                                <SelectItem key={itemProfile.id} value={itemProfile.id} disabled={itemProfile.id === profile.id}>
                                  {itemProfile.name}
                                </SelectItem>
                              ))}
                            </SelectContent>
                          </Select>
                        </div>
                      </div>
                    ))}
                  </div>
                </div>
              ))}
            </div>
          </div>
        )}

        <DialogFooter className="pt-4 border-t mt-4">
          <Button variant="outline" onClick={() => onOpenChange(false)}>
            Close
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
