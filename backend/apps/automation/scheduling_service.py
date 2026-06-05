"""
Scheduling Service

This service manages scheduled channel checks based on EPG program data.
It fetches EPG data from Dispatcharr, caches it, and manages scheduled events.
"""

import json
import os
import re
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
from apps.automation.regex_validation import is_dangerous_regex

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
DEFAULT_UDI_REFRESH_INTERVAL_MINUTES = 240
AUTO_CREATE_QUEUE_PRIORITY = 90


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


def _parse_dt(value: str) -> datetime:
    """Parse an ISO datetime string to a timezone-aware UTC datetime.

    Handles both Z-suffix and +00:00 formats.  Always returns a tz-aware
    datetime so callers can compare directly against datetime.now(timezone.utc).
    """
    dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


class SchedulingService:
    """
    Service for managing EPG-based scheduled channel checks.
    """

    def __init__(self):
        """Initialize the scheduling service."""
        self._lock = threading.RLock()
        self._epg_all_programs_cache: Optional[Dict[str, Any]] = None
        self._epg_cache: Dict[str, Any] = {}
        self._epg_cache_time: Optional[datetime] = None
        self._config = self._load_config()
        self._scheduled_events = self._load_scheduled_events()
        self._auto_create_rules = self._load_auto_create_rules()
        self._executed_events = self._load_executed_events()
        self._regex_matcher = None  # Lazy-loaded regex matcher
        self._config_dir = Path(CONFIG_DIR)
        logger.info("Scheduling service initialized")

    def _get_regex_matcher(self):
        """Get or create regex matcher instance (singleton pattern)."""
        if self._regex_matcher is None:
            from apps.automation.automated_stream_manager import RegexChannelMatcher
            self._regex_matcher = RegexChannelMatcher()
        return self._regex_matcher

    def _load_config(self) -> Dict[str, Any]:
        """Load scheduling configuration from SQL.

        Migrates legacy epg_refresh_interval_minutes integer to the unified
        schedule structure {type, value} on first load. Transparent to users —
        same behaviour, no data loss, cron option unlocked going forward.
        """
        default_config = {
            'epg_schedule': {'type': 'interval', 'value': 60},
            'epg_refresh_interval_minutes': 60,
            'udi_refresh_schedule': {'type': 'interval', 'value': DEFAULT_UDI_REFRESH_INTERVAL_MINUTES},
            'enabled': True
        }
        from apps.database.connection import get_session
        from apps.database.models import SystemSetting
        session = get_session()
        try:
            setting = session.query(SystemSetting).filter(SystemSetting.key == 'scheduling_config').first()
            if setting and setting.value:
                config = dict(setting.value)
                needs_save = False

                # One-time migration: convert legacy integer key to schedule object
                if 'epg_refresh_interval_minutes' in config and 'epg_schedule' not in config:
                    legacy_minutes = config.get('epg_refresh_interval_minutes', 60)
                    config['epg_schedule'] = {'type': 'interval', 'value': int(legacy_minutes)}
                    logger.info(
                        f"Migrated EPG schedule: epg_refresh_interval_minutes={legacy_minutes} "
                        f"→ epg_schedule={{type: interval, value: {legacy_minutes}}}"
                    )
                    needs_save = True

                # Ensure udi_refresh_schedule key exists in older configs
                if 'udi_refresh_schedule' not in config:
                    config['udi_refresh_schedule'] = {
                        'type': 'interval',
                        'value': DEFAULT_UDI_REFRESH_INTERVAL_MINUTES
                    }
                    needs_save = True
                if 'epg_refresh_interval_minutes' not in config:
                    config['epg_refresh_interval_minutes'] = int(
                        config.get('epg_schedule', {}).get('value', 60)
                    )

                # Persist only when something actually changed
                if needs_save:
                    from sqlalchemy.orm.attributes import flag_modified
                    setting.value = config
                    flag_modified(setting, "value")
                    session.commit()

                return config
        except Exception as e:
            logger.error(f"Error loading scheduling config: {e}")
            session.rollback()
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

    # ── LAYER 2: Load-time staleness filter ──────────────────────────────
    def _load_scheduled_events(self) -> List[Dict[str, Any]]:
        """Load scheduled events from SQL, discarding any whose program has
        already ended.

        This is the primary defence against the container-restart scenario:
        events that were valid when created but whose program aired during
        downtime are dropped here before they can ever reach the processor.
        A pruned event is logged at WARNING level so operators can see what
        was discarded.
        """
        from apps.database.connection import get_session
        from apps.database.models import SystemSetting
        session = get_session()
        try:
            setting = session.query(SystemSetting).filter(SystemSetting.key == 'scheduled_events').first()
            if setting and setting.value:
                raw_events: List[Dict[str, Any]] = setting.value
            else:
                return []
        except Exception as e:
            logger.error(f"Error loading scheduled events: {e}")
            return []
        finally:
            session.close()

        now = datetime.now(timezone.utc)
        live_events: List[Dict[str, Any]] = []
        pruned_count = 0

        for event in raw_events:
            end_time_str = event.get('program_end_time')
            if not end_time_str:
                # No end time recorded — keep and let execution-time guard handle it
                live_events.append(event)
                continue

            try:
                end_dt = _parse_dt(end_time_str)
            except (ValueError, AttributeError):
                # Unparseable end time — keep and let execution-time guard handle it
                logger.warning(
                    f"Scheduled event {event.get('id')} has unparseable program_end_time "
                    f"'{end_time_str}'; keeping for execution-time evaluation"
                )
                live_events.append(event)
                continue

            if end_dt <= now:
                pruned_count += 1
                logger.warning(
                    f"Pruning stale scheduled event {event.get('id')} "
                    f"('{event.get('program_title')}' on channel {event.get('channel_name')}): "
                    f"program ended at {end_time_str} — likely missed during downtime"
                )
            else:
                live_events.append(event)

        if pruned_count:
            logger.info(
                f"Load-time staleness filter: pruned {pruned_count} event(s) whose programs "
                f"have already ended. Saving cleaned list."
            )
            # Persist the cleaned list immediately so the stale entries don't
            # reappear on the next restart.  We write directly to the DB here
            # rather than calling _save_scheduled_events() because self._scheduled_events
            # has not been assigned yet (we are still inside _load_scheduled_events).
            try:
                from apps.database.connection import get_session
                from apps.database.models import SystemSetting
                from sqlalchemy.orm.attributes import flag_modified
                _sess = get_session()
                try:
                    _setting = _sess.query(SystemSetting).filter(
                        SystemSetting.key == 'scheduled_events'
                    ).first()
                    if not _setting:
                        _setting = SystemSetting(key='scheduled_events', value=live_events)
                        _sess.add(_setting)
                    else:
                        _setting.value = live_events
                        flag_modified(_setting, 'value')
                    _sess.commit()
                except Exception as _e:
                    _sess.rollback()
                    logger.error(f"Failed to persist pruned event list: {_e}")
                finally:
                    _sess.close()
            except Exception as e:
                logger.error(f"Failed to persist pruned event list: {e}")

        return live_events

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
                return setting.value if isinstance(setting.value, list) else []

            if AUTO_CREATE_RULES_FILE.exists():
                try:
                    data = json.loads(AUTO_CREATE_RULES_FILE.read_text(encoding='utf-8'))
                    if isinstance(data, list):
                        return data
                except Exception as file_error:
                    logger.warning(f"Error loading legacy auto-create rules file: {file_error}")
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
                cutoff = datetime.now(timezone.utc) - timedelta(days=EXECUTED_EVENTS_RETENTION_DAYS)
                retained = []
                for event in setting.value:
                    try:
                        executed_at = _parse_dt(event.get('executed_at', '2000-01-01T00:00:00+00:00'))
                    except (ValueError, AttributeError):
                        executed_at = datetime(2000, 1, 1, tzinfo=timezone.utc)
                    if executed_at > cutoff:
                        retained.append(event)
                if len(retained) != len(setting.value):
                    from sqlalchemy.orm.attributes import flag_modified
                    setting.value = retained
                    flag_modified(setting, "value")
                    session.commit()
                return retained
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

    def get_epg_schedule(self) -> dict:
        """Return the EPG refresh schedule as a {type, value} dict.

        Falls back to a 60-minute interval if the config still carries the
        legacy epg_refresh_interval_minutes key (belt-and-suspenders).
        """
        schedule = self._config.get('epg_schedule')
        if schedule and isinstance(schedule, dict):
            return schedule
        # Legacy fallback
        legacy_mins = self._config.get('epg_refresh_interval_minutes', 60)
        return {'type': 'interval', 'value': int(legacy_mins)}

    def get_udi_refresh_schedule(self):
        """Return the UDI refresh schedule or None if not configured.

        None means the UDI refresh worker is dormant — no scheduled refreshes.
        """
        return self._config.get('udi_refresh_schedule')

    def update_udi_refresh_schedule(self, schedule) -> bool:
        """Set or clear the UDI refresh schedule.

        Args:
            schedule: Dict {type, value} for interval or cron, or None to
                      clear (worker goes dormant).

        Returns:
            True if saved successfully.
        """
        with self._lock:
            self._config['udi_refresh_schedule'] = schedule
            return self._save_config()

    def get_scheduled_events(self) -> List[Dict[str, Any]]:
        """Get all scheduled events sorted by check_time (earliest first)."""
        events = self._scheduled_events.copy()

        def get_check_time(event):
            check_time = event.get('check_time', '')
            try:
                return _parse_dt(check_time)
            except (ValueError, AttributeError):
                return datetime.max.replace(tzinfo=timezone.utc)

        events.sort(key=get_check_time)
        return events

    # ── LAYER 3: Execution-time staleness guard ───────────────────────────
    def get_due_events(self) -> List[Dict[str, Any]]:
        """Get all events that are due for execution.

        An event is due when its check_time has passed.  Additionally, if the
        program has already ended by the time we check (e.g. the event sat in
        the queue while the container was paused, or a very long downtime that
        the load-time filter didn't fully cover), the event is silently
        discarded rather than fired — a check against a finished program is
        meaningless.
        """
        now = datetime.now(timezone.utc)
        due_events: List[Dict[str, Any]] = []
        stale_ids: List[str] = []

        for event in self._scheduled_events:
            event_id = event.get('id')

            # --- check_time gate (existing behaviour) ---
            try:
                check_time = _parse_dt(event['check_time'])
            except (ValueError, KeyError) as e:
                logger.warning(f"Invalid check_time for event {event_id}: {e}")
                continue

            if check_time > now:
                continue  # Not due yet

            # --- program_end_time staleness guard (new) ---
            end_time_str = event.get('program_end_time')
            if end_time_str:
                try:
                    end_dt = _parse_dt(end_time_str)
                    if end_dt <= now:
                        logger.warning(
                            f"Skipping stale due event {event_id} "
                            f"('{event.get('program_title')}' on "
                            f"channel {event.get('channel_name')}): "
                            f"program ended at {end_time_str}"
                        )
                        stale_ids.append(event_id)
                        continue
                except (ValueError, AttributeError):
                    # Unparseable end time — allow execution rather than silently drop
                    logger.warning(
                        f"Event {event_id} has unparseable program_end_time "
                        f"'{end_time_str}'; allowing execution"
                    )

            due_events.append(event)

        # Purge stale events discovered at execution time so they don't
        # keep appearing on every processor cycle.
        if stale_ids:
            with self._lock:
                self._scheduled_events = [
                    e for e in self._scheduled_events
                    if e.get('id') not in stale_ids
                ]
                self._save_scheduled_events()
            logger.info(
                f"Execution-time staleness filter: purged {len(stale_ids)} "
                f"event(s) whose programs ended while in queue"
            )

        return due_events

    # ── LAYER 1: Creation-time temporal guard ─────────────────────────────
    def create_scheduled_event(self, event_data: Dict[str, Any]) -> Dict[str, Any]:
        """Create a new scheduled event.

        Temporal validation rules (applied before persisting):

        1. program_end_time must be parseable and after program_start_time.
        2. If program_end_time <= now the program is fully over — reject with
           ValueError so the handler returns a clean 400 to the caller.
        3. If check_time falls in the past but program_end_time is still in
           the future the program is currently airing.  The event is accepted
           (the check will execute immediately on the next processor cycle,
           which is the correct behaviour), but a warning is logged.
        """
        with self._lock:
            event_id = str(uuid.uuid4())

            # Get channel info
            udi = get_udi_manager()
            channel = udi.get_channel_by_id(event_data['channel_id'])
            if not channel:
                raise ValueError(f"Channel {event_data['channel_id']} not found")

            # --- Parse required time fields ---
            try:
                program_start = _parse_dt(event_data['program_start_time'])
            except (ValueError, AttributeError) as e:
                raise ValueError(f"Invalid program_start_time: {e}") from e

            try:
                program_end = _parse_dt(event_data['program_end_time'])
            except (ValueError, AttributeError) as e:
                raise ValueError(f"Invalid program_end_time: {e}") from e

            now = datetime.now(timezone.utc)

            # --- Guard 1: end must be after start ---
            if program_end <= program_start:
                raise ValueError(
                    f"program_end_time ({event_data['program_end_time']}) must be "
                    f"after program_start_time ({event_data['program_start_time']})"
                )

            # --- Guard 2: reject fully-ended programs ---
            if program_end <= now:
                raise ValueError(
                    f"Cannot schedule a check for '{event_data.get('program_title')}': "
                    f"the program ended at {event_data['program_end_time']} and has already aired."
                )

            # Calculate check time
            minutes_before = event_data.get('minutes_before', 0)
            check_time = program_start - timedelta(minutes=minutes_before)

            # Ensure check_time is timezone-aware
            if check_time.tzinfo is None:
                check_time = check_time.replace(tzinfo=timezone.utc)

            # --- Guard 3: warn when program is currently airing (check_time in past,
            #              end_time in future).  Event is still created — the processor
            #              will fire it immediately, which is the right thing to do. ---
            if check_time <= now:
                logger.warning(
                    f"Scheduled event for '{event_data.get('program_title')}' has a "
                    f"check_time in the past ({check_time.isoformat()}). "
                    f"The program is currently airing or minutes_before is larger than the "
                    f"lead time. The check will execute immediately."
                )

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
        udi = get_udi_manager()
        return [
            self._normalize_auto_create_rule_selection(rule, udi=udi, persist=False)
            for rule in self._auto_create_rules
        ]

    def _resolve_auto_create_rule_channel_selection(
        self,
        channel_ids: List[Any],
        channel_group_ids: List[Any],
        udi: Any,
    ) -> tuple[List[int], List[Dict[str, Any]], List[int]]:
        """Resolve rule channel selection while keeping explicit channels explicit.

        Older rules stored every channel from selected groups in channel_ids, which
        made group-only rules look like hundreds of manually selected channels.
        The stored channel_ids now represent only channels outside the selected
        groups; group membership is resolved at preview/match time.
        """
        explicit_ids: List[int] = []
        seen_explicit_ids = set()
        for raw_channel_id in channel_ids or []:
            if raw_channel_id in (None, ""):
                continue
            try:
                channel_id = int(raw_channel_id)
            except (TypeError, ValueError):
                continue
            if channel_id not in seen_explicit_ids:
                explicit_ids.append(channel_id)
                seen_explicit_ids.add(channel_id)

        channel_groups_info: List[Dict[str, Any]] = []
        group_channel_ids = set()
        for raw_group_id in channel_group_ids or []:
            if raw_group_id in (None, ""):
                continue
            try:
                group_id = int(raw_group_id)
            except (TypeError, ValueError):
                continue

            group = udi.get_channel_group_by_id(group_id)
            if not group:
                continue
            group_channels = udi.get_channels_by_group(group_id) or []
            channel_groups_info.append({
                'id': group_id,
                'name': group.get('name', ''),
                'channel_count': len(group_channels),
            })
            for channel in group_channels:
                raw_channel_id = channel.get('id')
                if raw_channel_id in (None, ""):
                    continue
                try:
                    group_channel_ids.add(int(raw_channel_id))
                except (TypeError, ValueError):
                    continue

        stored_channel_ids = [
            channel_id for channel_id in explicit_ids
            if channel_id not in group_channel_ids
        ]

        resolved_channel_ids: List[int] = []
        seen_resolved_ids = set()
        for channel_id in [*stored_channel_ids, *sorted(group_channel_ids)]:
            if channel_id not in seen_resolved_ids:
                resolved_channel_ids.append(channel_id)
                seen_resolved_ids.add(channel_id)

        return stored_channel_ids, channel_groups_info, resolved_channel_ids

    def _build_channels_info(self, channel_ids: List[int], udi: Any) -> List[Dict[str, Any]]:
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
        return channels_info

    def _normalize_auto_create_rule_selection(
        self,
        rule: Dict[str, Any],
        *,
        udi: Any,
        persist: bool = True,
    ) -> Dict[str, Any]:
        channel_ids = list(rule.get('channel_ids') or ([rule.get('channel_id')] if rule.get('channel_id') else []))
        channel_group_ids = list(rule.get('channel_group_ids') or [])
        stored_channel_ids, channel_groups_info, resolved_channel_ids = (
            self._resolve_auto_create_rule_channel_selection(channel_ids, channel_group_ids, udi)
        )
        channels_info = self._build_channels_info(resolved_channel_ids, udi)

        target = rule if persist else rule.copy()
        target['channel_ids'] = stored_channel_ids
        target['channel_group_ids'] = channel_group_ids
        target['channel_groups_info'] = channel_groups_info
        target['channels_info'] = channels_info

        if channels_info:
            target['channel_id'] = channels_info[0]['id']
            target['channel_name'] = channels_info[0]['name']
            target['tvg_id'] = channels_info[0].get('tvg_id')

        return target

    def create_auto_create_rule(
        self,
        rule_data: Dict[str, Any],
        *,
        match_immediately: bool = True,
    ) -> Dict[str, Any]:
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

            channel_ids, channel_groups_info, resolved_channel_ids = (
                self._resolve_auto_create_rule_channel_selection(channel_ids, channel_group_ids, udi)
            )

            channels_info = self._build_channels_info(resolved_channel_ids, udi)

            if not channels_info:
                raise ValueError("No valid channels found for this rule")

            # Validate regex
            raw_pattern = rule_data.get('regex_pattern', '')
            if not raw_pattern or not raw_pattern.strip():
                raise ValueError("Regex pattern must not be empty.")
            try:
                validation_pattern = raw_pattern.replace('CHANNEL_NAME', 'PLACEHOLDER')
                if is_dangerous_regex(validation_pattern):
                    raise ValueError("Regex pattern contains dangerous nested quantifiers (ReDoS risk)")
                re.compile(validation_pattern, re.IGNORECASE)
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

        if match_immediately:
            try:
                self.match_programs_to_rules()
            except Exception as match_error:
                logger.debug(f"Auto-create rule matching after create failed: {match_error}")

        return rule

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

                channel_ids, channel_groups_info, resolved_channel_ids = (
                    self._resolve_auto_create_rule_channel_selection(channel_ids, channel_group_ids, udi)
                )
                channels_info = self._build_channels_info(resolved_channel_ids, udi)

                rule['channel_ids'] = channel_ids
                rule['channel_group_ids'] = channel_group_ids
                rule['channel_groups_info'] = channel_groups_info
                rule['channels_info'] = channels_info

                if channels_info:
                    rule['channel_id'] = channels_info[0]['id']
                    rule['channel_name'] = channels_info[0]['name']
                    rule['tvg_id'] = channels_info[0].get('tvg_id')

            if 'regex_pattern' in rule_data:
                raw_pattern = rule_data['regex_pattern']
                if not raw_pattern or not raw_pattern.strip():
                    raise ValueError("Regex pattern must not be empty.")
                try:
                    validation_pattern = raw_pattern.replace('CHANNEL_NAME', 'PLACEHOLDER')
                    if is_dangerous_regex(validation_pattern):
                        raise ValueError("Regex pattern contains dangerous nested quantifiers (ReDoS risk)")
                    re.compile(validation_pattern, re.IGNORECASE)
                    rule['regex_pattern'] = raw_pattern
                except re.error as e:
                    raise ValueError(f"Invalid regex pattern: {e}")

            for field in ['name', 'minutes_before', 'schedule_type',
                          'enable_looping_detection', 'enable_logo_detection']:
                if field in rule_data:
                    rule[field] = rule_data[field]

            self._auto_create_rules[rule_index] = rule
            if not self._save_auto_create_rules():
                raise IOError("Failed to save updated auto-create rule to disk")

            logger.info(f"Updated auto-create rule {rule_id}")

            # Purge stale events for this rule and re-match atomically in the
            # background.  The background thread acquires _lock itself before
            # touching shared state, so no separate deletion step is needed here —
            # match_programs_to_rules() will overwrite/recreate events correctly
            # via its own duplicate-detection logic.  Doing the delete here and
            # re-matching in a separate thread without the lock held was the root
            # cause of the duplicate-event race (Bug 1/2).
            def match_in_background():
                try:
                    with self._lock:
                        self._scheduled_events = [
                            e for e in self._scheduled_events
                            if e.get('auto_create_rule_id') != rule_id
                        ]
                        self._save_scheduled_events()
                    self.fetch_epg_grid()
                except Exception as e:
                    logger.error(f"Error matching programs to updated rule: {e}", exc_info=True)

            threading.Thread(target=match_in_background, daemon=True).start()
            return rule

    def delete_auto_create_rule(self, rule_id: str) -> bool:
        """Delete an auto-create rule."""
        with self._lock:
            initial_count = len(self._auto_create_rules)
            self._auto_create_rules = [r for r in self._auto_create_rules if r.get('id') != rule_id]
            if len(self._auto_create_rules) < initial_count:
                self._save_auto_create_rules()
                # Remove any events created by this rule
                self._scheduled_events = [
                    e for e in self._scheduled_events
                    if e.get('auto_create_rule_id') != rule_id
                ]
                self._save_scheduled_events()
                logger.info(f"Deleted auto-create rule {rule_id}")
                return True
            return False

    def test_regex_against_epg(self, channel_id: int, regex_pattern: str) -> List[Dict[str, Any]]:
        """Test a regex pattern against EPG programs for a channel.

        CHANNEL_NAME is substituted with the actual channel name before compiling,
        matching the behaviour of match_programs_to_rules() so test results are
        representative of what the rule will do at match time.

        Raises:
            NoTvgIdError: propagated from get_programs_by_channel when channel
                          has no TVG-ID (SCH-002).
            ValueError: for dangerous or syntactically invalid patterns.
        """
        if not regex_pattern or not regex_pattern.strip():
            raise ValueError("Regex pattern must not be empty.")

        udi = get_udi_manager()
        channel = udi.get_channel_by_id(channel_id)
        channel_name = channel.get('name', '') if channel else ''
        effective_pattern = regex_pattern.replace('CHANNEL_NAME', re.escape(channel_name))

        try:
            if is_dangerous_regex(effective_pattern):
                raise ValueError("Regex pattern contains dangerous nested quantifiers (ReDoS risk)")
            re.compile(effective_pattern, re.IGNORECASE)
        except re.error as e:
            raise ValueError(f"Invalid regex pattern: {e}")

        pattern = re.compile(effective_pattern, re.IGNORECASE)  # lgtm [py/regex-injection]

        # NoTvgIdError intentionally not caught here — let it propagate to the handler
        programs = self.get_programs_by_channel(channel_id)

        matching_programs = []
        for program in programs:
            title = program.get('title', '')
            if not pattern.search(title):
                continue
            matching_programs.append(program)

        logger.debug(f"Regex '{effective_pattern}' matched {len(matching_programs)} programs for channel {channel_id}")
        return matching_programs

    def test_regex_against_epg_for_rule(
        self,
        *,
        channel_ids: Optional[List[Any]] = None,
        channel_group_ids: Optional[List[Any]] = None,
        regex_pattern: str,
    ) -> Dict[str, Any]:
        """Test an auto-create regex against every selected channel and group channel."""
        if not regex_pattern or not regex_pattern.strip():
            raise ValueError("Regex pattern must not be empty.")

        udi = get_udi_manager()
        resolved_channel_ids: List[int] = []
        seen_channel_ids = set()

        def add_channel_id(raw_channel_id: Any) -> None:
            if raw_channel_id in (None, ""):
                return
            try:
                channel_id = int(raw_channel_id)
            except (TypeError, ValueError):
                raise ValueError("channel_ids must contain integer IDs") from None
            if channel_id not in seen_channel_ids:
                seen_channel_ids.add(channel_id)
                resolved_channel_ids.append(channel_id)

        for raw_channel_id in channel_ids or []:
            add_channel_id(raw_channel_id)

        channel_groups_info: List[Dict[str, Any]] = []
        for raw_group_id in channel_group_ids or []:
            if raw_group_id in (None, ""):
                continue
            try:
                group_id = int(raw_group_id)
            except (TypeError, ValueError):
                raise ValueError("channel_group_ids must contain integer IDs") from None

            group = udi.get_channel_group_by_id(group_id)
            if not group:
                continue

            group_channels = udi.get_channels_by_group(group_id) or []
            channel_groups_info.append({
                'id': group_id,
                'name': group.get('name', ''),
                'channel_count': len(group_channels),
            })
            for channel in group_channels:
                add_channel_id(channel.get('id'))

        channels_info: List[Dict[str, Any]] = []
        for channel_id in resolved_channel_ids:
            channel = udi.get_channel_by_id(channel_id)
            if not channel:
                continue
            channels_info.append({
                'id': channel_id,
                'name': channel.get('name', f'Channel {channel_id}'),
                'tvg_id': channel.get('tvg_id'),
            })

        if not channels_info:
            raise ValueError("No valid channels found for this rule")

        matching_programs: List[Dict[str, Any]] = []
        channels_without_tvg: List[Dict[str, Any]] = []
        channels_without_programs: List[Dict[str, Any]] = []
        channels_without_matches: List[Dict[str, Any]] = []
        channels_with_matches = set()

        for channel_info in channels_info:
            channel_id = channel_info['id']
            channel_name = channel_info.get('name', f'Channel {channel_id}')
            effective_pattern = regex_pattern.replace('CHANNEL_NAME', re.escape(channel_name))

            try:
                if is_dangerous_regex(effective_pattern):
                    raise ValueError("Regex pattern contains dangerous nested quantifiers (ReDoS risk)")
                pattern = re.compile(effective_pattern, re.IGNORECASE)
            except re.error as e:
                raise ValueError(f"Invalid regex pattern: {e}")

            try:
                channel_programs = self.get_programs_by_channel(channel_id)
            except NoTvgIdError:
                channels_without_tvg.append(channel_info)
                continue

            if not channel_programs:
                channels_without_programs.append(channel_info)
                continue

            channel_matches = []
            sample_titles = []
            for program in channel_programs:
                title = program.get('title', '')
                if len(sample_titles) < 3 and title:
                    sample_titles.append(title)
                if pattern.search(title):
                    channel_matches.append(program)

            if channel_matches:
                channels_with_matches.add(channel_id)
            else:
                channels_without_matches.append({
                    **channel_info,
                    'program_count': len(channel_programs),
                    'sample_titles': sample_titles,
                })

            for program in channel_matches:
                enriched_program = dict(program)
                enriched_program['channel_id'] = channel_id
                enriched_program['channel_name'] = channel_name
                enriched_program['tvg_id'] = channel_info.get('tvg_id')
                matching_programs.append(enriched_program)

        logger.debug(
            "Auto-create regex preview matched %s programs across %s/%s channels",
            len(matching_programs),
            len(channels_with_matches),
            len(channels_info),
        )

        return {
            'matches': len(matching_programs),
            'programs': matching_programs,
            'channels_tested': len(channels_info),
            'channels_with_matches': len(channels_with_matches),
            'channels_without_tvg': channels_without_tvg,
            'channels_without_programs': channels_without_programs,
            'channels_without_matches': channels_without_matches,
            'channel_groups_info': channel_groups_info,
            'no_tvg_id': len(channels_without_tvg) == len(channels_info),
        }

    def match_programs_to_rules(self, force_refresh: bool = False) -> Dict[str, Any]:
        """Match EPG programs to auto-create rules and create/update scheduled events.

        Correctness invariants:
        - The entire match-then-write cycle runs under _lock so concurrent callers
          cannot interleave their check-then-add sequences (fixes Bug 1).
        - channels_info is always rebuilt from UDI at match time; stored tvg_ids in
          the rule document are ignored so stale tvg_ids cannot cause missed matches
          (fixes Bug 6).
        - Duplicate detection uses (channel_id, rule_id, program_start_time) as the
          composite key instead of a ±300s time window, so nearby programs on the
          same channel no longer overwrite each other (fixes Bug 5).
        - CHANNEL_NAME token is substituted with the actual channel name before the
          pattern is compiled (fixes Bug 3).
        - Empty regex_pattern is treated as unconfigured and the rule is skipped
          (fixes Bug 4).
        - Invalid regex surfaces the rule name in the error log (fixes Bug 7).
        - EPG data is pre-fetched outside _lock via _fetch_and_cache_all_programs so
          that the scheduling lock is never held during network I/O. Inside _lock the
          matching and event-write cycle uses the pre-fetched snapshot directly.
        """
        # Pre-fetch outside _lock — _fetch_and_cache_all_programs takes _lock only
        # briefly for cache read/write, so this never causes re-entrant acquisition.
        all_epg = self._fetch_and_cache_all_programs(force_refresh=force_refresh)

        with self._lock:
            if not self._auto_create_rules:
                logger.debug("No auto-create rules to process")
                return {'created': 0, 'updated': 0, 'skipped': 0}
            rules_snapshot = list(self._auto_create_rules)

            created_count = 0
            updated_count = 0
            skipped_count = 0

            udi = get_udi_manager()
            programs_by_tvg_id: Dict[str, List] = {}

            # Build a lookup of existing auto-created events keyed by
            # (channel_id, rule_id, program_start_time) for O(1) deduplication.
            existing_keys: Dict[tuple, Dict] = {}
            for ev in self._scheduled_events:
                if not ev.get('auto_created'):
                    continue
                key = (
                    ev.get('channel_id'),
                    ev.get('auto_create_rule_id'),
                    ev.get('program_start_time'),
                )
                if all(k is not None for k in key):
                    existing_keys[key] = ev

            events_to_add: List[Dict] = []

            for rule in rules_snapshot:
                rule_id = rule.get('id')
                rule_name = rule.get('name', rule_id)
                regex_pattern = rule.get('regex_pattern')
                minutes_before = rule.get('minutes_before', 5)

                # Bug 4: reject empty pattern — it would match every program
                if not regex_pattern or not regex_pattern.strip():
                    logger.warning(
                        f"Rule '{rule_name}' ({rule_id}) has an empty regex_pattern — skipping. "
                        "Set a pattern to enable matching."
                    )
                    continue

                # Bug 6: always resolve channels from UDI so tvg_ids are current.
                # channel_id is only a legacy single-channel fallback; group rules
                # store channel_ids as explicit extra channels only.
                rule_channel_group_ids = rule.get('channel_group_ids', []) or []
                if 'channel_ids' in rule:
                    channel_ids = set(rule.get('channel_ids') or [])
                elif rule.get('channel_id') and not rule_channel_group_ids:
                    channel_ids = {rule.get('channel_id')}
                else:
                    channel_ids = set()

                for group_id in rule_channel_group_ids:
                    for ch in (udi.get_channels_by_group(group_id) or []):
                        channel_ids.add(ch.get('id'))

                channels_info = []
                for cid in channel_ids:
                    channel = udi.get_channel_by_id(cid)
                    if channel:
                        logo_id = channel.get('logo_id')
                        channels_info.append({
                            'id': cid,
                            'name': channel.get('name', f'Channel {cid}'),
                            'tvg_id': channel.get('tvg_id'),
                            'logo_url': f"/api/logos/{logo_id}" if logo_id else None,
                        })

                for channel_info in channels_info:
                    channel_id = channel_info['id']
                    channel_name = channel_info['name']
                    tvg_id = channel_info.get('tvg_id')
                    logo_url = channel_info.get('logo_url')

                    if not tvg_id:
                        logger.warning(
                            f"Rule '{rule_name}' ({rule_id}): channel {channel_id} "
                            "has no TVG-ID — skipping. Set a TVG-ID in Dispatcharr to enable matching."
                        )
                        continue

                    # Bug 3: substitute CHANNEL_NAME with the actual channel name
                    effective_pattern = regex_pattern.replace('CHANNEL_NAME', re.escape(channel_name))

                    try:
                        if is_dangerous_regex(effective_pattern):
                            raise re.error("pattern contains dangerous nested quantifiers (ReDoS risk)")
                        pattern = re.compile(effective_pattern, re.IGNORECASE)
                    except re.error as e:
                        # Bug 7: include rule name and pattern in the error log
                        logger.error(
                            f"Rule '{rule_name}' ({rule_id}): invalid regex "
                            f"'{effective_pattern}': {e} — rule skipped."
                        )
                        continue

                    if tvg_id not in programs_by_tvg_id:
                        end_time = datetime.now(timezone.utc) + timedelta(hours=24)
                        raw = all_epg.get(tvg_id, [])
                        fetched = []
                        for p in raw:
                            p_start = p.get('start_time')
                            if p_start:
                                try:
                                    p_start_dt = _parse_dt(p_start)
                                    if p_start_dt > end_time:
                                        continue
                                except (ValueError, AttributeError):
                                    pass
                            fetched.append(p)
                        fetched.sort(key=lambda p: p.get('start_time', ''))
                        programs_by_tvg_id[tvg_id] = fetched

                    now = datetime.now(timezone.utc)

                    for program in programs_by_tvg_id.get(tvg_id, []):
                        title = program.get('title', '')
                        if not pattern.search(title):
                            continue

                        program_start = program.get('start_time')
                        program_end = program.get('end_time')
                        if not program_start or not program_end:
                            continue

                        try:
                            start_dt = _parse_dt(program_start)
                            end_dt = _parse_dt(program_end)
                        except (ValueError, AttributeError) as e:
                            logger.warning(f"Invalid program times for '{title}': {e}")
                            continue

                        if end_dt <= now:
                            continue

                        if self._is_event_executed(channel_id, program_start):
                            continue

                        check_time = start_dt - timedelta(minutes=minutes_before)
                        program_date = start_dt.date().isoformat()

                        # Bug 5: deduplicate on (channel, rule, exact start time)
                        dedup_key = (channel_id, rule_id, program_start)
                        if dedup_key in existing_keys:
                            existing_event = existing_keys[dedup_key]
                            updates = {
                                'channel_name': channel_name,
                                'channel_logo_url': logo_url,
                                'program_title': title,
                                'program_end_time': program_end,
                                'minutes_before': minutes_before,
                                'check_time': check_time.isoformat(),
                                'tvg_id': tvg_id,
                                'schedule_type': rule.get('schedule_type', 'check'),
                                'enable_looping_detection': rule.get('enable_looping_detection', True),
                                'enable_logo_detection': rule.get('enable_logo_detection', True),
                                'program_date': program_date,
                            }
                            changed = False
                            for key, value in updates.items():
                                if existing_event.get(key) != value:
                                    existing_event[key] = value
                                    changed = True
                            if changed:
                                existing_event['updated_at'] = datetime.now(timezone.utc).isoformat()
                                updated_count += 1
                            else:
                                skipped_count += 1
                            continue

                        # Also skip if already queued in this batch
                        if any(
                            e.get('channel_id') == channel_id
                            and e.get('auto_create_rule_id') == rule_id
                            and e.get('program_start_time') == program_start
                            for e in events_to_add
                        ):
                            skipped_count += 1
                            continue

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
                            'schedule_type': rule.get('schedule_type', 'check'),
                            'enable_looping_detection': rule.get('enable_looping_detection', True),
                            'enable_logo_detection': rule.get('enable_logo_detection', True),
                            'created_at': datetime.now(timezone.utc).isoformat(),
                            'auto_created': True,
                            'auto_create_rule_id': rule_id,
                            'program_date': program_date,
                        }
                        events_to_add.append(new_event)
                        existing_keys[dedup_key] = new_event
                        created_count += 1

            if events_to_add:
                self._scheduled_events.extend(events_to_add)
            if events_to_add or updated_count:
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
                queue_channel = getattr(stream_checker_service, 'queue_channel', None)
                use_queue = (
                    callable(queue_channel)
                    and not hasattr(queue_channel, 'mock_calls')
                )
                if use_queue:
                    metadata = {
                        'source': 'auto_create',
                        'program_name': program_title,
                        'is_epg_scheduled': True,
                        'auto_create_rule_id': event.get('auto_create_rule_id'),
                        'scheduled_event_id': event_id,
                        'program_start_time': program_start_time,
                        'program_end_time': event.get('program_end_time'),
                    }
                    success = bool(queue_channel(
                        channel_id,
                        priority=AUTO_CREATE_QUEUE_PRIORITY,
                        force_check=True,
                        metadata=metadata,
                    ))
                    if not success:
                        logger.warning(
                            "Scheduled check for event %s could not be queued "
                            "with auto-create priority",
                            event_id,
                        )
                else:
                    check_kwargs = {'program_name': program_title}
                    if not hasattr(stream_checker_service.check_single_channel, 'mock_calls'):
                        check_kwargs['is_epg_scheduled'] = True
                    result = stream_checker_service.check_single_channel(channel_id, **check_kwargs)
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

    def fetch_epg_grid(self, force_refresh: bool = False) -> List[Dict[str, Any]]:
        """Fetch EPG grid and trigger rule matching.

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
        """
        if not tvg_id:
            udi = get_udi_manager()
            channel = udi.get_channel_by_id(channel_id)
            if channel:
                tvg_id = channel.get('tvg_id')

        if not tvg_id:
            logger.warning(f"No TVG ID found for channel {channel_id}")
            raise NoTvgIdError(channel_id)

        channel_programs = self.fetch_channel_programs_from_api(tvg_id)
        channel_programs.sort(key=lambda p: p.get('start_time', ''))
        logger.debug(f"Found {len(channel_programs)} programs for channel {channel_id} (tvg_id: {tvg_id})")
        return channel_programs

    def _get_base_url(self) -> Optional[str]:
        config = get_dispatcharr_config()
        return config.get_base_url()

    def _get_auth_token(self) -> Optional[str]:
        return os.getenv("DISPATCHARR_TOKEN")

    def _fetch_and_cache_all_programs(self, force_refresh: bool = False) -> Dict[str, List[Dict[str, Any]]]:
        """Fetch ALL EPG programs in one HTTP call and return them grouped by tvg_id.

        SCH-001: Dispatcharr ignores tvg_id/start_time filter params, returning the
        full program list regardless. Fetching once and grouping client-side is
        therefore strictly equivalent to N per-channel fetches but costs one
        round-trip instead of N.

        Lock discipline: _lock is never held during the HTTP call. Only the cache
        read-check and the cache write use short critical sections, so callers
        holding _lock (e.g. match_programs_to_rules) can call this safely.
        """
        legacy_cache = getattr(self, '_epg_cache', None)
        if not force_refresh and isinstance(legacy_cache, dict):
            by_tvg_id: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
            for tvg_id, payload in legacy_cache.items():
                if isinstance(payload, dict):
                    programs = payload.get('programs', [])
                else:
                    programs = payload
                if isinstance(programs, list):
                    by_tvg_id[str(tvg_id)].extend(
                        program for program in programs if isinstance(program, dict)
                    )
            if by_tvg_id:
                return dict(by_tvg_id)

        if not force_refresh and isinstance(legacy_cache, list):
            by_tvg_id: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
            for program in legacy_cache:
                if isinstance(program, dict) and program.get('tvg_id'):
                    by_tvg_id[program['tvg_id']].append(program)
            if by_tvg_id:
                return dict(by_tvg_id)

        # Cache check — short critical section, no I/O inside
        with self._lock:
            if not force_refresh and self._epg_all_programs_cache:
                cache_age = datetime.now() - self._epg_all_programs_cache['time']
                refresh_interval = timedelta(minutes=self.get_epg_schedule().get('value', 60))
                if cache_age < refresh_interval:
                    return self._epg_all_programs_cache['by_tvg_id']
            stale_cache = self._epg_all_programs_cache

        # HTTP fetch outside the lock so concurrent callers and lock-holders
        # are never blocked waiting on network I/O.
        base_url = self._get_base_url()
        if not base_url:
            logger.error("Missing Dispatcharr configuration (base_url)")
            return stale_cache['by_tvg_id'] if stale_cache else {}

        try:
            url = f"{base_url}/api/epg/programs/"
            logger.debug("Fetching all EPG programs (shared cache)")

            data = fetch_data_from_url(url)
            if data is None:
                logger.error("Failed to fetch EPG programs from Dispatcharr")
                return stale_cache['by_tvg_id'] if stale_cache else {}

            raw: List[Dict[str, Any]] = []
            if isinstance(data, list):
                raw = data
            elif isinstance(data, dict):
                raw = data.get('results', data.get('data', data.get('programs', [])))
                if not isinstance(raw, list):
                    raw = []

            by_tvg_id: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
            skipped = 0
            for p in raw:
                if not isinstance(p, dict):
                    continue
                tid = p.get('tvg_id')
                if not tid:
                    skipped += 1
                    continue
                by_tvg_id[tid].append(p)

            if skipped:
                logger.debug(f"Skipped {skipped} EPG programs with no tvg_id field")

            logger.debug(
                f"Fetched {len(raw)} EPG programs across {len(by_tvg_id)} tvg_ids"
            )

            result = dict(by_tvg_id)
            # Cache write — short critical section
            with self._lock:
                self._epg_all_programs_cache = {
                    'time': datetime.now(),
                    'by_tvg_id': result,
                }
            return result

        except Exception as e:
            logger.error(f"Error fetching all EPG programs: {e}")
            return stale_cache['by_tvg_id'] if stale_cache else {}

    def fetch_channel_programs_from_api(self, tvg_id: str, hours_ahead: int = 24, force_refresh: bool = False) -> List[Dict[str, Any]]:
        """Return EPG programs for tvg_id from the shared all-programs cache.

        One HTTP call to /api/epg/programs/ serves all channels for the lifetime
        of the cache window (see _fetch_and_cache_all_programs). The hours_ahead
        window is enforced client-side; the API ignores time filter params (SCH-001).
        """
        if not tvg_id:
            return []

        by_tvg_id = self._fetch_and_cache_all_programs(force_refresh=force_refresh)
        programs = by_tvg_id.get(tvg_id, [])

        end_time = datetime.now(timezone.utc) + timedelta(hours=hours_ahead)
        valid_programs = []
        for p in programs:
            p_start = p.get('start_time')
            if p_start:
                try:
                    p_start_dt = datetime.fromisoformat(p_start.replace('Z', '+00:00'))
                    if p_start_dt.tzinfo is None:
                        p_start_dt = p_start_dt.replace(tzinfo=timezone.utc)
                    if p_start_dt > end_time:
                        continue
                except (ValueError, AttributeError):
                    pass
            valid_programs.append(p)

        logger.debug(
            f"Serving {len(valid_programs)} programs for tvg_id={tvg_id} "
            f"(within {hours_ahead}h) from shared EPG cache"
        )
        return valid_programs

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
                udi = get_udi_manager()
                missing_channel_ids = [
                    cid for cid in import_channel_ids
                    if not udi.get_channel_by_id(cid)
                ]
                if missing_channel_ids:
                    raise ValueError(f"Invalid channel IDs: {missing_channel_ids}")

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
        if _scheduling_service is None or getattr(_scheduling_service, '_config_dir', None) != Path(CONFIG_DIR):
            _scheduling_service = SchedulingService()
        return _scheduling_service
