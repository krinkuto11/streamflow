
"""Regex and matching API handler functions extracted from web_api."""

import re
from typing import Any, Callable

from flask import jsonify

from apps.api.schemas import (
    BulkRegexPatternsSchema,
    ChannelMatchSettingsSchema,
    GroupRegexConfigSchema,
    RegexPatternCreateSchema,
)
from apps.channels.repository import UdiChannelRepository
from apps.core.api_responses import error_response
from apps.core.exceptions import ValidationError
from apps.core.logging_config import setup_logging

logger = setup_logging(__name__)


def get_regex_patterns_response(*, get_regex_matcher: Callable[[], Any]):
    """Handle fetching all regex patterns."""
    try:
        matcher = get_regex_matcher()
        matcher.reload_patterns()
        patterns = matcher.get_patterns()
        return jsonify(patterns)
    except Exception as exc:
        logger.error(f"Error getting regex patterns: {exc}")
        return jsonify({"error": "Internal Server Error"}), 500


def add_regex_pattern_response(*, payload: Any, get_regex_matcher: Callable[[], Any]):
    """Handle single channel regex create/update."""
    try:
        parsed = RegexPatternCreateSchema.from_payload(payload)

        matcher = get_regex_matcher()
        matcher.add_channel_pattern(
            parsed.channel_id,
            parsed.name,
            parsed.regex,
            parsed.enabled,
            m3u_accounts=parsed.m3u_accounts,
        )

        return jsonify({"message": "Pattern added/updated successfully"})
    except ValidationError as exc:
        return error_response(
            exc.message,
            status_code=exc.status_code,
            code=exc.error_code,
            details=exc.details,
        )
    except ValueError as exc:
        logger.warning(f"Validation error adding regex pattern: {exc}")
        return error_response(
            "Invalid value or request parameters",
            status_code=400,
            code="validation_error",
        )
    except Exception as exc:
        logger.error(f"Error adding regex pattern: {exc}")
        return error_response("Internal Server Error", status_code=500, code="internal_error")


def delete_regex_pattern_response(*, channel_id: str, get_regex_matcher: Callable[[], Any]):
    """Handle regex pattern deletion for one channel.

    Previously this read the full pattern dict, deleted one entry, then called
    _save_patterns() which rewrote *every* channel via a merge=False import.
    That triggered the bulk-delete orphan bug (ChannelRegexPattern rows surviving
    the ChannelRegexConfig wipe) and was also unnecessarily expensive.

    Now we delegate directly to matcher.delete_channel_pattern() which:
      - Removes the entry from the in-memory cache under the lock
      - Calls db.delete_channel_regex_config() which deletes the ORM object,
        allowing SQLAlchemy cascade to cleanly remove the pattern rows
    No other channel's patterns are touched.
    """
    try:
        matcher = get_regex_matcher()
        cid = str(channel_id)

        # Reload to ensure the in-memory cache is current before the existence
        # check, avoiding a false 404 on a stale cache.
        matcher.reload_patterns()
        patterns = matcher.get_patterns()

        if "patterns" not in patterns or cid not in patterns["patterns"]:
            return jsonify({"error": "Pattern not found"}), 404

        matcher.delete_channel_pattern(cid)
        return jsonify({"message": "Pattern deleted successfully"})
    except Exception as exc:
        logger.error(f"Error deleting regex pattern: {exc}")
        return jsonify({"error": "Internal Server Error"}), 500


def update_channel_match_settings_response(
    *,
    channel_id: str,
    payload: Any,
    get_regex_matcher: Callable[[], Any],
):
    """Handle channel match settings updates."""
    try:
        parsed = ChannelMatchSettingsSchema.from_payload(payload)

        matcher = get_regex_matcher()
        if parsed.match_by_tvg_id is not None:
            matcher.set_match_by_tvg_id(channel_id, parsed.match_by_tvg_id)

        return jsonify({"message": "Match settings updated successfully"})
    except ValidationError as exc:
        return error_response(
            exc.message,
            status_code=exc.status_code,
            code=exc.error_code,
            details=exc.details,
        )
    except Exception as exc:
        logger.error(f"Error updating match settings for channel {channel_id}: {exc}")
        return error_response("Internal Server Error", status_code=500, code="internal_error")


def get_group_regex_config_response(*, group_id: int, get_regex_matcher: Callable[[], Any]):
    """Handle group regex configuration retrieval."""
    try:
        matcher = get_regex_matcher()
        matcher.reload_patterns()
        config = matcher.get_group_pattern(group_id) or {
            "name": "",
            "enabled": True,
            "match_by_tvg_id": False,
            "regex_patterns": [],
        }
        return jsonify(config), 200
    except Exception as exc:
        logger.error(f"Error getting regex config for group {group_id}: {exc}")
        return jsonify({"error": "Internal Server Error"}), 500


