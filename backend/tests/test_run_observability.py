from datetime import datetime, timedelta
import json
import threading
import unittest
from unittest.mock import Mock, patch

from apps.automation.automated_stream_manager import AutomatedStreamManager, ChangelogManager
from apps.udi.fetcher import UDIFetcher


class FakeSettingsDB:
    def __init__(self, settings=None):
        self.settings = dict(settings or {})

    def get_system_setting(self, key, default=None):
        return self.settings.get(key, default)

    def set_system_setting(self, key, value):
        self.settings[key] = value


class AutomationRunStatusTests(unittest.TestCase):
    def _manager(self):
        manager = AutomatedStreamManager.__new__(AutomatedStreamManager)
        manager._run_status_lock = threading.RLock()
        manager._run_sequence = 0
        manager._manual_stop_requested = threading.Event()
        manager.automation_thread = None
        manager.automation_running = False
        manager.running = False
        manager.automation_wake_event = threading.Event()
        manager.automation_start_time = None
        manager._run_status = manager._build_run_status(
            run_id=None,
            state="idle",
            stage="idle",
            stage_label="Idle",
            message="No automation cycle has run yet",
        )
        return manager

    def test_run_status_tracks_stage_counts_and_finish(self):
        manager = self._manager()

        manager._start_run_status(forced=True, forced_period_id="period-1")
        manager._update_run_status(
            stage="m3u_refresh",
            stage_label="Refreshing M3U",
            counts={"channels_with_periods": 12},
            durations={"m3u_refresh_seconds": 1.23456},
        )
        manager._finish_run_status(
            state="completed",
            stage="completed",
            stage_label="Completed",
            message="Automation cycle completed",
        )

        status = manager.get_run_status()
        self.assertEqual(status["state"], "completed")
        self.assertEqual(status["stage"], "completed")
        self.assertEqual(status["forced_period_id"], "period-1")
        self.assertEqual(status["counts"]["channels_with_periods"], 12)
        self.assertEqual(status["durations"]["m3u_refresh_seconds"], 1.235)
        self.assertIsNotNone(status["completed_at"])
        self.assertIsNotNone(status["duration_seconds"])
        self.assertEqual(
            [stage["key"] for stage in status["stages"]],
            [
                "settings",
                "period_discovery",
                "m3u_refresh",
                "cache_sync",
                "stream_matching",
                "quality_queueing",
                "quality_checking",
                "finalizing",
            ],
        )

    def test_run_status_captures_initial_and_effective_run_snapshot(self):
        manager = self._manager()
        manager.config = {
            "enabled_features": {
                "changelog_tracking": True,
                "teamarr_event_groups": False,
            },
        }
        fake_db = FakeSettingsDB({
            AutomatedStreamManager.RUN_SNAPSHOT_SETTINGS_KEY: {
                "max_bytes": 8192,
                "retention_count": 3,
            },
        })
        automation_config = Mock()
        automation_config.get_profile.return_value = {
            "name": "Prime",
            "stream_checking": {
                "enabled": True,
                "check_all_streams": True,
                "stream_limit": 4,
            },
        }
        udi = Mock()
        udi.is_network_ready.return_value = True
        udi.get_m3u_accounts.return_value = [
            {
                "id": 5,
                "name": "Provider A",
                "is_active": True,
                "status": "fetching",
                "last_message": "Processing completed in 168.5 seconds.",
            },
            {
                "id": 7,
                "name": "Provider B",
                "is_active": True,
                "status": "success",
                "last_message": "Processing completed in 140.9 seconds.",
            },
        ]

        with patch("apps.database.manager.get_db_manager", return_value=fake_db):
            manager._start_run_status(forced=True, forced_period_id="period-1")

            initial_snapshot = manager.get_run_status()["run_snapshot"]
            self.assertEqual(initial_snapshot["run_mode"], "manual_period_run")
            self.assertEqual(initial_snapshot["start_source"], "manual")
            self.assertEqual(initial_snapshot["forced_period_id"], "period-1")
            self.assertEqual(initial_snapshot["limits"]["retention_count"], 3)

            manager._finalize_run_snapshot(
                {
                    ("period-1", "Prime Time"): {
                        "profile_id": "profile-a",
                        "profile_name": "Prime",
                        "channels": [{"id": 10}, {"id": 11}],
                    },
                },
                automation_config,
                {"regular_automation_enabled": True},
                udi=udi,
                teamarr_event_window={"hours": 12},
            )
            manager._finish_run_status(
                state="completed",
                stage="completed",
                stage_label="Completed",
                message="Automation cycle completed",
            )

        status = manager.get_run_status()
        snapshot = status["run_snapshot"]
        self.assertEqual(snapshot["run_id"], status["run_id"])
        self.assertEqual(snapshot["effective_profile_count"], 1)
        self.assertEqual(snapshot["channel_count"], 2)
        self.assertEqual(snapshot["effective_profiles"][0]["profile_name"], "Prime")
        self.assertEqual(snapshot["quality_rules"][0]["enabled"], True)
        self.assertEqual(snapshot["capacity_profile_context"]["type"], "provider_account_profiles")
        self.assertEqual(snapshot["dispatcharr_status"]["network_ready"], True)
        self.assertEqual(snapshot["dispatcharr_status"]["stale_status"]["status"], "stale_risk")
        self.assertEqual(snapshot["dispatcharr_status"]["stale_status"]["stale_suspected_count"], 1)
        self.assertEqual(snapshot["stale_warnings"][0]["type"], "dispatcharr_status_risk")
        self.assertNotIn("Provider A", json.dumps(snapshot))
        self.assertNotIn("Processing completed", json.dumps(snapshot))
        self.assertEqual(snapshot["teamarr_status"]["event_window_active"], True)
        self.assertEqual(snapshot["feature_flags"]["regular_automation_enabled"], True)
        self.assertFalse(snapshot["snapshot_truncated"])

        stored = fake_db.settings[AutomatedStreamManager.RUN_SNAPSHOT_HISTORY_KEY]
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0]["run_id"], status["run_id"])

    def test_run_status_marks_prior_stages_completed(self):
        manager = self._manager()

        manager._start_run_status(forced=True, forced_period_id="period-1")
        manager._update_run_status(
            stage="settings",
            stage_label="Preparing Automation",
            message="Reading configuration",
        )
        manager._update_run_status(
            stage="stream_matching",
            stage_label="Matching Streams",
            message="Matching streams",
            progress={"current": 2, "total": 4, "message": "Matching streams"},
        )

        status = manager.get_run_status()
        stages = {stage["key"]: stage for stage in status["stages"]}

        self.assertEqual(stages["settings"]["status"], "completed")
        self.assertEqual(stages["period_discovery"]["status"], "completed")
        self.assertEqual(stages["m3u_refresh"]["status"], "completed")
        self.assertEqual(stages["cache_sync"]["status"], "completed")
        self.assertEqual(stages["stream_matching"]["status"], "running")
        self.assertEqual(stages["stream_matching"]["current"], 2)
        self.assertEqual(stages["stream_matching"]["total"], 4)
        self.assertEqual(stages["quality_queueing"]["status"], "pending")

    def test_run_status_returns_copy(self):
        manager = self._manager()
        status = manager.get_run_status()
        status["counts"]["channels_with_periods"] = 99

        self.assertNotIn("channels_with_periods", manager.get_run_status()["counts"])

    def test_running_run_status_elapsed_fields_are_live(self):
        manager = self._manager()
        manager._start_run_status(forced=False, forced_period_id=None)
        now = datetime.now()
        with manager._run_status_lock:
            manager._run_status["started_at"] = (now - timedelta(seconds=120)).isoformat()
            manager._run_status["stage_started_at"] = (now - timedelta(seconds=45)).isoformat()
            manager._run_status["updated_at"] = (now - timedelta(seconds=60)).isoformat()
            manager._run_status["duration_seconds"] = 0
            manager._run_status["stage_duration_seconds"] = 0

        status = manager.get_run_status()

        self.assertGreaterEqual(status["duration_seconds"], 119)
        self.assertGreaterEqual(status["stage_duration_seconds"], 44)
        self.assertNotEqual(status["updated_at"], manager._run_status["updated_at"])

    def test_cycle_abort_finishes_as_aborted_not_failed(self):
        manager = self._manager()

        manager._start_run_status(forced=True, forced_period_id="period-1")
        manager._update_run_status(stage="quality_checking", stage_label="Quality Checking")
        outcome = manager._finish_cycle_outcome(
            refresh_success=True,
            cycle_abort_message="Quality check stage stopped before completion (0/212 channels checked)",
        )

        status = manager.get_run_status()
        self.assertEqual(outcome, "aborted")
        self.assertEqual(status["state"], "aborted")
        self.assertEqual(status["stage"], "aborted")
        self.assertEqual(status["stage_label"], "Aborted")
        self.assertEqual(status["last_error"], "Quality check stage stopped before completion (0/212 channels checked)")

    def test_cycle_quality_exception_finishes_as_failed(self):
        manager = self._manager()

        manager._start_run_status(forced=True, forced_period_id="period-1")
        manager._update_run_status(stage="quality_checking", stage_label="Quality Checking")
        outcome = manager._finish_cycle_outcome(
            refresh_success=True,
            cycle_abort_message=None,
            cycle_failed_message="Quality check stage failed: boom",
        )

        status = manager.get_run_status()
        self.assertEqual(outcome, "failed")
        self.assertEqual(status["state"], "failed")
        self.assertEqual(status["stage"], "failed")
        self.assertEqual(status["stage_label"], "Failed")
        self.assertEqual(status["last_error"], "Quality check stage failed: boom")

    def test_cycle_provider_partial_finishes_as_completed_degraded(self):
        manager = self._manager()

        manager._start_run_status(forced=True, forced_period_id="period-1")
        manager._update_run_status(stage="finalizing", stage_label="Finalizing")
        outcome = manager._finish_cycle_outcome(
            refresh_success=True,
            refresh_degraded=True,
            cycle_abort_message=None,
        )

        status = manager.get_run_status()
        stages = {stage["key"]: stage for stage in status["stages"]}
        self.assertEqual(outcome, "completed_degraded")
        self.assertEqual(status["state"], "completed_degraded")
        self.assertEqual(status["stage"], "completed_degraded")
        self.assertEqual(status["stage_label"], "Completed with Warnings")
        self.assertEqual(stages["finalizing"]["status"], "completed")

    def test_manual_stop_request_finishes_cycle_as_aborted(self):
        manager = self._manager()

        manager._start_run_status(forced=True, forced_period_id="period-1")
        manager._update_run_status(stage="stream_matching", stage_label="Matching Streams")
        manager._manual_stop_requested.set()
        outcome = manager._finish_cycle_outcome(
            refresh_success=True,
            cycle_abort_message=None,
        )

        status = manager.get_run_status()
        self.assertEqual(outcome, "aborted")
        self.assertEqual(status["state"], "aborted")
        self.assertEqual(status["stage"], "aborted")
        self.assertEqual(status["message"], "Automation run was stopped by the user")
        self.assertEqual(status["last_error"], "Automation run was stopped by the user")

    def test_aborted_cycle_does_not_advance_period_schedule_clock(self):
        manager = self._manager()
        original_last_run = datetime.now() - timedelta(minutes=90)
        manager.period_last_run = {"period-1": original_last_run}
        manager.last_playlist_update = original_last_run
        manager._save_state = Mock()

        advanced = manager._advance_period_run_timestamps(
            {("period-1", "Full Check"): {"channels": [1, 2, 3]}},
            "aborted",
        )

        self.assertFalse(advanced)
        self.assertEqual(manager.period_last_run["period-1"], original_last_run)
        self.assertEqual(manager.last_playlist_update, original_last_run)
        manager._save_state.assert_not_called()

    def test_completed_cycle_advances_period_schedule_clock(self):
        manager = self._manager()
        original_last_run = datetime.now() - timedelta(minutes=90)
        manager.period_last_run = {"period-1": original_last_run}
        manager.last_playlist_update = original_last_run
        manager._save_state = Mock()

        advanced = manager._advance_period_run_timestamps(
            {("period-1", "Full Check"): {"channels": [1, 2, 3]}},
            "completed",
        )

        self.assertTrue(advanced)
        self.assertGreater(manager.period_last_run["period-1"], original_last_run)
        self.assertGreater(manager.last_playlist_update, original_last_run)
        manager._save_state.assert_called_once()

    def test_request_active_run_stop_keeps_scheduler_service_running(self):
        manager = self._manager()

        manager._start_run_status(forced=False, forced_period_id=None)
        manager.automation_running = True
        manager.running = True

        requested = manager.request_active_run_stop()

        status = manager.get_run_status()
        self.assertTrue(requested)
        self.assertTrue(manager._manual_stop_requested.is_set())
        self.assertTrue(manager.automation_running)
        self.assertTrue(manager.running)
        self.assertEqual(status["state"], "running")
        self.assertEqual(status["message"], "Stop requested; active automation run is shutting down")

    def test_stop_automation_requests_abort_for_active_run(self):
        manager = self._manager()

        manager._start_run_status(forced=True, forced_period_id="period-1")
        manager.automation_running = True
        manager.running = True
        manager.stop_automation()

        status = manager.get_run_status()
        self.assertTrue(manager._manual_stop_requested.is_set())
        self.assertFalse(manager.automation_running)
        self.assertFalse(manager.running)
        self.assertEqual(status["state"], "running")
        self.assertEqual(status["message"], "Stop requested; automation is shutting down")

    def test_stop_automation_does_not_rewrite_completed_run(self):
        manager = self._manager()

        manager._start_run_status(forced=True, forced_period_id="period-1")
        manager._finish_run_status(
            state="completed",
            stage="completed",
            stage_label="Completed",
            message="Automation cycle completed",
        )
        manager.automation_running = True
        manager.running = True
        manager.stop_automation()

        status = manager.get_run_status()
        self.assertFalse(manager._manual_stop_requested.is_set())
        self.assertEqual(status["state"], "completed")
        self.assertEqual(status["message"], "Automation cycle completed")

    def test_quality_summary_flags_connectivity_abort_and_incomplete_run(self):
        summary = AutomatedStreamManager._summarize_quality_check_results(
            {
                101: {"success": True},
                102: {
                    "success": False,
                    "error": "connectivity_guard",
                    "aborted": True,
                    "message": "dispatcharr_api connectivity probe timed out",
                },
            },
            expected_count=5,
        )

        self.assertFalse(summary["ok"])
        self.assertEqual(summary["checked_count"], 2)
        self.assertEqual(summary["aborted_count"], 1)
        self.assertEqual(summary["incomplete_count"], 3)
        self.assertEqual(summary["abort_message"], "dispatcharr_api connectivity probe timed out")

    def test_channel_visibility_summary_counts_unique_hidden_and_ready_channels(self):
        summary = AutomatedStreamManager._summarize_channel_visibility_events([
            {"changed": True, "action": "hidden", "channel_id": 10},
            {"changed": True, "action": "hidden", "channel_id": 10},
            {"changed": True, "action": "unhidden", "channel_ref": "channel-11"},
            {"changed": False, "action": "hidden", "channel_id": 12},
            {"changed": True, "action": "noop", "channel_id": 13},
        ])

        self.assertEqual(summary["channels_hidden"], 1)
        self.assertEqual(summary["channels_ready"], 1)
        self.assertEqual(summary["channel_visibility_changed"], 4)

    def test_refresh_playlists_emits_account_progress(self):
        manager = self._manager()
        manager.config = {
            "enabled_features": {
                "auto_playlist_update": True,
                "changelog_tracking": False,
            },
            "enabled_m3u_accounts": [],
        }
        events = []
        response = Mock(status_code=200)
        scheduling_service = Mock()

        with patch(
            "apps.automation.automated_stream_manager.get_m3u_accounts",
            return_value=[
                {"id": 1, "name": "One", "is_active": True},
                {"id": 2, "name": "Two", "is_active": True},
                {"id": 3, "name": "custom", "is_active": True},
            ],
        ), patch(
            "apps.automation.automated_stream_manager.refresh_m3u_playlists",
            return_value=response,
        ), patch(
            "apps.automation.scheduling_service.get_scheduling_service",
            return_value=scheduling_service,
        ):
            success, accounts = manager.refresh_playlists(
                skip_changelog=True,
                progress_callback=events.append,
            )

        self.assertTrue(success)
        self.assertEqual([account["id"] for account in accounts], [1, 2])
        self.assertEqual(
            [(event["state"], event["current"], event["total"]) for event in events],
            [
                ("planned", 0, 2),
                ("requesting", 0, 2),
                ("accepted", 1, 2),
                ("requesting", 1, 2),
                ("accepted", 2, 2),
            ],
        )
        self.assertEqual(events[1]["message"], "Refreshing playlist 1/2: One")
        self.assertEqual(events[-1]["message"], "Playlist 2/2 refresh accepted: Two")

    def test_refresh_playlists_reports_missing_account_as_skipped(self):
        manager = self._manager()
        manager.config = {
            "enabled_features": {
                "auto_playlist_update": True,
                "changelog_tracking": False,
            },
            "enabled_m3u_accounts": [],
        }
        events = []
        scheduling_service = Mock()

        with patch(
            "apps.automation.automated_stream_manager.get_m3u_accounts",
            return_value=[{"id": 2, "name": "Two", "is_active": True}],
        ), patch(
            "apps.automation.automated_stream_manager.refresh_m3u_playlists",
        ) as refresh_mock, patch(
            "apps.automation.scheduling_service.get_scheduling_service",
            return_value=scheduling_service,
        ):
            success, accounts = manager.refresh_playlists(
                account_id=99,
                skip_changelog=True,
                progress_callback=events.append,
            )

        self.assertTrue(success)
        self.assertEqual(accounts, [])
        refresh_mock.assert_not_called()
        self.assertEqual(events[-1]["state"], "skipped")
        self.assertEqual(events[-1]["message"], "No active playlists matched the refresh request")

    def test_refresh_playlists_monitors_already_running_account_without_retriggering(self):
        manager = self._manager()
        manager.config = {
            "enabled_features": {
                "auto_playlist_update": True,
                "changelog_tracking": False,
            },
            "enabled_m3u_accounts": [],
        }
        events = []
        scheduling_service = Mock()

        with patch(
            "apps.automation.automated_stream_manager.get_m3u_accounts",
            return_value=[{"id": 1, "name": "One", "is_active": True, "status": "refreshing"}],
        ), patch(
            "apps.automation.automated_stream_manager.refresh_m3u_playlists",
        ) as refresh_mock, patch(
            "apps.automation.scheduling_service.get_scheduling_service",
            return_value=scheduling_service,
        ):
            success, accounts = manager.refresh_playlists(
                skip_changelog=True,
                progress_callback=events.append,
            )

        self.assertTrue(success)
        self.assertEqual(accounts, [{"id": 1, "name": "One", "already_running": True}])
        refresh_mock.assert_not_called()
        self.assertEqual(events[-1]["state"], "already_running")

    def test_refresh_playlists_continues_when_some_provider_requests_fail(self):
        manager = self._manager()
        manager.config = {
            "enabled_features": {
                "auto_playlist_update": True,
                "changelog_tracking": False,
            },
            "enabled_m3u_accounts": [],
        }
        events = []
        scheduling_service = Mock()
        success_response = Mock(status_code=200)
        failed_response = Mock(status_code=500)

        with patch(
            "apps.automation.automated_stream_manager.get_m3u_accounts",
            return_value=[
                {"id": 1, "name": "One", "is_active": True},
                {"id": 2, "name": "Two", "is_active": True},
            ],
        ), patch(
            "apps.automation.automated_stream_manager.refresh_m3u_playlists",
            side_effect=[success_response, failed_response],
        ), patch(
            "apps.automation.scheduling_service.get_scheduling_service",
            return_value=scheduling_service,
        ):
            result = manager.refresh_playlists(
                skip_changelog=True,
                progress_callback=events.append,
            )
            success, accounts = result

        self.assertTrue(success)
        self.assertTrue(result.degraded)
        self.assertEqual(result.outcome, "completed_degraded")
        self.assertEqual(result.failed_refresh_request_count, 1)
        self.assertEqual([account["id"] for account in accounts], [1])
        self.assertEqual(events[-1]["state"], "partial")
        self.assertEqual(events[-1]["failed_refresh_requests"], 1)

    def test_m3u_refresh_wait_settles_after_stable_snapshot(self):
        manager = self._manager()
        manager.config = {
            "m3u_refresh_wait": {
                "timeout_seconds": 30,
                "poll_interval_seconds": 1,
                "stable_polls_required": 2,
                "min_wait_seconds": 0,
            },
        }
        events = []
        udi = Mock()
        udi.refresh_m3u_accounts.return_value = True
        udi.get_m3u_accounts.return_value = [{"id": 1, "name": "One", "status": "idle"}]
        udi.refresh_streams.side_effect = AssertionError("M3U refresh wait must not sync full stream cache")
        udi.get_streams.return_value = [{"id": 10}, {"id": 11}]

        with patch("apps.automation.automated_stream_manager.get_udi_manager", return_value=udi), patch.object(
            manager._manual_stop_requested,
            "wait",
            return_value=False,
        ) as wait_mock:
            result = manager._wait_for_m3u_refresh_completion([{"id": 1, "name": "One"}], events.append)

        self.assertTrue(result["ok"])
        self.assertEqual(result["state"], "settled")
        self.assertEqual(events[-1]["state"], "settled")
        self.assertEqual(events[-1]["wait_streams_seen"], 2)
        udi.refresh_streams.assert_not_called()
        wait_mock.assert_called_once_with(timeout=1)

    def test_m3u_refresh_wait_treats_parsing_account_as_busy(self):
        manager = self._manager()
        manager.config = {
            "m3u_refresh_wait": {
                "timeout_seconds": 30,
                "poll_interval_seconds": 1,
                "stable_polls_required": 1,
                "min_wait_seconds": 0,
            },
        }
        events = []
        udi = Mock()
        udi.refresh_m3u_accounts.return_value = True
        udi.get_m3u_accounts.side_effect = [
            [{"id": 1, "name": "One", "status": "parsing"}],
            [{"id": 1, "name": "One", "status": "success"}],
        ]
        udi.refresh_streams.side_effect = AssertionError("M3U refresh wait must not sync full stream cache")
        udi.get_streams.return_value = [{"id": 10}, {"id": 11}]

        with patch("apps.automation.automated_stream_manager.get_udi_manager", return_value=udi), patch.object(
            manager._manual_stop_requested,
            "wait",
            return_value=False,
        ) as wait_mock:
            result = manager._wait_for_m3u_refresh_completion([{"id": 1, "name": "One"}], events.append)

        self.assertTrue(result["ok"])
        self.assertEqual(result["state"], "settled")
        self.assertEqual(events[0]["wait_busy_accounts"], 1)
        self.assertEqual(events[0]["current"], 0)
        self.assertEqual(events[0]["total"], 1)
        self.assertIn("playlist parsing", events[0]["message"])
        self.assertEqual(events[-1]["state"], "settled")
        udi.refresh_streams.assert_not_called()
        wait_mock.assert_called_once_with(timeout=1)

    def test_m3u_refresh_wait_aborts_when_manual_stop_requested(self):
        manager = self._manager()
        manager.config = {
            "m3u_refresh_wait": {
                "timeout_seconds": 30,
                "poll_interval_seconds": 60,
                "stable_polls_required": 1,
                "min_wait_seconds": 0,
            },
        }
        events = []
        udi = Mock()
        udi.refresh_m3u_accounts.return_value = True
        udi.get_m3u_accounts.return_value = [{"id": 1, "name": "One", "status": "parsing"}]
        udi.refresh_streams.side_effect = AssertionError("M3U refresh wait must not sync full stream cache")
        udi.get_streams.return_value = [{"id": 10}]

        def record_and_stop(payload):
            events.append(payload)
            if payload.get("state") == "waiting":
                manager._manual_stop_requested.set()

        with patch("apps.automation.automated_stream_manager.get_udi_manager", return_value=udi):
            result = manager._wait_for_m3u_refresh_completion(
                [{"id": 1, "name": "One"}],
                record_and_stop,
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["state"], "aborted")
        self.assertEqual(result["message"], "Automation run was stopped by the user")
        self.assertEqual(events[-1]["state"], "aborted")
        self.assertTrue(manager._manual_stop_requested.is_set())
        udi.refresh_streams.assert_not_called()

    def test_stream_matching_parallel_loop_aborts_when_manual_stop_requested(self):
        manager = self._manager()
        manager.config = {
            "enabled_features": {"auto_stream_discovery": True},
            "enabled_m3u_accounts": [],
        }
        manager._lock = threading.Lock()
        manager._m3u_accounts_cache = [{"id": 1, "name": "Provider", "is_active": True}]
        manager.regex_matcher = Mock()
        manager.regex_matcher.reload_patterns.return_value = None
        manager.regex_matcher.has_regex_patterns.return_value = True
        manager.regex_matcher.get_match_by_tvg_id.return_value = False
        manager._filter_channels_by_profile = Mock(side_effect=lambda channels, _reason: channels)
        manager._record_channel_visibility_events = Mock()
        manager._is_dead_stream_removal_enabled = Mock(return_value=False)
        manager._update_run_progress = Mock()
        manager._get_channel_visibility_config = Mock(return_value={})

        streams = [{"id": i, "name": f"Stream {i}", "m3u_account": 1} for i in range(2000)]
        channels = [{"id": 10, "name": "Channel 10"}]
        automation_config = Mock()
        automation_config.get_effective_configuration.return_value = {
            "profile": {
                "stream_matching": {"enabled": True, "match_priority_order": ["regex"]},
                "stream_checking": {"enabled": False},
            }
        }
        udi = Mock()
        udi.get_channel_streams.return_value = []
        session_manager = Mock()
        session_manager.get_channels_in_active_sessions.return_value = []

        def stop_during_batch(*_args, **_kwargs):
            manager._manual_stop_requested.set()
            return {}, {}

        manager._match_streams_batch = Mock(side_effect=stop_during_batch)

        with patch("apps.automation.automated_stream_manager.get_streams", return_value=streams), \
             patch("apps.automation.automated_stream_manager.get_channels", return_value=channels), \
             patch("apps.automation.automated_stream_manager.get_udi_manager", return_value=udi), \
             patch("apps.automation.automated_stream_manager.get_automation_config_manager", return_value=automation_config), \
             patch("apps.stream.stream_session_manager.get_session_manager", return_value=session_manager):
            result = manager._discover_and_assign_streams_impl(force=True, skip_check_trigger=True)

        self.assertTrue(result["aborted"])
        self.assertFalse(result["success"])
        self.assertEqual(result["error"], "Automation run was stopped by the user")
        self.assertEqual(result["assignment_count"], {})
        self.assertTrue(manager._manual_stop_requested.is_set())

    def test_stream_validation_parallel_loop_aborts_when_manual_stop_requested(self):
        manager = self._manager()
        manager.config = {}
        manager._lock = threading.Lock()
        manager._filter_channels_by_profile = Mock(side_effect=lambda channels, _reason: channels)
        manager.changelog = Mock()

        channels = [{"id": i, "name": f"Channel {i}"} for i in range(60)]
        streams = [{"id": i, "name": f"Stream {i}"} for i in range(10)]
        udi = Mock()
        udi.get_channels.return_value = channels
        udi.get_streams.return_value = streams
        automation_config = Mock()
        automation_config.get_effective_configuration.return_value = {
            "profile": {
                "stream_matching": {
                    "enabled": True,
                    "validate_existing_streams": True,
                }
            }
        }

        def stop_during_batch(*_args, **_kwargs):
            manager._manual_stop_requested.set()
            return {"channels_checked": 0, "details": []}

        manager._validate_channels_batch = Mock(side_effect=stop_during_batch)

        with patch("apps.automation.automated_stream_manager.get_udi_manager", return_value=udi), \
             patch("apps.automation.automation_config_manager.get_automation_config_manager", return_value=automation_config):
            result = manager._validate_and_remove_non_matching_streams_impl(force=True)

        self.assertTrue(result["aborted"])
        self.assertFalse(result["success"])
        self.assertEqual(result["error"], "Automation run was stopped by the user")
        self.assertTrue(manager._manual_stop_requested.is_set())
        manager.changelog.add_entry.assert_not_called()

    def test_m3u_refresh_wait_reports_failed_account_status(self):
        manager = self._manager()
        manager.config = {"m3u_refresh_wait": {"stable_polls_required": 2, "min_wait_seconds": 0}}
        events = []
        udi = Mock()
        udi.refresh_m3u_accounts.return_value = True
        udi.get_m3u_accounts.return_value = [{"id": 1, "name": "One", "status": "failed"}]
        udi.refresh_streams.side_effect = AssertionError("M3U refresh wait must not sync full stream cache")
        udi.get_streams.return_value = [{"id": 10}]

        with patch("apps.automation.automated_stream_manager.get_udi_manager", return_value=udi):
            result = manager._wait_for_m3u_refresh_completion([{"id": 1, "name": "One"}], events.append)

        self.assertFalse(result["ok"])
        self.assertEqual(result["state"], "failed")
        self.assertEqual(events[-1]["state"], "waiting")
        self.assertEqual(events[-1]["wait_streams_seen"], 1)
        udi.refresh_streams.assert_not_called()

    def test_m3u_refresh_wait_continues_with_partial_failed_account_status(self):
        manager = self._manager()
        manager.config = {
            "m3u_refresh_wait": {
                "stable_polls_required": 1,
                "min_wait_seconds": 0,
                "retry_failed_providers": False,
            },
        }
        events = []
        udi = Mock()
        udi.refresh_m3u_accounts.return_value = True
        udi.get_m3u_accounts.return_value = [
            {"id": 1, "name": "One", "status": "failed"},
            {"id": 2, "name": "Two", "status": "idle"},
        ]
        udi.refresh_streams.side_effect = AssertionError("M3U refresh wait must not sync full stream cache")
        udi.get_streams.return_value = [{"id": 10}]

        with patch("apps.automation.automated_stream_manager.get_udi_manager", return_value=udi):
            result = manager._wait_for_m3u_refresh_completion(
                [{"id": 1, "name": "One"}, {"id": 2, "name": "Two"}],
                events.append,
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["state"], "partial")
        self.assertEqual(events[-1]["state"], "partial")
        self.assertEqual(events[-1]["wait_failed_accounts"], 1)
        udi.refresh_streams.assert_not_called()

    def test_m3u_refresh_wait_retries_failed_accounts_only_once(self):
        manager = self._manager()
        manager.config = {
            "m3u_refresh_wait": {
                "timeout_seconds": 30,
                "poll_interval_seconds": 1,
                "stable_polls_required": 1,
                "min_wait_seconds": 0,
                "retry_failed_providers": True,
            },
        }
        events = []
        udi = Mock()
        udi.refresh_m3u_accounts.return_value = True
        udi.get_m3u_accounts.side_effect = [
            [
                {"id": 1, "name": "One", "status": "failed"},
                {"id": 2, "name": "Two", "status": "idle"},
            ],
            [
                {"id": 1, "name": "One", "status": "idle"},
                {"id": 2, "name": "Two", "status": "idle"},
            ],
        ]
        udi.refresh_streams.side_effect = AssertionError("M3U refresh wait must not sync full stream cache")
        udi.get_streams.return_value = [{"id": 10}]
        response = Mock(status_code=200)

        with patch("apps.automation.automated_stream_manager.get_udi_manager", return_value=udi), patch(
            "apps.automation.automated_stream_manager.refresh_m3u_playlists",
            return_value=response,
        ) as refresh_mock, patch.object(
            manager._manual_stop_requested,
            "wait",
            return_value=False,
        ):
            result = manager._wait_for_m3u_refresh_completion(
                [{"id": 1, "name": "One"}, {"id": 2, "name": "Two"}],
                events.append,
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["state"], "settled")
        refresh_mock.assert_called_once_with(account_id=1)
        self.assertIn("retrying_failed", [event["state"] for event in events])
        udi.refresh_streams.assert_not_called()

    def test_cache_sync_reports_progress_before_long_fetch_completes(self):
        manager = self._manager()
        manager._start_run_status(forced=True)
        manager._update_run_status(
            stage="cache_sync",
            stage_label="Syncing Cache",
            message="Refreshing cache after playlist update",
        )

        udi = Mock()

        def refresh_streams(progress_callback=None):
            status = manager.get_run_status()
            self.assertEqual(status["current"], 1)
            self.assertEqual(status["total"], 100)
            self.assertEqual(status["percent"], 1)
            stages = {stage["key"]: stage for stage in status["stages"]}
            self.assertEqual(stages["cache_sync"]["percent"], 1)
            self.assertIsNotNone(progress_callback)
            progress_callback({
                "completed_pages": 2,
                "total_pages": 4,
                "items_fetched": 10000,
                "expected_count": 20000,
            })
            status = manager.get_run_status()
            self.assertEqual(status["current"], 40)
            self.assertEqual(status["total"], 100)
            self.assertEqual(status["percent"], 40)
            self.assertIn("2/4 pages", status["message"])
            return True

        def refresh_channels():
            status = manager.get_run_status()
            self.assertEqual(status["current"], 90)
            self.assertEqual(status["total"], 100)
            self.assertEqual(status["percent"], 90)
            return True

        udi.refresh_streams.side_effect = refresh_streams
        udi.refresh_channels.side_effect = refresh_channels

        self.assertTrue(manager._sync_udi_cache_after_playlist_refresh(udi))
        status = manager.get_run_status()
        self.assertEqual(status["current"], 100)
        self.assertEqual(status["percent"], 100)
        self.assertEqual(status["counts"]["cache_sync_successful_steps"], 2)
        self.assertEqual(status["counts"]["cache_sync_total_steps"], 2)


