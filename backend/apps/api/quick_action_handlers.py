"""Quick-action and utility API handler functions extracted from web_api."""

from typing import Any, Callable

from flask import jsonify

from apps.core.logging_config import setup_logging

logger = setup_logging(__name__)


def discover_streams_response(*, get_automation_manager: Callable[[], Any]):
    """Handle manual stream discovery and assignment quick action."""
    try:
        manager = get_automation_manager()
        result = manager.discover_and_assign_streams(force=True)
        if not isinstance(result, dict):
            raise ValueError("Invalid stream discovery result")

        aborted = result.get("aborted") is True
        if aborted or result.get("success") is False or result.get("error"):
            # Worker errors may contain provider URLs, credentials or internal
            # paths. Keep diagnostics in the server log, never in API payloads.
            logger.warning("Stream discovery did not complete: %s", result.get("error"))
            return jsonify({
                "success": False,
                "aborted": aborted,
                "partial_writes": result.get("partial_writes") is True,
                "error": "Stream discovery was aborted" if aborted else "Stream discovery failed",
                "code": "stream_discovery_aborted" if aborted else "stream_discovery_failed",
            }), 409 if aborted else 500

        # Current managers return a structured result; older callers return a
        # channel-to-count mapping. Expose only the numeric assignment summary,
        # excluding internal result details even on a successful discovery.
        counts = result.get("assignment_count", result)
        if not isinstance(counts, dict):
            raise ValueError("Invalid stream assignment counts")
        assignments = {}
        for channel_id, count in counts.items():
            if (
                isinstance(channel_id, bool)
                or not isinstance(channel_id, (int, str))
                or isinstance(count, bool)
                or not isinstance(count, int)
                or count < 0
            ):
                raise ValueError("Invalid stream assignment counts")
            normalized_channel_id = int(channel_id)
            if normalized_channel_id <= 0:
                raise ValueError("Invalid stream assignment channel ID")
            assignments[str(normalized_channel_id)] = int(count)
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
        if payload is not None and not isinstance(payload, dict):
            return jsonify({"error": "Request body must be a valid JSON object"}), 400

        data = payload or {}
        account_id = data.get("account_id")
        if account_id is not None and (
            isinstance(account_id, bool)
            or not isinstance(account_id, int)
            or account_id <= 0
        ):
            return jsonify({"error": "account_id must be a positive integer"}), 400

        manager = get_automation_manager()
        if account_id is None:
            result = manager.refresh_playlists(force=True)
        else:
            result = manager.refresh_playlists(force=True, account_id=account_id)
        success, _ = result

        if success:
            return jsonify({"message": "Playlist refresh request accepted"})
        if account_id is None:
            return jsonify({"error": "Playlist refresh failed"}), 500
        return jsonify({
            "error": "Playlist refresh request was not accepted",
            "outcome": getattr(result, "outcome", "failed"),
        }), 500
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
