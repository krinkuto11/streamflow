import { useState, useEffect } from 'react'
import { useParams, useNavigate } from 'react-router-dom'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card.jsx'
import { Button } from '@/components/ui/button.jsx'
import { Input } from '@/components/ui/input.jsx'
import { Label } from '@/components/ui/label.jsx'
import { Switch } from '@/components/ui/switch.jsx'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select.jsx'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert.jsx'
import { Badge } from '@/components/ui/badge.jsx'
import { Separator } from '@/components/ui/separator.jsx'
import { Checkbox } from '@/components/ui/checkbox.jsx'
import { Loader2, ArrowLeft, Save, AlertCircle, ArrowUp, ArrowDown, Check, GripVertical } from 'lucide-react'
import { automationAPI, m3uAPI } from '@/services/api.js'
import { useToast } from '@/hooks/use-toast.js'
import { cn } from '@/lib/utils'
import { DndContext, closestCenter, KeyboardSensor, PointerSensor, useSensor, useSensors } from '@dnd-kit/core'
import { arrayMove, SortableContext, sortableKeyboardCoordinates, verticalListSortingStrategy, useSortable } from '@dnd-kit/sortable'
import { CSS } from '@dnd-kit/utilities'

const DEFAULT_PROFILE = {
    name: '',
    description: '',
    m3u_update: {
        enabled: true,
        playlists: []
    },
    stream_matching: {
        enabled: true,
        validate_existing_streams: false
    },
    stream_checking: {
        enabled: true,
        allow_revive: true,
        remove_dead_streams: true,
        check_all_streams: false,
        loop_check_enabled: false,
        stream_limit: 0,
        min_resolution: 'any',
        min_fps: 0,
        min_bitrate: 0,
        m3u_priority: [],
        m3u_priority_mode: 'absolute'
    },
    scoring_weights: {
        bitrate: 0.40,
        resolution: 0.35,
        fps: 0.15,
        codec: 0.10,
        prefer_h265: true,
        loop_penalty: 0
    }
}

const STEPS = [
    { id: 'm3u_update', label: 'Playlist Updating', sublabel: 'M3U Updates' },
    { id: 'stream_matching', label: 'Stream Matching', sublabel: 'Regex Pattern matching' },
    { id: 'stream_checking', label: 'Stream Checking', sublabel: 'Quality & Scoring' }
]

