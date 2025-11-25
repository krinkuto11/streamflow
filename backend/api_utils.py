"""
API utilities for interacting with the Dispatcharr API.

This module provides authentication, request handling, and helper functions
for communicating with the Dispatcharr API endpoints.

This module now integrates with the caching layer to reduce redundant API calls.
"""

import os
import json
import sys
import time
from typing import Dict, List, Optional, Any, Tuple
import requests
from pathlib import Path
from dotenv import load_dotenv, set_key

from logging_config import (
    setup_logging, log_function_call, log_function_return,
    log_exception, log_api_request, log_api_response
)
from dispatcharr_cache import get_cache

# Setup logging for this module
logger = setup_logging(__name__)

# Standard timeout for Dispatcharr API requests
# Tuple of (connect_timeout, read_timeout) to prevent hanging on connection issues
API_TIMEOUT = (10, 30)

env_path = Path('.') / '.env'

# Load environment variables from .env file if it exists
# This allows fallback to .env file while supporting env vars
if env_path.exists():
    load_dotenv(dotenv_path=env_path)
    logger.debug(f"Loaded environment from {env_path}")


def _get_base_url() -> Optional[str]:
    """
    Get the base URL from environment variables.
    
    Returns:
        Optional[str]: The Dispatcharr base URL or None if not set.
    """
    return os.getenv("DISPATCHARR_BASE_URL")

def _validate_token(token: str) -> bool:
    """
    Validate if a token is still valid by making a test API request.
    
    Args:
        token: The authentication token to validate
        
    Returns:
        bool: True if token is valid, False otherwise
    """
    log_function_call(logger, "_validate_token", token="<redacted>")
    base_url = _get_base_url()
    if not base_url or not token:
        logger.debug("Validation failed: missing base_url or token")
        return False
    
    try:
        start_time = time.time()
        # Make a lightweight API call to validate token
        test_url = f"{base_url}/api/channels/channels/"
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Content-Type": "application/json"
        }
        log_api_request(logger, "GET", test_url, params={'page_size': 1})
        resp = requests.get(test_url, headers=headers, timeout=5, params={'page_size': 1})
        elapsed = time.time() - start_time
        log_api_response(logger, "GET", test_url, resp.status_code, elapsed)
        
        result = resp.status_code == 200
        log_function_return(logger, "_validate_token", result, elapsed)
        return result
    except Exception as e:
        log_exception(logger, e, "_validate_token")
        return False

def login() -> bool:
    """
    Log into Dispatcharr and save the token to .env file.
    
    Authenticates with Dispatcharr using credentials from environment
    variables. Stores the received token in .env file if it exists,
    otherwise stores it in memory.
    
    Returns:
        bool: True if login successful, False otherwise.
    """
    log_function_call(logger, "login")
    username = os.getenv("DISPATCHARR_USER")
    password = os.getenv("DISPATCHARR_PASS")
    base_url = _get_base_url()

    if not all([username, password, base_url]):
        logger.error(
            "DISPATCHARR_USER, DISPATCHARR_PASS, and "
            "DISPATCHARR_BASE_URL must be set in the .env file."
        )
        return False

    login_url = f"{base_url}/api/accounts/token/"
    logger.info(f"Attempting to log in to {base_url}...")

    try:
        start_time = time.time()
        log_api_request(logger, "POST", login_url, json={"username": username, "password": "***"})
        resp = requests.post(
            login_url,
            headers={"Content-Type": "application/json"},
            json={"username": username, "password": password},
            timeout=API_TIMEOUT
        )
        elapsed = time.time() - start_time
        log_api_response(logger, "POST", login_url, resp.status_code, elapsed)
        
        resp.raise_for_status()
        data = resp.json()
        token = data.get("access") or data.get("token")

        if token:
            logger.debug(f"Received token (length: {len(token)})")
            # Save token to .env if exists, else store in memory
            if env_path.exists():
                set_key(env_path, "DISPATCHARR_TOKEN", token)
                logger.info("Login successful. Token saved.")
            else:
                # Token needs refresh on restart when no .env file
                os.environ["DISPATCHARR_TOKEN"] = token
                logger.info(
                    "Login successful. Token stored in memory."
                )
            log_function_return(logger, "login", True, elapsed)
            return True
        else:
            logger.error(
                "Login failed: No access token found in response."
            )
            logger.debug(f"Response data: {data}")
            return False
    except requests.exceptions.ConnectTimeout:
        logger.error("Connection timeout during login - Dispatcharr may be unreachable")
        return False
    except requests.exceptions.ReadTimeout:
        logger.error("Read timeout during login - Dispatcharr may be slow to respond")
        return False
    except requests.exceptions.ConnectionError as e:
        logger.error(f"Connection error during login: {e}")
        return False
    except requests.exceptions.RequestException as e:
        log_exception(logger, e, "login")
        if hasattr(e, 'response') and e.response is not None:
            logger.error(f"Response content: {e.response.text}")
        return False
    except json.JSONDecodeError as e:
        log_exception(logger, e, "login - JSON decode")
        logger.error(
            "Login failed: Invalid JSON response from server."
        )
        return False

