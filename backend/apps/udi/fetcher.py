"""
API data fetching for the Universal Data Index (UDI) system.

Handles fetching data from the Dispatcharr API for initial load and refresh operations.
"""

import os
import sys
import time
import json
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any
import requests
from pathlib import Path
from dotenv import load_dotenv, set_key

from apps.core.logging_config import setup_logging, log_api_request, log_api_response

# Import Dispatcharr configuration manager
from apps.config.dispatcharr_config import get_dispatcharr_config

logger = setup_logging(__name__)

env_path = Path('.') / '.env'

# Load environment variables from .env file if it exists
if env_path.exists():
    load_dotenv(dotenv_path=env_path)

# Token validation cache - stores last validated token and timestamp
# This reduces redundant API calls for token validation
_token_validation_cache: Dict[str, float] = {}
# Default TTL for token validation cache (in seconds)
TOKEN_VALIDATION_TTL = int(os.getenv("TOKEN_VALIDATION_TTL", "60"))


@dataclass
class FetchResult:
    """Result of a paginated fetch operation.

    Attributes:
        items:          All records collected across all pages.
        expected_count: Total record count reported by the API on the first
                        page (the ``count`` field in a DRF paginated response).
                        ``None`` when the endpoint is not paginated or when the
                        count field is absent — callers must treat ``None`` as
                        "unknown" rather than zero.
    """
    items: List[Dict[str, Any]] = field(default_factory=list)
    expected_count: Optional[int] = None

    # ------------------------------------------------------------------ #
    # Convenience helpers so callers that only need the list can write
    #   result = fetcher.fetch_channels()
    #   for ch in result:  ...          (iteration)
    #   n = len(result)                  (length)
    # without breaking existing code that treats the return value as a list.
    # ------------------------------------------------------------------ #
    def __iter__(self):
        return iter(self.items)

    def __len__(self):
        return len(self.items)

    def __bool__(self):
        return bool(self.items)

    def __getitem__(self, index):
        return self.items[index]


def _get_base_url() -> Optional[str]:
    """Get the base URL from configuration.
    
    Priority: Environment variable > Config file
    
    Returns:
        The Dispatcharr base URL or None if not configured.
    """
    config = get_dispatcharr_config()
    return config.get_base_url()


def _validate_token(token: str) -> bool:
    """Validate if a token is still valid by making a test API request.
    
    Uses a cache to avoid redundant API calls for token validation.
    The cache TTL is controlled by TOKEN_VALIDATION_TTL environment variable
    (default: 60 seconds).
    
    Args:
        token: The authentication token to validate
        
    Returns:
        True if token is valid, False otherwise
    """
    base_url = _get_base_url()
    if not base_url or not token:
        return False
    
    # Check cache first - if token was recently validated, skip API call
    cache_check_start = time.time()
    cached_time = _token_validation_cache.get(token)
    if cached_time is not None:
        age = cache_check_start - cached_time
        if age < TOKEN_VALIDATION_TTL:
            logger.debug(f"Token validation cached (age: {age:.1f}s, TTL: {TOKEN_VALIDATION_TTL}s)")
            return True
        else:
            logger.debug(f"Token validation cache expired (age: {age:.1f}s, TTL: {TOKEN_VALIDATION_TTL}s)")
    
    try:
        test_url = f"{base_url}/api/channels/channels/"
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Content-Type": "application/json"
        }
        log_api_request(logger, "GET", test_url, params={'page_size': 1})
        start_time = time.time()
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
        
        return result
    except Exception:
        # Clear cache on error
        _token_validation_cache.pop(token, None)
        return False


def _clear_token_validation_cache() -> None:
    """Clear the token validation cache.
    
    This should be called when the token changes (e.g., after login or token refresh)
    to ensure the new token is properly validated.
    """
    _token_validation_cache.clear()
    logger.debug("Token validation cache cleared")


def _login() -> bool:
    """Log into Dispatcharr and save the token.
    
    Returns:
        True if login successful, False otherwise.
    """
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
    logger.debug(f"Attempting to log in to {base_url}...")

    try:
        resp = requests.post(
            login_url,
            headers={"Content-Type": "application/json"},
            json={"username": username, "password": password},
            timeout=10
        )
        resp.raise_for_status()
        data = resp.json()
        token = data.get("access") or data.get("token")

        if token:
            # Clear old token validation cache before saving new token
            _clear_token_validation_cache()
            if env_path.exists():
                set_key(env_path, "DISPATCHARR_TOKEN", token)
                logger.info("Login successful. Token saved.")
            else:
                os.environ["DISPATCHARR_TOKEN"] = token
                logger.debug("Login successful. Token stored in memory.")
            return True
        else:
            logger.error("Login failed: No access token found in response.")
            return False
    except requests.exceptions.RequestException as e:
        logger.error(f"Login failed: {e}")
        return False
    except json.JSONDecodeError:
        logger.error("Login failed: Invalid JSON response from server.")
        return False


