import os
import sys
import json
import logging
from pathlib import Path
from datetime import datetime

# Setup paths
backend_dir = Path(__file__).parent.parent
sys.path.append(str(backend_dir))

from apps.database.connection import init_db, get_session
from apps.database.models import (
    DeadStream, AutomationProfile, AutomationPeriod,
    ChannelRegexConfig, ChannelRegexPattern, SystemSetting,
    Run, ChannelHealth, StreamTelemetry,
)

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

CONFIG_DIR = Path(os.environ.get('CONFIG_DIR', str(backend_dir / 'data')))

# All JSON files that may be migrated, used for backup and re-run prevention
ALL_MIGRATION_FILES = [
    'dead_streams.json',
    'automation_config.json',
    'channel_regex_config.json',
    'changelog.json',
    'stream_checker_config.json',
    'auto_create_rules.json',
    'scheduling_config.json',
    'dispatcharr_config.json',
    'session_settings.json',
]


def load_json(path: Path):
    if not path.exists():
        return None
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"Failed to load {path}: {e}")
        return None


def parse_datetime(iso_str):
    if not iso_str:
        return None
    try:
        return datetime.fromisoformat(iso_str)
    except Exception:
        logger.warning(f"Could not parse datetime value: {iso_str!r}")
        return None


# ---------------------------------------------------------------------------
# Telemetry sanitizer helpers (inlined from telemetry_db to avoid opening a
# second session while the migration session is still holding a write lock)
# ---------------------------------------------------------------------------

def _sanitize_bitrate(bitrate_str):
    if not bitrate_str:
        return None
    try:
        if isinstance(bitrate_str, (int, float)):
            return int(bitrate_str)
        s = str(bitrate_str).lower().replace(' ', '')
        if 'mbps' in s:
            return int(float(s.replace('mbps', '')) * 1000)
        elif 'kbps' in s:
            return int(float(s.replace('kbps', '')))
        return int(float(s))
    except (ValueError, TypeError):
        return None


def _sanitize_fps(fps_str):
    if not fps_str:
        return None
    try:
        if isinstance(fps_str, (int, float)):
            return float(fps_str)
        return float(str(fps_str).lower().replace(' fps', '').strip())
    except (ValueError, TypeError):
        return None


def _sanitize_resolution(res_str):
    if not res_str or 'x' not in str(res_str).lower():
        return None, None
    try:
        parts = str(res_str).lower().split('x')
        return int(parts[0]), int(parts[1])
    except (ValueError, TypeError):
        return None, None


def _parse_duration(value):
    if not value:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).lower().replace('s', '').strip())
    except (ValueError, TypeError):
        return 0.0


# ---------------------------------------------------------------------------
# Migrators
# ---------------------------------------------------------------------------

def migrate_dead_streams(session, data_dir) -> int:
    data = load_json(data_dir / 'dead_streams.json')
    if not data:
        return 0
    count = 0
    for url, item in data.items():
        dead = DeadStream(
            url=url,
            stream_id=item.get('stream_id'),
            stream_name=item.get('stream_name'),
            channel_id=item.get('channel_id'),
            marked_dead_at=parse_datetime(item.get('marked_dead_at')),
            reason=item.get('reason', 'unknown')
        )
        session.merge(dead)
        count += 1
    logger.info(f"✓ Migrated {count} Dead Streams from {data_dir.name}")
    return count


def _build_cron(schedule: dict, per_item: dict) -> str:
    """
    Reconstruct a cron schedule string from the legacy JSON period structure.

    The old format stores schedule as:
        {"type": "interval", "value": 20}           # run every 20 minutes
        {"type": "cron",     "value": "0 * * * *"}  # explicit cron expression
        {"type": "interval", "value": "*/15 * * * *"}  # already a cron string

    If the value is already a fully-formed cron expression we use it verbatim.
    Bare integers are treated as minute-intervals (*/N * * * *).
    Anything unrecognised is stored as-is so no data is silently dropped.
    """
    sched_type = schedule.get('type', '')
    value = schedule.get('value', '')

    if not value and not sched_type:
        # Fall back to top-level cron_schedule field if present
        return per_item.get('cron_schedule', '0 * * * *')

    value_str = str(value).strip()

    # Already a proper cron expression (contains spaces -> 5 fields)
    if ' ' in value_str:
        return value_str

    if sched_type == 'interval':
        try:
            minutes = int(value_str)
            return f'*/{minutes} * * * *'
        except (ValueError, TypeError):
            return value_str

    if sched_type == 'cron':
        return value_str

    logger.warning(f"Unknown schedule type {sched_type!r}; storing value verbatim: {value_str!r}")
    return value_str


