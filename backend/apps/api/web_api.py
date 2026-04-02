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
import requests
import socket
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, Optional, Tuple, cast
from dataclasses import asdict
from flask import Flask, request, jsonify, make_response, Response
from flask_cors import CORS
from werkzeug.utils import secure_filename

from apps.automation.automated_stream_manager import AutomatedStreamManager, RegexChannelMatcher
from apps.automation.automation_events_scheduler import get_events_scheduler
from apps.automation.regex_validation import is_dangerous_regex
from apps.core.api_utils import _get_base_url
from apps.stream.stream_checker_service import get_stream_checker_service
from apps.automation.scheduling_service import get_scheduling_service
from apps.background.scheduling_workers import (
    epg_refresh_processor_loop,
    scheduled_event_processor_loop,
)
from apps.stream.udp_proxy import UDPProxyManager

from apps.config.dispatcharr_config import get_dispatcharr_config
from apps.config.acestream_orchestrator_config import get_acestream_orchestrator_config
from apps.channels.channel_order_manager import get_channel_order_manager
from apps.channels.repository import UdiChannelRepository
from apps.channels.service import ChannelService
from apps.automation.automation_config_manager import get_automation_config_manager
from apps.api.channel_handlers import (
    get_channel_groups_response,
    get_channel_logo_cached_response,
    get_channel_logo_response,
    get_channel_stats_response,
    get_channels_response,
)
from apps.api.regex_handlers import (
    add_bulk_regex_patterns_response,
    add_regex_pattern_response,
    bulk_delete_regex_patterns_response,
    bulk_edit_regex_pattern_response,
    bulk_update_match_settings_response,
    delete_group_regex_config_response,
    delete_regex_pattern_response,
    export_regex_patterns_response,
    get_group_regex_config_response,
    get_common_regex_patterns_response,
    import_regex_patterns_response,
    get_regex_patterns_response,
    mass_edit_preview_response,
    mass_edit_regex_patterns_response,
    test_regex_pattern_live_response,
    test_regex_pattern_response,
    update_channel_match_settings_response,
    update_group_match_settings_response,
    upsert_group_regex_config_response,
)
from apps.api.automation_handlers import (
    assign_automation_profile_channel_response,
    assign_automation_profile_channels_response,
    assign_automation_profile_group_response,
    assign_epg_scheduled_profile_channel_response,
    assign_epg_scheduled_profile_channels_response,
    assign_epg_scheduled_profile_group_response,
    assign_period_to_channels_response,
    assign_period_to_groups_response,
    batch_assign_periods_to_channels_response,
    batch_assign_periods_to_groups_response,
    bulk_delete_automation_profiles_response,
    get_batch_period_usage_response,
    get_automation_status_response,
    get_channel_automation_periods_response,
    get_group_automation_periods_response,
    get_period_channels_response,
    get_upcoming_automation_events_response,
    handle_automation_period_response,
    handle_automation_periods_response,
    handle_automation_profile_response,
    handle_automation_profiles_response,
    handle_global_automation_settings_response,
    invalidate_automation_events_cache_response,
    remove_period_from_channels_response,
    remove_period_from_groups_response,
    start_automation_service_api_response,
    stop_automation_service_api_response,
    trigger_automation_cycle_response,
)
from apps.api.channel_order_handlers import (
    clear_channel_order_response,
    get_channel_order_response,
    set_channel_order_response,
)
from apps.api.telemetry_handlers import (
    clear_all_dead_streams_response,
    get_changelog_response,
    get_dead_streams_response,
    revive_dead_stream_response,
)
from apps.api.quick_action_handlers import (
    discover_streams_response,
    get_m3u_accounts_response,
    refresh_playlist_response,
)
from apps.api.setup_wizard_handlers import (
    create_sample_patterns_response,
    ensure_wizard_config_response,
    get_setup_wizard_status_response,
)
from apps.api.match_preview_handlers import test_match_live_response, bulk_match_count_response
from apps.api.stream_checker_handlers import (
    add_to_stream_checker_queue_response,
    check_single_channel_now_response,
    check_specific_channel_response,
    clear_stream_checker_queue_response,
    get_stream_checker_status_response,
    mark_channels_updated_response,
    queue_all_channels_response,
    update_stream_checker_config_response,
    get_stream_checker_config_response,
    get_stream_checker_progress_response,
    get_stream_checker_queue_response,
    start_stream_checker_response,
    stop_stream_checker_response,
)
from apps.api.scheduling_handlers import (
    create_auto_create_rule_response,
    create_scheduled_event_response,
    delete_auto_create_rule_response,
    delete_scheduled_event_response,
    export_auto_create_rules_response,
    get_auto_create_rules_response,
    get_channel_programs_response,
    get_epg_grid_response,
    get_epg_refresh_processor_status_response,
    get_scheduled_event_processor_status_response,
    get_scheduled_events_response,
    get_scheduling_config_response,
    import_auto_create_rules_response,
    process_due_scheduled_events_response,
    start_epg_refresh_processor_api_response,
    start_scheduled_event_processor_api_response,
    stop_epg_refresh_processor_api_response,
    stop_scheduled_event_processor_api_response,
    test_auto_create_rule_response,
    trigger_epg_refresh_response,
    update_auto_create_rule_response,
    update_scheduling_config_response,
)
from apps.api.acestream_handlers import (
    check_acestream_orchestrator_ready_response,
    create_acestream_channel_session_response,
    create_acestream_group_sessions_response,
    delete_acestream_channel_session_response,
    delete_acestream_monitor_entry_response,
    get_acestream_channel_session_response,
    get_acestream_monitor_session_response,
    get_acestream_orchestrator_config_response,
    list_acestream_channel_sessions_response,
    list_acestream_monitor_sessions_response,
    list_acestream_started_streams_response,
    parse_acestream_m3u_response,
    quarantine_acestream_channel_stream_response,
    revive_acestream_channel_stream_response,
    start_acestream_monitor_session_response,
    stop_acestream_channel_session_response,
    stop_acestream_monitor_session_response,
    update_acestream_orchestrator_config_response,
)
from apps.api.legacy_automation_handlers import (
    assign_profile_to_channel_legacy_response,
    assign_profile_to_group_legacy_response,
    create_automation_profile_legacy_response,
    delete_automation_profile_legacy_response,
    get_automation_profile_legacy_response,
    get_automation_profiles_legacy_response,
    get_effective_profile_legacy_response,
    get_global_automation_settings_legacy_response,
    update_automation_profile_legacy_response,
    update_global_automation_settings_legacy_response,
)
from apps.api.dispatcharr_handlers import (
    get_dispatcharr_config_response,
    get_udi_initialization_status_response,
    initialize_udi_response,
    test_dispatcharr_connection_response,
    update_dispatcharr_config_response,
)
from apps.api.stream_sessions_handlers import (
    batch_delete_sessions_response,
    batch_stop_sessions_response,
    create_group_stream_sessions_response,
    create_session_from_event_response,
    create_stream_session_response,
    delete_stream_session_response,
    get_alive_screenshots_response,
    get_playing_streams_response,
    get_proxy_status_response,
    get_stream_metrics_response,
    get_stream_session_response,
    get_stream_sessions_response,
    get_stream_viewer_url_response,
    handle_session_settings_response,
    quarantine_stream_response,
    revive_stream_response,
    serve_screenshot_response,
    start_stream_session_response,
    stop_stream_session_response,
    stream_proxy_url_response,
)
from apps.api.meta_handlers import (
    get_environment_response,
    health_check_response,
    get_version_response,
    root_response,
    serve_frontend_response,
)
from apps.api.middleware import API_RATE_LIMIT_ENABLED, api_rate_limiter
from apps.core.api_responses import error_response

# Pre-compiled regex pattern for whitespace conversion (performance optimization)
# This pattern matches one or more spaces that are NOT preceded by a backslash
# Used to convert literal spaces to flexible whitespace while preserving escaped spaces
_WHITESPACE_PATTERN = re.compile(r'(?<!\\) +')

# Import UDI for direct data access
from apps.udi import get_udi_manager

