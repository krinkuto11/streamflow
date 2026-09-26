import { useEffect, useRef, useState } from 'react'
import * as DialogPrimitive from '@radix-ui/react-dialog'
import { Link, useLocation } from 'react-router-dom'
import {
  Activity, Calendar, CalendarCheck, CheckCircle, ChevronLeft, ChevronRight,
  CircleHelp, Eye, History, LayoutDashboard, ListChecks, Menu, Settings,
  TrendingUp, X,
} from 'lucide-react'
import { cn } from '@/lib/utils.js'
import { Button } from '@/components/ui/button.jsx'
import { ThemeToggle } from '@/components/ThemeToggle.jsx'
import { versionAPI, environmentAPI } from '@/services/api.js'
import {
  Tooltip, TooltipContent, TooltipProvider, TooltipTrigger,
} from '@/components/ui/tooltip'

const navigationGroups = [
  {
    label: 'Workspace',
    items: [
      { text: 'Dashboard', icon: LayoutDashboard, path: '/' },
      { text: 'Channels', icon: ListChecks, path: '/channels' },
      { text: 'Monitoring', icon: Activity, path: '/stream-monitoring' },
    ],
  },
  {
    label: 'Operations',
    items: [
      { text: 'Stream Checker', icon: CheckCircle, path: '/stream-checker' },
      { text: 'Shadow Monitor', icon: Eye, path: '/shadow-monitor' },
      { text: 'Teamarr Preflight', icon: CalendarCheck, path: '/teamarr-preflight' },
      { text: 'Scheduling', icon: Calendar, path: '/scheduling' },
      { text: 'Analytics', icon: TrendingUp, path: '/stats' },
      { text: 'Changelog', icon: History, path: '/changelog' },
    ],
  },
  {
    label: 'System',
    items: [
      { text: 'Settings', icon: Settings, path: '/settings' },
      { text: 'Help', icon: CircleHelp, path: '/help' },
    ],
  },
]
const primaryNavigation = navigationGroups[0].items
const allNavigation = navigationGroups.flatMap(group => group.items)

export function getNavigationItem(pathname) {
  if (pathname === '/dashboard') return allNavigation[0]
  if (pathname.startsWith('/automation/profiles/')) return allNavigation.find(item => item.path === '/settings')
  return allNavigation.find(item => pathname === item.path || (item.path !== '/' && pathname.startsWith(item.path + '/'))) || allNavigation[0]
}

