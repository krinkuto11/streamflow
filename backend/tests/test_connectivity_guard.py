import os
import socket
import sys
import threading
from datetime import datetime, timedelta
from unittest.mock import Mock, patch

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from apps.stream.connectivity_guard import ConnectivityCheckResult, StreamConnectivityGuard
from apps.stream.stream_checker_components import StreamCheckConfig
from apps.stream.stream_checker_service import StreamCheckerService


class _Response:
    def __init__(self, status_code):
        self.status_code = status_code


class _ResolvingSocket:
    def getaddrinfo(self, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))]


class _DnsFailureSocket:
    def getaddrinfo(self, *args, **kwargs):
        raise socket.gaierror("dns failed")


class _RequestsOk:
    def __init__(self):
        self.urls = []

    def get(self, url, **kwargs):
        self.urls.append(url)
        if "channels/channels" in url:
            return _Response(200)
        return _Response(204)


class _RequestsAuthRefresh:
    def __init__(self):
        self.urls = []
        self.dispatcharr_attempts = 0

    def get(self, url, **kwargs):
        self.urls.append(url)
        if "channels/channels" not in url:
            return _Response(204)

        self.dispatcharr_attempts += 1
        if self.dispatcharr_attempts == 1:
            return _Response(401)
        return _Response(200)


class _RequestsDispatcharrTimeoutThenOk:
    def __init__(self):
        self.urls = []
        self.dispatcharr_attempts = 0

    def get(self, url, **kwargs):
        self.urls.append(url)
        if "channels/channels" not in url:
            return _Response(204)

        self.dispatcharr_attempts += 1
        if self.dispatcharr_attempts == 1:
            raise requests.exceptions.Timeout("temporary timeout")
        return _Response(200)


class _RequestsDispatcharrAlwaysTimeout:
    def __init__(self):
        self.dispatcharr_attempts = 0

    def get(self, url, **kwargs):
        if "channels/channels" not in url:
            return _Response(204)

        self.dispatcharr_attempts += 1
        raise requests.exceptions.Timeout("persistent timeout")


class _RequestsDispatcharrRequiresLongBudget:
    def __init__(self):
        self.dispatcharr_timeouts = []

    def get(self, url, **kwargs):
        if "channels/channels" not in url:
            return _Response(204)

        timeout = kwargs.get("timeout")
        self.dispatcharr_timeouts.append(timeout)
        if timeout <= 3:
            raise requests.exceptions.Timeout("response needs more than three seconds")
        return _Response(200)


class _RequestsUnauthorized:
    def get(self, url, **kwargs):
        if "channels/channels" in url:
            return _Response(403)
        return _Response(204)


def test_connectivity_guard_passes_when_internet_and_dispatcharr_are_reachable():
    requests_ok = _RequestsOk()
    guard = StreamConnectivityGuard(
        requests_module=requests_ok,
        socket_module=_ResolvingSocket(),
        default_internet_probe_urls=("https://probe.example/generate_204",),
    )

    result = guard.check(
        config={"enabled": True, "timeout_seconds": 1},
        dispatcharr_base_url="http://dispatcharr.local",
        dispatcharr_headers_provider=lambda: {"Authorization": "Bearer test"},
    )

    assert result.ok is True
    assert result.reason == "ok"
    assert requests_ok.urls == [
        "https://probe.example/generate_204",
        "http://dispatcharr.local/api/channels/channels/?page=1&page_size=1",
    ]
    assert result.details["dispatcharr_api"]["api_reachable"] is True
    assert result.details["dispatcharr_api"]["destructive_writes_allowed"] is True


def test_slow_dispatcharr_read_can_continue_analysis_without_allowing_writes():
    requests_slow = _RequestsDispatcharrRequiresLongBudget()
    guard = StreamConnectivityGuard(
        requests_module=requests_slow,
        socket_module=_ResolvingSocket(),
        default_internet_probe_urls=("https://probe.example/generate_204",),
    )

    result = guard.check(
        config={
            "enabled": True,
            "timeout_seconds": 3,
            "analysis_timeout_seconds": 8,
            "retry_attempts": 0,
        },
        dispatcharr_base_url="http://dispatcharr.local",
        dispatcharr_headers_provider=lambda: {"Authorization": "Bearer test"},
        operation="analysis",
    )

    assert result.ok is True
    assert requests_slow.dispatcharr_timeouts == [8]
    assert result.details["dispatcharr_api"]["operation"] == "analysis"
    assert result.details["dispatcharr_api"]["destructive_writes_allowed"] is False