# Import centralized stream stats utilities
from apps.core.stream_stats_utils import (
    extract_stream_stats,
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


@app.before_request
def _apply_rate_limit():
    """Apply a lightweight in-memory rate limit to API routes."""
    if not API_RATE_LIMIT_ENABLED:
        return None

    path = request.path or ""
    if not path.startswith('/api/'):
        return None
    if path in {'/api/health', '/api/v1/health', '/api/version', '/api/environment'}:
        return None

    client_ip = request.headers.get('X-Forwarded-For', request.remote_addr or 'unknown')
    key = f"{client_ip}:{path}"
    decision = api_rate_limiter.check(key)
    if decision.allowed:
        return None

    return error_response(
        "Rate limit exceeded",
        status_code=429,
        code="rate_limited",
        details={"retry_after_seconds": decision.retry_after_seconds},
    )


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
channel_service = None
scheduled_event_processor_thread = None
scheduled_event_processor_running = False
scheduled_event_processor_wake = None  # threading.Event to wake up the processor early
epg_refresh_thread = None
epg_refresh_running = False
epg_refresh_wake = None  # threading.Event to wake up the refresh early


def _set_epg_refresh_running(value: bool):
    global epg_refresh_running
    epg_refresh_running = bool(value)

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


def get_channel_service():
    """Get or create channel service instance."""
    global channel_service
    if channel_service is None:
        channel_service = ChannelService(
            repository=UdiChannelRepository(),
            automation_config_manager=get_automation_config_manager(),
            channel_order_manager=get_channel_order_manager(),
            stream_checker_service=get_stream_checker_service(),
        )
    return channel_service

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
    return scheduled_event_processor_loop(
        is_running=lambda: scheduled_event_processor_running,
        get_wake_event=lambda: scheduled_event_processor_wake,
        get_scheduling_service=get_scheduling_service,
        get_stream_checker_service=get_stream_checker_service,
        logger=logger,
        check_interval=30,
    )


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
    return epg_refresh_processor_loop(
        is_running=lambda: epg_refresh_running,
        clear_running=lambda: _set_epg_refresh_running(False),
        get_wake_event=lambda: epg_refresh_wake,
        get_scheduling_service=get_scheduling_service,
        logger=logger,
        initial_delay_seconds=EPG_REFRESH_INITIAL_DELAY_SECONDS,
        error_retry_seconds=EPG_REFRESH_ERROR_RETRY_SECONDS,
    )


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
    return root_response(static_folder=static_folder)

@app.route('/api/health', methods=['GET'])
@app.route('/api/v1/health', methods=['GET'])
def health_check():
    """Health check endpoint."""
    return health_check_response()

@app.route('/health', methods=['GET'])
def health_check_stripped():
    """Health check endpoint for nginx proxy (stripped /api prefix)."""
    return health_check_response()

@app.route('/api/version', methods=['GET'])
def get_version():
    """Get application version."""
    return get_version_response(current_file=Path(__file__))

@app.route('/api/environment', methods=['GET'])
def get_environment():
    """Get environment info including public IP."""
    return get_environment_response()

# Legacy automation endpoints removed. Using newer implementations in 'Automation Service API' section.

@app.route('/api/channels', methods=['GET'])
@app.route('/api/v1/channels', methods=['GET'])
def get_channels():
    """Get all channels from UDI with custom ordering applied."""
    return get_channels_response(
        request_args=request.args,
        parse_pagination_params=_parse_pagination_params,
        get_channel_service=get_channel_service,
    )

@app.route('/api/channels/<channel_id>/stats', methods=['GET'])
@app.route('/api/v1/channels/<channel_id>/stats', methods=['GET'])
def get_channel_stats(channel_id):
    """Get channel statistics including stream count, dead streams, resolution, and bitrate."""
    return get_channel_stats_response(
        channel_id=channel_id,
        get_channel_service=get_channel_service,
    )

@app.route('/api/channels/groups', methods=['GET'])
def get_channel_groups():
    """Get all channel groups from UDI."""
    return get_channel_groups_response(get_udi_manager=get_udi_manager)

@app.route('/api/channels/logos/<logo_id>', methods=['GET'])
def get_channel_logo(logo_id):
    """Get channel logo from UDI."""
    return get_channel_logo_response(logo_id=logo_id, get_udi_manager=get_udi_manager)

@app.route('/api/channels/logos/<logo_id>/cache', methods=['GET'])
def get_channel_logo_cached(logo_id):
    """Download and cache channel logo locally, then serve it."""
    return get_channel_logo_cached_response(
        logo_id=logo_id,
        config_dir=CONFIG_DIR,
        get_udi_manager=get_udi_manager,
        get_dispatcharr_config=get_dispatcharr_config,
    )

@app.route('/api/regex-patterns', methods=['GET'])
@app.route('/api/v1/regex-patterns', methods=['GET'])
def get_regex_patterns():
    """Get all regex patterns for channel matching."""
    return get_regex_patterns_response(get_regex_matcher=get_regex_matcher)

@app.route('/api/regex-patterns', methods=['POST'])
@app.route('/api/v1/regex-patterns', methods=['POST'])
def add_regex_pattern():
    """Add or update a regex pattern for a channel."""
    return add_regex_pattern_response(
        payload=request.get_json(silent=True),
        get_regex_matcher=get_regex_matcher,
    )

@app.route('/api/regex-patterns/<channel_id>', methods=['DELETE'])
@app.route('/api/v1/regex-patterns/<channel_id>', methods=['DELETE'])
def delete_regex_pattern(channel_id):
    """Delete a regex pattern for a channel."""
    return delete_regex_pattern_response(
        channel_id=channel_id,
        get_regex_matcher=get_regex_matcher,
    )

@app.route('/api/channels/<channel_id>/match-settings', methods=['POST'])
def update_channel_match_settings(channel_id):
    """Update matching settings for a channel (e.g., match_by_tvg_id)."""
    return update_channel_match_settings_response(
        channel_id=channel_id,
        payload=request.get_json(silent=True),
        get_regex_matcher=get_regex_matcher,
    )


@app.route('/api/channels/groups/<int:group_id>/regex-config', methods=['GET'])
def get_group_regex_config(group_id):
    """Get regex matching config for a channel group."""
    return get_group_regex_config_response(
        group_id=group_id,
        get_regex_matcher=get_regex_matcher,
    )


@app.route('/api/channels/groups/<int:group_id>/regex-config', methods=['POST'])
def upsert_group_regex_config(group_id):
    """Add or update regex matching config for a channel group."""
    return upsert_group_regex_config_response(
        group_id=group_id,
        payload=request.get_json(silent=True),
        get_regex_matcher=get_regex_matcher,
    )


@app.route('/api/channels/groups/<int:group_id>/regex-config', methods=['DELETE'])
def delete_group_regex_config(group_id):
    """Delete regex matching config for a channel group."""
    return delete_group_regex_config_response(
        group_id=group_id,
        get_regex_matcher=get_regex_matcher,
    )


@app.route('/api/channels/groups/<int:group_id>/match-settings', methods=['POST'])
def update_group_match_settings(group_id):
    """Update match settings for a group (e.g., match_by_tvg_id)."""
    return update_group_match_settings_response(
        group_id=group_id,
        payload=request.get_json(silent=True),
        get_regex_matcher=get_regex_matcher,
    )

@app.route('/api/regex-patterns/bulk', methods=['POST'])
@app.route('/api/v1/regex-patterns/bulk', methods=['POST'])
def add_bulk_regex_patterns():
    """Add the same regex patterns to multiple channels."""
    return add_bulk_regex_patterns_response(
        payload=request.get_json(silent=True),
        get_regex_matcher=get_regex_matcher,
    )

# ==========================================
# Automation Profiles & Settings Endpoints
# ==========================================

@app.route('/api/settings/automation', methods=['GET'])
def get_global_automation_settings():
    """Get global automation settings."""
    return get_global_automation_settings_legacy_response(
        get_automation_config_manager=get_automation_config_manager,
    )

@app.route('/api/settings/automation', methods=['POST'])
def update_global_automation_settings():
    """Update global automation settings."""
    return update_global_automation_settings_legacy_response(
        payload=request.get_json(silent=True),
        get_automation_config_manager=get_automation_config_manager,
    )

@app.route('/api/settings/automation/profiles', methods=['GET'])
def get_automation_profiles():
    """Get all automation profiles."""
    return get_automation_profiles_legacy_response(
        get_automation_config_manager=get_automation_config_manager,
    )

@app.route('/api/settings/automation/profiles', methods=['POST'])
def create_automation_profile():
    """Create a new automation profile."""
    return create_automation_profile_legacy_response(
        payload=request.get_json(silent=True),
        get_automation_config_manager=get_automation_config_manager,
    )

@app.route('/api/settings/automation/profiles/<profile_id>', methods=['GET'])
def get_automation_profile(profile_id):
    """Get a specific automation profile."""
    return get_automation_profile_legacy_response(
        profile_id=profile_id,
        get_automation_config_manager=get_automation_config_manager,
    )

@app.route('/api/settings/automation/profiles/<profile_id>', methods=['PUT'])
def update_automation_profile(profile_id):
    """Update a specific automation profile."""
    return update_automation_profile_legacy_response(
        profile_id=profile_id,
        payload=request.get_json(silent=True),
        get_automation_config_manager=get_automation_config_manager,
    )

@app.route('/api/settings/automation/profiles/<profile_id>', methods=['DELETE'])
def delete_automation_profile(profile_id):
    """Delete a specific automation profile."""
    return delete_automation_profile_legacy_response(
        profile_id=profile_id,
        get_automation_config_manager=get_automation_config_manager,
    )

@app.route('/api/settings/automation/assign/channel', methods=['POST'])
def assign_profile_to_channel():
    """Assign a profile to a channel."""
    return assign_profile_to_channel_legacy_response(
        payload=request.get_json(silent=True),
        get_automation_config_manager=get_automation_config_manager,
    )

@app.route('/api/settings/automation/assign/group', methods=['POST'])
def assign_profile_to_group():
    """Assign a profile to a group."""
    return assign_profile_to_group_legacy_response(
        payload=request.get_json(silent=True),
        get_automation_config_manager=get_automation_config_manager,
    )

@app.route('/api/settings/automation/effective/<int:channel_id>', methods=['GET'])
def get_effective_profile(channel_id):
    """Get the effective profile for a channel (resolving assignments)."""
    return get_effective_profile_legacy_response(
        channel_id=channel_id,
        get_automation_config_manager=get_automation_config_manager,
        get_udi_manager=get_udi_manager,
    )

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
    return import_regex_patterns_response(
        payload=request.get_json(silent=True),
        get_regex_matcher=get_regex_matcher,
    )


@app.route('/api/regex-patterns/export', methods=['GET'])
def export_regex_patterns():
    """Export all regex patterns as a JSON blob (canonical format).

    The returned JSON is compatible with the ``/api/regex-patterns/import``
    endpoint, making round-trip backup/restore straightforward.
    """
    return export_regex_patterns_response()

@app.route('/api/regex-patterns/bulk-delete', methods=['POST'])
def bulk_delete_regex_patterns():
    """Delete all regex patterns from multiple channels."""
    return bulk_delete_regex_patterns_response(
        payload=request.get_json(silent=True),
        get_regex_matcher=get_regex_matcher,
    )

@app.route('/api/regex-patterns/common', methods=['POST'])
def get_common_regex_patterns():
    """Get common regex patterns across multiple channels, ordered by frequency."""
    return get_common_regex_patterns_response(
        payload=request.get_json(silent=True),
        get_regex_matcher=get_regex_matcher,
    )

@app.route('/api/regex-patterns/bulk-edit', methods=['POST'])
def bulk_edit_regex_pattern():
    """Edit a specific regex pattern across multiple channels.
    
    This endpoint allows editing the pattern itself, its associated playlists (m3u_accounts), and priority.
    """
    return bulk_edit_regex_pattern_response(
        payload=request.get_json(silent=True),
        get_regex_matcher=get_regex_matcher,
        get_udi_manager=get_udi_manager,
    )

@app.route('/api/regex-patterns/bulk-settings', methods=['POST'])
def bulk_update_match_settings():
    """Update match settings (e.g., match_by_tvg_id) for multiple channels."""
    return bulk_update_match_settings_response(
        payload=request.get_json(silent=True),
        get_regex_matcher=get_regex_matcher,
    )

@app.route('/api/settings/automation/global', methods=['GET', 'PUT'])
def handle_global_automation_settings():
    """Get or update global automation settings."""
    return handle_global_automation_settings_response(
        method=request.method,
        updates=request.get_json(silent=True),
        get_automation_config_manager=get_automation_config_manager,
        check_wizard_complete=check_wizard_complete,
        get_automation_manager=get_automation_manager,
    )

@app.route('/api/regex-patterns/mass-edit-preview', methods=['POST'])
def mass_edit_preview():
    """Preview the results of a mass find/replace operation on regex patterns.
    
    This endpoint shows what patterns will be affected by the find/replace operation
    without actually making changes.
    """
    return mass_edit_preview_response(
        payload=request.get_json(silent=True),
        get_regex_matcher=get_regex_matcher,
        get_udi_manager=get_udi_manager,
        is_dangerous_regex=is_dangerous_regex,
    )

@app.route('/api/regex-patterns/mass-edit', methods=['POST'])
def mass_edit_regex_patterns():
    """Apply a mass find/replace operation on regex patterns across multiple channels.
    
    This endpoint performs find/replace on all patterns in the selected channels,
    optionally updating M3U accounts as well.
    """
    return mass_edit_regex_patterns_response(
        payload=request.get_json(silent=True),
        get_regex_matcher=get_regex_matcher,
        get_udi_manager=get_udi_manager,
        is_dangerous_regex=is_dangerous_regex,
    )

@app.route('/api/test-regex', methods=['POST'])
def test_regex_pattern():
    """Test a regex pattern against a stream name."""
    return test_regex_pattern_response(
        payload=request.get_json(silent=True),
        is_dangerous_regex=is_dangerous_regex,
        whitespace_pattern=_WHITESPACE_PATTERN,
    )

@app.route('/api/test-regex-live', methods=['POST'])
def test_regex_pattern_live():
    """Test regex patterns against all available streams to see what would be matched."""
    return test_regex_pattern_live_response(
        payload=request.get_json(silent=True),
        is_dangerous_regex=is_dangerous_regex,
        whitespace_pattern=_WHITESPACE_PATTERN,
    )




@app.route('/api/changelog', methods=['GET'])
def get_changelog():
    """Get recent changelog entries from the new telemetry database."""
    return get_changelog_response(request_args=request.args)

@app.route('/api/dead-streams', methods=['GET'])
def get_dead_streams():
    """Get dead streams statistics and list with SQL-native pagination, sorting, and filtering."""
    return get_dead_streams_response(
        request_args=request.args,
        parse_pagination_params=_parse_pagination_params,
        default_per_page=DEAD_STREAMS_DEFAULT_PER_PAGE,
        max_per_page=DEAD_STREAMS_MAX_PER_PAGE,
    )

@app.route('/api/dead-streams/revive', methods=['POST'])
def revive_dead_stream():
    """Mark a stream as alive (remove from dead streams)."""
    return revive_dead_stream_response(
        payload=request.get_json(silent=True),
        get_stream_checker_service=get_stream_checker_service,
    )

@app.route('/api/dead-streams/clear', methods=['POST'])
def clear_all_dead_streams():
    """Clear all dead streams from the tracker."""
    return clear_all_dead_streams_response(
        get_stream_checker_service=get_stream_checker_service,
    )



# ==================== CHANNEL ORDER ENDPOINTS ====================

@app.route('/api/channel-order', methods=['GET'])
def get_channel_order():
    """Get current channel order configuration."""
    return get_channel_order_response(get_channel_order_manager=get_channel_order_manager)

@app.route('/api/channel-order', methods=['PUT'])
def set_channel_order():
    """Set channel order configuration."""
    return set_channel_order_response(
        payload=request.get_json(silent=True),
        get_channel_order_manager=get_channel_order_manager,
    )

@app.route('/api/channel-order', methods=['DELETE'])
def clear_channel_order():
    """Clear custom channel order (revert to default)."""
    return clear_channel_order_response(get_channel_order_manager=get_channel_order_manager)

@app.route('/api/discover-streams', methods=['POST'])
def discover_streams():
    """Trigger stream discovery and assignment (manual Quick Action)."""
    return discover_streams_response(get_automation_manager=get_automation_manager)

@app.route('/api/refresh-playlist', methods=['POST'])
def refresh_playlist():
    """Trigger M3U playlist refresh (manual Quick Action)."""
    return refresh_playlist_response(
        payload=request.get_json(silent=True),
        get_automation_manager=get_automation_manager,
    )

@app.route('/api/m3u-accounts', methods=['GET'])
def get_m3u_accounts_endpoint():
    """Get all M3U accounts from Dispatcharr, filtering out 'custom' account if no custom streams exist and non-active accounts.
    
    (Priority is now handled per-profile).
    """
    from apps.core.api_utils import get_m3u_accounts, has_custom_streams

    return get_m3u_accounts_response(
        get_m3u_accounts=get_m3u_accounts,
        has_custom_streams=has_custom_streams,
    )





@app.route('/api/setup-wizard', methods=['GET'])
def get_setup_wizard_status():
    """Get setup wizard completion status."""
    return get_setup_wizard_status_response(
        test_mode=os.getenv('TEST_MODE', 'false').lower() == 'true',
        get_automation_config_manager=get_automation_config_manager,
        get_dispatcharr_config=get_dispatcharr_config,
        get_udi_manager=get_udi_manager,
    )

@app.route('/api/test-match-live', methods=['POST'])
def test_match_live():
    """
    Test stream matching against all available streams with full configuration 
    (Regex + TVG-ID + Priority).
    
    Used for the enhanced preview in Channel Configuration.
    """
    from apps.core.api_utils import get_streams, get_m3u_accounts

    return test_match_live_response(
        payload=request.get_json(silent=True),
        get_streams=get_streams,
        get_m3u_accounts=get_m3u_accounts,
        is_dangerous_regex=is_dangerous_regex,
    )

@app.route('/api/regex-match-counts', methods=['POST'])
def regex_match_counts():
    """
    Bulk regex match count for multiple channels in a single stream pool scan.

    Accepts an array of channel configs (each with regex_patterns, tvg_id, etc.)
    and returns per-channel match counts against the full UDI stream pool.
    Used to display 'X / Y' (potential matches / assigned streams) on the
    Regex Configuration tab without N individual test-match-live calls.
    """
    from apps.core.api_utils import get_streams, get_m3u_accounts

    return bulk_match_count_response(
        payload=request.get_json(silent=True),
        get_streams=get_streams,
        get_m3u_accounts=get_m3u_accounts,
        is_dangerous_regex=is_dangerous_regex,
    )

@app.route('/api/setup-wizard/ensure-config', methods=['POST'])
def ensure_wizard_config():
    """Ensure wizard configuration files exist (creates empty files if needed).
    
    This endpoint is called during wizard progression to ensure required config
    files exist, even if users skip optional steps like pattern configuration.
    """
    return ensure_wizard_config_response(
        get_automation_config_manager=get_automation_config_manager,
    )

@app.route('/api/setup-wizard/create-sample-patterns', methods=['POST'])
def create_sample_patterns():
    """Create sample regex patterns for testing setup completion."""
    return create_sample_patterns_response()

@app.route('/api/dispatcharr/config', methods=['GET'])
def get_dispatcharr_config_endpoint():
    """Get current Dispatcharr configuration (without exposing password)."""
    return get_dispatcharr_config_response(
        get_dispatcharr_config=get_dispatcharr_config,
    )

@app.route('/api/dispatcharr/config', methods=['PUT'])
def update_dispatcharr_config_endpoint():
    """Update Dispatcharr configuration."""
    return update_dispatcharr_config_response(
        payload=request.get_json(silent=True),
        get_dispatcharr_config=get_dispatcharr_config,
        get_udi_manager=get_udi_manager,
    )

@app.route('/api/dispatcharr/test-connection', methods=['POST'])
def test_dispatcharr_connection():
    """Test Dispatcharr connection with provided or existing credentials."""
    return test_dispatcharr_connection_response(
        payload=request.get_json(silent=True),
        get_dispatcharr_config=get_dispatcharr_config,
    )

@app.route('/api/dispatcharr/initialization-status', methods=['GET'])
def get_udi_initialization_status():
    """Get the current UDI initialization progress."""
    return get_udi_initialization_status_response(
        get_udi_manager=get_udi_manager,
    )

@app.route('/api/dispatcharr/initialize-udi', methods=['POST'])
def initialize_udi():
    """Initialize UDI Manager with current Dispatcharr credentials.
    
    This endpoint should be called after successfully testing the Dispatcharr
    connection to ensure the UDI Manager is initialized with fresh data from
    the Dispatcharr API.
    """
    return initialize_udi_response(
        get_dispatcharr_config=get_dispatcharr_config,
        get_udi_manager=get_udi_manager,
    )

# ===== Stream Checker Endpoints =====

@app.route('/api/stream-checker/status', methods=['GET'])
def get_stream_checker_status():
    """Get current stream checker status."""
    return get_stream_checker_status_response(
        get_stream_checker_service=get_stream_checker_service,
        concurrent_streams_enabled_key=CONCURRENT_STREAMS_ENABLED_KEY,
        concurrent_streams_global_limit_key=CONCURRENT_STREAMS_GLOBAL_LIMIT_KEY,
    )

@app.route('/api/stream-checker/start', methods=['POST'])
def start_stream_checker():
    """Start the stream checker service."""
    return start_stream_checker_response(get_stream_checker_service=get_stream_checker_service)

@app.route('/api/stream-checker/stop', methods=['POST'])
def stop_stream_checker():
    """Stop the stream checker service."""
    return stop_stream_checker_response(get_stream_checker_service=get_stream_checker_service)

@app.route('/api/stream-checker/queue', methods=['GET'])
def get_stream_checker_queue():
    """Get current queue status."""
    return get_stream_checker_queue_response(get_stream_checker_service=get_stream_checker_service)

@app.route('/api/stream-checker/queue/add', methods=['POST'])
def add_to_stream_checker_queue():
    """Add channel(s) to the checking queue."""
    return add_to_stream_checker_queue_response(
        payload=request.get_json(silent=True),
        get_stream_checker_service=get_stream_checker_service,
    )

@app.route('/api/stream-checker/queue/clear', methods=['POST'])
def clear_stream_checker_queue():
    """Clear the checking queue."""
    return clear_stream_checker_queue_response(get_stream_checker_service=get_stream_checker_service)

@app.route('/api/stream-checker/config', methods=['GET'])
def get_stream_checker_config():
    """Get stream checker configuration."""
    return get_stream_checker_config_response(get_stream_checker_service=get_stream_checker_service)

@app.route('/api/stream-checker/config', methods=['PUT'])
def update_stream_checker_config():
    """Update stream checker configuration."""
    return update_stream_checker_config_response(
        payload=request.get_json(silent=True),
        croniter_available=CRONITER_AVAILABLE,
        croniter_module=globals().get('croniter'),
        get_stream_checker_service=get_stream_checker_service,
        get_automation_manager=get_automation_manager,
        get_automation_config_manager=get_automation_config_manager,
        check_wizard_complete=check_wizard_complete,
        stop_scheduled_event_processor=stop_scheduled_event_processor,
        stop_epg_refresh_processor=stop_epg_refresh_processor,
        start_scheduled_event_processor=start_scheduled_event_processor,
        start_epg_refresh_processor=start_epg_refresh_processor,
        scheduled_event_processor_running=bool(
            scheduled_event_processor_thread and scheduled_event_processor_thread.is_alive()
        ),
        epg_refresh_running=bool(epg_refresh_thread and epg_refresh_thread.is_alive()),
    )

@app.route('/api/stream-checker/progress', methods=['GET'])
def get_stream_checker_progress():
    """Get current checking progress."""
    return get_stream_checker_progress_response(get_stream_checker_service=get_stream_checker_service)

@app.route('/api/stream-checker/check-channel', methods=['POST'])
def check_specific_channel():
    """Manually check a specific channel immediately (add to queue with high priority)."""
    return check_specific_channel_response(
        payload=request.get_json(silent=True),
        get_stream_checker_service=get_stream_checker_service,
    )

@app.route('/api/stream-checker/check-single-channel', methods=['POST'])
def check_single_channel_now():
    """Immediately check a single channel synchronously and return results."""
    return check_single_channel_now_response(
        payload=request.get_json(silent=True),
        get_stream_checker_service=get_stream_checker_service,
    )

@app.route('/api/stream-checker/mark-updated', methods=['POST'])
def mark_channels_updated():
    """Mark channels as updated (triggered by M3U refresh)."""
    return mark_channels_updated_response(
        payload=request.get_json(silent=True),
        get_stream_checker_service=get_stream_checker_service,
    )

@app.route('/api/stream-checker/queue-all', methods=['POST'])
def queue_all_channels():
    """Queue all channels for checking (manual trigger for full check)."""
    return queue_all_channels_response(
        get_stream_checker_service=get_stream_checker_service,
        get_udi_manager=get_udi_manager,
    )


# ============================================================================
# Scheduling API Endpoints
# ============================================================================

@app.route('/api/scheduling/config', methods=['GET'])
@log_function_call
def get_scheduling_config():
    """Get scheduling configuration including EPG refresh interval."""
    return get_scheduling_config_response(get_scheduling_service=get_scheduling_service)

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
    return update_scheduling_config_response(
        payload=request.get_json(silent=True),
        get_scheduling_service=get_scheduling_service,
    )

@app.route('/api/scheduling/epg/grid', methods=['GET'])
@log_function_call
def get_epg_grid():
    """Get EPG grid data (all programs for next 24 hours).
    
    Query parameters:
    - force_refresh: If true, bypass cache and fetch fresh data
    """
    return get_epg_grid_response(
        force_refresh=request.args.get('force_refresh', 'false').lower() == 'true',
        get_scheduling_service=get_scheduling_service,
    )

@app.route('/api/scheduling/epg/channel/<int:channel_id>', methods=['GET'])
@log_function_call
def get_channel_programs(channel_id):
    """Get programs for a specific channel.
    
    Args:
        channel_id: Channel ID
    
    Returns:
        List of programs for the channel
    """
    return get_channel_programs_response(
        channel_id=channel_id,
        get_scheduling_service=get_scheduling_service,
    )

@app.route('/api/scheduling/events', methods=['GET'])
@log_function_call
def get_scheduled_events():
    """Get all scheduled events."""
    return get_scheduled_events_response(get_scheduling_service=get_scheduling_service)

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
    return create_scheduled_event_response(
        payload=request.get_json(silent=True),
        get_scheduling_service=get_scheduling_service,
        scheduled_event_processor_wake=scheduled_event_processor_wake,
    )

@app.route('/api/scheduling/events/<event_id>', methods=['DELETE'])
@log_function_call
def delete_scheduled_event(event_id):
    """Delete a scheduled event.
    
    Args:
        event_id: Event ID
    """
    return delete_scheduled_event_response(
        event_id=event_id,
        get_scheduling_service=get_scheduling_service,
    )


@app.route('/api/scheduling/auto-create-rules', methods=['GET'])
@log_function_call
def get_auto_create_rules():
    """Get all auto-create rules."""
    return get_auto_create_rules_response(get_scheduling_service=get_scheduling_service)


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
    return create_auto_create_rule_response(
        payload=request.get_json(silent=True),
        get_scheduling_service=get_scheduling_service,
        scheduled_event_processor_wake=scheduled_event_processor_wake,
    )


@app.route('/api/scheduling/auto-create-rules/<rule_id>', methods=['DELETE'])
@log_function_call
def delete_auto_create_rule(rule_id):
    """Delete an auto-create rule.
    
    Args:
        rule_id: Rule ID
    """
    return delete_auto_create_rule_response(
        rule_id=rule_id,
        get_scheduling_service=get_scheduling_service,
    )


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
    return update_auto_create_rule_response(
        rule_id=rule_id,
        payload=request.get_json(silent=True),
        get_scheduling_service=get_scheduling_service,
        scheduled_event_processor_wake=scheduled_event_processor_wake,
    )


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
    return test_auto_create_rule_response(
        payload=request.get_json(silent=True),
        get_scheduling_service=get_scheduling_service,
    )


@app.route('/api/scheduling/auto-create-rules/export', methods=['GET'])
@log_function_call
def export_auto_create_rules():
    """Export all auto-create rules as JSON.
    
    Returns:
        JSON array of auto-create rules
    """
    return export_auto_create_rules_response(get_scheduling_service=get_scheduling_service)


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
    return import_auto_create_rules_response(
        payload=request.get_json(silent=True),
        get_scheduling_service=get_scheduling_service,
        scheduled_event_processor_wake=scheduled_event_processor_wake,
    )


@app.route('/api/scheduling/process-due-events', methods=['POST'])
@log_function_call
def process_due_scheduled_events():
    """Process all scheduled events that are due for execution.
    
    This endpoint should be called periodically (e.g., by a cron job or scheduler)
    to check for and execute any scheduled channel checks.
    
    Returns:
        JSON with execution results
    """
    return process_due_scheduled_events_response(
        get_scheduling_service=get_scheduling_service,
        get_stream_checker_service=get_stream_checker_service,
    )


@app.route('/api/scheduling/processor/status', methods=['GET'])
@log_function_call
def get_scheduled_event_processor_status():
    """Get the status of the scheduled event processor background thread.
    
    Returns:
        JSON with processor status
    """
    return get_scheduled_event_processor_status_response(
        scheduled_event_processor_thread=scheduled_event_processor_thread,
        scheduled_event_processor_running=scheduled_event_processor_running,
    )


@app.route('/api/scheduling/processor/start', methods=['POST'])
@log_function_call
def start_scheduled_event_processor_api():
    """Start the scheduled event processor background thread.
    
    Returns:
        JSON with result
    """
    return start_scheduled_event_processor_api_response(
        start_scheduled_event_processor=start_scheduled_event_processor,
    )


@app.route('/api/scheduling/processor/stop', methods=['POST'])
@log_function_call
def stop_scheduled_event_processor_api():
    """Stop the scheduled event processor background thread.
    
    Returns:
        JSON with result
    """
    return stop_scheduled_event_processor_api_response(
        stop_scheduled_event_processor=stop_scheduled_event_processor,
    )


@app.route('/api/scheduling/epg-refresh/status', methods=['GET'])
@log_function_call
def get_epg_refresh_processor_status():
    """Get the status of the EPG refresh processor background thread.
    
    Returns:
        JSON with processor status
    """
    return get_epg_refresh_processor_status_response(
        epg_refresh_thread=epg_refresh_thread,
        epg_refresh_running=epg_refresh_running,
    )


@app.route('/api/scheduling/epg-refresh/start', methods=['POST'])
@log_function_call
def start_epg_refresh_processor_api():
    """Start the EPG refresh processor background thread.
    
    Returns:
        JSON with result
    """
    return start_epg_refresh_processor_api_response(
        start_epg_refresh_processor=start_epg_refresh_processor,
    )


@app.route('/api/scheduling/epg-refresh/stop', methods=['POST'])
@log_function_call
def stop_epg_refresh_processor_api():
    """Stop the EPG refresh processor background thread.
    
    Returns:
        JSON with result
    """
    return stop_epg_refresh_processor_api_response(
        stop_epg_refresh_processor=stop_epg_refresh_processor,
    )


@app.route('/api/scheduling/epg-refresh/trigger', methods=['POST'])
@log_function_call
def trigger_epg_refresh():
    """Manually trigger an immediate EPG refresh.
    
    Returns:
        JSON with result
    """
    return trigger_epg_refresh_response(
        epg_refresh_wake=epg_refresh_wake,
        epg_refresh_running=epg_refresh_running,
        epg_refresh_thread=epg_refresh_thread,
    )

# ==================== Automation Service API ====================

@app.route('/api/automation/status', methods=['GET'])
@log_function_call
def get_automation_status():
    """Get the status of the automation background service.
    
    Returns:
        JSON with service status
    """
    return get_automation_status_response(
        get_automation_manager=get_automation_manager,
        get_automation_config_manager=get_automation_config_manager,
    )


@app.route('/api/automation/start', methods=['POST'])
@log_function_call
def start_automation_service_api():
    """Start the automation background service.
    
    Returns:
        JSON with result
    """
    return start_automation_service_api_response(get_automation_manager=get_automation_manager)


@app.route('/api/automation/stop', methods=['POST'])
@log_function_call
def stop_automation_service_api():
    """Stop the automation background service.
    
    Returns:
        JSON with result
    """
    return stop_automation_service_api_response(get_automation_manager=get_automation_manager)


@app.route('/api/automation/trigger', methods=['POST'])
@log_function_call
def trigger_automation_cycle():
    """Manually trigger an immediate automation cycle."""
    return trigger_automation_cycle_response(
        payload=request.get_json(silent=True),
        get_automation_manager=get_automation_manager,
    )


@app.route('/api/automation/config', methods=['GET', 'PUT'])
@log_function_call
def handle_automation_global_config():
    """Get or update global automation configuration."""
    return handle_global_automation_settings_response(
        method=request.method,
        updates=request.get_json(silent=True),
        get_automation_config_manager=get_automation_config_manager,
        check_wizard_complete=check_wizard_complete,
        get_automation_manager=get_automation_manager,
    )


@app.route('/api/automation/profiles', methods=['GET', 'POST'])
@log_function_call
def handle_automation_profiles():
    """Get all profiles or create a new profile.

    GET supports optional query parameters:
      - search (str): filter profiles by name (case-insensitive).
      - page (int): page number (1-based). If omitted, returns full list.
      - per_page (int): items per page (default 50, max 200).
    """
    return handle_automation_profiles_response(
        method=request.method,
        args=request.args,
        payload=request.get_json(silent=True),
        get_automation_config_manager=get_automation_config_manager,
    )


@app.route('/api/automation/profiles/bulk-delete', methods=['POST'])
@log_function_call
def bulk_delete_automation_profiles():
    """Delete multiple automation profiles at once."""
    return bulk_delete_automation_profiles_response(
        payload=request.get_json(silent=True),
        get_automation_config_manager=get_automation_config_manager,
    )


@app.route('/api/automation/profiles/<profile_id>', methods=['GET', 'PUT', 'DELETE'])
@log_function_call
def handle_automation_profile(profile_id):
    """Get, update, or delete a specific automation profile."""
    return handle_automation_profile_response(
        method=request.method,
        profile_id=profile_id,
        payload=request.get_json(silent=True),
        get_automation_config_manager=get_automation_config_manager,
    )


@app.route('/api/automation/assign/channel', methods=['POST'])
@log_function_call
def assign_automation_profile_channel():
    """Assign an automation profile to a channel."""
    return assign_automation_profile_channel_response(
        payload=request.get_json(silent=True),
        get_automation_config_manager=get_automation_config_manager,
    )


@app.route('/api/automation/assign/channels', methods=['POST'])
@log_function_call
def assign_automation_profile_channels():
    """Assign an automation profile to multiple channels."""
    return assign_automation_profile_channels_response(
        payload=request.get_json(silent=True),
        get_automation_config_manager=get_automation_config_manager,
    )


@app.route('/api/automation/assign/group', methods=['GET', 'POST'])
@log_function_call
def assign_automation_profile_group():
    """GET: Return all group→automation-profile assignments.
    POST: Assign (or remove) an automation profile for a channel group."""
    return assign_automation_profile_group_response(
        method=request.method,
        payload=request.get_json(silent=True),
        get_automation_config_manager=get_automation_config_manager,
    )


@app.route('/api/automation/assign/epg-profile/channel', methods=['POST'])
@log_function_call
def assign_epg_scheduled_profile_channel():
    """Assign an EPG scheduled automation profile to a channel."""
    return assign_epg_scheduled_profile_channel_response(
        payload=request.get_json(silent=True),
        get_automation_config_manager=get_automation_config_manager,
    )


@app.route('/api/automation/assign/epg-profile/channels', methods=['POST'])
@log_function_call
def assign_epg_scheduled_profile_channels():
    """Assign an EPG scheduled automation profile to multiple channels."""
    return assign_epg_scheduled_profile_channels_response(
        payload=request.get_json(silent=True),
        get_automation_config_manager=get_automation_config_manager,
    )


@app.route('/api/automation/assign/epg-profile/group', methods=['GET', 'POST'])
@log_function_call
def assign_epg_scheduled_profile_group():
    """GET: Return all group→EPG-profile assignments.
    POST: Assign (or remove) an EPG scheduled automation profile for a channel group."""
    return assign_epg_scheduled_profile_group_response(
        method=request.method,
        payload=request.get_json(silent=True),
        get_automation_config_manager=get_automation_config_manager,
    )


@app.route('/api/channels/groups/<int:group_id>/automation-periods', methods=['GET'])
@log_function_call
def get_group_automation_periods(group_id):
    """Get all automation periods assigned to a group."""
    return get_group_automation_periods_response(
        group_id=group_id,
        get_automation_config_manager=get_automation_config_manager,
    )


@app.route('/api/automation/periods/<period_id>/assign-groups', methods=['POST'])
@log_function_call
def assign_period_to_groups(period_id):
    """Assign an automation period to one or more groups with a profile."""
    return assign_period_to_groups_response(
        period_id=period_id,
        payload=request.get_json(silent=True),
        get_automation_config_manager=get_automation_config_manager,
    )


@app.route('/api/automation/periods/<period_id>/remove-groups', methods=['POST'])
@log_function_call
def remove_period_from_groups(period_id):
    """Remove an automation period from specific groups."""
    return remove_period_from_groups_response(
        period_id=period_id,
        payload=request.get_json(silent=True),
        get_automation_config_manager=get_automation_config_manager,
    )


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
    return batch_assign_periods_to_groups_response(
        payload=request.get_json(silent=True),
        get_automation_config_manager=get_automation_config_manager,
    )


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
    return handle_automation_periods_response(
        method=request.method,
        args=request.args,
        payload=request.get_json(silent=True),
        get_automation_config_manager=get_automation_config_manager,
        croniter_available=CRONITER_AVAILABLE,
        croniter_module=globals().get('croniter'),
    )


@app.route('/api/automation/periods/<period_id>', methods=['GET', 'PUT', 'DELETE'])
@log_function_call
def handle_automation_period(period_id):
    """Get, update, or delete a specific automation period."""
    return handle_automation_period_response(
        method=request.method,
        period_id=period_id,
        payload=request.get_json(silent=True),
        get_automation_config_manager=get_automation_config_manager,
        croniter_available=CRONITER_AVAILABLE,
        croniter_module=globals().get('croniter'),
    )


@app.route('/api/automation/periods/<period_id>/assign-channels', methods=['POST'])
@log_function_call
def assign_period_to_channels(period_id):
    """Assign an automation period to multiple channels with a profile."""
    return assign_period_to_channels_response(
        period_id=period_id,
        payload=request.get_json(silent=True),
        get_automation_config_manager=get_automation_config_manager,
    )


@app.route('/api/automation/periods/<period_id>/remove-channels', methods=['POST'])
@log_function_call
def remove_period_from_channels(period_id):
    """Remove an automation period from specific channels."""
    return remove_period_from_channels_response(
        period_id=period_id,
        payload=request.get_json(silent=True),
        get_automation_config_manager=get_automation_config_manager,
    )


@app.route('/api/automation/periods/<period_id>/channels', methods=['GET'])
@log_function_call
def get_period_channels(period_id):
    """Get all channels assigned to a period."""
    return get_period_channels_response(
        period_id=period_id,
        get_automation_config_manager=get_automation_config_manager,
        get_udi_manager=get_udi_manager,
    )


@app.route('/api/channels/batch/period-usage', methods=['POST'])
@log_function_call
def get_batch_period_usage():
    """Analyze automation period usage across multiple channels."""
    return get_batch_period_usage_response(
        payload=request.get_json(),
        get_automation_config_manager=get_automation_config_manager,
    )

@app.route('/api/channels/<int:channel_id>/automation-periods', methods=['GET'])
@log_function_call
def get_channel_automation_periods(channel_id):
    """Get all automation periods assigned to a channel (including group-inherited ones)."""
    return get_channel_automation_periods_response(
        channel_id=channel_id,
        get_automation_config_manager=get_automation_config_manager,
        get_udi_manager=get_udi_manager,
    )


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
    return batch_assign_periods_to_channels_response(
        payload=request.get_json(silent=True),
        get_automation_config_manager=get_automation_config_manager,
    )


# ==================== Automation Events API ====================
# Calculate and retrieve upcoming automation events based on periods


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
    return get_upcoming_automation_events_response(
        args=request.args,
        get_events_scheduler=get_events_scheduler,
        get_automation_config_manager=get_automation_config_manager,
    )


@app.route('/api/automation/events/invalidate-cache', methods=['POST'])
@log_function_call
def invalidate_automation_events_cache():
    """Invalidate the automation events cache.
    
    This should be called whenever automation periods are modified.
    """
    return invalidate_automation_events_cache_response(
        get_events_scheduler=get_events_scheduler,
    )


# ==================== Stream Monitoring Session API ====================
# Advanced stream monitoring with live quality tracking, reliability scoring,
# and screenshot capture for event-based stream management

from apps.stream.stream_session_manager import get_session_manager, REVIEW_DURATION
from apps.stream.stream_monitoring_service import get_monitoring_service
from apps.stream.acestream_monitoring_client import AceStreamMonitoringClient, normalize_content_id
from apps.stream.acestream_session_service import (
    annotate_monitors_with_playback as _service_annotate_monitors_with_playback,
    _compute_ace_management_score as _service_compute_ace_management_score,
    _evaluate_ace_entry_management as _service_evaluate_ace_entry_management,
    _schedule_ace_ffprobe_recheck as _service_schedule_ace_ffprobe_recheck,
    apply_ace_dispatcharr_sync as _service_apply_ace_dispatcharr_sync,
    build_ace_channel_session_summary as _service_build_ace_channel_session_summary,
    check_ace_session_epg_auto_stop as _service_check_ace_session_epg_auto_stop,
    compact_ace_monitor_payload as _service_compact_ace_monitor_payload,
    create_acestream_channel_session_impl as _service_create_acestream_channel_session_impl,
    evaluate_ace_session_management as _service_evaluate_ace_session_management,
    get_ace_management_settings as _service_get_ace_management_settings,
    refresh_ace_session_streams as _service_refresh_ace_session_streams,
    save_ace_session_telemetry_snapshot as _service_save_ace_session_telemetry_snapshot,
)


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
    """Ping the orchestrator /api/v1/version endpoint to verify it is running and reachable.

    Returns (True, version_str) on success, (False, error_msg) on failure.
    The response must be a JSON object with a 'title' field containing 'AceStream Orchestrator'.
    """
    if client is None:
        client = _get_acestream_monitoring_client()
    if not client.is_configured():
        return False, "AceStream orchestrator is not configured"

    base_url = client.base_url
    version_url = f"{base_url.rstrip('/')}/api/v1/version"
    try:
        resp = requests.get(version_url, timeout=5)
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, dict):
            return False, "Orchestrator /api/v1/version returned unexpected format"
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
        return False, f"Orchestrator /api/v1/version returned HTTP {exc.response.status_code}"
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
    return _service_get_ace_management_settings()


