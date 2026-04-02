"""
Scheduling Service for StreamFlow.

This service manages scheduled channel checks based on EPG program data.
It fetches EPG data from Dispatcharr, caches it, and manages scheduled events.
"""

import json
import os
import re

def is_dangerous_regex(pattern: str) -> bool:
    """Return True if the regex pattern contains nested quantifiers (ReDoS risk)."""
    inside_parens = False
    has_inner_quantifier = False
    for i, char in enumerate(pattern):
        if i > 0 and pattern[i-1] == '\\':
            continue
        if char == '(':
            inside_parens = True
            has_inner_quantifier = False
        elif char == ')':
            if inside_parens and has_inner_quantifier:
                if i + 1 < len(pattern) and pattern[i+1] in '+*':
                    return True
            inside_parens = False
        elif inside_parens and char in '+*':
            has_inner_quantifier = True
    return False

import uuid
import requests
import threading
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Any

from apps.core.logging_config import setup_logging
from apps.udi import get_udi_manager
from apps.config.dispatcharr_config import get_dispatcharr_config
from apps.core.api_utils import fetch_data_from_url

logger = setup_logging(__name__)

# Configuration
CONFIG_DIR = Path(os.environ.get('CONFIG_DIR', '/app/data'))
SCHEDULING_CONFIG_FILE = CONFIG_DIR / 'scheduling_config.json'
SCHEDULED_EVENTS_FILE = CONFIG_DIR / 'scheduled_events.json'
AUTO_CREATE_RULES_FILE = CONFIG_DIR / 'auto_create_rules.json'
EXECUTED_EVENTS_FILE = CONFIG_DIR / 'executed_events.json'

# Constants
DUPLICATE_DETECTION_WINDOW_SECONDS = 300  # 5 minutes window for detecting duplicate events
EXECUTED_EVENTS_RETENTION_DAYS = 7  # Keep executed events history for 7 days


# ── SCH-002 ────────────────────────────────────────────────────────────────
class NoTvgIdError(Exception):
    """Raised when a channel has no TVG-ID and EPG programs cannot be fetched.

    The test button and rule-matching code catch this to surface a specific
    actionable message rather than silently returning zero results.
    """
    def __init__(self, channel_id: int):
        self.channel_id = channel_id
        super().__init__(
            f"Channel {channel_id} has no TVG-ID configured. "
            f"Set the TVG-ID in Dispatcharr (open channel \u2192 Use EPG TVG-ID) "
            f"to enable EPG program matching."
        )
# ──────────────────────────────────────────────────────────────────────────