def _get_auth_headers() -> Dict[str, str]:
    """
    Get authorization headers for API requests.
    
    Retrieves the authentication token from environment variables.
    If no token is found or token is invalid, attempts to log in first.
    
    Returns:
        Dict[str, str]: Dictionary containing authorization headers.
        
    Raises:
        SystemExit: If login fails or token cannot be retrieved.
    """
    log_function_call(logger, "_get_auth_headers")
    current_token = os.getenv("DISPATCHARR_TOKEN")
    
    # If token exists, validate it before using
    if current_token and _validate_token(current_token):
        logger.debug("Using existing valid token")
        return {
            "Authorization": f"Bearer {current_token}",
            "Accept": "application/json",
            "Content-Type": "application/json"
        }
    
    # Token is missing or invalid, need to login
    if current_token:
        logger.info("Existing token is invalid. Attempting to log in...")
    else:
        logger.info("DISPATCHARR_TOKEN not found. Attempting to log in...")
    
    if login():
        # Reload from .env file only if it exists
        if env_path.exists():
            load_dotenv(dotenv_path=env_path, override=True)
            logger.debug("Reloaded environment variables after login")
        current_token = os.getenv("DISPATCHARR_TOKEN")
        if not current_token:
            logger.error(
                "Login succeeded, but token not found. Aborting."
            )
            sys.exit(1)
    else:
        logger.error(
            "Login failed. Check credentials. Aborting."
        )
        sys.exit(1)

    log_function_return(logger, "_get_auth_headers", "<headers with token>")
    return {
        "Authorization": f"Bearer {current_token}",
        "Accept": "application/json",
        "Content-Type": "application/json"
    }

def _refresh_token() -> bool:
    """
    Refresh the authentication token.
    
    Attempts to refresh the authentication token by calling the login
    function. If successful, reloads environment variables.
    
    Returns:
        bool: True if refresh successful, False otherwise.
    """
    logger.info("Token expired or invalid. Attempting to refresh...")
    if login():
        # Reload from .env file only if it exists
        if env_path.exists():
            load_dotenv(dotenv_path=env_path, override=True)
        logger.info("Token refreshed successfully.")
        return True
    else:
        logger.error("Token refresh failed.")
        return False

