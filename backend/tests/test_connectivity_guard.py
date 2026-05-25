import os
import socket
import sys
from unittest.mock import Mock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from apps.stream.connectivity_guard import ConnectivityCheckResult, StreamConnectivityGuard
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
        "http://dispatcharr.local/api/channels/channels/",
    ]


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
    assert service.connectivity_guard_status["ok"] is False


def test_mid_run_outage_does_not_mark_dead_or_update_channel():
    service = StreamCheckerService()
    service.config.config["concurrent_streams"]["enabled"] = False
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
    mock_udi.apply_profile_url_transformation.side_effect = lambda stream: stream["url"]
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

    streams = [{"id": 1001, "name": "Dead Candidate", "url": "http://stream.example/live"}]
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
         patch("apps.automation.automation_config_manager.get_automation_config_manager", return_value=mock_automation_config), \
         patch("apps.stream.stream_check_utils.analyze_stream", return_value=dead_analysis), \
         patch("apps.stream.stream_checker_service.update_channel_streams") as update_channel_streams:
        result = service._check_channel(42)

    assert result["success"] is False
    assert result["error"] == "connectivity_guard"
    service.dead_streams_tracker.mark_as_dead.assert_not_called()
    update_channel_streams.assert_not_called()
    assert service.connectivity_guard.check.call_count == 2
