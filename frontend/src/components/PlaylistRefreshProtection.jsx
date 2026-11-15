import { useEffect, useState } from 'react'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card.jsx'
import { Button } from '@/components/ui/button.jsx'
import { Checkbox } from '@/components/ui/checkbox.jsx'
import { automationAPI } from '@/services/api.js'

export default function PlaylistRefreshProtection() {
  const [attempts, setAttempts] = useState([])
  const [loading, setLoading] = useState(true)
  const [resolving, setResolving] = useState(null)
  const [confirmed, setConfirmed] = useState({})
  const [error, setError] = useState(null)
  const [message, setMessage] = useState(null)

  const load = async () => {
    setLoading(true)
    setError(null)
    setConfirmed({})
    try {
      const response = await automationAPI.getRefreshAttempts()
      setAttempts(response.data.data.attempts)
    } catch (err) {
      setError(err.response?.data?.error || 'Could not load playlist refresh protection. Reload before resolving a start.')
    } finally {
      setLoading(false)
    }
  }
  useEffect(() => { load() }, [])

  const resolve = async (attempt) => {
    setResolving(attempt.attempt_id)
    setError(null)
    setMessage(null)
    try {
      const response = await automationAPI.resolveRefreshAttempt({
        account_id: attempt.account_id, attempt_id: attempt.attempt_id, confirmed_finished: true,
      })
      setMessage(response.data.message)
      await load()
    } catch (err) {
      setError(err.response?.data?.error || 'Could not resolve this start. Reload its current status before trying again.')
      setConfirmed({})
    } finally {
      setResolving(null)
    }
  }

  return <Card>
    <CardHeader>
      <CardTitle>Playlist refresh protection</CardTitle>
      <CardDescription>
        When Dispatcharr may have accepted a refresh but its reply is missing, StreamFlow blocks another start for that playlist, including after a restart.
      </CardDescription>
    </CardHeader>
    <CardContent className="space-y-4">
      <Button variant="outline" onClick={load} disabled={loading || resolving !== null}>Reload refresh status</Button>
      {loading && <p role="status">Loading refresh protection…</p>}
      {error && <p role="alert" className="text-sm text-destructive">{error}</p>}
      {message && <p role="status" className="text-sm">{message}</p>}
      {!loading && !error && attempts.length === 0 && <p className="text-sm text-muted-foreground">No unresolved playlist refresh starts.</p>}
      {attempts.map(attempt => {
        const disabled = loading || resolving !== null || !!error || !attempt.status_available || attempt.busy || attempt.request_in_flight
        const checkboxId = `confirm-refresh-${attempt.attempt_id}`
        return <div key={attempt.attempt_id} className="rounded-md border p-4 space-y-3">
          <p className="font-medium">{attempt.name || `Playlist ${attempt.account_id}`}</p>
          <p className="text-sm text-muted-foreground">
            Start outcome {attempt.state === 'sending' ? 'unconfirmed' : 'unknown'} since {new Date(attempt.started_at * 1000).toLocaleString()}.
            {' '}Current account status: {attempt.status_available ? (attempt.status || 'not reported') : 'unavailable'}.
          </p>
          <p className="text-sm">Check Dispatcharr and verify that both the live import and any VOD refresh tasks have finished or stopped. An idle or successful account status alone does not prove that VOD has finished.</p>
          {attempt.request_in_flight && <p className="text-sm">The start request may still be in flight. Wait and reload its status.</p>}
          {attempt.busy && <p className="text-sm">Dispatcharr still reports a running refresh.</p>}
          <div className="flex items-start gap-2">
            <Checkbox id={checkboxId} checked={!!confirmed[attempt.attempt_id]} disabled={disabled}
              onCheckedChange={checked => setConfirmed(prev => ({ ...prev, [attempt.attempt_id]: checked === true }))} />
            <label htmlFor={checkboxId} className="text-sm">I verified that both live and VOD refresh tasks have finished or stopped in Dispatcharr.</label>
          </div>
          <Button variant="outline" disabled={disabled || !confirmed[attempt.attempt_id]} onClick={() => resolve(attempt)}>
            {resolving === attempt.attempt_id ? 'Resolving…' : 'Allow future refreshes'}
          </Button>
        </div>
      })}
    </CardContent>
  </Card>
}