def test_slow_dispatcharr_read_still_fails_closed_for_destructive_writes():
    requests_slow = _RequestsDispatcharrRequiresLongBudget()
    guard = StreamConnectivityGuard(
        requests_module=requests_slow,
        socket_module=_ResolvingSocket(),
        default_internet_probe_urls=("https://probe.example/generate_204",),
    )

    result = guard.check(
        config={
            "enabled": True,
            "timeout_seconds": 3,
            "analysis_timeout_seconds": 8,
            "retry_attempts": 0,
        },
        dispatcharr_base_url="http://dispatcharr.local",
        dispatcharr_headers_provider=lambda: {"Authorization": "Bearer test"},
        operation="destructive_write",
    )

    assert result.ok is False
    assert result.reason == "connectivity_timeout"
    assert requests_slow.dispatcharr_timeouts == [3]
    assert result.details["destructive_writes_allowed"] is False


def test_connectivity_guard_refreshes_auth_once_when_dispatcharr_token_is_stale():
    requests_refresh = _RequestsAuthRefresh()
    refresh = Mock(return_value=True)
    headers = Mock(side_effect=[
        {"Authorization": "Bearer stale"},
        {"Authorization": "Bearer fresh"},
    ])
    guard = StreamConnectivityGuard(
        requests_module=requests_refresh,
        socket_module=_ResolvingSocket(),
        default_internet_probe_urls=("https://probe.example/generate_204",),
    )

    result = guard.check(
        config={"enabled": True, "timeout_seconds": 1},
        dispatcharr_base_url="http://dispatcharr.local",
        dispatcharr_headers_provider=headers,
        dispatcharr_auth_refresh_provider=refresh,
    )

    assert result.ok is True
    assert result.reason == "ok"
    assert requests_refresh.dispatcharr_attempts == 2
    refresh.assert_called_once()
    assert headers.call_count == 2
    assert result.details["dispatcharr_api"]["auth_refresh_attempted"] is True
    assert result.details["dispatcharr_api"]["auth_refresh_ok"] is True


def test_dispatcharr_auth_failure_keeps_reachability_separate_from_write_permission():
    guard = StreamConnectivityGuard(
        requests_module=_RequestsUnauthorized(),
        socket_module=_ResolvingSocket(),
        default_internet_probe_urls=("https://probe.example/generate_204",),
    )

    result = guard.check(
        config={"enabled": True, "retry_attempts": 0},
        dispatcharr_base_url="http://dispatcharr.local",
        dispatcharr_headers_provider=lambda: {"Authorization": "Bearer denied"},
        operation="destructive_write",
    )

    assert result.ok is False
    assert result.reason == "dispatcharr_auth_failed"
    assert result.details["api_reachable"] is True
    assert result.details["api_authenticated"] is False
    assert result.details["destructive_writes_allowed"] is False


def test_connectivity_guard_retries_transient_dispatcharr_timeout():
    requests_retry = _RequestsDispatcharrTimeoutThenOk()
    guard = StreamConnectivityGuard(
        requests_module=requests_retry,
        socket_module=_ResolvingSocket(),
        default_internet_probe_urls=("https://probe.example/generate_204",),
    )

    result = guard.check(
        config={
            "enabled": True,
            "timeout_seconds": 1,
            "retry_attempts": 1,
            "retry_backoff_seconds": 0,
        },
        dispatcharr_base_url="http://dispatcharr.local",
        dispatcharr_headers_provider=lambda: {"Authorization": "Bearer test"},
    )

    assert result.ok is True
    assert result.reason == "ok"
    assert requests_retry.dispatcharr_attempts == 2
    assert result.details["dispatcharr_api"]["attempts"] == 2
    assert result.details["dispatcharr_api"]["max_attempts"] == 2
    assert result.details["dispatcharr_api"]["destructive_writes_allowed"] is True


