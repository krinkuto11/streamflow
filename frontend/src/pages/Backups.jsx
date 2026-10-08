import { useEffect, useRef, useState } from 'react'
import { Archive, Download, Loader2, RefreshCw, Trash2, Upload } from 'lucide-react'
import { Link } from 'react-router-dom'
import { backupsAPI } from '@/services/api.js'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card.jsx'
import { Button } from '@/components/ui/button.jsx'
import { Input } from '@/components/ui/input.jsx'
import { Label } from '@/components/ui/label.jsx'
import { Switch } from '@/components/ui/switch.jsx'
import { Alert, AlertDescription } from '@/components/ui/alert.jsx'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog.jsx'
import { createSequentialPoller } from '@/lib/sequential-poller.js'
import RestoreReview from '@/components/backups/RestoreReview.jsx'

const selectClass = 'flex h-10 w-full rounded-md border border-input bg-background px-3 py-2 text-sm'
const weekdays = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']
const date = value => value ? new Date(typeof value === 'number' ? value * 1000 : value).toLocaleString() : '—'
const size = value => `${(value / 1024 / 1024).toFixed(1)} MB`
const message = error => error?.response?.data?.error || error?.message || 'The request failed. Please try again.'
const active = operation => ['running', 'restarting'].includes(operation?.state)

