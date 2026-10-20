import { useEffect, useState } from 'react'
import { Accordion, AccordionContent, AccordionItem, AccordionTrigger } from '@/components/ui/accordion.jsx'
import { Badge } from '@/components/ui/badge.jsx'
import { Button } from '@/components/ui/button.jsx'
import { teamarrPreflightAPI } from '@/services/api.js'
import { preflightAction, preflightOrderLabels, preflightScore, preflightStreamStatus } from '@/lib/preflight-stream-details.js'

export function PreflightStreamDetails({ event }) {
  const inspectionId = event?.details?.inspection_id
  const [open, setOpen] = useState('')
  const [data, setData] = useState(null)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)
  const [retry, setRetry] = useState(0)

  useEffect(() => {
    if (!open || !inspectionId || data) return
    const controller = new AbortController()
    setLoading(true)
    setError('')
    teamarrPreflightAPI.getInspection(inspectionId, { signal: controller.signal })
      .then(response => { if (!controller.signal.aborted) setData(response.data) })
      .catch(err => {
        if (!controller.signal.aborted) setError(err.response?.status === 404
          ? 'These check details have expired or are no longer available.'
          : 'Could not load stream details. Please try again.')
      })
      .finally(() => { if (!controller.signal.aborted) setLoading(false) })
    return () => controller.abort()
  }, [open, inspectionId, data, retry])

  if (!event) return null
  if (!inspectionId) return event.type === 'preflight_completed' ? (
    <p className="mt-2 text-xs text-muted-foreground">Stream details are not available for this check.</p>
  ) : null

  return (
    <Accordion type="single" collapsible value={open} onValueChange={setOpen} className="mt-3 border-t border-border">
      <AccordionItem value="streams" className="border-0">
        <AccordionTrigger className="py-2 text-sm">Stream details</AccordionTrigger>
        <AccordionContent>
          {loading && <p role="status" className="text-sm text-muted-foreground">Loading stream details…</p>}
          {error && <div role="alert" className="flex flex-wrap items-center gap-2 text-sm">
            <p>{error}</p><Button type="button" size="sm" variant="outline" onClick={() => setRetry(value => value + 1)}>Retry</Button>
          </div>}
          {data && <div className="space-y-3">
            <div className="space-y-1 text-xs text-muted-foreground">
              <p>Checked {data.checked_at ? new Date(data.checked_at).toLocaleString() : 'at an unknown time'} · Profile: {data.profile_name || 'Unavailable'} · {data.total_streams} candidates</p>
              <p>Order: {preflightOrderLabels[data.priority_mode] || 'Unavailable'}{data.ordering_status === 'not_applied' ? ' · Order not applied' : ''}</p>
              <p>Ranks describe channel order at this check, not active playback. Previous measurements are labelled below.</p>
            </div>
            {data.truncated && <p role="status" className="text-sm text-amber-700 dark:text-amber-300">Showing {data.streams.length} of {data.total_streams} candidates. Details were limited in size.</p>}
            {data.streams.length === 0 && <p className="text-sm text-muted-foreground">No stream results were recorded.</p>}
            <ul className="space-y-2" aria-label="Checked streams">
              {data.streams.map((stream, index) => {
                const status = preflightStreamStatus(stream)
                return <li key={stream.stream_id ?? index} className="grid gap-3 rounded-md border border-border p-3 lg:grid-cols-[minmax(0,2fr)_minmax(0,2fr)_8rem]">
                  <div className="min-w-0 space-y-1">
                    <p className="break-words font-medium">{stream.stream_name || `Stream ${stream.stream_id ?? 'unknown'}`}</p>
                    <p className="break-words text-xs text-muted-foreground">{stream.provider_name || (stream.provider_id ? `Provider ${stream.provider_id}` : 'Provider unavailable')} · ID {stream.stream_id ?? 'unknown'}</p>
                    <div className="flex flex-wrap gap-1"><Badge variant={status.variant}>{status.label}</Badge><Badge variant="outline">{preflightAction(stream)}</Badge></div>
                    {stream.reason && <p className="break-words text-xs text-muted-foreground">{stream.reason.replaceAll('_', ' ')}</p>}
                    {stream.measurement_source !== 'current_check' && <p className="text-xs text-muted-foreground">{stream.measurement_source === 'previous_measurement' ? 'Previous measurement · not rechecked' : 'No current measurement'}</p>}
                  </div>
                  <dl className="grid grid-cols-2 gap-x-4 gap-y-1 text-xs">
                    <div><dt className="text-muted-foreground">Resolution</dt><dd>{stream.resolution || 'N/A'}</dd></div>
                    <div><dt className="text-muted-foreground">Frame rate</dt><dd>{stream.fps || 'N/A'}</dd></div>
                    <div><dt className="text-muted-foreground">Bitrate</dt><dd>{stream.bitrate || 'N/A'}</dd></div>
                    <div><dt className="text-muted-foreground">Video / Audio</dt><dd className="break-words">{stream.video_codec || 'N/A'} / {stream.audio_codec || 'N/A'}</dd></div>
                  </dl>
                  <div className="text-xs"><p className="text-muted-foreground">Quality score</p><p className="font-mono text-base font-semibold">{preflightScore(stream.score)}</p>{stream.provider_rank && <p className="mt-1 text-muted-foreground">Playlist priority {stream.provider_rank}</p>}</div>
                </li>
              })}
            </ul>
          </div>}
        </AccordionContent>
      </AccordionItem>
    </Accordion>
  )
}
