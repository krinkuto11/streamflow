"""Tests for StreamCheckerService._stream_cache_reusable (stream_cache feature)."""

import unittest
from pathlib import Path
from unittest.mock import patch, Mock

from apps.stream.stream_checker_service import StreamCheckerService


class TestStreamCacheReusable(unittest.TestCase):
    """Verify the reuse gate returns the right verdict for each stats shape."""

    def setUp(self):
        self.service = StreamCheckerService()
        # Exercise the enabled path and a non-default TTL to confirm the value
        # is plumbed through to the telemetry lookup.
        self.service.config.update({"stream_cache": {"enabled": True, "ttl_hours": 48}})

    def _stream(self):
        return {"id": 404, "name": "Test", "url": "http://example.test/s.m3u8"}

    @patch("apps.stream.stream_checker_service.get_last_quality_stats")
    def test_fresh_measurement_is_reusable(self, mock_lqs):
        mock_lqs.return_value = {
            "measured": True,
            "recheck_required": False,
            "stale": False,
        }
        self.assertTrue(self.service._stream_cache_reusable(self._stream()))
        mock_lqs.assert_called_once_with(stream_id=404, stale_after_hours=48)

    @patch("apps.stream.stream_checker_service.get_last_quality_stats")
    def test_stale_measurement_is_not_reusable(self, mock_lqs):
        mock_lqs.return_value = {
            "measured": True,
            "recheck_required": False,
            "stale": True,
        }
        self.assertFalse(self.service._stream_cache_reusable(self._stream()))

    @patch("apps.stream.stream_checker_service.get_last_quality_stats")
    def test_never_measured_is_not_reusable(self, mock_lqs):
        mock_lqs.return_value = {
            "measured": False,
            "recheck_required": True,
            "stale": False,
        }
        self.assertFalse(self.service._stream_cache_reusable(self._stream()))

    @patch("apps.stream.stream_checker_service.get_last_quality_stats")
    def test_unusable_last_result_is_not_reusable(self, mock_lqs):
        mock_lqs.return_value = {
            "measured": False,
            "recheck_required": True,
            "stale": False,
            "reason": "last_result_not_reusable",
        }
        self.assertFalse(self.service._stream_cache_reusable(self._stream()))

    @patch("apps.stream.stream_checker_service.get_last_quality_stats")
    def test_lookup_error_is_not_reusable(self, mock_lqs):
        mock_lqs.side_effect = RuntimeError("boom")
        self.assertFalse(self.service._stream_cache_reusable(self._stream()))

    @patch("apps.stream.stream_checker_service.get_last_quality_stats")
    def test_non_integer_stream_id_is_not_reusable(self, mock_lqs):
        self.assertFalse(
            self.service._stream_cache_reusable({"id": None, "name": "x"})
        )
        mock_lqs.assert_not_called()


if __name__ == "__main__":
    unittest.main()