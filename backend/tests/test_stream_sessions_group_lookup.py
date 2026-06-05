#!/usr/bin/env python3
"""Tests for group stream session creation handler group-id lookup behavior."""

import unittest
from unittest.mock import Mock

from flask import Flask

from apps.api.stream_sessions_handlers import create_group_stream_sessions_response


class TestCreateGroupStreamSessionsGroupLookup(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)

    def test_accepts_string_group_id_and_uses_integer_lookup(self):
        udi = Mock()
        udi.get_channels_by_group.return_value = [
            {"id": 10, "name": "Channel 10", "group_id": 2}
        ]
        udi.refresh_streams.return_value = True

        session_manager = Mock()
        session_manager.create_session.return_value = "sess-1"
        session_manager.start_session.return_value = True

        monitoring_service = Mock()
        monitoring_service._running = False

        regex_matcher = Mock()

        payload = {
            "group_id": "2",
            "regex_filter": ".*",
            "pre_event_minutes": 30,
            "stagger_ms": 200,
            "timeout_ms": 30000,
            "enable_looping_detection": True,
            "enable_logo_detection": True,
        }

        with self.app.app_context():
            response, status_code = create_group_stream_sessions_response(
                payload=payload,
                get_udi_manager=lambda: udi,
                get_session_manager=lambda: session_manager,
                get_monitoring_service=lambda: monitoring_service,
                get_regex_matcher=lambda: regex_matcher,
            )

        self.assertEqual(status_code, 200)
        udi.get_channels_by_group.assert_called_once_with(2)
        body = response.get_json()
        self.assertEqual(body.get("group_id"), 2)
        self.assertEqual(len(body.get("sessions", [])), 1)

    def test_rejects_non_integer_group_id(self):
        payload = {
            "group_id": "sports",
            "regex_filter": ".*",
        }

        with self.app.app_context():
            response, status_code = create_group_stream_sessions_response(
                payload=payload,
                get_udi_manager=lambda: Mock(),
                get_session_manager=lambda: Mock(),
                get_monitoring_service=lambda: Mock(),
                get_regex_matcher=lambda: Mock(),
            )

        self.assertEqual(status_code, 400)
        body = response.get_json()
        self.assertEqual(body.get("error"), "group_id must be an integer")


if __name__ == "__main__":
    unittest.main(verbosity=2)
