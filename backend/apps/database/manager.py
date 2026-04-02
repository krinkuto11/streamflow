
import logging
from typing import List, Dict, Optional, Any, Set, Tuple
from contextlib import contextmanager
from sqlalchemy import asc, desc
from sqlalchemy.orm import Session
from datetime import datetime

from apps.database.connection import get_session
from apps.database.repositories import ChannelStreamRepository
from apps.database.models import (
    Channel, Stream, ChannelGroup, Logo, 
    M3UAccount, M3UAccountProfile,
    channel_streams, group_accounts,
    MatchProfile, MatchProfileStep,
    AutomationProfile, AutomationPeriod,
    MonitoringSession, DeadStream
)

logger = logging.getLogger(__name__)

def _model_to_dict(obj) -> Dict[str, Any]:
    """Helper to convert SQLAlchemy model to dictionary."""
    if not obj:
        return {}
    
    result = {}
    for column in obj.__table__.columns:
        value = getattr(obj, column.name)
        if isinstance(value, datetime):
            value = value.isoformat()
        result[column.name] = value
    return result

class DatabaseManager:
    """
    Data Access Layer (DAL) Manager.
    
    Provides high-level helpers for CRUD operations, acting as a bridge 
    towards full migration of singletons.
    """
    
    def __init__(self, session_factory=None):
        self.session_factory = session_factory or get_session
        self.channel_stream_repository = ChannelStreamRepository()

    def _get_session(self) -> Session:
        return self.session_factory()

    @contextmanager
    def session_scope(self):
        """Provide a transactional scope around operations."""
        session = self._get_session()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    # === Channels ===

    def get_channels(self, as_dict: bool = True) -> List[Any]:
        """Get all channels."""
        with self.session_scope() as session:
            return self.channel_stream_repository.get_channels(session, as_dict=as_dict)

    def get_channel_by_id(self, channel_id: int, as_dict: bool = True) -> Optional[Any]:
        """Get a channel by ID."""
        with self.session_scope() as session:
            return self.channel_stream_repository.get_channel_by_id(session, channel_id, as_dict=as_dict)

    def update_channel(self, channel_id: int, data: Dict[str, Any]) -> bool:
        """Update or insert a channel."""
        try:
            with self.session_scope() as session:
                ch = session.query(Channel).filter(Channel.id == channel_id).first()
                is_new = False
                if not ch:
                    ch = Channel(id=channel_id)
                    session.add(ch)
                    is_new = True

                # Update fields
                for k, v in data.items():
                    if k != 'streams' and hasattr(ch, k):
                        if k in ['updated_at', 'last_seen'] and isinstance(v, str):
                            try:
                                v = datetime.fromisoformat(v)
                            except Exception:
                                pass
                        setattr(ch, k, v)

                # Update stream relationships if present in dict style `streams: [id1, id2]`
                if 'streams' in data:
                    stream_ids = data['streams']
                    streams = session.query(Stream).filter(Stream.id.in_(stream_ids)).all()
                    ch.streams = streams

            logger.debug(f"{'Created' if is_new else 'Updated'} channel {channel_id}")
            return True
        except Exception as e:
            logger.error(f"Error updating channel {channel_id}: {e}")
            return False

    # === Streams ===

    def get_streams(self, as_dict: bool = True) -> List[Any]:
        """Get all streams."""
        with self.session_scope() as session:
            return self.channel_stream_repository.get_streams(session, as_dict=as_dict)

    def get_stream_by_id(self, stream_id: int, as_dict: bool = True) -> Optional[Any]:
        """Get a stream by ID."""
        with self.session_scope() as session:
            return self.channel_stream_repository.get_stream_by_id(session, stream_id, as_dict=as_dict)

    def update_stream(self, stream_id: int, data: Dict[str, Any]) -> bool:
        """Update or insert a stream."""
        try:
            with self.session_scope() as session:
                st = session.query(Stream).filter(Stream.id == stream_id).first()
                is_new = False
                if not st:
                    st = Stream(id=stream_id)
                    session.add(st)
                    is_new = True

                for k, v in data.items():
                    if hasattr(st, k):
                        if k in ['updated_at', 'last_seen', 'stats_updated_at'] and isinstance(v, str):
                            try:
                                v = datetime.fromisoformat(v)
                            except Exception:
                                pass
                        setattr(st, k, v)

            logger.debug(f"{'Created' if is_new else 'Updated'} stream {stream_id}")
            return True
        except Exception as e:
            logger.error(f"Error updating stream {stream_id}: {e}")
            return False

    # === Channel Groups ===

    def get_channel_groups(self, as_dict: bool = True) -> List[Any]:
        session = self._get_session()
        try:
            groups = session.query(ChannelGroup).all()
            if as_dict:
                return [_model_to_dict(g) for g in groups]
            return groups
        finally:
            session.close()

    # === Dead Streams Tracker ===

    def mark_stream_dead(self, url: str, stream_id: int, stream_name: str, channel_id: Optional[int] = None, reason: str = 'offline') -> bool:
        session = self._get_session()
        try:
            dead = session.query(DeadStream).filter(DeadStream.url == url).first()
            if not dead:
                dead = DeadStream(url=url)
                session.add(dead)
            
            dead.stream_id = stream_id
            dead.stream_name = stream_name
            dead.channel_id = channel_id
            dead.reason = reason
            dead.marked_dead_at = datetime.utcnow()
            
            session.commit()
            return True
        except Exception as e:
            session.rollback()
            logger.error(f"Error marking stream dead {url}: {e}")
            return False
        finally:
            session.close()

    def remove_dead_stream(self, url: str) -> bool:
        session = self._get_session()
        try:
            dead = session.query(DeadStream).filter(DeadStream.url == url).first()
            if dead:
                session.delete(dead)
                session.commit()
                return True
            return False
        except Exception as e:
            session.rollback()
            return False
        finally:
            session.close()

    def is_stream_dead(self, url: str) -> bool:
        session = self._get_session()
        try:
            return session.query(DeadStream).filter(DeadStream.url == url).first() is not None
        finally:
            session.close()

    def get_dead_streams(self, as_dict: bool = True) -> List[Any]:
        session = self._get_session()
        try:
            dead = session.query(DeadStream).all()
            if as_dict:
                # For backward compatibility, return a DICT keyed by URL
                return {d.url: _model_to_dict(d) for d in dead}
            return dead
        finally:
            session.close()

    def get_dead_streams_paginated(
        self,
        page: int = 1,
        per_page: int = 20,
        sort_by: str = 'marked_dead_at',
        sort_dir: str = 'desc',
        search: str = '',
    ) -> Dict[str, Any]:
        """Return dead streams with SQL-native pagination, sorting, and optional search.

        Returns a dict with keys: items, total, page, per_page, total_pages,
        has_next, has_prev.
        """
        _VALID_SORT_COLS = {'marked_dead_at', 'stream_name', 'url', 'reason'}
        if sort_by not in _VALID_SORT_COLS:
            sort_by = 'marked_dead_at'

        session = self._get_session()
        try:
            q = session.query(DeadStream)
            if search:
                like = f'%{search}%'
                q = q.filter(
                    DeadStream.stream_name.ilike(like) | DeadStream.url.ilike(like)
                )
            col = getattr(DeadStream, sort_by, DeadStream.marked_dead_at)
            q = q.order_by(desc(col) if sort_dir == 'desc' else asc(col))
            total = q.count()
            offset = (page - 1) * per_page
            end = offset + per_page
            items = q.offset(offset).limit(per_page).all()
            total_pages = max(1, (total + per_page - 1) // per_page)
            return {
                'items': [_model_to_dict(d) for d in items],
                'total': total,
                'page': page,
                'per_page': per_page,
                'total_pages': total_pages,
                'has_next': end < total,
                'has_prev': page > 1,
            }
        finally:
            session.close()

    def get_channels_paginated(
        self,
        page: Optional[int] = None,
        per_page: Optional[int] = None,
        sort_by: str = 'name',
        sort_dir: str = 'asc',
        search: str = '',
    ) -> Dict[str, Any]:
        """Return channels with optional SQL-native pagination, sorting, and search.

        When *page* is None the full list is returned (no pagination envelope),
        which preserves backward compatibility for callers that do not pass page/per_page.
        When *page* is provided a dict with pagination metadata is returned.
        """
        _VALID_SORT_COLS = {'name', 'channel_number', 'id'}
        if sort_by not in _VALID_SORT_COLS:
            sort_by = 'name'

        session = self._get_session()
        try:
            q = session.query(Channel)
            if search:
                like = f'%{search}%'
                q = q.filter(Channel.name.ilike(like))
            col = getattr(Channel, sort_by, Channel.name)
            q = q.order_by(desc(col) if sort_dir == 'desc' else asc(col))

            if page is None:
                channels = q.all()
                result = []
                for ch in channels:
                    d = _model_to_dict(ch)
                    d['streams'] = [st.id for st in ch.streams]
                    result.append(d)
                return result

            total = q.count()
            offset = (page - 1) * per_page
            end = offset + per_page
            channels = q.offset(offset).limit(per_page).all()
            result = []
            for ch in channels:
                d = _model_to_dict(ch)
                d['streams'] = [st.id for st in ch.streams]
                result.append(d)
            total_pages = max(1, (total + per_page - 1) // per_page)
            return {
                'items': result,
                'total': total,
                'page': page,
                'per_page': per_page,
                'total_pages': total_pages,
                'has_next': end < total,
                'has_prev': page > 1,
            }
        finally:
            session.close()

    def count_dead_streams_for_channel(self, channel_id: int) -> int:
        session = self._get_session()
        try:
            return session.query(DeadStream).filter(DeadStream.channel_id == channel_id).count()
        finally:
            session.close()

    def get_dead_streams_for_channel(self, channel_id: int, as_dict: bool = True) -> Any:
        session = self._get_session()
        try:
            dead = session.query(DeadStream).filter(DeadStream.channel_id == channel_id).all()
            if as_dict:
                return {d.url: _model_to_dict(d) for d in dead}
            return dead
        finally:
            session.close()

    def clear_all_dead_streams(self) -> int:
        session = self._get_session()
        try:
            count = session.query(DeadStream).delete()
            session.commit()
            return count
        except Exception as e:
            session.rollback()
            logger.error(f"Error clearing dead streams: {e}")
            return 0
        finally:
            session.close()

    def get_system_setting(self, key: str, default: Any = None) -> Any:
        from apps.database.models import SystemSetting
        session = self._get_session()
        try:
            sett = session.query(SystemSetting).filter(SystemSetting.key == key).first()
            return sett.value if sett else default
        except Exception as e:
            logger.debug(f"Error getting system config {key} (might be expected during startup): {e}")
            return default
        finally:
            session.close()

    def set_system_setting(self, key: str, value: Any):
        from apps.database.models import SystemSetting
        session = self._get_session()
        try:
            sett = session.query(SystemSetting).filter(SystemSetting.key == key).first()
            if sett:
                sett.value = value
            else:
                sett = SystemSetting(key=key, value=value)
                session.add(sett)
            session.commit()
            return True
        except Exception as e:
            session.rollback()
            logger.error(f"Error setting system config {key}: {e}")
            return False
        finally:
            session.close()

    # === Channel Regex Configs ===

    def _regex_config_to_dict(self, cfg) -> Dict[str, Any]:
        """Convert a ChannelRegexConfig + its ChannelRegexPattern rows to a legacy-compat dict."""
        return {
            'name': cfg.name,
            'enabled': cfg.enabled,
            'match_by_tvg_id': cfg.match_by_tvg_id,
            'regex_patterns': [
                {
                    'pattern': p.pattern,
                    'm3u_accounts': p.m3u_accounts,
                    # step_order doubles as priority for round-trip fidelity
                    'priority': p.step_order,
                }
                for p in cfg.patterns
            ],
        }

    def get_channel_regex_config(self, channel_id: int) -> Optional[Dict[str, Any]]:
        """Return the regex config dict for a single channel, or None."""
        from apps.database.models import ChannelRegexConfig
        session = self._get_session()
        try:
            cfg = session.query(ChannelRegexConfig).filter(
                ChannelRegexConfig.channel_id == str(channel_id)
            ).first()
            if cfg is None:
                return None
            # Eagerly load patterns in same session
            _ = cfg.patterns  # touch to load
            return self._regex_config_to_dict(cfg)
        finally:
            session.close()

    def get_all_channel_regex_configs(self) -> Dict[str, Dict[str, Any]]:
        """Return all channel regex configs as a dict keyed by str(channel_id)."""
        from apps.database.models import ChannelRegexConfig
        session = self._get_session()
        try:
            configs = session.query(ChannelRegexConfig).all()
            result = {}
            for cfg in configs:
                _ = cfg.patterns  # eagerly load
                result[str(cfg.channel_id)] = self._regex_config_to_dict(cfg)
            return result
        finally:
            session.close()

    def upsert_channel_regex_config(
        self,
        channel_id: int,
        name: str,
        enabled: bool,
        match_by_tvg_id: bool,
        regex_patterns: List[Dict[str, Any]],
    ) -> bool:
        """Insert or replace the regex config for a channel (atomic)."""
        from apps.database.models import ChannelRegexConfig, ChannelRegexPattern
        session = self._get_session()
        try:
            cfg = session.query(ChannelRegexConfig).filter(
                ChannelRegexConfig.channel_id == str(channel_id)
            ).first()
            if cfg is None:
                cfg = ChannelRegexConfig(
                    channel_id=str(channel_id),
                    name=name,
                    enabled=enabled,
                    match_by_tvg_id=match_by_tvg_id,
                )
                session.add(cfg)
            else:
                cfg.name = name
                cfg.enabled = enabled
                cfg.match_by_tvg_id = match_by_tvg_id
                # Remove old patterns – new ones will be added below
                for p in list(cfg.patterns):
                    session.delete(p)

            session.flush()  # ensure cfg.channel_id is available

            for idx, pat in enumerate(regex_patterns):
                # If the caller supplied an explicit 'priority', use it as
                # step_order for round-trip fidelity (otherwise use idx).
                order = pat.get('priority', idx) if isinstance(pat, dict) else idx
                rp = ChannelRegexPattern(
                    channel_id=str(channel_id),
                    pattern=pat['pattern'],
                    m3u_accounts=pat.get('m3u_accounts'),
                    step_order=order,
                )
                session.add(rp)

            session.commit()
            return True
        except Exception as e:
            session.rollback()
            logger.error(f"Error upserting regex config for channel {channel_id}: {e}")
            return False
        finally:
            session.close()

    def delete_channel_regex_config(self, channel_id: int) -> bool:
        """Delete the regex config (and patterns) for a channel."""
        from apps.database.models import ChannelRegexConfig
        session = self._get_session()
        try:
            cfg = session.query(ChannelRegexConfig).filter(
                ChannelRegexConfig.channel_id == str(channel_id)
            ).first()
            if cfg:
                session.delete(cfg)
                session.commit()
            return True
        except Exception as e:
            session.rollback()
            logger.error(f"Error deleting regex config for channel {channel_id}: {e}")
            return False
        finally:
            session.close()

    def update_channel_regex_tvg_id(self, channel_id: int, match_by_tvg_id: bool) -> bool:
        """Toggle the match_by_tvg_id flag without touching patterns."""
        from apps.database.models import ChannelRegexConfig
        session = self._get_session()
        try:
            cfg = session.query(ChannelRegexConfig).filter(
                ChannelRegexConfig.channel_id == str(channel_id)
            ).first()
            if cfg is None:
                cfg = ChannelRegexConfig(
                    channel_id=str(channel_id),
                    name='',
                    enabled=True,
                    match_by_tvg_id=match_by_tvg_id,
                )
                session.add(cfg)
            else:
                cfg.match_by_tvg_id = match_by_tvg_id
            session.commit()
            return True
        except Exception as e:
            session.rollback()
            logger.error(f"Error updating tvg_id flag for channel {channel_id}: {e}")
            return False
        finally:
            session.close()

    def import_channel_regex_configs_from_json(
        self, data: Dict[str, Any], merge: bool = False
    ) -> Tuple[int, List[str]]:
        """Import channel regex configs from the canonical JSON export format.

        Args:
            data: dict with ``patterns`` key (and optional ``global_settings``).
            merge: If False (default) existing configs are replaced entirely.
                   If True existing configs are preserved for channels not in *data*.

        Returns:
            (imported_count, error_list)
        """
        from apps.database.models import ChannelRegexConfig, ChannelRegexPattern
        errors: List[str] = []
        imported = 0

        patterns_dict = data.get('patterns', {})

        session = self._get_session()
        try:
            if not merge:
                # Wipe all existing configs and their patterns.
                # Must delete ChannelRegexPattern rows explicitly first because
                # SQLAlchemy's bulk query.delete() bypasses ORM-level cascade,
                # and SQLite does not enforce foreign key CASCADE by default
                # (requires PRAGMA foreign_keys = ON per connection).
                # Deleting patterns first prevents orphaned rows that survive
                # the config wipe and cause duplicates on the next import.
                session.query(ChannelRegexPattern).delete()
                session.query(ChannelRegexConfig).delete()
                session.flush()

            for channel_id_str, cfg_data in patterns_dict.items():
                channel_id = str(channel_id_str).strip()
                if not channel_id:
                    errors.append("Empty channel_id – skipped")
                    continue

                if not isinstance(cfg_data, dict):
                    errors.append(f"Channel {channel_id_str}: config must be a dict")
                    continue

                # Normalise patterns from old or new format
                raw = cfg_data.get('regex_patterns') or [
                    {'pattern': p, 'm3u_accounts': cfg_data.get('m3u_accounts')}
                    for p in cfg_data.get('regex', [])
                ]
                norm_patterns = []
                for item in raw:
                    if isinstance(item, str):
                        norm_patterns.append({'pattern': item, 'm3u_accounts': None, 'priority': 0})
                    elif isinstance(item, dict) and item.get('pattern'):
                        norm_patterns.append({
                            'pattern': item['pattern'],
                            'm3u_accounts': item.get('m3u_accounts'),
                            'priority': item.get('priority', 0),
                        })

                if merge:
                    existing = session.query(ChannelRegexConfig).filter(
                        ChannelRegexConfig.channel_id == channel_id
                    ).first()
                    if existing:
                        existing.name = cfg_data.get('name', existing.name)
                        existing.enabled = cfg_data.get('enabled', existing.enabled)
                        existing.match_by_tvg_id = cfg_data.get('match_by_tvg_id', existing.match_by_tvg_id)
                        for p in list(existing.patterns):
                            session.delete(p)
                        session.flush()
                        cfg = existing
                    else:
                        cfg = ChannelRegexConfig(
                            channel_id=channel_id,
                            name=cfg_data.get('name', ''),
                            enabled=cfg_data.get('enabled', True),
                            match_by_tvg_id=cfg_data.get('match_by_tvg_id', False),
                        )
                        session.add(cfg)
                        session.flush()
                else:
                    cfg = ChannelRegexConfig(
                        channel_id=channel_id,
                        name=cfg_data.get('name', ''),
                        enabled=cfg_data.get('enabled', True),
                        match_by_tvg_id=cfg_data.get('match_by_tvg_id', False),
                    )
                    session.add(cfg)
                    session.flush()

                for idx, pat in enumerate(norm_patterns):
                    session.add(ChannelRegexPattern(
                        channel_id=channel_id,
                        pattern=pat['pattern'],
                        m3u_accounts=pat.get('m3u_accounts'),
                        step_order=pat.get('priority', idx),
                    ))

                imported += 1

            session.commit()
        except Exception as e:
            session.rollback()
            logger.error(f"Error importing regex configs: {e}")
            errors.append(str(e))
        finally:
            session.close()

        return imported, errors

    def export_channel_regex_configs_as_json(self) -> Dict[str, Any]:
        """Export all channel regex configs in the canonical JSON format."""
        configs = self.get_all_channel_regex_configs()
        # Also fetch global settings from SystemSetting if present
        global_settings = self.get_system_setting('channel_regex_global_settings', {
            'case_sensitive': True,
            'require_exact_match': False,
        })
        return {
            'patterns': configs,
            'global_settings': global_settings,
        }

    def get_channel_regex_configs_paginated(
        self,
        page: Optional[int] = None,
        per_page: int = 50,
        search: str = '',
        sort_by: str = 'channel_id',
        sort_dir: str = 'asc',
    ) -> Any:
        """Return channel regex configs with optional SQL pagination and search."""
        from apps.database.models import ChannelRegexConfig
        _VALID_SORT_COLS = {'channel_id', 'name', 'enabled'}
        if sort_by not in _VALID_SORT_COLS:
            sort_by = 'channel_id'

        session = self._get_session()
        try:
            q = session.query(ChannelRegexConfig)
            if search:
                q = q.filter(ChannelRegexConfig.name.ilike(f'%{search}%'))
            col = getattr(ChannelRegexConfig, sort_by, ChannelRegexConfig.channel_id)
            q = q.order_by(desc(col) if sort_dir == 'desc' else asc(col))

            if page is None:
                configs = q.all()
                result = {}
                for cfg in configs:
                    _ = cfg.patterns
                    result[str(cfg.channel_id)] = self._regex_config_to_dict(cfg)
                return result

            total = q.count()
            offset = (page - 1) * per_page
            configs = q.offset(offset).limit(per_page).all()
            items = {}
            for cfg in configs:
                _ = cfg.patterns
                items[str(cfg.channel_id)] = self._regex_config_to_dict(cfg)
            total_pages = max(1, (total + per_page - 1) // per_page)
            return {
                'items': items,
                'total': total,
                'page': page,
                'per_page': per_page,
                'total_pages': total_pages,
                'has_next': (offset + per_page) < total,
                'has_prev': page > 1,
            }
        finally:
            session.close()
_db_manager = None

def get_db_manager():
    global _db_manager
    if _db_manager is None:
        _db_manager = DatabaseManager()
    return _db_manager