def migrate_automation(session, data_dir) -> int:
    data = load_json(data_dir / 'automation_config.json')
    if not data:
        return 0

    rows_written = 0

    # --- Global automation settings ---
    # These three keys live at the top level of automation_config.json and must
    # be written to SystemSetting individually (matching how AutomationConfigManager reads them).
    global_keys = (
        'regular_automation_enabled',
        'playlist_update_interval_minutes',
        'validate_existing_streams',
    )
    for key in global_keys:
        if key in data:
            session.merge(SystemSetting(key=key, value=data[key]))
            rows_written += 1
            logger.info(f"  ✓ Migrated global automation setting: {key}")

    # --- Profiles ---
    # The profile relationship is NOT stored inside period objects. Periods have
    # no profile_id field in the JSON. The profile → period → channel relationship
    # is stored separately in channel_period_assignments (see below). We still
    # need the profile DB id for the AutomationPeriod FK, so we derive it from
    # channel_period_assignments: find any channel that references this period,
    # read its profile UUID, and look that UUID up in the profiles dict.
    #
    # Build: period_uuid -> profile_uuid from channel_period_assignments
    period_to_profile_uuid: dict = {}
    for cid, period_map in data.get('channel_period_assignments', {}).items():
        if isinstance(period_map, dict):
            for period_uuid, profile_uuid in period_map.items():
                period_to_profile_uuid.setdefault(str(period_uuid), str(profile_uuid))

    # Build: profile_uuid -> new DB integer id
    profile_uuid_to_db_id: dict = {}
    count_profiles = 0

    profiles = data.get('profiles', {})
    for p_uuid, p_item in profiles.items():
        name = p_item.get('name', 'Profile')

        existing = session.query(AutomationProfile).filter(AutomationProfile.name == name).first()
        if existing:
            logger.info(f"  Automation Profile '{name}' already exists in DB, skipping.")
            profile_uuid_to_db_id[str(p_uuid)] = existing.id
            continue

        extra = {
            'stream_matching': p_item.get('stream_matching'),
            'stream_checking': p_item.get('stream_checking'),
            'scoring_weights': p_item.get('scoring_weights'),
        }
        prof = AutomationProfile(
            name=name,
            description=p_item.get('description'),
            enabled=p_item.get('enabled', True),
            parallel_checks=p_item.get('parallel_checks', 1),
            extra_settings=extra,
        )
        session.add(prof)
        session.flush()
        profile_uuid_to_db_id[str(p_uuid)] = prof.id
        count_profiles += 1
        rows_written += 1

    # --- Periods ---
    # After migration, AutomationConfigManager reads periods by their integer DB id
    # but channel_period_assignments continues to use the original UUID strings as
    # keys (it is stored as a JSON blob in SystemSetting). The AutomationPeriod row
    # needs a valid profile_id FK, resolved via the maps built above.
    count_periods = 0

    periods = data.get('automation_periods', {})
    for per_uuid, per_item in periods.items():
        name = per_item.get('name', 'Period')

        existing_period = session.query(AutomationPeriod).filter(AutomationPeriod.name == name).first()
        if existing_period:
            logger.info(f"  Automation Period '{name}' already exists in DB, skipping.")
            continue

        # Resolve profile FK: period_uuid -> profile_uuid -> DB integer id
        profile_uuid = period_to_profile_uuid.get(str(per_uuid))
        target_profile_db_id = profile_uuid_to_db_id.get(profile_uuid) if profile_uuid else None

        if target_profile_db_id is None:
            first_prof = session.query(AutomationProfile).first()
            if not first_prof:
                logger.warning(f"  No profiles in DB; cannot migrate period '{name}'. Skipping.")
                continue
            target_profile_db_id = first_prof.id
            logger.warning(
                f"  Period '{name}' has no profile assignment in channel_period_assignments; "
                f"assigning to profile id {target_profile_db_id}."
            )

        schedule = per_item.get('schedule', {})
        cron = _build_cron(schedule, per_item)

        period = AutomationPeriod(
            profile_id=target_profile_db_id,
            cron_schedule=cron,
            name=name,
            enabled=per_item.get('enabled', True),
            channel_regex=per_item.get('channel_regex'),
            exclude_regex=per_item.get('exclude_regex'),
            matching_type=per_item.get('matching_type'),
            automation_type=per_item.get('automation_type'),
            extra_settings=per_item.get('extra_settings'),
        )
        session.add(period)
        count_periods += 1
        rows_written += 1

    logger.info(
        f"✓ Migrated {count_profiles} Automation Profiles and {count_periods} Periods "
        f"from {data_dir.name}"
    )

    # --- Channel / group / period assignments ---
    # At runtime AutomationConfigManager stores these blobs with INTEGER string ids
    # as keys/values (e.g. "1", "2") because create_period() and create_profile()
    # return str(row.id). The JSON backup used UUIDs from the old file-based system.
    # get_period() and get_profile() both do int(id) and return None on a UUID, so
    # we must remap every UUID to its new integer DB id before writing.
    #
    # profile_uuid_to_db_id and period_uuid_to_db_id are built from the loops above.
    # Build period_uuid -> DB id map from what was just inserted.
    period_uuid_to_db_id: dict = {}
    for per_uuid, per_item in periods.items():
        name = per_item.get('name', 'Period')
        row = session.query(AutomationPeriod).filter(AutomationPeriod.name == name).first()
        if row:
            period_uuid_to_db_id[str(per_uuid)] = str(row.id)

    # channel_period_assignments: { channel_id: { period_uuid: profile_uuid } }
    # -> remapped to:             { channel_id: { str(period_db_id): str(profile_db_id) } }
    raw_cpa = data.get('channel_period_assignments', {})
    if raw_cpa:
        remapped_cpa = {}
        skipped = 0
        for cid, period_map in raw_cpa.items():
            if not isinstance(period_map, dict):
                continue
            remapped_inner = {}
            for per_uuid, prof_uuid in period_map.items():
                per_db_id  = period_uuid_to_db_id.get(str(per_uuid))
                prof_db_id = str(profile_uuid_to_db_id.get(str(prof_uuid), ''))
                if per_db_id and prof_db_id:
                    remapped_inner[per_db_id] = prof_db_id
                else:
                    skipped += 1
                    logger.warning(
                        f"  channel_period_assignments: could not remap "
                        f"period {per_uuid!r} or profile {prof_uuid!r} for channel {cid} — skipped"
                    )
            if remapped_inner:
                remapped_cpa[str(cid)] = remapped_inner
        session.merge(SystemSetting(key='channel_period_assignments', value=remapped_cpa))
        rows_written += 1
        logger.info(
            f"  ✓ Migrated channel_period_assignments "
            f"({len(remapped_cpa)} channels"
            + (f", {skipped} entries skipped" if skipped else "") + ")"
        )

    # channel_assignments: { channel_id: profile_uuid } -> { channel_id: str(profile_db_id) }
    raw_ca = data.get('channel_assignments', {})
    if raw_ca:
        remapped_ca = {
            str(cid): str(profile_uuid_to_db_id[str(prof_uuid)])
            for cid, prof_uuid in raw_ca.items()
            if str(prof_uuid) in profile_uuid_to_db_id
        }
        if remapped_ca:
            session.merge(SystemSetting(key='channel_assignments', value=remapped_ca))
            rows_written += 1
            logger.info(f"  ✓ Migrated channel_assignments ({len(remapped_ca)} entries)")

    # group_assignments: { group_id: profile_uuid } -> { group_id: str(profile_db_id) }
    raw_ga = data.get('group_assignments', {})
    if raw_ga:
        remapped_ga = {
            str(gid): str(profile_uuid_to_db_id[str(prof_uuid)])
            for gid, prof_uuid in raw_ga.items()
            if str(prof_uuid) in profile_uuid_to_db_id
        }
        if remapped_ga:
            session.merge(SystemSetting(key='group_assignments', value=remapped_ga))
            rows_written += 1
            logger.info(f"  ✓ Migrated group_assignments ({len(remapped_ga)} entries)")

    return rows_written


