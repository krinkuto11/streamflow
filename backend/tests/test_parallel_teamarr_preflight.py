"""Parallel Teamarr jobs keep queue ownership and run state independent."""

import threading
import time
from types import SimpleNamespace

import pytest

from apps.stream.stream_checker_service import StreamCheckerService
from apps.stream.stream_checker_components import StreamCheckerProgress


def wait_until(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    assert predicate()


@pytest.fixture
def parallel_checker(monkeypatch):
    settings = {"max_concurrent_checks": 2}
    monkeypatch.setattr(
        "apps.stream.teamarr_preflight_service.get_teamarr_preflight_service",
        lambda: SimpleNamespace(get_config=lambda: settings),
    )
    checker = StreamCheckerService()
    entered = {cid: threading.Event() for cid in (1, 2, 3)}
    release = {cid: threading.Event() for cid in (1, 2, 3)}
    runs = {}

    # Replace the external media pipeline with a controllable long-running
    # phase; retain the real queue, ownership, progress, and dispatcher.
    def check(run, entry, **kwargs):
        cid = entry["channel_id"]
        runs[cid] = run
        run.progress.update(cid, f"Game {cid}", 0, 1, step="Loop testing")
        entered[cid].set()
        assert release[cid].wait(5)
        run._complete_channel_check(cid, queue_entry_token=entry["queue_entry_token"])

    monkeypatch.setattr(StreamCheckerService, "_run_specialized_queue_entry_owned", check)
    for cid in (1, 2, 3):
        checker.check_queue.add_channel(
            cid, priority=100, metadata={"source": "teamarr_preflight"},
        )
    yield checker, settings, entered, release, runs
    for event in release.values():
        event.set()
    wait_until(lambda: not getattr(checker, "_parallel_preflight_runs", {}))


def test_parallel_games_fill_vacated_slot_without_waiting_for_other_loop(parallel_checker):
    checker, _, entered, release, runs = parallel_checker
    assert checker._dispatch_parallel_preflight_checks()
    assert entered[1].wait(2)
    assert checker._dispatch_parallel_preflight_checks()
    assert entered[2].wait(2)
    assert checker._dispatch_parallel_preflight_checks()
    assert not entered[3].is_set()
    assert checker.check_queue.get_status()["in_progress"] == 2

    assert runs[1].progress.get()["channel_id"] == 1
    assert runs[2].progress.get()["channel_id"] == 2
    assert len(checker.get_status()["parallel_checks"]) == 2
    release[1].set()
    wait_until(lambda: len(checker._parallel_preflight_runs) == 1)
    assert checker._dispatch_parallel_preflight_checks()
    assert entered[3].wait(2)
    assert not release[2].is_set()
    assert runs[2].progress.get()["channel_id"] == 2
    assert checker._begin_single_channel_check_operation() is False


def test_clear_aborts_all_runs_and_retains_ownership_until_they_exit(parallel_checker):
    checker, _, entered, release, runs = parallel_checker
    checker._dispatch_parallel_preflight_checks()
    checker._dispatch_parallel_preflight_checks()
    assert entered[1].wait(2) and entered[2].wait(2)
    result = checker.clear_queue()
    assert result["abort_requested"] is True
    assert all(run.abort_current_check.is_set() for run in runs.values())
    assert all(run.progress.get() is None for run in runs.values())
    assert checker._begin_single_channel_check_operation() is False
    assert checker._dispatch_parallel_preflight_checks()
    assert not entered[3].is_set()
    release[1].set()
    release[2].set()
    wait_until(lambda: not checker._parallel_preflight_runs)
    assert checker.check_queue.get_status()["completed"] == 0
    assert checker._begin_single_channel_check_operation() is True
    checker._end_single_channel_check_operation()


def test_run_abort_does_not_cancel_another_game(parallel_checker):
    checker, _, entered, _, runs = parallel_checker
    checker._dispatch_parallel_preflight_checks()
    checker._dispatch_parallel_preflight_checks()
    assert entered[1].wait(2) and entered[2].wait(2)
    runs[1].request_abort("connectivity failure")
    assert runs[1].abort_current_check.is_set()
    assert not runs[2].abort_current_check.is_set()
    assert not checker.abort_current_check.is_set()


def test_reduced_limit_drains_running_games_before_starting_more(parallel_checker):
    checker, settings, entered, release, _ = parallel_checker
    checker._dispatch_parallel_preflight_checks()
    checker._dispatch_parallel_preflight_checks()
    assert entered[1].wait(2) and entered[2].wait(2)
    settings["max_concurrent_checks"] = 1
    release[1].set()
    wait_until(lambda: len(checker._parallel_preflight_runs) == 1)
    assert checker._dispatch_parallel_preflight_checks()
    assert not entered[3].is_set()


def test_finished_group_releases_shared_budget(parallel_checker):
    checker, _, entered, release, _ = parallel_checker
    checker._dispatch_parallel_preflight_checks()
    assert entered[1].wait(2)
    release[1].set()
    wait_until(lambda: not checker._parallel_preflight_runs)
    assert checker._preflight_probe_slots is None


def test_completed_channel_cannot_overlap_its_still_unwinding_execution(parallel_checker):
    checker, _, entered, _, _ = parallel_checker
    checker._dispatch_parallel_preflight_checks()
    assert entered[1].wait(2)
    token = next(iter(checker._parallel_preflight_runs))[1]
    checker.check_queue.mark_completed(1, entry_token=token)
    checker.check_queue.remove_from_completed(1)
    assert checker.check_queue.add_channel(1, priority=1000, metadata={"source": "teamarr_preflight"})
    checker._dispatch_parallel_preflight_checks()
    assert entered[2].wait(2)
    assert len([key for key in checker._parallel_preflight_runs if key[0] == 1]) == 1
    assert 1 in checker.check_queue.queued


def test_pause_at_dequeue_cannot_start_a_new_game(parallel_checker, monkeypatch):
    checker, _, entered, _, _ = parallel_checker
    dequeue = checker.check_queue.get_next_entry_for_metadata_sources

    def pause_then_dequeue(*args, **kwargs):
        checker.check_queue.paused = True
        return dequeue(*args, **kwargs)

    monkeypatch.setattr(checker.check_queue, "get_next_entry_for_metadata_sources", pause_then_dequeue)
    checker._dispatch_parallel_preflight_checks()
    assert not entered[1].is_set()
    assert checker.check_queue.get_status()["queue_size"] == 3


def test_force_cleanup_failure_still_releases_owner(parallel_checker, monkeypatch):
    checker, _, entered, release, _ = parallel_checker
    monkeypatch.setattr(checker, "_queue_force_check_state", lambda cid: (True, 1))

    def failed_clear(*args, **kwargs):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(checker.update_tracker, "clear_force_check", failed_clear)
    checker._dispatch_parallel_preflight_checks()
    assert entered[1].wait(2)
    release[1].set()
    wait_until(lambda: not checker._parallel_preflight_runs)
    assert checker._active_queue_entry_executions == {}
    assert checker._preflight_probe_slots is None


@pytest.mark.parametrize("blocked_by", ["paused", "gate", "automation", "single", "batch"])
def test_parallel_admission_respects_existing_owners(parallel_checker, blocked_by):
    checker, _, entered, _, _ = parallel_checker
    if blocked_by == "paused":
        checker.check_queue.paused = True
    elif blocked_by == "gate":
        checker.set_specialized_queue_gate("teamarr_preflight_automation", True)
    elif blocked_by == "automation":
        checker._automation_cycle_active = True
    elif blocked_by == "single":
        checker._single_channel_check_active = True
    else:
        checker._sync_batch_execution_active = True
    checker._dispatch_parallel_preflight_checks()
    assert not any(event.is_set() for event in entered.values())
    assert checker.check_queue.get_status()["queue_size"] == 3


def test_memory_progress_cannot_overwrite_or_clear_persistent_progress():
    persistent = StreamCheckerProgress()
    first = StreamCheckerProgress(persist=False)
    second = StreamCheckerProgress(persist=False)
    persistent.update(9, "Manual", 0, 1)
    first.update(1, "Game 1", 0, 1)
    second.update(2, "Game 2", 0, 1)
    generation = first.get_generation()
    first.clear()
    assert first.get() is None
    assert first.update(1, "Stale", 1, 1, expected_generation=generation) is False
    assert second.get()["channel_id"] == 2
    assert persistent.get()["channel_id"] == 9


def test_no_queue_during_other_work_still_allows_parallel_preflight_siblings(parallel_checker):
    from apps.stream.teamarr_preflight_service import TeamarrPreflightService

    checker, settings, entered, _, _ = parallel_checker
    service = TeamarrPreflightService.__new__(TeamarrPreflightService)
    service.stream_checker_provider = lambda: checker
    service.automation_status_provider = lambda: {}
    config = {**settings, "queue_during_active_checks": False}
    checker._dispatch_parallel_preflight_checks()
    assert entered[1].wait(2)
    assert service._active_work_defer_reason(config) is None
    checker.check_queue.add_channel(99)
    assert service._active_work_defer_reason(config) == "stream_checker_active"


def test_separate_schedulers_share_the_same_probe_budget():
    from concurrent.futures import ThreadPoolExecutor
    from apps.stream.concurrent_stream_limiter import AccountStreamLimiter, SmartStreamScheduler

    limiter = AccountStreamLimiter()
    scheduler = SmartStreamScheduler(limiter, global_limit=4)
    slots = threading.BoundedSemaphore(1)
    first_started = threading.Event()
    second_started = threading.Event()
    release = threading.Event()

    def analyze(**kwargs):
        event = first_started if kwargs["stream_id"] == 1 else second_started
        event.set()
        assert release.wait(3)
        return {"stream_id": kwargs["stream_id"], "status": "OK"}

    def check(cid):
        return scheduler.check_streams_with_limits(
            [{"id": cid, "url": f"https://example.test/{cid}", "m3u_account": None, "is_custom": True}],
            analyze, probe_slots=slots,
        )

    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(check, 1)
        assert first_started.wait(2)
        second = pool.submit(check, 2)
        try:
            assert not second_started.wait(0.15)
        finally:
            release.set()
        assert len(first.result()) == len(second.result()) == 1
    assert second_started.is_set()
    assert slots.acquire(blocking=False)


@pytest.mark.parametrize("fails", [False, True])
def test_parallel_inventory_refresh_keeps_valid_sibling_authority_until_publication(monkeypatch, fails):
    from apps.stream.checker.preflight_run import PreflightCheckRun
    from apps.stream.concurrent_stream_limiter import AccountStreamLimiter, initialize_account_limits

    owner = StreamCheckerService()
    run = PreflightCheckRun(owner, (1, 1))
    limiter = AccountStreamLimiter()
    monkeypatch.setattr("apps.stream.concurrent_stream_limiter.get_account_limiter", lambda: limiter)
    accounts = [{"id": 1, "max_streams": 2, "profiles": []}]
    assert initialize_account_limits(accounts)
    observations = []

    def get_accounts():
        observations.append(limiter.account_inventory_trusted)
        if fails:
            raise RuntimeError("inventory unavailable")
        return accounts

    ok = run._initialize_provider_probe_account_inventory(
        udi=SimpleNamespace(get_m3u_accounts=get_accounts), limiter=limiter,
        initialize_account_limits=initialize_account_limits, operation_label="Test",
    )
    assert observations == [True]
    assert ok is not fails
    assert limiter.account_inventory_trusted is not fails


def test_loop_probe_waits_for_shared_budget_and_releases_it_on_error(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from unittest.mock import Mock
    from apps.stream.concurrent_stream_limiter import AccountStreamLimiter

    checker = StreamCheckerService()
    slots = threading.BoundedSemaphore(1)
    slots.acquire()
    checker._preflight_probe_slots = slots
    raw = {"id": 1, "url": "https://example.test/live", "is_custom": True}
    udi = Mock()
    udi.get_m3u_accounts.return_value = []
    udi.get_stream_by_id.return_value = raw
    limiter = AccountStreamLimiter(udi_manager=udi)
    monkeypatch.setattr("apps.stream.stream_checker_service.get_udi_manager", lambda: udi)
    monkeypatch.setattr("apps.stream.concurrent_stream_limiter.get_account_limiter", lambda: limiter)
    started = threading.Event()

    def probe(**kwargs):
        started.set()
        raise RuntimeError("decoder failed")

    monkeypatch.setattr("apps.stream.stream_check_utils._probe_stream_for_loops", probe)
    candidate = {"stream_id": 1, "stream_url": raw["url"], "score": 0.9, "status": "OK"}
    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(checker._run_loop_probes, [candidate])
        try:
            assert not started.wait(0.15)
        finally:
            slots.release()
        future.result(3)
    assert started.is_set()
    assert not candidate["loop_probe_ran"]
    assert slots.acquire(blocking=False)


def test_parallel_loop_retries_busy_profile_without_holding_account_slot(monkeypatch):
    from unittest.mock import Mock

    checker = StreamCheckerService()
    checker._preflight_probe_slots = threading.BoundedSemaphore(2)
    raw = {"id": 1, "url": "https://example.test/live", "m3u_account_id": 7}
    profile = {"id": 11}
    udi = Mock()
    udi.get_m3u_accounts.return_value = [{"id": 7, "max_streams": 2}]
    udi.get_stream_by_id.return_value = raw
    limiter = Mock()
    limiter.acquire.return_value = (True, "acquired")
    limiter.should_preempt_profile_for_viewer.return_value = False
    attempts = []

    def reserve(stream):
        attempts.append(stream)
        if len(attempts) == 1:
            return False, "checking_capacity", None, ""
        limiter.release.assert_called_once_with(7)
        return True, "acquired", profile, raw["url"]

    limiter.reserve_profile_for_stream_with_url.side_effect = reserve
    monkeypatch.setattr("apps.stream.stream_checker_service.get_udi_manager", lambda: udi)
    monkeypatch.setattr("apps.stream.concurrent_stream_limiter.get_account_limiter", lambda: limiter)
    monkeypatch.setattr("apps.stream.concurrent_stream_limiter.initialize_account_limits", lambda accounts: True)
    probe = Mock(return_value=(False, None, 90))
    monkeypatch.setattr("apps.stream.stream_check_utils._probe_stream_for_loops", probe)
    candidate = {"stream_id": 1, "stream_url": raw["url"], "score": 0.9, "status": "OK"}
    checker._run_loop_probes([candidate], probe_duration=180)
    assert candidate["loop_probe_ran"] is True
    assert len(attempts) == 2
    assert limiter.release.call_count == 2
    limiter.release_profile.assert_called_once_with(profile)
    assert probe.call_args.kwargs["probe_duration"] == 180


@pytest.mark.parametrize("start_empty", [False, True, "enable_while_waiting"])
def test_six_events_roll_through_worker_and_single_channel_reporting(monkeypatch, start_empty):
    """Exercise real worker -> queued validation -> channel -> report ownership."""
    from unittest.mock import Mock

    settings = {"max_concurrent_checks": 1 if start_empty == "enable_while_waiting" else 2}
    preflight = Mock()
    preflight.get_config.return_value = settings
    preflight.validate_queued_check.return_value = None
    monkeypatch.setattr("apps.stream.teamarr_preflight_service.get_teamarr_preflight_service", lambda: preflight)
    udi = Mock()
    udi.get_channel_by_id.side_effect = lambda cid: {"id": cid, "name": f"Game {cid}", "streams": [cid]}
    udi.is_network_ready.return_value = False
    streams = lambda cid: [{"id": cid, "name": f"Feed {cid}", "url": f"https://example.test/{cid}", "is_custom": True}]
    monkeypatch.setattr("apps.stream.stream_checker_service.get_udi_manager", lambda: udi)
    monkeypatch.setattr("apps.stream.stream_checker_service.fetch_channel_streams", streams)
    sessions = Mock()
    sessions.get_channels_in_active_sessions.return_value = []
    monkeypatch.setattr("apps.stream.stream_checker_service.get_session_manager", lambda: sessions)
    profile = {"id": "preflight", "name": "Game checks", "m3u_update": {"enabled": False},
               "stream_matching": {"enabled": False}, "stream_checking": {"enabled": True}}
    automation = Mock()
    automation.get_profile.return_value = profile
    monkeypatch.setattr("apps.stream.stream_checker_service.get_automation_config_manager", lambda: automation)
    monkeypatch.setattr(StreamCheckerService, "_require_quality_check_connectivity", lambda *args, **kwargs: None)
    monkeypatch.setattr(StreamCheckerService, "_check_channel_limits", lambda *args, **kwargs: None)
    entered = {cid: threading.Event() for cid in range(1, 7)}
    release = {cid: threading.Event() for cid in range(1, 7)}

    def media_phase(run, cid, **kwargs):
        assert kwargs["run_mode"] == "teamarr_preflight"
        assert kwargs["forced_profile_id"] == "preflight"
        run.progress.update(cid, f"Game {cid}", 0, 1, step="Loop testing")
        entered[cid].set()
        assert release[cid].wait(5)
        run._complete_channel_check(cid, queue_entry_token=kwargs["queue_entry_token"])
        return {"success": True, "analyzed_streams": [{"stream_id": cid, "loop_detected": False,
                                                      "loop_probe_ran": True, "score": 0.9}]}

    monkeypatch.setattr(StreamCheckerService, "_check_channel", media_phase)
    checker = StreamCheckerService()
    checker.changelog = Mock()
    def enqueue_games():
        for cid in range(1, 7):
            checker.check_queue.add_channel(cid, metadata={"source": "teamarr_preflight", "forced_profile_id": "preflight"})

    waiting = threading.Event()
    dequeue = checker.check_queue.get_next_entry

    def ordinary_dequeue(*args, **kwargs):
        waiting.set()
        return dequeue(*args, **kwargs)

    monkeypatch.setattr(checker.check_queue, "get_next_entry", ordinary_dequeue)
    if not start_empty:
        enqueue_games()
    checker.running = True
    worker = threading.Thread(target=checker._worker_loop, daemon=True)
    worker.start()
    try:
        if start_empty:
            assert waiting.wait(2)
            settings["max_concurrent_checks"] = 2
            enqueue_games()
        assert entered[1].wait(3) and entered[2].wait(3)
        assert not entered[3].is_set()
        for cid in range(1, 5):
            release[cid].set()
            assert entered[cid + 2].wait(3)
            assert len(checker._parallel_preflight_runs) <= 2
        release[5].set()
        release[6].set()
        wait_until(lambda: checker.check_queue.get_status()["completed"] == 6 and not checker._parallel_preflight_runs)
        assert preflight.validate_queued_check.call_count == 6
        results = [call.args[1] for call in preflight.record_queued_check_result.call_args_list]
        assert len(results) == 6 and all(result["success"] for result in results)
        assert {result["channel_id"] for result in results} == set(range(1, 7))
        assert checker.changelog.add_single_channel_check_entry.call_count == 6
        assert udi.set_automation_busy.call_count == udi.clear_automation_busy.call_count == 6
    finally:
        checker.running = False
        for event in release.values():
            event.set()
        worker.join(3)
        wait_until(lambda: not checker._parallel_preflight_runs)
