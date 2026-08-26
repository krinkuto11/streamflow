import { lazy, Suspense, useState, useEffect } from 'react'
import { Navigate, Routes, Route, useLocation, useNavigate } from 'react-router-dom'
import { cn } from '@/lib/utils.js'
import { Sidebar } from '@/components/layout/Sidebar.jsx'
import { Toaster } from '@/components/ui/toaster.jsx'
import { useToast } from '@/hooks/use-toast.js'
import { api } from '@/services/api.js'
import { AppErrorBoundary } from '@/components/AppErrorBoundary.jsx'
import StreamFlowInitializingScreen from '@/components/Dashboard/StreamFlowInitializingScreen.jsx'
import {
  getInitializationStateFromStatus,
  getInitializationStateFromStatusError,
  isStartupGateActive,
  shouldRedirectForStartupGate,
} from '@/lib/startup-gate-state.js'
import { createSequentialPoller } from '@/lib/sequential-poller.js'

const Dashboard = lazy(() => import('@/pages/Dashboard'))
const StreamChecker = lazy(() => import('@/pages/StreamChecker'))
const StreamMonitoring = lazy(() => import('@/pages/StreamMonitoring'))
const ShadowBlankMonitor = lazy(() => import('@/pages/ShadowBlankMonitor'))
const TeamarrPreflight = lazy(() => import('@/pages/TeamarrPreflight'))
const ChannelConfiguration = lazy(() => import('@/pages/ChannelConfiguration'))
const AutomationSettings = lazy(() => import('@/pages/AutomationSettings'))
const Changelog = lazy(() => import('@/pages/Changelog'))
const SetupWizard = lazy(() => import('@/pages/SetupWizard'))
const AutomationProfileEditor = lazy(() => import('@/pages/AutomationProfileEditor'))
const Scheduling = lazy(() => import('@/pages/Scheduling'))
const StatsDashboard = lazy(() => import('@/pages/StatsDashboard'))
const OperatorHelp = lazy(() => import('@/pages/OperatorHelp'))

function PageLoading() {
  return (
    <div className="flex min-h-[50vh] items-center justify-center" role="status" aria-label="Loading page">
      <div className="h-10 w-10 animate-spin rounded-full border-b-2 border-primary" />
    </div>
  )
}

