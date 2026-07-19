#!/usr/bin/env python3
"""
Core unit tests for Stream Checker service functionality.

This module consolidates tests for:
- Stream stats handling and default values
- Progress tracking and variable initialization
"""

import unittest
import tempfile
import json
import threading
import concurrent.futures
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock
import sys
import os

# Add backend to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from apps.stream.stream_checker_service import StreamCheckerService
from apps.stream.stream_checker_components import StreamCheckerProgress


class TestStreamStatsHandling(unittest.TestCase):
    """Test handling of stream_stats with various data formats."""
    
    def test_none_stream_stats_handling(self):
        """Test that None stream_stats is handled properly."""
        # Simulate the logic from stream_checker_service.py
        stream_data = {'stream_stats': None}
        
        stream_stats = stream_data.get('stream_stats', {})
        # Handle None case explicitly
        if stream_stats is None:
            stream_stats = {}
        if isinstance(stream_stats, str):
            try:
                stream_stats = json.loads(stream_stats)
                # Handle case where JSON string is "null"
                if stream_stats is None:
                    stream_stats = {}
            except json.JSONDecodeError:
                stream_stats = {}
        
        # Should not raise AttributeError
        resolution = stream_stats.get('resolution', '0x0')
        fps = stream_stats.get('source_fps', 0)
        bitrate = stream_stats.get('ffmpeg_output_bitrate', 0)
        
        self.assertEqual(resolution, '0x0')
        self.assertEqual(fps, 0)
        self.assertEqual(bitrate, 0)
    
    def test_empty_stream_stats_defaults(self):
        """Test that empty stream_stats uses correct defaults."""
        stream_data = {'stream_stats': {}}
        
        stream_stats = stream_data.get('stream_stats', {})
        if stream_stats is None:
            stream_stats = {}
        
        resolution = stream_stats.get('resolution', '0x0')
        fps = stream_stats.get('source_fps', 0)
        bitrate = stream_stats.get('ffmpeg_output_bitrate', 0)
        
        self.assertEqual(resolution, '0x0', "Resolution should default to '0x0'")
        self.assertEqual(fps, 0, "FPS should default to 0")
        self.assertEqual(bitrate, 0, "Bitrate should default to 0")
    
    def test_json_string_null_handling(self):
        """Test that JSON string 'null' is handled properly."""
        stream_data = {'stream_stats': 'null'}
        
        stream_stats = stream_data.get('stream_stats', {})
        if stream_stats is None:
            stream_stats = {}
        if isinstance(stream_stats, str):
            try:
                stream_stats = json.loads(stream_stats)
                # Handle case where JSON string is "null"
                if stream_stats is None:
                    stream_stats = {}
            except json.JSONDecodeError:
                stream_stats = {}
        
        # Should not raise AttributeError
        resolution = stream_stats.get('resolution', '0x0')
        fps = stream_stats.get('source_fps', 0)
        bitrate = stream_stats.get('ffmpeg_output_bitrate', 0)
        
        self.assertEqual(resolution, '0x0')
        self.assertEqual(fps, 0)
        self.assertEqual(bitrate, 0)
    
    def test_json_string_invalid_handling(self):
        """Test that invalid JSON string is handled properly."""
        stream_data = {'stream_stats': 'invalid json'}
        
        stream_stats = stream_data.get('stream_stats', {})
        if stream_stats is None:
            stream_stats = {}
        if isinstance(stream_stats, str):
            try:
                stream_stats = json.loads(stream_stats)
                if stream_stats is None:
                    stream_stats = {}
            except json.JSONDecodeError:
                stream_stats = {}
        
        # Should not raise AttributeError
        resolution = stream_stats.get('resolution', '0x0')
        fps = stream_stats.get('source_fps', 0)
        bitrate = stream_stats.get('ffmpeg_output_bitrate', 0)
        
        self.assertEqual(resolution, '0x0')
        self.assertEqual(fps, 0)
        self.assertEqual(bitrate, 0)
    
    def test_valid_stream_stats(self):
        """Test that valid stream_stats are used correctly."""
        stream_data = {
            'stream_stats': {
                'resolution': '1920x1080',
                'source_fps': 60,
                'ffmpeg_output_bitrate': 5000
            }
        }
        
        stream_stats = stream_data.get('stream_stats', {})
        if stream_stats is None:
            stream_stats = {}
        
        resolution = stream_stats.get('resolution', '0x0')
        fps = stream_stats.get('source_fps', 0)
        bitrate = stream_stats.get('ffmpeg_output_bitrate', 0)
        
        self.assertEqual(resolution, '1920x1080')
        self.assertEqual(fps, 60)
        self.assertEqual(bitrate, 5000)

    def test_score_fallback_handles_none_video_codec(self):
        """Score calculation should tolerate persisted stats with video_codec=None."""
        service = StreamCheckerService.__new__(StreamCheckerService)
        service._is_stream_dead = Mock(return_value=(False, 'none'))
        service.config = Mock()
        service.config.get = Mock(side_effect=lambda key, default=None: default)

        score = service._calculate_stream_score({
            'resolution': '1280x720',
            'fps': 25,
            'video_codec': None,
            'bitrate_kbps': 3000,
        })

        self.assertGreaterEqual(score, 0.0)

    def test_score_uses_bitrate_kbps_from_analysis_result(self):
        """A bitrate estimated during analysis should participate in quality scoring."""
        service = StreamCheckerService.__new__(StreamCheckerService)
        service._is_stream_dead = Mock(return_value=(False, 'none'))
        service.config = Mock()
        service.config.get = Mock(side_effect=lambda key, default=None: default)

        base_stream = {
            'resolution': '1920x1080',
            'fps': 30,
            'video_codec': 'h264',
        }

        score_without_bitrate = service._calculate_stream_score({
            **base_stream,
            'bitrate_kbps': None,
        })
        score_with_fallback_bitrate = service._calculate_stream_score({
            **base_stream,
            'bitrate_kbps': 5000.0,
        })

        self.assertGreater(score_with_fallback_bitrate, score_without_bitrate)


