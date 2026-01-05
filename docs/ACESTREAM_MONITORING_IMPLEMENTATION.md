# AceStream Monitoring Feature - Implementation Guide

## Overview

This document provides a comprehensive implementation guide for adding AceStream monitoring capabilities to StreamFlow. The feature will enable continuous monitoring of AceStream channels, health tracking, automatic stream ordering, and resource-efficient operation.

## Feature Requirements

### Core Functionality

1. **AceStream Channel Tagging**
   - Channels can be marked as "AceStream Channels"
   - Tagged channels are excluded from other automations
   - Only contain AceStream streams with URLs like: `http://<host>:<port>/ace/getstream?id=<id>`

2. **Continuous Monitoring**
   - FFmpeg-based stream monitoring (similar to stream check)
   - Gather stream statistics continuously
   - Query Orchestrator `/streams` endpoint for additional metrics
   - Match streams by `id` field from Orchestrator with URL `id` parameter

3. **Health Tracking & Ordering**
   - Calculate stream health based on:
     - Peers count
     - Speed down/up (KB/s)
     - Downloaded/uploaded bytes
     - Buffer statistics
     - FFmpeg stats (bitrate, resolution, errors)
   - Automatically reorder streams in Dispatcharr channels by health
   - Keep streams alive to maintain Orchestrator stats

4. **Resource Management**
   - Minimize resource usage while keeping streams alive
   - Send cleanup requests to `command_url` on service shutdown
   - Graceful shutdown handling

5. **UI/Configuration**
   - Separate monitoring section with graphs
   - Configure orchestrator URL
   - Tag channels as AceStream channels
   - View real-time monitoring data and graphs

## Architecture Design

### Database Schema

Since the requirement mentions creating a database (non-JSON based) for better information storage, we'll add SQLite support:

```python
# backend/acestream_db.py
import sqlite3
from datetime import datetime
from typing import Dict, List, Optional, Any

class AceStreamDatabase:
    """Database for storing AceStream monitoring data."""
    
    def __init__(self, db_path: str = "data/acestream_monitoring.db"):
        self.db_path = db_path
        self._init_db()
    
    def _init_db(self):
        """Initialize database tables."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            
            # Stream monitoring sessions
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS monitoring_sessions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    stream_id INTEGER NOT NULL,
                    channel_id INTEGER NOT NULL,
                    acestream_id TEXT NOT NULL,
                    started_at TIMESTAMP NOT NULL,
                    ended_at TIMESTAMP,
                    status TEXT NOT NULL,
                    command_url TEXT,
                    stat_url TEXT
                )
            ''')
            
            # Stream health metrics (time-series data)
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS stream_metrics (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id INTEGER NOT NULL,
                    timestamp TIMESTAMP NOT NULL,
                    peers INTEGER,
                    speed_down INTEGER,
                    speed_up INTEGER,
                    downloaded INTEGER,
                    uploaded INTEGER,
                    buffer_pieces INTEGER,
                    ffmpeg_bitrate INTEGER,
                    ffmpeg_resolution TEXT,
                    ffmpeg_fps REAL,
                    health_score REAL,
                    FOREIGN KEY (session_id) REFERENCES monitoring_sessions(id)
                )
            ''')
            
            # Create indexes for better query performance
            cursor.execute('''
                CREATE INDEX IF NOT EXISTS idx_metrics_session_time 
                ON stream_metrics(session_id, timestamp)
            ''')
            
            cursor.execute('''
                CREATE INDEX IF NOT EXISTS idx_sessions_channel 
                ON monitoring_sessions(channel_id)
            ''')
            
            conn.commit()
```

### Data Models

Extend existing models to support AceStream features:

```python
# backend/udi/models.py - Add to Channel class

@dataclass
class Channel:
    # ... existing fields ...
    is_acestream: bool = False
    acestream_orchestrator_url: Optional[str] = None
    acestream_config: Optional[Dict[str, Any]] = None
```

### AceStream Monitoring Service

