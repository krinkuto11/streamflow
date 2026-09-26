import { useState, useEffect, useMemo, useRef } from 'react';
import * as React from 'react';
import { cn } from '@/lib/utils';
import { ArrowLeft, Square, Activity, AlertCircle, Image as ImageIcon, Calendar, Clock, Ban, Play, Volume2, VolumeX, Radio, ExternalLink, Maximize2, Minimize2, ChevronDown, ChevronUp } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { Progress } from '@/components/ui/progress';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table';
import { Dialog, DialogContent, DialogHeader, DialogTitle } from '@/components/ui/dialog';
import { useToast } from '@/hooks/use-toast';
import { streamSessionsAPI } from '@/services/streamSessions';
import { LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip as RechartsTooltip, ResponsiveContainer, ReferenceArea } from 'recharts';
import { TimelineControl } from './TimelineControl';

// Constants
const FALLBACK_IMAGE_SVG = 'data:image/svg+xml,%3Csvg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 400 225"%3E%3Crect fill="%23111" width="400" height="225"/%3E%3Ctext fill="%23666" x="50%25" y="50%25" text-anchor="middle" dominant-baseline="middle" font-family="sans-serif"%3ENo Image%3C/text%3E%3C/svg%3E';
const CHANNEL_LOGO_PREFIX = 'streamflow_channel_logo_';

