"""Legacy automation settings API handlers extracted from web_api."""

from typing import Any, Callable, Dict, Optional

from flask import jsonify

from apps.core.logging_config import setup_logging

logger = setup_logging(__name__)


def get_global_automation_settings_legacy_response(*, get_automation_config_manager: Callable[[], Any]):
    """Get global automation settings for legacy settings endpoint."""
    try:
        manager = get_automation_config_manager()
        return jsonify(manager.get_global_settings())
    except Exception as exc:
        logger.error(f"Error getting global automation settings: {exc}")
        return jsonify({"error": "Internal Server Error"}), 500


def update_global_automation_settings_legacy_response(
    *,
    payload: Optional[Dict[str, Any]],
    get_automation_config_manager: Callable[[], Any],
):
    """Update global automation settings for legacy settings endpoint."""
    try:
        data = payload
        if not data:
            return jsonify({"error": "No data provided"}), 400

        manager = get_automation_config_manager()
        success = manager.update_global_settings(
            regular_automation_enabled=data.get("regular_automation_enabled")
        )

        if success:
            return jsonify({"message": "Settings updated successfully", "settings": manager.get_global_settings()})
        return jsonify({"error": "Failed to update settings"}), 500
    except Exception as exc:
        logger.error(f"Error updating global automation settings: {exc}")
        return jsonify({"error": "Internal Server Error"}), 500


def get_automation_profiles_legacy_response(*, get_automation_config_manager: Callable[[], Any]):
    """Get all automation profiles for legacy settings endpoint."""
    try:
        manager = get_automation_config_manager()
        return jsonify(manager.get_all_profiles())
    except Exception as exc:
        logger.error(f"Error getting automation profiles: {exc}")
        return jsonify({"error": "Internal Server Error"}), 500


def create_automation_profile_legacy_response(
    *,
    payload: Optional[Dict[str, Any]],
    get_automation_config_manager: Callable[[], Any],
):
    """Create automation profile for legacy settings endpoint."""
    try:
        data = payload
        if not data:
            return jsonify({"error": "No data provided"}), 400

        manager = get_automation_config_manager()
        profile_id = manager.create_profile(data)

        if profile_id:
            return jsonify(
                {
                    "message": "Profile created successfully",
                    "id": profile_id,
                    "profile": manager.get_profile(profile_id),
                }
            )
        return jsonify({"error": "Failed to create profile"}), 500
    except Exception as exc:
        logger.error(f"Error creating automation profile: {exc}")
        return jsonify({"error": "Internal Server Error"}), 500


def get_automation_profile_legacy_response(
    *,
    profile_id: str,
    get_automation_config_manager: Callable[[], Any],
):
    """Get one automation profile for legacy settings endpoint."""
    try:
        manager = get_automation_config_manager()
        profile = manager.get_profile(profile_id)
        if profile:
            return jsonify(profile)
        return jsonify({"error": "Profile not found"}), 404
    except Exception as exc:
        logger.error(f"Error getting automation profile {profile_id}: {exc}")
        return jsonify({"error": "Internal Server Error"}), 500


def update_automation_profile_legacy_response(
    *,
    profile_id: str,
    payload: Optional[Dict[str, Any]],
    get_automation_config_manager: Callable[[], Any],
):
    """Update one automation profile for legacy settings endpoint."""
    try:
        data = payload
        if not data:
            return jsonify({"error": "No data provided"}), 400

        manager = get_automation_config_manager()
        success = manager.update_profile(profile_id, data)

        if success:
            return jsonify({"message": "Profile updated successfully", "profile": manager.get_profile(profile_id)})
        return jsonify({"error": "Failed to update profile (not found or save error)"}), 404
    except Exception as exc:
        logger.error(f"Error updating automation profile {profile_id}: {exc}")
        return jsonify({"error": "Internal Server Error"}), 500


def delete_automation_profile_legacy_response(
    *,
    profile_id: str,
    get_automation_config_manager: Callable[[], Any],
):
    """Delete one automation profile for legacy settings endpoint."""
    try:
        manager = get_automation_config_manager()
        success = manager.delete_profile(profile_id)

        if success:
            return jsonify({"message": "Profile deleted successfully"})
        return jsonify({"error": "Failed to delete profile (not found or save error)"}), 404
    except Exception as exc:
        logger.error(f"Error deleting automation profile {profile_id}: {exc}")
        return jsonify({"error": "Internal Server Error"}), 500


def assign_profile_to_channel_legacy_response(
    *,
    payload: Optional[Dict[str, Any]],
    get_automation_config_manager: Callable[[], Any],
):
    """Assign profile to channel for legacy settings endpoint."""
    try:
        data = payload
        if not data or "channel_id" not in data:
            return jsonify({"error": "Missing channel_id"}), 400

        channel_id = data["channel_id"]
        profile_id = data.get("profile_id")

        manager = get_automation_config_manager()
        success = manager.assign_profile_to_channel(channel_id, profile_id)

        if success:
            return jsonify({"message": "Assignment updated successfully"})
        return jsonify({"error": "Failed to update assignment"}), 500
    except Exception as exc:
        logger.error(f"Error assigning profile to channel: {exc}")
        return jsonify({"error": "Internal Server Error"}), 500


def assign_profile_to_group_legacy_response(
    *,
    payload: Optional[Dict[str, Any]],
    get_automation_config_manager: Callable[[], Any],
):
    """Assign profile to group for legacy settings endpoint."""
    try:
        data = payload
        if not data or "group_id" not in data:
            return jsonify({"error": "Missing group_id"}), 400

        group_id = data["group_id"]
        profile_id = data.get("profile_id")

        manager = get_automation_config_manager()
        success = manager.assign_profile_to_group(group_id, profile_id)

        if success:
            return jsonify({"message": "Assignment updated successfully"})
        return jsonify({"error": "Failed to update assignment"}), 500
    except Exception as exc:
        logger.error(f"Error assigning profile to group: {exc}")
        return jsonify({"error": "Internal Server Error"}), 500


def get_effective_profile_legacy_response(
    *,
    channel_id: int,
    get_automation_config_manager: Callable[[], Any],
    get_udi_manager: Callable[[], Any],
):
    """Get effective profile for channel for legacy settings endpoint."""
    try:
        udi = get_udi_manager()
        channel = udi.get_channel_by_id(channel_id)
        group_id = None
        if channel:
            group_id = channel.get("group_id")

        manager = get_automation_config_manager()
        profile = manager.get_effective_profile(channel_id, group_id)

        effective_id = manager.get_effective_profile_id(channel_id, group_id)
        assigned_profile_id = manager.get_channel_assignment(channel_id)
        assigned_group_profile_id = manager.get_group_assignment(group_id) if group_id else None

        return jsonify(
            {
                "effective_profile": profile,
                "effective_profile_id": effective_id,
                "channel_assignment": assigned_profile_id,
                "group_assignment": assigned_group_profile_id,
                "source": "channel" if assigned_profile_id else ("group" if assigned_group_profile_id else "default"),
            }
        )
    except Exception as exc:
        logger.error(f"Error getting effective profile for channel {channel_id}: {exc}")
        return jsonify({"error": "Internal Server Error"}), 500
