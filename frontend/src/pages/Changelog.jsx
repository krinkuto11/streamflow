import { useState, useEffect, useMemo } from 'react'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card.jsx'
import { Badge } from '@/components/ui/badge.jsx'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table.jsx'
import { Accordion, AccordionContent, AccordionItem, AccordionTrigger } from '@/components/ui/accordion.jsx'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select.jsx'
import { useToast } from '@/hooks/use-toast.js'
import { changelogAPI } from '@/services/api.js'
import { Loader2, CheckCircle2, AlertCircle, Activity, ChevronDown } from 'lucide-react'

function formatTimestamp(timestamp) {
  const date = new Date(timestamp)
  const now = new Date()
  const diffMs = now - date
  const diffMins = Math.floor(diffMs / 60000)
  const diffHours = Math.floor(diffMs / 3600000)
  const diffDays = Math.floor(diffMs / 86400000)

  if (diffMins < 1) return 'Just now'
  if (diffMins < 60) return `${diffMins}m ago`
  if (diffHours < 24) return `${diffHours}h ago`
  if (diffDays < 7) return `${diffDays}d ago`

  return date.toLocaleDateString() + ' ' + date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
}

function getActionLabel(action) {
  switch (action) {
    case 'playlist_update_match':
      return 'Playlist Update & Match'
    case 'global_check':
      return 'Global Check'
    case 'single_channel_check':
      return 'Single Channel Check'
    case 'batch_stream_check':
      return 'Batch Stream Check'
    case 'playlist_refresh':
      return 'Playlist Refresh'
    case 'streams_assigned':
      return 'Streams Assigned'
    case 'stream_check':
      return 'Stream Check'
    case 'automation_run':
      return 'Automation Run'
    default:
      return action
  }
}

function getActionIcon(action) {
  switch (action) {
    case 'playlist_update_match':
    case 'global_check':
    case 'single_channel_check':
      return <CheckCircle2 className="h-4 w-4" />
    case 'playlist_refresh':
      return <Activity className="h-4 w-4" />
    case 'automation_run':
      return <Activity className="h-4 w-4" />
    default:
      return <AlertCircle className="h-4 w-4" />
  }
}

function getActionColor(action) {
  switch (action) {
    case 'playlist_update_match':
      return 'bg-blue-500/10 text-blue-500 border-blue-500/20'
    case 'global_check':
      return 'bg-green-500/10 text-green-500 border-green-500/20'
    case 'single_channel_check':
      return 'bg-purple-500/10 text-purple-500 border-purple-500/20'
    case 'batch_stream_check':
      return 'bg-cyan-500/10 text-cyan-500 border-cyan-500/20'
    case 'playlist_refresh':
      return 'bg-orange-500/10 text-orange-500 border-orange-500/20'
    case 'automation_run':
      return 'bg-indigo-500/10 text-indigo-500 border-indigo-500/20'
    default:
      return 'bg-gray-500/10 text-gray-500 border-gray-500/20'
  }
}

