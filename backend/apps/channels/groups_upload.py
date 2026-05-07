"""
Groups upload utility for Dispatcharr.

This module synchronizes channel groups from a CSV file with Dispatcharr,
creating new groups and updating existing ones as needed.
"""

import csv
import os
import sys
from typing import Dict, Any, Optional
import requests

from apps.core.logging_config import setup_logging, log_function_call, log_function_return, log_exception
from apps.core.auth import _get_auth_headers, _get_base_url, _refresh_token

# --- Setup ---
logger = setup_logging(__name__)


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
    except requests.exceptions.RequestException as e:
        logger.error(f"Request failed: {e}")
        raise

# --- Main Functionality ---
def fetch_existing_groups() -> Dict[str, Dict[str, Any]]:
    """
    Fetch existing groups from Dispatcharr.
    
    Returns:
        Dict[str, Dict[str, Any]]: Dictionary mapping group IDs
            to group objects.
    """
    url = f"{_get_base_url()}/api/channels/groups/"
    try:
        response = _make_request("GET", url)
        if response.status_code == 200:
            return {str(g["id"]): g for g in response.json()}
        return {}
    except requests.exceptions.RequestException as e:
        logger.error(f"Could not fetch existing groups: {e}")
        return {}


def update_group(group_id: str, new_name: str) -> requests.Response:
    """
    Update an existing group in Dispatcharr.
    
    Parameters:
        group_id (str): The ID of the group to update.
        new_name (str): The new name for the group.
        
    Returns:
        requests.Response: The response object.
    """
    url = f"{_get_base_url()}/api/channels/groups/{group_id}/"
    payload = {"name": new_name}
    return _make_request("PATCH", url, json=payload)


def create_group(name: str) -> requests.Response:
    """
    Create a new group in Dispatcharr.
    
    Parameters:
        name (str): The name for the new group.
        
    Returns:
        requests.Response: The response object.
    """
    url = f"{_get_base_url()}/api/channels/groups/"
    payload = {"name": name}
    return _make_request("POST", url, json=payload)

def main() -> None:
    """
    Sync groups from a CSV file to Dispatcharr.
    
    Reads groups from csv/groups_template.csv and creates or updates
    them in Dispatcharr to match the CSV content.
    
    Raises:
        SystemExit: If CSV file not found.
    """
    csv_file = "csv/groups_template.csv"
    if not os.path.exists(csv_file):
        logger.error(f"Error: The file {csv_file} was not found.")
        sys.exit(1)

    logger.info("📥 Syncing groups from CSV...")
    existing_groups = fetch_existing_groups()

    with open(csv_file, mode="r", newline="", encoding="utf-8") as file:
        reader = csv.DictReader(file)
        for row in reader:
            gid = row.get("id", "").strip()
            name = row.get("name", "").strip()
            if not gid or not name:
                logger.warning(
                    f"Skipping row with missing id or name: {row}"
                )
                continue

            if gid in existing_groups:
                current_name = existing_groups[gid]["name"]
                if current_name != name:
                    try:
                        update_group(gid, name)
                        logger.info(
                            f"  🔁 Updated group ID {gid}: "
                            f"'{current_name}' → '{name}'"
                        )
                    except requests.exceptions.RequestException:
                        logger.error(
                            f"  ❌ Failed to update group ID {gid}"
                        )
                else:
                    logger.info(
                        f"  ✅ Group ID {gid} ('{name}') "
                        f"already up-to-date"
                    )
            else:
                try:
                    create_group(name)
                    logger.info(f"  ➕ Created new group: {name}")
                except requests.exceptions.RequestException:
                    logger.error(
                        f"  ❌ Failed to create group: {name}"
                    )

    logger.info("\n✅ Sync complete!")

if __name__ == "__main__":
    main()