def upsert_group_regex_config_response(
    *,
    group_id: int,
    payload: Any,
    get_regex_matcher: Callable[[], Any],
):
    """Handle group regex configuration create/update."""
    try:
        parsed = GroupRegexConfigSchema.from_payload(payload or {})
        matcher = get_regex_matcher()

        name = parsed.name
        if not name:
            try:
                group = UdiChannelRepository().get_channel_group_by_id(group_id)
                if isinstance(group, dict):
                    name = group.get("name", "")
            except Exception:
                pass

        matcher.add_group_pattern(
            group_id=group_id,
            name=name,
            regex_patterns=parsed.regex_patterns,
            enabled=parsed.enabled,
            match_by_tvg_id=parsed.match_by_tvg_id,
            m3u_accounts=parsed.m3u_accounts,
        )

        return jsonify({"message": "Group regex config updated successfully"}), 200
    except ValidationError as exc:
        return error_response(
            exc.message,
            status_code=exc.status_code,
            code=exc.error_code,
            details=exc.details,
        )
    except ValueError as exc:
        logger.warning(f"Validation error upserting group regex config for group {group_id}: {exc}")
        return error_response(str(exc), status_code=400, code="validation_error")
    except Exception as exc:
        logger.error(f"Error upserting regex config for group {group_id}: {exc}")
        return error_response("Internal Server Error", status_code=500, code="internal_error")


def delete_group_regex_config_response(*, group_id: int, get_regex_matcher: Callable[[], Any]):
    """Handle deletion of group regex configuration."""
    try:
        matcher = get_regex_matcher()
        matcher.delete_group_pattern(group_id)
        return jsonify({"message": "Group regex config deleted successfully"}), 200
    except Exception as exc:
        logger.error(f"Error deleting regex config for group {group_id}: {exc}")
        return jsonify({"error": "Internal Server Error"}), 500


def update_group_match_settings_response(
    *,
    group_id: int,
    payload: Any,
    get_regex_matcher: Callable[[], Any],
):
    """Handle group-level match settings updates."""
    try:
        parsed = ChannelMatchSettingsSchema.from_payload(payload or {})
        matcher = get_regex_matcher()

        if parsed.match_by_tvg_id is not None:
            matcher.set_group_match_by_tvg_id(group_id, parsed.match_by_tvg_id)

        return jsonify({"message": "Group match settings updated successfully"}), 200
    except ValidationError as exc:
        return error_response(
            exc.message,
            status_code=exc.status_code,
            code=exc.error_code,
            details=exc.details,
        )
    except Exception as exc:
        logger.error(f"Error updating match settings for group {group_id}: {exc}")
        return error_response("Internal Server Error", status_code=500, code="internal_error")


def add_bulk_regex_patterns_response(
    *,
    payload: Any,
    get_regex_matcher: Callable[[], Any],
):
    """Handle bulk regex pattern additions for multiple channels."""
    try:
        parsed = BulkRegexPatternsSchema.from_payload(payload)
        channel_ids = parsed.channel_ids
        regex_patterns = parsed.regex_patterns
        m3u_accounts = parsed.m3u_accounts

        channel_repo = UdiChannelRepository()
        matcher = get_regex_matcher()

        is_valid, _ = matcher.validate_regex_patterns(regex_patterns)
        if not is_valid:
            return jsonify({"error": "Invalid value or request parameters"}), 400

        success_count = 0
        failed_channels = []

        for channel_id in channel_ids:
            try:
                channel = channel_repo.get_channel_by_id(channel_id)
                if not channel:
                    failed_channels.append({"channel_id": channel_id, "error": "Channel not found"})
                    continue

                channel_name = channel.get("name", f"Channel {channel_id}")

                with matcher.lock:
                    patterns = matcher.get_patterns()
                    existing_pattern_data = patterns.get("patterns", {}).get(str(channel_id), {})

                    existing_regex_patterns = existing_pattern_data.get("regex_patterns")
                    normalized_existing = []
                    if existing_regex_patterns:
                        for pattern in existing_regex_patterns:
                            if isinstance(pattern, dict):
                                normalized_existing.append(pattern)
                            else:
                                normalized_existing.append(
                                    {
                                        "pattern": pattern,
                                        "m3u_accounts": existing_pattern_data.get("m3u_accounts"),
                                    }
                                )
                    else:
                        old_regex = existing_pattern_data.get("regex", [])
                        old_m3u_accounts = existing_pattern_data.get("m3u_accounts")
                        for pattern in old_regex:
                            normalized_existing.append(
                                {
                                    "pattern": pattern,
                                    "m3u_accounts": old_m3u_accounts,
                                }
                            )

                    normalized_new = []
                    for pattern in regex_patterns:
                        normalized_new.append({"pattern": pattern, "m3u_accounts": m3u_accounts})

                    merged_patterns = list(normalized_existing)
                    existing_pattern_strings = {p["pattern"] for p in normalized_existing}

                    for new_pattern in normalized_new:
                        if new_pattern["pattern"] not in existing_pattern_strings:
                            merged_patterns.append(new_pattern)
                            existing_pattern_strings.add(new_pattern["pattern"])

                    matcher.add_channel_pattern(
                        str(channel_id),
                        channel_name,
                        merged_patterns,
                        existing_pattern_data.get("enabled", True),
                        m3u_accounts=None,
                        silent=True,
                    )

                success_count += 1
            except Exception as exc:
                logger.error(f"Error adding pattern to channel {channel_id}: {exc}")
                failed_channels.append({"channel_id": channel_id, "error": str(exc)})

        response_data = {
            "message": f"Successfully added patterns to {success_count} channel(s)",
            "success_count": success_count,
            "total_channels": len(channel_ids),
        }

        if failed_channels:
            response_data["failed_channels"] = failed_channels
            response_data["failed_count"] = len(failed_channels)

        return jsonify(response_data)
    except ValidationError as exc:
        return error_response(
            exc.message,
            status_code=exc.status_code,
            code=exc.error_code,
            details=exc.details,
        )
    except ValueError as exc:
        logger.warning(f"Validation error in bulk regex pattern addition: {exc}")
        return error_response(
            "Invalid value or request parameters",
            status_code=400,
            code="validation_error",
        )
    except Exception as exc:
        logger.error(f"Error adding bulk regex patterns: {exc}")
        return error_response("Internal Server Error", status_code=500, code="internal_error")


