"""Repository abstractions for channel-related data access."""

from typing import Any, Callable, Dict, List, Optional, Protocol

from apps.udi import get_udi_manager


class ChannelRepository(Protocol):
    """Abstraction for channel and stream reads."""

    def get_channels(self) -> Optional[List[Dict[str, Any]]]:
        ...

    def get_channel_by_id(self, channel_id: int) -> Optional[Dict[str, Any]]:
        ...

    def get_stream_by_id(self, stream_id: int) -> Optional[Dict[str, Any]]:
        ...

    def get_channel_group_by_id(self, group_id: int) -> Optional[Dict[str, Any]]:
        ...


class UdiChannelRepository:
    """Repository implementation backed by the UDI manager."""

    def __init__(self, udi_provider: Callable[[], Any] = get_udi_manager) -> None:
        self._udi_provider = udi_provider

    def _udi(self) -> Any:
        return self._udi_provider()

    def get_channels(self) -> Optional[List[Dict[str, Any]]]:
        return self._udi().get_channels()

    def get_channel_by_id(self, channel_id: int) -> Optional[Dict[str, Any]]:
        return self._udi().get_channel_by_id(channel_id)

    def get_stream_by_id(self, stream_id: int) -> Optional[Dict[str, Any]]:
        return self._udi().get_stream_by_id(stream_id)

    def get_channel_group_by_id(self, group_id: int) -> Optional[Dict[str, Any]]:
        return self._udi().get_channel_group_by_id(group_id)