```python
# backend/acestream_monitor_service.py

import asyncio
import logging
import requests
import subprocess
import threading
import time
from typing import Dict, List, Optional, Tuple
from datetime import datetime, timedelta

from acestream_db import AceStreamDatabase
from udi.manager import UDIManager
from logging_config import setup_logging

logger = setup_logging(__name__)

class AceStreamMonitor:
    """
    Continuous monitoring service for AceStream channels.
    
    Features:
    - FFmpeg-based stream monitoring
    - Orchestrator API integration
    - Health scoring and stream ordering
    - Resource-efficient operation
    - Graceful shutdown with cleanup
    """
    
    def __init__(self, udi_manager: UDIManager, orchestrator_url: str):
        self.udi_manager = udi_manager
        self.orchestrator_url = orchestrator_url
        self.db = AceStreamDatabase()
        
        self.monitoring_threads: Dict[int, threading.Thread] = {}
        self.active_sessions: Dict[int, Dict] = {}
        self.shutdown_event = threading.Event()
        
        self.ffmpeg_processes: Dict[int, subprocess.Popen] = {}
        
    def start_monitoring(self):
        """Start monitoring all AceStream channels."""
        logger.info("Starting AceStream monitoring service...")
        
        # Get all AceStream channels
        channels = self._get_acestream_channels()
        
        for channel in channels:
            self._start_channel_monitoring(channel)
        
        logger.info(f"Monitoring started for {len(channels)} AceStream channels")
        
    def _get_acestream_channels(self) -> List:
        """Get all channels marked as AceStream channels."""
        all_channels = self.udi_manager.get_all_channels()
        return [ch for ch in all_channels if getattr(ch, 'is_acestream', False)]
    
    def _start_channel_monitoring(self, channel):
        """Start monitoring a specific channel."""
        thread = threading.Thread(
            target=self._monitor_channel,
            args=(channel,),
            daemon=True
        )
        self.monitoring_threads[channel.id] = thread
        thread.start()
    
    def _monitor_channel(self, channel):
        """Main monitoring loop for a channel."""
        logger.info(f"Starting monitoring for channel {channel.id}: {channel.name}")
        
        while not self.shutdown_event.is_set():
            try:
                # Get channel streams
                streams = self._get_channel_streams(channel)
                
                if not streams:
                    logger.warning(f"No streams found for channel {channel.id}")
                    time.sleep(60)
                    continue
                
                # Monitor each stream
                stream_health = []
                
                for stream in streams:
                    health = self._check_stream_health(channel, stream)
                    if health:
                        stream_health.append((stream, health))
                
                # Reorder streams by health
                if stream_health:
                    self._reorder_streams_by_health(channel, stream_health)
                
                # Wait before next check (e.g., every 30 seconds)
                time.sleep(30)
                
            except Exception as e:
                logger.error(f"Error monitoring channel {channel.id}: {e}")
                time.sleep(60)
        
        logger.info(f"Stopped monitoring channel {channel.id}")
    
    def _get_channel_streams(self, channel) -> List:
        """Get streams for a channel from UDI."""
        stream_ids = channel.streams
        streams = []
        for stream_id in stream_ids:
            stream = self.udi_manager.get_stream(stream_id)
            if stream:
                streams.append(stream)
        return streams
    
    def _check_stream_health(self, channel, stream) -> Optional[Dict]:
        """
        Check stream health by combining FFmpeg stats and Orchestrator data.
        
        Returns health metrics dict or None if check failed.
        """
        try:
            # Extract AceStream ID from URL
            acestream_id = self._extract_acestream_id(stream.url)
            if not acestream_id:
                logger.warning(f"Cannot extract AceStream ID from URL: {stream.url}")
                return None
            
            # Get stats from Orchestrator
            orchestrator_stats = self._get_orchestrator_stats(acestream_id)
            
            # Get FFmpeg stats (lightweight check - just probe, don't download much)
            ffmpeg_stats = self._get_ffmpeg_stats(stream.url)
            
            # Calculate health score
            health_score = self._calculate_health_score(
                orchestrator_stats, 
                ffmpeg_stats
            )
            
            # Save to database
            session_id = self._get_or_create_session(channel.id, stream.id, acestream_id)
            self._save_metrics(session_id, orchestrator_stats, ffmpeg_stats, health_score)
            
            return {
                'stream_id': stream.id,
                'acestream_id': acestream_id,
                'health_score': health_score,
                'orchestrator_stats': orchestrator_stats,
                'ffmpeg_stats': ffmpeg_stats
            }
            
        except Exception as e:
            logger.error(f"Error checking stream {stream.id} health: {e}")
            return None
    
    def _extract_acestream_id(self, url: str) -> Optional[str]:
        """Extract AceStream ID from URL like http://host:port/ace/getstream?id=<id>"""
        import re
        match = re.search(r'[?&]id=([a-f0-9]+)', url, re.IGNORECASE)
        return match.group(1) if match else None
    
    def _get_orchestrator_stats(self, acestream_id: str) -> Optional[Dict]:
        """
        Query Orchestrator /streams endpoint for stream stats.
        
        Example response:
        {
            "id": "74defb8f...|1ddd74d...",
            "key": "74defb8f...",
            "peers": 26,
            "speed_down": 5059,
            "speed_up": 17,
            "downloaded": 203423744,
            "uploaded": 753664,
            "livepos": {...}
        }
        """
        try:
            url = f"{self.orchestrator_url}/streams"
            response = requests.get(url, timeout=5)
            response.raise_for_status()
            
            streams = response.json()
            
            # Find stream by matching acestream_id with key field
            for stream_data in streams:
                if stream_data.get('key') == acestream_id:
                    return stream_data
            
            return None
            
        except Exception as e:
            logger.error(f"Error querying Orchestrator: {e}")
            return None
    
    def _get_ffmpeg_stats(self, stream_url: str, duration: int = 5) -> Optional[Dict]:
        """
        Use FFmpeg to probe stream and get basic stats.
        Keep it lightweight - just enough to verify stream is working.
        """
        try:
            # Use ffmpeg with limited duration to minimize resource usage
            cmd = [
                'ffmpeg',
                '-i', stream_url,
                '-t', str(duration),
                '-f', 'null',
                '-'
            ]
            
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=duration + 5
            )
            
            # Parse ffmpeg output for stats
            output = result.stderr
            
            stats = {
                'bitrate': self._parse_bitrate(output),
                'resolution': self._parse_resolution(output),
                'fps': self._parse_fps(output),
                'codec': self._parse_codec(output),
                'errors': self._count_errors(output)
            }
            
            return stats
            
        except subprocess.TimeoutExpired:
            logger.warning(f"FFmpeg timeout for URL: {stream_url}")
            return None
        except Exception as e:
            logger.error(f"Error getting FFmpeg stats: {e}")
            return None
    
    def _parse_bitrate(self, output: str) -> Optional[int]:
        """Parse bitrate from FFmpeg output."""
        import re
        match = re.search(r'bitrate:\s*(\d+)\s*kb/s', output)
        return int(match.group(1)) if match else None
    
    def _parse_resolution(self, output: str) -> Optional[str]:
        """Parse resolution from FFmpeg output."""
        import re
        match = re.search(r'(\d{3,4})x(\d{3,4})', output)
        return match.group(0) if match else None
    
    def _parse_fps(self, output: str) -> Optional[float]:
        """Parse FPS from FFmpeg output."""
        import re
        match = re.search(r'(\d+\.?\d*)\s*fps', output)
        return float(match.group(1)) if match else None
    
    def _parse_codec(self, output: str) -> Optional[str]:
        """Parse video codec from FFmpeg output."""
        import re
        match = re.search(r'Video:\s*(\w+)', output)
        return match.group(1) if match else None
    
    def _count_errors(self, output: str) -> int:
        """Count errors in FFmpeg output."""
        error_keywords = ['error', 'corrupt', 'invalid', 'failed']
        count = 0
        for line in output.lower().split('\n'):
            if any(keyword in line for keyword in error_keywords):
                count += 1
        return count
    
    def _calculate_health_score(
        self, 
        orchestrator_stats: Optional[Dict], 
        ffmpeg_stats: Optional[Dict]
    ) -> float:
        """
        Calculate health score (0-100) based on available metrics.
        
        Scoring factors:
        - Peers count (more = better)
        - Download speed (higher = better)
        - Upload speed (should be reasonable)
        - FFmpeg bitrate (higher = better, within reason)
        - FFmpeg errors (fewer = better)
        - Stream availability (working = better)
        """
        score = 0.0
        
        # Base score for having orchestrator stats
        if orchestrator_stats:
            # Peers score (0-25 points)
            peers = orchestrator_stats.get('peers', 0)
            score += min(peers * 1.0, 25)
            
            # Download speed score (0-25 points)
            # Assume good speed is 5000+ KB/s
            speed_down = orchestrator_stats.get('speed_down', 0)
            score += min((speed_down / 5000) * 25, 25)
            
            # Upload contribution (0-10 points)
            # Having some upload is good
            speed_up = orchestrator_stats.get('speed_up', 0)
            if speed_up > 0:
                score += min(speed_up / 5, 10)
        
        # FFmpeg stats score
        if ffmpeg_stats:
            # Stream is working (20 points)
            score += 20
            
            # Bitrate score (0-15 points)
            # Assume good bitrate is 3000+ kbps
            bitrate = ffmpeg_stats.get('bitrate', 0) or 0
            score += min((bitrate / 3000) * 15, 15)
            
            # Penalty for errors (-5 points per error, max -20)
            errors = ffmpeg_stats.get('errors', 0)
            score -= min(errors * 5, 20)
        
        # Ensure score is between 0 and 100
        return max(0, min(score, 100))
    
    def _get_or_create_session(
        self, 
        channel_id: int, 
        stream_id: int, 
        acestream_id: str
    ) -> int:
        """Get existing session ID or create new one."""
        # Check if there's an active session
        if stream_id in self.active_sessions:
            return self.active_sessions[stream_id]['session_id']
        
        # Create new session in database
        with sqlite3.connect(self.db.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO monitoring_sessions 
                (stream_id, channel_id, acestream_id, started_at, status)
                VALUES (?, ?, ?, ?, ?)
            ''', (stream_id, channel_id, acestream_id, datetime.now(), 'active'))
            
            session_id = cursor.lastrowid
            conn.commit()
        
        self.active_sessions[stream_id] = {'session_id': session_id}
        return session_id
    
    def _save_metrics(
        self,
        session_id: int,
        orchestrator_stats: Optional[Dict],
        ffmpeg_stats: Optional[Dict],
        health_score: float
    ):
        """Save metrics to database."""
        with sqlite3.connect(self.db.db_path) as conn:
            cursor = conn.cursor()
            
            cursor.execute('''
                INSERT INTO stream_metrics (
                    session_id, timestamp, peers, speed_down, speed_up,
                    downloaded, uploaded, buffer_pieces, ffmpeg_bitrate,
                    ffmpeg_resolution, ffmpeg_fps, health_score
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                session_id,
                datetime.now(),
                orchestrator_stats.get('peers') if orchestrator_stats else None,
                orchestrator_stats.get('speed_down') if orchestrator_stats else None,
                orchestrator_stats.get('speed_up') if orchestrator_stats else None,
                orchestrator_stats.get('downloaded') if orchestrator_stats else None,
                orchestrator_stats.get('uploaded') if orchestrator_stats else None,
                orchestrator_stats.get('livepos', {}).get('buffer_pieces') if orchestrator_stats else None,
                ffmpeg_stats.get('bitrate') if ffmpeg_stats else None,
                ffmpeg_stats.get('resolution') if ffmpeg_stats else None,
                ffmpeg_stats.get('fps') if ffmpeg_stats else None,
                health_score
            ))
            
            conn.commit()
    
    def _reorder_streams_by_health(self, channel, stream_health: List[Tuple]):
        """
        Reorder streams in Dispatcharr channel based on health scores.
        
        Args:
            channel: Channel object
            stream_health: List of (stream, health_dict) tuples
        """
        try:
            # Sort by health score (descending)
            sorted_streams = sorted(
                stream_health,
                key=lambda x: x[1]['health_score'],
                reverse=True
            )
            
            # Get stream IDs in new order
            new_order = [s[0].id for s in sorted_streams]
            
            # Update channel in Dispatcharr via UDI
            # This will call the Dispatcharr API to update stream order
            self.udi_manager.update_channel_streams(channel.id, new_order)
            
            logger.info(
                f"Reordered {len(new_order)} streams for channel {channel.id} "
                f"by health (best: {sorted_streams[0][1]['health_score']:.1f})"
            )
            
        except Exception as e:
            logger.error(f"Error reordering streams for channel {channel.id}: {e}")
    
    def shutdown(self):
        """Gracefully shutdown monitoring and cleanup resources."""
        logger.info("Shutting down AceStream monitoring service...")
        
        # Signal all threads to stop
        self.shutdown_event.set()
        
        # Send cleanup requests to Orchestrator
        self._cleanup_orchestrator_sessions()
        
        # Stop all FFmpeg processes
        self._stop_ffmpeg_processes()
        
        # Wait for threads to finish
        for thread in self.monitoring_threads.values():
            thread.join(timeout=5)
        
        # Close active sessions in database
        self._close_active_sessions()
        
        logger.info("AceStream monitoring service shutdown complete")
    
    def _cleanup_orchestrator_sessions(self):
        """Send cleanup requests to command_url for all active sessions."""
        logger.info("Cleaning up Orchestrator sessions...")
        
        try:
            # Get all active sessions from database
            with sqlite3.connect(self.db.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    SELECT acestream_id FROM monitoring_sessions 
                    WHERE status = 'active' AND command_url IS NOT NULL
                ''')
                
                sessions = cursor.fetchall()
            
            # Get current stream data from Orchestrator
            url = f"{self.orchestrator_url}/streams"
            response = requests.get(url, timeout=5)
            response.raise_for_status()
            streams = response.json()
            
            # Send stop command for each session
            for session in sessions:
                acestream_id = session[0]
                
                # Find matching stream in orchestrator data
                for stream_data in streams:
                    if stream_data.get('key') == acestream_id:
                        command_url = stream_data.get('command_url')
                        if command_url:
                            try:
                                # Send stop command
                                requests.post(f"{command_url}?method=stop", timeout=5)
                                logger.info(f"Sent stop command for stream {acestream_id}")
                            except Exception as e:
                                logger.error(f"Error sending stop command: {e}")
                        break
            
        except Exception as e:
            logger.error(f"Error during Orchestrator cleanup: {e}")
    
    def _stop_ffmpeg_processes(self):
        """Stop all running FFmpeg processes."""
        for stream_id, process in self.ffmpeg_processes.items():
            try:
                process.terminate()
                process.wait(timeout=5)
            except Exception as e:
                logger.error(f"Error stopping FFmpeg process for stream {stream_id}: {e}")
    
    def _close_active_sessions(self):
        """Mark all active sessions as ended in database."""
        with sqlite3.connect(self.db.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                UPDATE monitoring_sessions 
                SET status = 'stopped', ended_at = ?
                WHERE status = 'active'
            ''', (datetime.now(),))
            conn.commit()
```