def _get_auth_headers() -> Dict[str, str]:
    """Get authorization headers for API requests.
    
    Token validation is not done proactively - invalid tokens are 
    handled by the 401 retry logic in the calling functions (e.g.,
    _fetch_url, fetch_data_from_url) that make requests with these headers.
    
    Returns:
        Dictionary containing authorization headers.
        
    Raises:
        SystemExit: If login fails or token cannot be retrieved.
    """
    current_token = os.getenv("DISPATCHARR_TOKEN")
    
    # If token exists, use it directly (validation happens on 401 response)
    if current_token:
        return {
            "Authorization": f"Bearer {current_token}",
            "Accept": "application/json",
            "Content-Type": "application/json"
        }
    
    # Token is missing, need to login
    logger.debug("DISPATCHARR_TOKEN not found. Attempting to log in...")
    
    if _login():
        if env_path.exists():
            load_dotenv(dotenv_path=env_path, override=True)
        current_token = os.getenv("DISPATCHARR_TOKEN")
        if not current_token:
            logger.error("Login succeeded, but token not found.")
            raise Exception("Login succeeded but token not found")
    else:
        logger.error("Login failed. Check credentials.")
        raise Exception("Login failed")

    return {
        "Authorization": f"Bearer {current_token}",
        "Accept": "application/json",
        "Content-Type": "application/json"
    }


def _refresh_token() -> bool:
    """Refresh the authentication token.
    
    Returns:
        True if refresh successful, False otherwise.
    """
    logger.info("Token expired or invalid. Attempting to refresh...")
    if _login():
        if env_path.exists():
            load_dotenv(dotenv_path=env_path, override=True)
        logger.info("Token refreshed successfully.")
        return True
    else:
        logger.error("Token refresh failed.")
        return False


