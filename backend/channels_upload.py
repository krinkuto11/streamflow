"""
Channels upload utility for Dispatcharr.

This module synchronizes channels from a CSV file with Dispatcharr,
creating new channels and updating existing ones. It also refreshes
channel metadata after synchronization.
"""

import csv
import os
import sys
from typing import Dict, Any, Optional
import requests
import argparse
import json
from dotenv import load_dotenv, set_key
from pathlib import Path

from logging_config import setup_logging, log_function_call, log_function_return, log_exception

# --- Setup ---
logger = setup_logging(__name__)
env_path = Path('.') / '.env'
load_dotenv(dotenv_path=env_path)

# Standard timeout for Dispatcharr API requests
# Tuple of (connect_timeout, read_timeout) to prevent hanging on connection issues
API_TIMEOUT = (10, 30)


# --- API Utilities ---
def _get_base_url() -> str:
    """
    Get the base URL from environment variables.
    
    Returns:
        str: The Dispatcharr base URL.
        
    Raises:
        SystemExit: If DISPATCHARR_BASE_URL not set.
    """
    base_url = os.getenv("DISPATCHARR_BASE_URL")
    if not base_url:
        logger.error(
            "DISPATCHARR_BASE_URL not found in .env. Please set it."
        )
        sys.exit(1)
    return base_url

def _get_auth_headers() -> Dict[str, str]:
    """
    Get authorization headers for API requests.
    
    Returns:
        Dict[str, str]: Dictionary with authorization headers.
        
    Raises:
        SystemExit: If login fails or token unavailable.
    """
    current_token = os.getenv("DISPATCHARR_TOKEN")
    if not current_token:
        logger.info(
            "DISPATCHARR_TOKEN not found. Attempting to log in..."
        )
        if login():
            load_dotenv(dotenv_path=env_path, override=True)
            current_token = os.getenv("DISPATCHARR_TOKEN")
            if not current_token:
                logger.error(
                    "Login succeeded, token still not found. Aborting."
                )
                sys.exit(1)
        else:
            logger.error(
                "Login failed. Check credentials in .env. Aborting."
            )
            sys.exit(1)
    return {
        "Authorization": f"Bearer {current_token}",
        "Accept": "application/json",
        "Content-Type": "application/json"
    }

def login() -> bool:
    """
    Log into Dispatcharr and save token to .env file.
    
    Returns:
        bool: True if login successful, False otherwise.
    """
    username = os.getenv("DISPATCHARR_USER")
    password = os.getenv("DISPATCHARR_PASS")
    base_url = _get_base_url()

    if not all([username, password, base_url]):
        logger.error(
            "DISPATCHARR_USER, DISPATCHARR_PASS, and "
            "DISPATCHARR_BASE_URL must be set in .env file."
        )
        return False

    login_url = f"{base_url}/api/accounts/token/"
    logger.info(f"Attempting to log in to {base_url}...")

    try:
        resp = requests.post(
            login_url,
            headers={"Content-Type": "application/json"},
            json={"username": username, "password": password},
            timeout=API_TIMEOUT
        )
        resp.raise_for_status()
        data = resp.json()
        token = data.get("access") or data.get("token")

        if token:
            set_key(env_path, "DISPATCHARR_TOKEN", token)
            logger.info("Login successful. Token saved.")
            return True
        else:
            logger.error(
                "Login failed: No access token in response."
            )
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
        logger.error(f"Login failed: {e}")
        if hasattr(e, 'response') and e.response is not None:
            logger.error(f"Response content: {e.response.text}")
        return False

def _refresh_token() -> bool:
    """
    Refresh the authentication token.
    
    Returns:
        bool: True if refresh successful, False otherwise.
    """
    logger.info("Token expired or invalid. Attempting to refresh...")
    if login():
        load_dotenv(dotenv_path=env_path, override=True)
        logger.info("Token refreshed successfully.")
        return True
    else:
        logger.error("Token refresh failed.")
        return False


