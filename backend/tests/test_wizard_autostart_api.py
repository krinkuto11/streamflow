#!/usr/bin/env python3
"""
Integration tests for automation service auto-start via API endpoint.

This module tests:
- API endpoint auto-starts services when automation controls are enabled
- Services start only when wizard is complete
- Services don't start when all automation controls are disabled
"""

import unittest
import tempfile
import json
from pathlib import Path
from unittest.mock import patch, MagicMock
import sys
import os

# Add backend to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import web_api
from web_api import (
    app, get_automation_manager, get_stream_checker_service,
    stop_epg_refresh_processor, stop_scheduled_event_processor
)


class TestWizardAutostartAPI(unittest.TestCase):
    """Test automation service auto-start via API endpoint."""
    
    def setUp(self):
        """Set up test fixtures."""
        self.temp_dir = tempfile.mkdtemp()
        self.app = app.test_client()
        self.app.testing = True
    
    def tearDown(self):
        """Clean up test fixtures."""
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)
        
        # Stop any running services
        try:
            manager = get_automation_manager()
            if manager.running:
                manager.stop_automation()
        except Exception:
            pass
        
        try:
            service = get_stream_checker_service()
            if service.running:
                service.stop()
        except Exception:
            pass
        
        try:
            stop_epg_refresh_processor()
        except Exception:
            pass
        
        try:
            stop_scheduled_event_processor()
        except Exception:
            pass
    
    def _create_complete_wizard_config(self):
        """Helper to create a complete wizard configuration."""
        config_file = Path(self.temp_dir) / 'automation_config.json'
        regex_file = Path(self.temp_dir) / 'channel_regex_config.json'
        stream_checker_file = Path(self.temp_dir) / 'stream_checker_config.json'
        
        config_file.write_text(json.dumps({
            "playlist_update_interval_minutes": 5,
            "autostart_automation": False
        }))
        
        regex_file.write_text(json.dumps({
            "patterns": {
                "1": {
                    "name": "Test Channel",
                    "regex": [".*Test.*"],
                    "enabled": True
                }
            },
            "global_settings": {
                "case_sensitive": False
            }
        }))
        
        stream_checker_file.write_text(json.dumps({
            "automation_controls": {
                "auto_m3u_updates": False,
                "auto_stream_matching": False,
                "auto_quality_checking": False,
                "scheduled_global_action": False
            },
            "enabled": True
        }))
    
    def test_endpoint_starts_services_when_wizard_complete(self):
        """Test that API endpoint auto-starts services when wizard is complete."""
        with patch('web_api.CONFIG_DIR', Path(self.temp_dir)):
            with patch('automated_stream_manager.CONFIG_DIR', Path(self.temp_dir)):
                with patch('stream_checker_service.CONFIG_DIR', Path(self.temp_dir)):
                    # Setup complete wizard configuration
                    self._create_complete_wizard_config()
                    
                    # Update stream checker config via API with enabled automation controls
                    response = self.app.put(
                        '/api/stream-checker/config',
                        data=json.dumps({
                            'automation_controls': {
                                'auto_m3u_updates': True,
                                'auto_stream_matching': True,
                                'auto_quality_checking': True,
                                'scheduled_global_action': False,
                            }
                        }),
                        content_type='application/json'
                    )
                    
                    self.assertEqual(response.status_code, 200)
                    
                    # Verify services are running
                    service = get_stream_checker_service()
                    manager = get_automation_manager()
                    
                    self.assertTrue(service.running, "Stream checker service should be running")
                    self.assertTrue(manager.running, "Automation service should be running")
                    
                    # Verify background processors are running
                    import web_api
                    self.assertIsNotNone(web_api.epg_refresh_thread, "EPG refresh thread should exist")
                    self.assertTrue(web_api.epg_refresh_thread.is_alive(), "EPG refresh processor should be running")
                    self.assertIsNotNone(web_api.scheduled_event_processor_thread, "Scheduled event processor thread should exist")
                    self.assertTrue(web_api.scheduled_event_processor_thread.is_alive(), "Scheduled event processor should be running")
                    
                    # Cleanup
                    service.stop()
                    manager.stop_automation()
                    stop_epg_refresh_processor()
                    stop_scheduled_event_processor()
    
    def test_endpoint_doesnt_start_when_wizard_incomplete(self):
        """Test that API endpoint doesn't auto-start when wizard is incomplete."""
        with patch('web_api.CONFIG_DIR', Path(self.temp_dir)):
            with patch('automated_stream_manager.CONFIG_DIR', Path(self.temp_dir)):
                with patch('stream_checker_service.CONFIG_DIR', Path(self.temp_dir)):
                    # Don't create complete wizard config (missing patterns)
                    config_file = Path(self.temp_dir) / 'automation_config.json'
                    config_file.write_text(json.dumps({
                        "playlist_update_interval_minutes": 5
                    }))
                    
                    # Update stream checker config via API
                    response = self.app.put(
                        '/api/stream-checker/config',
                        data=json.dumps({
                            'automation_controls': {
                                'auto_m3u_updates': True,
                                'auto_stream_matching': True,
                                'auto_quality_checking': True,
                                'scheduled_global_action': False,
                            }
                        }),
                        content_type='application/json'
                    )
                    
                    self.assertEqual(response.status_code, 200)
                    
                    # Verify services are NOT running (wizard incomplete)
                    service = get_stream_checker_service()
                    manager = get_automation_manager()
                    
                    self.assertFalse(service.running, "Stream checker service should not be running")
                    self.assertFalse(manager.running, "Automation service should not be running")
    
    def test_endpoint_doesnt_start_when_all_automation_disabled(self):
        """Test that API endpoint doesn't auto-start when all automation controls are disabled."""
        with patch('web_api.CONFIG_DIR', Path(self.temp_dir)):
            with patch('automated_stream_manager.CONFIG_DIR', Path(self.temp_dir)):
                with patch('stream_checker_service.CONFIG_DIR', Path(self.temp_dir)):
                    # Setup complete wizard configuration
                    self._create_complete_wizard_config()
                    
                    # Update stream checker config via API with all automation disabled
                    response = self.app.put(
                        '/api/stream-checker/config',
                        data=json.dumps({
                            'automation_controls': {
                                'auto_m3u_updates': False,
                                'auto_stream_matching': False,
                                'auto_quality_checking': False,
                                'scheduled_global_action': False,
                            }
                        }),
                        content_type='application/json'
                    )
                    
                    self.assertEqual(response.status_code, 200)
                    
                    # Verify services are NOT running (pipeline disabled)
                    service = get_stream_checker_service()
                    manager = get_automation_manager()
                    
                    self.assertFalse(service.running, "Stream checker service should not be running")
                    self.assertFalse(manager.running, "Automation service should not be running")
    
    def test_endpoint_stops_services_when_switching_to_all_automation_disabled(self):
        """Test that API endpoint stops services when all automation controls are disabled."""
        with patch('web_api.CONFIG_DIR', Path(self.temp_dir)):
            with patch('automated_stream_manager.CONFIG_DIR', Path(self.temp_dir)):
                with patch('stream_checker_service.CONFIG_DIR', Path(self.temp_dir)):
                    # Setup complete wizard configuration
                    self._create_complete_wizard_config()
                    
                    # First, start services with active automation controls
                    response = self.app.put(
                        '/api/stream-checker/config',
                        data=json.dumps({
                            'automation_controls': {
                                'auto_m3u_updates': True,
                                'auto_stream_matching': True,
                                'auto_quality_checking': True,
                                'scheduled_global_action': False,
                            }
                        }),
                        content_type='application/json'
                    )
                    self.assertEqual(response.status_code, 200)
                    
                    # Verify services are running
                    service = get_stream_checker_service()
                    manager = get_automation_manager()
                    self.assertTrue(service.running, "Stream checker service should be running")
                    self.assertTrue(manager.running, "Automation service should be running")
                    
                    # Now disable all automation controls
                    response = self.app.put(
                        '/api/stream-checker/config',
                        data=json.dumps({
                            'automation_controls': {
                                'auto_m3u_updates': False,
                                'auto_stream_matching': False,
                                'auto_quality_checking': False,
                                'scheduled_global_action': False,
                            }
                        }),
                        content_type='application/json'
                    )
                    self.assertEqual(response.status_code, 200)
                    
                    # Verify services have been stopped
                    self.assertFalse(service.running, "Stream checker service should be stopped")
                    self.assertFalse(manager.running, "Automation service should be stopped")
                    
                    # Verify background processors have been stopped
                    import web_api
                    # Thread might still be alive briefly during shutdown, so check if they're not running or dead
                    if web_api.epg_refresh_thread:
                        self.assertFalse(web_api.epg_refresh_running, "EPG refresh processor should not be marked as running")
                    if web_api.scheduled_event_processor_thread:
                        self.assertFalse(web_api.scheduled_event_processor_running, "Scheduled event processor should not be marked as running")


if __name__ == '__main__':
    unittest.main()
