"""
Data models for the Universal Data Index (UDI) system.

Defines the structure for:
- Channels: TV channels with stream assignments
- Streams: Video streams from M3U sources
- ChannelGroups: Groups/categories for organizing channels
- Logos: Channel logos/icons
- M3UAccounts: M3U playlist source accounts
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any
from datetime import datetime


@dataclass
class Channel:
    """Represents a TV channel with stream assignments."""
    id: int
    channel_number: Optional[int] = None
    name: str = ""
    channel_group_id: Optional[int] = None
    tvg_id: Optional[str] = None
    epg_data_id: Optional[int] = None
    streams: List[int] = field(default_factory=list)
    stream_profile_id: Optional[int] = None
    uuid: Optional[str] = None
    logo_id: Optional[int] = None
    user_level: Optional[int] = None
    auto_created: bool = False
    auto_created_by: Optional[int] = None
    auto_created_by_name: Optional[str] = None
    tvc_guide_stationid: Optional[str] = None
    # AceStream monitoring fields
    is_acestream: bool = False
    acestream_orchestrator_url: Optional[str] = None
    acestream_config: Optional[Dict[str, Any]] = None
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'Channel':
        """Create a Channel from a dictionary (API response)."""
        return cls(
            id=data.get('id'),
            channel_number=data.get('channel_number'),
            name=data.get('name', ''),
            channel_group_id=data.get('channel_group_id'),
            tvg_id=data.get('tvg_id'),
            epg_data_id=data.get('epg_data_id'),
            streams=data.get('streams', []),
            stream_profile_id=data.get('stream_profile_id'),
            uuid=data.get('uuid'),
            logo_id=data.get('logo_id'),
            user_level=data.get('user_level'),
            auto_created=data.get('auto_created', False),
            auto_created_by=data.get('auto_created_by'),
            auto_created_by_name=data.get('auto_created_by_name'),
            tvc_guide_stationid=data.get('tvc_guide_stationid'),
            is_acestream=data.get('is_acestream', False),
            acestream_orchestrator_url=data.get('acestream_orchestrator_url'),
            acestream_config=data.get('acestream_config')
        )
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            'id': self.id,
            'channel_number': self.channel_number,
            'name': self.name,
            'channel_group_id': self.channel_group_id,
            'tvg_id': self.tvg_id,
            'epg_data_id': self.epg_data_id,
            'streams': self.streams,
            'stream_profile_id': self.stream_profile_id,
            'uuid': self.uuid,
            'logo_id': self.logo_id,
            'user_level': self.user_level,
            'auto_created': self.auto_created,
            'auto_created_by': self.auto_created_by,
            'auto_created_by_name': self.auto_created_by_name,
            'tvc_guide_stationid': self.tvc_guide_stationid,
            'is_acestream': self.is_acestream,
            'acestream_orchestrator_url': self.acestream_orchestrator_url,
            'acestream_config': self.acestream_config
        }


@dataclass
class Stream:
    """Represents a video stream from M3U sources."""
    id: int
    name: str = ""
    url: str = ""
    m3u_account: Optional[int] = None
    logo_url: Optional[str] = None
    tvg_id: Optional[str] = None
    local_file: Optional[str] = None
    current_viewers: int = 0
    updated_at: Optional[str] = None
    last_seen: Optional[str] = None
    stream_profile_id: Optional[int] = None
    is_custom: bool = False
    channel_group: Optional[int] = None
    stream_hash: Optional[str] = None
    stream_stats: Optional[Dict[str, Any]] = None
    stream_stats_updated_at: Optional[str] = None
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'Stream':
        """Create a Stream from a dictionary (API response)."""
        return cls(
            id=data.get('id'),
            name=data.get('name', ''),
            url=data.get('url', ''),
            m3u_account=data.get('m3u_account'),
            logo_url=data.get('logo_url'),
            tvg_id=data.get('tvg_id'),
            local_file=data.get('local_file'),
            current_viewers=data.get('current_viewers', 0),
            updated_at=data.get('updated_at'),
            last_seen=data.get('last_seen'),
            stream_profile_id=data.get('stream_profile_id'),
            is_custom=data.get('is_custom', False),
            channel_group=data.get('channel_group'),
            stream_hash=data.get('stream_hash'),
            stream_stats=data.get('stream_stats'),
            stream_stats_updated_at=data.get('stream_stats_updated_at')
        )
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            'id': self.id,
            'name': self.name,
            'url': self.url,
            'm3u_account': self.m3u_account,
            'logo_url': self.logo_url,
            'tvg_id': self.tvg_id,
            'local_file': self.local_file,
            'current_viewers': self.current_viewers,
            'updated_at': self.updated_at,
            'last_seen': self.last_seen,
            'stream_profile_id': self.stream_profile_id,
            'is_custom': self.is_custom,
            'channel_group': self.channel_group,
            'stream_hash': self.stream_hash,
            'stream_stats': self.stream_stats,
            'stream_stats_updated_at': self.stream_stats_updated_at
        }


@dataclass
class ChannelGroup:
    """Represents a group/category for organizing channels."""
    id: int
    name: str = ""
    channel_count: int = 0
    m3u_account_count: int = 0
    m3u_accounts: List[int] = field(default_factory=list)
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'ChannelGroup':
        """Create a ChannelGroup from a dictionary (API response)."""
        return cls(
            id=data.get('id'),
            name=data.get('name', ''),
            channel_count=data.get('channel_count', 0),
            m3u_account_count=data.get('m3u_account_count', 0),
            m3u_accounts=data.get('m3u_accounts', [])
        )
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            'id': self.id,
            'name': self.name,
            'channel_count': self.channel_count,
            'm3u_account_count': self.m3u_account_count,
            'm3u_accounts': self.m3u_accounts
        }


@dataclass
class Logo:
    """Represents a channel logo/icon."""
    id: int
    name: str = ""
    url: str = ""
    cache_url: Optional[str] = None
    channel_count: int = 0
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'Logo':
        """Create a Logo from a dictionary (API response)."""
        return cls(
            id=data.get('id'),
            name=data.get('name', ''),
            url=data.get('url', ''),
            cache_url=data.get('cache_url'),
            channel_count=data.get('channel_count', 0)
        )
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            'id': self.id,
            'name': self.name,
            'url': self.url,
            'cache_url': self.cache_url,
            'channel_count': self.channel_count
        }


@dataclass
class M3UAccountProfile:
    """Represents a profile within an M3U account (e.g., different provider logins)."""
    id: int
    name: str = ""
    max_streams: int = 0
    is_active: bool = True
    is_default: bool = False
    current_viewers: int = 0
    search_pattern: Optional[str] = None
    replace_pattern: Optional[str] = None
    custom_properties: Optional[Dict[str, Any]] = None
    account: Optional[int] = None  # Parent M3U account ID
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'M3UAccountProfile':
        """Create an M3UAccountProfile from a dictionary (API response)."""
        return cls(
            id=data.get('id'),
            name=data.get('name', ''),
            max_streams=data.get('max_streams', 0),
            is_active=data.get('is_active', True),
            is_default=data.get('is_default', False),
            current_viewers=data.get('current_viewers', 0),
            search_pattern=data.get('search_pattern'),
            replace_pattern=data.get('replace_pattern'),
            custom_properties=data.get('custom_properties'),
            account=data.get('account')
        )
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            'id': self.id,
            'name': self.name,
            'max_streams': self.max_streams,
            'is_active': self.is_active,
            'is_default': self.is_default,
            'current_viewers': self.current_viewers,
            'search_pattern': self.search_pattern,
            'replace_pattern': self.replace_pattern,
            'custom_properties': self.custom_properties,
            'account': self.account
        }


@dataclass
class M3UAccount:
    """Represents an M3U playlist source account."""
    id: int
    name: str = ""
    server_url: Optional[str] = None
    file_path: Optional[str] = None
    server_group: Optional[str] = None
    max_streams: int = 0
    is_active: bool = True
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    filters: Optional[Dict[str, Any]] = None
    user_agent: Optional[str] = None
    profiles: List['M3UAccountProfile'] = field(default_factory=list)
    locked: bool = False
    channel_groups: List[int] = field(default_factory=list)
    refresh_interval: int = 0
    custom_properties: Optional[Dict[str, Any]] = None
    account_type: Optional[str] = None
    username: Optional[str] = None
    password: Optional[str] = None
    stale_stream_days: int = 0
    priority: int = 0
    priority_mode: str = "disabled"  # Options: "disabled", "same_resolution", "all_streams"
    status: Optional[str] = None
    last_message: Optional[str] = None
    enable_vod: bool = False
    auto_enable_new_groups_live: bool = False
    auto_enable_new_groups_vod: bool = False
    auto_enable_new_groups_series: bool = False
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'M3UAccount':
        """Create an M3UAccount from a dictionary (API response)."""
        # Parse profiles if present
        profiles = []
        profiles_data = data.get('profiles', [])
        if profiles_data:
            for profile_data in profiles_data:
                if isinstance(profile_data, dict):
                    # Skip profiles without an ID (invalid data)
                    if profile_data.get('id') is not None:
                        profiles.append(M3UAccountProfile.from_dict(profile_data))
        
        return cls(
            id=data.get('id'),
            name=data.get('name', ''),
            server_url=data.get('server_url'),
            file_path=data.get('file_path'),
            server_group=data.get('server_group'),
            max_streams=data.get('max_streams', 0),
            is_active=data.get('is_active', True),
            created_at=data.get('created_at'),
            updated_at=data.get('updated_at'),
            filters=data.get('filters'),
            user_agent=data.get('user_agent'),
            profiles=profiles,
            locked=data.get('locked', False),
            channel_groups=data.get('channel_groups', []),
            refresh_interval=data.get('refresh_interval', 0),
            custom_properties=data.get('custom_properties'),
            account_type=data.get('account_type'),
            username=data.get('username'),
            password=data.get('password'),
            stale_stream_days=data.get('stale_stream_days', 0),
            priority=data.get('priority', 0),
            priority_mode=data.get('priority_mode', 'disabled'),
            status=data.get('status'),
            last_message=data.get('last_message'),
            enable_vod=data.get('enable_vod', False),
            auto_enable_new_groups_live=data.get('auto_enable_new_groups_live', False),
            auto_enable_new_groups_vod=data.get('auto_enable_new_groups_vod', False),
            auto_enable_new_groups_series=data.get('auto_enable_new_groups_series', False)
        )
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            'id': self.id,
            'name': self.name,
            'server_url': self.server_url,
            'file_path': self.file_path,
            'server_group': self.server_group,
            'max_streams': self.max_streams,
            'is_active': self.is_active,
            'created_at': self.created_at,
            'updated_at': self.updated_at,
            'filters': self.filters,
            'user_agent': self.user_agent,
            'profiles': [p.to_dict() for p in self.profiles] if self.profiles else [],
            'locked': self.locked,
            'channel_groups': self.channel_groups,
            'refresh_interval': self.refresh_interval,
            'custom_properties': self.custom_properties,
            'account_type': self.account_type,
            'username': self.username,
            'password': self.password,
            'stale_stream_days': self.stale_stream_days,
            'priority': self.priority,
            'priority_mode': self.priority_mode,
            'status': self.status,
            'last_message': self.last_message,
            'enable_vod': self.enable_vod,
            'auto_enable_new_groups_live': self.auto_enable_new_groups_live,
            'auto_enable_new_groups_vod': self.auto_enable_new_groups_vod,
            'auto_enable_new_groups_series': self.auto_enable_new_groups_series
        }


@dataclass
class UDIMetadata:
    """Metadata about the UDI cache state."""
    last_full_refresh: Optional[str] = None
    channels_last_updated: Optional[str] = None
    streams_last_updated: Optional[str] = None
    channel_groups_last_updated: Optional[str] = None
    logos_last_updated: Optional[str] = None
    m3u_accounts_last_updated: Optional[str] = None
    version: str = "1.0.0"
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'UDIMetadata':
        """Create UDIMetadata from a dictionary."""
        return cls(
            last_full_refresh=data.get('last_full_refresh'),
            channels_last_updated=data.get('channels_last_updated'),
            streams_last_updated=data.get('streams_last_updated'),
            channel_groups_last_updated=data.get('channel_groups_last_updated'),
            logos_last_updated=data.get('logos_last_updated'),
            m3u_accounts_last_updated=data.get('m3u_accounts_last_updated'),
            version=data.get('version', '1.0.0')
        )
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            'last_full_refresh': self.last_full_refresh,
            'channels_last_updated': self.channels_last_updated,
            'streams_last_updated': self.streams_last_updated,
            'channel_groups_last_updated': self.channel_groups_last_updated,
            'logos_last_updated': self.logos_last_updated,
            'm3u_accounts_last_updated': self.m3u_accounts_last_updated,
            'version': self.version
        }


@dataclass
class ScheduledEvent:
    """Represents a scheduled channel check based on EPG program."""
    id: str
    channel_id: int
    channel_name: str
    program_title: str
    program_start_time: str
    program_end_time: str
    minutes_before: int
    check_time: str
    tvg_id: Optional[str] = None
    created_at: Optional[str] = None
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'ScheduledEvent':
        """Create a ScheduledEvent from a dictionary."""
        return cls(
            id=data.get('id', ''),
            channel_id=data.get('channel_id', 0),
            channel_name=data.get('channel_name', ''),
            program_title=data.get('program_title', ''),
            program_start_time=data.get('program_start_time', ''),
            program_end_time=data.get('program_end_time', ''),
            minutes_before=data.get('minutes_before', 0),
            check_time=data.get('check_time', ''),
            tvg_id=data.get('tvg_id'),
            created_at=data.get('created_at')
        )
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            'id': self.id,
            'channel_id': self.channel_id,
            'channel_name': self.channel_name,
            'program_title': self.program_title,
            'program_start_time': self.program_start_time,
            'program_end_time': self.program_end_time,
            'minutes_before': self.minutes_before,
            'check_time': self.check_time,
            'tvg_id': self.tvg_id,
            'created_at': self.created_at
        }
