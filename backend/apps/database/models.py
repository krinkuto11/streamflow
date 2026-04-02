from sqlalchemy import Column, Integer, String, Float, Boolean, DateTime, ForeignKey, Table, Index
from sqlalchemy.orm import relationship
from sqlalchemy.dialects.sqlite import JSON
from datetime import datetime

from apps.database.connection import Base

# ==========================================
# Association Tables (Many-to-Many)
# ==========================================

channel_streams = Table(
    'channel_streams',
    Base.metadata,
    Column('channel_id', Integer, ForeignKey('channels.id', ondelete='CASCADE'), primary_key=True),
    Column('stream_id', Integer, ForeignKey('streams.id', ondelete='CASCADE'), primary_key=True)
)

group_accounts = Table(
    'group_accounts',
    Base.metadata,
    Column('group_id', Integer, ForeignKey('channel_groups.id', ondelete='CASCADE'), primary_key=True),
    Column('account_id', Integer, ForeignKey('m3u_accounts.id', ondelete='CASCADE'), primary_key=True)
)

# ==========================================
# UDI Core Models (Cache)
# ==========================================

class ChannelGroup(Base):
    __tablename__ = 'channel_groups'
    
    id = Column(Integer, primary_key=True, autoincrement=False) # ID from UDI/Dispatcharr
    name = Column(String(255), nullable=False)
    match_profile_id = Column(Integer, ForeignKey('match_profiles.id', ondelete='SET NULL'), nullable=True)
    
    # Relationships
    channels = relationship("Channel", back_populates="group")
    accounts = relationship("M3UAccount", secondary=group_accounts, back_populates="groups")


class Logo(Base):
    __tablename__ = 'logos'
    
    id = Column(Integer, primary_key=True, autoincrement=False)
    name = Column(String(255), nullable=True)
    url = Column(String(1024), nullable=True)
    cache_url = Column(String(1024), nullable=True)


class Channel(Base):
    __tablename__ = 'channels'
    
    id = Column(Integer, primary_key=True, autoincrement=False)
    channel_number = Column(Integer, nullable=True)
    name = Column(String(255), nullable=False)
    channel_group_id = Column(Integer, ForeignKey('channel_groups.id', ondelete='SET NULL'), nullable=True)
    tvg_id = Column(String(100), nullable=True)
    epg_data_id = Column(Integer, nullable=True)
    stream_profile_id = Column(Integer, nullable=True)
    uuid = Column(String(100), nullable=True)
    logo_id = Column(Integer, ForeignKey('logos.id', ondelete='SET NULL'), nullable=True)
    user_level = Column(Integer, nullable=True)
    auto_created = Column(Boolean, default=False)
    auto_created_by = Column(Integer, nullable=True)
    auto_created_by_name = Column(String(255), nullable=True)
    tvc_guide_stationid = Column(String(100), nullable=True)
    match_profile_id = Column(Integer, ForeignKey('match_profiles.id', ondelete='SET NULL'), nullable=True)
    is_adult = Column(Boolean, default=False)

    # Relationships
    group = relationship("ChannelGroup", back_populates="channels")
    logo = relationship("Logo")
    streams = relationship("Stream", secondary=channel_streams, back_populates="channels")


class M3UAccount(Base):
    __tablename__ = 'm3u_accounts'
    
    id = Column(Integer, primary_key=True, autoincrement=False)
    name = Column(String(255), nullable=False)
    server_url = Column(String(1024), nullable=True)
    file_path = Column(String(1024), nullable=True)
    server_group = Column(String(255), nullable=True)
    max_streams = Column(Integer, default=0)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, nullable=True)
    updated_at = Column(DateTime, nullable=True)
    filters = Column(JSON, nullable=True)
    user_agent = Column(String(512), nullable=True)
    locked = Column(Boolean, default=False)
    refresh_interval = Column(Integer, default=0)
    custom_properties = Column(JSON, nullable=True)
    account_type = Column(String(100), nullable=True)
    username = Column(String(255), nullable=True)
    password = Column(String(255), nullable=True)
    stale_stream_days = Column(Integer, default=0)
    status = Column(String(100), nullable=True)
    last_message = Column(String(1024), nullable=True)
    enable_vod = Column(Boolean, default=False)

    # Relationships
    profiles = relationship("M3UAccountProfile", back_populates="account", cascade="all, delete-orphan")
    groups = relationship("ChannelGroup", secondary=group_accounts, back_populates="accounts")


