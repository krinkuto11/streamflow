#!/usr/bin/env python3
"""
Unified Data Index (UDI) for Streamflow.

This module provides a centralized SQLite-based database that acts as an optimized
index containing all data from Dispatcharr. The index is rebuilt on every M3U refresh
and serves as the single source of truth for all stream checking, matching, and 
related tools.

Key Features:
- SQLite-based persistent storage for fast local queries
- Rebuilt on every M3U refresh from Dispatcharr API
- Tracks all changes made by tools (changelog system)
- Single point of truth for stream, account, and channel data
- Thread-safe operations

Architecture:
- M3U refresh triggers index rebuild (full refresh from Dispatcharr)
- All tools read from this index instead of making API calls
- Changes are recorded to the index and a pending_changes table
- DispatcharrSyncService reads pending_changes and makes API calls
"""

import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from logging_config import setup_logging

logger = setup_logging(__name__)

# Configuration directory - persisted via Docker volume
CONFIG_DIR = Path(os.environ.get('CONFIG_DIR', '/app/data'))


class UnifiedDataIndex:
    """
    Centralized SQLite-based index for all Dispatcharr data.
    
    This class maintains a local database of streams, channels, accounts,
    and groups that is synchronized with Dispatcharr on M3U refresh.
    All stream checking and matching tools should use this index instead
    of making direct API calls.
    """
    
    _instance = None
    _lock = threading.Lock()
    
    def __new__(cls, db_path: Optional[Path] = None):
        """Singleton pattern to ensure only one instance exists."""
        with cls._lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._initialized = False
            return cls._instance
    
    def __init__(self, db_path: Optional[Path] = None):
        """
        Initialize the Unified Data Index.
        
        Args:
            db_path: Path to the SQLite database file. Defaults to CONFIG_DIR/unified_index.db
        """
        if self._initialized:
            return
            
        if db_path is None:
            db_path = CONFIG_DIR / 'unified_index.db'
        
        self.db_path = Path(db_path)
        self._local = threading.local()
        self._write_lock = threading.Lock()
        
        # Ensure database directory exists
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        
        # Initialize database schema
        self._init_database()
        
        self._initialized = True
        logger.info(f"Unified Data Index initialized at {self.db_path}")
    
    def _get_connection(self) -> sqlite3.Connection:
        """Get thread-local database connection."""
        if not hasattr(self._local, 'conn') or self._local.conn is None:
            self._local.conn = sqlite3.connect(
                str(self.db_path),
                check_same_thread=False,
                timeout=30.0
            )
            self._local.conn.row_factory = sqlite3.Row
            # Enable foreign keys
            self._local.conn.execute("PRAGMA foreign_keys = ON")
            # Enable WAL mode for better concurrent access
            self._local.conn.execute("PRAGMA journal_mode = WAL")
        return self._local.conn
    
    @contextmanager
    def _get_cursor(self, commit: bool = False):
        """Context manager for database cursor with optional commit."""
        conn = self._get_connection()
        cursor = conn.cursor()
        try:
            yield cursor
            if commit:
                conn.commit()
        except Exception as e:
            conn.rollback()
            raise e
        finally:
            cursor.close()
    
    def _init_database(self):
        """Initialize database schema."""
        with self._write_lock:
            conn = self._get_connection()
            cursor = conn.cursor()
            
            # M3U Accounts table
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS m3u_accounts (
                    id INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    server_url TEXT,
                    file_path TEXT,
                    max_streams INTEGER DEFAULT 0,
                    is_active INTEGER DEFAULT 1,
                    account_type TEXT DEFAULT 'STD',
                    priority INTEGER DEFAULT 0,
                    status TEXT,
                    last_message TEXT,
                    custom_properties TEXT,
                    created_at TEXT,
                    updated_at TEXT,
                    synced_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            # Channel Groups table
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS channel_groups (
                    id INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    channel_count INTEGER DEFAULT 0,
                    m3u_account_count INTEGER DEFAULT 0,
                    synced_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            # Channels table
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS channels (
                    id INTEGER PRIMARY KEY,
                    channel_number REAL,
                    name TEXT NOT NULL,
                    channel_group_id INTEGER,
                    tvg_id TEXT,
                    epg_data_id INTEGER,
                    stream_profile_id INTEGER,
                    uuid TEXT,
                    logo_id INTEGER,
                    user_level INTEGER DEFAULT 0,
                    auto_created INTEGER DEFAULT 0,
                    auto_created_by INTEGER,
                    synced_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (channel_group_id) REFERENCES channel_groups(id)
                )
            ''')
            
            # Streams table
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS streams (
                    id INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    url TEXT,
                    m3u_account_id INTEGER,
                    logo_url TEXT,
                    tvg_id TEXT,
                    current_viewers INTEGER DEFAULT 0,
                    stream_profile_id INTEGER,
                    is_custom INTEGER DEFAULT 0,
                    channel_group_id INTEGER,
                    stream_hash TEXT,
                    stream_stats TEXT,
                    stream_stats_updated_at TEXT,
                    updated_at TEXT,
                    last_seen TEXT,
                    synced_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (m3u_account_id) REFERENCES m3u_accounts(id),
                    FOREIGN KEY (channel_group_id) REFERENCES channel_groups(id)
                )
            ''')
            
            # Channel-Stream relationship table
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS channel_streams (
                    channel_id INTEGER NOT NULL,
                    stream_id INTEGER NOT NULL,
                    position INTEGER DEFAULT 0,
                    synced_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (channel_id, stream_id),
                    FOREIGN KEY (channel_id) REFERENCES channels(id) ON DELETE CASCADE,
                    FOREIGN KEY (stream_id) REFERENCES streams(id) ON DELETE CASCADE
                )
            ''')
            
            # Pending changes table - tracks modifications to be synced to Dispatcharr
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS pending_changes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    entity_type TEXT NOT NULL,
                    entity_id INTEGER NOT NULL,
                    operation TEXT NOT NULL,
                    field_name TEXT,
                    old_value TEXT,
                    new_value TEXT,
                    payload TEXT,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    synced_at TEXT,
                    sync_status TEXT DEFAULT 'pending',
                    sync_error TEXT
                )
            ''')
            
            # Changelog table - permanent record of all changes
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS changelog (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT DEFAULT CURRENT_TIMESTAMP,
                    action TEXT NOT NULL,
                    entity_type TEXT,
                    entity_id INTEGER,
                    entity_name TEXT,
                    details TEXT,
                    source TEXT DEFAULT 'system'
                )
            ''')
            
            # Index metadata table
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS index_metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            # Create indexes for performance
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_streams_m3u_account ON streams(m3u_account_id)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_streams_channel_group ON streams(channel_group_id)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_streams_name ON streams(name)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_channels_group ON channels(channel_group_id)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_channel_streams_channel ON channel_streams(channel_id)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_channel_streams_stream ON channel_streams(stream_id)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_pending_changes_status ON pending_changes(sync_status)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_changelog_timestamp ON changelog(timestamp)')
            
            conn.commit()
            cursor.close()
            logger.debug("Database schema initialized")
    
    def rebuild_from_dispatcharr(self, api_fetcher, progress_callback=None) -> Dict[str, int]:
        """
        Rebuild the entire index from Dispatcharr API.
        
        This should be called after every M3U refresh to ensure the index
        is in sync with Dispatcharr.
        
        Progress is printed differently based on DEBUG_MODE:
        - DEBUG_MODE=true: Detailed logging of each step with timing
        - DEBUG_MODE=false: Simple progress bar with percentage
        
        Args:
            api_fetcher: Object with methods to fetch data from Dispatcharr:
                - fetch_m3u_accounts() -> List[Dict]
                - fetch_channel_groups() -> List[Dict]
                - fetch_channels() -> List[Dict]
                - fetch_streams() -> List[Dict]
                - fetch_channel_streams(channel_id) -> List[Dict]
            progress_callback: Optional callback(step, current, total, message)
                for external progress tracking
        
        Returns:
            Dict with counts of synced entities
        """
        import os
        import sys
        
        debug_mode = os.getenv('DEBUG_MODE', 'false').lower() in ('true', '1', 'yes', 'on')
        
        logger.info("=" * 60)
        logger.info("REBUILDING UNIFIED DATA INDEX FROM DISPATCHARR")
        logger.info("=" * 60)
        start_time = datetime.now()
        
        counts = {
            'accounts': 0,
            'groups': 0,
            'channels': 0,
            'streams': 0,
            'channel_streams': 0
        }
        
        # Progress tracking steps
        steps = ['accounts', 'groups', 'streams', 'channels', 'channel_streams', 'finalize']
        current_step = 0
        total_steps = len(steps)
        
        def _report_progress(step_name: str, current: int, total: int, message: str = ""):
            """Report progress based on debug mode."""
            nonlocal current_step
            current_step = steps.index(step_name) + 1 if step_name in steps else current_step
            
            if progress_callback:
                progress_callback(step_name, current, total, message)
            
            if debug_mode:
                # Advanced debug output with detailed info
                elapsed = (datetime.now() - start_time).total_seconds()
                if total > 0:
                    logger.debug(f"  [{step_name}] {current}/{total} ({100*current/total:.1f}%) - {message} [{elapsed:.1f}s elapsed]")
                else:
                    logger.debug(f"  [{step_name}] {message} [{elapsed:.1f}s elapsed]")
            else:
                # Simple progress bar for non-debug mode
                overall_pct = (current_step / total_steps) * 100
                bar_width = 30
                filled = int(bar_width * current_step / total_steps)
                bar = '█' * filled + '░' * (bar_width - filled)
                progress_msg = f"\r  Reindexing: [{bar}] {overall_pct:.0f}% - {step_name}"
                # Print to stdout with carriage return for in-place update
                sys.stdout.write(progress_msg)
                sys.stdout.flush()
        
        with self._write_lock:
            conn = self._get_connection()
            cursor = conn.cursor()
            
            try:
                # Clear existing data (but preserve pending changes and changelog)
                _report_progress('accounts', 0, 0, "Clearing old data...")
                cursor.execute('DELETE FROM channel_streams')
                cursor.execute('DELETE FROM streams')
                cursor.execute('DELETE FROM channels')
                cursor.execute('DELETE FROM channel_groups')
                cursor.execute('DELETE FROM m3u_accounts')
                
                # Sync M3U accounts
                step_start = datetime.now()
                _report_progress('accounts', 0, 0, "Fetching M3U accounts...")
                accounts = api_fetcher.fetch_m3u_accounts()
                if accounts:
                    total_accounts = len(accounts)
                    logger.info(f"Indexing {total_accounts} M3U accounts...")
                    for idx, account in enumerate(accounts, 1):
                        self._insert_account(cursor, account)
                        counts['accounts'] += 1
                        if debug_mode and idx % 10 == 0:
                            _report_progress('accounts', idx, total_accounts, f"Account: {account.get('name', 'Unknown')}")
                    step_elapsed = (datetime.now() - step_start).total_seconds()
                    logger.info(f"  ✓ Indexed {counts['accounts']} accounts in {step_elapsed:.2f}s")
                else:
                    logger.info("  ⚠ No M3U accounts found")
                
                # Sync channel groups
                step_start = datetime.now()
                _report_progress('groups', 0, 0, "Fetching channel groups...")
                groups = api_fetcher.fetch_channel_groups()
                if groups:
                    total_groups = len(groups)
                    logger.info(f"Indexing {total_groups} channel groups...")
                    for idx, group in enumerate(groups, 1):
                        self._insert_group(cursor, group)
                        counts['groups'] += 1
                        if debug_mode and idx % 10 == 0:
                            _report_progress('groups', idx, total_groups, f"Group: {group.get('name', 'Unknown')}")
                    step_elapsed = (datetime.now() - step_start).total_seconds()
                    logger.info(f"  ✓ Indexed {counts['groups']} groups in {step_elapsed:.2f}s")
                else:
                    logger.info("  ⚠ No channel groups found")
                
                # Sync streams
                step_start = datetime.now()
                _report_progress('streams', 0, 0, "Fetching streams...")
                streams = api_fetcher.fetch_streams()
                if streams:
                    total_streams = len(streams)
                    logger.info(f"Indexing {total_streams} streams...")
                    for idx, stream in enumerate(streams, 1):
                        self._insert_stream(cursor, stream)
                        counts['streams'] += 1
                        # Report progress more frequently for streams (every 100 or 5%)
                        if idx % max(100, total_streams // 20) == 0:
                            _report_progress('streams', idx, total_streams, f"Stream {idx}/{total_streams}")
                    step_elapsed = (datetime.now() - step_start).total_seconds()
                    logger.info(f"  ✓ Indexed {counts['streams']} streams in {step_elapsed:.2f}s")
                else:
                    logger.warning("  ⚠ No streams found - this may indicate a problem with the Dispatcharr connection")
                
                # Sync channels
                step_start = datetime.now()
                _report_progress('channels', 0, 0, "Fetching channels...")
                channels = api_fetcher.fetch_channels()
                if channels:
                    total_channels = len(channels)
                    logger.info(f"Indexing {total_channels} channels and their stream assignments...")
                    for idx, channel in enumerate(channels, 1):
                        self._insert_channel(cursor, channel)
                        counts['channels'] += 1
                        
                        # Sync channel-stream relationships
                        channel_id = channel.get('id')
                        stream_ids = channel.get('streams', [])
                        if stream_ids:
                            for position, stream_id in enumerate(stream_ids):
                                cursor.execute('''
                                    INSERT OR REPLACE INTO channel_streams 
                                    (channel_id, stream_id, position, synced_at)
                                    VALUES (?, ?, ?, ?)
                                ''', (channel_id, stream_id, position, datetime.now().isoformat()))
                                counts['channel_streams'] += 1
                        
                        # Report progress for channels (every 10 or 5%)
                        if idx % max(10, total_channels // 20) == 0:
                            _report_progress('channels', idx, total_channels, f"Channel {idx}/{total_channels}: {channel.get('name', 'Unknown')}")
                    
                    step_elapsed = (datetime.now() - step_start).total_seconds()
                    logger.info(f"  ✓ Indexed {counts['channels']} channels with {counts['channel_streams']} stream assignments in {step_elapsed:.2f}s")
                else:
                    logger.warning("  ⚠ No channels found")
                
                # Finalize
                _report_progress('finalize', 0, 0, "Finalizing index...")
                
                # Update metadata
                cursor.execute('''
                    INSERT OR REPLACE INTO index_metadata (key, value, updated_at)
                    VALUES ('last_full_sync', ?, ?)
                ''', (datetime.now().isoformat(), datetime.now().isoformat()))
                
                # Add changelog entry using internal method (already holding lock)
                elapsed = (datetime.now() - start_time).total_seconds()
                self._add_changelog_entry_internal(
                    cursor,
                    action='index_rebuild',
                    entity_type='index',
                    details=json.dumps({
                        'counts': counts,
                        'elapsed_seconds': elapsed
                    }),
                    source='m3u_refresh'
                )
                
                conn.commit()
                
                # Clear progress line in non-debug mode
                if not debug_mode:
                    sys.stdout.write('\r' + ' ' * 70 + '\r')  # Clear the line
                    sys.stdout.flush()
                
                logger.info("=" * 60)
                logger.info(f"INDEX REBUILD COMPLETE in {elapsed:.2f}s")
                logger.info(f"  Accounts: {counts['accounts']}")
                logger.info(f"  Groups: {counts['groups']}")
                logger.info(f"  Streams: {counts['streams']}")
                logger.info(f"  Channels: {counts['channels']}")
                logger.info(f"  Channel-Stream assignments: {counts['channel_streams']}")
                logger.info("=" * 60)
                
            except Exception as e:
                conn.rollback()
                # Clear progress line on error
                if not debug_mode:
                    sys.stdout.write('\r' + ' ' * 70 + '\r')
                    sys.stdout.flush()
                logger.error(f"Failed to rebuild index: {e}")
                raise
            finally:
                cursor.close()
        
        return counts
    
    def _insert_account(self, cursor, account: Dict):
        """Insert or update an M3U account."""
        cursor.execute('''
            INSERT OR REPLACE INTO m3u_accounts 
            (id, name, server_url, file_path, max_streams, is_active, account_type, 
             priority, status, last_message, custom_properties, created_at, updated_at, synced_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            account.get('id'),
            account.get('name'),
            account.get('server_url'),
            account.get('file_path'),
            account.get('max_streams', 0),
            1 if account.get('is_active', True) else 0,
            account.get('account_type', 'STD'),
            account.get('priority', 0),
            account.get('status'),
            account.get('last_message'),
            json.dumps(account.get('custom_properties')) if account.get('custom_properties') else None,
            account.get('created_at'),
            account.get('updated_at'),
            datetime.now().isoformat()
        ))
    
    def _insert_group(self, cursor, group: Dict):
        """Insert or update a channel group."""
        cursor.execute('''
            INSERT OR REPLACE INTO channel_groups 
            (id, name, channel_count, m3u_account_count, synced_at)
            VALUES (?, ?, ?, ?, ?)
        ''', (
            group.get('id'),
            group.get('name'),
            group.get('channel_count', 0),
            group.get('m3u_account_count', 0),
            datetime.now().isoformat()
        ))
    
    def _insert_channel(self, cursor, channel: Dict):
        """Insert or update a channel."""
        cursor.execute('''
            INSERT OR REPLACE INTO channels 
            (id, channel_number, name, channel_group_id, tvg_id, epg_data_id, 
             stream_profile_id, uuid, logo_id, user_level, auto_created, 
             auto_created_by, synced_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            channel.get('id'),
            channel.get('channel_number'),
            channel.get('name'),
            channel.get('channel_group_id'),
            channel.get('tvg_id'),
            channel.get('epg_data_id'),
            channel.get('stream_profile_id'),
            channel.get('uuid'),
            channel.get('logo_id'),
            channel.get('user_level', 0),
            1 if channel.get('auto_created') else 0,
            channel.get('auto_created_by'),
            datetime.now().isoformat()
        ))
    
    def _insert_stream(self, cursor, stream: Dict):
        """Insert or update a stream."""
        cursor.execute('''
            INSERT OR REPLACE INTO streams 
            (id, name, url, m3u_account_id, logo_url, tvg_id, current_viewers,
             stream_profile_id, is_custom, channel_group_id, stream_hash,
             stream_stats, stream_stats_updated_at, updated_at, last_seen, synced_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            stream.get('id'),
            stream.get('name'),
            stream.get('url'),
            stream.get('m3u_account'),
            stream.get('logo_url'),
            stream.get('tvg_id'),
            stream.get('current_viewers', 0),
            stream.get('stream_profile_id'),
            1 if stream.get('is_custom') else 0,
            stream.get('channel_group'),
            stream.get('stream_hash'),
            json.dumps(stream.get('stream_stats')) if stream.get('stream_stats') else None,
            stream.get('stream_stats_updated_at'),
            stream.get('updated_at'),
            stream.get('last_seen'),
            datetime.now().isoformat()
        ))
    
    # ==================== READ OPERATIONS ====================
    
    def get_all_streams(self, m3u_account_id: Optional[int] = None, 
                        is_active_account: bool = True) -> List[Dict]:
        """
        Get all streams from the index.
        
        Args:
            m3u_account_id: Filter by specific M3U account ID
            is_active_account: Only include streams from active accounts
            
        Returns:
            List of stream dictionaries
        """
        with self._get_cursor() as cursor:
            if m3u_account_id is not None:
                cursor.execute('''
                    SELECT s.* FROM streams s
                    JOIN m3u_accounts a ON s.m3u_account_id = a.id
                    WHERE s.m3u_account_id = ? AND (? = 0 OR a.is_active = 1)
                ''', (m3u_account_id, 0 if not is_active_account else 1))
            elif is_active_account:
                cursor.execute('''
                    SELECT s.* FROM streams s
                    LEFT JOIN m3u_accounts a ON s.m3u_account_id = a.id
                    WHERE s.is_custom = 1 OR a.is_active = 1 OR s.m3u_account_id IS NULL
                ''')
            else:
                cursor.execute('SELECT * FROM streams')
            
            return [self._row_to_dict(row) for row in cursor.fetchall()]
    
    def get_stream(self, stream_id: int) -> Optional[Dict]:
        """Get a single stream by ID."""
        with self._get_cursor() as cursor:
            cursor.execute('SELECT * FROM streams WHERE id = ?', (stream_id,))
            row = cursor.fetchone()
            return self._row_to_dict(row) if row else None
    
    def get_streams_by_ids(self, stream_ids: List[int]) -> List[Dict]:
        """Get multiple streams by their IDs."""
        if not stream_ids:
            return []
        
        placeholders = ','.join(['?' for _ in stream_ids])
        with self._get_cursor() as cursor:
            cursor.execute(f'SELECT * FROM streams WHERE id IN ({placeholders})', stream_ids)
            return [self._row_to_dict(row) for row in cursor.fetchall()]
    
    def get_valid_stream_ids(self) -> Set[int]:
        """Get set of all valid stream IDs in the index."""
        with self._get_cursor() as cursor:
            cursor.execute('SELECT id FROM streams')
            return {row[0] for row in cursor.fetchall()}
    
    def get_stream_url_mapping(self) -> Dict[int, str]:
        """Get mapping of stream IDs to URLs."""
        with self._get_cursor() as cursor:
            cursor.execute('SELECT id, url FROM streams')
            return {row[0]: row[1] for row in cursor.fetchall()}
    
    def get_all_channels(self) -> List[Dict]:
        """Get all channels from the index."""
        with self._get_cursor() as cursor:
            cursor.execute('SELECT * FROM channels ORDER BY channel_number')
            return [self._row_to_dict(row) for row in cursor.fetchall()]
    
    def get_channel(self, channel_id: int) -> Optional[Dict]:
        """Get a single channel by ID."""
        with self._get_cursor() as cursor:
            cursor.execute('SELECT * FROM channels WHERE id = ?', (channel_id,))
            row = cursor.fetchone()
            return self._row_to_dict(row) if row else None
    
    def get_channel_streams(self, channel_id: int) -> List[Dict]:
        """Get all streams for a channel, ordered by position."""
        with self._get_cursor() as cursor:
            cursor.execute('''
                SELECT s.*, cs.position FROM streams s
                JOIN channel_streams cs ON s.id = cs.stream_id
                WHERE cs.channel_id = ?
                ORDER BY cs.position
            ''', (channel_id,))
            return [self._row_to_dict(row) for row in cursor.fetchall()]
    
    def get_channel_stream_ids(self, channel_id: int) -> List[int]:
        """Get stream IDs for a channel, ordered by position."""
        with self._get_cursor() as cursor:
            cursor.execute('''
                SELECT stream_id FROM channel_streams
                WHERE channel_id = ?
                ORDER BY position
            ''', (channel_id,))
            return [row[0] for row in cursor.fetchall()]
    
    def get_all_m3u_accounts(self, is_active: bool = True) -> List[Dict]:
        """Get all M3U accounts from the index."""
        with self._get_cursor() as cursor:
            if is_active:
                cursor.execute('SELECT * FROM m3u_accounts WHERE is_active = 1')
            else:
                cursor.execute('SELECT * FROM m3u_accounts')
            return [self._row_to_dict(row) for row in cursor.fetchall()]
    
    def get_all_channel_groups(self) -> List[Dict]:
        """Get all channel groups from the index."""
        with self._get_cursor() as cursor:
            cursor.execute('SELECT * FROM channel_groups')
            return [self._row_to_dict(row) for row in cursor.fetchall()]
    
    # ==================== WRITE OPERATIONS (LOCAL) ====================
    
    def update_channel_streams(self, channel_id: int, stream_ids: List[int], 
                               source: str = 'system') -> bool:
        """
        Update streams for a channel in the local index and queue for sync.
        
        Args:
            channel_id: The channel ID
            stream_ids: Ordered list of stream IDs
            source: Source of the change (for changelog)
            
        Returns:
            True if successful
        """
        with self._write_lock:
            conn = self._get_connection()
            cursor = conn.cursor()
            
            try:
                # Get current stream IDs for comparison
                cursor.execute('''
                    SELECT stream_id FROM channel_streams 
                    WHERE channel_id = ? ORDER BY position
                ''', (channel_id,))
                old_stream_ids = [row[0] for row in cursor.fetchall()]
                
                # Only proceed if there's an actual change
                if old_stream_ids == stream_ids:
                    cursor.close()
                    return True
                
                # Delete existing relationships
                cursor.execute('DELETE FROM channel_streams WHERE channel_id = ?', (channel_id,))
                
                # Insert new relationships
                for position, stream_id in enumerate(stream_ids):
                    cursor.execute('''
                        INSERT INTO channel_streams (channel_id, stream_id, position, synced_at)
                        VALUES (?, ?, ?, ?)
                    ''', (channel_id, stream_id, position, datetime.now().isoformat()))
                
                # Queue pending change for Dispatcharr sync
                cursor.execute('''
                    INSERT INTO pending_changes 
                    (entity_type, entity_id, operation, old_value, new_value, payload)
                    VALUES (?, ?, ?, ?, ?, ?)
                ''', (
                    'channel',
                    channel_id,
                    'update_streams',
                    json.dumps(old_stream_ids),
                    json.dumps(stream_ids),
                    json.dumps({'streams': stream_ids})
                ))
                
                # Get channel name for changelog
                cursor.execute('SELECT name FROM channels WHERE id = ?', (channel_id,))
                row = cursor.fetchone()
                channel_name = row[0] if row else f'Channel {channel_id}'
                
                # Add changelog entry
                self._add_changelog_entry_internal(
                    cursor,
                    action='streams_reordered',
                    entity_type='channel',
                    entity_id=channel_id,
                    entity_name=channel_name,
                    details=json.dumps({
                        'old_count': len(old_stream_ids),
                        'new_count': len(stream_ids),
                        'added': list(set(stream_ids) - set(old_stream_ids)),
                        'removed': list(set(old_stream_ids) - set(stream_ids))
                    }),
                    source=source
                )
                
                conn.commit()
                logger.debug(f"Updated channel {channel_id} streams in index ({len(stream_ids)} streams)")
                return True
                
            except Exception as e:
                conn.rollback()
                logger.error(f"Failed to update channel streams: {e}")
                return False
            finally:
                cursor.close()
    
    def add_streams_to_channel(self, channel_id: int, stream_ids: List[int],
                                source: str = 'system') -> int:
        """
        Add streams to a channel (appending to existing streams).
        
        Args:
            channel_id: The channel ID
            stream_ids: Stream IDs to add
            source: Source of the change
            
        Returns:
            Number of streams actually added
        """
        current_streams = self.get_channel_stream_ids(channel_id)
        current_set = set(current_streams)
        
        # Only add streams that aren't already in the channel
        new_streams = [sid for sid in stream_ids if sid not in current_set]
        
        if not new_streams:
            return 0
        
        # Append new streams to existing
        updated_streams = current_streams + new_streams
        
        if self.update_channel_streams(channel_id, updated_streams, source):
            return len(new_streams)
        return 0
    
    def update_stream_stats(self, stream_id: int, stats: Dict,
                            source: str = 'stream_checker') -> bool:
        """
        Update stream statistics in the local index.
        
        Args:
            stream_id: The stream ID
            stats: Stream statistics dictionary
            source: Source of the change
            
        Returns:
            True if successful
        """
        with self._write_lock:
            conn = self._get_connection()
            cursor = conn.cursor()
            
            try:
                # Get current stats for comparison
                cursor.execute('SELECT stream_stats FROM streams WHERE id = ?', (stream_id,))
                row = cursor.fetchone()
                old_stats = json.loads(row[0]) if row and row[0] else {}
                
                # Merge stats
                merged_stats = {**old_stats, **stats}
                
                cursor.execute('''
                    UPDATE streams SET stream_stats = ?, stream_stats_updated_at = ?
                    WHERE id = ?
                ''', (json.dumps(merged_stats), datetime.now().isoformat(), stream_id))
                
                # Queue pending change
                cursor.execute('''
                    INSERT INTO pending_changes 
                    (entity_type, entity_id, operation, field_name, old_value, new_value, payload)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                ''', (
                    'stream',
                    stream_id,
                    'update',
                    'stream_stats',
                    json.dumps(old_stats),
                    json.dumps(merged_stats),
                    json.dumps({'stream_stats': merged_stats})
                ))
                
                conn.commit()
                return True
                
            except Exception as e:
                conn.rollback()
                logger.error(f"Failed to update stream stats: {e}")
                return False
            finally:
                cursor.close()
    
    # ==================== PENDING CHANGES ====================
    
    def get_pending_changes(self, limit: int = 100) -> List[Dict]:
        """Get pending changes that need to be synced to Dispatcharr."""
        with self._get_cursor() as cursor:
            cursor.execute('''
                SELECT * FROM pending_changes 
                WHERE sync_status = 'pending'
                ORDER BY created_at
                LIMIT ?
            ''', (limit,))
            return [self._row_to_dict(row) for row in cursor.fetchall()]
    
    def mark_change_synced(self, change_id: int, success: bool, error: str = None):
        """Mark a pending change as synced (or failed)."""
        with self._write_lock:
            conn = self._get_connection()
            cursor = conn.cursor()
            
            try:
                cursor.execute('''
                    UPDATE pending_changes 
                    SET sync_status = ?, synced_at = ?, sync_error = ?
                    WHERE id = ?
                ''', (
                    'synced' if success else 'failed',
                    datetime.now().isoformat(),
                    error,
                    change_id
                ))
                conn.commit()
            except Exception as e:
                conn.rollback()
                logger.error(f"Failed to mark change synced: {e}")
            finally:
                cursor.close()
    
    def clear_synced_changes(self, older_than_hours: int = 24):
        """Clear old synced changes from the pending changes table."""
        with self._write_lock:
            conn = self._get_connection()
            cursor = conn.cursor()
            
            try:
                cursor.execute('''
                    DELETE FROM pending_changes 
                    WHERE sync_status = 'synced' 
                    AND datetime(synced_at) < datetime('now', ?)
                ''', (f'-{older_than_hours} hours',))
                deleted = cursor.rowcount
                conn.commit()
                if deleted > 0:
                    logger.info(f"Cleared {deleted} old synced changes")
            except Exception as e:
                conn.rollback()
                logger.error(f"Failed to clear synced changes: {e}")
            finally:
                cursor.close()
    
    # ==================== CHANGELOG ====================
    
    def add_changelog_entry(self, action: str, entity_type: str = None,
                           entity_id: int = None, entity_name: str = None,
                           details: str = None, source: str = 'system'):
        """Add an entry to the changelog."""
        with self._write_lock:
            conn = self._get_connection()
            cursor = conn.cursor()
            
            try:
                self._add_changelog_entry_internal(
                    cursor, action, entity_type, entity_id, entity_name, details, source
                )
                conn.commit()
            except Exception as e:
                conn.rollback()
                logger.error(f"Failed to add changelog entry: {e}")
            finally:
                cursor.close()
    
    def _add_changelog_entry_internal(self, cursor, action: str, 
                                       entity_type: str = None,
                                       entity_id: int = None, 
                                       entity_name: str = None,
                                       details: str = None, 
                                       source: str = 'system'):
        """Internal method to add changelog entry (requires cursor)."""
        cursor.execute('''
            INSERT INTO changelog (action, entity_type, entity_id, entity_name, details, source)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', (action, entity_type, entity_id, entity_name, details, source))
    
    def get_changelog(self, days: int = 7, limit: int = 1000) -> List[Dict]:
        """Get recent changelog entries."""
        with self._get_cursor() as cursor:
            cursor.execute('''
                SELECT * FROM changelog 
                WHERE datetime(timestamp) > datetime('now', ?)
                ORDER BY timestamp DESC
                LIMIT ?
            ''', (f'-{days} days', limit))
            return [self._row_to_dict(row) for row in cursor.fetchall()]
    
    # ==================== UTILITIES ====================
    
    def _row_to_dict(self, row: sqlite3.Row) -> Dict:
        """Convert sqlite3.Row to dictionary."""
        if row is None:
            return None
        d = dict(row)
        # Parse JSON fields
        for key in ['custom_properties', 'stream_stats']:
            if key in d and d[key]:
                try:
                    d[key] = json.loads(d[key])
                except (json.JSONDecodeError, TypeError):
                    pass
        return d
    
    def get_stats(self) -> Dict:
        """Get index statistics."""
        with self._get_cursor() as cursor:
            stats = {}
            
            cursor.execute('SELECT COUNT(*) FROM m3u_accounts')
            stats['accounts'] = cursor.fetchone()[0]
            
            cursor.execute('SELECT COUNT(*) FROM channel_groups')
            stats['groups'] = cursor.fetchone()[0]
            
            cursor.execute('SELECT COUNT(*) FROM channels')
            stats['channels'] = cursor.fetchone()[0]
            
            cursor.execute('SELECT COUNT(*) FROM streams')
            stats['streams'] = cursor.fetchone()[0]
            
            cursor.execute('SELECT COUNT(*) FROM channel_streams')
            stats['channel_streams'] = cursor.fetchone()[0]
            
            cursor.execute("SELECT COUNT(*) FROM pending_changes WHERE sync_status = 'pending'")
            stats['pending_changes'] = cursor.fetchone()[0]
            
            cursor.execute('SELECT value FROM index_metadata WHERE key = ?', ('last_full_sync',))
            row = cursor.fetchone()
            stats['last_sync'] = row[0] if row else None
            
            return stats
    
    def close(self):
        """Close database connections."""
        if hasattr(self._local, 'conn') and self._local.conn:
            self._local.conn.close()
            self._local.conn = None


# Global instance getter
_udi_instance = None

def get_unified_data_index(db_path: Optional[Path] = None) -> UnifiedDataIndex:
    """Get the global Unified Data Index instance."""
    global _udi_instance
    if _udi_instance is None:
        _udi_instance = UnifiedDataIndex(db_path)
    return _udi_instance
