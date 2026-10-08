import { useEffect, useState } from 'react'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card.jsx'
import { Button } from '@/components/ui/button.jsx'
import { Input } from '@/components/ui/input.jsx'
import { Label } from '@/components/ui/label.jsx'
import { Switch } from '@/components/ui/switch.jsx'
import { playbackStabilityAPI } from '@/services/api.js'
import { useVisiblePolling } from '@/hooks/use-visible-polling.js'
import { useToast } from '@/hooks/use-toast.js'

export function PlaybackStabilityHistory({ status }) {
  if (!status?.enabled) return <p className="text-sm text-muted-foreground">Recording is off. Playback history has no effect on quality scoring.</p>
  return <div className="space-y-3">
    <p className="text-sm text-muted-foreground">
      {status.recording ? `${status.active_playbacks} playback sessions observed` : 'Waiting for playback data'}.
      A score requires at least 10 observed minutes and 60 valid samples per source.
    </p>
    {status.error && <p role="alert" className="text-sm text-destructive">{status.error}</p>}
    {!status.streams?.length ? <p className="text-sm text-muted-foreground">No playback history yet. Streams without sufficient history keep their existing quality score.</p> :
      <div className="overflow-x-auto"><table className="w-full text-sm">
        <caption className="sr-only">Passive playback stability history</caption>
        <thead><tr className="border-b text-left"><th className="py-2">Stream source</th><th>Observed</th><th>Stalled</th><th>Failovers</th><th>Stability</th></tr></thead>
        <tbody>{status.streams.map(row => <tr key={row.stream_id} className="border-b">
          <td className="py-2">{row.stream_name || `Stream ${row.stream_id}`}</td>
          <td>{Math.floor(row.observed_seconds / 60)} min</td><td>{Math.round(row.stalled_seconds)} s</td><td>{row.failovers}</td>
          <td>{row.eligible && typeof row.score === 'number' ? `${Math.round(row.score * 100)}%` : 'Not enough data'}</td>
        </tr>)}</tbody>
      </table>{status.total_streams > status.streams.length && <p className="text-xs text-muted-foreground">Showing the {status.streams.length} most recently observed sources of {status.total_streams}.</p>}</div>}
  </div>
}

export function PlaybackStabilityScoring({ weights, onChange, recordingEnabled, loadError = false }) {
  return <div className="space-y-3 rounded-md border p-3">
    <div className="flex items-center gap-3">
      <Switch id="use_playback_stability" checked={weights.use_playback_stability === true}
        disabled={loadError || !recordingEnabled}
        onCheckedChange={checked => onChange('use_playback_stability', checked)} />
      <Label htmlFor="use_playback_stability">Use Playback Stability</Label>
    </div>
    <p className="text-xs text-muted-foreground">
      {loadError ? 'Recording settings could not load. Reload before changing this option.' : !recordingEnabled ? 'Enable Record Playback Stability in Settings → Monitoring first.' :
        'Apply a bounded deduction for observed stalls and failovers. Streams without sufficient history keep their existing quality score.'}
    </p>
    <Label htmlFor="playback_stability_weight">Playback Stability Weight</Label>
    <Input id="playback_stability_weight" type="number" min="0" max="1" step="0.05"
      disabled={loadError || !recordingEnabled || weights.use_playback_stability !== true}
      value={weights.playback_stability_weight ?? 0.15}
      onChange={e => onChange('playback_stability_weight', Math.max(0, Math.min(1, Number(e.target.value) || 0)))} />
    <p className="text-xs text-muted-foreground">0.15 limits the deduction to 15% of the quality score. Stable and unobserved sources receive no deduction.</p>
  </div>
}

export default function PlaybackStabilitySettings() {
  const [config, setConfig] = useState(null)
  const [status, setStatus] = useState(null)
  const [error, setError] = useState(null)
  const [saving, setSaving] = useState(false)
  const { toast } = useToast()
  useEffect(() => {
    const controller = new AbortController()
    playbackStabilityAPI.getConfig({ signal: controller.signal }).then(({ data }) => {
      if (!controller.signal.aborted) setConfig(data)
    }).catch(() => {
      if (!controller.signal.aborted) setError('Playback Stability settings could not load. Reload before saving.')
    })
    return () => controller.abort()
  }, [])
  useVisiblePolling(async signal => {
    try {
      const { data } = await playbackStabilityAPI.getStatus({ signal })
      if (!signal.aborted) setStatus(data)
    } catch {
      if (!signal.aborted) setStatus({ enabled: config?.enabled, error: 'Playback history could not load.', streams: [] })
    }
  }, 15000, Boolean(config))
  const save = async () => {
    setSaving(true)
    try {
      const { data } = await playbackStabilityAPI.updateConfig(config)
      setConfig(data)
      toast({ title: 'Playback Stability settings saved' })
      try {
        const result = await playbackStabilityAPI.getStatus()
        setStatus(result.data)
      } catch {
        setStatus({ enabled: data.enabled, error: 'Playback history could not load.', streams: [] })
      }
    } catch {
      toast({ title: 'Playback Stability settings could not save', variant: 'destructive' })
    } finally { setSaving(false) }
  }
  return <Card>
    <CardHeader><CardTitle>Playback Stability</CardTitle><CardDescription>Observe actual Dispatcharr playback without opening additional provider connections. Recording and profile scoring are off by default.</CardDescription></CardHeader>
    <CardContent className="space-y-4">
      {error && <p role="alert" className="text-destructive">{error}</p>}
      {!config && !error && <p>Loading playback settings…</p>}
      {config && <>
        <div className="flex items-center gap-3"><Switch id="record_playback_stability" checked={config.enabled} onCheckedChange={enabled => setConfig({ ...config, enabled })} />
          <Label htmlFor="record_playback_stability">Record Playback Stability</Label></div>
        <div className="grid gap-4 sm:grid-cols-2">
          <div className="space-y-2"><Label htmlFor="playback_poll_interval">Poll interval (seconds)</Label><Input id="playback_poll_interval" type="number" min="5" max="60" value={config.poll_interval_seconds} onChange={e => setConfig({ ...config, poll_interval_seconds: Number(e.target.value) })} /></div>
          <div className="space-y-2"><Label htmlFor="playback_retention">History retention (days)</Label><Input id="playback_retention" type="number" min="1" max="30" value={config.retention_days} onChange={e => setConfig({ ...config, retention_days: Number(e.target.value) })} /></div>
        </div>
        <p className="text-sm text-muted-foreground">Scoring is enabled separately for each profile in Stream Checking → Stream Quality Scoring. Normal session endings and API outages are not counted as failures.</p>
        <PlaybackStabilityHistory status={status} />
        <div className="flex justify-end"><Button onClick={save} disabled={saving}>{saving ? 'Saving…' : 'Save Playback Stability'}</Button></div>
      </>}
    </CardContent>
  </Card>
}
