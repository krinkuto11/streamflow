#!/usr/bin/env python3
"""
Unit test to verify that automation cycle respects stream checking mode.

This test verifies that when stream checking mode is active:
1. The automation cycle is queued/deferred instead of lost
2. No UDI refresh calls are made
3. No API requests are triggered
"""

import unittest
import tempfile
import json
import threading
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock, call
import sys
import os

# Add backend to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from apps.automation.automated_stream_manager import AutomatedStreamManager


class TestAutomationStreamCheckingMode(unittest.TestCase):
    """Test that automation cycle respects stream checking mode."""
    
    def setUp(self):
        """Set up test fixtures."""
        # Create temporary directory for test files
        self.temp_dir = tempfile.mkdtemp()
        self.config_file = Path(self.temp_dir) / 'automation_config.json'
        self.changelog_file = Path(self.temp_dir) / 'changelog.json'
        self.regex_config_file = Path(self.temp_dir) / 'channel_regex_config.json'
        
    def tearDown(self):
        """Clean up test fixtures."""
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)
    
    def test_automation_queues_when_stream_checking_mode_active(self):
        """Test that automation cycle queues when stream_checking_mode is True."""
        with patch('automated_stream_manager.CONFIG_DIR', Path(self.temp_dir)):
            manager = AutomatedStreamManager(config_file=self.config_file)
            
            # Mock stream checker service to return stream_checking_mode = True
            mock_stream_checker = Mock()
            mock_status = {
                'stream_checking_mode': True,
                'checking': False,
                'queue': {'queue_size': 1}  # One channel in queue
            }
            mock_stream_checker.get_status.return_value = mock_status
            
            # Mock the import and service getter
            with patch('stream_checker_service.get_stream_checker_service', return_value=mock_stream_checker):
                # Mock refresh_playlists to track if it's called
                manager.refresh_playlists = Mock()
                
                # Set last_playlist_update to None to ensure it would normally run
                manager.last_playlist_update = None
                
                # Run automation cycle
                manager.run_automation_cycle()
                
                # Verify that refresh_playlists was NOT called
                manager.refresh_playlists.assert_not_called()
                
                # Verify that get_status was called to check stream_checking_mode
                mock_stream_checker.get_status.assert_called_once()

                run_status = manager.get_status()["run_status"]
                self.assertEqual(run_status["state"], "queued")
                self.assertEqual(run_status["stage"], "queued")
                self.assertIn("queued", run_status["message"])
                self.assertIsNone(run_status["completed_at"])

    def test_forced_automation_requeues_when_stream_checking_mode_active(self):
        """Test that forced full-run intent is preserved while checker is active."""
        with patch('automated_stream_manager.CONFIG_DIR', Path(self.temp_dir)):
            manager = AutomatedStreamManager(config_file=self.config_file)

            mock_stream_checker = Mock()
            mock_stream_checker.get_status.return_value = {
                'stream_checking_mode': True,
                'checking': False,
                'queue': {'queue_size': 0},
            }

            with patch('stream_checker_service.get_stream_checker_service', return_value=mock_stream_checker):
                manager.refresh_playlists = Mock()

                manager.run_automation_cycle(forced=True, forced_period_id="1")

                manager.refresh_playlists.assert_not_called()
                mock_stream_checker.get_status.assert_called_once()
                self.assertTrue(manager.force_next_run)
                self.assertEqual(manager.forced_period_id, "1")

                run_status = manager.get_status()["run_status"]
                self.assertEqual(run_status["state"], "queued")
                self.assertEqual(run_status["forced"], True)
                self.assertEqual(run_status["forced_period_id"], "1")
                self.assertIsNone(run_status["completed_at"])

    def test_forced_automation_requeues_when_reservation_loses_late_race(self):
        with patch('automated_stream_manager.CONFIG_DIR', Path(self.temp_dir)):
            manager = AutomatedStreamManager(config_file=self.config_file)
            mock_stream_checker = Mock()
            mock_stream_checker.get_status.return_value = {
                'stream_checking_mode': False,
                'checking': False,
                'queue': {'queue_size': 0},
            }
            mock_stream_checker.begin_automation_cycle_operation.return_value = False

            with patch(
                'stream_checker_service.get_stream_checker_service',
                return_value=mock_stream_checker,
            ):
                manager.refresh_playlists = Mock()
                manager.run_automation_cycle(forced=True, forced_period_id='period-1')

            mock_stream_checker.begin_automation_cycle_operation.assert_called_once_with()
            mock_stream_checker.end_automation_cycle_operation.assert_not_called()
            manager.refresh_playlists.assert_not_called()
            self.assertTrue(manager.force_next_run)
            self.assertEqual(manager.forced_period_id, 'period-1')
            self.assertEqual(manager.get_status()['run_status']['state'], 'queued')

    def test_background_scheduler_resumes_same_queued_run_when_checker_clears(self):
        """A checker clear must reconcile the exact queued run without id churn."""
        with patch('automated_stream_manager.CONFIG_DIR', Path(self.temp_dir)):
            manager = AutomatedStreamManager(config_file=self.config_file)
            manager.automation_running = True
            manager.last_playlist_update = None
            manager.refresh_playlists = Mock(return_value=True)
            manager.discover_and_assign_streams = Mock(return_value={})

            observed_queued = []
            status_calls = 0

            def checker_status():
                nonlocal status_calls
                status_calls += 1
                if status_calls == 1:
                    return {
                        'stream_checking_mode': True,
                        'checking': True,
                        'queue': {'queue_size': 1},
                    }
                observed_queued.append(manager.get_status()['run_status'])
                return {
                    'stream_checking_mode': False,
                    'checking': False,
                    'queue': {'queue_size': 0},
                }

            mock_stream_checker = Mock()
            mock_stream_checker.get_status.side_effect = checker_status
            mock_stream_checker.begin_automation_cycle_operation.return_value = True

            with patch(
                'stream_checker_service.get_stream_checker_service',
                return_value=mock_stream_checker,
            ), patch('automated_stream_manager.time.sleep') as mocked_sleep:
                manager.run_automation_cycle()

            self.assertEqual(status_calls, 2)
            mocked_sleep.assert_called_once_with(0.25)
            self.assertEqual(len(observed_queued), 1)
            queued = observed_queued[0]
            terminal = manager.get_status()['run_status']
            self.assertEqual(queued['state'], 'queued')
            self.assertFalse(queued['active'])
            self.assertIsNone(queued['completed_at'])
            self.assertEqual(terminal['run_id'], queued['run_id'])
            self.assertEqual(terminal['state'], 'completed')
            mock_stream_checker.begin_automation_cycle_operation.assert_called_once_with()
            mock_stream_checker.end_automation_cycle_operation.assert_called_once_with()
            manager.refresh_playlists.assert_called_once_with()

    def test_background_scheduler_stop_terminalizes_queued_checker_wait(self):
        """Stopping the service cannot leave a stale queued run behind."""
        with patch('automated_stream_manager.CONFIG_DIR', Path(self.temp_dir)):
            manager = AutomatedStreamManager(config_file=self.config_file)
            manager.automation_running = True
            manager.refresh_playlists = Mock()

            mock_stream_checker = Mock()

            def checker_status():
                manager.automation_running = False
                return {
                    'stream_checking_mode': True,
                    'checking': True,
                    'queue': {'queue_size': 1},
                }

            mock_stream_checker.get_status.side_effect = checker_status

            with patch(
                'stream_checker_service.get_stream_checker_service',
                return_value=mock_stream_checker,
            ):
                manager.run_automation_cycle(forced=True, forced_period_id='period-1')

            terminal = manager.get_status()['run_status']
            self.assertEqual(terminal['state'], 'aborted')
            self.assertEqual(terminal['stage'], 'aborted')
            self.assertIsNotNone(terminal['completed_at'])
            self.assertIn('stopped while waiting', terminal['message'])
            self.assertTrue(manager.force_next_run)
            self.assertEqual(manager.forced_period_id, 'period-1')
            manager.refresh_playlists.assert_not_called()

    def test_preserve_forced_run_intent_keeps_period_and_full_run_identity(self):
        manager = AutomatedStreamManager.__new__(AutomatedStreamManager)
        manager._trigger_lock = threading.Lock()
        manager.force_next_run = False
        manager.forced_period_id = None

        self.assertTrue(
            manager._preserve_forced_run_intent(
                forced=True,
                forced_period_id="period-1",
            )
        )
        self.assertTrue(manager.force_next_run)
        self.assertEqual(manager.forced_period_id, "period-1")

        # A trigger accepted after the deferred cycle was consumed owns the
        # pending slot and must not be replaced by the older retry intent.
        manager.force_next_run = False
        manager.forced_period_id = "newer-period"
        self.assertTrue(
            manager._preserve_forced_run_intent(
                forced=True,
                forced_period_id=None,
            )
        )
        self.assertFalse(manager.force_next_run)
        self.assertEqual(manager.forced_period_id, "newer-period")

        manager.force_next_run = False
        manager.forced_period_id = None
        self.assertFalse(
            manager._preserve_forced_run_intent(
                forced=False,
                forced_period_id=None,
            )
        )
        self.assertFalse(manager.force_next_run)
        self.assertIsNone(manager.forced_period_id)

        # A period-only manual trigger remains a real pending intent.
        self.assertTrue(
            manager._preserve_forced_run_intent(
                forced=False,
                forced_period_id="period-only",
            )
        )
        self.assertFalse(manager.force_next_run)
        self.assertEqual(manager.forced_period_id, "period-only")
    
    def test_automation_runs_when_stream_checking_mode_inactive(self):
        """Test that automation cycle runs when stream_checking_mode is False."""
        with patch('automated_stream_manager.CONFIG_DIR', Path(self.temp_dir)):
            manager = AutomatedStreamManager(config_file=self.config_file)
            
            # Mock stream checker service to return stream_checking_mode = False
            mock_stream_checker = Mock()
            mock_status = {
                'stream_checking_mode': False,
                'checking': False,
                'queue': {'queue_size': 0}
            }
            mock_stream_checker.get_status.return_value = mock_status
            mock_stream_checker.begin_automation_cycle_operation.return_value = True
            
            # Mock the import and service getter
            with patch('stream_checker_service.get_stream_checker_service', return_value=mock_stream_checker):
                # Mock refresh_playlists to track if it's called
                manager.refresh_playlists = Mock(return_value=True)
                manager.discover_and_assign_streams = Mock(return_value={})
                
                # Set last_playlist_update to None to ensure it would run
                manager.last_playlist_update = None
                
                # Run automation cycle
                manager.run_automation_cycle()
                
                # Verify that refresh_playlists WAS called
                manager.refresh_playlists.assert_called_once()
                
                # Verify that get_status was called to check stream_checking_mode
                mock_stream_checker.get_status.assert_called_once()
                mock_stream_checker.begin_automation_cycle_operation.assert_called_once_with()
                mock_stream_checker.end_automation_cycle_operation.assert_called_once_with()
    
    def test_automation_defers_without_running_logs(self):
        """Test that automation cycle defers without logging a completed run."""
        with patch('automated_stream_manager.CONFIG_DIR', Path(self.temp_dir)):
            manager = AutomatedStreamManager(config_file=self.config_file)
            
            # Mock stream checker service
            mock_stream_checker = Mock()
            mock_status = {
                'stream_checking_mode': True,
                'checking': False,
                'queue': {'queue_size': 0}
            }
            mock_stream_checker.get_status.return_value = mock_status
            
            # Mock logger to track logging calls
            with patch('automated_stream_manager.logger') as mock_logger:
                with patch('stream_checker_service.get_stream_checker_service', return_value=mock_stream_checker):
                    manager.refresh_playlists = Mock()
                    manager.last_playlist_update = None
                    
                    # Run automation cycle
                    manager.run_automation_cycle()
                    
                    # Verify no INFO or DEBUG logs about "Running" or "completed"
                    # Only debug log should be if there's an error checking status
                    for call_args in mock_logger.debug.call_args_list:
                        args = call_args[0]
                        if args:
                            message = args[0]
                            # Ensure we're not logging "Running automation cycle..." or similar
                            self.assertNotIn("Running automation cycle", message)
                            self.assertNotIn("completed", message.lower())
                    
                    for call_args in mock_logger.info.call_args_list:
                        args = call_args[0]
                        if args:
                            message = args[0]
                            # Ensure we're not logging about starting or completing cycle
                            self.assertNotIn("Starting automation cycle", message)
                            self.assertNotIn("Automation cycle completed", message)
    
    def test_status_check_handles_exception_gracefully(self):
        """Automation fails closed when shared checker ownership is unavailable."""
        with patch('automated_stream_manager.CONFIG_DIR', Path(self.temp_dir)):
            manager = AutomatedStreamManager(config_file=self.config_file)
            
            # Mock stream checker service to raise an exception
            def raise_exception():
                raise Exception("Stream checker not available")
            
            with patch('stream_checker_service.get_stream_checker_service', side_effect=raise_exception):
                # Mock refresh methods
                manager.refresh_playlists = Mock(return_value=True)
                manager.discover_and_assign_streams = Mock(return_value={})
                manager.last_playlist_update = None
                
                # Run automation cycle - it must queue rather than overlap an
                # unobservable checker operation.
                manager.run_automation_cycle()

                manager.refresh_playlists.assert_not_called()
                self.assertEqual(manager.get_status()['run_status']['state'], 'queued')


if __name__ == '__main__':
    # Run tests with verbose output
    unittest.main(verbosity=2)
