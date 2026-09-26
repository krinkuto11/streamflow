"""Logo cache must bound provider responses and validate redirects/images."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from flask import Flask

from apps.api import channel_handlers
from apps.channels import logo_cache
from apps.channels import logo_verification_service
from apps.config import dispatcharr_config
from apps.udi import manager as udi_manager


PNG = b"\x89PNG\r\n\x1a\n" + b"small-logo"
SVG = b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"><rect width="10" height="10"/></svg>'


class FakeResponse:
    def __init__(self, *, body=b"", status=200, headers=None, chunks=None):
        self.body = body
        self.status_code = status
        self.headers = headers or {}
        self.chunks = chunks
        self.closed = False
        self.reads = 0

    def raise_for_status(self):
        if self.status_code >= 400:
            raise logo_cache.requests.HTTPError(f"{self.status_code}")

    def iter_content(self, *, chunk_size):
        assert chunk_size <= 64 * 1024
        for chunk in self.chunks if self.chunks is not None else [self.body]:
            self.reads += 1
            yield chunk

    def close(self):
        self.closed = True


def _fetch(tmp_path: Path, logo_url: str):
    app = Flask(__name__)
    with app.test_request_context("/api/channels/logos/7/cache"):
        return channel_handlers.get_channel_logo_cached_response(
            logo_id="7",
            config_dir=tmp_path,
            get_udi_manager=lambda: SimpleNamespace(get_logo_by_id=lambda _id: {"url": logo_url}),
            get_dispatcharr_config=lambda: SimpleNamespace(get_base_url=lambda: "http://127.0.0.1:9191"),
        )


def _status(result):
    return result[1] if isinstance(result, tuple) else result.status_code


def test_rejected_logo_does_not_expose_exception_details(monkeypatch, tmp_path):
    marker = "secret://provider:password@internal.example/private-logo-path"
    download = Mock(side_effect=logo_cache.InvalidLogoResponse(marker))
    log_warning = Mock()
    monkeypatch.setattr(channel_handlers, "download_and_cache_logo", download)
    monkeypatch.setattr(channel_handlers.logger, "warning", log_warning)

    response, status = _fetch(tmp_path, "http://10.10.30.20/logo.png")

    assert status == 422
    assert response.get_json() == {"error": "Logo response was rejected"}
    assert marker not in response.get_data(as_text=True)
    assert marker == str(log_warning.call_args.args[-1])


def test_logo_cache_downloads_once_with_safe_headers(monkeypatch, tmp_path):
    upstream = FakeResponse(body=PNG, headers={"Content-Type": "image/png"})
    requests = []

    def get(url, **kwargs):
        requests.append((url, kwargs))
        return upstream

    monkeypatch.setattr(logo_cache.requests, "get", get)
    first = _fetch(tmp_path, "http://10.10.30.20/logo.png")
    second = _fetch(tmp_path, "http://10.10.30.20/logo.png")

    assert _status(first) == _status(second) == 200
    assert first.headers["Content-Type"].startswith("image/png")
    assert first.headers["X-Content-Type-Options"] == "nosniff"
    assert len(requests) == 1
    assert requests[0][1]["stream"] is True
    assert requests[0][1]["allow_redirects"] is False
    assert upstream.closed
    assert (tmp_path / "logos_cache" / "logo_7.png").read_bytes() == PNG
    first.close()
    second.close()


def test_logo_rejects_oversized_content_length_before_reading(monkeypatch, tmp_path):
    upstream = FakeResponse(headers={"Content-Length": str(logo_cache.MAX_LOGO_BYTES + 1)})
    monkeypatch.setattr(logo_cache.requests, "get", lambda *_args, **_kwargs: upstream)

    result = _fetch(tmp_path, "http://10.10.30.20/logo.png")

    assert _status(result) == 422
    assert upstream.reads == 0
    assert upstream.closed
    assert list((tmp_path / "logos_cache").iterdir()) == []


def test_logo_rejects_chunked_response_above_limit_and_cleans_up(monkeypatch, tmp_path):
    upstream = FakeResponse(chunks=[b"x" * (64 * 1024)] * 65)
    monkeypatch.setattr(logo_cache.requests, "get", lambda *_args, **_kwargs: upstream)

    result = _fetch(tmp_path, "http://10.10.30.20/logo.png")

    assert _status(result) == 422
    assert upstream.reads == 65
    assert upstream.closed
    assert list((tmp_path / "logos_cache").iterdir()) == []


def test_logo_redirect_checks_target_before_following(monkeypatch, tmp_path):
    upstream = FakeResponse(status=302, headers={"Location": "http://169.254.169.254/latest/meta-data"})
    calls = []

    def get(url, **_kwargs):
        calls.append(url)
        return upstream

    monkeypatch.setattr(logo_cache.requests, "get", get)

    result = _fetch(tmp_path, "https://1.1.1.1/logo.png")

    assert _status(result) == 422
    assert calls == ["https://1.1.1.1/logo.png"]
    assert upstream.closed


def test_logo_allows_public_redirect_and_configured_dispatcharr_loopback(monkeypatch, tmp_path):
    redirect = FakeResponse(status=302, headers={"Location": "https://1.0.0.1/logo.png"})
    image = FakeResponse(body=PNG, headers={"Content-Type": "image/png"})
    calls = []

    def get(url, **_kwargs):
        calls.append(url)
        return [redirect, image][len(calls) - 1]

    monkeypatch.setattr(logo_cache.requests, "get", get)
    first = _fetch(tmp_path, "https://1.1.1.1/logo.png")
    assert _status(first) == 200
    assert calls == ["https://1.1.1.1/logo.png", "https://1.0.0.1/logo.png"]
    assert redirect.closed and image.closed

    first.close()
    monkeypatch.setattr(logo_cache.requests, "get", lambda url, **_kwargs: calls.append(url) or image)
    (tmp_path / "logos_cache" / "logo_7.png").unlink()
    calls.clear()
    second = _fetch(tmp_path, "http://127.0.0.1:9191/api/logos/7")
    assert _status(second) == 200
    assert calls == ["http://127.0.0.1:9191/api/logos/7"]
    second.close()


def test_logo_resolves_dispatcharr_relative_cache_url(monkeypatch, tmp_path):
    upstream = FakeResponse(body=PNG, headers={"Content-Type": "image/png"})
    calls = []
    monkeypatch.setattr(logo_cache.requests, "get", lambda url, **_kwargs: calls.append(url) or upstream)

    result = _fetch(tmp_path, "/api/logos/7/")

    assert _status(result) == 200
    assert calls == ["http://127.0.0.1:9191/api/logos/7/"]
    result.close()


def test_logo_blocks_unrelated_loopback_and_embedded_credentials(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(logo_cache.requests, "get", lambda *_args, **_kwargs: calls.append(1))

    assert _status(_fetch(tmp_path, "http://127.0.0.1:5000/admin")) == 422
    assert _status(_fetch(tmp_path, "http://user:pass@10.10.30.20/logo.png")) == 422
    assert calls == []


def test_logo_blocks_hostname_resolving_to_metadata_address(monkeypatch, tmp_path):
    monkeypatch.setattr(
        logo_cache.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [(2, 1, 6, "", ("169.254.169.254", 80))],
    )
    calls = []
    monkeypatch.setattr(logo_cache.requests, "get", lambda *_args, **_kwargs: calls.append(1))

    assert _status(_fetch(tmp_path, "http://fake-provider.example/logo.png")) == 422
    assert calls == []


def test_logo_stops_after_three_redirects(monkeypatch, tmp_path):
    upstream = FakeResponse(status=302, headers={"Location": "/next"})
    calls = []
    monkeypatch.setattr(
        logo_cache.requests,
        "get",
        lambda url, **_kwargs: calls.append(url) or upstream,
    )

    assert _status(_fetch(tmp_path, "http://10.10.30.20/logo.png")) == 422
    assert len(calls) == logo_cache.MAX_LOGO_REDIRECTS + 1
    assert upstream.closed


def test_logo_validates_image_bytes_and_sandboxes_svg(monkeypatch, tmp_path):
    upstream = FakeResponse(body=SVG, headers={"Content-Type": "image/svg+xml"})
    monkeypatch.setattr(logo_cache.requests, "get", lambda *_args, **_kwargs: upstream)

    result = _fetch(tmp_path, "http://10.10.30.20/logo.svg")
    assert _status(result) == 200
    assert result.headers["Content-Type"].startswith("image/svg+xml")
    assert "sandbox" in result.headers["Content-Security-Policy"]
    assert result.headers["X-Content-Type-Options"] == "nosniff"
    result.close()

    (tmp_path / "logos_cache" / "logo_7.svg").unlink()
    upstream = FakeResponse(body=b"<html><script>alert(1)</script></html>", headers={"Content-Type": "image/svg+xml"})
    assert _status(_fetch(tmp_path, "http://10.10.30.20/logo.svg")) == 422
    assert list((tmp_path / "logos_cache").iterdir()) == []


def test_logo_cache_prunes_old_entries_but_keeps_new_image(monkeypatch, tmp_path):
    cache = tmp_path / "logos_cache"
    cache.mkdir()
    old = cache / "logo_1.png"
    old.write_bytes(PNG)
    monkeypatch.setattr(logo_cache, "MAX_LOGO_CACHE_BYTES", len(PNG) + 1)
    upstream = FakeResponse(body=PNG, headers={"Content-Type": "image/png"})
    monkeypatch.setattr(logo_cache.requests, "get", lambda *_args, **_kwargs: upstream)

    result = _fetch(tmp_path, "http://10.10.30.20/logo.png")

    assert _status(result) == 200
    assert not old.exists()
    assert (cache / "logo_7.png").exists()


def test_visual_logo_verification_uses_same_bounded_cache(monkeypatch, tmp_path):
    cache_dir = tmp_path / "logos_cache"
    monkeypatch.setattr(logo_verification_service, "LOGOS_CACHE_DIR", cache_dir)
    monkeypatch.setattr(
        udi_manager,
        "get_udi_manager",
        lambda: SimpleNamespace(get_logo_by_id=lambda _id: {"url": "http://10.10.30.20/logo.png"}),
    )
    monkeypatch.setattr(
        dispatcharr_config,
        "get_dispatcharr_config",
        lambda: SimpleNamespace(get_base_url=lambda: "http://127.0.0.1:9191"),
    )
    upstream = FakeResponse(body=PNG, headers={"Content-Type": "image/png"})
    calls = []
    monkeypatch.setattr(logo_cache.requests, "get", lambda url, **_kwargs: calls.append(url) or upstream)

    path = logo_verification_service.get_cached_logo_path(7)

    assert path == str(cache_dir / "logo_7.png")
    assert Path(path).read_bytes() == PNG
    assert logo_verification_service.get_cached_logo_path(7) == path
    assert calls == ["http://10.10.30.20/logo.png"]
    assert upstream.closed


def test_visual_logo_verification_rejects_oversized_provider_body(monkeypatch, tmp_path):
    cache_dir = tmp_path / "logos_cache"
    monkeypatch.setattr(logo_verification_service, "LOGOS_CACHE_DIR", cache_dir)
    monkeypatch.setattr(
        udi_manager,
        "get_udi_manager",
        lambda: SimpleNamespace(get_logo_by_id=lambda _id: {"url": "http://10.10.30.20/logo.png"}),
    )
    monkeypatch.setattr(
        dispatcharr_config,
        "get_dispatcharr_config",
        lambda: SimpleNamespace(get_base_url=lambda: "http://127.0.0.1:9191"),
    )
    upstream = FakeResponse(headers={"Content-Length": str(logo_cache.MAX_LOGO_BYTES + 1)})
    monkeypatch.setattr(logo_cache.requests, "get", lambda *_args, **_kwargs: upstream)

    assert logo_verification_service.get_cached_logo_path(7) is None
    assert upstream.reads == 0
    assert upstream.closed
    assert list(cache_dir.iterdir()) == []