def fetch_data_from_url(url: str) -> Optional[Any]:
    """
    Fetch data from a given URL with authentication and retry logic.
    
    Makes an authenticated GET request to the specified URL. If the
    request fails with a 401 error, automatically refreshes the token
    and retries once.
    
    Parameters:
        url (str): The URL to fetch data from.
        
    Returns:
        Optional[Any]: JSON response data if successful, None otherwise.
    """
    log_function_call(logger, "fetch_data_from_url", url=url[:80] if len(url) > 80 else url)
    start_time = time.time()
    
    try:
        log_api_request(logger, "GET", url)
        resp = requests.get(url, headers=_get_auth_headers(), timeout=API_TIMEOUT)
        elapsed = time.time() - start_time
        log_api_response(logger, "GET", url, resp.status_code, elapsed)
        
        resp.raise_for_status()
        data = resp.json()
        
        # Log summary of response data
        if isinstance(data, dict):
            logger.debug(f"Response contains dict with {len(data)} keys")
        elif isinstance(data, list):
            logger.debug(f"Response contains list with {len(data)} items")
        
        log_function_return(logger, "fetch_data_from_url", f"<data: {type(data).__name__}>", elapsed)
        return data
    except requests.exceptions.HTTPError as e:
        if e.response.status_code == 401:
            logger.debug("Got 401 response, attempting token refresh")
            if _refresh_token():
                logger.info("Retrying request with new token...")
                retry_start = time.time()
                log_api_request(logger, "GET", url)
                resp = requests.get(url, headers=_get_auth_headers(), timeout=API_TIMEOUT)
                retry_elapsed = time.time() - retry_start
                log_api_response(logger, "GET", url, resp.status_code, retry_elapsed)
                
                resp.raise_for_status()
                data = resp.json()
                total_elapsed = time.time() - start_time
                log_function_return(logger, "fetch_data_from_url", f"<data: {type(data).__name__}>", total_elapsed)
                return data
            else:
                logger.error("Token refresh failed")
                return None
        else:
            log_exception(logger, e, f"fetch_data_from_url ({url})")
            return None
    except requests.exceptions.ConnectTimeout:
        logger.error(f"Connection timeout fetching {url} - Dispatcharr may be unreachable")
        return None
    except requests.exceptions.ReadTimeout:
        logger.error(f"Read timeout fetching {url} - response took too long")
        return None
    except requests.exceptions.ConnectionError as e:
        logger.error(f"Connection error fetching {url}: {e}")
        return None
    except requests.exceptions.RequestException as e:
        log_exception(logger, e, f"fetch_data_from_url ({url})")
        return None

def patch_request(url: str, payload: Dict[str, Any]) -> requests.Response:
    """
    Send a PATCH request with authentication and retry logic.
    
    Makes an authenticated PATCH request to the specified URL. If the
    request fails with a 401 error, automatically refreshes the token
    and retries once.
    
    Parameters:
        url (str): The URL to send the PATCH request to.
        payload (Dict[str, Any]): The JSON payload to send.
        
    Returns:
        requests.Response: The response object from the request.
        
    Raises:
        requests.exceptions.RequestException: If request fails.
    """
    try:
        resp = requests.patch(
            url, json=payload, headers=_get_auth_headers(), timeout=API_TIMEOUT
        )
        resp.raise_for_status()
        return resp
    except requests.exceptions.HTTPError as e:
        if e.response.status_code == 401:
            if _refresh_token():
                logger.info("Retrying PATCH request with new token...")
                resp = requests.patch(
                    url, json=payload, headers=_get_auth_headers(), timeout=API_TIMEOUT
                )
                resp.raise_for_status()
                return resp
            else:
                raise
        else:
            logger.error(
                f"Error patching data to {url}: {e.response.text}"
            )
            raise
    except requests.exceptions.ConnectTimeout:
        logger.error(f"Connection timeout patching {url} - Dispatcharr may be unreachable")
        raise
    except requests.exceptions.ReadTimeout:
        logger.error(f"Read timeout patching {url} - response took too long")
        raise
    except requests.exceptions.ConnectionError as e:
        logger.error(f"Connection error patching {url}: {e}")
        raise
    except requests.exceptions.RequestException as e:
        logger.error(f"Error patching data to {url}: {e}")
        raise

def post_request(url: str, payload: Dict[str, Any]) -> requests.Response:
    """
    Send a POST request with authentication and retry logic.
    
    Makes an authenticated POST request to the specified URL. If the
    request fails with a 401 error, automatically refreshes the token
    and retries once.
    
    Parameters:
        url (str): The URL to send the POST request to.
        payload (Dict[str, Any]): The JSON payload to send.
        
    Returns:
        requests.Response: The response object from the request.
        
    Raises:
        requests.exceptions.RequestException: If request fails.
    """
    try:
        resp = requests.post(
            url, json=payload, headers=_get_auth_headers(), timeout=API_TIMEOUT
        )
        resp.raise_for_status()
        return resp
    except requests.exceptions.HTTPError as e:
        if e.response.status_code == 401:
            if _refresh_token():
                logger.info("Retrying POST request with new token...")
                resp = requests.post(
                    url, json=payload, headers=_get_auth_headers(), timeout=API_TIMEOUT
                )
                resp.raise_for_status()
                return resp
            else:
                raise
        else:
            logger.error(
                f"Error posting data to {url}: {e.response.text}"
            )
            raise
    except requests.exceptions.ConnectTimeout:
        logger.error(f"Connection timeout posting to {url} - Dispatcharr may be unreachable")
        raise
    except requests.exceptions.ReadTimeout:
        logger.error(f"Read timeout posting to {url} - response took too long")
        raise
    except requests.exceptions.ConnectionError as e:
        logger.error(f"Connection error posting to {url}: {e}")
        raise
    except requests.exceptions.RequestException as e:
        logger.error(f"Error posting data to {url}: {e}")
        raise