def bulk_delete_regex_patterns_response(*, payload: Any, get_regex_matcher: Callable[[], Any]):
    """Handle bulk deletion of regex patterns across channels."""
    try:
        if not payload:
            return jsonify({"error": "No data provided"}), 400

        channel_ids = payload.get("channel_ids", [])
        if not isinstance(channel_ids, list) or len(channel_ids) == 0:
            return jsonify({"error": "channel_ids must be a non-empty list"}), 400

        matcher = get_regex_matcher()
        success_count = 0
        failed_channels = []

        for channel_id in channel_ids:
            try:
                matcher.delete_channel_pattern(str(channel_id))
                success_count += 1
            except Exception as exc:
                logger.error(f"Error deleting patterns from channel {channel_id}: {exc}")
                failed_channels.append({"channel_id": channel_id, "error": str(exc)})

        response_data = {
            "message": f"Successfully deleted patterns from {success_count} channel(s)",
            "success_count": success_count,
            "total_channels": len(channel_ids),
        }
        if failed_channels:
            response_data["failed_channels"] = failed_channels
            response_data["failed_count"] = len(failed_channels)

        return jsonify(response_data)
    except Exception as exc:
        logger.error(f"Error bulk deleting regex patterns: {exc}")
        return jsonify({"error": "Internal Server Error"}), 500


def get_common_regex_patterns_response(*, payload: Any, get_regex_matcher: Callable[[], Any]):
    """Handle retrieval of common regex patterns across channels."""
    try:
        if not payload:
            return jsonify({"error": "No data provided"}), 400

        channel_ids = payload.get("channel_ids", [])
        if not isinstance(channel_ids, list) or len(channel_ids) == 0:
            return jsonify({"error": "channel_ids must be a non-empty list"}), 400

        matcher = get_regex_matcher()
        patterns_data = matcher.get_patterns()

        pattern_count = {}
        pattern_to_channels = {}

        for channel_id in channel_ids:
            channel_patterns = patterns_data.get("patterns", {}).get(str(channel_id), {})
            regex_patterns = channel_patterns.get("regex_patterns")
            if regex_patterns is None:
                regex_patterns = [
                    {"pattern": pattern, "priority": 0}
                    for pattern in channel_patterns.get("regex", [])
                ]

            for pattern_obj in regex_patterns:
                pattern = pattern_obj.get("pattern", "") if isinstance(pattern_obj, dict) else pattern_obj
                if not pattern:
                    continue

                if pattern not in pattern_count:
                    pattern_count[pattern] = 0
                    pattern_to_channels[pattern] = []

                pattern_count[pattern] += 1
                pattern_to_channels[pattern].append(str(channel_id))

        sorted_patterns = sorted(pattern_count.items(), key=lambda item: item[1], reverse=True)
        common_patterns = []
        for pattern, count in sorted_patterns:
            common_patterns.append(
                {
                    "pattern": pattern,
                    "count": count,
                    "channel_ids": pattern_to_channels[pattern],
                    "percentage": round((count / len(channel_ids)) * 100, 1),
                }
            )

        return jsonify(
            {
                "patterns": common_patterns,
                "total_channels": len(channel_ids),
                "total_patterns": len(common_patterns),
            }
        )
    except Exception as exc:
        logger.error(f"Error getting common regex patterns: {exc}")
        return jsonify({"error": "Internal Server Error"}), 500


