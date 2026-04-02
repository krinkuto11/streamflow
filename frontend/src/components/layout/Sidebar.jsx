import { useState, useEffect } from 'react'
import { Link, useLocation } from 'react-router-dom'
import { cn } from '@/lib/utils.js'
import {
  LayoutDashboard,
  CheckCircle,
  Settings,
  ListChecks,
  History,
  Menu,
  X,
  Calendar,
  Activity,
  ChevronLeft,
  ChevronRight,
  TrendingUp
} from 'lucide-react'
import { Button } from '@/components/ui/button.jsx'
import { ThemeToggle } from '@/components/ThemeToggle.jsx'
import { versionAPI, environmentAPI } from '@/services/api.js'
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip"

const menuItems = [
  { text: 'Dashboard', icon: LayoutDashboard, path: '/' },
  { text: 'Stream Checker', icon: CheckCircle, path: '/stream-checker' },
  { text: 'Stream Monitoring', icon: Activity, path: '/stream-monitoring' },
  { text: 'Channel Configuration', icon: ListChecks, path: '/channels' },
  { text: 'Scheduling', icon: Calendar, path: '/scheduling' },
  { text: 'Analytics', icon: TrendingUp, path: '/stats' },
  { text: 'Settings', icon: Settings, path: '/settings' },
  { text: 'Changelog', icon: History, path: '/changelog' },
]

export function Sidebar({ isCollapsed, setIsCollapsed }) {
  const [isOpen, setIsOpen] = useState(false)
  const [version, setVersion] = useState(null)
  const [publicIp, setPublicIp] = useState(null)
  const location = useLocation()

  useEffect(() => {
    const fetchVersion = async () => {
      try {
        const response = await versionAPI.getVersion()
        setVersion(response.data.version)
      } catch (error) {
        console.error('Failed to fetch version:', error)
        setVersion('dev-unknown')
      }
    }
    fetchVersion()
  }, [])

  useEffect(() => {
    const fetchEnvironment = async () => {
      try {
        const response = await environmentAPI.getEnvironment()
        setPublicIp(response.data.public_ip)
      } catch (error) {
        console.error('Failed to fetch environment:', error)
      }
    }
    fetchEnvironment()
  }, [])

  return (
    <>
      {/* Mobile menu button */}
      <Button
        variant="ghost"
        size="icon"
        className="fixed top-4 left-4 z-50 lg:hidden"
        onClick={() => setIsOpen(!isOpen)}
      >
        {isOpen ? <X className="h-6 w-6" /> : <Menu className="h-6 w-6" />}
      </Button>

      {/* Overlay for mobile */}
      {isOpen && (
        <div
          className="fixed inset-0 bg-black/50 z-40 lg:hidden"
          onClick={() => setIsOpen(false)}
        />
      )}

      {/* Sidebar */}
      <aside
        className={cn(
          "fixed top-0 left-0 h-full bg-card border-r border-border z-40 transition-all duration-300 ease-in-out flex flex-col",
          isCollapsed ? "w-20" : "w-64",
          isOpen ? "translate-x-0" : "-translate-x-full lg:translate-x-0"
        )}
      >
        <div className={cn(
          "p-6 relative flex flex-col",
          isCollapsed && "items-center px-0"
        )}>
          <div className="flex items-center justify-between w-full">
            {!isCollapsed && (
              <div className="animate-in fade-in slide-in-from-left-2 duration-300">
                <h1 className="text-2xl font-bold text-primary">StreamFlow</h1>
                <p className="text-sm text-muted-foreground">for Dispatcharr</p>
              </div>
            )}

            <Button
              variant="ghost"
              size="icon"
              className={cn(
                "hidden lg:flex h-8 w-8 rounded-full border border-border bg-background shadow-sm hover:bg-accent",
                isCollapsed ? "mx-auto" : ""
              )}
              onClick={() => setIsCollapsed(!isCollapsed)}
            >
              {isCollapsed ? <ChevronRight className="h-4 w-4" /> : <ChevronLeft className="h-4 w-4" />}
            </Button>
          </div>
        </div>

        <nav className={cn(
          "px-3 space-y-1 flex-1 overflow-y-auto overflow-x-hidden pt-2",
          isCollapsed && "px-2 items-center"
        )}>
          <TooltipProvider delayDuration={0}>
            {menuItems.map((item) => {
              const Icon = item.icon
              const isActive = location.pathname === item.path

              const linkContent = (
                <Link
                  key={item.path}
                  to={item.path}
                  onClick={() => setIsOpen(false)}
                  className={cn(
                    "flex items-center gap-3 px-3 py-2 rounded-lg transition-all duration-200 group relative",
                    isActive
                      ? "bg-primary text-primary-foreground shadow-md"
                      : "hover:bg-accent hover:text-accent-foreground",
                    isCollapsed ? "justify-center px-0 w-12 h-12 mx-auto" : "w-full"
                  )}
                >
                  <Icon className={cn("h-5 w-5 shrink-0", !isActive && "text-muted-foreground group-hover:text-foreground")} />
                  {!isCollapsed && (
                    <span className="text-sm font-medium whitespace-nowrap overflow-hidden transition-all duration-300">
                      {item.text}
                    </span>
                  )}
                  {isActive && isCollapsed && (
                    <div className="absolute left-0 w-1 h-6 bg-primary-foreground rounded-r-full" />
                  )}
                </Link>
              )

              if (isCollapsed) {
                return (
                  <Tooltip key={item.path}>
                    <TooltipTrigger asChild>
                      {linkContent}
                    </TooltipTrigger>
                    <TooltipContent side="right" className="font-medium">
                      {item.text}
                    </TooltipContent>
                  </Tooltip>
                )
              }

              return linkContent
            })}
          </TooltipProvider>
        </nav>

        <div className={cn(
          "p-3 border-t border-border space-y-2 mt-auto bg-card/50",
          isCollapsed && "flex flex-col items-center px-0"
        )}>
          <div className={cn(
            "flex items-center justify-between w-full px-2",
            isCollapsed && "flex-col gap-4 justify-center"
          )}>
            {!isCollapsed && <span className="text-sm text-muted-foreground font-medium">Theme</span>}
            <div className={cn(isCollapsed ? "scale-90" : "")}>
              <ThemeToggle />
            </div>
          </div>
          {!isCollapsed && publicIp && (
            <div className="px-2 space-y-1">
              <span className="text-xs text-muted-foreground font-medium">Public IP</span>
              <div className="rounded-md border border-border bg-background px-2 py-1.5">
                <span className="text-xs font-mono text-foreground block text-center">
                  {publicIp}
                </span>
              </div>
            </div>
          )}
          {version && (
            <div className={cn(
              "pt-2 text-[10px] text-muted-foreground text-center font-mono opacity-60",
              isCollapsed ? "w-full overflow-hidden truncate px-1" : ""
            )}>
              {isCollapsed ? version.split('-')[0] : `v${version}`}
            </div>
          )}
        </div>
      </aside>
    </>
  )
}
