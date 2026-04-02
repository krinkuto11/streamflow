"""
Advanced Stream Session Manager

Manages event-based stream monitoring sessions with quality testing,
continuous monitoring, and reliability scoring using a Capped Sliding Window algorithm.
"""

import json
import logging
import os
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Any, Set
from dataclasses import dataclass, asdict
from collections import deque
import re

from apps.core.logging_config import setup_logging
from apps.udi import get_udi_manager

logger = setup_logging(__name__)

# Configuration constants
CONFIG_DIR = Path(os.environ.get('CONFIG_DIR', '/app/data'))
SESSION_CONFIG_FILE = CONFIG_DIR / 'stream_session_config.json'
SESSION_DATA_FILE = CONFIG_DIR / 'stream_session_data.json'

# Session defaults
DEFAULT_PRE_EVENT_MINUTES = 30
DEFAULT_STAGGER_MS = 1000
DEFAULT_TIMEOUT_MS = 30000
DEFAULT_PROBE_INTERVAL_MS = 1000
DEFAULT_EVALUATION_INTERVAL_MS = 1000
DEFAULT_SCREENSHOT_INTERVAL_SECONDS = 60
DEFAULT_WINDOW_SIZE = 3600  # 1 hour at 1 second interval (increased from 600)
DEFAULT_MAX_SCORE = 100.0
DEFAULT_MIN_SCORE = 0.0

# Quarantine & Review Lifecycle
DEFAULT_REVIEW_DURATION = 60.0  # 1 minute in review before stable (default)
QUARANTINE_DURATION = 900.0  # 15 minutes before retry

# Keep for backward compatibility imports, but will be overridden by instance config
REVIEW_DURATION = DEFAULT_REVIEW_DURATION


# Pre-compiled regex pattern for whitespace conversion (consistent with AutomatedStreamManager)
_WHITESPACE_PATTERN = re.compile(r'(?<!\\) +')


@dataclass
class StreamMetrics:
    """Metrics for a single stream at a point in time"""
    timestamp: float
    speed: float
    bitrate: float
    fps: float
    is_alive: bool
    buffering: bool = False
    reliability_score: float = 50.0
    status: str = 'review'
    status_reason: Optional[str] = None
    rank: Optional[int] = None
    loop_duration: Optional[float] = None
    display_logo_status: str = 'PENDING'

    

@dataclass
class StreamInfo:
    """Information about a monitored stream"""
    stream_id: int
    url: str
    name: str
    channel_id: int
    width: Optional[int] = None
    height: Optional[int] = None
    fps: Optional[float] = None
    bitrate: Optional[int] = None
    m3u_account: Optional[str] = None
    hdr_format: Optional[str] = None
    status: str = 'review'  # 'stable', 'review', 'quarantined'
    last_status_change: float = 0.0
    failure_count: int = 0
    reliability_score: float = 50.0  # Start at middle score
    metrics_history: Optional[deque] = None
    rank: Optional[int] = None
    last_screenshot_time: float = 0
    screenshot_path: Optional[str] = None
    # Low speed tracking for auto-quarantine
    low_speed_start_time: Optional[float] = None  # When speed first dropped below threshold
    status_reason: Optional[str] = None  # e.g., 'looping', 'dead', 'timeout'
    last_loop_time: Optional[float] = None  # When a loop was last detected
    last_logo_status: str = 'PENDING'
    display_logo_status: str = 'PENDING'
    consecutive_logo_misses: int = 0
    loop_duration: Optional[float] = None
    last_loop_log_time: float = 0.0
    last_logo_log_time: float = 0.0
    # Transport health evaluation (populated from FFmpeg DiagnosticTracker)
    transport_health: str = 'Healthy'           # Healthy | Degraded | Severe | Critical
    transport_health_summary: str = ''
    transport_error_density: float = 0.0        # peak errors/minute in last 60s
    
    @property
    def is_quarantined(self) -> bool:
        return self.status == 'quarantined'
    
    @is_quarantined.setter
    def is_quarantined(self, value: bool):
        # Allow setting via property for backward compatibility during deserialization
        # or legacy code usage
        if value:
            # Only update timestamp if it wasn't already quarantined
            if self.status != 'quarantined':
                self.status = 'quarantined'
                self.last_status_change = time.time()
        elif self.status == 'quarantined':
            # If was quarantined and setting to False, move to review (safer than stable)
            self.status = 'review'
            self.last_status_change = time.time()

    def __post_init__(self):
        if self.metrics_history is None:
            self.metrics_history = deque(maxlen=3600)
        if self.last_status_change == 0.0:
            self.last_status_change = time.time()


