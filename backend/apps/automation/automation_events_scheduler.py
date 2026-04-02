#!/usr/bin/env python3
"""
Automation Events Scheduler

Calculates and caches upcoming automation events based on automation periods.
Cache is invalidated when automation periods are modified.
"""

import json
import threading
from datetime import datetime, timedelta
from typing import List, Dict, Optional, Any
from pathlib import Path
import os

from apps.core.logging_config import setup_logging
from apps.automation.automation_config_manager import get_automation_config_manager

logger = setup_logging(__name__)

# Cache configuration constants
CACHE_VALIDITY_SECONDS = 300  # 5 minutes
CACHE_STALENESS_THRESHOLD_SECONDS = 3600  # 1 hour

# Compatibility constants retained for legacy tests/tools that monkeypatch
# scheduler cache paths. Cache persistence now uses database settings.
CONFIG_DIR = Path(os.environ.get('CONFIG_DIR', '/app/data'))
EVENTS_CACHE_FILE = CONFIG_DIR / 'automation_events_cache.json'

# Try to import croniter for cron expression support
try:
    from croniter import croniter
    CRONITER_AVAILABLE = True
except ImportError:
    CRONITER_AVAILABLE = False
    logger.warning("croniter not available - cron schedules will not be calculated")

# Configuration directory - removed file constants for database migration


