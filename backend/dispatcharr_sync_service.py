#!/usr/bin/env python3
"""
Dispatcharr Sync Service for Streamflow.

This module provides a centralized service responsible for ALL communication
with the Dispatcharr API. It reads pending changes from the Unified Data Index
and batches API calls to minimize the number of requests to Dispatcharr.

Key Features:
- Single service handles all POST and PATCH requests to Dispatcharr
- Batches similar changes together to reduce API calls
- Reads pending changes from the Unified Data Index
- Updates the index after successful API calls
- Provides methods to fetch data from Dispatcharr for index rebuilding

Architecture:
- All other services write changes to the Unified Data Index
- This service periodically reads pending changes and syncs to Dispatcharr
- Changelog entries are created for all synced changes
"""

import json
import os
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests

from logging_config import setup_logging
from unified_data_index import get_unified_data_index

logger = setup_logging(__name__)

# Configuration directory
CONFIG_DIR = Path(os.environ.get('CONFIG_DIR', '/app/data'))


class DispatcharrAPIFetcher:
    """
    Handles fetching data from Dispatcharr API for index rebuilding.
    
    This class provides methods to fetch all entities from Dispatcharr,
    used by the Unified Data Index during rebuild operations.
    """
    
    def __init__(self, base_url: str, auth_headers: Dict[str, str]):
        """
        Initialize the API fetcher.
        
        Args:
            base_url: Dispatcharr base URL
            auth_headers: Authorization headers for API requests
        """
        self.base_url = base_url.rstrip('/')
        self.auth_headers = auth_headers
        # Use tuple (connect_timeout, read_timeout) to prevent hanging on connection issues
        # Connect timeout: 10 seconds to establish TCP connection
        # Read timeout: 30 seconds to receive response data
        self.timeout = (10, 30)
    
    def _fetch_paginated(self, url: str) -> List[Dict]:
        """Fetch all pages of a paginated endpoint."""
        all_results = []
        
        while url:
            try:
                response = requests.get(url, headers=self.auth_headers, timeout=self.timeout)
                response.raise_for_status()
                data = response.json()
                
                if isinstance(data, dict) and 'results' in data:
                    all_results.extend(data.get('results', []))
                    url = data.get('next')
                elif isinstance(data, list):
                    all_results.extend(data)
                    url = None
                else:
                    break
                    
            except requests.exceptions.ConnectTimeout:
                logger.error(f"Connection timeout fetching {url} - Dispatcharr may be unreachable")
                break
            except requests.exceptions.ReadTimeout:
                logger.error(f"Read timeout fetching {url} - response took too long")
                break
            except requests.exceptions.ConnectionError as e:
                logger.error(f"Connection error fetching {url}: {e} - check network connectivity")
                break
            except Exception as e:
                logger.error(f"Error fetching from {url}: {e}")
                break
        
        return all_results
    
    def fetch_m3u_accounts(self) -> List[Dict]:
        """Fetch all M3U accounts from Dispatcharr."""
        url = f"{self.base_url}/api/m3u/accounts/"
        return self._fetch_paginated(url)
    
    def fetch_channel_groups(self) -> List[Dict]:
        """Fetch all channel groups from Dispatcharr."""
        url = f"{self.base_url}/api/channels/groups/"
        return self._fetch_paginated(url)
    
    def fetch_channels(self) -> List[Dict]:
        """Fetch all channels from Dispatcharr."""
        url = f"{self.base_url}/api/channels/channels/?page_size=100"
        return self._fetch_paginated(url)
    
    def fetch_streams(self) -> List[Dict]:
        """Fetch all streams from Dispatcharr."""
        url = f"{self.base_url}/api/channels/streams/?page_size=100"
        return self._fetch_paginated(url)
    
    def fetch_channel_streams(self, channel_id: int) -> List[Dict]:
        """Fetch streams for a specific channel."""
        url = f"{self.base_url}/api/channels/channels/{channel_id}/streams/"
        try:
            response = requests.get(url, headers=self.auth_headers, timeout=self.timeout)
            response.raise_for_status()
            return response.json()
        except requests.exceptions.ConnectTimeout:
            logger.error(f"Connection timeout fetching channel {channel_id} streams - Dispatcharr may be unreachable")
            return []
        except requests.exceptions.ReadTimeout:
            logger.error(f"Read timeout fetching channel {channel_id} streams - response took too long")
            return []
        except requests.exceptions.ConnectionError as e:
            logger.error(f"Connection error fetching channel {channel_id} streams: {e}")
            return []
        except Exception as e:
            logger.error(f"Error fetching channel {channel_id} streams: {e}")
            return []


