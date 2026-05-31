"""Pytest configuration for backend tests.

Provides an autouse fixture that runs each test function against a fresh
in-memory SQLite database.  This avoids any leftover state between tests
and removes the need for a real on-disk DB file during the test suite.
"""
import builtins
import importlib
import os
from pathlib import Path
import sys

import pytest


def _install_legacy_module_alias(alias: str, target: str) -> None:
    """Expose old top-level module names used by legacy tests."""
    module = importlib.import_module(target)
    sys.modules.setdefault(alias, module)
    setattr(builtins, alias, module)


for _alias, _target in {
    'api_utils': 'apps.core.api_utils',
    'automated_stream_manager': 'apps.automation.automated_stream_manager',
    'automation_config_manager': 'apps.automation.automation_config_manager',
    'scheduling_service': 'apps.automation.scheduling_service',
    'stream_checker_service': 'apps.stream.stream_checker_service',
    'stream_monitoring_service': 'apps.stream.stream_monitoring_service',
    'stream_session_manager': 'apps.stream.stream_session_manager',
}.items():
    _install_legacy_module_alias(_alias, _target)


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

    channel_settings_file = Path(os.environ.get('CONFIG_DIR', '/app/data')) / 'channel_settings.json'
    channel_settings_file.unlink(missing_ok=True)

    yield test_engine

    # Cleanup
    mgr._db_manager = None
    Base.metadata.drop_all(test_engine)