class M3UAccountProfile(Base):
    __tablename__ = 'm3u_account_profiles'
    
    id = Column(Integer, primary_key=True, autoincrement=False)
    account_id = Column(Integer, ForeignKey('m3u_accounts.id', ondelete='CASCADE'), nullable=False)
    name = Column(String(255), nullable=False)
    max_streams = Column(Integer, default=0)
    is_active = Column(Boolean, default=True)
    is_default = Column(Boolean, default=False)
    current_viewers = Column(Integer, default=0)
    search_pattern = Column(String(512), nullable=True)
    replace_pattern = Column(String(512), nullable=True)
    custom_properties = Column(JSON, nullable=True)

    # Relationships
    account = relationship("M3UAccount", back_populates="profiles")


class Stream(Base):
    __tablename__ = 'streams'
    
    id = Column(Integer, primary_key=True, autoincrement=False)
    name = Column(String(255), nullable=False)
    url = Column(String(1024), nullable=False)
    m3u_account_id = Column(Integer, ForeignKey('m3u_accounts.id', ondelete='SET NULL'), nullable=True)
    logo_url = Column(String(1024), nullable=True)
    tvg_id = Column(String(100), nullable=True)
    local_file = Column(String(1024), nullable=True)
    current_viewers = Column(Integer, default=0)
    updated_at = Column(DateTime, nullable=True)
    last_seen = Column(DateTime, nullable=True)
    stream_profile_id = Column(Integer, nullable=True)
    is_custom = Column(Boolean, default=False)
    channel_group_id = Column(Integer, ForeignKey('channel_groups.id', ondelete='SET NULL'), nullable=True)
    stream_hash = Column(String(255), nullable=True)
    stream_stats = Column(JSON, nullable=True)
    stats_updated_at = Column(DateTime, nullable=True)
    is_stale = Column(Boolean, default=False)
    is_adult = Column(Boolean, default=False)
    provider_stream_id = Column(Integer, nullable=True)
    stream_chno = Column(Float, nullable=True)

    # Relationships
    m3u_account = relationship("M3UAccount")
    channels = relationship("Channel", secondary=channel_streams, back_populates="streams")


# ==========================================
# Automation & Rules Models
# ==========================================

class MatchProfile(Base):
    __tablename__ = 'match_profiles'
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(255), nullable=False)
    description = Column(String(1024), nullable=True)
    enabled = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    steps = relationship("MatchProfileStep", back_populates="profile", cascade="all, delete-orphan")


class MatchProfileStep(Base):
    __tablename__ = 'match_profile_steps'
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    profile_id = Column(Integer, ForeignKey('match_profiles.id', ondelete='CASCADE'), nullable=False)
    type = Column(String(50), nullable=False) # 'regex_name', 'tvg_id', 'regex_url'
    pattern = Column(String(512), nullable=False)
    variables = Column(JSON, nullable=True)
    enabled = Column(Boolean, default=True)
    step_order = Column(Integer, default=0)

    # Relationships
    profile = relationship("MatchProfile", back_populates="steps")


class AutomationProfile(Base):
    __tablename__ = 'automation_profiles'
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(255), nullable=False)
    description = Column(String(1024), nullable=True)
    enabled = Column(Boolean, default=True)
    parallel_checks = Column(Integer, default=1)
    enable_loop_detection = Column(Boolean, default=False)
    extra_settings = Column(JSON, nullable=True)

    # Relationships
    periods = relationship("AutomationPeriod", back_populates="profile", cascade="all, delete-orphan")


class AutomationPeriod(Base):
    __tablename__ = 'automation_periods'
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    profile_id = Column(Integer, ForeignKey('automation_profiles.id', ondelete='CASCADE'), nullable=False)
    cron_schedule = Column(String(100), nullable=False)
    name = Column(String(255), nullable=False)
    enabled = Column(Boolean, default=True)
    channel_regex = Column(String(512), nullable=True)
    exclude_regex = Column(String(512), nullable=True)
    matching_type = Column(String(50), nullable=True)
    automation_type = Column(String(50), nullable=True)
    enable_loop_detection = Column(Boolean, default=False)
    extra_settings = Column(JSON, nullable=True)

    # Relationships
    profile = relationship("AutomationProfile", back_populates="periods")


# ==========================================
# Monitoring & status Models
# ==========================================

class MonitoringSession(Base):
    __tablename__ = 'monitoring_sessions'
    
    # SpeedRun ID or UUID
    session_id = Column(String(100), primary_key=True)
    stream_id = Column(Integer, ForeignKey('streams.id', ondelete='SET NULL'), nullable=True)
    status = Column(String(50), default='stopped')
    start_time = Column(DateTime, default=datetime.utcnow)
    last_update = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    pid = Column(Integer, nullable=True)
    current_speed = Column(Float, default=0.0)
    current_bitrate = Column(Integer, default=0)
    enable_loop_detection = Column(Boolean, default=False)
    raw_info = Column(JSON, nullable=True)


