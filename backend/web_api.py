#!/usr/bin/env python3
"""
Web API Server for StreamFlow for Dispatcharr

Provides REST API endpoints for the React frontend to interact with
the automated stream management system.
"""

import json
import logging
import os
import re
import requests
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, Any
from werkzeug.utils import secure_filename

from flask import Flask, request, jsonify, send_from_directory, send_file
from flask_cors import CORS

from automated_stream_manager import AutomatedStreamManager, RegexChannelMatcher
from api_utils import _get_base_url
from stream_checker_service import get_stream_checker_service
from scheduling_service import get_scheduling_service
from channel_settings_manager import get_channel_settings_manager
from dispatcharr_config import get_dispatcharr_config
from channel_order_manager import get_channel_order_manager
from profile_config import ProfileConfig

# Pre-compiled regex pattern for whitespace conversion (performance optimization)
# This pattern matches one or more spaces that are NOT preceded by a backslash
# Used to convert literal spaces to flexible whitespace while preserving escaped spaces
_WHITESPACE_PATTERN = re.compile(r'(?<!\\) +')

# Import UDI for direct data access
from udi import get_udi_manager

# Import centralized stream stats utilities
from stream_stats_utils import (
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
from logging_config import setup_logging, log_function_call, log_function_return, log_exception

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

# Initialize Flask app with static file serving
# Note: static_folder set to None to disable Flask's built-in static route
# The catch-all route will handle serving all static files from the React build
static_folder = Path(__file__).parent / 'static'
app = Flask(__name__, static_folder=None)
CORS(app)  # Enable CORS for React frontend

# Global instances
automation_manager = None
regex_matcher = None
scheduled_event_processor_thread = None
scheduled_event_processor_running = False
scheduled_event_processor_wake = None  # threading.Event to wake up the processor early
epg_refresh_thread = None
epg_refresh_running = False
epg_refresh_wake = None  # threading.Event to wake up the refresh early

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
        config_file = CONFIG_DIR / 'automation_config.json'
        regex_file = CONFIG_DIR / 'channel_regex_config.json'
        
        # Check if configuration files exist
        if not config_file.exists() or not regex_file.exists():
            return False
        
        # Check if we can connect to Dispatcharr (optional - use cached result)
        # For startup, we'll accept the configuration exists as sufficient
        # The actual connection test will be done by the wizard
        
        return True
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
            programs = service.fetch_epg_grid(force_refresh=True)
            logger.info(f"EPG refresh complete. Fetched {len(programs)} programs.")
            
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

@app.route('/api/automation/status', methods=['GET'])
def get_automation_status():
    """Get current automation status."""
    try:
        manager = get_automation_manager()
        status = manager.get_status()
        return jsonify(status)
    except Exception as e:
        logger.error(f"Error getting automation status: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/automation/start', methods=['POST'])
def start_automation():
    """Start the automation system."""
    try:
        manager = get_automation_manager()
        manager.start_automation()
        return jsonify({"message": "Automation started successfully", "status": "running"})
    except Exception as e:
        logger.error(f"Error starting automation: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/automation/stop', methods=['POST'])
def stop_automation():
    """Stop the automation system."""
    try:
        manager = get_automation_manager()
        manager.stop_automation()
        return jsonify({"message": "Automation stopped successfully", "status": "stopped"})
    except Exception as e:
        logger.error(f"Error stopping automation: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/automation/cycle', methods=['POST'])
def run_automation_cycle():
    """Run one automation cycle manually."""
    try:
        manager = get_automation_manager()
        manager.run_automation_cycle()
        return jsonify({"message": "Automation cycle completed successfully"})
    except Exception as e:
        logger.error(f"Error running automation cycle: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/automation/config', methods=['GET'])
def get_automation_config():
    """Get automation configuration."""
    try:
        manager = get_automation_manager()
        return jsonify(manager.config)
    except Exception as e:
        logger.error(f"Error getting automation config: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/automation/config', methods=['PUT'])
def update_automation_config():
    """Update automation configuration."""
    try:
        data = request.get_json()
        if not data:
            return jsonify({"error": "No configuration data provided"}), 400
        
        manager = get_automation_manager()
        manager.update_config(data)
        return jsonify({"message": "Configuration updated successfully", "config": manager.config})
    except Exception as e:
        logger.error(f"Error updating automation config: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/channels', methods=['GET'])
def get_channels():
    """Get all channels from UDI with custom ordering applied."""
    try:
        udi = get_udi_manager()
        channels = udi.get_channels()
        
        if channels is None:
            return jsonify({"error": "Failed to fetch channels"}), 500
        
        # Apply custom channel order if configured
        order_manager = get_channel_order_manager()
        channels = order_manager.apply_order(channels)
        
        return jsonify(channels)
    except Exception as e:
        logger.error(f"Error fetching channels: {e}")
        return jsonify({"error": str(e)}), 500

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
        return jsonify({"error": str(e)}), 500

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
        return jsonify({"error": str(e)}), 500

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
        return jsonify({"error": str(e)}), 500

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
        return jsonify({"error": f"Failed to download logo: {str(e)}"}), 500
    except Exception as e:
        logger.error(f"Error caching logo {logo_id}: {e}")
        return jsonify({"error": str(e)}), 500

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
        return jsonify({"error": str(e)}), 500

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
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        logger.error(f"Error adding regex pattern: {e}")
        return jsonify({"error": str(e)}), 500

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
        return jsonify({"error": str(e)}), 500

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
            return jsonify({"error": error_msg}), 400
        
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
                
                # Get existing patterns for this channel
                patterns = matcher.get_patterns()
                existing_patterns = patterns.get('patterns', {}).get(str(channel_id), {})
                existing_regex = existing_patterns.get('regex', [])
                existing_m3u_accounts = existing_patterns.get('m3u_accounts')
                
                # Merge with new patterns (avoid duplicates)
                merged_regex = list(existing_regex)
                for pattern in regex_patterns:
                    if pattern not in merged_regex:
                        merged_regex.append(pattern)
                
                # Use provided m3u_accounts or keep existing
                final_m3u_accounts = m3u_accounts if m3u_accounts is not None else existing_m3u_accounts
                
                # Add/update pattern
                matcher.add_channel_pattern(
                    str(channel_id),
                    channel_name,
                    merged_regex,
                    existing_patterns.get('enabled', True),
                    m3u_accounts=final_m3u_accounts
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
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        logger.error(f"Error adding bulk regex patterns: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/regex-patterns/import', methods=['POST'])
def import_regex_patterns():
    """Import regex patterns from JSON file."""
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
        
        # Validate each pattern
        matcher = get_regex_matcher()
        for channel_id, pattern_data in data['patterns'].items():
            if not isinstance(pattern_data, dict):
                return jsonify({"error": f"Invalid pattern format for channel {channel_id}"}), 400
            
            if 'regex' not in pattern_data:
                return jsonify({"error": f"Missing 'regex' field for channel {channel_id}"}), 400
            
            if not isinstance(pattern_data['regex'], list):
                return jsonify({"error": f"'regex' must be a list for channel {channel_id}"}), 400
            
            # Validate regex patterns
            is_valid, error_msg = matcher.validate_regex_patterns(pattern_data['regex'])
            if not is_valid:
                return jsonify({"error": f"Invalid regex pattern for channel {channel_id}: {error_msg}"}), 400
        
        # If validation passes, save the patterns
        matcher._save_patterns(data)
        
        # Reload patterns to ensure they're in sync
        matcher.reload_patterns()
        
        pattern_count = len(data['patterns'])
        logger.info(f"Imported {pattern_count} regex patterns successfully")
        
        return jsonify({
            "message": f"Successfully imported {pattern_count} patterns",
            "pattern_count": pattern_count
        })
    except Exception as e:
        logger.error(f"Error importing regex patterns: {e}")
        return jsonify({"error": str(e)}), 500

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
            match = re.search(search_pattern, search_name)
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
            return jsonify({"error": f"Invalid regex pattern: {str(e)}"}), 400
    except Exception as e:
        logger.error(f"Error testing regex pattern: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/test-regex-live', methods=['POST'])
def test_regex_pattern_live():
    """Test regex patterns against all available streams to see what would be matched."""
    try:
        from api_utils import get_streams, get_m3u_accounts
        import re
        
        data = request.get_json()
        if not data:
            return jsonify({"error": "Missing request body"}), 400
        
        # Get patterns to test - can be a single pattern or multiple patterns per channel
        patterns = data.get('patterns', [])
        case_sensitive = data.get('case_sensitive', False)
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
                        if re.search(search_pattern, search_name):
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
        return jsonify({"error": str(e)}), 500

# ============================================================
# PROFILE CONFIGURATION API ENDPOINTS
# ============================================================

@app.route('/api/profile-config', methods=['GET'])
@log_function_call
def get_profile_config():
    """Get the current profile configuration.
    
    Returns:
        JSON with profile configuration
    """
    try:
        from profile_config import get_profile_config
        profile_config = get_profile_config()
        
        config = profile_config.get_config()
        
        return jsonify(config)
    except Exception as e:
        logger.error(f"Error getting profile config: {e}")
        return jsonify({"error": str(e)}), 500


@app.route('/api/profile-config', methods=['PUT'])
@log_function_call
def update_profile_config():
    """Update profile configuration.
    
    Expects JSON with configuration options:
    - selected_profile_id: Profile ID to use (null for general)
    - selected_profile_name: Profile name (for display)
    - dead_streams.enabled: Enable empty channel management
    - dead_streams.target_profile_id: Profile to disable channels in
    - dead_streams.target_profile_name: Profile name
    - dead_streams.use_snapshot: Use snapshot for re-enabling
    
    Returns:
        JSON with result
    """
    try:
        from profile_config import get_profile_config
        profile_config = get_profile_config()
        
        data = request.get_json()
        
        # Update selected profile
        if 'selected_profile_id' in data or 'use_profile' in data:
            # Get the profile IDs from the request, defaulting to current values if not provided
            profile_id = data.get('selected_profile_id')
            profile_name = data.get('selected_profile_name')
            
            # If use_profile is explicitly set to False, clear the selected profile
            if 'use_profile' in data and not data['use_profile']:
                profile_id = None
                profile_name = None
            
            profile_config.set_selected_profile(profile_id, profile_name)
        
        # Update dead stream config
        if 'dead_streams' in data:
            ds_config = data['dead_streams']
            profile_config.set_dead_stream_config(
                enabled=ds_config.get('enabled'),
                target_profile_id=ds_config.get('target_profile_id'),
                target_profile_name=ds_config.get('target_profile_name'),
                use_snapshot=ds_config.get('use_snapshot')
            )
        
        return jsonify({"message": "Profile configuration updated successfully"})
    except Exception as e:
        logger.error(f"Error updating profile config: {e}")
        return jsonify({"error": str(e)}), 500


@app.route('/api/profiles', methods=['GET'])
@log_function_call
def get_profiles():
    """Get all available channel profiles from Dispatcharr.
    
    Returns:
        JSON list of profiles
    """
    try:
        # Get profiles from UDI
        udi = get_udi_manager()
        profiles = udi.get_channel_profiles()
        
        logger.info(f"Returning {len(profiles)} channel profiles")
        if len(profiles) == 0:
            logger.warning("No channel profiles found in UDI cache")
        
        return jsonify(profiles)
    except Exception as e:
        logger.error(f"Error getting profiles: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


@app.route('/api/profiles/refresh', methods=['POST'])
@log_function_call
def refresh_profiles():
    """Force refresh channel profiles from Dispatcharr API.
    
    This endpoint can be used to manually trigger a profile refresh
    when profiles are not appearing in the UI.
    
    Returns:
        JSON with refresh status and profile count
    """
    try:
        udi = get_udi_manager()
        logger.info("Forcing channel profiles refresh...")
        
        success = udi.refresh_channel_profiles()
        
        if success:
            profiles = udi.get_channel_profiles()
            return jsonify({
                "success": True,
                "message": f"Successfully refreshed {len(profiles)} channel profiles",
                "profile_count": len(profiles),
                "profiles": profiles
            })
        else:
            return jsonify({
                "success": False,
                "message": "Failed to refresh channel profiles"
            }), 500
            
    except Exception as e:
        logger.error(f"Error refreshing profiles: {e}", exc_info=True)
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500


@app.route('/api/profiles/diagnose', methods=['GET'])
@log_function_call
def diagnose_profiles():
    """Diagnostic endpoint to check profile fetching status.
    
    Returns detailed information about:
    - UDI initialization status
    - Profile cache contents
    - Dispatcharr API connectivity
    - Recent refresh attempts
    
    Returns:
        JSON with diagnostic information
    """
    try:
        udi = get_udi_manager()
        
        # Check if UDI is initialized
        is_initialized = udi.is_initialized()
        
        # Get current profile cache
        profiles = udi.get_channel_profiles()
        
        # Check Dispatcharr configuration
        from dispatcharr_config import get_dispatcharr_config
        config = get_dispatcharr_config()
        is_configured = config.is_configured()
        base_url = config.get_base_url()
        
        # Check if profiles exist in storage
        storage_count = udi.get_storage_count('channel_profiles')
        
        # Get last refresh time
        last_refresh = udi.get_cache_last_refresh('channel_profiles')
        
        diagnostic_info = {
            "udi_initialized": is_initialized,
            "dispatcharr_configured": is_configured,
            "dispatcharr_base_url": base_url,
            "cache_profile_count": len(profiles),
            "storage_profile_count": storage_count,
            "last_refresh_time": last_refresh.isoformat() if last_refresh else None,
            "profiles_in_cache": profiles
        }
        
        if len(profiles) == 0:
            diagnostic_info["diagnosis"] = "No profiles found"
            diagnostic_info["possible_causes"] = [
                "No channel profiles have been created in Dispatcharr yet",
                "Profile fetch failed during initialization",
                "Authentication issue preventing API access",
                "Network connectivity problem"
            ]
            diagnostic_info["recommended_actions"] = [
                "Create channel profiles in Dispatcharr web UI (Channels > Profiles)",
                "Click 'Refresh Profiles' button to force a refresh",
                "Check Dispatcharr logs for errors",
                "Verify DISPATCHARR_BASE_URL, DISPATCHARR_USER, and DISPATCHARR_PASS in .env"
            ]
        
        return jsonify(diagnostic_info)
        
    except Exception as e:
        logger.error(f"Error in profile diagnostics: {e}", exc_info=True)
        return jsonify({
            "error": str(e),
            "message": "Failed to run profile diagnostics"
        }), 500


def _get_all_channels_as_enabled():
    """Helper function to get all channels formatted with enabled=True.
    
    This is used as a fallback when profile channel parsing fails.
    
    Returns:
        List of channel dicts in format [{channel_id, enabled}, ...]
    """
    udi = get_udi_manager()
    all_channels = udi.get_channels()
    return [
        {'channel_id': ch['id'], 'enabled': True}
        for ch in all_channels if ch.get('id')
    ]


@app.route('/api/profiles/<int:profile_id>/channels', methods=['GET'])
@log_function_call
def get_profile_channels(profile_id):
    """Get channels for a specific profile from UDI cache.
    
    Uses cached data from UDI instead of making direct API calls to Dispatcharr.
    The cache is updated when playlists are refreshed.
    
    Args:
        profile_id: Profile ID
        
    Query Parameters:
        include_snapshot: If 'true', also include channels from the profile snapshot
                         even if they're currently disabled
        
    Returns:
        JSON with profile and channels
    """
    try:
        # Check if we should include snapshot channels
        include_snapshot = request.args.get('include_snapshot', '').lower() == 'true'
        
        # Get data from UDI cache
        udi = get_udi_manager()
        
        # Get profile info
        profile = udi.get_channel_profile_by_id(profile_id)
        if not profile:
            return jsonify({"error": f"Profile {profile_id} not found in UDI cache"}), 404
        
        # Get cached profile channels
        profile_channels_data = udi.get_profile_channels(profile_id)
        
        if profile_channels_data:
            # Use cached data
            channels = profile_channels_data.get('channels', [])
            
            # If include_snapshot is requested, merge with snapshot channels
            if include_snapshot:
                profile_config = ProfileConfig()
                snapshot = profile_config.get_snapshot(profile_id)
                
                if snapshot:
                    snapshot_channel_ids = set(snapshot.get('channel_ids', []))
                    # Get current channel IDs from the profile
                    current_channel_ids = set()
                    for ch in channels:
                        # Channels can be integers or objects with channel_id
                        if isinstance(ch, int):
                            current_channel_ids.add(ch)
                        elif isinstance(ch, dict):
                            ch_id = ch.get('channel_id') or ch.get('id')
                            if ch_id:
                                current_channel_ids.add(ch_id)
                    
                    # Find channel IDs in snapshot but not in current channels
                    missing_channel_ids = snapshot_channel_ids - current_channel_ids
                    
                    if missing_channel_ids:
                        logger.info(f"Including {len(missing_channel_ids)} snapshot channels that are not currently in profile {profile_id}")
                        # Add the missing channels to the list
                        # Convert channels to list to make it mutable, then extend with missing IDs
                        channels = list(channels)
                        channels.extend(missing_channel_ids)
            
            logger.info(f"Returning {len(channels)} cached channel associations for profile {profile_id}" + 
                       (f" (including snapshot channels)" if include_snapshot else ""))
            return jsonify({
                'profile': profile,
                'channels': channels
            })
        else:
            # If not in cache yet (e.g., first load), fall back to direct API call
            # This will only happen once until the next playlist refresh
            logger.warning(f"Profile channels not in UDI cache for profile {profile_id}, making direct API call")
            base_url = _get_base_url()
            if not base_url:
                return jsonify({"error": "Dispatcharr base URL not configured"}), 500
            
            # Import here to avoid circular dependency
            from udi.fetcher import _get_auth_headers
            
            # Get profile details directly from Dispatcharr
            profile_url = f"{base_url}/api/channels/profiles/{profile_id}/"
            resp = requests.get(profile_url, headers=_get_auth_headers(), timeout=30)
            resp.raise_for_status()
            profile_data = resp.json()
            
            # Parse the channels field
            channels_data = profile_data.get('channels', '')
            
            # Try to parse if it's a JSON string
            if isinstance(channels_data, str) and channels_data.strip():
                try:
                    channels_data = json.loads(channels_data)
                except json.JSONDecodeError as e:
                    logger.warning(f"Could not parse profile.channels as JSON: {e}")
                    channels_data = []
            elif not isinstance(channels_data, list):
                channels_data = []
            
            # Return the data and cache it for future use
            profile_channels_to_cache = {
                'profile': profile_data,
                'channels': channels_data
            }
            # Store in UDI for future use
            udi.update_profile_channels(profile_id, profile_channels_to_cache)
            
            return jsonify(profile_channels_to_cache)
    
    except Exception as e:
        logger.error(f"Error fetching profile channels: {e}")
        return jsonify({"error": str(e)}), 500


@app.route('/api/profiles/<int:profile_id>/snapshot', methods=['POST'])
@log_function_call
def create_profile_snapshot(profile_id):
    """Create a snapshot of the current channels in a profile.
    
    This records which channels are in the profile so we can re-enable them
    after they've been filled back again.
    
    Args:
        profile_id: Profile ID
        
    Returns:
        JSON with result
    """
    try:
        from profile_config import get_profile_config
        
        # Get profile info from UDI
        udi = get_udi_manager()
        profile = udi.get_channel_profile_by_id(profile_id)
        
        if not profile:
            return jsonify({"error": "Profile not found"}), 404
        
        # Get channels for this profile - only enabled channels
        # First try to get from cache
        profile_channels_data = udi.get_profile_channels(profile_id)
        
        if profile_channels_data:
            # Use cached data
            # The 'channels' field is a list of channel IDs (integers)
            # Being in the profile means the channel is enabled
            channels = profile_channels_data.get('channels', [])
            
            # Ensure channels is a list and contains integers
            if isinstance(channels, list):
                channel_ids = [ch for ch in channels if isinstance(ch, int)]
            else:
                logger.warning(f"Unexpected channels data type: {type(channels)}")
                channel_ids = []
            
            logger.info(f"Creating snapshot with {len(channel_ids)} enabled channels from cache")
        else:
            # Fall back to direct API call if not in cache
            logger.info("Profile channels not in cache, fetching from Dispatcharr API")
            base_url = _get_base_url()
            if not base_url:
                return jsonify({"error": "Dispatcharr base URL not configured"}), 500
            
            from udi.fetcher import _get_auth_headers
            
            # Get profile details directly from Dispatcharr
            profile_url = f"{base_url}/api/channels/profiles/{profile_id}/"
            resp = requests.get(profile_url, headers=_get_auth_headers(), timeout=30)
            resp.raise_for_status()
            profile_data = resp.json()
            
            # Parse the channels field
            # According to swagger.json, this is a JSON string containing a list of channel IDs
            channels_data = profile_data.get('channels', '')
            
            # Try to parse if it's a JSON string
            if isinstance(channels_data, str) and channels_data.strip():
                try:
                    channels_data = json.loads(channels_data)
                except json.JSONDecodeError as e:
                    logger.warning(f"Could not parse profile.channels as JSON: {e}")
                    channels_data = []
            elif not isinstance(channels_data, list):
                channels_data = []
            
            # channels_data is a list of channel IDs (integers)
            # Being in the profile means the channel is enabled
            if isinstance(channels_data, list):
                channel_ids = [ch for ch in channels_data if isinstance(ch, int)]
            else:
                logger.warning(f"Unexpected channels_data type: {type(channels_data)}")
                channel_ids = []
            
            logger.info(f"Creating snapshot with {len(channel_ids)} enabled channels from API")
        
        # Create snapshot
        profile_config = get_profile_config()
        success = profile_config.create_snapshot(
            profile_id,
            profile.get('name', 'Unknown'),
            channel_ids
        )
        
        if success:
            snapshot = profile_config.get_snapshot(profile_id)
            return jsonify({
                "message": "Snapshot created successfully",
                "snapshot": snapshot
            })
        else:
            return jsonify({"error": "Failed to create snapshot"}), 500
            
    except Exception as e:
        logger.error(f"Error creating profile snapshot: {e}")
        return jsonify({"error": str(e)}), 500


@app.route('/api/profiles/<int:profile_id>/snapshot', methods=['GET'])
@log_function_call
def get_profile_snapshot(profile_id):
    """Get the snapshot for a profile.
    
    Args:
        profile_id: Profile ID
        
    Returns:
        JSON with snapshot data
    """
    try:
        from profile_config import get_profile_config
        profile_config = get_profile_config()
        
        snapshot = profile_config.get_snapshot(profile_id)
        
        if snapshot:
            return jsonify(snapshot)
        else:
            return jsonify({"message": "No snapshot found for this profile"}), 404
            
    except Exception as e:
        logger.error(f"Error getting profile snapshot: {e}")
        return jsonify({"error": str(e)}), 500


@app.route('/api/profiles/<int:profile_id>/snapshot', methods=['DELETE'])
@log_function_call
def delete_profile_snapshot(profile_id):
    """Delete the snapshot for a profile.
    
    Args:
        profile_id: Profile ID
        
    Returns:
        JSON with result
    """
    try:
        from profile_config import get_profile_config
        profile_config = get_profile_config()
        
        success = profile_config.delete_snapshot(profile_id)
        
        if success:
            return jsonify({"message": "Snapshot deleted successfully"})
        else:
            return jsonify({"error": "Failed to delete snapshot"}), 500
            
    except Exception as e:
        logger.error(f"Error deleting profile snapshot: {e}")
        return jsonify({"error": str(e)}), 500


@app.route('/api/profiles/snapshots', methods=['GET'])
@log_function_call
def get_all_snapshots():
    """Get all profile snapshots.
    
    Returns:
        JSON with all snapshots
    """
    try:
        from profile_config import get_profile_config
        profile_config = get_profile_config()
        
        snapshots = profile_config.get_all_snapshots()
        
        return jsonify(snapshots)
    except Exception as e:
        logger.error(f"Error getting snapshots: {e}")
        return jsonify({"error": str(e)}), 500


@app.route('/api/profiles/<int:profile_id>/disable-empty-channels', methods=['POST'])
@log_function_call
def disable_empty_channels_in_profile_endpoint(profile_id):
    """Disable channels with no streams in a specific profile.
    
    This removes channels from the profile if they have no working streams.
    Uses the dead_streams_tracker to identify empty channels.
    
    Args:
        profile_id: Profile ID
        
    Returns:
        JSON with result and count of disabled channels
    """
    try:
        from empty_channel_manager import disable_empty_channels_in_profile
        
        disabled_count, total_checked = disable_empty_channels_in_profile(profile_id)
        
        return jsonify({
            "message": f"Disabled {disabled_count} empty channels",
            "disabled_count": disabled_count,
            "total_checked": total_checked
        })
        
    except Exception as e:
        logger.error(f"Error disabling empty channels: {e}")
        return jsonify({"error": str(e)}), 500


@app.route('/api/changelog', methods=['GET'])
def get_changelog():
    """Get recent changelog entries from both automation and stream checker."""
    try:
        days = request.args.get('days', 7, type=int)
        
        # Get automation changelog entries
        manager = get_automation_manager()
        automation_changelog = manager.changelog.get_recent_entries(days)
        
        # Get stream checker changelog entries
        stream_checker_changelog = []
        try:
            checker = get_stream_checker_service()
            if checker.changelog:
                stream_checker_changelog = checker.changelog.get_recent_entries(days)
        except Exception as e:
            logger.warning(f"Could not get stream checker changelog: {e}")
        
        # Merge and sort by timestamp (newest first)
        merged_changelog = automation_changelog + stream_checker_changelog
        merged_changelog.sort(key=lambda x: x.get('timestamp', ''), reverse=True)
        
        return jsonify(merged_changelog)
    except Exception as e:
        logger.error(f"Error getting changelog: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/dead-streams', methods=['GET'])
def get_dead_streams():
    """Get dead streams statistics and list with pagination."""
    try:
        # Get pagination parameters with better error handling
        page_param = request.args.get('page', '1')
        per_page_param = request.args.get('per_page', str(DEAD_STREAMS_DEFAULT_PER_PAGE))
        
        try:
            page = int(page_param)
        except (ValueError, TypeError) as e:
            logger.error(f"Invalid page parameter: {page_param} (type: {type(page_param).__name__}) - {str(e)}")
            return jsonify({"error": f"Invalid page parameter: must be an integer"}), 400
        
        try:
            per_page = int(per_page_param)
        except (ValueError, TypeError) as e:
            logger.error(f"Invalid per_page parameter: {per_page_param} (type: {type(per_page_param).__name__}) - {str(e)}")
            return jsonify({"error": f"Invalid per_page parameter: must be an integer"}), 400
        
        # Validate pagination parameters
        if page < 1:
            page = 1
        if per_page < 1 or per_page > DEAD_STREAMS_MAX_PER_PAGE:
            per_page = DEAD_STREAMS_DEFAULT_PER_PAGE
        
        checker = get_stream_checker_service()
        if not checker or not checker.dead_streams_tracker:
            return jsonify({"error": "Dead streams tracker not available"}), 503
        
        dead_streams = checker.dead_streams_tracker.get_dead_streams()
        
        # Transform to a more frontend-friendly format
        dead_streams_list = []
        for url, info in dead_streams.items():
            dead_streams_list.append({
                'url': url,
                'stream_id': info.get('stream_id'),
                'stream_name': info.get('stream_name'),
                'marked_dead_at': info.get('marked_dead_at')
            })
        
        # Sort by marked_dead_at (newest first)
        dead_streams_list.sort(key=lambda x: x.get('marked_dead_at', ''), reverse=True)
        
        # Calculate pagination
        total_count = len(dead_streams_list)
        start_index = (page - 1) * per_page
        end_index = start_index + per_page
        paginated_streams = dead_streams_list[start_index:end_index]
        total_pages = (total_count + per_page - 1) // per_page
        
        return jsonify({
            "total_dead_streams": total_count,
            "dead_streams": paginated_streams,
            "pagination": {
                "page": page,
                "per_page": per_page,
                "total_pages": total_pages,
                "has_next": end_index < total_count,
                "has_prev": page > 1
            }
        })
    except Exception as e:
        logger.error(f"Error getting dead streams: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/dead-streams/revive', methods=['POST'])
def revive_dead_stream():
    """Mark a stream as alive (remove from dead streams)."""
    try:
        data = request.json
        stream_url = data.get('stream_url')
        
        if not stream_url:
            return jsonify({"error": "stream_url is required"}), 400
        
        checker = get_stream_checker_service()
        if not checker or not checker.dead_streams_tracker:
            return jsonify({"error": "Dead streams tracker not available"}), 503
        
        success = checker.dead_streams_tracker.mark_as_alive(stream_url)
        
        if success:
            return jsonify({"success": True, "message": "Stream marked as alive"})
        else:
            return jsonify({"error": "Failed to mark stream as alive"}), 500
    except Exception as e:
        logger.error(f"Error reviving dead stream: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/dead-streams/clear', methods=['POST'])
def clear_all_dead_streams():
    """Clear all dead streams from the tracker."""
    try:
        checker = get_stream_checker_service()
        if not checker or not checker.dead_streams_tracker:
            return jsonify({"error": "Dead streams tracker not available"}), 503
        
        dead_count = len(checker.dead_streams_tracker.get_dead_streams())
        checker.dead_streams_tracker.clear_all_dead_streams()
        
        return jsonify({
            "success": True,
            "message": f"Cleared {dead_count} dead stream(s)",
            "cleared_count": dead_count
        })
    except Exception as e:
        logger.error(f"Error clearing dead streams: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/channel-settings', methods=['GET'])
def get_all_channel_settings():
    """Get settings for all channels with group inheritance info."""
    try:
        settings_manager = get_channel_settings_manager()
        udi = get_udi_manager()
        
        # Get all channels to retrieve their group IDs
        all_channels = udi.get_channels()
        
        # Build a map of effective settings for all channels
        all_effective_settings = {}
        for channel in all_channels:
            if isinstance(channel, dict) and 'id' in channel:
                channel_id = channel['id']
                channel_group_id = channel.get('channel_group_id')
                all_effective_settings[channel_id] = settings_manager.get_channel_effective_settings(
                    channel_id, 
                    channel_group_id
                )
        
        return jsonify(all_effective_settings)
    except Exception as e:
        logger.error(f"Error getting all channel settings: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/channel-settings/<int:channel_id>', methods=['GET'])
def get_channel_settings_endpoint(channel_id):
    """Get settings for a specific channel with group inheritance info."""
    try:
        settings_manager = get_channel_settings_manager()
        
        # Get the channel to retrieve its group ID
        udi = get_udi_manager()
        channel = udi.get_channel_by_id(channel_id)
        channel_group_id = channel.get('channel_group_id') if channel else None
        
        # Get effective settings with inheritance info
        effective_settings = settings_manager.get_channel_effective_settings(channel_id, channel_group_id)
        
        return jsonify(effective_settings)
    except Exception as e:
        logger.error(f"Error getting channel settings: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/channel-settings/<int:channel_id>', methods=['PUT', 'PATCH'])
def update_channel_settings_endpoint(channel_id):
    """Update settings for a specific channel."""
    try:
        data = request.get_json()
        if not data:
            return jsonify({"error": "No data provided"}), 400
        
        matching_mode = data.get('matching_mode')
        checking_mode = data.get('checking_mode')
        
        # Validate modes if provided
        valid_modes = ['enabled', 'disabled']
        if matching_mode and matching_mode not in valid_modes:
            return jsonify({"error": f"Invalid matching_mode. Must be one of: {valid_modes}"}), 400
        if checking_mode and checking_mode not in valid_modes:
            return jsonify({"error": f"Invalid checking_mode. Must be one of: {valid_modes}"}), 400
        
        settings_manager = get_channel_settings_manager()
        success = settings_manager.set_channel_settings(
            channel_id,
            matching_mode=matching_mode,
            checking_mode=checking_mode
        )
        
        if success:
            updated_settings = settings_manager.get_channel_settings(channel_id)
            return jsonify({
                "message": "Channel settings updated successfully",
                "settings": updated_settings
            })
        else:
            return jsonify({"error": "Failed to update channel settings"}), 500
    except Exception as e:
        logger.error(f"Error updating channel settings: {e}")
        return jsonify({"error": str(e)}), 500

# ==================== GROUP SETTINGS ENDPOINTS ====================

@app.route('/api/group-settings', methods=['GET'])
def get_all_group_settings():
    """Get settings for all channel groups."""
    try:
        settings_manager = get_channel_settings_manager()
        all_settings = settings_manager.get_all_group_settings()
        return jsonify(all_settings)
    except Exception as e:
        logger.error(f"Error getting all group settings: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/group-settings/<int:group_id>', methods=['GET'])
def get_group_settings_endpoint(group_id):
    """Get settings for a specific channel group."""
    try:
        settings_manager = get_channel_settings_manager()
        settings = settings_manager.get_group_settings(group_id)
        return jsonify(settings)
    except Exception as e:
        logger.error(f"Error getting group settings: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/group-settings/<int:group_id>', methods=['PUT', 'PATCH'])
def update_group_settings_endpoint(group_id):
    """Update settings for a specific channel group and cascade to all channels in the group."""
    try:
        data = request.get_json()
        if not data:
            return jsonify({"error": "No data provided"}), 400
        
        matching_mode = data.get('matching_mode')
        checking_mode = data.get('checking_mode')
        cascade_to_channels = data.get('cascade_to_channels', False)  # Default to False to preserve individual channel settings
        
        # Validate modes if provided
        valid_modes = ['enabled', 'disabled']
        if matching_mode and matching_mode not in valid_modes:
            return jsonify({"error": f"Invalid matching_mode. Must be one of: {valid_modes}"}), 400
        if checking_mode and checking_mode not in valid_modes:
            return jsonify({"error": f"Invalid checking_mode. Must be one of: {valid_modes}"}), 400
        
        settings_manager = get_channel_settings_manager()
        
        # Update the group settings
        success = settings_manager.set_group_settings(
            group_id,
            matching_mode=matching_mode,
            checking_mode=checking_mode
        )
        
        if not success:
            return jsonify({"error": "Failed to update group settings"}), 500
        
        # Cascade to all channels in the group if requested
        channels_updated = 0
        if cascade_to_channels:
            try:
                # Get all channels in this group
                udi = get_udi_manager()
                all_channels = udi.get_channels()
                
                for channel in all_channels:
                    if isinstance(channel, dict) and channel.get('channel_group_id') == group_id:
                        channel_id = channel.get('id')
                        if channel_id:
                            # Update channel to match group settings
                            settings_manager.set_channel_settings(
                                channel_id,
                                matching_mode=matching_mode,
                                checking_mode=checking_mode
                            )
                            channels_updated += 1
                
                logger.info(f"Cascaded group {group_id} settings to {channels_updated} channel(s)")
            except Exception as e:
                logger.error(f"Error cascading settings to channels: {e}")
                # Continue even if cascade fails
        
        updated_settings = settings_manager.get_group_settings(group_id)
        return jsonify({
            "message": "Group settings updated successfully",
            "settings": updated_settings,
            "channels_updated": channels_updated
        })
    except Exception as e:
        logger.error(f"Error updating group settings: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/group-settings/bulk-disable-matching', methods=['POST'])
def bulk_disable_group_matching():
    """Disable matching for all channel groups."""
    try:
        settings_manager = get_channel_settings_manager()
        udi = get_udi_manager()
        
        # Get all groups (with channels)
        groups = udi.get_channel_groups()
        
        updated_count = 0
        for group in groups:
            group_id = group.get('id')
            if group_id:
                success = settings_manager.set_group_settings(
                    group_id,
                    matching_mode='disabled'
                )
                if success:
                    updated_count += 1
        
        logger.info(f"Bulk disabled matching for {updated_count} group(s)")
        return jsonify({
            "message": f"Disabled matching for {updated_count} group(s)",
            "groups_updated": updated_count
        })
    except Exception as e:
        logger.error(f"Error in bulk disable matching: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/group-settings/bulk-disable-checking', methods=['POST'])
def bulk_disable_group_checking():
    """Disable checking for all channel groups."""
    try:
        settings_manager = get_channel_settings_manager()
        udi = get_udi_manager()
        
        # Get all groups (with channels)
        groups = udi.get_channel_groups()
        
        updated_count = 0
        for group in groups:
            group_id = group.get('id')
            if group_id:
                success = settings_manager.set_group_settings(
                    group_id,
                    checking_mode='disabled'
                )
                if success:
                    updated_count += 1
        
        logger.info(f"Bulk disabled checking for {updated_count} group(s)")
        return jsonify({
            "message": f"Disabled checking for {updated_count} group(s)",
            "groups_updated": updated_count
        })
    except Exception as e:
        logger.error(f"Error in bulk disable checking: {e}")
        return jsonify({"error": str(e)}), 500

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
        return jsonify({"error": str(e)}), 500

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
        return jsonify({"error": str(e)}), 500

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
        return jsonify({"error": str(e)}), 500

@app.route('/api/discover-streams', methods=['POST'])
def discover_streams():
    """Trigger stream discovery and assignment (manual Quick Action)."""
    try:
        manager = get_automation_manager()
        # Use force=True to bypass feature flags for manual Quick Actions
        assignments = manager.discover_and_assign_streams(force=True)
        return jsonify({
            "message": "Stream discovery completed",
            "assignments": assignments,
            "total_assigned": sum(assignments.values())
        })
    except Exception as e:
        logger.error(f"Error discovering streams: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/refresh-playlist', methods=['POST'])
def refresh_playlist():
    """Trigger M3U playlist refresh (manual Quick Action)."""
    try:
        data = request.get_json() or {}
        account_id = data.get('account_id')
        
        manager = get_automation_manager()
        # Use force=True to bypass feature flags for manual Quick Actions
        success = manager.refresh_playlists(force=True)
        
        if success:
            return jsonify({"message": "Playlist refresh completed successfully"})
        else:
            return jsonify({"error": "Playlist refresh failed"}), 500
    except Exception as e:
        logger.error(f"Error refreshing playlist: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/m3u-accounts', methods=['GET'])
def get_m3u_accounts_endpoint():
    """Get all M3U accounts from Dispatcharr, filtering out 'custom' account if no custom streams exist and non-active accounts.
    
    Also merges priority_mode settings from local configuration and returns global priority mode.
    """
    try:
        from api_utils import get_m3u_accounts, has_custom_streams
        from m3u_priority_config import get_m3u_priority_config
        
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
        
        # Get global priority mode
        priority_config = get_m3u_priority_config()
        global_priority_mode = priority_config.get_global_priority_mode()
        
        # Return accounts with global priority mode
        return jsonify({
            "accounts": accounts,
            "global_priority_mode": global_priority_mode
        })
    except Exception as e:
        logger.error(f"Error fetching M3U accounts: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/m3u-accounts/<int:account_id>/priority', methods=['PATCH'])
@log_function_call
def update_m3u_account_priority(account_id):
    """Update M3U account priority settings.
    
    This endpoint:
    - Updates the 'priority' field in Dispatcharr via API
    - Stores the 'priority_mode' field locally (StreamFlow-specific)
    
    Request body:
        {
            "priority": int (0-100) - optional, updates Dispatcharr
            "priority_mode": str ("disabled", "same_resolution", "all_streams") - optional, stored locally
        }
    """
    try:
        data = request.get_json()
        
        if not data:
            return jsonify({"error": "No data provided"}), 400
        
        # Update priority in Dispatcharr if provided
        if 'priority' in data:
            priority = data.get('priority', 0)
            if not isinstance(priority, int) or priority < 0:
                return jsonify({"error": "Priority must be a non-negative integer"}), 400
            
            # Update via Dispatcharr API
            from api_utils import _get_base_url, _get_auth_headers
            base_url = _get_base_url()
            headers = _get_auth_headers()
            
            if not base_url or not headers:
                return jsonify({"error": "Dispatcharr not configured"}), 500
            
            # PATCH request to update priority
            url = f"{base_url}/api/m3u/accounts/{account_id}/"
            resp = requests.patch(
                url,
                headers=headers,
                json={"priority": priority},
                timeout=10
            )
            
            if resp.status_code not in [200, 201]:
                logger.error(f"Failed to update priority in Dispatcharr: {resp.status_code} - {resp.text}")
                return jsonify({"error": f"Failed to update priority: {resp.text}"}), resp.status_code
            
            logger.info(f"Updated priority for M3U account {account_id} to {priority} in Dispatcharr")
        
        # Store priority_mode locally (StreamFlow-specific configuration)
        if 'priority_mode' in data:
            priority_mode = data.get('priority_mode', 'disabled')
            from m3u_priority_config import get_m3u_priority_config
            
            priority_config = get_m3u_priority_config()
            if not priority_config.set_priority_mode(account_id, priority_mode):
                return jsonify({"error": "Failed to save priority_mode"}), 500
            
            logger.info(f"Updated priority_mode for M3U account {account_id} to {priority_mode}")
        
        # Refresh UDI to get updated data from Dispatcharr
        if 'priority' in data:
            udi = get_udi_manager()
            udi.refresh_m3u_accounts()
        
        return jsonify({"message": "M3U account priority updated successfully"})
        
    except Exception as e:
        logger.error(f"Error updating M3U account priority: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500

@app.route('/api/m3u-priority/global-mode', methods=['PUT'])
@log_function_call
def update_global_priority_mode():
    """Update the global priority mode for all M3U accounts.
    
    Request body:
        {
            "priority_mode": str ("disabled", "same_resolution", "all_streams")
        }
    """
    try:
        data = request.get_json()
        
        if not data or 'priority_mode' not in data:
            return jsonify({"error": "priority_mode is required"}), 400
        
        priority_mode = data.get('priority_mode')
        
        from m3u_priority_config import get_m3u_priority_config
        priority_config = get_m3u_priority_config()
        
        if not priority_config.set_global_priority_mode(priority_mode):
            return jsonify({"error": "Failed to save global priority_mode"}), 500
        
        logger.info(f"Updated global priority_mode to {priority_mode}")
        return jsonify({"message": "Global priority mode updated successfully", "priority_mode": priority_mode})
        
    except Exception as e:
        logger.error(f"Error updating global priority mode: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500

@app.route('/api/setup-wizard', methods=['GET'])
def get_setup_wizard_status():
    """Get setup wizard completion status."""
    try:
        # Check if basic configuration exists
        config_file = CONFIG_DIR / 'automation_config.json'
        regex_file = CONFIG_DIR / 'channel_regex_config.json'
        
        status = {
            "automation_config_exists": config_file.exists(),
            "regex_config_exists": regex_file.exists(),
            "has_patterns": False,
            "has_channels": False,
            "dispatcharr_connection": False
        }
        
        # Check if we have patterns configured
        if regex_file.exists():
            matcher = get_regex_matcher()
            # Reload patterns from disk to ensure we have the latest configuration
            matcher.reload_patterns()
            patterns = matcher.get_patterns()
            status["has_patterns"] = bool(patterns.get('patterns'))
        
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
                    channels = udi.get_channels()
                    status["dispatcharr_connection"] = channels is not None
                    status["has_channels"] = bool(channels)
                except:
                    pass
        
        # Patterns are now optional - wizard can be completed without them
        status["setup_complete"] = all([
            status["automation_config_exists"],
            status["regex_config_exists"],
            # Removed has_patterns requirement - it's now optional
            status["has_channels"],
            status["dispatcharr_connection"]
        ])
        
        return jsonify(status)
    except Exception as e:
        logger.error(f"Error getting setup wizard status: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/setup-wizard/create-sample-patterns', methods=['POST'])
def create_sample_patterns():
    """Create sample regex patterns for testing setup completion."""
    try:
        matcher = get_regex_matcher()
        
        # Add some sample patterns
        patterns = {
            "patterns": {
                "1": {
                    "name": "News Channels",
                    "regex": [".*News.*", ".*CNN.*", ".*BBC.*"],
                    "enabled": True
                },
                "2": {
                    "name": "Sports Channels", 
                    "regex": [".*Sport.*", ".*ESPN.*", ".*Fox Sports.*"],
                    "enabled": True
                }
            },
            "global_settings": {
                "case_sensitive": False,
                "require_exact_match": False
            }
        }
        
        # Save the sample patterns
        with open(CONFIG_DIR / 'channel_regex_config.json', 'w') as f:
            json.dump(patterns, f, indent=2)
        
        return jsonify({"message": "Sample patterns created successfully"})
    except Exception as e:
        logger.error(f"Error creating sample patterns: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/dispatcharr/config', methods=['GET'])
def get_dispatcharr_config_endpoint():
    """Get current Dispatcharr configuration (without exposing password)."""
    try:
        config_manager = get_dispatcharr_config()
        config = config_manager.get_config()
        return jsonify(config)
    except Exception as e:
        logger.error(f"Error getting Dispatcharr config: {e}")
        return jsonify({"error": str(e)}), 500

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
                logger.info("Dispatcharr credentials updated, initializing UDI Manager...")
                udi = get_udi_manager()
                udi.initialize(force_refresh=True)
                logger.info("UDI Manager initialized successfully with new credentials")
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
        return jsonify({"error": str(e)}), 500

@app.route('/api/dispatcharr/test-connection', methods=['POST'])
def test_dispatcharr_connection():
    """Test Dispatcharr connection with provided or existing credentials."""
    try:
        data = request.get_json() or {}
        
        # Temporarily use provided credentials if available, otherwise use existing
        test_base_url = data.get('base_url', os.getenv("DISPATCHARR_BASE_URL"))
        test_username = data.get('username', os.getenv("DISPATCHARR_USER"))
        test_password = data.get('password', os.getenv("DISPATCHARR_PASS"))
        
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
            return jsonify({
                "success": False,
                "error": f"Connection failed: {str(e)}"
            }), 400
            
    except Exception as e:
        logger.error(f"Error testing Dispatcharr connection: {e}")
        return jsonify({"error": str(e)}), 500

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
        return jsonify({
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
        return jsonify({"error": str(e)}), 500

@app.route('/api/stream-checker/start', methods=['POST'])
def start_stream_checker():
    """Start the stream checker service."""
    try:
        service = get_stream_checker_service()
        service.start()
        return jsonify({"message": "Stream checker started successfully", "status": "running"})
    except Exception as e:
        logger.error(f"Error starting stream checker: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/stream-checker/stop', methods=['POST'])
def stop_stream_checker():
    """Stop the stream checker service."""
    try:
        service = get_stream_checker_service()
        service.stop()
        return jsonify({"message": "Stream checker stopped successfully", "status": "stopped"})
    except Exception as e:
        logger.error(f"Error stopping stream checker: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/stream-checker/queue', methods=['GET'])
def get_stream_checker_queue():
    """Get current queue status."""
    try:
        service = get_stream_checker_service()
        status = service.get_status()
        return jsonify(status.get('queue', {}))
    except Exception as e:
        logger.error(f"Error getting stream checker queue: {e}")
        return jsonify({"error": str(e)}), 500

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
        return jsonify({"error": str(e)}), 500

@app.route('/api/stream-checker/queue/clear', methods=['POST'])
def clear_stream_checker_queue():
    """Clear the checking queue."""
    try:
        service = get_stream_checker_service()
        service.clear_queue()
        return jsonify({"message": "Queue cleared successfully"})
    except Exception as e:
        logger.error(f"Error clearing stream checker queue: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/stream-checker/config', methods=['GET'])
def get_stream_checker_config():
    """Get stream checker configuration."""
    try:
        service = get_stream_checker_service()
        return jsonify(service.config.config)
    except Exception as e:
        logger.error(f"Error getting stream checker config: {e}")
        return jsonify({"error": str(e)}), 500

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
                if manager.running:
                    manager.stop_automation()
                    logger.info("Automation service stopped (all automation disabled)")
                # Stop background processors
                stop_scheduled_event_processor()
                stop_epg_refresh_processor()
            else:
                # Start services if automation is enabled and they're not already running
                if not service.running:
                    service.start()
                    logger.info(f"Stream checker service auto-started after config update")
                if not manager.running:
                    manager.start_automation()
                    logger.info(f"Automation service auto-started after config update")
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
        return jsonify({"error": str(e)}), 500

@app.route('/api/stream-checker/progress', methods=['GET'])
def get_stream_checker_progress():
    """Get current checking progress."""
    try:
        service = get_stream_checker_service()
        status = service.get_status()
        return jsonify(status.get('progress', {}))
    except Exception as e:
        logger.error(f"Error getting stream checker progress: {e}")
        return jsonify({"error": str(e)}), 500

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
        return jsonify({"error": str(e)}), 500

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
            return jsonify(result)
        else:
            return jsonify(result), 500
    
    except Exception as e:
        logger.error(f"Error checking single channel: {e}")
        return jsonify({"error": str(e)}), 500

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
        return jsonify({"error": str(e)}), 500

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
        return jsonify({"error": str(e)}), 500

@app.route('/api/stream-checker/global-action', methods=['POST'])
def trigger_global_action():
    """Trigger a manual global action (Update M3U, Match streams, Check all channels).
    
    This performs a complete global action that:
    1. Reloads enabled M3U accounts
    2. Matches new streams with regex patterns
    3. Checks every channel, bypassing 2-hour immunity
    """
    try:
        service = get_stream_checker_service()
        
        if not service.running:
            return jsonify({"error": "Stream checker service is not running"}), 400
        
        success = service.trigger_global_action()
        
        if success:
            return jsonify({
                "message": "Global action triggered successfully",
                "status": "in_progress",
                "description": "Update, Match, and Check all channels in progress"
            })
        else:
            return jsonify({"error": "Failed to trigger global action"}), 500
    
    except Exception as e:
        logger.error(f"Error triggering global action: {e}")
        return jsonify({"error": str(e)}), 500

# ============================================================================
# Scheduling API Endpoints
# ============================================================================

@app.route('/api/scheduling/config', methods=['GET'])
@log_function_call
def get_scheduling_config():
    """Get scheduling configuration including EPG refresh interval."""
    try:
        from scheduling_service import get_scheduling_service
        service = get_scheduling_service()
        config = service.get_config()
        return jsonify(config)
    except Exception as e:
        logger.error(f"Error getting scheduling config: {e}")
        return jsonify({"error": str(e)}), 500

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
        from scheduling_service import get_scheduling_service
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
        return jsonify({"error": str(e)}), 500

@app.route('/api/scheduling/epg/grid', methods=['GET'])
@log_function_call
def get_epg_grid():
    """Get EPG grid data (all programs for next 24 hours).
    
    Query parameters:
    - force_refresh: If true, bypass cache and fetch fresh data
    """
    try:
        from scheduling_service import get_scheduling_service
        service = get_scheduling_service()
        force_refresh = request.args.get('force_refresh', 'false').lower() == 'true'
        
        programs = service.fetch_epg_grid(force_refresh=force_refresh)
        return jsonify(programs)
    
    except Exception as e:
        logger.error(f"Error fetching EPG grid: {e}")
        return jsonify({"error": str(e)}), 500

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
        from scheduling_service import get_scheduling_service
        service = get_scheduling_service()
        
        programs = service.get_programs_by_channel(channel_id)
        return jsonify(programs)
    
    except Exception as e:
        logger.error(f"Error fetching programs for channel {channel_id}: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/scheduling/events', methods=['GET'])
@log_function_call
def get_scheduled_events():
    """Get all scheduled events."""
    try:
        from scheduling_service import get_scheduling_service
        service = get_scheduling_service()
        events = service.get_scheduled_events()
        return jsonify(events)
    except Exception as e:
        logger.error(f"Error getting scheduled events: {e}")
        return jsonify({"error": str(e)}), 500

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
        from scheduling_service import get_scheduling_service
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
        global scheduled_event_processor_wake
        if scheduled_event_processor_wake:
            scheduled_event_processor_wake.set()
        
        return jsonify(event), 201
    
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        logger.error(f"Error creating scheduled event: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/scheduling/events/<event_id>', methods=['DELETE'])
@log_function_call
def delete_scheduled_event(event_id):
    """Delete a scheduled event.
    
    Args:
        event_id: Event ID
    """
    try:
        from scheduling_service import get_scheduling_service
        service = get_scheduling_service()
        
        success = service.delete_scheduled_event(event_id)
        
        if success:
            return jsonify({"message": "Event deleted"}), 200
        else:
            return jsonify({"error": "Event not found"}), 404
    
    except Exception as e:
        logger.error(f"Error deleting scheduled event: {e}")
        return jsonify({"error": str(e)}), 500


@app.route('/api/scheduling/auto-create-rules', methods=['GET'])
@log_function_call
def get_auto_create_rules():
    """Get all auto-create rules."""
    try:
        from scheduling_service import get_scheduling_service
        service = get_scheduling_service()
        rules = service.get_auto_create_rules()
        return jsonify(rules)
    except Exception as e:
        logger.error(f"Error getting auto-create rules: {e}")
        return jsonify({"error": str(e)}), 500


@app.route('/api/scheduling/auto-create-rules', methods=['POST'])
@log_function_call
def create_auto_create_rule():
    """Create a new auto-create rule.
    
    Expected JSON body:
    {
        "name": "Rule Name",
        "channel_ids": [123, 456],  // or "channel_id": 123 for backward compatibility
        "regex_pattern": "^Breaking News",
        "minutes_before": 5
    }
    """
    try:
        from scheduling_service import get_scheduling_service
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
        global scheduled_event_processor_wake
        if scheduled_event_processor_wake:
            scheduled_event_processor_wake.set()
        
        return jsonify(rule), 201
    
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        logger.error(f"Error creating auto-create rule: {e}")
        return jsonify({"error": str(e)}), 500


@app.route('/api/scheduling/auto-create-rules/<rule_id>', methods=['DELETE'])
@log_function_call
def delete_auto_create_rule(rule_id):
    """Delete an auto-create rule.
    
    Args:
        rule_id: Rule ID
    """
    try:
        from scheduling_service import get_scheduling_service
        service = get_scheduling_service()
        
        success = service.delete_auto_create_rule(rule_id)
        
        if success:
            return jsonify({"message": "Rule deleted"}), 200
        else:
            return jsonify({"error": "Rule not found"}), 404
    
    except Exception as e:
        logger.error(f"Error deleting auto-create rule: {e}")
        return jsonify({"error": str(e)}), 500


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
        "minutes_before": 10
    }
    """
    try:
        from scheduling_service import get_scheduling_service
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
            global scheduled_event_processor_wake
            if scheduled_event_processor_wake:
                scheduled_event_processor_wake.set()
            
            return jsonify(updated_rule), 200
        else:
            return jsonify({"error": "Rule not found"}), 404
    
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        logger.error(f"Error updating auto-create rule: {e}")
        return jsonify({"error": str(e)}), 500


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
        from scheduling_service import get_scheduling_service
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
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        logger.error(f"Error testing auto-create rule: {e}")
        return jsonify({"error": str(e)}), 500


@app.route('/api/scheduling/auto-create-rules/export', methods=['GET'])
@log_function_call
def export_auto_create_rules():
    """Export all auto-create rules as JSON.
    
    Returns:
        JSON array of auto-create rules
    """
    try:
        from scheduling_service import get_scheduling_service
        service = get_scheduling_service()
        rules = service.export_auto_create_rules()
        return jsonify(rules), 200
    except Exception as e:
        logger.error(f"Error exporting auto-create rules: {e}")
        return jsonify({"error": str(e)}), 500


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
        from scheduling_service import get_scheduling_service
        service = get_scheduling_service()
        rules_data = request.get_json()
        
        if not rules_data:
            return jsonify({"error": "No rules data provided"}), 400
        
        if not isinstance(rules_data, list):
            return jsonify({"error": "Rules data must be an array"}), 400
        
        result = service.import_auto_create_rules(rules_data)
        
        # Wake up the processor to check for new events immediately
        global scheduled_event_processor_wake
        if scheduled_event_processor_wake:
            scheduled_event_processor_wake.set()
        
        # Return 200 even if some rules failed - the response contains details
        return jsonify(result), 200
    
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        logger.error(f"Error importing auto-create rules: {e}")
        return jsonify({"error": str(e)}), 500


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
        from scheduling_service import get_scheduling_service
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
        return jsonify({"error": str(e)}), 500


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
        return jsonify({"error": str(e)}), 500


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
        return jsonify({"error": str(e)}), 500


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
        return jsonify({"error": str(e)}), 500


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
        return jsonify({"error": str(e)}), 500


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
        return jsonify({"error": str(e)}), 500


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
        return jsonify({"error": str(e)}), 500


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
        return jsonify({"error": str(e)}), 500


# ============================================================================
# AceStream Monitoring API Endpoints
# ============================================================================

# Global instance of AceStream monitor (initialized lazily)
acestream_monitor = None

def get_acestream_monitor():
    """Get or create AceStream monitor instance."""
    global acestream_monitor
    if acestream_monitor is None:
        try:
            from acestream_monitor_service import AceStreamMonitor
            udi_manager = get_udi_manager()
            
            # Get configuration from dispatcharr config
            default_url = "http://gluetun:19000"
            config_dict = {}
            try:
                from dispatcharr_config import get_dispatcharr_config
                config = get_dispatcharr_config()
                default_url = config.get('acestream_orchestrator_url', default_url)
                config_dict = {
                    'monitoring_interval': config.get('acestream_monitoring_interval', 30),
                    'ffmpeg_probe_duration': config.get('acestream_ffmpeg_probe_duration', 5)
                }
            except:
                pass
            
            acestream_monitor = AceStreamMonitor(udi_manager, default_url, config_dict)
            logger.info("AceStream monitor instance created")
        except Exception as e:
            logger.error(f"Failed to create AceStream monitor: {e}")
            return None
    return acestream_monitor


@app.route('/api/acestream/config', methods=['GET'])
@log_function_call
def get_acestream_config():
    """Get AceStream configuration.
    
    Returns:
        JSON with AceStream configuration
    """
    try:
        from dispatcharr_config import get_dispatcharr_config
        config = get_dispatcharr_config()
        
        # Get AceStream-related configuration
        acestream_config = {
            'enabled': config.get('acestream_enabled', False),
            'orchestrator_url': config.get('acestream_orchestrator_url', 'http://gluetun:19000'),
            'monitoring_interval': config.get('acestream_monitoring_interval', 30),
            'ffmpeg_probe_duration': config.get('acestream_ffmpeg_probe_duration', 5)
        }
        
        return jsonify(acestream_config)
    except Exception as e:
        logger.error(f"Error getting AceStream config: {e}")
        return jsonify({"error": str(e)}), 500


@app.route('/api/acestream/config', methods=['POST'])
@log_function_call
def update_acestream_config():
    """Update AceStream configuration.
    
    Expected JSON:
        {
            "enabled": bool,
            "orchestrator_url": str,
            "monitoring_interval": int,
            "ffmpeg_probe_duration": int
        }
    
    Returns:
        JSON with success status
    """
    try:
        data = request.json
        
        from dispatcharr_config import get_dispatcharr_config
        config = get_dispatcharr_config()
        
        # Update AceStream config
        if 'enabled' in data:
            config['acestream_enabled'] = data['enabled']
        if 'orchestrator_url' in data:
            config['acestream_orchestrator_url'] = data['orchestrator_url']
        if 'monitoring_interval' in data:
            config['acestream_monitoring_interval'] = data['monitoring_interval']
        if 'ffmpeg_probe_duration' in data:
            config['acestream_ffmpeg_probe_duration'] = data['ffmpeg_probe_duration']
        
        # Save config
        config.save()
        
        # Restart monitoring if enabled
        monitor = get_acestream_monitor()
        if monitor:
            if data.get('enabled', False):
                if not monitor.running:
                    monitor.start_monitoring()
            else:
                if monitor.running:
                    monitor.shutdown()
        
        return jsonify({'success': True})
    except Exception as e:
        logger.error(f"Error updating AceStream config: {e}")
        return jsonify({"error": str(e)}), 500


@app.route('/api/acestream/channels', methods=['GET'])
@log_function_call
def get_acestream_channels():
    """Get all channels marked as AceStream channels.
    
    Returns:
        JSON with list of AceStream channels
    """
    try:
        udi_manager = get_udi_manager()
        all_channels = udi_manager.get_channels()
        
        acestream_channels = [
            ch for ch in all_channels 
            if ch.get('is_acestream', False)
        ]
        
        return jsonify(acestream_channels)
    except Exception as e:
        logger.error(f"Error getting AceStream channels: {e}")
        return jsonify({"error": str(e)}), 500


@app.route('/api/acestream/channels/<int:channel_id>/tag', methods=['POST'])
@log_function_call
def tag_channel_as_acestream(channel_id):
    """Mark a channel as AceStream channel.
    
    Args:
        channel_id: Channel ID
    
    Expected JSON:
        {
            "is_acestream": bool,
            "orchestrator_url": str (optional)
        }
    
    Returns:
        JSON with success status
    """
    try:
        data = request.json
        is_acestream = data.get('is_acestream', False)
        orchestrator_url = data.get('orchestrator_url')
        
        udi_manager = get_udi_manager()
        
        # Get channel
        channel = udi_manager.get_channel_by_id(channel_id)
        if not channel:
            return jsonify({"error": "Channel not found"}), 404
        
        # Update channel properties (channel is a dict)
        channel['is_acestream'] = is_acestream
        if orchestrator_url:
            channel['acestream_orchestrator_url'] = orchestrator_url
        
        # Update channel in UDI cache with modified data
        success = udi_manager.update_channel(channel_id, channel)
        
        if not success:
            logger.warning(f"Failed to update channel {channel_id} in UDI cache")
        
        # If monitoring is enabled and channel is now AceStream, refresh monitoring
        monitor = get_acestream_monitor()
        if monitor and is_acestream and monitor.running:
            monitor.refresh_channels()
        
        return jsonify({'success': True})
    except Exception as e:
        logger.error(f"Error tagging channel as AceStream: {e}")
        return jsonify({"error": str(e)}), 500


@app.route('/api/acestream/monitoring/status', methods=['GET'])
@log_function_call
def get_acestream_monitoring_status():
    """Get current monitoring status for all AceStream channels.
    
    Returns:
        JSON with monitoring status
    """
    try:
        monitor = get_acestream_monitor()
        if not monitor:
            return jsonify({'error': 'Monitoring service not available'}), 503
        
        status = monitor.get_status()
        return jsonify(status)
    except Exception as e:
        logger.error(f"Error getting monitoring status: {e}")
        return jsonify({"error": str(e)}), 500


@app.route('/api/acestream/monitoring/start', methods=['POST'])
@log_function_call
def start_acestream_monitoring():
    """Start AceStream monitoring service.
    
    Returns:
        JSON with success status
    """
    try:
        monitor = get_acestream_monitor()
        if not monitor:
            return jsonify({'error': 'Monitoring service not available'}), 503
        
        if monitor.running:
            return jsonify({'message': 'Monitoring already running'}), 200
        
        monitor.start_monitoring()
        return jsonify({'success': True, 'message': 'Monitoring started'})
    except Exception as e:
        logger.error(f"Error starting monitoring: {e}")
        return jsonify({"error": str(e)}), 500


@app.route('/api/acestream/monitoring/stop', methods=['POST'])
@log_function_call
def stop_acestream_monitoring():
    """Stop AceStream monitoring service.
    
    Returns:
        JSON with success status
    """
    try:
        monitor = get_acestream_monitor()
        if not monitor:
            return jsonify({'error': 'Monitoring service not available'}), 503
        
        if not monitor.running:
            return jsonify({'message': 'Monitoring not running'}), 200
        
        monitor.shutdown()
        return jsonify({'success': True, 'message': 'Monitoring stopped'})
    except Exception as e:
        logger.error(f"Error stopping monitoring: {e}")
        return jsonify({"error": str(e)}), 500


@app.route('/api/acestream/monitoring/channel/<int:channel_id>/metrics', methods=['GET'])
@log_function_call
def get_channel_metrics(channel_id):
    """Get monitoring metrics for a specific channel.
    
    Args:
        channel_id: Channel ID
    
    Query params:
        hours: Number of hours of history (default: 24)
    
    Returns:
        JSON with metrics data
    """
    try:
        hours = request.args.get('hours', 24, type=int)
        
        from acestream_db import AceStreamDatabase
        db = AceStreamDatabase()
        
        metrics = db.get_channel_metrics(channel_id, hours)
        
        return jsonify(metrics)
    except Exception as e:
        logger.error(f"Error getting channel metrics: {e}")
        return jsonify({"error": str(e)}), 500


@app.route('/api/acestream/monitoring/stream/<int:stream_id>/health', methods=['GET'])
@log_function_call
def get_stream_health(stream_id):
    """Get current health score and stats for a stream.
    
    Args:
        stream_id: Stream ID
    
    Returns:
        JSON with health data
    """
    try:
        from acestream_db import AceStreamDatabase
        db = AceStreamDatabase()
        
        health = db.get_latest_stream_health(stream_id)
        
        if health:
            return jsonify(health)
        else:
            return jsonify({"message": "No health data available"}), 404
    except Exception as e:
        logger.error(f"Error getting stream health: {e}")
        return jsonify({"error": str(e)}), 500


# Serve React app for all frontend routes (catch-all - must be last!)
@app.route('/<path:path>')
def serve_frontend(path):
    """Serve React frontend files or return index.html for client-side routing."""
    file_path = static_folder / path
    if file_path.exists() and file_path.is_file():
        return send_from_directory(static_folder, path)
    else:
        # Return index.html for client-side routing (React Router)
        try:
            return send_file(static_folder / 'index.html')
        except FileNotFoundError:
            return jsonify({"error": "Frontend not found"}), 404

if __name__ == '__main__':
    import argparse
    
    parser = argparse.ArgumentParser(description='StreamFlow for Dispatcharr Web API')
    parser.add_argument('--host', default=os.environ.get('API_HOST', '0.0.0.0'), help='Host to bind to')
    parser.add_argument('--port', type=int, default=int(os.environ.get('API_PORT', '5000')), help='Port to bind to')
    parser.add_argument('--debug', action='store_true', help='Enable debug mode')
    
    args = parser.parse_args()
    
    logger.info(f"Starting StreamFlow for Dispatcharr Web API on {args.host}:{args.port}")
    
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
    
    # Auto-start automation service if automation is configured AND wizard is complete
    try:
        # Check if wizard has been completed
        if not check_wizard_complete():
            logger.info("Automation service will not start - setup wizard has not been completed")
        else:
            manager = get_automation_manager()
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
                logger.info("Automation service is disabled (all automation controls disabled)")
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
    
    # Auto-start AceStream monitoring if enabled and wizard is complete
    try:
        if not check_wizard_complete():
            logger.info("AceStream monitoring will not start - setup wizard has not been completed")
        else:
            from dispatcharr_config import get_dispatcharr_config
            config = get_dispatcharr_config()
            
            if config.get('acestream_enabled', False):
                monitor = get_acestream_monitor()
                if monitor:
                    monitor.start_monitoring()
                    logger.info("AceStream monitoring auto-started")
                else:
                    logger.warning("Failed to create AceStream monitor instance")
            else:
                logger.info("AceStream monitoring is disabled in configuration")
    except Exception as e:
        logger.error(f"Failed to auto-start AceStream monitoring: {e}")
    
    app.run(host=args.host, port=args.port, debug=args.debug)