def _compute_ace_management_score(monitor, entry=None):
    return _service_compute_ace_management_score(monitor, entry)


def _evaluate_ace_entry_management(entry, monitor, now_ts, settings):
    return _service_evaluate_ace_entry_management(entry, monitor, now_ts, settings)

def _schedule_ace_ffprobe_recheck(entry, monitor):
    return _service_schedule_ace_ffprobe_recheck(entry, monitor)


def _evaluate_ace_session_management(raw_session, monitors_by_id, settings):
    return _service_evaluate_ace_session_management(raw_session, monitors_by_id, settings)


def _check_ace_session_epg_auto_stop(raw_session, client):
    return _service_check_ace_session_epg_auto_stop(raw_session, client)


def _apply_ace_dispatcharr_sync(raw_session):
    return _service_apply_ace_dispatcharr_sync(raw_session)


def _save_ace_session_telemetry_snapshot(raw_session, monitors_by_id):
    return _service_save_ace_session_telemetry_snapshot(raw_session, monitors_by_id)


def _build_ace_channel_session_summary(raw_session, monitors_by_id):
    return _service_build_ace_channel_session_summary(raw_session, monitors_by_id)


def _annotate_monitors_with_playback(client, monitors_by_id):
    return _service_annotate_monitors_with_playback(client, monitors_by_id)