export function Sidebar({ isCollapsed, setIsCollapsed, navigationDisabled = false }) {
  const [isOpen, setIsOpen] = useState(false)
  const [version, setVersion] = useState(null)
  const [publicIp, setPublicIp] = useState(null)
  const navigationOpener = useRef(null)
  const location = useLocation()
  const currentItem = getNavigationItem(location.pathname)

  useEffect(() => {
    setIsOpen(false)
  }, [location.pathname])

  useEffect(() => {
    versionAPI.getVersion()
      .then(response => setVersion(response.data.version))
      .catch(error => {
        console.error('Failed to fetch version:', error)
        setVersion('dev-unknown')
      })
    environmentAPI.getEnvironment()
      .then(response => setPublicIp(response.data.public_ip))
      .catch(error => console.error('Failed to fetch environment:', error))
  }, [])

  useEffect(() => {
    const desktop = window.matchMedia('(min-width: 1024px)')
    const closeOnDesktop = () => { if (desktop.matches) setIsOpen(false) }
    desktop.addEventListener('change', closeOnDesktop)
    return () => desktop.removeEventListener('change', closeOnDesktop)
  }, [])

  const openNavigation = event => {
    navigationOpener.current = event.currentTarget
    setIsOpen(true)
  }

  const renderLink = (item, compact = false) => {
    const Icon = item.icon
    const isDisabled = navigationDisabled && item.path !== '/'
    const isActive = currentItem.path === item.path && (!navigationDisabled || item.path === '/')
    return (
      <Link
        key={item.path}
        to={item.path}
        aria-current={isActive ? 'page' : undefined}
        aria-disabled={isDisabled || undefined}
        aria-label={compact ? item.text : undefined}
        tabIndex={isDisabled ? -1 : undefined}
        onClick={event => {
          if (isDisabled) event.preventDefault()
          else setIsOpen(false)
        }}
        className={cn(
          'flex min-h-11 items-center gap-3 rounded-lg border px-3 py-2.5 text-sm font-medium transition-colors focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary',
          isActive
            ? 'border-primary/30 bg-primary/15 text-primary dark:text-emerald-200'
            : 'border-transparent text-muted-foreground hover:bg-muted hover:text-foreground',
          isDisabled && 'cursor-not-allowed opacity-40',
          compact && 'lg:mx-auto lg:h-11 lg:w-11 lg:justify-center lg:px-0',
        )}
      >
        <Icon className="h-[18px] w-[18px] shrink-0" aria-hidden="true" />
        <span className={cn('truncate', compact && 'lg:sr-only')}>{item.text}</span>
      </Link>
    )
  }

  const renderNavigation = (mobile = false) => {
    const compact = !mobile && isCollapsed
    return <>
        <div className={cn('flex h-20 shrink-0 items-center justify-between gap-2 border-b px-4', compact && 'justify-center px-2')}>
          <div className={cn('min-w-0', compact && 'hidden')}>
            <div className="text-lg font-bold tracking-tight text-foreground">
              <span className="text-primary">Stream</span>Flow
            </div>
            <div className="text-[11px] font-medium uppercase tracking-[0.14em] text-muted-foreground">for Dispatcharr</div>
          </div>
          {mobile ? <Button variant="ghost" size="icon" className="h-11 w-11 shrink-0" aria-label="Close navigation" onClick={() => setIsOpen(false)}>
            <X className="h-5 w-5" />
          </Button> : <Button
            variant="outline"
            size="icon"
            className="h-8 w-8 shrink-0"
            aria-label={isCollapsed ? 'Expand navigation' : 'Collapse navigation'}
            aria-expanded={!isCollapsed}
            onClick={() => setIsCollapsed(!isCollapsed)}
          >
            {isCollapsed ? <ChevronRight className="h-4 w-4" /> : <ChevronLeft className="h-4 w-4" />}
          </Button>}
        </div>

        <nav className="min-h-0 flex-1 space-y-6 overflow-y-auto px-3 py-5" aria-label="Pages">
          <TooltipProvider delayDuration={0}>
            {navigationGroups.map(group => (
              <div key={group.label}>
                <div className={cn('mb-2 px-3 text-[10px] font-bold uppercase tracking-[0.15em] text-muted-foreground/80', compact && 'sr-only')}>
                  {group.label}
                </div>
                <div className="space-y-0.5">
                  {group.items.map(item => compact ? (
                    <Tooltip key={item.path}>
                      <TooltipTrigger asChild>{renderLink(item, true)}</TooltipTrigger>
                      <TooltipContent side="right">{item.text}</TooltipContent>
                    </Tooltip>
                  ) : renderLink(item))}
                </div>
              </div>
            ))}
          </TooltipProvider>
        </nav>

        <div className="shrink-0 space-y-3 border-t px-4 py-4">
          <div className={cn('flex items-center justify-between gap-2', compact && 'justify-center')}>
            <span className={cn('text-xs text-muted-foreground', compact && 'hidden')}>Appearance</span>
            <ThemeToggle />
          </div>
          {publicIp && <div className={cn('text-[11px] text-muted-foreground', compact && 'hidden')}>Public IP <span className="block truncate font-mono text-foreground">{publicIp}</span></div>}
          {version && <div className="truncate text-[10px] text-muted-foreground" title={version}>v{version}</div>}
        </div>
    </>
  }

  return (
    <>
      <header className="fixed inset-x-0 top-0 z-30 flex h-14 items-center justify-between gap-3 border-b bg-card/95 px-4 backdrop-blur lg:hidden">
        <span className="min-w-0 truncate text-sm font-semibold">
          <span className="mr-2 text-primary">StreamFlow</span>
          <span className="text-muted-foreground">/</span> {currentItem.text}
        </span>
        <Button variant="ghost" size="icon" className="h-11 w-11" aria-label="Open all navigation" aria-haspopup="dialog" aria-expanded={isOpen} onClick={openNavigation}>
          <Menu className="h-5 w-5" />
        </Button>
      </header>

      <aside aria-label="Main navigation" className={cn('fixed inset-y-0 left-0 z-30 hidden flex-col border-r bg-card lg:flex', isCollapsed ? 'w-20' : 'w-56')}>
        {renderNavigation()}
      </aside>

      <DialogPrimitive.Root open={isOpen} onOpenChange={setIsOpen}>
        <DialogPrimitive.Portal>
          <DialogPrimitive.Overlay className="fixed inset-0 z-40 bg-black/60" />
          <DialogPrimitive.Content
            className="fixed inset-y-0 left-0 z-50 flex w-72 max-w-[calc(100vw-2rem)] flex-col border-r bg-card shadow-xl outline-none"
            aria-describedby={undefined}
            onKeyDown={event => {
              // Radix handles Escape during capture. Keep an unhandled key usable
              // while its layer registration settles after rapid focus changes.
              // Portal menus and nested dialogs retain their own Escape behavior.
              if (event.key !== 'Escape' || event.defaultPrevented) return
              if (!event.currentTarget.contains(event.target)) return
              if (event.target.closest('[role="dialog"]') !== event.currentTarget) return
              if (event.target.closest('[role="menu"], [role="listbox"]')) return
              if (event.currentTarget.querySelector('[aria-haspopup="menu"][aria-expanded="true"]')) return
              event.preventDefault()
              setIsOpen(false)
            }}
            onCloseAutoFocus={event => {
              event.preventDefault()
              const opener = navigationOpener.current
              if (opener?.isConnected && opener.getClientRects().length) opener.focus()
            }}
          >
            <DialogPrimitive.Title className="sr-only">StreamFlow navigation</DialogPrimitive.Title>
            {renderNavigation(true)}
          </DialogPrimitive.Content>
        </DialogPrimitive.Portal>
      </DialogPrimitive.Root>

      <nav aria-label="Quick navigation" className="fixed inset-x-0 bottom-0 z-30 grid grid-cols-4 border-t bg-card/95 pb-[env(safe-area-inset-bottom)] backdrop-blur lg:hidden">
        {primaryNavigation.map(item => {
          const Icon = item.icon
          const active = currentItem.path === item.path
          const disabled = navigationDisabled && item.path !== '/'
          return (
            <Link
              key={item.path}
              to={item.path}
              aria-current={active ? 'page' : undefined}
              aria-disabled={disabled || undefined}
              tabIndex={disabled ? -1 : undefined}
              onClick={event => { if (disabled) event.preventDefault() }}
              className={cn('flex min-h-16 flex-col items-center justify-center gap-1 text-[10px] font-semibold focus-visible:outline focus-visible:outline-2 focus-visible:outline-primary', active ? 'text-primary' : 'text-muted-foreground', disabled && 'opacity-40')}
            >
              <Icon className="h-5 w-5" aria-hidden="true" />
              {item.text}
            </Link>
          )
        })}
        <button type="button" onClick={openNavigation} aria-label="More pages" aria-haspopup="dialog" aria-expanded={isOpen} className={cn('flex min-h-16 flex-col items-center justify-center gap-1 text-[10px] font-semibold focus-visible:outline focus-visible:outline-2 focus-visible:outline-primary', primaryNavigation.includes(currentItem) ? 'text-muted-foreground' : 'text-primary')}>
          <Menu className="h-5 w-5" aria-hidden="true" />
          More
        </button>
      </nav>
    </>
  )
}