def fetch_channel_streams(channel_id: int) -> Optional[List[Dict[str, Any]]]:
    """
    Fetch streams for a given channel ID.
    
    If caching is enabled, returns cached data. Otherwise, fetches from API.
    
    Parameters:
        channel_id (int): The ID of the channel.
        
    Returns:
        Optional[List[Dict[str, Any]]]: List of stream objects or None.
    """
    cache = get_cache()
    
    def _fetch():
        url = (
            f"{_get_base_url()}/api/channels/channels/{channel_id}/"
            f"streams/"
        )
        return fetch_data_from_url(url)
    
    # Use cache if enabled, otherwise fetch directly
    if cache.is_enabled():
        return cache.get_channel_streams(channel_id, _fetch)
    else:
        return _fetch()


def update_channel_streams(
    channel_id: int, stream_ids: List[int], valid_stream_ids: Optional[set] = None,
    allow_dead_streams: bool = False,
    stream_id_to_url: Optional[Dict[int, str]] = None
) -> bool:
    """
    Update the streams for a given channel ID.
    
    Filters out stream IDs that no longer exist in Dispatcharr and dead streams
    to prevent adding dead/removed streams back to channels.
    
    Parameters:
        channel_id (int): The ID of the channel to update.
        stream_ids (List[int]): List of stream IDs to assign.
        valid_stream_ids (Optional[set]): Set of valid stream IDs. If None,
            will fetch from API. Pass this to avoid redundant API calls when
            updating multiple channels.
        allow_dead_streams (bool): If True, allows dead streams (used during
            global checks to give dead streams a second chance). Default False.
        stream_id_to_url (Optional[Dict[int, str]]): Mapping of stream IDs to URLs.
            If None, will fetch from API. Pass this to avoid redundant API calls
            when updating multiple channels.
        
    Returns:
        bool: True if update successful, False otherwise.
        
    Raises:
        Exception: If the API request fails.
    """
    # Filter out stream IDs that no longer exist in Dispatcharr
    if valid_stream_ids is None:
        valid_stream_ids = get_valid_stream_ids()
    
    original_count = len(stream_ids)
    filtered_stream_ids = [sid for sid in stream_ids if sid in valid_stream_ids]
    
    non_existent_count = original_count - len(filtered_stream_ids)
    if non_existent_count > 0:
        logger.warning(
            f"Filtered out {non_existent_count} non-existent stream(s) for channel {channel_id}"
        )
    
    # Filter out dead streams unless allow_dead_streams is True (e.g., during global checks)
    if not allow_dead_streams:
        filtered_stream_ids, dead_count = filter_dead_streams(filtered_stream_ids, stream_id_to_url)
        if dead_count > 0:
            logger.warning(
                f"Filtered out {dead_count} dead stream(s) for channel {channel_id}"
            )
    
    url = f"{_get_base_url()}/api/channels/channels/{channel_id}/"
    data = {"streams": filtered_stream_ids}
    
    try:
        response = patch_request(url, data)
        if response and response.status_code in [200, 204]:
            logger.info(
                f"Successfully updated channel {channel_id} with "
                f"{len(filtered_stream_ids)} streams"
            )
            return True
        else:
            status = response.status_code if response else 'None'
            logger.warning(
                f"Unexpected response for channel {channel_id}: "
                f"{status}"
            )
            return False
    except Exception as e:
        logger.error(
            f"Failed to update channel {channel_id} streams: {e}"
        )
        raise