class SchedulingService:
    """
    Service for managing EPG-based scheduled channel checks.
    """

    def __init__(self):
        """Initialize the scheduling service."""
        self._lock = threading.Lock()
        self._epg_cache: Dict[str, Dict[str, Any]] = {}
        self._config = self._load_config()
        self._scheduled_events = self._load_scheduled_events()
        self._auto_create_rules = self._load_auto_create_rules()
        self._executed_events = self._load_executed_events()
        self._regex_matcher = None  # Lazy-loaded regex matcher
        logger.info("Scheduling service initialized")

    def _get_regex_matcher(self):
        """Get or create regex matcher instance (singleton pattern)."""
        if self._regex_matcher is None:
            from apps.automation.automated_stream_manager import RegexChannelMatcher
            self._regex_matcher = RegexChannelMatcher()
        return self._regex_matcher

    def _load_config(self) -> Dict[str, Any]:
        """Load scheduling configuration from SQL."""
        default_config = {
            'epg_refresh_interval_minutes': 60,
            'enabled': True
        }
        from apps.database.connection import get_session
        from apps.database.models import SystemSetting
        session = get_session()
        try:
            setting = session.query(SystemSetting).filter(SystemSetting.key == 'scheduling_config').first()
            if setting and setting.value:
                return setting.value
        except Exception as e:
            logger.error(f"Error loading scheduling config: {e}")
        finally:
            session.close()
        return default_config

    def _save_config(self) -> bool:
        from apps.database.connection import get_session
        from apps.database.models import SystemSetting
        session = get_session()
        try:
            setting = session.query(SystemSetting).filter(SystemSetting.key == 'scheduling_config').first()
            if not setting:
                setting = SystemSetting(key='scheduling_config', value=self._config)
                session.add(setting)
            else:
                from sqlalchemy.orm.attributes import flag_modified
                setting.value = self._config
                flag_modified(setting, "value")
            session.commit()
            return True
        except Exception as e:
            session.rollback()
            logger.error(f"Error saving scheduling config: {e}")
            return False
        finally:
            session.close()

    def _load_scheduled_events(self) -> List[Dict[str, Any]]:
        from apps.database.connection import get_session
        from apps.database.models import SystemSetting
        session = get_session()
        try:
            setting = session.query(SystemSetting).filter(SystemSetting.key == 'scheduled_events').first()
            if setting and setting.value:
                return setting.value
        except Exception as e:
            logger.error(f"Error loading scheduled events: {e}")
        finally:
            session.close()
        return []

    def _save_scheduled_events(self) -> bool:
        from apps.database.connection import get_session
        from apps.database.models import SystemSetting
        session = get_session()
        try:
            setting = session.query(SystemSetting).filter(SystemSetting.key == 'scheduled_events').first()
            if not setting:
                setting = SystemSetting(key='scheduled_events', value=self._scheduled_events)
                session.add(setting)
            else:
                from sqlalchemy.orm.attributes import flag_modified
                setting.value = self._scheduled_events
                flag_modified(setting, "value")
            session.commit()
            return True
        except Exception as e:
            session.rollback()
            logger.error(f"Error saving scheduled events: {e}")
            return False
        finally:
            session.close()

    def _load_auto_create_rules(self) -> List[Dict[str, Any]]:
        from apps.database.connection import get_session
        from apps.database.models import SystemSetting
        session = get_session()
        try:
            setting = session.query(SystemSetting).filter(SystemSetting.key == 'auto_create_rules').first()
            if setting and setting.value:
                return setting.value
        except Exception as e:
            logger.error(f"Error loading auto-create rules: {e}")
        finally:
            session.close()
        return []

    def _save_auto_create_rules(self) -> bool:
        from apps.database.connection import get_session
        from apps.database.models import SystemSetting
        session = get_session()
        try:
            setting = session.query(SystemSetting).filter(SystemSetting.key == 'auto_create_rules').first()
            if not setting:
                setting = SystemSetting(key='auto_create_rules', value=self._auto_create_rules)
                session.add(setting)
            else:
                from sqlalchemy.orm.attributes import flag_modified
                setting.value = self._auto_create_rules
                flag_modified(setting, "value")
            session.commit()
            return True
        except Exception as e:
            session.rollback()
            logger.error(f"Error saving auto-create rules: {e}")
            return False
        finally:
            session.close()

    def _load_executed_events(self) -> List[Dict[str, Any]]:
        from apps.database.connection import get_session
        from apps.database.models import SystemSetting
        session = get_session()
        try:
            setting = session.query(SystemSetting).filter(SystemSetting.key == 'executed_events').first()
            if setting and setting.value:
                return setting.value
        except Exception as e:
            logger.error(f"Error loading executed events: {e}")
        finally:
            session.close()
        return []

    def _save_executed_events(self) -> bool:
        from apps.database.connection import get_session
        from apps.database.models import SystemSetting
        session = get_session()
        try:
            setting = session.query(SystemSetting).filter(SystemSetting.key == 'executed_events').first()
            if not setting:
                setting = SystemSetting(key='executed_events', value=self._executed_events)
                session.add(setting)
            else:
                from sqlalchemy.orm.attributes import flag_modified
                setting.value = self._executed_events
                flag_modified(setting, "value")
            session.commit()
            return True
        except Exception as e:
            session.rollback()
            logger.error(f"Error saving executed events: {e}")
            return False
        finally:
            session.close()

    def get_config(self) -> Dict[str, Any]:
        return self._config.copy()

    def update_config(self, config: Dict[str, Any]) -> bool:
        with self._lock:
            self._config.update(config)
            return self._save_config()

    def _get_base_url(self) -> Optional[str]:
        config = get_dispatcharr_config()
        return config.get_base_url()

    def _get_auth_token(self) -> Optional[str]:
        return os.getenv("DISPATCHARR_TOKEN")

    def fetch_channel_programs_from_api(self, tvg_id: str, hours_ahead: int = 24, force_refresh: bool = False) -> List[Dict[str, Any]]:
        """Fetch EPG programs for a specific channel from Dispatcharr API.

        Args:
            tvg_id: TVG ID of the channel
            hours_ahead: Number of hours ahead to include (enforced client-side)
            force_refresh: If True, bypass cache and fetch fresh data

        Returns:
            List of program dictionaries

        Notes (SCH-001):
            The Dispatcharr /api/epg/programs/ endpoint accepts tvg_id as a query
            filter but silently ignores start_time__gte / start_time__lte parameters.
            The hours_ahead window is therefore enforced client-side after parsing
            the response. The start_time__gte hint is still sent as a best-effort
            performance hint in case a future Dispatcharr version honours it.

            The tvg_id post-filter is applied only when programs actually carry a
            tvg_id field in the response. When the field is absent (Dispatcharr does
            not always echo it back) we trust the API filtered by tvg_id correctly.
        """
        if not tvg_id:
            return []

        # Check cache
        with self._lock:
            cached_data = self._epg_cache.get(tvg_id)
            if not force_refresh and cached_data:
                cache_age = datetime.now() - cached_data['time']
                refresh_interval = timedelta(minutes=self._config.get('epg_refresh_interval_minutes', 60))
                if cache_age < refresh_interval:
                    return cached_data['programs'].copy()

        # Fetch fresh data
        base_url = self._get_base_url()
        if not base_url:
            logger.error("Missing Dispatcharr configuration (base_url)")
            return []

        try:
            now = datetime.now(timezone.utc)
            start_time = (now - timedelta(hours=1)).isoformat()
            end_time = now + timedelta(hours=hours_ahead)

            # Send start_time__gte as a hint; end-time filter is enforced below
            # because the API ignores start_time__lte (SCH-001).
            url = f"{base_url}/api/epg/programs/?tvg_id={tvg_id}&start_time__gte={start_time}"
            logger.debug(f"Fetching EPG programs for TVG ID {tvg_id}")

            data = fetch_data_from_url(url)
            if data is None:
                logger.error(f"Failed to fetch EPG programs for TVG ID {tvg_id}")
                return []

            programs = []
            if isinstance(data, list):
                programs = data
            elif isinstance(data, dict):
                programs = data.get('results', data.get('data', data.get('programs', [])))
                if not isinstance(programs, list):
                    programs = []

            # ── SCH-001 client-side filtering ────────────────────────────
            # Only apply tvg_id cross-channel guard when programs actually carry
            # the field — Dispatcharr does not always echo it back.
            any_have_tvg_id = any(
                isinstance(p, dict) and p.get('tvg_id')
                for p in programs
            )

            valid_programs = []
            for p in programs:
                if not isinstance(p, dict):
                    continue
                # Optional cross-channel guard
                if any_have_tvg_id and p.get('tvg_id') and p.get('tvg_id') != tvg_id:
                    continue
                # Enforce hours_ahead window
                p_start = p.get('start_time')
                if p_start:
                    try:
                        p_start_dt = datetime.fromisoformat(p_start.replace('Z', '+00:00'))
                        if p_start_dt.tzinfo is None:
                            p_start_dt = p_start_dt.replace(tzinfo=timezone.utc)
                        if p_start_dt > end_time:
                            continue
                    except (ValueError, AttributeError):
                        pass  # Unparseable time — include and let caller decide
                valid_programs.append(p)

            logger.debug(
                f"Fetched {len(programs)} programs for tvg_id={tvg_id}, "
                f"kept {len(valid_programs)} within {hours_ahead}h window"
            )
            # ─────────────────────────────────────────────────────────────

            # Update cache
            with self._lock:
                self._epg_cache[tvg_id] = {
                    'time': datetime.now(),
                    'programs': valid_programs
                }

            return valid_programs.copy()

        except Exception as e:
            logger.error(f"Error fetching EPG programs for {tvg_id}: {e}")
            with self._lock:
                cached_data = self._epg_cache.get(tvg_id)
                if cached_data:
                    return cached_data['programs'].copy()
            return []

    def fetch_epg_grid(self, force_refresh: bool = False) -> List[Dict[str, Any]]:
        """Fetch EPG grid data from Dispatcharr API and trigger matching.

        Note: This is now a wrapper around match_programs_to_rules to maintain
        compatibility with legacy code and tests.
        """
        logger.info("Triggering EPG refresh and rule matching")
        self.match_programs_to_rules(force_refresh=force_refresh)
        return []

    def get_programs_by_channel(self, channel_id: int, tvg_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """Get programs for a specific channel from Dispatcharr API.

        Args:
            channel_id: Channel ID
            tvg_id: Optional TVG ID override

        Returns:
            List of program dictionaries

        Raises:
            NoTvgIdError: When the channel has no TVG-ID configured (SCH-002).
                          Callers that want a user-facing message should catch this
                          and surface it explicitly rather than treating it as zero
                          results.
        """
        if not tvg_id:
            udi = get_udi_manager()
            channel = udi.get_channel_by_id(channel_id)
            if channel:
                tvg_id = channel.get('tvg_id')

        if not tvg_id:
            # ── SCH-002 ──────────────────────────────────────────────────
            # Raise a typed error so the API handler can return a structured
            # response with a specific message instead of {matches: 0}.
            logger.warning(f"No TVG ID found for channel {channel_id}")
            raise NoTvgIdError(channel_id)
            # ─────────────────────────────────────────────────────────────

        channel_programs = self.fetch_channel_programs_from_api(tvg_id)
        channel_programs.sort(key=lambda p: p.get('start_time', ''))
        logger.debug(f"Found {len(channel_programs)} programs for channel {channel_id} (tvg_id: {tvg_id})")
        return channel_programs

    def get_scheduled_events(self) -> List[Dict[str, Any]]:
        """Get all scheduled events sorted by check_time (earliest first)."""
        events = self._scheduled_events.copy()

        def get_check_time(event):
            check_time = event.get('check_time', '')
            try:
                return datetime.fromisoformat(check_time.replace('Z', '+00:00'))
            except (ValueError, AttributeError):
                return datetime.max.replace(tzinfo=timezone.utc)

        events.sort(key=get_check_time)
        return events

    def get_due_events(self) -> List[Dict[str, Any]]:
        """Get all events that are due for execution."""
        now = datetime.now(timezone.utc)
        due_events = []
        for event in self._scheduled_events:
            try:
                check_time = datetime.fromisoformat(event['check_time'].replace('Z', '+00:00'))
                if check_time.tzinfo is None:
                    check_time = check_time.replace(tzinfo=timezone.utc)
                if check_time <= now:
                    due_events.append(event)
            except (ValueError, KeyError) as e:
                logger.warning(f"Invalid check_time for event {event.get('id')}: {e}")
        return due_events

    def create_scheduled_event(self, event_data: Dict[str, Any]) -> Dict[str, Any]:
        """Create a new scheduled event."""
        with self._lock:
            event_id = str(uuid.uuid4())

            # Get channel info
            udi = get_udi_manager()
            channel = udi.get_channel_by_id(event_data['channel_id'])
            if not channel:
                raise ValueError(f"Channel {event_data['channel_id']} not found")

            # Calculate check time
            program_start = datetime.fromisoformat(
                event_data['program_start_time'].replace('Z', '+00:00')
            )
            minutes_before = event_data.get('minutes_before', 0)
            check_time = program_start - timedelta(minutes=minutes_before)

            # Ensure check_time is timezone-aware
            if check_time.tzinfo is None:
                logger.warning(f"check_time is missing timezone info, assuming UTC: {check_time}")
                check_time = check_time.replace(tzinfo=timezone.utc)

            # Get channel logo info
            logo_id = channel.get('logo_id')
            logo_url = None
            if logo_id:
                logo_url = f"/api/logos/{logo_id}"

            # Get schedule type (default to 'check' for backward compatibility)
            schedule_type = event_data.get('schedule_type', 'check')
            if schedule_type not in ['check', 'monitoring']:
                schedule_type = 'check'

            event = {
                'id': event_id,
                'channel_id': event_data['channel_id'],
                'channel_name': channel.get('name', ''),
                'channel_logo_url': logo_url,
                'program_title': event_data['program_title'],
                'program_start_time': event_data['program_start_time'],
                'program_end_time': event_data['program_end_time'],
                'minutes_before': minutes_before,
                'check_time': check_time.isoformat(),
                'tvg_id': channel.get('tvg_id'),
                'schedule_type': schedule_type,
                'session_type': event_data.get('session_type', 'standard'),
                'interval_s': event_data.get('interval_s', 1.0),
                'run_seconds': event_data.get('run_seconds', 0),
                'per_sample_timeout_s': event_data.get('per_sample_timeout_s', 1.0),
                'engine_container_id': event_data.get('engine_container_id'),
                'enable_looping_detection': event_data.get('enable_looping_detection', True),
                'enable_logo_detection': event_data.get('enable_logo_detection', True),
                'created_at': datetime.now(timezone.utc).isoformat()
            }

            self._scheduled_events.append(event)
            self._save_scheduled_events()

            logger.info(
                f"Created scheduled event {event_id} ({schedule_type}) "
                f"for channel {channel.get('name')} at {check_time}"
            )
            return event

    def delete_scheduled_event(self, event_id: str) -> bool:
        """Delete a scheduled event."""
        with self._lock:
            initial_count = len(self._scheduled_events)
            self._scheduled_events = [e for e in self._scheduled_events if e.get('id') != event_id]
            if len(self._scheduled_events) < initial_count:
                self._save_scheduled_events()
                logger.info(f"Deleted scheduled event {event_id}")
                return True
            logger.warning(f"Scheduled event {event_id} not found")
            return False

    def _is_event_executed(self, channel_id: int, program_start_time: str) -> bool:
        """Check if an event has already been executed."""
        channel_id_str = str(channel_id)
        for executed in self._executed_events:
            if (str(executed.get('channel_id')) == channel_id_str and
                    executed.get('program_start_time') == program_start_time):
                return True
        return False

    def _record_executed_event(self, channel_id: int, program_start_time: str) -> None:
        """Record an event as executed to prevent re-creation."""
        self._executed_events.append({
            'channel_id': channel_id,
            'program_start_time': program_start_time,
            'executed_at': datetime.now(timezone.utc).isoformat(),
        })
        # Prune old entries
        cutoff = datetime.now(timezone.utc) - timedelta(days=EXECUTED_EVENTS_RETENTION_DAYS)
        self._executed_events = [
            e for e in self._executed_events
            if datetime.fromisoformat(
                e.get('executed_at', '2000-01-01').replace('Z', '+00:00')
            ) > cutoff
        ]
        self._save_executed_events()

    def get_auto_create_rules(self) -> List[Dict[str, Any]]:
        """Get all auto-create rules."""
        return self._auto_create_rules.copy()

    def create_auto_create_rule(self, rule_data: Dict[str, Any]) -> Dict[str, Any]:
        """Create a new auto-create rule."""
        with self._lock:
            rule_id = str(uuid.uuid4())
            udi = get_udi_manager()

            channel_ids = []
            channel_group_ids = []

            if 'channel_id' in rule_data and 'channel_ids' not in rule_data and 'channel_group_ids' not in rule_data:
                channel_ids = [rule_data['channel_id']]
            else:
                if 'channel_ids' in rule_data:
                    channel_ids = list(rule_data['channel_ids'])
                if 'channel_group_ids' in rule_data:
                    channel_group_ids = list(rule_data['channel_group_ids'])

            if not channel_ids and not channel_group_ids:
                raise ValueError("Missing required field: channel_id, channel_ids, or channel_group_ids")

            # Expand channel groups
            channel_groups_info = []
            for group_id in channel_group_ids:
                group = udi.get_channel_group_by_id(group_id)
                if group:
                    group_channels = udi.get_channels_by_group(group_id) or []
                    channel_groups_info.append({
                        'id': group_id,
                        'name': group.get('name', ''),
                        'channel_count': len(group_channels),
                    })
                    for ch in group_channels:
                        if ch.get('id') not in channel_ids:
                            channel_ids.append(ch.get('id'))

            # Validate channels
            channels_info = []
            for channel_id in channel_ids:
                channel = udi.get_channel_by_id(channel_id)
                if not channel:
                    continue
                logo_url = None
                logo_id = channel.get('logo_id')
                if logo_id:
                    logo_url = f"/api/logos/{logo_id}"
                channels_info.append({
                    'id': channel_id,
                    'name': channel.get('name', ''),
                    'logo_url': logo_url,
                    'tvg_id': channel.get('tvg_id'),
                })

            if not channels_info:
                raise ValueError("No valid channels found for this rule")

            # Validate regex
            try:
                validation_pattern = rule_data['regex_pattern'].replace('CHANNEL_NAME', 'PLACEHOLDER')
                if is_dangerous_regex(validation_pattern):
                    raise ValueError("Regex pattern contains dangerous nested quantifiers (ReDoS risk)")
                re.compile(validation_pattern)
            except re.error as e:
                raise ValueError(f"Invalid regex pattern: {e}")

            schedule_type = rule_data.get('schedule_type', 'check')
            if schedule_type not in ['check', 'monitoring']:
                schedule_type = 'check'

            rule = {
                'id': rule_id,
                'name': rule_data['name'],
                'channel_ids': channel_ids,
                'channel_group_ids': channel_group_ids,
                'channel_groups_info': channel_groups_info,
                'channels_info': channels_info,
                'regex_pattern': rule_data['regex_pattern'],
                'minutes_before': rule_data.get('minutes_before', 5),
                'schedule_type': schedule_type,
                'session_type': rule_data.get('session_type', 'standard'),
                'interval_s': rule_data.get('interval_s', 1.0),
                'run_seconds': rule_data.get('run_seconds', 0),
                'per_sample_timeout_s': rule_data.get('per_sample_timeout_s', 1.0),
                'engine_container_id': rule_data.get('engine_container_id', ''),
                'enable_looping_detection': rule_data.get('enable_looping_detection', True),
                'enable_logo_detection': rule_data.get('enable_logo_detection', True),
                'created_at': datetime.now(timezone.utc).isoformat(),
            }

            # Backward compat
            if channels_info:
                rule['channel_id'] = channels_info[0]['id']
                rule['channel_name'] = channels_info[0]['name']
                rule['tvg_id'] = channels_info[0].get('tvg_id')

            self._auto_create_rules.append(rule)
            if not self._save_auto_create_rules():
                raise IOError("Failed to save auto-create rule to disk")

            logger.info(f"Created auto-create rule {rule_id}: {rule_data['name']}")
            return rule

    def delete_auto_create_rule(self, rule_id: str) -> bool:
        """Delete an auto-create rule."""
        with self._lock:
            initial_count = len(self._auto_create_rules)
            self._auto_create_rules = [r for r in self._auto_create_rules if r.get('id') != rule_id]
            if len(self._auto_create_rules) < initial_count:
                self._save_auto_create_rules()
                self._scheduled_events = [
                    e for e in self._scheduled_events
                    if e.get('auto_create_rule_id') != rule_id
                ]
                self._save_scheduled_events()
                logger.info(f"Deleted auto-create rule {rule_id}")
                return True
            return False

    def update_auto_create_rule(self, rule_id: str, rule_data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Update an existing auto-create rule."""
        with self._lock:
            rule_index = next(
                (i for i, r in enumerate(self._auto_create_rules) if r.get('id') == rule_id),
                None
            )
            if rule_index is None:
                return None

            rule = self._auto_create_rules[rule_index].copy()

            if 'channel_ids' in rule_data or 'channel_id' in rule_data or 'channel_group_ids' in rule_data:
                udi = get_udi_manager()
                channel_ids = []
                channel_group_ids = list(rule_data.get('channel_group_ids', []))

                if 'channel_ids' in rule_data:
                    channel_ids = list(rule_data['channel_ids'])
                elif 'channel_id' in rule_data:
                    channel_ids = [rule_data['channel_id']]

                channel_groups_info = []
                for group_id in channel_group_ids:
                    group = udi.get_channel_group_by_id(group_id)
                    if group:
                        group_channels = udi.get_channels_by_group(group_id) or []
                        channel_groups_info.append({
                            'id': group_id,
                            'name': group.get('name', ''),
                            'channel_count': len(group_channels),
                        })
                        for ch in group_channels:
                            if ch.get('id') not in channel_ids:
                                channel_ids.append(ch.get('id'))

                channels_info = []
                for cid in channel_ids:
                    channel = udi.get_channel_by_id(cid)
                    if channel:
                        logo_url = f"/api/logos/{channel['logo_id']}" if channel.get('logo_id') else None
                        channels_info.append({
                            'id': cid,
                            'name': channel.get('name', ''),
                            'logo_url': logo_url,
                            'tvg_id': channel.get('tvg_id'),
                        })

                rule['channel_ids'] = channel_ids
                rule['channel_group_ids'] = channel_group_ids
                rule['channel_groups_info'] = channel_groups_info
                rule['channels_info'] = channels_info

                if channels_info:
                    rule['channel_id'] = channels_info[0]['id']
                    rule['channel_name'] = channels_info[0]['name']
                    rule['tvg_id'] = channels_info[0].get('tvg_id')

            if 'regex_pattern' in rule_data:
                try:
                    validation_pattern = rule_data['regex_pattern'].replace('CHANNEL_NAME', 'PLACEHOLDER')
                    if is_dangerous_regex(validation_pattern):
                        raise ValueError("Regex pattern contains dangerous nested quantifiers (ReDoS risk)")
                    re.compile(validation_pattern)
                    rule['regex_pattern'] = rule_data['regex_pattern']
                except re.error as e:
                    raise ValueError(f"Invalid regex pattern: {e}")

            for field in ['name', 'minutes_before', 'session_type', 'interval_s',
                          'run_seconds', 'per_sample_timeout_s', 'engine_container_id',
                          'enable_looping_detection', 'enable_logo_detection']:
                if field in rule_data:
                    rule[field] = rule_data[field]

            self._auto_create_rules[rule_index] = rule
            if not self._save_auto_create_rules():
                raise IOError("Failed to save updated auto-create rule to disk")

            logger.info(f"Updated auto-create rule {rule_id}")

            initial_count = len(self._scheduled_events)
            self._scheduled_events = [
                e for e in self._scheduled_events
                if e.get('auto_create_rule_id') != rule_id
            ]
            if len(self._scheduled_events) < initial_count:
                self._save_scheduled_events()

            def match_in_background():
                try:
                    self.fetch_epg_grid()
                except Exception as e:
                    logger.error(f"Error matching programs to updated rule: {e}", exc_info=True)

            threading.Thread(target=match_in_background, daemon=True).start()
            return rule

    def test_regex_against_epg(self, channel_id: int, regex_pattern: str) -> List[Dict[str, Any]]:
        """Test a regex pattern against EPG programs for a channel.

        Raises:
            NoTvgIdError: propagated from get_programs_by_channel when channel
                          has no TVG-ID (SCH-002).
            ValueError: for dangerous or syntactically invalid patterns.
        """
        try:
            validation_pattern = regex_pattern.replace('CHANNEL_NAME', 'PLACEHOLDER')
            if is_dangerous_regex(validation_pattern):
                raise ValueError("Regex pattern contains dangerous nested quantifiers (ReDoS risk)")
            re.compile(validation_pattern, re.IGNORECASE)
        except re.error as e:
            raise ValueError(f"Invalid regex pattern: {e}")

        pattern = re.compile(regex_pattern, re.IGNORECASE)  # lgtm [py/regex-injection]

        # NoTvgIdError intentionally not caught here — let it propagate to the handler
        programs = self.get_programs_by_channel(channel_id)

        matching_programs = []
        for program in programs:
            title = program.get('title', '')
            if pattern.search(title):
                matching_programs.append(program)

        logger.debug(f"Regex '{regex_pattern}' matched {len(matching_programs)} programs for channel {channel_id}")
        return matching_programs

    def match_programs_to_rules(self, force_refresh: bool = False) -> Dict[str, Any]:
        """Match EPG programs to auto-create rules and create/update scheduled events."""
        with self._lock:
            if not self._auto_create_rules:
                logger.debug("No auto-create rules to process")
                return {'created': 0, 'updated': 0, 'skipped': 0}
            rules_snapshot = self._auto_create_rules.copy()

        created_count = 0
        updated_count = 0
        skipped_count = 0

        udi = get_udi_manager()
        events_to_add = []
        programs_by_tvg_id = {}

        for rule in rules_snapshot:
            channel_ids = rule.get('channel_ids') or ([rule.get('channel_id')] if rule.get('channel_id') else [])
            channel_group_ids = rule.get('channel_group_ids', [])
            regex_pattern = rule.get('regex_pattern')
            minutes_before = rule.get('minutes_before', 5)

            all_channel_ids = set(channel_ids)

            # Dynamically expand channel groups
            for group_id in channel_group_ids:
                group_channels = udi.get_channels_by_group(group_id) or []
                for ch in group_channels:
                    all_channel_ids.add(ch.get('id'))

            channels_info = rule.get('channels_info', [])
            if not channels_info:
                channels_info = []
                for cid in all_channel_ids:
                    channel = udi.get_channel_by_id(cid)
                    if channel:
                        channels_info.append({
                            'id': cid,
                            'name': channel.get('name', ''),
                            'tvg_id': channel.get('tvg_id'),
                        })

            if not regex_pattern:
                continue

            try:
                validation_pattern = regex_pattern.replace('CHANNEL_NAME', 'PLACEHOLDER')
                re.compile(validation_pattern, re.IGNORECASE)
            except re.error as e:
                logger.error(f"Invalid regex pattern in rule {rule.get('id')}: {e}")
                continue

            pattern = re.compile(regex_pattern, re.IGNORECASE)  # lgtm [py/regex-injection]

            for channel_info in channels_info:
                channel_id = channel_info.get('id')
                tvg_id = channel_info.get('tvg_id')

                if not tvg_id:
                    logger.warning(f"Rule {rule.get('id')} channel {channel_id} has no TVG ID, skipping")
                    continue

                if tvg_id not in programs_by_tvg_id:
                    fetched = self.fetch_channel_programs_from_api(tvg_id, force_refresh=force_refresh)
                    fetched.sort(key=lambda p: p.get('start_time', ''))
                    programs_by_tvg_id[tvg_id] = fetched

                programs = programs_by_tvg_id.get(tvg_id, [])

                # Get channel logo for events created by rule matching
                channel = udi.get_channel_by_id(channel_id)
                logo_id = channel.get('logo_id') if channel else None
                logo_url = f"/api/logos/{logo_id}" if logo_id else None

                for program in programs:
                    title = program.get('title', '')
                    if not pattern.search(title):
                        continue

                    program_start = program.get('start_time')
                    program_end = program.get('end_time')

                    if not program_start or not program_end:
                        continue

                    try:
                        start_dt = datetime.fromisoformat(program_start.replace('Z', '+00:00'))
                        end_dt = datetime.fromisoformat(program_end.replace('Z', '+00:00'))
                    except (ValueError, AttributeError) as e:
                        logger.warning(f"Invalid program times for {title}: {e}")
                        continue

                    if start_dt.tzinfo is None:
                        start_dt = start_dt.replace(tzinfo=timezone.utc)
                    if end_dt.tzinfo is None:
                        end_dt = end_dt.replace(tzinfo=timezone.utc)

                    now = datetime.now(timezone.utc)
                    if start_dt <= now:
                        continue

                    if self._is_event_executed(channel_id, program_start):
                        continue

                    check_time = start_dt - timedelta(minutes=minutes_before)
                    program_date = start_dt.date().isoformat()

                    # Duplicate detection
                    with self._lock:
                        existing_event = None
                        for event in self._scheduled_events:
                            if event.get('channel_id') != channel_id:
                                continue
                            existing_start = event.get('program_start_time')
                            if not existing_start:
                                continue
                            try:
                                existing_dt = datetime.fromisoformat(existing_start.replace('Z', '+00:00'))
                                if existing_dt.tzinfo is None:
                                    existing_dt = existing_dt.replace(tzinfo=timezone.utc)
                                diff = abs((start_dt - existing_dt).total_seconds())
                                if diff <= DUPLICATE_DETECTION_WINDOW_SECONDS:
                                    existing_event = event
                                    break
                            except (ValueError, AttributeError):
                                continue

                    if existing_event:
                        if (existing_event.get('program_title') != title or
                                existing_event.get('program_start_time') != program_start):
                            with self._lock:
                                for event in self._scheduled_events:
                                    if event.get('id') == existing_event.get('id'):
                                        event['program_title'] = title
                                        event['program_start_time'] = program_start
                                        event['program_end_time'] = program_end
                                        event['check_time'] = check_time.isoformat()
                                        break
                                self._save_scheduled_events()
                            updated_count += 1
                        else:
                            skipped_count += 1
                        continue

                    channel_name = channel_info.get('name', f'Channel {channel_id}')
                    schedule_type = rule.get('schedule_type', 'check')

                    new_event = {
                        'id': str(uuid.uuid4()),
                        'channel_id': channel_id,
                        'channel_name': channel_name,
                        'channel_logo_url': logo_url,
                        'program_title': title,
                        'program_start_time': program_start,
                        'program_end_time': program_end,
                        'minutes_before': minutes_before,
                        'check_time': check_time.isoformat(),
                        'tvg_id': tvg_id,
                        'schedule_type': schedule_type,
                        'session_type': rule.get('session_type', 'standard'),
                        'interval_s': rule.get('interval_s', 1.0),
                        'run_seconds': rule.get('run_seconds', 0),
                        'per_sample_timeout_s': rule.get('per_sample_timeout_s', 1.0),
                        'engine_container_id': rule.get('engine_container_id'),
                        'enable_looping_detection': rule.get('enable_looping_detection', True),
                        'enable_logo_detection': rule.get('enable_logo_detection', True),
                        'created_at': datetime.now(timezone.utc).isoformat(),
                        'auto_created': True,
                        'auto_create_rule_id': rule.get('id'),
                        'program_date': program_date,
                    }
                    events_to_add.append(new_event)
                    created_count += 1

        if events_to_add:
            with self._lock:
                self._scheduled_events.extend(events_to_add)
                self._save_scheduled_events()

        logger.info(
            f"Rule matching complete: {created_count} created, "
            f"{updated_count} updated, {skipped_count} skipped"
        )
        return {'created': created_count, 'updated': updated_count, 'skipped': skipped_count}

    def execute_scheduled_check(self, event_id: str, stream_checker_service) -> bool:
        """Execute a scheduled channel check or create monitoring session and remove the event."""
        # First, find and extract event data while holding the lock
        with self._lock:
            event = None
            for e in self._scheduled_events:
                if e.get('id') == event_id:
                    event = e
                    break

            if not event:
                logger.warning(f"Scheduled event {event_id} not found for execution")
                return False

            channel_id = event.get('channel_id')
            program_title = event.get('program_title', 'Unknown Program')
            program_start_time = event.get('program_start_time')
            schedule_type = event.get('schedule_type', 'check')

            if not channel_id or not program_start_time:
                logger.error(f"Scheduled event {event_id} missing required fields (channel_id or program_start_time)")
                return False

        # Release lock before executing the long-running operation
        logger.info(f"Executing scheduled {schedule_type} for channel {channel_id} (program: {program_title})")

        try:
            success = False
            if schedule_type == 'monitoring':
                session_type = event.get('session_type', 'standard')
                if session_type == 'acestream':
                    from apps.api.web_api import create_acestream_channel_session_impl
                    interval_s = float(event.get('interval_s', 1.0))
                    run_seconds = int(event.get('run_seconds', 0))
                    per_sample_timeout_s = float(event.get('per_sample_timeout_s', 1.0))
                    engine_container_id = event.get('engine_container_id')

                    result, status_code = create_acestream_channel_session_impl(
                        channel_id=channel_id,
                        interval_s=interval_s,
                        run_seconds=run_seconds,
                        per_sample_timeout_s=per_sample_timeout_s,
                        engine_container_id=engine_container_id,
                        epg_event_title=program_title,
                        epg_event_start=program_start_time,
                        epg_event_end=event.get('program_end_time'),
                    )
                    if status_code in (200, 201):
                        logger.info(f"Started AceStream monitoring session {result.get('session_id')} for event {event_id}")
                        success = True
                    else:
                        logger.error(f"Failed to start AceStream monitoring session for event {event_id}: {result}")
                        success = False
                else:
                    session_id = self.create_session_from_event(event_id)
                    if session_id:
                        from apps.stream.stream_session_manager import get_session_manager
                        session_manager = get_session_manager()
                        existing = session_manager.sessions.get(session_id)
                        if existing and existing.is_active:
                            logger.info(
                                f"Monitoring session {session_id} is already active for event "
                                f"{event_id}; EPG info updated"
                            )
                            success = True
                        elif session_manager.start_session(session_id):
                            logger.info(f"Started monitoring session {session_id} for event {event_id}")
                            success = True
                        else:
                            logger.error(f"Failed to start monitoring session {session_id} for event {event_id}")
                            success = False
                    else:
                        logger.error(f"Failed to create monitoring session for event {event_id}")
                        success = False
            else:
                result = stream_checker_service.check_single_channel(
                    channel_id,
                    program_name=program_title,
                    is_epg_scheduled=True
                )
                success = result.get('success', False)
                if not success:
                    logger.error(f"Scheduled check for event {event_id} failed: {result.get('error')}")

            if success:
                with self._lock:
                    initial_count = len(self._scheduled_events)
                    self._scheduled_events = [e for e in self._scheduled_events if e.get('id') != event_id]
                    if len(self._scheduled_events) < initial_count:
                        self._save_scheduled_events()
                        logger.info(f"Scheduled event {event_id} ({schedule_type}) executed and removed successfully")
                    else:
                        logger.warning(f"Scheduled event {event_id} was already removed by another thread")
                    if program_start_time:
                        self._record_executed_event(channel_id, program_start_time)
                return True
            return False

        except Exception as e:
            logger.error(f"Error executing scheduled event {event_id}: {e}", exc_info=True)
            return False

    def create_session_from_event(self, event_id: str) -> Optional[str]:
        """Create a monitoring session from a scheduled event."""
        with self._lock:
            event = next((e for e in self._scheduled_events if e.get('id') == event_id), None)
        if not event:
            return None

        try:
            channel_id = event.get('channel_id')
            program_title = event.get('program_title')
            program_start = event.get('program_start_time')
            program_end = event.get('program_end_time')
            minutes_before = event.get('minutes_before', 5)

            from apps.stream.stream_session_manager import get_session_manager
            session_manager = get_session_manager()

            # Try to get channel-specific regex and match settings
            regex_filter = ".*"
            match_by_tvg_id = False
            try:
                regex_matcher = self._get_regex_matcher()
                group_id = event.get('group_id') or event.get('channel_group_id')
                if group_id is None:
                    udi = get_udi_manager()
                    channel_data = udi.get_channel_by_id(int(channel_id))
                    if isinstance(channel_data, dict):
                        group_id = channel_data.get('group_id') or channel_data.get('channel_group_id')
                match_config = regex_matcher.get_channel_match_config(str(channel_id), group_id)
                match_by_tvg_id = match_config.get('match_by_tvg_id', False)
                regex_filter = regex_matcher.get_channel_regex_filter(str(channel_id), default=None, group_id=group_id)
            except Exception as e:
                logger.debug(f"Could not get channel regex from matcher: {e}")

            epg_event = {
                'title': program_title,
                'start_time': program_start,
                'end_time': program_end,
            }

            session_id = session_manager.create_session(
                channel_id=channel_id,
                regex_filter=regex_filter,
                pre_event_minutes=minutes_before,
                epg_event=epg_event,
                auto_created=event.get('auto_created', False),
                auto_create_rule_id=event.get('auto_create_rule_id'),
                match_by_tvg_id=match_by_tvg_id,
                enable_looping_detection=event.get('enable_looping_detection', True),
                enable_logo_detection=event.get('enable_logo_detection', True),
            )

            logger.info(f"Created monitoring session {session_id} from event {event_id}")
            return session_id

        except Exception as e:
            logger.error(f"Error creating session from event {event_id}: {e}", exc_info=True)
            return None

    def export_auto_create_rules(self) -> List[Dict[str, Any]]:
        """Export auto-create rules."""
        exported = []
        for rule in self._auto_create_rules:
            exported_rule = {
                'name': rule.get('name'),
                'channel_ids': rule.get('channel_ids', []),
                'channel_group_ids': rule.get('channel_group_ids', []),
                'regex_pattern': rule.get('regex_pattern'),
                'minutes_before': rule.get('minutes_before', 5),
            }
            if len(exported_rule['channel_ids']) == 1 and not exported_rule['channel_group_ids']:
                exported_rule['channel_id'] = exported_rule['channel_ids'][0]
            exported.append(exported_rule)
        logger.info(f"Exported {len(exported)} auto-create rules")
        return exported

    def import_auto_create_rules(self, rules_data: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Import auto-create rules from JSON data."""
        if not isinstance(rules_data, list):
            raise ValueError("Rules data must be a list")

        imported_count = 0
        merged_count = 0
        replaced_count = 0
        failed_count = 0
        errors = []

        for idx, rule_data in enumerate(rules_data):
            try:
                for field in ['name', 'regex_pattern']:
                    if field not in rule_data:
                        raise ValueError(f"Missing required field: {field}")

                if 'channel_id' not in rule_data and 'channel_ids' not in rule_data and 'channel_group_ids' not in rule_data:
                    raise ValueError("Missing required field: channel_id, channel_ids, or channel_group_ids")

                if 'channel_ids' in rule_data:
                    import_channel_ids = list(rule_data['channel_ids'])
                elif 'channel_id' in rule_data:
                    import_channel_ids = [rule_data['channel_id']]
                else:
                    import_channel_ids = []

                import_channel_group_ids = list(rule_data.get('channel_group_ids', []))
                import_regex = rule_data['regex_pattern']
                import_name = rule_data['name']

                with self._lock:
                    matching_rule = next(
                        (r for r in self._auto_create_rules if r['regex_pattern'] == import_regex),
                        None
                    )

                    if matching_rule:
                        existing_channel_ids = set(matching_rule.get('channel_ids', []))
                        existing_group_ids = set(matching_rule.get('channel_group_ids', []))
                        import_channel_ids_set = set(import_channel_ids)
                        import_group_ids_set = set(import_channel_group_ids)

                        if (existing_channel_ids == import_channel_ids_set and
                                existing_group_ids == import_group_ids_set):
                            matching_rule['name'] = import_name
                            matching_rule['minutes_before'] = rule_data.get('minutes_before', 5)
                            if not self._save_auto_create_rules():
                                raise IOError("Failed to save replaced rule")
                            replaced_count += 1
                        else:
                            udi = get_udi_manager()
                            all_ids = list(existing_channel_ids | import_channel_ids_set)
                            channels_info = []
                            for cid in all_ids:
                                channel = udi.get_channel_by_id(cid)
                                if channel:
                                    channels_info.append({
                                        'id': cid,
                                        'name': channel.get('name', ''),
                                        'tvg_id': channel.get('tvg_id'),
                                    })
                            matching_rule['channel_ids'] = all_ids
                            matching_rule['channels_info'] = channels_info
                            if not self._save_auto_create_rules():
                                raise IOError("Failed to save merged rule")
                            merged_count += 1
                    else:
                        self.create_auto_create_rule(rule_data)
                        imported_count += 1

            except Exception as e:
                failed_count += 1
                errors.append(f"Rule {idx + 1} ('{rule_data.get('name', 'unknown')}'): {str(e)}")
                logger.warning(f"Failed to import rule: {errors[-1]}")

        result = {
            'imported': imported_count,
            'merged': merged_count,
            'replaced': replaced_count,
            'failed': failed_count,
            'total': len(rules_data),
            'errors': errors,
        }
        logger.info(
            f"Import complete: {imported_count} new, {merged_count} merged, "
            f"{replaced_count} replaced, {failed_count} failed out of {len(rules_data)} rules"
        )
        return result


# Global singleton instance
_scheduling_service: Optional[SchedulingService] = None
_scheduling_lock = threading.Lock()


def get_scheduling_service() -> SchedulingService:
    """Get the global scheduling service singleton instance."""
    global _scheduling_service
    with _scheduling_lock:
        if _scheduling_service is None:
            _scheduling_service = SchedulingService()
        return _scheduling_service
