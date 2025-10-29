#!/usr/bin/env python3
"""
Web API Server for StreamFlow for Dispatcharr

Provides REST API endpoints for the React frontend to interact with
the automated stream management system.
"""

import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Dict, Any

from flask import Flask, request, jsonify, send_from_directory, send_file
from flask_cors import CORS

from automated_stream_manager import AutomatedStreamManager, RegexChannelMatcher
from api_utils import fetch_data_from_url, _get_base_url
from stream_checker_service import get_stream_checker_service

# Import croniter for cron expression validation
try:
    from croniter import croniter
    CRONITER_AVAILABLE = True
except ImportError:
    CRONITER_AVAILABLE = False
    logging.warning("croniter not installed - cron expression validation will be disabled")



# Custom logging filter to exclude HTTP-related logs
class HTTPLogFilter(logging.Filter):
    """Filter out HTTP-related log messages."""
    def filter(self, record):
        # Exclude messages containing HTTP request/response indicators
        message = record.getMessage().lower()
        http_indicators = [
            'http request',
            'http response',
            'status code',
            'get /',
            'post /',
            'put /',
            'delete /',
            'patch /',
            '" with',
            '- - [',  # Common HTTP access log format
            'werkzeug',
        ]
        return not any(indicator in message for indicator in http_indicators)

# Setup logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Apply HTTP filter to all handlers
for handler in logging.root.handlers:
    handler.addFilter(HTTPLogFilter())

# Configuration directory - persisted via Docker volume
CONFIG_DIR = Path(os.environ.get('CONFIG_DIR', '/app/data'))

# Initialize Flask app with static file serving
# Note: static_folder set to None to disable Flask's built-in static route
# The catch-all route will handle serving all static files from the React build
static_folder = Path(__file__).parent / 'static'
app = Flask(__name__, static_folder=None)
CORS(app)  # Enable CORS for React frontend

# Global instances
automation_manager = None
regex_matcher = None

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
    """Check if the setup wizard has been completed."""
    try:
        config_file = CONFIG_DIR / 'automation_config.json'
        regex_file = CONFIG_DIR / 'channel_regex_config.json'
        
        # Check if configuration files exist
        if not config_file.exists() or not regex_file.exists():
            return False
        
        # Check if we have patterns configured
        if regex_file.exists():
            matcher = get_regex_matcher()
            patterns = matcher.get_patterns()
            if not patterns.get('patterns'):
                return False
        else:
            return False
        
        # Check if we can connect to Dispatcharr (optional - use cached result)
        # For startup, we'll accept the configuration exists as sufficient
        # The actual connection test will be done by the wizard
        
        return True
    except Exception as e:
        logging.warning(f"Error checking wizard completion status: {e}")
        return False



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