def test_retry_uses_successful_attempt_latency_for_write_permission():
    class _RequestsSlowTimeoutThenFastSuccess:
        def __init__(self):
            self.calls = 0

        def get(self, url, **kwargs):
            self.calls += 1
            if self.calls == 1:
                raise requests.exceptions.Timeout("first attempt timed out")
            return _Response(200)

    requests_retry = _RequestsSlowTimeoutThenFastSuccess()
    guard = StreamConnectivityGuard(
        requests_module=requests_retry,
        socket_module=_ResolvingSocket(),
    )

    with patch(
        "apps.stream.connectivity_guard.time.monotonic",
        side_effect=[0.0, 0.0, 3.1, 3.1, 3.2, 3.2],
    ):
        result = guard.check(
            config={
                "enabled": True,
                "require_internet": False,
                "timeout_seconds": 3,
                "retry_attempts": 1,
                "retry_backoff_seconds": 0,
            },
            dispatcharr_base_url="http://dispatcharr.local",
            dispatcharr_headers_provider=lambda: {"Authorization": "Bearer test"},
            operation="destructive_write",
        )

    assert result.ok is True
    details = result.details["dispatcharr_api"]
    assert details["api_latency_seconds"] == 0.1
    assert details["total_probe_elapsed_seconds"] == 3.2
    assert details["destructive_writes_allowed"] is True


def test_connectivity_guard_fails_after_configured_dispatcharr_timeout_retries():
    requests_timeout = _RequestsDispatcharrAlwaysTimeout()
    guard = StreamConnectivityGuard(
        requests_module=requests_timeout,
        socket_module=_ResolvingSocket(),
        default_internet_probe_urls=("https://probe.example/generate_204",),
    )

    result = guard.check(
        config={
            "enabled": True,
            "timeout_seconds": 1,
            "retry_attempts": 2,
            "retry_backoff_seconds": 0,
        },
        dispatcharr_base_url="http://dispatcharr.local",
        dispatcharr_headers_provider=lambda: {"Authorization": "Bearer test"},
    )

    assert result.ok is False
    assert result.reason == "connectivity_timeout"
    assert requests_timeout.dispatcharr_attempts == 3
    assert result.details["attempts"] == 3
    assert result.details["max_attempts"] == 3
    assert result.details["timeout_seconds"] == 1


def test_connectivity_guard_fails_closed_on_dns_failure():
    guard = StreamConnectivityGuard(
        requests_module=_RequestsOk(),
        socket_module=_DnsFailureSocket(),
        default_internet_probe_urls=("https://probe.example/generate_204",),
    )

    result = guard.check(
        config={"enabled": True},
        dispatcharr_base_url="http://dispatcharr.local",
        dispatcharr_headers_provider=lambda: {},
    )

    assert result.ok is False
    assert result.reason == "dns_resolution_failed"


def test_connectivity_guard_can_be_disabled_by_config():
    requests_ok = _RequestsOk()
    guard = StreamConnectivityGuard(
        requests_module=requests_ok,
        socket_module=_DnsFailureSocket(),
    )

    result = guard.check(
        config={"enabled": False},
        dispatcharr_base_url=None,
        dispatcharr_headers_provider=lambda: {},
    )

    assert result.ok is True
    assert result.reason == "disabled"
    assert requests_ok.urls == []


def test_quality_check_startup_offline_aborts_before_channel_check():
    service = StreamCheckerService()
    failed = ConnectivityCheckResult(
        ok=False,
        reason="network_unreachable",
        message="internet connectivity probe could not reach its endpoint",
    )
    service.connectivity_guard.check = Mock(return_value=failed)

    with patch.object(service, "_check_channel_sequential") as sequential:
        result = service._check_channel(42)

    sequential.assert_not_called()
    assert result["success"] is False
    assert result["error"] == "connectivity_guard"
    assert result["aborted"] is True
    assert result["skip_reason"] == "connectivity_guard"
    assert service.connectivity_guard_status["ok"] is False
    service.connectivity_guard.check.assert_called_once()
    assert service.connectivity_guard.check.call_args.kwargs["operation"] == "analysis"


