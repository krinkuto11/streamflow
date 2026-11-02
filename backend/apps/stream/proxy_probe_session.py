"""Own exactly one native HLS session for each Shadow proxy probe."""

from functools import wraps
import logging
import re
from urllib.parse import parse_qs, urljoin, urlsplit, urlunsplit

from apps.core import http_transport

logger = logging.getLogger(__name__)
_PLAYLIST_PATH = re.compile(r"/proxy/hls/([A-Za-z0-9_-]{16,128})/index\.m3u8$")


class ProxyProbeSession:
    """Mint with the watcher identity, then release only its opaque session.

    Other proxy formats and direct provider pre-probes pass through unchanged.
    The capability playlist needs no API key in FFmpeg's downstream requests.
    """

    def __init__(self, url, config):
        self.url = url
        self.config = config
        self._delete_url = None
        self._headers = None

    def __enter__(self):
        parsed = urlsplit(self.url)
        if parse_qs(parsed.query).get("output_format", [""])[0].lower() != "hls":
            return self
        if "/proxy/ts/stream/" not in parsed.path:
            return self
        key = str(self.config.get("watcher_api_key") or "").strip()
        if not key:
            raise RuntimeError("An HLS watcher session requires a Watcher API Key")
        self._headers = {
            "X-API-Key": key,
            "Authorization": f"ApiKey {key}",
            "User-Agent": self.config.get("watcher_user_agent") or "StreamFlow-Shadow-Blank-Monitor/1.0",
        }
        # Read only the tune redirect, never a streaming response body. Following
        # it here would hide the session token from our lifetime owner.
        try:
            response = http_transport.get(
                self.url, headers=self._headers, allow_redirects=False,
                stream=True, timeout=(5, 10),
            )
        except Exception:
            raise RuntimeError("Could not open the Dispatcharr HLS watcher session") from None
        try:
            if response.status_code not in (301, 302, 303, 307, 308):
                raise RuntimeError("Dispatcharr did not return an HLS session redirect")
            playlist = urlsplit(urljoin(self.url, response.headers.get("Location", "")))
            match = _PLAYLIST_PATH.search(playlist.path)
            if (
                (playlist.scheme, playlist.netloc) != (parsed.scheme, parsed.netloc)
                or not match or playlist.query or playlist.fragment
            ):
                raise RuntimeError("Dispatcharr returned an invalid HLS session redirect")
            prefix = parsed.path.split("/proxy/ts/stream/", 1)[0]
            self._delete_url = urlunsplit((
                parsed.scheme, parsed.netloc,
                f"{prefix}/api/proxy/hls/sessions/{match.group(1)}/", "", "",
            ))
            self.url = playlist.geturl()
            self.config = dict(self.config, watcher_api_key="")
            return self
        finally:
            response.close()

    def close(self):
        delete_url, self._delete_url = self._delete_url, None
        if not delete_url:
            return
        # A transient DELETE can be retried safely: the session belongs to this
        # probe, and 404 also means it has already been removed.
        for attempt in range(2):
            try:
                response = http_transport.delete(
                    delete_url, headers=self._headers, allow_redirects=False, timeout=(3, 3),
                )
                try:
                    if response.status_code in (200, 202, 204, 404):
                        return
                    if response.status_code < 500 and response.status_code != 429:
                        break
                finally:
                    response.close()
            except Exception:
                if attempt == 0:
                    continue
        # URLs contain capability tokens. Neither URLs, headers, response
        # bodies nor exception strings belong in logs or public probe results.
        logger.warning("Could not release an owned HLS watcher session; Dispatcharr ghost cleanup will recover it")

    def __exit__(self, *_exc):
        self.close()


def with_proxy_session(probe):
    """Keep a finite probe's HLS session through success, cancel, and errors."""
    @wraps(probe)
    def run(service, url, config, *args, **kwargs):
        with ProxyProbeSession(url, config) as session:
            return probe(service, session.url, session.config, *args, **kwargs)
    return run
