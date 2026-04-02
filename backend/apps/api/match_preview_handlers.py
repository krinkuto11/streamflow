"""Live match preview API handler functions extracted from web_api."""

import re
from typing import Any, Callable

from flask import jsonify

from apps.core.logging_config import setup_logging

logger = setup_logging(__name__)


def test_match_live_response(
    *,
    payload: Any,
    get_streams: Callable[[], Any],
    get_m3u_accounts: Callable[[], Any],
    is_dangerous_regex: Callable[[str], bool],
):
    """Handle live stream matching preview with regex and TVG-ID priority."""
    try:
        data = payload
        if not data:
            return jsonify({"error": "Missing request body"}), 400

        channel_name = data.get("channel_name", "Unknown Channel")
        match_by_tvg_id = data.get("match_by_tvg_id", False)
        tvg_id = data.get("tvg_id", None)

        regex_patterns = data.get("regex_patterns", [])
        normalized_patterns = []
        for pattern in regex_patterns:
            if isinstance(pattern, dict):
                normalized_patterns.append(pattern)
            elif isinstance(pattern, str):
                normalized_patterns.append({"pattern": pattern, "priority": 0})

        match_priority_order = data.get("match_priority_order", ["tvg", "regex"])
        case_sensitive = data.get("case_sensitive", True)
        max_matches = data.get("max_matches", 100)

        all_streams = get_streams()
        if not all_streams:
            return jsonify({"matches": [], "total_streams": 0})

        m3u_accounts_list = get_m3u_accounts() or []
        m3u_account_map = {
            account.get("id"): account.get("name", f"Account {account.get('id')}")
            for account in m3u_accounts_list
            if account.get("id") is not None
        }

        whitespace_pattern = re.compile(r"(?<!\\) +")
        matches = []

        for stream in all_streams:
            if not isinstance(stream, dict):
                continue

            stream_name = stream.get("name", "")
            stream_id = stream.get("id")
            if not stream_name:
                continue

            stream_tvg_id = stream.get("tvg_id")
            stream_m3u_account = stream.get("m3u_account")

            matched = False
            priority = 0
            match_source = "regex"
            matched_pattern = None

            for match_type in match_priority_order:
                if match_type == "tvg":
                    if match_by_tvg_id and tvg_id and stream_tvg_id:
                        if stream_tvg_id == tvg_id:
                            matched = True
                            match_source = "tvg_id"
                            if match_priority_order[0] == "tvg":
                                priority = 1000
                            else:
                                priority = 0
                            break

                elif match_type == "regex":
                    if matched:
                        continue

                    search_name = stream_name if case_sensitive else stream_name.lower()
                    regex_matched = False
                    best_regex_priority = 0
                    best_pattern_str = None

                    for pattern_obj in normalized_patterns:
                        pattern = pattern_obj.get("pattern", "")
                        pattern_m3u_accounts = pattern_obj.get("m3u_accounts")
                        pattern_priority = pattern_obj.get("priority", 0)

                        if not pattern:
                            continue

                        if pattern_m3u_accounts and len(pattern_m3u_accounts) > 0:
                            if (
                                stream_m3u_account is None
                                or stream_m3u_account not in pattern_m3u_accounts
                            ):
                                continue

                        escaped_channel_name = re.escape(channel_name)
                        substituted_pattern = pattern.replace("CHANNEL_NAME", escaped_channel_name)

                        search_pattern = substituted_pattern if case_sensitive else substituted_pattern.lower()
                        search_pattern = whitespace_pattern.sub(r"\\s+", search_pattern)

                        try:
                            if is_dangerous_regex(search_pattern):
                                continue
                            if re.search(search_pattern, search_name):
                                regex_matched = True
                                if pattern_priority >= best_regex_priority:
                                    best_regex_priority = pattern_priority
                                    best_pattern_str = pattern
                        except re.error:
                            continue

                    if regex_matched:
                        matched = True
                        match_source = "regex"
                        priority = best_regex_priority
                        matched_pattern = best_pattern_str
                        break

            if matched:
                matches.append(
                    {
                        "stream_id": stream_id,
                        "stream_name": stream_name,
                        "stream_tvg_id": stream_tvg_id,
                        "m3u_account_name": m3u_account_map.get(stream_m3u_account),
                        "source": match_source,
                        "priority": priority,
                        "matched_pattern": matched_pattern,
                    }
                )

                if len(matches) >= max_matches:
                    break

        return jsonify(
            {
                "matches": matches,
                "total_tested_streams": len(all_streams),
                "total_matches": len(matches),
            }
        )
    except Exception as exc:
        logger.error(f"Error testing match live: {exc}")
        return jsonify({"error": "Internal Server Error"}), 500


