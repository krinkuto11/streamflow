"""
API utilities for interacting with the Dispatcharr API.

This module provides authentication, request handling, and helper functions
for communicating with the Dispatcharr API endpoints.

Data access is handled through the Universal Data Index (UDI) system,
which serves as a single source of truth for all Dispatcharr data.
Write operations (PATCH, POST, DELETE) still use direct API calls.
"""

import os
import json
import sys
import time
from typing import Dict, List, Optional, Any, Tuple
import requests
from pathlib import Path
from dotenv import load_dotenv, set_key

from apps.core.logging_config import (
    setup_logging, log_function_call, log_function_return,
    log_exception, log_api_request, log_api_response
)

# Import UDI Manager for data access
from apps.udi import get_udi_manager

# Import Dispatcharr configuration manager
from apps.config.dispatcharr_config import get_dispatcharr_config

# Setup logging for this module
logger = setup_logging(__name__)

env_path = Path('.') / '.env'

# Load environment variables from .env file if it exists
# This allows fallback to .env file while supporting env vars
if env_path.exists():
    load_dotenv(dotenv_path=env_path)
    logger.debug(f"Loaded environment from {env_path}")

# Token validation cache - stores last validated token and timestamp
# This reduces redundant API calls for token validation
_token_validation_cache: Dict[str, float] = {}
# Default TTL for token validation cache (in seconds)
# Token validation result is cached for this duration to reduce API calls
TOKEN_VALIDATION_TTL = int(os.getenv("TOKEN_VALIDATION_TTL", "60"))


def _get_base_url() -> Optional[str]:
    """
    Get the base URL from configuration.
    
    Priority: Environment variable > Config file
    
    Returns:
        Optional[str]: The Dispatcharr base URL or None if not set.
    """
    config = get_dispatcharr_config()
    return config.get_base_url()

def _validate_token(token: str) -> bool:
    """
    Validate if a token is still valid by making a test API request.
    
    Uses a cache to avoid redundant API calls for token validation.
    The cache TTL is controlled by TOKEN_VALIDATION_TTL environment variable
    (default: 60 seconds).
    
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
    
    # Check cache first - if token was recently validated, skip API call
    cache_check_start = time.time()
    cached_time = _token_validation_cache.get(token)
    if cached_time is not None:
        age = cache_check_start - cached_time
        if age < TOKEN_VALIDATION_TTL:
            cache_elapsed = time.time() - cache_check_start
            logger.debug(f"Token validation cached (age: {age:.1f}s, TTL: {TOKEN_VALIDATION_TTL}s)")
            log_function_return(logger, "_validate_token", "cached", cache_elapsed)
            return True
        else:
            logger.debug(f"Token validation cache expired (age: {age:.1f}s, TTL: {TOKEN_VALIDATION_TTL}s)")
    
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
        
        # Cache successful validation using start_time as the reference point
        if result:
            _token_validation_cache[token] = start_time
            logger.debug(f"Token validation successful, cached for {TOKEN_VALIDATION_TTL}s")
        else:
            # Clear cache on failed validation
            _token_validation_cache.pop(token, None)
        
        log_function_return(logger, "_validate_token", result, elapsed)
        return result
    except Exception as e:
        # Clear cache on error
        _token_validation_cache.pop(token, None)
        log_exception(logger, e, "_validate_token")
        return False


def _clear_token_validation_cache() -> None:
    """
    Clear the token validation cache.
    
    This should be called when the token changes (e.g., after login or token refresh)
    to ensure the new token is properly validated.
    """
    _token_validation_cache.clear()
    logger.debug("Token validation cache cleared")


def login() -> bool:
    """
    Log into Dispatcharr and save the token to .env file.
    
    Authenticates with Dispatcharr using credentials from configuration
    (JSON file or environment variables). Stores the received token in 
    .env file if it exists, otherwise stores it in memory.
    
    Returns:
        bool: True if login successful, False otherwise.
    """
    log_function_call(logger, "login")
    config = get_dispatcharr_config()
    username = config.get_username()
    password = config.get_password()
    base_url = config.get_base_url()

    if not all([username, password, base_url]):
        logger.error(
            "DISPATCHARR_USER, DISPATCHARR_PASS, and "
            "DISPATCHARR_BASE_URL must be configured."
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
            timeout=10
        )
        elapsed = time.time() - start_time
        log_api_response(logger, "POST", login_url, resp.status_code, elapsed)
        
        resp.raise_for_status()
        data = resp.json()
        token = data.get("access") or data.get("token")

        if token:
            logger.debug(f"Received token (length: {len(token)})")
            # Clear old token validation cache before saving new token
            _clear_token_validation_cache()
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
    If no token is found, attempts to log in first. Token validation
    is not done proactively - invalid tokens are handled by the 401
    retry logic in API request functions.
    
    Returns:
        Dict[str, str]: Dictionary containing authorization headers.
        
    Raises:
        SystemExit: If login fails or token cannot be retrieved.
    """
    log_function_call(logger, "_get_auth_headers")
    current_token = os.getenv("DISPATCHARR_TOKEN")
    
    # If token exists, use it directly (validation happens on 401 response)
    if current_token:
        logger.debug("Using existing token")
        log_function_return(logger, "_get_auth_headers", "<headers with token>")
        return {
            "Authorization": f"Bearer {current_token}",
            "Accept": "application/json",
            "Content-Type": "application/json"
        }
    
    # Token is missing, need to login
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
        resp = requests.get(url, headers=_get_auth_headers(), timeout=30)
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
                resp = requests.get(url, headers=_get_auth_headers(), timeout=30)
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
    except requests.exceptions.RequestException as e:
        log_exception(logger, e, f"fetch_data_from_url ({url})")
        return None

