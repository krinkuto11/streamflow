"""Probe ownership must never stop a real viewer's native HLS session."""
import subprocess
from unittest.mock import Mock

import pytest

from apps.stream import proxy_probe_session as module
from apps.stream.proxy_probe_session import ProxyProbeSession, with_proxy_session
from apps.stream.shadow_blank_monitor_service import ShadowBlankMonitorService

TUNE = "http://dispatcharr.test/proxy/ts/stream/channel-1?output_format=hls"
TOKEN = "own-session-token-123456789"
PLAYLIST = f"http://dispatcharr.test/proxy/hls/{TOKEN}/index.m3u8"
DELETE = f"http://dispatcharr.test/api/proxy/hls/sessions/{TOKEN}/"
CONFIG = {"watcher_api_key": "private-watcher-key", "watcher_user_agent": "shadow-test"}


@pytest.fixture
def transport(monkeypatch):
    response = Mock(status_code=302, headers={"Location": PLAYLIST})
    get = Mock(return_value=response)
    delete = Mock(return_value=Mock(status_code=204))
    monkeypatch.setattr(module.http_transport, "get", get)
    monkeypatch.setattr(module.http_transport, "delete", delete)
    return get, delete


@pytest.mark.parametrize("failure", [None, RuntimeError("probe failed"), subprocess.TimeoutExpired("ffmpeg", 1)])
def test_finite_probe_releases_its_session_on_success_or_failure(transport, failure):
    @with_proxy_session
    def probe(_service, url, config):
        assert url == PLAYLIST
        assert config["watcher_api_key"] == ""
        if failure:
            raise failure
        return {"stopped": True}

    if failure:
        with pytest.raises(type(failure)):
            probe(None, TUNE, CONFIG)
    else:
        assert probe(None, TUNE, CONFIG) == {"stopped": True}
    get, delete = transport
    assert get.call_args.kwargs["allow_redirects"] is False
    assert get.call_args.kwargs["headers"]["X-API-Key"] == CONFIG["watcher_api_key"]
    assert delete.call_args.args == (DELETE,)
    assert delete.call_args.kwargs["headers"]["X-API-Key"] == CONFIG["watcher_api_key"]
    assert CONFIG["watcher_api_key"] == "private-watcher-key"


@pytest.mark.parametrize("url", ["http://provider.test/live/test.ts", TUNE.replace("hls", "fmp4"), PLAYLIST])
def test_other_inputs_do_not_mint_or_delete_sessions(transport, url):
    with ProxyProbeSession(url, CONFIG) as session:
        assert session.url == url
    for call in transport:
        call.assert_not_called()


def test_nested_probes_share_one_lease(transport):
    @with_proxy_session
    def nested(_service, url, config):
        return url

    @with_proxy_session
    def probe(service, url, config):
        return nested(service, url, config)

    assert probe(None, TUNE, CONFIG) == PLAYLIST
    assert transport[0].call_count == transport[1].call_count == 1


@pytest.mark.parametrize("location", ["http://foreign.test/proxy/hls/" + TOKEN + "/index.m3u8", "/unexpected/path", ""])
def test_untrusted_redirect_is_not_followed(transport, location):
    transport[0].return_value.headers["Location"] = location
    with pytest.raises(RuntimeError, match="invalid HLS session redirect"):
        with ProxyProbeSession(TUNE, CONFIG):
            pytest.fail("invalid redirect accepted")
    transport[1].assert_not_called()


@pytest.mark.parametrize("exited", [True, False])
def test_persistent_watcher_releases_even_after_process_exit(transport, exited):
    lease = ProxyProbeSession(TUNE, CONFIG).__enter__()
    process = Mock()
    process.poll.return_value = 0 if exited else None
    session = {"process": process, "proxy_session": lease}
    ShadowBlankMonitorService._terminate_persistent_watcher_session(session)
    ShadowBlankMonitorService._terminate_persistent_watcher_session(session)
    assert transport[1].call_count == 1
    if exited:
        process.terminate.assert_not_called()


def test_cleanup_retries_transient_errors_without_disclosing_token(transport, caplog):
    transport[1].side_effect = [RuntimeError(PLAYLIST), Mock(status_code=204)]
    with ProxyProbeSession(TUNE, CONFIG):
        pass
    assert transport[1].call_count == 2
    assert TOKEN not in caplog.text
    assert CONFIG["watcher_api_key"] not in caplog.text


def test_hls_aliases_and_unknown_format_do_not_inherit_server_default():
    service = object.__new__(ShadowBlankMonitorService)
    service.base_url_provider = lambda: "http://dispatcharr.test"
    for value in ("hls", "m3u8", "hls:p21"):
        assert service._channel_proxy_url("channel-1", value) == TUNE
    assert service._channel_proxy_url("channel-1").endswith("?output_format=mpegts")