def _compact_ace_monitor_payload(monitor, recent_limit=8):
    return _service_compact_ace_monitor_payload(monitor, recent_limit=recent_limit)


@app.route('/api/acestream-channel-sessions', methods=['GET'])
def list_acestream_channel_sessions():
    """List channel-scoped AceStream monitoring sessions."""
    return list_acestream_channel_sessions_response(
        args=request.args,
        get_client_or_error=_acestream_client_or_error,
        load_store=_ace_channel_sessions_store,
        get_management_settings=_get_ace_management_settings,
        save_store=_save_ace_channel_sessions_store,
        annotate_playback=_annotate_monitors_with_playback,
        evaluate_management=_evaluate_ace_session_management,
        save_telemetry_snapshot=_save_ace_session_telemetry_snapshot,
        check_epg_auto_stop=_check_ace_session_epg_auto_stop,
        refresh_session_streams=_refresh_ace_session_streams,
        apply_dispatcharr_sync=_apply_ace_dispatcharr_sync,
        build_summary=_build_ace_channel_session_summary,
    )


def _refresh_ace_session_streams(raw_session, client, interval_s=1.0, run_seconds=0, per_sample_timeout_s=1.0):
    return _service_refresh_ace_session_streams(
        raw_session,
        client,
        interval_s=interval_s,
        run_seconds=run_seconds,
        per_sample_timeout_s=per_sample_timeout_s,
    )


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
    return _service_create_acestream_channel_session_impl(
        channel_id,
        interval_s=interval_s,
        run_seconds=run_seconds,
        per_sample_timeout_s=per_sample_timeout_s,
        engine_container_id=engine_container_id,
        epg_event_title=epg_event_title,
        epg_event_description=epg_event_description,
        epg_event_start=epg_event_start,
        epg_event_end=epg_event_end,
        epg_event_id=epg_event_id,
        get_client_or_error=_acestream_client_or_error,
        ping_orchestrator_ready=_ping_orchestrator_ready,
        load_store=_ace_channel_sessions_store,
        save_store=_save_ace_channel_sessions_store,
    )