class TestProgressTracking(unittest.TestCase):
    """Test progress tracking and variable initialization."""
    
    def setUp(self):
        """Set up test fixtures."""
        # Create temporary directory for test files
        self.temp_dir = tempfile.mkdtemp()
        
    def tearDown(self):
        """Clean up test fixtures."""
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)
    
    @patch('stream_checker_service.fetch_channel_streams')
    @patch('stream_checker_service.get_udi_manager')
    @patch('stream_checker_service._get_base_url')
    def test_total_streams_defined_before_use(self, mock_base_url, mock_get_udi, mock_fetch_streams):
        """Test that total_streams is defined before being used in progress updates."""
        # Setup mocks
        mock_base_url.return_value = "http://test:8000"
        
        # Mock UDI manager
        mock_udi = MagicMock()
        mock_udi.get_channel_by_id.return_value = {
            'id': 1,
            'name': 'Test Channel'
        }
        mock_get_udi.return_value = mock_udi
        
        # Mock streams - 3 streams to check
        mock_fetch_streams.return_value = [
            {'id': 1, 'name': 'Stream 1', 'url': 'http://test1'},
            {'id': 2, 'name': 'Stream 2', 'url': 'http://test2'},
            {'id': 3, 'name': 'Stream 3', 'url': 'http://test3'},
        ]
        
        # Create service instance with temporary config directory
        with patch('stream_checker_service.CONFIG_DIR', Path(self.temp_dir)):
            service = StreamCheckerService()
            
            # Mock the progress update to capture calls
            progress_calls = []
            original_update = service.progress.update
            
            def mock_progress_update(**kwargs):
                progress_calls.append(kwargs)
                # Call original to maintain state
                return original_update(**kwargs)
            
            service.progress.update = mock_progress_update
            
            # Mock analyze_stream from stream_check_utils to avoid actual stream analysis
            with patch('stream_check_utils.analyze_stream') as mock_analyze_stream:
                mock_analyze_stream.return_value = {
                    'stream_id': 1,
                    'stream_name': 'Stream 1',
                    'stream_url': 'http://test1',
                    'resolution': '1920x1080',
                    'fps': 30,
                    'video_codec': 'h264',
                    'audio_codec': 'aac',
                    'bitrate_kbps': 5000,
                    'status': 'OK'
                }
                
                with patch.object(service, '_update_stream_stats', return_value=True):
                    with patch('stream_checker_service.update_channel_streams'):
                        try:
                            # This should not raise NameError for total_streams
                            service._check_channel(1)
                            
                            # Verify that progress updates were made with total parameter
                            analyzing_updates = [c for c in progress_calls if c.get('status') == 'analyzing']
                            
                            if analyzing_updates:
                                # Check that total is defined (not None) and equals number of streams
                                for update in analyzing_updates:
                                    self.assertIn('total', update, "total parameter missing in progress update")
                                    self.assertIsNotNone(update['total'], "total parameter should not be None")
                                    self.assertEqual(update['total'], 3, "total should equal number of streams to check")
                                    
                        except NameError as e:
                            if 'total_streams' in str(e):
                                self.fail(f"NameError for total_streams should not occur: {e}")
                            raise


