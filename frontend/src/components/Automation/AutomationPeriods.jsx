import { useState, useEffect } from 'react'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card.jsx'
import { Button } from '@/components/ui/button.jsx'
import { Input } from '@/components/ui/input.jsx'
import { Label } from '@/components/ui/label.jsx'
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle, DialogFooter } from '@/components/ui/dialog.jsx'
import { AlertDialog, AlertDialogAction, AlertDialogCancel, AlertDialogContent, AlertDialogDescription, AlertDialogFooter, AlertDialogHeader, AlertDialogTitle } from '@/components/ui/alert-dialog.jsx'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select.jsx'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs.jsx'
import { Badge } from '@/components/ui/badge.jsx'
import { Switch } from '@/components/ui/switch.jsx'
import { useToast } from '@/hooks/use-toast.js'
import { automationAPI } from '@/services/api.js'
import { Plus, Trash2, Edit2, Clock, Calendar, Loader2 } from 'lucide-react'

export default function AutomationPeriods() {
  const [periods, setPeriods] = useState([])
  const [profiles, setProfiles] = useState([])
  const [loading, setLoading] = useState(true)
  const [editDialogOpen, setEditDialogOpen] = useState(false)
  const [deleteDialogOpen, setDeleteDialogOpen] = useState(false)
  const [currentPeriod, setCurrentPeriod] = useState(null)
  const [periodToDelete, setPeriodToDelete] = useState(null)
  const [saving, setSaving] = useState(false)

  const { toast } = useToast()

  useEffect(() => {
    loadData()
  }, [])

  const loadData = async () => {
    try {
      setLoading(true)
      const [periodsResponse, profilesResponse] = await Promise.all([
        automationAPI.getPeriods(),
        automationAPI.getProfiles()
      ])
      setPeriods(periodsResponse.data)
      setProfiles(profilesResponse.data)
    } catch (err) {
      console.error('Failed to load automation periods:', err)
      toast({
        title: "Error",
        description: "Failed to load automation periods",
        variant: "destructive"
      })
    } finally {
      setLoading(false)
    }
  }

  const handleCreate = () => {
    setCurrentPeriod({
      name: '',
      schedule: { type: 'interval', value: 60 },
      priority: 0,
      catch_up_missed_runs: false
    })
    setEditDialogOpen(true)
  }

  const handleEdit = (period) => {
    setCurrentPeriod({ ...period })
    setEditDialogOpen(true)
  }

  const handleSave = async () => {
    if (!currentPeriod.name.trim()) {
      toast({
        title: "Validation Error",
        description: "Period name is required",
        variant: "destructive"
      })
      return
    }

    if (currentPeriod.schedule?.type === 'cron') {
      const cronRegex = /^(\*|([0-5]?\d)(-[0-5]?\d)?(,\d+)*(\/\d+)?) (\*|([01]?\d|2[0-3])(-[01]?\d|2[0-3])?(,\d+)*(\/\d+)?) (\*|(0?[1-9]|[12]\d|3[01])(-(0?[1-9]|[12]\d|3[01]))?(,\d+)*(\/\d+)?) (\*|([1-9]|1[012])(-([1-9]|1[012]))?(,\d+)*(\/\d+)?) (\*|([0-6])(-[0-6])?(,\d+)*(\/\d+)?)$/;
      if (!cronRegex.test(currentPeriod.schedule.value)) {
        toast({
          title: "Validation Error",
          description: "Invalid cron expression format (must be 5 fields)",
          variant: "destructive"
        })
        return
      }
    }

    try {
      setSaving(true)
      if (currentPeriod.id) {
        // Update existing
        await automationAPI.updatePeriod(currentPeriod.id, currentPeriod)
        toast({
          title: "Success",
          description: "Period updated successfully"
        })
      } else {
        // Create new
        await automationAPI.createPeriod(currentPeriod)
        toast({
          title: "Success",
          description: "Period created successfully"
        })
      }
      setEditDialogOpen(false)
      loadData()
    } catch (err) {
      console.error('Failed to save period:', err)
      toast({
        title: "Error",
        description: err.response?.data?.error || "Failed to save period",
        variant: "destructive"
      })
    } finally {
      setSaving(false)
    }
  }

  const handleDelete = (period) => {
    setPeriodToDelete(period)
    setDeleteDialogOpen(true)
  }

  const confirmDelete = async () => {
    try {
      await automationAPI.deletePeriod(periodToDelete.id)
      toast({
        title: "Success",
        description: "Period deleted successfully"
      })
      setDeleteDialogOpen(false)
      loadData()
    } catch (err) {
      console.error('Failed to delete period:', err)
      toast({
        title: "Error",
        description: "Failed to delete period",
        variant: "destructive"
      })
    }
  }

  const getProfileName = (profileId) => {
    const profile = profiles.find(p => p.id === profileId)
    return profile ? profile.name : 'Unknown Profile'
  }

  const formatSchedule = (schedule) => {
    if (!schedule) return 'Not configured'
    if (schedule.type === 'interval') {
      return `Every ${schedule.value} minutes`
    } else if (schedule.type === 'cron') {
      return `Cron: ${schedule.value}`
    }
    return 'Unknown'
  }

  if (loading) {
    return (
      <div className="flex items-center justify-center min-h-[400px]">
        <Loader2 className="h-8 w-8 animate-spin text-muted-foreground" />
      </div>
    )
  }

  return (
    <div className="space-y-6">
      <Card>
        <CardHeader>
          <div className="flex items-center justify-between">
            <div>
              <CardTitle>Automation Periods</CardTitle>
              <CardDescription>
                Create and manage automation periods with different schedules. Profiles are assigned per-channel.
              </CardDescription>
            </div>
            <Button onClick={handleCreate}>
              <Plus className="h-4 w-4 mr-2" />
              Create Period
            </Button>
          </div>
        </CardHeader>
        <CardContent>
          {periods.length === 0 ? (
            <div className="text-center py-12 text-muted-foreground">
              <Calendar className="h-12 w-12 mx-auto mb-4 opacity-50" />
              <p className="text-lg mb-2">No automation periods configured</p>
              <p className="text-sm mb-4">Create your first automation period to get started</p>
              <Button onClick={handleCreate} variant="outline">
                <Plus className="h-4 w-4 mr-2" />
                Create Period
              </Button>
            </div>
          ) : (
            <div className="space-y-4">
              {periods.map((period) => (
                <div
                  key={period.id}
                  className="flex items-center justify-between p-4 border rounded-lg hover:bg-accent/50 transition-colors"
                >
                  <div className="flex-1 space-y-1">
                    <div className="flex items-center gap-2">
                      <h3 className="font-semibold">{period.name}</h3>
                      <Badge variant="secondary">
                        {period.channel_count || 0} channel{period.channel_count !== 1 ? 's' : ''}
                      </Badge>
                      {period.channel_count > 0 ? (
                        <Badge className="bg-green-600 hover:bg-green-700 text-white">
                          Enabled
                        </Badge>
                      ) : (
                        <Badge variant="outline" className="text-muted-foreground">
                          Disabled
                        </Badge>
                      )}
                    </div>
                    <div className="flex items-center gap-4 text-sm text-muted-foreground mt-2">
                      <div className="flex items-center gap-1">
                        <Clock className="h-4 w-4" />
                        {formatSchedule(period.schedule)}
                      </div>
                      <Badge variant="outline" className="font-normal text-xs text-muted-foreground w-fit">
                        Priority: {period.priority || 0}
                      </Badge>
                      {period.catch_up_missed_runs && (
                        <Badge variant="outline" className="font-normal text-xs text-muted-foreground w-fit">
                          Startup catch-up
                        </Badge>
                      )}
                    </div>
                  </div>
                  <div className="flex items-center gap-2">
                    <Button
                      variant="ghost"
                      size="sm"
                      onClick={() => handleEdit(period)}
                    >
                      <Edit2 className="h-4 w-4" />
                    </Button>
                    <Button
                      variant="ghost"
                      size="sm"
                      onClick={() => handleDelete(period)}
                    >
                      <Trash2 className="h-4 w-4 text-destructive" />
                    </Button>
                  </div>
                </div>
              ))}
            </div>
          )}
        </CardContent>
      </Card>

      {/* Edit/Create Dialog */}
      <Dialog open={editDialogOpen} onOpenChange={setEditDialogOpen}>
        <DialogContent className="max-w-2xl">
          <DialogHeader>
            <DialogTitle>
              {currentPeriod?.id ? 'Edit Automation Period' : 'Create Automation Period'}
            </DialogTitle>
            <DialogDescription>
              Configure the schedule for this automation period. Profiles will be assigned per-channel.
            </DialogDescription>
          </DialogHeader>

          {currentPeriod && (
            <div className="space-y-6">
              {/* Name */}
              <div className="space-y-2">
                <Label htmlFor="period-name">Period Name</Label>
                <Input
                  id="period-name"
                  placeholder="e.g., Evening Automation"
                  value={currentPeriod.name}
                  onChange={(e) => setCurrentPeriod({ ...currentPeriod, name: e.target.value })}
                />
              </div>

              {/* Schedule Configuration */}
              <div className="space-y-2">
                <Label>Schedule</Label>
                <Tabs
                  value={currentPeriod.schedule?.type || 'interval'}
                  onValueChange={(type) => {
                    const value = type === 'interval' ? 60 : '*/60 * * * *'
                    setCurrentPeriod({
                      ...currentPeriod,
                      schedule: { type, value }
                    })
                  }}
                >
                  <TabsList className="grid w-full grid-cols-2">
                    <TabsTrigger value="interval">Interval (Minutes)</TabsTrigger>
                    <TabsTrigger value="cron">Cron Expression</TabsTrigger>
                  </TabsList>

                  <TabsContent value="interval" className="space-y-2">
                    <div className="flex items-center gap-2">
                      <Input
                        type="number"
                        min="1"
                        max="1440"
                        className="max-w-[120px]"
                        value={currentPeriod.schedule?.type === 'interval' ? currentPeriod.schedule.value : 60}
                        onChange={(e) => setCurrentPeriod({
                          ...currentPeriod,
                          schedule: { type: 'interval', value: parseInt(e.target.value) || 60 }
                        })}
                      />
                      <span className="text-sm text-muted-foreground">minutes</span>
                    </div>
                    <p className="text-xs text-muted-foreground">
                      How often to run the automation cycle
                    </p>
                  </TabsContent>

                  <TabsContent value="cron" className="space-y-2">
                    <Input
                      placeholder="*/60 * * * *"
                      value={currentPeriod.schedule?.type === 'cron' ? currentPeriod.schedule.value : ''}
                      onChange={(e) => setCurrentPeriod({
                        ...currentPeriod,
                        schedule: { type: 'cron', value: e.target.value }
                      })}
                    />
                    <p className="text-xs text-muted-foreground">
                      Standard 5-field cron expression (minute hour day month day-of-week)
                      <br />
                      Example: */30 * * * * = every 30 minutes
                    </p>
                  </TabsContent>
                </Tabs>
              </div>

              {/* Priority Configuration */}
              <div className="space-y-2">
                <Label htmlFor="period-priority">Priority</Label>
                <div className="flex items-center gap-2">
                  <Input
                    id="period-priority"
                    type="number"
                    min="0"
                    max="1000"
                    className="max-w-[120px]"
                    value={currentPeriod.priority || 0}
                    onChange={(e) => setCurrentPeriod({ ...currentPeriod, priority: parseInt(e.target.value) || 0 })}
                  />
                  <span className="text-xs text-muted-foreground">
                    Higher values take precedence when schedules overlap exactly.
                  </span>
                </div>
              </div>

              <div className="flex items-start justify-between gap-4 rounded-md border p-4">
                <div className="space-y-1">
                  <Label htmlFor="period-catch-up">Startup catch-up</Label>
                  <p className="text-xs text-muted-foreground leading-relaxed">
                    When no saved last-run timestamp exists for this period, include it once on the next scheduler pass instead of waiting for the next interval or cron block.
                  </p>
                </div>
                <Switch
                  id="period-catch-up"
                  checked={Boolean(currentPeriod.catch_up_missed_runs)}
                  onCheckedChange={(checked) => setCurrentPeriod({
                    ...currentPeriod,
                    catch_up_missed_runs: checked
                  })}
                />
              </div>
            </div>
          )}

          <DialogFooter>
            <Button variant="outline" onClick={() => setEditDialogOpen(false)}>
              Cancel
            </Button>
            <Button onClick={handleSave} disabled={saving}>
              {saving && <Loader2 className="h-4 w-4 mr-2 animate-spin" />}
              {currentPeriod?.id ? 'Update' : 'Create'}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Delete Confirmation Dialog */}
      <AlertDialog open={deleteDialogOpen} onOpenChange={setDeleteDialogOpen}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Delete Automation Period</AlertDialogTitle>
            <AlertDialogDescription>
              Are you sure you want to delete "{periodToDelete?.name}"?
              This will remove the period from all assigned channels.
              This action cannot be undone.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <AlertDialogAction onClick={confirmDelete} className="bg-destructive text-destructive-foreground hover:bg-destructive/90">
              Delete
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  )
}
