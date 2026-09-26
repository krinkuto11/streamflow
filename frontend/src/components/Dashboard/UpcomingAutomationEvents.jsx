import { useState, useEffect } from 'react'
import { Link } from 'react-router-dom'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card.jsx'
import { Badge } from '@/components/ui/badge.jsx'
import { Button } from '@/components/ui/button.jsx'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select.jsx'
import { automationAPI } from '@/services/api.js'
import { useToast } from '@/hooks/use-toast.js'
import { Calendar, Clock, RefreshCw, Filter, Loader2 } from 'lucide-react'

const EVENTS_AUTO_REFRESH_MS = 300000
const NEXT_EVENT_REFRESH_DELAY_MS = 15000

export default function UpcomingAutomationEvents() {
  const [events, setEvents] = useState([])
  const [allPeriods, setAllPeriods] = useState([])
  const [loading, setLoading] = useState(true)
  const [refreshing, setRefreshing] = useState(false)
  const [periodFilter, setPeriodFilter] = useState('all')
  const [timeRangeFilter, setTimeRangeFilter] = useState('24')
  const [cachedAt, setCachedAt] = useState(null)
  const [automationEnabled, setAutomationEnabled] = useState(true)
  const [loadError, setLoadError] = useState('')
  const { toast } = useToast()

  useEffect(() => {
    loadData()
    loadPeriods()

    const interval = setInterval(() => {
      loadData(false)
    }, EVENTS_AUTO_REFRESH_MS)

    return () => clearInterval(interval)
  }, [timeRangeFilter, periodFilter])

  useEffect(() => {
    if (events.length === 0) {
      return undefined
    }

    const nextTimestamp = Date.parse(events[0]?.time)
    if (!Number.isFinite(nextTimestamp)) {
      return undefined
    }

    const delay = Math.max(1000, nextTimestamp - Date.now() + NEXT_EVENT_REFRESH_DELAY_MS)
    const timer = setTimeout(() => {
      loadData(true)
    }, delay)

    return () => clearTimeout(timer)
  }, [events, timeRangeFilter, periodFilter])

  const loadData = async (forceRefresh = false) => {
    try {
      if (forceRefresh) {
        setRefreshing(true)
      } else {
        setLoading(true)
      }

      const hours = parseInt(timeRangeFilter)
      const periodId = periodFilter !== 'all' ? periodFilter : null

      const response = await automationAPI.getUpcomingEvents(hours, 100, periodId, forceRefresh)
      setEvents(response.data.events || [])
      setCachedAt(response.data.cached_at)
      setAutomationEnabled(response.data.automation_enabled ?? true)
      setLoadError('')
    } catch (err) {
      console.error('Failed to load upcoming events:', err)
      setLoadError('Upcoming automation events are temporarily unavailable. Retrying automatically.')
      if (forceRefresh) {
        toast({
          title: "Error",
          description: "Failed to load upcoming automation events",
          variant: "destructive"
        })
      }
    } finally {
      setLoading(false)
      setRefreshing(false)
    }
  }

  const loadPeriods = async () => {
    try {
      const response = await automationAPI.getPeriods({ page: 1, per_page: 200 })
      const periodItems = Array.isArray(response.data) ? response.data : response.data?.items || []
      setAllPeriods(periodItems)
    } catch (err) {
      console.error('Failed to load periods:', err)
    }
  }

  const handleRefresh = () => {
    loadData(true)
  }

  const formatTime = (isoString) => {
    const date = new Date(isoString)
    const now = new Date()
    const diff = date - now
    const hours = Math.floor(diff / (1000 * 60 * 60))
    const minutes = Math.floor((diff % (1000 * 60 * 60)) / (1000 * 60))

    if (hours < 0) return 'Past'
    if (hours === 0 && minutes < 1) return 'Now'
    if (hours === 0) return `In ${minutes}m`
    if (hours < 24) return `In ${hours}h ${minutes}m`

    const days = Math.floor(hours / 24)
    const remainingHours = hours % 24
    return `In ${days}d ${remainingHours}h`
  }

  const formatDateTime = (isoString) => {
    const date = new Date(isoString)
    return date.toLocaleString('en-US', {
      month: 'short',
      day: 'numeric',
      hour: '2-digit',
      minute: '2-digit'
    })
  }

  const groupEventsByTime = () => {
    const now = new Date()
    const groups = {
      next: [],      // Next event (first one)
      soon: [],      // Within 1 hour
      today: [],     // Within 24 hours
      upcoming: []   // Beyond 24 hours
    }

    events.forEach((event, index) => {
      const eventTime = new Date(event.time)
      const diffMs = eventTime - now
      const diffHours = diffMs / (1000 * 60 * 60)

      if (index === 0) {
        groups.next.push(event)
      } else if (diffHours <= 1) {
        groups.soon.push(event)
      } else if (diffHours <= 24) {
        groups.today.push(event)
      } else {
        groups.upcoming.push(event)
      }
    })

    return groups
  }

  const renderEvent = (event, isNext = false) => (
    <div
      key={`${event.period_id}-${event.time}`}
      className={`flex min-w-0 flex-wrap items-start justify-between gap-3 p-3 border rounded-lg ${isNext ? 'border-primary/30 bg-primary/5' : 'hover:bg-accent/50'
        } transition-colors`}
    >
      <div className="min-w-0 flex-1 space-y-2">
        <div className="flex flex-wrap items-center gap-2">
          <span className="break-words font-medium text-sm">{event.period_name}</span>
          {isNext && <Badge variant="default" className="text-xs">Next</Badge>}
        </div>
        <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-muted-foreground">
          <div className="flex items-center gap-1">
            <Clock className="h-3 w-3" />
            {formatDateTime(event.time)}
          </div>
          <div className="break-words">
            Profiles: {event.profile_display || 'No Profile'}
          </div>
          <div>
            {event.channel_count} channel{event.channel_count !== 1 ? 's' : ''}
          </div>
        </div>
      </div>
      <div className="flex shrink-0 items-center gap-2">
        <Badge variant="outline" className="text-xs">
          {formatTime(event.time)}
        </Badge>
      </div>
    </div>
  )

  const grouped = groupEventsByTime()
  const additionalEventCount = Math.max(0, events.length - 1)

  return (
    <Card>
      <CardHeader className="flex flex-col gap-2 pb-3 sm:flex-row sm:items-center sm:justify-between">
        <div className="min-w-0">
          <CardTitle className="flex items-center gap-2 text-base"><Calendar className="h-4 w-4 text-muted-foreground" />Upcoming schedule</CardTitle>
          <CardDescription className="mt-1">Your next automated check</CardDescription>
        </div>
        <div className="flex flex-wrap items-center gap-1">
          <Button variant="ghost" className="min-h-11" asChild><Link to="/scheduling">Manage schedule</Link></Button>
          <Button variant="outline" size="icon" className="h-11 w-11" aria-label="Refresh upcoming schedule" onClick={handleRefresh} disabled={refreshing || loading}>
            <RefreshCw className={`h-4 w-4 ${refreshing ? 'animate-spin' : ''}`} />
          </Button>
        </div>
      </CardHeader>
      <CardContent className="space-y-3">
        {loadError && (
          <div className="rounded-md border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-sm text-amber-700 dark:text-amber-300" role="status">{loadError}</div>
        )}
        {loading && events.length === 0 ? (
          <div className="flex items-center gap-2 py-2 text-sm text-muted-foreground" role="status"><Loader2 className="h-4 w-4 animate-spin" />Loading upcoming schedule...</div>
        ) : !automationEnabled ? (
          <div className="text-sm">
            <p className="font-medium">Regular automation is disabled</p>
            <p className="mt-1 text-muted-foreground">Enable Regular Automation in Settings to resume scheduled runs.</p>
          </div>
        ) : events.length === 0 ? (
          <div className="text-sm">
            <p className="font-medium">No upcoming events in this view</p>
            <p className="mt-1 text-muted-foreground">{allPeriods.length === 0 ? 'Create a period to schedule your checks.' : 'Adjust the filters below or review your period schedules and channel assignments.'}</p>
          </div>
        ) : grouped.next.map(event => renderEvent(event, true))}
        <details className="border-t pt-2">
          <summary className="cursor-pointer py-2 text-sm font-medium marker:text-primary">
            {additionalEventCount > 0 && automationEnabled ? `${additionalEventCount} more scheduled events and filters` : 'Schedule filters'}
          </summary>
          <div className="mt-3 space-y-4">
            <div className="grid min-w-0 gap-3 sm:grid-cols-2">
              <div className="min-w-0">
                <label htmlFor="dashboard-period-filter" className="mb-2 flex items-center gap-2 text-xs font-medium text-muted-foreground"><Filter className="h-3.5 w-3.5" />Period</label>
                <Select value={periodFilter} onValueChange={setPeriodFilter}>
                  <SelectTrigger id="dashboard-period-filter" className="min-h-11"><SelectValue /></SelectTrigger>
                  <SelectContent>
                    <SelectItem value="all">All periods</SelectItem>
                    {allPeriods.map(period => <SelectItem key={period.id} value={String(period.id)}>{period.name}</SelectItem>)}
                  </SelectContent>
                </Select>
              </div>
              <div className="min-w-0">
                <label htmlFor="dashboard-time-filter" className="mb-2 flex items-center gap-2 text-xs font-medium text-muted-foreground"><Clock className="h-3.5 w-3.5" />Time range</label>
                <Select value={timeRangeFilter} onValueChange={setTimeRangeFilter}>
                  <SelectTrigger id="dashboard-time-filter" className="min-h-11"><SelectValue /></SelectTrigger>
                  <SelectContent>
                    <SelectItem value="6">Next 6 hours</SelectItem>
                    <SelectItem value="12">Next 12 hours</SelectItem>
                    <SelectItem value="24">Next 24 hours</SelectItem>
                    <SelectItem value="48">Next 2 days</SelectItem>
                    <SelectItem value="72">Next 3 days</SelectItem>
                    <SelectItem value="168">Next week</SelectItem>
                  </SelectContent>
                </Select>
              </div>
            </div>
            {loading && <p className="flex items-center gap-2 text-xs text-muted-foreground" role="status"><Loader2 className="h-3.5 w-3.5 animate-spin" />Updating schedule...</p>}
            {automationEnabled && [
              ['Within one hour', grouped.soon],
              ['Later today', grouped.today],
              ['Upcoming', grouped.upcoming],
            ].filter(([, items]) => items.length > 0).map(([title, items]) => (
              <div key={title} className="space-y-2">
                <h3 className="text-xs font-semibold text-muted-foreground">{title}</h3>
                <div className="max-h-80 space-y-2 overflow-y-auto">{items.map(event => renderEvent(event))}</div>
              </div>
            ))}
            {cachedAt && <p className="text-xs text-muted-foreground">Schedule updated {new Date(cachedAt).toLocaleTimeString()}</p>}
          </div>
        </details>
      </CardContent>
    </Card>
  )
}
