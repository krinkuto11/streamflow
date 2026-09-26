import { lazy, Suspense, useState, useEffect, useRef } from 'react';
import { Play, Square, Trash2, Plus, Activity, AlertCircle, LayoutGrid, List, X, ArrowRight } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Card, CardContent } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { Tabs, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { getSessionHealth, sortMonitoringSessions, toggleVisibleSessionSelection } from '@/lib/monitoring-session-display';
import { AlertDialog, AlertDialogAction, AlertDialogCancel, AlertDialogContent, AlertDialogDescription, AlertDialogFooter, AlertDialogHeader, AlertDialogTitle } from '@/components/ui/alert-dialog';
import { useToast } from '@/hooks/use-toast';
import { streamSessionsAPI } from '@/services/streamSessions';
import CreateSessionDialog from '@/components/stream-monitoring/CreateSessionDialog';

const SessionMonitorView = lazy(() => import('@/components/stream-monitoring/SessionMonitorView'));

function StreamMonitoring() {
  const [sessions, setSessions] = useState([]);
  const [activeSessions, setActiveSessions] = useState([]);
  const [loading, setLoading] = useState(true);
  const [createDialogOpen, setCreateDialogOpen] = useState(false);
  const [deleteDialogOpen, setDeleteDialogOpen] = useState(false);
  const [sessionToDelete, setSessionToDelete] = useState(null);
  const [selectedSession, setSelectedSession] = useState(null);
  const { toast } = useToast();
  const [viewMode, setViewMode] = useState('list');
  const [sessionFilter, setSessionFilter] = useState('active');
  const [loadError, setLoadError] = useState(false);

  const [selectedSessions, setSelectedSessions] = useState(new Set());
  const activeSessionCountRef = useRef(0);

  useEffect(() => {
    if (selectedSession) return undefined;
    let stopped = false;
    let timer;
    const poll = async () => {
      if (document.visibilityState === 'visible') await loadSessions(false);
      if (!stopped) timer = setTimeout(poll, activeSessionCountRef.current > 0 ? 5000 : 15000);
    };
    loadSessions();
    timer = setTimeout(poll, 5000);
    const onVisibilityChange = () => {
      if (document.visibilityState === 'visible') loadSessions(false);
    };
    document.addEventListener('visibilitychange', onVisibilityChange);
    return () => {
      stopped = true;
      clearTimeout(timer);
      document.removeEventListener('visibilitychange', onVisibilityChange);
    };
  }, [selectedSession]);

  const loadSessions = async (showLoading = true) => {
    try {
      if (showLoading) {
        setLoading(true);
      }
      const response = await streamSessionsAPI.getSessions();
      const standardAll = response.data || [];
      const standardActive = standardAll.filter(session => session.is_active);

      setSessions(sortMonitoringSessions(standardAll));
      setLoadError(false);
      setSelectedSessions(previous => new Set([...previous].filter(id => standardAll.some(session => session.session_id === id))));
      setActiveSessions(sortMonitoringSessions(standardActive));
      activeSessionCountRef.current = standardActive.length;
    } catch (err) {
      console.error('Failed to load sessions:', err);
      setLoadError(true);
      if (showLoading) {
        toast({
          title: 'Error',
          description: 'Failed to load monitoring sessions',
          variant: 'destructive'
        });
      }
    } finally {
      if (showLoading) {
        setLoading(false);
      }
    }
  };

  const handleStartSession = async (session) => {
    try {
      await streamSessionsAPI.startSession(session.session_id);
      toast({
        title: 'Success',
        description: 'Session started successfully'
      });
      loadSessions();
    } catch (err) {
      console.error('Failed to start session:', err);
      toast({
        title: 'Error',
        description: 'Failed to start session',
        variant: 'destructive'
      });
    }
  };

  const handleStopSession = async (session) => {
    try {
      await streamSessionsAPI.stopSession(session.session_id);
      toast({
        title: 'Success',
        description: 'Session stopped successfully'
      });
      loadSessions();
    } catch (err) {
      console.error('Failed to stop session:', err);
      toast({
        title: 'Error',
        description: 'Failed to stop session',
        variant: 'destructive'
      });
    }
  };

  const handleDeleteSession = async (session) => {
    setSessionToDelete(session);
    setDeleteDialogOpen(true);
  };

  const confirmDeleteSession = async () => {
    if (!sessionToDelete) return;

    try {
      await streamSessionsAPI.deleteSession(sessionToDelete.session_id);
      toast({
        title: 'Success',
        description: 'Session deleted successfully'
      });

      if (selectedSession && selectedSession.session_id === sessionToDelete.session_id) {
        setSelectedSession(null);
      }

      // Remove from selection if deleted
      if (selectedSessions.has(sessionToDelete.session_id)) {
        const newSelected = new Set(selectedSessions);
        newSelected.delete(sessionToDelete.session_id);
        setSelectedSessions(newSelected);
      }

      loadSessions();
    } catch (err) {
      console.error('Failed to delete session:', err);
      toast({
        title: 'Error',
        description: 'Failed to delete session',
        variant: 'destructive'
      });
    } finally {
      setDeleteDialogOpen(false);
      setSessionToDelete(null);
    }
  };

  const handleCreateSession = async (sessionData) => {
    try {
      if (sessionData.group_id) {
        // Group Monitoring
        const response = await streamSessionsAPI.createGroupSession(sessionData);
        toast({
          title: 'Success',
          description: response.data.message || `Started sessions for group`
        });

        // Show any errors if partial success
        if (response.data.errors && response.data.errors.length > 0) {
          toast({
            title: 'Warning',
            description: `Some sessions failed to start: ${response.data.errors[0]}`,
            variant: 'warning'
          });
        }
      } else {
        // Single Channel Monitoring
        const response = await streamSessionsAPI.createSession(sessionData);
        toast({
          title: 'Success',
          description: 'Session created successfully'
        });

        // Optionally auto-start the session
        if (sessionData.autoStart) {
          await streamSessionsAPI.startSession(response.data.session_id);
        }
      }

      setCreateDialogOpen(false);
      loadSessions();
    } catch (err) {
      console.error('Failed to create session:', err);
      toast({
        title: 'Error',
        description: err.response?.data?.error || 'Failed to create session',
        variant: 'destructive'
      });
    }
  };

  const handleViewSession = (session) => {
    setSelectedSession(session);
  };

  const handleBackToList = () => {
    setSelectedSession(null);
    loadSessions();
  };

  // Batch Operations
  const toggleSelection = (sessionId) => {
    const newSelected = new Set(selectedSessions);
    if (newSelected.has(sessionId)) {
      newSelected.delete(sessionId);
    } else {
      newSelected.add(sessionId);
    }
    setSelectedSessions(newSelected);
  };

  const toggleSelectAll = (visibleSessions) => {
    setSelectedSessions(previous => toggleVisibleSessionSelection(previous, visibleSessions));
  };

  const handleBatchStop = async () => {
    if (selectedSessions.size === 0) return;

    try {
      const standardIds = sessions
        .filter((s) => selectedSessions.has(s.session_id))
        .map((s) => s.session_id);

      if (standardIds.length > 0) {
        await streamSessionsAPI.batchStopSessions(standardIds);
      }

      toast({
        title: 'Batch Operation',
        description: `Stopped ${selectedSessions.size} sessions`
      });
      loadSessions();
      setSelectedSessions(new Set());
    } catch (err) {
      console.error('Batch stop failed:', err);
      toast({
        title: 'Error',
        description: 'Failed to stop selected sessions',
        variant: 'destructive'
      });
    }
  };

  const handleBatchDelete = async () => {
    if (selectedSessions.size === 0) return;

    if (!window.confirm(`Are you sure you want to delete ${selectedSessions.size} sessions? This cannot be undone.`)) {
      return;
    }

    try {
      const standardIds = sessions
        .filter((s) => selectedSessions.has(s.session_id))
        .map((s) => s.session_id);

      if (standardIds.length > 0) {
        await streamSessionsAPI.batchDeleteSessions(standardIds);
      }

      toast({
        title: 'Batch Operation',
        description: `Deleted ${selectedSessions.size} sessions`
      });
      loadSessions();
      setSelectedSessions(new Set());
    } catch (err) {
      console.error('Batch delete failed:', err);
      toast({
        title: 'Error',
        description: 'Failed to delete selected sessions',
        variant: 'destructive'
      });
    }
  };

  const visibleSessions = sessionFilter === 'active' ? activeSessions : sessions;
  const quarantinedSessions = activeSessions.filter(session => session.quarantined_count > 0);
  const reviewCount = activeSessions.reduce((total, session) => total + (session.review_count || 0), 0);
  const allVisibleSelected = visibleSessions.length > 0 && visibleSessions.every(session => selectedSessions.has(session.session_id));

  return (
    <>
      {selectedSession ? (
        <Suspense fallback={<div className="flex min-h-64 items-center justify-center text-sm text-muted-foreground" role="status">Loading session details...</div>}>
          <SessionMonitorView
            sessionId={selectedSession.session_id}
            onBack={handleBackToList}
            onStop={() => handleStopSession(selectedSession)}
          />
        </Suspense>
      ) : (
        <div className="min-w-0 space-y-5">
          <div className="grid grid-cols-[minmax(0,1fr)_auto] items-center gap-x-3 gap-y-1">
            <h1 className="text-3xl font-bold tracking-tight">Monitoring</h1>
            <Button className="min-h-11 px-3 sm:px-4" onClick={() => setCreateDialogOpen(true)}>
              <Plus className="mr-1.5 h-4 w-4" aria-hidden="true" /> New session
            </Button>
            <p className="col-span-2 text-sm text-muted-foreground">Watch stream quality and investigate source issues.</p>
          </div>

          {loadError && (
            <div role="alert" className="flex flex-wrap items-center justify-between gap-2 rounded-lg border border-destructive/30 bg-destructive/5 p-3 text-sm">
              <span>{sessions.length ? 'Session refresh failed. The list may be out of date.' : 'Monitoring sessions could not be loaded.'}</span>
              <Button variant="outline" className="min-h-11" onClick={() => loadSessions()}>Retry</Button>
            </div>
          )}

          {!loading && activeSessions.length > 0 && (
            <div className="flex items-center justify-between gap-3 rounded-lg border bg-card px-3 py-3 sm:px-4">
              <div className="flex min-w-0 items-start gap-3">
                {quarantinedSessions.length > 0 ? <AlertCircle className="mt-0.5 h-5 w-5 shrink-0 text-amber-600 dark:text-amber-400" aria-hidden="true" /> : <Activity className="mt-0.5 h-5 w-5 shrink-0 text-primary" aria-hidden="true" />}
                <div>
                  <p className="text-sm font-medium">
                    {quarantinedSessions.length > 0
                      ? `${quarantinedSessions.length} of ${activeSessions.length} active sessions with quarantined sources`
                      : `${activeSessions.length} active ${activeSessions.length === 1 ? 'session' : 'sessions'}`}
                  </p>
                  <p className="mt-0.5 text-xs text-muted-foreground">
                    {reviewCount > 0 ? `${reviewCount} ${reviewCount === 1 ? 'source' : 'sources'} under review` : 'Open a session for source measurements'}
                  </p>
                </div>
              </div>
              {quarantinedSessions.length > 0 && (
                <Button variant="outline" className="min-h-11 shrink-0 px-3" onClick={() => handleViewSession(quarantinedSessions[0])}>
                  Review <ArrowRight className="ml-2 hidden h-4 w-4 sm:block" aria-hidden="true" />
                </Button>
              )}
            </div>
          )}

          <div className="flex flex-wrap items-center justify-between gap-3">
            <Tabs value={sessionFilter} onValueChange={setSessionFilter} className="min-w-0">
              <TabsList className="h-auto">
                <TabsTrigger className="min-h-11 px-3" value="active">Active ({activeSessions.length})</TabsTrigger>
                <TabsTrigger className="min-h-11 px-3" value="all">All sessions ({sessions.length})</TabsTrigger>
              </TabsList>
            </Tabs>
            {visibleSessions.length > 0 && (
              <label className="inline-flex min-h-11 cursor-pointer items-center gap-2 text-sm text-muted-foreground">
                <input type="checkbox" className="h-4 w-4 accent-primary" checked={allVisibleSelected} onChange={() => toggleSelectAll(visibleSessions)} />
                <span className="sm:hidden">Select all</span><span className="hidden sm:inline">Select all shown</span>
              </label>
            )}
          </div>

          {loading ? (
            <div className="py-12 text-center" role="status">
              <div className="mx-auto mb-4 h-8 w-8 animate-spin rounded-full border-b-2 border-primary" />
              <p className="text-sm text-muted-foreground">Loading sessions...</p>
            </div>
          ) : visibleSessions.length === 0 && !loadError ? (
            <Card>
              <CardContent className="flex flex-col items-center px-5 py-10 text-center">
                <Activity className="mb-3 h-8 w-8 text-muted-foreground" aria-hidden="true" />
                <h2 className="text-lg font-medium">{sessionFilter === 'active' ? 'No active sessions' : 'No monitoring sessions'}</h2>
                <p className="mt-2 max-w-md text-sm text-muted-foreground">Use New session to monitor a channel or group. Quality measurements, source status and recovery controls will appear here.</p>
                {sessionFilter === 'active' && sessions.length > 0 && <Button className="mt-3 min-h-11" variant="outline" onClick={() => setSessionFilter('all')}>View previous sessions</Button>}
              </CardContent>
            </Card>
          ) : visibleSessions.length > 0 && (
            <section aria-label={sessionFilter === 'active' ? 'Active monitoring sessions' : 'All monitoring sessions'} className="space-y-2">
              {selectedSessions.size > 0 && (
                <div className="sticky top-2 z-20 flex flex-wrap items-center gap-2 rounded-lg border bg-popover p-3 shadow-sm" aria-label="Selected session actions">
                  <span className="mr-auto text-sm font-medium">{selectedSessions.size} selected</span>
                  <Button className="min-h-11" variant="secondary" onClick={handleBatchStop}><Square className="mr-2 h-4 w-4" aria-hidden="true" />Stop selected</Button>
                  <Button className="min-h-11" variant="destructive" onClick={handleBatchDelete}><Trash2 className="mr-2 h-4 w-4" aria-hidden="true" />Delete selected</Button>
                  <Button size="icon" variant="ghost" className="h-11 w-11" aria-label="Clear session selection" onClick={() => setSelectedSessions(new Set())}><X className="h-4 w-4" aria-hidden="true" /></Button>
                </div>
              )}
              <div className={viewMode === 'grid' ? 'grid gap-3 md:grid-cols-2 2xl:grid-cols-3' : 'divide-y rounded-lg border bg-card'}>
                {visibleSessions.map(session => (
                  <SessionCard key={session.session_id} session={session} compact={viewMode === 'list'}
                    onView={handleViewSession} onStart={handleStartSession} onStop={handleStopSession} onDelete={handleDeleteSession}
                    selected={selectedSessions.has(session.session_id)} onToggleSelection={toggleSelection} />
                ))}
              </div>
            </section>
          )}

          {sessions.length > 0 && (
            <details className="text-sm text-muted-foreground">
              <summary className="w-fit cursor-pointer rounded py-3 focus-visible:outline focus-visible:outline-2 focus-visible:outline-primary">Display options</summary>
              <div className="mt-1 flex w-fit items-center gap-1 rounded-md border p-0.5" aria-label="Session layout">
                <Button variant={viewMode === 'list' ? 'secondary' : 'ghost'} className="min-h-11" onClick={() => setViewMode('list')} aria-pressed={viewMode === 'list'}>
                  <List className="mr-2 h-4 w-4" aria-hidden="true" /> List view
                </Button>
                <Button variant={viewMode === 'grid' ? 'secondary' : 'ghost'} className="min-h-11" onClick={() => setViewMode('grid')} aria-pressed={viewMode === 'grid'}>
                  <LayoutGrid className="mr-2 h-4 w-4" aria-hidden="true" /> Grid view
                </Button>
              </div>
            </details>
          )}

          <details className="text-sm text-muted-foreground">
            <summary className="w-fit cursor-pointer rounded py-3 focus-visible:outline focus-visible:outline-2 focus-visible:outline-primary">About monitoring</summary>
            <p className="max-w-3xl pb-2 leading-relaxed">Monitoring sessions track live reliability and manage stream selection in Dispatcharr. Sources move between stable, review and quarantine states. FFmpeg sessions can include screenshots and live previews; OpenStream sessions show swarm health. Open a session to inspect source measurements and use quarantine or revive controls.</p>
          </details>

          <CreateSessionDialog open={createDialogOpen} onOpenChange={setCreateDialogOpen} onCreateSession={handleCreateSession} />
          <AlertDialog open={deleteDialogOpen} onOpenChange={setDeleteDialogOpen}>
            <AlertDialogContent>
              <AlertDialogHeader>
                <AlertDialogTitle>Delete session</AlertDialogTitle>
                <AlertDialogDescription>Delete this session and its metrics and screenshots? This action cannot be undone.</AlertDialogDescription>
              </AlertDialogHeader>
              <AlertDialogFooter><AlertDialogCancel>Cancel</AlertDialogCancel><AlertDialogAction onClick={confirmDeleteSession}>Delete</AlertDialogAction></AlertDialogFooter>
            </AlertDialogContent>
          </AlertDialog>
        </div>
      )}
    </>
  );
}

export function SessionCard({ session, compact = false, onView, onStart, onStop, onDelete, selected, onToggleSelection }) {
  const health = getSessionHealth(session);
  const healthColor = {
    warning: 'text-amber-700 dark:text-amber-400', review: 'text-blue-700 dark:text-blue-400',
    stable: 'text-emerald-700 dark:text-emerald-400', unknown: 'text-muted-foreground',
  }[health.tone];
  return (
    <article className={`${compact ? 'p-4 xl:grid xl:grid-cols-[minmax(0,1fr)_minmax(180px,0.7fr)_auto] xl:items-center xl:gap-5' : 'rounded-lg border bg-card p-4'} min-w-0 ${selected ? 'bg-primary/5 ring-1 ring-inset ring-primary' : ''}`} aria-label={`${session.channel_name} monitoring session`}>
      <div className="flex min-w-0 items-start gap-2">
        <label className="flex h-11 w-8 shrink-0 cursor-pointer items-center justify-center">
          <input type="checkbox" className="h-4 w-4 accent-primary" checked={selected} onChange={() => onToggleSelection(session.session_id)} aria-label={`Select ${session.channel_name}`} />
        </label>
        {session.channel_logo_url && <img src={session.channel_logo_url} alt="" className="mt-1 h-9 w-9 shrink-0 rounded bg-muted object-contain p-1" onError={event => { event.target.style.display = 'none'; }} />}
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <h2 className="break-words text-base font-semibold">{session.channel_name}</h2>
            <Badge variant={session.is_active ? 'default' : 'secondary'}>{session.is_active ? 'Active' : 'Inactive'}</Badge>
          </div>
          {session.epg_event_title && <p className="mt-1 truncate text-sm text-muted-foreground" title={session.epg_event_title}>{session.epg_event_title}</p>}
          <p className="mt-1 text-xs text-muted-foreground">{session.session_type === 'openstream' ? 'OpenStream' : 'FFmpeg'} <span aria-hidden="true"> &middot; </span> Created {new Date(session.created_at * 1000).toLocaleString()}</p>
        </div>
      </div>
      <div className={`mt-3 ${compact ? 'xl:mt-0' : ''}`}>
        <p className={`text-sm font-medium ${healthColor}`}>{health.label}</p>
        <div className="mt-1 flex flex-wrap gap-x-3 gap-y-1 text-xs text-muted-foreground" aria-label="Source counts">
          <span>{health.stable} stable</span><span>{health.review} in review</span><span>{health.quarantined} quarantined</span>
        </div>
      </div>
      <div className={`mt-4 flex flex-wrap items-center gap-2 ${compact ? 'xl:mt-0' : ''}`}>
        <Button variant="outline" className="min-h-11 flex-1 text-primary xl:flex-none" onClick={() => onView(session)} aria-label={`View details for ${session.channel_name}`}>View details</Button>
        <Button size="icon" variant="ghost" className="h-11 w-11 shrink-0" onClick={() => session.is_active ? onStop(session) : onStart(session)} aria-label={`${session.is_active ? 'Stop' : 'Start'} ${session.channel_name}`} title={session.is_active ? 'Stop monitoring' : 'Start monitoring'}>
          {session.is_active ? <Square className="h-4 w-4" aria-hidden="true" /> : <Play className="h-4 w-4" aria-hidden="true" />}
        </Button>
        <Button size="icon" variant="ghost" className="h-11 w-11 shrink-0 text-muted-foreground hover:text-destructive" onClick={() => onDelete(session)} aria-label={`Delete ${session.channel_name}`} title="Delete session"><Trash2 className="h-4 w-4" aria-hidden="true" /></Button>
      </div>
    </article>
  );
}

export default StreamMonitoring;