def refresh_m3u_playlists(
    account_id: Optional[int] = None
) -> requests.Response:
    """
    Trigger refresh of M3U playlists.
    
    If account_id is None, refreshes all M3U playlists. Otherwise,
    refreshes only the specified account.
    
    Parameters:
        account_id (Optional[int]): The account ID to refresh,
            or None for all accounts.
            
    Returns:
        requests.Response: The response object from the request.
        
    Raises:
        Exception: If the API request fails.
    """
    base_url = _get_base_url()
    if account_id:
        url = f"{base_url}/api/m3u/refresh/{account_id}/"
    else:
        url = f"{base_url}/api/m3u/refresh/"
    
    try:
        resp = post_request(url, {})
        logger.info("M3U refresh initiated successfully")
        return resp
    except Exception as e:
        logger.error(f"Failed to refresh M3U playlists: {e}")
        raise


def get_m3u_accounts() -> Optional[List[Dict[str, Any]]]:
    """
    Fetch all M3U accounts.
    
    Returns:
        Optional[List[Dict[str, Any]]]: List of M3U account objects
            or None if request fails.
    """
    url = f"{_get_base_url()}/api/m3u/accounts/"
    return fetch_data_from_url(url)

def get_streams(log_result: bool = True) -> List[Dict[str, Any]]:
    """
    Fetch all available streams with pagination support.
    
    Fetches all streams from the Dispatcharr API, handling pagination
    automatically. Uses page_size=100 to minimize API calls.
    
    If caching is enabled, returns cached data on subsequent calls.
    
    Parameters:
        log_result (bool): Whether to log the number of fetched streams.
            Default is True. Set to False to avoid duplicate log entries.
    
    Returns:
        List[Dict[str, Any]]: List of all stream objects.
    """
    cache = get_cache()
    
    def _fetch():
        """Internal function to fetch streams from API."""
        base_url = _get_base_url()
        # Use page_size parameter to maximize streams per request
        url = f"{base_url}/api/channels/streams/?page_size=100"
        
        all_streams: List[Dict[str, Any]] = []
        
        while url:
            response = fetch_data_from_url(url)
            if not response:
                break
            
            # Handle paginated response
            if isinstance(response, dict) and 'results' in response:
                all_streams.extend(response.get('results', []))
                url = response.get('next')  # Get next page URL
            else:
                # If response is list (non-paginated), use it directly
                if isinstance(response, list):
                    all_streams.extend(response)
                break
        
        if log_result:
            logger.info(f"Fetched {len(all_streams)} total streams")
        return all_streams
    
    # Use cache if enabled, otherwise fetch directly
    if cache.is_enabled():
        return cache.get_streams(_fetch)
    else:
        return _fetch()


def get_valid_stream_ids() -> set:
    """
    Get a set of all valid stream IDs that currently exist in Dispatcharr.
    
    This is used to filter out stream IDs that no longer exist (e.g., removed
    from M3U playlists) before updating channels.
    
    If caching is enabled, uses cached stream data.
    
    Returns:
        set: Set of valid stream IDs.
    """
    cache = get_cache()
    
    def _fetch():
        return get_streams(log_result=False)
    
    try:
        if cache.is_enabled():
            # Use cache to get valid IDs
            return cache.get_valid_stream_ids(_fetch)
        else:
            # Original behavior - fetch directly
            all_streams = get_streams(log_result=False)
            valid_ids = {stream['id'] for stream in all_streams if isinstance(stream, dict) and 'id' in stream}
            return valid_ids
    except Exception as e:
        logger.error(f"Failed to fetch valid stream IDs: {e}")
        # Return empty set on error - this will cause all stream IDs to be filtered out
        # which is safer than allowing potentially invalid IDs
        return set()


def get_dead_stream_urls() -> set:
    """
    Get a set of URLs for streams marked as dead in the DeadStreamsTracker.
    
    This is used to filter out dead streams before updating channels, except
    during global checks where dead streams are given a second chance.
    
    Returns:
        set: Set of dead stream URLs.
    """
    try:
        from dead_streams_tracker import DeadStreamsTracker
        tracker = DeadStreamsTracker()
        dead_streams = tracker.get_dead_streams()
        return set(dead_streams.keys())
    except Exception as e:
        logger.warning(f"Could not load dead streams tracker: {e}")
        # Return empty set if tracker not available
        return set()