### API Endpoints

Add to `backend/web_api.py`:

```python
# AceStream Configuration Endpoints

@app.route('/api/acestream/config', methods=['GET', 'POST'])
def acestream_config():
    """Get or update AceStream configuration."""
    if request.method == 'GET':
        # Return current configuration
        config = load_acestream_config()
        return jsonify(config)
    else:
        # Update configuration
        config = request.json
        save_acestream_config(config)
        return jsonify({'success': True})

@app.route('/api/acestream/channels', methods=['GET'])
def get_acestream_channels():
    """Get all channels marked as AceStream channels."""
    channels = udi_manager.get_all_channels()
    acestream_channels = [
        ch.to_dict() for ch in channels 
        if getattr(ch, 'is_acestream', False)
    ]
    return jsonify(acestream_channels)

@app.route('/api/acestream/channels/<int:channel_id>/tag', methods=['POST'])
def tag_channel_as_acestream(channel_id):
    """Mark a channel as AceStream channel."""
    data = request.json
    is_acestream = data.get('is_acestream', False)
    orchestrator_url = data.get('orchestrator_url')
    
    # Update channel via Dispatcharr API
    channel_data = {
        'is_acestream': is_acestream,
        'acestream_orchestrator_url': orchestrator_url
    }
    
    # This would need to be implemented in UDI manager
    result = udi_manager.update_channel_acestream_config(channel_id, channel_data)
    
    return jsonify({'success': True})

@app.route('/api/acestream/monitoring/status', methods=['GET'])
def get_monitoring_status():
    """Get current monitoring status for all AceStream channels."""
    if not acestream_monitor:
        return jsonify({'error': 'Monitoring service not running'}), 503
    
    # Get status from monitoring service
    status = {
        'active_channels': len(acestream_monitor.monitoring_threads),
        'active_sessions': len(acestream_monitor.active_sessions)
    }
    
    return jsonify(status)

@app.route('/api/acestream/monitoring/channel/<int:channel_id>/metrics', methods=['GET'])
def get_channel_metrics(channel_id):
    """Get monitoring metrics for a specific channel."""
    # Query from database
    db = AceStreamDatabase()
    
    # Get time range from query params
    hours = request.args.get('hours', 24, type=int)
    
    metrics = db.get_channel_metrics(channel_id, hours)
    
    return jsonify(metrics)

@app.route('/api/acestream/monitoring/stream/<int:stream_id>/health', methods=['GET'])
def get_stream_health(stream_id):
    """Get current health score and stats for a stream."""
    db = AceStreamDatabase()
    
    health = db.get_latest_stream_health(stream_id)
    
    return jsonify(health)
```

