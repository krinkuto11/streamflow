#!/usr/bin/env python3
"""
Web API Server for StreamFlow for Dispatcharr

Provides REST API endpoints for the React frontend to interact with
the automated stream management system.
"""

import json
import logging
import os
import queue
import re

def is_dangerous_regex(pattern: str) -> bool:
    """Return True if the regex pattern contains nested quantifiers (ReDoS risk)."""
    inside_parens = False
    has_inner_quantifier = False
    for i, char in enumerate(pattern):
        if i > 0 and pattern[i-1] == '\\':
            continue
        if char == '(':
            inside_parens = True
            has_inner_quantifier = False
        elif char == ')':
            if inside_parens and has_inner_quantifier:
                if i + 1 < len(pattern) and pattern[i+1] in '+*':
                    return True
            inside_parens = False
        elif inside_parens and char in '+*':
            has_inner_quantifier = True
    return False
import requests
import socket
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, Optional, Tuple
from dataclasses import asdict
from flask import Flask, request, jsonify, send_from_directory, send_file, make_response, Response
from flask_cors import CORS
from werkzeug.utils import secure_filename

from apps.automation.automated_stream_manager import AutomatedStreamManager, RegexChannelMatcher
from apps.core.api_utils import _get_base_url
from apps.stream.stream_checker_service import get_stream_checker_service
from apps.automation.scheduling_service import get_scheduling_service

from apps.config.dispatcharr_config import get_dispatcharr_config
from apps.config.acestream_orchestrator_config import get_acestream_orchestrator_config
from apps.channels.channel_order_manager import get_channel_order_manager
from apps.automation.automation_config_manager import get_automation_config_manager

# Pre-compiled regex pattern for whitespace conversion (performance optimization)
# This pattern matches one or more spaces that are NOT preceded by a backslash
# Used to convert literal spaces to flexible whitespace while preserving escaped spaces
_WHITESPACE_PATTERN = re.compile(r'(?<!\\) +')

# Import UDI for direct data access
from apps.udi import get_udi_manager

# Import centralized stream stats utilities
from apps.core.stream_stats_utils import (
    extract_stream_stats,
    format_bitrate,
    parse_bitrate_value,
    calculate_channel_averages
)

# Import croniter for cron expression validation
try:
    from croniter import croniter
    CRONITER_AVAILABLE = True
except ImportError:
    CRONITER_AVAILABLE = False



# Setup centralized logging
from apps.core.logging_config import setup_logging, log_function_call, log_function_return, log_exception

logger = setup_logging(__name__)

# Configuration constants
CONFIG_DIR = Path(os.environ.get('CONFIG_DIR', '/app/data'))
CONCURRENT_STREAMS_GLOBAL_LIMIT_KEY = 'concurrent_streams.global_limit'
CONCURRENT_STREAMS_ENABLED_KEY = 'concurrent_streams.enabled'

# Dead streams pagination constants
DEAD_STREAMS_DEFAULT_PER_PAGE = 20
DEAD_STREAMS_MAX_PER_PAGE = 100

# EPG refresh processor constants
EPG_REFRESH_INITIAL_DELAY_SECONDS = 5  # Delay before first EPG refresh
EPG_REFRESH_ERROR_RETRY_SECONDS = 300  # Retry interval after errors (5 minutes)
THREAD_SHUTDOWN_TIMEOUT_SECONDS = 5  # Timeout for graceful thread shutdown
# HLS Constants
# Constants

# Initialize Flask app with static file serving
# Note: static_folder set to None to disable Flask's built-in static route
# The catch-all route will handle serving all static files from the React build
# Try Docker/Production path: up 3 levels then /static
static_folder_docker = Path(__file__).parent.parent.parent / 'static'
static_folder_local = Path(__file__).parent.parent.parent.parent / 'frontend' / 'build'

static_folder = static_folder_docker if static_folder_docker.exists() else static_folder_local
app = Flask(__name__, static_folder=None)
CORS(app)  # Enable CORS for React frontend


def _parse_pagination_params(
    page_param: Optional[str],
    per_page_param: str,
    default_per_page: int = 50,
    max_per_page: int = 500,
) -> Tuple[Optional[int], int, Optional['Response']]:
    """Parse and validate pagination query parameters.

    Returns a ``(page, per_page, error_response)`` triple.  When the
    parameters are valid *error_response* is ``None``; when they are invalid
    a Flask ``Response`` object with status 400 is returned instead and
    ``page`` / ``per_page`` are undefined.
    """
    page: Optional[int] = None
    if page_param is not None:
        try:
            page = max(1, int(page_param))
        except (ValueError, TypeError) as exc:
            logger.warning(
                f"Invalid 'page' query parameter {page_param!r}: {exc}"
            )
            return None, default_per_page, (
                jsonify({"error": "Invalid page parameter: must be an integer"}), 400
            )

    try:
        per_page = int(per_page_param)
    except (ValueError, TypeError):
        per_page = default_per_page
    per_page = min(max(per_page, 1), max_per_page)

    return page, per_page, None

# Global instances
automation_manager = None
regex_matcher = None
scheduled_event_processor_thread = None
scheduled_event_processor_running = False
scheduled_event_processor_wake = None  # threading.Event to wake up the processor early
epg_refresh_thread = None
epg_refresh_running = False
epg_refresh_wake = None  # threading.Event to wake up the refresh early