class FetcherTimingSummaryTests(unittest.TestCase):
    def test_api_timing_summary_is_sanitized_and_percentiled(self):
        fetcher = UDIFetcher.__new__(UDIFetcher)
        fetcher.base_url = "http://dispatcharr.local"
        fetcher._timing_lock = threading.Lock()
        fetcher._request_timings = []

        fetcher._record_request_timing(
            method="GET",
            url="http://dispatcharr.local/api/channels/streams/?page=1",
            elapsed=0.1,
            status_code=200,
            success=True,
        )
        fetcher._record_request_timing(
            method="GET",
            url="http://dispatcharr.local/api/channels/channels/?page=1",
            elapsed=0.3,
            status_code=200,
            success=True,
        )
        fetcher._record_request_timing(
            method="POST",
            url="http://dispatcharr.local/api/channels/streams/by-ids/",
            elapsed=0.5,
            status_code=500,
            success=False,
        )

        summary = fetcher.get_api_timing_summary()

        self.assertEqual(summary["sample_count"], 3)
        self.assertEqual(summary["failure_count"], 1)
        self.assertGreater(summary["p95_seconds"], 0.3)
        self.assertGreater(summary["p99_seconds"], summary["p95_seconds"])
        self.assertEqual(summary["slowest"][0]["path"], "/api/channels/streams/by-ids/")
        self.assertNotIn("dispatcharr.local", summary["slowest"][0]["path"])