function SessionMonitorView({ sessionId, onBack, onStop }) {
  const [session, setSession] = useState(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState(false);
  const [aliveScreenshots, setAliveScreenshots] = useState([]);
  const [logoUrl, setLogoUrl] = useState(null);
  const [playingStreamIds, setPlayingStreamIds] = useState(new Set());
  const [cursorTime, setCursorTime] = useState(null); // Current timestamp of the timeline
  const [isLive, setIsLive] = useState(true); // Whether we are following the latest updates
  const [zoomLevel, setZoomLevel] = useState(60); // Window size in seconds (default 1 minute)
  const [activePreviewTab, setActivePreviewTab] = useState("");
  const [showTimeline, setShowTimeline] = useState(false);
  const [expandedStreamId, setExpandedStreamId] = useState(null);
  const { toast } = useToast();
  const latestTimestampRef = useRef(null);
  const sessionActiveRef = useRef(true);
  const auxiliaryPollRef = useRef({ playing: 0, screenshots: 0 });

  // Helper to find the metric closest to the cursor time
  const getSnapshotAtTime = (stream, time) => {
    // Normalize speed field: use speed if available, fallback to current_speed
    const normalizedStream = {
      ...stream,
      speed: stream.speed !== undefined ? stream.speed : stream.current_speed
    };

    if (!stream.metrics_history || stream.metrics_history.length === 0) return normalizedStream;

    // If live, return normalized current state
    if (isLive) return normalizedStream;

    // Find closest metric
    // Assuming metrics are sorted by timestamp (they should be appended in order)
    // We can do a simple search or binary search. For < 1000 items, simple reverse search is fine.

    // If time is past the last metric, use the last one (but maybe show as unknown if too far?)
    const lastMetric = stream.metrics_history[stream.metrics_history.length - 1];
    if (time >= lastMetric.timestamp) return stream;

    let closest = null;
    let minDiff = Infinity;

    for (const metric of stream.metrics_history) {
      const diff = Math.abs(metric.timestamp - time);
      if (diff <= minDiff) {
        minDiff = diff;
        closest = metric;
      }
    }

    if (closest) {
      return {
        ...stream,
        speed: closest.speed,
        bitrate: closest.bitrate,
        fps: closest.fps,
        reliability_score: closest.reliability_score !== undefined ? closest.reliability_score : stream.reliability_score,
        status: closest.status || stream.status, // Fallback if status wasn't recorded in old history
        status_reason: closest.status_reason || stream.status_reason,
        is_quarantined: (closest.status || stream.status) === 'quarantined',
        is_alive: closest.is_alive,
        rank: closest.rank,
        display_logo_status: closest.display_logo_status || stream.display_logo_status
      };
    }

    return stream;
  };

  // Use a ref to track isLive state for the interval callback
  const isLiveRef = useRef(isLive);

  // Update ref when state changes
  useEffect(() => {
    isLiveRef.current = isLive;
  }, [isLive]);

  // Cache channel logo from localStorage
  useEffect(() => {
    if (session && session.channel_id) {
      const cachedLogo = localStorage.getItem(`${CHANNEL_LOGO_PREFIX}${session.channel_id}`);
      if (cachedLogo) {
        setLogoUrl(cachedLogo);
      }

      // If session has a logo URL, use it and cache it
      if (session.channel_logo_url) {
        setLogoUrl(session.channel_logo_url);
        localStorage.setItem(`${CHANNEL_LOGO_PREFIX}${session.channel_id}`, session.channel_logo_url);
      }
    }
  }, [session?.channel_id, session?.channel_logo_url]);

  useEffect(() => {
    latestTimestampRef.current = null;
    sessionActiveRef.current = true;
    auxiliaryPollRef.current = { playing: 0, screenshots: 0 };
    setSession(null);
    setLoading(true);
    setLoadError(false);
  }, [sessionId]);

  useEffect(() => {
    let stopped = false;
    let timer;
    const poll = async () => {
      if (document.visibilityState === 'visible' && sessionActiveRef.current) {
        const now = Date.now();
        const requests = [loadSession()];
        if (now - auxiliaryPollRef.current.playing >= 5000) {
          auxiliaryPollRef.current.playing = now;
          requests.push(loadPlayingStreams());
        }
        if (activePreviewTab === 'screenshots' && now - auxiliaryPollRef.current.screenshots >= 5000) {
          auxiliaryPollRef.current.screenshots = now;
          requests.push(loadAliveScreenshots());
        }
        await Promise.all(requests);
      }
      if (!stopped && sessionActiveRef.current) timer = setTimeout(poll, 2000);
    };
    if (activePreviewTab === 'screenshots' && !sessionActiveRef.current) void loadAliveScreenshots();
    void poll();
    const onVisibilityChange = () => {
      if (document.visibilityState === 'visible' && sessionActiveRef.current) {
        clearTimeout(timer);
        void poll();
      }
    };
    document.addEventListener('visibilitychange', onVisibilityChange);
    return () => {
      stopped = true;
      clearTimeout(timer);
      document.removeEventListener('visibilitychange', onVisibilityChange);
    };
  }, [sessionId, activePreviewTab]);

  const loadPlayingStreams = async () => {
    try {
      const response = await streamSessionsAPI.getPlayingStreams();
      setPlayingStreamIds(new Set(response.data.playing_stream_ids));
    } catch (err) {
      console.error('Failed to load playing streams:', err);
    }
  };

  const loadSession = async () => {
    try {
      const response = await streamSessionsAPI.getSession(sessionId, latestTimestampRef.current);
      setLoadError(false);
      
      // Update ref with latest timestamp from response
      let maxTime = latestTimestampRef.current || response.data.created_at || 0;
      if (response.data.streams) {
        response.data.streams.forEach(s => {
          if (s.metrics_history && s.metrics_history.length > 0) {
            const last = s.metrics_history[s.metrics_history.length - 1];
            if (last.timestamp > maxTime) maxTime = last.timestamp;
          }
        });
      }
      latestTimestampRef.current = maxTime;
      sessionActiveRef.current = Boolean(response.data.is_active);

      setSession(currentSession => {
        if (!currentSession) return response.data;

        // Merge streams and their metrics
        const previousById = new Map(currentSession.streams.map(stream => [stream.stream_id, stream]));
        const mergedStreams = response.data.streams.map(newStream => {
          const prevStream = previousById.get(newStream.stream_id);
          if (prevStream) {
            const existingTimestamps = new Set(prevStream.metrics_history?.map(m => m.timestamp) || []);
            const newMetrics = (newStream.metrics_history || []).filter(m => !existingTimestamps.has(m.timestamp));
            return {
              ...newStream,
              metrics_history: [...(prevStream.metrics_history || []), ...newMetrics].slice(-3600)
            };
          }
          return newStream;
        });

        return {
          ...response.data,
          streams: mergedStreams
        };
      });

      // Handle inactive sessions
      if (!response.data.is_active) {
        setIsLive(false);
        // Only update cursor if we haven't set it yet or if we were live
        if (isLiveRef.current || cursorTime === null) {
          setCursorTime(maxTime);
        }
      } else {
        // Update cursor if live, utilizing the ref to avoid stale closures in interval
        if (isLiveRef.current) {
          setCursorTime(Math.floor(Date.now() / 1000));
        }
      }

      setLoading(false);
    } catch (err) {
      console.error('Failed to load session:', err);
      setLoadError(true);
      // Suppress toast if we already have session data (session exists and we are just refreshing)
      if (!session) {
        toast({
          title: 'Error',
          description: 'Failed to load session details',
          variant: 'destructive'
        });
      }
      setLoading(false);
    }
  };

  const handleTimeChange = (newTime) => {
    setCursorTime(newTime);
    setIsLive(false);
  };

  const handleLiveClick = () => {
    setIsLive(true);
    if (session) {
      setCursorTime(Math.floor(Date.now() / 1000));
    }
  };

  const loadAliveScreenshots = async () => {
    try {
      const response = await streamSessionsAPI.getAliveScreenshots(sessionId);
      // Only update if screenshots array changed
      setAliveScreenshots(prevScreenshots => {
        const newScreenshots = response.data.screenshots || [];

        // Quick length check first
        if (prevScreenshots.length !== newScreenshots.length) {
          return newScreenshots;
        }

        // Check if screenshot URLs or stream IDs changed
        const hasChanged = newScreenshots.some((newShot, idx) => {
          const prevShot = prevScreenshots[idx];
          return !prevShot ||
            prevShot.stream_id !== newShot.stream_id ||
            prevShot.screenshot_url !== newShot.screenshot_url;
        });

        return hasChanged ? newScreenshots : prevScreenshots;
      });
    } catch (err) {
      console.error('Failed to load screenshots:', err);
    }
  };

  const handleViewScreenshot = (stream) => {
    setSelectedStream(stream);
    setScreenshotDialogOpen(true);
  };

  const handleQuarantineStream = async (streamId) => {
    try {
      await streamSessionsAPI.quarantineStream(sessionId, streamId);
      toast({
        title: 'Success',
        description: 'Stream quarantined successfully'
      });
      // Reload session to reflect changes
      loadSession();
    } catch (err) {
      console.error('Failed to quarantine stream:', err);
      toast({
        title: 'Error',
        description: 'Failed to quarantine stream',
        variant: 'destructive'
      });
    }
  };

  const handleReviveStream = async (streamId) => {
    try {
      await streamSessionsAPI.reviveStream(sessionId, streamId);
      toast({
        title: 'Success',
        description: 'Stream revived successfully (moved to Under Review)'
      });
      loadSession();
    } catch (err) {
      console.error('Failed to revive stream:', err);
      toast({
        title: 'Error',
        description: 'Failed to revive stream',
        variant: 'destructive'
      });
    }
  };

  /* Filter streams based on cursor time */
  const currentStreams = useMemo(() => {
    if (!session || !session.streams) return [];

    // If we have no cursor time yet, use current time
    const time = cursorTime || Math.floor(Date.now() / 1000);

    return session.streams.map(stream => getSnapshotAtTime(stream, time));
  }, [session, cursorTime, isLive]);

  const sortStreams = (a, b) => {
    // If both have rank, use it (ascending: 1 is best)
    if (a.rank !== undefined && a.rank !== null && b.rank !== undefined && b.rank !== null) {
      return a.rank - b.rank;
    }
    // Fallback to reliability score (descending)
    return b.reliability_score - a.reliability_score;
  };

  const stableStreams = currentStreams.filter(s => s.status === 'stable' && !s.is_quarantined).sort(sortStreams);
  const reviewStreams = currentStreams.filter(s => s.status === 'review' && !s.is_quarantined).sort(sortStreams);
  const quarantinedStreams = currentStreams.filter(s => s.status === 'quarantined' || s.is_quarantined);
  const activeStreams = [...stableStreams, ...reviewStreams];

  /* Extract significant events for timeline markers */
  const timelineEvents = useMemo(() => {
    if (!session || !session.streams) return [];

    const events = [];
    const streams = session.streams;

    // 1. Stream Status Changes (Quarantine, Promotion)
    streams.forEach(stream => {
      if (!stream.metrics_history || stream.metrics_history.length < 2) return;

      // Iterate history to find status changes
      for (let i = 1; i < stream.metrics_history.length; i++) {
        const prev = stream.metrics_history[i - 1];
        const curr = stream.metrics_history[i];

        // Status change detect
        if (prev.status !== curr.status) {
          // Stable -> Quarantined (Yellow)
          if (curr.status === 'quarantined') {
            events.push({
              time: curr.timestamp,
              type: 'Quarantine',
              description: `${stream.name} quarantined (${curr.reliability_score.toFixed(1)}%)`,
              color: 'yellow'
            });
          }
          // Review -> Stable (Blue)
          else if (prev.status === 'review' && curr.status === 'stable') {
            events.push({
              time: curr.timestamp,
              type: 'Promotion',
              description: `${stream.name} promoted to Stable`,
              color: 'blue'
            });
          }
        }
      }
    });

    // 2. Primary Stream Changes (Green)
    // We need to find when the rank 1 stream changes
    // This requires aggregating all streams at each timestamp, which is expensive.
    // Optimization: Collect all timestamps where any stream has rank 1.
    const rankOneChanges = [];
    const timestamps = new Set();
    streams.forEach(s => {
      s.metrics_history?.forEach(m => timestamps.add(m.timestamp));
    });

    const sortedTimestamps = Array.from(timestamps).sort((a, b) => a - b);
    let lastTopStreamId = null;

    // Sample every few seconds to avoid excessive processing if history is huge
    // But for accuracy in timeline, we should try to be precise.
    // Let's filter to timestamps where a rank change might have occurred (approx)

    // Simpler approach: Iterate through all metrics of all streams, filtering for rank=1
    const rankOneMetrics = [];
    streams.forEach(s => {
      s.metrics_history?.forEach(m => {
        if (m.rank === 1) {
          rankOneMetrics.push({ ...m, stream_id: s.stream_id, stream_name: s.name });
        }
      });
    });

    rankOneMetrics.sort((a, b) => a.timestamp - b.timestamp);

    rankOneMetrics.forEach(metric => {
      if (lastTopStreamId !== metric.stream_id) {
        if (lastTopStreamId !== null) {
          events.push({
            time: metric.timestamp,
            type: 'Order Change',
            description: `Primary Stream changed to ${metric.stream_name}`,
            color: 'green'
          });
        }
        lastTopStreamId = metric.stream_id;
      }
    });

    return events;
  }, [session]);

  const minTime = useMemo(() => {
    // Find earliest metric or session start
    if (!session) return 0;
    let min = session.created_at || 0;
    if (session.streams) {
      session.streams.forEach(s => {
        if (s.metrics_history && s.metrics_history.length > 0) {
          if (s.metrics_history[0].timestamp < min) min = s.metrics_history[0].timestamp;
        }
      });
    }
    return min;
  }, [session]);

  const maxTime = useMemo(() => {
    if (!session) return Math.floor(Date.now() / 1000);
    // If active, use current time
    if (session.is_active) return Math.floor(Date.now() / 1000);

    // If inactive, find max timestamp in metrics
    let max = session.created_at || 0;
    if (session.streams) {
      session.streams.forEach(s => {
        if (s.metrics_history && s.metrics_history.length > 0) {
          const last = s.metrics_history[s.metrics_history.length - 1];
          if (last.timestamp > max) max = last.timestamp;
        }
      });
    }
    return max;
  }, [session]);


  if (!loading && !session && loadError) {
    return <div className="space-y-4 rounded-lg border p-6" role="alert">
      <p>Session details could not be loaded.</p>
      <div className="flex flex-wrap gap-2">
        <Button variant="outline" className="min-h-11" onClick={onBack}>Back to sessions</Button>
        <Button className="min-h-11" onClick={() => loadSession()}>Retry</Button>
      </div>
    </div>;
  }

  if (loading || !session) {
    return (
      <div className="text-center py-12">
        <div className="animate-spin rounded-full h-12 w-12 border-b-2 border-primary mx-auto mb-4"></div>
        <p className="text-muted-foreground">Loading session...</p>
      </div>
    );
  }

  return (
    <div className="min-w-0 space-y-5 pb-16">
      {loadError && <p role="alert" className="rounded-lg border border-destructive/30 bg-destructive/5 p-3 text-sm">Session refresh failed. These measurements may be out of date.</p>}
      {/* Header with Channel Logo and EPG Info */}
      <div className="flex min-w-0 flex-col gap-4 sm:flex-row sm:items-center sm:justify-between">
        <div className="flex items-center gap-4 min-w-0">
          <Button variant="ghost" size="icon" className="h-11 w-11 shrink-0" aria-label="Back to monitoring sessions" onClick={onBack}>
            <ArrowLeft className="h-5 w-5" />
          </Button>
          {logoUrl && (
            <div className="flex-shrink-0">
              <img
                src={logoUrl}
                alt={session.channel_name}
                className="h-11 w-11 object-contain rounded-md bg-muted p-1"
                onError={(e) => { e.target.style.display = 'none'; }}
              />
            </div>
          )}
          <div className="min-w-0">
            <h1 className="truncate text-2xl font-bold tracking-tight sm:text-3xl">{session.channel_name}</h1>
            <p className="text-muted-foreground mt-1 truncate">
              Session Monitor - {session.is_active ? 'Active' : 'Inactive'}
            </p>
          </div>
        </div>
        <div className="flex gap-2">
          {session.is_active && (
            <Button variant="outline" className="min-h-11 w-full sm:w-auto" onClick={onStop}>
              <Square className="h-4 w-4 mr-2" />
              Stop Monitoring
            </Button>
          )}
        </div>
      </div>

      <dl className="grid grid-cols-2 gap-x-6 gap-y-4 border-y py-4 sm:grid-cols-4">
        <SessionStat label="Total sources" value={session.streams.length} />
        <SessionStat label="Stable" value={stableStreams.length} className="text-emerald-700 dark:text-emerald-400" />
        <SessionStat label="Under review" value={reviewStreams.length} className="text-blue-700 dark:text-blue-400" />
        <SessionStat label="Quarantined" value={quarantinedStreams.length} className="text-amber-700 dark:text-amber-400" />
      </dl>
      <p className="text-sm text-muted-foreground">Average reliability of stable and review sources: <span className="font-medium text-foreground">{activeStreams.length > 0 ? `${calculateAverageScore(activeStreams)}%` : 'No measurements'}</span></p>

      {/* Streams Tables */}
      <Tabs defaultValue="stable" className="min-w-0 w-full">
        <TabsList className="grid h-auto w-full grid-cols-3 sm:inline-flex sm:w-auto">
          <TabsTrigger className="min-h-11 min-w-0 whitespace-normal px-2 text-xs sm:px-3 sm:text-sm" value="stable">
            Stable ({stableStreams.length})
          </TabsTrigger>
          <TabsTrigger className="min-h-11 min-w-0 whitespace-normal px-2 text-xs sm:px-3 sm:text-sm" value="review">
            Review ({reviewStreams.length})
          </TabsTrigger>
          <TabsTrigger className="min-h-11 min-w-0 whitespace-normal px-2 text-xs sm:px-3 sm:text-sm" value="quarantined">
            Quarantined ({quarantinedStreams.length})
          </TabsTrigger>
        </TabsList>

        <TabsContent value="stable" className="min-w-0">
          <Card>
            <CardHeader>
              <CardTitle>Stable Streams</CardTitle>
              <CardDescription>
                Streams that have passed review and are considered reliable.
              </CardDescription>
            </CardHeader>
            <CardContent>
              {stableStreams.length === 0 ? (
                <div className="text-center py-12">
                  <AlertCircle className="h-12 w-12 text-muted-foreground mx-auto mb-4" />
                  <p className="text-muted-foreground">No stable streams</p>
                </div>
              ) : (
                <StreamsTable
                  streams={stableStreams}
                  isOpenStream={session?.session_type === 'openstream'}
                  sessionId={sessionId}
                  onQuarantine={handleQuarantineStream}
                  playingStreamIds={playingStreamIds}
                  cursorTime={cursorTime}

                  isLive={isLive}
                  zoomLevel={zoomLevel}
                  adPeriods={session?.ad_periods || []}
                />
              )}
            </CardContent>
          </Card>
        </TabsContent>

        <TabsContent value="review" className="min-w-0">
          <Card>
            <CardHeader>
              <CardTitle>Under Review</CardTitle>
              <CardDescription>
                New or revived streams being monitored for reliability before becoming stable.
              </CardDescription>
            </CardHeader>
            <CardContent>
              {reviewStreams.length === 0 ? (
                <div className="text-center py-12">
                  <Activity className="h-12 w-12 text-muted-foreground mx-auto mb-4" />
                  <p className="text-muted-foreground">No streams under review</p>
                </div>
              ) : (
                <StreamsTable
                  streams={reviewStreams}
                  isOpenStream={session?.session_type === 'openstream'}
                  sessionId={sessionId}
                  onQuarantine={handleQuarantineStream}
                  playingStreamIds={playingStreamIds}
                  cursorTime={cursorTime}
                  isLive={isLive}
                  zoomLevel={zoomLevel}
                  isReview
                  adPeriods={session?.ad_periods || []}
                />
              )}
            </CardContent>
          </Card>
        </TabsContent>

        <TabsContent value="quarantined" className="min-w-0">
          <Card>
            <CardHeader>
              <CardTitle>Quarantined Streams</CardTitle>
              <CardDescription>
                Streams that failed quality checks or are dead. They will be retried automatically after a cooldown.
              </CardDescription>
            </CardHeader>
            <CardContent>
              {quarantinedStreams.length === 0 ? (
                <div className="text-center py-12">
                  <Activity className="h-12 w-12 text-muted-foreground mx-auto mb-4" />
                  <p className="text-muted-foreground">No quarantined streams</p>
                </div>
              ) : (
                <StreamsTable
                  streams={quarantinedStreams}
                  isOpenStream={session?.session_type === 'openstream'}
                  sessionId={sessionId}
                  showQuarantined
                  onRevive={handleReviveStream}
                  adPeriods={session?.ad_periods || []}
                />
              )}
            </CardContent>
          </Card>
        </TabsContent>
      </Tabs>

      {/* Live Stream Preview */}
      {activeStreams.length > 0 && (
        <details className="min-w-0 overflow-hidden rounded-lg border bg-card" onToggle={event => { if (!event.currentTarget.open) setActivePreviewTab(''); }}>
          <summary className="cursor-pointer rounded-lg px-4 py-4 text-sm font-medium focus-visible:outline focus-visible:outline-2 focus-visible:outline-primary">Screenshots and live previews</summary>
          <div className="min-w-0 px-4 pb-4">
            <Tabs value={activePreviewTab} onValueChange={setActivePreviewTab} className="w-full">
              <TabsList className="mb-4 h-auto max-w-full">
                <TabsTrigger className="min-h-11" value="screenshots">Screenshots</TabsTrigger>
                <TabsTrigger className="min-h-11" value="live">Live Streams</TabsTrigger>
              </TabsList>

              {!activePreviewTab && (
                <div className="rounded-lg border border-dashed bg-muted/10 px-3 py-5 text-center text-sm text-muted-foreground">
                  <p>Select a preview mode above to view stream activity</p>
                </div>
              )}

              <TabsContent value="screenshots" className="mt-0 outline-none">
                {activePreviewTab === 'screenshots' && (
                  aliveScreenshots.length > 0 ? (
                    <div className="w-full relative overflow-hidden">
                      <div className="max-w-full overflow-x-auto pb-2 scrollbar-thin scrollbar-thumb-gray-400 scrollbar-track-gray-200 dark:scrollbar-thumb-gray-600 dark:scrollbar-track-gray-800">
                        <div className="flex gap-4 pb-4">
                          {aliveScreenshots.map((screenshot) => (
                            <div key={screenshot.stream_id} className="w-64 flex-none sm:w-80">
                              <Card>
                                <CardContent className="p-4">
                                  <div className="aspect-video bg-black rounded-md overflow-hidden mb-3">
                                    <img
                                      src={screenshot.screenshot_url}
                                      alt={screenshot.stream_name}
                                      className="w-full h-full object-contain"
                                      onError={(e) => {
                                        e.target.src = FALLBACK_IMAGE_SVG;
                                      }}
                                    />
                                  </div>
                                  <div className="space-y-1">
                                    <p className="font-medium text-sm truncate" title={screenshot.stream_name}>
                                      {screenshot.stream_name}
                                    </p>
                                    <p className="text-xs text-muted-foreground">
                                      Stream ID: {screenshot.stream_id}
                                    </p>
                                  </div>
                                </CardContent>
                              </Card>
                            </div>
                          ))}
                        </div>
                      </div>
                    </div>
                  ) : (
                    <div className="text-center py-12">
                      <ImageIcon className="h-12 w-12 text-muted-foreground mx-auto mb-4" />
                      <p className="text-muted-foreground">No screenshots available yet</p>
                    </div>
                  )
                )}
              </TabsContent>

              <TabsContent value="live">
                {activePreviewTab === "live" && (
                  <LiveStreamsGrid streams={activeStreams.filter(s => (s.speed || s.current_speed || 0) >= 0.9)} sessionId={sessionId} isActive={activePreviewTab === "live"} />
                )}
              </TabsContent>
            </Tabs>
          </div>
        </details>
      )}

      {(session.epg_event_title || session.epg_event_description) && (
        <details className="rounded-lg border bg-card">
          <summary className="cursor-pointer rounded-lg px-4 py-3 text-sm font-medium focus-visible:outline focus-visible:outline-2 focus-visible:outline-primary">
            {session.epg_event_title || 'Programme details'}
          </summary>
          <div className="space-y-2 px-4 pb-4 text-sm text-muted-foreground">
            {session.epg_event_description && <p>{session.epg_event_description}</p>}
            {session.epg_event_start && <p>Start: {new Date(session.epg_event_start).toLocaleString()}</p>}
            {session.epg_event_end && <p>End: {new Date(session.epg_event_end).toLocaleString()}</p>}
          </div>
        </details>
      )}

      {!showTimeline && (
        <div className="fixed bottom-20 right-4 z-30 lg:bottom-6 lg:right-6">
          <Button variant="outline" onClick={() => setShowTimeline(true)} className="min-h-11 gap-2 rounded-full bg-card px-5 shadow-lg">
            Show Timeline <ChevronUp className="h-4 w-4" aria-hidden="true" />
          </Button>
        </div>
      )}

      {/* Timeline Control */}
      {session && showTimeline && (
        <TimelineControl
          minTime={minTime}
          maxTime={maxTime}
          currentTime={cursorTime || maxTime}
          onTimeChange={handleTimeChange}
          isLive={isLive}
          onLiveClick={handleLiveClick}
          events={timelineEvents}
          streams={session.streams || []}
          zoomLevel={zoomLevel}
          onZoomChange={setZoomLevel}
          adPeriods={session?.ad_periods || []}
          showTimeline={showTimeline}
          onToggleTimeline={() => setShowTimeline(!showTimeline)}
        />
      )}
    </div >
  );
}

function SessionStat({ label, value, className = '' }) {
  return (
    <div className="min-w-0">
      <dt className="text-xs text-muted-foreground">{label}</dt>
      <dd className={`mt-1 text-2xl font-semibold tabular-nums ${className}`}>{value}</dd>
    </div>
  );
}

// Transport Health Badge Helper
const TransportHealthBadge = ({ status, summary, errorDensity }) => {
  const getStyles = () => {
    switch (status) {
      case 'Healthy': return 'bg-green-500/10 text-green-600 border-green-500/20';
      case 'Degraded': return 'bg-yellow-500/10 text-yellow-600 border-yellow-500/20';
      case 'Severe': return 'bg-orange-500/10 text-orange-600 border-orange-500/20';
      case 'Critical': return 'bg-red-500/10 text-red-600 border-red-500/20';
      default: return 'bg-gray-500/10 text-gray-600 border-gray-500/20';
    }
  };

  return (
    <div className="flex flex-col gap-1" title={summary || ''}>
      <Badge
        variant="outline"
        className={`text-[10px] px-2 h-5 uppercase font-bold flex items-center justify-center ${getStyles()}`}
      >
        <span>{status || 'Healthy'}</span>
      </Badge>
      {errorDensity > 0 && (
        <span className="text-[10px] text-muted-foreground font-medium">
          Worst: {errorDensity}/m
        </span>
      )}
    </div>
  );
};

// Streams Table Component
function StreamsTable({ streams, isOpenStream = false, sessionId, onQuarantine, onRevive, playingStreamIds = new Set(), showQuarantined = false, isReview = false, cursorTime, isLive, zoomLevel, adPeriods = [] }) {
  const [expandedChartId, setExpandedChartId] = useState(null);
  const formatQuality = (stream) => {
    if (!stream.width || !stream.height) return 'Unknown';
    return `${stream.width}x${stream.height}`;
  };

  const formatBitrate = (bitrate) => {
    if (!bitrate) return 'N/A';
    if (bitrate >= 1000) {
      return `${(bitrate / 1000).toFixed(1)} Mbps`;
    }
    return `${bitrate} kbps`;
  };

  const formatDownload = (kbps) => {
    if (kbps == null || kbps <= 0) return 'N/A';
    if (kbps >= 1000) {
      return `${(kbps / 1000).toFixed(1)} Mbps`;
    }
    return `${kbps.toFixed(0)} kbps`;
  };

  const formatTimeRemaining = (seconds) => {
    if (seconds === undefined || seconds === null) return '-';
    if (seconds <= 0) return 'Stable soon';
    const mins = Math.floor(seconds / 60);
    const secs = Math.floor(seconds % 60);
    return `${mins}:${secs.toString().padStart(2, '0')}`;
  };

  return (
    <div className="min-w-0 max-w-full">
      <p className="mb-2 text-xs text-muted-foreground xl:hidden">Scroll sideways to see all source measurements and controls.</p>
      <div className="max-w-full overflow-x-auto rounded-md focus-visible:outline focus-visible:outline-2 focus-visible:outline-primary" role="region" aria-label="Source measurements" tabIndex={0}>
      <Table className="min-w-[900px]">
        <TableHeader>
          <TableRow>
            <TableHead className="w-12">#</TableHead>
            <TableHead>Name</TableHead>
            {!isOpenStream && <TableHead>Quality</TableHead>}
            {!isOpenStream && <TableHead>FPS</TableHead>}
            <TableHead>{isOpenStream ? 'Keep-up' : 'Speed'}</TableHead>
            {isOpenStream ? (
              <>
                <TableHead>Download</TableHead>
                <TableHead>Peers/Seeders</TableHead>
                <TableHead>Latency</TableHead>
                <TableHead>Swarm Score</TableHead>
              </>
            ) : (
              <TableHead>Bitrate</TableHead>
            )}
            {!isOpenStream && <TableHead>Logo Verify</TableHead>}
            <TableHead>{isOpenStream ? 'Swarm Health' : 'Transport Health'}</TableHead>
            {!showQuarantined ? (
              <>
                <TableHead>Reliability</TableHead>
                {isReview && <TableHead>Time into Stable</TableHead>}
                <TableHead>Actions</TableHead>
              </>
            ) : (
              <>
                <TableHead>Score</TableHead>
                <TableHead>Actions</TableHead>
              </>
            )}
          </TableRow>
        </TableHeader>
        <TableBody>
          {streams.map((stream, index) => (
            <React.Fragment key={stream.stream_id}>
              <TableRow>
                <TableCell className="font-medium">{index + 1}</TableCell>
                <TableCell className="min-w-[180px] max-w-xs">
                  <div className="flex items-center gap-2">
                    <span className="truncate" title={stream.name}>
                      {stream.name}
                    </span>
                    {playingStreamIds.has(stream.stream_id) && (
                      <div className="flex items-center justify-center bg-green-100 dark:bg-green-900/30 p-1 rounded-full" title="Broadcasting">
                        <Radio className="h-3 w-3 text-green-600 dark:text-green-400" />
                      </div>
                    )}
                    {stream.status === 'quarantined' && (
                      <Badge variant="destructive" className="text-[10px] px-1 py-0 h-4 uppercase font-bold">
                        Dead
                      </Badge>
                    )}
                    {(stream.status === 'review' || stream.status === 'quarantined') && stream.status_reason === 'looping' && (
                      <Badge variant="outline" className="bg-yellow-500/10 text-yellow-600 border-yellow-500/20 text-[10px] px-2 h-5 uppercase font-bold flex items-center justify-center leading-none">
                        <span className="mt-[0.5px]">Looping {stream.loop_duration ? `(${stream.loop_duration.toFixed(1)}s)` : ''}</span>
                      </Badge>
                    )}
                    {stream.status === 'quarantined' && stream.status_reason === 'logo-mismatch' && (
                      <Badge variant="outline" className="bg-red-500/10 text-red-600 border-red-500/20 text-[10px] px-2 h-5 uppercase font-bold">
                        Logo Mismatch
                      </Badge>
                    )}
                  </div>
                  {stream.status === 'quarantined' && stream.status_reason === 'logo-mismatch' && stream.screenshot_url && (
                    <div className="mt-2 text-xs text-muted-foreground">
                      <p className="mb-1">Last seen:</p>
                      <a className="block w-24 aspect-video bg-black rounded overflow-hidden focus-visible:outline focus-visible:outline-2 focus-visible:outline-primary" href={stream.screenshot_url} target="_blank" rel="noreferrer" aria-label={`Open logo mismatch screenshot for ${stream.name}`}>
                        <img
                          src={stream.screenshot_url}
                          alt="Logo mismatch screenshot"
                          className="w-full h-full object-contain transition-transform hover:scale-105"
                        />
                      </a>
                    </div>
                  )}
                </TableCell>
                {!isOpenStream && (
                  <TableCell>
                    <div className="flex items-center gap-2">
                      <span>{formatQuality(stream)}</span>
                      {stream.hdr_format && (
                        <Badge variant="outline" className="bg-blue-500/10 text-blue-500 border-blue-500/20 text-xs px-2 py-0 h-5">
                          {stream.hdr_format}
                        </Badge>
                      )}
                    </div>
                  </TableCell>
                )}
                {!isOpenStream && (
                  <TableCell>{stream.fps ? `${stream.fps.toFixed(0)} fps` : 'N/A'}</TableCell>
                )}
                <TableCell>
                  {(() => {
                    const margin = isOpenStream
                      ? (stream.keepup_margin ?? stream.current_speed ?? 0)
                      : (stream.speed ?? stream.current_speed ?? 0);
                    return (
                      <span className={`font-mono ${margin < 0.9 ? 'text-orange-500' : 'text-green-500'}`}>
                        {margin.toFixed(2)}{isOpenStream ? '' : 'x'}
                      </span>
                    );
                  })()}
                </TableCell>
                {isOpenStream ? (
                  <>
                    <TableCell className="font-mono">{formatDownload(stream.download_kbps)}</TableCell>
                    <TableCell className="font-mono">
                      <span className="text-green-500">{stream.seeders ?? 0}</span>
                      <span className="text-muted-foreground"> / {stream.peers ?? 0}</span>
                    </TableCell>
                    <TableCell className="font-mono">
                      {stream.latency_secs != null ? `${stream.latency_secs}s` : 'N/A'}
                    </TableCell>
                    <TableCell className="font-mono">
                      {stream.swarm_reliability != null ? `${(stream.swarm_reliability * 100).toFixed(0)}%` : 'N/A'}
                    </TableCell>
                  </>
                ) : (
                  <TableCell>{formatBitrate(stream.bitrate)}</TableCell>
                )}
                {!isOpenStream && (
                <TableCell>
                  <div className="flex flex-col gap-1">
                    <Badge
                      variant="outline"
                      className={`text-[10px] px-2 h-5 uppercase font-bold flex items-center justify-center leading-none ${stream.display_logo_status === 'SUCCESS' ? 'bg-green-500/10 text-green-600 border-green-500/20' :
                        stream.display_logo_status === 'FAILED' ? 'bg-red-500/10 text-red-600 border-red-500/20' :
                          'bg-gray-500/10 text-gray-600 border-gray-500/20'
                        }`}
                    >
                      <span className="mt-[0.5px]">{stream.display_logo_status || 'PENDING'}</span>
                    </Badge>
                    {stream.consecutive_logo_misses > 0 && (
                      <span className="text-[10px] text-red-500 font-medium">
                        Misses: {stream.consecutive_logo_misses}
                      </span>
                    )}
                  </div>
                </TableCell>
                )}
                <TableCell>
                  <TransportHealthBadge
                    status={stream.transport_health} 
                    summary={stream.transport_health_summary}
                    errorDensity={stream.transport_error_density}
                  />
                </TableCell>
                {!showQuarantined && (
                  <>
                    <TableCell>
                      <div className="flex items-center gap-2">
                        <Progress
                          value={stream.reliability_score}
                          className="w-20 h-2"
                        />
                        <span className="text-sm font-medium">
                          {stream.reliability_score.toFixed(1)}
                        </span>
                      </div>
                    </TableCell>
                    {isReview && (
                      <TableCell>
                        <div className="flex items-center gap-2 text-blue-600 dark:text-blue-400 font-medium">
                          <Clock className="h-4 w-4" />
                          {formatTimeRemaining(stream.review_time_remaining)}
                        </div>
                      </TableCell>
                    )}
                    <TableCell>
                      <div className="flex gap-2">
                        <Button
                          size="icon"
                          variant="outline"
                          onClick={() => onQuarantine(stream.stream_id)}
                          className="h-11 w-11 text-orange-600 hover:text-orange-700 hover:bg-orange-50 dark:hover:bg-orange-950"
                          title="Quarantine"
                          aria-label={`Quarantine ${stream.name}`}
                        >
                          <Ban className="h-4 w-4" />
                        </Button>
                      </div>
                    </TableCell>
                  </>
                )}
                {showQuarantined && onRevive && (
                  <>
                    <TableCell>
                      <span className="text-sm text-muted-foreground">
                        Score: {stream.reliability_score.toFixed(1)}
                      </span>
                    </TableCell>
                    <TableCell>
                      <Button
                        size="sm"
                        variant="outline"
                        onClick={() => onRevive(stream.stream_id)}
                        className="min-h-11"
                        aria-label={`Revive ${stream.name}`}
                      >
                        <Activity className="h-3 w-3 mr-1" />
                        Revive
                      </Button>
                    </TableCell>
                  </>
                )}
              </TableRow>
              {!showQuarantined && (
                <TableRow>
                  <TableCell colSpan={10 + (isReview ? 1 : 0)} className="bg-muted/30 p-2">
                    <button
                      type="button"
                      className="min-h-11 rounded px-2 py-1 text-xs font-semibold text-primary hover:underline focus-visible:outline focus-visible:outline-2 focus-visible:outline-primary"
                      aria-expanded={expandedChartId === stream.stream_id}
                      onClick={() => setExpandedChartId(current => current === stream.stream_id ? null : stream.stream_id)}
                    >
                      {expandedChartId === stream.stream_id ? 'Hide' : 'Show'} speed history for {stream.name}
                    </button>
                    {expandedChartId === stream.stream_id && (
                      <SpeedMetricsChart sessionId={sessionId} streamId={stream.stream_id} cursorTime={cursorTime} isLive={isLive} zoomLevel={zoomLevel} />
                    )}
                  </TableCell>
                </TableRow>
              )}
            </React.Fragment>
          ))}
        </TableBody>
      </Table>
      </div>
    </div>
  );
}

// Speed Metrics Chart Component
function SpeedMetricsChart({ sessionId, streamId, cursorTime, isLive, zoomLevel }) {
  const [allMetrics, setAllMetrics] = useState([]);
  const [loading, setLoading] = useState(true);
  const lastTimestampRef = useRef(null);

  useEffect(() => {
    let stopped = false;
    let timer;
    let polling = false;
    const loadMetrics = async () => {
      try {
        const response = await streamSessionsAPI.getStreamMetrics(sessionId, streamId, lastTimestampRef.current);
        if (stopped) return;
        const newMetrics = response.data?.metrics || [];
        if (newMetrics.length > 0) {
          lastTimestampRef.current = newMetrics[newMetrics.length - 1].timestamp;
          setAllMetrics(previous => {
            const firstNewTimestamp = newMetrics[0].timestamp;
            return [...previous.filter(metric => metric.timestamp < firstNewTimestamp), ...newMetrics].slice(-3600);
          });
        }
      } catch (err) {
        if (!stopped) console.error('Failed to load metrics:', err);
      } finally {
        if (!stopped) setLoading(false);
      }
    };
    const poll = async () => {
      if (stopped || polling) return;
      polling = true;
      try {
        if (document.visibilityState === 'visible') await loadMetrics();
      } finally {
        polling = false;
        if (!stopped) timer = setTimeout(poll, 5000);
      }
    };
    void poll();
    const onVisibilityChange = () => {
      if (document.visibilityState === 'visible') {
        clearTimeout(timer);
        void poll();
      }
    };
    document.addEventListener('visibilitychange', onVisibilityChange);
    return () => {
      stopped = true;
      clearTimeout(timer);
      document.removeEventListener('visibilitychange', onVisibilityChange);
    };
  }, [sessionId, streamId]);

  // Determine the reference time (end of the visible window)
  const referenceTime = useMemo(() => {
    if (isLive) {
      const lastMetric = allMetrics[allMetrics.length - 1];
      return lastMetric ? lastMetric.timestamp : Math.floor(Date.now() / 1000);
    } else {
      // In history mode, use cursor time or fallback to latest metric
      if (cursorTime) return cursorTime;
      const lastMetric = allMetrics[allMetrics.length - 1];
      return lastMetric ? lastMetric.timestamp : Math.floor(Date.now() / 1000);
    }
  }, [allMetrics, cursorTime, isLive]);

  const endTimestamp = Math.ceil(referenceTime);
  const startTime = referenceTime - zoomLevel;

  const chartData = useMemo(() => {
    if (!allMetrics.length) return [];

    return allMetrics.filter(m =>
      m.timestamp >= startTime && m.timestamp <= endTimestamp
    ).map((metric) => {
      // Timestamp is in Unix seconds, convert to milliseconds for JavaScript Date
      const date = new Date(metric.timestamp * 1000);
      // Format as HH:MM:SS for better granularity
      const hours = date.getHours().toString().padStart(2, '0');
      const minutes = date.getMinutes().toString().padStart(2, '0');
      const seconds = date.getSeconds().toString().padStart(2, '0');
      // Map quarantined streams to 0 speed in chart
      let displaySpeed = metric.speed || 0;
      if (metric.status === 'quarantined') {
        displaySpeed = 0;
      }

      return {
        time: `${hours}:${minutes}:${seconds}`,
        speed: displaySpeed,
        timestamp: metric.timestamp, // Keep original for reference
      };
    });
  }, [allMetrics, startTime, endTimestamp]);

  const formatTime = (ts) => {
    const d = new Date(ts * 1000);
    return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
  };

  if (loading) {
    return <div className="flex h-24 items-center justify-center text-sm text-muted-foreground">Loading metrics...</div>;
  }

  // If we have no metrics AT ALL for this stream (never recorded)
  if (allMetrics.length === 0) {
    return (
      <div className="h-24 flex items-center justify-center text-muted-foreground text-sm">
        No metrics recorded
      </div>
    );
  }

  // If we have metrics but none in the current view (Gap in data)
  if (chartData.length === 0) {
    return (
      <div className="h-24 flex flex-col items-center justify-center text-muted-foreground text-sm">
        <span>No data in this time range</span>
        <span className="text-xs opacity-70">
          ({formatTime(startTime)} - {formatTime(endTimestamp)})
        </span>
      </div>
    );
  }

  return (
    <div className="space-y-1">
      <div className="text-xs text-muted-foreground font-medium px-2">FFmpeg Speed</div>
      <ResponsiveContainer width="100%" height={80}>
        <LineChart data={chartData} margin={{ top: 5, right: 10, left: 0, bottom: 5 }}>
          <CartesianGrid strokeDasharray="3 3" opacity={0.3} />
          <XAxis
            dataKey="timestamp"
            tickFormatter={formatTime}
            tick={{ fontSize: 10 }}
            domain={[startTime, endTimestamp]}
            type="number"
            interval="preserveStartEnd"
          />
          <YAxis
            tickFormatter={(value) => value}
            tick={{ fontSize: 10 }}
            width={35}
            domain={[0, 'auto']}
          />
          <RechartsTooltip
            contentStyle={{
              backgroundColor: 'hsl(var(--background))',
              border: '1px solid hsl(var(--border))',
              borderRadius: '6px',
              fontSize: '12px'
            }}
            labelFormatter={(value) => formatTime(value)}
            formatter={(value) => [`${value.toFixed(2)}x`, 'Speed']}
          />
          <Line
            type="monotone"
            dataKey="speed"
            stroke="hsl(var(--primary))"
            strokeWidth={2}
            dot={false}
            animationDuration={300}
          />
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}

// Screenshot Dialog Component  
// LiveStreamsGrid Component with mpegts.js support
function LiveStreamsGrid({ streams, sessionId, isActive }) {
  const [mpegtsLib, setMpegtsLib] = React.useState(null);
  const [loading, setLoading] = React.useState(true);
  const [error, setError] = React.useState(null);

  useEffect(() => {
    // Import mpegts.js once for all stream players
    const loadMpegts = async () => {
      try {
        const MpegtsModule = await import('mpegts.js');
        let mpegts = MpegtsModule.default || MpegtsModule;
        
        if (typeof mpegts.createPlayer !== 'function') {
           mpegts = MpegtsModule.mpegts || mpegts;
        }

        setMpegtsLib(() => mpegts);
        setLoading(false);
      } catch (err) {
        console.error('Failed to dynamic import mpegts.js:', err);
        setError(`Failed to load player library: ${err.message}`);
        setLoading(false);
      }
    };

    loadMpegts();
  }, []);

  const [expandedStreamId, setExpandedStreamId] = useState(null);

  // Consistent sorting: Expanded stream always at index 0
  const displayStreams = React.useMemo(() => {
    if (!expandedStreamId) return streams;
    const expanded = streams.find(s => s.stream_id === expandedStreamId);
    if (!expanded) return streams;
    const others = streams.filter(s => s.stream_id !== expandedStreamId);
    return [expanded, ...others];
  }, [streams, expandedStreamId]);

  // Track original grid positions for animation origin context
  const originalIndices = React.useMemo(() => {
    const map = {};
    streams.forEach((s, i) => map[s.stream_id] = i);
    return map;
  }, [streams]);

  if (loading) {
    return (
      <div className="text-center py-12">
        <div className="animate-spin rounded-full h-12 w-12 border-b-2 border-primary mx-auto mb-4"></div>
        <p className="text-muted-foreground">Loading stream player...</p>
      </div>
    );
  }

  // Unified layout with CSS transitions
  if (!expandedStreamId) {
    return (
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6 transition-all duration-500">
        {displayStreams.map((stream) => (
          <div key={stream.stream_id} className="w-full min-w-0">
            <LiveStreamPlayer
              stream={stream}
              mpegtsLib={mpegtsLib}
              isExpanded={false}
              isActive={isActive}
              onToggleExpand={() => setExpandedStreamId(stream.stream_id)}
            />
          </div>
        ))}
      </div>
    );
  }

  // Expanded layout: Flexbox with sidebar
  const expandedStream = displayStreams[0];
  const sidebarStreams = displayStreams.slice(1);

  return (
    <div className="flex flex-col lg:flex-row gap-6 min-h-[650px] transition-all duration-700 ease-in-out">
      {/* Main Expanded Stream */}
      <div className="flex-1 w-full animate-in fade-in zoom-in-95 duration-700">
        <LiveStreamPlayer
          stream={expandedStream}
          mpegtsLib={mpegtsLib}
          isExpanded={true}
          isActive={isActive}
          onToggleExpand={() => setExpandedStreamId(null)}
        />
      </div>

      {/* Sidebar Streams */}
      <div className="w-full lg:w-[320px] flex flex-col gap-4 overflow-y-auto max-h-[750px] pr-2 scrollbar-thin scrollbar-thumb-zinc-800">
        {sidebarStreams.map((stream) => (
          <div key={stream.stream_id} className="w-full animate-in fade-in slide-in-from-right-12 duration-500">
            <LiveStreamPlayer
              stream={stream}
              mpegtsLib={mpegtsLib}
              isExpanded={false}
              isActive={isActive}
              onToggleExpand={() => setExpandedStreamId(stream.stream_id)}
            />
          </div>
        ))}
      </div>
    </div>
  );
}

// Live Stream Player Component using mpegts.js
function LiveStreamPlayer({ stream, mpegtsLib, isExpanded, isActive, onToggleExpand }) {
  const videoRef = React.useRef(null);
  const playerRef = React.useRef(null);
  const [streamUrl, setStreamUrl] = React.useState(null);
  const [loading, setLoading] = React.useState(true);
  const [error, setError] = React.useState(null);
  const [isMuted, setIsMuted] = React.useState(true);
  const [retryKey, setRetryKey] = React.useState(0);
  const { toast } = useToast();

  // Helper function to clean up player instance
  const cleanupPlayer = React.useCallback(() => {
    if (playerRef.current) {
      try {
        playerRef.current.destroy();
      } catch (err) {
        console.error('Error destroying player:', err);
      }
      playerRef.current = null;
    }

    // Also clean up the video element directly for native HLS (Safari/iOS)
    if (videoRef.current) {
      try {
        videoRef.current.pause();
        videoRef.current.src = "";
        videoRef.current.load();
      } catch (err) {
        console.warn('Error clearing video element:', err);
      }
    }
  }, []);

  useEffect(() => {
    // Load stream URL
    const loadStreamUrl = async () => {
      try {
        const response = await streamSessionsAPI.getStreamViewerUrl(stream.stream_id);
        if (response.data.success) {
          setStreamUrl(response.data.stream_url);
          setLoading(false);
        } else {
          setError(response.data.error || 'Failed to get stream URL');
          setLoading(false);
        }
      } catch (err) {
        console.error('Failed to get stream URL:', err);
        setError('Failed to load stream');
        setLoading(false);
      }
    };

    loadStreamUrl();
  }, [stream.stream_id]);

  useEffect(() => {
    // Initialize mpegts.js player when stream URL is available AND active
    if (!streamUrl || !videoRef.current || !mpegtsLib || !isActive) {
      cleanupPlayer();
      return;
    }

    let isMounted = true;

    const initPlayer = async () => {
      try {
        cleanupPlayer();
        if (!isMounted) return;

        if (mpegtsLib.getFeatureList().mseLivePlayback) {
          const player = mpegtsLib.createPlayer({
            type: 'mpegts',
            isLive: true,
            url: streamUrl,
            enableStashBuffer: false, // Reduced latency
            liveBufferLatencyChasing: true, // Reduced latency
            fixAudioTimestampGap: true, // Tolerate minor TS timestamp issues
          }, {
            enableWorker: true,
            lazyLoad: false,
          });

          playerRef.current = player;
          player.attachMediaElement(videoRef.current);
          player.load();
          
          player.on(mpegtsLib.Events.ERROR, (type, detail, info) => {
            console.warn('mpegts error:', type, detail, info);
            // Only show error UI for fatal network or media errors.
            // Non-fatal demux warnings (e.g. sync_byte mismatches during initial
            // buffering) are logged to console but don't break playback.
            const isFatal =
              type === mpegtsLib.ErrorTypes.NETWORK_ERROR ||
              (type === mpegtsLib.ErrorTypes.MEDIA_ERROR &&
                detail === mpegtsLib.ErrorDetails.MEDIA_MSE_ERROR);
            if (isMounted && isFatal) {
              setError(`Playback error: ${detail}`);
            }
          });

          player.play().catch(err => {
            console.warn('Autoplay blocked:', err);
          });
        } else {
          setError('Browser does not support MPEG-TS playback');
        }
      } catch (err) {
        console.error('Error initializing mpegts player:', err);
        if (isMounted) setError(`Player error: ${err.message}`);
      }
    };

    initPlayer();

    return () => {
      isMounted = false;
      cleanupPlayer();
    };
  }, [streamUrl, mpegtsLib, retryKey, isActive, cleanupPlayer]);

  const handleRetry = () => {
    setError(null);
    setRetryKey(prev => prev + 1);
  };

  const formatQuality = () => {
    if (!stream.width || !stream.height) return 'Unknown';
    return `${stream.width}x${stream.height}`;
  };

  const formatBitrate = () => {
    if (!stream.bitrate) return 'N/A';
    if (stream.bitrate >= 1000) {
      return `${(stream.bitrate / 1000).toFixed(1)} Mbps`;
    }
    return `${stream.bitrate} kbps`;
  };

  const handleToggleMute = () => {
    if (videoRef.current) {
      videoRef.current.muted = !isMuted;
      setIsMuted(!isMuted);
    }
  };

  return (
    <Card className={cn(
      "transition-all duration-500 ease-in-out border border-white/5",
      isExpanded ? "h-full shadow-2xl ring-1 ring-primary/20" : "hover:scale-[1.02] shadow-md"
    )}>
      <CardContent className={cn(
        "p-4 transition-all duration-500",
        isExpanded ? "flex flex-col h-full bg-zinc-950/40" : ""
      )}>
        <div className={cn(
          "bg-black rounded-md overflow-hidden mb-3 relative group transition-all duration-500 shadow-inner border border-white/5",
          isExpanded ? "flex-1 min-h-[450px]" : "aspect-video"
        )}>
          {loading ? (
            <div className="w-full h-full flex items-center justify-center">
              <div className="animate-spin rounded-full h-12 w-12 border-b-2 border-primary"></div>
            </div>
          ) : error ? (
            <div className="w-full h-full flex flex-col items-center justify-center text-destructive">
              <div className="text-center p-4">
                <AlertCircle className="h-12 w-12 mx-auto mb-2" />
                <p className="text-sm mb-3">{error}</p>
                <Button
                  size="sm"
                  variant="outline"
                  onClick={handleRetry}
                  className="text-sm"
                >
                  Retry
                </Button>
              </div>
            </div>
          ) : (
            <>
              <video
                ref={videoRef}
                muted={isMuted}
                playsInline
                className="w-full h-full object-contain [&::-webkit-media-controls]:hidden [&::-webkit-media-controls-enclosure]:hidden"
              />
              <div className="absolute top-2 right-2 flex gap-2 opacity-100 transition-opacity sm:opacity-0 sm:group-hover:opacity-100 sm:group-focus-within:opacity-100">
                <Button
                  size="sm"
                  variant="secondary"
                  className="h-11 w-11 p-0"
                  onClick={handleToggleMute}
                  title={isMuted ? "Unmute" : "Mute"}
                  aria-label={isMuted ? "Unmute stream" : "Mute stream"}
                >
                  {isMuted ? <VolumeX className="h-4 w-4" /> : <Volume2 className="h-4 w-4" />}
                </Button>

                {onToggleExpand && (
                  <Button
                    size="sm"
                    variant="secondary"
                    className="h-11 w-11 p-0"
                    onClick={onToggleExpand}
                    title={isExpanded ? "Minimize" : "Expand"}
                    aria-label={isExpanded ? "Minimize stream preview" : "Expand stream preview"}
                  >
                    {isExpanded ? <Minimize2 className="h-4 w-4" /> : <Maximize2 className="h-4 w-4" />}
                  </Button>
                )}
              </div>
            </>
          )}
        </div>
        <div className="space-y-2">
          <p className="font-medium text-sm truncate" title={stream.name}>
            {stream.name}
          </p>
          <div className="grid grid-cols-2 gap-2 text-xs text-muted-foreground">
            <div>
              <span className="font-medium">Quality:</span> {formatQuality()}
            </div>
            <div>
              <span className="font-medium">FPS:</span> {stream.fps ? `${stream.fps.toFixed(1)}` : 'N/A'}
            </div>
            <div>
              <span className="font-medium">Bitrate:</span> {formatBitrate()}
            </div>
            <div>
              <span className="font-medium">Score:</span> {stream.reliability_score.toFixed(1)}%
            </div>
          </div>
        </div>
      </CardContent>
    </Card>
  );
}

// Helper function
function calculateAverageScore(streams) {
  if (streams.length === 0) return 0;
  const sum = streams.reduce((acc, s) => acc + (s.reliability_score || 0), 0);
  return (sum / streams.length).toFixed(1);
}

export default SessionMonitorView;