class DeadStream(Base):
    __tablename__ = 'dead_streams'
    
    # URL is unique enough for tracking
    url = Column(String(1024), primary_key=True)
    stream_id = Column(Integer, nullable=True)
    stream_name = Column(String(255), nullable=True)
    channel_id = Column(Integer, ForeignKey('channels.id', ondelete='SET NULL'), nullable=True)
    marked_dead_at = Column(DateTime, default=datetime.utcnow)
    reason = Column(String(255), nullable=True)


class SystemSetting(Base):
    __tablename__ = 'system_settings'
    
    key = Column(String(100), primary_key=True)
    value = Column(JSON, nullable=True)


# ==========================================
# Per-Channel Regex Matching Configuration
# ==========================================

class ChannelRegexConfig(Base):
    """Per-channel regex matching configuration.

    Replaces the ``channel_regex_config`` JSON blob previously stored in
    ``system_settings``.  One row per channel that has patterns configured.
    """
    __tablename__ = 'channel_regex_configs'

    # String type to support both integer IDs (from Dispatcharr) and string
    # identifiers used in tests and future integrations.
    channel_id = Column(String(50), primary_key=True)
    name = Column(String(255), nullable=False, default='')
    enabled = Column(Boolean, default=True, nullable=False)
    match_by_tvg_id = Column(Boolean, default=False, nullable=False)

    # Ordered list of regex patterns for this channel
    patterns = relationship(
        "ChannelRegexPattern",
        back_populates="config",
        cascade="all, delete-orphan",
        order_by="ChannelRegexPattern.step_order",
    )


class ChannelRegexPattern(Base):
    """A single regex pattern belonging to a :class:`ChannelRegexConfig`."""
    __tablename__ = 'channel_regex_patterns'

    id = Column(Integer, primary_key=True, autoincrement=True)
    channel_id = Column(
        String(50),
        ForeignKey('channel_regex_configs.channel_id', ondelete='CASCADE'),
        nullable=False,
        index=True,
    )
    pattern = Column(String(512), nullable=False)
    # NULL → applies to all M3U accounts; list of ints → account-specific
    m3u_accounts = Column(JSON, nullable=True)
    # ``step_order`` stores the physical ordering and doubles as ``priority``
    # so that round-trip import/export preserves the priority value.
    step_order = Column(Integer, default=0, nullable=False)

    config = relationship("ChannelRegexConfig", back_populates="patterns")


# ==========================================
# Telemetry historical reporting Models
# ==========================================

class Run(Base):
    __tablename__ = 'runs'
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    timestamp = Column(DateTime, default=datetime.utcnow, index=True)
    duration_seconds = Column(Float, nullable=False, default=0.0)
    total_channels = Column(Integer, nullable=False, default=0)
    total_streams = Column(Integer, nullable=False, default=0)
    global_dead_count = Column(Integer, nullable=False, default=0)
    global_revived_count = Column(Integer, nullable=False, default=0)
    run_type = Column(String(50), nullable=False, default='automation_run')
    raw_details = Column(String, nullable=True)
    raw_subentries = Column(String, nullable=True)

    channel_healths = relationship("ChannelHealth", back_populates="run", cascade="all, delete-orphan")
    stream_telemetries = relationship("StreamTelemetry", back_populates="run", cascade="all, delete-orphan")


class ChannelHealth(Base):
    __tablename__ = 'channel_health'
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    run_id = Column(Integer, ForeignKey('runs.id', ondelete='CASCADE'), nullable=False, index=True)
    channel_id = Column(Integer, nullable=False, index=True)
    channel_name = Column(String(255), nullable=True)
    offline = Column(Boolean, nullable=False, default=False)
    available_streams = Column(Integer, nullable=False, default=0)
    dead_streams = Column(Integer, nullable=False, default=0)
    
    run = relationship("Run", back_populates="channel_healths")


class StreamTelemetry(Base):
    __tablename__ = 'stream_telemetry'
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    run_id = Column(Integer, ForeignKey('runs.id', ondelete='CASCADE'), nullable=False, index=True)
    channel_id = Column(Integer, nullable=False, index=True)
    provider_id = Column(Integer, nullable=True, index=True)
    stream_id = Column(Integer, nullable=False, index=True)

    bitrate_kbps = Column(Integer, nullable=True)
    resolution_width = Column(Integer, nullable=True)
    resolution_height = Column(Integer, nullable=True)
    fps = Column(Float, nullable=True)
    codec = Column(String(50), nullable=True)
    audio_codec = Column(String(50), nullable=True)
    quality_score = Column(Float, nullable=True)
    is_dead = Column(Boolean, nullable=False, default=False)
    is_hdr = Column(Boolean, nullable=False, default=False)
    
    run = relationship("Run", back_populates="stream_telemetries")