def bulk_edit_regex_pattern_response(
    *,
    payload: Any,
    get_regex_matcher: Callable[[], Any],
    get_udi_manager: Callable[[], Any],
):
    """Handle edit of one regex pattern across multiple channels."""
    try:
        if not payload:
            return jsonify({"error": "No data provided"}), 400

        required_fields = ["channel_ids", "old_pattern", "new_pattern"]
        if not all(field in payload for field in required_fields):
            return jsonify({"error": f"Missing required fields: {required_fields}"}), 400

        channel_ids = payload["channel_ids"]
        old_pattern = payload["old_pattern"]
        new_pattern = payload["new_pattern"]
        new_m3u_accounts = payload.get("new_m3u_accounts")

        if not isinstance(channel_ids, list) or len(channel_ids) == 0:
            return jsonify({"error": "channel_ids must be a non-empty list"}), 400

        matcher = get_regex_matcher()
        is_valid, _ = matcher.validate_regex_patterns([new_pattern])
        if not is_valid:
            return jsonify({"error": "Invalid value or request parameters"}), 400

        udi = get_udi_manager()
        success_count = 0
        failed_channels = []

        for channel_id in channel_ids:
            try:
                channel = udi.get_channel_by_id(channel_id)
                if not channel:
                    failed_channels.append({"channel_id": channel_id, "error": "Channel not found"})
                    continue

                channel_name = channel.get("name", f"Channel {channel_id}")
                patterns = matcher.get_patterns()
                existing_patterns = patterns.get("patterns", {}).get(str(channel_id), {})

                regex_patterns = existing_patterns.get("regex_patterns")
                if regex_patterns is None:
                    old_regex = existing_patterns.get("regex", [])
                    old_m3u_accounts = existing_patterns.get("m3u_accounts")
                    regex_patterns = [
                        {"pattern": pattern, "m3u_accounts": old_m3u_accounts}
                        for pattern in old_regex
                    ]

                pattern_found = False
                updated_patterns = []
                seen_patterns = set()

                for pattern_obj in regex_patterns:
                    if isinstance(pattern_obj, dict):
                        pattern = pattern_obj.get("pattern", "")
                        pattern_m3u_accounts = pattern_obj.get("m3u_accounts")
                    else:
                        pattern = pattern_obj
                        pattern_m3u_accounts = None

                    if pattern == old_pattern:
                        pattern_found = True
                        if new_pattern not in seen_patterns:
                            updated_patterns.append(
                                {
                                    "pattern": new_pattern,
                                    "m3u_accounts": (
                                        new_m3u_accounts
                                        if new_m3u_accounts is not None
                                        else pattern_m3u_accounts
                                    ),
                                }
                            )
                            seen_patterns.add(new_pattern)
                    else:
                        if pattern not in seen_patterns:
                            updated_patterns.append(
                                {"pattern": pattern, "m3u_accounts": pattern_m3u_accounts}
                            )
                            seen_patterns.add(pattern)

                if pattern_found:
                    matcher.add_channel_pattern(
                        str(channel_id),
                        channel_name,
                        updated_patterns,
                        existing_patterns.get("enabled", True),
                        silent=True,
                    )
                    success_count += 1
                else:
                    failed_channels.append(
                        {"channel_id": channel_id, "error": "Pattern not found in channel"}
                    )
            except Exception as exc:
                logger.error(f"Error editing pattern in channel {channel_id}: {exc}")
                failed_channels.append({"channel_id": channel_id, "error": str(exc)})

        response_data = {
            "message": f"Successfully edited pattern in {success_count} channel(s)",
            "success_count": success_count,
            "total_channels": len(channel_ids),
        }
        if failed_channels:
            response_data["failed_channels"] = failed_channels
            response_data["failed_count"] = len(failed_channels)

        return jsonify(response_data)
    except ValueError as exc:
        logger.warning(f"Validation error in bulk pattern edit: {exc}")
        return jsonify({"error": "Invalid value or request parameters"}), 400
    except Exception as exc:
        logger.error(f"Error bulk editing regex pattern: {exc}")
        return jsonify({"error": "Internal Server Error"}), 500


