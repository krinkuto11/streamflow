import { useEffect, useState } from 'react'
import { backupsAPI } from '@/services/api.js'
import { Alert, AlertDescription } from '@/components/ui/alert.jsx'
import { Button } from '@/components/ui/button.jsx'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card.jsx'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog.jsx'
import { Input } from '@/components/ui/input.jsx'
import { Label } from '@/components/ui/label.jsx'

const kinds = ['providers', 'channels', 'groups', 'channel_profiles']
const labels = { providers: 'Providers', channels: 'Channels', groups: 'Groups', channel_profiles: 'Dispatcharr channel profiles' }
const selectClass = 'flex h-10 w-full min-w-0 rounded-md border border-input bg-background px-3 py-2 text-sm'

function Assignments({ kind, rows, targets, selected, onChange, disabled }) {
  const [query, setQuery] = useState('')
  const [targetQuery, setTargetQuery] = useState('')
  const [offset, setOffset] = useState(0)
  const matching = rows.filter(row => `${row.name} ${row.id}`.toLowerCase().includes(query.toLowerCase()))
  const candidates = targets.filter(row => `${row.name} ${row.id}`.toLowerCase().includes(targetQuery.toLowerCase())).slice(0, 100)
  const missing = rows.filter(row => !selected?.[row.id]).length
  if (!rows.length) return null
  return <details className="rounded-md border p-4" open={kind === 'providers'}>
    <summary className="cursor-pointer font-medium">{labels[kind]} · {rows.length} saved · {missing} unresolved</summary>
    <div className="mt-4 space-y-4">
      <div className="grid gap-3 sm:grid-cols-2">
        <Input aria-label={`Find saved ${labels[kind].toLowerCase()}`} placeholder="Find saved name or ID" value={query} onChange={event => { setQuery(event.target.value); setOffset(0) }} />
        <Input aria-label={`Filter target ${labels[kind].toLowerCase()}`} placeholder="Filter target name or ID" value={targetQuery} onChange={event => setTargetQuery(event.target.value)} />
      </div>
      <p className="text-xs text-muted-foreground">Targets show up to 100 matches. Filter by name or ID to find more. Names are suggestions; verified entries also match an identity fingerprint.</p>
      {matching.slice(offset, offset + 20).map(row => {
        const chosen = targets.find(item => String(item.id) === selected?.[row.id])
        const options = chosen && !candidates.some(item => item.id === chosen.id) ? [chosen, ...candidates] : candidates
        return <div key={row.id} className="grid items-center gap-2 border-t pt-3 sm:grid-cols-2">
          <div className="min-w-0"><p className="break-words text-sm font-medium">{row.name} (#{row.id})</p><p className="text-xs text-muted-foreground">{row.status === 'verified' ? 'Identity verified' : row.status === 'moved' ? 'Identity found under a different ID' : 'Review this assignment'}</p></div>
          <select className={selectClass} value={selected?.[row.id] || ''} disabled={disabled} aria-label={`Target for ${kind} ${row.name} (${row.id})`} onChange={event => onChange(row.id, event.target.value)}>
            <option value="">Choose a target…</option><option value="skip">Do not restore this assignment</option>
            {options.map(target => <option key={target.id} value={String(target.id)}>{target.name || labels[kind]} (#{target.id})</option>)}
          </select>
        </div>
      })}
      <div className="flex flex-wrap items-center gap-3 text-sm">
        <Button variant="outline" size="sm" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - 20))}>Previous assignments</Button>
        <span>{matching.length ? offset + 1 : 0}–{Math.min(offset + 20, matching.length)} of {matching.length}</span>
        <Button variant="outline" size="sm" disabled={offset + 20 >= matching.length} onClick={() => setOffset(offset + 20)}>Next assignments</Button>
      </div>
    </div>
  </details>
}

export default function RestoreReview({ review, disabled, run, onRestart, error }) {
  const [connection, setConnection] = useState(null)
  const [selected, setSelected] = useState({})
  const [preview, setPreview] = useState(null)
  const report = review.report
  useEffect(() => {
    if (!connection) setConnection({ base_url: review.connection?.base_url || '', auth_mode: review.connection?.auth_mode || 'api_key', username: review.connection?.username || '', api_key: '', password: '' })
  }, [review.connection])
  useEffect(() => {
    if (report) setSelected(Object.fromEntries(kinds.map(kind => [kind, Object.fromEntries(report.entities[kind].map(row => [row.id, row.suggested ? String(row.suggested) : '']))])))
    setPreview(null)
  }, [report?.token])
  const unresolved = !report || kinds.some(kind => report.entities[kind].some(row => !selected[kind]?.[row.id]))
  const mappings = () => Object.fromEntries(kinds.map(kind => [kind, Object.fromEntries(Object.entries(selected[kind] || {}).map(([key, value]) => [key, value === 'skip' ? null : Number(value)]))]))
  return <Card>
    <CardHeader><CardTitle>Review restored Dispatcharr assignments</CardTitle><CardDescription>StreamFlow work is paused until these assignments are checked and confirmed. Stored automation preferences are retained.</CardDescription></CardHeader>
    <CardContent className="space-y-4">
      <Alert><AlertDescription>Provider IDs, channel IDs and stream IDs can belong to different objects on another Dispatcharr installation. Compare the target server before resuming.</AlertDescription></Alert>
      <details className="rounded-md border p-4"><summary className="cursor-pointer font-medium">Target Dispatcharr connection</summary>
        {connection && <div className="mt-4 grid gap-3 sm:grid-cols-2">
          <div className="space-y-2"><Label htmlFor="restore-target-url">Target server URL</Label><Input id="restore-target-url" value={connection.base_url} disabled={disabled || review.base_url_managed_externally} onChange={event => setConnection({ ...connection, base_url: event.target.value })} /></div>
          <div className="space-y-2"><Label htmlFor="restore-auth-mode">Authentication</Label><select id="restore-auth-mode" className={selectClass} value={connection.auth_mode} disabled={disabled} onChange={event => setConnection({ ...connection, auth_mode: event.target.value })}><option value="api_key">API key</option><option value="credentials">Username and password</option></select></div>
          {connection.auth_mode === 'api_key' ? <div className="space-y-2"><Label htmlFor="restore-api-key">Target API key</Label><Input id="restore-api-key" type="password" autoComplete="new-password" value={connection.api_key} disabled={disabled || review.connection?.api_key_managed_externally} placeholder="Blank keeps the stored key" onChange={event => setConnection({ ...connection, api_key: event.target.value })} /></div> : <>
            <div className="space-y-2"><Label htmlFor="restore-username">Target username</Label><Input id="restore-username" value={connection.username} disabled={disabled} onChange={event => setConnection({ ...connection, username: event.target.value })} /></div>
            <div className="space-y-2"><Label htmlFor="restore-password">Target password</Label><Input id="restore-password" type="password" autoComplete="new-password" value={connection.password} disabled={disabled || review.connection?.password_managed_externally} placeholder="Blank keeps the stored password" onChange={event => setConnection({ ...connection, password: event.target.value })} /></div>
          </>}
          <p className="text-xs text-muted-foreground sm:col-span-2">External credentials and addresses stay managed in the container template. Connection changes here do not start checks or inventory refresh workers.</p>
          <Button variant="outline" disabled={disabled} onClick={() => run(async () => { const { base_url, ...settings } = connection; await backupsAPI.saveRestoreConnection(review.base_url_managed_externally ? settings : connection); setConnection({ ...connection, api_key: '', password: '' }) }, 'Target connection saved. Compare again.')}>Save target connection</Button>
        </div>}
      </details>
      <Button disabled={disabled} onClick={() => run(() => backupsAPI.compareRestore(), 'Comparing the current Dispatcharr inventory…')}>Compare Dispatcharr assignments</Button>
      {report && <>
        {report.legacy_inventory && <Alert><AlertDescription>This older backup has no identity snapshot. Confirm configuration assignments manually. Unverified measurement history will not be attached to live channels or streams.</AlertDescription></Alert>}
        {kinds.map(kind => <Assignments key={kind} kind={kind} rows={report.entities[kind]} targets={report.targets[kind]} selected={selected[kind]} disabled={disabled} onChange={(key, value) => { setSelected(previous => ({ ...previous, [kind]: { ...previous[kind], [key]: value } })); setPreview(null) }} />)}
        <p className="text-sm text-muted-foreground">{report.history_note} Skipped provider restrictions are removed, never changed into an unrestricted provider selection.</p>
        <Button disabled={disabled || unresolved} onClick={() => run(async () => { const chosen = mappings(); const response = await backupsAPI.previewRestoreMappings(report.token, chosen); setPreview({ ...response.data.data, mappings: chosen, token: report.token }) })}>Preview mapped restore</Button>
      </>}
      <Dialog open={Boolean(preview)} onOpenChange={open => { if (!open && !disabled) setPreview(null) }}><DialogContent>
        <DialogHeader><DialogTitle>Confirm restored assignments?</DialogTitle><DialogDescription>StreamFlow verifies the live inventory again, creates a safety backup, applies these assignments and restarts. Saved automatic services may resume afterwards.</DialogDescription></DialogHeader>
        {error && <Alert variant="destructive"><AlertDescription>{error}</AlertDescription></Alert>}
        {preview && <div className="space-y-3 text-sm">
          <p>Skipped assignments: {preview.skipped_assignments}</p>
          <p>Configurations with an off or disabled scope after remapping: {preview.disabled_configurations || 0}</p>
          {Object.entries(preview.history).map(([key, value]) => <p key={key}>{key === 'stream_telemetry' ? 'Quality measurements' : 'Playback observations'}: {value.kept} kept · {value.removed} removed because identity is unverified.</p>)}
          <p>{preview.monitoring_history_kept ? 'Verified monitoring snapshots remain stopped.' : 'Monitoring snapshots and cached session references are removed because identifiers or identities changed.'}</p>
          <p>The original backup remains available for download.</p>
        </div>}
        <DialogFooter><Button variant="outline" disabled={disabled} onClick={() => setPreview(null)}>Cancel</Button><Button disabled={disabled} onClick={() => run(async () => { const response = await backupsAPI.confirmRestoreMappings(preview.token, preview.mappings); setPreview(null); onRestart(response.data.data) })}>Confirm assignments and restart</Button></DialogFooter>
      </DialogContent></Dialog>
    </CardContent>
  </Card>
}
