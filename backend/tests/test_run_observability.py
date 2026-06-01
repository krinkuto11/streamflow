from datetime import datetime, timedelta
import threading
import unittest

from apps.automation.automated_stream_manager import AutomatedStreamManager
from apps.udi.fetcher import UDIFetcher


class AutomationRunStatusTests(unittest.TestCase):
    def _manager(self):
        manager = AutomatedStreamManager.__new__(AutomatedStreamManager)
        manager._run_status_lock = threading.RLock()
        manager._run_sequence = 0
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


if __name__ == "__main__":
    unittest.main()
