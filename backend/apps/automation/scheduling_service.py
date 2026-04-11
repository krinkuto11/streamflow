"""
Scheduling Service

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
        """Load scheduling configuration from SQL.

        Migrates legacy epg_refresh_interval_minutes integer to the unified
        schedule structure {type, value} on first load. Transparent to users —
        same behaviour, no data loss, cron option unlocked going forward.
        """
        default_config = {
            'epg_schedule': {'type': 'interval', 'value': 60},
            'udi_refresh_schedule': None,
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
                    legacy_minutes = config.pop('epg_refresh_interval_minutes', 60)
                    config['epg_schedule'] = {'type': 'interval', 'value': int(legacy_minutes)}
                    logger.info(
                        f"Migrated EPG schedule: epg_refresh_interval_minutes={legacy_minutes} "
                        f"→ epg_schedule={{type: interval, value: {legacy_minutes}}}"
                    )
                    needs_save = True

                # Ensure udi_refresh_schedule key exists in older configs
                if 'udi_refresh_schedule' not in config:
                    config['udi_refresh_schedule'] = None
                    needs_save = True

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
            # reappear on the next restart.
            try:
                self._scheduled_events = live_events
                self._save_scheduled_events()
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

            channel_ids_raw = rule_data.get('channel_ids') or (
                [rule_data['channel_id']] if 'channel_id' in rule_data else []
            )
            channel_ids = [int(cid) for cid in channel_ids_raw]
            if not channel_ids:
                raise ValueError("At least one channel_id or channel_ids must be provided")

            udi = get_udi_manager()
            channels_info = []
            for cid in channel_ids:
                ch = udi.get_channel_by_id(cid)
                if ch:
                    channels_info.append({
                        'id': ch.get('id'),
                        'name': ch.get('name', ''),
                        'tvg_id': ch.get('tvg_id'),
                    })

            regex_pattern = rule_data.get('regex_pattern', '')
            try:
                validation_pattern = regex_pattern.replace('CHANNEL_NAME', 'PLACEHOLDER')
                if is_dangerous_regex(validation_pattern):
                    raise ValueError("Regex pattern contains dangerous nested quantifiers (ReDoS risk)")
                re.compile(validation_pattern, re.IGNORECASE)
            except re.error as e:
                raise ValueError(f"Invalid regex pattern: {e}")

            rule = {
                'id': rule_id,
                'name': rule_data.get('name', ''),
                'channel_ids': channel_ids,
                'channels_info': channels_info,
                'regex_pattern': regex_pattern,
                'minutes_before': int(rule_data.get('minutes_before', 5)),
                'schedule_type': rule_data.get('schedule_type', 'check'),
                'session_type': rule_data.get('session_type', 'standard'),
                'interval_s': float(rule_data.get('interval_s', 1.0)),
                'run_seconds': int(rule_data.get('run_seconds', 0)),
                'per_sample_timeout_s': float(rule_data.get('per_sample_timeout_s', 1.0)),
                'engine_container_id': rule_data.get('engine_container_id'),
                'enable_looping_detection': rule_data.get('enable_looping_detection', True),
                'enable_logo_detection': rule_data.get('enable_logo_detection', True),
                'created_at': datetime.now(timezone.utc).isoformat(),
            }

            self._auto_create_rules.append(rule)
            if not self._save_auto_create_rules():
                raise IOError("Failed to save auto-create rule to disk")

            logger.info(f"Created auto-create rule {rule_id}: {rule['name']}")

        def match_in_background():
            try:
                self.fetch_epg_grid()
            except Exception as e:
                logger.error(f"Error matching programs to new rule: {e}", exc_info=True)

        threading.Thread(target=match_in_background, daemon=True).start()
        return rule

    def update_auto_create_rule(self, rule_id: str, rule_data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Update an existing auto-create rule."""
        with self._lock:
            rule_index = None
            for i, rule in enumerate(self._auto_create_rules):
                if rule.get('id') == rule_id:
                    rule_index = i
                    break

            if rule_index is None:
                return None

            rule = dict(self._auto_create_rules[rule_index])

            if 'regex_pattern' in rule_data:
                try:
                    validation_pattern = rule_data['regex_pattern'].replace('CHANNEL_NAME', 'PLACEHOLDER')
                    if is_dangerous_regex(validation_pattern):
                        raise ValueError("Regex pattern contains dangerous nested quantifiers (ReDoS risk)")
                    re.compile(validation_pattern, re.IGNORECASE)
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
        udi = get_udi_manager()
        rules = self._auto_create_rules.copy()
        programs_by_tvg_id: Dict[str, List[Dict[str, Any]]] = {}

        created_count = 0
        updated_count = 0
        skipped_count = 0

        for rule in rules:
            channel_ids = rule.get('channel_ids') or (
                [rule['channel_id']] if 'channel_id' in rule else []
            )
            minutes_before = int(rule.get('minutes_before', 5))

            try:
                pattern = re.compile(rule.get('regex_pattern', ''), re.IGNORECASE)
            except re.error:
                logger.warning(f"Rule {rule.get('id')} has invalid regex, skipping")
                continue

            for channel_id in channel_ids:
                channel_info = udi.get_channel_by_id(channel_id)
                if not channel_info:
                    logger.warning(f"Rule {rule.get('id')} channel {channel_id} not found, skipping")
                    continue

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
                        start_dt = _parse_dt(program_start)
                        end_dt = _parse_dt(program_end)
                    except (ValueError, AttributeError) as e:
                        logger.warning(f"Invalid program times for {title}: {e}")
                        continue

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
                                existing_dt = _parse_dt(existing_start)
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
                    tvg_id_val = channel_info.get('tvg_id')
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
                        'tvg_id': tvg_id_val,
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
                    with self._lock:
                        self._scheduled_events.append(new_event)
                    created_count += 1

        if created_count:
            with self._lock:
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

            session_id = session_manager.create_session(
                channel_id=channel_id,
                regex_filter=regex_filter,
                pre_event_minutes=minutes_before,
                epg_event={
                    'title': program_title,
                    'start_time': program_start,
                    'end_time': program_end,
                },
                auto_created=True,
                auto_create_rule_id=event.get('auto_create_rule_id'),
                enable_looping_detection=event.get('enable_looping_detection', True),
                enable_logo_detection=event.get('enable_logo_detection', True),
            )
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

    def fetch_channel_programs_from_api(self, tvg_id: str, force_refresh: bool = False) -> List[Dict[str, Any]]:
        """Fetch programs for a specific TVG ID from the Dispatcharr API.

        Authentication is handled transparently by fetch_data_from_url via
        the api_utils token machinery — no manual header construction needed.
        """
        config = get_dispatcharr_config()
        base_url = config.get_base_url()

        if not base_url:
            logger.warning("No Dispatcharr base URL configured")
            return []

        url = f"{base_url}/api/epg/programs/?tvg_id={tvg_id}"

        try:
            data = fetch_data_from_url(url)
            if isinstance(data, list):
                return data
            return []
        except Exception as e:
            logger.error(f"Error fetching programs for tvg_id {tvg_id}: {e}")
            return []

    def export_auto_create_rules(self) -> List[Dict[str, Any]]:
        """Export all auto-create rules as a list."""
        rules = []
        for rule in self._auto_create_rules:
            exported = dict(rule)
            # Normalise to multi-channel format; drop legacy single channel_id
            if 'channel_id' in exported and 'channel_ids' not in exported:
                exported['channel_ids'] = [exported.pop('channel_id')]
            elif 'channel_id' in exported:
                exported.pop('channel_id', None)
            rules.append(exported)
        return rules

    def import_auto_create_rules(self, rules_data: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Import auto-create rules from a list."""
        imported_count = 0
        merged_count = 0
        replaced_count = 0
        failed_count = 0
        errors = []

        for idx, rule_data in enumerate(rules_data):
            try:
                # Normalise channel binding
                if 'channel_id' in rule_data and 'channel_ids' not in rule_data:
                    rule_data = dict(rule_data)
                    rule_data['channel_ids'] = [rule_data.pop('channel_id')]

                existing_rules = [
                    r for r in self._auto_create_rules
                    if r.get('name') == rule_data.get('name')
                ]

                if existing_rules:
                    matching_rule = existing_rules[0]
                    new_ids = set(rule_data.get('channel_ids', []))
                    existing_ids = set(matching_rule.get('channel_ids', []))
                    all_ids = sorted(existing_ids | new_ids)
                    udi = get_udi_manager()
                    channels_info = []
                    for cid in all_ids:
                        ch = udi.get_channel_by_id(cid)
                        if ch:
                            channels_info.append({
                                'id': ch.get('id'),
                                'name': ch.get('name', ''),
                                'tvg_id': ch.get('tvg_id'),
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
