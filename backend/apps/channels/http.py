"""Shared HTTP helper for Dispatcharr channel upload scripts."""

from typing import Any

import requests

from apps.core.auth import _get_auth_headers, _get_base_url, _refresh_token
from apps.core.logging_config import setup_logging

logger = setup_logging(__name__)


def make_request(method: str, url: str, **kwargs: Any) -> requests.Response:
    """Make an authenticated request with one automatic token-refresh retry.

    Args:
        method: HTTP method (GET, POST, PATCH, …).
        url: Target URL.
        **kwargs: Passed through to requests.request().

    Returns:
        requests.Response with a 2xx status code.

    Raises:
        requests.exceptions.RequestException: on network or HTTP errors.
    """
    try:
        resp = requests.request(method, url, headers=_get_auth_headers(), **kwargs)
        resp.raise_for_status()
        return resp
    except requests.exceptions.HTTPError as e:
        if e.response.status_code == 401:
            if _refresh_token():
                logger.info(f"Retrying {method} {url} with refreshed token...")
                resp = requests.request(method, url, headers=_get_auth_headers(), **kwargs)
                resp.raise_for_status()
                return resp
            raise
        logger.error(f"HTTP {e.response.status_code} for {url}: {e.response.text}")
        raise
    except requests.exceptions.RequestException as e:
        logger.error(f"Request failed: {e}")
        raise