function ChannelItem({ item, groupType, groupIndex, itemIndex }) {
  const [logoError, setLogoError] = useState(false)
  const channelLabel = item.channel_name
  const channelStats = groupType === 'check' && item.stats ?
    `Avg ${item.stats.avg_resolution || 'N/A'}, ${item.stats.avg_bitrate || 'N/A'}` :
    null

  return (
    <AccordionItem key={itemIndex} value={`channel-${groupIndex}-${itemIndex}`}>
      <AccordionTrigger className="hover:no-underline py-2">
        <div className="flex items-center gap-2">
          {item.logo_url && !logoError && (
            <img
              src={item.logo_url}
              alt={channelLabel}
              className="w-6 h-6 object-contain"
              onError={() => setLogoError(true)}
            />
          )}
          <span className="text-sm font-medium">{channelLabel}</span>
          {channelStats && (
            <span className="text-xs text-muted-foreground ml-2">: {channelStats}</span>
          )}
        </div>
      </AccordionTrigger>
      <AccordionContent>
        <div className="space-y-1 pl-4">
          {groupType === 'update_match' && item.streams && (
            <ul className="list-none text-sm space-y-1">
              {item.streams.map((stream, idx) => (
                <li key={stream.id || stream.stream_id || `stream-${itemIndex}-${idx}`} className="text-muted-foreground">
                  - {stream.name || stream.stream_name || `Stream ${stream.id || stream.stream_id}`}
                </li>
              ))}
            </ul>
          )}

          {groupType === 'check' && item.stats && item.stats.stream_details && item.stats.stream_details.length > 0 && (
            <div className="rounded-md border mt-2">
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Stream Name</TableHead>
                    <TableHead>M3U Account</TableHead>
                    <TableHead>Resolution</TableHead>
                    <TableHead>Framerate</TableHead>
                    <TableHead>Bitrate</TableHead>
                    <TableHead>Codec</TableHead>
                    {item.stats.stream_details.some(s => s.score !== undefined && s.score !== null) && (
                      <TableHead>Score</TableHead>
                    )}
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {[...item.stats.stream_details]
                    .sort((a, b) => {
                      const scoreA = a.score !== undefined && a.score !== null ? a.score : -Infinity
                      const scoreB = b.score !== undefined && b.score !== null ? b.score : -Infinity
                      return scoreB - scoreA
                    })
                    .map((streamDetail, idx) => (
                      <TableRow key={streamDetail.stream_id || `detail-${idx}`}>
                        <TableCell className="font-medium">{streamDetail.stream_name || 'Unknown'}</TableCell>
                        <TableCell>{streamDetail.m3u_account || 'N/A'}</TableCell>
                        <TableCell>
                          <div className="flex items-center gap-2">
                            <span>{streamDetail.resolution || 'N/A'}</span>
                            {streamDetail.hdr_format && (
                              <Badge variant="outline" className="bg-blue-500/10 text-blue-500 border-blue-500/20 text-xs px-2 py-0 h-5">
                                {streamDetail.hdr_format}
                              </Badge>
                            )}
                          </div>
                        </TableCell>
                        <TableCell>{streamDetail.fps || 'N/A'}</TableCell>
                        <TableCell>{streamDetail.bitrate || 'N/A'}</TableCell>
                        <TableCell>{streamDetail.video_codec || 'N/A'}</TableCell>
                        {item.stats.stream_details.some(s => s.score !== undefined && s.score !== null) && (
                          <TableCell>{streamDetail.score !== undefined && streamDetail.score !== null ? streamDetail.score.toFixed(2) : 'N/A'}</TableCell>
                        )}
                      </TableRow>
                    ))}
                </TableBody>
              </Table>
            </div>
          )}
        </div>
      </AccordionContent>
    </AccordionItem>
  )
}

function getStepIcon(name) {
  switch (name.toLowerCase()) {
    case 'playlist refresh': return <Activity className="h-4 w-4" />
    case 'validation': return <AlertCircle className="h-4 w-4" />
    case 'assignment': return <CheckCircle2 className="h-4 w-4" />
    case 'quality check': return <Activity className="h-4 w-4" />
    default: return <Activity className="h-4 w-4" />
  }
}

function getStepColor(status, name = '') {
  const stepName = name.toLowerCase()
  if (stepName === 'playlist refresh') return 'text-orange-500 bg-orange-500/5 border-orange-500/10'
  if (stepName === 'assignment') return 'text-green-500 bg-green-500/5 border-green-500/10'
  if (stepName === 'quality check') return 'text-indigo-500 bg-indigo-500/5 border-indigo-500/10'
  if (stepName === 'validation') return 'text-purple-500 bg-purple-500/5 border-purple-500/10'

  if (status === 'success') return 'text-green-500 bg-green-500/5 border-green-500/10'
  if (status === 'failed') return 'text-destructive bg-destructive/5 border-destructive/10'
  if (status === 'skipped') return 'text-muted-foreground bg-muted/5 border-muted/10'
  return 'text-blue-500 bg-blue-500/5 border-blue-500/10'
}

function hasStepDetails(name, details) {
  return (name === 'Validation' && details.removed_count > 0) ||
    (name === 'Assignment' && details.added_count > 0) ||
    (name === 'Playlist Refresh' && details.accounts && details.accounts.length > 0) ||
    (name === 'Quality Check' && (details.dead_streams_count > 0 || details.revived_streams_count > 0 || details.skipped_streams_count > 0 || details.checked_streams?.length > 0))
}

