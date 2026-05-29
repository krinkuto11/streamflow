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
from typing import Dict, List, Optional, Any, Set
import requests

from apps.core.logging_config import setup_logging, log_api_request, log_api_response
from apps.config.dispatcharr_config import get_dispatcharr_config

# All auth primitives live in apps.core.auth so they can be shared with
# api_utils without a circular import (api_utils → udi → manager → fetcher).
from apps.core.auth import (
    _get_base_url,
    _get_auth_headers,
    _validate_auth_headers,
    _validate_token,
    _refresh_token,
    _token_refresh_lock,
    _token_validation_cache,
    TOKEN_VALIDATION_TTL,
    _clear_token_validation_cache,
)

logger = setup_logging(__name__)

GET_TIMEOUT_SECONDS = 45
GET_RETRY_ATTEMPTS = 3
GET_RETRY_BACKOFF_SECONDS = 2


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
        self._timing_lock = threading.Lock()
        self._request_timings: List[Dict[str, Any]] = []

    def _record_request_timing(
        self,
        *,
        method: str,
        url: str,
        elapsed: float,
        status_code: Optional[int] = None,
        success: bool = True,
    ) -> None:
        path = url
        if self.base_url and path.startswith(self.base_url):
            path = path[len(self.base_url):] or "/"

        entry = {
            "method": method,
            "path": path.split("?", 1)[0],
            "elapsed_seconds": round(float(elapsed), 4),
            "status_code": status_code,
            "success": success,
            "timestamp": time.time(),
        }
        with self._timing_lock:
            self._request_timings.append(entry)
            if len(self._request_timings) > 500:
                self._request_timings = self._request_timings[-500:]

    @staticmethod
    def _percentile(values: List[float], percentile: float) -> Optional[float]:
        if not values:
            return None
        ordered = sorted(values)
        if len(ordered) == 1:
            return round(ordered[0], 4)
        index = (len(ordered) - 1) * percentile
        lower = math.floor(index)
        upper = math.ceil(index)
        if lower == upper:
            return round(ordered[int(index)], 4)
        weight = index - lower
        return round(ordered[lower] * (1 - weight) + ordered[upper] * weight, 4)

    def get_api_timing_summary(self) -> Dict[str, Any]:
        """Return a URL-sanitized latency summary for recent Dispatcharr API calls."""
        with self._timing_lock:
            timings = list(self._request_timings)

        latencies = [entry["elapsed_seconds"] for entry in timings]
        failures = [entry for entry in timings if not entry.get("success")]
        slowest = sorted(timings, key=lambda entry: entry.get("elapsed_seconds", 0), reverse=True)[:5]
        return {
            "sample_count": len(timings),
            "failure_count": len(failures),
            "p95_seconds": self._percentile(latencies, 0.95),
            "p99_seconds": self._percentile(latencies, 0.99),
            "slowest": slowest,
        }
        
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
            if headers.get("X-API-Key") or headers.get("Authorization", "").startswith("ApiKey "):
                return _validate_auth_headers(headers)
            token = headers.get("Authorization", "").replace("Bearer ", "")
            return _validate_token(token)
        except Exception:
            return False
    
    def fetch_all_ids(self) -> Dict[str, Set[int]]:
        """Fetch all channel and stream IDs concurrently from the /ids/ endpoints.

        Both requests run in parallel — total cost is one round-trip rather than two.
        The resulting sets are the primary input for delta-sync diffing.

        Returns:
            Dict with keys ``'channels'`` and ``'streams'``, each mapped to a
            ``Set[int]``.  A key is absent when its endpoint was unreachable.
        """
        if not self.base_url:
            logger.warning("fetch_all_ids: base_url not set")
            return {}

        def _ids_from_url(url: str) -> Optional[Set[int]]:
            data = self._fetch_url(url)
            if not isinstance(data, list):
                return None
            try:
                return {int(i) for i in data}
            except (TypeError, ValueError) as e:
                logger.warning(f"fetch_all_ids: non-integer ID in response from {url}: {e}")
                return None

        result: Dict[str, Set[int]] = {}
        with ThreadPoolExecutor(max_workers=2) as ex:
            ch_future = ex.submit(_ids_from_url, f"{self.base_url}/api/channels/channels/ids/")
            st_future = ex.submit(_ids_from_url, f"{self.base_url}/api/channels/streams/ids/")
            ch_ids = ch_future.result()
            st_ids = st_future.result()

        if ch_ids is not None:
            result['channels'] = ch_ids
            logger.info(f"fetch_all_ids: {len(ch_ids)} channels")
        else:
            logger.warning("fetch_all_ids: failed to fetch channel IDs")
        if st_ids is not None:
            result['streams'] = st_ids
            logger.info(f"fetch_all_ids: {len(st_ids)} streams")
        else:
            logger.warning("fetch_all_ids: failed to fetch stream IDs")

        return result

    def fetch_entity_counts(self) -> Dict[str, Optional[int]]:
        """Return entity counts derived from fetch_all_ids() (integrity oracle for refresh_all).

        Returns:
            Dict with keys ``'channels'`` and ``'streams'``, each mapped to
            an integer count or ``None`` if the endpoint was unreachable.
        """
        ids = self.fetch_all_ids()
        counts: Dict[str, Optional[int]] = {
            'channels': len(ids['channels']) if 'channels' in ids else None,
            'streams':  len(ids['streams'])  if 'streams'  in ids else None,
        }
        logger.info(
            f"fetch_entity_counts: channels={counts['channels']}, streams={counts['streams']}"
        )
        return counts

    # Maximum IDs per POST body for by-ids endpoints.  Keeps request bodies
    # within a safe size even for very large delta batches.
    _BULK_CHUNK = 500

    def fetch_streams_by_ids(self, stream_ids: List[int]) -> List[Dict[str, Any]]:
        """Fetch specific streams by ID using the bulk POST endpoint.

        Sends a single ``POST /api/channels/streams/by-ids/`` per chunk of
        ``_BULK_CHUNK`` IDs instead of one GET per stream.  For typical delta
        batches (tens to low-hundreds of new streams) this is a single call.

        Args:
            stream_ids: List of stream IDs to fetch.

        Returns:
            List of stream dicts for IDs that were found (order not guaranteed).
        """
        if not self.base_url or not stream_ids:
            return []

        url = f"{self.base_url}/api/channels/streams/by-ids/"
        ids = list(stream_ids)
        results: List[Dict[str, Any]] = []

        for i in range(0, len(ids), self._BULK_CHUNK):
            chunk = ids[i: i + self._BULK_CHUNK]
            data = self._post_url(url, {"ids": chunk})
            if isinstance(data, list):
                results.extend(data)
            else:
                logger.error(
                    f"fetch_streams_by_ids: unexpected response for chunk "
                    f"{i}–{i + len(chunk)}: {type(data).__name__}"
                )

        logger.debug(
            f"fetch_streams_by_ids: fetched {len(results)}/{len(ids)} streams "
            f"in {math.ceil(len(ids) / self._BULK_CHUNK)} POST(s)"
        )
        return results

    def fetch_channels_by_ids(self, channel_ids: List[int]) -> List[Dict[str, Any]]:
        """Fetch specific channels by ID concurrently (one GET per ID).

        Used by delta refresh to pull only newly added channels.

        Args:
            channel_ids: List of channel IDs to fetch.

        Returns:
            List of channel dicts for IDs that were found.
        """
        if not self.base_url or not channel_ids:
            return []
        results: List[Dict[str, Any]] = []
        with ThreadPoolExecutor(max_workers=min(20, len(channel_ids))) as ex:
            futures = {ex.submit(self.fetch_channel_by_id, cid): cid for cid in channel_ids}
            for future in as_completed(futures):
                cid = futures[future]
                try:
                    data = future.result()
                    if data:
                        results.append(data)
                except Exception as e:
                    logger.error(f"Error fetching channel {cid}: {e}")
        return results

    def _fetch_url(self, url: str) -> Optional[Any]:
        """Fetch data from a URL with authentication and retry logic.
        
        Args:
            url: The URL to fetch
            
        Returns:
            JSON response data or None if failed
        """
        for attempt in range(1, GET_RETRY_ATTEMPTS + 1):
            try:
                start_time = time.time()
                log_api_request(logger, "GET", url)
                resp = requests.get(url, headers=_get_auth_headers(), timeout=GET_TIMEOUT_SECONDS)
                elapsed = time.time() - start_time
                log_api_response(logger, "GET", url, resp.status_code, elapsed)

                resp.raise_for_status()
                return resp.json()
            except requests.exceptions.HTTPError as e:
                status_code = e.response.status_code if e.response is not None else None
                if status_code == 401:
                    if _refresh_token():
                        logger.info("Retrying request with new token...")
                        resp = requests.get(url, headers=_get_auth_headers(), timeout=GET_TIMEOUT_SECONDS)
                        resp.raise_for_status()
                        return resp.json()

                retryable = status_code in {429, 500, 502, 503, 504}
                if retryable and attempt < GET_RETRY_ATTEMPTS:
                    logger.warning(
                        f"Transient HTTP {status_code} fetching {url}; retrying "
                        f"({attempt}/{GET_RETRY_ATTEMPTS})"
                    )
                    time.sleep(GET_RETRY_BACKOFF_SECONDS * attempt)
                    continue
                logger.error(f"Error fetching {url}: {e}")
                return None
            except requests.exceptions.RequestException as e:
                if attempt < GET_RETRY_ATTEMPTS:
                    logger.warning(
                        f"Transient error fetching {url}: {e}; retrying "
                        f"({attempt}/{GET_RETRY_ATTEMPTS})"
                    )
                    time.sleep(GET_RETRY_BACKOFF_SECONDS * attempt)
                    continue
                logger.error(f"Error fetching {url}: {e}")
                return None
    
    def _post_url(self, url: str, json_body: Any) -> Optional[Any]:
        """POST JSON to a URL with authentication and one auto-retry on 401.

        Args:
            url:       The endpoint URL.
            json_body: Dict/list to serialise as the request body.

        Returns:
            Parsed JSON response, or None on failure.
        """
        try:
            start_time = time.time()
            log_api_request(logger, "POST", url, json=json_body)
            resp = requests.post(url, headers=_get_auth_headers(), json=json_body, timeout=30)
            elapsed = time.time() - start_time
            log_api_response(logger, "POST", url, resp.status_code, elapsed)
            self._record_request_timing(
                method="POST",
                url=url,
                elapsed=elapsed,
                status_code=resp.status_code,
                success=resp.status_code < 400,
            )

            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.HTTPError as e:
            if e.response is not None and e.response.status_code == 401:
                if _refresh_token():
                    logger.info("Retrying POST request with new token...")
                    retry_start = time.time()
                    resp = requests.post(url, headers=_get_auth_headers(), json=json_body, timeout=30)
                    retry_elapsed = time.time() - retry_start
                    self._record_request_timing(
                        method="POST",
                        url=url,
                        elapsed=retry_elapsed,
                        status_code=resp.status_code,
                        success=resp.status_code < 400,
                    )
                    resp.raise_for_status()
                    return resp.json()
            logger.error(f"Error POSTing to {url}: {e}")
            return None
        except requests.exceptions.RequestException as e:
            self._record_request_timing(
                method="POST",
                url=url,
                elapsed=time.time() - start_time if 'start_time' in locals() else 0,
                status_code=None,
                success=False,
            )
            logger.error(f"Error POSTing to {url}: {e}")
            return None

    def _fetch_paginated(self, base_url: str, page_size: int = 1000, max_workers: int = 10) -> FetchResult:
        """Fetch paginated data from an API endpoint.

        Page 1 is fetched synchronously to capture the total ``count``.  All
        remaining pages are then fetched concurrently so that a 1 000-page
        dataset completes in roughly one round-trip rather than 1 000.

        Args:
            base_url:  The base URL for the endpoint (no query string).
            page_size: Number of items per page.
            max_workers: Maximum number of concurrent page requests.

        Returns:
            FetchResult with all collected items and the expected_count
            reported by the API on the first page (None if not present).
        """
        try:
            page_size = int(page_size or 1000)
        except (TypeError, ValueError):
            page_size = 1000
        try:
            max_workers = int(max_workers or 10)
        except (TypeError, ValueError):
            max_workers = 10
        page_size = max(1, page_size)
        max_workers = max(1, max_workers)

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
            f"(page_size={page_size}, max_workers={max_workers}, expected={expected_count})"
        )

        # Fetch remaining pages concurrently. Auth retries inside _fetch_url are
        # serialised by _token_refresh_lock so 401 bursts are safe.
        page_results: Dict[int, List] = {}
        with ThreadPoolExecutor(max_workers=min(max_workers, len(remaining))) as executor:
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
        config = get_dispatcharr_config()
        page_size = config.get_stream_fetch_page_size()
        max_workers = config.get_stream_fetch_max_workers()
        result = self._fetch_paginated(url, page_size=page_size, max_workers=max_workers)
        logger.info(
            f"Fetched {len(result)} streams"
            + (f" (expected {result.expected_count})" if result.expected_count is not None else "")
            + f" with page_size={page_size}, max_workers={max_workers}"
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
