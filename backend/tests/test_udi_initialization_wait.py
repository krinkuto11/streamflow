import threading
import time

from apps.udi.manager import UDIManager


class _ConfiguredDispatcharr:
    def is_configured(self):
        return True


def test_initialize_waits_for_concurrent_refresh(monkeypatch):
    manager = UDIManager()

    monkeypatch.setattr(
        "apps.udi.manager.get_dispatcharr_config",
        lambda: _ConfiguredDispatcharr(),
    )

    def slow_refresh():
        time.sleep(0.2)
        manager._update_init_progress(
            status="completed",
            percentage=100,
            message="Initialization complete",
            current_step="done",
        )
        manager._network_ready = True
        return True

    monkeypatch.setattr(manager, "refresh_all", slow_refresh)

    first_result = []
    first = threading.Thread(
        target=lambda: first_result.append(manager.initialize(force_refresh=True))
    )
    first.start()

    deadline = time.time() + 2
    while time.time() < deadline:
        with manager._lock:
            if manager._init_in_progress:
                break
        time.sleep(0.01)

    started = time.time()
    second_result = manager.initialize(force_refresh=True)
    elapsed = time.time() - started
    first.join(timeout=2)

    assert first_result == [True]
    assert second_result is True
    assert elapsed >= 0.15
    assert manager.is_network_ready() is True
