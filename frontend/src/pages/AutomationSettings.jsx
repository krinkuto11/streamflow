import { useState, useEffect } from 'react'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card.jsx'
import { Button } from '@/components/ui/button.jsx'
import { Input } from '@/components/ui/input.jsx'
import { Label } from '@/components/ui/label.jsx'
import { Switch } from '@/components/ui/switch.jsx'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs.jsx'
import { AlertCircle, CheckCircle2, Loader2 } from 'lucide-react'
import { Separator } from '@/components/ui/separator.jsx'
import { useToast } from '@/hooks/use-toast.js'
import { automationAPI, streamCheckerAPI, dispatcharrAPI, sessionSettingsAPI, schedulingAPI, aceStreamOrchestratorAPI } from '@/services/api.js'
import AutomationProfileStudio from '@/components/Automation/AutomationProfileStudio.jsx'
import AutomationPeriods from '@/components/Automation/AutomationPeriods.jsx'

export default function AutomationSettings() {
  const [config, setConfig] = useState(null)
  const [streamCheckerConfig, setStreamCheckerConfig] = useState(null)
  const [dispatcharrConfig, setDispatcharrConfig] = useState(null)
  const [sessionConfig, setSessionConfig] = useState({ review_duration: 60 })
  const [schedulingConfig, setSchedulingConfig] = useState({ epg_schedule: { type: 'interval', value: 60 }, udi_refresh_schedule: null, enabled: true })
  const [orchestratorConfig, setOrchestratorConfig] = useState({ host: '', port: 8000, has_api_key: false, api_key: '' })
  const [testingConnection, setTestingConnection] = useState(false)
  const [connectionTestResult, setConnectionTestResult] = useState(null)
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)

  const { toast } = useToast()

  useEffect(() => {
    loadConfig()
  }, [])

  const loadConfig = async () => {
    try {
      setLoading(true)
      const [automationResponse, streamCheckerResponse, dispatcharrResponse, sessionResponse, schedulingResponse, orchestratorResponse] = await Promise.all([
        automationAPI.getConfig(),
        streamCheckerAPI.getConfig(),
        dispatcharrAPI.getConfig(),
        sessionSettingsAPI.getSettings(),
        schedulingAPI.getConfig(),
        aceStreamOrchestratorAPI.getConfig(),
      ])
      setConfig(automationResponse.data)
      setStreamCheckerConfig(streamCheckerResponse.data)
      setDispatcharrConfig(dispatcharrResponse.data)
      setSessionConfig(sessionResponse.data)
      setSchedulingConfig(schedulingResponse.data)
      setOrchestratorConfig({
        host: orchestratorResponse.data?.host || '',
        port: orchestratorResponse.data?.port ?? 8000,
        has_api_key: !!orchestratorResponse.data?.has_api_key,
        api_key: '',
      })
    } catch (err) {
      console.error('Failed to load config:', err)
      toast({
        title: "Error",
        description: "Failed to load configuration",
        variant: "destructive"
      })
    } finally {
      setLoading(false)
    }
  }

  const handleSave = async () => {
    try {
      setSaving(true)
      await Promise.all([
        automationAPI.updateConfig(config),
        streamCheckerAPI.updateConfig(streamCheckerConfig),
        dispatcharrAPI.updateConfig(dispatcharrConfig),
        sessionSettingsAPI.updateSettings(sessionConfig),
        schedulingAPI.updateConfig(schedulingConfig),
        aceStreamOrchestratorAPI.updateConfig({
          host: orchestratorConfig?.host,
          port: orchestratorConfig?.port,
          api_key: orchestratorConfig?.api_key || undefined,
        })
      ])
      toast({
        title: "Success",
        description: "Configuration saved successfully",
      })
    } catch (err) {
      toast({
        title: "Error",
        description: "Failed to save configuration",
        variant: "destructive"
      })
    } finally {
      setSaving(false)
    }
  }

  const handleConfigChange = (field, value) => {
    setConfig(prev => ({
      ...prev,
      [field]: value
    }))
  }

  const handleStreamCheckerConfigChange = (field, value) => {
    if (field.includes('.')) {
      const parts = field.split('.')
      if (parts.length === 2) {
        const [parent, child] = parts
        setStreamCheckerConfig(prev => ({
          ...prev,
          [parent]: {
            ...(prev[parent] || {}),
            [child]: value
          }
        }))
      } else if (parts.length === 3) {
        const [parent, child, grandchild] = parts
        setStreamCheckerConfig(prev => ({
          ...prev,
          [parent]: {
            ...(prev[parent] || {}),
            [child]: {
              ...(prev[parent]?.[child] || {}),
              [grandchild]: value
            }
          }
        }))
      }
    } else {
      setStreamCheckerConfig(prev => ({
        ...prev,
        [field]: value
      }))
    }
  }

  const handleDispatcharrConfigChange = (field, value) => {
    setDispatcharrConfig(prev => ({
      ...prev,
      [field]: value
    }))
  }

  const handleSessionConfigChange = (field, value) => {
    setSessionConfig(prev => ({
      ...prev,
      [field]: value
    }))
  }


  const handleOrchestratorConfigChange = (field, value) => {
    setOrchestratorConfig(prev => ({
      ...prev,
      [field]: value
    }))
  }

  const handleGlobalAutomationChange = (field, value) => {
    setConfig(prev => ({
      ...prev,
      [field]: value
    }))
  }

  const handleTestConnection = async () => {
    try {
      setTestingConnection(true)
      setConnectionTestResult(null)

      const response = await dispatcharrAPI.testConnection(dispatcharrConfig)
      setConnectionTestResult({
        success: true,
        message: 'Connection successful!',
        ...response.data
      })

      toast({
        title: "Success",
        description: "Successfully connected to Dispatcharr"
      })
    } catch (err) {
      const errorMsg = err.response?.data?.error || 'Failed to connect to Dispatcharr'
      setConnectionTestResult({
        success: false,
        message: errorMsg
      })
      toast({
        title: "Connection Failed",
        description: errorMsg,
        variant: "destructive"
      })
    } finally {
      setTestingConnection(false)
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
    <div className="space-y-6">
      <div>
        <h1 className="text-3xl font-bold tracking-tight">Settings</h1>
        <p className="text-muted-foreground">
          Configure Dispatcharr connection, automation profiles, and system parameters
        </p>
      </div>

      <Tabs defaultValue="periods" className="w-full">
        <TabsList className="grid w-full grid-cols-5">
          <TabsTrigger value="periods">Periods</TabsTrigger>
          <TabsTrigger value="automation">Profiles</TabsTrigger>
          <TabsTrigger value="scheduling">Scheduling</TabsTrigger>
          <TabsTrigger value="monitoring">Monitoring</TabsTrigger>
          <TabsTrigger value="connection">Connection</TabsTrigger>
        </TabsList>

        <TabsContent value="periods" className="space-y-6">
          <AutomationPeriods />
        </TabsContent>

        <TabsContent value="automation" className="space-y-6">
          <AutomationProfileStudio />
        </TabsContent>

        <TabsContent value="scheduling" className="space-y-6">
          <Card>
            <CardHeader>
              <CardTitle>Event Scheduling</CardTitle>
              <CardDescription>
                Configure how often to refresh EPG and UDI data from Dispatcharr
              </CardDescription>
            </CardHeader>
            <CardContent>
              <div className="grid grid-cols-2 gap-8">
                {/* EPG Refresh — left column */}
                <div className="space-y-2">
                  <Label htmlFor="refresh-interval">EPG Refresh Interval (minutes)</Label>
                  <div className="flex items-center gap-2">
                    <Input
                      id="refresh-interval"
                      type="number"
                      min="1"
                      max="1440"
                      className="max-w-[120px]"
                      value={schedulingConfig?.epg_schedule?.value || 60}
                      onChange={(e) => setSchedulingConfig(prev => ({
                        ...prev,
                        epg_schedule: { type: 'interval', value: parseInt(e.target.value) || 60 }
                      }))}
                    />
                    <span className="text-sm text-muted-foreground">minutes</span>
                  </div>
                </div>

                {/* UDI Cache Refresh — right column */}
                <div className="space-y-2">
                  <Label htmlFor="udi-refresh-interval">UDI Refresh Interval (minutes)</Label>
                  <div className="flex items-center gap-2">
                    <Input
                      id="udi-refresh-interval"
                      type="number"
                      min="0"
                      max="720"
                      step="15"
                      className="max-w-[120px]"
                      value={schedulingConfig?.udi_refresh_schedule?.value || 0}
                      onChange={(e) => {
                        const raw = parseInt(e.target.value) || 0
                        const snapped = Math.floor(Math.min(Math.max(raw, 0), 720) / 15) * 15
                        setSchedulingConfig(prev => ({
                          ...prev,
                          udi_refresh_schedule: snapped === 0 ? null : { type: 'interval', value: snapped }
                        }))
                      }}
                    />
                    <span className="text-sm text-muted-foreground">minutes</span>
                  </div>
                  <p className="text-xs text-muted-foreground">
                    A zero interval disables automatic UDI cache refresh
                  </p>
                </div>
              </div>
              <div className="flex justify-end pt-4">
                <Button onClick={handleSave} disabled={saving}>
                  {saving && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}
                  Save Settings
                </Button>
              </div>
            </CardContent>
          </Card>
        </TabsContent>

        <TabsContent value="monitoring" className="space-y-6">
          <Card>
            <CardHeader>
              <CardTitle>Monitoring Settings</CardTitle>
              <CardDescription>
                Configure thresholds and timings for stream monitoring
              </CardDescription>
            </CardHeader>
            <CardContent className="space-y-4">
              <div className="space-y-2">
                <Label htmlFor="review_duration">Review Duration (seconds)</Label>
                <Input
                  id="review_duration"
                  type="number"
                  min="1"
                  value={sessionConfig?.review_duration || 60}
                  onChange={(e) => handleSessionConfigChange('review_duration', parseInt(e.target.value))}
                />
                <p className="text-sm text-muted-foreground">
                  Time a stream must remain in "Review" state with good score before becoming "Stable". Default: 60s.
                </p>
              </div>
              <div className="flex justify-end pt-4">
                <Button onClick={handleSave} disabled={saving}>
                  {saving && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}
                  Save Settings
                </Button>
              </div>
            </CardContent>
          </Card>
        </TabsContent>

        <TabsContent value="connection" className="space-y-6">
          <Card>
            <CardHeader>
              <CardTitle>Dispatcharr Connection</CardTitle>
              <CardDescription>
                Configure connection settings to the Dispatcharr API
              </CardDescription>
            </CardHeader>
            <CardContent className="space-y-4">
              <div className="space-y-2">
                <Label htmlFor="base_url">Base URL</Label>
                <Input
                  id="base_url"
                  type="url"
                  value={dispatcharrConfig?.base_url || ''}
                  onChange={(e) => handleDispatcharrConfigChange('base_url', e.target.value)}
                  placeholder="http://localhost:9191"
                />
              </div>

              <div className="space-y-2">
                <Label htmlFor="username">Username</Label>
                <Input
                  id="username"
                  type="text"
                  value={dispatcharrConfig?.username || ''}
                  onChange={(e) => handleDispatcharrConfigChange('username', e.target.value)}
                  placeholder="admin"
                />
              </div>

              <div className="space-y-2">
                <Label htmlFor="password">Password</Label>
                <Input
                  id="password"
                  type="password"
                  value={dispatcharrConfig?.password || ''}
                  onChange={(e) => handleDispatcharrConfigChange('password', e.target.value)}
                  placeholder={dispatcharrConfig?.has_password ? '••••••••' : 'Enter password'}
                />
              </div>

              <div className="flex items-center gap-2 pt-2">
                <Button
                  onClick={handleTestConnection}
                  disabled={testingConnection}
                  variant="outline"
                >
                  {testingConnection && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}
                  Test Connection
                </Button>
                {connectionTestResult && (
                  <div className="flex items-center gap-2">
                    {connectionTestResult.success ? (
                      <>
                        <CheckCircle2 className="h-4 w-4 text-green-600" />
                        <span className="text-sm text-green-600">{connectionTestResult.message}</span>
                      </>
                    ) : (
                      <>
                        <AlertCircle className="h-4 w-4 text-destructive" />
                        <span className="text-sm text-destructive">{connectionTestResult.message}</span>
                      </>
                    )}
                  </div>
                )}
              </div>
              <div className="flex justify-end pt-4">
                <Button onClick={handleSave} disabled={saving}>
                  {saving && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}
                  Save Settings
                </Button>
              </div>
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle>AceStream Orchestrator</CardTitle>
              <CardDescription>
                Configure AceStream monitoring orchestrator host, port, and API key
              </CardDescription>
            </CardHeader>
            <CardContent className="space-y-4">
              <div className="space-y-2">
                <Label htmlFor="orchestrator-host">Host</Label>
                <Input
                  id="orchestrator-host"
                  type="text"
                  value={orchestratorConfig?.host || ''}
                  onChange={(e) => handleOrchestratorConfigChange('host', e.target.value)}
                  placeholder="orchestrator.local or http://orchestrator.local"
                />
              </div>

              <div className="space-y-2">
                <Label htmlFor="orchestrator-port">Port</Label>
                <Input
                  id="orchestrator-port"
                  type="number"
                  min="1"
                  max="65535"
                  value={orchestratorConfig?.port ?? ''}
                  onChange={(e) => handleOrchestratorConfigChange('port', parseInt(e.target.value || '0', 10) || '')}
                  placeholder="8000"
                />
              </div>

              <div className="space-y-2">
                <Label htmlFor="orchestrator-api-key">API Key</Label>
                <Input
                  id="orchestrator-api-key"
                  type="password"
                  value={orchestratorConfig?.api_key || ''}
                  onChange={(e) => handleOrchestratorConfigChange('api_key', e.target.value)}
                  placeholder={orchestratorConfig?.has_api_key ? '••••••••' : 'Enter API key'}
                />
                <p className="text-sm text-muted-foreground">
                  Leave blank to keep the existing key.
                </p>
              </div>

              <div className="flex justify-end pt-4">
                <Button onClick={handleSave} disabled={saving}>
                  {saving && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}
                  Save Settings
                </Button>
              </div>
            </CardContent>
          </Card>
        </TabsContent>
      </Tabs>
    </div>
  )
}