function StepHeader({ step, isExpanded, onToggle }) {
  const { step: name, status, details } = step
  const hasDetails = hasStepDetails(name, details)

  return (
    <div
      className={`rounded-lg border transition-all ${getStepColor(status, name)} flex items-center justify-between cursor-pointer hover:bg-black/5 dark:hover:bg-white/5 transition-colors p-3 ${isExpanded ? 'ring-1 ring-current/30 shadow-inner' : ''}`}
      onClick={() => hasDetails && onToggle()}
    >
      <div className="flex items-center gap-3 font-medium">
        <div className={`p-1.5 rounded-md ${status === 'success' ? 'bg-current/10 text-current' : 'bg-muted text-muted-foreground'} border border-current/20`}>
          {getStepIcon(name)}
        </div>
        <span className="text-sm">{name}</span>
      </div>
      <div className="flex items-center gap-2">
        <Badge variant="outline" className={`${getStepColor(status, name)} border-current font-bold uppercase text-[9px] px-1.5 py-0 h-4`}>
          {status}
        </Badge>
        {hasDetails && (
          <ChevronDown className={`h-3 w-3 transition-transform duration-200 ${isExpanded ? 'rotate-180' : ''}`} />
        )}
      </div>
    </div>
  )
}

function StepContent({ step }) {
  const { step: name, details } = step

  return (
    <div className={`mt-2 p-3 rounded-lg border border-current/10 bg-black/5 dark:bg-white/5 animate-in slide-in-from-top-1 duration-200 overflow-hidden ${getStepColor(step.status, name)}`}>
      {name === 'Playlist Refresh' && details.accounts && (
        <div className="text-xs space-y-1 mt-1 opacity-90">
          <p className="font-semibold text-muted-foreground">Updated accounts:</p>
          <ul className="list-none pl-1 space-y-1">
            {details.accounts.map((acc, idx) => (
              <li key={idx} className="flex items-center gap-2">
                <div className="w-1 h-1 rounded-full bg-current opacity-40" />
                <span className="truncate">{acc.name}</span>
              </li>
            ))}
          </ul>
        </div>
      )}

      {name === 'Validation' && details.removed_count > 0 && (
        <div className="text-xs space-y-1 mt-1 opacity-90">
          <p className="font-semibold text-muted-foreground">Removed {details.removed_count} non-matching streams:</p>
          <div className="max-h-32 overflow-y-auto pr-1 custom-scrollbar">
            <ul className="list-none pl-1 space-y-1">
              {details.streams.map((s, idx) => (
                <li key={idx} className="flex items-center gap-2">
                  <div className="w-1 h-1 rounded-full bg-destructive/40" />
                  <span className="truncate">{s.name || `Stream ${s.id}`}</span>
                </li>
              ))}
            </ul>
          </div>
        </div>
      )}

      {name === 'Assignment' && details.added_count > 0 && (
        <div className="text-xs space-y-1 mt-1 opacity-90">
          <p className="font-semibold text-muted-foreground">Added {details.added_count} new streams:</p>
          <div className="max-h-32 overflow-y-auto pr-1 custom-scrollbar">
            <ul className="list-none pl-1 space-y-1">
              {details.streams.map((s, idx) => (
                <li key={idx} className="flex items-center gap-2 animate-in fade-in duration-300">
                  <div className="w-1 h-1 rounded-full bg-green-500/40" />
                  <span className="truncate">{s.stream_name || s.name || `Stream ${s.stream_id || s.id}`}</span>
                </li>
              ))}
            </ul>
          </div>
        </div>
      )}

      {name === 'Quality Check' && (
        <div className="space-y-3 mt-1">
          {details.dead_streams_count > 0 && (
            <div className="text-xs space-y-1 opacity-90">
              <p className="font-semibold text-destructive">Dead Streams ({details.dead_streams_count}):</p>
              <div className="max-h-32 overflow-y-auto pr-1 custom-scrollbar">
                <ul className="list-none pl-1 space-y-1">
                  {details.dead_streams.map((s, idx) => (
                    <li key={idx} className="flex items-center gap-2">
                      <div className="w-1 h-1 rounded-full bg-destructive" />
                      <span className="truncate text-destructive">{s.name || `Stream ${s.id}`}</span>
                    </li>
                  ))}
                </ul>
              </div>
            </div>
          )}

          {details.revived_streams_count > 0 && (
            <div className="text-xs space-y-1 opacity-90">
              <p className="font-semibold text-green-500">Revived Streams ({details.revived_streams_count}):</p>
              <div className="max-h-32 overflow-y-auto pr-1 custom-scrollbar">
                <ul className="list-none pl-1 space-y-1">
                  {details.revived_streams.map((s, idx) => (
                    <li key={idx} className="flex items-center gap-2">
                      <div className="w-1 h-1 rounded-full bg-green-500" />
                      <span className="truncate text-green-500">{s.name || `Stream ${s.id}`}</span>
                    </li>
                  ))}
                </ul>
              </div>
            </div>
          )}

          {details.skipped_streams_count > 0 && (
            <div className="text-xs space-y-1 opacity-90">
              <p className="font-semibold text-muted-foreground">Skipped Streams (Grace Period) ({details.skipped_streams_count}):</p>
              <div className="max-h-32 overflow-y-auto pr-1 custom-scrollbar">
                <ul className="list-none pl-1 space-y-1">
                  {details.skipped_streams.map((s, idx) => (
                    <li key={idx} className="flex items-center gap-2">
                      <div className="w-1 h-1 rounded-full bg-muted-foreground/50" />
                      <span className="truncate text-muted-foreground">{s.name || `Stream ${s.id}`}</span>
                    </li>
                  ))}
                </ul>
              </div>
            </div>
          )}

          {details.checked_streams && details.checked_streams.length > 0 && (
            <div className="text-xs space-y-1 opacity-90 pt-1">
              <p className="font-semibold text-muted-foreground">Analyzed Streams ({details.checked_streams.length}):</p>
              <div className="rounded-md border bg-card/50 overflow-hidden">
                <Table>
                  <TableHeader>
                    <TableRow className="hover:bg-transparent border-b border-muted/50">
                      <TableHead className="h-7 text-[10px] uppercase font-bold text-muted-foreground w-[30%]">Stream Name</TableHead>
                      <TableHead className="h-7 text-[10px] uppercase font-bold text-muted-foreground">M3U</TableHead>
                      <TableHead className="h-7 text-[10px] uppercase font-bold text-muted-foreground">Resolution</TableHead>
                      <TableHead className="h-7 text-[10px] uppercase font-bold text-muted-foreground">Rate</TableHead>
                      <TableHead className="h-7 text-[10px] uppercase font-bold text-muted-foreground">Bitrate</TableHead>
                      <TableHead className="h-7 text-[10px] uppercase font-bold text-muted-foreground">Codec</TableHead>
                      <TableHead className="h-7 text-[10px] uppercase font-bold text-muted-foreground text-right">Score</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {details.checked_streams.map((s, idx) => (
                      <TableRow key={idx} className="hover:bg-muted/30 border-b border-muted/30 last:border-0 h-8">
                        <TableCell className="py-1 font-medium truncate max-w-[150px]" title={s.stream_name}>
                          {s.stream_name || `Stream ${s.stream_id}`}
                        </TableCell>
                        <TableCell className="py-1 text-muted-foreground truncate max-w-[80px]" title={s.m3u_account}>
                          {s.m3u_account || '-'}
                        </TableCell>
                        <TableCell className="py-1">
                          <div className="flex items-center gap-1.5">
                            <span>{s.resolution || '-'}</span>
                            {s.hdr_format && (
                              <Badge variant="outline" className="text-[9px] px-1 py-0 h-3.5 border-blue-500/30 text-blue-500">HDR</Badge>
                            )}
                          </div>
                        </TableCell>
                        <TableCell className="py-1">
                          {s.fps || '-'}
                        </TableCell>
                        <TableCell className="py-1">
                          {s.bitrate || '-'}
                        </TableCell>
                        <TableCell className="py-1 text-muted-foreground">
                          {s.video_codec || '-'}
                        </TableCell>
                        <TableCell className="py-1 text-right font-mono text-xs">
                          {s.score !== undefined && s.score !== null ? s.score.toFixed(2) : '-'}
                        </TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  )
}

function AutomationChannel({ channel, cIdx }) {
  const [expandedStepIdx, setExpandedStepIdx] = useState(null)

  return (
    <div className="space-y-4 animate-in fade-in slide-in-from-left-2 duration-300" style={{ animationDelay: `${cIdx * 50}ms` }}>
      <div className="flex items-center gap-4">
        <div className="relative group">
          {channel.logo_url ? (
            <img src={channel.logo_url} alt={channel.channel_name} className="w-10 h-10 object-contain rounded-lg bg-white dark:bg-card p-1 border shadow-sm group-hover:scale-110 transition-transform" />
          ) : (
            <div className="w-10 h-10 rounded-lg bg-muted flex items-center justify-center text-xs font-bold border group-hover:scale-110 transition-transform">
              {channel.channel_name.substring(0, 2).toUpperCase()}
            </div>
          )}
          <div className="absolute -bottom-1 -right-1 w-3 h-3 bg-green-500 border-2 border-background rounded-full" />
        </div>
        <div className="flex flex-col gap-0.5">
          <span className="font-bold text-base tracking-tight">{channel.channel_name}</span>
          <div className="flex items-center gap-2">
            <Badge variant="outline" className="text-[9px] uppercase font-bold tracking-wider px-1.5 py-0 h-4 bg-primary/5 border-primary/20 text-primary/80">
              {channel.profile_name} Profile
            </Badge>
          </div>
        </div>
      </div>

      <div className="pl-0 md:pl-14 space-y-3">
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-3">
          {channel.steps.map((step, sIdx) => (
            <StepHeader
              key={sIdx}
              step={step}
              isExpanded={expandedStepIdx === sIdx}
              onToggle={() => setExpandedStepIdx(expandedStepIdx === sIdx ? null : sIdx)}
            />
          ))}
        </div>

        {expandedStepIdx !== null && (
          <div className="w-full animate-in zoom-in-95 duration-200">
            <StepContent step={channel.steps[expandedStepIdx]} />
          </div>
        )}
      </div>
    </div>
  )
}

function ChangelogEntry({ entry }) {
  const { timestamp, action, details, subentries } = entry
  const hasSubentries = subentries && subentries.length > 0

  return (
    <Card className={`overflow-hidden shadow-md transition-shadow hover:shadow-lg dark:bg-card/40 ${action === 'automation_run' ? 'border-2 border-blue-500 dark:border-green-500' : 'border-muted/60'}`}>
      <CardHeader className="pb-3 bg-muted/10">
        <div className="flex items-start justify-between">
          <div className="flex items-center gap-2">
            <Badge variant="outline" className={`${getActionColor(action)} border-current font-bold px-2 py-0.5`}>
              <div className="bg-current/10 p-1 rounded-sm mr-2 inline-flex">
                {getActionIcon(action)}
              </div>
              <span className="text-[11px] uppercase tracking-wider">{getActionLabel(action)}</span>
            </Badge>
          </div>
          <span className="text-[11px] font-medium text-muted-foreground bg-muted/30 px-2 py-1 rounded-md">{formatTimestamp(timestamp)}</span>
        </div>

        <div className="grid grid-cols-2 md:grid-cols-4 gap-4 mt-3 pt-3 border-t">
          {details.total_channels !== undefined && (
            <div>
              <p className="text-[10px] text-muted-foreground uppercase tracking-tight font-bold">Total Channels</p>
              <p className="text-lg font-bold">{details.total_channels}</p>
            </div>
          )}
          {details.successful_checks !== undefined && (
            <div>
              <p className="text-[10px] text-muted-foreground uppercase tracking-tight font-bold">Successful</p>
              <p className="text-lg font-bold text-green-500">{details.successful_checks}</p>
            </div>
          )}
          {details.failed_checks !== undefined && details.failed_checks > 0 && (
            <div>
              <p className="text-[10px] text-muted-foreground uppercase tracking-tight font-bold">Failed</p>
              <p className="text-lg font-bold text-destructive">{details.failed_checks}</p>
            </div>
          )}
          {details.total_streams !== undefined && (
            <div>
              <p className="text-[10px] text-muted-foreground uppercase tracking-tight font-bold">Total Streams</p>
              <p className="text-lg font-bold">{details.total_streams}</p>
            </div>
          )}
          {details.num_streams !== undefined && (
            <div>
              <p className="text-[10px] text-muted-foreground uppercase tracking-tight font-bold">Total Streams</p>
              <p className="text-lg font-bold">{details.num_streams}</p>
            </div>
          )}
          {details.streams_analyzed !== undefined && (
            <div>
              <p className="text-[10px] text-muted-foreground uppercase tracking-tight font-bold">Streams Analyzed</p>
              <p className="text-lg font-bold">{details.streams_analyzed}</p>
            </div>
          )}
          {details.dead_streams !== undefined && (
            <div>
              <p className="text-[10px] text-muted-foreground uppercase tracking-tight font-bold">Dead Streams</p>
              <p className="text-lg font-bold text-destructive">{details.dead_streams}</p>
            </div>
          )}
          {details.streams_revived !== undefined && details.streams_revived > 0 && (
            <div>
              <p className="text-[10px] text-muted-foreground uppercase tracking-tight font-bold">Revived Streams</p>
              <p className="text-lg font-bold text-green-500">{details.streams_revived}</p>
            </div>
          )}
          {details.duration && (
            <div>
              <p className="text-[10px] text-muted-foreground uppercase tracking-tight font-bold">Duration</p>
              <p className="text-lg font-bold">{details.duration}</p>
            </div>
          )}
          {details.avg_bitrate && details.avg_bitrate !== 'N/A' && (
            <div>
              <p className="text-[10px] text-muted-foreground uppercase tracking-tight font-bold">Avg Bitrate</p>
              <p className="text-lg font-bold text-blue-500">{details.avg_bitrate}</p>
            </div>
          )}
          {details.avg_resolution && details.avg_resolution !== 'N/A' && (
            <div>
              <p className="text-[10px] text-muted-foreground uppercase tracking-tight font-bold">Avg Res</p>
              <p className="text-lg font-bold">{details.avg_resolution}</p>
            </div>
          )}
          {details.avg_fps && details.avg_fps !== 'N/A' && (
            <div>
              <p className="text-[10px] text-muted-foreground uppercase tracking-tight font-bold">Avg FPS</p>
              <p className="text-lg font-bold">{details.avg_fps}</p>
            </div>
          )}
        </div>
      </CardHeader>

      {action === 'automation_run' && details.periods && (
        <CardContent className="pt-0 space-y-6 bg-muted/5">
          <div className="pt-4 px-1">
            <Accordion type="multiple" className="w-full space-y-3">
              {details.periods.map((period, pIdx) => (
                <AccordionItem key={pIdx} value={`period-${pIdx}`} className="border rounded-xl overflow-hidden bg-background shadow-sm border-muted/50">
                  <AccordionTrigger className="hover:no-underline hover:bg-muted/30 px-5 py-4 transition-colors">
                    <div className="flex items-center justify-between w-full pr-4">
                      <div className="flex items-center gap-4">
                        <div className="w-10 h-10 rounded-xl bg-primary/10 flex items-center justify-center text-primary font-bold text-sm border border-primary/20">
                          {pIdx + 1}
                        </div>
                        <div className="flex flex-col items-start gap-0.5">
                          <span className="font-bold text-lg tracking-tight">{period.period_name}</span>
                          <span className="text-[10px] font-bold uppercase text-muted-foreground tracking-widest opacity-70">Automation Period</span>
                        </div>
                      </div>
                      <Badge variant="secondary" className="font-bold px-3 py-1 bg-primary/10 text-primary border border-primary/20">
                        {period.channels.length} {period.channels.length === 1 ? 'Channel' : 'Channels'}
                      </Badge>
                    </div>
                  </AccordionTrigger>
                  <AccordionContent className="pt-2 px-1 pb-5">
                    <div className="space-y-6 px-4 pt-4 border-t bg-muted/10">
                      {period.channels.map((channel, cIdx) => (
                        <div key={cIdx} className="space-y-6">
                          <AutomationChannel channel={channel} cIdx={cIdx} />
                          {cIdx < period.channels.length - 1 && <div className="h-px bg-muted/40 w-full mt-6 ml-0 md:ml-14" />}
                        </div>
                      ))}
                    </div>
                  </AccordionContent>
                </AccordionItem>
              ))}
            </Accordion>
          </div>
        </CardContent>
      )}

      {action === 'streams_assigned' && details.assignments && details.assignments.length > 0 && (
        <CardContent className="pt-0">
          <Accordion type="multiple" className="w-full">
            <AccordionItem value="streams-assigned-details">
              <AccordionTrigger className="hover:no-underline font-semibold">
                <div className="flex items-center gap-2">
                  {getActionIcon(action)}
                  <span>Streams Assigned to Channels ({details.assignments.length} channels)</span>
                </div>
              </AccordionTrigger>
              <AccordionContent>
                <Accordion type="multiple" className="w-full pl-4">
                  {details.assignments.map((assignment, idx) => (
                    <AccordionItem key={idx} value={`assignment-${idx}`}>
                      <AccordionTrigger className="hover:no-underline py-2">
                        <div className="flex items-center gap-2">
                          {assignment.logo_url && (
                            <img
                              src={assignment.logo_url}
                              alt={assignment.channel_name}
                              className="w-6 h-6 object-contain"
                              onError={(e) => e.target.style.display = 'none'}
                            />
                          )}
                          <span className="text-sm font-medium">{assignment.channel_name}</span>
                          <span className="text-xs text-muted-foreground ml-2">
                            ({assignment.stream_count} {assignment.stream_count === 1 ? 'stream' : 'streams'})
                          </span>
                        </div>
                      </AccordionTrigger>
                      <AccordionContent>
                        <div className="space-y-1 pl-4">
                          {assignment.streams && assignment.streams.length > 0 && (
                            <ul className="list-none text-sm space-y-1">
                              {assignment.streams.map((stream, streamIdx) => (
                                <li key={streamIdx} className="text-muted-foreground">
                                  - {stream.name || stream.stream_name || `Stream ${stream.id || stream.stream_id}`}
                                </li>
                              ))}
                              {assignment.stream_count > assignment.streams.length && (
                                <li className="text-xs text-muted-foreground italic">
                                  ... and {assignment.stream_count - assignment.streams.length} more
                                </li>
                              )}
                            </ul>
                          )}
                        </div>
                      </AccordionContent>
                    </AccordionItem>
                  ))}
                </Accordion>
                {details.has_more_channels && (
                  <p className="text-xs text-muted-foreground italic mt-2 pl-4">
                    ... and more channels (showing top {details.assignments.length} by stream count)
                  </p>
                )}
              </AccordionContent>
            </AccordionItem>
          </Accordion>
        </CardContent>
      )}

      {hasSubentries && (
        <CardContent className="pt-0">
          <Accordion type="multiple" className="w-full">
            <AccordionItem value="main-reason">
              <AccordionTrigger className="hover:no-underline font-semibold">
                <div className="flex items-center gap-2">
                  {getActionIcon(action)}
                  <span>{getActionLabel(action)} Details</span>
                </div>
              </AccordionTrigger>
              <AccordionContent>
                <Accordion type="multiple" className="w-full pl-4">
                  {subentries.map((group, groupIndex) => {
                    const groupType = group.group
                    const items = group.items || []
                    const groupLabel = groupType === 'update_match' ? 'Added Streams' : 'Checked Streams'
                    const totalCount = items.reduce((sum, item) => {
                      if (groupType === 'update_match') {
                        return sum + (item.streams?.length || 0)
                      } else {
                        return sum + (item.stats?.total_streams || 0)
                      }
                    }, 0)

                    return (
                      <AccordionItem key={groupIndex} value={`group-${groupIndex}`}>
                        <AccordionTrigger className="hover:no-underline">
                          <div className="flex items-center gap-2">
                            <span className="font-medium">
                              {groupLabel}. {groupType === 'update_match' ? 'Added' : 'Checked'}: {totalCount} streams.
                            </span>
                          </div>
                        </AccordionTrigger>
                        <AccordionContent>
                          <Accordion type="multiple" className="w-full pl-4">
                            {items.map((item, itemIndex) => (
                              <ChannelItem
                                key={itemIndex}
                                item={item}
                                groupType={groupType}
                                groupIndex={groupIndex}
                                itemIndex={itemIndex}
                              />
                            ))}
                          </Accordion>
                        </AccordionContent>
                      </AccordionItem>
                    )
                  })}
                </Accordion>
              </AccordionContent>
            </AccordionItem>
          </Accordion>
        </CardContent>
      )}
    </Card>
  )
}

export default function Changelog() {
  const [entries, setEntries] = useState([])
  const [loading, setLoading] = useState(true)
  const [days, setDays] = useState(7)
  const [page, setPage] = useState(1)
  const [totalPages, setTotalPages] = useState(1)
  const [actionFilter, setActionFilter] = useState('all')
  const { toast } = useToast()

  useEffect(() => {
    // Reset page when filter changes
    setPage(1)
  }, [days, actionFilter])

  useEffect(() => {
    loadChangelog()
  }, [days, page, actionFilter])

  const loadChangelog = async () => {
    try {
      setLoading(true)
      const response = await changelogAPI.getChangelog(days, page, 10)

      const responseData = response.data || {};
      const dataArray = Array.isArray(responseData) ? responseData : (responseData.data || []);

      // If we got the new paginated format, use it
      if (responseData.total_pages !== undefined) {
        setTotalPages(responseData.total_pages)
      } else {
        // Fallback array handling
        setTotalPages(1)
      }

      setEntries(dataArray)
    } catch (err) {
      console.error('Failed to load changelog:', err)
      toast({
        title: "Error",
        description: "Failed to load changelog",
        variant: "destructive"
      })
    } finally {
      setLoading(false)
    }
  }

  const filteredEntries = useMemo(() => {
    return actionFilter === 'all'
      ? entries
      : entries.filter(entry => entry.action === actionFilter)
  }, [entries, actionFilter])

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-3xl font-bold tracking-tight">Changelog</h1>
          <p className="text-muted-foreground">
            View activity history and system events
          </p>
        </div>

        <div className="flex gap-3">
          <Select value={actionFilter} onValueChange={setActionFilter}>
            <SelectTrigger className="w-[200px]">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="all">All Actions</SelectItem>
              <SelectItem value="automation_run">Automation Runs</SelectItem>
              <SelectItem value="playlist_update_match">Playlist Update & Match</SelectItem>
              <SelectItem value="global_check">Global Check</SelectItem>
              <SelectItem value="single_channel_check">Single Channel Check</SelectItem>
              <SelectItem value="batch_stream_check">Batch Stream Check</SelectItem>
              <SelectItem value="playlist_refresh">Playlist Refresh</SelectItem>
              <SelectItem value="streams_assigned">Streams Assigned</SelectItem>
              <SelectItem value="stream_check">Stream Check</SelectItem>
            </SelectContent>
          </Select>

          <Select value={days.toString()} onValueChange={(value) => setDays(Number(value))}>
            <SelectTrigger className="w-[150px]">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="1">Last 24 hours</SelectItem>
              <SelectItem value="7">Last 7 days</SelectItem>
              <SelectItem value="30">Last 30 days</SelectItem>
              <SelectItem value="90">Last 90 days</SelectItem>
            </SelectContent>
          </Select>
        </div>
      </div>

      {loading && entries.length === 0 ? (
        <Card>
          <CardContent className="flex items-center justify-center py-12">
            <Loader2 className="h-8 w-8 animate-spin text-muted-foreground" />
          </CardContent>
        </Card>
      ) : filteredEntries.length === 0 ? (
        <Card>
          <CardContent className="flex flex-col items-center justify-center py-12">
            <Activity className="h-12 w-12 text-muted-foreground mb-4" />
            <p className="text-muted-foreground">
              {actionFilter === 'all'
                ? 'No changelog entries found for the selected period'
                : `No ${getActionLabel(actionFilter)} entries found for the selected period`}
            </p>
          </CardContent>
        </Card>
      ) : (
        <div className="space-y-4">
          {filteredEntries.map((entry, index) => (
            <ChangelogEntry key={index} entry={entry} />
          ))}

          {/* Pagination Controls */}
          {totalPages > 1 && (
            <div className="flex items-center justify-between pt-4 border-t border-muted">
              <span className="text-sm text-muted-foreground">
                Page {page} of {totalPages}
              </span>
              <div className="flex gap-2">
                <button
                  onClick={() => setPage(p => Math.max(1, p - 1))}
                  disabled={page === 1 || loading}
                  className="px-4 py-2 text-sm font-medium rounded-md border text-muted-foreground bg-background hover:bg-muted disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
                >
                  Previous
                </button>
                <button
                  onClick={() => setPage(p => Math.min(totalPages, p + 1))}
                  disabled={page >= totalPages || loading}
                  className="px-4 py-2 text-sm font-medium rounded-md border text-muted-foreground bg-background hover:bg-muted disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
                >
                  Next
                </button>
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  )
}
