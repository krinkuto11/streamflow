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
    }, 10000) // Refresh every 10 seconds for live updates
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

  const handleChannelSelect = (channel) => {
    setSelectedChannel(channel)
    loadChannelMetrics(channel.id)
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
            Channels currently being monitored. Click to view metrics.
          </CardDescription>
        </CardHeader>
        <CardContent>
          {channels.length === 0 ? (
            <div className="text-center py-8 text-muted-foreground">
              No AceStream channels configured. Tag channels below to start monitoring.
            </div>
          ) : (
            <div className="space-y-2">
              {channels.map((channel) => (
                <div
                  key={channel.id}
                  className="flex items-center justify-between p-3 border rounded-lg cursor-pointer hover:bg-accent transition-colors"
                  onClick={() => handleChannelSelect(channel)}
                >
                  <div>
                    <div className="font-medium">{channel.name}</div>
                    <div className="text-sm text-muted-foreground">
                      Channel #{channel.channel_number} • {channel.streams?.length || 0} streams
                    </div>
                  </div>
                  <Badge variant={selectedChannel?.id === channel.id ? "default" : "outline"}>
                    {selectedChannel?.id === channel.id ? "Selected" : "View"}
                  </Badge>
                </div>
              ))}
            </div>
          )}
        </CardContent>
      </Card>

      {/* Metrics Chart */}
      {selectedChannel && metrics.data.length > 0 && (
        <Card>
          <CardHeader>
            <CardTitle>Stream Health Over Time</CardTitle>
            <CardDescription>
              Individual stream metrics for {selectedChannel.name} (Last 24 hours)
            </CardDescription>
          </CardHeader>
          <CardContent>
            <div className="space-y-6">
              {/* Health Score Chart */}
              <div>
                <h3 className="text-sm font-medium mb-2">Health Score</h3>
                <ResponsiveContainer width="100%" height={300}>
                  <LineChart data={metrics.data}>
                    <CartesianGrid strokeDasharray="3 3" />
                    <XAxis 
                      dataKey="timestamp" 
                      tickFormatter={(value) => new Date(value).toLocaleTimeString()}
                    />
                    <YAxis domain={[0, 100]} />
                    <Tooltip 
                      labelFormatter={(value) => new Date(value).toLocaleString()}
                    />
                    <Legend />
                    {metrics.streamIds.map((streamId, index) => (
                      <Line
                        key={`health-${streamId}`}
                        type="monotone"
                        dataKey={`stream_${streamId}_health`}
                        stroke={`hsl(var(--chart-${(index % 5) + 1}))`}
                        name={`Stream ${streamId}`}
                        strokeWidth={2}
                        connectNulls
                      />
                    ))}
                  </LineChart>
                </ResponsiveContainer>
              </div>

              {/* Peers Chart */}
              <div>
                <h3 className="text-sm font-medium mb-2">Peers</h3>
                <ResponsiveContainer width="100%" height={300}>
                  <LineChart data={metrics.data}>
                    <CartesianGrid strokeDasharray="3 3" />
                    <XAxis 
                      dataKey="timestamp" 
                      tickFormatter={(value) => new Date(value).toLocaleTimeString()}
                    />
                    <YAxis />
                    <Tooltip 
                      labelFormatter={(value) => new Date(value).toLocaleString()}
                    />
                    <Legend />
                    {metrics.streamIds.map((streamId, index) => (
                      <Line
                        key={`peers-${streamId}`}
                        type="monotone"
                        dataKey={`stream_${streamId}_peers`}
                        stroke={`hsl(var(--chart-${(index % 5) + 1}))`}
                        name={`Stream ${streamId}`}
                        strokeWidth={2}
                        connectNulls
                      />
                    ))}
                  </LineChart>
                </ResponsiveContainer>
              </div>

              {/* Download Speed Chart */}
              <div>
                <h3 className="text-sm font-medium mb-2">Download Speed (KB/s)</h3>
                <ResponsiveContainer width="100%" height={300}>
                  <LineChart data={metrics.data}>
                    <CartesianGrid strokeDasharray="3 3" />
                    <XAxis 
                      dataKey="timestamp" 
                      tickFormatter={(value) => new Date(value).toLocaleTimeString()}
                    />
                    <YAxis />
                    <Tooltip 
                      labelFormatter={(value) => new Date(value).toLocaleString()}
                    />
                    <Legend />
                    {metrics.streamIds.map((streamId, index) => (
                      <Line
                        key={`down-${streamId}`}
                        type="monotone"
                        dataKey={`stream_${streamId}_speed_down`}
                        stroke={`hsl(var(--chart-${(index % 5) + 1}))`}
                        name={`Stream ${streamId}`}
                        strokeWidth={2}
                        connectNulls
                      />
                    ))}
                  </LineChart>
                </ResponsiveContainer>
              </div>

              {/* Upload Speed Chart */}
              <div>
                <h3 className="text-sm font-medium mb-2">Upload Speed (KB/s)</h3>
                <ResponsiveContainer width="100%" height={300}>
                  <LineChart data={metrics.data}>
                    <CartesianGrid strokeDasharray="3 3" />
                    <XAxis 
                      dataKey="timestamp" 
                      tickFormatter={(value) => new Date(value).toLocaleTimeString()}
                    />
                    <YAxis />
                    <Tooltip 
                      labelFormatter={(value) => new Date(value).toLocaleString()}
                    />
                    <Legend />
                    {metrics.streamIds.map((streamId, index) => (
                      <Line
                        key={`up-${streamId}`}
                        type="monotone"
                        dataKey={`stream_${streamId}_speed_up`}
                        stroke={`hsl(var(--chart-${(index % 5) + 1}))`}
                        name={`Stream ${streamId}`}
                        strokeWidth={2}
                        connectNulls
                      />
                    ))}
                  </LineChart>
                </ResponsiveContainer>
              </div>
            </div>
          </CardContent>
        </Card>
      )}

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