@app.route('/api/acestream-channel-sessions', methods=['POST'])
def create_acestream_channel_session():
    """Create and start AceStream monitoring for all AceStream streams in a channel."""
    return create_acestream_channel_session_response(
        payload=request.get_json(silent=True),
        create_session_impl=create_acestream_channel_session_impl,
    )


@app.route('/api/acestream-channel-sessions/group/start', methods=['POST'])
def create_acestream_group_sessions():
    """Create AceStream channel sessions for all channels in a group."""
    return create_acestream_group_sessions_response(
        payload=request.get_json(silent=True),
        get_client_or_error=_acestream_client_or_error,
        ping_orchestrator_ready=_ping_orchestrator_ready,
        get_udi_manager=get_udi_manager,
        create_session_impl=create_acestream_channel_session_impl,
    )


@app.route('/api/acestream-channel-sessions/<session_id>', methods=['GET'])
def get_acestream_channel_session(session_id):
    """Get detailed channel-scoped AceStream monitoring session."""
    return get_acestream_channel_session_response(
        session_id=session_id,
        get_client_or_error=_acestream_client_or_error,
        load_store=_ace_channel_sessions_store,
        get_management_settings=_get_ace_management_settings,
        save_store=_save_ace_channel_sessions_store,
        annotate_playback=_annotate_monitors_with_playback,
        evaluate_management=_evaluate_ace_session_management,
        save_telemetry_snapshot=_save_ace_session_telemetry_snapshot,
        check_epg_auto_stop=_check_ace_session_epg_auto_stop,
        apply_dispatcharr_sync=_apply_ace_dispatcharr_sync,
        build_summary=_build_ace_channel_session_summary,
        compact_monitor_payload=_compact_ace_monitor_payload,
    )