def test_destructive_quality_phase_uses_strict_write_connectivity_mode():
    service = StreamCheckerService()
    failed = ConnectivityCheckResult(
        ok=False,
        reason="connectivity_timeout",
        message="Dispatcharr API response exceeded the write budget",
    )
    service.connectivity_guard.check = Mock(return_value=failed)
    service.config.config["connectivity_guard"]["recovery_wait_seconds"] = 0

    result = service._require_quality_check_connectivity(
        phase="channel_stream_update",
        channel_id=42,
        channel_name="Test Channel",
        update_progress=False,
    )

    assert result is failed
    assert service.connectivity_guard.check.call_args.kwargs["operation"] == "destructive_write"
    assert service.connectivity_guard_status["channel_id"] == 42
    assert service.connectivity_guard_status["channel_name"] == "Test Channel"


def test_connectivity_abort_progress_cannot_resurrect_cleared_run():
    class FakeDB:
        def __init__(self):
            self.settings = {}

        def get_system_setting(self, key, default=None):
            return self.settings.get(key, default)

        def set_system_setting(self, key, value):
            self.settings[key] = value

    fake_db = FakeDB()
    failed = ConnectivityCheckResult(
        ok=False,
        reason="connectivity_timeout",
        message="Dispatcharr API did not recover",
    )
    service = StreamCheckerService()
    service.config.config["connectivity_guard"]["recovery_wait_seconds"] = 0
    service._run_connectivity_guard = Mock(return_value=failed)

    with patch('apps.database.manager.get_db_manager', return_value=fake_db):
        run_generation = service.progress.get_generation()
        service.progress.clear()
        progress_updates = []
        original_update = service.progress.update

        def record_progress(**kwargs):
            progress_updates.append(dict(kwargs))
            return original_update(**kwargs)

        service.progress.update = record_progress
        result = service._require_quality_check_connectivity(
            phase="channel_stream_update",
            channel_id=42,
            channel_name="Cleared connectivity run",
            progress_context={"expected_generation": run_generation},
        )

    assert result is failed
    assert len(progress_updates) == 1
    assert progress_updates[0]["status"] == "aborted"
    assert progress_updates[0]["expected_generation"] == run_generation
    assert fake_db.settings["stream_checker_progress"] == {}


def test_connectivity_recovery_progress_preserves_run_generation():
    failed = ConnectivityCheckResult(
        ok=False,
        reason="connectivity_timeout",
        message="Dispatcharr API is temporarily unavailable",
    )
    recovered = ConnectivityCheckResult(
        ok=True,
        reason="ok",
        message="Dispatcharr API recovered",
    )
    service = StreamCheckerService()
    service.config.config["connectivity_guard"]["recovery_wait_seconds"] = 1
    service.config.config["connectivity_guard"]["recovery_poll_seconds"] = 1
    service._run_connectivity_guard = Mock(side_effect=[failed, recovered])
    service.progress.update = Mock(return_value=True)

    with patch('apps.stream.stream_checker_service.time.sleep'):
        result = service._require_quality_check_connectivity(
            phase="channel_stream_update",
            channel_id=42,
            channel_name="Recovering connectivity run",
            progress_context={"expected_generation": 7},
        )

    assert result is None
    service.progress.update.assert_called_once()
    assert service.progress.update.call_args.kwargs["status"] == "waiting_connectivity"
    assert service.progress.update.call_args.kwargs["expected_generation"] == 7


