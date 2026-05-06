"""
API data fetching for the Universal Data Index (UDI) system.

Handles fetching data from the Dispatcharr API for initial load and refresh operations.
"""

import math
import threading
import time
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any
import requests

from apps.core.logging_config import setup_logging, log_api_request, log_api_response
from apps.config.dispatcharr_config import get_dispatcharr_config

# All auth primitives live in apps.core.auth so they can be shared with
# api_utils without a circular import (api_utils → udi → manager → fetcher).
from apps.core.auth import (
    _get_base_url,
    _get_auth_headers,
    _validate_token,
    _refresh_token,
    _token_refresh_lock,
    _token_validation_cache,
    TOKEN_VALIDATION_TTL,
    _clear_token_validation_cache,
)

logger = setup_logging(__name__)


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

        Page 1 is fetched synchronously to capture the total ``count``.  All
        remaining pages are then fetched concurrently so that a 1 000-page
        dataset completes in roughly one round-trip rather than 1 000.

        Args:
            base_url:  The base URL for the endpoint (no query string).
            page_size: Number of items per page.

        Returns:
            FetchResult with all collected items and the expected_count
            reported by the API on the first page (None if not present).
        """
        # Include page=1 explicitly so ChannelPagination does not short-circuit
        # into bare-list mode (it disables pagination when no 'page' param is
        # present, which drops the DRF count/results envelope).
        response = self._fetch_url(f"{base_url}?page_size={page_size}&page=1")
        if not response:
            return FetchResult()

        # Non-paginated endpoint returns a bare list.
        if isinstance(response, list):
            return FetchResult(items=response, expected_count=None)

        if not (isinstance(response, dict) and 'results' in response):
            return FetchResult()

        all_items: List[Dict[str, Any]] = list(response.get('results', []))
        expected_count: Optional[int] = None

        raw_count = response.get('count')
        if raw_count is not None:
            try:
                expected_count = int(raw_count)
            except (TypeError, ValueError):
                logger.warning(
                    f"Unexpected 'count' value in paginated response: {raw_count!r}"
                )

        # If we already have everything (or count is unknown), return early.
        if expected_count is None or expected_count <= page_size:
            return FetchResult(items=all_items, expected_count=expected_count)

        total_pages = math.ceil(expected_count / page_size)
        remaining = range(2, total_pages + 1)
        logger.debug(
            f"Fetching {total_pages - 1} remaining pages of {base_url} concurrently "
            f"(page_size={page_size}, expected={expected_count})"
        )

        # Fetch remaining pages concurrently.  Cap at 10 workers to avoid
        # overwhelming Dispatcharr; auth retries inside _fetch_url are
        # serialised by _token_refresh_lock so 401 bursts are safe.
        page_results: Dict[int, List] = {}
        with ThreadPoolExecutor(max_workers=min(10, len(remaining))) as executor:
            future_to_page = {
                executor.submit(
                    self._fetch_url,
                    f"{base_url}?page_size={page_size}&page={p}",
                ): p
                for p in remaining
            }
            for future in as_completed(future_to_page):
                page_num = future_to_page[future]
                try:
                    result = future.result()
                    if result and isinstance(result, dict) and 'results' in result:
                        page_results[page_num] = result['results']
                    elif result and isinstance(result, list):
                        page_results[page_num] = result
                except Exception as e:
                    logger.error(f"Error fetching page {page_num} of {base_url}: {e}")

        # Merge pages in page-number order so item ordering is deterministic.
        for page_num in sorted(page_results):
            all_items.extend(page_results[page_num])

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
        """Fetch channel associations for multiple profiles concurrently.

        Args:
            profile_ids: List of profile IDs to fetch channels for
            progress_callback: Optional function(completed, total, message) for progress reporting

        Returns:
            Dictionary mapping profile_id to profile channel data
        """
        if not self.base_url:
            logger.error("DISPATCHARR_BASE_URL not set")
            return {}

        total = len(profile_ids)
        profile_channels: Dict[int, Dict[str, Any]] = {}
        completed = [0]
        progress_lock = threading.Lock()

        def fetch_one(profile_id: int):
            url = f"{self.base_url}/api/channels/profiles/{profile_id}/"
            logger.debug(f"Fetching channels for profile {profile_id}")
            profile_data = self._fetch_url(url)
            if not profile_data:
                return profile_id, None

            channels_data = profile_data.get('channels', '')
            if isinstance(channels_data, str) and channels_data.strip():
                try:
                    channels_data = json.loads(channels_data)
                except json.JSONDecodeError as e:
                    logger.warning(f"Could not parse channels for profile {profile_id}: {e}")
                    channels_data = []
            elif not isinstance(channels_data, list):
                channels_data = []

            return profile_id, {'profile': profile_data, 'channels': channels_data}

        workers = min(8, max(1, total))
        with ThreadPoolExecutor(max_workers=workers) as executor:
            future_to_id = {executor.submit(fetch_one, pid): pid for pid in profile_ids}
            for future in as_completed(future_to_id):
                pid = future_to_id[future]
                try:
                    profile_id, data = future.result()
                    if data is not None:
                        profile_channels[profile_id] = data
                        logger.debug(
                            f"Fetched {len(data['channels'])} channel associations "
                            f"for profile {profile_id}"
                        )
                except Exception as e:
                    logger.error(f"Error fetching channels for profile {pid}: {e}")

                with progress_lock:
                    completed[0] += 1
                    if progress_callback:
                        progress_callback(
                            completed[0], total,
                            f"Fetching profile channels {completed[0]}/{total}",
                        )

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