@app.route('/api/acestream-channel-sessions/<session_id>/stop', methods=['POST'])
def stop_acestream_channel_session(session_id):
    """Stop all orchestrator monitor sessions attached to a channel session."""
    return stop_acestream_channel_session_response(
        session_id=session_id,
        get_client_or_error=_acestream_client_or_error,
        load_store=_ace_channel_sessions_store,
    )


@app.route('/api/acestream-channel-sessions/<session_id>', methods=['DELETE'])
def delete_acestream_channel_session(session_id):
    """Delete channel session and all orchestrator monitor entries."""
    return delete_acestream_channel_session_response(
        session_id=session_id,
        get_client_or_error=_acestream_client_or_error,
        load_store=_ace_channel_sessions_store,
        save_store=_save_ace_channel_sessions_store,
    )


@app.route('/api/acestream-channel-sessions/<session_id>/streams/<int:stream_id>/quarantine', methods=['POST'])
def quarantine_acestream_channel_stream(session_id, stream_id):
    """Manually quarantine one Ace stream entry within a channel session."""
    return quarantine_acestream_channel_stream_response(
        session_id=session_id,
        stream_id=stream_id,
        load_store=_ace_channel_sessions_store,
        save_store=_save_ace_channel_sessions_store,
        now_ts=time.time(),
    )