export default function AutomationProfileEditor() {
    const { profileId } = useParams()
    const navigate = useNavigate()
    const { toast } = useToast()
    const [loading, setLoading] = useState(true)
    const [saving, setSaving] = useState(false)
    const [profile, setProfile] = useState(null)
    const [m3uAccounts, setM3uAccounts] = useState([])
    const [activeStep, setActiveStep] = useState('m3u_update')

    useEffect(() => {
        loadData()
    }, [profileId])

    const loadData = async () => {
        try {
            setLoading(true)
            // Fetch M3U accounts for the selection list
            const m3uResponse = await m3uAPI.getAccounts()
            setM3uAccounts(m3uResponse.data.accounts || [])

            if (profileId === 'new') {
                setProfile({ ...DEFAULT_PROFILE })
            } else {
                try {
                    const profileResponse = await automationAPI.getProfile(profileId)
                    if (profileResponse.data) {
                        // Merge with defaults to ensure all config matrices exist for older profiles
                        const loadedProfile = profileResponse.data
                        setProfile({
                            ...DEFAULT_PROFILE,
                            ...loadedProfile,
                            m3u_update: {
                                ...DEFAULT_PROFILE.m3u_update,
                                ...(loadedProfile.m3u_update || {})
                            },
                            stream_matching: {
                                ...DEFAULT_PROFILE.stream_matching,
                                ...(loadedProfile.stream_matching || {})
                            },
                            stream_checking: {
                                ...DEFAULT_PROFILE.stream_checking,
                                ...(loadedProfile.stream_checking || {})
                            },
                            scoring_weights: {
                                ...DEFAULT_PROFILE.scoring_weights,
                                ...(loadedProfile.scoring_weights || {})
                            }
                        })
                    } else {
                        throw new Error('No profile data received')
                    }
                } catch (err) {
                    console.error('Failed to fetch profile:', err)
                    toast({
                        title: "Error",
                        description: "Profile not found or failed to load",
                        variant: "destructive"
                    })
                    navigate('/settings')
                }
            }
        } catch (err) {
            console.error('Failed to load initial data:', err)
            toast({
                title: "Error",
                description: "Failed to load necessary data",
                variant: "destructive"
            })
        } finally {
            setLoading(false)
        }
    }

    const updateProfile = (field, value) => {
        setProfile(prev => {
            const newData = { ...prev }
            if (field.includes('.')) {
                const parts = field.split('.')
                let current = newData
                for (let i = 0; i < parts.length - 1; i++) {
                    const key = parts[i]
                    if (['__proto__', 'constructor', 'prototype'].includes(key)) {
                        continue; // Block prototype pollution
                    }
                    if (!current[key]) current[key] = {}
                    current = current[key]
                }
                if (!['__proto__', 'constructor', 'prototype'].includes(parts[parts.length - 1])) {
                    current[parts[parts.length - 1]] = value
                }
            } else {
                newData[field] = value
            }
            return newData
        })
    }

    const handleSave = async () => {
        if (!profile.name) {
            toast({
                title: "Validation Error",
                description: "Profile name is required",
                variant: "destructive"
            })
            return
        }
        try {
            setSaving(true)
            if (profileId === 'new') {
                await automationAPI.createProfile(profile)
                toast({ title: "Success", description: "Profile created successfully" })
            } else {
                await automationAPI.updateProfile(profileId, profile)
                toast({ title: "Success", description: "Profile updated successfully" })
            }
            navigate('/settings')
        } catch (err) {
            toast({
                title: "Error",
                description: "Failed to save profile",
                variant: "destructive"
            })
        } finally {
            setSaving(false)
        }
    }

    if (loading) {
        return (
            <div className="flex justify-center items-center h-[60vh]">
                <Loader2 className="h-8 w-8 animate-spin text-muted-foreground" />
            </div>
        )
    }

    const isStepEnabled = (stepId) => {
        return profile?.[stepId]?.enabled
    }

    return (
        <div className="max-w-4xl mx-auto space-y-8">
            <div className="flex items-center justify-between">
                <div className="flex items-center gap-4">
                    <Button variant="ghost" size="icon" onClick={() => navigate('/settings')}>
                        <ArrowLeft className="h-5 w-5" />
                    </Button>
                    <div>
                        <h2 className="text-2xl font-bold tracking-tight">
                            {profileId === 'new' ? 'Create Profile' : `Edit: ${profile.name}`}
                        </h2>
                        <p className="text-muted-foreground">Configure how automation behaves for assigned channels.</p>
                    </div>
                </div>
                <Button onClick={handleSave} disabled={saving} className="min-w-[120px]">
                    {saving ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : <Save className="mr-2 h-4 w-4" />}
                    Save Changes
                </Button>
            </div>

            {/* Profile Header Card */}
            <Card>
                <CardHeader>
                    <CardTitle>Basic Information</CardTitle>
                    <CardDescription>Give your profile a name and description.</CardDescription>
                </CardHeader>
                <CardContent className="grid gap-6 md:grid-cols-2">
                    <div className="space-y-2">
                        <Label htmlFor="name">Profile Name</Label>
                        <Input
                            id="name"
                            value={profile.name}
                            onChange={(e) => updateProfile('name', e.target.value)}
                            placeholder="e.g., Premium Sports"
                        />
                    </div>
                    <div className="space-y-2">
                        <Label htmlFor="description">Description</Label>
                        <Input
                            id="description"
                            value={profile.description}
                            onChange={(e) => updateProfile('description', e.target.value)}
                            placeholder="Brief description of this profile"
                        />
                    </div>
                </CardContent>
            </Card>

            {/* Progress Line UI */}
            <div className="relative pt-12 pb-4">
                <div className="absolute top-[4.25rem] left-0 w-full h-1 bg-muted -translate-y-1/2 z-0" />
                <div className="flex justify-between items-center relative z-10">
                    {STEPS.map((step, index) => {
                        const enabled = isStepEnabled(step.id)
                        const active = activeStep === step.id
                        return (
                            <div key={step.id} className="flex flex-col items-center">
                                <button
                                    onClick={() => setActiveStep(step.id)}
                                    className={cn(
                                        "w-10 h-10 rounded-full border-4 flex items-center justify-center transition-all duration-300",
                                        active ? "ring-4 ring-primary/20 scale-110" : "scale-100",
                                        enabled
                                            ? "bg-primary border-primary text-primary-foreground"
                                            : "bg-background border-muted text-muted-foreground"
                                    )}
                                >
                                    {enabled ? <Check className="h-5 w-5" /> : <span className="text-sm font-bold">{index + 1}</span>}
                                </button>
                                <div className="mt-3 text-center">
                                    <p className={cn(
                                        "text-sm font-medium transition-colors",
                                        active ? "text-primary" : "text-muted-foreground"
                                    )}>
                                        {step.label}
                                    </p>
                                    <p className="text-[10px] text-muted-foreground uppercase tracking-wider hidden md:block">
                                        {step.sublabel}
                                    </p>
                                </div>
                            </div>
                        )
                    })}
                </div>
            </div>

            {/* Step Content Area */}
            <Card className={cn("transition-all duration-300", isStepEnabled(activeStep) ? "min-h-[400px]" : "")}>
                <CardHeader>
                    <div className="flex items-center justify-between mb-2">
                        <div>
                            <CardTitle>{STEPS.find(s => s.id === activeStep)?.label}</CardTitle>
                            <CardDescription>Configure options for this automation step.</CardDescription>
                        </div>
                        <div className="flex items-center gap-2">
                            <span className="text-sm font-medium">{isStepEnabled(activeStep) ? 'Step Enabled' : 'Step Disabled'}</span>
                            <Switch
                                checked={isStepEnabled(activeStep)}
                                onCheckedChange={(checked) => {
                                    if (activeStep === 'm3u_update' && checked) {
                                        setProfile(prev => ({
                                            ...prev,
                                            m3u_update: {
                                                ...prev.m3u_update,
                                                enabled: true,
                                                playlists: m3uAccounts.map(a => a.id)
                                            }
                                        }))
                                    } else if (activeStep === 'stream_matching' && checked) {
                                        updateProfile('stream_matching.enabled', true)
                                    } else {
                                        updateProfile(`${activeStep}.enabled`, checked)
                                    }
                                }}
                            />
                        </div>
                    </div>
                </CardHeader>
                <CardContent className="space-y-6">
                    {/* Step 1: Playlist Updating */}
                    {activeStep === 'm3u_update' && (
                        <div className="space-y-4">
                            <p className="text-sm text-muted-foreground">
                                Automatically pick up changes from M3U playlists for channels assigned to this profile.
                            </p>
                            {profile.m3u_update.enabled ? (
                                <div className="space-y-4 border rounded-lg p-4 bg-muted/30">
                                    <Label className="text-sm font-semibold">Playlists to Update</Label>
                                    <p className="text-xs text-muted-foreground mb-4">Select specific playlists to follow. Leave empty to update from any playlist that matches the channel.</p>
                                    <div className="grid gap-3 max-h-[300px] overflow-y-auto">
                                        {m3uAccounts.map(account => (
                                            <div key={account.id} className="flex items-center space-x-3 p-2 hover:bg-muted/50 rounded-md transition-colors">
                                                <Checkbox
                                                    id={`m3u-${account.id}`}
                                                    checked={profile.m3u_update.playlists.includes(account.id)}
                                                    onCheckedChange={(checked) => {
                                                        const current = profile.m3u_update.playlists
                                                        let newPlaylists = []
                                                        if (checked) {
                                                            newPlaylists = [...current, account.id]
                                                        } else {
                                                            newPlaylists = current.filter(id => id !== account.id)
                                                        }
                                                        if (newPlaylists.length === 0) {
                                                            setProfile(prev => ({
                                                                ...prev,
                                                                m3u_update: {
                                                                    ...prev.m3u_update,
                                                                    enabled: false,
                                                                    playlists: []
                                                                }
                                                            }))
                                                        } else {
                                                            updateProfile('m3u_update.playlists', newPlaylists)
                                                        }
                                                    }}
                                                />
                                                <Label htmlFor={`m3u-${account.id}`} className="flex-1 cursor-pointer font-medium">{account.name}</Label>
                                            </div>
                                        ))}
                                    </div>
                                </div>
                            ) : (
                                <div className="p-8 border-2 border-dashed rounded-lg text-center opacity-50">
                                    <AlertCircle className="h-12 w-12 text-muted-foreground mx-auto mb-4" />
                                    <p className="font-medium text-muted-foreground">Playlist Updating is Inactive</p>
                                    <p className="text-sm text-muted-foreground mt-1 text-muted-foreground">Changes to source playlists will not be automatically synchronized.</p>
                                </div>
                            )}
                        </div>
                    )}

                    {/* Step 2: Stream Matching */}
                    {activeStep === 'stream_matching' && (
                        <div className="space-y-4">
                            <p className="text-sm text-muted-foreground">
                                Use regex patterns to find and assign new streams to channels.
                            </p>
                            {profile.stream_matching.enabled ? (
                                <div className="space-y-4">
                                    <div className="flex items-center space-x-3 bg-muted/50 p-3 rounded-md">
                                        <Switch
                                            id="validate_streams"
                                            checked={profile.stream_matching.validate_existing_streams}
                                            onCheckedChange={(checked) => updateProfile('stream_matching.validate_existing_streams', checked)}
                                        />
                                        <div className="space-y-0.5">
                                            <Label htmlFor="validate_streams" className="cursor-pointer font-medium">Validate Existing Streams</Label>
                                            <p className="text-[10px] text-muted-foreground">Remove streams that no longer match the channel's Regex rules.</p>
                                        </div>
                                    </div>
                                </div>
                            ) : (
                                <div className="p-8 border-2 border-dashed rounded-lg text-center opacity-50">
                                    <AlertCircle className="h-12 w-12 text-muted-foreground mx-auto mb-4" />
                                    <p className="font-medium text-muted-foreground">Stream Matching is Inactive</p>
                                    <p className="text-sm text-muted-foreground mt-1 text-muted-foreground">Manual stream management will be required.</p>
                                </div>
                            )}
                        </div>
                    )}

                    {/* Step 3: Stream Checking */}
                    {activeStep === 'stream_checking' && (
                        <div className="space-y-2 pb-6">
                            <p className="text-sm text-muted-foreground mb-6">
                                Validate stream quality and availability during automation cycles.
                            </p>
                            {profile.stream_checking.enabled && (
                                <div className="grid gap-8">
                                    <div className="grid md:grid-cols-2 gap-8">
                                        <div className="space-y-4">
                                            <div className="flex items-center space-x-3 bg-muted/50 p-3 rounded-md">
                                                <Switch
                                                    id="revive"
                                                    checked={profile.stream_checking.allow_revive}
                                                    onCheckedChange={(checked) => updateProfile('stream_checking.allow_revive', checked)}
                                                />
                                                <Label htmlFor="revive" className="cursor-pointer font-medium">Allow Automatic Revive (Dead &rarr; Alive)</Label>
                                            </div>
                                            <div className="flex items-center space-x-3 bg-muted/50 p-3 rounded-md">
                                                <Switch
                                                    id="grace_period"
                                                    checked={profile.stream_checking.grace_period}
                                                    onCheckedChange={(checked) => updateProfile('stream_checking.grace_period', checked)}
                                                />
                                                <div className="space-y-0.5">
                                                    <Label htmlFor="grace_period" className="cursor-pointer font-medium">Respect 2h Grace Period</Label>
                                                    <p className="text-[10px] text-muted-foreground">Skip re-analyzing streams checked within the last 2 hours.</p>
                                                </div>
                                            </div>
                                            <div className="flex items-center space-x-3 bg-muted/50 p-3 rounded-md">
                                                <Switch
                                                    id="check_all_streams"
                                                    checked={profile.stream_checking.check_all_streams}
                                                    onCheckedChange={(checked) => updateProfile('stream_checking.check_all_streams', checked)}
                                                />
                                                <div className="space-y-0.5">
                                                    <Label htmlFor="check_all_streams" className="cursor-pointer font-medium">Check All Streams in Channel</Label>
                                                    <p className="text-[10px] text-muted-foreground">Check all streams assigned to the channel, not just matched ones.</p>
                                                </div>
                                            </div>
                                            <div className="flex items-center space-x-3 bg-muted/50 p-3 rounded-md">
                                                <Switch
                                                    id="remove_dead_streams"
                                                    checked={profile.stream_checking.remove_dead_streams ?? true}
                                                    onCheckedChange={(checked) => updateProfile('stream_checking.remove_dead_streams', checked)}
                                                />
                                                <div className="space-y-0.5">
                                                    <Label htmlFor="remove_dead_streams" className="cursor-pointer font-medium">Remove Dead Streams From Channel</Label>
                                                    <p className="text-[10px] text-muted-foreground">When disabled, dead streams are kept in channel ordering during this profile&apos;s checks.</p>
                                                </div>
                                            </div>
                                            <div className="flex items-center space-x-3 bg-muted/50 p-3 rounded-md">
                                                <Switch
                                                    id="loop_check_enabled"
                                                    checked={profile.stream_checking.loop_check_enabled ?? false}
                                                    onCheckedChange={(checked) => updateProfile('stream_checking.loop_check_enabled', checked)}
                                                />
                                                <div className="space-y-0.5">
                                                    <Label htmlFor="loop_check_enabled" className="cursor-pointer font-medium">Check scored streams for looping?</Label>
                                                    <p className="text-[10px] text-muted-foreground">Checks top 25% of streams with a score greater than 0.50</p>
                                                </div>
                                            </div>

                                            <div className="space-y-2">
                                                <Label htmlFor="s_limit">Stream Limit per Channel</Label>
                                                <Input
                                                    id="s_limit"
                                                    type="number"
                                                    min="0"
                                                    value={profile.stream_checking.stream_limit}
                                                    onChange={(e) => updateProfile('stream_checking.stream_limit', parseInt(e.target.value) || 0)}
                                                />
                                                <p className="text-[10px] text-muted-foreground">0 = Unlimited (Keep All Streams)</p>
                                            </div>

                                            <div className="space-y-2">
                                                <Label>Minimum Quality Requirements</Label>
                                                <div className="grid grid-cols-2 gap-4">
                                                    <div className="space-y-2">
                                                        <Label className="text-xs">Resolution</Label>
                                                        <Select
                                                            value={profile.stream_checking.min_resolution}
                                                            onValueChange={(val) => updateProfile('stream_checking.min_resolution', val)}
                                                        >
                                                            <SelectTrigger><SelectValue /></SelectTrigger>
                                                            <SelectContent>
                                                                <SelectItem value="any">Any</SelectItem>
                                                                <SelectItem value="720p">720p+</SelectItem>
                                                                <SelectItem value="1080p">1080p+</SelectItem>
                                                                <SelectItem value="4k">4K+</SelectItem>
                                                            </SelectContent>
                                                        </Select>
                                                    </div>
                                                    <div className="space-y-2">
                                                        <Label className="text-xs">FPS</Label>
                                                        <Input
                                                            type="number"
                                                            value={profile.stream_checking.min_fps}
                                                            onChange={(e) => updateProfile('stream_checking.min_fps', parseInt(e.target.value) || 0)}
                                                        />
                                                    </div>
                                                    <div className="space-y-2">
                                                        <Label className="text-xs">Bitrate (kbps)</Label>
                                                        <Input
                                                            type="number"
                                                            value={profile.stream_checking.min_bitrate}
                                                            onChange={(e) => updateProfile('stream_checking.min_bitrate', parseInt(e.target.value) || 0)}
                                                        />
                                                    </div>
                                                    <div className="space-y-2">
                                                        <Label className="text-xs">Minimum HDR Format</Label>
                                                        <Select
                                                            value={profile.stream_checking.require_hdr || 'any'}
                                                            onValueChange={(val) => updateProfile('stream_checking.require_hdr', val)}
                                                        >
                                                            <SelectTrigger><SelectValue /></SelectTrigger>
                                                            <SelectContent>
                                                                <SelectItem value="any">Any</SelectItem>
                                                                <SelectItem value="hdr10">HDR10</SelectItem>
                                                                <SelectItem value="hdr10+">HDR10+ Only</SelectItem>
                                                            </SelectContent>
                                                        </Select>
                                                    </div>
                                                </div>
                                            </div>
                                        </div>

                                        {/* M3U Priority Settings */}
                                        <div className="space-y-4">
                                            <Label className="font-semibold block border-b pb-2">M3U Priority Settings</Label>
                                            <div className="space-y-2">
                                                <Label className="text-xs">Priority Mode</Label>
                                                <Select
                                                    value={profile.stream_checking.m3u_priority_mode || 'absolute'}
                                                    onValueChange={(val) => updateProfile('stream_checking.m3u_priority_mode', val)}
                                                >
                                                    <SelectTrigger><SelectValue /></SelectTrigger>
                                                    <SelectContent>
                                                        <SelectItem value="absolute">Absolute (Priority &gt; Quality)</SelectItem>
                                                        <SelectItem value="same_resolution">Same Resolution (Quality &gt; Priority)</SelectItem>
                                                        <SelectItem value="equal">Equal (Fallback Only)</SelectItem>
                                                    </SelectContent>
                                                </Select>
                                            </div>
                                            <div className="space-y-2">
                                                <Label className="text-xs">Playlist Priority Rank</Label>
                                                <div className="border rounded-md divide-y overflow-hidden bg-background">
                                                    {(() => {
                                                        const allAvailableIds = m3uAccounts.map(a => a.id)
                                                        const pOrder = profile.stream_checking.m3u_priority || []
                                                        const sortedIds = [...new Set([...pOrder, ...allAvailableIds])]
                                                            .filter(id => allAvailableIds.includes(id))
                                                        return sortedIds.map((id, idx) => {
                                                            const acct = m3uAccounts.find(a => a.id === id)
                                                            return (
                                                                <div key={id} className="flex items-center justify-between p-2 text-sm group">
                                                                    <span className="truncate max-w-[150px]"><span className="text-muted-foreground mr-1">#{idx + 1}</span> {acct?.name}</span>
                                                                    <div className="flex gap-0.5">
                                                                        <Button
                                                                            variant="ghost"
                                                                            size="icon"
                                                                            className="h-6 w-6"
                                                                            disabled={idx === 0 || profile.stream_checking.m3u_priority_mode === 'equal'}
                                                                            onClick={() => {
                                                                                const newOrder = [...sortedIds]
                                                                                const temp = newOrder[idx]
                                                                                newOrder[idx] = newOrder[idx - 1]
                                                                                newOrder[idx - 1] = temp
                                                                                updateProfile('stream_checking.m3u_priority', newOrder)
                                                                            }}
                                                                        >
                                                                            <ArrowUp className="h-3 w-3" />
                                                                        </Button>
                                                                        <Button
                                                                            variant="ghost"
                                                                            size="icon"
                                                                            className="h-6 w-6"
                                                                            disabled={idx === sortedIds.length - 1 || profile.stream_checking.m3u_priority_mode === 'equal'}
                                                                            onClick={() => {
                                                                                const newOrder = [...sortedIds]
                                                                                const temp = newOrder[idx]
                                                                                newOrder[idx] = newOrder[idx + 1]
                                                                                newOrder[idx + 1] = temp
                                                                                updateProfile('stream_checking.m3u_priority', newOrder)
                                                                            }}
                                                                        >
                                                                            <ArrowDown className="h-3 w-3" />
                                                                        </Button>
                                                                    </div>
                                                                </div>
                                                            )
                                                        })
                                                    })()}
                                                </div>
                                            </div>
                                        </div>
                                    </div>

                                    {/* Scoring Weights Section */}
                                    <Separator className="my-6" />
                                    <div className="space-y-4">
                                        <div>
                                            <h4 className="font-semibold mb-1">Stream Quality Scoring</h4>
                                            <p className="text-xs text-muted-foreground">
                                                Adjust how different quality metrics are weighted when scoring streams
                                            </p>
                                        </div>
                                        <div className="grid gap-4 md:grid-cols-2">
                                            <div className="space-y-2">
                                                <Label htmlFor="weight_bitrate" className="text-xs">Bitrate Weight</Label>
                                                <Input
                                                    id="weight_bitrate"
                                                    type="number"
                                                    step="0.05"
                                                    value={profile.scoring_weights.bitrate}
                                                    onChange={(e) => updateProfile('scoring_weights.bitrate', parseFloat(e.target.value) || 0)}
                                                    min={0}
                                                    max={1}
                                                />
                                            </div>
                                            <div className="space-y-2">
                                                <Label htmlFor="weight_resolution" className="text-xs">Resolution Weight</Label>
                                                <Input
                                                    id="weight_resolution"
                                                    type="number"
                                                    step="0.05"
                                                    value={profile.scoring_weights.resolution}
                                                    onChange={(e) => updateProfile('scoring_weights.resolution', parseFloat(e.target.value) || 0)}
                                                    min={0}
                                                    max={1}
                                                />
                                            </div>
                                            <div className="space-y-2">
                                                <Label htmlFor="weight_fps" className="text-xs">FPS Weight</Label>
                                                <Input
                                                    id="weight_fps"
                                                    type="number"
                                                    step="0.05"
                                                    value={profile.scoring_weights.fps}
                                                    onChange={(e) => updateProfile('scoring_weights.fps', parseFloat(e.target.value) || 0)}
                                                    min={0}
                                                    max={1}
                                                />
                                            </div>
                                            <div className="space-y-2">
                                                <Label htmlFor="weight_codec" className="text-xs">Codec Weight</Label>
                                                <Input
                                                    id="weight_codec"
                                                    type="number"
                                                    step="0.05"
                                                    value={profile.scoring_weights.codec}
                                                    onChange={(e) => updateProfile('scoring_weights.codec', parseFloat(e.target.value) || 0)}
                                                    min={0}
                                                    max={1}
                                                />
                                            </div>
                                            <div className="space-y-2">
                                                <Label htmlFor="weight_hdr" className="text-xs">HDR Weight</Label>
                                                <Input
                                                    id="weight_hdr"
                                                    type="number"
                                                    step="0.05"
                                                    value={profile.scoring_weights.hdr || 0.10}
                                                    onChange={(e) => updateProfile('scoring_weights.hdr', parseFloat(e.target.value) || 0)}
                                                    min={0}
                                                    max={1}
                                                />
                                            </div>
                                            <div className="space-y-2">
                                                <Label htmlFor="loop_penalty" className="text-xs">Looping Punishment</Label>
                                                <Input
                                                    id="loop_penalty"
                                                    type="number"
                                                    step="0.01"
                                                    value={profile.scoring_weights.loop_penalty ?? 0}
                                                    onChange={(e) => {
                                                        const val = parseFloat(e.target.value) || 0
                                                        updateProfile('scoring_weights.loop_penalty', Math.min(0, Math.max(-0.25, val)))
                                                    }}
                                                    min={-0.25}
                                                    max={0}
                                                />
                                                <p className="text-[10px] text-muted-foreground">Score penalty for looping streams (0 = disabled, min -0.25)</p>
                                            </div>
                                        </div>
                                        <div className="flex items-center space-x-3 bg-muted/50 p-3 rounded-md">
                                            <Switch
                                                id="prefer_h265"
                                                checked={profile.scoring_weights.prefer_h265}
                                                onCheckedChange={(checked) => updateProfile('scoring_weights.prefer_h265', checked)}
                                            />
                                            <div className="space-y-0.5">
                                                <Label htmlFor="prefer_h265" className="cursor-pointer font-medium text-sm">Prefer H.265/HEVC</Label>
                                                <p className="text-[10px] text-muted-foreground">Give preference to H.265 codec over H.264</p>
                                            </div>
                                        </div>
                                    </div>
                                </div>
                            )}
                        </div>
                    )}
                </CardContent>
            </Card>

            <div className="flex justify-end gap-3 pt-4 border-t">
                <Button variant="outline" onClick={() => navigate('/settings')}>Cancel</Button>
                <Button onClick={handleSave} disabled={saving}>
                    {saving && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}
                    {profileId === 'new' ? 'Create Profile' : 'Save Changes'}
                </Button>
            </div>
        </div>
    )
}