def test_outer_channel_preflight_cannot_resurrect_progress_after_clear():
    class FakeDB:
        def __init__(self):
            self.settings = {}

        def get_system_setting(self, key, default=None):
            return self.settings.get(key, default)

        def set_system_setting(self, key, value):
            self.settings[key] = value

    fake_db = FakeDB()
    guard_entered = threading.Event()
    release_guard = threading.Event()
    failed = ConnectivityCheckResult(
        ok=False,
        reason="connectivity_timeout",
        message="Blocked preflight failed after clear",
    )
    service = StreamCheckerService()
    service.config.config["connectivity_guard"]["recovery_wait_seconds"] = 0

    def blocked_guard(*_args, **_kwargs):
        guard_entered.set()
        assert release_guard.wait(timeout=2)
        return failed

    service._run_connectivity_guard = blocked_guard
    results = []

    with patch('apps.database.manager.get_db_manager', return_value=fake_db):
        check_thread = threading.Thread(
            target=lambda: results.append(service._check_channel(42)),
        )
        check_thread.start()
        assert guard_entered.wait(timeout=2)

        service.progress.clear()
        release_guard.set()
        check_thread.join(timeout=2)

    assert not check_thread.is_alive()
    assert len(results) == 1
    assert results[0]["error"] == "connectivity_guard"
    assert fake_db.settings["stream_checker_progress"] == {}


def test_outer_channel_preflight_keeps_original_generation_after_clear():
    guard_entered = threading.Event()
    release_guard = threading.Event()
    service = StreamCheckerService()
    service._check_channel_concurrent = Mock(return_value={"success": True})
    original_generation = service.progress.get_generation()

    def blocked_success(*_args, **_kwargs):
        guard_entered.set()
        assert release_guard.wait(timeout=2)
        return ConnectivityCheckResult(
            ok=True,
            reason="ok",
            message="Preflight completed after clear",
        )

    service._run_connectivity_guard = blocked_success
    results = []
    check_thread = threading.Thread(
        target=lambda: results.append(service._check_channel(42)),
    )
    check_thread.start()
    assert guard_entered.wait(timeout=2)

    service.progress.clear()
    release_guard.set()
    check_thread.join(timeout=2)

    assert not check_thread.is_alive()
    assert results == [{"success": True}]
    assert service._check_channel_concurrent.call_args.kwargs[
        "expected_progress_generation"
    ] == original_generation


def test_automation_matching_preflight_uses_strict_write_connectivity_mode():
    service = StreamCheckerService()
    service.connectivity_guard.check = Mock(
        return_value=ConnectivityCheckResult(
            ok=True,
            reason="ok",
            message="Connectivity verified for destructive writes",
        )
    )

    result = service._require_quality_check_connectivity(
        phase="automation_quality_preflight",
        update_progress=False,
    )

    assert result is None
    assert service.connectivity_guard.check.call_args.kwargs["operation"] == "destructive_write"


def test_idle_status_marks_previous_connectivity_failure_as_stale():
    service = StreamCheckerService()
    service.checking = False
    service.connectivity_guard_status = {
        "ok": False,
        "reason": "connectivity_timeout",
        "message": "internet connectivity probe timed out",
    }

    with patch.object(service.progress, "get", return_value=None):
        status = service.get_status()

    assert status["stream_checking_mode"] is False
    assert status["connectivity_guard"]["active_failure"] is False
    assert status["connectivity_guard"]["stale_failure"] is True


def test_idle_stale_connectivity_failure_rechecks_and_clears_after_interval():
    service = StreamCheckerService()
    service.checking = False
    service.config.config["connectivity_guard"]["stale_recheck_interval_seconds"] = 10
    service.connectivity_guard_status = {
        "ok": False,
        "reason": "connectivity_timeout",
        "message": "internet connectivity probe timed out",
        "checked_at": (datetime.now() - timedelta(seconds=30)).isoformat(),
    }
    service.connectivity_guard.check = Mock(
        return_value=ConnectivityCheckResult(ok=True, reason="ok", message="Connectivity verified")
    )

    with patch.object(service.progress, "get", return_value=None):
        status = service.get_status()

    service.connectivity_guard.check.assert_called_once()
    assert status["stream_checking_mode"] is False
    assert status["connectivity_guard"]["ok"] is True
    assert status["connectivity_guard"]["phase"] == "stale_failure_recovery"
    assert status["connectivity_guard"]["active_failure"] is False
    assert status["connectivity_guard"]["stale_failure"] is False


