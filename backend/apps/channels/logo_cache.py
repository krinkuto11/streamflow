"""Bounded logo downloads shared by the API and visual logo verification."""

import ipaddress
import logging
import socket
import tempfile
import threading
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import requests

logger = logging.getLogger(__name__)

MAX_LOGO_BYTES = 4 * 1024 * 1024
MAX_LOGO_CACHE_BYTES = 512 * 1024 * 1024
MAX_LOGO_REDIRECTS = 3
LOGO_DOWNLOAD_DEADLINE_SECONDS = 20
LOGO_EXTENSIONS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg")
_LOGO_CACHE_LOCK = threading.Lock()


class InvalidLogoResponse(ValueError):
    """The upstream response is unsuitable for the local logo cache."""


def _logo_url_origin(url: str) -> tuple[str, str, int]:
    if not isinstance(url, str) or "\\" in url or any(ord(char) < 32 for char in url):
        raise InvalidLogoResponse("Logo URL contains invalid characters")
    try:
        parsed = urlsplit(url)
    except ValueError as exc:
        raise InvalidLogoResponse("Logo URL is invalid") from exc
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise InvalidLogoResponse("Logo URL must use HTTP or HTTPS")
    if parsed.username is not None or parsed.password is not None or "%" in parsed.hostname:
        raise InvalidLogoResponse("Logo URL contains an invalid host or credentials")
    try:
        port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
    except ValueError as exc:
        raise InvalidLogoResponse("Logo URL contains an invalid port") from exc
    return parsed.scheme.lower(), parsed.hostname.lower().rstrip("."), port


def _check_logo_url(url: str, dispatcharr_origin: tuple[str, str, int] | None) -> None:
    """Keep LAN provider logos while blocking local services and metadata endpoints."""
    origin = _logo_url_origin(url)
    hostname = origin[1]
    if hostname in {"metadata.google.internal", "instance-data"}:
        raise InvalidLogoResponse("Logo URL points to a metadata service")
    if hostname == "localhost" or hostname.endswith(".localhost"):
        if origin != dispatcharr_origin:
            raise InvalidLogoResponse("Logo URL points to a local service")
        return

    try:
        addresses = [ipaddress.ip_address(hostname)]
    except ValueError:
        try:
            addresses = [
                ipaddress.ip_address(address[4][0])
                for address in socket.getaddrinfo(hostname, origin[2], type=socket.SOCK_STREAM)
            ]
        except (OSError, ValueError) as exc:
            raise InvalidLogoResponse("Logo hostname could not be resolved") from exc

    for address in addresses:
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
            address = address.ipv4_mapped
        if address.is_link_local or address.is_multicast or address.is_unspecified:
            raise InvalidLogoResponse("Logo URL points to a restricted address")
        if address.is_loopback and origin != dispatcharr_origin:
            raise InvalidLogoResponse("Logo URL points to a local service")


def _download_logo(url: str, dispatcharr_origin: tuple[str, str, int] | None) -> tuple[bytes, str]:
    deadline = time.monotonic() + LOGO_DOWNLOAD_DEADLINE_SECONDS
    for redirect_count in range(MAX_LOGO_REDIRECTS + 1):
        _check_logo_url(url, dispatcharr_origin)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise InvalidLogoResponse("Logo download timed out")
        response = requests.get(
            url,
            timeout=(min(3, remaining), min(8, remaining)),
            verify=True,
            stream=True,
            allow_redirects=False,
        )
        try:
            if response.status_code in {301, 302, 303, 307, 308}:
                location = response.headers.get("Location")
                if not location or redirect_count >= MAX_LOGO_REDIRECTS:
                    raise InvalidLogoResponse("Logo redirected too many times")
                url = urljoin(url, location)
                continue
            response.raise_for_status()

            content_length = response.headers.get("Content-Length")
            if content_length:
                try:
                    advertised_size = int(content_length)
                except ValueError as exc:
                    raise InvalidLogoResponse("Logo has an invalid Content-Length") from exc
                if advertised_size > MAX_LOGO_BYTES:
                    raise InvalidLogoResponse("Logo exceeds the download size limit")

            content = bytearray()
            for chunk in response.iter_content(chunk_size=64 * 1024):
                if time.monotonic() > deadline:
                    raise InvalidLogoResponse("Logo download timed out")
                content.extend(chunk)
                if len(content) > MAX_LOGO_BYTES:
                    raise InvalidLogoResponse("Logo exceeds the download size limit")
            if not content:
                raise InvalidLogoResponse("Logo response is empty")
            return bytes(content), response.headers.get("Content-Type", "")
        finally:
            response.close()
    raise InvalidLogoResponse("Logo redirected too many times")