def _make_request(
    method: str, url: str, **kwargs: Any
) -> requests.Response:
    """
    Make a request with authentication and retry logic.
    
    Parameters:
        method (str): HTTP method (GET, POST, PATCH, etc.).
        url (str): The URL to send the request to.
        **kwargs: Additional arguments to pass to requests.
        
    Returns:
        requests.Response: The response object.
        
    Raises:
        requests.exceptions.RequestException: If request fails.
    """
    # Ensure timeout is set
    if 'timeout' not in kwargs:
        kwargs['timeout'] = API_TIMEOUT
    
    try:
        resp = requests.request(
            method, url, headers=_get_auth_headers(), **kwargs
        )
        resp.raise_for_status()
        return resp
    except requests.exceptions.HTTPError as e:
        if e.response.status_code == 401:
            if _refresh_token():
                logger.info(
                    f"Retrying {method} request to {url} "
                    f"with new token..."
                )
                resp = requests.request(
                    method, url, headers=_get_auth_headers(), **kwargs
                )
                resp.raise_for_status()
                return resp
            else:
                raise
        else:
            logger.error(
                f"HTTP Error: {e.response.status_code} for URL: {url}"
            )
            logger.error(f"Response: {e.response.text}")
            raise
    except requests.exceptions.ConnectTimeout:
        logger.error(f"Connection timeout for {method} {url} - Dispatcharr may be unreachable")
        raise
    except requests.exceptions.ReadTimeout:
        logger.error(f"Read timeout for {method} {url} - response took too long")
        raise
    except requests.exceptions.ConnectionError as e:
        logger.error(f"Connection error for {method} {url}: {e}")
        raise
    except requests.exceptions.RequestException as e:
        logger.error(f"Request failed: {e}")
        raise

# --- Main Functionality ---
def fetch_existing_channels() -> Dict[str, Dict[str, Any]]:
    """
    Fetch existing channels from Dispatcharr.
    
    Returns:
        Dict[str, Dict[str, Any]]: Dictionary mapping channel IDs
            to channel objects.
    """
    url = f"{_get_base_url()}/api/channels/channels/"
    try:
        response = _make_request("GET", url)
        data = response.json()
        if isinstance(data, list):
            return {str(c["id"]): c for c in data}
        return {}
    except (
        requests.exceptions.RequestException, json.JSONDecodeError
    ) as e:
        logger.error(f"Could not fetch existing channels: {e}")
        return {}


def update_channel(
    cid: str, payload: Dict[str, Any]
) -> requests.Response:
    """
    Update an existing channel in Dispatcharr.
    
    Parameters:
        cid (str): The ID of the channel to update.
        payload (Dict[str, Any]): The channel data to update.
        
    Returns:
        requests.Response: The response object.
    """
    url = f"{_get_base_url()}/api/channels/channels/{cid}/"
    return _make_request("PATCH", url, json=payload)


def create_channel(payload: Dict[str, Any]) -> requests.Response:
    """
    Create a new channel in Dispatcharr.
    
    Parameters:
        payload (Dict[str, Any]): The channel data.
        
    Returns:
        requests.Response: The response object.
    """
    url = f"{_get_base_url()}/api/channels/channels/"
    return _make_request("POST", url, json=payload)

def refresh_channel_metadata(output_file: str) -> None:
    """
    Fetch all channels and save metadata to a CSV file.
    
    Parameters:
        output_file (str): Path to the output CSV file.
    """
    logger.info(
        f"🔄 Refreshing channel metadata file: {output_file}"
    )
    try:
        url = f"{_get_base_url()}/api/channels/channels/"
        channels = _make_request("GET", url).json()
        if not channels:
            logger.warning("No channels found to refresh.")
            return

        with open(
            output_file, mode="w", newline="", encoding="utf-8"
        ) as f:
            headers = [
                "id", "channel_number", "name", "channel_group_id",
                "tvg_id", "tvc_guide_stationid", "epg_data_id",
                "stream_profile_id", "uuid", "logo_id", "user_level"
            ]
            writer = csv.writer(f)
            writer.writerow(headers)
            for ch in channels:
                row_data = []
                for h in headers:
                    value = ch.get(h, "")
                    # Default to 0 if channel_group_id is blank
                    if h == "channel_group_id" and (
                        value is None or value == ""
                    ):
                        row_data.append(0)
                    # Ensure empty string if blank
                    elif h == "tvc_guide_stationid" and (
                        value is None or value == ""
                    ):
                        row_data.append("")
                    else:
                        row_data.append(value)
                writer.writerow(row_data)
        logger.info("✅ Successfully refreshed channel metadata.")

    except (
        requests.exceptions.RequestException, json.JSONDecodeError
    ) as e:
        logger.error(f"❌ Failed to refresh channel metadata: {e}")

