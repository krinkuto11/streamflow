import React, { useRef, useEffect, useState, useMemo } from 'react';
import { Button } from "@/components/ui/button";
import { Slider } from "@/components/ui/slider";
import { Play, Pause, SkipForward, ChevronDown } from "lucide-react";
import { Card } from "@/components/ui/card";

/**
 * TimelineControl component
 * 
 * A video-editor style timeline with a time ruler, scrubbable playhead, and status-based subway lanes.
 */
export function TimelineControl({ minTime, maxTime, currentTime, onTimeChange, isLive, onLiveClick, streams = [], zoomLevel = 60, onZoomChange, adPeriods = [], showTimeline, onToggleTimeline, className }) {
    const [isPlaying, setIsPlaying] = useState(false);
    const containerRef = useRef(null);
    const playbackRef = useRef(null);
    const [isDragging, setIsDragging] = useState(false);
    const [hoveredStreamId, setHoveredStreamId] = useState(null);

    // Calculate total duration
    const duration = Math.max(1, maxTime - minTime);

    // Auto-playback logic
    useEffect(() => {
        if (isPlaying) {
            playbackRef.current = setInterval(() => {
                onTimeChange(prev => {
                    const next = prev + 1;
                    if (next >= maxTime) {
                        setIsPlaying(false);
                        return maxTime;
                    }
                    return next;
                });
            }, 1000);
        } else {
            clearInterval(playbackRef.current);
        }
        return () => clearInterval(playbackRef.current);
    }, [isPlaying, maxTime, onTimeChange]);

    // Handle mouse events for scrubbing
    const handleMouseDown = (e) => {
        if (!containerRef.current) return;
        const rect = containerRef.current.getBoundingClientRect();
        const y = e.clientY - rect.top;

        if (y < 30 || e.shiftKey) {
            setIsDragging(true);
            setIsPlaying(false);
            updateTimeFromMouse(e);
        }
    };

    const updateTimeFromMouse = (e) => {
        if (!containerRef.current) return;
        const rect = containerRef.current.getBoundingClientRect();
        const x = Math.max(0, Math.min(e.clientX - rect.left, rect.width));
        const percentage = x / rect.width;
        const newTime = minTime + (percentage * duration);
        onTimeChange(Math.floor(newTime));
    };

    useEffect(() => {
        const handleMouseMove = (e) => { if (isDragging) updateTimeFromMouse(e); };
        const handleMouseUp = () => { setIsDragging(false); };
        if (isDragging) {
            window.addEventListener('mousemove', handleMouseMove);
            window.addEventListener('mouseup', handleMouseUp);
        }
        return () => {
            window.removeEventListener('mousemove', handleMouseMove);
            window.removeEventListener('mouseup', handleMouseUp);
        };
    }, [isDragging]);

    const formatTime = (timestamp) => {
        if (!timestamp) return '--:--:--';
        const date = new Date(timestamp * 1000);
        return date.toLocaleTimeString('en-US', { hour12: false });
    };

    const getXPosition = (time) => ((time - minTime) / duration) * 100;

    const SECTION_HEIGHT = 60;

    // 1. Build Historical World State (Cumulative & Forward-Filled)
    // This pre-calculates the section capacities and offsets at every event point
    // to provide STABLE vertical positions during scrolling.
    const worldHistory = useMemo(() => {
        const events = new Map();

        // Normalize status helper
        const normSt = (s, reason) => {
            const base = (s === 'quarantine' || s === 'quarantined' ? 'quarantined' : s);
            if (base !== 'quarantined') return base;
            if (reason === 'logo-mismatch') return 'quarantined-logo';
            if (reason === 'looping') return 'quarantined-loop';
            return 'quarantined-dead';
        };

        const getKeys = () => ({
            stable: 0,
            review: 0,
            'quarantined-logo': 0,
            'quarantined-loop': 0,
            'quarantined-dead': 0
        });

        // Collect all timestamps where counts might change
        streams.forEach(s => {
            s.metrics_history?.forEach(m => {
                const ts = m.timestamp;
                if (!events.has(ts)) {
                    events.set(ts, getKeys());
                }
            });
            if (s.last_status_change) {
                const ts = s.last_status_change;
                if (!events.has(ts)) {
                    events.set(ts, getKeys());
                }
            }
        });

        const sortedTs = Array.from(events.keys()).sort((a, b) => a - b);

        // Pre-calculate counts at each event point
        const result = sortedTs.map(ts => {
            const counts = getKeys();
            streams.forEach(s => {
                // Determine status AT this exact historical timestamp
                const m = s.metrics_history?.findLast(x => x.timestamp <= ts);
                // Fallback: If no metric BEFORE ts, use 'stable' unless ts >= last_status_change
                const status = normSt(
                    m ? m.status : (ts >= s.last_status_change ? s.status : 'stable'),
                    m ? m.status_reason : s.status_reason
                );
                counts[status]++;
            });
            return {
                ts,
                counts,
                offsets: {
                    stable: 0,
                    review: counts.stable,
                    'quarantined-logo': counts.stable + counts.review,
                    'quarantined-loop': counts.stable + counts.review + counts['quarantined-logo'],
                    'quarantined-dead': counts.stable + counts.review + counts['quarantined-logo'] + counts['quarantined-loop']
                }
            };
        });

        // Current world state (Live fallback)
        const nowCounts = getKeys();
        streams.forEach(s => nowCounts[normSt(s.status, s.status_reason)]++);
        const nowState = {
            ts: Date.now() / 1000 + 3600, // Into the future
            counts: nowCounts,
            offsets: {
                stable: 0,
                review: nowCounts.stable,
                'quarantined-logo': nowCounts.stable + nowCounts.review,
                'quarantined-loop': nowCounts.stable + nowCounts.review + nowCounts['quarantined-logo'],
                'quarantined-dead': nowCounts.stable + nowCounts.review + nowCounts['quarantined-logo'] + nowCounts['quarantined-loop']
            }
        };

        return result.length > 0 ? result : [nowState];
    }, [streams]);

    const getYAt = (ts, status, rank, reason) => {
        // Forward-fill lookup: Find the state record at or before ts
        let state = null;
        for (let i = worldHistory.length - 1; i >= 0; i--) {
            if (worldHistory[i].ts <= ts) {
                state = worldHistory[i];
                break;
            }
        }
        if (!state) state = worldHistory[0];

        const normalizedStatus = (status === 'quarantine' || status === 'quarantined') ? 'quarantined' : status;

        let effectiveStatus = normalizedStatus;
        let base = 0;
        let zoneHeight = SECTION_HEIGHT;

        if (normalizedStatus === 'stable') {
            base = 0;
        } else if (normalizedStatus === 'review') {
            base = SECTION_HEIGHT;
        } else {
            // Quarantined subsections
            zoneHeight = SECTION_HEIGHT / 3;
            if (reason === 'logo-mismatch') {
                effectiveStatus = 'quarantined-logo';
                base = SECTION_HEIGHT * 2;
            } else if (reason === 'looping') {
                effectiveStatus = 'quarantined-loop';
                base = SECTION_HEIGHT * 2 + zoneHeight;
            } else {
                effectiveStatus = 'quarantined-dead';
                base = SECTION_HEIGHT * 2 + zoneHeight * 2;
            }
        }

        const count = state.counts[effectiveStatus] || 1;
        const offset = state.offsets[effectiveStatus] || 0;
        const spacing = zoneHeight / (count + 1);
        const relativeRank = Math.max(1, rank - offset);

        return base + (relativeRank * spacing);
    };

    // 2. Prepare Single-Path Subway Lanes
    const streamPaths = useMemo(() => {
        if (!streams || !streams.length) return [];

        return streams.map(stream => {
            const fullHistory = stream.metrics_history || [];

            // Build a list of historical markers
            const markers = fullHistory
                .filter(h => h.timestamp >= minTime - 300 && h.timestamp <= maxTime + 300)
                .map(m => ({
                    ts: m.timestamp,
                    status: m.status,
                    rank: m.rank || stream.rank || 1,
                    reason: m.status_reason
                }));

            // CRITICAL: Normalize status for transitions
            const currentStatus = stream.status === 'quarantine' ? 'quarantined' : stream.status;

            // Handle status change event
            if (stream.last_status_change && stream.last_status_change >= minTime - 3600) {
                // Add a point RIGHT AT the status change with the NEW status
                markers.push({
                    ts: stream.last_status_change,
                    status: currentStatus,
                    rank: stream.rank || 1,
                    reason: stream.status_reason,
                    isBoundary: true // Mark as an event boundary
                });
            }

            // Ensure we have a start point
            if (!markers.length || markers[0].ts > minTime) {
                markers.push({
                    ts: minTime - 300,
                    status: markers.length > 0 ? markers[0].status : currentStatus,
                    rank: markers.length > 0 ? markers[0].rank : (stream.rank || 1),
                    reason: markers.length > 0 ? markers[0].reason : stream.status_reason
                });
            }

            // Sort markers by timestamp
            markers.sort((a, b) => a.ts - b.ts);

            // Deduplicate at same ts
            const points = [];
            markers.forEach(p => {
                if (points.length > 0 && Math.abs(points[points.length - 1].ts - p.ts) < 0.1) {
                    points[points.length - 1] = p;
                } else {
                    points.push(p);
                }
            });

            // Convert to SVG points
            const svgPoints = points.map(p => ({
                x: getXPosition(p.ts),
                y: getYAt(p.ts, p.status, p.rank, p.reason),
                status: p.status,
                ts: p.ts
            }));

            // Final extension to playhead - Anchor it to current rank and status
            const finalY = getYAt(maxTime, currentStatus, stream.rank || 1, stream.status_reason);
            svgPoints.push({
                x: getXPosition(maxTime),
                y: finalY,
                status: currentStatus,
                ts: maxTime
            });

            // Transition logic: Crisp bend BEFORE the destination point
            const transitionSec = 2.5; // Fixed 2.5s transition window for crispness

            let d = `M ${svgPoints[0].x} ${svgPoints[0].y}`;
            for (let j = 1; j < svgPoints.length; j++) {
                const p1 = svgPoints[j - 1];
                const p2 = svgPoints[j];

                if (Math.abs(p1.y - p2.y) < 0.1) {
                    // Straight horizontal line
                    d += ` L ${p2.x} ${p2.y}`;
                } else {
                    // Lane change bend
                    // We draw horizontal at p1.y until transitionSec before p2.ts
                    const switchX = getXPosition(p2.ts - transitionSec);
                    const clampedSwitchX = Math.max(p1.x, switchX);

                    d += ` L ${clampedSwitchX} ${p1.y} L ${p2.x} ${p2.y}`;
                }
            }

            return {
                streamId: stream.stream_id,
                name: stream.name,
                path: d
            };
        });
    }, [streams, worldHistory, minTime, maxTime, duration]);

    const ticks = useMemo(() => {
        const generated = [];
        for (let i = 0; i <= 10; i++) {
            const t = minTime + (i * duration / 10);
            generated.push({ label: formatTime(t), percent: i * 10 });
        }
        return generated;
    }, [minTime, duration]);

    const zoomLevels = [3600, 1800, 600, 300, 60, 30];
    const currentZoomIndex = useMemo(() => {
        let idx = 0, diff = Infinity;
        zoomLevels.forEach((l, i) => {
            const d = Math.abs(l - zoomLevel);
            if (d < diff) { diff = d; idx = i; }
        });
        return idx;
    }, [zoomLevel]);

    const handleZoomChange = (val) => {
        if (onZoomChange && zoomLevels[val[0]]) onZoomChange(zoomLevels[val[0]]);
    };

    // Calculate viewport overlap for the shaded rect
    const viewportStart = Math.max(minTime, currentTime - zoomLevel);
    const viewportX = getXPosition(viewportStart);
    const viewportWidth = getXPosition(currentTime) - viewportX;

    return (
        <Card className={`min-w-0 border-t sticky bottom-16 z-30 bg-card/95 backdrop-blur shadow-lg lg:bottom-0 ${className || ''}`}>
            <div className="flex min-w-0 flex-row">
                <div className="flex min-w-0 flex-1 flex-col">
                    <div className="flex flex-wrap items-center justify-between gap-2 border-b bg-muted/50 px-3 py-2">
                        <div className="flex flex-wrap items-center gap-2">
                            <div className="flex min-h-11 items-center rounded-md border bg-muted px-2 py-0.5">
                                <span className={`text-[10px] font-bold mr-2 ${isLive ? 'text-red-600 dark:text-red-400' : 'text-muted-foreground'}`}>
                                    {isLive ? 'LIVE' : 'REC'}
                                </span>
                                <span className="font-mono text-xs tabular-nums text-foreground">
                                    {formatTime(currentTime)}
                                </span>
                            </div>
                            {!isLive && (
                                <Button variant="ghost" size="sm" className="min-h-11 text-xs gap-1 hover:text-primary hover:bg-primary/10" onClick={onLiveClick}>
                                    <SkipForward className="h-3 w-3" /> Return to Live
                                </Button>
                            )}
                        </div>
                        <div className="flex items-center gap-1">
                            <Button variant="secondary" size="icon" className="h-11 w-11 rounded-full" aria-label={isPlaying ? "Pause timeline playback" : "Play timeline history"} onClick={() => setIsPlaying(!isPlaying)}>
                                {isPlaying ? <Pause className="h-4 w-4" /> : <Play className="h-4 w-4 ml-0.5" />}
                            </Button>
                        </div>
                        <div className="flex min-w-0 flex-wrap items-center justify-end gap-2">
                            {hoveredStreamId && (
                                <div className="hidden max-w-48 truncate text-xs font-medium text-primary bg-primary/10 px-2 py-0.5 rounded border border-primary/20 sm:block">
                                    {streams.find(s => s.stream_id === hoveredStreamId)?.name || 'Stream'}
                                </div>
                            )}
                            <Button
                                variant="ghost"
                                size="sm"
                                onClick={onToggleTimeline}
                                className="min-h-11 text-xs text-muted-foreground flex items-center gap-1"
                            >
                                Hide Timeline <ChevronDown className="h-3 w-3" />
                            </Button>
                        </div>
                    </div>

                    <label className="flex min-w-0 items-center gap-3 border-b px-3 text-xs text-muted-foreground">
                        <span className="shrink-0">Position</span>
                        <input type="range" aria-label="Timeline position" min={minTime} max={Math.max(minTime + 1, maxTime)} step="1" value={Math.max(minTime, Math.min(currentTime, maxTime))} onChange={event => { setIsPlaying(false); onTimeChange(Number(event.target.value)); }} className="h-11 min-w-0 flex-1 accent-primary" />
                    </label>
                    <div ref={containerRef} className="relative h-[210px] w-full cursor-crosshair select-none overflow-hidden bg-black" onMouseDown={handleMouseDown}>
                        {/* Ruler */}
                        <div className="absolute top-0 left-0 right-0 h-[30px] border-b border-white/10 bg-zinc-900/90 backdrop-blur-sm z-20">
                            {ticks.map((t, i) => (
                                <div key={i} className="absolute top-0 bottom-0 pointer-events-none border-l border-white/5" style={{ left: `${t.percent}%` }}>
                                    <span className="absolute left-1 top-1.5 text-[9px] text-zinc-500 font-mono">
                                        {t.label}
                                    </span>
                                </div>
                            ))}
                        </div>

                        {/* Section Labels Overlay (HTML to prevent stretching) */}
                        <div className="absolute top-[30px] bottom-0 left-0 right-0 pointer-events-none flex flex-col z-10">
                            <div className="flex-1 flex items-center justify-end px-4">
                                <span className="text-[10px] uppercase font-black text-green-500/40 tracking-widest">Stable</span>
                            </div>
                            <div className="flex-1 flex items-center justify-end px-4 border-t border-white/5">
                                <span className="text-[10px] uppercase font-black text-blue-500/40 tracking-widest">Review</span>
                            </div>
                            {/* Quarantine Subsections */}
                            <div className="h-[20px] flex items-center justify-end px-4 border-t border-white/5 bg-yellow-500/5">
                                <span className="text-[8px] uppercase font-black text-orange-500/40 tracking-widest">Logo Mismatch</span>
                            </div>
                            <div className="h-[20px] flex items-center justify-end px-4 border-t border-white/5 bg-yellow-500/5">
                                <span className="text-[8px] uppercase font-black text-yellow-600/40 tracking-widest">Looping</span>
                            </div>
                            <div className="h-[20px] flex items-center justify-end px-4 border-t border-white/5 bg-red-500/5">
                                <span className="text-[8px] uppercase font-black text-red-500/40 tracking-widest">Dead</span>
                            </div>
                        </div>

                        {/* SVG Layer with Integrated Background */}
                        <svg viewBox="0 0 100 180" preserveAspectRatio="none" className="absolute top-[30px] left-0 w-full h-[180px] pointer-events-none overflow-visible">
                            <defs>
                                <filter id="glow" x="-20%" y="-20%" width="140%" height="140%">
                                    <feGaussianBlur stdDeviation="0.8" result="blur" />
                                    <feComposite in="SourceGraphic" in2="blur" operator="over" />
                                </filter>

                                <linearGradient id="subwayGradient" x1="0" y1="0" x2="0" y2="180" gradientUnits="userSpaceOnUse">
                                    <stop offset="0%" stopColor="#22c55e" />
                                    <stop offset="33.3%" stopColor="#22c55e" />
                                    <stop offset="33.3%" stopColor="#3b82f6" />
                                    <stop offset="66.6%" stopColor="#3b82f6" />
                                    <stop offset="66.6%" stopColor="#f97316" />
                                    <stop offset="77.7%" stopColor="#f97316" />
                                    <stop offset="77.7%" stopColor="#eab308" />
                                    <stop offset="88.8%" stopColor="#eab308" />
                                    <stop offset="88.8%" stopColor="#ef4444" />
                                    <stop offset="100%" stopColor="#ef4444" />
                                </linearGradient>
                            </defs>

                            {/* SVG Background Rects - Perfectly in Sync with Grid */}
                            <g className="bg-zones">
                                <rect x="0" y="0" width="100" height="60" fill="#22c55e" fillOpacity="0.1" />
                                <rect x="0" y="60" width="100" height="60" fill="#3b82f6" fillOpacity="0.1" />

                                {/* Quarantine Subsections */}
                                <rect x="0" y="120" width="100" height="20" fill="#f97316" fillOpacity="0.08" />
                                <rect x="0" y="140" width="100" height="20" fill="#eab308" fillOpacity="0.08" />
                                <rect x="0" y="160" width="100" height="20" fill="#ef4444" fillOpacity="0.08" />

                                <line x1="0" y1="60" stroke="white" strokeOpacity="0.1" vectorEffect="non-scaling-stroke" />
                                <line x1="0" y1="120" stroke="white" strokeOpacity="0.1" vectorEffect="non-scaling-stroke" />
                                <line x1="0" y1="140" stroke="white" strokeOpacity="0.05" vectorEffect="non-scaling-stroke" />
                                <line x1="0" y1="160" stroke="white" strokeOpacity="0.05" vectorEffect="non-scaling-stroke" />
                            </g>

                            {/* Ad Periods Rendering */}
                            <g className="ad-periods">
                                {adPeriods.map((period, idx) => {
                                    const startX = getXPosition(period.start);
                                    const endX = getXPosition(period.end || maxTime);
                                    if (startX > 100 || endX < 0) return null;
                                    return (
                                        <rect
                                            key={idx}
                                            x={Math.max(0, startX)}
                                            y="0"
                                            width={Math.min(100, endX) - Math.max(0, startX)}
                                            height="180"
                                            fill="#f97316"
                                            fillOpacity="0.15"
                                            pointerEvents="none"
                                        />
                                    );
                                })}
                            </g>

                            {/* Viewport Overlay (Currently visible window in charts) */}
                            <rect
                                x={viewportX}
                                y="0"
                                width={viewportWidth}
                                height="180"
                                fill="white"
                                fillOpacity="0.07"
                                stroke="white"
                                strokeOpacity="0.15"
                                strokeWidth="0.5"
                                vectorEffect="non-scaling-stroke"
                            />

                            {/* Subway Paths */}
                            {streamPaths.map((stream) => (
                                <g key={stream.streamId}
                                    className="transition-opacity duration-300"
                                    style={{ opacity: hoveredStreamId && hoveredStreamId !== stream.streamId ? 0.15 : 1 }}
                                >
                                    <path
                                        d={stream.path}
                                        fill="none"
                                        stroke="url(#subwayGradient)"
                                        strokeWidth={hoveredStreamId === stream.streamId ? 2.5 : 1.3}
                                        strokeLinecap="round"
                                        strokeLinejoin="round"
                                        vectorEffect="non-scaling-stroke"
                                        style={{
                                            filter: hoveredStreamId === stream.streamId ? 'url(#glow)' : 'none',
                                        }}
                                        className="transition-all duration-300"
                                    />
                                    <path
                                        d={stream.path}
                                        fill="none"
                                        stroke="transparent"
                                        strokeWidth={12}
                                        vectorEffect="non-scaling-stroke"
                                        className="pointer-events-auto cursor-pointer"
                                        onMouseEnter={() => setHoveredStreamId(stream.streamId)}
                                        onMouseLeave={() => setHoveredStreamId(null)}
                                    />
                                </g>
                            ))}
                        </svg>

                        {/* Playhead */}
                        <div className="absolute top-0 bottom-0 w-[1.5px] bg-red-600 z-30 pointer-events-none shadow-[0_0_15px_rgba(220,38,38,0.8)]" style={{ left: `${getXPosition(currentTime)}%` }}>
                            <div className="absolute top-0 -left-[5px] w-0 h-0 border-l-[5px] border-l-transparent border-r-[5px] border-r-transparent border-t-[8px] border-t-red-600"></div>
                            <div className="absolute top-[30px] bottom-0 -left-[10px] w-[20px] bg-red-600/5"></div>
                        </div>
                    </div>
                </div>

                <div className="flex w-11 shrink-0 flex-col items-center justify-center gap-2 border-l bg-muted py-2">
                    <span className="text-[8px] text-muted-foreground font-black tracking-tighter uppercase whitespace-nowrap">Scale</span>
                    <div className="h-40 py-2">
                        <Slider aria-label="Timeline time scale" orientation="vertical" min={0} max={zoomLevels.length - 1} step={1} value={[currentZoomIndex]} onValueChange={handleZoomChange} className="h-full" />
                    </div>
                    <span className="text-[9px] font-mono text-muted-foreground w-full text-center truncate px-0.5">
                        {zoomLevels[currentZoomIndex] < 60 ? `${zoomLevels[currentZoomIndex]}s` : `${Math.round(zoomLevels[currentZoomIndex] / 60)}m`}
                    </span>
                </div>
            </div>
        </Card>
    );
}
