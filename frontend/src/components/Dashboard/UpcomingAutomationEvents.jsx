import { useState, useEffect } from 'react'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card.jsx'
import { Badge } from '@/components/ui/badge.jsx'
import { Button } from '@/components/ui/button.jsx'
import { Progress } from '@/components/ui/progress.jsx'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select.jsx'
import { automationAPI } from '@/services/api.js'
import { useToast } from '@/hooks/use-toast.js'
import { Calendar, Clock, RefreshCw, Filter, Loader2 } from 'lucide-react'

const EVENTS_AUTO_REFRESH_MS = 300000
const NEXT_EVENT_REFRESH_DELAY_MS = 15000

export default function UpcomingAutomationEvents({ udiInitialization = null }) {
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
  const udiInitializing = Boolean(udiInitialization?.inProgress)
  const udiProgress = Number.isFinite(Number(udiInitialization?.percentage))
    ? Math.max(0, Math.min(100, Number(udiInitialization.percentage)))
    : 0

  useEffect(() => {
    if (udiInitializing) {
      setLoading(false)
      setRefreshing(false)
      return undefined
    }

    loadData()
    loadPeriods()

    const interval = setInterval(() => {
      loadData(false)
    }, EVENTS_AUTO_REFRESH_MS)

    return () => clearInterval(interval)
  }, [timeRangeFilter, periodFilter, udiInitializing])

  useEffect(() => {
    if (udiInitializing || events.length === 0) {
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
  }, [events, timeRangeFilter, periodFilter, udiInitializing])

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
      const response = await automationAPI.getPeriods()
      setAllPeriods(response.data || [])
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
      className={`flex items-start justify-between p-3 border rounded-lg ${isNext ? 'border-primary bg-primary/5' : 'hover:bg-accent/50'
        } transition-colors`}
    >
      <div className="flex-1 space-y-1">
        <div className="flex items-center gap-2">
          <span className="font-medium text-sm">{event.period_name}</span>
          {isNext && <Badge variant="default" className="text-xs">Next</Badge>}
        </div>
        <div className="flex items-center gap-4 text-xs text-muted-foreground">
          <div className="flex items-center gap-1">
            <Clock className="h-3 w-3" />
            {formatDateTime(event.time)}
          </div>
          <div>
            Profiles: {event.profile_display || 'No Profile'}
          </div>
          <div>
            {event.channel_count} channel{event.channel_count !== 1 ? 's' : ''}
          </div>
        </div>
      </div>
      <div className="flex items-center gap-2">
        <Badge variant="outline" className="text-xs">
          {formatTime(event.time)}
        </Badge>
      </div>
    </div>
  )

  if (loading && !refreshing) {
    return (
      <Card>
        <CardHeader>
          <CardTitle>Upcoming Automation Events</CardTitle>
        </CardHeader>
        <CardContent>
          <div className="flex items-center justify-center py-8">
            <Loader2 className="h-8 w-8 animate-spin text-muted-foreground" />
          </div>
        </CardContent>
      </Card>
    )
  }

  if (udiInitializing) {
    return (
      <Card>
        <CardHeader>
          <CardTitle>Upcoming Automation Events</CardTitle>
          <CardDescription>Waiting for Dispatcharr cache startup</CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="flex items-center justify-between gap-3 text-sm">
            <span className="text-muted-foreground">
              {udiInitialization?.message || 'Loading channels, streams, playlists and profiles'}
            </span>
            <span className="tabular-nums text-muted-foreground">{Math.round(udiProgress)}%</span>
          </div>
          <Progress value={udiProgress} className="h-2" />
          <p className="text-sm text-muted-foreground">
            Automation events will load automatically when the cache is ready.
          </p>
        </CardContent>
      </Card>
    )
  }

  const grouped = groupEventsByTime()

  return (
    <Card>
      <CardHeader>
        <div className="flex items-center justify-between">
          <div>
            <CardTitle>Upcoming Automation Events</CardTitle>
            <CardDescription>
              Scheduled automation runs based on configured periods
            </CardDescription>
          </div>
          <Button
            variant="outline"
            size="sm"
            onClick={handleRefresh}
            disabled={refreshing}
          >
            <RefreshCw className={`h-4 w-4 mr-2 ${refreshing ? 'animate-spin' : ''}`} />
            Refresh
          </Button>
        </div>
      </CardHeader>
      <CardContent className="space-y-4">
        {/* Filters */}
        {loadError && (
          <div className="rounded-md border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-sm text-amber-700 dark:text-amber-300">
            {loadError}
          </div>
        )}

        <div className="flex gap-4">
          <div className="flex-1">
            <div className="flex items-center gap-2 mb-2">
              <Filter className="h-4 w-4 text-muted-foreground" />
              <span className="text-sm font-medium">Filter by Period</span>
            </div>
            <Select value={periodFilter} onValueChange={setPeriodFilter}>
              <SelectTrigger>
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="all">All Periods</SelectItem>
                {allPeriods.map((period) => (
                  <SelectItem key={period.id} value={period.id}>
                    {period.name}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>

          <div className="flex-1">
            <div className="flex items-center gap-2 mb-2">
              <Calendar className="h-4 w-4 text-muted-foreground" />
              <span className="text-sm font-medium">Time Range</span>
            </div>
            <Select value={timeRangeFilter} onValueChange={setTimeRangeFilter}>
              <SelectTrigger>
                <SelectValue />
              </SelectTrigger>
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

        {/* Global Disabled State */}
        {!automationEnabled ? (
          <div className="text-center py-12">
            <Calendar className="h-12 w-12 mx-auto mb-4 opacity-50 text-muted-foreground" />
            <p className="text-muted-foreground mb-2">Automation is Globally Disabled</p>
            <p className="text-sm text-muted-foreground">
              Enable "Regular Automation" in Automation Settings to resume schedules.
            </p>
          </div>
        ) : events.length === 0 ? (
          <div className="text-center py-12">
            <Calendar className="h-12 w-12 mx-auto mb-4 opacity-50 text-muted-foreground" />
            <p className="text-muted-foreground mb-2">No upcoming automation events</p>
            <p className="text-sm text-muted-foreground">
              {allPeriods.length === 0
                ? 'Create automation periods to schedule events'
                : 'Check your period schedules and channel assignments'}
            </p>
          </div>
        ) : (
          <div className="space-y-4">
            {/* Next Event */}
            {grouped.next.length > 0 && (
              <div className="space-y-2">
                <h3 className="text-sm font-semibold text-primary">Next Event</h3>
                {grouped.next.map(event => renderEvent(event, true))}
              </div>
            )}

            {/* Soon (within 1 hour) */}
            {grouped.soon.length > 0 && (
              <div className="space-y-2">
                <h3 className="text-sm font-semibold">Within 1 Hour</h3>
                {grouped.soon.map(event => renderEvent(event))}
              </div>
            )}

            {/* Today (within 24 hours) */}
            {grouped.today.length > 0 && (
              <div className="space-y-2">
                <h3 className="text-sm font-semibold">Today</h3>
                <div className="space-y-2 max-h-[300px] overflow-y-auto">
                  {grouped.today.map(event => renderEvent(event))}
                </div>
              </div>
            )}

            {/* Upcoming (beyond 24 hours) */}
            {grouped.upcoming.length > 0 && (
              <div className="space-y-2">
                <h3 className="text-sm font-semibold">Upcoming</h3>
                <div className="space-y-2 max-h-[200px] overflow-y-auto">
                  {grouped.upcoming.map(event => renderEvent(event))}
                </div>
              </div>
            )}
          </div>
        )}

        {/* Cache info */}
        {cachedAt && (
          <p className="text-xs text-muted-foreground text-center pt-4 border-t">
            Last updated: {new Date(cachedAt).toLocaleTimeString()}
          </p>
        )}
      </CardContent>
    </Card>
  )
}