@dataclass
class SessionInfo:
    """Information about a monitoring session"""
    session_id: str
    channel_id: int
    channel_name: str
    regex_filter: str
    created_at: float
    is_active: bool
    pre_event_minutes: int = DEFAULT_PRE_EVENT_MINUTES
    stagger_ms: int = DEFAULT_STAGGER_MS
    timeout_ms: int = DEFAULT_TIMEOUT_MS
    probe_interval_ms: int = DEFAULT_PROBE_INTERVAL_MS
    evaluation_interval_ms: int = DEFAULT_EVALUATION_INTERVAL_MS
    screenshot_interval_seconds: int = DEFAULT_SCREENSHOT_INTERVAL_SECONDS
    window_size: int = DEFAULT_WINDOW_SIZE
    streams: Dict[int, StreamInfo] = None
    # EPG event attachment
    epg_event_id: Optional[int] = None
    epg_event_title: Optional[str] = None
    epg_event_start: Optional[str] = None
    epg_event_end: Optional[str] = None
    epg_event_description: Optional[str] = None
    # Channel logo
    channel_logo_url: Optional[str] = None
    logo_id: Optional[int] = None
    channel_tvg_id: Optional[str] = None
    # Auto-creation source (for tracking if created by rules)
    auto_created: bool = False
    auto_create_rule_id: Optional[str] = None
    # Detection toggles
    enable_looping_detection: bool = True
    enable_logo_detection: bool = True
    # Track quarantined stream IDs to prevent re-addition
    quarantined_stream_ids: Set[int] = None
    
    # Synchronization enforcement
    enforce_sync_interval_ms: int = 1000  # Default 1 second
    last_sync_time: float = 0.0
    
    # Matching configuration
    match_by_tvg_id: bool = False
    
    # Store advertisement periods (when global pardon is active)
    ad_periods: List[Dict[str, float]] = None
    
    def __post_init__(self):
        if self.streams is None:
            self.streams = {}
        if self.quarantined_stream_ids is None:
            self.quarantined_stream_ids = set()
        if self.ad_periods is None:
            self.ad_periods = []


class CappedSlidingWindow:
    """
    Capped Sliding Window algorithm for stream reliability scoring.
    
    Maintains a fixed-size window of recent measurements and calculates
    a reliability score that avoids large variance to minimize stream
    changes in Dispatcharr.
    """
    
    def __init__(self, window_size: int = DEFAULT_WINDOW_SIZE, 
                 max_score: float = DEFAULT_MAX_SCORE,
                 min_score: float = DEFAULT_MIN_SCORE):
        self.window_size = window_size
        self.max_score = max_score
        self.min_score = min_score
        self.measurements = deque(maxlen=window_size)
        
    def add_measurement(self, is_healthy: bool, speed: float = 1.0):
        """
        Add a measurement to the window.
        
        Args:
            is_healthy: Whether the stream is healthy (alive and not buffering)
            speed: Stream speed (1.0 is normal, <1.0 indicates buffering)
        """
        # Calculate health score for this measurement
        # Healthy stream with good speed = 1.0
        # Unhealthy or buffering = 0.0
        if not is_healthy:
            score = 0.0
        elif speed < 0.9:  # Buffering threshold
            score = 0.3  # Partial score for alive but buffering
        else:
            # Give partial credit based on speed
            score = min(1.0, speed)
            
        self.measurements.append(score)
        
    def get_score(self) -> float:
        """
        Calculate reliability score from the sliding window.
        
        Returns a score between min_score and max_score with limited variance.
        """
        if not self.measurements:
            return (self.max_score + self.min_score) / 2  # Neutral score
            
        # Calculate average health from measurements
        avg_health = sum(self.measurements) / len(self.measurements)
        
        # Apply dampening to reduce variance
        # Use exponential scaling to make changes gradual
        dampened_score = (avg_health ** 1.5) * (self.max_score - self.min_score) + self.min_score
        
        # Clamp to range
        return max(self.min_score, min(self.max_score, dampened_score))