### Frontend Components

Create new page for AceStream monitoring:

```jsx
// frontend/src/pages/AceStreamMonitoring.jsx

import React, { useState, useEffect } from 'react';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip, Legend, ResponsiveContainer } from 'recharts';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Switch } from '@/components/ui/switch';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import axios from 'axios';

const AceStreamMonitoring = () => {
  const [channels, setChannels] = useState([]);
  const [selectedChannel, setSelectedChannel] = useState(null);
  const [metrics, setMetrics] = useState([]);
  const [config, setConfig] = useState({
    orchestrator_url: ''
  });

  useEffect(() => {
    loadAceStreamChannels();
    loadConfig();
  }, []);

  useEffect(() => {
    if (selectedChannel) {
      loadChannelMetrics(selectedChannel.id);
      
      // Refresh metrics every 30 seconds
      const interval = setInterval(() => {
        loadChannelMetrics(selectedChannel.id);
      }, 30000);
      
      return () => clearInterval(interval);
    }
  }, [selectedChannel]);

  const loadAceStreamChannels = async () => {
    try {
      const response = await axios.get('/api/acestream/channels');
      setChannels(response.data);
    } catch (error) {
      console.error('Error loading AceStream channels:', error);
    }
  };

  const loadConfig = async () => {
    try {
      const response = await axios.get('/api/acestream/config');
      setConfig(response.data);
    } catch (error) {
      console.error('Error loading config:', error);
    }
  };

  const saveConfig = async () => {
    try {
      await axios.post('/api/acestream/config', config);
      // Show success message
    } catch (error) {
      console.error('Error saving config:', error);
    }
  };

  const loadChannelMetrics = async (channelId) => {
    try {
      const response = await axios.get(`/api/acestream/monitoring/channel/${channelId}/metrics?hours=24`);
      setMetrics(response.data);
    } catch (error) {
      console.error('Error loading metrics:', error);
    }
  };

  const toggleChannelAceStream = async (channelId, isAceStream) => {
    try {
      await axios.post(`/api/acestream/channels/${channelId}/tag`, {
        is_acestream: isAceStream,
        orchestrator_url: config.orchestrator_url
      });
      loadAceStreamChannels();
    } catch (error) {
      console.error('Error toggling AceStream tag:', error);
    }
  };

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-3xl font-bold">AceStream Monitoring</h1>
        <p className="text-muted-foreground">
          Monitor and manage AceStream channels with real-time health tracking
        </p>
      </div>

      {/* Configuration Section */}
      <Card>
        <CardHeader>
          <CardTitle>Configuration</CardTitle>
          <CardDescription>
            Configure AceStream Orchestrator connection
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="grid gap-2">
            <Label htmlFor="orchestrator-url">Orchestrator URL</Label>
            <Input
              id="orchestrator-url"
              placeholder="http://gluetun:19000"
              value={config.orchestrator_url}
              onChange={(e) => setConfig({ ...config, orchestrator_url: e.target.value })}
            />
          </div>
          <Button onClick={saveConfig}>Save Configuration</Button>
        </CardContent>
      </Card>

      {/* Channels List */}
      <Card>
        <CardHeader>
          <CardTitle>AceStream Channels</CardTitle>
          <CardDescription>
            Channels currently being monitored
          </CardDescription>
        </CardHeader>
        <CardContent>
          <div className="space-y-2">
            {channels.map((channel) => (
              <div
                key={channel.id}
                className="flex items-center justify-between p-3 border rounded-lg cursor-pointer hover:bg-accent"
                onClick={() => setSelectedChannel(channel)}
              >
                <div>
                  <div className="font-medium">{channel.name}</div>
                  <div className="text-sm text-muted-foreground">
                    Channel #{channel.channel_number} • {channel.streams?.length || 0} streams
                  </div>
                </div>
                <Badge variant={selectedChannel?.id === channel.id ? "default" : "outline"}>
                  {selectedChannel?.id === channel.id ? "Selected" : "View"}
                </Badge>
              </div>
            ))}
          </div>
        </CardContent>
      </Card>

      {/* Metrics Chart */}
      {selectedChannel && metrics.length > 0 && (
        <Card>
          <CardHeader>
            <CardTitle>Stream Health Over Time</CardTitle>
            <CardDescription>
              Health metrics for {selectedChannel.name}
            </CardDescription>
          </CardHeader>
          <CardContent>
            <ResponsiveContainer width="100%" height={400}>
              <LineChart data={metrics}>
                <CartesianGrid strokeDasharray="3 3" />
                <XAxis dataKey="timestamp" />
                <YAxis yAxisId="left" />
                <YAxis yAxisId="right" orientation="right" />
                <Tooltip />
                <Legend />
                <Line
                  yAxisId="left"
                  type="monotone"
                  dataKey="health_score"
                  stroke="#8884d8"
                  name="Health Score"
                />
                <Line
                  yAxisId="right"
                  type="monotone"
                  dataKey="peers"
                  stroke="#82ca9d"
                  name="Peers"
                />
                <Line
                  yAxisId="right"
                  type="monotone"
                  dataKey="speed_down"
                  stroke="#ffc658"
                  name="Download Speed (KB/s)"
                />
              </LineChart>
            </ResponsiveContainer>
          </CardContent>
        </Card>
      )}
    </div>
  );
};

export default AceStreamMonitoring;
```