def patch_request(url: str, payload: Dict[str, Any], max_retries: int = 2) -> requests.Response:
    """
    Send a PATCH request with authentication and retry logic.
    
    Makes an authenticated PATCH request to the specified URL. If the
    request fails with a 401 error, automatically refreshes the token
    and retries once. Handle 5xx errors and network issues with retries.
    
    Parameters:
        url (str): The URL to send the PATCH request to.
        payload (Dict[str, Any]): The JSON payload to send.
        max_retries (int): Maximum attempts for transient 500 errors.
        
    Returns:
        requests.Response: The response object from the request.
        
    Raises:
        requests.exceptions.RequestException: If request fails.
    """
    retries = 0
    while retries <= max_retries:
        try:
            resp = requests.patch(
                url, json=payload, headers=_get_auth_headers(), timeout=30
            )
            resp.raise_for_status()
            return resp
        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 401:
                if _refresh_token():
                    logger.info("Retrying PATCH request with new token...")
                    resp = requests.patch(
                        url, json=payload, headers=_get_auth_headers(), timeout=30
                    )
                    resp.raise_for_status()
                    return resp
                else:
                    raise
            elif e.response.status_code >= 500 and retries < max_retries:
                logger.warning(f"Got HTTP {e.response.status_code} patching data to {url}. Retrying {retries+1}/{max_retries}...")
                import time
                time.sleep(2)
                retries += 1
                continue
            else:
                logger.error(
                    f"Error patching data to {url}: {e.response.text}"
                )
                raise
        except requests.exceptions.RequestException as e:
            if retries < max_retries:
                logger.warning(f"Network error patching data to {url}: {e}. Retrying {retries+1}/{max_retries}...")
                import time
                time.sleep(2)
                retries += 1
                continue
            logger.error(f"Error patching data to {url}: {e}")
            raise

