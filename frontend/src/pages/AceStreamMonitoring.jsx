import { useState, useEffect } from 'react'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card.jsx'
import { Button } from '@/components/ui/button.jsx'
import { Badge } from '@/components/ui/badge.jsx'
import { Input } from '@/components/ui/input.jsx'
import { Label } from '@/components/ui/label.jsx'
import { Switch } from '@/components/ui/switch.jsx'
import { Accordion, AccordionContent, AccordionItem, AccordionTrigger } from '@/components/ui/accordion.jsx'
import { useToast } from '@/hooks/use-toast.js'
import { api } from '@/services/api.js'
import {
  Activity,
  Settings,
  PlayCircle,
  StopCircle,
  RefreshCw,
  Loader2,
  Radio,
  CheckCircle,
  XCircle
} from 'lucide-react'

export default function AceStreamMonitoring() {
  const [channels, setChannels] = useState([])
  const [allChannels, setAllChannels] = useState([])
  const [channelStreams, setChannelStreams] = useState({}) // {channelId: [streams]}
  const [streamHealth, setStreamHealth] = useState({}) // {streamId: health_data}
  const [config, setConfig] = useState({
    enabled: false,
    orchestrator_url: 'http://gluetun:19000',
    monitoring_interval: 30,
    dead_stream_retry_interval: 300,
    max_ffmpeg_failures: 3,
    livepos_buffer_tolerance: 30,
    speed_down_timeout: 10
  })
  const [status, setStatus] = useState(null)
  const [loading, setLoading] = useState(true)
  const [configLoading, setConfigLoading] = useState(false)
  const { toast } = useToast()

  useEffect(() => {
    loadData()
    const interval = setInterval(() => {
      loadStatus()
      loadChannelStreamsHealth()
    }, 30000) // Refresh every 30 seconds
    return () => clearInterval(interval)
  }, [channels])

  const loadData = async () => {
    try {
      setLoading(true)
      await Promise.all([
        loadConfig(),
        loadAceStreamChannels(),
        loadAllChannels(),
        loadStatus()
      ])
      await loadChannelStreamsHealth()
    } catch (error) {
      console.error('Error loading AceStream data:', error)
      toast({
        title: "Error",
        description: "Failed to load AceStream data",
        variant: "destructive"
      })
    } finally {
      setLoading(false)
    }
  }

  const loadConfig = async () => {
    try {
      const response = await api.get('/acestream/config')
      setConfig(response.data)
    } catch (error) {
      console.error('Error loading config:', error)
    }
  }

  const loadAceStreamChannels = async () => {
    try {
      const response = await api.get('/acestream/channels')
      setChannels(response.data)
    } catch (error) {
      console.error('Error loading AceStream channels:', error)
    }
  }

  const loadAllChannels = async () => {
    try {
      const response = await api.get('/channels')
      setAllChannels(response.data)
    } catch (error) {
      console.error('Error loading all channels:', error)
    }
  }

  const loadStatus = async () => {
    try {
      const response = await api.get('/acestream/monitoring/status')
      setStatus(response.data)
    } catch (error) {
      console.error('Error loading status:', error)
    }
  }

  const loadChannelStreamsHealth = async () => {
    try {
      // Load streams and their health for each channel
      const streamsData = {}
      const healthData = {}
      
      for (const channel of channels) {
        if (channel.streams && channel.streams.length > 0) {
          // Get stream details
          const streamPromises = channel.streams.map(streamId =>
            api.get(`/streams/${streamId}`).catch(() => null)
          )
          const streams = (await Promise.all(streamPromises)).filter(s => s !== null).map(s => s.data)
          streamsData[channel.id] = streams
          
          // Get health for each stream
          const healthPromises = channel.streams.map(streamId =>
            api.get(`/acestream/monitoring/stream/${streamId}/health`).catch(() => null)
          )
          const healthResults = await Promise.all(healthPromises)
          healthResults.forEach((result, idx) => {
            if (result && result.data) {
              healthData[channel.streams[idx]] = result.data
            }
          })
        } else {
          streamsData[channel.id] = []
        }
      }
      
      setChannelStreams(streamsData)
      setStreamHealth(healthData)
    } catch (error) {
      console.error('Error loading channel streams health:', error)
    }
  }

  const saveConfig = async () => {
    try {
      setConfigLoading(true)
      await api.post('/acestream/config', config)
      toast({
        title: "Success",
        description: "Configuration saved successfully"
      })
      await loadStatus()
    } catch (error) {
      console.error('Error saving config:', error)
      toast({
        title: "Error",
        description: "Failed to save configuration",
        variant: "destructive"
      })
    } finally {
      setConfigLoading(false)
    }
  }

  const startMonitoring = async () => {
    try {
      await api.post('/acestream/monitoring/start')
      toast({
        title: "Success",
        description: "AceStream monitoring started"
      })
      await loadStatus()
    } catch (error) {
      console.error('Error starting monitoring:', error)
      toast({
        title: "Error",
        description: "Failed to start monitoring",
        variant: "destructive"
      })
    }
  }

  const stopMonitoring = async () => {
    try {
      await api.post('/acestream/monitoring/stop')
      toast({
        title: "Success",
        description: "AceStream monitoring stopped"
      })
      await loadStatus()
    } catch (error) {
      console.error('Error stopping monitoring:', error)
      toast({
        title: "Error",
        description: "Failed to stop monitoring",
        variant: "destructive"
      })
    }
  }

  const toggleChannelAceStream = async (channelId, isAceStream) => {
    try {
      await api.post(`/acestream/channels/${channelId}/tag`, {
        is_acestream: isAceStream,
        orchestrator_url: config.orchestrator_url
      })
      toast({
        title: "Success",
        description: `Channel ${isAceStream ? 'marked as' : 'unmarked from'} AceStream`
      })
      await loadAceStreamChannels()
    } catch (error) {
      console.error('Error toggling channel:', error)
      toast({
        title: "Error",
        description: "Failed to update channel",
        variant: "destructive"
      })
    }
  }

  if (loading) {
    return (
      <div className="flex items-center justify-center h-96">
        <Loader2 className="h-8 w-8 animate-spin" />
      </div>
    )
  }

  return (
    <div className="space-y-6">
      {/* Header */}
      <div>
        <h1 className="text-3xl font-bold">AceStream Monitoring</h1>
        <p className="text-muted-foreground">
          Monitor and manage AceStream channels with real-time health tracking
        </p>
      </div>

      {/* Status Card */}
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <Activity className="h-5 w-5" />
            Monitoring Status
          </CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="flex items-center justify-between">
            <div className="space-y-1">
              <div className="flex items-center gap-2">
                <Badge variant={status?.running ? "success" : "secondary"}>
                  {status?.running ? "Running" : "Stopped"}
                </Badge>
                <span className="text-sm text-muted-foreground">
                  {status?.active_channels || 0} channels monitored
                </span>
              </div>
            </div>
            <div className="flex gap-2">
              {status?.running ? (
                <Button onClick={stopMonitoring} variant="destructive" size="sm">
                  <StopCircle className="h-4 w-4 mr-2" />
                  Stop Monitoring
                </Button>
              ) : (
                <Button onClick={startMonitoring} size="sm">
                  <PlayCircle className="h-4 w-4 mr-2" />
                  Start Monitoring
                </Button>
              )}
              <Button onClick={() => loadData()} variant="outline" size="sm">
                <RefreshCw className="h-4 w-4" />
              </Button>
            </div>
          </div>
        </CardContent>
      </Card>

      {/* Configuration Section */}
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <Settings className="h-5 w-5" />
            Configuration
          </CardTitle>
          <CardDescription>
            Configure AceStream Orchestrator connection and monitoring settings
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="grid gap-4">
            <div className="flex items-center justify-between">
              <div className="space-y-0.5">
                <Label htmlFor="enabled">Enable AceStream Monitoring</Label>
                <p className="text-sm text-muted-foreground">
                  Automatically monitor and reorder AceStream streams by health
                </p>
              </div>
              <Switch
                id="enabled"
                checked={config.enabled}
                onCheckedChange={(checked) => setConfig({ ...config, enabled: checked })}
              />
            </div>

            <div className="grid gap-2">
              <Label htmlFor="orchestrator-url">Orchestrator URL</Label>
              <Input
                id="orchestrator-url"
                placeholder="http://gluetun:19000"
                value={config.orchestrator_url}
                onChange={(e) => setConfig({ ...config, orchestrator_url: e.target.value })}
              />
              <p className="text-sm text-muted-foreground">
                URL of the AceStream Orchestrator API
              </p>
            </div>

            <div className="grid grid-cols-2 gap-4">
              <div className="grid gap-2">
                <Label htmlFor="monitoring-interval">Monitoring Interval (seconds)</Label>
                <Input
                  id="monitoring-interval"
                  type="number"
                  min="10"
                  max="300"
                  value={config.monitoring_interval}
                  onChange={(e) => {
                    const value = e.target.value === '' ? '' : parseInt(e.target.value, 10)
                    if (value === '' || (!isNaN(value) && value >= 10 && value <= 300)) {
                      setConfig({ ...config, monitoring_interval: value === '' ? 30 : value })
                    }
                  }}
                  onBlur={(e) => {
                    // Restore default if empty on blur
                    if (e.target.value === '') {
                      setConfig({ ...config, monitoring_interval: 30 })
                    }
                  }}
                />
                <p className="text-sm text-muted-foreground">
                  How often to check stream health
                </p>
              </div>

              <div className="grid gap-2">
                <Label htmlFor="dead-stream-retry">Dead Stream Retry Interval (seconds)</Label>
                <Input
                  id="dead-stream-retry"
                  type="number"
                  min="60"
                  max="3600"
                  value={config.dead_stream_retry_interval}
                  onChange={(e) => {
                    const value = e.target.value === '' ? '' : parseInt(e.target.value, 10)
                    if (value === '' || (!isNaN(value) && value >= 60 && value <= 3600)) {
                      setConfig({ ...config, dead_stream_retry_interval: value === '' ? 300 : value })
                    }
                  }}
                  onBlur={(e) => {
                    // Restore default if empty on blur
                    if (e.target.value === '') {
                      setConfig({ ...config, dead_stream_retry_interval: 300 })
                    }
                  }}
                />
                <p className="text-sm text-muted-foreground">
                  Time to wait before retrying dead streams
                </p>
              </div>
            </div>

            <div className="grid gap-2">
              <Label htmlFor="max-failures">Max FFmpeg Failures</Label>
              <Input
                id="max-failures"
                type="number"
                min="1"
                max="10"
                value={config.max_ffmpeg_failures}
                onChange={(e) => {
                  const value = e.target.value === '' ? '' : parseInt(e.target.value, 10)
                  if (value === '' || (!isNaN(value) && value >= 1 && value <= 10)) {
                    setConfig({ ...config, max_ffmpeg_failures: value === '' ? 3 : value })
                  }
                }}
                onBlur={(e) => {
                  // Restore default if empty on blur
                  if (e.target.value === '') {
                    setConfig({ ...config, max_ffmpeg_failures: 3 })
                  }
                }}
              />
              <p className="text-sm text-muted-foreground">
                Number of FFmpeg failures before marking stream as dead
              </p>
            </div>

            <div className="grid grid-cols-2 gap-4">
              <div className="grid gap-2">
                <Label htmlFor="livepos-tolerance">Livepos Buffer Tolerance (seconds)</Label>
                <Input
                  id="livepos-tolerance"
                  type="number"
                  min="5"
                  max="120"
                  value={config.livepos_buffer_tolerance}
                  onChange={(e) => {
                    const value = e.target.value === '' ? '' : parseInt(e.target.value, 10)
                    if (value === '' || (!isNaN(value) && value >= 5 && value <= 120)) {
                      setConfig({ ...config, livepos_buffer_tolerance: value === '' ? 30 : value })
                    }
                  }}
                  onBlur={(e) => {
                    if (e.target.value === '') {
                      setConfig({ ...config, livepos_buffer_tolerance: 30 })
                    }
                  }}
                />
                <p className="text-sm text-muted-foreground">
                  Mark stream dead if livepos doesn't advance for this long
                </p>
              </div>

              <div className="grid gap-2">
                <Label htmlFor="speed-timeout">Zero Speed Timeout (seconds)</Label>
                <Input
                  id="speed-timeout"
                  type="number"
                  min="5"
                  max="60"
                  value={config.speed_down_timeout}
                  onChange={(e) => {
                    const value = e.target.value === '' ? '' : parseInt(e.target.value, 10)
                    if (value === '' || (!isNaN(value) && value >= 5 && value <= 60)) {
                      setConfig({ ...config, speed_down_timeout: value === '' ? 10 : value })
                    }
                  }}
                  onBlur={(e) => {
                    if (e.target.value === '') {
                      setConfig({ ...config, speed_down_timeout: 10 })
                    }
                  }}
                />
                <p className="text-sm text-muted-foreground">
                  Mark stream dead if download speed is 0 for this long
                </p>
              </div>
            </div>
          </div>

          <Button onClick={saveConfig} disabled={configLoading}>
            {configLoading && <Loader2 className="h-4 w-4 mr-2 animate-spin" />}
            Save Configuration
          </Button>
        </CardContent>
      </Card>

      {/* AceStream Channels List */}
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <Radio className="h-5 w-5" />
            AceStream Channels
          </CardTitle>
          <CardDescription>
            Channels with alive/dead stream counts. Click chevron to expand and view live stats.
          </CardDescription>
        </CardHeader>
        <CardContent>
          {channels.length === 0 ? (
            <div className="text-center py-8 text-muted-foreground">
              No AceStream channels configured. Tag channels below to start monitoring.
            </div>
          ) : (
            <Accordion type="single" collapsible className="w-full">
              {channels.map((channel) => {
                const streams = channelStreams[channel.id] || []
                const aliveCount = streams.filter(s => {
                  const health = streamHealth[s.id]
                  return health && health.is_alive
                }).length
                const deadCount = streams.length - aliveCount
                
                return (
                  <AccordionItem key={channel.id} value={`channel-${channel.id}`}>
                    <AccordionTrigger className="hover:no-underline">
                      <div className="flex items-center justify-between w-full pr-4">
                        <div className="text-left">
                          <div className="font-medium">{channel.name}</div>
                          <div className="text-sm text-muted-foreground">
                            Channel #{channel.channel_number}
                          </div>
                        </div>
                        <div className="flex gap-2">
                          <Badge variant="default" className="bg-green-500">
                            <CheckCircle className="h-3 w-3 mr-1" />
                            {aliveCount} Alive
                          </Badge>
                          <Badge variant="destructive">
                            <XCircle className="h-3 w-3 mr-1" />
                            {deadCount} Dead
                          </Badge>
                        </div>
                      </div>
                    </AccordionTrigger>
                    <AccordionContent>
                      <div className="space-y-3 pt-2">
                        {streams.length === 0 ? (
                          <div className="text-sm text-muted-foreground text-center py-4">
                            No streams in this channel
                          </div>
                        ) : (
                          streams.map((stream) => {
                            const health = streamHealth[stream.id]
                            const isAlive = health && health.is_alive
                            
                            return (
                              <div
                                key={stream.id}
                                className="flex items-center justify-between p-3 border rounded-lg"
                              >
                                <div className="flex-1">
                                  <div className="flex items-center gap-2">
                                    <div className="font-medium">{stream.name || `Stream ${stream.id}`}</div>
                                    {isAlive ? (
                                      <Badge variant="default" className="bg-green-500">
                                        <CheckCircle className="h-3 w-3 mr-1" />
                                        Alive
                                      </Badge>
                                    ) : (
                                      <Badge variant="destructive">
                                        <XCircle className="h-3 w-3 mr-1" />
                                        Dead
                                      </Badge>
                                    )}
                                  </div>
                                  {health && (
                                    <div className="grid grid-cols-2 md:grid-cols-4 gap-2 mt-2 text-sm text-muted-foreground">
                                      <div>
                                        <span className="font-medium">Health:</span> {health.health_score?.toFixed(1) || 'N/A'}
                                      </div>
                                      <div>
                                        <span className="font-medium">Peers:</span> {health.peers || 0}
                                      </div>
                                      <div>
                                        <span className="font-medium">Down:</span> {health.speed_down || 0} KB/s
                                      </div>
                                      <div>
                                        <span className="font-medium">Up:</span> {health.speed_up || 0} KB/s
                                      </div>
                                    </div>
                                  )}
                                </div>
                              </div>
                            )
                          })
                        )}
                      </div>
                    </AccordionContent>
                  </AccordionItem>
                )
              })}
            </Accordion>
          )}
        </CardContent>
      </Card>

      {/* All Channels - Tag Management */}
      <Card>
        <CardHeader>
          <CardTitle>Channel Tagging</CardTitle>
          <CardDescription>
            Mark channels as AceStream channels to enable monitoring
          </CardDescription>
        </CardHeader>
        <CardContent>
          <div className="space-y-2 max-h-96 overflow-y-auto">
            {allChannels.map((channel) => (
              <div
                key={channel.id}
                className="flex items-center justify-between p-3 border rounded-lg"
              >
                <div>
                  <div className="font-medium">{channel.name}</div>
                  <div className="text-sm text-muted-foreground">
                    Channel #{channel.channel_number}
                  </div>
                </div>
                <Switch
                  checked={channel.is_acestream || false}
                  onCheckedChange={(checked) => toggleChannelAceStream(channel.id, checked)}
                />
              </div>
            ))}
          </div>
        </CardContent>
      </Card>
    </div>
  )
}