## Implementation Steps

### Step 1: Database Setup
1. Create `backend/acestream_db.py` with database schema
2. Initialize database on first run
3. Add database migration if needed

### Step 2: Data Model Extensions
1. Extend `Channel` model in `backend/udi/models.py` to include:
   - `is_acestream: bool`
   - `acestream_orchestrator_url: Optional[str]`
   - `acestream_config: Optional[Dict]`
2. Update UDI manager to handle new fields

### Step 3: Monitoring Service
1. Create `backend/acestream_monitor_service.py`
2. Implement FFmpeg-based monitoring
3. Implement Orchestrator API client
4. Implement health scoring algorithm
5. Implement stream ordering logic
6. Add graceful shutdown handling

### Step 4: API Integration
1. Add AceStream endpoints to `backend/web_api.py`
2. Implement configuration endpoints
3. Implement channel tagging endpoints
4. Implement monitoring data endpoints
5. Update Dispatcharr API calls via UDI

### Step 5: Frontend UI
1. Create `frontend/src/pages/AceStreamMonitoring.jsx`
2. Add configuration form
3. Add channel list with tagging
4. Add metrics visualization with charts
5. Add real-time updates
6. Update navigation/routing

### Step 6: Integration
1. Integrate monitoring service with main application lifecycle
2. Start monitoring service on application startup
3. Handle graceful shutdown
4. Add logging and error handling
5. Update documentation