def post_request(url: str, payload: Dict[str, Any], max_retries: int = 2) -> requests.Response:
    """
    Send a POST request with authentication and retry logic.
    
    Makes an authenticated POST request to the specified URL. If the
    request fails with a 401 error, automatically refreshes the token
    and retries once. Handle 5xx errors and network issues with retries.
    
    Parameters:
        url (str): The URL to send the POST request to.
        payload (Dict[str, Any]): The JSON payload to send.
        max_retries (int): Maximum attempts for transient 500 errors.
        
    Returns:
        requests.Response: The response object from the request.
        
    Raises:
        requests.exceptions.RequestException: If request fails.
    """
    retries = 0
    while retries <= max_retries:
        try:
            resp = requests.post(
                url, json=payload, headers=_get_auth_headers(), timeout=30
            )
            resp.raise_for_status()
            return resp
        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 401:
                if _refresh_token():
                    logger.info("Retrying POST request with new token...")
                    resp = requests.post(
                        url, json=payload, headers=_get_auth_headers(), timeout=30
                    )
                    resp.raise_for_status()
                    return resp
                else:
                    raise
            elif e.response.status_code >= 500 and retries < max_retries:
                logger.warning(f"Got HTTP {e.response.status_code} posting data to {url}. Retrying {retries+1}/{max_retries}...")
                import time
                time.sleep(2)
                retries += 1
                continue
            else:
                logger.error(
                    f"Error posting data to {url}: {e.response.text}"
                )
                raise
        except requests.exceptions.RequestException as e:
            if retries < max_retries:
                logger.warning(f"Network error posting data to {url}: {e}. Retrying {retries+1}/{max_retries}...")
                import time
                time.sleep(2)
                retries += 1
                continue
            logger.error(f"Error posting data to {url}: {e}")
            raise

def fetch_channel_streams(channel_id: int) -> Optional[List[Dict[str, Any]]]:
    """
    Fetch streams for a given channel ID from the UDI cache.
    
    Parameters:
        channel_id (int): The ID of the channel.
        
    Returns:
        Optional[List[Dict[str, Any]]]: List of stream objects or None.
    """
    udi = get_udi_manager()
    streams = udi.get_channel_streams(channel_id)
    if streams:
        return streams
    # Return empty list if channel exists but has no streams
    channel = udi.get_channel_by_id(channel_id)
    if channel is not None:
        return []
    return None