def migrate_channel_regex(session, data_dir) -> int:
    path = data_dir / 'channel_regex_config.json'
    if not path.exists():
        return 0
    data = load_json(path)
    if not data:
        return 0

    rows_written = 0
    count_configs = 0
    count_patterns = 0

    global_settings = data.get('global_settings', {})
    if global_settings:
        session.merge(SystemSetting(key='channel_regex_global_settings', value=global_settings))
        rows_written += 1

    patterns_dict = data.get('patterns', {})
    for channel_id, item in patterns_dict.items():
        config = ChannelRegexConfig(
            channel_id=str(channel_id),
            name=item.get('name', ''),
            enabled=item.get('enabled', True),
            match_by_tvg_id=item.get('match_by_tvg_id', False)
        )
        session.merge(config)
        session.flush()
        count_configs += 1

        session.query(ChannelRegexPattern).filter(
            ChannelRegexPattern.channel_id == str(channel_id)
        ).delete()

        for order, p in enumerate(item.get('regex_patterns', [])):
            session.add(ChannelRegexPattern(
                channel_id=str(channel_id),
                pattern=p.get('pattern'),
                m3u_accounts=p.get('m3u_accounts'),
                step_order=order
            ))
            count_patterns += 1
            rows_written += 1

    logger.info(
        f"✓ Migrated {count_configs} Channel Regex Configs with {count_patterns} Patterns "
        f"from {data_dir.name}"
    )
    return rows_written


