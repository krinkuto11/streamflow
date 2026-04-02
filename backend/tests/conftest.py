"""Pytest configuration for backend tests.

Provides an autouse fixture that runs each test function against a fresh
in-memory SQLite database.  This avoids any leftover state between tests
and removes the need for a real on-disk DB file during the test suite.
"""
import pytest


@pytest.fixture(autouse=True, scope='function')
def clean_test_db(monkeypatch):
    """Each test function gets its own in-memory SQLite database.

    The fixture patches ``database.connection.get_engine`` and
    ``database.connection.get_session`` so every DB access within the test
    (including accesses from ``DatabaseManager``) targets an isolated
    in-memory engine.  It also resets the ``DatabaseManager`` singleton so
    a fresh instance is created for the test.
    """
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    import apps.database.connection as conn
    import apps.database.manager as mgr

    # Create a brand-new in-memory engine for this test
    test_engine = create_engine('sqlite:///:memory:', echo=False)
    TestSession = sessionmaker(bind=test_engine)

    monkeypatch.setattr(conn, 'get_engine', lambda: test_engine)
    monkeypatch.setattr(conn, 'get_session', lambda: TestSession())

    # Reset the singleton so the next get_db_manager() call creates a fresh
    # instance that uses the patched session factory.
    mgr._db_manager = None

    # Create all tables
    from apps.database.connection import Base
    import apps.database.models  # noqa: F401 – registers all models with Base
    Base.metadata.create_all(test_engine)

    yield test_engine

    # Cleanup
    mgr._db_manager = None
    Base.metadata.drop_all(test_engine)