def update_channel_streams(
    channel_id: int, stream_ids: List[int], valid_stream_ids: Optional[set] = None,
    allow_dead_streams: bool = False
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
        filtered_stream_ids, dead_count = filter_dead_streams(filtered_stream_ids)
        if dead_count > 0:
            logger.warning(
                f"Filtered out {dead_count} dead stream(s) for channel {channel_id}"
            )
    
    url = f"{_get_base_url()}/api/channels/channels/{channel_id}/"
    data = {"streams": filtered_stream_ids}
    
    try:
        response = patch_request(url, data)
        if response and response.status_code in [200, 204]:
            logger.debug(
                f"Successfully updated channel {channel_id} with "
                f"{len(filtered_stream_ids)} streams"
            )
            
            # Refresh the channel in UDI cache to ensure fresh stream data
            # This is critical for immediate stream checking after assignment
            try:
                udi = get_udi_manager()
                udi.refresh_channel_by_id(channel_id)
                logger.debug(f"Refreshed UDI cache for channel {channel_id} after stream update")
            except Exception as e:
                logger.warning(f"Failed to refresh UDI cache for channel {channel_id}: {e}")
            
            return True
        else:
            status = response.status_code if response else 'None'
            logger.warning(
                f"Unexpected response for channel {channel_id}: "
                f"{status}"
            )
            return False
    except requests.exceptions.HTTPError as e:
        # Handle "Invalid pk" errors by refreshing UDI and retrying with validated IDs
        if e.response.status_code == 400 and 'Invalid pk' in str(e.response.text):
            logger.warning(
                f"Stream ID validation failed for channel {channel_id}. "
                f"Refreshing UDI and retrying with valid IDs only..."
            )
            try:
                # Refresh streams in UDI to get latest data
                udi = get_udi_manager()
                udi.refresh_streams()
                
                # Re-validate stream IDs with fresh data
                current_valid_ids = udi.get_valid_stream_ids()
                revalidated_stream_ids = [sid for sid in filtered_stream_ids if sid in current_valid_ids]
                
                invalid_count = len(filtered_stream_ids) - len(revalidated_stream_ids)
                if invalid_count > 0:
                    logger.info(
                        f"Filtered out {invalid_count} invalid stream ID(s) after UDI refresh "
                        f"for channel {channel_id}"
                    )
                
                # Retry with validated IDs
                if revalidated_stream_ids:
                    retry_data = {"streams": revalidated_stream_ids}
                    retry_response = patch_request(url, retry_data)
                    if retry_response and retry_response.status_code in [200, 204]:
                        logger.info(
                            f"✓ Successfully updated channel {channel_id} with "
                            f"{len(revalidated_stream_ids)} validated streams (after retry)"
                        )
                        return True
                else:
                    logger.warning(
                        f"No valid streams remaining for channel {channel_id} after UDI refresh"
                    )
                    return False
            except Exception as retry_error:
                logger.error(
                    f"Retry failed for channel {channel_id} after UDI refresh: {retry_error}"
                )
                raise
        else:
            logger.error(
                f"Failed to update channel {channel_id} streams: {e}"
            )
            raise
    except Exception as e:
        logger.error(
            f"Failed to update channel {channel_id} streams: {e}"
        )
        raise

def change_channel_stream(channel_id: int, stream_id: Optional[int] = None, url: Optional[str] = None) -> bool:
    """
    Force change the currently playing stream for a channel in Dispatcharr.
    
    Parameters:
        channel_id (int): The ID of the channel.
        stream_id (Optional[int]): The ID of the stream to change to.
        url (Optional[str]): The URL of the stream to change to.
        
    Returns:
        bool: True if the stream was successfully changed, False otherwise.
    """
    if not stream_id and not url:
        logger.error(f"Cannot change stream for channel {channel_id}: neither stream_id nor url provided")
        return False
        
    endpoint = f"{_get_base_url()}/api/proxy/ts/change_stream/{channel_id}"
    payload = {}
    if stream_id:
        payload["stream_id"] = stream_id
    if url:
        payload["url"] = url
        
    try:
        response = post_request(endpoint, payload)
        if response and response.status_code == 200:
            logger.info(f"Successfully forced stream change for channel {channel_id} (Target Stream ID: {stream_id})")
            return True
        else:
            status = response.status_code if response else 'None'
            logger.warning(f"Failed to force stream change for channel {channel_id}. Status: {status}")
            return False
    except Exception as e:
        logger.error(f"Error calling change_stream API for channel {channel_id}: {e}")
        return False

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
    Fetch all M3U accounts from the UDI cache.
    
    Returns:
        Optional[List[Dict[str, Any]]]: List of M3U account objects
            or None if not available.
    """
    logger.debug("get_m3u_accounts() called - fetching from UDI cache")
    udi = get_udi_manager()
    accounts = udi.get_m3u_accounts()
    return accounts

def get_streams(log_result: bool = True) -> List[Dict[str, Any]]:
    """
    Fetch all available streams from the UDI cache.
    
    Parameters:
        log_result (bool): Whether to log the number of fetched streams.
            Default is True. Set to False to avoid duplicate log entries.
    
    Returns:
        List[Dict[str, Any]]: List of all stream objects.
    """
    udi = get_udi_manager()
    streams = udi.get_streams(log_result=log_result)
    return streams


def get_valid_stream_ids() -> set:
    """
    Get a set of all valid stream IDs from the UDI cache.
    
    This is used to filter out stream IDs that no longer exist (e.g., removed
    from M3U playlists) before updating channels.
    
    Returns:
        set: Set of valid stream IDs.
    """
    udi = get_udi_manager()
    return udi.get_valid_stream_ids()


def get_dead_stream_urls() -> set:
    """
    Get a set of URLs for streams marked as dead in the DeadStreamsTracker.
    
    This is used to filter out dead streams before updating channels, except
    during global checks where dead streams are given a second chance.
    
    Returns:
        set: Set of dead stream URLs.
    """
    try:
        from apps.stream.dead_streams_tracker import DeadStreamsTracker
        tracker = DeadStreamsTracker()
        dead_streams = tracker.get_dead_streams()
        return set(dead_streams.keys())
    except Exception as e:
        logger.warning(f"Could not load dead streams tracker: {e}")
        # Return empty set if tracker not available
        return set()


def filter_dead_streams(stream_ids: List[int], stream_id_to_url: Optional[Dict[int, str]] = None, only_offline: bool = True) -> Tuple[List[int], int]:
    """
    Filter out dead streams from a list of stream IDs.
    
    By default (only_offline=True), this function only removes streams that are 
    marked as 'offline' (truly dead). If only_offline=False, it removes ALL 
    dead streams regardless of reason.
    
    Parameters:
        stream_ids: List of stream IDs to filter
        stream_id_to_url: Optional mapping of stream IDs to URLs.
        only_offline: If True (default), only filters out truly offline streams.
                      If False, filters out all streams in the dead tracker.
    
    Returns:
        Tuple of (filtered_stream_ids, count_filtered)
    """
    if not stream_ids:
        return stream_ids, 0
    
    # Get stream ID to URL mapping if not provided
    if stream_id_to_url is None:
        all_streams = get_streams(log_result=False)
        stream_id_to_url = {s['id']: s.get('url') for s in all_streams if isinstance(s, dict) and 'id' in s}
    
    # Get tracker
    try:
        from apps.stream.dead_streams_tracker import DeadStreamsTracker
        tracker = DeadStreamsTracker()
    except Exception as e:
        logger.warning(f"Could not load dead streams tracker in filter_dead_streams: {e}")
        return stream_ids, 0
    
    filtered_stream_ids = []
    count_filtered = 0
    
    for sid in stream_ids:
        url = stream_id_to_url.get(sid)
        if url is None:
            # Keep for safety, will be filtered by existence check elsewhere
            filtered_stream_ids.append(sid)
            continue
            
        is_dead = False
        if only_offline:
            # Only filter if truly offline
            is_dead = tracker.is_offline(url)
        else:
            # Filter if dead for any reason
            is_dead = tracker.is_dead(url)
            
        if is_dead:
            count_filtered += 1
        else:
            filtered_stream_ids.append(sid)
            
    return filtered_stream_ids, count_filtered

def has_custom_streams() -> bool:
    """
    Check if any custom streams exist in the UDI cache.
    
    Returns:
        bool: True if at least one custom stream exists, False otherwise.
    """
    udi = get_udi_manager()
    return udi.has_custom_streams()

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
    allow_dead_streams: bool = False
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

    # Preserve any stream IDs that are assigned to the channel in Dispatcharr but were
    # not returned by fetch_channel_streams() due to a stale UDI stream cache.
    # Without this, those streams would be silently dropped when updated_streams is
    # written back to the channel.
    _raw_channel = get_udi_manager().get_channel_by_id(channel_id)
    if _raw_channel:
        _raw_ids = _raw_channel.get('streams', [])
        _current_id_set = set(current_stream_ids)
        _uncached = [sid for sid in _raw_ids if sid not in _current_id_set]
        if _uncached:
            logger.warning(
                f"add_streams_to_channel: channel {channel_id} has {len(_uncached)} "
                f"stream ID(s) not in UDI stream cache — preserving to avoid accidental "
                f"removal: {_uncached[:5]}{'...' if len(_uncached) > 5 else ''}"
            )
            current_stream_ids = current_stream_ids + _uncached
    
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
        valid_new_stream_ids, dead_count = filter_dead_streams(valid_new_stream_ids)
        if dead_count > 0:
            logger.warning(
                f"Filtered out {dead_count} dead stream(s) "
                f"before adding to channel {channel_id}"
            )
    
    if valid_new_stream_ids:
        updated_streams = current_stream_ids + valid_new_stream_ids
        update_channel_streams(channel_id, updated_streams, valid_stream_ids, allow_dead_streams)
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

def batch_update_stream_stats(stream_stats_list: List[Dict[str, Any]], batch_size: int = 10) -> Tuple[int, int]:
    """
    Batch update stream stats to reduce API calls during stream checking.
    
    This function updates multiple stream stats in batches to optimize performance
    during large-scale stream checking operations. Instead of making one API call
    per stream, it groups updates to reduce network overhead.
    
    Parameters:
        stream_stats_list (List[Dict[str, Any]]): List of dicts with keys:
            - stream_id: int
            - stream_stats: dict with resolution, source_fps, video_codec, audio_codec, ffmpeg_output_bitrate
        batch_size (int): Number of streams to update per batch (default: 10)
        
    Returns:
        Tuple[int, int]: (successful_updates, failed_updates)
        
    Example:
        >>> stats = [
        ...     {'stream_id': 123, 'stream_stats': {'resolution': '1920x1080', 'source_fps': 30}},
        ...     {'stream_id': 124, 'stream_stats': {'resolution': '1280x720', 'source_fps': 25}}
        ... ]
        >>> success, failed = batch_update_stream_stats(stats, batch_size=10)
    """
    import json
    
    base_url = _get_base_url()
    if not base_url:
        logger.error("DISPATCHARR_BASE_URL not set")
        return 0, len(stream_stats_list)
    
    successful = 0
    failed = 0
    total = len(stream_stats_list)
    
    # Get UDI manager for cache updates
    try:
        udi = get_udi_manager()
        if not udi:
            logger.error("UDI manager not available, cannot perform batch update")
            return 0, total
    except Exception as e:
        logger.error(f"Failed to get UDI manager: {e}")
        return 0, total
    
    # Process in batches to limit concurrent API calls
    for i in range(0, total, batch_size):
        batch = stream_stats_list[i:i + batch_size]
        logger.debug(f"Processing batch {i // batch_size + 1}/{(total + batch_size - 1) // batch_size} ({len(batch)} streams)")
        
        for item in batch:
            stream_id = item.get('stream_id')
            stream_stats = item.get('stream_stats', {})
            
            if not stream_id or not stream_stats:
                logger.warning(f"Invalid stream stats item: {item}")
                failed += 1
                continue
            
            # Construct URL for this stream
            stream_url = f"{base_url}/api/channels/streams/{int(stream_id)}/"
            
            try:
                # Fetch existing stream data from UDI cache
                existing_stream_data = udi.get_stream_by_id(int(stream_id))
                if not existing_stream_data:
                    logger.warning(f"Stream {stream_id} not found in UDI cache, skipping stats update")
                    failed += 1
                    continue
                
                # Get existing stats or empty dict
                existing_stats = existing_stream_data.get("stream_stats") or {}
                if isinstance(existing_stats, str):
                    try:
                        existing_stats = json.loads(existing_stats)
                    except json.JSONDecodeError:
                        existing_stats = {}
                
                # Merge existing stats with new stats
                updated_stats = {**existing_stats, **stream_stats}
                
                # Send PATCH request
                patch_payload = {"stream_stats": updated_stats}
                response = patch_request(stream_url, patch_payload)
                
                if response and response.status_code in [200, 204]:
                    # Update UDI cache to keep it in sync
                    updated_stream_data = existing_stream_data.copy()
                    updated_stream_data['stream_stats'] = updated_stats
                    udi.update_stream(int(stream_id), updated_stream_data)
                    successful += 1
                    logger.debug(f"Updated stats for stream {stream_id}")
                else:
                    status = response.status_code if response else 'None'
                    logger.warning(f"Failed to update stream {stream_id}: status {status}")
                    failed += 1
                    
            except Exception as e:
                logger.error(f"Error updating stream {stream_id} stats: {e}")
                failed += 1
    
    logger.debug(f"Batch stats update complete: {successful} successful, {failed} failed out of {total} total")
    return successful, failed
