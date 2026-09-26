"""Monitoring interval values from the UI must reach created sessions."""

from unittest.mock import MagicMock

from flask import Flask

from apps.api.stream_sessions_handlers import (
    create_group_stream_sessions_response,
    create_stream_session_response,
)


def test_single_session_intervals_reach_manager():
    manager = MagicMock()
    manager.create_session.return_value = 'session-9'
    app = Flask(__name__)
    with app.app_context():
        response, status = create_stream_session_response(
            payload={
                'channel_id': '9',
                'regex_filter': '.*',
                'evaluation_interval_ms': 2400,
                'enforce_sync_interval_ms': 3500,
            },
            get_session_manager=lambda: manager,
            get_regex_matcher=MagicMock(),
        )
    assert status == 201
    assert response.get_json()['session_id'] == 'session-9'
    assert manager.create_session.call_args.kwargs['evaluation_interval_ms'] == 2400
    assert manager.create_session.call_args.kwargs['enforce_sync_interval_ms'] == 3500


def test_group_session_intervals_reach_each_session():
    manager = MagicMock()
    manager.create_session.side_effect = ['session-9', 'session-10']
    manager.start_session.return_value = True
    udi = MagicMock()
    udi.get_channels_by_group.return_value = [
        {'id': 9, 'name': 'Channel 9'},
        {'id': 10, 'name': 'Channel 10'},
    ]
    app = Flask(__name__)
    with app.app_context():
        response, status = create_group_stream_sessions_response(
            payload={
                'group_id': 5,
                'regex_filter': '.*',
                'evaluation_interval_ms': 2200,
                'enforce_sync_interval_ms': 3100,
            },
            get_udi_manager=lambda: udi,
            get_session_manager=lambda: manager,
            get_monitoring_service=lambda: MagicMock(_running=True),
            get_regex_matcher=MagicMock(),
        )
    assert status == 200
    assert len(response.get_json()['sessions']) == 2
    assert manager.create_session.call_count == 2
    for call in manager.create_session.call_args_list:
        assert call.kwargs['evaluation_interval_ms'] == 2200
        assert call.kwargs['enforce_sync_interval_ms'] == 3100


def test_invalid_sync_interval_is_rejected():
    manager = MagicMock()
    app = Flask(__name__)
    with app.app_context():
        _response, status = create_stream_session_response(
            payload={'channel_id': 9, 'regex_filter': '.*', 'enforce_sync_interval_ms': 0},
            get_session_manager=lambda: manager,
            get_regex_matcher=MagicMock(),
        )
    assert status == 400
    manager.create_session.assert_not_called()
