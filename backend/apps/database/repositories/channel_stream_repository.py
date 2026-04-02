"""Repository slice for channel and stream read operations."""

from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from apps.database.models import Channel, Stream


def _model_to_dict(obj: Any) -> Dict[str, Any]:
    if not obj:
        return {}

    result: Dict[str, Any] = {}
    for column in obj.__table__.columns:
        value = getattr(obj, column.name)
        if isinstance(value, datetime):
            value = value.isoformat()
        result[column.name] = value
    return result


class ChannelStreamRepository:
    """Encapsulates channel/stream read queries used by DatabaseManager."""

    def get_channels(self, session: Session, *, as_dict: bool = True) -> List[Any]:
        channels = session.query(Channel).all()
        if not as_dict:
            return channels

        result: List[Dict[str, Any]] = []
        for channel in channels:
            channel_dict = _model_to_dict(channel)
            channel_dict["streams"] = [stream.id for stream in channel.streams]
            result.append(channel_dict)
        return result

    def get_channel_by_id(self, session: Session, channel_id: int, *, as_dict: bool = True) -> Optional[Any]:
        channel = session.query(Channel).filter(Channel.id == channel_id).first()
        if channel is None or not as_dict:
            return channel

        channel_dict = _model_to_dict(channel)
        channel_dict["streams"] = [stream.id for stream in channel.streams]
        return channel_dict

    def get_streams(self, session: Session, *, as_dict: bool = True) -> List[Any]:
        streams = session.query(Stream).all()
        if not as_dict:
            return streams
        return [_model_to_dict(stream) for stream in streams]

    def get_stream_by_id(self, session: Session, stream_id: int, *, as_dict: bool = True) -> Optional[Any]:
        stream = session.query(Stream).filter(Stream.id == stream_id).first()
        if stream is None or not as_dict:
            return stream
        return _model_to_dict(stream)