class AutomationEventsScheduler:
    """
    Manages calculation and caching of upcoming automation events.
    """
    
    def __init__(self):
        self._lock = threading.RLock()
        self._cache = None
        self._cache_timestamp = None
        logger.info("Automation Events Scheduler initialized")
    
    def calculate_next_event_for_period(self, period: Dict, current_time: Optional[datetime] = None) -> Optional[Dict]:
        """Calculate the next scheduled event for a given automation period.
        
        Args:
            period: Period dictionary with schedule information
            current_time: Current time (defaults to now)
            
        Returns:
            Dict with next_run timestamp or None if schedule cannot be calculated
        """
        if current_time is None:
            current_time = datetime.now()
        
        schedule = period.get('schedule', {})
        schedule_type = schedule.get('type', 'interval')
        schedule_value = schedule.get('value')
        
        if not schedule_value:
            logger.warning(f"Period {period.get('id')} has no schedule value")
            return None
        
        try:
            if schedule_type == 'interval':
                # Interval in minutes
                minutes = int(schedule_value)
                next_run = current_time + timedelta(minutes=minutes)
                return {
                    'next_run': next_run.isoformat(),
                    'schedule_type': 'interval',
                    'schedule_value': minutes
                }
                
            elif schedule_type == 'cron' and CRONITER_AVAILABLE:
                # Add validation to prevent croniter exceptions for non-cron strings (e.g., '60')
                if not schedule_value or len(str(schedule_value).split()) < 5:
                    logger.warning(
                        f"Period {period.get('id')} has 'cron' schedule type, but value '{schedule_value}' "
                        f"is not a valid cron expression. Did you mean 'interval'?"
                    )
                    return None
                    
                # Cron expression
                try:
                    cron = croniter(schedule_value, current_time)
                    next_run = cron.get_next(datetime)
                    return {
                        'next_run': next_run.isoformat(),
                        'schedule_type': 'cron',
                        'schedule_value': schedule_value
                    }
                except Exception as e:
                    logger.error(f"Failed to parse cron expression '{schedule_value}': {e}")
                    return None
            else:
                logger.warning(f"Unknown schedule type or croniter not available: {schedule_type}")
                return None
                
        except Exception as e:
            logger.error(f"Error calculating next event for period {period.get('id')}: {e}")
            return None
    
    def calculate_upcoming_events(self, hours_ahead: int = 24, max_events: int = 100) -> List[Dict]:
        """Calculate upcoming automation events for all periods.
        
        Args:
            hours_ahead: How many hours into the future to calculate
            max_events: Maximum number of events to return
            
        Returns:
            List of event dictionaries sorted by time
        """
        automation_config = get_automation_config_manager()
        all_periods = automation_config.get_all_periods()
        
        if not all_periods:
            logger.info("No automation periods configured")
            return []
        
        current_time = datetime.now()
        end_time = current_time + timedelta(hours=hours_ahead)
        
        events = []
        
        for period in all_periods:
            period_id = period.get('id')
            period_name_raw = period.get('name', '')
            period_name = str(period_name_raw).strip() if period_name_raw is not None else ''
            if not period_name:
                period_name = f"Period {period_id}"
            
            # Resolve effective channel -> profile assignments for this period.
            # Includes group-level assignments with channel-level overrides.
            channel_profile_map = automation_config.get_effective_period_channel_profiles(period_id)
            channel_count = len(channel_profile_map)
            
            if channel_count == 0:
                continue  # Skip periods with no channels assigned
            
            # Group channels by profile to show which profiles will be used
            profile_counts = {}
            for profile_id in channel_profile_map.values():
                if profile_id:
                    profile_counts[profile_id] = profile_counts.get(profile_id, 0) + 1
            
            # Get profile names (could be multiple profiles for same period)
            profile_names = []
            for profile_id, count in profile_counts.items():
                profile = automation_config.get_profile(profile_id)
                if profile:
                    profile_names.append(f"{profile.get('name', 'Unknown')} ({count} channels)")
            
            profile_display = ", ".join(profile_names) if profile_names else "No Profile"
            
            # Calculate events for this period
            schedule = period.get('schedule', {})
            schedule_type = schedule.get('type', 'interval')
            schedule_value = schedule.get('value')
            
            if not schedule_value:
                continue
            
            # Generate events within the time window
            # Try to align the schedule track with the *actual* last run time for this period
            base_time = current_time
            try:
                from web_api import get_automation_manager
                manager = get_automation_manager()
                last_run = manager.period_last_run.get(period_id)
                if last_run:
                    if isinstance(last_run, datetime):
                        base_time = last_run
                    else:
                        base_time = datetime.fromisoformat(str(last_run))
            except Exception:
                pass

            temp_time = base_time
            period_events = []
            
            try:
                if schedule_type == 'interval':
                    minutes = int(schedule_value)
                    while temp_time < end_time and len(period_events) < 50:  # Limit per period
                        temp_time = temp_time + timedelta(minutes=minutes)
                        if temp_time >= current_time and temp_time <= end_time:
                            period_events.append({
                                'time': temp_time.isoformat(),
                                'period_id': period_id,
                                'period_name': period_name,
                                'profile_display': profile_display,
                                'channel_count': channel_count,
                                'schedule_type': 'interval',
                                'schedule_display': f'Every {minutes} minutes'
                            })
                
                elif schedule_type == 'cron' and CRONITER_AVAILABLE:
                    # Add validation to prevent croniter exceptions for non-cron strings (e.g., '60')
                    if not schedule_value or len(str(schedule_value).split()) < 5:
                        logger.warning(
                            f"Period {period_id} has 'cron' schedule type, but value '{schedule_value}' "
                            f"is not a valid cron expression. Skipping cron calculation."
                        )
                        continue
                        
                    try:
                        cron = croniter(schedule_value, base_time)
                        while temp_time < end_time and len(period_events) < 50:
                            temp_time = cron.get_next(datetime)
                            if temp_time >= current_time and temp_time <= end_time:
                                period_events.append({
                                    'time': temp_time.isoformat(),
                                    'period_id': period_id,
                                    'period_name': period_name,
                                    'profile_display': profile_display,
                                    'channel_count': channel_count,
                                    'schedule_type': 'cron',
                                    'schedule_display': f'Cron: {schedule_value}'
                                })
                    except Exception as e:
                        logger.error(f"Failed to calculate cron events for period {period_id}: {e}")
                        continue
                
                events.extend(period_events)
                
            except Exception as e:
                logger.error(f"Error generating events for period {period_id}: {e}")
                continue
        
        # Sort events by time
        events.sort(key=lambda x: x['time'])
        
        # Limit total events
        if len(events) > max_events:
            events = events[:max_events]
        
        logger.info(f"Calculated {len(events)} upcoming events for {len(all_periods)} periods")
        return events
    
    def get_cached_events(self, hours_ahead: int = 24, max_events: int = 100, force_refresh: bool = False) -> Dict[str, Any]:
        """Get cached upcoming events or recalculate if needed.
        
        Args:
            hours_ahead: How many hours into the future to calculate
            max_events: Maximum number of events to return
            force_refresh: Force recalculation even if cache is valid
            
        Returns:
            Dict with events list and cache metadata
        """
        with self._lock:
            current_time = datetime.now()
            
            # Check if cache is valid (less than CACHE_VALIDITY_SECONDS old)
            cache_valid = (
                not force_refresh
                and self._cache is not None
                and self._cache_timestamp is not None
                and (current_time - self._cache_timestamp).total_seconds() < CACHE_VALIDITY_SECONDS
            )
            
            if cache_valid:
                logger.debug("Returning cached automation events")
                return {
                    'events': self._cache,
                    'cached_at': self._cache_timestamp.isoformat(),
                    'from_cache': True
                }
            
            # Recalculate events
            logger.info("Calculating fresh automation events")
            events = self.calculate_upcoming_events(hours_ahead, max_events)
            
            # Update cache
            self._cache = events
            self._cache_timestamp = current_time
            
            # Persist cache to disk
            self._save_cache()
            
            return {
                'events': events,
                'cached_at': current_time.isoformat(),
                'from_cache': False
            }
    
    def invalidate_cache(self):
        """Invalidate the events cache."""
        with self._lock:
            logger.info("Invalidating automation events cache")
            self._cache = None
            self._cache_timestamp = None
            # Clear database setting
            try:
                from apps.database.manager import get_db_manager
                db = get_db_manager()
                db.set_system_setting('automation_events_cache', {})
            except Exception as e:
                logger.error(f"Failed to clear cache in database: {e}")
    
    def _save_cache(self):
        """Save cache to database for persistence across restarts."""
        from apps.database.manager import get_db_manager
        with self._lock:
            try:
                if self._cache is None:
                    return
                
                cache_data = {
                    'events': self._cache,
                    'timestamp': self._cache_timestamp.isoformat() if self._cache_timestamp else None
                }
                
                db = get_db_manager()
                success = db.set_system_setting('automation_events_cache', cache_data)
                if success:
                    logger.debug("Saved automation events cache to database")
                else:
                    logger.error("Failed to save automation events cache to database")
            except Exception as e:
                logger.error(f"Failed to save cache to database: {e}")
    
    def _load_cache(self):
        """Load cache from database if available."""
        from apps.database.manager import get_db_manager
        with self._lock:
            try:
                db = get_db_manager()
                cache_data = db.get_system_setting('automation_events_cache', {})
                if not cache_data:
                    return
                
                self._cache = cache_data.get('events')
                timestamp_str = cache_data.get('timestamp')
                
                if timestamp_str:
                    self._cache_timestamp = datetime.fromisoformat(timestamp_str)
                    age_seconds = (datetime.now() - self._cache_timestamp).total_seconds()
                    if age_seconds > CACHE_STALENESS_THRESHOLD_SECONDS:
                        logger.info("Cached events are stale, invalidating")
                        self.invalidate_cache()
                    else:
                        logger.info(f"Loaded {len(self._cache)} events from database ({age_seconds:.0f}s old)")
                
            except Exception as e:
                logger.error(f"Failed to load cache from database: {e}")
                self.invalidate_cache()


# Singleton instance
_events_scheduler = None
_scheduler_lock = threading.Lock()


def get_events_scheduler() -> AutomationEventsScheduler:
    """Get the singleton events scheduler instance."""
    global _events_scheduler
    if _events_scheduler is None:
        with _scheduler_lock:
            if _events_scheduler is None:
                _events_scheduler = AutomationEventsScheduler()
                _events_scheduler._load_cache()
    return _events_scheduler
