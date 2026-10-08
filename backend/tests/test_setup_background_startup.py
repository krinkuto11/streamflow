"""Regression coverage for first setup, recovery and concurrent worker starts."""

from concurrent.futures import ThreadPoolExecutor
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from apps.background.setup_startup import initialize_setup_data, start_setup_processors


@pytest.mark.parametrize("success,network_ready", [(False, False), (True, False), (True, True)])
def test_setup_starts_workers_only_after_successful_live_initialization(success, network_ready):
    udi = Mock()
    udi.initialize.return_value = success
    udi.is_network_ready.return_value = network_ready
    on_initialized = Mock()

    assert initialize_setup_data(udi=udi, force_refresh=True, on_initialized=on_initialized) is success
    udi.initialize.assert_called_once_with(force_refresh=True)
    assert on_initialized.call_count == int(success and network_ready)


def test_failed_initialization_can_be_retried_without_restarting():
    udi = Mock()
    udi.initialize.side_effect = [ConnectionError("offline"), True]
    udi.is_network_ready.return_value = True
    on_initialized = Mock()

    assert initialize_setup_data(udi=udi, force_refresh=True, on_initialized=on_initialized) is False
    on_initialized.assert_not_called()
    assert initialize_setup_data(udi=udi, force_refresh=False, on_initialized=on_initialized) is True
    on_initialized.assert_called_once_with()


def test_required_workers_are_not_started_with_missing_configuration():
    starts = [Mock(), Mock(), Mock()]
    start_setup_processors(
        is_configured=lambda: False,
        start_scheduled_events=starts[0], start_epg_refresh=starts[1], start_udi_refresh=starts[2],
    )
    assert all(start.call_count == 0 for start in starts)


def test_worker_start_failure_does_not_prevent_other_required_workers():
    starts = [Mock(side_effect=RuntimeError("start failed")), Mock(), Mock()]
    start_setup_processors(
        is_configured=lambda: True,
        start_scheduled_events=starts[0], start_epg_refresh=starts[1], start_udi_refresh=starts[2],
    )
    assert all(start.call_count == 1 for start in starts)


def test_config_save_initializes_in_background_then_calls_the_setup_hook(monkeypatch):
    from apps.api import web_api

    entered, release, completed = threading.Event(), threading.Event(), threading.Event()
    config = Mock()
    config.is_configured.return_value = True
    config.update_config.return_value = True
    udi = Mock()
    udi.is_network_ready.return_value = True

    def initialize(**kwargs):
        entered.set()
        assert release.wait(2)
        return True

    udi.initialize.side_effect = initialize
    hook = Mock(side_effect=completed.set)
    monkeypatch.setattr(web_api, "get_dispatcharr_config", lambda: config)
    monkeypatch.setattr(web_api, "get_udi_manager", lambda: udi)
    monkeypatch.setattr(web_api, "start_setup_background_processors", hook)
    try:
        with web_api.app.test_client() as client:
            response = client.put("/api/dispatcharr/config", json={"base_url": "http://dispatcharr.test"})
        assert response.status_code == 200
        assert entered.wait(1)
        hook.assert_not_called()
        release.set()
        assert completed.wait(2)
        udi.initialize.assert_called_once_with(force_refresh=True)
        hook.assert_called_once_with()
    finally:
        release.set()


def test_wizard_recovery_initialization_starts_required_workers(monkeypatch):
    from apps.api import web_api

    completed = threading.Event()
    config = Mock()
    config.is_configured.return_value = True
    udi = Mock()
    udi.is_initialized.return_value = False
    udi.is_network_ready.return_value = True
    udi.initialize.return_value = True
    udi.fetcher.test_connection.return_value = True
    udi._channels_cache = []
    hook = Mock(side_effect=completed.set)
    monkeypatch.setenv("TEST_MODE", "false")
    monkeypatch.setattr(web_api, "get_dispatcharr_config", lambda: config)
    monkeypatch.setattr(web_api, "get_udi_manager", lambda: udi)
    monkeypatch.setattr(web_api, "start_setup_background_processors", hook)

    with web_api.app.test_client() as client:
        response = client.get("/api/setup-wizard")
    assert response.status_code == 200
    assert completed.wait(2)
    udi.initialize.assert_called_once_with(force_refresh=False)
    hook.assert_called_once_with()


def test_concurrent_setup_and_direct_starts_keep_one_thread_per_worker(monkeypatch):
    from apps.api import web_api

    created = []

    def new_thread(*args, **kwargs):
        # Widen the alive-check/creation race that exists without serialization.
        time.sleep(0.01)
        thread = threading.Thread(*args, **kwargs)
        created.append(thread)
        return thread

    monkeypatch.setattr(web_api, "threading", SimpleNamespace(Event=threading.Event, Thread=new_thread))
    monkeypatch.setattr(web_api, "check_wizard_complete", lambda: True)
    for attr in ("epg_refresh_thread", "udi_refresh_thread", "epg_refresh_wake", "udi_refresh_wake", "scheduled_event_processor_wake"):
        monkeypatch.setattr(web_api, attr, None)
    monkeypatch.setattr(web_api, "scheduled_event_processor_thread", web_api._ThreadHandleProxy())
    for attr in ("epg_refresh_running", "udi_refresh_running", "scheduled_event_processor_running"):
        monkeypatch.setattr(web_api, attr, False)

    def worker(running, wake):
        while getattr(web_api, running):
            getattr(web_api, wake).wait(0.01)

    for target, running, wake in (
        ("epg_refresh_processor", "epg_refresh_running", "epg_refresh_wake"),
        ("udi_refresh_processor", "udi_refresh_running", "udi_refresh_wake"),
        ("scheduled_event_processor", "scheduled_event_processor_running", "scheduled_event_processor_wake"),
    ):
        monkeypatch.setattr(web_api, target, lambda r=running, w=wake: worker(r, w))
    optional_start = Mock(side_effect=AssertionError("optional automation must not start"))
    monkeypatch.setattr(web_api, "get_automation_manager", optional_start)
    monkeypatch.setattr(web_api, "get_stream_checker_service", optional_start)
    try:
        tasks = [web_api.start_setup_background_processors] * 8 + [
            web_api.start_epg_refresh_processor, web_api.start_udi_refresh_processor,
            web_api.start_scheduled_event_processor,
        ]
        with ThreadPoolExecutor(max_workers=11) as executor:
            futures = [executor.submit(task) for task in tasks]
            for future in futures:
                future.result(timeout=3)
        assert sorted(thread.name for thread in created) == [
            "EPGRefreshProcessor", "ScheduledEventProcessor", "UDIRefreshProcessor",
        ]
        assert all(thread.is_alive() for thread in created)
        web_api.start_setup_background_processors()
        assert len(created) == 3
        optional_start.assert_not_called()
    finally:
        web_api.stop_epg_refresh_processor()
        web_api.stop_scheduled_event_processor()
        web_api.stop_udi_refresh_processor()
        assert all(not thread.is_alive() for thread in created)
