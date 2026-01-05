"""
Database for storing AceStream monitoring data.

This module provides a SQLite-based storage system for AceStream monitoring metrics,
including stream sessions, health scores, and time-series data.
"""

import sqlite3
import os
from datetime import datetime
from typing import Dict, List, Optional, Any
from pathlib import Path

from logging_config import setup_logging

logger = setup_logging(__name__)


class AceStreamDatabase:
    """Database for storing AceStream monitoring data."""
    
    def __init__(self, db_path: Optional[str] = None):
        """
        Initialize the AceStream database.
        
        Args:
            db_path: Path to SQLite database file. Defaults to data/acestream_monitoring.db
        """
        if db_path is None:
            config_dir = Path(os.environ.get('CONFIG_DIR', '/app/data'))
            db_path = config_dir / 'acestream_monitoring.db'
        
        self.db_path = str(db_path)
        self._init_db()
        logger.info(f"AceStream database initialized at {self.db_path}")
    
    def _init_db(self):
        """Initialize database tables."""
        # Ensure directory exists
        os.makedirs(os.path.dirname(self.db_path) or '.', exist_ok=True)
        
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
            
            cursor.execute('''
                CREATE INDEX IF NOT EXISTS idx_sessions_status 
                ON monitoring_sessions(status)
            ''')
            
            conn.commit()
    
    def create_session(
        self,
        stream_id: int,
        channel_id: int,
        acestream_id: str,
        command_url: Optional[str] = None,
        stat_url: Optional[str] = None
    ) -> int:
        """
        Create a new monitoring session.
        
        Args:
            stream_id: Stream ID
            channel_id: Channel ID
            acestream_id: AceStream content ID
            command_url: Optional command URL from Orchestrator
            stat_url: Optional stats URL from Orchestrator
            
        Returns:
            Session ID
        """
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO monitoring_sessions 
                (stream_id, channel_id, acestream_id, started_at, status, command_url, stat_url)
                VALUES (?, ?, ?, ?, 'active', ?, ?)
            ''', (stream_id, channel_id, acestream_id, datetime.now(), command_url, stat_url))
            
            session_id = cursor.lastrowid
            conn.commit()
            
        logger.debug(f"Created monitoring session {session_id} for stream {stream_id}")
        return session_id
    
    def get_active_session(self, stream_id: int) -> Optional[Dict[str, Any]]:
        """
        Get the active monitoring session for a stream.
        
        Args:
            stream_id: Stream ID
            
        Returns:
            Session data dict or None if no active session
        """
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute('''
                SELECT * FROM monitoring_sessions 
                WHERE stream_id = ? AND status = 'active'
                ORDER BY started_at DESC
                LIMIT 1
            ''', (stream_id,))
            
            row = cursor.fetchone()
            return dict(row) if row else None
    
    def close_session(self, session_id: int):
        """
        Close a monitoring session.
        
        Args:
            session_id: Session ID to close
        """
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                UPDATE monitoring_sessions 
                SET status = 'stopped', ended_at = ?
                WHERE id = ?
            ''', (datetime.now(), session_id))
            conn.commit()
    
    def close_all_active_sessions(self):
        """Close all active monitoring sessions."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                UPDATE monitoring_sessions 
                SET status = 'stopped', ended_at = ?
                WHERE status = 'active'
            ''', (datetime.now(),))
            conn.commit()
            
        logger.info("Closed all active monitoring sessions")
    
    def save_metrics(
        self,
        session_id: int,
        health_score: float,
        orchestrator_stats: Optional[Dict[str, Any]] = None,
        ffmpeg_stats: Optional[Dict[str, Any]] = None
    ):
        """
        Save stream metrics to database.
        
        Args:
            session_id: Session ID
            health_score: Calculated health score (0-100)
            orchestrator_stats: Optional stats from Orchestrator
            ffmpeg_stats: Optional stats from FFmpeg
        """
        with sqlite3.connect(self.db_path) as conn:
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
    
    def get_channel_metrics(self, channel_id: int, hours: int = 24) -> List[Dict[str, Any]]:
        """
        Get aggregated metrics for a channel.
        Shows total (summed) download/upload/peer counts from all streams.
        
        Args:
            channel_id: Channel ID
            hours: Number of hours of history to return
            
        Returns:
            List of metric data points with summed values
        """
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            
            cursor.execute('''
                SELECT 
                    m.timestamp,
                    AVG(m.health_score) as avg_health_score,
                    SUM(m.peers) as total_peers,
                    SUM(m.speed_down) as total_speed_down,
                    SUM(m.speed_up) as total_speed_up,
                    SUM(m.downloaded) as total_downloaded,
                    SUM(m.uploaded) as total_uploaded,
                    AVG(m.ffmpeg_bitrate) as avg_bitrate
                FROM stream_metrics m
                JOIN monitoring_sessions s ON m.session_id = s.id
                WHERE s.channel_id = ?
                AND m.timestamp >= datetime('now', '-' || ? || ' hours')
                GROUP BY strftime('%Y-%m-%d %H:%M', m.timestamp)
                ORDER BY m.timestamp ASC
            ''', (channel_id, hours))
            
            return [dict(row) for row in cursor.fetchall()]
    
    def get_latest_stream_health(self, stream_id: int) -> Optional[Dict[str, Any]]:
        """
        Get the latest health metrics for a stream.
        
        Args:
            stream_id: Stream ID
            
        Returns:
            Latest health metrics or None
        """
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            
            cursor.execute('''
                SELECT m.*
                FROM stream_metrics m
                JOIN monitoring_sessions s ON m.session_id = s.id
                WHERE s.stream_id = ? AND s.status = 'active'
                ORDER BY m.timestamp DESC
                LIMIT 1
            ''', (stream_id,))
            
            row = cursor.fetchone()
            return dict(row) if row else None
    
    def get_active_sessions_for_channel(self, channel_id: int) -> List[Dict[str, Any]]:
        """
        Get all active sessions for a channel.
        
        Args:
            channel_id: Channel ID
            
        Returns:
            List of active session data
        """
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            
            cursor.execute('''
                SELECT * FROM monitoring_sessions 
                WHERE channel_id = ? AND status = 'active'
                ORDER BY started_at DESC
            ''', (channel_id,))
            
            return [dict(row) for row in cursor.fetchall()]
    
    def cleanup_old_metrics(self, days: int = 7):
        """
        Clean up old metrics data.
        
        Args:
            days: Keep metrics newer than this many days
        """
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            
            # Delete old metrics
            cursor.execute('''
                DELETE FROM stream_metrics 
                WHERE timestamp < datetime('now', '-' || ? || ' days')
            ''', (days,))
            
            deleted = cursor.rowcount
            conn.commit()
            
        logger.info(f"Cleaned up {deleted} old metric records")
        return deleted