class TestLegacySequentialDelegation(unittest.TestCase):
    def test_legacy_sequential_entry_uses_exact_profile_scheduler(self):
        service = StreamCheckerService.__new__(StreamCheckerService)
        service._check_channel_concurrent = Mock(return_value={'success': True})

        result = service._check_channel_sequential(
            77,
            skip_batch_changelog=True,
            target_stream_ids=['101'],
            forced_profile_id='profile-a',
            provider_limit_override=True,
            run_mode='quality',
            is_single_channel_check=True,
            force_check_override=True,
            force_check_generation=9,
            batch_changelog_generation=10,
            queue_entry_token=11,
        )

        self.assertEqual(result, {'success': True})
        service._check_channel_concurrent.assert_called_once_with(
            77,
            skip_batch_changelog=True,
            target_stream_ids=['101'],
            forced_profile_id='profile-a',
            provider_limit_override=True,
            run_mode='quality',
            is_single_channel_check=True,
            global_limit_override=1,
            force_check_override=True,
            force_check_generation=9,
            batch_changelog_generation=10,
            queue_entry_token=11,
        )


class TestLoopProbeCapacity(unittest.TestCase):
    """Regression coverage for long loop-probe capacity and abort behavior."""

    @staticmethod
    def _service(concurrent_enabled=True, global_limit=4):
        service = StreamCheckerService.__new__(StreamCheckerService)
        values = {
            'concurrent_streams.enabled': concurrent_enabled,
            'concurrent_streams.global_limit': global_limit,
        }
        service.config = Mock()
        service.config.get.side_effect = lambda key, default=None: values.get(key, default)
        service.progress = Mock()
        service.abort_current_check = threading.Event()
        return service

    @staticmethod
    def _analyzed_stream():
        return {
            'stream_id': 1,
            'stream_name': 'Candidate',
            'stream_url': 'http://old-profile.example/stream',
            'score': 0.9,
            'status': 'OK',
        }

    @staticmethod
    def _udi_and_limiter():
        profile = {'id': 11, 'name': 'Reserved', 'max_streams': 1, 'is_active': True}
        raw_stream = {
            'id': 1,
            'name': 'Candidate',
            'url': 'http://provider.example/stream',
            'm3u_account_id': 7,
        }
        account = {'id': 7, 'max_streams': 1, 'profiles': [profile]}
        udi = Mock()
        udi.get_m3u_accounts.return_value = [account]
        udi.get_stream_by_id.return_value = raw_stream
        udi.apply_profile_url_transformation.return_value = 'http://reserved-profile.example/stream'
        limiter = Mock()
        limiter.acquire.return_value = (True, 'acquired')
        limiter.reserve_profile_for_stream_with_url.return_value = (
            True,
            'acquired',
            profile,
            'http://reserved-profile.example/stream',
        )
        limiter.should_preempt_profile_for_viewer.return_value = False
        return udi, limiter, account, profile, raw_stream

    def test_loop_probe_initializes_limits_reserves_profile_and_transforms_raw_url_once(self):
        service = self._service(concurrent_enabled=False, global_limit=8)
        stream = self._analyzed_stream()
        udi, limiter, account, profile, raw_stream = self._udi_and_limiter()
        worker_limits = []
        real_executor = concurrent.futures.ThreadPoolExecutor

        def executor_factory(*args, **kwargs):
            max_workers = kwargs.get('max_workers', args[0] if args else None)
            worker_limits.append(max_workers)
            return real_executor(*args, **kwargs)

        def probe(**kwargs):
            self.assertFalse(kwargs['should_abort']())
            return False, None, 60

        with patch('apps.stream.stream_checker_service.get_udi_manager', return_value=udi), patch(
            'apps.stream.concurrent_stream_limiter.get_account_limiter', return_value=limiter
        ), patch(
            'apps.stream.concurrent_stream_limiter.initialize_account_limits'
        ) as initialize_limits, patch(
            'apps.stream.stream_check_utils._probe_stream_for_loops', side_effect=probe
        ) as loop_probe, patch(
            'concurrent.futures.ThreadPoolExecutor', side_effect=executor_factory
        ):
            service._run_loop_probes([stream])

        self.assertIs(limiter.udi_manager, udi)
        limiter.invalidate_account_inventory.assert_called_once_with()
        initialize_limits.assert_called_once_with([account])
        self.assertEqual(worker_limits, [1])
        limiter.acquire.assert_called_once_with(7, timeout=0)
        reservation_stream = limiter.reserve_profile_for_stream_with_url.call_args.args[0]
        self.assertEqual(reservation_stream['url'], raw_stream['url'])
        self.assertEqual(reservation_stream['m3u_account_id'], 7)
        self.assertEqual(reservation_stream['m3u_account'], 7)
        udi.apply_profile_url_transformation.assert_not_called()
        self.assertEqual(loop_probe.call_args.kwargs['url'], 'http://reserved-profile.example/stream')
        self.assertNotEqual(loop_probe.call_args.kwargs['url'], stream['stream_url'])
        limiter.release_profile.assert_called_once_with(profile)
        limiter.release.assert_called_once_with(7)
        self.assertTrue(stream['loop_probe_ran'])
        self.assertFalse(stream['loop_detected'])

    def test_loop_probe_empty_or_malformed_inventory_never_opens_capacity(self):
        for case_name, inventory in (
            ('malformed', {'id': 7}),
            ('rejected_malformed', [{'id': 'invalid', 'max_streams': 1}]),
        ):
            with self.subTest(case=case_name):
                service = self._service()
                stream = self._analyzed_stream()
                udi, limiter, _account, _profile, _raw_stream = (
                    self._udi_and_limiter()
                )
                events = []
                udi.get_m3u_accounts.side_effect = (
                    lambda value=inventory: events.append('fetch') or value
                )
                limiter.invalidate_account_inventory.side_effect = (
                    lambda: events.append('invalidate')
                )

                with patch(
                    'apps.stream.stream_checker_service.get_udi_manager',
                    return_value=udi,
                ), patch(
                    'apps.stream.concurrent_stream_limiter.get_account_limiter',
                    return_value=limiter,
                ), patch(
                    'apps.stream.concurrent_stream_limiter.initialize_account_limits',
                    side_effect=lambda accounts: (
                        events.append('initialize')
                        or (False if case_name == 'rejected_malformed' else None)
                    ),
                ) as initialize_limits, patch(
                    'apps.stream.stream_check_utils._probe_stream_for_loops',
                ) as loop_probe:
                    service._run_loop_probes([stream])

                self.assertEqual(events[0], 'invalidate')
                limiter.invalidate_account_inventory.assert_called_once_with()
                loop_probe.assert_not_called()
                limiter.acquire.assert_not_called()
                limiter.reserve_profile_for_stream_with_url.assert_not_called()
                limiter.release.assert_not_called()
                limiter.release_profile.assert_not_called()
                self.assertFalse(stream['loop_probe_ran'])
                self.assertIsNone(stream['loop_detected'])
                if case_name == 'rejected_malformed':
                    self.assertEqual(events, ['invalidate', 'fetch', 'initialize'])
                    initialize_limits.assert_called_once_with(inventory)
                else:
                    self.assertEqual(events, ['invalidate', 'fetch'])
                    initialize_limits.assert_not_called()

    def test_loop_probe_empty_inventory_allows_custom_but_blocks_provider(self):
        from apps.stream.concurrent_stream_limiter import get_account_limiter

        limiter = get_account_limiter()
        previous_udi_manager = limiter.udi_manager
        cases = (
            (
                'provider',
                {
                    'id': 1,
                    'name': 'Provider candidate',
                    'url': 'http://provider.invalid/live.m3u8',
                    'm3u_account_id': 7,
                },
                False,
            ),
            (
                'custom',
                {
                    'id': 1,
                    'name': 'Custom candidate',
                    'url': 'http://custom.invalid/live.m3u8',
                    'is_custom': True,
                },
                True,
            ),
        )

        try:
            for case_name, raw_stream, should_probe in cases:
                with self.subTest(case=case_name):
                    limiter.clear()
                    service = self._service()
                    analyzed = self._analyzed_stream()
                    preemption_checks = []
                    udi = Mock()
                    udi.get_m3u_accounts.return_value = []
                    udi.get_stream_by_id.return_value = raw_stream

                    def probe(**kwargs):
                        preemption_checks.append(kwargs['should_abort']())
                        return False, None, 60

                    with patch(
                        'apps.stream.stream_checker_service.get_udi_manager',
                        return_value=udi,
                    ), patch(
                        'apps.stream.stream_check_utils._probe_stream_for_loops',
                        side_effect=probe,
                    ) as loop_probe:
                        service._run_loop_probes([analyzed])

                    if should_probe:
                        loop_probe.assert_called_once()
                        self.assertEqual(
                            loop_probe.call_args.kwargs['url'],
                            raw_stream['url'],
                        )
                        self.assertTrue(analyzed['loop_probe_ran'])
                        self.assertFalse(analyzed['loop_detected'])
                        self.assertEqual(preemption_checks, [False])
                    else:
                        loop_probe.assert_not_called()
                        self.assertFalse(analyzed['loop_probe_ran'])
                        self.assertIsNone(analyzed['loop_detected'])
                        self.assertEqual(preemption_checks, [])
                    self.assertTrue(limiter.account_inventory_trusted)
                    self.assertEqual(limiter.account_inventory_ids, set())
                    self.assertEqual(limiter.account_checking_counts, {})
                    self.assertEqual(limiter.profile_checking_counts, {})
                    self.assertEqual(limiter.profile_reservations_by_token, {})
        finally:
            limiter.clear()
            limiter.udi_manager = previous_udi_manager

    def test_loop_probe_progress_cannot_resurrect_after_clear(self):
        service = self._service()
        stream = self._analyzed_stream()
        udi, limiter, _account, _profile, _raw_stream = self._udi_and_limiter()
        probe_started = threading.Event()
        release_probe = threading.Event()

        class FakeDB:
            def __init__(self):
                self.settings = {}

            def get_system_setting(self, key, default=None):
                return self.settings.get(key, default)

            def set_system_setting(self, key, value):
                self.settings[key] = value

        fake_db = FakeDB()

        def blocking_probe(**kwargs):
            probe_started.set()
            self.assertTrue(release_probe.wait(timeout=2))
            self.assertTrue(kwargs['should_abort']())
            return False, None, 0

        with patch('apps.database.manager.get_db_manager', return_value=fake_db), patch(
            'apps.stream.stream_checker_service.get_udi_manager', return_value=udi
        ), patch(
            'apps.stream.concurrent_stream_limiter.get_account_limiter', return_value=limiter
        ), patch(
            'apps.stream.concurrent_stream_limiter.initialize_account_limits'
        ), patch(
            'apps.stream.stream_check_utils._probe_stream_for_loops',
            side_effect=blocking_probe,
        ):
            service.progress = StreamCheckerProgress()
            run_generation = service.progress.get_generation()
            progress_updates = []
            original_update = service.progress.update

            def record_progress(**kwargs):
                progress_updates.append(dict(kwargs))
                return original_update(**kwargs)

            service.progress.update = record_progress
            probe_thread = threading.Thread(
                target=service._run_loop_probes,
                kwargs={
                    'analyzed_streams': [stream],
                    'channel_id': 99,
                    'channel_name': 'Loop clear race',
                    'streams_detail': [{'id': 1, 'status': 'completed'}],
                    'profile_progress_context': {
                        'expected_generation': run_generation,
                    },
                },
            )
            probe_thread.start()
            self.assertTrue(probe_started.wait(timeout=2))

            service.abort_current_check.set()
            service.progress.clear()
            release_probe.set()
            probe_thread.join(timeout=2)

        self.assertFalse(probe_thread.is_alive())
        self.assertEqual(len(progress_updates), 2)
        self.assertTrue(all(
            update.get('expected_generation') == run_generation
            for update in progress_updates
        ))
        self.assertEqual(
            progress_updates[-1]['streams_detail'][0]['status'],
            'aborted',
        )
        self.assertEqual(fake_db.settings['stream_checker_progress'], {})

    def test_loop_probe_honors_legacy_global_limit_override(self):
        service = self._service(concurrent_enabled=True, global_limit=8)
        streams = [
            {
                **self._analyzed_stream(),
                'stream_id': stream_id,
                'stream_name': f'Candidate {stream_id}',
                'score': 1.0 - (stream_id / 100),
            }
            for stream_id in range(1, 9)
        ]
        udi, limiter, _account, _profile, raw_stream = self._udi_and_limiter()
        udi.get_stream_by_id.side_effect = lambda stream_id: {
            **raw_stream,
            'id': stream_id,
        }
        worker_limits = []
        real_executor = concurrent.futures.ThreadPoolExecutor

        def executor_factory(*args, **kwargs):
            max_workers = kwargs.get('max_workers', args[0] if args else None)
            worker_limits.append(max_workers)
            return real_executor(*args, **kwargs)

        with patch('apps.stream.stream_checker_service.get_udi_manager', return_value=udi), patch(
            'apps.stream.concurrent_stream_limiter.get_account_limiter', return_value=limiter
        ), patch(
            'apps.stream.concurrent_stream_limiter.initialize_account_limits'
        ), patch(
            'apps.stream.stream_check_utils._probe_stream_for_loops',
            return_value=(False, None, 60),
        ), patch(
            'concurrent.futures.ThreadPoolExecutor', side_effect=executor_factory
        ):
            service._run_loop_probes(streams, global_limit_override=1)

        self.assertEqual(worker_limits, [1])
        self.assertEqual(limiter.reserve_profile_for_stream_with_url.call_count, 2)

    def test_loop_probe_viewer_preemption_keeps_result_unstamped_and_releases_both_slots(self):
        service = self._service()
        stream = self._analyzed_stream()
        original_score = stream['score']
        udi, limiter, _account, profile, _raw_stream = self._udi_and_limiter()
        limiter.should_preempt_profile_for_viewer.return_value = True

        def preempting_probe(**kwargs):
            self.assertTrue(kwargs['should_abort']())
            return False, None, 0

        with patch('apps.stream.stream_checker_service.get_udi_manager', return_value=udi), patch(
            'apps.stream.concurrent_stream_limiter.get_account_limiter', return_value=limiter
        ), patch(
            'apps.stream.concurrent_stream_limiter.initialize_account_limits'
        ), patch(
            'apps.stream.stream_check_utils._probe_stream_for_loops', side_effect=preempting_probe
        ):
            service._run_loop_probes(
                [stream],
                channel_id=99,
                streams_detail=[{'id': 1, 'status': 'completed'}],
            )

        self.assertFalse(stream['loop_probe_ran'])
        self.assertIsNone(stream['loop_detected'])
        self.assertEqual(stream['score'], original_score)
        limiter.release_profile.assert_called_once_with(profile)
        limiter.release.assert_called_once_with(7)
        limiter.release_viewer_preemption_claim.assert_called_once()
        final_progress = service.progress.update.call_args.kwargs
        self.assertEqual(final_progress['current'], 1)
        self.assertEqual(final_progress['streams_detail'][0]['status'], 'viewer_preempted')

    def test_loop_probe_manual_abort_stops_running_probe_without_clean_result(self):
        service = self._service()
        stream = self._analyzed_stream()
        udi, limiter, _account, profile, _raw_stream = self._udi_and_limiter()

        def aborting_probe(**kwargs):
            service.abort_current_check.set()
            self.assertTrue(kwargs['should_abort']())
            return False, None, 0

        with patch('apps.stream.stream_checker_service.get_udi_manager', return_value=udi), patch(
            'apps.stream.concurrent_stream_limiter.get_account_limiter', return_value=limiter
        ), patch(
            'apps.stream.concurrent_stream_limiter.initialize_account_limits'
        ), patch(
            'apps.stream.stream_check_utils._probe_stream_for_loops', side_effect=aborting_probe
        ):
            service._run_loop_probes(
                [stream],
                channel_id=99,
                streams_detail=[{'id': 1, 'status': 'completed'}],
            )

        self.assertFalse(stream['loop_probe_ran'])
        self.assertIsNone(stream['loop_detected'])
        limiter.should_preempt_profile_for_viewer.assert_not_called()
        limiter.release_profile.assert_called_once_with(profile)
        limiter.release.assert_called_once_with(7)
        self.assertEqual(
            service.progress.update.call_args.kwargs['streams_detail'][0]['status'],
            'aborted',
        )

    def test_loop_probe_capacity_wait_is_abortable_and_finishes_progress(self):
        service = self._service()
        stream = self._analyzed_stream()
        udi, limiter, _account, _profile, _raw_stream = self._udi_and_limiter()

        def blocked_acquire(_account_id, timeout=None):
            self.assertEqual(timeout, 0)
            service.abort_current_check.set()
            return False, 'timeout'

        limiter.acquire.side_effect = blocked_acquire
        with patch('apps.stream.stream_checker_service.get_udi_manager', return_value=udi), patch(
            'apps.stream.concurrent_stream_limiter.get_account_limiter', return_value=limiter
        ), patch(
            'apps.stream.concurrent_stream_limiter.initialize_account_limits'
        ), patch('apps.stream.stream_check_utils._probe_stream_for_loops') as loop_probe:
            service._run_loop_probes(
                [stream],
                channel_id=99,
                streams_detail=[{'id': 1, 'status': 'completed'}],
            )

        self.assertEqual(limiter.acquire.call_count, 1)
        limiter.reserve_profile_for_stream_with_url.assert_not_called()
        limiter.release.assert_not_called()
        loop_probe.assert_not_called()
        self.assertEqual(service.progress.update.call_args.kwargs['current'], 1)
        self.assertEqual(
            service.progress.update.call_args.kwargs['streams_detail'][0]['status'],
            'aborted',
        )

    def test_loop_probe_account_capacity_timeout_finishes_progress_as_skipped(self):
        service = self._service()
        stream = self._analyzed_stream()
        udi, limiter, _account, _profile, _raw_stream = self._udi_and_limiter()
        limiter.acquire.return_value = (False, 'active_viewers')

        with patch('apps.stream.stream_checker_service.get_udi_manager', return_value=udi), patch(
            'apps.stream.concurrent_stream_limiter.get_account_limiter', return_value=limiter
        ), patch(
            'apps.stream.concurrent_stream_limiter.initialize_account_limits'
        ), patch(
            'apps.stream.stream_checker_service.time.monotonic', side_effect=[0.0, 61.0]
        ), patch('apps.stream.stream_check_utils._probe_stream_for_loops') as loop_probe:
            service._run_loop_probes(
                [stream],
                channel_id=99,
                streams_detail=[{'id': 1, 'status': 'completed'}],
            )

        limiter.acquire.assert_called_once_with(7, timeout=0)
        limiter.reserve_profile_for_stream_with_url.assert_not_called()
        limiter.release.assert_not_called()
        loop_probe.assert_not_called()
        self.assertEqual(service.progress.update.call_args.kwargs['current'], 1)
        self.assertEqual(
            service.progress.update.call_args.kwargs['streams_detail'][0]['status'],
            'skipped',
        )

    def test_loop_probe_raw_lookup_and_profile_capacity_fail_closed_with_progress(self):
        for case_name in ('raw_missing', 'profile_full'):
            with self.subTest(case=case_name):
                service = self._service()
                stream = self._analyzed_stream()
                udi, limiter, _account, _profile, _raw_stream = self._udi_and_limiter()
                if case_name == 'raw_missing':
                    udi.get_stream_by_id.return_value = None
                else:
                    limiter.reserve_profile_for_stream_with_url.return_value = (
                        False,
                        'checking_capacity',
                        None,
                        '',
                    )

                with patch('apps.stream.stream_checker_service.get_udi_manager', return_value=udi), patch(
                    'apps.stream.concurrent_stream_limiter.get_account_limiter', return_value=limiter
                ), patch(
                    'apps.stream.concurrent_stream_limiter.initialize_account_limits'
                ), patch('apps.stream.stream_check_utils._probe_stream_for_loops') as loop_probe:
                    service._run_loop_probes(
                        [stream],
                        channel_id=99,
                        streams_detail=[{'id': 1, 'status': 'completed'}],
                    )

                loop_probe.assert_not_called()
                self.assertEqual(service.progress.update.call_args.kwargs['current'], 1)
                self.assertEqual(
                    service.progress.update.call_args.kwargs['streams_detail'][0]['status'],
                    'skipped',
                )
                if case_name == 'raw_missing':
                    limiter.acquire.assert_not_called()
                    limiter.release.assert_not_called()
                else:
                    limiter.acquire.assert_called_once_with(7, timeout=0)
                    limiter.release.assert_called_once_with(7)
                    limiter.release_profile.assert_not_called()


if __name__ == '__main__':
    unittest.main()
