import threading
from datetime import datetime, timedelta

from apps.udi.manager import UDIManager


def _busy_guard_manager(timeout_seconds=24 * 60 * 60):
    manager = UDIManager.__new__(UDIManager)
    manager._automation_busy = False
    manager._automation_busy_count = 0
    manager._automation_busy_lock = threading.Lock()
    manager._automation_busy_since = None
    manager._automation_busy_timeout_seconds = timeout_seconds
    return manager


def test_automation_busy_guard_is_reference_counted():
    manager = _busy_guard_manager()

    manager.set_automation_busy()
    manager.set_automation_busy()

    assert manager.is_automation_busy() is True

    manager.clear_automation_busy()

    assert manager.is_automation_busy() is True

    manager.clear_automation_busy()

    assert manager.is_automation_busy() is False


def test_automation_busy_guard_timeout_is_long_enough_for_full_runs():
    manager = UDIManager()

    assert manager._automation_busy_timeout_seconds >= 24 * 60 * 60


def test_automation_busy_guard_still_auto_clears_after_configured_timeout():
    manager = _busy_guard_manager(timeout_seconds=60)

    manager.set_automation_busy()
    manager._automation_busy_since = datetime.now() - timedelta(seconds=61)

    assert manager.is_automation_busy() is False
    assert manager._automation_busy_count == 0
