import { useState, useEffect } from 'react'
import { Routes, Route, useNavigate } from 'react-router-dom'
import { Sidebar } from '@/components/layout/Sidebar.jsx'
import { Toaster } from '@/components/ui/toaster.jsx'
import { useToast } from '@/hooks/use-toast.js'
import { api } from '@/services/api.js'

// Page imports
import Dashboard from '@/pages/Dashboard'
import StreamChecker from '@/pages/StreamChecker'
import ChannelConfiguration from '@/pages/ChannelConfiguration'
import AutomationSettings from '@/pages/AutomationSettings'
import Changelog from '@/pages/Changelog'
import SetupWizard from '@/pages/SetupWizard'
import Scheduling from '@/pages/Scheduling'
import AceStreamMonitoring from '@/pages/AceStreamMonitoring'

function App() {
  const [setupStatus, setSetupStatus] = useState(null)
  const [loading, setLoading] = useState(true)
  const { toast } = useToast()
  const navigate = useNavigate()

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
    return <SetupWizard onComplete={handleSetupComplete} setupStatus={setupStatus} />
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
      <Sidebar />
      
      <main className="flex-1 lg:ml-64 p-6">
        <div className="max-w-7xl mx-auto pt-12 lg:pt-0">
          <Routes>
            <Route path="/" element={<Dashboard />} />
            <Route path="/stream-checker" element={<StreamChecker />} />
            <Route path="/channels" element={<ChannelConfiguration />} />
            <Route path="/acestream" element={<AceStreamMonitoring />} />
            <Route path="/settings" element={<AutomationSettings />} />
            <Route path="/scheduling" element={<Scheduling />} />
            <Route path="/changelog" element={<Changelog />} />
          </Routes>
        </div>
      </main>

      <Toaster />
    </div>
  )
}

export default App