def bulk_match_count_response(
    *,
    payload: Any,
    get_streams: Callable[[], Any],
    get_m3u_accounts: Callable[[], Any],
    is_dangerous_regex: Callable[[str], bool],
):
    """Handle bulk regex match count for multiple channels in one stream pool scan.

    Accepts an array of channel configs and returns per-channel match counts.
    The stream pool is fetched once and shared across all channel evaluations,
    making this significantly cheaper than N individual test-match-live calls.

    Request body:
        {
            "channels": [
                {
                    "channel_id": 7200,
                    "channel_name": "Fox News",
                    "tvg_id": "FoxNews.us",
                    "match_by_tvg_id": false,
                    "regex_patterns": [{"pattern": "Fox News.*", "m3u_accounts": null}],
                    "match_priority_order": ["tvg", "regex"],
                    "case_sensitive": true
                },
                ...
            ]
        }

    Response:
        {
            "counts": {"7200": 47, "7201": 12},
            "total_streams": 12847
        }

    Channels with no effective patterns are omitted from counts entirely;
    the frontend handles the display of '—' for those.
    """
    try:
        data = payload
        if not data:
            return jsonify({"error": "Missing request body"}), 400

        channel_configs = data.get("channels", [])
        if not channel_configs:
            return jsonify({"counts": {}, "total_streams": 0})

        # Fetch stream pool once — shared across all channel evaluations
        all_streams = get_streams(log_result=False)
        if not all_streams:
            return jsonify({"counts": {}, "total_streams": 0})

        whitespace_pattern = re.compile(r"(?<!\\) +")
        counts = {}

        for channel_cfg in channel_configs:
            channel_id = channel_cfg.get("channel_id")
            if channel_id is None:
                continue

            channel_name = channel_cfg.get("channel_name", "Unknown Channel")
            match_by_tvg_id = channel_cfg.get("match_by_tvg_id", False)
            tvg_id = channel_cfg.get("tvg_id")
            case_sensitive = channel_cfg.get("case_sensitive", True)
            match_priority_order = channel_cfg.get("match_priority_order", ["tvg", "regex"])

            # Normalize regex patterns (same format as test_match_live_response)
            raw_patterns = channel_cfg.get("regex_patterns", [])
            normalized_patterns = []
            for p in raw_patterns:
                if isinstance(p, dict):
                    normalized_patterns.append(p)
                elif isinstance(p, str):
                    normalized_patterns.append({"pattern": p, "priority": 0})

            # Skip channels with nothing to match against
            has_regex = len(normalized_patterns) > 0
            has_tvg = match_by_tvg_id and bool(tvg_id)
            if not has_regex and not has_tvg:
                continue

            match_count = 0

            for stream in all_streams:
                if not isinstance(stream, dict):
                    continue

                stream_name = stream.get("name", "")
                if not stream_name:
                    continue

                stream_tvg_id = stream.get("tvg_id")
                stream_m3u_account = stream.get("m3u_account")

                matched = False

                for match_type in match_priority_order:
                    if matched:
                        break

                    if match_type == "tvg":
                        if has_tvg and stream_tvg_id and stream_tvg_id == tvg_id:
                            matched = True

                    elif match_type == "regex" and has_regex:
                        search_name = stream_name if case_sensitive else stream_name.lower()

                        for pattern_obj in normalized_patterns:
                            pattern = pattern_obj.get("pattern", "")
                            if not pattern:
                                continue

                            pattern_m3u_accounts = pattern_obj.get("m3u_accounts")
                            if pattern_m3u_accounts and len(pattern_m3u_accounts) > 0:
                                if (
                                    stream_m3u_account is None
                                    or stream_m3u_account not in pattern_m3u_accounts
                                ):
                                    continue

                            escaped_channel_name = re.escape(channel_name)
                            substituted_pattern = pattern.replace("CHANNEL_NAME", escaped_channel_name)
                            search_pattern = substituted_pattern if case_sensitive else substituted_pattern.lower()
                            search_pattern = whitespace_pattern.sub(r"\\s+", search_pattern)

                            try:
                                if is_dangerous_regex(search_pattern):
                                    continue
                                if re.search(search_pattern, search_name):
                                    matched = True
                                    break
                            except re.error:
                                continue

                if matched:
                    match_count += 1

            counts[str(channel_id)] = match_count

        return jsonify(
            {
                "counts": counts,
                "total_streams": len(all_streams),
            }
        )
    except Exception as exc:
        logger.error(f"Error computing bulk match counts: {exc}")
        return jsonify({"error": "Internal Server Error"}), 500