def test_idle_stale_connectivity_failure_waits_for_recheck_interval():
    service = StreamCheckerService()
    service.checking = False
    service.config.config["connectivity_guard"]["stale_recheck_interval_seconds"] = 60
    service.connectivity_guard_status = {
        "ok": False,
        "reason": "connectivity_timeout",
        "message": "internet connectivity probe timed out",
        "checked_at": (datetime.now() - timedelta(seconds=10)).isoformat(),
    }
    service.connectivity_guard.check = Mock()

    with patch.object(service.progress, "get", return_value=None):
        status = service.get_status()

    service.connectivity_guard.check.assert_not_called()
    assert status["connectivity_guard"]["active_failure"] is False
    assert status["connectivity_guard"]["stale_failure"] is True


def test_active_status_marks_current_connectivity_failure_as_active():
    service = StreamCheckerService()
    service.checking = True
    service.connectivity_guard_status = {
        "ok": False,
        "reason": "connectivity_timeout",
        "message": "internet connectivity probe timed out",
    }

    with patch.object(service.progress, "get", return_value=None):
        status = service.get_status()

    assert status["stream_checking_mode"] is True
    assert status["connectivity_guard"]["active_failure"] is True
    assert status["connectivity_guard"]["stale_failure"] is False


def test_connectivity_recovery_wait_default_is_four_minutes():
    service = StreamCheckerService()

    assert service.config.config["connectivity_guard"]["recovery_wait_seconds"] == 240
    assert service.config.config["connectivity_guard"]["analysis_timeout_seconds"] == 10.0


def test_stream_checker_config_migrates_legacy_recovery_wait_from_db():
    from apps.database.manager import get_db_manager

    db = get_db_manager()
    db.set_system_setting(
        "stream_checker_config",
        {"connectivity_guard": {"recovery_wait_seconds": 120}},
    )

    config = StreamCheckConfig()
    saved_config = db.get_system_setting("stream_checker_config", {})

    assert config.config["connectivity_guard"]["recovery_wait_seconds"] == 240
    assert saved_config["connectivity_guard"]["recovery_wait_seconds"] == 240


def test_mid_run_transient_outage_waits_for_recovery_before_marking_dead():
    service = StreamCheckerService()
    service.config.config["concurrent_streams"]["enabled"] = False
    service.config.config["connectivity_guard"]["recovery_wait_seconds"] = 0.01
    service.config.config["connectivity_guard"]["recovery_poll_seconds"] = 1
    service._check_channel_limits = Mock(return_value=None)
    service._get_m3u_account_name = Mock(return_value="Provider")
    service._update_stream_stats = Mock(return_value=True)
    service.dead_streams_tracker.is_dead = Mock(return_value=False)
    service.dead_streams_tracker.mark_as_dead = Mock(return_value=True)

    ok = ConnectivityCheckResult(ok=True, reason="ok", message="Connectivity verified")
    failed = ConnectivityCheckResult(
        ok=False,
        reason="network_unreachable",
        message="internet connectivity probe could not reach its endpoint",
    )
    service.connectivity_guard.check = Mock(side_effect=[ok, failed, ok])

    mock_udi = Mock()
    mock_udi.get_channel_by_id.return_value = {
        "id": 42,
        "name": "Test Channel",
        "streams": [1001],
    }
    mock_udi.get_m3u_accounts.return_value = []
    mock_udi.apply_profile_url_transformation.side_effect = (
        lambda stream, **_kwargs: stream["url"]
    )
    mock_udi.get_stream_by_id.return_value = None

    mock_profile = {
        "id": "profile-1",
        "name": "Default",
        "stream_checking": {
            "enabled": True,
            "remove_dead_streams": True,
            "allow_revive": True,
            "grace_period": False,
        },
    }
    mock_automation_config = Mock()
    mock_automation_config.get_effective_configuration.return_value = {"profile": mock_profile}

    streams = [{
        "id": 1001,
        "name": "Dead Candidate",
        "url": "http://stream.example/live",
        "is_custom": True,
    }]
    dead_analysis = {
        "stream_id": 1001,
        "stream_name": "Dead Candidate",
        "stream_url": "http://stream.example/live",
        "resolution": "0x0",
        "fps": 0,
        "video_codec": "N/A",
        "audio_codec": "N/A",
        "bitrate_kbps": 0,
        "status": "ERROR",
    }

    with patch("apps.stream.stream_checker_service.get_udi_manager", return_value=mock_udi), \
         patch("apps.stream.stream_checker_service.fetch_channel_streams", return_value=streams), \
         patch("apps.stream.stream_checker_service.get_automation_config_manager", return_value=mock_automation_config), \
         patch("apps.stream.stream_checker_service.analyze_stream", return_value=dead_analysis), \
         patch("apps.stream.stream_checker_service.update_channel_streams") as update_channel_streams, \
         patch("apps.stream.stream_checker_service.time.sleep"):
        result = service._check_channel(42)

    assert "error" not in result
    assert result["dead_streams_count"] == 1
    service.dead_streams_tracker.mark_as_dead.assert_called_once()
    update_channel_streams.assert_called_once()
    assert service.connectivity_guard.check.call_count == 3
    assert service.connectivity_guard_status["ok"] is True
    assert service.connectivity_guard_status["phase"] == "mark_dead_stream_recovery"


