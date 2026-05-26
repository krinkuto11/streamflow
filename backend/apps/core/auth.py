"""
Canonical Dispatcharr authentication module.

Provides token management, login, and auth-header construction used by both
the UDI fetcher (apps.udi.fetcher) and the API utility layer (apps.core.api_utils).

Neither module should define its own copies of these functions — import from here.
This module must NOT import from apps.udi or apps.core.api_utils to avoid circular
imports (fetcher ← manager ← api_utils, both of which depend on auth).
"""

import json
import os
import threading
import time
from pathlib import Path
from typing import Dict, Optional

import requests
from dotenv import load_dotenv, set_key

from apps.config.dispatcharr_config import get_dispatcharr_config
from apps.core.logging_config import setup_logging, log_api_request, log_api_response

logger = setup_logging(__name__)

env_path = Path(".") / ".env"

if env_path.exists():
    load_dotenv(dotenv_path=env_path)

# ---------------------------------------------------------------------------
# Shared state — single instances shared by all importers
# ---------------------------------------------------------------------------

_token_validation_cache: Dict[str, float] = {}
TOKEN_VALIDATION_TTL = int(os.getenv("TOKEN_VALIDATION_TTL", "60"))

# Serialises concurrent token-refresh calls so a burst of 401 responses
# only triggers one login() and one .env write.
_token_refresh_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_base_url() -> Optional[str]:
    config = get_dispatcharr_config()
    return config.get_base_url()


def _validate_token(token: str) -> bool:
    """Return True if token is accepted by Dispatcharr, using a TTL cache."""
    if not token:
        return False
    return _validate_auth_headers(
        {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
        cache_key=f"bearer:{token}",
    )


def _validate_auth_headers(headers: Dict[str, str], cache_key: Optional[str] = None) -> bool:
    """Return True if Dispatcharr accepts the provided auth headers."""
    base_url = _get_base_url()
    if not base_url or not headers:
        return False

    cache_key = cache_key or headers.get("Authorization") or headers.get("X-API-Key") or ""
    if not cache_key:
        return False

    now = time.time()
    cached_time = _token_validation_cache.get(cache_key)
    if cached_time is not None:
        age = now - cached_time
        if age < TOKEN_VALIDATION_TTL:
            logger.debug(f"Dispatcharr auth validation cached (age: {age:.1f}s)")
            return True
        logger.debug(f"Dispatcharr auth validation cache expired (age: {age:.1f}s)")

    try:
        test_url = f"{base_url}/api/channels/channels/"
        log_api_request(logger, "GET", test_url, params={"page_size": 1})
        start = time.time()
        resp = requests.get(test_url, headers=headers, timeout=5, params={"page_size": 1})
        elapsed = time.time() - start
        log_api_response(logger, "GET", test_url, resp.status_code, elapsed)

        if resp.status_code == 200:
            _token_validation_cache[cache_key] = start
            logger.debug(f"Dispatcharr auth validated and cached for {TOKEN_VALIDATION_TTL}s")
            return True
        _token_validation_cache.pop(cache_key, None)
        return False
    except Exception:
        _token_validation_cache.pop(cache_key, None)
        return False


def _clear_token_validation_cache() -> None:
    _token_validation_cache.clear()
    logger.debug("Token validation cache cleared")


def _login() -> bool:
    """Authenticate with Dispatcharr and persist the token.

    Returns True on success, False on any failure.
    """
    config = get_dispatcharr_config()
    if config.get_auth_mode() == "api_key":
        logger.info("Dispatcharr API key auth is configured; token login is not required.")
        return bool(config.get_api_key())

    username = config.get_username()
    password = config.get_password()
    base_url = config.get_base_url()

    if not all([username, password, base_url]):
        logger.error(
            "DISPATCHARR_USER, DISPATCHARR_PASS, and DISPATCHARR_BASE_URL must all be configured."
        )
        return False

    login_url = f"{base_url}/api/accounts/token/"
    logger.info(f"Attempting login to {base_url}...")

    try:
        log_api_request(logger, "POST", login_url, json={"username": username, "password": "***"})
        start = time.time()
        resp = requests.post(
            login_url,
            headers={"Content-Type": "application/json"},
            json={"username": username, "password": password},
            timeout=10,
        )
        elapsed = time.time() - start
        log_api_response(logger, "POST", login_url, resp.status_code, elapsed)
        resp.raise_for_status()

        data = resp.json()
        token = data.get("access") or data.get("token")

        if token:
            _clear_token_validation_cache()
            if env_path.exists():
                set_key(env_path, "DISPATCHARR_TOKEN", token)
                logger.info("Login successful — token saved to .env.")
            else:
                os.environ["DISPATCHARR_TOKEN"] = token
                logger.info("Login successful — token stored in memory (no .env file).")
            return True

        logger.error("Login failed: no access token in response.")
        return False

    except requests.exceptions.RequestException as e:
        if hasattr(e, "response") and e.response is not None:
            logger.error(f"Login request failed: {e} — {e.response.text}")
        else:
            logger.error(f"Login request failed: {e}")
        return False
    except json.JSONDecodeError:
        logger.error("Login failed: invalid JSON response from server.")
        return False


def _get_auth_headers() -> Dict[str, str]:
    """Return Authorization headers, logging in first if no token is present.

    Raises RuntimeError if login fails or the token is missing after login.
    """
    config = get_dispatcharr_config()
    if config.get_auth_mode() == "api_key":
        api_key = config.get_api_key()
        if not api_key:
            raise RuntimeError("Dispatcharr API key is not configured")
        return {
            "Authorization": f"ApiKey {api_key}",
            "X-API-Key": api_key,
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    current_token = os.getenv("DISPATCHARR_TOKEN")

    if current_token:
        return {
            "Authorization": f"Bearer {current_token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    logger.info("DISPATCHARR_TOKEN not found — attempting login...")
    if _login():
        if env_path.exists():
            load_dotenv(dotenv_path=env_path, override=True)
        current_token = os.getenv("DISPATCHARR_TOKEN")
        if not current_token:
            raise RuntimeError("Login succeeded but DISPATCHARR_TOKEN not found in environment")
    else:
        raise RuntimeError("Dispatcharr login failed — check DISPATCHARR_USER / DISPATCHARR_PASS")

    return {
        "Authorization": f"Bearer {current_token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


def _refresh_token() -> bool:
    """Refresh the token, serialised so only one thread runs login() at a time."""
    with _token_refresh_lock:
        config = get_dispatcharr_config()
        if config.get_auth_mode() == "api_key":
            logger.info("Dispatcharr API key auth is configured; token refresh is not available.")
            return False

        logger.info("Token expired or invalid — refreshing...")
        if _login():
            if env_path.exists():
                load_dotenv(dotenv_path=env_path, override=True)
            logger.info("Token refreshed successfully.")
            return True
        logger.error("Token refresh failed.")
        return False