class ChangelogV7SingleChannelTests(unittest.TestCase):
    def test_single_channel_changelog_copies_v7_run_context(self):
        manager = ChangelogManager("unused.json")
        manager.add_entry = Mock()
        run_snapshot = {
            "schema_version": 1,
            "run_mode": "single_channel_check",
            "start_source": "manual",
            "effective_profiles": [{"profile_name": "Single Profile"}],
            "quality_rules": [{"profile_name": "Single Profile", "enabled": True}],
            "capacity_profile_context": {"type": "provider_account_profiles"},
        }

        manager.add_single_channel_check_entry(
            channel_id=12,
            channel_name="Single Channel",
            check_stats={
                "total_streams": 3,
                "dead_streams": 1,
                "avg_resolution": "1920x1080",
                "avg_bitrate": "5000 kbps",
                "avg_fps": "50 fps",
                "duration_seconds": 17,
                "run_mode": "single_channel_check",
                "run_profile_name": "Single Profile",
                "run_profile_source": "forced",
                "quality_profile_name": "Single Profile",
                "quality_profile_source": "forced",
                "capacity_profile_name": "Provider account profiles",
                "capacity_profile_source": "m3u_account_profiles",
                "channels_hidden": 0,
                "channels_ready": 1,
                "channel_visibility_changed": 1,
                "run_snapshot": run_snapshot,
            },
        )

        manager.add_entry.assert_called_once()
        kwargs = manager.add_entry.call_args.kwargs
        details = kwargs["details"]
        self.assertEqual(kwargs["action"], "single_channel_check")
        self.assertEqual(details["run_mode"], "single_channel_check")
        self.assertEqual(details["run_profile_name"], "Single Profile")
        self.assertEqual(details["quality_profile_name"], "Single Profile")
        self.assertEqual(details["capacity_profile_source"], "m3u_account_profiles")
        self.assertEqual(details["channels_hidden"], 0)
        self.assertEqual(details["channels_ready"], 1)
        self.assertEqual(details["channel_visibility_changed"], 1)
        self.assertEqual(details["run_snapshot"], run_snapshot)


if __name__ == "__main__":
    unittest.main()