### Step 7: Testing
1. Test database operations
2. Test FFmpeg monitoring
3. Test Orchestrator API integration
4. Test health scoring
5. Test stream reordering
6. Test graceful shutdown
7. Test UI components

## Configuration Files

Add to `backend/config.ini`:

```ini
[acestream]
# Enable AceStream monitoring feature
enabled = false

# Default Orchestrator URL
orchestrator_url = http://gluetun:19000

# Monitoring interval in seconds
monitoring_interval = 30

# FFmpeg probe duration in seconds
ffmpeg_probe_duration = 5

# Health score thresholds
health_score_good = 75
health_score_warning = 50
```

## Security Considerations

1. **Validate Orchestrator URL**: Ensure URL is properly validated to prevent SSRF
2. **Rate Limiting**: Add rate limiting to prevent overwhelming Orchestrator
3. **Resource Limits**: Limit number of concurrent FFmpeg processes
4. **Input Validation**: Validate all user inputs for channel tagging
5. **Error Handling**: Proper error handling to prevent crashes

## Performance Optimization

1. **Database Indexing**: Proper indexes on time-series data
2. **Connection Pooling**: Reuse database connections
3. **Caching**: Cache Orchestrator responses when appropriate
4. **Batch Operations**: Batch database writes
5. **Thread Pooling**: Limit concurrent monitoring threads

