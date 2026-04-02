"""Service layer for channel listing and stats workflows."""

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, cast

from apps.channels.repository import ChannelRepository
from apps.core.stream_stats_utils import (
    calculate_channel_averages,
    extract_stream_stats,
    parse_bitrate_value,
)


@dataclass(frozen=True)
class ChannelQuery:
    """Normalized query inputs for channel list operations."""

    search: str = ""
    sort_by: str = "name"
    sort_dir: str = "asc"
    page: Optional[int] = None
    per_page: int = 50


class ChannelService:
    """Coordinates channel read workflows across repository and managers."""

    _VALID_SORT_COLS = {"name", "channel_number", "id"}

    def __init__(
        self,
        *,
        repository: ChannelRepository,
        automation_config_manager: Any,
        channel_order_manager: Any,
        stream_checker_service: Any,
    ) -> None:
        self._repository = repository
        self._automation_config = automation_config_manager
        self._channel_order_manager = channel_order_manager
        self._stream_checker_service = stream_checker_service

    def list_channels(self, query: ChannelQuery) -> Dict[str, Any]:
        channels = self._repository.get_channels()
        if channels is None:
            return {"error": "Failed to fetch channels", "status": 500}

        filtered = self._apply_search(channels, query.search)
        sorted_channels = self._apply_sorting(filtered, query.sort_by, query.sort_dir)
        enriched = self._enrich_channels(sorted_channels)

        if query.page is None:
            ordered = self._channel_order_manager.apply_order(enriched)
            return {
                "items": ordered,
                "paginated": False,
            }

        return self._paginate(enriched, query.page, query.per_page)

    def get_channel_stats(self, channel_id: int) -> Dict[str, Any]:
        channels = self._repository.get_channels()
        if channels is None:
            return {"error": "Failed to fetch channels", "status": 500}

        channels_dict = {
            cast(int, ch["id"]): ch
            for ch in channels
            if isinstance(ch, dict) and "id" in ch
        }
        channel = channels_dict.get(channel_id)
        if not channel:
            return {"error": "Channel not found", "status": 404}

        stream_ids = channel.get("streams", [])
        streams = []
        for stream_id in stream_ids:
            if isinstance(stream_id, int):
                stream = self._repository.get_stream_by_id(stream_id)
                if stream:
                    streams.append(stream)

        dead_count = 0
        checker = self._stream_checker_service
        if checker and getattr(checker, "dead_streams_tracker", None):
            dead_count = checker.dead_streams_tracker.get_dead_streams_count_for_channel(channel_id)

        channel_averages = calculate_channel_averages(streams, dead_stream_ids=set())
        most_common_resolution = channel_averages.get("avg_resolution", "Unknown")
        avg_bitrate_str = channel_averages.get("avg_bitrate", "N/A")
        avg_bitrate = 0
        if avg_bitrate_str != "N/A":
            parsed_bitrate = parse_bitrate_value(avg_bitrate_str)
            if parsed_bitrate:
                avg_bitrate = int(parsed_bitrate)

        resolutions: Dict[str, int] = {}
        for stream in streams:
            stats = extract_stream_stats(stream)
            resolution = stats.get("resolution", "Unknown")
            if resolution not in {"Unknown", "N/A"}:
                resolutions[resolution] = resolutions.get(resolution, 0) + 1

        return {
            "status": 200,
            "data": {
                "channel_id": channel_id,
                "channel_name": channel.get("name", ""),
                "logo_id": channel.get("logo_id"),
                "total_streams": len(stream_ids),
                "dead_streams": dead_count,
                "most_common_resolution": most_common_resolution,
                "average_bitrate": avg_bitrate,
                "resolutions": resolutions,
            },
        }

    def _apply_search(self, channels: List[Dict[str, Any]], search: str) -> List[Dict[str, Any]]:
        if not search:
            return channels
        search_lower = search.lower()
        return [ch for ch in channels if search_lower in ch.get("name", "").lower()]

    def _apply_sorting(
        self,
        channels: List[Dict[str, Any]],
        sort_by: str,
        sort_dir: str,
    ) -> List[Dict[str, Any]]:
        resolved_sort_by = sort_by if sort_by in self._VALID_SORT_COLS else "name"
        reverse = sort_dir == "desc"
        return sorted(
            channels,
            key=lambda ch: (ch.get(resolved_sort_by) is None, ch.get(resolved_sort_by, "")),
            reverse=reverse,
        )

    def _enrich_channels(self, channels: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        enriched = []
        for channel in channels:
            ch_copy = channel.copy()
            channel_id = ch_copy.get("id")
            group_id = ch_copy.get("channel_group_id")

            if channel_id is None:
                ch_copy["assigned_profile_id"] = None
                ch_copy["group_profile_id"] = None
                ch_copy["automation_profile_source"] = "default"
                ch_copy["automation_periods_count"] = 0
                ch_copy["channel_periods_count"] = 0
                ch_copy["group_periods_count"] = 0
                ch_copy["automation_periods_source"] = "none"
                ch_copy["channel_epg_scheduled_profile_id"] = None
                ch_copy["epg_scheduled_profile_id"] = None
                enriched.append(ch_copy)
                continue

            channel_id = cast(int, channel_id)
            group_id = cast(Optional[int], group_id)

            ch_copy["automation_profile_id"] = self._automation_config.get_effective_profile_id(
                channel_id,
                group_id,
            )
            ch_copy["assigned_profile_id"] = self._automation_config.get_channel_assignment(channel_id)
            ch_copy["group_profile_id"] = (
                self._automation_config.get_group_assignment(group_id)
                if group_id is not None
                else None
            )
            if ch_copy["assigned_profile_id"]:
                ch_copy["automation_profile_source"] = "channel"
            elif ch_copy["group_profile_id"]:
                ch_copy["automation_profile_source"] = "group"
            else:
                ch_copy["automation_profile_source"] = "default"

            periods = self._automation_config.get_effective_channel_periods(channel_id, group_id)
            ch_copy["automation_periods_count"] = len(periods)

            channel_periods = self._automation_config.get_channel_periods(channel_id)
            group_periods = (
                self._automation_config.get_group_periods(group_id)
                if group_id is not None
                else {}
            )
            ch_copy["channel_periods_count"] = len(channel_periods)
            ch_copy["group_periods_count"] = len(group_periods)
            if ch_copy["channel_periods_count"] > 0:
                ch_copy["automation_periods_source"] = "channel"
            elif ch_copy["group_periods_count"] > 0:
                ch_copy["automation_periods_source"] = "group"
            else:
                ch_copy["automation_periods_source"] = "none"

            ch_copy["channel_epg_scheduled_profile_id"] = (
                self._automation_config.get_channel_epg_scheduled_assignment(channel_id)
            )
            ch_copy["epg_scheduled_profile_id"] = self._automation_config.get_effective_epg_scheduled_profile_id(
                channel_id,
                group_id,
            )
            enriched.append(ch_copy)

        return enriched

    def _paginate(self, items: List[Dict[str, Any]], page: int, per_page: int) -> Dict[str, Any]:
        total = len(items)
        start = (page - 1) * per_page
        end = start + per_page
        page_items = items[start:end]
        total_pages = max(1, (total + per_page - 1) // per_page)

        return {
            "paginated": True,
            "items": page_items,
            "total": total,
            "page": page,
            "per_page": per_page,
            "total_pages": total_pages,
            "has_next": end < total,
            "has_prev": page > 1,
        }
