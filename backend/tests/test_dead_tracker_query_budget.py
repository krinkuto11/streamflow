"""Query budgets for dead-stream lookups in matching and channel writes."""

import pytest
from sqlalchemy import event

from apps.core.api_utils import filter_dead_streams
from apps.database.manager import get_db_manager
from apps.stream.dead_streams_tracker import DeadStreamsTracker


def _count_dead_stream_selects(engine):
    statements = []

    def record(_connection, _cursor, statement, _parameters, _context, _executemany):
        if statement.lstrip().upper().startswith("SELECT") and "dead_streams" in statement:
            statements.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    return statements, record


def test_filter_dead_streams_loads_one_snapshot_per_filter(clean_test_db):
    db = get_db_manager()
    db.mark_stream_dead("http://example.test/offline", 1, "Offline", reason="offline")
    db.mark_stream_dead("http://example.test/quality", 2, "Quality", reason="low_quality")
    urls = {
        1: "http://example.test/offline",
        2: "http://example.test/quality",
        3: "http://example.test/healthy",
    }

    statements, record = _count_dead_stream_selects(clean_test_db)
    try:
        assert filter_dead_streams([1, 2, 3], urls) == ([2, 3], 1)
        assert len(statements) == 1
        assert "WHERE dead_streams.url IN" in statements[0]
        statements.clear()
        assert filter_dead_streams([1, 2, 3], urls, only_offline=False) == ([3], 2)
        assert len(statements) == 1
        assert "WHERE dead_streams.url IN" in statements[0]
    finally:
        event.remove(clean_test_db, "before_cursor_execute", record)


def test_dead_reason_reads_only_requested_row(clean_test_db):
    db = get_db_manager()
    db.mark_stream_dead("http://example.test/offline", 1, "Offline", reason="offline")
    db.mark_stream_dead("http://example.test/quality", 2, "Quality", reason="low_quality")
    tracker = DeadStreamsTracker()

    statements, record = _count_dead_stream_selects(clean_test_db)
    try:
        assert tracker.get_dead_reason("http://example.test/offline") == "offline"
        assert tracker.get_dead_reason("http://example.test/healthy") is None
        assert len(statements) == 2
        assert all("WHERE dead_streams.url" in statement for statement in statements)
    finally:
        event.remove(clean_test_db, "before_cursor_execute", record)


def test_revival_does_not_report_success_when_delete_fails(clean_test_db, monkeypatch):
    db = get_db_manager()
    url = "http://example.test/revive"
    db.mark_stream_dead(url, 11, "Revive", reason="offline")
    tracker = DeadStreamsTracker()
    monkeypatch.setattr(db, "remove_dead_stream", lambda _url: False)

    statements, record = _count_dead_stream_selects(clean_test_db)
    try:
        assert tracker.mark_as_alive(url) is False
        assert len(statements) == 1
        assert "WHERE dead_streams.url" in statements[0]
    finally:
        event.remove(clean_test_db, "before_cursor_execute", record)

    assert tracker.is_dead(url) is True


def test_filter_refuses_write_if_snapshot_fails(clean_test_db, monkeypatch):
    db = get_db_manager()
    db.mark_stream_dead("http://example.test/offline", 1, "Offline", reason="offline")
    db.mark_stream_dead("http://example.test/quality", 2, "Quality", reason="low_quality")

    def fail_snapshot(_tracker, _urls):
        raise RuntimeError("injected snapshot failure")

    monkeypatch.setattr(DeadStreamsTracker, "get_dead_stream_reasons", fail_snapshot)
    urls = {1: "http://example.test/offline", 2: "http://example.test/quality"}
    for only_offline in (True, False):
        with pytest.raises(RuntimeError, match="Dead stream snapshot unavailable"):
            filter_dead_streams([1, 2], urls, only_offline=only_offline)
