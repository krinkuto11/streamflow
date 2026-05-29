#!/usr/bin/env python3
"""Tests for manual full-check queue start selection."""

import os
import sys
import tempfile
import unittest

os.environ["CONFIG_DIR"] = tempfile.mkdtemp()
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from apps.api.stream_checker_handlers import order_channels_for_queue_start


class TestQueueStartSelection(unittest.TestCase):
    def setUp(self):
        self.channels = [
            {"id": 10, "name": "Alpha"},
            {"id": 20, "name": "Beta"},
            {"id": 30, "name": "Gamma"},
            {"id": 40, "name": "Delta"},
        ]

    def test_first_start_preserves_channel_order(self):
        ordered, meta = order_channels_for_queue_start(
            self.channels,
            start_mode="first",
        )

        self.assertEqual([channel["id"] for channel in ordered], [10, 20, 30, 40])
        self.assertEqual(meta["start_channel_id"], 10)
        self.assertEqual(meta["start_channel_name"], "Alpha")

    def test_first_start_uses_channel_number_order_when_available(self):
        channels = [
            {"id": 40, "name": "Delta", "channel_number": 4.0},
            {"id": 10, "name": "Alpha", "channel_number": 1.0},
            {"id": 30, "name": "Gamma", "channel_number": 3.0},
            {"id": 20, "name": "Beta", "channel_number": 2.0},
        ]

        ordered, meta = order_channels_for_queue_start(
            channels,
            start_mode="first",
        )

        self.assertEqual([channel["id"] for channel in ordered], [10, 20, 30, 40])
        self.assertEqual(meta["start_channel_id"], 10)

    def test_last_start_uses_reverse_channel_number_order_when_available(self):
        channels = [
            {"id": 40, "name": "Delta", "channel_number": 4.0},
            {"id": 10, "name": "Alpha", "channel_number": 1.0},
            {"id": 30, "name": "Gamma", "channel_number": 3.0},
            {"id": 20, "name": "Beta", "channel_number": 2.0},
        ]

        ordered, meta = order_channels_for_queue_start(
            channels,
            start_mode="last",
        )

        self.assertEqual([channel["id"] for channel in ordered], [40, 30, 20, 10])
        self.assertEqual(meta["start_channel_id"], 40)

    def test_last_start_reverses_channel_order(self):
        ordered, meta = order_channels_for_queue_start(
            self.channels,
            start_mode="last",
        )

        self.assertEqual([channel["id"] for channel in ordered], [40, 30, 20, 10])
        self.assertEqual(meta["start_channel_id"], 40)
        self.assertEqual(meta["start_channel_name"], "Delta")

    def test_selected_channel_rotates_without_dropping_channels(self):
        ordered, meta = order_channels_for_queue_start(
            self.channels,
            start_mode="channel",
            start_channel_id=30,
        )

        self.assertEqual([channel["id"] for channel in ordered], [30, 40, 10, 20])
        self.assertEqual(meta["start_channel_id"], 30)
        self.assertEqual(meta["requested_channel_id"], 30)

    def test_selected_channel_requires_existing_channel(self):
        with self.assertRaises(ValueError):
            order_channels_for_queue_start(
                self.channels,
                start_mode="channel",
                start_channel_id=999,
            )

    def test_invalid_start_mode_is_rejected(self):
        with self.assertRaises(ValueError):
            order_channels_for_queue_start(self.channels, start_mode="random")


if __name__ == "__main__":
    unittest.main()
