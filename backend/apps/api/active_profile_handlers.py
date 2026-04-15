"""
Handler: GET /api/channels/<channel_id>/active-profile

Returns the currently resolved automation profile and EPG scheduled profile
for a single channel, using the same resolution logic the stream checker uses.

Response shape:
{
  "automation": {
    "profile_name": "HD Sports" | null,
    "period_name":  "Every 2h"  | null,
    "source":       "channel" | "group" | null
  },
  "epg_override": {
    "profile_name": "EPG Triggered" | null,
    "source":       "channel" | "group" | null
  }
}
"""

from typing import Any, Callable

from flask import jsonify

from apps.core.logging_config import setup_logging

logger = setup_logging(__name__)


def get_channel_active_profile_response(
    *,
    channel_id: int,
    get_automation_config_manager: Callable[[], Any],
    get_udi_manager: Callable[[], Any],
):
    """Resolve and return the active automation + EPG profile for a channel."""
    try:
        udi = get_udi_manager()
        channel = udi.get_channel_by_id(channel_id)
        group_id = None
        if channel:
            group_id = (
                channel.get("group_id")
                if channel.get("group_id") is not None
                else channel.get("channel_group_id")
            )

        automation_config = get_automation_config_manager()

        # --- Automation profile (period-based resolution) ---
        # Mirrors check_single_channel Step 2: get_effective_configuration
        automation_result = {"profile_name": None, "period_name": None, "source": None}
        config = automation_config.get_effective_configuration(channel_id, group_id)
        if config:
            profile = config.get("profile")
            automation_result["profile_name"] = profile.get("name") if profile else None
            automation_result["period_name"] = config.get("period_name")

            # Source: channel-level assignment beats group-level
            channel_periods = automation_config.get_channel_periods(channel_id)
            active_period_id = config.get("period_id")
            if active_period_id and str(active_period_id) in channel_periods:
                automation_result["source"] = "channel"
            else:
                automation_result["source"] = "group"

        # --- EPG scheduled profile override ---
        # Mirrors check_single_channel Step 1: get_effective_epg_scheduled_profile
        epg_result = {"profile_name": None, "source": None}
        epg_profile = automation_config.get_effective_epg_scheduled_profile(channel_id, group_id)
        if epg_profile:
            epg_result["profile_name"] = epg_profile.get("name")
            channel_epg_id = automation_config.get_channel_epg_scheduled_assignment(channel_id)
            epg_result["source"] = "channel" if channel_epg_id else "group"

        return jsonify({
            "automation": automation_result,
            "epg_override": epg_result,
        }), 200

    except Exception as exc:
        logger.error(f"Error resolving active profile for channel {channel_id}: {exc}")
        return jsonify({"error": "Internal Server Error"}), 500
