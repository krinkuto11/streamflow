"""Dispatcharr API handler functions extracted from web_api."""

import os
import threading
from typing import Any, Callable, Dict, Optional

import requests
from flask import jsonify

from apps.core.logging_config import setup_logging

logger = setup_logging(__name__)


def get_dispatcharr_config_response(*, get_dispatcharr_config: Callable[[], Any]):
    """Get current Dispatcharr configuration without exposing password."""
    try:
        config_manager = get_dispatcharr_config()
        config = config_manager.get_config()
        return jsonify(config)
    except Exception as exc:
        logger.error(f"Error getting Dispatcharr config: {exc}")
        return jsonify({"error": "Internal Server Error"}), 500


def update_dispatcharr_config_response(
    *,
    payload: Optional[Dict[str, Any]],
    get_dispatcharr_config: Callable[[], Any],
    get_udi_manager: Callable[[], Any],
):
    """Update Dispatcharr configuration and trigger UDI refresh when configured."""
    try:
        data = payload
        if not data:
            return jsonify({"error": "No configuration data provided"}), 400

        config_manager = get_dispatcharr_config()

        base_url = data.get("base_url")
        username = data.get("username")
        password = data.get("password")

        success = config_manager.update_config(base_url=base_url, username=username, password=password)

        if not success:
            return jsonify({"error": "Failed to save configuration"}), 500

        if base_url is not None:
            os.environ["DISPATCHARR_BASE_URL"] = base_url.strip()
        if username is not None:
            os.environ["DISPATCHARR_USER"] = username.strip()
        if password is not None:
            os.environ["DISPATCHARR_PASS"] = password

        os.environ["DISPATCHARR_TOKEN"] = ""

        if config_manager.is_configured():
            try:
                logger.info("Dispatcharr credentials updated, triggering background UDI Manager initialize...")
                udi = get_udi_manager()
                threading.Thread(target=udi.initialize, kwargs={"force_refresh": True}, daemon=True).start()
                logger.info("UDI Manager initialization started in background")
            except Exception as exc:
                logger.warning(
                    f"Failed to initialize UDI Manager after config update: {exc}. "
                    f"Data may not be available until manual refresh or application restart."
                )

        return jsonify({"message": "Dispatcharr configuration updated successfully"})
    except Exception as exc:
        logger.error(f"Error updating Dispatcharr config: {exc}")
        return jsonify({"error": "Internal Server Error"}), 500


def test_dispatcharr_connection_response(
    *,
    payload: Optional[Dict[str, Any]],
    get_dispatcharr_config: Callable[[], Any],
):
    """Test Dispatcharr connection with provided or existing credentials."""
    try:
        data = payload or {}
        config_manager = get_dispatcharr_config()

        test_base_url = data.get("base_url") or config_manager.get_base_url()
        test_username = data.get("username") or config_manager.get_username()
        test_password = data.get("password") or config_manager.get_password()

        if not all([test_base_url, test_username, test_password]):
            return (
                jsonify(
                    {
                        "success": False,
                        "error": "Missing required credentials (base_url, username, password)",
                    }
                ),
                400,
            )

        login_url = f"{test_base_url}/api/accounts/token/"

        try:
            resp = requests.post(
                login_url,
                headers={"Content-Type": "application/json"},
                json={"username": test_username, "password": test_password},
                timeout=10,
            )
            resp.raise_for_status()
            body = resp.json()
            token = body.get("access") or body.get("token")

            if token:
                channels_url = f"{test_base_url}/api/channels/channels/"
                channels_resp = requests.get(
                    channels_url,
                    headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                    params={"page_size": 1},
                    timeout=10,
                )

                if channels_resp.status_code == 200:
                    return jsonify({"success": True, "message": "Connection successful"})
                return (
                    jsonify(
                        {
                            "success": False,
                            "error": "Authentication successful but failed to fetch channels",
                        }
                    ),
                    400,
                )

            return jsonify({"success": False, "error": "No token received from Dispatcharr"}), 400
        except requests.exceptions.Timeout:
            return (
                jsonify(
                    {
                        "success": False,
                        "error": "Connection timeout. Please check the URL and network connectivity.",
                    }
                ),
                400,
            )
        except requests.exceptions.ConnectionError:
            return jsonify({"success": False, "error": "Could not connect to Dispatcharr. Please check the URL."}), 400
        except requests.exceptions.HTTPError as exc:
            if exc.response.status_code == 401:
                return jsonify({"success": False, "error": "Invalid username or password"}), 401
            return jsonify({"success": False, "error": f"HTTP error: {exc.response.status_code}"}), 400
        except Exception as exc:
            return jsonify({"success": False, "error": f"Connection failed: {str(exc)}"}), 400
    except Exception as exc:
        logger.error(f"Error testing Dispatcharr connection: {exc}")
        return jsonify({"error": "Internal Server Error"}), 500


def get_udi_initialization_status_response(*, get_udi_manager: Callable[[], Any]):
    """Get current UDI initialization progress."""
    try:
        udi = get_udi_manager()
        progress = udi.get_init_progress()
        return jsonify(progress)
    except Exception as exc:
        logger.error(f"Error getting UDI initialization status: {exc}")
        return jsonify({"error": "Internal Server Error"}), 500


def initialize_udi_response(*, get_dispatcharr_config: Callable[[], Any], get_udi_manager: Callable[[], Any]):
    """Initialize UDI manager with Dispatcharr credentials and return counts."""
    try:
        config_manager = get_dispatcharr_config()

        if not config_manager.is_configured():
            return (
                jsonify(
                    {
                        "success": False,
                        "error": "Dispatcharr is not fully configured. Please provide base_url, username, and password.",
                    }
                ),
                400,
            )

        logger.info("Initializing UDI Manager with fresh data from Dispatcharr...")
        udi = get_udi_manager()

        success = udi.initialize(force_refresh=True)

        if success:
            channels = udi.get_channels()
            streams = udi.get_streams()
            m3u_accounts = udi.get_m3u_accounts()

            logger.info(
                f"UDI Manager initialized successfully: {len(channels)} channels, {len(streams)} streams, {len(m3u_accounts)} M3U accounts"
            )

            return jsonify(
                {
                    "success": True,
                    "message": "UDI Manager initialized successfully",
                    "data": {
                        "channels_count": len(channels),
                        "streams_count": len(streams),
                        "m3u_accounts_count": len(m3u_accounts),
                    },
                }
            )

        logger.error("Failed to initialize UDI Manager")
        return (
            jsonify(
                {
                    "success": False,
                    "error": "Failed to initialize UDI Manager. Please check the logs for details.",
                }
            ),
            500,
        )

    except Exception as exc:
        logger.error(f"Error initializing UDI Manager: {exc}", exc_info=True)
        return jsonify({"success": False, "error": str(exc)}), 500
