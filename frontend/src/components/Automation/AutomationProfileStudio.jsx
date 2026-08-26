import { useState, useEffect } from 'react'
import { useNavigate } from 'react-router-dom'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card.jsx'
import { Button } from '@/components/ui/button.jsx'
import { Input } from '@/components/ui/input.jsx'
import { Label } from '@/components/ui/label.jsx'
import { Switch } from '@/components/ui/switch.jsx'
import { Badge } from '@/components/ui/badge.jsx'
import { Separator } from '@/components/ui/separator.jsx'
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { Loader2, Plus, Pencil, Trash2, AlertCircle, Check, X, ArrowUp, ArrowDown } from 'lucide-react'
import { automationAPI, m3uAPI, regexAPI } from '@/services/api.js'
import { Checkbox } from '@/components/ui/checkbox.jsx'
import { useToast } from '@/hooks/use-toast.js'
import {
    Table,
    TableBody,
    TableCell,
    TableHead,
    TableHeader,
    TableRow,
} from "@/components/ui/table"

export default function AutomationProfileStudio() {
    const [profiles, setProfiles] = useState([])
    const [loading, setLoading] = useState(true)
    const [selectedProfiles, setSelectedProfiles] = useState([])
    const [globalSettings, setGlobalSettings] = useState({
        regular_automation_enabled: false,
        playlist_update_interval_minutes: { type: 'interval', value: 5 }
    })
    const [regexGlobalSettings, setRegexGlobalSettings] = useState({
        case_sensitive: true,
        require_exact_match: false
    })
    const [savingRegexGlobalSettings, setSavingRegexGlobalSettings] = useState(false)

    const { toast } = useToast()
    const navigate = useNavigate()

    useEffect(() => {
        loadData()
    }, [])

    const loadData = async () => {
        try {
            setLoading(true)
            const [profilesResponse, m3uResponse, globalSettingsResponse, regexSettingsResponse] = await Promise.all([
                automationAPI.getProfiles(),
                m3uAPI.getAccounts(),
                automationAPI.getGlobalSettings(),
                regexAPI.getGlobalSettings()
            ])
            setProfiles(Object.values(profilesResponse.data))
            setGlobalSettings(globalSettingsResponse.data || {
                regular_automation_enabled: false,
                playlist_update_interval_minutes: { type: 'interval', value: 5 }
            })
            setRegexGlobalSettings({
                case_sensitive: true,
                require_exact_match: false,
                ...(regexSettingsResponse.data || {})
            })
        } catch (err) {
            console.error('Failed to load profiles:', err)
            toast({
                title: "Error",
                description: "Failed to load automation data",
                variant: "destructive"
            })
        } finally {
            setLoading(false)
        }
    }

    const handleCreateProfile = () => {
        navigate('/automation/profiles/new')
    }

    const updateGlobalSetting = async (key, value) => {
        try {
            const newSettings = { ...globalSettings, [key]: value }
            setGlobalSettings(newSettings)
            await automationAPI.updateGlobalSettings({ [key]: value })
            toast({ title: "Settings Updated", description: "Global automation settings saved." })
        } catch (error) {
            console.error('Error updating global settings:', error)
            loadData()
            toast({ variant: "destructive", title: "Error", description: "Failed to update settings" })
        }
    }

    const updateRegexGlobalSetting = async (key, value) => {
        const nextSettings = { ...regexGlobalSettings, [key]: value }
        const previousSettings = regexGlobalSettings

        try {
            setRegexGlobalSettings(nextSettings)
            setSavingRegexGlobalSettings(true)
            const response = await regexAPI.updateGlobalSettings({ [key]: value })
            setRegexGlobalSettings({
                case_sensitive: true,
                require_exact_match: false,
                ...(response.data?.settings || nextSettings)
            })
            toast({ title: "Settings Updated", description: "Global regex matching settings saved." })
        } catch (error) {
            console.error('Error updating regex global settings:', error)
            setRegexGlobalSettings(previousSettings)
            toast({ variant: "destructive", title: "Error", description: "Failed to update regex matching settings" })
        } finally {
            setSavingRegexGlobalSettings(false)
        }
    }

    const handleEditProfile = (profileId) => {
        navigate(`/automation/profiles/${profileId}`)
    }

    const handleDeleteProfile = async (profileId) => {
        if (!confirm('Are you sure you want to delete this profile?')) {
            return
        }

        try {
            await automationAPI.deleteProfile(profileId)
            toast({ title: "Success", description: "Profile deleted successfully" })
            loadData()
        } catch (err) {
            toast({ title: "Error", description: "Failed to delete profile", variant: "destructive" })
        }
    }

    const handleBulkDelete = async () => {
        if (selectedProfiles.length === 0) return

        if (!confirm(`Are you sure you want to delete ${selectedProfiles.length} profiles?`)) {
            return
        }

        try {
            await automationAPI.bulkDeleteProfiles(selectedProfiles)
            toast({ title: "Success", description: `${selectedProfiles.length} profiles deleted successfully` })
            setSelectedProfiles([])
            loadData()
        } catch (err) {
            toast({ title: "Error", description: "Failed to delete profiles", variant: "destructive" })
        }
    }

    const toggleProfileSelection = (profileId) => {
        if (profileId === 'default') return // Cannot select default for deletion
        setSelectedProfiles(prev =>
            prev.includes(profileId)
                ? prev.filter(id => id !== profileId)
                : [...prev, profileId]
        )
    }

    const toggleAllSelection = () => {
        const deletableProfiles = profiles.filter(p => p.id !== 'default').map(p => p.id)
        if (selectedProfiles.length === deletableProfiles.length) {
            setSelectedProfiles([])
        } else {
            setSelectedProfiles(deletableProfiles)
        }
    }

    if (loading) {
        return (
            <div className="flex justify-center p-8">
                <Loader2 className="h-8 w-8 animate-spin text-muted-foreground" />
            </div>
        )
    }

    return (
        <div className="space-y-6 pb-20">
            <div className="flex justify-between items-center">
                <div>
                    <h3 className="text-lg font-medium">Automation Profiles</h3>
                    <p className="text-sm text-muted-foreground">
                        Create profiles to define how channels are automated (matching, checking, etc.)
                    </p>
                </div>
                <div className="flex gap-2">
                    {selectedProfiles.length > 0 && (
                        <Button variant="destructive" onClick={handleBulkDelete}>
                            <Trash2 className="mr-2 h-4 w-4" />
                            Delete ({selectedProfiles.length})
                        </Button>
                    )}
                    <Button onClick={handleCreateProfile}>
                        <Plus className="mr-2 h-4 w-4" />
                        Create Profile
                    </Button>
                </div>
            </div>

            {/* Global Settings Card */}
            <Card className="mb-6">
                <CardHeader>
                    <CardTitle>Global Automation Settings</CardTitle>
                    <CardDescription>Configure the master switch and schedule for global automation runs.</CardDescription>
                </CardHeader>
                <CardContent className="space-y-6">
                    <div className="flex items-center justify-between">
                        <div className="space-y-0.5">
                            <Label className="text-base">Enable Regular Automation</Label>
                            <p className="text-sm text-muted-foreground">Master switch for all automation features</p>
                        </div>
                        <Switch
                            checked={globalSettings.regular_automation_enabled}
                            onCheckedChange={(checked) => updateGlobalSetting('regular_automation_enabled', checked)}
                        />
                    </div>
                    <Separator />
                    <div className="flex items-center justify-between">
                        <div className="space-y-0.5">
                            <Label className="text-base">Case Sensitive Regex Matching</Label>
                            <p className="text-sm text-muted-foreground">
                                Match channel regex patterns using exact uppercase and lowercase letters.
                            </p>
                        </div>
                        <Switch
                            checked={Boolean(regexGlobalSettings.case_sensitive)}
                            disabled={savingRegexGlobalSettings}
                            onCheckedChange={(checked) => updateRegexGlobalSetting('case_sensitive', Boolean(checked))}
                        />
                    </div>
                </CardContent>
            </Card>

            <Card>
                <CardContent className="p-0">
                    <Table>
                        <TableHeader>
                            <TableRow>
                                <TableHead className="w-[40px]">
                                    <Checkbox
                                        checked={selectedProfiles.length > 0 && selectedProfiles.length === profiles.filter(p => p.id !== 'default').length}
                                        onCheckedChange={toggleAllSelection}
                                    />
                                </TableHead>
                                <TableHead>Profile Name</TableHead>
                                <TableHead className="text-center">PL Update</TableHead>
                                <TableHead className="text-center">Matching</TableHead>
                                <TableHead className="text-center">Checking</TableHead>
                                <TableHead>Min Res</TableHead>
                                <TableHead>Min Bitrate</TableHead>
                                <TableHead className="text-right">Actions</TableHead>
                            </TableRow>
                        </TableHeader>
                        <TableBody>
                            {profiles.map((profile) => (
                                <TableRow key={profile.id} className={selectedProfiles.includes(profile.id) ? "bg-muted/50" : ""}>
                                    <TableCell>
                                        <Checkbox
                                            disabled={profile.id === 'default'}
                                            checked={selectedProfiles.includes(profile.id)}
                                            onCheckedChange={() => toggleProfileSelection(profile.id)}
                                        />
                                    </TableCell>
                                    <TableCell>
                                        <div className="font-medium">{profile.name}</div>
                                        <div className="text-xs text-muted-foreground truncate max-w-[200px]">{profile.description}</div>
                                    </TableCell>
                                    <TableCell className="text-center">
                                        <Badge variant={profile.m3u_update?.enabled ? "default" : "secondary"}>
                                            {profile.m3u_update?.enabled ? "Yes" : "No"}
                                        </Badge>
                                    </TableCell>
                                    <TableCell className="text-center">
                                        <Badge variant={profile.stream_matching?.enabled ? "default" : "secondary"}>
                                            {profile.stream_matching?.enabled ? "Yes" : "No"}
                                        </Badge>
                                    </TableCell>
                                    <TableCell className="text-center">
                                        <Badge variant={profile.stream_checking?.enabled ? "default" : "secondary"}>
                                            {profile.stream_checking?.enabled ? "Yes" : "No"}
                                        </Badge>
                                    </TableCell>
                                    <TableCell>
                                        <span className="text-sm">{profile.stream_checking?.min_resolution || 'Any'}</span>
                                    </TableCell>
                                    <TableCell>
                                        <span className="text-sm">{profile.stream_checking?.min_bitrate ? `${profile.stream_checking.min_bitrate} kbps` : 'Any'}</span>
                                    </TableCell>
                                    <TableCell className="text-right">
                                        <div className="flex justify-end gap-1">
                                            <Button variant="ghost" size="icon" className="h-8 w-8" onClick={() => handleEditProfile(profile.id)}>
                                                <Pencil className="h-4 w-4" />
                                            </Button>
                                            {profile.id !== 'default' && (
                                                <Button variant="ghost" size="icon" className="h-8 w-8 text-destructive" onClick={() => handleDeleteProfile(profile.id)}>
                                                    <Trash2 className="h-4 w-4" />
                                                </Button>
                                            )}
                                        </div>
                                    </TableCell>
                                </TableRow>
                            ))}
                        </TableBody>
                    </Table>
                </CardContent>
            </Card>
        </div>
    )
}