def bulk_update_match_settings_response(*, payload: Any, get_regex_matcher: Callable[[], Any]):
    """Handle match settings updates for multiple channels."""
    try:
        if not payload:
            return jsonify({"error": "No data provided"}), 400

        channel_ids = payload.get("channel_ids", [])
        settings = payload.get("settings", {})

        if not channel_ids or not isinstance(channel_ids, list):
            return jsonify({"error": "channel_ids must be a non-empty list"}), 400
        if not settings:
            return jsonify({"error": "settings must be provided"}), 400

        matcher = get_regex_matcher()
        success_count = 0
        failed_channels = []

        for channel_id in channel_ids:
            try:
                if "match_by_tvg_id" in settings:
                    if matcher.set_match_by_tvg_id(channel_id, bool(settings["match_by_tvg_id"])):
                        success_count += 1
                    else:
                        failed_channels.append(
                            {"channel_id": channel_id, "error": "Failed to set setting"}
                        )
            except Exception as exc:
                failed_channels.append({"channel_id": channel_id, "error": str(exc)})

        return jsonify(
            {
                "message": f"Updated settings for {success_count} channels",
                "success_count": success_count,
                "failed_count": len(failed_channels),
                "failed_channels": failed_channels,
            }
        )
    except Exception as exc:
        logger.error(f"Error bulk updating match settings: {exc}")
        return jsonify({"error": "Internal Server Error"}), 500


def mass_edit_preview_response(
    *,
    payload: Any,
    get_regex_matcher: Callable[[], Any],
    get_udi_manager: Callable[[], Any],
    is_dangerous_regex: Callable[[str], bool],
):
    """Handle preview for regex mass find/replace."""
    try:
        if not payload:
            return jsonify({"error": "No data provided"}), 400

        required_fields = ["channel_ids", "find_pattern", "replace_pattern"]
        if not all(field in payload for field in required_fields):
            return jsonify({"error": f"Missing required fields: {required_fields}"}), 400

        channel_ids = payload["channel_ids"]
        find_pattern = payload["find_pattern"]
        replace_pattern = payload["replace_pattern"]
        use_regex = payload.get("use_regex", False)

        if not isinstance(channel_ids, list) or len(channel_ids) == 0:
            return jsonify({"error": "channel_ids must be a non-empty list"}), 400

        matcher = get_regex_matcher()
        udi = get_udi_manager()

        if use_regex:
            try:
                if is_dangerous_regex(find_pattern):
                    return jsonify(
                        {
                            "error": "Regex pattern contains dangerous nested quantifiers (ReDoS risk)"
                        }
                    ), 400
                find_regex = re.compile(find_pattern)
            except re.error:
                return jsonify({"error": "Invalid regex pattern"}), 400

        affected_channels = []
        total_patterns_affected = 0

        for channel_id in channel_ids:
            try:
                channel = udi.get_channel_by_id(channel_id)
                if not channel:
                    continue

                channel_name = channel.get("name", f"Channel {channel_id}")
                patterns = matcher.get_patterns()
                existing_patterns = patterns.get("patterns", {}).get(str(channel_id), {})
                regex_patterns = existing_patterns.get("regex_patterns", [])

                if not regex_patterns:
                    continue

                affected_patterns = []
                for pattern_obj in regex_patterns:
                    if isinstance(pattern_obj, dict):
                        pattern = pattern_obj.get("pattern", "")
                        pattern_m3u_accounts = pattern_obj.get("m3u_accounts")
                        pattern_priority = pattern_obj.get("priority", 0)
                    else:
                        pattern = pattern_obj
                        pattern_m3u_accounts = None
                        pattern_priority = 0

                    try:
                        if use_regex:
                            new_pattern = find_regex.sub(replace_pattern, pattern)
                        else:
                            new_pattern = pattern.replace(find_pattern, replace_pattern)
                    except re.error:
                        return jsonify({"error": "Invalid replacement pattern"}), 400

                    if new_pattern != pattern:
                        affected_patterns.append(
                            {
                                "old_pattern": pattern,
                                "new_pattern": new_pattern,
                                "m3u_accounts": pattern_m3u_accounts,
                                "priority": pattern_priority,
                            }
                        )

                if affected_patterns:
                    affected_channels.append(
                        {
                            "channel_id": channel_id,
                            "channel_name": channel_name,
                            "affected_patterns": affected_patterns,
                            "total_affected": len(affected_patterns),
                        }
                    )
                    total_patterns_affected += len(affected_patterns)
            except Exception as exc:
                logger.error(f"Error previewing patterns for channel {channel_id}: {exc}")

        return jsonify(
            {
                "affected_channels": affected_channels,
                "total_channels_affected": len(affected_channels),
                "total_patterns_affected": total_patterns_affected,
                "find_pattern": find_pattern,
                "replace_pattern": replace_pattern,
                "use_regex": use_regex,
            }
        )
    except Exception as exc:
        logger.error(f"Error previewing mass edit: {exc}")
        return jsonify({"error": "Internal Server Error"}), 500