## Future Enhancements

1. **Alert System**: Alert on stream health degradation
2. **Historical Analytics**: Long-term trend analysis
3. **Auto-Recovery**: Automatic stream recovery attempts
4. **Custom Health Algorithms**: User-configurable health scoring
5. **Export/Import**: Export monitoring data for analysis

## Testing Checklist

- [x] Database creation and migration
- [ ] Channel tagging/untagging (API tested, UI needs manual testing)
- [ ] Orchestrator API connection (needs integration testing)
- [ ] FFmpeg stream monitoring (needs integration testing)
- [ ] Health score calculation (logic implemented)
- [ ] Stream reordering (logic implemented)
- [ ] Graceful shutdown (implemented)
- [ ] Cleanup requests sent (implemented)
- [x] UI configuration form (implemented)
- [x] UI metrics display (implemented)
- [x] Real-time updates (implemented)
- [x] Error handling (implemented)
- [ ] Resource usage (memory, CPU) - needs monitoring
- [ ] Long-running stability - needs extended testing

## Implementation Status (Updated)

**Phase 1: Foundation** ✅ COMPLETE
- Database module created with SQLite schema
- Channel model extended with AceStream fields
- All database operations tested and passing

**Phase 2: Core Monitoring Service** ✅ COMPLETE
- Monitoring service implemented with threading
- FFmpeg integration for stream probing
- Orchestrator API client implemented
- Health scoring algorithm complete
- Stream reordering logic complete
- Graceful shutdown with cleanup

**Phase 3: Backend API Integration** ✅ COMPLETE
- Configuration endpoints added
- Channel tagging endpoints added
- Monitoring control endpoints added
- Metrics query endpoints added
- Auto-start integration complete

