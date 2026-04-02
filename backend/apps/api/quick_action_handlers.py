"""Quick-action and utility API handler functions extracted from web_api."""

from typing import Any, Callable

from flask import jsonify

from apps.core.logging_config import setup_logging

logger = setup_logging(__name__)


def discover_streams_response(*, get_automation_manager: Callable[[], Any]):
    """Handle manual stream discovery and assignment quick action."""
    try:
        manager = get_automation_manager()
        assignments = manager.discover_and_assign_streams(force=True)
        return jsonify(
            {
                "message": "Stream discovery completed",
                "assignments": assignments,
                "total_assigned": sum(assignments.values()),
            }
        )
    except Exception as exc:
        logger.error(f"Error discovering streams: {exc}")
        return jsonify({"error": "Internal Server Error"}), 500


def refresh_playlist_response(*, payload: Any, get_automation_manager: Callable[[], Any]):
    """Handle manual M3U refresh quick action."""
    try:
        data = payload or {}
        _account_id = data.get("account_id")

        manager = get_automation_manager()
        success, _ = manager.refresh_playlists(force=True)

        if success:
            return jsonify({"message": "Playlist refresh completed successfully"})
        return jsonify({"error": "Playlist refresh failed"}), 500
    except Exception as exc:
        logger.error(f"Error refreshing playlist: {exc}")
        return jsonify({"error": "Internal Server Error"}), 500


def get_m3u_accounts_response(
    *,
    get_m3u_accounts: Callable[[], Any],
    has_custom_streams: Callable[[], bool],
):
    """Handle retrieval of active M3U accounts with custom-account filtering."""
    try:
        accounts = get_m3u_accounts()
        if accounts is None:
            return jsonify({"error": "Failed to fetch M3U accounts"}), 500

        accounts = [account for account in accounts if account.get("is_active") is True]
        has_custom = has_custom_streams()

        if not has_custom:
            accounts = [
                account
                for account in accounts
                if account.get("name", "").lower() != "custom"
            ]

        return jsonify({"accounts": accounts, "global_priority_mode": "disabled"})
    except Exception as exc:
        logger.error(f"Error fetching M3U accounts: {exc}")
        return jsonify({"error": "Internal Server Error"}), 500