def mass_edit_regex_patterns_response(
    *,
    payload: Any,
    get_regex_matcher: Callable[[], Any],
    get_udi_manager: Callable[[], Any],
    is_dangerous_regex: Callable[[str], bool],
):
    """Handle regex mass find/replace execution across channels."""
    try:
        if not payload:
            return jsonify({"error": "No data provided"}), 400

        required_fields = ["channel_ids", "find_pattern", "replace_pattern"]
        if not all(field in payload for field in required_fields):
            return jsonify({"error": f"Missing required fields: {required_fields}"}), 400

        channel_ids = payload["channel_ids"]
        find_pattern = payload["find_pattern"]
        replace_pattern = payload["replace_pattern"]
        use_regex = payload.get("use_regex", False)
        new_m3u_accounts = payload.get("new_m3u_accounts")

        if not isinstance(channel_ids, list) or len(channel_ids) == 0:
            return jsonify({"error": "channel_ids must be a non-empty list"}), 400

        matcher = get_regex_matcher()
        udi = get_udi_manager()

        if use_regex:
            try:
                if is_dangerous_regex(find_pattern):
                    return jsonify(
                        {
                            "error": "Regex pattern contains dangerous nested quantifiers (ReDoS risk)"
                        }
                    ), 400
                find_regex = re.compile(find_pattern)
            except re.error:
                return jsonify({"error": "Invalid regex pattern"}), 400

        success_count = 0
        failed_channels = []
        total_patterns_updated = 0

        for channel_id in channel_ids:
            try:
                channel = udi.get_channel_by_id(channel_id)
                if not channel:
                    failed_channels.append({"channel_id": channel_id, "error": "Channel not found"})
                    continue

                channel_name = channel.get("name", f"Channel {channel_id}")
                patterns = matcher.get_patterns()
                existing_patterns = patterns.get("patterns", {}).get(str(channel_id), {})
                regex_patterns = existing_patterns.get("regex_patterns", [])

                if not regex_patterns:
                    continue

                updated_patterns = []
                seen_patterns = set()
                patterns_changed = False
                channel_failed = False

                for pattern_obj in regex_patterns:
                    if isinstance(pattern_obj, dict):
                        pattern = pattern_obj.get("pattern", "")
                        pattern_m3u_accounts = pattern_obj.get("m3u_accounts")
                        pattern_priority = pattern_obj.get("priority", 0)
                    else:
                        pattern = pattern_obj
                        pattern_m3u_accounts = None
                        pattern_priority = 0

                    try:
                        if use_regex:
                            new_pattern = find_regex.sub(replace_pattern, pattern)
                        else:
                            new_pattern = pattern.replace(find_pattern, replace_pattern)
                    except re.error as exc:
                        failed_channels.append(
                            {
                                "channel_id": channel_id,
                                "error": f"Invalid replacement pattern: {str(exc)}",
                            }
                        )
                        channel_failed = True
                        break

                    if new_pattern != pattern:
                        patterns_changed = True

                    is_valid, error_msg = matcher.validate_regex_patterns([new_pattern])
                    if not is_valid:
                        failed_channels.append(
                            {
                                "channel_id": channel_id,
                                "error": f"Invalid resulting pattern '{new_pattern}': {error_msg}",
                            }
                        )
                        channel_failed = True
                        break

                    if new_pattern not in seen_patterns:
                        final_m3u_accounts = (
                            new_m3u_accounts if new_m3u_accounts is not None else pattern_m3u_accounts
                        )
                        updated_patterns.append(
                            {
                                "pattern": new_pattern,
                                "m3u_accounts": final_m3u_accounts,
                                "priority": pattern_priority,
                            }
                        )
                        seen_patterns.add(new_pattern)

                if not channel_failed and patterns_changed and updated_patterns:
                    matcher.add_channel_pattern(
                        str(channel_id),
                        channel_name,
                        updated_patterns,
                        existing_patterns.get("enabled", True),
                        silent=True,
                    )
                    success_count += 1
                    total_patterns_updated += len(updated_patterns)
            except Exception as exc:
                logger.error(f"Error applying mass edit to channel {channel_id}: {exc}")
                failed_channels.append({"channel_id": channel_id, "error": str(exc)})

        logger.info(
            f"Mass edit completed: {success_count} channels updated, {total_patterns_updated} patterns affected"
        )

        response_data = {
            "message": f"Successfully updated {success_count} channel(s)",
            "success_count": success_count,
            "total_channels": len(channel_ids),
            "total_patterns_updated": total_patterns_updated,
        }
        if failed_channels:
            response_data["failed_channels"] = failed_channels
            response_data["failed_count"] = len(failed_channels)

        return jsonify(response_data)
    except Exception as exc:
        logger.error(f"Error in mass edit: {exc}")
        return jsonify({"error": "Internal Server Error"}), 500


