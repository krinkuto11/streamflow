import { Badge } from '@/components/ui/badge.jsx'
import { Accordion, AccordionContent, AccordionItem, AccordionTrigger } from '@/components/ui/accordion.jsx'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table.jsx'
import { preflightMeasurementTime } from '@/lib/preflight-stream-details.js'

const measurementLabels = {
  checked: 'Freshly checked', reused: 'Known good reused', skipped: 'Other skipped', unknown: 'Not recorded',
}

export function ChangelogPreflightStreams({ rows }) {
  return <Table>
    <TableHeader><TableRow>
      <TableHead>Stream / Provider</TableHead><TableHead>Measurement</TableHead>
      <TableHead>Result / Reason</TableHead><TableHead>Resolution</TableHead><TableHead>Bitrate</TableHead>
    </TableRow></TableHeader>
    <TableBody>{rows.map((stream, index) => <TableRow key={stream.stream_id ?? index}>
      <TableCell className="min-w-40 max-w-80 whitespace-normal wrap-break-word">
        <p className="font-medium">{stream.stream_name || `Stream ${stream.stream_id ?? 'unknown'}`}</p>
        <p className="text-xs text-muted-foreground">{stream.provider_name || stream.m3u_account || 'Provider unavailable'} · ID {stream.stream_id ?? 'unknown'}</p>
      </TableCell>
      <TableCell className="min-w-36 whitespace-normal">
        <Badge variant="outline">{measurementLabels[stream.measurement]}</Badge>
        {stream.measurement === 'reused' && preflightMeasurementTime(stream.measurement_checked_at) &&
          <p className="mt-1 text-xs text-muted-foreground">Previous successful check: {preflightMeasurementTime(stream.measurement_checked_at)}</p>}
      </TableCell>
      <TableCell className="min-w-32 max-w-64 whitespace-normal wrap-break-word text-xs">
        <p>{stream.measurement === 'reused' ? 'Passed previously' : stream.status || 'Not recorded'}</p>
        {stream.reason && <p className="text-muted-foreground">{stream.reason.replaceAll('_', ' ')}</p>}
      </TableCell>
      <TableCell>{stream.resolution || 'N/A'}</TableCell><TableCell>{stream.bitrate || 'N/A'}</TableCell>
    </TableRow>)}</TableBody>
  </Table>
}

export function ChangelogPreflightSummary({ summary, showIdentity = true }) {
  if (!summary) return null
  return <div className="mt-3 min-w-0 space-y-2 border-t pt-3">
    {showIdentity && <p className="wrap-break-word text-base font-semibold">{summary.channelName}</p>}
    {showIdentity && summary.programName && <p className="wrap-break-word text-sm text-muted-foreground">{summary.programName}</p>}
    {summary.recorded ? <div className="flex flex-wrap gap-2" aria-label="Preflight measurements">
      <Badge variant="outline">Freshly checked: {summary.checked}</Badge>
      <Badge variant="secondary">Known good reused: {summary.reused}</Badge>
      <Badge variant="outline">Other skipped: {summary.skipped}</Badge>
    </div> : <p className="text-xs text-muted-foreground">Measurement reuse was not recorded for this check.</p>}
    {summary.truncated && <p className="text-xs text-muted-foreground">Counts cover the {summary.rows.length} shown results of {summary.total} candidates.</p>}
    <Accordion type="single" collapsible>
      <AccordionItem value="preflight-streams" className="border-0">
        <AccordionTrigger className="py-2 text-sm">Stream results ({summary.rows.length})</AccordionTrigger>
        <AccordionContent>
          {summary.rows.length ? <ChangelogPreflightStreams rows={summary.rows} />
            : <p className="text-sm text-muted-foreground">No stream results were recorded.</p>}
          <p className="mt-2 text-xs text-muted-foreground">Reused results describe an earlier successful check; they do not confirm current availability.</p>
        </AccordionContent>
      </AccordionItem>
    </Accordion>
  </div>
}
