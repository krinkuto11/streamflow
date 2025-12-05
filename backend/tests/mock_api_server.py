#!/usr/bin/env python3
"""
Mock API server to demonstrate stream checking mode behavior.
Run this to manually test the UI behavior.
"""

from flask import Flask, jsonify
from flask_cors import CORS
import time
import threading

app = Flask(__name__)
CORS(app)

# Simulate different states
state = {
    'mode': 'idle',  # idle, checking, global_action, queue
    'checking': False,
    'global_action_in_progress': False,
    'queue_size': 0,
    'in_progress': 0,
    'current_channel': None
}

@app.route('/api/stream-checker/status', methods=['GET'])
def get_status():
    """Return mock status based on current state."""
    
    # Calculate stream_checking_mode
    stream_checking_mode = (
        state['global_action_in_progress'] or
        state['checking'] or
        state['queue_size'] > 0 or
        state['in_progress'] > 0
    )
    
    return jsonify({
        'running': True,
        'checking': state['checking'],
        'global_action_in_progress': state['global_action_in_progress'],
        'stream_checking_mode': stream_checking_mode,
        'enabled': True,
        'queue': {
            'queue_size': state['queue_size'],
            'queued': 0,
            'in_progress': state['in_progress'],
            'completed': 0,
            'failed': 0,
            'current_channel': state['current_channel'],
            'total_queued': 0,
            'total_completed': 0,
            'total_failed': 0
        },
        'progress': {
            'channel_id': state['current_channel'],
            'channel_name': 'Test Channel' if state['current_channel'] else None,
            'current_stream': 1,
            'total_streams': 10,
            'percentage': 10.0,
            'current_stream_name': 'Stream 1',
            'status': 'checking',
            'step': 'Analyzing stream quality',
            'step_detail': 'Checking bitrate, resolution, codec (1/10)',
            'timestamp': time.strftime('%Y-%m-%dT%H:%M:%S')
        } if state['checking'] or state['in_progress'] > 0 else None,
        'last_global_check': '2025-12-04T10:00:00',
        'config': {
            'pipeline_mode': 'pipeline_1_5',
            'check_interval': 300,
            'global_check_schedule': {
                'enabled': True,
                'cron_expression': '0 3 * * *',
                'frequency': 'daily',
                'hour': 3,
                'minute': 0
            },
            'queue_settings': {
                'max_size': 1000,
                'check_on_update': True,
                'max_channels_per_run': 50
            }
        }
    })

@app.route('/api/automation/status', methods=['GET'])
def get_automation_status():
    """Return mock automation status."""
    return jsonify({
        'running': True,
        'last_playlist_update': '2025-12-04T10:00:00',
        'next_playlist_update': '2025-12-04T10:05:00',
        'config': {
            'playlist_update_interval_minutes': 5,
            'enabled_m3u_accounts': [],
            'enabled_features': {
                'playlist_update': True,
                'stream_discovery': True
            }
        },
        'recent_changelog': []
    })

@app.route('/api/set-mode/<mode>', methods=['POST'])
def set_mode(mode):
    """Set the current testing mode."""
    if mode == 'idle':
        state['checking'] = False
        state['global_action_in_progress'] = False
        state['queue_size'] = 0
        state['in_progress'] = 0
        state['current_channel'] = None
        state['mode'] = 'idle'
    elif mode == 'checking':
        state['checking'] = True
        state['global_action_in_progress'] = False
        state['queue_size'] = 0
        state['in_progress'] = 1
        state['current_channel'] = 1
        state['mode'] = 'checking'
    elif mode == 'global_action':
        state['checking'] = False
        state['global_action_in_progress'] = True
        state['queue_size'] = 10
        state['in_progress'] = 0
        state['current_channel'] = None
        state['mode'] = 'global_action'
    elif mode == 'queue':
        state['checking'] = False
        state['global_action_in_progress'] = False
        state['queue_size'] = 5
        state['in_progress'] = 0
        state['current_channel'] = None
        state['mode'] = 'queue'
    
    stream_checking_mode_value = (
        state['global_action_in_progress'] or 
        state['checking'] or 
        state['queue_size'] > 0 or 
        state['in_progress'] > 0
    )
    
    return jsonify({
        'mode': state['mode'], 
        'stream_checking_mode': stream_checking_mode_value
    })

if __name__ == '__main__':
    print("""
╔══════════════════════════════════════════════════════════════════╗
║            Stream Checking Mode - Mock API Server               ║
╚══════════════════════════════════════════════════════════════════╝

This mock server simulates different stream checking states.

API Endpoints:
  GET  /api/stream-checker/status   - Get current status
  GET  /api/automation/status        - Get automation status
  POST /api/set-mode/<mode>          - Set testing mode

Available modes:
  idle          - No checking active (buttons enabled)
  checking      - Individual channel check (buttons disabled)
  global_action - Global action in progress (buttons disabled)
  queue         - Channels in queue (buttons disabled)

Test the modes with curl:
  curl -X POST http://localhost:5000/api/set-mode/idle
  curl -X POST http://localhost:5000/api/set-mode/checking
  curl -X POST http://localhost:5000/api/set-mode/global_action
  curl -X POST http://localhost:5000/api/set-mode/queue

Open your browser to the frontend and observe how buttons are disabled
in different modes!

Starting server on http://localhost:5000
    """)
    
    app.run(host='0.0.0.0', port=5000, debug=True)