def filter_dead_streams(stream_ids: List[int], stream_id_to_url: Optional[Dict[int, str]] = None) -> Tuple[List[int], int]:
    """
    Filter out dead streams from a list of stream IDs.
    
    This function removes stream IDs whose URLs are marked as dead in the
    DeadStreamsTracker. It's used to prevent dead streams from being added
    back to channels during update operations.
    
    Performance Note: When processing multiple channels, pass stream_id_to_url
    to avoid redundant API calls. Example:
        all_streams = get_streams(log_result=False)
        mapping = {s['id']: s.get('url') for s in all_streams if 'id' in s}
        for channel_id in channels:
            filtered, count = filter_dead_streams(stream_ids, mapping)
    
    If caching is enabled, uses cached stream data automatically.
    
    Parameters:
        stream_ids: List of stream IDs to filter
        stream_id_to_url: Optional mapping of stream IDs to URLs. If None,
            will fetch from API (or cache if enabled). Pass this when filtering
            multiple batches to optimize performance.
    
    Returns:
        Tuple of (filtered_stream_ids, count_filtered)
    """
    if not stream_ids:
        return stream_ids, 0
    
    cache = get_cache()
    
    # Get stream ID to URL mapping if not provided
    if stream_id_to_url is None:
        def _fetch():
            return get_streams(log_result=False)
        
        if cache.is_enabled():
            # Use cache to get mapping
            stream_id_to_url = cache.get_stream_id_to_url_mapping(_fetch)
        else:
            # Original behavior - fetch directly
            all_streams = get_streams(log_result=False)
            # Use None as default instead of empty string to distinguish missing streams
            stream_id_to_url = {s['id']: s.get('url') for s in all_streams if isinstance(s, dict) and 'id' in s}
    
    # Get dead stream URLs (will not contain None or empty strings)
    dead_urls = get_dead_stream_urls()
    
    # Filter out streams with dead URLs
    # Keep streams where:
    # 1. URL is not in dead_urls (not dead)
    # 2. URL is None (stream not found in mapping - keep for safety, will be filtered by existence check)
    filtered_stream_ids = [
        sid for sid in stream_ids
        if stream_id_to_url.get(sid) not in dead_urls or stream_id_to_url.get(sid) is None
    ]
    
    count_filtered = len(stream_ids) - len(filtered_stream_ids)
    return filtered_stream_ids, count_filtered

def has_custom_streams() -> bool:
    """
    Efficiently check if any custom streams exist.
    
    Tries to use API filtering if supported, otherwise iterates through
    pages with early exit. This is much faster than fetching all streams
    when there are thousands.
    
    Returns:
        bool: True if at least one custom stream exists, False otherwise.
    """
    base_url = _get_base_url()
    
    # Try filtering by is_custom parameter first (if API supports it)
    # This would be the most efficient approach
    url = f"{base_url}/api/channels/streams/?is_custom=true&page_size=1"
    response = fetch_data_from_url(url)
    
    if response:
        # Handle paginated response
        if isinstance(response, dict):
            results = response.get('results', [])
            # If we got results with the filter, custom streams exist
            if results and any(s.get('is_custom', False) for s in results):
                return True
            # If no results, check if filtering is supported by checking total count
            # If count is explicitly 0 or results is empty list, no custom streams
            if 'results' in response:
                return False
        elif isinstance(response, list) and response:
            if any(s.get('is_custom', False) for s in response):
                return True
    
    # Fallback: If filtering isn't supported or unclear, iterate through pages
    # Use page_size=100 for efficiency (fewer API calls)
    url = f"{base_url}/api/channels/streams/?page_size=100"
    
    while url:
        response = fetch_data_from_url(url)
        if not response:
            break
        
        # Handle paginated response
        if isinstance(response, dict) and 'results' in response:
            streams = response.get('results', [])
            # Early exit if we find any custom stream
            if any(s.get('is_custom', False) for s in streams):
                return True
            url = response.get('next')
        elif isinstance(response, list):
            # Early exit if we find any custom stream
            if any(s.get('is_custom', False) for s in response):
                return True
            break
        else:
            break
    
    return False

