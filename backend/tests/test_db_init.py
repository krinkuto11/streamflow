"""Database initialization smoke tests."""

from apps.database.connection import get_session, init_db
from apps.database.manager import get_db_manager
from apps.database.models import Channel, ChannelGroup, Stream


def test_database_initializes_and_basic_dal_reads_work():
    """Exercise schema creation and basic ORM/DAL access without touching disk."""
    assert init_db() is True

    session = get_session()
    try:
        group = ChannelGroup(id=1, name="Test Group")
        channel = Channel(id=101, name="Test Channel", channel_group_id=1)
        stream = Stream(id=1001, name="Test Stream", url="http://example.com/stream.m3u8")

        session.add(group)
        session.flush()
        session.add(channel)
        session.flush()
        session.add(stream)
        session.commit()

        queried_channel = session.query(Channel).filter_by(id=101).first()
        assert queried_channel is not None
        assert queried_channel.name == "Test Channel"
        assert queried_channel.group.name == "Test Group"
    finally:
        session.close()

    db_manager = get_db_manager()
    channels = db_manager.get_channels(as_dict=True)
    streams = db_manager.get_streams(as_dict=True)

    assert len(channels) == 1
    assert channels[0]["name"] == "Test Channel"
    assert len(streams) == 1
    assert streams[0]["name"] == "Test Stream"