function App() {
  const [setupStatus, setSetupStatus] = useState(null)
  const [loading, setLoading] = useState(true)
  const [isCollapsed, setIsCollapsed] = useState(false)
  const [udiInitialization, setUdiInitialization] = useState(null)
  const [udiInitializationChecked, setUdiInitializationChecked] = useState(false)
  const { toast } = useToast()
  const navigate = useNavigate()
  const location = useLocation()

  useEffect(() => {
    checkSetupStatus()
  }, [])

  const checkSetupStatus = async () => {
    try {
      setLoading(true)
      const response = await api.get('/setup-wizard')
      setSetupStatus(response.data)
    } catch (err) {
      console.error('Failed to check setup status:', err)
      toast({
        title: "Connection Error",
        description: "Failed to connect to the backend server",
        variant: "destructive"
      })
    } finally {
      setLoading(false)
    }
  }

  const handleSetupComplete = () => {
    checkSetupStatus()
    navigate('/')
  }

  const setupComplete = setupStatus?.setup_complete || false
  const startupGateActive = isStartupGateActive({
    setupComplete,
    initializationChecked: udiInitializationChecked,
    initialization: udiInitialization,
  })

  useEffect(() => {
    if (!setupComplete) {
      setUdiInitialization(null)
      setUdiInitializationChecked(false)
      return undefined
    }

    const poller = createSequentialPoller({
      intervalMs: 3000,
      poll: async (signal) => {
        try {
          const response = await api.get('/readiness', { signal })
          if (signal.aborted) return false

          const data = response.data || {}
          setUdiInitialization(getInitializationStateFromStatus(data))
          setUdiInitializationChecked(true)
          return data.ready !== true
        } catch (err) {
          if (signal.aborted) return false
          const readiness = err?.response?.data
          if (readiness && typeof readiness.ready === 'boolean') {
            setUdiInitialization(getInitializationStateFromStatus(readiness))
            setUdiInitializationChecked(true)
            return readiness.ready !== true
          }
          console.error('Failed to check initialization status:', err)
          setUdiInitialization((previous) => getInitializationStateFromStatusError(previous))
          setUdiInitializationChecked(true)
          return true
        }
      },
    })

    poller.start()

    return () => {
      poller.stop()
    }
  }, [setupComplete])

  useEffect(() => {
    if (shouldRedirectForStartupGate({
      setupComplete,
      initializationChecked: udiInitializationChecked,
      initialization: udiInitialization,
      pathname: location.pathname,
    })) {
      navigate('/', { replace: true })
    }
  }, [location.pathname, navigate, setupComplete, udiInitialization, udiInitializationChecked])

  if (loading) {
    return (
      <div className="flex items-center justify-center h-screen">
        <div className="text-center">
          <div className="animate-spin rounded-full h-12 w-12 border-b-2 border-primary mx-auto mb-4"></div>
          <p className="text-muted-foreground">Loading...</p>
        </div>
      </div>
    )
  }

  if (!setupComplete && setupStatus) {
    return (
      <Suspense fallback={<PageLoading />}>
        <SetupWizard onComplete={handleSetupComplete} setupStatus={setupStatus} />
      </Suspense>
    )
  }

  if (!setupComplete && !setupStatus) {
    return (
      <div className="flex flex-col items-center justify-center h-screen p-4">
        <div className="text-center max-w-md">
          <h1 className="text-2xl font-bold mb-4">Connection Error</h1>
          <p className="text-muted-foreground mb-6">
            Failed to connect to the backend server. Please check your connection and try again.
          </p>
          <button
            onClick={checkSetupStatus}
            className="px-4 py-2 bg-primary text-primary-foreground rounded-md hover:bg-primary/90"
          >
            Retry Connection
          </button>
        </div>
      </div>
    )
  }

  return (
    <div className="flex min-h-screen bg-background">
      <Sidebar
        isCollapsed={isCollapsed}
        setIsCollapsed={setIsCollapsed}
        navigationDisabled={startupGateActive}
      />

      <main className={cn(
        "flex-1 p-6 transition-all duration-300 ease-in-out",
        isCollapsed ? "lg:ml-20" : "lg:ml-64"
      )}>
        <div className="max-w-7xl mx-auto pt-12 lg:pt-0">
          {startupGateActive ? (
            <StreamFlowInitializingScreen
              initialization={udiInitialization || {
                percentage: 0,
                message: 'Checking startup status...',
              }}
            />
          ) : (
            <AppErrorBoundary>
              <Suspense fallback={<PageLoading />}>
                <Routes>
                  <Route path="/" element={<Dashboard />} />
                  <Route path="/dashboard" element={<Dashboard />} />
                  <Route path="/stream-checker" element={<StreamChecker />} />
                  <Route path="/stream-monitoring" element={<StreamMonitoring />} />
                  <Route path="/shadow-monitor" element={<ShadowBlankMonitor />} />
                  <Route path="/teamarr-preflight" element={<TeamarrPreflight />} />
                  <Route path="/channels" element={<ChannelConfiguration />} />
                  <Route path="/settings" element={<AutomationSettings />} />
                  <Route path="/automation/profiles/:profileId" element={<AutomationProfileEditor />} />
                  <Route path="/scheduling" element={<Scheduling />} />
                  <Route path="/stats" element={<StatsDashboard />} />
                  <Route path="/help" element={<OperatorHelp />} />
                  <Route path="/help/:topicId" element={<OperatorHelp />} />
                  <Route path="/changelog" element={<Changelog />} />
                  <Route path="*" element={<Navigate to="/" replace />} />
                </Routes>
              </Suspense>
            </AppErrorBoundary>
          )}
        </div>
      </main>

      <Toaster />
    </div>
  )
}

export default App