def import_regex_patterns_response(*, payload: Any, get_regex_matcher: Callable[[], Any]):
    """Handle import of regex patterns from canonical JSON payload."""
    try:
        if not payload:
            return jsonify({"error": "No data provided"}), 400

        if not isinstance(payload, dict):
            return jsonify({"error": "Invalid JSON format: must be an object"}), 400
        if "patterns" not in payload:
            return jsonify({"error": "Invalid JSON format: missing 'patterns' field"}), 400
        if not isinstance(payload["patterns"], dict):
            return jsonify({"error": "Invalid JSON format: 'patterns' must be an object"}), 400

        matcher = get_regex_matcher()
        for channel_id, pattern_data in payload["patterns"].items():
            if not isinstance(pattern_data, dict):
                return jsonify({"error": f"Invalid pattern format for channel {channel_id}"}), 400

            regex_patterns_to_validate = []
            if "regex_patterns" in pattern_data:
                if not isinstance(pattern_data["regex_patterns"], list):
                    return jsonify({"error": f"'regex_patterns' must be a list for channel {channel_id}"}), 400

                for pattern_obj in pattern_data["regex_patterns"]:
                    if isinstance(pattern_obj, dict):
                        pattern = pattern_obj.get("pattern", "")
                        if not pattern:
                            return jsonify(
                                {
                                    "error": (
                                        "Pattern object missing or has empty 'pattern' "
                                        f"field for channel {channel_id}"
                                    )
                                }
                            ), 400
                        regex_patterns_to_validate.append(pattern)
                    elif isinstance(pattern_obj, str):
                        regex_patterns_to_validate.append(pattern_obj)
                    else:
                        return jsonify(
                            {
                                "error": (
                                    "Pattern in regex_patterns must be a string or object "
                                    f"for channel {channel_id}, got {type(pattern_obj).__name__}"
                                )
                            }
                        ), 400
            elif "regex" in pattern_data:
                if not isinstance(pattern_data["regex"], list):
                    return jsonify({"error": f"'regex' must be a list for channel {channel_id}"}), 400
                regex_patterns_to_validate = pattern_data["regex"]
            else:
                return jsonify(
                    {"error": f"Missing 'regex' or 'regex_patterns' field for channel {channel_id}"}
                ), 400

            if not regex_patterns_to_validate:
                return jsonify({"error": f"No patterns provided for channel {channel_id}"}), 400

            is_valid, _ = matcher.validate_regex_patterns(regex_patterns_to_validate)
            if not is_valid:
                return jsonify(
                    {"error": f"Invalid regex pattern components for channel {channel_id}"}
                ), 400

        from apps.database.manager import get_db_manager

        db = get_db_manager()
        _imported, errors = db.import_channel_regex_configs_from_json(payload, merge=False)
        if errors:
            logger.error(f"Errors during import: {errors}")
            return jsonify({"error": "Import failed", "details": errors}), 500

        if "global_settings" in payload and isinstance(payload["global_settings"], dict):
            db.set_system_setting("channel_regex_global_settings", payload["global_settings"])

        matcher.reload_patterns()
        pattern_count = len(payload["patterns"])
        logger.info(f"Imported {pattern_count} regex patterns successfully")

        return jsonify(
            {
                "message": f"Successfully imported {pattern_count} patterns",
                "pattern_count": pattern_count,
            }
        )
    except Exception as exc:
        logger.error(f"Error importing regex patterns: {exc}")
        return jsonify({"error": "Internal Server Error"}), 500


def export_regex_patterns_response():
    """Handle export of regex patterns in canonical importable format."""
    try:
        from apps.database.manager import get_db_manager

        db = get_db_manager()
        export_data = db.export_channel_regex_configs_as_json()
        return jsonify(export_data)
    except Exception as exc:
        logger.error(f"Error exporting regex patterns: {exc}")
        return jsonify({"error": "Internal Server Error"}), 500


