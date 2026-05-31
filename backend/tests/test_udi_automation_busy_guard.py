import threading

from apps.udi.manager import UDIManager


def _busy_guard_manager():
    manager = UDIManager.__new__(UDIManager)
    manager._automation_busy = False
    manager._automation_busy_count = 0
    manager._automation_busy_lock = threading.Lock()
    manager._automation_busy_since = None
    manager._automation_busy_timeout_seconds = 3600
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