class StreamProxy:
    """Manages a single UDP listener for a stream and broadcasts to multiple HTTP clients."""
    def __init__(self, stream_id, manager):
        self.stream_id = stream_id
        self.manager = manager
        self.port = 30000 + stream_id
        self.clients = {} # dict of queue.Queue -> bool (needs_resync)
        self.lock = threading.Lock()
        self.running = False
        self.thread = None
        self.last_client_time = None
        self.LINGER_SECONDS = 60
        self.last_drop_log_time = 0

    def add_client(self):
        # Buffer for about 2-4 seconds of stream (2000 packets)
        q = queue.Queue(maxsize=2000)
        with self.lock:
            # Always start in resync mode so the client waits for the first
            # valid 0x47 MPEG-TS sync byte. This prevents TSDemuxer errors
            # when connecting mid-packet or receiving a partially-filled
            # UDP datagram from FFmpeg's fifo muxer (which pads with 0xFF).
            self.clients[q] = True  # needs_resync = True
            if not self.running:
                self._start()
        return q

    def remove_client(self, q):
        with self.lock:
            if q in self.clients:
                del self.clients[q]
            if not self.clients:
                self.last_client_time = datetime.now()
                logger.info(f"Last client disconnected from stream {self.stream_id}. Entering {self.LINGER_SECONDS}s linger period.")

    def _start(self):
        self.running = True
        self.thread = threading.Thread(
            target=self._run, 
            daemon=True, 
            name=f"ProxyUDP-{self.stream_id}"
        )
        self.thread.start()
        logger.info(f"Started shared UDP listener for stream {self.stream_id} on port {self.port}")

    def _stop(self):
        self.running = False
        logger.info(f"Stopped shared UDP listener for stream {self.stream_id}")

    def _run(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        # Allow immediate reuse of the port
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            # Bind to all interfaces or localhost? FFmpeg sends to 127.0.0.1
            sock.bind(('127.0.0.1', self.port))
            sock.settimeout(1.0) # Short timeout to allow periodically checking for lingering cleanup
            residual = b'' # Helper for aligning MPEG-TS packets (188 bytes)
            while self.running:
                try:
                    # Short timeout to allow periodically checking for lingering cleanup
                    data, _ = sock.recvfrom(65535)
                except socket.timeout:
                    # Check if we should shut down due to lingering timeout
                    with self.lock:
                        if not self.clients and self.last_client_time:
                            elapsed = (datetime.now() - self.last_client_time).total_seconds()
                            if elapsed >= self.LINGER_SECONDS:
                                logger.info(f"Linger timeout reached for stream {self.stream_id}. Shutting down listener.")
                                self.running = False
                                self.manager.remove_proxy(self.stream_id)
                                break
                    continue

                if data:
                    data = residual + data
                    excess = len(data) % 188
                    if excess > 0:
                        residual = data[-excess:]
                        data = data[:-excess]
                    else:
                        residual = b''
                    
                    if not data:
                        continue

                    with self.lock:
                        if not self.clients:
                            # Discard data while lingering, but check for timeout
                            if self.last_client_time:
                                elapsed = (datetime.now() - self.last_client_time).total_seconds()
                                if elapsed >= self.LINGER_SECONDS:
                                    logger.info(f"Linger timeout reached during data recv for stream {self.stream_id}. Shutting down.")
                                    self.running = False
                                    self.manager.remove_proxy(self.stream_id)
                                    break
                            continue
                        
                        # We have clients, so we are active - clear linger timestamp
                        self.last_client_time = None
                        
                        for q, needs_resync in list(self.clients.items()):
                            try:
                                # If client previously dropped data, wait for next TS sync byte (0x47)
                                if needs_resync:
                                    if data and data[0] == 0x47:
                                        self.clients[q] = False
                                        # Proceed to put data
                                    else:
                                        continue # Still searching for sync
                                
                                q.put_nowait(data)
                            except queue.Full:
                                # Mark for resync to avoid pushing malformed fragments
                                self.clients[q] = True
                                
                                # Optimization: Clear the entire queue to "jump to live"
                                # This prevents the client from being permanently lagged
                                try:
                                    while not q.empty():
                                        q.get_nowait()
                                except queue.Empty:
                                    pass

                                # Log packet drops at most once every 5 seconds per stream
                                now = time.time()
                                if now - self.last_drop_log_time > 5:
                                    logger.warning(f"Shared proxy for {self.stream_id} buffer full - clearing queue (Jump-to-Live). Client processing too slow.")
                                    self.last_drop_log_time = now
                                pass # Data dropped for this specific slow client
        except Exception as e:
            logger.error(f"UDP listener error for stream {self.stream_id}: {e}")
        finally:
            sock.close()

class UDPProxyManager:
    """Singleton-like manager for all active UDP-to-HTTP proxies."""
    def __init__(self):
        self.proxies = {} # stream_id -> StreamProxy
        self.lock = threading.Lock()

    def get_proxy(self, stream_id):
        with self.lock:
            if stream_id not in self.proxies:
                self.proxies[stream_id] = StreamProxy(stream_id, self)
            return self.proxies[stream_id]

    def remove_proxy(self, stream_id):
        with self.lock:
            if stream_id in self.proxies:
                del self.proxies[stream_id]

# Initialize the global proxy manager
udp_proxy_manager = UDPProxyManager()

def get_automation_manager():
    """Get or create automation manager instance."""
    global automation_manager
    if automation_manager is None:
        automation_manager = AutomatedStreamManager()
    return automation_manager

def get_regex_matcher():
    """Get or create regex matcher instance."""
    global regex_matcher
    if regex_matcher is None:
        regex_matcher = RegexChannelMatcher()
    return regex_matcher

def check_wizard_complete():
    """Check if the setup wizard has been completed.
    
    Note: As of recent changes, regex patterns are now optional and not required
    for wizard completion. This allows users to complete the wizard and start
    using the system even if they haven't configured any channel patterns yet.
    """
    try:
        # SQL-backed readiness: require Dispatcharr credentials to be configured.
        dispatcharr_config = get_dispatcharr_config()
        return dispatcharr_config.is_configured()
    except Exception as e:
        logger.warning(f"Error checking wizard completion status: {e}")
        return False


def scheduled_event_processor():
    """Background thread to process scheduled EPG events.
    
    This function runs in a separate thread and checks for due scheduled events
    periodically. When events are due, it executes the channel checks and
    automatically deletes the completed events.
    
    Uses an event-based approach similar to the global action scheduler for
    better responsiveness. Checks every 30 seconds but can be woken early.
    """
    global scheduled_event_processor_running, scheduled_event_processor_wake
    
    logger.info("Scheduled event processor thread started")
    
    # Check interval in seconds - more frequent than the old 60s for better responsiveness
    check_interval = 30
    
    while scheduled_event_processor_running:
        try:
            # Wait for wake event or timeout (similar to _scheduler_loop pattern)
            # This allows the processor to be woken up early if needed
            if scheduled_event_processor_wake is None:
                # This should not happen during normal operation
                logger.error("Wake event is None! This indicates a programming error. Using fallback sleep.")
                time.sleep(check_interval)
            else:
                scheduled_event_processor_wake.wait(timeout=check_interval)
                scheduled_event_processor_wake.clear()
            
            # Check for due events
            service = get_scheduling_service()
            stream_checker = get_stream_checker_service()
            
            # Get all due events
            due_events = service.get_due_events()
            
            if due_events:
                logger.info(f"Found {len(due_events)} scheduled event(s) due for execution")
                
                for event in due_events:
                    event_id = event.get('id')
                    channel_name = event.get('channel_name', 'Unknown')
                    program_title = event.get('program_title', 'Unknown')
                    
                    logger.info(f"Executing scheduled event {event_id} for {channel_name} (program: {program_title})")
                    
                    try:
                        success = service.execute_scheduled_check(event_id, stream_checker)
                        if success:
                            logger.info(f"✓ Successfully executed and removed scheduled event {event_id}")
                        else:
                            logger.warning(f"✗ Failed to execute scheduled event {event_id}")
                    except Exception as e:
                        logger.error(f"Error executing scheduled event {event_id}: {e}", exc_info=True)
            
        except Exception as e:
            logger.error(f"Error in scheduled event processor: {e}", exc_info=True)
    
    logger.info("Scheduled event processor thread stopped")


def start_scheduled_event_processor():
    """Start the background thread for processing scheduled events."""
    global scheduled_event_processor_thread, scheduled_event_processor_running, scheduled_event_processor_wake
    
    if scheduled_event_processor_thread is not None and scheduled_event_processor_thread.is_alive():
        logger.warning("Scheduled event processor is already running")
        return False
    
    # Initialize wake event for responsive control
    scheduled_event_processor_wake = threading.Event()
    
    scheduled_event_processor_running = True
    scheduled_event_processor_thread = threading.Thread(
        target=scheduled_event_processor,
        name="ScheduledEventProcessor",
        daemon=True  # Daemon thread will exit when main program exits
    )
    scheduled_event_processor_thread.start()
    logger.info("Scheduled event processor started")
    return True


def stop_scheduled_event_processor():
    """Stop the background thread for processing scheduled events."""
    global scheduled_event_processor_thread, scheduled_event_processor_running, scheduled_event_processor_wake
    
    if scheduled_event_processor_thread is None or not scheduled_event_processor_thread.is_alive():
        logger.warning("Scheduled event processor is not running")
        return False
    
    logger.info("Stopping scheduled event processor...")
    scheduled_event_processor_running = False
    
    # Wake the thread so it can exit promptly
    if scheduled_event_processor_wake:
        scheduled_event_processor_wake.set()
    
    # Wait for thread to finish (with timeout)
    scheduled_event_processor_thread.join(timeout=5)
    
    if scheduled_event_processor_thread.is_alive():
        logger.warning("Scheduled event processor thread did not stop gracefully")
        return False
    
    logger.info("Scheduled event processor stopped")
    return True


def epg_refresh_processor():
    """Background thread to periodically refresh EPG data and match programs to auto-create rules.
    
    This function runs in a separate thread and periodically fetches EPG data from
    Dispatcharr, which automatically triggers matching of programs to auto-create rules.
    The interval is configured in the scheduling service config (epg_refresh_interval_minutes).
    """
    global epg_refresh_running, epg_refresh_wake
    
    logger.info("EPG refresh processor thread started")
    
    # Initial fetch with a small delay to allow service initialization
    time.sleep(EPG_REFRESH_INITIAL_DELAY_SECONDS)
    
    while epg_refresh_running:
        try:
            service = get_scheduling_service()
            config = service.get_config()
            
            # Get refresh interval from config (in minutes), with a minimum of 5 minutes
            refresh_interval_minutes = max(config.get('epg_refresh_interval_minutes', 60), 5)
            refresh_interval_seconds = refresh_interval_minutes * 60
            
            # Fetch EPG data (this will also trigger match_programs_to_rules)
            logger.info(f"Fetching EPG data and matching programs to auto-create rules...")
            result = service.match_programs_to_rules()
            logger.info(f"EPG refresh complete. Created {result.get('created', 0)} events.")
            
            # Wait for the next refresh interval or wake event
            if epg_refresh_wake is None:
                # This indicates a critical threading issue - the wake event should always be set
                logger.critical("EPG refresh wake event is None! This is a programming error. Stopping processor.")
                epg_refresh_running = False
                break
            
            logger.debug(f"EPG refresh will occur again in {refresh_interval_minutes} minutes")
            epg_refresh_wake.wait(timeout=refresh_interval_seconds)
            epg_refresh_wake.clear()
            
        except Exception as e:
            logger.error(f"Error in EPG refresh processor: {e}", exc_info=True)
            # On error, wait before retrying (using wake event for responsiveness)
            if epg_refresh_wake and epg_refresh_running:
                epg_refresh_wake.wait(timeout=EPG_REFRESH_ERROR_RETRY_SECONDS)
                epg_refresh_wake.clear()
            else:
                break  # Exit if wake event is invalid or processor is stopping
    
    logger.info("EPG refresh processor thread stopped")


def start_epg_refresh_processor():
    """Start the background thread for periodic EPG refresh."""
    global epg_refresh_thread, epg_refresh_running, epg_refresh_wake
    
    if epg_refresh_thread is not None and epg_refresh_thread.is_alive():
        logger.warning("EPG refresh processor is already running")
        return False
    
    # Initialize wake event
    epg_refresh_wake = threading.Event()
    
    epg_refresh_running = True
    epg_refresh_thread = threading.Thread(
        target=epg_refresh_processor,
        name="EPGRefreshProcessor",
        daemon=True
    )
    epg_refresh_thread.start()
    logger.info("EPG refresh processor started")
    return True


def stop_epg_refresh_processor():
    """Stop the background thread for EPG refresh."""
    global epg_refresh_thread, epg_refresh_running, epg_refresh_wake
    
    if epg_refresh_thread is None or not epg_refresh_thread.is_alive():
        logger.warning("EPG refresh processor is not running")
        return False
    
    logger.info("Stopping EPG refresh processor...")
    epg_refresh_running = False
    
    # Wake the thread so it can exit promptly
    if epg_refresh_wake:
        epg_refresh_wake.set()
    
    # Wait for thread to finish (with timeout)
    epg_refresh_thread.join(timeout=THREAD_SHUTDOWN_TIMEOUT_SECONDS)
    
    if epg_refresh_thread.is_alive():
        logger.warning("EPG refresh processor thread did not stop gracefully")
        return False
    
    logger.info("EPG refresh processor stopped")
    return True


@app.route('/', methods=['GET'])
def root():
    """Serve React frontend."""
    try:
        return send_file(static_folder / 'index.html')
    except FileNotFoundError:
        # Fallback to API info if frontend not built
        return jsonify({
            "message": "StreamFlow for Dispatcharr API",
            "version": "1.0",
            "endpoints": {
                "health": "/api/health",
                "docs": "/api/health",
                "frontend": "React frontend not found. Build frontend and place in static/ directory."
            }
        })

@app.route('/api/health', methods=['GET'])
def health_check():
    """Health check endpoint."""
    return jsonify({"status": "healthy", "timestamp": datetime.now().isoformat()})

@app.route('/health', methods=['GET'])
def health_check_stripped():
    """Health check endpoint for nginx proxy (stripped /api prefix)."""
    return health_check()

@app.route('/api/version', methods=['GET'])
def get_version():
    """Get application version."""
    try:
        version_file = Path(__file__).parent / 'version.txt'
        if version_file.exists():
            version = version_file.read_text().strip()
        else:
            version = "dev-unknown"
        return jsonify({"version": version})
    except Exception as e:
        logger.error(f"Failed to read version: {e}")
        return jsonify({"version": "dev-unknown"})


# In-memory cache for public IP — refreshed at most once every 15 minutes
_env_cache: Dict[str, Any] = {"public_ip": None, "fetched_at": 0.0}
_ENV_CACHE_TTL = 900  # seconds (15 minutes)

@app.route('/api/environment', methods=['GET'])
def get_environment():
    """Get environment info including public IP.

    The IP is resolved once per startup and then cached for up to
    _ENV_CACHE_TTL seconds to avoid hammering ipify on every page load.
    """
    now = time.time()
    if _env_cache["public_ip"] is None or (now - _env_cache["fetched_at"]) >= _ENV_CACHE_TTL:
        try:
            r = requests.get("https://api64.ipify.org?format=json", timeout=5)
            r.raise_for_status()
            _env_cache["public_ip"] = r.json().get("ip")
            _env_cache["fetched_at"] = now
        except requests.RequestException as e:
            logger.warning(f"Failed to fetch public IP: {e}")
            # Leave cached value (if any) in place; don't reset fetched_at so
            # a transient failure doesn't cause a retry storm.

    return jsonify({
        "public_ip": _env_cache["public_ip"],
        "country_code": None,
        "country_name": None,
    })

# Legacy automation endpoints removed. Using newer implementations in 'Automation Service API' section.

@app.route('/api/channels', methods=['GET'])
def get_channels():
    """Get all channels from UDI with custom ordering applied.

    Optional query parameters:
      - page (int): page number for pagination (1-based). If omitted the full list is returned.
      - per_page (int): items per page (default 50, max 500).
      - search (str): filter channels whose name contains this string (case-insensitive).
      - sort_by (str): field to sort by – 'name' (default), 'channel_number', or 'id'.
      - sort_dir (str): 'asc' (default) or 'desc'.
    """
    try:
        search = request.args.get('search', '').strip()
        sort_by = request.args.get('sort_by', 'name')
        sort_dir = request.args.get('sort_dir', 'asc')
        page_param = request.args.get('page', None)
        per_page_param = request.args.get('per_page', '50')

        page, per_page, err = _parse_pagination_params(page_param, per_page_param)
        if err:
            return err

        if sort_dir not in ('asc', 'desc'):
            sort_dir = 'asc'

        udi = get_udi_manager()
        channels = udi.get_channels()

        if channels is None:
            return jsonify({"error": "Failed to fetch channels"}), 500

        # Apply search filter (case-insensitive)
        if search:
            search_lower = search.lower()
            channels = [ch for ch in channels if search_lower in ch.get('name', '').lower()]

        # Apply sorting
        _VALID_SORT_COLS = {'name', 'channel_number', 'id'}
        if sort_by not in _VALID_SORT_COLS:
            sort_by = 'name'
        reverse = sort_dir == 'desc'
        channels = sorted(
            channels,
            key=lambda ch: (ch.get(sort_by) is None, ch.get(sort_by, '')),
            reverse=reverse,
        )

        # Inject automation profile assignments
        automation_config = get_automation_config_manager()
        channels_with_profiles = []
        for channel in channels:
            # Create a copy to avoid modifying the cached UDI object
            ch_copy = channel.copy()
            # Get effective profile (explicit assignment > group assignment > default)
            ch_copy['automation_profile_id'] = automation_config.get_effective_profile_id(
                ch_copy.get('id'), 
                ch_copy.get('channel_group_id')
            )
            # Also include explicit assignment for UI logic if needed
            ch_copy['assigned_profile_id'] = automation_config.get_channel_assignment(ch_copy.get('id'))

            # Get automation periods count (including group-inherited periods)
            periods = automation_config.get_effective_channel_periods(
                ch_copy.get('id'),
                ch_copy.get('channel_group_id')
            )
            ch_copy['automation_periods_count'] = len(periods)

            channels_with_profiles.append(ch_copy)

        # Apply custom channel order if configured (only for non-paginated responses)
        if page is None:
            order_manager = get_channel_order_manager()
            channels_with_profiles = order_manager.apply_order(channels_with_profiles)
            return jsonify(channels_with_profiles)

        # Paginated response
        total = len(channels_with_profiles)
        start = (page - 1) * per_page
        end = start + per_page
        page_items = channels_with_profiles[start:end]
        total_pages = max(1, (total + per_page - 1) // per_page)
        return jsonify({
            "items": page_items,
            "total": total,
            "page": page,
            "per_page": per_page,
            "total_pages": total_pages,
            "has_next": end < total,
            "has_prev": page > 1,
        })
    except Exception as e:
        logger.error(f"Error fetching channels: {e}")
        return jsonify({"error": "Internal Server Error"}), 500

@app.route('/api/channels/<channel_id>/stats', methods=['GET'])
def get_channel_stats(channel_id):
    """Get channel statistics including stream count, dead streams, resolution, and bitrate."""
    try:
        # Convert channel_id to int for comparison
        try:
            channel_id_int = int(channel_id)
        except (ValueError, TypeError):
            return jsonify({"error": "Invalid channel ID: must be a valid integer"}), 400
        
        udi = get_udi_manager()
        channels = udi.get_channels()
        
        if channels is None:
            return jsonify({"error": "Failed to fetch channels"}), 500
        
        # Find the specific channel - convert to dict for O(1) lookup
        # Filter out any invalid channel objects and build lookup dict
        channels_dict = {ch['id']: ch for ch in channels if isinstance(ch, dict) and 'id' in ch}
        channel = channels_dict.get(channel_id_int)
        
        if not channel:
            return jsonify({"error": "Channel not found"}), 404
        
        # Get streams for this channel
        # channel['streams'] is a list of stream IDs, need to fetch full stream objects
        stream_ids = channel.get('streams', [])
        total_streams = len(stream_ids)
        
        # Fetch full stream objects for each stream ID
        streams = []
        for stream_id in stream_ids:
            if isinstance(stream_id, int):
                stream = udi.get_stream_by_id(stream_id)
                if stream:
                    streams.append(stream)
        
        # Get dead streams count for this channel from the tracker
        # The tracker now stores channel_id for each dead stream, so we can directly count them
        dead_count = 0
        checker = get_stream_checker_service()
        if checker and checker.dead_streams_tracker:
            dead_count = checker.dead_streams_tracker.get_dead_streams_count_for_channel(channel_id_int)
            logger.debug(f"Channel {channel_id_int} has {dead_count} dead stream(s) in tracker")
        else:
            logger.warning(f"Dead streams tracker not available for channel {channel_id_int}")
        
        # Calculate resolution and bitrate statistics using centralized utility
        # This ensures consistent handling across the application
        channel_averages = calculate_channel_averages(streams, dead_stream_ids=set())
        
        # Extract most common resolution
        most_common_resolution = channel_averages.get('avg_resolution', 'Unknown')
        
        # Extract and parse average bitrate
        # The calculate_channel_averages returns formatted string (e.g., "5000 kbps")
        # We need the numeric value for backward compatibility with existing UI
        # TODO: Consider removing this conversion in v2.0 and have UI handle formatted strings
        avg_bitrate_str = channel_averages.get('avg_bitrate', 'N/A')
        avg_bitrate = 0
        if avg_bitrate_str != 'N/A':
            parsed_bitrate = parse_bitrate_value(avg_bitrate_str)
            if parsed_bitrate:
                avg_bitrate = int(parsed_bitrate)
        
        # Build resolutions dict for detailed breakdown (if needed by UI)
        resolutions = {}
        for stream in streams:
            stats = extract_stream_stats(stream)
            resolution = stats.get('resolution', 'Unknown')
            if resolution not in ['Unknown', 'N/A']:
                resolutions[resolution] = resolutions.get(resolution, 0) + 1
        
        return jsonify({
            "channel_id": channel_id_int,
            "channel_name": channel.get('name', ''),
            "logo_id": channel.get('logo_id'),
            "total_streams": total_streams,
            "dead_streams": dead_count,
            "most_common_resolution": most_common_resolution,
            "average_bitrate": avg_bitrate,
            "resolutions": resolutions
        })
    except Exception as e:
        logger.error(f"Error fetching channel stats: {e}")
        return jsonify({"error": "Internal Server Error"}), 500

@app.route('/api/channels/groups', methods=['GET'])
def get_channel_groups():
    """Get all channel groups from UDI."""
    try:
        udi = get_udi_manager()
        groups = udi.get_channel_groups()
        
        if groups is None:
            return jsonify({"error": "Failed to fetch channel groups"}), 500
        
        return jsonify(groups)
    except Exception as e:
        logger.error(f"Error fetching channel groups: {e}")
        return jsonify({"error": "Internal Server Error"}), 500

@app.route('/api/channels/logos/<logo_id>', methods=['GET'])
def get_channel_logo(logo_id):
    """Get channel logo from UDI."""
    try:
        udi = get_udi_manager()
        logo = udi.get_logo_by_id(int(logo_id))
        
        if logo is None:
            return jsonify({"error": "Failed to fetch logo"}), 500
        
        return jsonify(logo)
    except Exception as e:
        logger.error(f"Error fetching logo: {e}")
        return jsonify({"error": "Internal Server Error"}), 500

@app.route('/api/channels/logos/<logo_id>/cache', methods=['GET'])
def get_channel_logo_cached(logo_id):
    """Download and cache channel logo locally, then serve it.
    
    This endpoint:
    1. Checks if logo is already cached locally
    2. If not, downloads it from Dispatcharr
    3. Saves it to local storage
    4. Serves the cached file
    """
    try:
        # Validate logo_id is a positive integer
        try:
            logo_id_int = int(logo_id)
        except (ValueError, TypeError):
            return jsonify({"error": "Invalid logo ID: must be a valid integer"}), 400
        
        if logo_id_int <= 0:
            return jsonify({"error": "Invalid logo ID: must be a positive integer"}), 400
        
        # Create logos cache directory if it doesn't exist
        logos_cache_dir = CONFIG_DIR / 'logos_cache'
        logos_cache_dir.mkdir(exist_ok=True)
        
        # Check if logo is already cached
        logo_filename = f"logo_{logo_id_int}"
        
        # Try common image extensions
        for ext in ['.png', '.jpg', '.jpeg', '.gif', '.webp', '.svg']:
            cached_path = logos_cache_dir / f"{logo_filename}{ext}"
            if cached_path.exists():
                # Serve cached logo
                return send_file(cached_path, mimetype=f'image/{ext[1:]}')
        
        # Logo not cached, download it from Dispatcharr
        udi = get_udi_manager()
        logo = udi.get_logo_by_id(logo_id_int)
        
        if not logo:
            return jsonify({"error": "Logo not found"}), 404
        
        # Get the Dispatcharr base URL from config instead of environment variable
        # This ensures we use the configured value from the UI
        dispatcharr_config = get_dispatcharr_config()
        dispatcharr_base_url = dispatcharr_config.get_base_url()
        
        if not dispatcharr_base_url:
            # Fallback to environment variable for backward compatibility
            dispatcharr_base_url = os.getenv("DISPATCHARR_BASE_URL", "")
            if not dispatcharr_base_url:
                return jsonify({"error": "DISPATCHARR_BASE_URL not configured"}), 500
            
        logo_url = logo.get('cache_url') or logo.get('url')
        
        if not logo_url:
            return jsonify({"error": "Logo URL not available"}), 404
        
        # If cache_url is a relative path, make it absolute
        if logo_url.startswith('/'):
            logo_url = f"{dispatcharr_base_url}{logo_url}"
        
        # Validate URL scheme (must be http or https)
        if not logo_url.startswith(('http://', 'https://')):
            return jsonify({"error": "Invalid logo URL scheme"}), 400
        
        # Download the logo with SSL verification enabled
        logger.debug(f"Downloading logo {logo_id_int} from {logo_url}")
        response = requests.get(logo_url, timeout=10, verify=True)
        response.raise_for_status()
        
        # Determine file extension from content-type or URL
        content_type = response.headers.get('content-type', '').lower()
        ext = '.png'  # default
        if 'jpeg' in content_type or 'jpg' in content_type:
            ext = '.jpg'
        elif 'png' in content_type:
            ext = '.png'
        elif 'gif' in content_type:
            ext = '.gif'
        elif 'webp' in content_type:
            ext = '.webp'
        elif 'svg' in content_type:
            ext = '.svg'
        else:
            # Try to extract from URL
            if logo_url.lower().endswith('.jpg') or logo_url.lower().endswith('.jpeg'):
                ext = '.jpg'
            elif logo_url.lower().endswith('.png'):
                ext = '.png'
            elif logo_url.lower().endswith('.gif'):
                ext = '.gif'
            elif logo_url.lower().endswith('.webp'):
                ext = '.webp'
            elif logo_url.lower().endswith('.svg'):
                ext = '.svg'
        
        # Save the logo to cache
        cached_path = logos_cache_dir / f"{logo_filename}{ext}"
        with open(cached_path, 'wb') as f:
            f.write(response.content)
        
        logger.debug(f"Cached logo {logo_id} to {cached_path}")
        
        # Serve the cached logo
        mimetype = f'image/{ext[1:]}'
        if ext == '.svg':
            mimetype = 'image/svg+xml'
        return send_file(cached_path, mimetype=mimetype)
        
    except requests.exceptions.RequestException as e:
        logger.error(f"Error downloading logo {logo_id}: {e}")
        return jsonify({"error": "Failed to download logo"}), 500
    except Exception as e:
        logger.error(f"Error caching logo {logo_id}: {e}")
        return jsonify({"error": "Internal Server Error"}), 500

@app.route('/api/regex-patterns', methods=['GET'])
def get_regex_patterns():
    """Get all regex patterns for channel matching."""
    try:
        matcher = get_regex_matcher()
        # Reload patterns from disk to ensure we have the latest configuration
        # This handles cases where users manually edit the config file
        matcher.reload_patterns()
        patterns = matcher.get_patterns()
        return jsonify(patterns)
    except Exception as e:
        logger.error(f"Error getting regex patterns: {e}")
        return jsonify({"error": "Internal Server Error"}), 500

@app.route('/api/regex-patterns', methods=['POST'])
def add_regex_pattern():
    """Add or update a regex pattern for a channel."""
    try:
        data = request.get_json()
        if not data:
            return jsonify({"error": "No pattern data provided"}), 400
        
        required_fields = ['channel_id', 'name', 'regex']
        if not all(field in data for field in required_fields):
            return jsonify({"error": f"Missing required fields: {required_fields}"}), 400
        
        matcher = get_regex_matcher()
        m3u_accounts = data.get('m3u_accounts')  # Optional field for M3U account filtering
        matcher.add_channel_pattern(
            data['channel_id'],
            data['name'],
            data['regex'],
            data.get('enabled', True),
            m3u_accounts=m3u_accounts
        )
        
        return jsonify({"message": "Pattern added/updated successfully"})
    except ValueError as e:
        # Validation errors (e.g., invalid regex) should return 400
        logger.warning(f"Validation error adding regex pattern: {e}")
        return jsonify({"error": "Invalid value or request parameters"}), 400
    except Exception as e:
        logger.error(f"Error adding regex pattern: {e}")
        return jsonify({"error": "Internal Server Error"}), 500

@app.route('/api/regex-patterns/<channel_id>', methods=['DELETE'])
def delete_regex_pattern(channel_id):
    """Delete a regex pattern for a channel."""
    try:
        matcher = get_regex_matcher()
        # Reload patterns from disk to ensure we have the latest configuration
        matcher.reload_patterns()
        patterns = matcher.get_patterns()
        
        if 'patterns' in patterns and str(channel_id) in patterns['patterns']:
            del patterns['patterns'][str(channel_id)]
            matcher._save_patterns(patterns)
            return jsonify({"message": "Pattern deleted successfully"})
        else:
            return jsonify({"error": "Pattern not found"}), 404
    except Exception as e:
        logger.error(f"Error deleting regex pattern: {e}")
        return jsonify({"error": "Internal Server Error"}), 500

@app.route('/api/channels/<channel_id>/match-settings', methods=['POST'])
def update_channel_match_settings(channel_id):
    """Update matching settings for a channel (e.g., match_by_tvg_id)."""
    try:
        data = request.get_json()
        if not data:
            return jsonify({"error": "No settings provided"}), 400
            
        matcher = get_regex_matcher()
        
        # Handle match_by_tvg_id setting
        if 'match_by_tvg_id' in data:
            matcher.set_match_by_tvg_id(channel_id, data['match_by_tvg_id'])
            
        return jsonify({"message": "Match settings updated successfully"})
    except Exception as e:
        logger.error(f"Error updating match settings for channel {channel_id}: {e}")
        return jsonify({"error": "Internal Server Error"}), 500


@app.route('/api/channels/groups/<int:group_id>/regex-config', methods=['GET'])
def get_group_regex_config(group_id):
    """Get regex matching config for a channel group."""
    try:
        matcher = get_regex_matcher()
        matcher.reload_patterns()
        cfg = matcher.get_group_pattern(group_id) or {
            "name": "",
            "enabled": True,
            "match_by_tvg_id": False,
            "regex_patterns": []
        }
        return jsonify(cfg), 200
    except Exception as e:
        logger.error(f"Error getting regex config for group {group_id}: {e}")
        return jsonify({"error": "Internal Server Error"}), 500


@app.route('/api/channels/groups/<int:group_id>/regex-config', methods=['POST'])
def upsert_group_regex_config(group_id):
    """Add or update regex matching config for a channel group."""
    try:
        data = request.get_json() or {}
        matcher = get_regex_matcher()

        regex_patterns = data.get('regex_patterns')
        if regex_patterns is None:
            # Backward-compatible shorthand
            regex_patterns = data.get('regex', [])

        if not isinstance(regex_patterns, list):
            return jsonify({"error": "regex_patterns must be a list"}), 400

        enabled = bool(data.get('enabled', True))
        match_by_tvg_id = bool(data.get('match_by_tvg_id', False))

        name = data.get('name', '')
        if not name:
            try:
                udi = get_udi_manager()
                group = udi.get_channel_group_by_id(group_id)
                if isinstance(group, dict):
                    name = group.get('name', '')
            except Exception:
                pass

        m3u_accounts = data.get('m3u_accounts')
        matcher.add_group_pattern(
            group_id=group_id,
            name=name,
            regex_patterns=regex_patterns,
            enabled=enabled,
            match_by_tvg_id=match_by_tvg_id,
            m3u_accounts=m3u_accounts,
        )

        return jsonify({"message": "Group regex config updated successfully"}), 200
    except ValueError as e:
        logger.warning(f"Validation error upserting group regex config for group {group_id}: {e}")
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        logger.error(f"Error upserting regex config for group {group_id}: {e}")
        return jsonify({"error": "Internal Server Error"}), 500


@app.route('/api/channels/groups/<int:group_id>/regex-config', methods=['DELETE'])
def delete_group_regex_config(group_id):
    """Delete regex matching config for a channel group."""
    try:
        matcher = get_regex_matcher()
        matcher.delete_group_pattern(group_id)
        return jsonify({"message": "Group regex config deleted successfully"}), 200
    except Exception as e:
        logger.error(f"Error deleting regex config for group {group_id}: {e}")
        return jsonify({"error": "Internal Server Error"}), 500


@app.route('/api/channels/groups/<int:group_id>/match-settings', methods=['POST'])
def update_group_match_settings(group_id):
    """Update match settings for a group (e.g., match_by_tvg_id)."""
    try:
        data = request.get_json() or {}
        matcher = get_regex_matcher()

        if 'match_by_tvg_id' in data:
            matcher.set_group_match_by_tvg_id(group_id, bool(data['match_by_tvg_id']))

        return jsonify({"message": "Group match settings updated successfully"}), 200
    except Exception as e:
        logger.error(f"Error updating match settings for group {group_id}: {e}")
        return jsonify({"error": "Internal Server Error"}), 500

@app.route('/api/regex-patterns/bulk', methods=['POST'])
def add_bulk_regex_patterns():
    """Add the same regex patterns to multiple channels."""
    try:
        data = request.get_json()
        if not data:
            return jsonify({"error": "No data provided"}), 400
        
        required_fields = ['channel_ids', 'regex_patterns']
        if not all(field in data for field in required_fields):
            return jsonify({"error": f"Missing required fields: {required_fields}"}), 400
        
        channel_ids = data['channel_ids']
        regex_patterns = data['regex_patterns']
        m3u_accounts = data.get('m3u_accounts')  # Optional M3U account filtering
        
        if not isinstance(channel_ids, list) or len(channel_ids) == 0:
            return jsonify({"error": "channel_ids must be a non-empty list"}), 400
        
        if not isinstance(regex_patterns, list) or len(regex_patterns) == 0:
            return jsonify({"error": "regex_patterns must be a non-empty list"}), 400
        
        # Get UDI manager to fetch channel names
        udi = get_udi_manager()
        matcher = get_regex_matcher()
        
        # Validate patterns before applying
        is_valid, error_msg = matcher.validate_regex_patterns(regex_patterns)
        if not is_valid:
            return jsonify({"error": "Invalid value or request parameters"}), 400
        
        # Apply patterns to each channel
        success_count = 0
        failed_channels = []
        

        for channel_id in channel_ids:
            try:
                # Get channel name from UDI
                channel = udi.get_channel_by_id(channel_id)
                if not channel:
                    failed_channels.append({
                        "channel_id": channel_id,
                        "error": "Channel not found"
                    })
                    continue
                
                channel_name = channel.get('name', f'Channel {channel_id}')
                
                # Use lock to prevent race conditions during read-modify-write
                with matcher.lock:
                    # Get existing patterns for this channel
                    patterns = matcher.get_patterns()
                    existing_pattern_data = patterns.get('patterns', {}).get(str(channel_id), {})
                    
                    # Get existing regex patterns (support both new and old format)
                    existing_regex_patterns = existing_pattern_data.get('regex_patterns')
                    
                    # Normalize existing patterns to list of objects
                    normalized_existing = []
                    if existing_regex_patterns:
                        # New format: list of dicts
                        for p in existing_regex_patterns:
                            if isinstance(p, dict):
                                normalized_existing.append(p)
                            else:
                                normalized_existing.append({
                                    "pattern": p,
                                    "m3u_accounts": existing_pattern_data.get('m3u_accounts')
                                })
                    else:
                        # Old format: regex list
                        old_regex = existing_pattern_data.get('regex', [])
                        old_m3u_accounts = existing_pattern_data.get('m3u_accounts')
                        for p in old_regex:
                            normalized_existing.append({
                                "pattern": p,
                                "m3u_accounts": old_m3u_accounts
                            })
                    
                    # Normalize new patterns to list of objects
                    normalized_new = []
                    for p in regex_patterns:
                        normalized_new.append({
                            "pattern": p,
                            "m3u_accounts": m3u_accounts
                        })
                    
                    # Merge patterns (avoid duplicates based on pattern string)
                    # We prioritize existing patterns to preserve their specific settings (priority, m3u_accounts)
                    # unless the user explicitly wants to update them (which bulk add doesn't support yet, use mass edit for that)
                    merged_patterns = list(normalized_existing)
                    existing_pattern_strings = {p['pattern'] for p in normalized_existing}
                    
                    for new_p in normalized_new:
                        if new_p['pattern'] not in existing_pattern_strings:
                            merged_patterns.append(new_p)
                            existing_pattern_strings.add(new_p['pattern'])
                    
                    # Add/update pattern in new format
                    # We use the internal _save_patterns method directly or pass the full object to add_channel_pattern
                    # But add_channel_pattern expects the input format which it then normalizes
                    # So we can pass the merged list of dicts directly
                    matcher.add_channel_pattern(
                        str(channel_id),
                        channel_name,
                        merged_patterns,
                        existing_pattern_data.get('enabled', True),
                        m3u_accounts=None, # Not used when passing list of dicts
                        silent=True
                    )

                success_count += 1
            except Exception as e:
                logger.error(f"Error adding pattern to channel {channel_id}: {e}")
                failed_channels.append({
                    "channel_id": channel_id,
                    "error": str(e)
                })
        
        response_data = {
            "message": f"Successfully added patterns to {success_count} channel(s)",
            "success_count": success_count,
            "total_channels": len(channel_ids)
        }
        
        if failed_channels:
            response_data["failed_channels"] = failed_channels
            response_data["failed_count"] = len(failed_channels)
        
        return jsonify(response_data)
    except ValueError as e:
        logger.warning(f"Validation error in bulk regex pattern addition: {e}")
        return jsonify({"error": "Invalid value or request parameters"}), 400
    except Exception as e:
        logger.error(f"Error adding bulk regex patterns: {e}")
        return jsonify({"error": "Internal Server Error"}), 500

# ==========================================
# Automation Profiles & Settings Endpoints
# ==========================================

@app.route('/api/settings/automation', methods=['GET'])
def get_global_automation_settings():
    """Get global automation settings."""
    try:
        manager = get_automation_config_manager()
        return jsonify(manager.get_global_settings())
    except Exception as e:
        logger.error(f"Error getting global automation settings: {e}")
        return jsonify({"error": "Internal Server Error"}), 500

@app.route('/api/settings/automation', methods=['POST'])
def update_global_automation_settings():
    """Update global automation settings."""
    try:
        data = request.get_json()
        if not data:
            return jsonify({"error": "No data provided"}), 400
            
        manager = get_automation_config_manager()
        success = manager.update_global_settings(
            regular_automation_enabled=data.get('regular_automation_enabled')
        )
        
        if success:
            return jsonify({"message": "Settings updated successfully", "settings": manager.get_global_settings()})
        else:
            return jsonify({"error": "Failed to update settings"}), 500
    except Exception as e:
        logger.error(f"Error updating global automation settings: {e}")
        return jsonify({"error": "Internal Server Error"}), 500

@app.route('/api/settings/automation/profiles', methods=['GET'])
def get_automation_profiles():
    """Get all automation profiles."""
    try:
        manager = get_automation_config_manager()
        return jsonify(manager.get_all_profiles())
    except Exception as e:
        logger.error(f"Error getting automation profiles: {e}")
        return jsonify({"error": "Internal Server Error"}), 500

@app.route('/api/settings/automation/profiles', methods=['POST'])
def create_automation_profile():
    """Create a new automation profile."""
    try:
        data = request.get_json()
        if not data:
            return jsonify({"error": "No data provided"}), 400
            
        manager = get_automation_config_manager()
        profile_id = manager.create_profile(data)
        
        if profile_id:
            return jsonify({"message": "Profile created successfully", "id": profile_id, "profile": manager.get_profile(profile_id)})
        else:
            return jsonify({"error": "Failed to create profile"}), 500
    except Exception as e:
        logger.error(f"Error creating automation profile: {e}")
        return jsonify({"error": "Internal Server Error"}), 500

@app.route('/api/settings/automation/profiles/<profile_id>', methods=['GET'])
def get_automation_profile(profile_id):
    """Get a specific automation profile."""
    try:
        manager = get_automation_config_manager()
        profile = manager.get_profile(profile_id)
        if profile:
            return jsonify(profile)
        else:
            return jsonify({"error": "Profile not found"}), 404
    except Exception as e:
        logger.error(f"Error getting automation profile {profile_id}: {e}")
        return jsonify({"error": "Internal Server Error"}), 500

@app.route('/api/settings/automation/profiles/<profile_id>', methods=['PUT'])
def update_automation_profile(profile_id):
    """Update a specific automation profile."""
    try:
        data = request.get_json()
        if not data:
            return jsonify({"error": "No data provided"}), 400
            
        manager = get_automation_config_manager()
        success = manager.update_profile(profile_id, data)
        
        if success:
            return jsonify({"message": "Profile updated successfully", "profile": manager.get_profile(profile_id)})
        else:
            return jsonify({"error": "Failed to update profile (not found or save error)"}), 404
    except Exception as e:
        logger.error(f"Error updating automation profile {profile_id}: {e}")
        return jsonify({"error": "Internal Server Error"}), 500

@app.route('/api/settings/automation/profiles/<profile_id>', methods=['DELETE'])
def delete_automation_profile(profile_id):
    """Delete a specific automation profile."""
    try:
        manager = get_automation_config_manager()
        success = manager.delete_profile(profile_id)
        
        if success:
            return jsonify({"message": "Profile deleted successfully"})
        else:
            return jsonify({"error": "Failed to delete profile (not found or save error)"}), 404
    except Exception as e:
        logger.error(f"Error deleting automation profile {profile_id}: {e}")
        return jsonify({"error": "Internal Server Error"}), 500

@app.route('/api/settings/automation/assign/channel', methods=['POST'])
def assign_profile_to_channel():
    """Assign a profile to a channel."""
    try:
        data = request.get_json()
        if not data or 'channel_id' not in data:
            return jsonify({"error": "Missing channel_id"}), 400
            
        channel_id = data['channel_id']
        profile_id = data.get('profile_id')  # None is valid (unassign)
        
        manager = get_automation_config_manager()
        success = manager.assign_profile_to_channel(channel_id, profile_id)
        
        if success:
            return jsonify({"message": "Assignment updated successfully"})
        else:
            return jsonify({"error": "Failed to update assignment"}), 500
    except Exception as e:
        logger.error(f"Error assigning profile to channel: {e}")
        return jsonify({"error": "Internal Server Error"}), 500

@app.route('/api/settings/automation/assign/group', methods=['POST'])
def assign_profile_to_group():
    """Assign a profile to a group."""
    try:
        data = request.get_json()
        if not data or 'group_id' not in data:
            return jsonify({"error": "Missing group_id"}), 400
            
        group_id = data['group_id']
        profile_id = data.get('profile_id')  # None is valid (unassign)
        
        manager = get_automation_config_manager()
        success = manager.assign_profile_to_group(group_id, profile_id)
        
        if success:
            return jsonify({"message": "Assignment updated successfully"})
        else:
            return jsonify({"error": "Failed to update assignment"}), 500
    except Exception as e:
        logger.error(f"Error assigning profile to group: {e}")
        return jsonify({"error": "Internal Server Error"}), 500

@app.route('/api/settings/automation/effective/<int:channel_id>', methods=['GET'])
def get_effective_profile(channel_id):
    """Get the effective profile for a channel (resolving assignments)."""
    try:
        # We need the group ID to resolve fully.
        # Ideally frontend provides it, or we look it up.
        # Looking it up is safer.
        udi = get_udi_manager()
        channel = udi.get_channel_by_id(channel_id)
        group_id = None
        if channel:
             group_id = channel.get('group_id')
             
        manager = get_automation_config_manager()
        profile = manager.get_effective_profile(channel_id, group_id)
        
        effective_id = manager.get_effective_profile_id(channel_id, group_id)
        assigned_profile_id = manager.get_channel_assignment(channel_id)
        assigned_group_profile_id = manager.get_group_assignment(group_id) if group_id else None
        
        return jsonify({
            "effective_profile": profile,
            "effective_profile_id": effective_id,
            "channel_assignment": assigned_profile_id,
            "group_assignment": assigned_group_profile_id,
            "source": "channel" if assigned_profile_id else ("group" if assigned_group_profile_id else "default")
        })
    except Exception as e:
        logger.error(f"Error getting effective profile for channel {channel_id}: {e}")
        return jsonify({"error": "Internal Server Error"}), 500

@app.route('/api/regex-patterns/import', methods=['POST'])
def import_regex_patterns():
    """Import regex patterns from JSON.

    Accepts the canonical export format::

        {
          "patterns": {
            "<channel_id>": {
              "name": "...",
              "enabled": true,
              "match_by_tvg_id": false,
              "regex_patterns": [
                {"pattern": "...", "m3u_accounts": null}
              ]
            }
          },
          "global_settings": {"case_sensitive": true, "require_exact_match": false}
        }

    The old ``"regex"`` key is also accepted for backward compatibility.
    Existing patterns are fully replaced (non-merge mode).
    """
    try:
        data = request.get_json()
        if not data:
            return jsonify({"error": "No data provided"}), 400
        
        # Validate the JSON structure
        if not isinstance(data, dict):
            return jsonify({"error": "Invalid JSON format: must be an object"}), 400
        
        if 'patterns' not in data:
            return jsonify({"error": "Invalid JSON format: missing 'patterns' field"}), 400
        
        if not isinstance(data['patterns'], dict):
            return jsonify({"error": "Invalid JSON format: 'patterns' must be an object"}), 400
        
        # Validate each pattern before importing
        matcher = get_regex_matcher()
        for channel_id, pattern_data in data['patterns'].items():
            if not isinstance(pattern_data, dict):
                return jsonify({"error": f"Invalid pattern format for channel {channel_id}"}), 400
            
            # Support both old format (regex), new format (regex_patterns), and legacy hybrid format
            regex_patterns_to_validate = []
            
            if 'regex_patterns' in pattern_data:
                if not isinstance(pattern_data['regex_patterns'], list):
                    return jsonify({"error": f"'regex_patterns' must be a list for channel {channel_id}"}), 400
                
                for pattern_obj in pattern_data['regex_patterns']:
                    if isinstance(pattern_obj, dict):
                        pattern = pattern_obj.get('pattern', '')
                        if not pattern:
                            return jsonify({"error": f"Pattern object missing or has empty 'pattern' field for channel {channel_id}"}), 400
                        regex_patterns_to_validate.append(pattern)
                    elif isinstance(pattern_obj, str):
                        regex_patterns_to_validate.append(pattern_obj)
                    else:
                        return jsonify({"error": f"Pattern in regex_patterns must be a string or object for channel {channel_id}, got {type(pattern_obj).__name__}"}), 400
            elif 'regex' in pattern_data:
                if not isinstance(pattern_data['regex'], list):
                    return jsonify({"error": f"'regex' must be a list for channel {channel_id}"}), 400
                regex_patterns_to_validate = pattern_data['regex']
            else:
                return jsonify({"error": f"Missing 'regex' or 'regex_patterns' field for channel {channel_id}"}), 400
            
            if not regex_patterns_to_validate:
                return jsonify({"error": f"No patterns provided for channel {channel_id}"}), 400
            
            is_valid, error_msg = matcher.validate_regex_patterns(regex_patterns_to_validate)
            if not is_valid:
                return jsonify({"error": f"Invalid regex pattern components for channel {channel_id}"}), 400
        
        # Validation passed – persist via DAL and reload in-memory cache
        from apps.database.manager import get_db_manager
        db = get_db_manager()
        imported, errors = db.import_channel_regex_configs_from_json(data, merge=False)
        if errors:
            logger.error(f"Errors during import: {errors}")
            return jsonify({"error": "Import failed", "details": errors}), 500

        # Persist global_settings if provided
        if 'global_settings' in data and isinstance(data['global_settings'], dict):
            db.set_system_setting('channel_regex_global_settings', data['global_settings'])

        matcher.reload_patterns()
        
        pattern_count = len(data['patterns'])
        logger.info(f"Imported {pattern_count} regex patterns successfully")
        
        return jsonify({
            "message": f"Successfully imported {pattern_count} patterns",
            "pattern_count": pattern_count
        })
    except Exception as e:
        logger.error(f"Error importing regex patterns: {e}")
        return jsonify({"error": "Internal Server Error"}), 500


@app.route('/api/regex-patterns/export', methods=['GET'])
def export_regex_patterns():
    """Export all regex patterns as a JSON blob (canonical format).

    The returned JSON is compatible with the ``/api/regex-patterns/import``
    endpoint, making round-trip backup/restore straightforward.
    """
    try:
        from apps.database.manager import get_db_manager
        db = get_db_manager()
        export_data = db.export_channel_regex_configs_as_json()
        return jsonify(export_data)
    except Exception as e:
        logger.error(f"Error exporting regex patterns: {e}")
        return jsonify({"error": "Internal Server Error"}), 500

@app.route('/api/regex-patterns/bulk-delete', methods=['POST'])
def bulk_delete_regex_patterns():
    """Delete all regex patterns from multiple channels."""
    try:
        data = request.get_json()
        if not data:
            return jsonify({"error": "No data provided"}), 400
        
        channel_ids = data.get('channel_ids', [])
        
        if not isinstance(channel_ids, list) or len(channel_ids) == 0:
            return jsonify({"error": "channel_ids must be a non-empty list"}), 400
        
        matcher = get_regex_matcher()
        
        # Delete patterns for each channel
        success_count = 0
        failed_channels = []
        
        for channel_id in channel_ids:
            try:
                matcher.delete_channel_pattern(str(channel_id))
                success_count += 1
            except Exception as e:
                logger.error(f"Error deleting patterns from channel {channel_id}: {e}")
                failed_channels.append({
                    "channel_id": channel_id,
                    "error": str(e)
                })
        
        response_data = {
            "message": f"Successfully deleted patterns from {success_count} channel(s)",
            "success_count": success_count,
            "total_channels": len(channel_ids)
        }
        
        if failed_channels:
            response_data["failed_channels"] = failed_channels
            response_data["failed_count"] = len(failed_channels)
        
        return jsonify(response_data)
    except Exception as e:
        logger.error(f"Error bulk deleting regex patterns: {e}")
        return jsonify({"error": "Internal Server Error"}), 500

@app.route('/api/regex-patterns/common', methods=['POST'])
def get_common_regex_patterns():
    """Get common regex patterns across multiple channels, ordered by frequency."""
    try:
        data = request.get_json()
        if not data:
            return jsonify({"error": "No data provided"}), 400
        
        channel_ids = data.get('channel_ids', [])
        
        if not isinstance(channel_ids, list) or len(channel_ids) == 0:
            return jsonify({"error": "channel_ids must be a non-empty list"}), 400
        
        matcher = get_regex_matcher()
        patterns_data = matcher.get_patterns()
        
        # Count pattern occurrences across selected channels
        pattern_count = {}
        pattern_to_channels = {}
        
        for channel_id in channel_ids:
            channel_patterns = patterns_data.get('patterns', {}).get(str(channel_id), {})
            
            # Support both old format (regex) and new format (regex_patterns)
            regex_patterns = channel_patterns.get('regex_patterns')
            if regex_patterns is None:
                # Fallback to old format
                regex_patterns = [{"pattern": p, "priority": 0} for p in channel_patterns.get('regex', [])]
            
            for pattern_obj in regex_patterns:
                if isinstance(pattern_obj, dict):
                    pattern = pattern_obj.get('pattern', '')
                else:
                    # Legacy string format
                    pattern = pattern_obj
                
                if pattern:
                    if pattern not in pattern_count:
                        pattern_count[pattern] = 0
                        pattern_to_channels[pattern] = []
                    
                    pattern_count[pattern] += 1
                    pattern_to_channels[pattern].append(str(channel_id))
        
        # Sort patterns by frequency (most common first)
        sorted_patterns = sorted(pattern_count.items(), key=lambda x: x[1], reverse=True)
        
        # Format results
        common_patterns = []
        for pattern, count in sorted_patterns:
            common_patterns.append({
                "pattern": pattern,
                "count": count,
                "channel_ids": pattern_to_channels[pattern],
                "percentage": round((count / len(channel_ids)) * 100, 1)
            })
        
        return jsonify({
            "patterns": common_patterns,
            "total_channels": len(channel_ids),
            "total_patterns": len(common_patterns)
        })
    except Exception as e:
        logger.error(f"Error getting common regex patterns: {e}")
        return jsonify({"error": "Internal Server Error"}), 500

@app.route('/api/regex-patterns/bulk-edit', methods=['POST'])
def bulk_edit_regex_pattern():
    """Edit a specific regex pattern across multiple channels.
    
    This endpoint allows editing the pattern itself, its associated playlists (m3u_accounts), and priority.
    """
    try:
        data = request.get_json()
        if not data:
            return jsonify({"error": "No data provided"}), 400
        
        required_fields = ['channel_ids', 'old_pattern', 'new_pattern']
        if not all(field in data for field in required_fields):
            return jsonify({"error": f"Missing required fields: {required_fields}"}), 400
        
        channel_ids = data['channel_ids']
        old_pattern = data['old_pattern']
        new_pattern = data['new_pattern']
        new_m3u_accounts = data.get('new_m3u_accounts')  # Optional: new playlist filter (list of M3U account IDs, or None to keep existing accounts, or null to apply to all playlists)
        new_priority = data.get('new_priority')  # Optional: new priority value (integer, or None to keep existing priority)
        
        if not isinstance(channel_ids, list) or len(channel_ids) == 0:
            return jsonify({"error": "channel_ids must be a non-empty list"}), 400
        
        # Validate new pattern
        matcher = get_regex_matcher()
        is_valid, error_msg = matcher.validate_regex_patterns([new_pattern])
        if not is_valid:
            return jsonify({"error": "Invalid value or request parameters"}), 400
        
        # Get UDI manager to fetch channel names
        udi = get_udi_manager()
        
        # Update pattern in each channel
        success_count = 0
        failed_channels = []
        
        for channel_id in channel_ids:
            try:
                # Get channel info
                channel = udi.get_channel_by_id(channel_id)
                if not channel:
                    failed_channels.append({
                        "channel_id": channel_id,
                        "error": "Channel not found"
                    })
                    continue
                
                channel_name = channel.get('name', f'Channel {channel_id}')
                
                # Get existing patterns
                patterns = matcher.get_patterns()
                existing_patterns = patterns.get('patterns', {}).get(str(channel_id), {})
                
                # Support both old format (regex) and new format (regex_patterns)
                regex_patterns = existing_patterns.get('regex_patterns')
                if regex_patterns is None:
                    # Fallback to old format
                    old_regex = existing_patterns.get('regex', [])
                    old_m3u_accounts = existing_patterns.get('m3u_accounts')
                    regex_patterns = [{"pattern": p, "m3u_accounts": old_m3u_accounts} for p in old_regex]
                
                # Find and replace pattern
                pattern_found = False
                updated_patterns = []
                seen_patterns = set()  # Track patterns to avoid duplicates
                
                for pattern_obj in regex_patterns:
                    if isinstance(pattern_obj, dict):
                        pattern = pattern_obj.get("pattern", "")
                        pattern_m3u_accounts = pattern_obj.get("m3u_accounts")
                    else:
                        # Legacy string format
                        pattern = pattern_obj
                        pattern_m3u_accounts = None
                    
                    if pattern == old_pattern:
                        pattern_found = True
                        # Update the pattern and optionally the m3u_accounts
                        # Only add if we haven't seen the new pattern yet (avoid duplicates)
                        if new_pattern not in seen_patterns:
                            updated_pattern = {
                                "pattern": new_pattern,
                                "m3u_accounts": new_m3u_accounts if new_m3u_accounts is not None else pattern_m3u_accounts
                            }
                            updated_patterns.append(updated_pattern)
                            seen_patterns.add(new_pattern)
                    else:
                        # Only add if we haven't seen this pattern yet (avoid duplicates)
                        if pattern not in seen_patterns:
                            updated_patterns.append({
                                "pattern": pattern,
                                "m3u_accounts": pattern_m3u_accounts
                            })
                            seen_patterns.add(pattern)
                
                if pattern_found:
                    # Update pattern using new format
                    matcher.add_channel_pattern(
                        str(channel_id),
                        channel_name,
                        updated_patterns,
                        existing_patterns.get('enabled', True),
                        silent=True  # Suppress per-channel logging during batch operations
                    )
                    success_count += 1
                else:
                    # Pattern not found in this channel, skip
                    failed_channels.append({
                        "channel_id": channel_id,
                        "error": "Pattern not found in channel"
                    })
            except Exception as e:
                logger.error(f"Error editing pattern in channel {channel_id}: {e}")
                failed_channels.append({
                    "channel_id": channel_id,
                    "error": str(e)
                })
        
        response_data = {
            "message": f"Successfully edited pattern in {success_count} channel(s)",
            "success_count": success_count,
            "total_channels": len(channel_ids)
        }
        
        if failed_channels:
            response_data["failed_channels"] = failed_channels
            response_data["failed_count"] = len(failed_channels)
        
        return jsonify(response_data)
    except ValueError as e:
        logger.warning(f"Validation error in bulk pattern edit: {e}")
        return jsonify({"error": "Invalid value or request parameters"}), 400
    except Exception as e:
        logger.error(f"Error bulk editing regex pattern: {e}")
        return jsonify({"error": "Internal Server Error"}), 500

@app.route('/api/regex-patterns/bulk-settings', methods=['POST'])
def bulk_update_match_settings():
    """Update match settings (e.g., match_by_tvg_id) for multiple channels."""
    try:
        data = request.get_json()
        if not data:
            return jsonify({"error": "No data provided"}), 400
            
        channel_ids = data.get('channel_ids', [])
        settings = data.get('settings', {})
        
        if not channel_ids or not isinstance(channel_ids, list):
            return jsonify({"error": "channel_ids must be a non-empty list"}), 400
            
        if not settings:
            return jsonify({"error": "settings must be provided"}), 400
            
        matcher = get_regex_matcher()
        success_count = 0
        failed_channels = []
        
        # Iterate and update
        for channel_id in channel_ids:
            try:
                # Assuming settings currently only contains 'match_by_tvg_id'
                # but could be expanded.
                if 'match_by_tvg_id' in settings:
                    if matcher.set_match_by_tvg_id(channel_id, bool(settings['match_by_tvg_id'])):
                        success_count += 1
                    else:
                        failed_channels.append({"channel_id": channel_id, "error": "Failed to set setting"})
            except Exception as e:
                failed_channels.append({"channel_id": channel_id, "error": str(e)})
        
        return jsonify({  # lgtm [py/stack-trace-exposure]
            "message": f"Updated settings for {success_count} channels",
            "success_count": success_count,
            "failed_count": len(failed_channels),
            "failed_channels": failed_channels
        })
    except Exception as e:
        logger.error(f"Error bulk updating match settings: {e}")
        return jsonify({"error": "Internal Server Error"}), 500

@app.route('/api/settings/automation/global', methods=['GET', 'PUT'])
def handle_global_automation_settings():
    """Get or update global automation settings."""
    config_manager = get_automation_config_manager()
    
    if request.method == 'GET':
        try:
            settings = config_manager.get_global_settings()
            return jsonify(settings)
        except Exception as e:
            logger.error(f"Error getting global automation settings: {e}")
            return jsonify({"error": "Internal Server Error"}), 500
            
    elif request.method == 'PUT':
        try:
            updates = request.json
            if not updates:
                return jsonify({"error": "No data provided"}), 400
                
            # Get current settings before update to detect changes
            old_settings = config_manager.get_global_settings()
            old_regular_enabled = old_settings.get('regular_automation_enabled', False)
                
            if config_manager.update_global_settings(settings=updates):
                # Return updated settings
                new_settings = config_manager.get_global_settings()
                new_regular_enabled = new_settings.get('regular_automation_enabled', False)
                
                # Control automation service lifecycle if regular_automation_enabled changed
                if old_regular_enabled != new_regular_enabled and check_wizard_complete():
                    manager = get_automation_manager()
                    
                    if new_regular_enabled:
                        # Start automation service if not already running
                        if not manager.automation_running:
                            manager.start_automation()
                            logger.info("Automation service started (Enable Regular Automation toggled ON via /settings)")
                    else:
                        # Stop automation service if running
                        if manager.automation_running:
                            manager.stop_automation()
                            logger.info("Automation service stopped (Enable Regular Automation toggled OFF via /settings)")
                
                return jsonify(new_settings)
            else:
                return jsonify({"error": "Failed to update global settings"}), 500
        except Exception as e:
            logger.error(f"Error updating global automation settings: {e}")
            return jsonify({"error": "Internal Server Error"}), 500

@app.route('/api/regex-patterns/mass-edit-preview', methods=['POST'])
def mass_edit_preview():
    """Preview the results of a mass find/replace operation on regex patterns.
    
    This endpoint shows what patterns will be affected by the find/replace operation
    without actually making changes.
    """
    try:
        data = request.get_json()
        if not data:
            return jsonify({"error": "No data provided"}), 400
        
        required_fields = ['channel_ids', 'find_pattern', 'replace_pattern']
        if not all(field in data for field in required_fields):
            return jsonify({"error": f"Missing required fields: {required_fields}"}), 400
        
        channel_ids = data['channel_ids']
        find_pattern = data['find_pattern']
        replace_pattern = data['replace_pattern']
        use_regex = data.get('use_regex', False)  # Whether to use regex for find/replace
        
        if not isinstance(channel_ids, list) or len(channel_ids) == 0:
            return jsonify({"error": "channel_ids must be a non-empty list"}), 400
        
        matcher = get_regex_matcher()
        udi = get_udi_manager()
        
        # Compile regex if needed
        if use_regex:
            try:
                if is_dangerous_regex(find_pattern):
                    return jsonify({"error": "Regex pattern contains dangerous nested quantifiers (ReDoS risk)"}), 400
                find_regex = re.compile(find_pattern)  # lgtm [py/regex-injection]
            except re.error as e:
                return jsonify({"error": "Invalid regex pattern"}), 400
        
        affected_channels = []
        total_patterns_affected = 0
        
        for channel_id in channel_ids:
            try:
                # Get channel info
                channel = udi.get_channel_by_id(channel_id)
                if not channel:
                    continue
                
                channel_name = channel.get('name', f'Channel {channel_id}')
                
                # Get existing patterns
                patterns = matcher.get_patterns()
                existing_patterns = patterns.get('patterns', {}).get(str(channel_id), {})
                regex_patterns = existing_patterns.get('regex_patterns', [])
                
                if not regex_patterns:
                    continue
                
                # Find patterns that will be affected
                affected_patterns = []
                for pattern_obj in regex_patterns:
                    if isinstance(pattern_obj, dict):
                        pattern = pattern_obj.get("pattern", "")
                        pattern_m3u_accounts = pattern_obj.get("m3u_accounts")
                        pattern_priority = pattern_obj.get("priority", 0)
                    else:
                        pattern = pattern_obj
                        pattern_m3u_accounts = None
                        pattern_priority = 0
                    
                    # Check if this pattern will be affected
                    try:
                        if use_regex:
                            # Use regex replace
                            new_pattern = find_regex.sub(replace_pattern, pattern)
                        else:
                            # Use simple string replace
                            new_pattern = pattern.replace(find_pattern, replace_pattern)
                    except re.error as e:
                        # Invalid replacement pattern (e.g., bad backreference)
                        return jsonify({"error": "Invalid replacement pattern"}), 400
                    
                    # Only include if the pattern actually changes
                    if new_pattern != pattern:
                        affected_patterns.append({
                            "old_pattern": pattern,
                            "new_pattern": new_pattern,
                            "m3u_accounts": pattern_m3u_accounts,
                            "priority": pattern_priority
                        })
                
                if affected_patterns:
                    affected_channels.append({
                        "channel_id": channel_id,
                        "channel_name": channel_name,
                        "affected_patterns": affected_patterns,
                        "total_affected": len(affected_patterns)
                    })
                    total_patterns_affected += len(affected_patterns)
            
            except Exception as e:
                logger.error(f"Error previewing patterns for channel {channel_id}: {e}")
                continue
        
        return jsonify({
            "affected_channels": affected_channels,
            "total_channels_affected": len(affected_channels),
            "total_patterns_affected": total_patterns_affected,
            "find_pattern": find_pattern,
            "replace_pattern": replace_pattern,
            "use_regex": use_regex
        })
    
    except Exception as e:
        logger.error(f"Error previewing mass edit: {e}")
        return jsonify({"error": "Internal Server Error"}), 500

@app.route('/api/regex-patterns/mass-edit', methods=['POST'])
def mass_edit_regex_patterns():
    """Apply a mass find/replace operation on regex patterns across multiple channels.
    
    This endpoint performs find/replace on all patterns in the selected channels,
    optionally updating M3U accounts as well.
    """
    try:
        data = request.get_json()
        if not data:
            return jsonify({"error": "No data provided"}), 400
        
        required_fields = ['channel_ids', 'find_pattern', 'replace_pattern']
        if not all(field in data for field in required_fields):
            return jsonify({"error": f"Missing required fields: {required_fields}"}), 400
        
        channel_ids = data['channel_ids']
        find_pattern = data['find_pattern']
        replace_pattern = data['replace_pattern']
        use_regex = data.get('use_regex', False)
        new_m3u_accounts = data.get('new_m3u_accounts')  # Optional: update M3U accounts for affected patterns
        
        if not isinstance(channel_ids, list) or len(channel_ids) == 0:
            return jsonify({"error": "channel_ids must be a non-empty list"}), 400
        
        matcher = get_regex_matcher()
        udi = get_udi_manager()
        
        # Compile regex if needed
        if use_regex:
            try:
                if is_dangerous_regex(find_pattern):
                    return jsonify({"error": "Regex pattern contains dangerous nested quantifiers (ReDoS risk)"}), 400
                find_regex = re.compile(find_pattern)  # lgtm [py/regex-injection]
            except re.error as e:
                return jsonify({"error": "Invalid regex pattern"}), 400
        
        success_count = 0
        failed_channels = []
        total_patterns_updated = 0
        
        for channel_id in channel_ids:
            try:
                # Get channel info
                channel = udi.get_channel_by_id(channel_id)
                if not channel:
                    failed_channels.append({
                        "channel_id": channel_id,
                        "error": "Channel not found"
                    })
                    continue
                
                channel_name = channel.get('name', f'Channel {channel_id}')
                
                # Get existing patterns
                patterns = matcher.get_patterns()
                existing_patterns = patterns.get('patterns', {}).get(str(channel_id), {})
                regex_patterns = existing_patterns.get('regex_patterns', [])
                
                if not regex_patterns:
                    continue
                
                # Apply find/replace to all patterns
                updated_patterns = []
                seen_patterns = set()
                patterns_changed = False
                channel_failed = False
                
                for pattern_obj in regex_patterns:
                    if isinstance(pattern_obj, dict):
                        pattern = pattern_obj.get("pattern", "")
                        pattern_m3u_accounts = pattern_obj.get("m3u_accounts")
                        pattern_priority = pattern_obj.get("priority", 0)
                    else:
                        pattern = pattern_obj
                        pattern_m3u_accounts = None
                        pattern_priority = 0
                    
                    # Apply find/replace
                    try:
                        if use_regex:
                            new_pattern = find_regex.sub(replace_pattern, pattern)
                        else:
                            new_pattern = pattern.replace(find_pattern, replace_pattern)
                    except re.error as e:
                        failed_channels.append({
                            "channel_id": channel_id,
                            "error": f"Invalid replacement pattern: {str(e)}"
                        })
                        channel_failed = True
                        break
                    
                    # Track if anything changed
                    if new_pattern != pattern:
                        patterns_changed = True
                    
                    # Validate the new pattern
                    is_valid, error_msg = matcher.validate_regex_patterns([new_pattern])
                    if not is_valid:
                        failed_channels.append({
                            "channel_id": channel_id,
                            "error": f"Invalid resulting pattern '{new_pattern}': {error_msg}"
                        })
                        channel_failed = True
                        break
                    
                    # Only add if not duplicate
                    if new_pattern not in seen_patterns:
                        # Update M3U accounts if specified, otherwise keep existing
                        final_m3u_accounts = new_m3u_accounts if new_m3u_accounts is not None else pattern_m3u_accounts
                        
                        updated_patterns.append({
                            "pattern": new_pattern,
                            "m3u_accounts": final_m3u_accounts,
                            "priority": pattern_priority  # Preserve priority
                        })
                        seen_patterns.add(new_pattern)
                
                # Only update if patterns actually changed and no failures occurred
                if not channel_failed and patterns_changed and updated_patterns:
                    matcher.add_channel_pattern(
                        str(channel_id),
                        channel_name,
                        updated_patterns,
                        existing_patterns.get('enabled', True),
                        silent=True  # Suppress per-channel logging during batch operations
                    )
                    success_count += 1
                    total_patterns_updated += len(updated_patterns)
            
            except Exception as e:
                logger.error(f"Error applying mass edit to channel {channel_id}: {e}")
                failed_channels.append({
                    "channel_id": channel_id,
                    "error": str(e)
                })
        
        logger.info(f"Mass edit completed: {success_count} channels updated, {total_patterns_updated} patterns affected")
        
        response_data = {
            "message": f"Successfully updated {success_count} channel(s)",
            "success_count": success_count,
            "total_channels": len(channel_ids),
            "total_patterns_updated": total_patterns_updated
        }
        
        if failed_channels:
            response_data["failed_channels"] = failed_channels
            response_data["failed_count"] = len(failed_channels)
        
        return jsonify(response_data)
    
    except Exception as e:
        logger.error(f"Error in mass edit: {e}")
        return jsonify({"error": "Internal Server Error"}), 500

@app.route('/api/test-regex', methods=['POST'])
def test_regex_pattern():
    """Test a regex pattern against a stream name."""
    try:
        data = request.get_json()
        if not data or 'pattern' not in data or 'stream_name' not in data:
            return jsonify({"error": "Missing pattern or stream_name"}), 400
        
        pattern = data['pattern']
        stream_name = data['stream_name']
        case_sensitive = data.get('case_sensitive', False)
        
        import re
        
        search_pattern = pattern if case_sensitive else pattern.lower()
        search_name = stream_name if case_sensitive else stream_name.lower()
        
        # Convert literal spaces in pattern to flexible whitespace regex (\s+)
        # This allows matching streams with different whitespace characters
        # BUT: Don't convert escaped spaces - they should remain literal
        # We replace only non-escaped spaces using pre-compiled pattern for performance
        search_pattern = _WHITESPACE_PATTERN.sub(r'\\s+', search_pattern)
        
        try:
            if is_dangerous_regex(search_pattern):
                return jsonify({"error": "Regex pattern contains dangerous nested quantifiers (ReDoS risk)"}), 400
            match = re.search(search_pattern, search_name)  # lgtm [py/regex-injection]
            return jsonify({
                "matches": bool(match),
                "match_details": {
                    "pattern": pattern,
                    "stream_name": stream_name,
                    "case_sensitive": case_sensitive,
                    "match_start": match.start() if match else None,
                    "match_end": match.end() if match else None,
                    "matched_text": match.group() if match else None
                }
            })
        except re.error as e:
            return jsonify({"error": "Invalid regex pattern"}), 400
    except Exception as e:
        logger.error(f"Error testing regex pattern: {e}")
        return jsonify({"error": "Internal Server Error"}), 500

@app.route('/api/test-regex-live', methods=['POST'])
def test_regex_pattern_live():
    """Test regex patterns against all available streams to see what would be matched."""
    try:
        from apps.core.api_utils import get_streams, get_m3u_accounts
        import re
        
        data = request.get_json()
        if not data:
            return jsonify({"error": "Missing request body"}), 400
        
        # Get patterns to test - can be a single pattern or multiple patterns per channel
        patterns = data.get('patterns', [])
        case_sensitive = data.get('case_sensitive', True)
        max_matches_per_pattern = data.get('max_matches', 100)  # Limit results
        
        if not patterns:
            return jsonify({"error": "No patterns provided"}), 400
        
        # Get all available streams
        all_streams = get_streams()
        if not all_streams:
            return jsonify({
                "matches": [],
                "total_streams": 0,
                "message": "No streams available"
            })
        
        # Get M3U accounts to map account IDs to names
        m3u_accounts_list = get_m3u_accounts() or []
        m3u_account_map = {acc.get('id'): acc.get('name', f'Account {acc.get("id")}') 
                          for acc in m3u_accounts_list if acc.get('id') is not None}
        
        results = []
        
        # Test each pattern against all streams
        for pattern_info in patterns:
            channel_id = pattern_info.get('channel_id', 'unknown')
            channel_name = pattern_info.get('channel_name', 'Unknown Channel')
            regex_patterns = pattern_info.get('regex', [])
            m3u_accounts = pattern_info.get('m3u_accounts')  # Get M3U account filter (None or empty = all accounts)
            
            if not regex_patterns:
                continue
            
            matched_streams = []
            
            # Filter streams by M3U account if specified
            streams_to_test = all_streams
            if m3u_accounts:
                logger.debug(f"Filtering streams by M3U accounts {m3u_accounts}: testing against subset of streams")
                # Filter to only include streams from the specified M3U accounts
                streams_to_test = [s for s in all_streams if s.get('m3u_account') in m3u_accounts]
                logger.debug(f"Filtered to {len(streams_to_test)} of {len(all_streams)} streams")
            
            for stream in streams_to_test:
                if not isinstance(stream, dict):
                    continue
                
                stream_name = stream.get('name', '')
                stream_id = stream.get('id')
                
                if not stream_name:
                    continue
                
                search_name = stream_name if case_sensitive else stream_name.lower()
                
                # Test against all regex patterns for this channel
                matched = False
                matched_pattern = None
                
                for pattern in regex_patterns:
                    # Substitute CHANNEL_NAME variable with actual channel name
                    # This matches the behavior in automated_stream_manager.py
                    escaped_channel_name = re.escape(channel_name)
                    substituted_pattern = pattern.replace('CHANNEL_NAME', escaped_channel_name)
                    
                    search_pattern = substituted_pattern if case_sensitive else substituted_pattern.lower()
                    
                    # Convert literal spaces in pattern to flexible whitespace regex (\s+)
                    # This allows matching streams with different whitespace characters
                    # (non-breaking spaces, tabs, double spaces, etc.)
                    # BUT: Don't convert escaped spaces (from re.escape) - they should remain literal
                    # We replace only non-escaped spaces using pre-compiled pattern for performance
                    search_pattern = _WHITESPACE_PATTERN.sub(r'\\s+', search_pattern)
                    
                    try:
                        if is_dangerous_regex(search_pattern):
                            logger.warning(f"Invalid regex pattern '{pattern}': ReDoS risk")
                            continue
                        if re.search(search_pattern, search_name):  # lgtm [py/regex-injection]
                            matched = True
                            matched_pattern = pattern
                            break  # Only need one match
                    except re.error as e:
                        logger.warning(f"Invalid regex pattern '{pattern}': {e}")
                        continue
                
                if matched and len(matched_streams) < max_matches_per_pattern:
                    m3u_account_id = stream.get('m3u_account')
                    matched_streams.append({
                        "stream_id": stream_id,
                        "stream_name": stream_name,
                        "matched_pattern": matched_pattern,
                        "m3u_account": m3u_account_id,
                        "m3u_account_name": m3u_account_map.get(m3u_account_id) if m3u_account_id else None
                    })
            
            results.append({
                "channel_id": channel_id,
                "channel_name": channel_name,
                "patterns": regex_patterns,
                "m3u_accounts": m3u_accounts,
                "matched_streams": matched_streams,
                "match_count": len(matched_streams),
                "total_tested_streams": len(streams_to_test)
            })
        
        return jsonify({
            "results": results,
            "total_streams": len(all_streams),
            "case_sensitive": case_sensitive
        })
        
    except Exception as e:
        logger.error(f"Error testing regex patterns live: {e}")
        return jsonify({"error": "Internal Server Error"}), 500




@app.route('/api/changelog', methods=['GET'])
def get_changelog():
    """Get recent changelog entries from the new telemetry database."""
    try:
        days = request.args.get('days', 7, type=int)
        page = request.args.get('page', 1, type=int)
        limit = request.args.get('limit', 10, type=int)
        
        from apps.telemetry.telemetry_db import get_session, Run
        from datetime import datetime, timedelta
        import json
        
        cutoff = datetime.utcnow() - timedelta(days=days)
        session = get_session()
        
        try:
            # Query the required runs, excluding AceStream monitor telemetry snapshots
            runs = session.query(Run).filter(
                Run.timestamp >= cutoff,
                Run.run_type != 'acestream_monitor',
            ).order_by(Run.timestamp.desc()).all()
            
            merged_changelog = []
            for r in runs:
                details = {}
                if r.raw_details:
                    details = json.loads(r.raw_details)
                subentries = []
                if r.raw_subentries:
                    subentries = json.loads(r.raw_subentries)
                    
                merged_changelog.append({
                    "timestamp": r.timestamp.isoformat(),
                    "action": r.run_type,
                    "details": details,
                    "subentries": subentries
                })
                
            # Apply pagination
            total = len(merged_changelog)
            total_pages = (total + limit - 1) // limit if limit > 0 else 0
            start_idx = (page - 1) * limit
            end_idx = start_idx + limit
            paginated_data = merged_changelog[start_idx:end_idx] if limit > 0 else merged_changelog
            
            return jsonify({
                'data': paginated_data,
                'page': page,
                'limit': limit,
                'total': total,
                'total_pages': total_pages
            })
        finally:
            session.close()
    except Exception as e:
        logger.error(f"Error getting changelog: {e}")
        return jsonify({"error": "Internal Server Error"}), 500

@app.route('/api/dead-streams', methods=['GET'])
def get_dead_streams():
    """Get dead streams statistics and list with SQL-native pagination, sorting, and filtering."""
    try:
        page_param = request.args.get('page', '1')
        per_page_param = request.args.get('per_page', str(DEAD_STREAMS_DEFAULT_PER_PAGE))
        sort_by = request.args.get('sort_by', 'marked_dead_at')
        sort_dir = request.args.get('sort_dir', 'desc')
        search = request.args.get('search', '').strip()

        page, per_page, err = _parse_pagination_params(
            page_param, per_page_param,
            default_per_page=DEAD_STREAMS_DEFAULT_PER_PAGE,
            max_per_page=DEAD_STREAMS_MAX_PER_PAGE,
        )
        if err:
            return err

        if sort_dir not in ('asc', 'desc'):
            sort_dir = 'desc'

        from apps.database.manager import get_db_manager
        db = get_db_manager()
        result = db.get_dead_streams_paginated(
            page=page,
            per_page=per_page,
            sort_by=sort_by,
            sort_dir=sort_dir,
            search=search,
        )

        return jsonify({
            "total_dead_streams": result['total'],
            "dead_streams": result['items'],
            "pagination": {
                "page": result['page'],
                "per_page": result['per_page'],
                "total_pages": result['total_pages'],
                "has_next": result['has_next'],
                "has_prev": result['has_prev'],
            }
        })
    except Exception as e:
        logger.error(f"Error getting dead streams: {e}")
        return jsonify({"error": "Internal Server Error"}), 500

@app.route('/api/dead-streams/revive', methods=['POST'])
def revive_dead_stream():
    """Mark a stream as alive (remove from dead streams)."""
    try:
        data = request.json
        stream_url = data.get('stream_url')
        
        if not stream_url:
            return jsonify({"error": "stream_url is required"}), 400

        from apps.database.manager import get_db_manager
        db = get_db_manager()
        # Also keep the in-memory tracker in sync when the checker is running
        checker = get_stream_checker_service()
        if checker and checker.dead_streams_tracker:
            success = checker.dead_streams_tracker.mark_as_alive(stream_url)
        else:
            success = db.remove_dead_stream(stream_url)

        if success:
            return jsonify({"success": True, "message": "Stream marked as alive"})
        else:
            return jsonify({"error": "Failed to mark stream as alive"}), 500
    except Exception as e:
        logger.error(f"Error reviving dead stream: {e}")
        return jsonify({"error": "Internal Server Error"}), 500

@app.route('/api/dead-streams/clear', methods=['POST'])
def clear_all_dead_streams():
    """Clear all dead streams from the tracker."""
    try:
        from apps.database.manager import get_db_manager
        db = get_db_manager()
        dead_count = db.get_dead_streams_paginated(page=1, per_page=1)['total']

        checker = get_stream_checker_service()
        if checker and checker.dead_streams_tracker:
            checker.dead_streams_tracker.clear_all_dead_streams()
        else:
            db.clear_all_dead_streams()

        return jsonify({
            "success": True,
            "message": f"Cleared {dead_count} dead stream(s)",
            "cleared_count": dead_count
        })
    except Exception as e:
        logger.error(f"Error clearing dead streams: {e}")
        return jsonify({"error": "Internal Server Error"}), 500



# ==================== CHANNEL ORDER ENDPOINTS ====================

@app.route('/api/channel-order', methods=['GET'])
def get_channel_order():
    """Get current channel order configuration."""
    try:
        order_manager = get_channel_order_manager()
        order = order_manager.get_order()
        return jsonify({"order": order})
    except Exception as e:
        logger.error(f"Error getting channel order: {e}")
        return jsonify({"error": "Internal Server Error"}), 500

@app.route('/api/channel-order', methods=['PUT'])
def set_channel_order():
    """Set channel order configuration."""
    try:
        data = request.get_json()
        if not data or 'order' not in data:
            return jsonify({"error": "Missing 'order' field in request"}), 400
        
        order = data['order']
        if not isinstance(order, list):
            return jsonify({"error": "'order' must be a list of channel IDs"}), 400
        
        # Validate that all items are integers
        if not all(isinstance(item, int) for item in order):
            return jsonify({"error": "'order' must contain only integer channel IDs"}), 400
        
        order_manager = get_channel_order_manager()
        success = order_manager.set_order(order)
        
        if success:
            return jsonify({
                "message": "Channel order updated successfully",
                "order": order
            })
        else:
            return jsonify({"error": "Failed to update channel order"}), 500
    except Exception as e:
        logger.error(f"Error updating channel order: {e}")
        return jsonify({"error": "Internal Server Error"}), 500

@app.route('/api/channel-order', methods=['DELETE'])
def clear_channel_order():
    """Clear custom channel order (revert to default)."""
    try:
        order_manager = get_channel_order_manager()
        success = order_manager.clear_order()
        
        if success:
            return jsonify({"message": "Channel order cleared successfully"})
        else:
            return jsonify({"error": "Failed to clear channel order"}), 500
    except Exception as e:
        logger.error(f"Error clearing channel order: {e}")
        return jsonify({"error": "Internal Server Error"}), 500

@app.route('/api/discover-streams', methods=['POST'])
def discover_streams():
    """Trigger stream discovery and assignment (manual Quick Action)."""
    try:
        manager = get_automation_manager()
        # Use force=True to bypass feature flags for manual Quick Actions
        assignments = manager.discover_and_assign_streams(force=True)
        return jsonify({  # lgtm [py/stack-trace-exposure]
            "message": "Stream discovery completed",
            "assignments": assignments,
            "total_assigned": sum(assignments.values())
        })
    except Exception as e:
        logger.error(f"Error discovering streams: {e}")
        return jsonify({"error": "Internal Server Error"}), 500

@app.route('/api/refresh-playlist', methods=['POST'])
def refresh_playlist():
    """Trigger M3U playlist refresh (manual Quick Action)."""
    try:
        data = request.get_json() or {}
        account_id = data.get('account_id')
        
        manager = get_automation_manager()
        # Use force=True to bypass feature flags for manual Quick Actions
        success, _ = manager.refresh_playlists(force=True)
        
        if success:
            return jsonify({"message": "Playlist refresh completed successfully"})
        else:
            return jsonify({"error": "Playlist refresh failed"}), 500
    except Exception as e:
        logger.error(f"Error refreshing playlist: {e}")
        return jsonify({"error": "Internal Server Error"}), 500

@app.route('/api/m3u-accounts', methods=['GET'])
def get_m3u_accounts_endpoint():
    """Get all M3U accounts from Dispatcharr, filtering out 'custom' account if no custom streams exist and non-active accounts.
    
    (Priority is now handled per-profile).
    """
    try:
        from apps.core.api_utils import get_m3u_accounts, has_custom_streams
        
        # Get accounts from UDI
        accounts = get_m3u_accounts()
        if accounts is None:
            return jsonify({"error": "Failed to fetch M3U accounts"}), 500
        
        # Filter out non-active accounts per Dispatcharr API spec
        # Only show enabled/active playlists in the priority UI
        # Filter explicitly for is_active == True to avoid showing inactive accounts
        accounts = [acc for acc in accounts if acc.get('is_active') is True]
        
        # Check if there are any custom streams using efficient method
        has_custom = has_custom_streams()
        
        # Filter out "custom" M3U account if there are no custom streams
        if not has_custom:
            # Filter accounts by checking name only
            # Only filter accounts named "custom" (case-insensitive)
            # Do not filter based on null URLs as legitimate disabled/file-based accounts may have these
            accounts = [
                acc for acc in accounts 
                if acc.get('name', '').lower() != 'custom'
            ]
        
        return jsonify({
            "accounts": accounts,
            "global_priority_mode": "disabled" # Deprecated
        })
    except Exception as e:
        logger.error(f"Error fetching M3U accounts: {e}")
        return jsonify({"error": "Internal Server Error"}), 500





@app.route('/api/setup-wizard', methods=['GET'])
def get_setup_wizard_status():
    """Get setup wizard completion status."""
    try:
        from apps.database.manager import get_db_manager
        manager = get_automation_config_manager()
        db = get_db_manager()

        automation_config_exists = False
        try:
            # Accessing settings validates SQL-backed automation config availability.
            manager.get_global_settings()
            automation_config_exists = True
        except Exception:
            automation_config_exists = False

        regex_global_settings = db.get_system_setting('channel_regex_global_settings', None)
        regex_configs = db.get_all_channel_regex_configs()
        
        status = {
            "automation_config_exists": automation_config_exists,
            "regex_config_exists": regex_global_settings is not None,
            "has_patterns": bool(regex_configs),
            "has_channels": False,
            "dispatcharr_connection": False
        }
        
        # Check if we can connect to Dispatcharr
        # For testing purposes, simulate connection if running in test mode
        test_mode = os.getenv('TEST_MODE', 'false').lower() == 'true'
        
        if test_mode:
            # In test mode, simulate successful connection and channels
            status["dispatcharr_connection"] = True
            status["has_channels"] = True
        else:
            # Only check for channels if Dispatcharr is configured
            # This prevents unnecessary initialization attempts and error logs
            dispatcharr_config = get_dispatcharr_config()
            if dispatcharr_config.is_configured():
                try:
                    udi = get_udi_manager()
                    # Test connection with short timeout to prevent setup wizard timeouts
                    status["dispatcharr_connection"] = udi.fetcher.test_connection()
                    if status["dispatcharr_connection"]:
                        # Trigger background fetch if not initialized yet to allow async sync
                        if not udi.is_initialized():
                            import threading
                            threading.Thread(target=udi.initialize, kwargs={"force_refresh": False}, daemon=True).start()
                        # Avoid calling get_channels() synchronously here as it blocks
                        status["has_channels"] = bool(getattr(udi, '_channels_cache', []))
                except Exception as e:
                    logger.warning(f"Error checking Dispatcharr connection: {e}")
        
        # Setup is complete if Dispatcharr is configured
        # Note: We check is_configured() rather than active connection status
        # to prevent temporary downtime triggering the wizard view.
        status["setup_complete"] = get_dispatcharr_config().is_configured() or test_mode
        
        return jsonify(status)
    except Exception as e:
        logger.error(f"Error getting setup wizard status: {e}")
        return jsonify({"error": "Internal Server Error"}), 500

@app.route('/api/test-match-live', methods=['POST'])
def test_match_live():
    """
    Test stream matching against all available streams with full configuration 
    (Regex + TVG-ID + Priority).
    
    Used for the enhanced preview in Channel Configuration.
    """
    try:
        from apps.core.api_utils import get_streams, get_m3u_accounts
        import re
        
        data = request.get_json()
        if not data:
            return jsonify({"error": "Missing request body"}), 400
        
        # Configuration
        channel_name = data.get('channel_name', 'Unknown Channel')
        # match_by_tvg_id can be passed as explicit boolean
        match_by_tvg_id = data.get('match_by_tvg_id', False)
        tvg_id = data.get('tvg_id', None)
        
        regex_patterns = data.get('regex_patterns', [])
        # Normalization: ensure patterns are list of dicts
        normalized_patterns = []
        for p in regex_patterns:
            if isinstance(p, dict):
                normalized_patterns.append(p)
            elif isinstance(p, str):
                normalized_patterns.append({"pattern": p, "priority": 0})
        
        match_priority_order = data.get('match_priority_order', ['tvg', 'regex'])
        case_sensitive = data.get('case_sensitive', True)
        max_matches = data.get('max_matches', 100)
        
        # Get all streams
        all_streams = get_streams()
        if not all_streams:
            return jsonify({
                "matches": [],
                "total_streams": 0
            })
            
        # Get M3U accounts for mapping
        m3u_accounts_list = get_m3u_accounts() or []
        m3u_account_map = {acc.get('id'): acc.get('name', f'Account {acc.get("id")}') 
                          for acc in m3u_accounts_list if acc.get('id') is not None}

        # Whitespace regex (duplicated from automated_stream_manager.py)
        _WHITESPACE_PATTERN = re.compile(r'(?<!\\) +')

        matches = []
        
        for stream in all_streams:
            if not isinstance(stream, dict):
                continue
            
            stream_name = stream.get('name', '')
            stream_id = stream.get('id')
            
            if not stream_name:
                continue
                
            stream_tvg_id = stream.get('tvg_id')
            stream_m3u_account = stream.get('m3u_account')
            
            matched = False
            priority = 0
            match_source = "regex"
            matched_pattern = None
            
            # Check based on priority order
            for match_type in match_priority_order:
                if match_type == 'tvg':
                    if match_by_tvg_id and tvg_id and stream_tvg_id:
                        if stream_tvg_id == tvg_id:
                            matched = True
                            match_source = "tvg_id"
                            if match_priority_order[0] == 'tvg':
                                priority = 1000
                            else:
                                priority = 0
                            break
                            
                elif match_type == 'regex':
                    if matched: continue
                    
                    search_name = stream_name if case_sensitive else stream_name.lower()
                    
                    regex_matched = False
                    best_regex_priority = 0
                    best_pattern_str = None
                    
                    for pattern_obj in normalized_patterns:
                        pattern = pattern_obj.get("pattern", "")
                        pattern_m3u_accounts = pattern_obj.get("m3u_accounts")
                        pattern_priority = pattern_obj.get("priority", 0)
                        
                        if not pattern: continue
                        
                        # M3U Filter
                        if pattern_m3u_accounts and len(pattern_m3u_accounts) > 0:
                            if stream_m3u_account is None or stream_m3u_account not in pattern_m3u_accounts:
                                continue
                                
                        # Substitution
                        # Substitute CHANNEL_NAME variable
                        escaped_channel_name = re.escape(channel_name)
                        substituted_pattern = pattern.replace('CHANNEL_NAME', escaped_channel_name)
                        
                        search_pattern = substituted_pattern if case_sensitive else substituted_pattern.lower()
                        search_pattern = _WHITESPACE_PATTERN.sub(r'\\s+', search_pattern)
                        
                        try:
                            if is_dangerous_regex(search_pattern):
                                continue
                            if re.search(search_pattern, search_name):  # lgtm [py/regex-injection]
                                regex_matched = True
                                if pattern_priority >= best_regex_priority:
                                    best_regex_priority = pattern_priority
                                    best_pattern_str = pattern
                                # Matches logic in AutomatedStreamManager now finds BEST priority
                        except re.error:
                            continue
                            
                    if regex_matched:
                        matched = True
                        match_source = "regex"
                        priority = best_regex_priority
                        matched_pattern = best_pattern_str
                        break
            
            if matched:
                matches.append({
                    "stream_id": stream_id,
                    "stream_name": stream_name,
                    "stream_tvg_id": stream_tvg_id,
                    "m3u_account_name": m3u_account_map.get(stream_m3u_account),
                    "source": match_source,
                    "priority": priority,
                    "matched_pattern": matched_pattern
                })
                
                if len(matches) >= max_matches:
                    break
        
        return jsonify({
            "matches": matches,
            "total_tested_streams": len(all_streams),
            "total_matches": len(matches)
        })
        
    except Exception as e:
        logger.error(f"Error testing match live: {e}")
        return jsonify({"error": "Internal Server Error"}), 500

@app.route('/api/setup-wizard/ensure-config', methods=['POST'])
def ensure_wizard_config():
    """Ensure wizard configuration files exist (creates empty files if needed).
    
    This endpoint is called during wizard progression to ensure required config
    files exist, even if users skip optional steps like pattern configuration.
    """
    try:
        from apps.database.manager import get_db_manager

        manager = get_automation_config_manager()
        db = get_db_manager()

        # Ensure automation defaults exist in SQL-backed system settings.
        automation_defaults = {
            "regular_automation_enabled": False,
            "validate_existing_streams": False,
            "playlist_update_interval_minutes": {"type": "interval", "value": 5},
            "channel_assignments": {},
            "group_assignments": {},
            "channel_period_assignments": {},
        }
        for key, value in automation_defaults.items():
            if db.get_system_setting(key, None) is None:
                db.set_system_setting(key, value)

        # Touch manager to ensure SQL-backed config path is initialized.
        manager.get_global_settings()

        # Ensure regex global settings exist in SQL.
        if db.get_system_setting('channel_regex_global_settings', None) is None:
            db.set_system_setting('channel_regex_global_settings', {
                "case_sensitive": False,
                "require_exact_match": False,
            })

        return jsonify({"message": "Configuration defaults ensured in SQL"})
    except Exception as e:
        logger.error(f"Error ensuring wizard config: {e}")
        return jsonify({"error": "Internal Server Error"}), 500

@app.route('/api/setup-wizard/create-sample-patterns', methods=['POST'])
def create_sample_patterns():
    """Create sample regex patterns for testing setup completion."""
    try:
        from apps.database.manager import get_db_manager
        db = get_db_manager()

        # Add some sample patterns via SQL-backed regex config storage.
        patterns = {
            "patterns": {
                "1": {
                    "name": "News Channels",
                    "regex_patterns": [
                        {"pattern": ".*News.*", "priority": 0},
                        {"pattern": ".*CNN.*", "priority": 1},
                        {"pattern": ".*BBC.*", "priority": 2},
                    ],
                    "enabled": True
                },
                "2": {
                    "name": "Sports Channels", 
                    "regex_patterns": [
                        {"pattern": ".*Sport.*", "priority": 0},
                        {"pattern": ".*ESPN.*", "priority": 1},
                        {"pattern": ".*Fox Sports.*", "priority": 2},
                    ],
                    "enabled": True
                }
            },
            "global_settings": {
                "case_sensitive": False,
                "require_exact_match": False
            }
        }

        imported, errors = db.import_channel_regex_configs_from_json(patterns, merge=False)
        db.set_system_setting('channel_regex_global_settings', patterns['global_settings'])

        if errors:
            return jsonify({
                "message": "Sample patterns created with warnings",
                "imported": imported,
                "warnings": errors,
            }), 200

        return jsonify({"message": "Sample patterns created successfully", "imported": imported})
    except Exception as e:
        logger.error(f"Error creating sample patterns: {e}")
        return jsonify({"error": "Internal Server Error"}), 500

@app.route('/api/dispatcharr/config', methods=['GET'])
def get_dispatcharr_config_endpoint():
    """Get current Dispatcharr configuration (without exposing password)."""
    try:
        config_manager = get_dispatcharr_config()
        config = config_manager.get_config()
        return jsonify(config)
    except Exception as e:
        logger.error(f"Error getting Dispatcharr config: {e}")
        return jsonify({"error": "Internal Server Error"}), 500

@app.route('/api/dispatcharr/config', methods=['PUT'])
def update_dispatcharr_config_endpoint():
    """Update Dispatcharr configuration."""
    try:
        data = request.get_json()
        if not data:
            return jsonify({"error": "No configuration data provided"}), 400
        
        config_manager = get_dispatcharr_config()
        
        # Update configuration
        base_url = data.get('base_url')
        username = data.get('username')
        password = data.get('password')
        
        success = config_manager.update_config(
            base_url=base_url,
            username=username,
            password=password
        )
        
        if not success:
            return jsonify({"error": "Failed to save configuration"}), 500
        
        # Also update environment variables for backward compatibility
        # and immediate effect without restart
        if base_url is not None:
            os.environ["DISPATCHARR_BASE_URL"] = base_url.strip()
        if username is not None:
            os.environ["DISPATCHARR_USER"] = username.strip()
        if password is not None:
            os.environ["DISPATCHARR_PASS"] = password
        
        # Clear token when credentials change so we re-authenticate
        os.environ["DISPATCHARR_TOKEN"] = ""
        
        # Initialize UDI with the new configuration if all credentials are provided
        # This ensures data is fetched immediately after saving credentials
        if config_manager.is_configured():
            try:
                logger.info("Dispatcharr credentials updated, triggering background UDI Manager initialize...")
                udi = get_udi_manager()
                import threading
                threading.Thread(target=udi.initialize, kwargs={"force_refresh": True}, daemon=True).start()
                logger.info("UDI Manager initialization started in background")
            except Exception as e:
                logger.warning(
                    f"Failed to initialize UDI Manager after config update: {e}. "
                    f"Data may not be available until manual refresh or application restart."
                )
                # Don't fail the config update if UDI initialization fails
                # The UI will poll and detect if data is not loaded
        
        return jsonify({"message": "Dispatcharr configuration updated successfully"})
    except Exception as e:
        logger.error(f"Error updating Dispatcharr config: {e}")
        return jsonify({"error": "Internal Server Error"}), 500

@app.route('/api/dispatcharr/test-connection', methods=['POST'])
def test_dispatcharr_connection():
    """Test Dispatcharr connection with provided or existing credentials."""
    try:
        data = request.get_json() or {}
        
        # Temporarily use provided credentials if available, otherwise use existing
        from apps.config.dispatcharr_config import get_dispatcharr_config
        config_manager = get_dispatcharr_config()
        
        test_base_url = data.get('base_url') or config_manager.get_base_url()
        test_username = data.get('username') or config_manager.get_username()
        test_password = data.get('password') or config_manager.get_password()
        
        if not all([test_base_url, test_username, test_password]):
            return jsonify({
                "success": False,
                "error": "Missing required credentials (base_url, username, password)"
            }), 400
        
        # Test login
        import requests
        login_url = f"{test_base_url}/api/accounts/token/"
        
        try:
            resp = requests.post(
                login_url,
                headers={"Content-Type": "application/json"},
                json={"username": test_username, "password": test_password},
                timeout=10
            )
            resp.raise_for_status()
            data = resp.json()
            token = data.get("access") or data.get("token")
            
            if token:
                # Test if we can fetch channels
                channels_url = f"{test_base_url}/api/channels/channels/"
                channels_resp = requests.get(
                    channels_url,
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Accept": "application/json"
                    },
                    params={'page_size': 1},
                    timeout=10
                )
                
                if channels_resp.status_code == 200:
                    return jsonify({
                        "success": True,
                        "message": "Connection successful"
                    })
                else:
                    return jsonify({
                        "success": False,
                        "error": "Authentication successful but failed to fetch channels"
                    }), 400
            else:
                return jsonify({
                    "success": False,
                    "error": "No token received from Dispatcharr"
                }), 400
        except requests.exceptions.Timeout:
            return jsonify({
                "success": False,
                "error": "Connection timeout. Please check the URL and network connectivity."
            }), 400
        except requests.exceptions.ConnectionError:
            return jsonify({
                "success": False,
                "error": "Could not connect to Dispatcharr. Please check the URL."
            }), 400
        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 401:
                return jsonify({
                    "success": False,
                    "error": "Invalid username or password"
                }), 401
            else:
                return jsonify({
                    "success": False,
                    "error": f"HTTP error: {e.response.status_code}"
                }), 400
        except Exception as e:
            return jsonify({  # lgtm [py/stack-trace-exposure]
                "success": False,
                "error": f"Connection failed: {str(e)}"
            }), 400
            
    except Exception as e:
        logger.error(f"Error testing Dispatcharr connection: {e}")
        return jsonify({"error": "Internal Server Error"}), 500

@app.route('/api/dispatcharr/initialization-status', methods=['GET'])
def get_udi_initialization_status():
    """Get the current UDI initialization progress."""
    try:
        udi = get_udi_manager()
        progress = udi.get_init_progress()
        return jsonify(progress)
    except Exception as e:
        logger.error(f"Error getting UDI initialization status: {e}")
        return jsonify({"error": "Internal Server Error"}), 500

@app.route('/api/dispatcharr/initialize-udi', methods=['POST'])
def initialize_udi():
    """Initialize UDI Manager with current Dispatcharr credentials.
    
    This endpoint should be called after successfully testing the Dispatcharr
    connection to ensure the UDI Manager is initialized with fresh data from
    the Dispatcharr API.
    """
    try:
        config_manager = get_dispatcharr_config()
        
        # Check if Dispatcharr is configured
        if not config_manager.is_configured():
            return jsonify({
                "success": False,
                "error": "Dispatcharr is not fully configured. Please provide base_url, username, and password."
            }), 400
        
        logger.info("Initializing UDI Manager with fresh data from Dispatcharr...")
        
        udi = get_udi_manager()
        
        # Force refresh to fetch fresh data from API
        success = udi.initialize(force_refresh=True)
        
        if success:
            # Get counts to report back
            channels = udi.get_channels()
            streams = udi.get_streams()
            m3u_accounts = udi.get_m3u_accounts()
            
            logger.info(f"UDI Manager initialized successfully: {len(channels)} channels, {len(streams)} streams, {len(m3u_accounts)} M3U accounts")
            
            return jsonify({
                "success": True,
                "message": "UDI Manager initialized successfully",
                "data": {
                    "channels_count": len(channels),
                    "streams_count": len(streams),
                    "m3u_accounts_count": len(m3u_accounts)
                }
            })
        else:
            logger.error("Failed to initialize UDI Manager")
            return jsonify({
                "success": False,
                "error": "Failed to initialize UDI Manager. Please check the logs for details."
            }), 500
            
    except Exception as e:
        logger.error(f"Error initializing UDI Manager: {e}", exc_info=True)
        return jsonify({  # lgtm [py/stack-trace-exposure]
            "success": False,
            "error": str(e)
        }), 500

# ===== Stream Checker Endpoints =====

@app.route('/api/stream-checker/status', methods=['GET'])
def get_stream_checker_status():
    """Get current stream checker status."""
    try:
        service = get_stream_checker_service()
        status = service.get_status()
        
        # Add parallel checking information
        concurrent_enabled = service.config.get(CONCURRENT_STREAMS_ENABLED_KEY, True)
        global_limit = service.config.get(CONCURRENT_STREAMS_GLOBAL_LIMIT_KEY, 10)
        
        status['parallel'] = {
            'enabled': concurrent_enabled,
            'max_workers': global_limit,
            'mode': 'parallel' if concurrent_enabled else 'sequential'
        }
        
        return jsonify(status)
    except Exception as e:
        logger.error(f"Error getting stream checker status: {e}")
        return jsonify({"error": "Internal Server Error"}), 500

@app.route('/api/stream-checker/start', methods=['POST'])
def start_stream_checker():
    """Start the stream checker service."""
    try:
        service = get_stream_checker_service()
        service.start()
        return jsonify({"message": "Stream checker started successfully", "status": "running"})
    except Exception as e:
        logger.error(f"Error starting stream checker: {e}")
        return jsonify({"error": "Internal Server Error"}), 500

@app.route('/api/stream-checker/stop', methods=['POST'])
def stop_stream_checker():
    """Stop the stream checker service."""
    try:
        service = get_stream_checker_service()
        service.stop()
        return jsonify({"message": "Stream checker stopped successfully", "status": "stopped"})
    except Exception as e:
        logger.error(f"Error stopping stream checker: {e}")
        return jsonify({"error": "Internal Server Error"}), 500

@app.route('/api/stream-checker/queue', methods=['GET'])
def get_stream_checker_queue():
    """Get current queue status."""
    try:
        service = get_stream_checker_service()
        status = service.get_status()
        return jsonify(status.get('queue', {}))
    except Exception as e:
        logger.error(f"Error getting stream checker queue: {e}")
        return jsonify({"error": "Internal Server Error"}), 500

@app.route('/api/stream-checker/queue/add', methods=['POST'])
def add_to_stream_checker_queue():
    """Add channel(s) to the checking queue."""
    try:
        data = request.get_json()
        if not data:
            return jsonify({"error": "No data provided"}), 400
        
        service = get_stream_checker_service()
        force_check = data.get('force_check', False)
        
        # Handle single channel or multiple channels
        if 'channel_id' in data:
            channel_id = data['channel_id']
            priority = data.get('priority', 10)
            success = service.queue_channel(channel_id, priority, force_check=force_check)
            if success:
                return jsonify({"message": f"Channel {channel_id} queued successfully"})
            else:
                return jsonify({"error": "Failed to queue channel"}), 500
        
        elif 'channel_ids' in data:
            channel_ids = data['channel_ids']
            priority = data.get('priority', 10)
            added = service.queue_channels(channel_ids, priority, force_check=force_check)
            return jsonify({"message": f"Queued {added} channels successfully", "added": added})
        
        else:
            return jsonify({"error": "Must provide channel_id or channel_ids"}), 400
    
    except Exception as e:
        logger.error(f"Error adding to stream checker queue: {e}")
        return jsonify({"error": "Internal Server Error"}), 500

@app.route('/api/stream-checker/queue/clear', methods=['POST'])
def clear_stream_checker_queue():
    """Clear the checking queue."""
    try:
        service = get_stream_checker_service()
        service.clear_queue()
        return jsonify({"message": "Queue cleared successfully"})
    except Exception as e:
        logger.error(f"Error clearing stream checker queue: {e}")
        return jsonify({"error": "Internal Server Error"}), 500

@app.route('/api/stream-checker/config', methods=['GET'])
def get_stream_checker_config():
    """Get stream checker configuration."""
    try:
        service = get_stream_checker_service()
        return jsonify(service.config.config)
    except Exception as e:
        logger.error(f"Error getting stream checker config: {e}")
        return jsonify({"error": "Internal Server Error"}), 500

@app.route('/api/stream-checker/config', methods=['PUT'])
def update_stream_checker_config():
    """Update stream checker configuration."""
    try:
        data = request.get_json()
        if not data:
            return jsonify({"error": "No configuration data provided"}), 400
        
        # Validate cron expression if provided
        if 'global_check_schedule' in data and 'cron_expression' in data['global_check_schedule']:
            cron_expr = data['global_check_schedule']['cron_expression']
            if cron_expr:
                if CRONITER_AVAILABLE:
                    try:
                        if not croniter.is_valid(cron_expr):
                            return jsonify({"error": f"Invalid cron expression: {cron_expr}"}), 400
                    except Exception as e:
                        # Log the full error but only return a generic message to the user
                        logger.error(f"Cron expression validation error: {e}")
                        return jsonify({"error": "Invalid cron expression format"}), 400
                else:
                    logger.warning("croniter not available - cron expression validation skipped")
        
        service = get_stream_checker_service()
        service.update_config(data)
        
        # Auto-start or stop services based on automation_controls when wizard is complete
        if 'automation_controls' in data and check_wizard_complete():
            automation_controls = data['automation_controls']
            manager = get_automation_manager()
            automation_config = get_automation_config_manager()
            
            # Check the master switch for regular automation
            global_settings = automation_config.get_global_settings()
            regular_automation_enabled = global_settings.get('regular_automation_enabled', False)
            
            # Check if any automation is enabled
            any_automation_enabled = (
                automation_controls.get('auto_m3u_updates', False) or
                automation_controls.get('auto_stream_matching', False) or
                automation_controls.get('auto_quality_checking', False) or
                automation_controls.get('scheduled_global_action', False)
            )
            
            if not any_automation_enabled:
                # Stop services if all automation is disabled
                if service.running:
                    service.stop()
                    logger.info("Stream checker service stopped (all automation disabled)")
                if manager.automation_running:
                    manager.stop_automation()
                    logger.info("Automation service stopped (all automation disabled)")
                # Stop background processors
                stop_scheduled_event_processor()
                stop_epg_refresh_processor()
            else:
                # Start services if automation is enabled and they're not already running
                # But respect the regular_automation_enabled master switch for automation service
                if not service.running:
                    service.start()
                    logger.info(f"Stream checker service auto-started after config update")
                
                # Only start automation service if regular automation is enabled
                if regular_automation_enabled and not manager.automation_running:
                    manager.start_automation()
                    logger.info(f"Automation service auto-started after config update")
                elif not regular_automation_enabled and manager.automation_running:
                    # Don't auto-start if master switch is off, but also don't stop it
                    # (user might have it on for testing, we only stop via the master switch)
                    pass
                
                # Start background processors if not running
                if not (scheduled_event_processor_thread and scheduled_event_processor_thread.is_alive()):
                    start_scheduled_event_processor()
                    logger.info("Scheduled event processor auto-started after config update")
                if not (epg_refresh_thread and epg_refresh_thread.is_alive()):
                    start_epg_refresh_processor()
                    logger.info("EPG refresh processor auto-started after config update")
        
        return jsonify({"message": "Configuration updated successfully", "config": service.config.config})
    except Exception as e:
        logger.error(f"Error updating stream checker config: {e}")
        return jsonify({"error": "Internal Server Error"}), 500

@app.route('/api/stream-checker/progress', methods=['GET'])
def get_stream_checker_progress():
    """Get current checking progress."""
    try:
        service = get_stream_checker_service()
        status = service.get_status()
        return jsonify(status.get('progress', {}))
    except Exception as e:
        logger.error(f"Error getting stream checker progress: {e}")
        return jsonify({"error": "Internal Server Error"}), 500

@app.route('/api/stream-checker/check-channel', methods=['POST'])
def check_specific_channel():
    """Manually check a specific channel immediately (add to queue with high priority)."""
    try:
        data = request.get_json()
        if not data or 'channel_id' not in data:
            return jsonify({"error": "channel_id required"}), 400
        
        channel_id = data['channel_id']
        service = get_stream_checker_service()
        
        # Add with highest priority
        success = service.queue_channel(channel_id, priority=100)
        if success:
            return jsonify({"message": f"Channel {channel_id} queued for immediate checking"})
        else:
            return jsonify({"error": "Failed to queue channel"}), 500
    
    except Exception as e:
        logger.error(f"Error checking specific channel: {e}")
        return jsonify({"error": "Internal Server Error"}), 500

@app.route('/api/stream-checker/check-single-channel', methods=['POST'])
def check_single_channel_now():
    """Immediately check a single channel synchronously and return results."""
    try:
        data = request.get_json()
        if not data or 'channel_id' not in data:
            return jsonify({"error": "channel_id required"}), 400
        
        channel_id = data['channel_id']
        service = get_stream_checker_service()
        
        # Perform synchronous check
        result = service.check_single_channel(channel_id)
        
        if result.get('success'):
            return jsonify(result)  # lgtm [py/stack-trace-exposure]
        else:
            return jsonify(result), 500  # lgtm [py/stack-trace-exposure]
    
    except Exception as e:
        logger.error(f"Error checking single channel: {e}")
        return jsonify({"error": "Internal Server Error"}), 500

@app.route('/api/stream-checker/mark-updated', methods=['POST'])
def mark_channels_updated():
    """Mark channels as updated (triggered by M3U refresh)."""
    try:
        data = request.get_json()
        if not data:
            return jsonify({"error": "No data provided"}), 400
        
        service = get_stream_checker_service()
        
        if 'channel_id' in data:
            channel_id = data['channel_id']
            service.update_tracker.mark_channel_updated(channel_id)
            return jsonify({"message": f"Channel {channel_id} marked as updated"})
        
        elif 'channel_ids' in data:
            channel_ids = data['channel_ids']
            service.update_tracker.mark_channels_updated(channel_ids)
            return jsonify({"message": f"Marked {len(channel_ids)} channels as updated"})
        
        else:
            return jsonify({"error": "Must provide channel_id or channel_ids"}), 400
    
    except Exception as e:
        logger.error(f"Error marking channels updated: {e}")
        return jsonify({"error": "Internal Server Error"}), 500

@app.route('/api/stream-checker/queue-all', methods=['POST'])
def queue_all_channels():
    """Queue all channels for checking (manual trigger for full check)."""
    try:
        service = get_stream_checker_service()
        
        # Fetch all channels from UDI
        udi = get_udi_manager()
        channels = udi.get_channels()
        
        if not channels:
            return jsonify({"error": "Could not fetch channels"}), 500
        
        channel_ids = [ch['id'] for ch in channels if isinstance(ch, dict) and 'id' in ch]
        
        if not channel_ids:
            return jsonify({"message": "No channels found to queue", "count": 0})
        
        # Mark all channels as updated and add to queue
        service.update_tracker.mark_channels_updated(channel_ids)
        added = service.check_queue.add_channels(channel_ids, priority=10)
        
        return jsonify({
            "message": f"Queued {added} channels for checking",
            "total_channels": len(channel_ids),
            "queued": added
        })
    
    except Exception as e:
        logger.error(f"Error queueing all channels: {e}")
        return jsonify({"error": "Internal Server Error"}), 500


# ============================================================================
# Scheduling API Endpoints
# ============================================================================

@app.route('/api/scheduling/config', methods=['GET'])
@log_function_call
def get_scheduling_config():
    """Get scheduling configuration including EPG refresh interval."""
    try:
        from apps.automation.scheduling_service import get_scheduling_service
        service = get_scheduling_service()
        config = service.get_config()
        return jsonify(config)
    except Exception as e:
        logger.error(f"Error getting scheduling config: {e}")
        return jsonify({"error": "Internal Server Error"}), 500

@app.route('/api/scheduling/config', methods=['PUT'])
@log_function_call
def update_scheduling_config():
    """Update scheduling configuration.
    
    Expected JSON body:
    {
        "epg_refresh_interval_minutes": 60,
        "enabled": true
    }
    """
    try:
        from apps.automation.scheduling_service import get_scheduling_service
        service = get_scheduling_service()
        config = request.get_json()
        
        if not config:
            return jsonify({"error": "No configuration provided"}), 400
        
        success = service.update_config(config)
        
        if success:
            return jsonify({"message": "Configuration updated", "config": service.get_config()})
        else:
            return jsonify({"error": "Failed to save configuration"}), 500
    
    except Exception as e:
        logger.error(f"Error updating scheduling config: {e}")
        return jsonify({"error": "Internal Server Error"}), 500

@app.route('/api/scheduling/epg/grid', methods=['GET'])
@log_function_call
def get_epg_grid():
    """Get EPG grid data (all programs for next 24 hours).
    
    Query parameters:
    - force_refresh: If true, bypass cache and fetch fresh data
    """
    try:
        from apps.automation.scheduling_service import get_scheduling_service
        service = get_scheduling_service()
        force_refresh = request.args.get('force_refresh', 'false').lower() == 'true'
        
        programs = service.fetch_epg_grid(force_refresh=force_refresh)
        return jsonify(programs)
    
    except Exception as e:
        logger.error(f"Error fetching EPG grid: {e}")
        return jsonify({"error": "Internal Server Error"}), 500

@app.route('/api/scheduling/epg/channel/<int:channel_id>', methods=['GET'])
@log_function_call
def get_channel_programs(channel_id):
    """Get programs for a specific channel.
    
    Args:
        channel_id: Channel ID
    
    Returns:
        List of programs for the channel
    """
    try:
        from apps.automation.scheduling_service import get_scheduling_service
        service = get_scheduling_service()
        
        programs = service.get_programs_by_channel(channel_id)
        return jsonify(programs)
    
    except Exception as e:
        logger.error(f"Error fetching programs for channel {channel_id}: {e}")
        return jsonify({"error": "Internal Server Error"}), 500

@app.route('/api/scheduling/events', methods=['GET'])
@log_function_call
def get_scheduled_events():
    """Get all scheduled events."""
    try:
        from apps.automation.scheduling_service import get_scheduling_service
        service = get_scheduling_service()
        events = service.get_scheduled_events()
        return jsonify(events)
    except Exception as e:
        logger.error(f"Error getting scheduled events: {e}")
        return jsonify({"error": "Internal Server Error"}), 500

@app.route('/api/scheduling/events', methods=['POST'])
@log_function_call
def create_scheduled_event():
    """Create a new scheduled event.
    
    Expected JSON body:
    {
        "channel_id": 123,
        "program_start_time": "2024-01-01T10:00:00Z",
        "program_end_time": "2024-01-01T11:00:00Z",
        "program_title": "Program Name",
        "minutes_before": 5
    }
    """
    try:
        from apps.automation.scheduling_service import get_scheduling_service
        service = get_scheduling_service()
        event_data = request.get_json()
        
        if not event_data:
            return jsonify({"error": "No event data provided"}), 400
        
        # Validate required fields
        required_fields = ['channel_id', 'program_start_time', 'program_end_time', 'program_title']
        for field in required_fields:
            if field not in event_data:
                return jsonify({"error": f"Missing required field: {field}"}), 400
        
        event = service.create_scheduled_event(event_data)
        
        # Wake up the processor to check for new events immediately
        if scheduled_event_processor_wake:
            scheduled_event_processor_wake.set()
        
        return jsonify(event), 201
    
    except ValueError as e:
        return jsonify({"error": "Invalid value or request parameters"}), 400
    except Exception as e:
        logger.error(f"Error creating scheduled event: {e}")
        return jsonify({"error": "Internal Server Error"}), 500

@app.route('/api/scheduling/events/<event_id>', methods=['DELETE'])
@log_function_call
def delete_scheduled_event(event_id):
    """Delete a scheduled event.
    
    Args:
        event_id: Event ID
    """
    try:
        from apps.automation.scheduling_service import get_scheduling_service
        service = get_scheduling_service()
        
        success = service.delete_scheduled_event(event_id)
        
        if success:
            return jsonify({"message": "Event deleted"}), 200
        else:
            return jsonify({"error": "Event not found"}), 404
    
    except Exception as e:
        logger.error(f"Error deleting scheduled event: {e}")
        return jsonify({"error": "Internal Server Error"}), 500


@app.route('/api/scheduling/auto-create-rules', methods=['GET'])
@log_function_call
def get_auto_create_rules():
    """Get all auto-create rules."""
    try:
        from apps.automation.scheduling_service import get_scheduling_service
        service = get_scheduling_service()
        rules = service.get_auto_create_rules()
        return jsonify(rules)
    except Exception as e:
        logger.error(f"Error getting auto-create rules: {e}")
        return jsonify({"error": "Internal Server Error"}), 500


@app.route('/api/scheduling/auto-create-rules', methods=['POST'])
@log_function_call
def create_auto_create_rule():
    """Create a new auto-create rule.
    
    Expected JSON body:
    {
        "name": "Rule Name",
        "channel_ids": [123, 456],  // or "channel_id": 123 for backward compatibility
        "regex_pattern": "^Breaking News",
        "minutes_before": 5,
        "enable_looping_detection": true,
        "enable_logo_detection": true
    }
    """
    try:
        from apps.automation.scheduling_service import get_scheduling_service
        service = get_scheduling_service()
        rule_data = request.get_json()
        
        if not rule_data:
            return jsonify({"error": "No rule data provided"}), 400
        
        # Validate required fields - accept either channel_id or channel_ids
        required_fields = ['name', 'regex_pattern']
        for field in required_fields:
            if field not in rule_data:
                return jsonify({"error": f"Missing required field: {field}"}), 400
        
        # Validate that either channel_id or channel_ids is provided
        if 'channel_id' not in rule_data and 'channel_ids' not in rule_data:
            return jsonify({"error": "Missing required field: channel_id or channel_ids"}), 400
        
        rule = service.create_auto_create_rule(rule_data)
        
        # Immediately match programs to the new rule
        try:
            service.match_programs_to_rules()
            logger.info("Triggered immediate program matching after creating auto-create rule")
        except Exception as e:
            logger.warning(f"Failed to immediately match programs to new rule: {e}")
        
        # Wake up the processor to check for new events immediately
        if scheduled_event_processor_wake:
            scheduled_event_processor_wake.set()
        
        return jsonify(rule), 201
    
    except ValueError as e:
        return jsonify({"error": "Invalid value or request parameters"}), 400
    except Exception as e:
        logger.error(f"Error creating auto-create rule: {e}")
        return jsonify({"error": "Internal Server Error"}), 500


@app.route('/api/scheduling/auto-create-rules/<rule_id>', methods=['DELETE'])
@log_function_call
def delete_auto_create_rule(rule_id):
    """Delete an auto-create rule.
    
    Args:
        rule_id: Rule ID
    """
    try:
        from apps.automation.scheduling_service import get_scheduling_service
        service = get_scheduling_service()
        
        success = service.delete_auto_create_rule(rule_id)
        
        if success:
            return jsonify({"message": "Rule deleted"}), 200
        else:
            return jsonify({"error": "Rule not found"}), 404
    
    except Exception as e:
        logger.error(f"Error deleting auto-create rule: {e}")
        return jsonify({"error": "Internal Server Error"}), 500


@app.route('/api/scheduling/auto-create-rules/<rule_id>', methods=['PUT', 'PATCH'])
@log_function_call
def update_auto_create_rule(rule_id):
    """Update an auto-create rule.
    
    Args:
        rule_id: Rule ID
        
    Expected JSON body (all fields optional):
    {
        "name": "Updated Rule Name",
        "channel_id": 123,
        "regex_pattern": "^Updated Pattern",
        "minutes_before": 10,
        "enable_looping_detection": false,
        "enable_logo_detection": false
    }
    """
    try:
        from apps.automation.scheduling_service import get_scheduling_service
        service = get_scheduling_service()
        rule_data = request.get_json()
        
        if not rule_data:
            return jsonify({"error": "No rule data provided"}), 400
        
        updated_rule = service.update_auto_create_rule(rule_id, rule_data)
        
        if updated_rule:
            # Immediately match programs to the updated rule
            try:
                service.match_programs_to_rules()
                logger.info("Triggered immediate program matching after updating auto-create rule")
            except Exception as e:
                logger.warning(f"Failed to immediately match programs to updated rule: {e}")
            
            # Wake up the processor to check for new events immediately
            if scheduled_event_processor_wake:
                scheduled_event_processor_wake.set()
            
            return jsonify(updated_rule), 200
        else:
            return jsonify({"error": "Rule not found"}), 404
    
    except ValueError as e:
        return jsonify({"error": "Invalid value or request parameters"}), 400
    except Exception as e:
        logger.error(f"Error updating auto-create rule: {e}")
        return jsonify({"error": "Internal Server Error"}), 500


@app.route('/api/scheduling/auto-create-rules/test', methods=['POST'])
@log_function_call
def test_auto_create_rule():
    """Test a regex pattern against EPG programs for a channel.
    
    Expected JSON body:
    {
        "channel_id": 123,
        "regex_pattern": "^Breaking News"
    }
    """
    try:
        from apps.automation.scheduling_service import get_scheduling_service
        service = get_scheduling_service()
        test_data = request.get_json()
        
        if not test_data:
            return jsonify({"error": "No test data provided"}), 400
        
        # Validate required fields
        required_fields = ['channel_id', 'regex_pattern']
        for field in required_fields:
            if field not in test_data:
                return jsonify({"error": f"Missing required field: {field}"}), 400
        
        matching_programs = service.test_regex_against_epg(
            test_data['channel_id'],
            test_data['regex_pattern']
        )
        
        return jsonify({
            "matches": len(matching_programs),
            "programs": matching_programs
        })
    
    except ValueError as e:
        return jsonify({"error": "Invalid value or request parameters"}), 400
    except Exception as e:
        logger.error(f"Error testing auto-create rule: {e}")
        return jsonify({"error": "Internal Server Error"}), 500


@app.route('/api/scheduling/auto-create-rules/export', methods=['GET'])
@log_function_call
def export_auto_create_rules():
    """Export all auto-create rules as JSON.
    
    Returns:
        JSON array of auto-create rules
    """
    try:
        from apps.automation.scheduling_service import get_scheduling_service
        service = get_scheduling_service()
        rules = service.export_auto_create_rules()
        return jsonify(rules), 200
    except Exception as e:
        logger.error(f"Error exporting auto-create rules: {e}")
        return jsonify({"error": "Internal Server Error"}), 500


@app.route('/api/scheduling/auto-create-rules/import', methods=['POST'])
@log_function_call
def import_auto_create_rules():
    """Import auto-create rules from JSON.
    
    Expected JSON body:
    [
        {
            "name": "Rule Name",
            "channel_ids": [123, 456],
            "regex_pattern": "^Breaking News",
            "minutes_before": 5
        },
        ...
    ]
    
    Returns:
        JSON with import results
    """
    try:
        from apps.automation.scheduling_service import get_scheduling_service
        service = get_scheduling_service()
        rules_data = request.get_json()
        
        if not rules_data:
            return jsonify({"error": "No rules data provided"}), 400
        
        if not isinstance(rules_data, list):
            return jsonify({"error": "Rules data must be an array"}), 400
        
        result = service.import_auto_create_rules(rules_data)
        
        # Wake up the processor to check for new events immediately
        if scheduled_event_processor_wake:
            scheduled_event_processor_wake.set()
        
        # Return 200 even if some rules failed - the response contains details
        return jsonify(result), 200  # lgtm [py/stack-trace-exposure]
    
    except ValueError as e:
        return jsonify({"error": "Invalid value or request parameters"}), 400
    except Exception as e:
        logger.error(f"Error importing auto-create rules: {e}")
        return jsonify({"error": "Internal Server Error"}), 500


@app.route('/api/scheduling/process-due-events', methods=['POST'])
@log_function_call
def process_due_scheduled_events():
    """Process all scheduled events that are due for execution.
    
    This endpoint should be called periodically (e.g., by a cron job or scheduler)
    to check for and execute any scheduled channel checks.
    
    Returns:
        JSON with execution results
    """
    try:
        from apps.automation.scheduling_service import get_scheduling_service
        service = get_scheduling_service()
        stream_checker = get_stream_checker_service()
        
        # Get all due events
        due_events = service.get_due_events()
        
        if not due_events:
            return jsonify({
                "message": "No events due for execution",
                "processed": 0
            }), 200
        
        results = []
        for event in due_events:
            event_id = event.get('id')
            channel_name = event.get('channel_name', 'Unknown')
            program_title = event.get('program_title', 'Unknown')
            
            logger.info(f"Processing due event {event_id} for {channel_name} (program: {program_title})")
            
            success = service.execute_scheduled_check(event_id, stream_checker)
            results.append({
                'event_id': event_id,
                'channel_name': channel_name,
                'program_title': program_title,
                'success': success
            })
        
        successful = sum(1 for r in results if r['success'])
        
        return jsonify({
            "message": f"Processed {len(results)} event(s), {successful} successful",
            "processed": len(results),
            "successful": successful,
            "results": results
        }), 200
    
    except Exception as e:
        logger.error(f"Error processing due scheduled events: {e}", exc_info=True)
        return jsonify({"error": "Internal Server Error"}), 500


@app.route('/api/scheduling/processor/status', methods=['GET'])
@log_function_call
def get_scheduled_event_processor_status():
    """Get the status of the scheduled event processor background thread.
    
    Returns:
        JSON with processor status
    """
    try:
        global scheduled_event_processor_thread, scheduled_event_processor_running
        
        thread_alive = scheduled_event_processor_thread is not None and scheduled_event_processor_thread.is_alive()
        is_running = thread_alive and scheduled_event_processor_running
        
        return jsonify({
            "running": is_running,
            "thread_alive": thread_alive
        }), 200
    
    except Exception as e:
        logger.error(f"Error getting scheduled event processor status: {e}")
        return jsonify({"error": "Internal Server Error"}), 500


@app.route('/api/scheduling/processor/start', methods=['POST'])
@log_function_call
def start_scheduled_event_processor_api():
    """Start the scheduled event processor background thread.
    
    Returns:
        JSON with result
    """
    try:
        success = start_scheduled_event_processor()
        
        if success:
            return jsonify({"message": "Scheduled event processor started"}), 200
        else:
            return jsonify({"message": "Scheduled event processor is already running"}), 200
    
    except Exception as e:
        logger.error(f"Error starting scheduled event processor: {e}")
        return jsonify({"error": "Internal Server Error"}), 500


@app.route('/api/scheduling/processor/stop', methods=['POST'])
@log_function_call
def stop_scheduled_event_processor_api():
    """Stop the scheduled event processor background thread.
    
    Returns:
        JSON with result
    """
    try:
        success = stop_scheduled_event_processor()
        
        if success:
            return jsonify({"message": "Scheduled event processor stopped"}), 200
        else:
            return jsonify({"message": "Scheduled event processor is not running"}), 200
    
    except Exception as e:
        logger.error(f"Error stopping scheduled event processor: {e}")
        return jsonify({"error": "Internal Server Error"}), 500


@app.route('/api/scheduling/epg-refresh/status', methods=['GET'])
@log_function_call
def get_epg_refresh_processor_status():
    """Get the status of the EPG refresh processor background thread.
    
    Returns:
        JSON with processor status
    """
    try:
        global epg_refresh_thread, epg_refresh_running
        
        thread_alive = epg_refresh_thread is not None and epg_refresh_thread.is_alive()
        is_running = thread_alive and epg_refresh_running
        
        return jsonify({
            "running": is_running,
            "thread_alive": thread_alive
        }), 200
    
    except Exception as e:
        logger.error(f"Error getting EPG refresh processor status: {e}")
        return jsonify({"error": "Internal Server Error"}), 500


@app.route('/api/scheduling/epg-refresh/start', methods=['POST'])
@log_function_call
def start_epg_refresh_processor_api():
    """Start the EPG refresh processor background thread.
    
    Returns:
        JSON with result
    """
    try:
        success = start_epg_refresh_processor()
        
        if success:
            return jsonify({"message": "EPG refresh processor started"}), 200
        else:
            return jsonify({"message": "EPG refresh processor is already running"}), 200
    
    except Exception as e:
        logger.error(f"Error starting EPG refresh processor: {e}")
        return jsonify({"error": "Internal Server Error"}), 500


@app.route('/api/scheduling/epg-refresh/stop', methods=['POST'])
@log_function_call
def stop_epg_refresh_processor_api():
    """Stop the EPG refresh processor background thread.
    
    Returns:
        JSON with result
    """
    try:
        success = stop_epg_refresh_processor()
        
        if success:
            return jsonify({"message": "EPG refresh processor stopped"}), 200
        else:
            return jsonify({"message": "EPG refresh processor is not running"}), 200
    
    except Exception as e:
        logger.error(f"Error stopping EPG refresh processor: {e}")
        return jsonify({"error": "Internal Server Error"}), 500


@app.route('/api/scheduling/epg-refresh/trigger', methods=['POST'])
@log_function_call
def trigger_epg_refresh():
    """Manually trigger an immediate EPG refresh.
    
    Returns:
        JSON with result
    """
    try:
        global epg_refresh_wake, epg_refresh_running, epg_refresh_thread
        
        # Validate that the processor is actually running
        if epg_refresh_wake and epg_refresh_running and epg_refresh_thread and epg_refresh_thread.is_alive():
            epg_refresh_wake.set()
            return jsonify({"message": "EPG refresh triggered"}), 200
        else:
            return jsonify({"error": "EPG refresh processor is not running"}), 400
    
    except Exception as e:
        logger.error(f"Error triggering EPG refresh: {e}")
        return jsonify({"error": "Internal Server Error"}), 500

# ==================== Automation Service API ====================

@app.route('/api/automation/status', methods=['GET'])
@log_function_call
def get_automation_status():
    """Get the status of the automation background service.
    
    Returns:
        JSON with service status
    """
    try:
        manager = get_automation_manager()
        
        # Check thread status
        thread_alive = manager.automation_thread is not None and manager.automation_thread.is_alive()
        is_running = thread_alive and manager.automation_running
        
        # Aggregate logic for new Dashboard UI readouts
        from apps.automation.automation_config_manager import get_automation_config_manager
        config_manager = get_automation_config_manager()
        profiles = config_manager.get_all_profiles()
        
        profiles_count = len(profiles)
        # Verify if ANY profile has stream checking currently toggled 'on'
        stream_checking_enabled = any(p.get("stream_checking", {}).get("enabled", False) for p in profiles)
        
        return jsonify({
            "running": is_running,
            "thread_alive": thread_alive,
            "last_playlist_update": manager.last_playlist_update.isoformat() if manager.last_playlist_update else None,
            "profiles_count": profiles_count,
            "stream_checking_enabled": stream_checking_enabled
        }), 200
    
    except Exception as e:
        logger.error(f"Error getting automation status: {e}")
        return jsonify({"error": "Internal Server Error"}), 500


@app.route('/api/automation/start', methods=['POST'])
@log_function_call
def start_automation_service_api():
    """Start the automation background service.
    
    Returns:
        JSON with result
    """
    try:
        manager = get_automation_manager()
        
        if manager.automation_thread and manager.automation_thread.is_alive():
            return jsonify({"message": "Automation service is already running"}), 200
            
        manager.start_automation()
        return jsonify({"message": "Automation service started"}), 200
    
    except Exception as e:
        logger.error(f"Error starting automation service: {e}")
        return jsonify({"error": "Internal Server Error"}), 500


@app.route('/api/automation/stop', methods=['POST'])
@log_function_call
def stop_automation_service_api():
    """Stop the automation background service.
    
    Returns:
        JSON with result
    """
    try:
        manager = get_automation_manager()
        manager.stop_automation()
        return jsonify({"message": "Automation service stopped"}), 200
    
    except Exception as e:
        logger.error(f"Error stopping automation service: {e}")
        return jsonify({"error": "Internal Server Error"}), 500


@app.route('/api/automation/trigger', methods=['POST'])
@log_function_call
def trigger_automation_cycle():
    """Manually trigger an immediate automation cycle."""
    try:
        data = request.json or {}
        period_id = data.get('period_id')
        force = data.get('force', True)
        
        manager = get_automation_manager()
        
        # We can trigger it by waking up the thread if it's running
        # We can trigger it by waking up the thread if it's running
        if manager.automation_running:
            manager.trigger_automation(period_id=period_id, force=force)
            return jsonify({"message": f"Automation cycle triggered{' for period ' + period_id if period_id else ''}"}), 200
        elif force:
            # Allow forcing a cycle even if service is not running (e.g. for testing)
            # Run in background to avoid blocking
            import threading
            def run_forced():
                try:
                    manager.run_automation_cycle(forced=True, forced_period_id=period_id)
                except Exception as e:
                    logger.error(f"Error in forced automation cycle: {e}")
            
            threading.Thread(target=run_forced).start()
            return jsonify({"message": f"Forced automation cycle started{' for period ' + period_id if period_id else ''}"}), 200
        else:
            return jsonify({"error": "Automation service is not running"}), 400
    
    except Exception as e:
        logger.error(f"Error triggering automation cycle: {e}")
        return jsonify({"error": "Internal Server Error"}), 500


@app.route('/api/automation/config', methods=['GET', 'PUT'])
@log_function_call
def handle_automation_global_config():
    """Get or update global automation configuration."""
    try:
        automation_config = get_automation_config_manager()
        
        if request.method == 'GET':
            settings = automation_config.get_global_settings()
            return jsonify(settings), 200
            
        elif request.method == 'PUT':
            data = request.json
            if not data:
                return jsonify({"error": "No data provided"}), 400
            
            # Get current settings before update to detect changes
            old_settings = automation_config.get_global_settings()
            old_regular_enabled = old_settings.get('regular_automation_enabled', False)
                
            if automation_config.update_global_settings(settings=data):
                new_settings = automation_config.get_global_settings()
                new_regular_enabled = new_settings.get('regular_automation_enabled', False)
                
                # Control automation service lifecycle if regular_automation_enabled changed
                if old_regular_enabled != new_regular_enabled and check_wizard_complete():
                    manager = get_automation_manager()
                    
                    if new_regular_enabled:
                        # Start automation service if not already running
                        if not manager.automation_running:
                            manager.start_automation()
                            logger.info("Automation service started (Enable Regular Automation toggled ON)")
                    else:
                        # Stop automation service if running
                        if manager.automation_running:
                            manager.stop_automation()
                            logger.info("Automation service stopped (Enable Regular Automation toggled OFF)")
                
                return jsonify({"message": "Global automation settings updated", "settings": new_settings}), 200
            else:
                return jsonify({"error": "Failed to update global settings"}), 500
                
    except Exception as e:
        logger.error(f"Error handling automation global config: {e}")
        return jsonify({"error": "Internal Server Error"}), 500


@app.route('/api/automation/profiles', methods=['GET', 'POST'])
@log_function_call
def handle_automation_profiles():
    """Get all profiles or create a new profile.

    GET supports optional query parameters:
      - search (str): filter profiles by name (case-insensitive).
      - page (int): page number (1-based). If omitted, returns full list.
      - per_page (int): items per page (default 50, max 200).
    """
    try:
        automation_config = get_automation_config_manager()
        
        if request.method == 'GET':
            search = request.args.get('search', '').strip()
            page_param = request.args.get('page', None)
            per_page_param = request.args.get('per_page', '50')

            page: Optional[int] = None
            if page_param is not None:
                try:
                    page = max(1, int(page_param))
                except (ValueError, TypeError):
                    return jsonify({"error": "Invalid page parameter: must be an integer"}), 400

            try:
                per_page = min(max(int(per_page_param), 1), 200)
            except (ValueError, TypeError):
                per_page = 50

            profiles = automation_config.get_all_profiles(search=search, page=page, per_page=per_page)
            return jsonify(profiles), 200
            
        elif request.method == 'POST':
            data = request.json
            if not data:
                return jsonify({"error": "No data provided"}), 400
                
            # Validate required fields (name)
            if 'name' not in data:
                return jsonify({"error": "Profile name is required"}), 400
                
            profile = automation_config.create_profile(data)
            if profile:
                return jsonify(profile), 201
            else:
                return jsonify({"error": "Failed to create profile"}), 500
                
    except Exception as e:
        logger.error(f"Error handling automation profiles: {e}")
        return jsonify({"error": "Internal Server Error"}), 500


@app.route('/api/automation/profiles/bulk-delete', methods=['POST'])
@log_function_call
def bulk_delete_automation_profiles():
    """Delete multiple automation profiles at once."""
    try:
        automation_config = get_automation_config_manager()
        data = request.json
        if not data or 'profile_ids' not in data:
            return jsonify({"error": "No profile_ids provided"}), 400
            
        profile_ids = data['profile_ids']
        if not isinstance(profile_ids, list):
            return jsonify({"error": "profile_ids must be a list"}), 400
            
        deleted_count = 0
        failed_ids = []
        
        for pid in profile_ids:
            # Don't allow deleting the default profile via bulk delete
            if pid == 'default':
                continue
            if automation_config.delete_profile(pid):
                deleted_count += 1
            else:
                failed_ids.append(pid)
                
        return jsonify({
            "message": f"Deleted {deleted_count} profiles",
            "deleted_count": deleted_count,
            "failed_ids": failed_ids
        }), 200
        
    except Exception as e:
        logger.error(f"Error bulk deleting automation profiles: {e}")
        return jsonify({"error": "Internal Server Error"}), 500


@app.route('/api/automation/profiles/<profile_id>', methods=['GET', 'PUT', 'DELETE'])
@log_function_call
def handle_automation_profile(profile_id):
    """Get, update, or delete a specific automation profile."""
    try:
        automation_config = get_automation_config_manager()
        
        if request.method == 'GET':
            profile = automation_config.get_profile(profile_id)
            if profile:
                return jsonify(profile), 200
            else:
                return jsonify({"error": "Profile not found"}), 404
                
        elif request.method == 'PUT':
            data = request.json
            if not data:
                return jsonify({"error": "No data provided"}), 400
                
            updated_profile = automation_config.update_profile(profile_id, data)
            if updated_profile:
                return jsonify(updated_profile), 200
            else:
                return jsonify({"error": "Profile not found or update failed"}), 404
                
        elif request.method == 'DELETE':
            if automation_config.delete_profile(profile_id):
                return jsonify({"message": "Profile deleted"}), 200
            else:
                return jsonify({"error": "Profile not found or delete failed"}), 404
                
    except Exception as e:
        logger.error(f"Error handling automation profile {profile_id}: {e}")
        return jsonify({"error": "Internal Server Error"}), 500


@app.route('/api/automation/assign/channel', methods=['POST'])
@log_function_call
def assign_automation_profile_channel():
    """Assign an automation profile to a channel."""
    try:
        automation_config = get_automation_config_manager()
        data = request.json
        
        channel_id = data.get('channel_id')
        profile_id = data.get('profile_id') # Can be None to unassign
        
        if channel_id is None:
            return jsonify({"error": "channel_id is required"}), 400
            
        if automation_config.assign_profile_to_channel(channel_id, profile_id):
            return jsonify({"message": f"Profile {profile_id} assigned to channel {channel_id}"}), 200
        else:
            return jsonify({"error": "Failed to assign profile"}), 500
            
    except Exception as e:
        logger.error(f"Error assigning profile to channel: {e}")
        return jsonify({"error": "Internal Server Error"}), 500


@app.route('/api/automation/assign/channels', methods=['POST'])
@log_function_call
def assign_automation_profile_channels():
    """Assign an automation profile to multiple channels."""
    try:
        automation_config = get_automation_config_manager()
        data = request.json
        
        channel_ids = data.get('channel_ids')
        profile_id = data.get('profile_id') # Can be None to unassign
        
        if not channel_ids or not isinstance(channel_ids, list):
            return jsonify({"error": "channel_ids list is required"}), 400
            
        if automation_config.assign_profile_to_channels(channel_ids, profile_id):
            return jsonify({"message": f"Profile {profile_id} assigned to {len(channel_ids)} channels"}), 200
        else:
            return jsonify({"error": "Failed to assign profile to channels"}), 500
            
    except Exception as e:
        logger.error(f"Error assigning profile to channels: {e}")
        return jsonify({"error": "Internal Server Error"}), 500


@app.route('/api/automation/assign/group', methods=['POST'])
@log_function_call
def assign_automation_profile_group():
    """Assign an automation profile to a channel group."""
    try:
        automation_config = get_automation_config_manager()
        data = request.json
        
        group_id = data.get('group_id')
        profile_id = data.get('profile_id') # Can be None to unassign
        
        if group_id is None:
            return jsonify({"error": "group_id is required"}), 400
            
        if automation_config.assign_profile_to_group(group_id, profile_id):
            return jsonify({"message": f"Profile {profile_id} assigned to group {group_id}"}), 200
        else:
            return jsonify({"error": "Failed to assign profile"}), 500
            
    except Exception as e:
        logger.error(f"Error assigning profile to group: {e}")
        return jsonify({"error": "Internal Server Error"}), 500


@app.route('/api/channels/groups/<int:group_id>/automation-periods', methods=['GET'])
@log_function_call
def get_group_automation_periods(group_id):
    """Get all automation periods assigned to a group."""
    try:
        automation_config = get_automation_config_manager()
        period_assignments = automation_config.get_group_periods(group_id)

        periods = []
        for pid, profile_id in period_assignments.items():
            period = automation_config.get_period(pid)
            if period:
                period_copy = period.copy()
                period_copy['profile_id'] = profile_id
                profile = automation_config.get_profile(profile_id)
                if profile:
                    period_copy['profile_name'] = profile.get('name')
                periods.append(period_copy)

        return jsonify(periods), 200

    except Exception as e:
        logger.error(f"Error getting automation periods for group {group_id}: {e}")
        return jsonify({"error": "Internal Server Error"}), 500


@app.route('/api/automation/periods/<period_id>/assign-groups', methods=['POST'])
@log_function_call
def assign_period_to_groups(period_id):
    """Assign an automation period to one or more groups with a profile."""
    try:
        automation_config = get_automation_config_manager()
        data = request.json

        group_ids = data.get('group_ids')
        profile_id = data.get('profile_id')
        replace = data.get('replace', False)

        if not group_ids or not isinstance(group_ids, list):
            return jsonify({"error": "group_ids list is required"}), 400
        if not profile_id:
            return jsonify({"error": "profile_id is required"}), 400

        if automation_config.assign_period_to_groups(period_id, group_ids, profile_id, replace):
            return jsonify({
                "message": f"Period {period_id} with profile {profile_id} assigned to {len(group_ids)} groups",
                "group_ids": group_ids
            }), 200
        else:
            return jsonify({"error": "Failed to assign period to groups"}), 500

    except Exception as e:
        logger.error(f"Error assigning period to groups: {e}")
        return jsonify({"error": "Internal Server Error"}), 500


@app.route('/api/automation/periods/<period_id>/remove-groups', methods=['POST'])
@log_function_call
def remove_period_from_groups(period_id):
    """Remove an automation period from specific groups."""
    try:
        automation_config = get_automation_config_manager()
        data = request.json

        group_ids = data.get('group_ids')

        if not group_ids or not isinstance(group_ids, list):
            return jsonify({"error": "group_ids list is required"}), 400

        if automation_config.remove_period_from_groups(period_id, group_ids):
            return jsonify({
                "message": f"Period {period_id} removed from {len(group_ids)} groups"
            }), 200
        else:
            return jsonify({"error": "Failed to remove period from groups"}), 500

    except Exception as e:
        logger.error(f"Error removing period from groups: {e}")
        return jsonify({"error": "Internal Server Error"}), 500


@app.route('/api/channels/groups/batch/assign-periods', methods=['POST'])
@log_function_call
def batch_assign_periods_to_groups():
    """Batch assign automation periods to multiple groups with profiles.

    Expects format:
    {
        "group_ids": [1, 2, 3],
        "period_assignments": [
            {"period_id": "period1", "profile_id": "profile1"},
            {"period_id": "period2", "profile_id": "profile2"}
        ],
        "replace": false
    }
    """
    try:
        automation_config = get_automation_config_manager()
        data = request.json

        group_ids = data.get('group_ids')
        period_assignments = data.get('period_assignments')
        replace = data.get('replace', False)

        if not group_ids or not isinstance(group_ids, list):
            return jsonify({"error": "group_ids list is required"}), 400
        if not period_assignments or not isinstance(period_assignments, list):
            return jsonify({"error": "period_assignments list is required"}), 400

        for assignment in period_assignments:
            if 'period_id' not in assignment or 'profile_id' not in assignment:
                return jsonify({"error": "Each period assignment must have period_id and profile_id"}), 400

        is_first = True
        for assignment in period_assignments:
            pid = assignment['period_id']
            profile_id = assignment['profile_id']
            automation_config.assign_period_to_groups(pid, group_ids, profile_id, replace and is_first)
            is_first = False

        return jsonify({
            "message": f"Assigned {len(period_assignments)} period-profile pairs to {len(group_ids)} groups"
        }), 200

    except Exception as e:
        logger.error(f"Error batch assigning periods to groups: {e}")
        return jsonify({"error": "Internal Server Error"}), 500


# ==================== Automation Periods API ====================
# Manage automation periods - multiple scheduled automation configurations per channel

@app.route('/api/automation/periods', methods=['GET', 'POST'])
@log_function_call
def handle_automation_periods():
    """Get all automation periods or create a new period.

    GET supports optional query parameters:
      - search (str): filter periods by name (case-insensitive).
      - page (int): page number (1-based). If omitted, returns full list.
      - per_page (int): items per page (default 50, max 200).
    """
    try:
        automation_config = get_automation_config_manager()
        
        if request.method == 'GET':
            search = request.args.get('search', '').strip()
            page_param = request.args.get('page', None)
            per_page_param = request.args.get('per_page', '50')

            page: Optional[int] = None
            if page_param is not None:
                try:
                    page = max(1, int(page_param))
                except (ValueError, TypeError):
                    return jsonify({"error": "Invalid page parameter: must be an integer"}), 400

            try:
                per_page = min(max(int(per_page_param), 1), 200)
            except (ValueError, TypeError):
                per_page = 50

            result = automation_config.get_all_periods(search=search, page=page, per_page=per_page)

            if page is None:
                # Plain list – add channel count to each period (backward compatible)
                for period in result:
                    channels = automation_config.get_period_channels(period['id'])
                    period['channel_count'] = len(channels)
                return jsonify(result), 200
            else:
                # Paginated envelope – add channel count to each item
                for period in result['items']:
                    channels = automation_config.get_period_channels(period['id'])
                    period['channel_count'] = len(channels)
                return jsonify(result), 200
            
        elif request.method == 'POST':
            data = request.json
            if not data:
                return jsonify({"error": "No data provided"}), 400
                
            # Validate required fields (profile_id is no longer required here)
            if 'name' not in data:
                return jsonify({"error": "Period name is required"}), 400
            if 'schedule' not in data:
                return jsonify({"error": "Schedule is required"}), 400
            
            schedule = data['schedule']
            if schedule.get('type') == 'cron':
                if not CRONITER_AVAILABLE:
                    return jsonify({"error": "Cron scheduling is not available because the 'croniter' package is missing"}), 400
                if not croniter.is_valid(schedule.get('value', '')):
                    return jsonify({"error": "Invalid cron expression"}), 400
                
            period_id = automation_config.create_period(data)
            if period_id:
                period = automation_config.get_period(period_id)
                return jsonify(period), 201
            else:
                return jsonify({"error": "Failed to create period"}), 500
                
    except Exception as e:
        logger.error(f"Error handling automation periods: {e}")
        return jsonify({"error": "Internal Server Error"}), 500


@app.route('/api/automation/periods/<period_id>', methods=['GET', 'PUT', 'DELETE'])
@log_function_call
def handle_automation_period(period_id):
    """Get, update, or delete a specific automation period."""
    try:
        automation_config = get_automation_config_manager()
        
        if request.method == 'GET':
            period = automation_config.get_period(period_id)
            if period:
                # Add channel list
                channels = automation_config.get_period_channels(period_id)
                period['channels'] = channels
                return jsonify(period), 200
            else:
                return jsonify({"error": "Period not found"}), 404
                
        elif request.method == 'PUT':
            data = request.json
            if not data:
                return jsonify({"error": "No data provided"}), 400
                
            if 'schedule' in data:
                schedule = data['schedule']
                if schedule.get('type') == 'cron':
                    if not CRONITER_AVAILABLE:
                        return jsonify({"error": "Cron scheduling is not available because the 'croniter' package is missing"}), 400
                    if not croniter.is_valid(schedule.get('value', '')):
                        return jsonify({"error": "Invalid cron expression"}), 400
                
            if automation_config.update_period(period_id, data):
                period = automation_config.get_period(period_id)
                return jsonify(period), 200
            else:
                return jsonify({"error": "Period not found or update failed"}), 404
                
        elif request.method == 'DELETE':
            if automation_config.delete_period(period_id):
                return jsonify({"message": "Period deleted"}), 200
            else:
                return jsonify({"error": "Period not found or delete failed"}), 404
                
    except Exception as e:
        logger.error(f"Error handling automation period {period_id}: {e}")
        return jsonify({"error": "Internal Server Error"}), 500


@app.route('/api/automation/periods/<period_id>/assign-channels', methods=['POST'])
@log_function_call
def assign_period_to_channels(period_id):
    """Assign an automation period to multiple channels with a profile."""
    try:
        automation_config = get_automation_config_manager()
        data = request.json
        
        channel_ids = data.get('channel_ids')
        profile_id = data.get('profile_id')  # Now required
        replace = data.get('replace', False)  # If True, replace existing periods
        
        if not channel_ids or not isinstance(channel_ids, list):
            return jsonify({"error": "channel_ids list is required"}), 400
        if not profile_id:
            return jsonify({"error": "profile_id is required"}), 400
            
        if automation_config.assign_period_to_channels(period_id, channel_ids, profile_id, replace):
            return jsonify({
                "message": f"Period {period_id} with profile {profile_id} assigned to {len(channel_ids)} channels",
                "channel_ids": channel_ids
            }), 200
        else:
            return jsonify({"error": "Failed to assign period to channels"}), 500
            
    except Exception as e:
        logger.error(f"Error assigning period to channels: {e}")
        return jsonify({"error": "Internal Server Error"}), 500


@app.route('/api/automation/periods/<period_id>/remove-channels', methods=['POST'])
@log_function_call
def remove_period_from_channels(period_id):
    """Remove an automation period from specific channels."""
    try:
        automation_config = get_automation_config_manager()
        data = request.json
        
        channel_ids = data.get('channel_ids')
        
        if not channel_ids or not isinstance(channel_ids, list):
            return jsonify({"error": "channel_ids list is required"}), 400
            
        if automation_config.remove_period_from_channels(period_id, channel_ids):
            return jsonify({
                "message": f"Period {period_id} removed from {len(channel_ids)} channels"
            }), 200
        else:
            return jsonify({"error": "Failed to remove period from channels"}), 500
            
    except Exception as e:
        logger.error(f"Error removing period from channels: {e}")
        return jsonify({"error": "Internal Server Error"}), 500


@app.route('/api/automation/periods/<period_id>/channels', methods=['GET'])
@log_function_call
def get_period_channels(period_id):
    """Get all channels assigned to a period."""
    try:
        automation_config = get_automation_config_manager()
        period = automation_config.get_period(period_id)
        
        if not period:
            return jsonify({"error": "Period not found"}), 404
            
        channel_ids = automation_config.get_period_channels(period_id)
        
        # Get channel details from UDI if possible
        try:
            udi = get_udi_manager()
            channels = []
            for cid in channel_ids:
                channel = udi.get_channel(cid)
                if channel:
                    channels.append({
                        "id": cid,
                        "number": channel.get('number'),
                        "name": channel.get('name')
                    })
                else:
                    channels.append({"id": cid})
            return jsonify(channels), 200
        except:
            # If UDI fails, just return IDs
            return jsonify([{"id": cid} for cid in channel_ids]), 200
            
    except Exception as e:
        logger.error(f"Error getting channels for period {period_id}: {e}")
        return jsonify({"error": "Internal Server Error"}), 500


@app.route('/api/channels/batch/period-usage', methods=['POST'])
@log_function_call
def get_batch_period_usage():
    """Analyze automation period usage across multiple channels."""
    try:
        data = request.get_json()
        if not data:
            return jsonify({"error": "No data provided"}), 400
        
        channel_ids = data.get('channel_ids', [])
        if not isinstance(channel_ids, list) or len(channel_ids) == 0:
            return jsonify({"error": "channel_ids must be a non-empty list"}), 400
            
        automation_config = get_automation_config_manager()
        
        # Structure: {period_id: {"count": N, "profile_ids": {profile_id: [channel_ids]}}}
        period_usage = {}
        
        for ch_id in channel_ids:
            # get_channel_periods returns {period_id: profile_id}
            assignments = automation_config.get_channel_periods(ch_id)
            for pid, prof_id in assignments.items():
                if pid not in period_usage:
                    period = automation_config.get_period(pid)
                    if not period:
                        continue
                    period_usage[pid] = {
                        "id": pid,
                        "name": period.get('name', 'Unknown'),
                        "count": 0,
                        "profile_breakdown": {} # {profile_id: {"name": str, "channel_ids": []}}
                    }
                
                usage = period_usage[pid]
                usage["count"] += 1
                
                if prof_id not in usage["profile_breakdown"]:
                    profile = automation_config.get_profile(prof_id)
                    usage["profile_breakdown"][prof_id] = {
                        "id": prof_id,
                        "name": profile.get('name', 'Unknown') if profile else 'Unknown',
                        "channel_ids": []
                    }
                
                usage["profile_breakdown"][prof_id]["channel_ids"].append(ch_id)
                
        # Format for frontend
        results = []
        for pid, usage in period_usage.items():
            results.append({
                "id": pid,
                "name": usage["name"],
                "count": usage["count"],
                "percentage": round((usage["count"] / len(channel_ids)) * 100, 1),
                "profiles": list(usage["profile_breakdown"].values())
            })
            
        # Sort by frequency
        results.sort(key=lambda x: x['count'], reverse=True)
        
        return jsonify({
            "periods": results,
            "total_channels": len(channel_ids)
        }), 200
    except Exception as e:
        logger.error(f"Error getting batch period usage: {e}")
        return jsonify({"error": "Internal Server Error"}), 500

@app.route('/api/channels/<int:channel_id>/automation-periods', methods=['GET'])
@log_function_call
def get_channel_automation_periods(channel_id):
    """Get all automation periods assigned to a channel (including group-inherited ones)."""
    try:
        automation_config = get_automation_config_manager()
        udi = get_udi_manager()
        channel = udi.get_channel_by_id(channel_id)
        group_id = channel.get('channel_group_id') if channel else None

        # get_effective_channel_periods merges group-level and channel-level assignments
        period_assignments = automation_config.get_effective_channel_periods(channel_id, group_id)

        periods = []
        for pid, profile_id in period_assignments.items():
            period = automation_config.get_period(pid)
            if period:
                period_copy = period.copy()
                period_copy['profile_id'] = profile_id
                # Include profile name
                profile = automation_config.get_profile(profile_id)
                if profile:
                    period_copy['profile_name'] = profile.get('name')
                periods.append(period_copy)

        return jsonify(periods), 200

    except Exception as e:
        logger.error(f"Error getting automation periods for channel {channel_id}: {e}")
        return jsonify({"error": "Internal Server Error"}), 500


@app.route('/api/channels/batch/assign-periods', methods=['POST'])
@log_function_call
def batch_assign_periods_to_channels():
    """Batch assign automation periods to multiple channels with profiles.
    
    Expects format:
    {
        "channel_ids": [1, 2, 3],
        "period_assignments": [
            {"period_id": "period1", "profile_id": "profile1"},
            {"period_id": "period2", "profile_id": "profile2"}
        ],
        "replace": false
    }
    """
    try:
        automation_config = get_automation_config_manager()
        data = request.json
        
        channel_ids = data.get('channel_ids')
        period_assignments = data.get('period_assignments')
        replace = data.get('replace', False)
        
        if not channel_ids or not isinstance(channel_ids, list):
            return jsonify({"error": "channel_ids list is required"}), 400
        if not period_assignments or not isinstance(period_assignments, list):
            return jsonify({"error": "period_assignments list is required"}), 400
        
        # Validate period_assignments format
        for assignment in period_assignments:
            if 'period_id' not in assignment or 'profile_id' not in assignment:
                return jsonify({"error": "Each period assignment must have period_id and profile_id"}), 400
        
        # Assign each period-profile pair to all channels
        is_first = True
        for assignment in period_assignments:
            pid = assignment['period_id']
            profile_id = assignment['profile_id']
            # Only the first period should replace if replace=True
            automation_config.assign_period_to_channels(pid, channel_ids, profile_id, replace and is_first)
            is_first = False
        
        return jsonify({
            "message": f"Assigned {len(period_assignments)} period-profile pairs to {len(channel_ids)} channels"
        }), 200
        
    except Exception as e:
        logger.error(f"Error batch assigning periods: {e}")
        return jsonify({"error": "Internal Server Error"}), 500


# ==================== Automation Events API ====================
# Calculate and retrieve upcoming automation events based on periods

from apps.automation.automation_events_scheduler import get_events_scheduler


@app.route('/api/automation/events/upcoming', methods=['GET'])
@log_function_call
def get_upcoming_automation_events():
    """Get upcoming automation events based on configured periods.
    
    Query parameters:
    - hours: Number of hours ahead to calculate (default: 24, max: 168)
    - max_events: Maximum number of events to return (default: 100, max: 500)
    - period_id: Filter by specific period ID
    - force_refresh: Force cache refresh (true/false)
    """
    try:
        events_scheduler = get_events_scheduler()
        
        # Parse query parameters
        hours_ahead = min(int(request.args.get('hours', 24)), 168)  # Max 1 week
        max_events = min(int(request.args.get('max_events', 100)), 500)
        period_id_filter = request.args.get('period_id')
        force_refresh = request.args.get('force_refresh', '').lower() == 'true'
        
        # Get cached or fresh events
        result = events_scheduler.get_cached_events(hours_ahead, max_events, force_refresh)
        
        # Check global settings
        from apps.automation.automation_config_manager import get_automation_config_manager
        config_manager = get_automation_config_manager()
        global_settings = config_manager.get_global_settings()
        automation_enabled = global_settings.get('regular_automation_enabled', False)
        
        # Inject the enabled status into the payload
        result['automation_enabled'] = automation_enabled
        
        # Filter by period if requested
        if period_id_filter:
            result['events'] = [e for e in result['events'] if e.get('period_id') == period_id_filter]
        
        return jsonify(result), 200
        
    except Exception as e:
        logger.error(f"Error getting upcoming automation events: {e}")
        return jsonify({"error": "Internal Server Error"}), 500


@app.route('/api/automation/events/invalidate-cache', methods=['POST'])
@log_function_call
def invalidate_automation_events_cache():
    """Invalidate the automation events cache.
    
    This should be called whenever automation periods are modified.
    """
    try:
        events_scheduler = get_events_scheduler()
        events_scheduler.invalidate_cache()
        
        return jsonify({"message": "Cache invalidated successfully"}), 200
        
    except Exception as e:
        logger.error(f"Error invalidating cache: {e}")
        return jsonify({"error": "Internal Server Error"}), 500


# ==================== Stream Monitoring Session API ====================
# Advanced stream monitoring with live quality tracking, reliability scoring,
# and screenshot capture for event-based stream management

from apps.stream.stream_session_manager import get_session_manager, REVIEW_DURATION
from apps.stream.stream_monitoring_service import get_monitoring_service
from apps.stream.acestream_monitoring_client import AceStreamMonitoringClient, normalize_content_id


def _get_acestream_monitoring_client() -> AceStreamMonitoringClient:
    """Build client for external AceStream orchestrator monitoring contract."""
    return AceStreamMonitoringClient()


def _acestream_client_or_error():
    client = _get_acestream_monitoring_client()
    if not client.is_configured():
        return None, (
            jsonify({
                "error": "AceStream orchestrator is not configured",
                "required_env": [
                    "ACESTREAM_ORCHESTRATOR_BASE_URL",
                    "ACESTREAM_ORCHESTRATOR_API_KEY"
                ]
            }),
            500,
        )
    return client, None


def _ping_orchestrator_ready(client=None):
    """Ping the orchestrator /version endpoint to verify it is running and reachable.

    Returns (True, version_str) on success, (False, error_msg) on failure.
    The response must be a JSON object with a 'title' field containing 'AceStream Orchestrator'.
    """
    if client is None:
        client = _get_acestream_monitoring_client()
    if not client.is_configured():
        return False, "AceStream orchestrator is not configured"

    base_url = client.base_url
    version_url = f"{base_url.rstrip('/')}/version"
    try:
        resp = requests.get(version_url, timeout=5)
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, dict):
            return False, "Orchestrator /version returned unexpected format"
        title = str(data.get('title') or '')
        if 'AceStream Orchestrator' not in title:
            return False, f"Unexpected orchestrator title: '{title}'"
        version = data.get('version', 'unknown')
        return True, str(version)
    except requests.exceptions.ConnectionError:
        return False, f"Cannot connect to AceStream orchestrator at {base_url}"
    except requests.exceptions.Timeout:
        return False, f"Timeout connecting to AceStream orchestrator at {base_url}"
    except requests.exceptions.HTTPError as exc:
        return False, f"Orchestrator /version returned HTTP {exc.response.status_code}"
    except Exception as exc:
        # Log full exception details server-side, but return a generic message to the client
        logger.error("Unexpected error while pinging AceStream orchestrator", exc_info=True)
        return False, "Unexpected error while pinging orchestrator"


