"""System and frontend handler functions extracted from web_api."""

import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict

import requests
from flask import jsonify, send_file, send_from_directory
from werkzeug.utils import safe_join

from apps.core.logging_config import setup_logging

logger = setup_logging(__name__)


# In-memory cache for public IP - refreshed at most once every 15 minutes.
_env_cache: Dict[str, Any] = {"public_ip": None, "fetched_at": 0.0}
_ENV_CACHE_TTL = 900  # seconds


def root_response(*, static_folder: Path):
    """Serve root frontend entrypoint with API fallback when not built."""
    try:
        return send_file(static_folder / "index.html")
    except FileNotFoundError:
        return jsonify(
            {
                "message": "StreamFlow for Dispatcharr API",
                "version": "1.0",
                "endpoints": {
                    "health": "/api/health",
                    "docs": "/api/health",
                    "frontend": "React frontend not found. Build frontend and place in static/ directory.",
                },
            }
        )


def health_check_response():
    """Return service health status with current timestamp."""
    return jsonify({"status": "healthy", "timestamp": datetime.now().isoformat()})


def get_version_response(*, current_file: Path):
    """Get application version from env var or known artifact locations."""
    try:
        env_version = os.getenv("STREAMFLOW_VERSION")
        if env_version:
            return jsonify({"version": env_version})

        candidate_files = [
            current_file.parent / "version.txt",
            current_file.parents[2] / "version.txt",
            current_file.parents[2] / "static" / "version.txt",
        ]

        version = "dev-unknown"
        for version_file in candidate_files:
            if version_file.exists():
                value = version_file.read_text().strip()
                if value:
                    version = value
                    break

        return jsonify({"version": version})
    except Exception as exc:
        logger.error(f"Failed to read version: {exc}")
        return jsonify({"version": "dev-unknown"})


def get_environment_response():
    """Get environment info including cached public IP and debug mode flag."""
    now = time.time()
    if _env_cache["public_ip"] is None or (now - _env_cache["fetched_at"]) >= _ENV_CACHE_TTL:
        try:
            resp = requests.get("https://api64.ipify.org?format=json", timeout=5)
            resp.raise_for_status()
            _env_cache["public_ip"] = resp.json().get("ip")
            _env_cache["fetched_at"] = now
        except requests.RequestException as exc:
            logger.warning(f"Failed to fetch public IP: {exc}")
            # Keep existing cache values on transient failures.

    # debug_mode is exposed so the frontend can conditionally render dev-only
    # tooling (e.g. the UDI fault injection panel).  It is derived purely from
    # the DEBUG_MODE env var and is always False in production containers where
    # that var is not set.
    debug_mode = os.getenv("DEBUG_MODE", "false").lower() in ("true", "1", "yes", "on")

    return jsonify(
        {
            "public_ip": _env_cache["public_ip"],
            "country_code": None,
            "country_name": None,
            "debug_mode": debug_mode,
        }
    )


def serve_frontend_response(*, static_folder: Path, path: str):
    """Serve static frontend assets or fallback to index.html for SPA routes."""
    resolved_path_str = safe_join(str(static_folder), path)
    if resolved_path_str is None:
        return jsonify({"error": "Invalid path"}), 400

    resolved_path = Path(resolved_path_str)
    if resolved_path.exists() and resolved_path.is_file():
        return send_from_directory(static_folder, path)

    try:
        return send_file(static_folder / "index.html")
    except FileNotFoundError:
        return jsonify({"error": "Frontend not found"}), 404