def test_regex_pattern_response(
    *,
    payload: Any,
    is_dangerous_regex: Callable[[str], bool],
    whitespace_pattern: Any,
):
    """Handle single regex test against one stream name."""
    try:
        if not payload or "pattern" not in payload or "stream_name" not in payload:
            return jsonify({"error": "Missing pattern or stream_name"}), 400

        pattern = payload["pattern"]
        stream_name = payload["stream_name"]
        case_sensitive = payload.get("case_sensitive", False)

        search_pattern = pattern if case_sensitive else pattern.lower()
        search_name = stream_name if case_sensitive else stream_name.lower()
        search_pattern = whitespace_pattern.sub(r"\\s+", search_pattern)

        try:
            if is_dangerous_regex(search_pattern):
                return jsonify(
                    {"error": "Regex pattern contains dangerous nested quantifiers (ReDoS risk)"}
                ), 400
            match = re.search(search_pattern, search_name)
            return jsonify(
                {
                    "matches": bool(match),
                    "match_details": {
                        "pattern": pattern,
                        "stream_name": stream_name,
                        "case_sensitive": case_sensitive,
                        "match_start": match.start() if match else None,
                        "match_end": match.end() if match else None,
                        "matched_text": match.group() if match else None,
                    },
                }
            )
        except re.error:
            return jsonify({"error": "Invalid regex pattern"}), 400
    except Exception as exc:
        logger.error(f"Error testing regex pattern: {exc}")
        return jsonify({"error": "Internal Server Error"}), 500


def test_regex_pattern_live_response(
    *,
    payload: Any,
    is_dangerous_regex: Callable[[str], bool],
    whitespace_pattern: Any,
):
    """Handle live regex testing across available streams."""
    try:
        from apps.core.api_utils import get_streams, get_m3u_accounts

        if not payload:
            return jsonify({"error": "Missing request body"}), 400

        patterns = payload.get("patterns", [])
        case_sensitive = payload.get("case_sensitive", True)
        max_matches_per_pattern = payload.get("max_matches", 100)

        if not patterns:
            return jsonify({"error": "No patterns provided"}), 400

        all_streams = get_streams()
        if not all_streams:
            return jsonify({"matches": [], "total_streams": 0, "message": "No streams available"})

        m3u_accounts_list = get_m3u_accounts() or []
        m3u_account_map = {
            account.get("id"): account.get("name", f"Account {account.get('id')}")
            for account in m3u_accounts_list
            if account.get("id") is not None
        }

        results = []
        for pattern_info in patterns:
            channel_id = pattern_info.get("channel_id", "unknown")
            channel_name = pattern_info.get("channel_name", "Unknown Channel")
            regex_patterns = pattern_info.get("regex", [])
            m3u_accounts = pattern_info.get("m3u_accounts")

            if not regex_patterns:
                continue

            matched_streams = []
            streams_to_test = all_streams
            if m3u_accounts:
                logger.debug(
                    f"Filtering streams by M3U accounts {m3u_accounts}: testing against subset of streams"
                )
                streams_to_test = [
                    stream for stream in all_streams if stream.get("m3u_account") in m3u_accounts
                ]
                logger.debug(f"Filtered to {len(streams_to_test)} of {len(all_streams)} streams")

            for stream in streams_to_test:
                if not isinstance(stream, dict):
                    continue

                stream_name = stream.get("name", "")
                stream_id = stream.get("id")
                if not stream_name:
                    continue

                search_name = stream_name if case_sensitive else stream_name.lower()
                matched = False
                matched_pattern = None

                for pattern in regex_patterns:
                    escaped_channel_name = re.escape(channel_name)
                    substituted_pattern = pattern.replace("CHANNEL_NAME", escaped_channel_name)
                    search_pattern = substituted_pattern if case_sensitive else substituted_pattern.lower()
                    search_pattern = whitespace_pattern.sub(r"\\s+", search_pattern)

                    try:
                        if is_dangerous_regex(search_pattern):
                            logger.warning(f"Invalid regex pattern '{pattern}': ReDoS risk")
                            continue
                        if re.search(search_pattern, search_name):
                            matched = True
                            matched_pattern = pattern
                            break
                    except re.error as exc:
                        logger.warning(f"Invalid regex pattern '{pattern}': {exc}")
                        continue

                if matched and len(matched_streams) < max_matches_per_pattern:
                    m3u_account_id = stream.get("m3u_account")
                    matched_streams.append(
                        {
                            "stream_id": stream_id,
                            "stream_name": stream_name,
                            "matched_pattern": matched_pattern,
                            "m3u_account": m3u_account_id,
                            "m3u_account_name": (
                                m3u_account_map.get(m3u_account_id) if m3u_account_id else None
                            ),
                        }
                    )

            results.append(
                {
                    "channel_id": channel_id,
                    "channel_name": channel_name,
                    "patterns": regex_patterns,
                    "m3u_accounts": m3u_accounts,
                    "matched_streams": matched_streams,
                    "match_count": len(matched_streams),
                    "total_tested_streams": len(streams_to_test),
                }
            )

        return jsonify(
            {
                "results": results,
                "total_streams": len(all_streams),
                "case_sensitive": case_sensitive,
            }
        )
    except Exception as exc:
        logger.error(f"Error testing regex patterns live: {exc}")
        return jsonify({"error": "Internal Server Error"}), 500
