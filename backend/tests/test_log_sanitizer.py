#!/usr/bin/env python3
"""Regression tests for operational log redaction helpers."""

import os
import sys
import unittest
from unittest.mock import patch


sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from apps.core.log_sanitizer import audit_ref, scrub_urls, stream_context, stream_ref
from apps.stream.dead_streams_tracker import DeadStreamsTracker
from apps.stream.ffmpeg_stream_monitor import FFmpegStreamMonitor


class FakeDeadStreamDb:
    def __init__(self):
        self.dead_streams = {}

    def mark_stream_dead(self, stream_url, stream_id, stream_name, channel_id, reason):
        self.dead_streams[stream_url] = {
            "stream_id": stream_id,
            "stream_name": stream_name,
            "channel_id": channel_id,
            "reason": reason,
        }
        return True

    def get_dead_streams(self, as_dict=True):
        return self.dead_streams

    def remove_dead_stream(self, stream_url):
        self.dead_streams.pop(stream_url, None)


class LogSanitizerTests(unittest.TestCase):
    def test_audit_refs_are_stable_and_do_not_include_raw_values(self):
        raw_value = "http://provider.example/live/user/password/123.ts?token=secret"

        first = audit_ref("url", raw_value)
        second = audit_ref("url", raw_value)

        self.assertEqual(first, second)
        self.assertTrue(first.startswith("url-"))
        self.assertNotIn("provider.example", first)
        self.assertNotIn("token=secret", first)

    def test_scrub_urls_replaces_raw_urls_with_refs(self):
        raw_url = "http://provider.example/live/user/password/123.ts?token=secret"

        message = scrub_urls(f"ffmpeg failed while opening {raw_url}")

        self.assertNotIn(raw_url, message)
        self.assertNotIn("provider.example", message)
        self.assertIn("<url-", message)

    def test_stream_context_uses_refs_only(self):
        raw_url = "http://provider.example/live/user/password/123.ts?token=secret"

        context = stream_context(
            stream_id=123,
            stream_url=raw_url,
            channel_id=456,
            reason="offline",
        )

        self.assertIn("stream_ref=stream-", context)
        self.assertIn("channel_ref=channel-", context)
        self.assertIn("reason=offline", context)
        self.assertNotIn(raw_url, context)
        self.assertNotIn("provider.example", context)

    def test_debug_mode_keeps_raw_stream_identifiers_visible(self):
        raw_url = "http://provider.example/live/user/password/123.ts?token=secret"

        with patch.dict(os.environ, {"DEBUG_MODE": "true"}):
            self.assertEqual(
                scrub_urls(f"ffmpeg failed while opening {raw_url}"),
                f"ffmpeg failed while opening {raw_url}",
            )
            self.assertIn("stream-123", stream_ref(123, raw_url))

            context = stream_context(
                stream_id=123,
                stream_url=raw_url,
                channel_id=456,
                reason="offline",
            )

        self.assertIn("stream_ref=stream-123", context)
        self.assertIn("channel_ref=channel-456", context)
        default_context = stream_context(stream_id=123, stream_url=raw_url)
        self.assertNotIn("stream_ref=stream-123", default_context)
        self.assertNotIn(raw_url, default_context)

    def test_dead_stream_tracker_logs_refs_without_names_or_urls(self):
        raw_url = "http://provider.example/live/user/password/123.ts?token=secret"
        raw_name = "Private Provider UHD Secret Channel"
        tracker = DeadStreamsTracker.__new__(DeadStreamsTracker)
        tracker.db = FakeDeadStreamDb()

        with self.assertLogs("apps.stream.dead_streams_tracker", level="INFO") as captured:
            tracker.mark_as_dead(raw_url, 123, raw_name, channel_id=456, reason="offline")
            tracker.mark_as_alive(raw_url)

        logs = "\n".join(captured.output)
        self.assertIn("stream-", logs)
        self.assertIn("channel-", logs)
        self.assertNotIn(raw_url, logs)
        self.assertNotIn(raw_name, logs)
        self.assertNotIn("provider.example", logs)

    def test_ffmpeg_url_validation_log_uses_url_ref(self):
        raw_url = "http://provider.example/live/user/password/123.ts?token=secret;rm"
        monitor = FFmpegStreamMonitor.__new__(FFmpegStreamMonitor)

        with self.assertLogs("apps.stream.ffmpeg_stream_monitor", level="WARNING") as captured:
            self.assertFalse(monitor._validate_url(raw_url))

        logs = "\n".join(captured.output)
        self.assertIn("url-", logs)
        self.assertNotIn(raw_url, logs)
        self.assertNotIn("provider.example", logs)


if __name__ == "__main__":
    unittest.main()
