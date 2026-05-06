"""
Storage layer for the Universal Data Index (UDI) system.

Provides SQL database persistence for cached data with thread-safe operations.
"""

import json
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Any

from apps.core.logging_config import setup_logging

logger = setup_logging(__name__)

# SQLite's compiled SQLITE_MAX_VARIABLE_NUMBER is often 999 on older builds
# and 32766 on modern ones; 100k+ streams exceed both.  Chunk all IN queries
# to stay safely under the lower bound.
_SQLITE_CHUNK = 500


def _query_by_ids(session, model_class, ids: list) -> dict:
    """Query rows by ID list in chunks to avoid SQLite variable limits.

    Returns a {id: row} dict.  Assumes the model has an ``id`` column.
    """
    if not ids:
        return {}
    result = {}
    for i in range(0, len(ids), _SQLITE_CHUNK):
        chunk = ids[i:i + _SQLITE_CHUNK]
        for row in session.query(model_class).filter(model_class.id.in_(chunk)).all():
            result[row.id] = row
    return result


class UDIStorage:
    """SQL-based storage for UDI data with thread-safe operations."""
    
    def __init__(self, storage_dir: Optional[Path] = None):
        """Initialize the UDI storage with DatabaseManager."""
        from apps.database.manager import get_db_manager
        self.db = get_db_manager()
        logger.info("UDI storage initialized with SQL backend Manager.")
    
    def _channel_to_dict(self, c) -> dict:
        if not c: return None
        res = {col.name: getattr(c, col.name) for col in c.__table__.columns}
        res['streams'] = [s.id for s in c.streams]
        return res

    def _stream_to_dict(self, s) -> dict:
        if not s: return None
        res = {col.name: getattr(s, col.name) for col in s.__table__.columns}
        if s.updated_at: res['updated_at'] = s.updated_at.isoformat()
        if s.last_seen: res['last_seen'] = s.last_seen.isoformat()
        if hasattr(s, 'stats_updated_at') and s.stats_updated_at: 
            res['stats_updated_at'] = s.stats_updated_at.isoformat()
        return res

    def _group_to_dict(self, g) -> dict:
        if not g: return None
        return {col.name: getattr(g, col.name) for col in g.__table__.columns}

    def _logo_to_dict(self, l) -> dict:
        if not l: return None
        return {col.name: getattr(l, col.name) for col in l.__table__.columns}

    def _account_to_dict(self, a) -> dict:
        if not a: return None
        res = {col.name: getattr(a, col.name) for col in a.__table__.columns}
        if a.created_at: res['created_at'] = a.created_at.isoformat()
        if a.updated_at: res['updated_at'] = a.updated_at.isoformat()
        return res

    def _profile_to_dict(self, p) -> dict:
        if not p: return None
        return {col.name: getattr(p, col.name) for col in p.__table__.columns}

    # Channels
    def load_channels(self) -> List[Dict[str, Any]]:
        from apps.database.models import Channel
        from apps.database.connection import get_session
        session = get_session()
        try:
            channels = session.query(Channel).all()
            return [self._channel_to_dict(c) for c in channels]
        finally:
            session.close()

    def save_channels(self, channels: List[Dict[str, Any]], skip_metadata: bool = False) -> bool:
        if not channels:
            return True
        from apps.database.models import Channel, Stream
        from apps.database.connection import get_session
        session = get_session()
        try:
            ids = [item.get('id') for item in channels if item.get('id') is not None]
            existing = _query_by_ids(session, Channel, ids)

            # Collect all stream IDs across all channels, then fetch in chunks.
            all_stream_ids = []
            for item in channels:
                all_stream_ids.extend(item.get('streams', []))
            streams_by_id = _query_by_ids(session, Stream, all_stream_ids)

            for item in channels:
                chan_id = item.get('id')
                c = existing.get(chan_id)
                if not c:
                    c = Channel(id=chan_id)
                    session.add(c)
                for k, v in item.items():
                    if k != 'streams' and hasattr(c, k):
                        setattr(c, k, v)
                if 'streams' in item:
                    c.streams = [streams_by_id[sid] for sid in item['streams'] if sid in streams_by_id]
            session.commit()
            if not skip_metadata:
                self._update_metadata('channels_last_updated')
            return True
        except Exception:
            session.rollback()
            return False
        finally:
            session.close()
    
    def get_channel_by_id(self, channel_id: int) -> Optional[Dict[str, Any]]:
        from apps.database.models import Channel
        from apps.database.connection import get_session
        session = get_session()
        try:
            c = session.query(Channel).filter(Channel.id == channel_id).first()
            return self._channel_to_dict(c)
        finally:
            session.close()
    
    def update_channel(self, channel_id: int, channel_data: Dict[str, Any]) -> bool:
        return self.save_channels([channel_data])
    
    # Streams
    def load_streams(self) -> List[Dict[str, Any]]:
        from apps.database.models import Stream
        from apps.database.connection import get_session
        session = get_session()
        try:
            streams = session.query(Stream).all()
            return [self._stream_to_dict(s) for s in streams]
        finally:
            session.close()
    
    def save_streams(self, streams: List[Dict[str, Any]], skip_metadata: bool = False) -> bool:
        if not streams:
            return True
        from apps.database.models import Stream
        from apps.database.connection import get_session
        session = get_session()
        try:
            ids = [item.get('id') for item in streams if item.get('id') is not None]
            existing = _query_by_ids(session, Stream, ids)
            for item in streams:
                sid = item.get('id')
                s = existing.get(sid)
                if not s:
                    s = Stream(id=sid)
                    session.add(s)
                for k, v in item.items():
                    if hasattr(s, k):
                        if k in ['updated_at', 'last_seen', 'stats_updated_at'] and isinstance(v, str):
                            try:
                                v = datetime.fromisoformat(v)
                            except Exception:
                                pass
                        setattr(s, k, v)
            session.commit()
            if not skip_metadata:
                self._update_metadata('streams_last_updated')
            return True
        except Exception:
            session.rollback()
            return False
        finally:
            session.close()
    
    def get_stream_by_id(self, stream_id: int) -> Optional[Dict[str, Any]]:
        from apps.database.models import Stream
        from apps.database.connection import get_session
        session = get_session()
        try:
            s = session.query(Stream).filter(Stream.id == stream_id).first()
            return self._stream_to_dict(s)
        finally:
            session.close()
    
    def update_stream(self, stream_id: int, stream_data: Dict[str, Any]) -> bool:
        return self.save_streams([stream_data])
    
    # Channel Groups
    def load_channel_groups(self) -> List[Dict[str, Any]]:
        from apps.database.models import ChannelGroup
        from apps.database.connection import get_session
        session = get_session()
        try:
            groups = session.query(ChannelGroup).all()
            return [self._group_to_dict(g) for g in groups]
        finally:
            session.close()
    
    def save_channel_groups(self, groups: List[Dict[str, Any]], skip_metadata: bool = False) -> bool:
        if not groups:
            return True
        from apps.database.models import ChannelGroup
        from apps.database.connection import get_session
        session = get_session()
        try:
            ids = [item.get('id') for item in groups if item.get('id') is not None]
            existing = _query_by_ids(session, ChannelGroup, ids)
            for item in groups:
                gid = item.get('id')
                g = existing.get(gid)
                if not g:
                    g = ChannelGroup(id=gid)
                    session.add(g)
                for k, v in item.items():
                    if hasattr(g, k):
                        setattr(g, k, v)
            session.commit()
            if not skip_metadata:
                self._update_metadata('channel_groups_last_updated')
            return True
        except Exception:
            session.rollback()
            return False
        finally:
            session.close()
    
    # Logos
    def load_logos(self) -> List[Dict[str, Any]]:
        from apps.database.models import Logo
        from apps.database.connection import get_session
        session = get_session()
        try:
            logos = session.query(Logo).all()
            return [self._logo_to_dict(l) for l in logos]
        finally:
            session.close()
    
    def save_logos(self, logos: List[Dict[str, Any]], skip_metadata: bool = False) -> bool:
        if not logos:
            return True
        from apps.database.models import Logo
        from apps.database.connection import get_session
        session = get_session()
        try:
            ids = [item.get('id') for item in logos if item.get('id') is not None]
            existing = _query_by_ids(session, Logo, ids)
            for item in logos:
                lid = item.get('id')
                lo = existing.get(lid)
                if not lo:
                    lo = Logo(id=lid)
                    session.add(lo)
                for k, v in item.items():
                    if hasattr(lo, k):
                        setattr(lo, k, v)
            session.commit()
            if not skip_metadata:
                self._update_metadata('logos_last_updated')
            return True
        except Exception:
            session.rollback()
            return False
        finally:
            session.close()
    
    def get_logo_by_id(self, logo_id: int) -> Optional[Dict[str, Any]]:
        from apps.database.models import Logo
        from apps.database.connection import get_session
        session = get_session()
        try:
            l = session.query(Logo).filter(Logo.id == logo_id).first()
            return self._logo_to_dict(l)
        finally:
            session.close()
    
    # M3U Accounts
    def load_m3u_accounts(self) -> List[Dict[str, Any]]:
        from apps.database.models import M3UAccount
        from apps.database.connection import get_session
        session = get_session()
        try:
            accounts = session.query(M3UAccount).all()
            return [self._account_to_dict(a) for a in accounts]
        finally:
            session.close()
    
    def save_m3u_accounts(self, accounts: List[Dict[str, Any]], skip_metadata: bool = False) -> bool:
        if not accounts:
            return True
        from apps.database.models import M3UAccount
        from apps.database.connection import get_session
        session = get_session()
        try:
            ids = [item.get('id') for item in accounts if item.get('id') is not None]
            existing = _query_by_ids(session, M3UAccount, ids)
            for item in accounts:
                aid = item.get('id')
                a = existing.get(aid)
                if not a:
                    a = M3UAccount(id=aid)
                    session.add(a)
                for k, v in item.items():
                    if hasattr(a, k):
                        if k in ['created_at', 'updated_at'] and isinstance(v, str):
                            try:
                                v = datetime.fromisoformat(v)
                            except Exception:
                                pass
                        setattr(a, k, v)
            session.commit()
            if not skip_metadata:
                self._update_metadata('m3u_accounts_last_updated')
            return True
        except Exception:
            session.rollback()
            return False
        finally:
            session.close()
    
    # Channel Profiles
    def load_channel_profiles(self) -> List[Dict[str, Any]]:
        from apps.database.models import M3UAccountProfile
        from apps.database.connection import get_session
        session = get_session()
        try:
            profiles = session.query(M3UAccountProfile).all()
            return [self._profile_to_dict(p) for p in profiles]
        finally:
            session.close()
    
    def save_channel_profiles(self, profiles: List[Dict[str, Any]], skip_metadata: bool = False) -> bool:
        if not profiles:
            return True
        from apps.database.models import M3UAccountProfile
        from apps.database.connection import get_session
        session = get_session()
        try:
            ids = [item.get('id') for item in profiles if item.get('id') is not None]
            existing = _query_by_ids(session, M3UAccountProfile, ids)
            for item in profiles:
                pid = item.get('id')
                p = existing.get(pid)
                if not p:
                    p = M3UAccountProfile(id=pid)
                    session.add(p)
                for k, v in item.items():
                    if hasattr(p, k):
                        setattr(p, k, v)
            session.commit()
            if not skip_metadata:
                self._update_metadata('channel_profiles_last_updated')
            return True
        except Exception:
            session.rollback()
            return False
        finally:
            session.close()
    
    # Profile Channels
    # Each profile is stored as its own SystemSetting row under key
    # 'udi_profile_channels_{profile_id}' rather than a single blob so that
    # per-profile updates are O(1) single-row upserts with no read-modify-write.
    _PROFILE_CHANNELS_PREFIX = 'udi_profile_channels_'

    def load_profile_channels(self) -> Dict[int, Dict[str, Any]]:
        from apps.database.models import SystemSetting
        from apps.database.connection import get_session
        session = get_session()
        try:
            rows = session.query(SystemSetting).filter(
                SystemSetting.key.like(f'{self._PROFILE_CHANNELS_PREFIX}%')
            ).all()
            result = {}
            for row in rows:
                try:
                    profile_id = int(row.key[len(self._PROFILE_CHANNELS_PREFIX):])
                    result[profile_id] = row.value
                except (ValueError, TypeError):
                    continue

            # One-time migration from the old single-blob format.
            if not result:
                old = session.query(SystemSetting).filter(
                    SystemSetting.key == 'udi_profile_channels'
                ).first()
                if old and old.value:
                    for k, v in old.value.items():
                        try:
                            result[int(k)] = v
                        except (ValueError, TypeError):
                            continue
                    # Delete the old blob so this branch never runs again.
                    session.delete(old)
                    session.commit()

            return result
        finally:
            session.close()

    def save_profile_channels(self, profile_channels: Dict[int, Dict[str, Any]], skip_metadata: bool = False) -> bool:
        from apps.database.models import SystemSetting
        from apps.database.connection import get_session
        session = get_session()
        try:
            keys = [f'{self._PROFILE_CHANNELS_PREFIX}{pid}' for pid in profile_channels]
            existing = {
                s.key: s
                for s in session.query(SystemSetting).filter(SystemSetting.key.in_(keys)).all()
            }
            for profile_id, data in profile_channels.items():
                key = f'{self._PROFILE_CHANNELS_PREFIX}{profile_id}'
                s = existing.get(key)
                if not s:
                    s = SystemSetting(key=key)
                    session.add(s)
                s.value = data
            session.commit()
            if not skip_metadata:
                self._update_metadata('profile_channels_last_updated')
            return True
        except Exception:
            session.rollback()
            return False
        finally:
            session.close()

    def load_profile_channels_by_id(self, profile_id: int) -> Optional[Dict[str, Any]]:
        from apps.database.models import SystemSetting
        from apps.database.connection import get_session
        session = get_session()
        try:
            key = f'{self._PROFILE_CHANNELS_PREFIX}{profile_id}'
            s = session.query(SystemSetting).filter(SystemSetting.key == key).first()
            return s.value if s else None
        finally:
            session.close()

    def save_profile_channels_by_id(self, profile_id: int, channels_data: Dict[str, Any]) -> bool:
        from apps.database.models import SystemSetting
        from apps.database.connection import get_session
        session = get_session()
        try:
            key = f'{self._PROFILE_CHANNELS_PREFIX}{profile_id}'
            s = session.query(SystemSetting).filter(SystemSetting.key == key).first()
            if not s:
                s = SystemSetting(key=key)
                session.add(s)
            s.value = channels_data
            session.commit()
            return True
        except Exception:
            session.rollback()
            return False
        finally:
            session.close()
    
    # Metadata
    def load_metadata(self) -> Dict[str, Any]:
        from apps.database.models import SystemSetting
        from apps.database.connection import get_session
        session = get_session()
        try:
            s = session.query(SystemSetting).filter(SystemSetting.key == 'udi_metadata').first()
            return s.value if s else {}
        finally:
            session.close()
    
    def save_metadata(self, metadata: Dict[str, Any]) -> bool:
        from apps.database.models import SystemSetting
        from apps.database.connection import get_session
        session = get_session()
        try:
            s = session.query(SystemSetting).filter(SystemSetting.key == 'udi_metadata').first()
            if not s:
                s = SystemSetting(key='udi_metadata')
                session.add(s)
            s.value = metadata
            session.commit()
            return True
        except Exception:
            session.rollback()
            return False
        finally:
            session.close()
    
    def _update_metadata(self, field: str) -> None:
        metadata = self.load_metadata()
        metadata[field] = datetime.now().isoformat()
        self.save_metadata(metadata)
    
    def get_last_updated(self, entity_type: str) -> Optional[str]:
        metadata = self.load_metadata()
        return metadata.get(f'{entity_type}_last_updated')
    
    # Match Profiles - Reusing MatchProfilesManager
    def load_match_profiles(self) -> List[Dict[str, Any]]:
        try:
            from apps.automation.match_profiles_manager import get_match_profiles_manager
            return get_match_profiles_manager().get_all_profiles()
        except Exception:
            return []

    def save_match_profiles(self, profiles: List[Dict[str, Any]]) -> bool:
        # Handled by MatchProfilesManager
        return True

    def get_match_profile(self, profile_id: int) -> Optional[Dict[str, Any]]:
        try:
            from apps.automation.match_profiles_manager import get_match_profiles_manager
            return get_match_profiles_manager().get_profile(str(profile_id))
        except Exception:
            return None

    def update_match_profile(self, profile_id: int, profile_data: Dict[str, Any]) -> bool:
        try:
            from apps.automation.match_profiles_manager import get_match_profiles_manager
            return get_match_profiles_manager().update_profile(str(profile_id), profile_data)
        except Exception:
            return False

    def delete_match_profile(self, profile_id: int) -> bool:
        try:
            from apps.automation.match_profiles_manager import get_match_profiles_manager
            return get_match_profiles_manager().delete_profile(str(profile_id))
        except Exception:
            return False
    
    def clear_all(self) -> bool:
        # Avoid clearing main databases unless explicitly asked!
        logger.warning("clear_all invoked on SQL storage, doing nothing for safety")
        return True
    
    def is_initialized(self) -> bool:
        from apps.database.models import Channel
        from apps.database.connection import get_session
        session = get_session()
        try:
            return session.query(Channel).count() > 0
        finally:
            session.close()