def test_mid_run_outage_does_not_mark_dead_or_update_channel():
    service = StreamCheckerService()
    service.config.config["concurrent_streams"]["enabled"] = False
    service.config.config["connectivity_guard"]["recovery_wait_seconds"] = 0
    service._check_channel_limits = Mock(return_value=None)
    service._get_m3u_account_name = Mock(return_value="Provider")
    service._update_stream_stats = Mock(return_value=True)
    service.dead_streams_tracker.is_dead = Mock(return_value=False)
    service.dead_streams_tracker.mark_as_dead = Mock(return_value=True)

    ok = ConnectivityCheckResult(ok=True, reason="ok", message="Connectivity verified")
    failed = ConnectivityCheckResult(
        ok=False,
        reason="network_unreachable",
        message="internet connectivity probe could not reach its endpoint",
    )
    service.connectivity_guard.check = Mock(side_effect=[ok, failed])

    mock_udi = Mock()
    mock_udi.get_channel_by_id.return_value = {
        "id": 42,
        "name": "Test Channel",
        "streams": [1001],
    }
    mock_udi.get_m3u_accounts.return_value = []
    mock_udi.apply_profile_url_transformation.side_effect = (
        lambda stream, **_kwargs: stream["url"]
    )
    mock_udi.get_stream_by_id.return_value = None

    mock_profile = {
        "id": "profile-1",
        "name": "Default",
        "stream_checking": {
            "enabled": True,
            "remove_dead_streams": True,
            "allow_revive": True,
            "grace_period": False,
        },
    }
    mock_automation_config = Mock()
    mock_automation_config.get_effective_configuration.return_value = {"profile": mock_profile}

    streams = [{
        "id": 1001,
        "name": "Dead Candidate",
        "url": "http://stream.example/live",
        "is_custom": True,
    }]
    dead_analysis = {
        "stream_id": 1001,
        "stream_name": "Dead Candidate",
        "stream_url": "http://stream.example/live",
        "resolution": "0x0",
        "fps": 0,
        "video_codec": "N/A",
        "audio_codec": "N/A",
        "bitrate_kbps": 0,
        "status": "ERROR",
    }

    with patch("apps.stream.stream_checker_service.get_udi_manager", return_value=mock_udi), \
         patch("apps.stream.stream_checker_service.fetch_channel_streams", return_value=streams), \
         patch("apps.stream.stream_checker_service.get_automation_config_manager", return_value=mock_automation_config), \
         patch("apps.stream.stream_checker_service.analyze_stream", return_value=dead_analysis), \
         patch("apps.stream.stream_checker_service.update_channel_streams") as update_channel_streams:
        result = service._check_channel(42)

    assert result["success"] is False
    assert result["error"] == "connectivity_guard"
    service.dead_streams_tracker.mark_as_dead.assert_not_called()
    update_channel_streams.assert_not_called()
    assert service.connectivity_guard.check.call_count == 2