@app.route('/api/acestream-channel-sessions/<session_id>/streams/<int:stream_id>/revive', methods=['POST'])
def revive_acestream_channel_stream(session_id, stream_id):
    """Revive one manually quarantined Ace stream entry back to review."""
    return revive_acestream_channel_stream_response(
        session_id=session_id,
        stream_id=stream_id,
        load_store=_ace_channel_sessions_store,
        save_store=_save_ace_channel_sessions_store,
        now_ts=time.time(),
    )


@app.route('/api/acestream-orchestrator/config', methods=['GET'])
def get_acestream_orchestrator_config_endpoint():
    """Get AceStream orchestrator configuration (without exposing API key)."""
    return get_acestream_orchestrator_config_response(
        get_acestream_orchestrator_config=get_acestream_orchestrator_config,
    )


@app.route('/api/acestream-orchestrator/config', methods=['PUT'])
def update_acestream_orchestrator_config_endpoint():
    """Update AceStream orchestrator host, port, and API key."""
    return update_acestream_orchestrator_config_response(
        payload=request.get_json(silent=True),
        get_acestream_orchestrator_config=get_acestream_orchestrator_config,
    )


@app.route('/api/acestream-orchestrator/ready', methods=['GET'])
def check_acestream_orchestrator_ready():
    """Check if the AceStream orchestrator is configured and reachable."""
    return check_acestream_orchestrator_ready_response(
        make_client=_get_acestream_monitoring_client,
        ping_orchestrator_ready=_ping_orchestrator_ready,
    )


@app.route('/api/acestream-monitor-sessions/start', methods=['POST'])
def start_acestream_monitor_session():
    """Start AceStream monitoring session via external orchestrator contract."""
    return start_acestream_monitor_session_response(
        payload=request.get_json(silent=True),
        get_client_or_error=_acestream_client_or_error,
        normalize_content_id=normalize_content_id,
    )


@app.route('/api/acestream-monitor-sessions', methods=['GET'])
def list_acestream_monitor_sessions():
    """List AceStream monitoring sessions with optional playback correlation."""
    return list_acestream_monitor_sessions_response(
        args=request.args,
        get_client_or_error=_acestream_client_or_error,
    )


@app.route('/api/acestream-monitor-sessions/<monitor_id>', methods=['GET'])
def get_acestream_monitor_session(monitor_id):
    """Get one AceStream monitoring session with detailed history."""
    return get_acestream_monitor_session_response(
        monitor_id=monitor_id,
        args=request.args,
        get_client_or_error=_acestream_client_or_error,
    )


@app.route('/api/acestream-monitor-sessions/<monitor_id>', methods=['DELETE'])
def stop_acestream_monitor_session(monitor_id):
    """Stop AceStream monitoring session lifecycle."""
    return stop_acestream_monitor_session_response(
        monitor_id=monitor_id,
        get_client_or_error=_acestream_client_or_error,
    )


@app.route('/api/acestream-monitor-sessions/<monitor_id>/entry', methods=['DELETE'])
def delete_acestream_monitor_entry(monitor_id):
    """Delete AceStream monitoring entry and ensure it is stopped."""
    return delete_acestream_monitor_entry_response(
        monitor_id=monitor_id,
        get_client_or_error=_acestream_client_or_error,
    )