export default function Backups() {
  const [status, setStatus] = useState(null)
  const [config, setConfig] = useState(null)
  const [includeHistory, setIncludeHistory] = useState(true)
  const [error, setError] = useState(null)
  const [notice, setNotice] = useState(null)
  const [working, setWorking] = useState(false)
  const [preview, setPreview] = useState(null)
  const [deleteTarget, setDeleteTarget] = useState(null)
  const [restarting, setRestarting] = useState(false)
  const restartStarted = useRef(null)
  const fileInput = useRef(null)
  const dirty = useRef(false)
  const historyInitialized = useRef(false)

  const refresh = async signal => {
    const response = await backupsAPI.getStatus({ signal })
    const data = response.data.data
    if (signal?.aborted) return
    setStatus(data)
    setConfig(previous => {
      if (!previous || !dirty.current) return data.config
      return previous
    })
    if (!historyInitialized.current) { setIncludeHistory(data.config.include_history); historyInitialized.current = true }
    if (restartStarted.current) {
      const result = data.restore_result
      if (result && new Date(result.finished_at).getTime() >= restartStarted.current) {
        restartStarted.current = null
        if (result.status === 'restored') window.location.assign('/backups')
        else {
          setRestarting(false)
          setError(result.message || 'Restore failed; the previous configuration was retained.')
        }
      } else if (data.operation?.state === 'failed') {
        restartStarted.current = null
        setRestarting(false)
        setError(data.operation.error)
      }
    }
  }

  useEffect(() => {
    const poller = createSequentialPoller({
      intervalMs: 5000,
      poll: async signal => {
        try { await refresh(signal) }
        catch (err) { if (!signal.aborted && !restartStarted.current) setError(message(err)) }
        return true
      },
    })
    poller.start()
    return () => poller.stop()
  }, [])

  const run = async (action, success) => {
    setWorking(true); setError(null); setNotice(null)
    try {
      await action()
      if (success) setNotice(success)
      await refresh()
    } catch (err) { setError(message(err)) }
    finally { setWorking(false) }
  }
  const change = (key, value) => { dirty.current = true; setConfig(previous => ({ ...previous, [key]: value })) }
  const disabled = working || restarting || active(status?.operation)

  const upload = event => {
    const file = event.target.files?.[0]
    event.target.value = ''
    if (!file) return
    run(async () => {
      const response = await backupsAPI.upload(file)
      setPreview(response.data.data)
    }, 'Backup uploaded and verified.')
  }
  const download = name => run(async () => {
    const response = await backupsAPI.download(name)
    const url = URL.createObjectURL(response.data)
    const link = document.createElement('a')
    link.href = url; link.download = name; link.click()
    setTimeout(() => URL.revokeObjectURL(url), 1000)
  })
  const restore = async () => {
    setWorking(true); setError(null)
    try {
      const response = await backupsAPI.restore(preview.name)
      restartStarted.current = response.data.data.started_at * 1000
      setPreview(null); setRestarting(true)
    } catch (err) { setError(message(err)) }
    finally { setWorking(false) }
  }
  const restartFromReview = operation => { restartStarted.current = operation.started_at * 1000; setRestarting(true) }

  return <div className="space-y-6">
    <div className="flex flex-wrap items-start justify-between gap-3">
      <div><h1 className="text-3xl font-bold tracking-tight">Backups</h1><p className="mt-2 text-muted-foreground">Save and restore your StreamFlow configuration.</p></div>
      <Button variant="outline" disabled={disabled} onClick={() => run(() => refresh())}><RefreshCw className="mr-2 h-4 w-4" />Refresh</Button>
    </div>
    {error && <Alert variant="destructive" role="alert"><AlertDescription>{error}</AlertDescription></Alert>}
    {notice && <Alert role="status"><AlertDescription>{notice}</AlertDescription></Alert>}
    {restarting && <Alert role="status"><AlertDescription>Restoring and restarting StreamFlow. This page will reconnect automatically. If it cannot reconnect, check the container log before restarting it.</AlertDescription></Alert>}
    {!status || !config ? <p role="status">Loading backups…</p> : <>
      {status.restore_review?.pending && <RestoreReview review={status.restore_review} disabled={disabled} run={run} onRestart={restartFromReview} />}
      <Card>
        <CardHeader><CardTitle>Create or upload</CardTitle><CardDescription>Includes settings, profiles, regex rules and stored connection credentials. Keep backup files private. Credentials supplied through container variables or secret files stay in your container configuration.</CardDescription></CardHeader>
        <CardContent className="space-y-4">
          <div><Label>Backup directory</Label><p className="mt-1 break-all rounded-md border bg-muted/40 px-3 py-2 font-mono text-sm">{status.directory}</p><p className="mt-2 text-sm text-muted-foreground">On Unraid, choose the host folder in the container template’s Backup storage path. Other installations can set BACKUP_DIR.</p></div>
          <div className="flex items-center gap-3"><Switch id="manual-history" checked={includeHistory} disabled={disabled} onCheckedChange={setIncludeHistory} /><Label htmlFor="manual-history">Include measurement history in this backup</Label></div>
          <p className="text-sm text-muted-foreground">History includes saved quality measurements, Analytics, playback stability and up to 1,000 recent monitoring samples per stream (100,000 total). Monitoring sessions are restored stopped. Screenshots, logs and live processes are excluded.</p>
          <div className="flex flex-wrap gap-3">
            <Button disabled={disabled} onClick={() => run(() => backupsAPI.create(includeHistory), 'Backup started. It will appear below when complete.')}><Archive className="mr-2 h-4 w-4" />Create backup</Button>
            <Button variant="outline" disabled={disabled} onClick={() => fileInput.current?.click()}><Upload className="mr-2 h-4 w-4" />Upload backup</Button>
            <input ref={fileInput} className="sr-only" type="file" accept=".zip,application/zip" aria-label="Choose backup file" onChange={upload} disabled={disabled} />
          </div>
          {active(status.operation) && <p role="status" className="flex items-center gap-2 text-sm"><Loader2 className="h-4 w-4 animate-spin" />{status.operation.state === 'restarting' ? 'Restarting…' : 'Backup operation in progress…'}</p>}
          {status.operation.state === 'failed' && <p className="text-sm text-destructive" role="alert">{status.operation.error}</p>}
          {status.restore_result && <p className="text-sm text-muted-foreground">Last restore: {status.restore_result.message} · {date(status.restore_result.finished_at)}</p>}
        </CardContent>
      </Card>
      <Card>
        <CardHeader><CardTitle>Automatic backups</CardTitle><CardDescription>Configure when backups run and how many files to keep. Automatic backups are off by default.</CardDescription></CardHeader>
        <CardContent className="space-y-4">
          <div className="flex items-center gap-3"><Switch id="backup-enabled" checked={config.enabled} disabled={disabled} onCheckedChange={value => change('enabled', value)} /><Label htmlFor="backup-enabled">Enable automatic backups</Label></div>
          <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-3">
            <div className="space-y-2"><Label htmlFor="backup-frequency">Frequency</Label><select id="backup-frequency" className={selectClass} value={config.frequency} disabled={disabled} onChange={event => change('frequency', event.target.value)}><option value="daily">Daily</option><option value="weekly">Weekly</option><option value="interval">Every N hours</option></select></div>
            {config.frequency === 'interval' ? <div className="space-y-2"><Label htmlFor="backup-interval">Interval (hours)</Label><Input id="backup-interval" type="number" min="1" max="168" value={config.interval_hours} disabled={disabled} onChange={event => change('interval_hours', Number(event.target.value))} /></div> : <div className="space-y-2"><Label htmlFor="backup-time">Time</Label><Input id="backup-time" type="time" value={config.time} disabled={disabled} onChange={event => change('time', event.target.value)} /></div>}
            {config.frequency === 'weekly' && <div className="space-y-2"><Label htmlFor="backup-weekday">Day</Label><select id="backup-weekday" className={selectClass} value={config.weekday} disabled={disabled} onChange={event => change('weekday', Number(event.target.value))}>{weekdays.map((day, index) => <option key={day} value={index}>{day}</option>)}</select></div>}
            <div className="space-y-2"><Label htmlFor="backup-timezone">Time zone</Label><Input id="backup-timezone" placeholder="Europe/Berlin" value={config.timezone} disabled={disabled} onChange={event => change('timezone', event.target.value)} /></div>
            <div className="space-y-2"><Label htmlFor="backup-retention">Backups to keep</Label><Input id="backup-retention" type="number" min="1" max="90" value={config.retention} disabled={disabled} onChange={event => change('retention', Number(event.target.value))} /></div>
          </div>
          <div className="flex items-center gap-3"><Switch id="scheduled-history" checked={config.include_history} disabled={disabled} onCheckedChange={value => change('include_history', value)} /><Label htmlFor="scheduled-history">Include measurement history in automatic backups</Label></div>
          <p className="text-sm text-muted-foreground">The retention limit applies to manual, scheduled and uploaded backups together. The latest three restore safety backups are kept separately.</p>
          <p className="text-sm text-muted-foreground">Next automatic backup: {config.enabled ? date(status.schedule.next_run_at) : 'Disabled'} · Last automatic backup: {date(status.schedule.last_success_at)}</p>
          {status.schedule.last_error && <p className="text-sm text-destructive">{status.schedule.last_error}</p>}
          <Button disabled={disabled} onClick={() => run(async () => { await backupsAPI.saveConfig(config); dirty.current = false }, 'Backup settings saved.')}>Save backup settings</Button>
        </CardContent>
      </Card>
      <Card>
        <CardHeader><CardTitle>Saved backups</CardTitle><CardDescription>Verify a backup before restoring. StreamFlow creates a safety backup of the current state and restarts. Wait for active checks and monitoring sessions to finish first.</CardDescription></CardHeader>
        <CardContent>
          {!status.backups.length ? <p className="text-sm text-muted-foreground">No backups yet.</p> : <div className="space-y-3">{status.backups.map(backup => <div key={backup.name} className="flex flex-wrap items-center justify-between gap-3 rounded-lg border p-4">
            <div className="min-w-0"><p className="break-all text-sm font-medium">{backup.name}</p><p className="mt-1 text-xs text-muted-foreground">{date(backup.created_at)} · {size(backup.size)} · {backup.kind === 'safety' ? 'Restore safety backup' : backup.version || 'Unknown version'} · {backup.valid ? backup.include_history ? 'With measurement history' : 'Configuration only' : 'Invalid manifest'}</p></div>
            <div className="flex flex-wrap gap-2">
              <Button variant="outline" size="sm" disabled={disabled} onClick={() => download(backup.name)} aria-label={`Download ${backup.name}`}><Download className="mr-2 h-4 w-4" />Download</Button>
              <Button variant="outline" size="sm" disabled={disabled || !backup.valid || !status.restart_supported} onClick={() => run(async () => { const response = await backupsAPI.inspect(backup.name); setPreview(response.data.data) })}>Verify & restore</Button>
              <Button variant="outline" size="sm" disabled={disabled} onClick={() => setDeleteTarget(backup.name)} aria-label={`Delete ${backup.name}`}><Trash2 className="h-4 w-4" /></Button>
            </div>
          </div>)}</div>}
        </CardContent>
      </Card>
    </>}
    <p className="text-sm"><Link className="text-primary underline" to="/">Back to dashboard or setup</Link></p>
    <Dialog open={Boolean(preview)} onOpenChange={open => { if (!open && !working) setPreview(null) }}>
      <DialogContent><DialogHeader><DialogTitle>Restore verified backup?</DialogTitle><DialogDescription>This replaces the current StreamFlow settings, regex rules, profiles and stored connections. StreamFlow saves a safety backup first, then restarts. Automatic work remains paused until Dispatcharr assignments are reviewed and confirmed. It does not restore Dispatcharr or resume monitoring sessions.</DialogDescription></DialogHeader>
        {preview && <div className="space-y-2 text-sm"><p className="break-all font-medium">{preview.name}</p><p>Created: {date(preview.created_at)} · Version: {preview.version}</p><p>{preview.include_history ? 'Includes measurement history.' : 'Configuration only: current measurement history will be removed.'}</p><dl className="grid grid-cols-2 gap-2">{Object.entries(preview.summary || {}).map(([key, value]) => <div key={key}><dt className="text-muted-foreground">{key.replaceAll('_', ' ')}</dt><dd>{value}</dd></div>)}</dl></div>}
        {error && <p role="alert" className="text-sm text-destructive">{error}</p>}
        <DialogFooter><Button variant="outline" disabled={working} onClick={() => setPreview(null)}>Cancel</Button><Button variant="destructive" disabled={working} onClick={restore}>Restore and restart</Button></DialogFooter>
      </DialogContent>
    </Dialog>
    <Dialog open={Boolean(deleteTarget)} onOpenChange={open => { if (!open && !working) setDeleteTarget(null) }}><DialogContent><DialogHeader><DialogTitle>Delete backup?</DialogTitle><DialogDescription>This permanently removes the selected backup file.</DialogDescription></DialogHeader><p className="break-all text-sm">{deleteTarget}</p><DialogFooter><Button variant="outline" disabled={working} onClick={() => setDeleteTarget(null)}>Cancel</Button><Button variant="destructive" disabled={working} onClick={() => run(async () => { await backupsAPI.delete(deleteTarget); setDeleteTarget(null) }, 'Backup deleted.')}>Delete backup</Button></DialogFooter></DialogContent></Dialog>
  </div>
}