**Phase 4: Frontend UI** ✅ COMPLETE
- AceStream monitoring page created
- Configuration form with ShadCN components
- Channel list with tagging UI
- Metrics visualization with Recharts
- Navigation and routing complete
- Real-time polling every 30 seconds

**Phase 5: Testing** 🟡 IN PROGRESS
- Database tests written and passing (9/9)
- Integration tests needed
- Manual UI testing needed
- CodeQL security scan pending

**Phase 6: Documentation** 📝 NEEDED
- User guide needed
- API documentation needed
- Configuration reference needed

## User Guide

### Getting Started

1. **Access the AceStream Monitoring Page**
   - Navigate to "AceStream Monitoring" in the sidebar (Radio icon)

2. **Configure Orchestrator Connection**
   - In the Configuration section, set the Orchestrator URL (default: `http://gluetun:19000`)
   - Adjust monitoring interval (10-300 seconds, default: 30)
   - Adjust FFmpeg probe duration (3-15 seconds, default: 5)
   - Enable "AceStream Monitoring" switch
   - Click "Save Configuration"

3. **Tag Channels as AceStream**
   - Scroll to "Channel Tagging" section
   - Toggle the switch next to channels that contain AceStream streams
   - Tagged channels will appear in "AceStream Channels" section

4. **Start Monitoring**
   - Click "Start Monitoring" button in the Status section
   - Monitor will check streams every interval and reorder by health

5. **View Metrics**
   - Click on any AceStream channel in the list
   - View health score trends over 24 hours
   - See peers, download speed, and health metrics

### How It Works

**Health Scoring**
Streams are scored 0-100 based on:
- Peer count (0-25 points)
- Download speed (0-25 points)
- Upload contribution (0-10 points)
- FFmpeg working (20 points)
- Bitrate quality (0-15 points)
- Error penalty (-5 per error, max -20)

**Automatic Reordering**
- Every monitoring interval, all streams are checked
- Health scores are calculated
- Streams are reordered in Dispatcharr with healthiest first
- This ensures viewers always get the best available stream

**Resource Efficient**
- FFmpeg probes are short (default 5 seconds)
- Lightweight Orchestrator API calls
- Configurable intervals to reduce load
- Graceful cleanup on shutdown

### Troubleshooting

**Monitoring Won't Start**
- Check Orchestrator URL is correct and reachable
- Ensure FFmpeg is installed in the container
- Check logs for error messages

**No Metrics Showing**
- Ensure channels are tagged as AceStream
- Verify streams have AceStream URLs (contain `?id=`)
- Wait for one monitoring cycle to complete

**Streams Not Reordering**
- Check that monitoring is running
- Verify health scores are being calculated
- Ensure UDI sync is working

### Configuration Reference

| Setting | Default | Range | Description |
|---------|---------|-------|-------------|
| Orchestrator URL | `http://gluetun:19000` | - | AceStream Orchestrator API endpoint |
| Monitoring Interval | 30 | 10-300 seconds | How often to check streams |
| FFmpeg Probe Duration | 5 | 3-15 seconds | How long to probe each stream |
| Enabled | false | boolean | Enable/disable monitoring service |

### API Endpoints

See the main implementation guide for full API documentation. Key endpoints:

- `GET /api/acestream/config` - Get configuration
- `POST /api/acestream/config` - Update configuration
- `GET /api/acestream/channels` - List AceStream channels
- `POST /api/acestream/channels/<id>/tag` - Tag/untag channel
- `GET /api/acestream/monitoring/status` - Get monitoring status
- `POST /api/acestream/monitoring/start` - Start monitoring
- `POST /api/acestream/monitoring/stop` - Stop monitoring
- `GET /api/acestream/monitoring/channel/<id>/metrics` - Get channel metrics

## Documentation Requirements

1. **User Guide**: How to configure and use AceStream monitoring
2. **API Documentation**: Document all new endpoints
3. **Architecture Document**: System architecture and design decisions
4. **Troubleshooting Guide**: Common issues and solutions
5. **Configuration Reference**: All configuration options

## Notes for Next Agent

This is a substantial feature that requires:
- Backend service development (Python)
- Database schema design and implementation
- Frontend UI development (React)
- API integration with Dispatcharr
- FFmpeg integration for stream monitoring
- Real-time data visualization

Estimated effort: 3-5 days of focused development work.

Priority order if implementing incrementally:
1. Database and data models
2. Basic monitoring service without auto-ordering
3. API endpoints
4. Frontend UI
5. Auto-ordering functionality
6. Polish and optimization