class StreamSessionManager:
    """
    Manages advanced stream monitoring sessions.
    
    This class handles:
    - Session lifecycle (create, start, stop)
    - Stream discovery and testing
    - Continuous monitoring via FFmpeg
    - Reliability scoring with Capped Sliding Window
    - Screenshot capture
    - Metrics persistence
    """
    
    _instance = None
    _lock = threading.Lock()
    
    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
        return cls._instance
    
    def __init__(self):
        if hasattr(self, '_initialized'):
            return
            
        self._initialized = True
        self.sessions: Dict[str, SessionInfo] = {}
        self.session_locks: Dict[str, threading.Lock] = {}
        self.scoring_windows: Dict[str, Dict[int, CappedSlidingWindow]] = {}  # session_id -> stream_id -> window
        self.channel_ownership: Dict[int, str] = {}  # channel_id -> session_id
        
        from apps.database.manager import get_db_manager
        self.db = get_db_manager()

        # Load settings from SystemSetting
        from apps.database.models import SystemSetting
        from apps.database.connection import get_session
        session = get_session()
        try:
            setting = session.query(SystemSetting).filter(SystemSetting.key == 'session_settings').first()
            data = setting.value if setting else {}
            self.review_duration = data.get('review_duration', 60.0)
            self.loop_review_duration = data.get('loop_review_duration', 600.0)
        finally:
            session.close()

        self._last_streams_refresh = 0
        logger.info("StreamSessionManager initialized with SQL backend")

        self._load_sessions()

    def _load_settings(self): pass
    def save_settings(self): pass

    def get_review_duration(self) -> float: return self.review_duration
    def set_review_duration(self, duration: float):
        self.review_duration = float(duration)
        self._save_settings_to_db()

    def get_loop_review_duration(self) -> float: return self.loop_review_duration
    def set_loop_review_duration(self, duration: float):
        self.loop_review_duration = float(duration)
        self._save_settings_to_db()

    def _save_settings_to_db(self):
        from apps.database.models import SystemSetting
        from apps.database.connection import get_session
        session = get_session()
        try:
            s = session.query(SystemSetting).filter(SystemSetting.key == 'session_settings').first()
            if not s:
                 s = SystemSetting(key='session_settings')
                 session.add(s)
            s.value = {'review_duration': self.review_duration, 'loop_review_duration': self.loop_review_duration}
            session.commit()
        except: session.rollback()
        finally: session.close()

    def _load_sessions(self):
        from apps.database.models import MonitoringSession
        from apps.database.connection import get_session
        session = get_session()
        try:
            m_sessions = session.query(MonitoringSession).all()
            session_groups = {}
            for ms in m_sessions:
                raw = ms.raw_info or {}
                cs_id = raw.get('channel_session_id')
                if cs_id:
                     if cs_id not in session_groups: session_groups[cs_id] = []
                     session_groups[cs_id].append(ms)

            for cs_id, ms_list in session_groups.items():
                if not ms_list: continue
                first_raw = ms_list[0].raw_info
                
                s_info = SessionInfo(
                    session_id=cs_id,
                    channel_id=first_raw.get('channel_id', 0),
                    channel_name=first_raw.get('channel_name', ''),
                    regex_filter=first_raw.get('regex_filter', ''),
                    created_at=first_raw.get('created_at', 0.0),
                    is_active=False
                )
                for k, v in first_raw.items():
                    if hasattr(s_info, k) and k not in ['streams', 'quarantined_stream_ids']:
                         setattr(s_info, k, v)
                if 'quarantined_stream_ids' in first_raw:
                     s_info.quarantined_stream_ids = set(first_raw['quarantined_stream_ids'])

                for ms in ms_list:
                    raw = ms.raw_info or {}
                    stream_id = raw.get('stream_id')
                    if stream_id:
                        from apps.stream.stream_session_manager import StreamInfo
                        # Convert dict values to make sure types align or pass direct
                        # Handle conversion if StreamInfo dataclass accepts kwargs
                        try:
                             s_info.streams[stream_id] = StreamInfo(**{k: v for k,v in raw.items() if k in [f_name for f_name in [f.name for f in StreamInfo.__dataclass_fields__.values()]]})
                        except: pass
                self.sessions[cs_id] = s_info
                self.session_locks[cs_id] = threading.Lock()
            logger.info(f"Loaded {len(self.sessions)} sessions from SQL")
        except Exception as e:
            logger.error(f"Failed to load sessions: {e}")
        finally:
            session.close()

    def _save_sessions(self):
        try:
            import copy
            snapshot = copy.deepcopy(dict(self.sessions))
            def _execute_save(snapshot):
                from apps.database.models import MonitoringSession
                from apps.database.connection import get_session
                session = get_session()
                try:
                    for cs_id, s_info in snapshot.items():
                         # Pack Session Info
                         base_raw = {k: v for k, v in self._serialize_session(s_info).items() if k != 'streams'}
                         base_raw['channel_session_id'] = cs_id
                         
                         for stream_id, stream_info in s_info.streams.items():
                              ms_id = f"{cs_id}_{stream_id}"
                              ms = session.query(MonitoringSession).filter(MonitoringSession.session_id == ms_id).first()
                              if not ms:
                                   ms = MonitoringSession(session_id=ms_id)
                                   session.add(ms)
                              ms.stream_id = stream_id
                              ms.status = stream_info.status
                              ms.current_speed = getattr(stream_info, 'current_speed', 0.0)
                              ms.current_bitrate = getattr(stream_info, 'bitrate', 0)
                              
                              # Combine base_raw with stream info
                              from dataclasses import asdict
                              item_raw = base_raw.copy()
                              item_raw.update(asdict(stream_info) if not isinstance(stream_info, dict) else stream_info)
                              if 'metrics_history' in item_raw: del item_raw['metrics_history']
                              ms.raw_info = item_raw
                    session.commit()
                except Exception as e:
                    session.rollback()
                    logger.error(f"Background save sessions failed: {e}")
                finally: session.close()

            import threading
            threading.Thread(target=_execute_save, args=(snapshot,), daemon=True, name="SessionSaver").start()
        except Exception as e:
            logger.error(f"Failed to trigger save sessions: {e}")

    def _serialize_session(self, session: SessionInfo) -> Dict[str, Any]:
        """Serialize session to JSON-compatible dict"""
        data = asdict(session)
        # Convert nested dataclasses
        if 'streams' in data:
            streams_dict = {}
            for stream_id, stream_info in data['streams'].items():
                stream_dict = dict(stream_info) if isinstance(stream_info, dict) else asdict(stream_info)
                # Limit metrics history size for storage
                if 'metrics_history' in stream_dict and stream_dict['metrics_history']:
                    # Convert to list to ensure JSON serializability
                    stream_dict['metrics_history'] = list(stream_dict['metrics_history'])[-1000:]
                streams_dict[str(stream_id)] = stream_dict
            data['streams'] = streams_dict
            
        # Convert set to list for JSON serialization
        if 'quarantined_stream_ids' in data and isinstance(data['quarantined_stream_ids'], set):
            data['quarantined_stream_ids'] = list(data['quarantined_stream_ids'])
            
        return data
    
    def _deserialize_session(self, data: Dict[str, Any]) -> SessionInfo:
        """Deserialize session from JSON dict"""
        # Convert streams dict back to StreamInfo objects
        if 'streams' in data and data['streams']:
            streams = {}
            for stream_id, stream_data in data['streams'].items():
                # Handle migration from is_quarantined (bool) to status (str)
                if 'is_quarantined' in stream_data:
                    is_q = stream_data.pop('is_quarantined')
                    if is_q and 'status' not in stream_data:
                        stream_data['status'] = 'quarantined'
                
                # Convert metrics_history back to StreamMetrics objects
                if 'metrics_history' in stream_data and stream_data['metrics_history']:
                    metrics = []
                    for m in stream_data['metrics_history']:
                        if isinstance(m, dict):
                            metrics.append(StreamMetrics(**m))
                        else:
                            metrics.append(m)
                    # Convert list back to deque with maxlen
                    window_size = data.get('window_size', DEFAULT_WINDOW_SIZE)
                    stream_data['metrics_history'] = deque(metrics, maxlen=window_size)
                
                streams[int(stream_id)] = StreamInfo(**stream_data)
            data['streams'] = streams
        
        # Convert list back to set for internal use
        if 'quarantined_stream_ids' in data and isinstance(data['quarantined_stream_ids'], list):
            data['quarantined_stream_ids'] = set(data['quarantined_stream_ids'])
            
        return SessionInfo(**data)
    
    def create_session(self, channel_id: int, regex_filter: str = ".*",
                      pre_event_minutes: int = DEFAULT_PRE_EVENT_MINUTES,
                      epg_event: Optional[Dict[str, Any]] = None,
                      allow_duplicate_channel: bool = False,
                      skip_stream_refresh: bool = False,
                      evaluation_interval_ms: int = DEFAULT_EVALUATION_INTERVAL_MS,
                      match_by_tvg_id: bool = False,
                      enable_looping_detection: bool = True,
                      enable_logo_detection: bool = True,
                      **kwargs) -> str:
        """
        Create a new monitoring session.
        
        Args:
            channel_id: Dispatcharr channel ID to monitor
            regex_filter: Regex pattern to filter streams (optional if match_by_tvg_id is True)
            pre_event_minutes: Minutes before event to start monitoring
            epg_event: Optional EPG event to attach to this session
            allow_duplicate_channel: Allow creating a session if one already exists for this channel
            skip_stream_refresh: Skip refreshing global stream list (useful for batch operations)
            evaluation_interval_ms: Interval for stream evaluation in milliseconds
            match_by_tvg_id: Whether to match streams by TVG-ID
            enable_looping_detection: Whether to enable looping detection for this session
            enable_logo_detection: Whether to enable logo detection for this session
            **kwargs: Additional session parameters (auto_created, auto_create_rule_id, etc.)
            
        Returns:
            Session ID
        """
        # Check for existing active session for this channel
        if not allow_duplicate_channel and self.is_channel_in_active_session(channel_id):
            # Find the existing session and update its EPG info if a new EPG event is provided
            for s in self.sessions.values():
                if s.is_active and s.channel_id == channel_id:
                    logger.info(f"Using existing active session {s.session_id} for channel {channel_id}")
                    if epg_event:
                        s.epg_event_id = epg_event.get('id')
                        s.epg_event_title = epg_event.get('title')
                        s.epg_event_start = epg_event.get('start_time')
                        s.epg_event_end = epg_event.get('end_time')
                        s.epg_event_description = epg_event.get('description', '')
                        self._save_sessions()
                        logger.info(
                            f"Updated EPG info on existing session {s.session_id}: "
                            f"'{s.epg_event_title}'"
                        )
                    return s.session_id

        # Get channel info from UDI
        udi = get_udi_manager()
        
        # Always refresh the specific channel to ensure metadata (name, logo) is up to date
        udi.refresh_channel_by_id(channel_id)
        
        # Refresh global stream list with rate limiting (30s cooldown)
        # This prevents API spam when batch creating sessions while keeping data reasonably fresh
        if not skip_stream_refresh:
            current_time = time.time()
            if current_time - self._last_streams_refresh > 30:
                logger.info("Refreshing global stream list (cooldown passed)")
                if udi.refresh_streams():
                    self._last_streams_refresh = current_time
            else:
                logger.debug(
                    f"Skipping stream list refresh (cooldown active, "
                    f"{30 - (current_time - self._last_streams_refresh):.1f}s remaining)"
                )

        
        channel = udi.get_channel_by_id(channel_id)
        if not channel:
            raise ValueError(f"Channel {channel_id} not found")
        
        # Generate session ID
        session_id = f"session_{channel_id}_{int(time.time())}"
        
        # Extract EPG event info if provided
        epg_event_id = None
        epg_event_title = None
        epg_event_start = None
        epg_event_end = None
        epg_event_description = None
        if epg_event:
            epg_event_id = epg_event.get('id')
            epg_event_title = epg_event.get('title')
            epg_event_start = epg_event.get('start_time')
            epg_event_end = epg_event.get('end_time')
            epg_event_description = epg_event.get('description')
        
        # Get channel logo info
        channel_logo_url = None
        channel_tvg_id = channel.get('tvg_id')
        logo_id = channel.get('logo_id')
        if logo_id:
            # Use cached logo endpoint for better performance
            channel_logo_url = f"/api/channels/logos/{logo_id}/cache"
        
        # Create session
        session = SessionInfo(
            session_id=session_id,
            channel_id=channel_id,
            channel_name=channel.get('name', f'Channel {channel_id}'),
            regex_filter=regex_filter,
            created_at=time.time(),
            is_active=False,
            pre_event_minutes=pre_event_minutes,
            evaluation_interval_ms=evaluation_interval_ms,
            epg_event_id=epg_event_id,
            epg_event_title=epg_event_title,
            epg_event_start=epg_event_start,
            epg_event_end=epg_event_end,
            epg_event_description=epg_event_description,
            channel_logo_url=channel_logo_url,
            logo_id=logo_id,
            channel_tvg_id=channel_tvg_id,
            match_by_tvg_id=match_by_tvg_id,
            enable_looping_detection=enable_looping_detection,
            enable_logo_detection=enable_logo_detection,
            **kwargs
        )
        
        self.sessions[session_id] = session
        self.session_locks[session_id] = threading.Lock()
        self.scoring_windows[session_id] = {}
        
        self._save_sessions()
        logger.info(f"Created session {session_id} for channel {channel_id} (eval interval: {evaluation_interval_ms}ms)")
        
        return session_id
    
    def start_session(self, session_id: str) -> bool:
        """
        Start monitoring for a session.
        
        This will:
        1. Fetch matching streams from Dispatcharr
        2. Test streams for quality
        3. Start continuous monitoring
        4. Begin reliability scoring
        
        Args:
            session_id: Session to start
            
        Returns:
            True if started successfully
        """
        if session_id not in self.sessions:
            logger.error(f"Session {session_id} not found")
            return False
        
        session = self.sessions[session_id]
        if session.is_active:
            logger.warning(f"Session {session_id} is already active")
            return False
        
        with self.session_locks[session_id]:
            # Check for exclusive channel ownership
            current_owner = self.get_session_owner(session.channel_id)
            if current_owner and current_owner != session_id:
                logger.error(f"Cannot start session {session_id}: Channel {session.channel_id} is already owned by active session {current_owner}")
                return False
            
            # Claim ownership
            self.channel_ownership[session.channel_id] = session_id
            
            session.is_active = True
            session.last_sync_time = 0.0  # Reset sync time
            
            # Discover streams
            self._discover_streams(session_id)
            
            # Reset metrics and status for all streams to ensure fresh calculation
            # from the new start time
            current_time = time.time()
            for stream_id, stream_info in session.streams.items():
                if not stream_info.is_quarantined:
                    # Reset non-quarantined streams to 'review' state
                    stream_info.status = 'review'
                    stream_info.last_status_change = current_time
                    stream_info.reliability_score = 50.0  # Reset score
                    stream_info.metrics_history = []  # Clear history
                    stream_info.failure_count = 0
                    stream_info.low_speed_start_time = None
                    
                    # Reset/Create scoring window
                    if session_id not in self.scoring_windows:
                        self.scoring_windows[session_id] = {}
                    
                    self.scoring_windows[session_id][stream_id] = CappedSlidingWindow(
                        window_size=session.window_size
                    )
            
            self._save_sessions()
        
        logger.info(f"Started session {session_id}")
        return True
    
    def stop_session(self, session_id: str) -> bool:
        """
        Stop monitoring for a session.
        
        Args:
            session_id: Session to stop
            
        Returns:
            True if stopped successfully
        """
        if session_id not in self.sessions:
            logger.error(f"Session {session_id} not found")
            return False
        
        session = self.sessions[session_id]
        
        with self.session_locks[session_id]:
            session.is_active = False
            # Release ownership
            if self.channel_ownership.get(session.channel_id) == session_id:
                del self.channel_ownership[session.channel_id]
                logger.debug(f"Released ownership of channel {session.channel_id} from session {session_id}")
            
            self._save_sessions()
        
        # Explicitly stop all FFmpeg monitors for this session
        # Import here to avoid circular dependency between session_manager and monitoring_service
        try:
            from apps.stream.stream_monitoring_service import get_monitoring_service
            monitoring_service = get_monitoring_service()
            monitoring_service.stop_session_monitors(session_id)
        except Exception as e:
            logger.error(f"Error stopping monitors for session {session_id}: {e}")
        
        logger.info(f"Stopped session {session_id}")
        return True
    
    def _discover_streams(self, session_id: str):
        """
        Discover streams matching the session's regex filter.
        
        Args:
            session_id: Session ID
        """
        session = self.sessions[session_id]
        udi = get_udi_manager()
        
        # Get all streams
        all_streams = udi.get_streams()
        
        regex = None
        if session.regex_filter:
            # Filter by regex with safety measures
            try:
                # Validate regex complexity (basic check)
                regex_pattern = session.regex_filter
                if len(regex_pattern) > 500:
                    logger.error(f"Regex pattern too long ({len(regex_pattern)} chars), max 500")
                    return
                
                # Substitute CHANNEL_NAME variable if present (consistent with channel matcher)
                channel_name = session.channel_name or f"Channel {session.channel_id}"
                escaped_channel_name = re.escape(channel_name)
                regex_pattern = regex_pattern.replace('CHANNEL_NAME', escaped_channel_name)
                
                # Convert literal spaces in pattern to flexible whitespace regex (\s+)
                # This matches behavior in RegexChannelMatcher
                regex_pattern = _WHITESPACE_PATTERN.sub(r'\\s+', regex_pattern)
                
                # Compile with timeout protection
                regex = re.compile(regex_pattern, re.IGNORECASE)
                
                # Test regex with empty string to catch catastrophic backtracking
                try:
                    regex.search('')
                except Exception as e:
                    logger.error(f"Regex pattern failed safety test: {e}")
                    return
                    
            except re.error as e:
                logger.error(f"Invalid regex filter: {e}")
                return
            except Exception as e:
                logger.error(f"Unexpected error compiling regex: {e}")
                return
        
        # Find matching streams
        matching_streams = []
        
        # Pre-calculate session TVG-ID if needed
        session_tvg_id = session.channel_tvg_id
        
        for s in all_streams:
            try:
                matched = False
                
                # Check 1: TVG-ID Match
                if session.match_by_tvg_id and session_tvg_id:
                    stream_tvg_id = s.get('tvg_id')
                    if stream_tvg_id == session_tvg_id:
                        matched = True
                
                # Check 2: Regex Match (if not already matched by TVG-ID or if we want cumulative)
                # Currently implementing OR logic: if matches TVG-ID OR Regex, include it.
                if not matched and regex:
                    # Use search with a reasonable timeout approach
                    # Python regex doesn't have direct timeout, but we can limit input size
                    stream_name = s.get('name', '')[:1000]  # Limit input size
                    if regex.search(stream_name):
                        matched = True
                
                if matched:
                    matching_streams.append(s)
                    
            except Exception as e:
                logger.warning(f"Error matching stream {s.get('id')}: {e}")
                continue
        
        # Filter out streams that are currently quarantined in this session
        # This prevents re-adding streams that were manually or automatically removed
        quarantined_ids = set(session.quarantined_stream_ids)
        if quarantined_ids:
            original_count = len(matching_streams)
            matching_streams = [s for s in matching_streams if s.get('id') not in quarantined_ids]
            filtered_count = original_count - len(matching_streams)
            if filtered_count > 0:
                logger.debug(f"Session {session_id}: Filtered out {filtered_count} quarantined streams from discovery")

        # logger.info(f"Session {session_id}: Found {len(matching_streams)} matching streams (after quarantine filtering)")
        
        # NOTE: We no longer add streams to Dispatcharr immediately upon discovery.
        # Streams remain in 'Review' status until validated by the monitor.
        # Check _evaluate_session_streams in stream_monitoring_service.py for the logic
        # that promotes 'Stable' streams to Dispatcharr.
        
        # Import stream stats utilities for extracting bitrate and other metadata
        from apps.core.stream_stats_utils import extract_stream_stats
        
        # Add streams to session
        for stream_data in matching_streams:
            stream_id = stream_data.get('id')
            if stream_id not in session.streams:
                # Extract stream stats (including bitrate from stream_stats)
                stats = extract_stream_stats(stream_data)
                
                # Extract bitrate safely, handling various data types
                bitrate = None
                if stats.get('bitrate_kbps'):
                    try:
                        bitrate = int(stats.get('bitrate_kbps'))
                    except (ValueError, TypeError):
                        bitrate = stream_data.get('bitrate')
                else:
                    bitrate = stream_data.get('bitrate')
                
                stream_info = StreamInfo(
                    stream_id=stream_id,
                    url=stream_data.get('url', ''),
                    name=stream_data.get('name', ''),
                    channel_id=session.channel_id,
                    width=stream_data.get('width'),
                    height=stream_data.get('height'),
                    fps=stats.get('fps') or stream_data.get('fps'),
                    bitrate=bitrate,
                    m3u_account=stream_data.get('m3u_account'),
                    hdr_format=stats.get('hdr_format') or stream_data.get('hdr_format'),
                    status='review',
                    last_status_change=time.time()
                )
                session.streams[stream_id] = stream_info
                
                # Initialize scoring window
                if session_id not in self.scoring_windows:
                    self.scoring_windows[session_id] = {}
                self.scoring_windows[session_id][stream_id] = CappedSlidingWindow(
                    window_size=session.window_size
                )
    
    def get_session(self, session_id: str) -> Optional[SessionInfo]:
        """Get session by ID"""
        return self.sessions.get(session_id)
    
    def get_all_sessions(self) -> List[SessionInfo]:
        """Get all sessions"""
        return list(self.sessions.values())
    
    def get_active_sessions(self) -> List[SessionInfo]:
        """Get all active sessions"""
        return [s for s in self.sessions.values() if s.is_active]
    
    def is_channel_in_active_session(self, channel_id: int) -> bool:
        """Check if a channel is currently in an active monitoring session.
        
        Args:
            channel_id: Channel ID to check
            
        Returns:
            True if channel is in an active session
        """
        for session in self.sessions.values():
            if session.is_active and session.channel_id == channel_id:
                return True
        return False
    
    def get_channels_in_active_sessions(self) -> Set[int]:
        """Get set of channel IDs that are currently in active monitoring sessions.
        
        Returns:
            Set of channel IDs in active sessions
        """
        channels = set()
        for session in self.sessions.values():
            if session.is_active:
                channels.add(session.channel_id)
        return channels
    
    def get_session_owner(self, channel_id: int) -> Optional[str]:
        """Get the session ID that currently owns the channel.
        
        Args:
            channel_id: Channel ID to check
            
        Returns:
            Session ID if owned, None otherwise
        """
        return self.channel_ownership.get(channel_id)
    
    def add_stream_to_session(self, session_id: str, stream_id: int) -> bool:
        """Add a newly discovered stream to an active session.
        
        This is called when automation discovers new streams for a channel
        that's currently in a monitoring session.
        
        Args:
            session_id: Session ID
            stream_id: Stream ID to add
            
        Returns:
            True if stream was added successfully
        """
        if session_id not in self.sessions:
            logger.error(f"Session {session_id} not found")
            return False
        
        session = self.sessions[session_id]
        if not session.is_active:
            logger.warning(f"Session {session_id} is not active")
            return False
        
        # Check if stream already exists
        if stream_id in session.streams:
            logger.debug(f"Stream {stream_id} already in session {session_id}")
            return True
        
        # Check if stream is quarantined in this session
        if stream_id in session.quarantined_stream_ids:
            logger.debug(f"Stream {stream_id} is quarantined in session {session_id} - skipping add")
            return False
    
        # Get stream info from UDI
        udi = get_udi_manager()
        stream = udi.get_stream_by_id(stream_id)
        if not stream:
            logger.error(f"Stream {stream_id} not found in UDI")
            return False
        
        # Check if stream matches session's regex filter
        import re
        try:
            # Prepare regex with variable substitution (same as _discover_streams)
            regex_pattern = session.regex_filter or '.*'
            
            # Substitute CHANNEL_NAME variable
            channel_name = session.channel_name or f"Channel {session.channel_id}"
            escaped_channel_name = re.escape(channel_name)
            regex_pattern = regex_pattern.replace('CHANNEL_NAME', escaped_channel_name)
            
            # Convert literal spaces
            regex_pattern = _WHITESPACE_PATTERN.sub(r'\\s+', regex_pattern)
            
            pattern = re.compile(regex_pattern, re.IGNORECASE)
            
            # Limit input size for safety
            stream_name = stream.get('name', '')[:1000]
            if not pattern.search(stream_name):
                logger.debug(f"Stream {stream_id} does not match session regex filter")
                return False
        except re.error:
            logger.warning(f"Invalid regex filter in session {session_id}")
        
        # Add stream to session
        with self.session_locks[session_id]:
            stream_info = StreamInfo(
                stream_id=stream_id,
                url=stream.get('url', ''),
                name=stream.get('name', ''),
                channel_id=session.channel_id,
                m3u_account=stream.get('m3u_account'),
                hdr_format=stream.get('stream_stats', {}).get('hdr_format') if isinstance(stream.get('stream_stats'), dict) else None,
                status='review',
                last_status_change=time.time()
            )
            session.streams[stream_id] = stream_info
            self.scoring_windows[session_id][stream_id] = CappedSlidingWindow(session.window_size)
            
            logger.info(f"Added stream {stream_id} to session {session_id}")
            self._save_sessions()
            
            # Also add stream to Dispatcharr channel for real-time sync
            try:
                from apps.core.api_utils import add_streams_to_channel
                added_count = add_streams_to_channel(session.channel_id, [stream_id], allow_dead_streams=True)
                if added_count > 0:
                    logger.info(
                        f"✓ Successfully added stream {stream_id} to Dispatcharr "
                        f"channel {session.channel_id} (real-time sync)"
                    )
                    # Refresh the channel in UDI to update cache
                    udi.refresh_channel_by_id(session.channel_id)
                else:
                    logger.debug(
                        f"Stream {stream_id} already in Dispatcharr channel {session.channel_id} "
                        f"or filtered out (possibly dead stream)"
                    )
            except Exception as e:
                logger.error(
                    f"Failed to add stream {stream_id} to Dispatcharr channel {session.channel_id}: {e}",
                    exc_info=True
                )
                # Don't fail the entire operation if Dispatcharr sync fails
            
            return True
    
    def quarantine_stream(self, session_id: str, stream_id: int, remove_from_dispatcharr: bool = True, reason: str = "manual") -> bool:
        """
        Manually quarantine a stream and optionally remove it from Dispatcharr.
        
        Args:
            session_id: Session ID
            stream_id: Stream ID to quarantine
            remove_from_dispatcharr: If True, also removes the stream from Dispatcharr channel
            
        Returns:
            True if quarantined successfully
        """
        if session_id not in self.sessions:
            logger.error(f"Session {session_id} not found")
            return False
        
        session = self.sessions[session_id]
        with self.session_locks[session_id]:
            stream_info = session.streams.get(stream_id)
            
            if not stream_info:
                logger.error(f"Stream {stream_id} not found in session {session_id}")
                return False
            if stream_info.status != 'quarantined':
                # Update status to quarantined
                stream_info.status = 'quarantined'
                stream_info.status_reason = reason
                stream_info.last_status_change = time.time()
                
                # Add to persistent blocklist
                if session.quarantined_stream_ids is None:
                    session.quarantined_stream_ids = set()
                session.quarantined_stream_ids.add(stream_id)
                
                self._save_sessions()
        
        log_msg = f"Quarantined stream {stream_id} in session {session_id}"
        if reason == "manual":
            logger.info(f"Manually {log_msg.lower()}")
        else:
            logger.info(f"Automatically {log_msg.lower()} (Reason: {reason})")
        
        # Remove from Dispatcharr channel if requested
        if remove_from_dispatcharr:
            try:
                from apps.udi import get_udi_manager
                udi = get_udi_manager()
                
                # Mark as dead in tracker first
                try:
                    from apps.stream.dead_streams_tracker import DeadStreamsTracker
                    tracker = DeadStreamsTracker()
                    tracker.mark_as_dead(
                        stream_url=stream_info.url,
                        stream_id=stream_id,
                        stream_name=stream_info.name,
                        channel_id=session.channel_id
                    )
                    logger.debug(f"Marked quarantined stream {stream_id} as dead in tracker")
                except Exception as e:
                    logger.warning(f"Failed to mark stream {stream_id} as dead: {e}")

                # Remove from channel (but not delete from UDI)
                channel = udi.get_channel_by_id(session.channel_id)
                if channel:
                    current_streams = channel.get('streams', [])
                    if stream_id in current_streams:
                        # Create new list without the quarantined stream
                        new_streams = [sid for sid in current_streams if sid != stream_id]
                        
                        # Update channel with new stream list
                        # use api_utils to ensure proper update
                        from apps.core.api_utils import update_channel_streams
                        success = update_channel_streams(session.channel_id, new_streams)
                        
                        if success:
                            logger.debug(f"Removed quarantined stream {stream_id} from Dispatcharr channel {session.channel_id}")
                            # Refresh channel in UDI
                            udi.refresh_channel_by_id(session.channel_id)
                        else:
                            logger.warning(f"Failed to update channel {session.channel_id} to remove stream {stream_id}")
                    else:
                        logger.info(f"Stream {stream_id} was not in channel {session.channel_id} streams list")
                else:
                    logger.warning(f"Channel {session.channel_id} not found, could not remove stream {stream_id}")

            except Exception as e:
                logger.error(f"Error handling Dispatcharr updates for quarantined stream {stream_id}: {e}", exc_info=True)
         
        return True



    def move_stream_to_review(self, session_id: str, stream_id: int, reason: str = "manual") -> bool:
        """
        Moves a stream back to 'review' status (e.g., when it is detected looping while stable).
        
        Args:
            session_id: Session ID
            stream_id: Stream ID to move to review
            reason: The reason for moving to review
            
        Returns:
            True if moved successfully
        """
        if session_id not in self.sessions:
            logger.error(f"Session {session_id} not found")
            return False
            
        session = self.sessions[session_id]
        with self.session_locks[session_id]:
            stream_info = session.streams.get(stream_id)
            
            if not stream_info:
                logger.error(f"Stream {stream_id} not found in session {session_id}")
                return False
                
            if stream_info.status == "quarantined":
                logger.debug(f"Stream {stream_id} is quarantined; not moving to review.")
                return False
                
            if stream_info.status != "review":
                stream_info.status = "review"
                stream_info.status_reason = reason
                stream_info.last_status_change = time.time()
                logger.debug(f"Stream {stream_id} moved to Review. Reason: {reason}")
                self._save_sessions()
                return True
            
        return False

    def revive_stream(self, session_id: str, stream_id: int) -> bool:
        """
        Revive a quarantined stream by moving it to 'review' status.
        
        Args:
            session_id: Session ID
            stream_id: Stream ID
            
        Returns:
            True if successful
        """
        if session_id not in self.sessions:
            return False
            
        session = self.sessions[session_id]
        with self.session_locks[session_id]:
            stream_info = session.streams.get(stream_id)
            
            if not stream_info:
                return False
            if stream_info.status == 'quarantined':
                stream_info.status = 'review'
                stream_info.last_status_change = time.time()
                
                # Reset counters for fresh start
                stream_info.low_speed_start_time = None
                stream_info.failure_count = 0
                stream_info.consecutive_logo_misses = 0
                stream_info.loop_duration = None
                stream_info.status_reason = None
                
                # Remove from persistent blocklist
                if session.quarantined_stream_ids and stream_id in session.quarantined_stream_ids:
                    session.quarantined_stream_ids.remove(stream_id)
                
                self._save_sessions()
                logger.info(f"Revived stream {stream_id} in session {session_id} (moved to review)")
                
                # Add back to Dispatcharr channel
                try:
                    from apps.core.api_utils import add_streams_to_channel
                    add_streams_to_channel(session.channel_id, [stream_id], allow_dead_streams=True)
                    # Refresh UDI to reflect change
                    udi = get_udi_manager()
                    udi.refresh_channel_by_id(session.channel_id)
                except Exception as e:
                    logger.error(f"Failed to restore stream {stream_id} to Dispatcharr: {e}")
                
                return True
            else:
                logger.debug(f"Stream {stream_id} is not quarantined (status: {stream_info.status})")
                return False
    
    def delete_session(self, session_id: str) -> bool:
        """Delete a session"""
        if session_id not in self.sessions:
            return False
        
        # Stop if active, but ALWAYS try to stop monitors to ensure cleanup
        if self.sessions[session_id].is_active:
            self.stop_session(session_id)
        else:
            # Even if marked inactive, ensure monitors are stopped
            try:
                from apps.stream.stream_monitoring_service import get_monitoring_service
                monitoring_service = get_monitoring_service()
                monitoring_service.stop_session_monitors(session_id)
            except Exception as e:
                logger.error(f"Error checking monitors for deleted session {session_id}: {e}")
        
        # Strip all physical screenshot files associated with this session before removal
        # Only delete if no other active session is currently monitoring the same stream
        def is_stream_actively_monitored(sid: int) -> bool:
            for other_sid, other_session in self.sessions.items():
                if other_sid != session_id and other_session.is_active and sid in other_session.streams:
                    return True
            return False

        try:
            from apps.stream.stream_screenshot_service import get_screenshot_service
            screenshot_service = get_screenshot_service()
            for stream_id in self.sessions[session_id].streams.keys():
                if not is_stream_actively_monitored(stream_id):
                    screenshot_service.delete_screenshot(stream_id)
        except Exception as e:
            logger.error(f"Error cleaning up screenshots for deleted session {session_id}: {e}")
        
        # Remove session
        del self.sessions[session_id]
        if session_id in self.session_locks:
            del self.session_locks[session_id]
        if session_id in self.scoring_windows:
            del self.scoring_windows[session_id]
        
        self._save_sessions()
        logger.info(f"Deleted session {session_id}")
        return True


# Global instance accessor
_manager_instance = None
_manager_lock = threading.Lock()


def get_session_manager() -> StreamSessionManager:
    """Get the global StreamSessionManager instance"""
    global _manager_instance
    if _manager_instance is None:
        with _manager_lock:
            if _manager_instance is None:
                _manager_instance = StreamSessionManager()
    return _manager_instance