class DispatcharrSyncService:
    """
    Centralized service for synchronizing changes to Dispatcharr.
    
    This service is the ONLY component that should make POST and PATCH
    requests to Dispatcharr. It reads pending changes from the Unified
    Data Index and batches them for efficient API communication.
    """
    
    _instance = None
    _lock = threading.Lock()
    
    def __new__(cls):
        """Singleton pattern."""
        with cls._lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._initialized = False
            return cls._instance
    
    def __init__(self):
        """Initialize the sync service."""
        if self._initialized:
            return
        
        self.running = False
        self.sync_thread = None
        self.sync_interval = 5  # seconds between sync checks
        self._stop_event = threading.Event()
        
        # Token management
        self._token = None
        self._token_lock = threading.Lock()
        
        self._initialized = True
        logger.info("Dispatcharr Sync Service initialized")
    
    def _get_base_url(self) -> Optional[str]:
        """Get the Dispatcharr base URL."""
        return os.getenv("DISPATCHARR_BASE_URL")
    
    def _login(self) -> bool:
        """
        Authenticate with Dispatcharr and get a token.
        
        Returns:
            bool: True if login successful
        """
        base_url = self._get_base_url()
        username = os.getenv("DISPATCHARR_USER")
        password = os.getenv("DISPATCHARR_PASS")
        
        if not all([base_url, username, password]):
            logger.error("Missing Dispatcharr credentials")
            return False
        
        try:
            login_url = f"{base_url}/api/accounts/token/"
            response = requests.post(
                login_url,
                headers={"Content-Type": "application/json"},
                json={"username": username, "password": password},
                timeout=(10, 30)  # (connect_timeout, read_timeout)
            )
            response.raise_for_status()
            data = response.json()
            
            token = data.get("access") or data.get("token")
            if token:
                with self._token_lock:
                    self._token = token
                logger.info("Dispatcharr login successful")
                return True
            else:
                logger.error("No token in login response")
                return False
        
        except requests.exceptions.ConnectTimeout:
            logger.error("Connection timeout during Dispatcharr login - service may be unreachable")
            return False
        except requests.exceptions.ReadTimeout:
            logger.error("Read timeout during Dispatcharr login - service may be slow")
            return False
        except requests.exceptions.ConnectionError as e:
            logger.error(f"Connection error during Dispatcharr login: {e}")
            return False
        except Exception as e:
            logger.error(f"Dispatcharr login failed: {e}")
            return False
    
    def _get_auth_headers(self) -> Dict[str, str]:
        """Get authorization headers, refreshing token if needed."""
        with self._token_lock:
            if not self._token:
                if not self._login():
                    raise Exception("Failed to authenticate with Dispatcharr")
        
        return {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/json",
            "Content-Type": "application/json"
        }
    
    def _make_request(self, method: str, url: str, data: Dict = None,
                      retry_on_401: bool = True) -> Tuple[bool, Optional[Dict], Optional[str]]:
        """
        Make an authenticated request to Dispatcharr.
        
        Args:
            method: HTTP method (GET, POST, PATCH, PUT, DELETE)
            url: Full URL
            data: Request payload
            retry_on_401: Whether to retry after refreshing token on 401
            
        Returns:
            Tuple of (success, response_data, error_message)
        """
        # Use tuple (connect_timeout, read_timeout) to prevent hanging
        timeout = (10, 30)
        
        try:
            headers = self._get_auth_headers()
            
            if method.upper() == 'GET':
                response = requests.get(url, headers=headers, timeout=timeout)
            elif method.upper() == 'POST':
                response = requests.post(url, json=data, headers=headers, timeout=timeout)
            elif method.upper() == 'PATCH':
                response = requests.patch(url, json=data, headers=headers, timeout=timeout)
            elif method.upper() == 'PUT':
                response = requests.put(url, json=data, headers=headers, timeout=timeout)
            elif method.upper() == 'DELETE':
                response = requests.delete(url, headers=headers, timeout=timeout)
            else:
                return False, None, f"Unsupported method: {method}"
            
            if response.status_code == 401 and retry_on_401:
                # Token expired, refresh and retry
                with self._token_lock:
                    self._token = None
                if self._login():
                    return self._make_request(method, url, data, retry_on_401=False)
                return False, None, "Authentication failed"
            
            response.raise_for_status()
            
            try:
                return True, response.json(), None
            except json.JSONDecodeError:
                return True, None, None
        
        except requests.exceptions.ConnectTimeout:
            logger.error(f"Connection timeout for {method} {url} - Dispatcharr may be unreachable")
            return False, None, "Connection timeout - Dispatcharr may be unreachable"
        except requests.exceptions.ReadTimeout:
            logger.error(f"Read timeout for {method} {url} - response took too long")
            return False, None, "Read timeout - response took too long"
        except requests.exceptions.ConnectionError as e:
            logger.error(f"Connection error for {method} {url}: {e}")
            return False, None, f"Connection error: {e}"
        except requests.exceptions.HTTPError as e:
            error_msg = str(e)
            if hasattr(e, 'response') and e.response is not None:
                try:
                    error_msg = e.response.text
                except:
                    pass
            return False, None, error_msg
            
        except Exception as e:
            return False, None, str(e)
    
    def get_api_fetcher(self) -> DispatcharrAPIFetcher:
        """Get an API fetcher for rebuilding the index."""
        base_url = self._get_base_url()
        if not base_url:
            raise Exception("DISPATCHARR_BASE_URL not set")
        
        return DispatcharrAPIFetcher(base_url, self._get_auth_headers())
    
    def rebuild_index(self) -> Dict[str, int]:
        """
        Rebuild the Unified Data Index from Dispatcharr.
        
        This should be called after M3U refresh to sync the index.
        
        Returns:
            Dict with counts of synced entities
        """
        logger.info("Triggering index rebuild from Dispatcharr...")
        
        try:
            fetcher = self.get_api_fetcher()
            index = get_unified_data_index()
            return index.rebuild_from_dispatcharr(fetcher)
        except Exception as e:
            logger.error(f"Index rebuild failed: {e}")
            raise
    
    def sync_pending_changes(self) -> Dict[str, int]:
        """
        Sync all pending changes to Dispatcharr.
        
        Reads pending changes from the index, groups them by entity,
        and makes batched API calls.
        
        Returns:
            Dict with sync statistics
        """
        index = get_unified_data_index()
        pending = index.get_pending_changes(limit=100)
        
        if not pending:
            return {'total': 0, 'synced': 0, 'failed': 0}
        
        stats = {'total': len(pending), 'synced': 0, 'failed': 0}
        base_url = self._get_base_url()
        
        if not base_url:
            logger.error("Cannot sync: DISPATCHARR_BASE_URL not set")
            return stats
        
        # Group changes by entity type and ID for potential batching
        # For now, process individually but structure allows for future batching
        for change in pending:
            change_id = change['id']
            entity_type = change['entity_type']
            entity_id = change['entity_id']
            operation = change['operation']
            payload = json.loads(change['payload']) if change.get('payload') else {}
            
            success = False
            error = None
            
            try:
                if entity_type == 'channel' and operation == 'update_streams':
                    # Update channel streams
                    url = f"{base_url}/api/channels/channels/{entity_id}/"
                    success, _, error = self._make_request('PATCH', url, payload)
                    
                elif entity_type == 'stream' and operation == 'update':
                    # Update stream stats
                    url = f"{base_url}/api/channels/streams/{entity_id}/"
                    success, _, error = self._make_request('PATCH', url, payload)
                    
                else:
                    logger.warning(f"Unknown change type: {entity_type}/{operation}")
                    success = True  # Mark as synced to avoid retry loop
                    
            except Exception as e:
                error = str(e)
            
            # Mark change as synced or failed
            index.mark_change_synced(change_id, success, error)
            
            if success:
                stats['synced'] += 1
                logger.debug(f"Synced change {change_id}: {entity_type}/{operation}")
            else:
                stats['failed'] += 1
                logger.warning(f"Failed to sync change {change_id}: {error}")
        
        if stats['synced'] > 0 or stats['failed'] > 0:
            logger.info(f"Sync complete: {stats['synced']} synced, {stats['failed']} failed")
        
        return stats
    
    def trigger_m3u_refresh(self, account_id: Optional[int] = None) -> bool:
        """
        Trigger M3U refresh on Dispatcharr.
        
        Args:
            account_id: Specific account to refresh, or None for all
            
        Returns:
            bool: True if refresh triggered successfully
        """
        base_url = self._get_base_url()
        if not base_url:
            return False
        
        if account_id:
            url = f"{base_url}/api/m3u/refresh/{account_id}/"
        else:
            url = f"{base_url}/api/m3u/refresh/"
        
        success, _, error = self._make_request('POST', url, {})
        
        if success:
            logger.info(f"M3U refresh triggered (account_id={account_id})")
        else:
            logger.error(f"M3U refresh failed: {error}")
        
        return success
    
    def start(self):
        """Start the background sync service."""
        with self._lock:
            if self.running:
                logger.warning("Sync service already running")
                return
            
            self.running = True
            self._stop_event.clear()
            
            self.sync_thread = threading.Thread(target=self._sync_loop, daemon=True)
            self.sync_thread.start()
            
            logger.info("Dispatcharr Sync Service started")
    
    def stop(self):
        """Stop the background sync service."""
        with self._lock:
            if not self.running:
                return
            
            self.running = False
            self._stop_event.set()
            
            if self.sync_thread and self.sync_thread.is_alive():
                self.sync_thread.join(timeout=10)
            
            logger.info("Dispatcharr Sync Service stopped")
    
    def _sync_loop(self):
        """Background loop that periodically syncs pending changes."""
        logger.info("Sync loop started")
        
        while not self._stop_event.is_set():
            try:
                # Sync pending changes
                self.sync_pending_changes()
                
                # Clean up old synced changes periodically
                index = get_unified_data_index()
                index.clear_synced_changes(older_than_hours=24)
                
            except Exception as e:
                logger.error(f"Error in sync loop: {e}")
            
            # Wait for next sync interval or stop signal
            self._stop_event.wait(timeout=self.sync_interval)
        
        logger.info("Sync loop stopped")
    
    def get_status(self) -> Dict[str, Any]:
        """Get current service status."""
        index = get_unified_data_index()
        
        return {
            'running': self.running,
            'has_token': self._token is not None,
            'base_url': self._get_base_url(),
            'index_stats': index.get_stats(),
            'sync_interval': self.sync_interval
        }


# Global instance getter
_sync_service_instance = None

def get_dispatcharr_sync_service() -> DispatcharrSyncService:
    """Get the global Dispatcharr Sync Service instance."""
    global _sync_service_instance
    if _sync_service_instance is None:
        _sync_service_instance = DispatcharrSyncService()
    return _sync_service_instance
