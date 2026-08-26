import { useEffect, useState } from 'react'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card.jsx'
import { Button } from '@/components/ui/button.jsx'
import { Input } from '@/components/ui/input.jsx'
import { Label } from '@/components/ui/label.jsx'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert.jsx'
import { Progress } from '@/components/ui/progress.jsx'
import { useToast } from '@/hooks/use-toast.js'
import {
  setupAPI,
  dispatcharrAPI
} from '@/services/api.js'
import {
  CheckCircle2,
  AlertCircle,
  Loader2,
  KeyRound,
  UserRound,
  Zap
} from 'lucide-react'

const STEPS = [
  { id: 0, label: 'Connection', description: 'Connect to Dispatcharr' },
  { id: 1, label: 'Syncing', description: 'Fetching data from Dispatcharr' },
  { id: 2, label: 'Complete', description: 'Setup Complete' }
]

export default function SetupWizard({ onComplete }) {
  const [activeStep, setActiveStep] = useState(0)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState(null)
  const [syncProgress, setSyncProgress] = useState({ percentage: 0, message: '', status: 'idle' })
  const { toast } = useToast()

  const [dispatcharrConfig, setDispatcharrConfig] = useState({
    base_url: '',
    auth_mode: 'credentials',
    username: '',
    password: '',
    api_key: ''
  })
  const [connectionStatus, setConnectionStatus] = useState(null)

  useEffect(() => {
    checkInitialStatus()
  }, [])

  useEffect(() => {
    let pollInterval
    if (activeStep === 1) {
      pollInterval = setInterval(async () => {
        try {
          const res = await dispatcharrAPI.getInitializationStatus()
          setSyncProgress(res.data)
          if (res.data.status === 'completed') {
            clearInterval(pollInterval)
            setActiveStep(2)
          } else if (res.data.status === 'failed') {
            clearInterval(pollInterval)
            setError(res.data.message || 'Synchronization failed')
            setActiveStep(0)
          }
        } catch (err) {
          console.error('Polling error:', err)
        }
      }, 1000)
    }
    return () => clearInterval(pollInterval)
  }, [activeStep])

  const checkInitialStatus = async () => {
    try {
      setLoading(true)
      await setupAPI.getStatus()

      const daConfigReq = await dispatcharrAPI.getConfig()
      if (daConfigReq.data) {
        setDispatcharrConfig({
          base_url: daConfigReq.data.base_url || '',
          auth_mode: daConfigReq.data.auth_mode || 'credentials',
          username: daConfigReq.data.username || '',
          password: '',
          api_key: ''
        })
      }
    } catch (err) {
      console.error('Failed to check status:', err)
    } finally {
      setLoading(false)
    }
  }

  const canConnect = () => {
    if (!dispatcharrConfig.base_url) return false
    if (dispatcharrConfig.auth_mode === 'api_key') {
      return Boolean(dispatcharrConfig.api_key)
    }
    return Boolean(dispatcharrConfig.username && dispatcharrConfig.password)
  }

  const handleStartSync = async () => {
    try {
      setLoading(true)
      setError(null)

      await dispatcharrAPI.updateConfig(dispatcharrConfig)
      await dispatcharrAPI.testConnection(dispatcharrConfig)
      await dispatcharrAPI.initializeUDI()

      setConnectionStatus('success')
      setActiveStep(1)
    } catch (err) {
      setConnectionStatus('error')
      const message = err.response?.data?.error || err.message || 'Failed to connect to Dispatcharr'
      setError(message)
      toast({
        title: 'Connection Failed',
        description: message || 'Check your URL and credentials',
        variant: 'destructive'
      })
    } finally {
      setLoading(false)
    }
  }

  const handleComplete = async () => {
    try {
      setLoading(true)
      await setupAPI.ensureConfig()

      if (onComplete) {
        onComplete()
      } else {
        window.location.href = '/'
      }
    } catch (err) {
      setError('Failed to complete setup')
    } finally {
      setLoading(false)
    }
  }

  const renderCredentialsFields = () => {
    if (dispatcharrConfig.auth_mode === 'api_key') {
      return (
        <div className="grid gap-2">
          <Label htmlFor="api_key">API Key</Label>
          <Input
            id="api_key"
            type="password"
            placeholder="Enter API key"
            value={dispatcharrConfig.api_key}
            onChange={(e) => setDispatcharrConfig({ ...dispatcharrConfig, api_key: e.target.value })}
          />
        </div>
      )
    }

    return (
      <>
        <div className="grid gap-2">
          <Label htmlFor="username">Username</Label>
          <Input
            id="username"
            placeholder="admin"
            value={dispatcharrConfig.username}
            onChange={(e) => setDispatcharrConfig({ ...dispatcharrConfig, username: e.target.value })}
          />
        </div>
        <div className="grid gap-2">
          <Label htmlFor="password">Password</Label>
          <Input
            id="password"
            type="password"
            placeholder="Enter password"
            value={dispatcharrConfig.password}
            onChange={(e) => setDispatcharrConfig({ ...dispatcharrConfig, password: e.target.value })}
          />
        </div>
      </>
    )
  }

  const renderStepContent = () => {
    switch (activeStep) {
      case 0:
        return (
          <div className="space-y-4">
            <div className="grid gap-2">
              <Label htmlFor="base_url">Dispatcharr URL</Label>
              <Input
                id="base_url"
                placeholder="http://localhost:8000"
                value={dispatcharrConfig.base_url}
                onChange={(e) => setDispatcharrConfig({ ...dispatcharrConfig, base_url: e.target.value })}
              />
            </div>

            <div className="grid gap-2">
              <Label>Authentication</Label>
              <div className="grid grid-cols-2 rounded-md border bg-muted/30 p-1">
                <Button
                  type="button"
                  variant={dispatcharrConfig.auth_mode === 'credentials' ? 'default' : 'ghost'}
                  className="h-9"
                  onClick={() => setDispatcharrConfig({ ...dispatcharrConfig, auth_mode: 'credentials' })}
                >
                  <UserRound className="mr-2 h-4 w-4" />
                  User / Pass
                </Button>
                <Button
                  type="button"
                  variant={dispatcharrConfig.auth_mode === 'api_key' ? 'default' : 'ghost'}
                  className="h-9"
                  onClick={() => setDispatcharrConfig({ ...dispatcharrConfig, auth_mode: 'api_key' })}
                >
                  <KeyRound className="mr-2 h-4 w-4" />
                  API Key
                </Button>
              </div>
            </div>

            {renderCredentialsFields()}
          </div>
        )

      case 1:
        return (
          <div className="space-y-6 pt-4">
            <div className="text-center space-y-2">
              <h3 className="text-lg font-medium">Synchronizing Data</h3>
              <p className="text-sm text-muted-foreground">{syncProgress.message || 'Initializing connection...'}</p>
            </div>
            <div className="space-y-2">
              <Progress value={syncProgress.percentage} className="h-3" />
              <div className="flex justify-between text-xs text-muted-foreground font-mono">
                <span>{syncProgress.current_step || 'UDI'}</span>
                <span>{syncProgress.percentage}%</span>
              </div>
            </div>
            <div className="flex justify-center">
              <Loader2 className="h-8 w-8 animate-spin text-primary" />
            </div>
          </div>
        )

      case 2:
        return (
          <div className="space-y-6 text-center py-4">
            <div className="flex justify-center mb-4">
              <CheckCircle2 className="h-16 w-16 text-green-500" />
            </div>
            <h3 className="text-xl font-semibold">Setup Complete!</h3>
            <p className="text-muted-foreground">
              StreamFlow has successfully synchronized with Dispatcharr.
            </p>
            <div className="flex justify-center pt-4">
              <Button onClick={handleComplete} size="lg" className="w-full sm:w-auto font-semibold">
                Start Using StreamFlow
              </Button>
            </div>
          </div>
        )
      default: return null
    }
  }

  return (
    <div className="min-h-screen bg-background flex items-center justify-center p-4">
      <Card className="w-full max-w-2xl border-2 shadow-xl ring-1 ring-black/5">
        <CardHeader className="text-center">
          <CardTitle className="text-2xl font-bold">StreamFlow Setup</CardTitle>
          <CardDescription>Connect your instance to Dispatcharr to begin monitoring.</CardDescription>
        </CardHeader>
        <CardContent>
          <div className="mb-8">
            <div className="flex justify-between mb-2">
              {STEPS.map(s => (
                <div key={s.id} className={`text-xs uppercase tracking-wider font-bold ${activeStep >= s.id ? 'text-primary' : 'text-muted-foreground'}`}>{s.label}</div>
              ))}
            </div>
            <Progress value={(activeStep / (STEPS.length - 1)) * 100} className="h-1.5" />
          </div>

          <div className="min-h-[200px]">
            {renderStepContent()}
          </div>

          {error && (
            <Alert variant="destructive" className="mt-6 border-2">
              <AlertCircle className="h-4 w-4" />
              <AlertTitle>Setup Error</AlertTitle>
              <AlertDescription>{error}</AlertDescription>
            </Alert>
          )}

          <div className="flex justify-end mt-8 pt-4 border-t">
            {activeStep === 0 && (
              <Button
                onClick={handleStartSync}
                disabled={loading || !canConnect()}
                className="w-full sm:w-auto font-bold px-8"
                size="lg"
              >
                {loading ? <Loader2 className="h-4 w-4 animate-spin mr-2" /> : <Zap className="mr-2 h-4 w-4 fill-current" />}
                Connect & Setup
              </Button>
            )}
          </div>
        </CardContent>
      </Card>
    </div>
  )
}
