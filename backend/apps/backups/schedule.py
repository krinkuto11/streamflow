"""UTC persisted schedules with explicit civil time and DST handling."""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_CONFIG = {'enabled': False, 'frequency': 'daily', 'time': '03:00',
                  'timezone': 'UTC', 'weekday': 0, 'interval_hours': 24,
                  'retention': 7, 'include_history': True}


def validate_config(payload, current=None):
    if not isinstance(payload, dict) or set(payload) - set(DEFAULT_CONFIG):
        raise ValueError('Unknown backup setting')
    config = {**DEFAULT_CONFIG, **(current or {}), **payload}
    for key in ('enabled', 'include_history'):
        if not isinstance(config[key], bool):
            raise ValueError(f'{key} must be a boolean')
    if config['frequency'] not in ('daily', 'weekly', 'interval'):
        raise ValueError('frequency must be daily, weekly or interval')
    for key, low, high in (('retention', 1, 90), ('weekday', 0, 6), ('interval_hours', 1, 168)):
        value = config[key]
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            raise ValueError(f'{key} must be an integer between {low} and {high}')
    value = config['time']
    if not isinstance(value, str) or len(value) != 5 or value[2] != ':':
        raise ValueError('time must use HH:MM')
    try:
        hour, minute = map(int, value.split(':'))
        if not 0 <= hour <= 23 or not 0 <= minute <= 59 or value != f'{hour:02}:{minute:02}':
            raise ValueError
    except ValueError:
        raise ValueError('time must use HH:MM') from None
    try:
        ZoneInfo(config['timezone'])
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        raise ValueError('timezone must be a valid IANA timezone') from None
    return config


def next_run(config, now):
    """Return the next UTC timestamp; skip nonexistent times, choose the first fold."""
    now = datetime.fromtimestamp(now, timezone.utc)
    if config['frequency'] == 'interval':
        return (now + timedelta(hours=config['interval_hours'])).timestamp()
    zone = ZoneInfo(config['timezone'])
    local = now.astimezone(zone)
    hour, minute = map(int, config['time'].split(':'))
    for offset in range(15):
        day = local.date() + timedelta(days=offset)
        candidate = datetime(day.year, day.month, day.day, hour, minute, tzinfo=zone, fold=0)
        if config['frequency'] == 'weekly' and candidate.weekday() != config['weekday']:
            continue
        utc = candidate.astimezone(timezone.utc)
        if utc > now and utc.astimezone(zone).replace(tzinfo=None) == candidate.replace(tzinfo=None):
            return utc.timestamp()
    raise ValueError('Could not calculate the next backup time')
