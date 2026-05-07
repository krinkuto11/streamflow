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

        if query.page is None:
            # No pagination: apply order, then enrich the full ordered list
            ordered = self._channel_order_manager.apply_order(sorted_channels)
            return {
                "items": self._enrich_channels(ordered),
                "paginated": False,
            }

        # Paginate first, then enrich only the page slice — avoids O(N) enrichment
        # for every channel when the caller only wants one page of results.
        result = self._paginate(sorted_channels, query.page, query.per_page)
        result["items"] = self._enrich_channels(result["items"])
        return result

    def get_channel_stats(self, channel_id: int) -> Dict[str, Any]:
        channel = self._repository.get_channel_by_id(channel_id)
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
        if not channels:
            return channels

        # One DB query fetches all six config dicts; remaining work is pure dict lookups.
        bulk = self._automation_config.get_bulk_enrichment_data()
        chan_assign = bulk.get("channel_assignments", {})
        grp_assign = bulk.get("group_assignments", {})
        chan_periods = bulk.get("channel_period_assignments", {})
        grp_periods = bulk.get("group_period_assignments", {})
        chan_epg = bulk.get("channel_epg_scheduled_assignments", {})
        grp_epg = bulk.get("group_epg_scheduled_assignments", {})

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
            cid_str = str(channel_id)
            gid_str = str(group_id) if group_id is not None else None

            # Automation profile — in-memory lookups only
            assigned_profile_id = chan_assign.get(cid_str)
            group_profile_id = grp_assign.get(gid_str) if gid_str else None
            ch_copy["automation_profile_id"] = assigned_profile_id or group_profile_id
            ch_copy["assigned_profile_id"] = assigned_profile_id
            ch_copy["group_profile_id"] = group_profile_id
            if assigned_profile_id:
                ch_copy["automation_profile_source"] = "channel"
            elif group_profile_id:
                ch_copy["automation_profile_source"] = "group"
            else:
                ch_copy["automation_profile_source"] = "default"

            # Periods
            channel_period_map = chan_periods.get(cid_str, {})
            if not isinstance(channel_period_map, dict):
                channel_period_map = {}
            group_period_map = grp_periods.get(gid_str, {}) if gid_str else {}
            if not isinstance(group_period_map, dict):
                group_period_map = {}
            effective_periods = {**group_period_map, **channel_period_map}
            ch_copy["automation_periods_count"] = len(effective_periods)
            ch_copy["channel_periods_count"] = len(channel_period_map)
            ch_copy["group_periods_count"] = len(group_period_map)
            if channel_period_map:
                ch_copy["automation_periods_source"] = "channel"
            elif group_period_map:
                ch_copy["automation_periods_source"] = "group"
            else:
                ch_copy["automation_periods_source"] = "none"

            # EPG scheduled profile
            channel_epg_id = chan_epg.get(cid_str)
            group_epg_id = grp_epg.get(gid_str) if gid_str else None
            ch_copy["channel_epg_scheduled_profile_id"] = channel_epg_id
            ch_copy["epg_scheduled_profile_id"] = channel_epg_id or group_epg_id

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