def main() -> None:
    """
    Sync channels from a CSV file to Dispatcharr.
    
    Reads channels from a CSV file and creates or updates them in
    Dispatcharr. After syncing, refreshes the channel metadata CSV.
    
    Raises:
        SystemExit: If CSV file not found.
    """
    parser = argparse.ArgumentParser(
        description="Synchronize channels with Dispatcharr from CSV."
    )
    parser.add_argument(
        "csv_file", nargs='?',
        default="csv/channels_template.csv",
        help="Path to CSV file. Defaults to csv/channels_template.csv"
    )
    args = parser.parse_args()

    input_csv_file = args.csv_file
    metadata_csv_file = "csv/01_channels_metadata.csv"

    if not os.path.exists(input_csv_file):
        logger.error(
            f"Error: The file {input_csv_file} was not found."
        )
        sys.exit(1)

    logger.info(f"📡 Syncing channels from {input_csv_file}...")
    existing_channels = fetch_existing_channels()

    def get_int_or_none(value: str) -> Optional[int]:
        """
        Convert string to int or return None.
        
        Parameters:
            value (str): The value to convert.
            
        Returns:
            Optional[int]: Integer value or None.
        """
        if value and value.strip() and value.strip() != "0":
            try:
                return int(value)
            except (ValueError, TypeError):
                return None
        return None

    try:
        with open(input_csv_file, mode="r", encoding="utf-8") as file:
            reader = csv.DictReader(file)
            for row in reader:
                try:
                    channel_number = row.get("channel_number", "").strip()
                    name = row.get("name", "").strip()

                    if not channel_number or not name:
                        logger.warning(f"  ❗️ Skipping row due to missing channel_number or name: {row}")
                        continue
                    
                    cid = row.get("id", "").strip()
                    tvg_id = row.get("tvg_id", "").strip()
                    if not tvg_id:
                        tvg_id = name.replace(" ", "")

                    payload = {
                        "channel_number": channel_number,
                        "name": name,
                        "channel_group_id": get_int_or_none(row.get("channel_group_id")),
                        "tvg_id": tvg_id,
                        "tvc_guide_stationid": row.get("tvc_guide_stationid", "").strip(),
                        "epg_data_id": get_int_or_none(row.get("epg_data_id")),
                        "stream_profile_id": get_int_or_none(row.get("stream_profile_id")),
                        "uuid": row.get("uuid", "").strip() or None,
                        "logo_id": get_int_or_none(row.get("logo_id")),
                        "user_level": get_int_or_none(row.get("user_level")),
                    }
                    
                    payload = {k: v for k, v in payload.items() if v is not None}

                    if cid and cid in existing_channels:
                        r = update_channel(cid, payload)
                        if r.status_code == 200:
                            logger.info(f"  🔁 Updated channel ID {cid}: {payload.get('name', 'N/A')}")
                        else:
                            logger.error(f"  ❌ Failed to update channel ID {cid}. Status: {r.status_code}, Response: {r.text}")
                    else:
                        r = create_channel(payload)
                        if r.status_code == 201:
                            logger.info(f"  ➕ Created channel: {payload.get('name', 'N/A')}")
                        else:
                            logger.error(f"  ❌ Failed to create channel: {payload.get('name', 'N/A')}. Status: {r.status_code}, Response: {r.text}")
                except KeyError as e:
                    logger.warning(f"  ❗️ Skipping row due to missing CSV column: {e}")
                except (ValueError, TypeError) as e:
                    logger.warning(f"  ❗️ Skipping row due to data conversion error: {e} - Row: {row}")

    except FileNotFoundError:
        logger.error(f"❌ Error: The file {input_csv_file} was not found.")
    except Exception as e:
        logger.error(f"An unexpected error occurred: {e}")

    logger.info("\n✅ Channel sync complete!")
    refresh_channel_metadata(metadata_csv_file)


if __name__ == "__main__":
    main()