def _logo_extension(content: bytes, content_type: str) -> str:
    """Use image bytes for the extension; never serve arbitrary upstream HTML."""
    mime = content_type.split(";", 1)[0].strip().lower()
    mime = {"image/jpg": "image/jpeg", "image/pjpeg": "image/jpeg", "image/x-png": "image/png"}.get(mime, mime)
    allowed_mime_types = {
        "", "application/octet-stream", "text/plain", "image/png",
        "image/jpeg", "image/gif", "image/webp", "image/svg+xml",
    }
    if mime not in allowed_mime_types:
        raise InvalidLogoResponse("Logo has an unsupported content type")
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        ext, expected_mime = ".png", "image/png"
    elif content.startswith(b"\xff\xd8\xff"):
        ext, expected_mime = ".jpg", "image/jpeg"
    elif content.startswith((b"GIF87a", b"GIF89a")):
        ext, expected_mime = ".gif", "image/gif"
    elif content.startswith(b"RIFF") and content[8:12] == b"WEBP":
        ext, expected_mime = ".webp", "image/webp"
    else:
        if b"<!DOCTYPE" in content.upper() or b"<!ENTITY" in content.upper():
            raise InvalidLogoResponse("Logo SVG contains a document type or entity")
        try:
            root = ET.fromstring(content)
        except ET.ParseError as exc:
            raise InvalidLogoResponse("Logo is not a supported image") from exc
        if root.tag not in {"svg", "{http://www.w3.org/2000/svg}svg"}:
            raise InvalidLogoResponse("Logo is not a supported image")
        ext, expected_mime = ".svg", "image/svg+xml"
    if mime.startswith("image/") and mime != expected_mime:
        raise InvalidLogoResponse("Logo content type does not match its image bytes")
    return ext


def find_cached_logo(cache_dir: Path, logo_id: int) -> Path | None:
    """Ignore and remove legacy oversized cache entries before serving them."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    for ext in LOGO_EXTENSIONS:
        path = cache_dir / f"logo_{logo_id}{ext}"
        if path.exists():
            if path.stat().st_size <= MAX_LOGO_BYTES:
                return path
            path.unlink()
    return None


def _prune_logo_cache(cache_dir: Path, protected_path: Path) -> None:
    files = []
    total_bytes = 0
    for path in cache_dir.glob("logo_*"):
        if path.suffix not in LOGO_EXTENSIONS or not path.is_file():
            continue
        stat = path.stat()
        files.append((stat.st_mtime, path, stat.st_size))
        total_bytes += stat.st_size
    for _, path, size in sorted(files):
        if total_bytes <= MAX_LOGO_CACHE_BYTES:
            break
        if path != protected_path:
            try:
                path.unlink(missing_ok=True)
                total_bytes -= size
            except OSError as exc:
                logger.warning("Could not prune cached logo %s: %s", path, exc)


def download_and_cache_logo(cache_dir: Path, logo_id: int, logo_url: str, dispatcharr_base_url: str = "") -> Path:
    """Download a verified image into the shared cache with an atomic replacement."""
    if not isinstance(logo_url, str):
        raise InvalidLogoResponse("Logo URL is invalid")
    dispatcharr_origin = _logo_url_origin(dispatcharr_base_url) if dispatcharr_base_url else None
    if logo_url.startswith("//"):
        raise InvalidLogoResponse("Logo URL must include a scheme")
    if logo_url.startswith("/"):
        if not dispatcharr_base_url:
            raise InvalidLogoResponse("Dispatcharr URL is required for relative logos")
        logo_url = urljoin(f"{dispatcharr_base_url.rstrip('/')}/", logo_url)

    content, content_type = _download_logo(logo_url, dispatcharr_origin)
    ext = _logo_extension(content, content_type)
    cache_dir.mkdir(parents=True, exist_ok=True)
    cached_path = cache_dir / f"logo_{logo_id}{ext}"
    with _LOGO_CACHE_LOCK:
        temporary_path = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb", dir=cache_dir, prefix=f".logo_{logo_id}_", suffix=".tmp", delete=False
            ) as file_obj:
                temporary_path = Path(file_obj.name)
                file_obj.write(content)
            temporary_path.replace(cached_path)
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
        _prune_logo_cache(cache_dir, cached_path)
    return cached_path
