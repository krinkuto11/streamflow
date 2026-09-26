"""Session detail polls should use memory and bound historical SQL results."""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from flask import Flask
from sqlalchemy import event

from apps.api.stream_sessions_handlers import get_stream_session_response
from apps.database.connection import get_session
from apps.database.models import Run, StreamTelemetry
from apps.stream.stream_session_manager import SessionInfo, StreamInfo, StreamMetrics


def _session(created_at, stream_ids):
    return SessionInfo(
        session_id='session-9', channel_id=9, channel_name='Channel 9',
        regex_filter='.*', created_at=created_at, is_active=True,
        streams={
            stream_id: StreamInfo(
                stream_id=stream_id,
                url=f'http://example.invalid/{stream_id}',
                name=f'Source {stream_id}', channel_id=9, status='stable',
            )
            for stream_id in stream_ids
        },
    )


def _response(session, since_timestamp=None):
    manager = MagicMock()
    manager.get_session.return_value = session
    udi = MagicMock()
    udi.get_channel_by_id.return_value = {'streams': list(session.streams)}
    app = Flask(__name__)
    with app.app_context():
        response, status = get_stream_session_response(
            session_id=session.session_id,
            since_timestamp=since_timestamp,
            get_session_manager=lambda: manager,
            get_udi_manager=lambda: udi,
        )
        return response.get_json(), status


def test_active_stream_history_avoids_telemetry_database(clean_test_db):
    now = datetime.now(timezone.utc).timestamp()
    session = _session(now - 60, [101])
    session.streams[101].metrics_history.append(StreamMetrics(
        timestamp=now, speed=1.0, bitrate=2500, fps=25.0, is_alive=True,
    ))
    with patch('apps.database.connection.get_session', side_effect=AssertionError('unexpected DB read')):
        data, status = _response(session)
    assert status == 200
    assert data['streams'][0]['metrics_count'] == 1
    assert data['streams'][0]['metrics_history'][0]['bitrate'] == 2500


def test_missing_history_applies_sql_cursor_and_per_stream_window(clean_test_db):
    base = datetime(2026, 9, 23, 8, 0, 0, tzinfo=timezone.utc)
    session = _session(base.timestamp() - 1, [101, 102])
    db = get_session()
    try:
        runs = []
        telemetry = []
        for index in range(3605):
            run_id = index + 1
            runs.append(Run(id=run_id, timestamp=(base + timedelta(seconds=index)).replace(tzinfo=None)))
            telemetry.append(StreamTelemetry(
                run_id=run_id, channel_id=9, stream_id=101, bitrate_kbps=index,
            ))
        telemetry.append(StreamTelemetry(
            run_id=11, channel_id=9, stream_id=102, bitrate_kbps=9000,
        ))
        telemetry.append(StreamTelemetry(
            run_id=11, channel_id=99, stream_id=103, bitrate_kbps=9999,
        ))
        db.add_all(runs)
        db.add_all(telemetry)
        db.commit()
    finally:
        db.close()

    statements = []
    def record(_connection, _cursor, statement, _parameters, _context, _executemany):
        if statement.lstrip().upper().startswith('SELECT'):
            statements.append(statement)

    event.listen(clean_test_db, 'before_cursor_execute', record)
    try:
        data, status = _response(session, since_timestamp=(base + timedelta(seconds=2)).timestamp())
    finally:
        event.remove(clean_test_db, 'before_cursor_execute', record)

    assert status == 200
    by_id = {stream['stream_id']: stream for stream in data['streams']}
    assert len(by_id[101]['metrics_history']) == 3600
    assert by_id[101]['metrics_history'][0]['timestamp'] == (base + timedelta(seconds=5)).timestamp()
    assert len(by_id[102]['metrics_history']) == 1
    assert by_id[102]['metrics_history'][0]['bitrate'] == 9000
    assert len(statements) == 1
    assert 'row_number() OVER' in statements[0]