def migrate_system_settings(session, data_dir) -> int:
    files_to_migrate = [
        ('stream_checker_config.json',  'stream_checker_config'),
        ('auto_create_rules.json',       'auto_create_rules'),
        ('scheduling_config.json',       'scheduling_config'),
        ('dispatcharr_config.json',      'dispatcharr_config'),
        ('session_settings.json',        'session_settings'),
    ]

    rows_written = 0
    for filename, key in files_to_migrate:
        path = data_dir / filename
        if not path.exists():
            continue
        data = load_json(path)
        if not data:
            continue
        session.merge(SystemSetting(key=key, value=data))
        rows_written += 1
        logger.info(f"✓ Migrated {filename} -> SystemSetting({key}) from {data_dir.name}")

    return rows_written


def migrate_changelog(session, data_dir) -> int:
    """
    Migrate historical changelog entries into the relational telemetry tables.

    Writes directly to the caller's session rather than calling telemetry_db
    functions, which each open their own session and would deadlock against the
    open write transaction held by main().
    """
    path = data_dir / 'changelog.json'
    if not path.exists():
        return 0
    data = load_json(path)
    if not data:
        return 0

    count = 0
    errors = 0

    for entry in data:
        action     = entry.get('action')
        details    = entry.get('details', {}) or {}
        subentries = entry.get('subentries')
        timestamp  = entry.get('timestamp')

        try:
            run_ts = parse_datetime(timestamp) or datetime.utcnow()

            if action == 'automation_run':
                global_stats = details.get('global_stats', {})
                run = Run(
                    timestamp=run_ts,
                    duration_seconds=_parse_duration(
                        details.get('duration_seconds', details.get('duration', 0.0))
                    ),
                    total_channels=(
                        details.get('total_channels_processed')
                        or details.get('total_channels')
                        or global_stats.get('total_channels_processed', 0)
                    ),
                    total_streams=details.get('total_streams', 0) or global_stats.get('total_streams', 0),
                    global_dead_count=(
                        details.get('total_dead_streams')
                        or details.get('dead_streams')
                        or global_stats.get('total_dead_streams', 0)
                    ),
                    global_revived_count=(
                        details.get('total_revived_streams')
                        or details.get('streams_revived')
                        or global_stats.get('total_revived_streams', 0)
                    ),
                    run_type=action,
                    raw_details=json.dumps(details),
                    raw_subentries=json.dumps(subentries) if subentries else None,
                )
                session.add(run)
                session.flush()

                periods = details.get('periods', [])
                if not periods and 'summary' in details:
                    periods = details.get('summary', {}).get('periods', [])

                for p in periods:
                    for c in p.get('channels', []):
                        channel_id = c.get('channel_id')
                        ch_health = ChannelHealth(
                            run_id=run.id,
                            channel_id=channel_id,
                            channel_name=c.get('channel_name'),
                            offline=False,
                            available_streams=0,
                            dead_streams=0,
                        )
                        session.add(ch_health)
                        session.flush()

                        for step in c.get('steps', []):
                            if step.get('step') == 'Quality Check':
                                step_details = step.get('details', {})
                                dead_streams    = step_details.get('dead_streams', [])
                                checked_streams = step_details.get('checked_streams', [])

                                ch_health.dead_streams      += len(dead_streams)
                                ch_health.available_streams += len(checked_streams)
                                ch_health.offline = (
                                    ch_health.available_streams == 0
                                    and ch_health.dead_streams > 0
                                )

                                for ds in dead_streams:
                                    session.add(StreamTelemetry(
                                        run_id=run.id,
                                        channel_id=channel_id,
                                        stream_id=ds.get('id', ds.get('stream_id', 0)),
                                        provider_id=None,
                                        is_dead=True,
                                    ))

                                for cs in checked_streams:
                                    width, height = _sanitize_resolution(cs.get('resolution'))
                                    session.add(StreamTelemetry(
                                        run_id=run.id,
                                        channel_id=channel_id,
                                        stream_id=cs.get('stream_id', 0),
                                        provider_id=None,
                                        bitrate_kbps=_sanitize_bitrate(cs.get('bitrate')),
                                        resolution_width=width,
                                        resolution_height=height,
                                        fps=_sanitize_fps(cs.get('fps')),
                                        codec=cs.get('video_codec'),
                                        audio_codec=cs.get('audio_codec'),
                                        quality_score=cs.get('score'),
                                        is_dead=False,
                                        is_hdr=bool(cs.get('hdr_format') or cs.get('is_hdr')),
                                    ))

            else:
                # Generic / fallback entry (playlist updates, single checks, etc.)
                run = Run(
                    timestamp=run_ts,
                    duration_seconds=_parse_duration(
                        details.get('duration_seconds', details.get('duration', 0.0))
                    ),
                    total_channels=(
                        details.get('total_channels', 0)
                        or details.get('total_channels_processed', 0)
                    ),
                    total_streams=details.get('total_streams', 0),
                    global_dead_count=(
                        details.get('total_dead_streams', 0)
                        or details.get('dead_streams', 0)
                    ),
                    global_revived_count=details.get('total_revived_streams', 0),
                    run_type=action,
                    raw_details=json.dumps(details),
                    raw_subentries=json.dumps(subentries) if subentries else None,
                )
                session.add(run)
                session.flush()

                if subentries:
                    for group in subentries:
                        if group.get('group') == 'check':
                            for item in group.get('items', []):
                                cid   = item.get('channel_id')
                                stats = item.get('stats', {})
                                ch_health = ChannelHealth(
                                    run_id=run.id,
                                    channel_id=cid,
                                    channel_name=item.get('channel_name'),
                                    available_streams=(
                                        stats.get('total_streams', 0)
                                        - stats.get('dead_streams', 0)
                                    ),
                                    dead_streams=stats.get('dead_streams', 0),
                                )
                                session.add(ch_health)
                                session.flush()

                                for s_det in stats.get('stream_details', []):
                                    width, height = _sanitize_resolution(s_det.get('resolution'))
                                    session.add(StreamTelemetry(
                                        run_id=run.id,
                                        channel_id=cid,
                                        stream_id=s_det.get('stream_id', 0),
                                        provider_id=None,
                                        bitrate_kbps=_sanitize_bitrate(s_det.get('bitrate')),
                                        resolution_width=width,
                                        resolution_height=height,
                                        fps=_sanitize_fps(s_det.get('fps')),
                                        codec=s_det.get('video_codec'),
                                        audio_codec=s_det.get('audio_codec'),
                                        quality_score=s_det.get('score'),
                                        is_dead=(s_det.get('status') == 'dead'),
                                        is_hdr=bool(s_det.get('hdr_format') or s_det.get('is_hdr')),
                                    ))

            count += 1

        except Exception as e:
            errors += 1
            logger.warning(f"  Changelog entry {count + errors} ({action!r}) failed to migrate: {e}")

        if (count + errors) % 50 == 0:
            logger.debug(f"  Changelog progress: {count} migrated, {errors} errors")

    logger.info(
        f"✓ Migrated {count} changelog entries from {data_dir.name}"
        + (f" ({errors} entries skipped due to errors)" if errors else "")
    )
    return count