@app.route('/api/automation/status', methods=['GET'])
def get_automation_status():
    """Get current automation status."""
    try:
        manager = get_automation_manager()
        status = manager.get_status()
        return jsonify(status)
    except Exception as e:
        logging.error(f"Error getting automation status: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/automation/start', methods=['POST'])
def start_automation():
    """Start the automation system."""
    try:
        manager = get_automation_manager()
        manager.start_automation()
        return jsonify({"message": "Automation started successfully", "status": "running"})
    except Exception as e:
        logging.error(f"Error starting automation: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/automation/stop', methods=['POST'])
def stop_automation():
    """Stop the automation system."""
    try:
        manager = get_automation_manager()
        manager.stop_automation()
        return jsonify({"message": "Automation stopped successfully", "status": "stopped"})
    except Exception as e:
        logging.error(f"Error stopping automation: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/automation/cycle', methods=['POST'])
def run_automation_cycle():
    """Run one automation cycle manually."""
    try:
        manager = get_automation_manager()
        manager.run_automation_cycle()
        return jsonify({"message": "Automation cycle completed successfully"})
    except Exception as e:
        logging.error(f"Error running automation cycle: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/automation/config', methods=['GET'])
def get_automation_config():
    """Get automation configuration."""
    try:
        manager = get_automation_manager()
        return jsonify(manager.config)
    except Exception as e:
        logging.error(f"Error getting automation config: {e}")
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
        logging.error(f"Error updating automation config: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/channels', methods=['GET'])
def get_channels():
    """Get all channels from Dispatcharr."""
    try:
        base_url = _get_base_url()
        channels = fetch_data_from_url(f"{base_url}/api/channels/channels/")
        
        if channels is None:
            return jsonify({"error": "Failed to fetch channels"}), 500
        
        return jsonify(channels)
    except Exception as e:
        logging.error(f"Error fetching channels: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/channels/groups', methods=['GET'])
def get_channel_groups():
    """Get all channel groups from Dispatcharr."""
    try:
        base_url = _get_base_url()
        groups = fetch_data_from_url(f"{base_url}/api/channels/groups/")
        
        if groups is None:
            return jsonify({"error": "Failed to fetch channel groups"}), 500
        
        return jsonify(groups)
    except Exception as e:
        logging.error(f"Error fetching channel groups: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/channels/logos/<logo_id>', methods=['GET'])
def get_channel_logo(logo_id):
    """Get channel logo from Dispatcharr."""
    try:
        base_url = _get_base_url()
        logo = fetch_data_from_url(f"{base_url}/api/channels/logos/{logo_id}/")
        
        if logo is None:
            return jsonify({"error": "Failed to fetch logo"}), 500
        
        return jsonify(logo)
    except Exception as e:
        logging.error(f"Error fetching logo: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/regex-patterns', methods=['GET'])
def get_regex_patterns():
    """Get all regex patterns for channel matching."""
    try:
        matcher = get_regex_matcher()
        patterns = matcher.get_patterns()
        return jsonify(patterns)
    except Exception as e:
        logging.error(f"Error getting regex patterns: {e}")
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
        matcher.add_channel_pattern(
            data['channel_id'],
            data['name'],
            data['regex'],
            data.get('enabled', True)
        )
        
        return jsonify({"message": "Pattern added/updated successfully"})
    except ValueError as e:
        # Validation errors (e.g., invalid regex) should return 400
        logging.warning(f"Validation error adding regex pattern: {e}")
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        logging.error(f"Error adding regex pattern: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/regex-patterns/<channel_id>', methods=['DELETE'])
def delete_regex_pattern(channel_id):
    """Delete a regex pattern for a channel."""
    try:
        matcher = get_regex_matcher()
        patterns = matcher.get_patterns()
        
        if 'patterns' in patterns and str(channel_id) in patterns['patterns']:
            del patterns['patterns'][str(channel_id)]
            matcher._save_patterns(patterns)
            return jsonify({"message": "Pattern deleted successfully"})
        else:
            return jsonify({"error": "Pattern not found"}), 404
    except Exception as e:
        logging.error(f"Error deleting regex pattern: {e}")
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
        logging.info(f"Imported {pattern_count} regex patterns successfully")
        
        return jsonify({
            "message": f"Successfully imported {pattern_count} patterns",
            "pattern_count": pattern_count
        })
    except Exception as e:
        logging.error(f"Error importing regex patterns: {e}")
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
        search_pattern = re.sub(r' +', r'\\s+', search_pattern)
        
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
        logging.error(f"Error testing regex pattern: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/test-regex-live', methods=['POST'])
def test_regex_pattern_live():
    """Test regex patterns against all available streams to see what would be matched."""
    try:
        from api_utils import get_streams
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
        
        results = []
        
        # Test each pattern against all streams
        for pattern_info in patterns:
            channel_id = pattern_info.get('channel_id', 'unknown')
            channel_name = pattern_info.get('channel_name', 'Unknown Channel')
            regex_patterns = pattern_info.get('regex', [])
            
            if not regex_patterns:
                continue
            
            matched_streams = []
            
            for stream in all_streams:
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
                    search_pattern = pattern if case_sensitive else pattern.lower()
                    
                    # Convert literal spaces in pattern to flexible whitespace regex (\s+)
                    # This allows matching streams with different whitespace characters
                    search_pattern = re.sub(r' +', r'\\s+', search_pattern)
                    
                    try:
                        if re.search(search_pattern, search_name):
                            matched = True
                            matched_pattern = pattern
                            break  # Only need one match
                    except re.error as e:
                        logging.warning(f"Invalid regex pattern '{pattern}': {e}")
                        continue
                
                if matched and len(matched_streams) < max_matches_per_pattern:
                    matched_streams.append({
                        "stream_id": stream_id,
                        "stream_name": stream_name,
                        "matched_pattern": matched_pattern
                    })
            
            results.append({
                "channel_id": channel_id,
                "channel_name": channel_name,
                "patterns": regex_patterns,
                "matched_streams": matched_streams,
                "match_count": len(matched_streams)
            })
        
        return jsonify({
            "results": results,
            "total_streams": len(all_streams),
            "case_sensitive": case_sensitive
        })
        
    except Exception as e:
        logging.error(f"Error testing regex patterns live: {e}")
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
            logging.warning(f"Could not get stream checker changelog: {e}")
        
        # Merge and sort by timestamp (newest first)
        merged_changelog = automation_changelog + stream_checker_changelog
        merged_changelog.sort(key=lambda x: x.get('timestamp', ''), reverse=True)
        
        return jsonify(merged_changelog)
    except Exception as e:
        logging.error(f"Error getting changelog: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/discover-streams', methods=['POST'])
def discover_streams():
    """Trigger stream discovery and assignment."""
    try:
        manager = get_automation_manager()
        assignments = manager.discover_and_assign_streams()
        return jsonify({
            "message": "Stream discovery completed",
            "assignments": assignments,
            "total_assigned": sum(assignments.values())
        })
    except Exception as e:
        logging.error(f"Error discovering streams: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/refresh-playlist', methods=['POST'])
def refresh_playlist():
    """Trigger M3U playlist refresh."""
    try:
        data = request.get_json() or {}
        account_id = data.get('account_id')
        
        manager = get_automation_manager()
        success = manager.refresh_playlists()
        
        if success:
            return jsonify({"message": "Playlist refresh completed successfully"})
        else:
            return jsonify({"error": "Playlist refresh failed"}), 500
    except Exception as e:
        logging.error(f"Error refreshing playlist: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/m3u-accounts', methods=['GET'])
def get_m3u_accounts_endpoint():
    """Get all M3U accounts from Dispatcharr, filtering out 'custom' account if no custom streams exist and non-active accounts."""
    try:
        from api_utils import get_m3u_accounts, has_custom_streams
        accounts = get_m3u_accounts()
        
        if accounts is None:
            return jsonify({"error": "Failed to fetch M3U accounts"}), 500
        
        # Filter out non-active accounts per Dispatcharr API spec
        accounts = [acc for acc in accounts if acc.get('is_active', True)]
        
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
        
        return jsonify(accounts)
    except Exception as e:
        logging.error(f"Error fetching M3U accounts: {e}")
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
            try:
                base_url = _get_base_url()
                if base_url:
                    channels = fetch_data_from_url(f"{base_url}/api/channels/channels/")
                    status["dispatcharr_connection"] = channels is not None
                    status["has_channels"] = bool(channels)
            except:
                pass
        
        status["setup_complete"] = all([
            status["automation_config_exists"],
            status["regex_config_exists"],
            status["has_patterns"],
            status["has_channels"],
            status["dispatcharr_connection"]
        ])
        
        return jsonify(status)
    except Exception as e:
        logging.error(f"Error getting setup wizard status: {e}")
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
        logging.error(f"Error creating sample patterns: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/dispatcharr/config', methods=['GET'])
def get_dispatcharr_config():
    """Get current Dispatcharr configuration (without exposing password)."""
    try:
        config = {
            "base_url": os.getenv("DISPATCHARR_BASE_URL", ""),
            "username": os.getenv("DISPATCHARR_USER", ""),
            # Never return the password for security reasons
            "has_password": bool(os.getenv("DISPATCHARR_PASS"))
        }
        return jsonify(config)
    except Exception as e:
        logging.error(f"Error getting Dispatcharr config: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/dispatcharr/config', methods=['PUT'])
def update_dispatcharr_config():
    """Update Dispatcharr configuration."""
    try:
        data = request.get_json()
        if not data:
            return jsonify({"error": "No configuration data provided"}), 400
        
        from dotenv import set_key
        env_file = Path('.') / '.env'
        
        # Update environment variables
        if 'base_url' in data:
            base_url = data['base_url'].strip()
            if env_file.exists():
                set_key(env_file, "DISPATCHARR_BASE_URL", base_url)
            os.environ["DISPATCHARR_BASE_URL"] = base_url
        
        if 'username' in data:
            username = data['username'].strip()
            if env_file.exists():
                set_key(env_file, "DISPATCHARR_USER", username)
            os.environ["DISPATCHARR_USER"] = username
        
        if 'password' in data:
            password = data['password']
            if env_file.exists():
                set_key(env_file, "DISPATCHARR_PASS", password)
            os.environ["DISPATCHARR_PASS"] = password
        
        # Clear token when credentials change so we re-authenticate
        if env_file.exists():
            set_key(env_file, "DISPATCHARR_TOKEN", "")
        os.environ["DISPATCHARR_TOKEN"] = ""
        
        return jsonify({"message": "Dispatcharr configuration updated successfully"})
    except Exception as e:
        logging.error(f"Error updating Dispatcharr config: {e}")
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
                    })
            else:
                return jsonify({
                    "success": False,
                    "error": "No token received from Dispatcharr"
                })
        except requests.exceptions.Timeout:
            return jsonify({
                "success": False,
                "error": "Connection timeout. Please check the URL and network connectivity."
            })
        except requests.exceptions.ConnectionError:
            return jsonify({
                "success": False,
                "error": "Could not connect to Dispatcharr. Please check the URL."
            })
        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 401:
                return jsonify({
                    "success": False,
                    "error": "Invalid username or password"
                })
            else:
                return jsonify({
                    "success": False,
                    "error": f"HTTP error: {e.response.status_code}"
                })
        except Exception as e:
            return jsonify({
                "success": False,
                "error": f"Connection failed: {str(e)}"
            })
            
    except Exception as e:
        logging.error(f"Error testing Dispatcharr connection: {e}")
        return jsonify({"error": str(e)}), 500

# ===== Stream Checker Endpoints =====

@app.route('/api/stream-checker/status', methods=['GET'])
def get_stream_checker_status():
    """Get current stream checker status."""
    try:
        service = get_stream_checker_service()
        status = service.get_status()
        return jsonify(status)
    except Exception as e:
        logging.error(f"Error getting stream checker status: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/stream-checker/start', methods=['POST'])
def start_stream_checker():
    """Start the stream checker service."""
    try:
        service = get_stream_checker_service()
        service.start()
        return jsonify({"message": "Stream checker started successfully", "status": "running"})
    except Exception as e:
        logging.error(f"Error starting stream checker: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/stream-checker/stop', methods=['POST'])
def stop_stream_checker():
    """Stop the stream checker service."""
    try:
        service = get_stream_checker_service()
        service.stop()
        return jsonify({"message": "Stream checker stopped successfully", "status": "stopped"})
    except Exception as e:
        logging.error(f"Error stopping stream checker: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/stream-checker/queue', methods=['GET'])
def get_stream_checker_queue():
    """Get current queue status."""
    try:
        service = get_stream_checker_service()
        status = service.get_status()
        return jsonify(status.get('queue', {}))
    except Exception as e:
        logging.error(f"Error getting stream checker queue: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/stream-checker/queue/add', methods=['POST'])
def add_to_stream_checker_queue():
    """Add channel(s) to the checking queue."""
    try:
        data = request.get_json()
        if not data:
            return jsonify({"error": "No data provided"}), 400
        
        service = get_stream_checker_service()
        
        # Handle single channel or multiple channels
        if 'channel_id' in data:
            channel_id = data['channel_id']
            priority = data.get('priority', 10)
            success = service.queue_channel(channel_id, priority)
            if success:
                return jsonify({"message": f"Channel {channel_id} queued successfully"})
            else:
                return jsonify({"error": "Failed to queue channel"}), 500
        
        elif 'channel_ids' in data:
            channel_ids = data['channel_ids']
            priority = data.get('priority', 10)
            added = service.queue_channels(channel_ids, priority)
            return jsonify({"message": f"Queued {added} channels successfully", "added": added})
        
        else:
            return jsonify({"error": "Must provide channel_id or channel_ids"}), 400
    
    except Exception as e:
        logging.error(f"Error adding to stream checker queue: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/stream-checker/queue/clear', methods=['POST'])
def clear_stream_checker_queue():
    """Clear the checking queue."""
    try:
        service = get_stream_checker_service()
        service.clear_queue()
        return jsonify({"message": "Queue cleared successfully"})
    except Exception as e:
        logging.error(f"Error clearing stream checker queue: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/stream-checker/config', methods=['GET'])
def get_stream_checker_config():
    """Get stream checker configuration."""
    try:
        service = get_stream_checker_service()
        return jsonify(service.config.config)
    except Exception as e:
        logging.error(f"Error getting stream checker config: {e}")
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
                        logging.error(f"Cron expression validation error: {e}")
                        return jsonify({"error": "Invalid cron expression format"}), 400
                else:
                    logging.warning("croniter not available - cron expression validation skipped")
        
        service = get_stream_checker_service()
        service.update_config(data)
        
        # Auto-start or stop services based on pipeline mode when wizard is complete
        if 'pipeline_mode' in data and check_wizard_complete():
            pipeline_mode = data['pipeline_mode']
            manager = get_automation_manager()
            
            if pipeline_mode == 'disabled':
                # Stop services if pipeline is disabled
                if service.running:
                    service.stop()
                    logging.info("Stream checker service stopped (pipeline disabled)")
                if manager.running:
                    manager.stop_automation()
                    logging.info("Automation service stopped (pipeline disabled)")
            else:
                # Start services if pipeline is active and they're not already running
                if not service.running:
                    service.start()
                    logging.info(f"Stream checker service auto-started after config update (mode: {pipeline_mode})")
                if not manager.running:
                    manager.start_automation()
                    logging.info(f"Automation service auto-started after config update (mode: {pipeline_mode})")
        
        return jsonify({"message": "Configuration updated successfully", "config": service.config.config})
    except Exception as e:
        logging.error(f"Error updating stream checker config: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/stream-checker/progress', methods=['GET'])
def get_stream_checker_progress():
    """Get current checking progress."""
    try:
        service = get_stream_checker_service()
        status = service.get_status()
        return jsonify(status.get('progress', {}))
    except Exception as e:
        logging.error(f"Error getting stream checker progress: {e}")
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
        logging.error(f"Error checking specific channel: {e}")
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
        logging.error(f"Error marking channels updated: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/stream-checker/queue-all', methods=['POST'])
def queue_all_channels():
    """Queue all channels for checking (manual trigger for full check)."""
    try:
        service = get_stream_checker_service()
        
        # Fetch all channels
        from api_utils import fetch_data_from_url, _get_base_url
        base_url = _get_base_url()
        channels_data = fetch_data_from_url(f"{base_url}/api/channels/channels/")
        
        if not channels_data:
            return jsonify({"error": "Could not fetch channels"}), 500
        
        if isinstance(channels_data, dict) and 'results' in channels_data:
            channels = channels_data['results']
        else:
            channels = channels_data
        
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
        logging.error(f"Error queueing all channels: {e}")
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
        logging.error(f"Error triggering global action: {e}")
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
    parser.add_argument('--host', default='0.0.0.0', help='Host to bind to')
    parser.add_argument('--port', type=int, default=5000, help='Port to bind to')
    parser.add_argument('--debug', action='store_true', help='Enable debug mode')
    
    args = parser.parse_args()
    
    logging.info(f"Starting StreamFlow for Dispatcharr Web API on {args.host}:{args.port}")
    
    # Auto-start stream checker service if enabled and pipeline mode is not disabled AND wizard is complete
    try:
        # Check if wizard has been completed
        if not check_wizard_complete():
            logging.info("Stream checker service will not start - setup wizard has not been completed")
        else:
            service = get_stream_checker_service()
            pipeline_mode = service.config.get('pipeline_mode', 'pipeline_1_5')
            
            if pipeline_mode == 'disabled':
                logging.info("Stream checker service is disabled via pipeline mode")
            elif service.config.get('enabled', True):
                service.start()
                logging.info(f"Stream checker service auto-started (mode: {pipeline_mode})")
            else:
                logging.info("Stream checker service is disabled in configuration")
    except Exception as e:
        logging.error(f"Failed to auto-start stream checker service: {e}")
    
    # Auto-start automation service if pipeline mode is not disabled AND wizard is complete
    # When any pipeline other than disabled is selected, automation should auto-start
    try:
        # Check if wizard has been completed
        if not check_wizard_complete():
            logging.info("Automation service will not start - setup wizard has not been completed")
        else:
            manager = get_automation_manager()
            service = get_stream_checker_service()
            pipeline_mode = service.config.get('pipeline_mode', 'pipeline_1_5')
            
            if pipeline_mode == 'disabled':
                logging.info("Automation service is disabled via pipeline mode")
            else:
                # Auto-start automation for any active pipeline
                manager.start_automation()
                logging.info(f"Automation service auto-started (mode: {pipeline_mode})")
    except Exception as e:
        logging.error(f"Failed to auto-start automation service: {e}")
    
    app.run(host=args.host, port=args.port, debug=args.debug)