class UDIFetcher:
    """Fetches data from the Dispatcharr API for the UDI system."""
    
    def __init__(self):
        """Initialize the UDI fetcher."""
        self.base_url = _get_base_url()
        
    def test_connection(self) -> bool:
        """Test connection to Dispatcharr API with short timeout.
        
        Returns:
            True if connection and auth successful
        """
        if not self.base_url:
            return False
            
        try:
            # Uses 5 second timeout in _validate_token
            headers = _get_auth_headers()
            token = headers.get("Authorization", "").replace("Bearer ", "")
            return _validate_token(token)
        except Exception:
            return False
    
    def fetch_entity_counts(self) -> Dict[str, Optional[int]]:
        """Fetch lightweight entity counts from Dispatcharr without pulling full objects.

        Uses the ``/ids/`` action endpoints exposed by ChannelViewSet and
        StreamViewSet — each returns only a flat list of IDs, making this
        much cheaper than a full paginated fetch while still giving an
        authoritative total from Dispatcharr's database.

        Designed to be called *before* a full refresh so manager.py can
        compare post-fetch counts against a known-good oracle, and also
        *after* a refresh to verify completeness independently of the
        pagination envelope.

        Returns:
            Dict with keys ``'channels'`` and ``'streams'``, each mapped to
            an integer count or ``None`` if the endpoint was unreachable.
        """
        if not self.base_url:
            logger.warning("fetch_entity_counts: base_url not set")
            return {'channels': None, 'streams': None}

        counts: Dict[str, Optional[int]] = {'channels': None, 'streams': None}

        # Channels /ids/ — ChannelViewSet.get_ids() returns a flat list of IDs
        try:
            channel_ids = self._fetch_url(f"{self.base_url}/api/channels/channels/ids/")
            if isinstance(channel_ids, list):
                counts['channels'] = len(channel_ids)
                logger.info(f"fetch_entity_counts: {counts['channels']} channels (from /ids/)")
            else:
                logger.warning("fetch_entity_counts: unexpected response from channels/ids/")
        except Exception as e:
            logger.warning(f"fetch_entity_counts: failed to fetch channel IDs: {e}")

        # Streams /ids/ — StreamViewSet.get_ids() returns a flat list of IDs
        try:
            stream_ids = self._fetch_url(f"{self.base_url}/api/channels/streams/ids/")
            if isinstance(stream_ids, list):
                counts['streams'] = len(stream_ids)
                logger.info(f"fetch_entity_counts: {counts['streams']} streams (from /ids/)")
            else:
                logger.warning("fetch_entity_counts: unexpected response from streams/ids/")
        except Exception as e:
            logger.warning(f"fetch_entity_counts: failed to fetch stream IDs: {e}")

        return counts

    def _fetch_url(self, url: str) -> Optional[Any]:
        """Fetch data from a URL with authentication and retry logic.
        
        Args:
            url: The URL to fetch
            
        Returns:
            JSON response data or None if failed
        """
        try:
            start_time = time.time()
            log_api_request(logger, "GET", url)
            resp = requests.get(url, headers=_get_auth_headers(), timeout=30)
            elapsed = time.time() - start_time
            log_api_response(logger, "GET", url, resp.status_code, elapsed)
            
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 401:
                if _refresh_token():
                    logger.info("Retrying request with new token...")
                    resp = requests.get(url, headers=_get_auth_headers(), timeout=30)
                    resp.raise_for_status()
                    return resp.json()
            logger.error(f"Error fetching {url}: {e}")
            return None
        except requests.exceptions.RequestException as e:
            logger.error(f"Error fetching {url}: {e}")
            return None
    
    def _fetch_paginated(self, base_url: str, page_size: int = 100) -> FetchResult:
        """Fetch paginated data from an API endpoint.

        Captures the ``count`` field from the first-page response so callers
        can compare expected vs. received totals for integrity checking.

        Args:
            base_url:  The base URL for the endpoint (no query string).
            page_size: Number of items per page.

        Returns:
            FetchResult with all collected items and the expected_count
            reported by the API on the first page (None if not present).
        """
        all_items: List[Dict[str, Any]] = []
        expected_count: Optional[int] = None
        first_page = True
        # Include page=1 explicitly so ChannelPagination does not short-circuit
        # into bare-list mode (it disables pagination when no 'page' param is
        # present, which drops the DRF count/results envelope).
        url = f"{base_url}?page_size={page_size}&page=1"

        while url:
            response = self._fetch_url(url)
            if not response:
                break

            if isinstance(response, dict) and 'results' in response:
                # Capture total count from the first page only
                if first_page:
                    raw_count = response.get('count')
                    if raw_count is not None:
                        try:
                            expected_count = int(raw_count)
                        except (TypeError, ValueError):
                            logger.warning(
                                f"Unexpected 'count' value in paginated response: {raw_count!r}"
                            )
                    first_page = False

                all_items.extend(response.get('results', []))
                url = response.get('next')
            else:
                if isinstance(response, list):
                    all_items.extend(response)
                break

        return FetchResult(items=all_items, expected_count=expected_count)
    
    def fetch_channels(self) -> FetchResult:
        """Fetch all channels from Dispatcharr.
        
        Returns:
            FetchResult with channel dictionaries and expected count.
        """
        if not self.base_url:
            logger.error("DISPATCHARR_BASE_URL not set")
            return FetchResult()
        
        url = f"{self.base_url}/api/channels/channels/"
        result = self._fetch_paginated(url)
        logger.info(
            f"Fetched {len(result)} channels"
            + (f" (expected {result.expected_count})" if result.expected_count is not None else "")
        )
        return result
    
    def fetch_channel_by_id(self, channel_id: int) -> Optional[Dict[str, Any]]:
        """Fetch a specific channel by ID.
        
        Args:
            channel_id: The channel ID
            
        Returns:
            Channel dictionary or None
        """
        if not self.base_url:
            return None
        
        url = f"{self.base_url}/api/channels/channels/{channel_id}/"
        return self._fetch_url(url)
    
    def fetch_channel_streams(self, channel_id: int) -> List[Dict[str, Any]]:
        """Fetch streams for a specific channel.
        
        Args:
            channel_id: The channel ID
            
        Returns:
            List of stream dictionaries for the channel
        """
        if not self.base_url:
            return []
        
        url = f"{self.base_url}/api/channels/channels/{channel_id}/streams/"
        streams = self._fetch_url(url)
        return streams if isinstance(streams, list) else []
    
    def fetch_streams(self) -> FetchResult:
        """Fetch all streams from Dispatcharr.
        
        Returns:
            FetchResult with stream dictionaries and expected count.
        """
        if not self.base_url:
            logger.error("DISPATCHARR_BASE_URL not set")
            return FetchResult()
        
        url = f"{self.base_url}/api/channels/streams/"
        result = self._fetch_paginated(url)
        logger.info(
            f"Fetched {len(result)} streams"
            + (f" (expected {result.expected_count})" if result.expected_count is not None else "")
        )
        return result
    
    def fetch_stream_by_id(self, stream_id: int) -> Optional[Dict[str, Any]]:
        """Fetch a specific stream by ID.
        
        Args:
            stream_id: The stream ID
            
        Returns:
            Stream dictionary or None
        """
        if not self.base_url:
            return None
        
        url = f"{self.base_url}/api/channels/streams/{stream_id}/"
        return self._fetch_url(url)
    
    def fetch_channel_groups(self) -> List[Dict[str, Any]]:
        """Fetch all channel groups from Dispatcharr.

        Channel groups use a non-paginated endpoint, so a plain list is
        returned rather than a FetchResult.
        
        Returns:
            List of channel group dictionaries.
        """
        if not self.base_url:
            logger.error("DISPATCHARR_BASE_URL not set")
            return []
        
        url = f"{self.base_url}/api/channels/groups/"
        groups = self._fetch_url(url)
        if isinstance(groups, list):
            logger.debug(f"Fetched {len(groups)} channel groups")
            return groups
        return []
    
    def fetch_logos(self) -> FetchResult:
        """Fetch all logos from Dispatcharr.
        
        Returns:
            FetchResult with logo dictionaries and expected count.
        """
        if not self.base_url:
            logger.error("DISPATCHARR_BASE_URL not set")
            return FetchResult()
        
        url = f"{self.base_url}/api/channels/logos/"
        result = self._fetch_paginated(url)
        logger.debug(
            f"Fetched {len(result)} logos"
            + (f" (expected {result.expected_count})" if result.expected_count is not None else "")
        )
        return result
    
    def fetch_logo_by_id(self, logo_id: int) -> Optional[Dict[str, Any]]:
        """Fetch a specific logo by ID.
        
        Args:
            logo_id: The logo ID
            
        Returns:
            Logo dictionary or None
        """
        if not self.base_url:
            return None
        
        url = f"{self.base_url}/api/channels/logos/{logo_id}/"
        return self._fetch_url(url)
    
    def fetch_m3u_accounts(self) -> List[Dict[str, Any]]:
        """Fetch all M3U accounts from Dispatcharr.

        M3U accounts use a non-paginated endpoint, so a plain list is
        returned rather than a FetchResult.
        
        Returns:
            List of M3U account dictionaries.
        """
        if not self.base_url:
            logger.error("DISPATCHARR_BASE_URL not set")
            return []
        
        url = f"{self.base_url}/api/m3u/accounts/"
        accounts = self._fetch_url(url)
        if isinstance(accounts, list):
            logger.debug(f"Fetched {len(accounts)} M3U accounts")
            return accounts
        return []
    
    def fetch_channel_profiles(self) -> List[Dict[str, Any]]:
        """Fetch all channel profiles from Dispatcharr.
        
        Returns:
            List of channel profile dictionaries
        """
        if not self.base_url:
            logger.error("DISPATCHARR_BASE_URL not set")
            return []
        
        url = f"{self.base_url}/api/channels/profiles/"
        logger.debug(f"Fetching channel profiles from {url}")
        profiles = self._fetch_url(url)
        
        if profiles is None:
            logger.error("Failed to fetch channel profiles - received None response")
            return []
        
        if isinstance(profiles, list):
            logger.debug(f"Successfully fetched {len(profiles)} channel profiles")
            if len(profiles) > 0:
                logger.debug(f"Sample profile: {profiles[0]}")
            return profiles
        
        logger.warning(f"Unexpected response type for channel profiles: {type(profiles).__name__}")
        logger.debug(f"Response content: {profiles}")
        return []
    
    def fetch_channel_profile_by_id(self, profile_id: int) -> Optional[Dict[str, Any]]:
        """Fetch a specific channel profile by ID.
        
        Args:
            profile_id: The profile ID
            
        Returns:
            Profile dictionary or None
        """
        if not self.base_url:
            return None
        
        url = f"{self.base_url}/api/channels/profiles/{profile_id}/"
        return self._fetch_url(url)
    
    def fetch_profile_channels(self, profile_ids: List[int], progress_callback=None) -> Dict[int, Dict[str, Any]]:
        """Fetch channel associations for multiple profiles.
        
        Args:
            profile_ids: List of profile IDs to fetch channels for
            progress_callback: Optional function(index, total, message) for progress reporting
            
        Returns:
            Dictionary mapping profile_id to profile channel data
        """
        if not self.base_url:
            logger.error("DISPATCHARR_BASE_URL not set")
            return {}
        
        profile_channels = {}
        total = len(profile_ids)
        for i, profile_id in enumerate(profile_ids):
            try:
                if progress_callback:
                    progress_callback(i, total, f"Fetching profile channels {i+1}/{total}")
                
                url = f"{self.base_url}/api/channels/profiles/{profile_id}/"
                logger.debug(f"Fetching channels for profile {profile_id} from {url}")
                profile_data = self._fetch_url(url)
                
                if profile_data:
                    # Parse the channels field
                    channels_data = profile_data.get('channels', '')
                    
                    # Try to parse if it's a JSON string
                    if isinstance(channels_data, str) and channels_data.strip():
                        try:
                            channels_data = json.loads(channels_data)
                        except json.JSONDecodeError as e:
                            logger.warning(f"Could not parse channels for profile {profile_id}: {e}")
                            channels_data = []
                    elif not isinstance(channels_data, list):
                        channels_data = []
                    
                    profile_channels[profile_id] = {
                        'profile': profile_data,
                        'channels': channels_data
                    }
                    logger.debug(f"Fetched {len(channels_data)} channel associations for profile {profile_id}")
            except Exception as e:
                logger.error(f"Error fetching channels for profile {profile_id}: {e}")
                continue
        
        logger.debug(f"Fetched channel data for {len(profile_channels)} profiles")
        return profile_channels
    
    def _process_channels_from_response(self, status_data: Any) -> Dict[str, Any]:
        """Process proxy status response and extract channels as a dict.
        
        Handles the API response format:
        - Standard format: {"channels": [...], "count": N}
        
        Args:
            status_data: Raw response from the proxy status endpoint
            
        Returns:
            Dictionary with channel_id -> status mapping
        """
        result = {}
        
        # Handle the API response format with nested channels array
        if isinstance(status_data, dict) and 'channels' in status_data:
            channels_list = status_data.get('channels', [])
            if isinstance(channels_list, list):
                for item in channels_list:
                    if isinstance(item, dict) and 'channel_id' in item:
                        result[str(item['channel_id'])] = item
                logger.debug(f"Processed {len(result)} channels from proxy status")
                return result
        
        logger.warning(f"Unexpected proxy status format: {type(status_data)}")
        return result
    
    def fetch_proxy_status(self) -> Dict[str, Any]:
        """Fetch real-time stream status from the proxy server.
        
        This fetches the actual running stream status from /proxy/ts/status endpoint,
        which provides accurate information about which streams are currently active.
        
        The endpoint returns the format:
        - Standard format: {"channels": [...], "count": N}
        
        Returns:
            Dictionary with channel_id -> status mapping, or empty dict if unavailable
        """
        if not self.base_url:
            logger.debug("DISPATCHARR_BASE_URL not set, cannot fetch proxy status")
            return {}
        
        url = f"{self.base_url}/proxy/ts/status"
        try:
            status_data = self._fetch_url(url)
            return self._process_channels_from_response(status_data)
        except Exception as e:
            logger.debug(f"Could not fetch proxy status: {e}")
        
        return {}
    
    def refresh_all(self) -> Dict[str, List[Dict[str, Any]]]:
        """Fetch all data from Dispatcharr.
        
        Returns:
            Dictionary with all fetched data
        """
        logger.info("Starting full data refresh from Dispatcharr API...")
        
        data = {
            'channels': self.fetch_channels(),
            'streams': self.fetch_streams(),
            'channel_groups': self.fetch_channel_groups(),
            'logos': self.fetch_logos(),
            'm3u_accounts': self.fetch_m3u_accounts(),
            'channel_profiles': self.fetch_channel_profiles()
        }
        
        logger.info(
            f"Full refresh complete: {len(data['channels'])} channels, "
            f"{len(data['streams'])} streams, {len(data['channel_groups'])} groups, "
            f"{len(data['logos'])} logos, {len(data['m3u_accounts'])} M3U accounts, "
            f"{len(data['channel_profiles'])} channel profiles"
        )
        
        return data