def main():
    logger.info("Starting Data Migration to SQL (Startup check)...")

    init_db()
    session = get_session()
    total_rows_written = 0

    try:
        restored = session.query(SystemSetting).filter(SystemSetting.key == 'backup_restore_sql_authoritative').first()
        if restored is not None and restored.value is True:
            logger.info('Restored SQL configuration is authoritative; skipping legacy JSON imports.')
            return
        source_dirs = [
            CONFIG_DIR,
            backend_dir.parent / 'old',
        ]

        logger.info(f"Checking directories for migration: {[str(d) for d in source_dirs]}")

        for data_dir in source_dirs:
            if not data_dir.exists():
                logger.debug(f"Source directory does not exist, skipping: {data_dir}")
                continue

            logger.info(f"Processing directory: {data_dir}")

            if (data_dir / 'dead_streams.json').exists():
                total_rows_written += migrate_dead_streams(session, data_dir)

            if (data_dir / 'automation_config.json').exists():
                total_rows_written += migrate_automation(session, data_dir)

            if (data_dir / 'channel_regex_config.json').exists():
                total_rows_written += migrate_channel_regex(session, data_dir)

            system_files = [
                'stream_checker_config.json', 'auto_create_rules.json',
                'scheduling_config.json', 'dispatcharr_config.json', 'session_settings.json',
            ]
            if any((data_dir / f).exists() for f in system_files):
                total_rows_written += migrate_system_settings(session, data_dir)

            if (data_dir / 'changelog.json').exists():
                total_rows_written += migrate_changelog(session, data_dir)

        if total_rows_written > 0:
            session.commit()
            logger.info(f"MIGRATION SUCCESSFUL! ({total_rows_written} rows written)")

            backup_dir = CONFIG_DIR / 'backup'
            backup_dir.mkdir(exist_ok=True)

            for fname in ALL_MIGRATION_FILES:
                fpath = CONFIG_DIR / fname
                if fpath.exists():
                    try:
                        dest = backup_dir / f"{fpath.name}.bak"
                        if dest.exists():
                            dest = backup_dir / f"{fpath.name}.{datetime.now().strftime('%Y%m%d%H%M%S')}.bak"
                        fpath.rename(dest)
                        logger.info(f"✓ Backed up: {fpath.name} -> {dest.name}")
                    except Exception as e:
                        logger.warning(f"Failed to back up {fpath.name}: {e}")
        else:
            logger.info("No JSON configuration files pending migration found. Skipping.")

    except Exception as e:
        session.rollback()
        logger.error(f"Migration FAILED: {e}", exc_info=True)
    finally:
        session.close()


if __name__ == '__main__':
    main()