@app.route('/api/acestream-monitor-sessions/parse-m3u', methods=['POST'])
def parse_acestream_m3u():
    """Parse M3U and extract AceStream IDs and names via orchestrator contract."""
    return parse_acestream_m3u_response(
        payload=request.get_json(silent=True),
        get_client_or_error=_acestream_client_or_error,
        parse_m3u_fallback=_parse_m3u_acestream_entries,
        normalize_content_id=normalize_content_id,
    )


@app.route('/api/acestream-monitor-sessions/streams/started', methods=['GET'])
def list_acestream_started_streams():
    """Optional playback correlation source from orchestrator proxy streams endpoint."""
    return list_acestream_started_streams_response(
        get_client_or_error=_acestream_client_or_error,
    )


@app.route('/api/stream-sessions', methods=['GET'])
def get_stream_sessions():
    """Get all stream monitoring sessions or filter by status."""
    return get_stream_sessions_response(
        status=request.args.get('status', '').lower(),
        get_session_manager=get_session_manager,
    )


@app.route('/api/stream-sessions', methods=['POST'])
def create_stream_session():
    """Create a new stream monitoring session."""
    return create_stream_session_response(
        payload=request.get_json(silent=True),
        get_session_manager=get_session_manager,
        get_regex_matcher=get_regex_matcher,
    )


@app.route('/api/stream-sessions/group/start', methods=['POST'])
def create_group_stream_sessions():
    """Create and start monitoring sessions for all channels in a group."""
    return create_group_stream_sessions_response(
        payload=request.get_json(silent=True),
        get_udi_manager=get_udi_manager,
        get_session_manager=get_session_manager,
        get_monitoring_service=get_monitoring_service,
        get_regex_matcher=get_regex_matcher,
    )


@app.route('/api/stream-sessions/<session_id>', methods=['GET'])
def get_stream_session(session_id):
    """Get detailed information about a specific session including all streams."""
    return get_stream_session_response(
        session_id=session_id,
        since_timestamp=request.args.get('since_timestamp', type=float),
        get_session_manager=get_session_manager,
        get_udi_manager=get_udi_manager,
    )


@app.route('/api/stream-sessions/<session_id>/streams/<int:stream_id>/quarantine', methods=['POST'])
def quarantine_stream(session_id, stream_id):
    """Quarantine a stream in a session."""
    return quarantine_stream_response(
        session_id=session_id,
        stream_id=stream_id,
        get_session_manager=get_session_manager,
    )


@app.route('/api/stream-sessions/<session_id>/streams/<int:stream_id>/revive', methods=['POST'])
def revive_stream(session_id, stream_id):
    """Revive a quarantined stream in a session"""
    return revive_stream_response(
        session_id=session_id,
        stream_id=stream_id,
        get_session_manager=get_session_manager,
    )


@app.route('/api/stream-sessions/<session_id>/start', methods=['POST'])
def start_stream_session(session_id):
    """Start monitoring for a session."""
    return start_stream_session_response(
        session_id=session_id,
        get_session_manager=get_session_manager,
        get_monitoring_service=get_monitoring_service,
    )


@app.route('/api/stream-sessions/<session_id>/stop', methods=['POST'])
def stop_stream_session(session_id):
    """Stop monitoring for a session."""
    return stop_stream_session_response(
        session_id=session_id,
        get_session_manager=get_session_manager,
    )


@app.route('/api/stream-sessions/<session_id>', methods=['DELETE'])
def delete_stream_session(session_id):
    """Delete a session."""
    return delete_stream_session_response(
        session_id=session_id,
        get_session_manager=get_session_manager,
    )


@app.route('/api/stream-sessions/batch/stop', methods=['POST'])
def batch_stop_sessions():
    """Stop multiple monitoring sessions."""
    return batch_stop_sessions_response(
        payload=request.get_json(silent=True),
        get_session_manager=get_session_manager,
    )


@app.route('/api/stream-sessions/batch/delete', methods=['POST'])
def batch_delete_sessions():
    """Delete multiple monitoring sessions."""
    return batch_delete_sessions_response(
        payload=request.get_json(silent=True),
        get_session_manager=get_session_manager,
    )


@app.route('/api/stream-sessions/<session_id>/streams/<int:stream_id>/metrics', methods=['GET'])
def get_stream_metrics(session_id, stream_id):
    """Get historical metrics for a stream in a session."""
    return get_stream_metrics_response(
        session_id=session_id,
        stream_id=stream_id,
        since_timestamp=request.args.get('since_timestamp', type=float),
        get_session_manager=get_session_manager,
    )


# Serve screenshots
@app.route('/api/data/screenshots/<filename>')
def serve_screenshot(filename):
    """Serve screenshot files."""
    return serve_screenshot_response(
        filename=filename,
        config_dir=CONFIG_DIR,
    )


@app.route('/api/stream-sessions/<session_id>/alive-screenshots', methods=['GET'])
def get_alive_screenshots(session_id):
    """Get screenshots and info for all alive (non-quarantined) streams in a session."""
    return get_alive_screenshots_response(
        session_id=session_id,
        get_session_manager=get_session_manager,
    )





@app.route('/api/proxy/status', methods=['GET'])
def get_proxy_status():
    """Get current proxy status showing which streams are being played."""
    return get_proxy_status_response(
        get_udi_manager=get_udi_manager,
    )


@app.route('/api/proxy/playing-streams', methods=['GET'])
def get_playing_streams():
    """Get list of stream IDs that are currently being played."""
    return get_playing_streams_response(
        get_udi_manager=get_udi_manager,
    )


@app.route('/api/stream-viewer/<int:stream_id>', methods=['GET'])
def get_stream_viewer_url(stream_id):
    """Get the stream's direct HLS URL for live viewing in browser."""
    return get_stream_viewer_url_response(
        stream_id=stream_id,
        get_udi_manager=get_udi_manager,
    )




@app.route('/api/stream/proxy/<int:stream_id>', methods=['GET'])
def stream_proxy_url(stream_id):
    """Proxy the local UDP stream from FFmpeg out via HTTP using a shared listener."""
    return stream_proxy_url_response(
        stream_id=stream_id,
        udp_proxy_manager=udp_proxy_manager,
    )


@app.route('/api/scheduled-events/<event_id>/create-session', methods=['POST'])
def create_session_from_event(event_id):
    """Create a monitoring session from a scheduled event."""
    return create_session_from_event_response(
        event_id=event_id,
        get_scheduling_service=get_scheduling_service,
    )



# Serve React app for all frontend routes (catch-all - must be last!)


# ==================== Settings API ====================

@app.route('/api/settings/session', methods=['GET', 'POST'])
def handle_session_settings():
    """Get or update session settings (like review duration)."""
    return handle_session_settings_response(
        method=request.method,
        payload=request.get_json(silent=True),
        get_session_manager=get_session_manager,
    )


# Serve React app for all frontend routes (catch-all - must be last!)
@app.route('/<path:path>')
def serve_frontend(path):
    """Serve React frontend files or return index.html for client-side routing."""
    return serve_frontend_response(static_folder=static_folder, path=path)
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
            
        # Force a full UDI Refresh on startup in the background if wizard is complete.
        # Polls Dispatcharr readiness before attempting the refresh so that a
        # simultaneous Docker stack startup does not silently fail while Dispatcharr
        # is still booting.
        try:
            if check_wizard_complete():
                def startup_udi_refresh():
                    """Wait for Dispatcharr to become ready, then do a full UDI refresh."""
                    from apps.core.api_utils import get_udi_manager
                    from apps.udi.fetcher import UDIFetcher

                    POLL_INTERVAL_SECONDS = 10
                    MAX_WAIT_SECONDS = 300  # 5-minute ceiling

                    fetcher = UDIFetcher()
                    elapsed = 0

                    logger.info(
                        f"Startup UDI refresh: polling Dispatcharr every {POLL_INTERVAL_SECONDS}s "
                        f"(max {MAX_WAIT_SECONDS}s)..."
                    )

                    while elapsed < MAX_WAIT_SECONDS:
                        if fetcher.test_connection():
                            logger.info(
                                f"Dispatcharr ready after {elapsed}s — starting UDI refresh..."
                            )
                            break
                        logger.info(
                            f"Dispatcharr not ready yet ({elapsed}s elapsed) — "
                            f"retrying in {POLL_INTERVAL_SECONDS}s..."
                        )
                        time.sleep(POLL_INTERVAL_SECONDS)
                        elapsed += POLL_INTERVAL_SECONDS
                    else:
                        logger.error(
                            f"Dispatcharr did not become ready within {MAX_WAIT_SECONDS}s. "
                            "Skipping startup UDI refresh — use 'Reload UDI' on the dashboard."
                        )
                        return

                    try:
                        udi = get_udi_manager()
                        udi.initialize(force_refresh=True)
                    except Exception as e:
                        logger.error(f"Background startup UDI refresh failed: {e}")

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
