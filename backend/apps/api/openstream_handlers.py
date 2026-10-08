"""OpenStream API handler functions (Settings → Connection)."""

from typing import Any, Callable, Dict, Optional
from urllib.parse import urlsplit

import requests
from flask import jsonify

from apps.core.logging_config import setup_logging
from apps.stream.openstream_monitor import parse_openstream_url

logger = setup_logging(__name__)

_TEST_TIMEOUT = 10


def get_openstream_config_response(*, get_openstream_config: Callable[[], Any]):
    """Current OpenStream configuration, without the API key itself."""
    try:
        return jsonify(get_openstream_config().get_config())
    except Exception as exc:
        logger.error(f"Error getting OpenStream config: {exc}")
        return jsonify({"error": "Internal Server Error"}), 500


def update_openstream_config_response(
    *,
    payload: Optional[Dict[str, Any]],
    get_openstream_config: Callable[[], Any],
):
    """Save the API key and test URL. An empty api_key keeps the saved key
    unless clear_api_key is set (same convention as the Dispatcharr form)."""
    try:
        data = payload
        if not data:
            return jsonify({"error": "No configuration data provided"}), 400
        api_key = data.get("api_key")
        if data.get("clear_api_key"):
            api_key = ""
        elif not api_key:
            api_key = None
        test_url = data.get("test_url")
        if not get_openstream_config().update_config(api_key=api_key, test_url=test_url):
            return jsonify({"error": "Failed to save configuration"}), 500
        return jsonify({"message": "OpenStream configuration updated successfully"})
    except Exception as exc:
        logger.error(f"Error updating OpenStream config: {exc}")
        return jsonify({"error": "Internal Server Error"}), 500


def _server_base(url: str) -> Optional[str]:
    """The server root of a test URL: a bare server address, or any stream URL."""
    base, _ = parse_openstream_url(url)
    if base:
        return base
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if parts.scheme in ("http", "https") and parts.netloc:
        return f"{parts.scheme}://{parts.netloc}"
    return None


def test_openstream_connection_response(
    *,
    payload: Optional[Dict[str, Any]],
    get_openstream_config: Callable[[], Any],
):
    """Check that an OpenStream server accepts the (entered or saved) API key by
    reading its fleet summary, the cheapest authenticated endpoint."""
    data = payload or {}
    config = get_openstream_config()
    url = (data.get("test_url") or config.get_test_url() or "").strip()
    api_key = (data.get("api_key") or config.get_api_key() or "").strip()
    base = _server_base(url)
    if not base:
        return jsonify({
            "success": False,
            "error": "Enter the OpenStream server URL (e.g. http://openstream:6878) or any of its stream URLs",
        }), 400
    headers = {"X-API-Key": api_key} if api_key else {}
    try:
        resp = requests.get(f"{base}/api/fleet", headers=headers, timeout=_TEST_TIMEOUT)
    except requests.exceptions.Timeout:
        return jsonify({"success": False, "error": f"Timed out connecting to {base}"}), 400
    except requests.exceptions.RequestException:
        return jsonify({"success": False, "error": f"Could not connect to {base}"}), 400

    if resp.status_code == 200:
        return jsonify({"success": True, "message": f"Connected to OpenStream at {base}"})
    errors = {
        401: "OpenStream rejected the API key (wrong or revoked)",
        403: "OpenStream requires an API key: create one in OpenStream under Settings → API Keys",
        428: "OpenStream has no account yet: finish its first-run setup, then create an API key",
    }
    if resp.status_code == 403 and api_key:
        # A key was sent but the server still answered like a keyless request:
        # an OpenStream build from before API keys, or a proxy stripping the header.
        errors[403] = "OpenStream refused the request (proxy stripping X-API-Key, or an unexpected server)"
    msg = errors.get(resp.status_code, f"Unexpected response from {base}: HTTP {resp.status_code}")
    return jsonify({"success": False, "error": msg}), 400