def create_channel_from_stream(
    stream_id: int,
    channel_number: Optional[int] = None,
    name: Optional[str] = None,
    channel_group_id: Optional[int] = None
) -> requests.Response:
    """
    Create a new channel from an existing stream.
    
    Parameters:
        stream_id (int): The ID of the stream to create channel from.
        channel_number (Optional[int]): The channel number to assign.
        name (Optional[str]): The name for the new channel.
        channel_group_id (Optional[int]): The channel group ID.
        
    Returns:
        requests.Response: The response object from the request.
    """
    url = f"{_get_base_url()}/api/channels/channels/from-stream/"
    data: Dict[str, Any] = {"stream_id": stream_id}
    
    if channel_number is not None:
        data["channel_number"] = channel_number
    if name:
        data["name"] = name
    if channel_group_id:
        data["channel_group_id"] = channel_group_id
    
    return post_request(url, data)

def add_streams_to_channel(
    channel_id: int, stream_ids: List[int], valid_stream_ids: Optional[set] = None,
    allow_dead_streams: bool = False,
    stream_id_to_url: Optional[Dict[int, str]] = None
) -> int:
    """
    Add new streams to an existing channel.
    
    Fetches the current streams for the channel, adds new streams
    while avoiding duplicates, and updates the channel. Filters out
    stream IDs that no longer exist in Dispatcharr and dead streams.
    
    Parameters:
        channel_id (int): The ID of the channel to update.
        stream_ids (List[int]): List of stream IDs to add.
        valid_stream_ids (Optional[set]): Set of valid stream IDs. If None,
            will fetch from API. Pass this to avoid redundant API calls when
            updating multiple channels.
        allow_dead_streams (bool): If True, allows dead streams (used during
            global checks to give dead streams a second chance). Default False.
        stream_id_to_url (Optional[Dict[int, str]]): Mapping of stream IDs to URLs.
            If None, will fetch from API. Pass this to avoid redundant API calls
            when updating multiple channels.
        
    Returns:
        int: Number of new streams actually added.
        
    Raises:
        ValueError: If current streams cannot be fetched.
    """
    # First get current streams
    current_streams = fetch_channel_streams(channel_id)
    if current_streams is None:
        raise ValueError(
            f"Could not fetch current streams for channel "
            f"{channel_id}"
        )
    
    current_stream_ids = [s['id'] for s in current_streams]
    
    # Filter out stream IDs that no longer exist in Dispatcharr
    if valid_stream_ids is None:
        valid_stream_ids = get_valid_stream_ids()
    
    valid_new_stream_ids = [
        sid for sid in stream_ids
        if sid in valid_stream_ids and sid not in current_stream_ids
    ]
    
    # Log if any stream IDs were filtered out as non-existent
    non_existent_count = len([sid for sid in stream_ids if sid not in valid_stream_ids])
    if non_existent_count > 0:
        logger.warning(
            f"Filtered out {non_existent_count} non-existent stream(s) "
            f"before adding to channel {channel_id}"
        )
    
    # Filter out dead streams unless allow_dead_streams is True
    if not allow_dead_streams and valid_new_stream_ids:
        valid_new_stream_ids, dead_count = filter_dead_streams(valid_new_stream_ids, stream_id_to_url)
        if dead_count > 0:
            logger.warning(
                f"Filtered out {dead_count} dead stream(s) "
                f"before adding to channel {channel_id}"
            )
    
    if valid_new_stream_ids:
        updated_streams = current_stream_ids + valid_new_stream_ids
        update_channel_streams(channel_id, updated_streams, valid_stream_ids, allow_dead_streams, stream_id_to_url)
        logger.info(
            f"Added {len(valid_new_stream_ids)} new streams to channel "
            f"{channel_id}"
        )
        return len(valid_new_stream_ids)
    else:
        logger.info(
            f"No new streams to add to channel {channel_id}"
        )
        return 0
