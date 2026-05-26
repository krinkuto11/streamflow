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

    def test_run_status_returns_copy(self):
        manager = self._manager()
        status = manager.get_run_status()
        status["counts"]["channels_with_periods"] = 99

        self.assertNotIn("channels_with_periods", manager.get_run_status()["counts"])

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