def _parse_m3u_acestream_entries(m3u_content: str):
    """Local parser fallback for acestream:// and /ace/getstream?id=<id> entries."""
    items = []
    pending_name = None

    for idx, raw_line in enumerate((m3u_content or '').splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue

        if line.startswith('#EXTINF:'):
            if ',' in line:
                pending_name = line.split(',', 1)[1].strip() or None
            else:
                pending_name = None
            continue

        content_id = normalize_content_id(line)
        if content_id:
            items.append({
                'content_id': content_id,
                'name': pending_name,
                'line_number': str(idx),
            })
            pending_name = None

    # Deduplicate by content_id keeping first non-empty name.
    merged = {}
    for item in items:
        key = item['content_id']
        if key not in merged:
            merged[key] = item
        elif not merged[key].get('name') and item.get('name'):
            merged[key]['name'] = item['name']

    return list(merged.values())


def _ace_channel_sessions_store():
    from apps.database.manager import get_db_manager
    db = get_db_manager()
    store = db.get_system_setting('acestream_channel_sessions', {})
    return store if isinstance(store, dict) else {}


def _save_ace_channel_sessions_store(store):
    from apps.database.manager import get_db_manager
    db = get_db_manager()
    return bool(db.set_system_setting('acestream_channel_sessions', store))


def _get_ace_management_settings():
    """Load management tuning from shared session settings."""
    from apps.database.manager import get_db_manager

    db = get_db_manager()
    session_settings = db.get_system_setting('session_settings', {})
    if not isinstance(session_settings, dict):
        session_settings = {}

    review_duration = float(session_settings.get('review_duration', 60.0) or 60.0)
    pass_score_threshold = 70.0
    return {
        'review_duration': max(0.0, review_duration),
        'pass_score_threshold': pass_score_threshold,
    }


def _clamp_score(value):
    return max(0.0, min(100.0, float(value)))


def _coerce_float(value):
    try:
        if value is None:
            return None
        return float(value)
    except Exception:
        return None


def _parse_resolution_pixels(resolution):
    if not isinstance(resolution, str):
        return 0
    match = re.match(r'^(\d{2,5})x(\d{2,5})$', resolution.strip())
    if not match:
        return 0
    return int(match.group(1)) * int(match.group(2))


def _parse_resolution_parts(resolution):
    if not isinstance(resolution, str):
        return None, None
    match = re.match(r'^(\d{2,5})x(\d{2,5})$', resolution.strip())
    if not match:
        return None, None
    return int(match.group(1)), int(match.group(2))


def _score_ffprobe_quality(ffprobe_stats):
    """Compute quality bonus from cached FFProbe-like stream stats."""
    if not isinstance(ffprobe_stats, dict):
        return 0.0, 'unknown'

    bonus = 0.0
    resolution = ffprobe_stats.get('resolution')
    pixels = _parse_resolution_pixels(resolution)
    if pixels >= 1920 * 1080:
        bonus += 8.0
    elif pixels >= 1280 * 720:
        bonus += 5.0
    elif pixels >= 960 * 540:
        bonus += 2.0
    elif pixels > 0:
        bonus -= 2.0

    video_codec = str(ffprobe_stats.get('video_codec') or '').lower()
    if video_codec in ('hevc', 'h265', 'av1'):
        bonus += 3.0
    elif video_codec in ('h264', 'avc'):
        bonus += 2.0
    elif video_codec and video_codec != 'n/a':
        bonus += 1.0

    audio_codec = str(ffprobe_stats.get('audio_codec') or '').lower()
    if audio_codec and audio_codec != 'n/a':
        bonus += 1.0

    fps = _coerce_float(ffprobe_stats.get('fps'))
    if fps is not None:
        if fps >= 50.0:
            bonus += 2.0
        elif fps >= 23.0:
            bonus += 1.0

    bitrate_kbps = _coerce_float(ffprobe_stats.get('bitrate_kbps'))
    if bitrate_kbps is not None:
        if bitrate_kbps >= 4000.0:
            bonus += 2.0
        elif bitrate_kbps >= 1500.0:
            bonus += 1.0

    if ffprobe_stats.get('hdr_format'):
        bonus += 2.0

    if bonus >= 14.0:
        rating = 'excellent'
    elif bonus >= 9.0:
        rating = 'good'
    elif bonus >= 4.0:
        rating = 'fair'
    else:
        rating = 'basic'

    return bonus, rating


def _extract_last_ts_values(monitor):
    values = []
    if not isinstance(monitor, dict):
        return values

    recent_status = monitor.get('recent_status')
    if isinstance(recent_status, list):
        for item in recent_status:
            if not isinstance(item, dict):
                continue
            last_ts = _coerce_float(item.get('last_ts'))
            if last_ts is not None:
                values.append(last_ts)

    latest = monitor.get('latest_status')
    if isinstance(latest, dict):
        latest_last_ts = _coerce_float(latest.get('last_ts'))
        if latest_last_ts is not None and (not values or values[-1] != latest_last_ts):
            values.append(latest_last_ts)

    return values


def _compute_last_ts_plateau_penalty(monitor):
    """Penalize frequent last_ts plateaus (limited progression between samples)."""
    values = _extract_last_ts_values(monitor)
    if len(values) < 3:
        return 0.0, 0.0

    deltas = []
    for i in range(1, len(values)):
        deltas.append(values[i] - values[i - 1])

    if not deltas:
        return 0.0, 0.0

    plateau_steps = sum(1 for d in deltas if d <= 0.0)
    plateau_ratio = float(plateau_steps) / float(len(deltas))
    penalty = min(18.0, plateau_ratio * 18.0)

    # Extra penalty if there is a long consecutive plateau streak.
    longest = 0
    current = 0
    for d in deltas:
        if d <= 0.0:
            current += 1
            longest = max(longest, current)
        else:
            current = 0
    if longest >= 3:
        penalty = min(22.0, penalty + 4.0)

    return penalty, plateau_ratio


def _compute_ace_management_score(monitor, entry=None):
    """Compute Ace reliability score using status, plateau behavior, and FFProbe quality."""
    if not isinstance(monitor, dict):
        return 25.0, {
            'plateau_penalty': 0.0,
            'plateau_ratio': 0.0,
            'ffprobe_bonus': 0.0,
            'ffprobe_rating': 'unknown',
        }

    status = (monitor.get('status') or '').lower()
    base = {
        'running': 82.0,
        'starting': 55.0,
        'reconnecting': 50.0,
        'stuck': 25.0,
        'dead': 0.0,
    }.get(status, 40.0)

    latest = monitor.get('latest_status') or {}

    speed_down = float(latest.get('speed_down') or 0.0)
    speed_up = float(latest.get('speed_up') or 0.0)
    peers = float(latest.get('peers') or 0.0)

    base += min(8.0, speed_down / 600.0)
    base += min(4.0, speed_up / 900.0)
    base += min(6.0, peers / 8.0)

    if monitor.get('currently_played'):
        base += 3.0

    plateau_penalty, plateau_ratio = _compute_last_ts_plateau_penalty(monitor)
    ffprobe_stats = entry.get('ffprobe_stats') if isinstance(entry, dict) else None
    ffprobe_bonus, ffprobe_rating = _score_ffprobe_quality(ffprobe_stats)

    score = _clamp_score(base - plateau_penalty + ffprobe_bonus)
    return score, {
        'plateau_penalty': plateau_penalty,
        'plateau_ratio': plateau_ratio,
        'ffprobe_bonus': ffprobe_bonus,
        'ffprobe_rating': ffprobe_rating,
    }


def _evaluate_ace_entry_management(entry, monitor, now_ts, settings):
    """Derive and persist management state fields for a channel-session entry."""
    changed = False

    prev_state = (entry.get('management_state') or 'review').lower()
    prev_since = float(entry.get('management_since') or now_ts)
    manual_quarantine = bool(entry.get('manual_quarantine'))

    score, score_meta = _compute_ace_management_score(monitor, entry)
    reason = None

    if not isinstance(monitor, dict):
        target_state = 'review'
        reason = 'missing-monitor'
    else:
        raw_status = (monitor.get('status') or 'unknown').lower()

        if manual_quarantine:
            target_state = 'quarantined'
            reason = 'manual'
        elif raw_status == 'dead':
            target_state = 'quarantined'
            reason = 'dead'
        elif raw_status == 'stuck':
            # Keep stuck as problematic but still monitored for potential recovery.
            target_state = 'quarantined'
            reason = 'stuck'
        elif raw_status in ('starting', 'reconnecting'):
            target_state = 'review'
            reason = raw_status
        elif raw_status == 'running':
            elapsed = max(0.0, now_ts - prev_since)
            if prev_state == 'stable':
                target_state = 'stable'
            elif prev_state == 'review' and elapsed >= settings['review_duration'] and score >= settings['pass_score_threshold']:
                target_state = 'stable'
            else:
                target_state = 'review'
                if score_meta.get('plateau_ratio', 0.0) >= 0.5:
                    reason = 'plateau'
                elif prev_state != 'stable':
                    reason = 'warming'
                else:
                    reason = 'low-score'
        else:
            target_state = 'review'
            reason = f'status-{raw_status}'

    # Auto-revive from quarantine after cooldown (mirrors standard QUARANTINE_DURATION of 15 min).
    # Only applies to automatic quarantines — manual quarantines persist until explicitly revived.
    QUARANTINE_DURATION = 900.0
    if target_state == 'quarantined' and not manual_quarantine:
        time_in_quarantine = now_ts - prev_since
        if prev_state == 'quarantined' and time_in_quarantine >= QUARANTINE_DURATION:
            target_state = 'review'
            reason = 'auto-revive'
            logger.info(
                f"Ace stream {entry.get('stream_id')} auto-revived after {time_in_quarantine:.0f}s in quarantine"
            )

    if target_state != prev_state:
        entry['management_state'] = target_state
        entry['management_since'] = now_ts
        changed = True
    elif 'management_state' not in entry:
        entry['management_state'] = target_state
        entry['management_since'] = now_ts
        changed = True

    if float(entry.get('management_score') or -1) != float(score):
        entry['management_score'] = score
        changed = True

    plateau_ratio = float(score_meta.get('plateau_ratio') or 0.0)
    if float(entry.get('management_plateau_ratio') or -1.0) != plateau_ratio:
        entry['management_plateau_ratio'] = plateau_ratio
        changed = True

    ffprobe_rating = score_meta.get('ffprobe_rating')
    if entry.get('management_ffprobe_rating') != ffprobe_rating:
        entry['management_ffprobe_rating'] = ffprobe_rating
        changed = True

    ffprobe_bonus = float(score_meta.get('ffprobe_bonus') or 0.0)
    if float(entry.get('management_ffprobe_bonus') or -999.0) != ffprobe_bonus:
        entry['management_ffprobe_bonus'] = ffprobe_bonus
        changed = True

    if entry.get('management_reason') != reason:
        entry['management_reason'] = reason
        changed = True

    # Update last_ts_delta sliding window for tie-breaking (last 10 samples)
    _ACE_LAST_TS_DELTA_WINDOW = 10
    current_delta = None
    if isinstance(monitor, dict):
        movement = monitor.get('livepos_movement') or {}
        raw_delta = movement.get('last_ts_delta')
        if raw_delta is not None:
            try:
                current_delta = float(raw_delta)
            except (TypeError, ValueError):
                pass
    if current_delta is not None:
        window = entry.get('_last_ts_delta_window')
        if not isinstance(window, list):
            window = []
            entry['_last_ts_delta_window'] = window
        if len(window) >= _ACE_LAST_TS_DELTA_WINDOW:
            del window[0]
        window.append(current_delta)
        avg_delta = sum(window) / len(window)
        if entry.get('_last_ts_delta_avg') != avg_delta:
            entry['_last_ts_delta_avg'] = avg_delta
            changed = True

    return changed

def _is_ace_ffprobe_stats_empty(ffprobe_stats):
    """Return True if ffprobe stats have no useful data (all N/A or missing)."""
    if not isinstance(ffprobe_stats, dict):
        return True
    return (
        (ffprobe_stats.get('resolution') or 'N/A') in ('N/A', '0x0', '') and
        (ffprobe_stats.get('video_codec') or 'N/A') == 'N/A'
    )


_ACE_FFPROBE_RECHECK_THROTTLE_S = 300   # seconds between automatic recheck attempts
_ACE_FFPROBE_PROBE_DURATION_S = 5       # seconds of stream to analyse per recheck
_ACE_FFPROBE_PROBE_TIMEOUT_S = 15       # subprocess timeout for each recheck


def _schedule_ace_ffprobe_recheck(entry, monitor):
    """Schedule a background ffprobe recheck on the engine HTTP URL for a running stream.

    Only triggers when:
    - Current ffprobe stats are empty/N/A
    - The monitor is in 'running' status
    - The engine host and port are known
    - The last attempt was more than _ACE_FFPROBE_RECHECK_THROTTLE_S seconds ago
    """
    if not _is_ace_ffprobe_stats_empty(entry.get('ffprobe_stats')):
        return

    monitor_status = (monitor.get('status') or '').lower() if isinstance(monitor, dict) else ''
    if monitor_status != 'running':
        return

    # Throttle: avoid hammering the engine
    last_attempt = entry.get('_ffprobe_attempt_ts')
    if last_attempt and time.time() - float(last_attempt) < _ACE_FFPROBE_RECHECK_THROTTLE_S:
        return

    engine = (monitor.get('engine') or {}) if isinstance(monitor, dict) else {}
    host = engine.get('host')
    port = engine.get('port')
    content_id = entry.get('content_id')

    if not host or not port or not content_id:
        return

    url = f"http://{host}:{port}/ace/getstream?id={content_id}"
    entry['_ffprobe_attempt_ts'] = time.time()
    thread_name = f"ace-ffprobe-{content_id[:8] if content_id else 'unknown'}"

    def _run():
        try:
            from apps.stream.stream_check_utils import get_stream_info_and_bitrate
            stats = get_stream_info_and_bitrate(
                url,
                duration=_ACE_FFPROBE_PROBE_DURATION_S,
                timeout=_ACE_FFPROBE_PROBE_TIMEOUT_S,
            )
            if stats and (stats.get('resolution', 'N/A') not in ('N/A', '0x0', '') or
                         stats.get('video_codec', 'N/A') != 'N/A'):
                entry['ffprobe_stats'] = {
                    'resolution': stats.get('resolution'),
                    'fps': stats.get('fps'),
                    'bitrate_kbps': stats.get('bitrate_kbps'),
                    'video_codec': stats.get('video_codec', 'N/A'),
                    'audio_codec': stats.get('audio_codec', 'N/A'),
                    'hdr_format': stats.get('hdr_format'),
                    'pixel_format': stats.get('pixel_format'),
                    'audio_sample_rate': stats.get('audio_sample_rate'),
                    'audio_channels': stats.get('audio_channels'),
                    'channel_layout': stats.get('channel_layout'),
                    'audio_bitrate': stats.get('audio_bitrate'),
                }
                logger.info(f"AceStream ffprobe recheck succeeded for {content_id}: {entry['ffprobe_stats'].get('resolution')}")
            else:
                logger.debug(f"AceStream ffprobe recheck yielded no useful data for {content_id}")
        except Exception as exc:
            logger.debug(f"AceStream ffprobe recheck failed for {content_id}: {exc}")

    t = threading.Thread(target=_run, daemon=True, name=thread_name)
    t.start()


def _evaluate_ace_session_management(raw_session, monitors_by_id, settings):
    """Evaluate and persist management state for every entry in one session."""
    if not isinstance(raw_session, dict):
        return False

    changed = False
    now_ts = time.time()
    udi = get_udi_manager()
    ffprobe_cache = {}
    entries = raw_session.get('entries') or []
    for entry in entries:
        if not isinstance(entry, dict):
            continue

        if not isinstance(entry.get('ffprobe_stats'), dict):
            stream_id = entry.get('stream_id')
            stream = None
            if stream_id not in ffprobe_cache:
                try:
                    stream = udi.get_stream_by_id(int(stream_id)) if stream_id is not None else None
                except Exception:
                    stream = None
                ffprobe_cache[stream_id] = stream
            else:
                stream = ffprobe_cache.get(stream_id)

            if isinstance(stream, dict):
                entry['ffprobe_stats'] = extract_stream_stats(stream)
                changed = True

        monitor = monitors_by_id.get(entry.get('monitor_id'))
        # Schedule a background ffprobe recheck via the engine HTTP URL when stats are empty
        _schedule_ace_ffprobe_recheck(entry, monitor)
        if _evaluate_ace_entry_management(entry, monitor, now_ts, settings):
            changed = True

    return changed


def _check_ace_session_epg_auto_stop(raw_session, client):
    """Stop Orchestrator monitors for a session when its EPG event end time has passed.

    Uses a 'epg_auto_stopped' flag to avoid issuing stop calls on every poll tick.
    Returns True if the store needs to be persisted.
    """
    if not isinstance(raw_session, dict):
        return False
    if raw_session.get('epg_auto_stopped'):
        return False

    epg_end = raw_session.get('epg_event_end')
    if not epg_end:
        return False

    try:
        from datetime import datetime, timezone
        end_time = datetime.fromisoformat(str(epg_end).replace('Z', '+00:00'))
        if end_time.tzinfo is None:
            end_time = end_time.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        if now < end_time:
            return False
    except Exception:
        return False

    session_id = raw_session.get('session_id', '?')
    logger.info(f"Ace session {session_id}: EPG event ended, auto-stopping Orchestrator monitors")

    for entry in raw_session.get('entries') or []:
        monitor_id = entry.get('monitor_id') if isinstance(entry, dict) else None
        if not monitor_id:
            continue
        try:
            client.stop_session(monitor_id)
        except Exception:
            pass

    raw_session['epg_auto_stopped'] = True
    return True


def _apply_ace_dispatcharr_sync(raw_session):
    """Enforce Dispatcharr channel stream order based on AceStream management states.

    Order: stable entries (desc score) → review entries (desc score).
    Quarantined entries are excluded from the channel.
    Mirrors the _update_monitoring_ranks / _execute_sync logic of the standard monitoring service.
    Returns True if a sync was attempted (store does NOT need extra save; update_channel_streams is async).
    """
    if not isinstance(raw_session, dict):
        return False

    channel_id = raw_session.get('channel_id')
    if not channel_id:
        return False

    entries = [e for e in (raw_session.get('entries') or []) if isinstance(e, dict)]
    if not entries:
        return False

    stable = sorted(
        [e for e in entries if (e.get('management_state') or 'review').lower() == 'stable'],
        key=lambda e: (float(e.get('management_score') or 0), float(e.get('_last_ts_delta_avg') or 0)),
        reverse=True,
    )
    review = sorted(
        [e for e in entries if (e.get('management_state') or 'review').lower() == 'review'],
        key=lambda e: (float(e.get('management_score') or 0), float(e.get('_last_ts_delta_avg') or 0)),
        reverse=True,
    )

    desired_ids = [int(e['stream_id']) for e in (stable + review) if e.get('stream_id') is not None]

    if not desired_ids:
        return False

    try:
        udi = get_udi_manager()
        channel = udi.get_channel_by_id(int(channel_id))
        current_ids = channel.get('streams', []) if isinstance(channel, dict) else []

        if desired_ids == current_ids:
            return False

        from apps.core.api_utils import update_channel_streams

        def _do_sync():
            try:
                success = update_channel_streams(int(channel_id), desired_ids)
                if success:
                    udi.refresh_channel_by_id(int(channel_id))
                    logger.debug(
                        f"Ace session {raw_session.get('session_id')}: synced channel {channel_id} "
                        f"stream order → {desired_ids}"
                    )
                else:
                    logger.warning(f"Ace Dispatcharr sync failed for channel {channel_id}")
            except Exception as exc:
                logger.error(f"Ace Dispatcharr sync error for channel {channel_id}: {exc}")

        import threading
        threading.Thread(target=_do_sync, daemon=True, name=f"AceSync-{channel_id}").start()
        return True

    except Exception as exc:
        logger.error(f"_apply_ace_dispatcharr_sync error: {exc}")
        return False


def _save_ace_session_telemetry_snapshot(raw_session, monitors_by_id):
    """Persist Ace monitoring telemetry to DB, deduped by latest monitor sample key."""
    if not isinstance(raw_session, dict):
        return False

    entries = raw_session.get('entries') or []
    if not isinstance(entries, list) or not entries:
        return False

    pending_entries = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        monitor = monitors_by_id.get(entry.get('monitor_id'))
        if not isinstance(monitor, dict):
            continue

        sample_key = str(monitor.get('last_collected_at') or monitor.get('sample_count') or '')
        if not sample_key:
            continue
        if entry.get('last_telemetry_key') == sample_key:
            continue

        pending_entries.append((entry, monitor, sample_key))

    if not pending_entries:
        return False

    from apps.database.connection import get_session as get_db_session
    from apps.database.models import Run, ChannelHealth, StreamTelemetry

    db_session = get_db_session()
    try:
        run_ts = datetime.utcnow()
        channel_id = int(raw_session.get('channel_id') or 0)
        channel_name = raw_session.get('channel_name')

        all_monitors = []
        quarantined_count = 0
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            monitor = monitors_by_id.get(entry.get('monitor_id'))
            if isinstance(monitor, dict):
                all_monitors.append(monitor)
            if (entry.get('management_state') or '').lower() == 'quarantined':
                quarantined_count += 1

        run = Run(
            timestamp=run_ts,
            duration_seconds=0.0,
            total_channels=1,
            total_streams=len(entries),
            global_dead_count=quarantined_count,
            global_revived_count=0,
            run_type='acestream_monitor',
            raw_details=json.dumps({
                'source_type': 'acestream',
                'session_id': raw_session.get('session_id'),
                'channel_id': channel_id,
                'channel_name': channel_name,
                'pending_streams': len(pending_entries),
            }),
            raw_subentries=None,
        )
        db_session.add(run)
        db_session.flush()

        channel_health = ChannelHealth(
            run_id=run.id,
            channel_id=channel_id,
            channel_name=channel_name,
            offline=len(all_monitors) == 0,
            available_streams=max(0, len(entries) - quarantined_count),
            dead_streams=quarantined_count,
        )
        db_session.add(channel_health)

        udi = get_udi_manager()
        stream_cache = {}
        for entry, monitor, sample_key in pending_entries:
            stream_id = int(entry.get('stream_id') or 0)
            if stream_id not in stream_cache:
                stream_cache[stream_id] = udi.get_stream_by_id(stream_id) if stream_id else None
            stream = stream_cache.get(stream_id)

            ffprobe_stats = entry.get('ffprobe_stats') if isinstance(entry.get('ffprobe_stats'), dict) else {}
            width, height = _parse_resolution_parts(ffprobe_stats.get('resolution'))

            row = StreamTelemetry(
                run_id=run.id,
                channel_id=channel_id,
                provider_id=(stream or {}).get('m3u_account_id') if isinstance(stream, dict) else None,
                stream_id=stream_id,
                bitrate_kbps=int(_coerce_float(ffprobe_stats.get('bitrate_kbps')) or 0) or None,
                resolution_width=width,
                resolution_height=height,
                fps=_coerce_float(ffprobe_stats.get('fps')),
                codec=ffprobe_stats.get('video_codec'),
                audio_codec=ffprobe_stats.get('audio_codec'),
                quality_score=_coerce_float(entry.get('management_score')),
                is_dead=((entry.get('management_state') or '').lower() == 'quarantined'),
                is_hdr=bool(ffprobe_stats.get('hdr_format')),
            )
            db_session.add(row)
            entry['last_telemetry_key'] = sample_key

        db_session.commit()
        return True
    except Exception as exc:
        db_session.rollback()
        logger.error(f"Error saving Ace telemetry snapshot: {exc}", exc_info=True)
        return False
    finally:
        db_session.close()


def _ace_status_buckets(monitor_items):
    active_states = {'starting', 'running', 'stuck', 'reconnecting'}
    running = 0
    stuck = 0
    dead = 0

    for item in monitor_items:
        status = (item.get('status') or '').lower()
        if status == 'running':
            running += 1
        elif status == 'stuck':
            stuck += 1
            # Keep stuck in the dead/problem section for UI grouping,
            # but still treat it as active for continued monitoring.
            dead += 1
        elif status == 'dead':
            dead += 1

    is_active = any((item.get('status') or '').lower() in active_states for item in monitor_items)
    return running, stuck, dead, is_active


def _build_ace_channel_session_summary(raw_session, monitors_by_id):
    monitor_items = [monitors_by_id.get(entry.get('monitor_id')) for entry in raw_session.get('entries', [])]
    monitor_items = [item for item in monitor_items if isinstance(item, dict)]

    stable = 0
    review = 0
    quarantined = 0
    for entry in raw_session.get('entries', []):
        if not isinstance(entry, dict):
            continue
        state = (entry.get('management_state') or 'review').lower()
        if state == 'stable':
            stable += 1
        elif state == 'quarantined':
            quarantined += 1
        else:
            review += 1

    _, _, _, is_active = _ace_status_buckets(monitor_items)
    return {
        'session_id': raw_session.get('session_id'),
        'source_type': 'acestream',
        'channel_id': raw_session.get('channel_id'),
        'channel_name': raw_session.get('channel_name'),
        'channel_logo_url': raw_session.get('channel_logo_url'),
        'epg_event_title': raw_session.get('epg_event_title'),
        'epg_event_description': raw_session.get('epg_event_description'),
        'epg_event_start': raw_session.get('epg_event_start'),
        'epg_event_end': raw_session.get('epg_event_end'),
        'epg_event_id': raw_session.get('epg_event_id'),
        'created_at': raw_session.get('created_at'),
        'is_active': is_active,
        'stable_count': stable,
        'review_count': review,
        'quarantined_count': quarantined,
        'stream_count': len(raw_session.get('entries', [])),
        'sample_count': sum(int(item.get('sample_count') or 0) for item in monitor_items),
        'played_count': sum(1 for item in monitor_items if item.get('currently_played')),
        'monitor_count': len(raw_session.get('entries', [])),
    }


def _annotate_monitors_with_playback(client, monitors_by_id):
    """Attach currently_played to monitor payloads using started streams endpoint."""
    if not monitors_by_id:
        return

    try:
        started_payload = client.list_started_streams()
    except Exception:
        return

    for monitor_id, monitor in list(monitors_by_id.items()):
        if not isinstance(monitor, dict):
            continue
        monitors_by_id[monitor_id] = client.annotate_with_playback(dict(monitor), started_payload)


def _compact_ace_monitor_payload(monitor, recent_limit=8):
    """Return a compact monitor shape suitable for 1s UI polling."""
    if not isinstance(monitor, dict):
        return None

    recent_status = monitor.get('recent_status')
    if isinstance(recent_status, list):
        recent_status = recent_status[-recent_limit:]
    else:
        recent_status = []

    return {
        'monitor_id': monitor.get('monitor_id'),
        'content_id': monitor.get('content_id'),
        'stream_name': monitor.get('stream_name'),
        'status': monitor.get('status'),
        'interval_s': monitor.get('interval_s'),
        'run_seconds': monitor.get('run_seconds'),
        'started_at': monitor.get('started_at'),
        'last_collected_at': monitor.get('last_collected_at'),
        'ended_at': monitor.get('ended_at'),
        'sample_count': monitor.get('sample_count'),
        'last_error': monitor.get('last_error'),
        'dead_reason': monitor.get('dead_reason'),
        'reconnect_attempts': monitor.get('reconnect_attempts'),
        'engine': monitor.get('engine'),
        'session': monitor.get('session'),
        'latest_status': monitor.get('latest_status'),
        'recent_status': recent_status,
        'livepos_movement': monitor.get('livepos_movement'),
        'currently_played': bool(monitor.get('currently_played')),
    }


@app.route('/api/acestream-channel-sessions', methods=['GET'])
def list_acestream_channel_sessions():
    """List channel-scoped AceStream monitoring sessions."""
    client, error_response = _acestream_client_or_error()
    if error_response:
        return error_response

    try:
        status_filter = (request.args.get('status') or '').lower()
        store = _ace_channel_sessions_store()
        mgmt_settings = _get_ace_management_settings()
        monitor_payload = client.list_sessions()
        monitor_items = monitor_payload.get('items', []) if isinstance(monitor_payload, dict) else []
        monitors_by_id = {item.get('monitor_id'): item for item in monitor_items if isinstance(item, dict) and item.get('monitor_id')}
        _annotate_monitors_with_playback(client, monitors_by_id)

        sessions = []
        store_changed = False
        for session_id, raw in store.items():
            if not isinstance(raw, dict):
                continue
            raw['session_id'] = session_id
            if _evaluate_ace_session_management(raw, monitors_by_id, mgmt_settings):
                store_changed = True
            if _save_ace_session_telemetry_snapshot(raw, monitors_by_id):
                store_changed = True
            if _check_ace_session_epg_auto_stop(raw, client):
                store_changed = True
            # Discover new matching streams and start monitors for them (mirrors standard refresh)
            if not raw.get('epg_auto_stopped'):
                if _refresh_ace_session_streams(raw, client):
                    store_changed = True
                    _evaluate_ace_session_management(raw, monitors_by_id, mgmt_settings)
            _apply_ace_dispatcharr_sync(raw)
            summary = _build_ace_channel_session_summary(raw, monitors_by_id)
            if status_filter == 'active' and not summary.get('is_active'):
                continue
            sessions.append(summary)

        if store_changed:
            _save_ace_channel_sessions_store(store)

        sessions.sort(key=lambda s: s.get('created_at') or 0, reverse=True)
        return jsonify(sessions), 200
    except requests.RequestException as exc:
        status_code = getattr(getattr(exc, 'response', None), 'status_code', 502)
        detail = None
        if getattr(exc, 'response', None) is not None:
            try:
                detail = exc.response.json()
            except Exception:
                detail = exc.response.text
        return jsonify({'error': 'Failed to list AceStream channel sessions', 'detail': detail}), status_code
    except Exception as e:
        logger.error(f"Error listing AceStream channel sessions: {e}", exc_info=True)
        return jsonify({'error': 'Internal Server Error'}), 500


def _refresh_ace_session_streams(raw_session, client, interval_s=1.0, run_seconds=0, per_sample_timeout_s=1.0):
    """Discover new streams matching the session filter and start monitors for them.

    Mirrors StreamSessionManager._refresh_session_streams: on each poll cycle the full UDI
    stream list is re-evaluated against the stored regex / TVG-ID filter so that streams
    added to the M3U source after session creation are automatically picked up.
    Returns True if new entries were added (store must be persisted by caller).
    """
    if not isinstance(raw_session, dict):
        return False

    channel_id = raw_session.get('channel_id')
    if not channel_id:
        return False

    resolved_regex = raw_session.get('regex_filter', '')
    match_by_tvg_id = bool(raw_session.get('match_by_tvg_id'))
    channel_tvg_id = raw_session.get('channel_tvg_id')
    channel_name = raw_session.get('channel_name', '')

    # Nothing to refresh if session used the channel-streams fallback
    if not resolved_regex and not (match_by_tvg_id and channel_tvg_id):
        return False

    import re as _re

    compiled_regex = None
    if resolved_regex:
        try:
            pattern = resolved_regex.replace('CHANNEL_NAME', _re.escape(channel_name))
            pattern = _re.sub(r'(?<!\\) +', r'\\s+', pattern)
            compiled_regex = _re.compile(pattern, _re.IGNORECASE)
        except _re.error:
            return False

    udi = get_udi_manager()
    all_streams = udi.get_streams() or []

    # Build set of content_ids already being monitored
    existing_content_ids = {
        e.get('content_id')
        for e in (raw_session.get('entries') or [])
        if isinstance(e, dict) and e.get('content_id')
    }

    new_entries = []
    for s in all_streams:
        matched = False
        if match_by_tvg_id and channel_tvg_id:
            if s.get('tvg_id') == channel_tvg_id:
                matched = True
        if not matched and compiled_regex:
            if compiled_regex.search((s.get('name') or '')[:1000]):
                matched = True
        if not matched:
            continue

        content_id = normalize_content_id(s.get('url'))
        if not content_id or content_id in existing_content_ids:
            continue

        try:
            payload = {
                'content_id': content_id,
                'stream_name': s.get('name') or channel_name,
                'interval_s': float(interval_s),
                'run_seconds': int(run_seconds),
                'per_sample_timeout_s': float(per_sample_timeout_s),
            }
            started = client.start_session(payload)
            monitor_id = started.get('monitor_id') if isinstance(started, dict) else None
            if not monitor_id:
                continue
            new_entries.append({
                'stream_id': s.get('id'),
                'stream_name': s.get('name'),
                'content_id': content_id,
                'monitor_id': monitor_id,
                'ffprobe_stats': extract_stream_stats(s),
            })
            existing_content_ids.add(content_id)
            logger.info(
                f"Ace session {raw_session.get('session_id')}: discovered new stream "
                f"{s.get('name')} ({content_id})"
            )
        except Exception as exc:
            logger.warning(f"Ace refresh: failed to start monitor for {content_id}: {exc}")

    if new_entries:
        raw_session.setdefault('entries', []).extend(new_entries)
        return True
    return False


def create_acestream_channel_session_impl(
    channel_id,
    interval_s=1.0,
    run_seconds=0,
    per_sample_timeout_s=1.0,
    engine_container_id=None,
    epg_event_title=None,
    epg_event_description=None,
    epg_event_start=None,
    epg_event_end=None,
    epg_event_id=None,
):
    """Core logic to create and start an AceStream channel session.

    Stream discovery always uses the channel's automation profile settings
    (regex filter and TVG-ID matching), the same as FFmpeg-based sessions.
    """
    client, error_response = _acestream_client_or_error()
    if error_response:
        return {'error': 'AceStream backend orchestrator is not configured.'}, 503

    # Verify the orchestrator is actually reachable and ready before starting
    ready, ready_detail = _ping_orchestrator_ready(client)
    if not ready:
        return {
            'error': 'AceStream orchestrator is not ready',
            'detail': ready_detail,
        }, 503

    try:
        udi = get_udi_manager()

        # Always refresh this channel to get up-to-date metadata
        udi.refresh_channel_by_id(int(channel_id))
        channel = udi.get_channel_by_id(int(channel_id))
        if not channel:
            return {'error': 'Channel not found'}, 404

        channel_name = channel.get('name') or f'Channel {channel_id}'
        channel_tvg_id = channel.get('tvg_id')
        logo_id = channel.get('logo_id')
        channel_logo_url = f"/api/channels/logos/{logo_id}/cache" if logo_id else channel.get('logo_url')

        # ── Stream discovery: always load from channel's automation profile ──
        # Mirrors what create_session_from_event does for FFmpeg-based sessions.
        resolved_regex = ''
        resolved_match_by_tvg_id = False
        try:
            from apps.automation.automated_stream_manager import RegexChannelMatcher
            regex_matcher = RegexChannelMatcher()
            group_id = channel.get('group_id') if channel.get('group_id') is not None else channel.get('channel_group_id')
            match_config = regex_matcher.get_channel_match_config(str(channel_id), group_id)
            resolved_match_by_tvg_id = match_config.get('match_by_tvg_id', False)
            channel_regex = regex_matcher.get_channel_regex_filter(str(channel_id), default=None, group_id=group_id)
            if channel_regex:
                resolved_regex = channel_regex
            logger.info(
                f"AceStream session for channel {channel_id}: channel profile "
                f"(regex='{resolved_regex}', match_by_tvg_id={resolved_match_by_tvg_id})"
            )
        except Exception as _e:
            logger.debug(f"Could not load channel automation profile for AceStream session: {_e}")

        compiled_regex = None
        if resolved_regex:
            import re as _re
            try:
                pattern = resolved_regex.replace('CHANNEL_NAME', _re.escape(channel_name))
                # Convert literal spaces to flexible whitespace (mirrors _WHITESPACE_PATTERN)
                pattern = _re.sub(r'(?<!\\) +', r'\\s+', pattern)
                compiled_regex = _re.compile(pattern, _re.IGNORECASE)
            except _re.error as exc:
                logger.error(f"Invalid regex for channel {channel_id} automation profile: {exc}")
                compiled_regex = None

        all_streams = udi.get_streams() or []
        candidate_stream_ids = []
        for s in all_streams:
            matched = False
            if resolved_match_by_tvg_id and channel_tvg_id:
                if s.get('tvg_id') == channel_tvg_id:
                    matched = True
            if not matched and compiled_regex:
                stream_name = (s.get('name') or '')[:1000]
                if compiled_regex.search(stream_name):
                    matched = True
            if matched:
                candidate_stream_ids.append(s.get('id'))

        # Fallback: if no filter specified at all, use channel's pre-assigned streams
        if not candidate_stream_ids and not resolved_regex and not (resolved_match_by_tvg_id and channel_tvg_id):
            candidate_stream_ids = channel.get('streams', [])

        entries = []
        seen_content_ids = set()
        for stream_id in candidate_stream_ids:
            stream = udi.get_stream_by_id(int(stream_id)) if stream_id else None
            if not stream:
                continue
            content_id = normalize_content_id(stream.get('url'))
            if not content_id or content_id in seen_content_ids:
                continue
            seen_content_ids.add(content_id)

            payload = {
                'content_id': content_id,
                'stream_name': stream.get('name') or channel_name,
                'interval_s': float(interval_s),
                'run_seconds': int(run_seconds),
                'per_sample_timeout_s': float(per_sample_timeout_s),
                'engine_container_id': engine_container_id if engine_container_id else None,
            }
            started = client.start_session(payload)
            monitor_id = started.get('monitor_id') if isinstance(started, dict) else None
            if not monitor_id:
                continue
            entries.append({
                'stream_id': stream.get('id'),
                'stream_name': stream.get('name'),
                'content_id': content_id,
                'monitor_id': monitor_id,
                'ffprobe_stats': extract_stream_stats(stream),
            })

        if not entries:
            return {'error': 'No AceStream-compatible streams found matching the discovery criteria'}, 400

        session_id = f"ace_channel_{int(channel_id)}_{int(time.time())}"
        store = _ace_channel_sessions_store()
        store[session_id] = {
            'session_id': session_id,
            'channel_id': int(channel_id),
            'channel_name': channel_name,
            'channel_logo_url': channel_logo_url,
            'channel_tvg_id': channel_tvg_id,
            'regex_filter': resolved_regex,
            'match_by_tvg_id': bool(resolved_match_by_tvg_id),
            'epg_event_title': epg_event_title,
            'epg_event_description': epg_event_description,
            'epg_event_start': epg_event_start,
            'epg_event_end': epg_event_end,
            'epg_event_id': epg_event_id,
            'created_at': time.time(),
            'entries': entries,
        }
        _save_ace_channel_sessions_store(store)

        logger.info(
            f"Created AceStream session {session_id} for channel {channel_id} "
            f"with {len(entries)} stream(s) "
            f"(filter: '{resolved_regex or 'channel-streams-fallback'}', tvg_id: {resolved_match_by_tvg_id})"
        )
        return {'session_id': session_id, 'message': 'AceStream channel session created', 'monitor_count': len(entries)}, 201
    except requests.RequestException as exc:
        status_code = getattr(getattr(exc, 'response', None), 'status_code', 502)
        detail = None
        if getattr(exc, 'response', None) is not None:
            try:
                detail = exc.response.json()
            except Exception:
                detail = exc.response.text
        return {'error': 'Failed to create AceStream channel session', 'detail': detail}, status_code
    except Exception as e:
        logger.error(f"Error creating AceStream channel session: {e}", exc_info=True)
        return {'error': 'Internal Server Error'}, 500


@app.route('/api/acestream-channel-sessions', methods=['POST'])
def create_acestream_channel_session():
    """Create and start AceStream monitoring for all AceStream streams in a channel."""
    data = request.get_json(silent=True) or {}
    channel_id = data.get('channel_id')
    if not channel_id:
        return jsonify({'error': 'channel_id is required'}), 400

    result, status_code = create_acestream_channel_session_impl(
        channel_id=channel_id,
        interval_s=data.get('interval_s', 1.0),
        run_seconds=data.get('run_seconds', 0),
        per_sample_timeout_s=data.get('per_sample_timeout_s', 1.0),
        engine_container_id=data.get('engine_container_id'),
        epg_event_title=data.get('epg_event_title'),
        epg_event_description=data.get('epg_event_description'),
        epg_event_start=data.get('epg_event_start'),
        epg_event_end=data.get('epg_event_end'),
        epg_event_id=data.get('epg_event_id'),
    )
    return jsonify(result), status_code


@app.route('/api/acestream-channel-sessions/group/start', methods=['POST'])
def create_acestream_group_sessions():
    """Create AceStream channel sessions for all channels in a group."""
    client, error_response = _acestream_client_or_error()
    if error_response:
        return error_response

    # Verify the orchestrator is reachable before starting any sessions
    ready, ready_detail = _ping_orchestrator_ready(client)
    if not ready:
        return jsonify({
            'error': 'AceStream orchestrator is not ready',
            'detail': ready_detail,
        }), 503

    try:
        data = request.get_json(silent=True) or {}
        group_id = data.get('group_id')
        if not group_id:
            return jsonify({'error': 'group_id is required'}), 400

        udi = get_udi_manager()
        channels = udi.get_channels_by_group(int(group_id)) or []
        if not channels:
            return jsonify({'error': 'Group not found or has no channels'}), 404

        created_sessions = []
        errors = []
        for channel in channels:
            payload = {
                'channel_id': channel.get('id'),
                'interval_s': data.get('interval_s', 1.0),
                'run_seconds': data.get('run_seconds', 0),
                'per_sample_timeout_s': data.get('per_sample_timeout_s', 1.0),
                'engine_container_id': data.get('engine_container_id'),
                'epg_event_title': data.get('epg_event_title'),
                'epg_event_description': data.get('epg_event_description'),
                'epg_event_start': data.get('epg_event_start'),
                'epg_event_end': data.get('epg_event_end'),
                'epg_event_id': data.get('epg_event_id'),
            }

            with app.test_request_context('/api/acestream-channel-sessions', method='POST', json=payload):
                # Reuse handler logic for consistency.
                response = create_acestream_channel_session()

            if isinstance(response, tuple):
                body, code = response
            else:
                body, code = response, response.status_code

            if code >= 200 and code < 300:
                created_sessions.append(body.get_json())
            else:
                try:
                    msg = body.get_json().get('error')
                except Exception:
                    msg = 'Unknown error'
                errors.append(f"Channel {channel.get('name')} ({channel.get('id')}): {msg}")

        return jsonify({
            'message': f"Started {len(created_sessions)} AceStream channel sessions from group {group_id}",
            'sessions': created_sessions,
            'errors': errors,
        }), 200 if created_sessions else 400
    except Exception as e:
        logger.error(f"Error creating AceStream group sessions: {e}", exc_info=True)
        return jsonify({'error': 'Internal Server Error'}), 500


@app.route('/api/acestream-channel-sessions/<session_id>', methods=['GET'])
def get_acestream_channel_session(session_id):
    """Get detailed channel-scoped AceStream monitoring session."""
    client, error_response = _acestream_client_or_error()
    if error_response:
        return error_response

    try:
        store = _ace_channel_sessions_store()
        raw = store.get(session_id)
        if not isinstance(raw, dict):
            return jsonify({'error': 'Session not found'}), 404

        mgmt_settings = _get_ace_management_settings()
        monitor_payload = client.list_sessions()
        monitor_items = monitor_payload.get('items', []) if isinstance(monitor_payload, dict) else []
        monitors_by_id = {item.get('monitor_id'): item for item in monitor_items if isinstance(item, dict) and item.get('monitor_id')}
        _annotate_monitors_with_playback(client, monitors_by_id)

        store_changed = False
        if _evaluate_ace_session_management(raw, monitors_by_id, mgmt_settings):
            store_changed = True
        if _save_ace_session_telemetry_snapshot(raw, monitors_by_id):
            store_changed = True
        if _check_ace_session_epg_auto_stop(raw, client):
            store_changed = True
        _apply_ace_dispatcharr_sync(raw)
        if store_changed:
            store[session_id] = raw
            _save_ace_channel_sessions_store(store)

        summary = _build_ace_channel_session_summary(raw, monitors_by_id)
        entries = []
        for entry in raw.get('entries', []):
            monitor = monitors_by_id.get(entry.get('monitor_id'))
            if monitor is None and entry.get('monitor_id'):
                try:
                    monitor = client.get_session(entry['monitor_id'])
                except Exception:
                    monitor = None
            entries.append({
                **entry,
                'monitor': _compact_ace_monitor_payload(monitor),
            })

        detail = {
            **summary,
            'entries': entries,
            'channel_id': raw.get('channel_id'),
            'channel_name': raw.get('channel_name'),
            'created_at': raw.get('created_at'),
        }
        return jsonify(detail), 200
    except requests.RequestException as exc:
        status_code = getattr(getattr(exc, 'response', None), 'status_code', 502)
        detail = None
        if getattr(exc, 'response', None) is not None:
            try:
                detail = exc.response.json()
            except Exception:
                detail = exc.response.text
        return jsonify({'error': 'Failed to get AceStream channel session', 'detail': detail}), status_code
    except Exception as e:
        logger.error(f"Error getting AceStream channel session {session_id}: {e}", exc_info=True)
        return jsonify({'error': 'Internal Server Error'}), 500


@app.route('/api/acestream-channel-sessions/<session_id>/stop', methods=['POST'])
def stop_acestream_channel_session(session_id):
    """Stop all orchestrator monitor sessions attached to a channel session."""
    client, error_response = _acestream_client_or_error()
    if error_response:
        return error_response

    try:
        store = _ace_channel_sessions_store()
        raw = store.get(session_id)
        if not isinstance(raw, dict):
            return jsonify({'error': 'Session not found'}), 404

        for entry in raw.get('entries', []):
            monitor_id = entry.get('monitor_id')
            if not monitor_id:
                continue
            try:
                client.stop_session(monitor_id)
            except Exception:
                pass

        return jsonify({'message': 'AceStream channel session stopped'}), 200
    except Exception as e:
        logger.error(f"Error stopping AceStream channel session {session_id}: {e}", exc_info=True)
        return jsonify({'error': 'Internal Server Error'}), 500


@app.route('/api/acestream-channel-sessions/<session_id>', methods=['DELETE'])
def delete_acestream_channel_session(session_id):
    """Delete channel session and all orchestrator monitor entries."""
    client, error_response = _acestream_client_or_error()
    if error_response:
        return error_response

    try:
        store = _ace_channel_sessions_store()
        raw = store.get(session_id)
        if not isinstance(raw, dict):
            return jsonify({'error': 'Session not found'}), 404

        for entry in raw.get('entries', []):
            monitor_id = entry.get('monitor_id')
            if not monitor_id:
                continue
            try:
                client.delete_entry(monitor_id)
            except Exception:
                try:
                    client.stop_session(monitor_id)
                except Exception:
                    pass

        del store[session_id]
        _save_ace_channel_sessions_store(store)
        return jsonify({'message': 'AceStream channel session deleted'}), 200
    except Exception as e:
        logger.error(f"Error deleting AceStream channel session {session_id}: {e}", exc_info=True)
        return jsonify({'error': 'Internal Server Error'}), 500


@app.route('/api/acestream-channel-sessions/<session_id>/streams/<int:stream_id>/quarantine', methods=['POST'])
def quarantine_acestream_channel_stream(session_id, stream_id):
    """Manually quarantine one Ace stream entry within a channel session."""
    try:
        store = _ace_channel_sessions_store()
        raw = store.get(session_id)
        if not isinstance(raw, dict):
            return jsonify({'error': 'Session not found'}), 404

        entries = raw.get('entries') or []
        target = None
        for entry in entries:
            if int(entry.get('stream_id') or -1) == int(stream_id):
                target = entry
                break

        if not isinstance(target, dict):
            return jsonify({'error': 'Stream not found in session'}), 404

        now_ts = time.time()
        target['manual_quarantine'] = True
        target['management_state'] = 'quarantined'
        target['management_reason'] = 'manual'
        target['management_since'] = now_ts

        _save_ace_channel_sessions_store(store)
        return jsonify({'message': 'Ace stream quarantined'}), 200
    except Exception as e:
        logger.error(f"Error quarantining Ace stream {stream_id} in session {session_id}: {e}", exc_info=True)
        return jsonify({'error': 'Internal Server Error'}), 500


@app.route('/api/acestream-channel-sessions/<session_id>/streams/<int:stream_id>/revive', methods=['POST'])
def revive_acestream_channel_stream(session_id, stream_id):
    """Revive one manually quarantined Ace stream entry back to review."""
    try:
        store = _ace_channel_sessions_store()
        raw = store.get(session_id)
        if not isinstance(raw, dict):
            return jsonify({'error': 'Session not found'}), 404

        entries = raw.get('entries') or []
        target = None
        for entry in entries:
            if int(entry.get('stream_id') or -1) == int(stream_id):
                target = entry
                break

        if not isinstance(target, dict):
            return jsonify({'error': 'Stream not found in session'}), 404

        now_ts = time.time()
        target['manual_quarantine'] = False
        target['management_state'] = 'review'
        target['management_reason'] = 'manual-revive'
        target['management_since'] = now_ts

        _save_ace_channel_sessions_store(store)
        return jsonify({'message': 'Ace stream moved to review'}), 200
    except Exception as e:
        logger.error(f"Error reviving Ace stream {stream_id} in session {session_id}: {e}", exc_info=True)
        return jsonify({'error': 'Internal Server Error'}), 500


@app.route('/api/acestream-orchestrator/config', methods=['GET'])
def get_acestream_orchestrator_config_endpoint():
    """Get AceStream orchestrator configuration (without exposing API key)."""
    try:
        cfg = get_acestream_orchestrator_config().get_config()
        return jsonify(cfg), 200
    except Exception as e:
        logger.error(f"Error getting AceStream orchestrator config: {e}")
        return jsonify({"error": "Internal Server Error"}), 500


@app.route('/api/acestream-orchestrator/config', methods=['PUT'])
def update_acestream_orchestrator_config_endpoint():
    """Update AceStream orchestrator host, port, and API key."""
    try:
        data = request.get_json() or {}
        if not isinstance(data, dict):
            return jsonify({"error": "No configuration data provided"}), 400

        host = data.get('host')
        port = data.get('port')
        api_key = data.get('api_key')

        cfg = get_acestream_orchestrator_config()
        success = cfg.update_config(host=host, port=port, api_key=api_key)
        if not success:
            return jsonify({"error": "Failed to save configuration"}), 500

        # Maintain env vars for immediate availability and compatibility.
        base_url = cfg.get_base_url() or ''
        if base_url:
            os.environ['ACESTREAM_ORCHESTRATOR_BASE_URL'] = base_url
            os.environ['ORCHESTRATOR_BASE_URL'] = base_url
        if api_key is not None:
            os.environ['ACESTREAM_ORCHESTRATOR_API_KEY'] = str(api_key)
            os.environ['ORCHESTRATOR_API_KEY'] = str(api_key)

        return jsonify({"message": "AceStream orchestrator configuration updated successfully"}), 200
    except Exception as e:
        logger.error(f"Error updating AceStream orchestrator config: {e}")
        return jsonify({"error": "Internal Server Error"}), 500


@app.route('/api/acestream-orchestrator/ready', methods=['GET'])
def check_acestream_orchestrator_ready():
    """Check if the AceStream orchestrator is configured and reachable."""
    try:
        client = _get_acestream_monitoring_client()
        if not client.is_configured():
            return jsonify({
                "ready": False,
                "error": "AceStream orchestrator is not configured",
            }), 200

        ready, detail = _ping_orchestrator_ready(client)
        return jsonify({
            "ready": ready,
            "detail": detail,
        }), 200
    except Exception as e:
        logger.error(f"Error checking AceStream orchestrator readiness: {e}")
        return jsonify({"ready": False, "error": "Internal Server Error"}), 500


@app.route('/api/acestream-monitor-sessions/start', methods=['POST'])
def start_acestream_monitor_session():
    """Start AceStream monitoring session via external orchestrator contract."""
    client, error_response = _acestream_client_or_error()
    if error_response:
        return error_response

    payload = request.get_json(silent=True) or {}
    normalized = normalize_content_id(payload.get('content_id'))
    if not normalized:
        return jsonify({"error": "content_id is required and must contain a valid 40-hex AceStream ID"}), 400
    payload['content_id'] = normalized
    try:
        response_data = client.start_session(payload)
        return jsonify(response_data), 200
    except requests.RequestException as exc:
        status_code = getattr(getattr(exc, 'response', None), 'status_code', 502)
        detail = None
        if getattr(exc, 'response', None) is not None:
            try:
                detail = exc.response.json()
            except Exception:
                detail = exc.response.text
        return jsonify({"error": "Failed to start AceStream monitoring session", "detail": detail}), status_code


@app.route('/api/acestream-monitor-sessions', methods=['GET'])
def list_acestream_monitor_sessions():
    """List AceStream monitoring sessions with optional playback correlation."""
    client, error_response = _acestream_client_or_error()
    if error_response:
        return error_response

    try:
        sessions_data = client.list_sessions()
        include_correlation = request.args.get('include_correlation', 'true').lower() != 'false'
        if include_correlation:
            started_streams = client.list_started_streams()
            sessions_data['items'] = client.annotate_many_with_playback(sessions_data.get('items', []), started_streams)
        return jsonify(sessions_data), 200
    except requests.RequestException as exc:
        status_code = getattr(getattr(exc, 'response', None), 'status_code', 502)
        detail = None
        if getattr(exc, 'response', None) is not None:
            try:
                detail = exc.response.json()
            except Exception:
                detail = exc.response.text
        return jsonify({"error": "Failed to list AceStream monitoring sessions", "detail": detail}), status_code


@app.route('/api/acestream-monitor-sessions/<monitor_id>', methods=['GET'])
def get_acestream_monitor_session(monitor_id):
    """Get one AceStream monitoring session with detailed history."""
    client, error_response = _acestream_client_or_error()
    if error_response:
        return error_response

    try:
        session_data = client.get_session(monitor_id)
        include_correlation = request.args.get('include_correlation', 'true').lower() != 'false'
        if include_correlation:
            started_streams = client.list_started_streams()
            session_data = client.annotate_with_playback(session_data, started_streams)
        return jsonify(session_data), 200
    except requests.RequestException as exc:
        status_code = getattr(getattr(exc, 'response', None), 'status_code', 502)
        detail = None
        if getattr(exc, 'response', None) is not None:
            try:
                detail = exc.response.json()
            except Exception:
                detail = exc.response.text
        return jsonify({"error": "Failed to get AceStream monitoring session", "detail": detail}), status_code


@app.route('/api/acestream-monitor-sessions/<monitor_id>', methods=['DELETE'])
def stop_acestream_monitor_session(monitor_id):
    """Stop AceStream monitoring session lifecycle."""
    client, error_response = _acestream_client_or_error()
    if error_response:
        return error_response

    try:
        response_data = client.stop_session(monitor_id)
        return jsonify(response_data if response_data is not None else {"ok": True}), 200
    except requests.RequestException as exc:
        status_code = getattr(getattr(exc, 'response', None), 'status_code', 502)
        detail = None
        if getattr(exc, 'response', None) is not None:
            try:
                detail = exc.response.json()
            except Exception:
                detail = exc.response.text
        return jsonify({"error": "Failed to stop AceStream monitoring session", "detail": detail}), status_code


@app.route('/api/acestream-monitor-sessions/<monitor_id>/entry', methods=['DELETE'])
def delete_acestream_monitor_entry(monitor_id):
    """Delete AceStream monitoring entry and ensure it is stopped."""
    client, error_response = _acestream_client_or_error()
    if error_response:
        return error_response

    try:
        response_data = client.delete_entry(monitor_id)
        return jsonify(response_data if response_data is not None else {"ok": True}), 200
    except requests.RequestException as exc:
        status_code = getattr(getattr(exc, 'response', None), 'status_code', 502)
        detail = None
        if getattr(exc, 'response', None) is not None:
            try:
                detail = exc.response.json()
            except Exception:
                detail = exc.response.text
        return jsonify({"error": "Failed to delete AceStream monitoring entry", "detail": detail}), status_code


@app.route('/api/acestream-monitor-sessions/parse-m3u', methods=['POST'])
def parse_acestream_m3u():
    """Parse M3U and extract AceStream IDs and names via orchestrator contract."""
    client, error_response = _acestream_client_or_error()
    if error_response:
        return error_response

    data = request.get_json(silent=True) or {}
    m3u_content = data.get('m3u_content', '')
    if not isinstance(m3u_content, str):
        return jsonify({"error": "m3u_content must be a string"}), 400

    try:
        response_data = client.parse_m3u(m3u_content)
        parsed_items = response_data.get('items', []) if isinstance(response_data, dict) else []

        # Merge with local parser to ensure /ace/getstream?id=<id> URLs are detected.
        fallback_items = _parse_m3u_acestream_entries(m3u_content)
        merged = {}
        for item in parsed_items:
            cid = normalize_content_id(item.get('content_id'))
            if not cid:
                continue
            merged[cid] = {
                'content_id': cid,
                'name': item.get('name'),
                'line_number': str(item.get('line_number')) if item.get('line_number') is not None else None,
            }
        for item in fallback_items:
            cid = item['content_id']
            if cid not in merged:
                merged[cid] = item
            elif not merged[cid].get('name') and item.get('name'):
                merged[cid]['name'] = item['name']

        response_data = {
            'count': len(merged),
            'items': list(merged.values()),
        }
        return jsonify(response_data), 200
    except requests.RequestException as exc:
        status_code = getattr(getattr(exc, 'response', None), 'status_code', 502)
        detail = None
        if getattr(exc, 'response', None) is not None:
            try:
                detail = exc.response.json()
            except Exception:
                detail = exc.response.text
        return jsonify({"error": "Failed to parse M3U for AceStream entries", "detail": detail}), status_code


@app.route('/api/acestream-monitor-sessions/streams/started', methods=['GET'])
def list_acestream_started_streams():
    """Optional playback correlation source from orchestrator proxy streams endpoint."""
    client, error_response = _acestream_client_or_error()
    if error_response:
        return error_response

    try:
        response_data = client.list_started_streams()
        return jsonify(response_data), 200
    except requests.RequestException as exc:
        status_code = getattr(getattr(exc, 'response', None), 'status_code', 502)
        detail = None
        if getattr(exc, 'response', None) is not None:
            try:
                detail = exc.response.json()
            except Exception:
                detail = exc.response.text
        return jsonify({"error": "Failed to list started streams", "detail": detail}), status_code


@app.route('/api/stream-sessions', methods=['GET'])
def get_stream_sessions():
    """Get all stream monitoring sessions or filter by status."""
    try:
        session_manager = get_session_manager()
        
        # Check for status filter
        status = request.args.get('status', '').lower()
        
        if status == 'active':
            sessions = session_manager.get_active_sessions()
        else:
            sessions = session_manager.get_all_sessions()
        
        # Convert to JSON-serializable format
        sessions_data = []
        for session in sessions:
            session_dict = {
                'session_id': session.session_id,
                'channel_id': session.channel_id,
                'channel_name': session.channel_name,
                'regex_filter': session.regex_filter,
                'created_at': session.created_at,
                'is_active': session.is_active,
                'pre_event_minutes': session.pre_event_minutes,
                'stagger_ms': session.stagger_ms,
                'timeout_ms': session.timeout_ms,
                'probe_interval_ms': session.probe_interval_ms,
                'screenshot_interval_seconds': session.screenshot_interval_seconds,
                'window_size': session.window_size,
                'stream_count': len(session.streams) if session.streams else 0,
                'active_streams': sum(1 for s in session.streams.values() if s.status in ['stable', 'review']) if session.streams else 0,
                'stable_count': sum(1 for s in session.streams.values() if s.status == 'stable') if session.streams else 0,
                'review_count': sum(1 for s in session.streams.values() if s.status == 'review') if session.streams else 0,
                'quarantined_count': sum(1 for s in session.streams.values() if s.status == 'quarantined' or s.is_quarantined) if session.streams else 0,
                # EPG event info
                'epg_event_id': session.epg_event_id,
                'epg_event_title': session.epg_event_title,
                'epg_event_start': session.epg_event_start,
                'epg_event_end': session.epg_event_end,
                'epg_event_description': session.epg_event_description,
                # Channel info
                'channel_logo_url': session.channel_logo_url,
                'channel_tvg_id': session.channel_tvg_id,
                # Auto-creation info
                'auto_created': session.auto_created,
                'auto_create_rule_id': session.auto_create_rule_id
            }
            sessions_data.append(session_dict)
        
        return jsonify(sessions_data), 200
        
    except Exception as e:
        logger.error(f"Error getting stream sessions: {e}", exc_info=True)
        return jsonify({"error": "Internal Server Error"}), 500


@app.route('/api/stream-sessions', methods=['POST'])
def create_stream_session():
    """Create a new stream monitoring session."""
    try:
        data = request.json
        
        channel_id = data.get('channel_id')
        if not channel_id:
            return jsonify({"error": "channel_id is required"}), 400
        
        # Determine regex and matching config for this channel
        regex_filter = data.get('regex_filter')
        match_by_tvg_id = False
        
        if not regex_filter:
            # Try to get channel's configured regex and match settings
            try:
                regex_matcher = get_regex_matcher()
                
                # Get match config to check for match_by_tvg_id
                group_id = channel.get('group_id') if channel.get('group_id') is not None else channel.get('channel_group_id')
                match_config = regex_matcher.get_channel_match_config(str(channel_id), group_id)
                match_by_tvg_id = match_config.get('match_by_tvg_id', False)
                
                # Get regex filter
                # We default to None if no regex is configured.
                # This ensures that if match_by_tvg_id is False AND no regex is set, we match NOTHING.
                # Previously we defaulted to ".*" which matched EVERYTHING.
                default_regex = None
                regex_filter = regex_matcher.get_channel_regex_filter(str(channel_id), default=default_regex, group_id=group_id)
                
                logger.info(f"Using channel config for manual session: regex='{regex_filter}', match_by_tvg_id={match_by_tvg_id}")
            except Exception as e:
                logger.debug(f"Could not get channel match config: {e}")
                regex_filter = '.*'  # Default fallback if error
        
        pre_event_minutes = data.get('pre_event_minutes', 30)
        stagger_ms = data.get('stagger_ms', 200)
        timeout_ms = data.get('timeout_ms', 30000)
        
        # Extract EPG event if provided
        epg_event = data.get('epg_event')
        
        # Extract auto-creation flags if provided
        auto_created = data.get('auto_created', False)
        auto_create_rule_id = data.get('auto_create_rule_id')
        
        session_manager = get_session_manager()
        session_id = session_manager.create_session(
            channel_id=channel_id,
            regex_filter=regex_filter,
            pre_event_minutes=pre_event_minutes,
            stagger_ms=stagger_ms,
            timeout_ms=timeout_ms,
            epg_event=epg_event,
            auto_created=auto_created,
            auto_create_rule_id=auto_create_rule_id,
            match_by_tvg_id=match_by_tvg_id,
            enable_looping_detection=data.get('enable_looping_detection', True),
            enable_logo_detection=data.get('enable_logo_detection', True)
        )
        
        return jsonify({"session_id": session_id, "message": "Session created successfully"}), 201
        
    except ValueError as e:
        return jsonify({"error": "Invalid value or request parameters"}), 400
    except Exception as e:
        logger.error(f"Error creating stream session: {e}", exc_info=True)
        return jsonify({"error": "Internal Server Error"}), 500


@app.route('/api/stream-sessions/group/start', methods=['POST'])
def create_group_stream_sessions():
    """Create and start monitoring sessions for all channels in a group."""
    try:
        data = request.json
        
        group_id = data.get('group_id')
        if not group_id:
            return jsonify({"error": "group_id is required"}), 400
        
        # Get channels in group
        udi = get_udi_manager()
        channels = udi.get_channels_by_group(group_id)
        
        if not channels:
            return jsonify({"error": "Group not found or has no channels"}), 404
        
        # Refresh global stream list once before creating sessions
        # This ensures all new streams are discovered without hammering the API in the loop
        if udi.refresh_streams():
            logger.info(f"Refreshed stream list before batch session creation for group {group_id}")
        
        # Common parameters
        regex_filter = data.get('regex_filter')
        pre_event_minutes = data.get('pre_event_minutes', 30)
        stagger_ms = data.get('stagger_ms', 200)
        timeout_ms = data.get('timeout_ms', 30000)
        
        session_manager = get_session_manager()
        monitoring_service = get_monitoring_service()
        
        created_sessions = []
        errors = []
        
        for channel in channels:
            try:
                channel_id = channel.get('id')
                
                # Determine regex and match config for this channel
                channel_regex = regex_filter
                match_by_tvg_id = False
                
                if not channel_regex:
                    # Try to get channel's configured regex and match settings
                    try:
                        regex_matcher = get_regex_matcher()
                        
                        # Get match config
                        group_id_for_channel = channel.get('group_id') if channel.get('group_id') is not None else channel.get('channel_group_id')
                        match_config = regex_matcher.get_channel_match_config(str(channel_id), group_id_for_channel)
                        match_by_tvg_id = match_config.get('match_by_tvg_id', False)
                        
                        # Get regex filter with appropriate default
                        # Default to None so we don't match everything by default if no rules exist
                        default_regex = None
                        channel_regex = regex_matcher.get_channel_regex_filter(str(channel_id), default=default_regex, group_id=group_id_for_channel)
                    except Exception:
                        channel_regex = None
                
                # Create session (skip stream refresh as we did it once above)
                session_id = session_manager.create_session(
                    channel_id=channel_id,
                    regex_filter=channel_regex,
                    pre_event_minutes=pre_event_minutes,
                    stagger_ms=stagger_ms,
                    timeout_ms=timeout_ms,
                    skip_stream_refresh=True,
                    match_by_tvg_id=match_by_tvg_id,
                    enable_looping_detection=data.get('enable_looping_detection', True),
                    enable_logo_detection=data.get('enable_logo_detection', True)
                )
                
                # Start session
                if session_manager.start_session(session_id):
                    created_sessions.append({
                        "session_id": session_id,
                        "channel_id": channel_id,
                        "channel_name": channel.get('name')
                    })
                else:
                    errors.append(f"Failed to start session for channel {channel.get('name')} ({channel_id})")
                    
            except Exception as e:
                errors.append(f"Error creating session for channel {channel.get('name')} ({channel_id}): {e}")
        
        # Ensure monitoring service is running if we started any sessions
        if created_sessions and not monitoring_service._running:
            monitoring_service.start()
        
        return jsonify({  # lgtm [py/stack-trace-exposure]
            "message": f"Started {len(created_sessions)} sessions from group {group_id}",
            "sessions": created_sessions,
            "errors": errors
        }), 200 if created_sessions else 400
        
    except Exception as e:
        logger.error(f"Error creating group stream sessions: {e}", exc_info=True)
        return jsonify({"error": "Internal Server Error"}), 500


@app.route('/api/stream-sessions/<session_id>', methods=['GET'])
def get_stream_session(session_id):
    """Get detailed information about a specific session including all streams."""
    try:
        session_manager = get_session_manager()
        session = session_manager.get_session(session_id)
        
        since_timestamp = request.args.get('since_timestamp', type=float)
        
        if not session:
            return jsonify({"error": "Session not found"}), 404
        
        # Fetch historical telemetry for ALL streams in this channel since session creation
        # (Fallback for inactive sessions or loaded from database with empty in-memory history)
        from apps.database.connection import get_session as get_db_session
        from apps.database.models import StreamTelemetry, Run
        
        db_metrics_by_stream = {}
        db_session = get_db_session()
        try:
            telemetry_rows = db_session.query(StreamTelemetry, Run.timestamp).join(Run).filter(
                StreamTelemetry.channel_id == session.channel_id,
                Run.timestamp >= datetime.fromtimestamp(session.created_at)
            ).order_by(Run.timestamp.asc()).all()
            
            for row, ts in telemetry_rows:
                from apps.stream.stream_session_manager import StreamMetrics
                m = StreamMetrics(
                    timestamp=ts.timestamp(),
                    speed=0.0 if row.is_dead else 1.0,
                    bitrate=row.bitrate_kbps or 0,
                    fps=row.fps or 0.0,
                    is_alive=not row.is_dead,
                    buffering=False,
                    reliability_score=row.quality_score or 50.0,
                    status='stable' if not row.is_dead else 'quarantined',
                    status_reason=None,
                    rank=None,
                    loop_duration=None,
                    display_logo_status='SUCCESS' if not row.is_dead else 'PENDING'
                )
                if row.stream_id not in db_metrics_by_stream:
                    db_metrics_by_stream[row.stream_id] = []
                db_metrics_by_stream[row.stream_id].append(m)
        except Exception as e:
            logger.error(f"Error loading historical telemetry for session {session_id}: {e}")
        finally:
            db_session.close()

        # Build detailed session data
        streams_data = []
        if session.streams:
            for stream_id, stream_info in session.streams.items():
                db_metrics = db_metrics_by_stream.get(stream_id, [])
                # Use in-memory if available, fallback to DB history
                stream_metrics_history = stream_info.metrics_history if stream_info.metrics_history else db_metrics
                
                stream_dict = {
                    'stream_id': stream_info.stream_id,
                    'url': stream_info.url,
                    'name': stream_info.name,
                    'channel_id': stream_info.channel_id,
                    'width': stream_info.width,
                    'height': stream_info.height,
                    'fps': stream_info.fps,
                    'bitrate': stream_info.bitrate,
                    'm3u_account': stream_info.m3u_account,
                    'hdr_format': stream_info.hdr_format,
                    'status': stream_info.status,
                    'status_reason': getattr(stream_info, 'status_reason', None),
                    'transport_health': getattr(stream_info, 'transport_health', 'Healthy'),
                    'transport_health_summary': getattr(stream_info, 'transport_health_summary', ''),
                    'transport_error_density': getattr(stream_info, 'transport_error_density', 0.0),
                    'last_loop_time': getattr(stream_info, 'last_loop_time', None),
                    'loop_duration': getattr(stream_info, 'loop_duration', None),
                    'is_quarantined': stream_info.is_quarantined,
                    'reliability_score': stream_info.reliability_score,
                    'current_speed': stream_metrics_history[-1].speed if stream_metrics_history else 0.0,
                    'rank': stream_info.rank,
                    'last_logo_status': getattr(stream_info, 'last_logo_status', 'PENDING'),
                    'display_logo_status': getattr(stream_info, 'display_logo_status', 'PENDING'),
                    'consecutive_logo_misses': getattr(stream_info, 'consecutive_logo_misses', 0),
                    'screenshot_path': stream_info.screenshot_path,
                    'screenshot_url': f"/api/data/screenshots/{Path(stream_info.screenshot_path).name}?t={int(stream_info.last_screenshot_time)}" if stream_info.screenshot_path else None,
                    'last_screenshot_time': stream_info.last_screenshot_time,
                    'metrics_count': len(stream_metrics_history) if stream_metrics_history else 0,
                    'metrics_history': [asdict(m) for m in stream_metrics_history if since_timestamp is None or m.timestamp > since_timestamp][-3600:] if stream_metrics_history else []
                }
                
                # Calculate review time remaining
                if stream_info.status == 'review':
                    time_in_review = time.time() - stream_info.last_status_change
                    review_limit = session_manager.get_review_duration()
                    remaining = max(0, review_limit - time_in_review)
                    stream_dict['review_time_remaining'] = remaining
                
                streams_data.append(stream_dict)
        
        # Sort streams to match Dispatcharr channel order
        # This ensures the frontend sees the exact same order as Dispatcharr
        try:
            from apps.udi import get_udi_manager
            udi = get_udi_manager()
            channel = udi.get_channel_by_id(session.channel_id)
            
            if channel and 'streams' in channel:
                # Create a map of stream_id -> index for O(1) lookup
                order_map = {sid: i for i, sid in enumerate(channel['streams'])}
                
                # Sort: 
                # 1. Streams in Dispatcharr (by index)
                # 2. Streams not in Dispatcharr (e.g. quarantined) - put at end
                # 3. Quarantined streams sorted by reliability score as tie breaker
                
                def get_sort_key(stream_data):
                    sid = stream_data['stream_id']
                    if sid in order_map:
                        return (0, order_map[sid])
                    return (1, -stream_data['reliability_score']) # Non-channel streams sorted by score desc
                
                streams_data.sort(key=get_sort_key)
                
                # Update rank in the return data based on final sorted order for UI consistency
                for i, sdata in enumerate(streams_data, start=1):
                    sdata['rank'] = i
            else:
                # Fallback if channel not found: sort by reliability score
                streams_data.sort(key=lambda x: x['reliability_score'], reverse=True)
                
        except Exception as e:
            logger.warning(f"Failed to sort streams by channel order: {e}")
            # Fallback
            streams_data.sort(key=lambda x: x['reliability_score'], reverse=True)
        
        session_dict = {
            'session_id': session.session_id,
            'channel_id': session.channel_id,
            'channel_name': session.channel_name,
            'regex_filter': session.regex_filter,
            'created_at': session.created_at,
            'is_active': session.is_active,
            'pre_event_minutes': session.pre_event_minutes,
            'stagger_ms': session.stagger_ms,
            'timeout_ms': session.timeout_ms,
            'probe_interval_ms': session.probe_interval_ms,
            'screenshot_interval_seconds': session.screenshot_interval_seconds,
            'window_size': session.window_size,
            'streams': streams_data,
            'ad_periods': session.ad_periods,
            # EPG event info
            'epg_event_id': session.epg_event_id,
            'epg_event_title': session.epg_event_title,
            'epg_event_start': session.epg_event_start,
            'epg_event_end': session.epg_event_end,
            'epg_event_description': session.epg_event_description,
            # Channel info
            'channel_logo_url': session.channel_logo_url,
            'channel_tvg_id': session.channel_tvg_id,
            # Auto-creation info
            'auto_created': session.auto_created,
            'auto_create_rule_id': session.auto_create_rule_id
        }
        
        return jsonify(session_dict), 200
        
    except Exception as e:
        logger.error(f"Error getting stream session {session_id}: {e}", exc_info=True)
        return jsonify({"error": "Internal Server Error"}), 500


@app.route('/api/stream-sessions/<session_id>/streams/<int:stream_id>/quarantine', methods=['POST'])
def quarantine_stream(session_id, stream_id):
    """Quarantine a stream in a session."""
    try:
        session_manager = get_session_manager()
        
        if not session_manager.quarantine_stream(session_id, stream_id):
            return jsonify({"error": "Failed to quarantine stream. It may already be quarantined or session/stream not found."}), 400
            
        return jsonify({"success": True}), 200
        
    except Exception as e:
        logger.error(f"Error quarantining stream {stream_id} in {session_id}: {e}", exc_info=True)
        return jsonify({"error": "Internal Server Error"}), 500


@app.route('/api/stream-sessions/<session_id>/streams/<int:stream_id>/revive', methods=['POST'])
def revive_stream(session_id, stream_id):
    """Revive a quarantined stream in a session"""
    try:
        session_manager = get_session_manager()
        
        if not session_manager.revive_stream(session_id, stream_id):
            return jsonify({"error": "Failed to revive stream. It may not be quarantined or session not found."}), 400
            
        return jsonify({"success": True}), 200
        
    except Exception as e:
        logger.error(f"Error reviving stream {stream_id} in {session_id}: {e}", exc_info=True)
        return jsonify({"error": "Internal Server Error"}), 500


@app.route('/api/stream-sessions/<session_id>/start', methods=['POST'])
def start_stream_session(session_id):
    """Start monitoring for a session."""
    try:
        session_manager = get_session_manager()
        
        if not session_manager.start_session(session_id):
            return jsonify({"error": "Failed to start session"}), 400
        
        # Ensure monitoring service is running
        monitoring_service = get_monitoring_service()
        if not monitoring_service._running:
            monitoring_service.start()
        
        return jsonify({"message": "Session started successfully"}), 200
        
    except Exception as e:
        logger.error(f"Error starting stream session {session_id}: {e}", exc_info=True)
        return jsonify({"error": "Internal Server Error"}), 500


@app.route('/api/stream-sessions/<session_id>/stop', methods=['POST'])
def stop_stream_session(session_id):
    """Stop monitoring for a session."""
    try:
        session_manager = get_session_manager()
        
        if not session_manager.stop_session(session_id):
            return jsonify({"error": "Failed to stop session"}), 400
        
        return jsonify({"message": "Session stopped successfully"}), 200
        
    except Exception as e:
        logger.error(f"Error stopping stream session {session_id}: {e}", exc_info=True)
        return jsonify({"error": "Internal Server Error"}), 500


@app.route('/api/stream-sessions/<session_id>', methods=['DELETE'])
def delete_stream_session(session_id):
    """Delete a session."""
    try:
        session_manager = get_session_manager()
        
        if not session_manager.delete_session(session_id):
            return jsonify({"error": "Session not found"}), 404
        
        return jsonify({"message": "Session deleted successfully"}), 200
        
    except Exception as e:
        logger.error(f"Error deleting stream session {session_id}: {e}", exc_info=True)
        return jsonify({"error": "Internal Server Error"}), 500


@app.route('/api/stream-sessions/batch/stop', methods=['POST'])
def batch_stop_sessions():
    """Stop multiple monitoring sessions."""
    try:
        data = request.json
        session_ids = data.get('session_ids', [])
        
        if not session_ids:
            return jsonify({"error": "No session_ids provided"}), 400
            
        session_manager = get_session_manager()
        success_count = 0
        failed_count = 0
        errors = []
        
        for session_id in session_ids:
            if session_manager.stop_session(session_id):
                success_count += 1
            else:
                failed_count += 1
                errors.append(f"Failed to stop session {session_id}")
        
        return jsonify({
            "message": f"Processed {len(session_ids)} sessions",
            "success_count": success_count,
            "failed_count": failed_count,
            "errors": errors
        }), 200
        
    except Exception as e:
        logger.error(f"Error in batch stop sessions: {e}", exc_info=True)
        return jsonify({"error": "Internal Server Error"}), 500


@app.route('/api/stream-sessions/batch/delete', methods=['POST'])
def batch_delete_sessions():
    """Delete multiple monitoring sessions."""
    try:
        data = request.json
        session_ids = data.get('session_ids', [])
        
        if not session_ids:
            return jsonify({"error": "No session_ids provided"}), 400
            
        session_manager = get_session_manager()
        success_count = 0
        failed_count = 0
        errors = []
        
        for session_id in session_ids:
            if session_manager.delete_session(session_id):
                success_count += 1
            else:
                failed_count += 1
                errors.append(f"Failed to delete session {session_id}")
        
        return jsonify({
            "message": f"Processed {len(session_ids)} sessions",
            "success_count": success_count,
            "failed_count": failed_count,
            "errors": errors
        }), 200
        
    except Exception as e:
        logger.error(f"Error in batch delete sessions: {e}", exc_info=True)
        return jsonify({"error": "Internal Server Error"}), 500


@app.route('/api/stream-sessions/<session_id>/streams/<int:stream_id>/metrics', methods=['GET'])
def get_stream_metrics(session_id, stream_id):
    """Get historical metrics for a stream in a session."""
    try:
        session_manager = get_session_manager()
        session = session_manager.get_session(session_id)
        
        if not session:
            return jsonify({"error": "Session not found"}), 404
        
        stream_info = session.streams.get(stream_id)
        if not stream_info:
            return jsonify({"error": "Stream not found in session"}), 404
        
        # Get metrics history
        since_ts = request.args.get('since_timestamp', type=float)
        metrics_data = []
        if stream_info.metrics_history:
            for metric in stream_info.metrics_history:
                if since_ts is not None and metric.timestamp <= since_ts:
                    continue
                metrics_data.append({
                    'timestamp': metric.timestamp,
                    'speed': metric.speed,
                    'bitrate': metric.bitrate,
                    'fps': metric.fps,
                    'is_alive': metric.is_alive,
                    'buffering': metric.buffering,
                    'reliability_score': getattr(metric, 'reliability_score', 50.0),
                    'status': getattr(metric, 'status', 'review'),
                    'rank': getattr(metric, 'rank', None)
                })
        
        # Explicit bound to maintain equivalent sync with frontend limitations
        metrics_data = metrics_data[-3600:]

        return jsonify({
            'stream_id': stream_id,
            'metrics': metrics_data
        }), 200
        
    except Exception as e:
        logger.error(f"Error getting stream metrics: {e}", exc_info=True)
        return jsonify({"error": "Internal Server Error"}), 500


# Serve screenshots
@app.route('/api/data/screenshots/<filename>')
def serve_screenshot(filename):
    """Serve screenshot files."""
    try:
        screenshots_dir = CONFIG_DIR / 'screenshots'
        response = make_response(send_from_directory(screenshots_dir, filename))
        response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
        response.headers['Pragma'] = 'no-cache'
        response.headers['Expires'] = '0'
        return response
    except Exception as e:
        logger.error(f"Error serving screenshot {filename}: {e}")
        return jsonify({"error": "Screenshot not found"}), 404


@app.route('/api/stream-sessions/<session_id>/alive-screenshots', methods=['GET'])
def get_alive_screenshots(session_id):
    """Get screenshots and info for all alive (non-quarantined) streams in a session."""
    try:
        session_manager = get_session_manager()
        session = session_manager.get_session(session_id)
        
        if not session:
            return jsonify({"error": "Session not found"}), 404
        
        # Get all alive streams with screenshots
        screenshots_data = []
        if session.streams:
            for stream_id, stream_info in session.streams.items():
                if not stream_info.is_quarantined and stream_info.screenshot_path:
                    # Only show screenshots for streams with good speed (>= 1.0)
                    latest_speed = 0.0
                    if stream_info.metrics_history:
                        latest_speed = stream_info.metrics_history[-1].speed
                    
                    if latest_speed >= 0.9:
                        screenshots_data.append({
                        'stream_id': stream_info.stream_id,
                        'stream_name': stream_info.name,
                        'screenshot_url': f"/api/data/screenshots/{Path(stream_info.screenshot_path).name}?t={int(stream_info.last_screenshot_time)}",
                        'reliability_score': stream_info.reliability_score,
                        'm3u_account': stream_info.m3u_account
                    })
        
        # Sort by reliability score descending
        screenshots_data.sort(key=lambda x: x['reliability_score'], reverse=True)
        
        return jsonify({
            'session_id': session_id,
            'screenshots': screenshots_data
        }), 200
        
    except Exception as e:
        logger.error(f"Error getting alive screenshots for session {session_id}: {e}", exc_info=True)
        return jsonify({"error": "Internal Server Error"}), 500





@app.route('/api/proxy/status', methods=['GET'])
def get_proxy_status():
    """Get current proxy status showing which streams are being played."""
    try:
        udi = get_udi_manager()
        proxy_status = udi.get_proxy_status()
        
        return jsonify(proxy_status), 200
        
    except Exception as e:
        logger.error(f"Error getting proxy status: {e}", exc_info=True)
        return jsonify({"error": "Internal Server Error"}), 500


@app.route('/api/proxy/playing-streams', methods=['GET'])
def get_playing_streams():
    """Get list of stream IDs that are currently being played."""
    try:
        udi = get_udi_manager()
        playing_stream_ids = udi.get_playing_stream_ids()
        
        return jsonify({
            "playing_stream_ids": list(playing_stream_ids),
            "count": len(playing_stream_ids)
        }), 200
        
    except Exception as e:
        logger.error(f"Error getting playing streams: {e}", exc_info=True)
        return jsonify({"error": "Internal Server Error"}), 500


@app.route('/api/stream-viewer/<int:stream_id>', methods=['GET'])
def get_stream_viewer_url(stream_id):
    """Get the stream's direct HLS URL for live viewing in browser."""
    try:
        udi = get_udi_manager()
        stream = udi.get_stream_by_id(stream_id)
        
        if not stream:
            return jsonify({
                'success': False,
                'error': f'Stream {stream_id} not found'
            }), 404
        
        # Use the direct MPEG-TS proxy URL
        # Return the upstream URL directly to save CPU overhead
        stream_url = stream.get('url')
        
        return jsonify({
            'success': True,
            'stream_url': stream_url,
            'stream_id': stream_id,
            'stream_name': stream.get('name', 'Unknown')
        })
    except Exception as e:
        logger.error(f"Error getting stream viewer URL: {e}", exc_info=True)
        return jsonify({"error": "Internal Server Error"}), 500




@app.route('/api/stream/proxy/<int:stream_id>', methods=['GET'])
def stream_proxy_url(stream_id):
    """Proxy the local UDP stream from FFmpeg out via HTTP using a shared listener."""
    proxy = udp_proxy_manager.get_proxy(stream_id)
    q = proxy.add_client()
    
    def generate():
        try:
            # Use a slightly longer timeout for the initial packet to account for FFmpeg startup
            while True:
                try:
                    # Block on the queue with a timeout
                    data = q.get(timeout=10.0)
                    yield data
                except queue.Empty:
                    # If we time out and the proxy is no longer running, stop
                    if not proxy.running:
                        logger.warning(f"Shared proxy for {stream_id} stopped, closing HTTP client")
                        break
                    # Otherwise, just keep waiting – maybe just a temporary gap in stream
                    continue
        except Exception as e:
            logger.debug(f"HTTP streaming client for {stream_id} disconnected: {e}")
        finally:
            proxy.remove_client(q)

    return Response(generate(), mimetype='video/mp2t')


@app.route('/api/scheduled-events/<event_id>/create-session', methods=['POST'])
def create_session_from_event(event_id):
    """Create a monitoring session from a scheduled event."""
    try:
        scheduling_service = get_scheduling_service()
        session_id = scheduling_service.create_session_from_event(event_id)
        
        if not session_id:
            return jsonify({"error": "Failed to create session from event"}), 400
        
        return jsonify({
            "session_id": session_id,
            "message": "Session created successfully from event"
        }), 201
        
    except Exception as e:
        logger.error(f"Error creating session from event {event_id}: {e}", exc_info=True)
        return jsonify({"error": "Internal Server Error"}), 500



# Serve React app for all frontend routes (catch-all - must be last!)


# ==================== Settings API ====================

@app.route('/api/settings/session', methods=['GET', 'POST'])
def handle_session_settings():
    """Get or update session settings (like review duration)."""
    try:
        session_manager = get_session_manager()
        
        if request.method == 'GET':
            return jsonify({
                "review_duration": session_manager.get_review_duration(),
                "loop_review_duration": session_manager.get_loop_review_duration()
            })
            
        elif request.method == 'POST':
            data = request.json
            duration = data.get('review_duration')
            loop_duration = data.get('loop_review_duration')
            
            updated = {}
            if duration is not None:
                try:
                    val = float(duration)
                    if val >= 0:
                        session_manager.set_review_duration(val)
                        updated['review_duration'] = val
                except ValueError: pass
                
            if loop_duration is not None:
                try:
                    val = float(loop_duration)
                    if val >= 0:
                        session_manager.set_loop_review_duration(val)
                        updated['loop_review_duration'] = val
                except ValueError: pass
            
            if updated:
                return jsonify({"message": "Settings updated", **updated})
            
            return jsonify({"error": "No settings provided"}), 400
            
    except Exception as e:
        logger.error(f"Error handling session settings: {e}", exc_info=True)
        return jsonify({"error": "Internal Server Error"}), 500


# Serve React app for all frontend routes (catch-all - must be last!)
@app.route('/<path:path>')
def serve_frontend(path):
    """Serve React frontend files or return index.html for client-side routing."""
    from werkzeug.utils import safe_join
    resolved_path_str = safe_join(str(static_folder), path)
    if resolved_path_str is None:
        return jsonify({"error": "Invalid path"}), 400
    resolved_path = Path(resolved_path_str)

    if resolved_path.exists() and resolved_path.is_file():
        return send_from_directory(static_folder, path)
    else:
        # Return index.html for client-side routing (React Router)
        try:
            return send_file(static_folder / 'index.html')
        except FileNotFoundError:
            return jsonify({"error": "Frontend not found"}), 404
# --- Telemetry API ---
from apps.telemetry.telemetry_api import telemetry_bp
app.register_blueprint(telemetry_bp, url_prefix='/api/telemetry')



if __name__ == '__main__':
    import argparse
    from apps.database.connection import init_db
    
    parser = argparse.ArgumentParser(description='StreamFlow for Dispatcharr Web API')
    parser.add_argument('--host', default=os.environ.get('API_HOST', '0.0.0.0'), help='Host to bind to')
    parser.add_argument('--port', type=int, default=int(os.environ.get('API_PORT', '5000')), help='Port to bind to')
    parser.add_argument('--debug', action='store_true', help='Enable debug mode')
    
    args = parser.parse_args()
    
    logger.info(f"Starting StreamFlow for Dispatcharr Web API on {args.host}:{args.port}")

    try:
        init_db()
        logger.info("Database schema initialization completed")
    except Exception as e:
        logger.error(f"Failed to initialize database schema: {e}", exc_info=True)
        raise
    
    # Only start background services in the reloader process (or if not using reloader)
    # WERKZEUG_RUN_MAIN is set to 'true' in the child process spawned by the reloader
    if not args.debug or os.environ.get("WERKZEUG_RUN_MAIN") == "true":
        logger.info("Starting background services (active process)...")
        
        # Auto-start stream checker service if enabled and automation is configured AND wizard is complete
        try:
            # Check if wizard has been completed
            if not check_wizard_complete():
                logger.info("Stream checker service will not start - setup wizard has not been completed")
            else:
                service = get_stream_checker_service()
                automation_controls = service.config.get('automation_controls', {})
                
                # Check if any automation is enabled
                any_automation_enabled = (
                    automation_controls.get('auto_m3u_updates', True) or
                    automation_controls.get('auto_stream_matching', True) or
                    automation_controls.get('auto_quality_checking', True) or
                    automation_controls.get('scheduled_global_action', False)
                )
                
                if not any_automation_enabled:
                    logger.info("Stream checker service is disabled (all automation controls disabled)")
                elif service.config.get('enabled', True):
                    service.start()
                    logger.info(f"Stream checker service auto-started")
                else:
                    logger.info("Stream checker service is disabled in configuration")
        except Exception as e:
            logger.error(f"Failed to auto-start stream checker service: {e}")
            
        # Register signal handlers for graceful shutdown (SIGTERM/SIGINT)
        try:
            import signal
            import sys
            
            def graceful_shutdown(signum, frame):
                logger.info(f"Received signal {signum}. Starting graceful shutdown...")
                
                try:
                    from apps.stream.stream_monitoring_service import get_monitoring_service
                    monitor = get_monitoring_service()
                    if monitor:
                        logger.info("Stopping Stream Monitoring Service...")
                        monitor.stop()
                except Exception as e:
                    logger.error(f"Error stopping monitoring service: {e}")
                    
                try:
                    from apps.stream.stream_checker_service import get_stream_checker_service
                    checker = get_stream_checker_service()
                    if checker:
                        logger.info("Stopping Stream Checker Service...")
                        checker.stop()
                except Exception as e:
                    logger.error(f"Error stopping stream checker service: {e}")
                    
                try:
                    stop_scheduled_event_processor()
                    stop_epg_refresh_processor()
                except Exception:
                    pass
                    
                logger.info("Graceful shutdown complete. Exiting.")
                sys.exit(0)

            signal.signal(signal.SIGTERM, graceful_shutdown)
            signal.signal(signal.SIGINT, graceful_shutdown)
            logger.info("Signal handlers registered for SIGTERM and SIGINT")
        except Exception as e:
            logger.error(f"Failed to register signal handlers: {e}")
        
        # Auto-start automation service if automation is configured AND wizard is complete
        try:
            # Check if wizard has been completed
            if not check_wizard_complete():
                logger.info("Automation service will not start - setup wizard has not been completed")
            else:
                manager = get_automation_manager()
                service = get_stream_checker_service()
                automation_controls = service.config.get('automation_controls', {})
                
                # Check the master switch for regular automation
                from apps.automation.automation_config_manager import get_automation_config_manager
                automation_config = get_automation_config_manager()
                global_settings = automation_config.get_global_settings()
                regular_automation_enabled = global_settings.get('regular_automation_enabled', False)
                
                if not regular_automation_enabled:
                    logger.info("Automation service is disabled (regular_automation_enabled is False)")
                else:
                    # Auto-start automation
                    manager.start_automation()
                    logger.info(f"Automation service auto-started")
        except Exception as e:
            logger.error(f"Failed to auto-start automation service: {e}")
        
        # Auto-start scheduled event processor if wizard is complete
        try:
            if not check_wizard_complete():
                logger.info("Scheduled event processor will not start - setup wizard has not been completed")
            else:
                start_scheduled_event_processor()
                logger.info("Scheduled event processor auto-started")
        except Exception as e:
            logger.error(f"Failed to auto-start scheduled event processor: {e}")
        
        # Auto-start EPG refresh processor if wizard is complete
        try:
            if not check_wizard_complete():
                logger.info("EPG refresh processor will not start - setup wizard has not been completed")
            else:
                start_epg_refresh_processor()
                logger.info("EPG refresh processor auto-started")
        except Exception as e:
            logger.error(f"Failed to auto-start EPG refresh processor: {e}")
        
        # Auto-start stream monitoring service (always starts, independent of wizard)
        try:
            monitoring_service = get_monitoring_service()
            monitoring_service.start()
            logger.info("Stream monitoring service auto-started")
        except Exception as e:
            logger.error(f"Failed to auto-start stream monitoring service: {e}")
            
        # Force a full UDI Refresh on startup in the background if wizard is complete
        try:
            if check_wizard_complete():
                def startup_udi_refresh():
                    logger.info("Initializing UDI Manager with fresh data from Dispatcharr on startup...")
                    try:
                        from apps.core.api_utils import get_udi_manager
                        udi = get_udi_manager()
                        udi.initialize(force_refresh=True)
                    except Exception as e:
                        logger.error(f"Background startup UDI refresh failed: {e}")
                        
                import threading
                threading.Thread(target=startup_udi_refresh, name="Startup-UDI-Refresh", daemon=True).start()
        except Exception as e:
             logger.error(f"Failed to start UDI background refresh thread: {e}")
    else:
        logger.info("Skipping background service startup in reloader parent process")
    
    if args.debug:
        logger.info(f"Starting development server on {args.host}:{args.port}")
        app.run(host=args.host, port=args.port, debug=args.debug)
    else:
        logger.info(f"Starting production WSGI server on {args.host}:{args.port}")
        try:
            from waitress import serve
            serve(app, host=args.host, port=args.port, threads=8)
        except ImportError:
            logger.warning("Waitress not installed, falling back to development server.")
            logger.warning("Please install waitress: pip install waitress")
            app.run(host=args.host, port=args.port, debug=False)
