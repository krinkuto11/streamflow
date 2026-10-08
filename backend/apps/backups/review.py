"""Restore review and conservative history mapping; no Dispatcharr mutations."""

import json
import sqlite3
from contextlib import closing
from pathlib import Path

from apps.backups.inventory import KINDS, INVENTORY_FILE, identifier, fingerprint, validate_inventory
from apps.backups.references import referenced_ids, rewrite_configuration, configuration_records, configuration_rewrites, visit


def load_source(root):
    from apps.backups.archive import read_json
    root = Path(root)
    if (root / INVENTORY_FILE).is_file():
        return validate_inventory(read_json(root / INVENTORY_FILE))
    # Older backups have no current UDI snapshot. Labels are useful, but IDs
    # and names alone can never prove that historical streams are identical.
    result = {'format_version': 1, **{kind: [] for kind in KINDS}}
    with closing(sqlite3.connect(root / 'streamflow.db')) as db:
        result['channels'] = [{'id': identifier(identity), 'name': name, 'identity': None}
                              for identity, name in db.execute('SELECT channel_id,name FROM channel_regex_configs')
                              if identifier(identity) is not None]
    return result


def required_ids(root, source):
    with closing(sqlite3.connect(Path(root) / 'streamflow.db')) as db:
        result = referenced_ids(db, Path(root))
        by_uuid = {row.get('uuid'): row['id'] for row in source['channels'] if row.get('uuid')}
        def collect_uuid(kind, value):
            if kind == 'channel_uuids' and value in by_uuid:
                result['channels'].add(by_uuid[value])
            return value
        for origin, key, value in configuration_records(db, Path(root)):
            visit(value, str(key).removesuffix('.json'), collect_uuid)
    # Providers referenced only by historical streams still need an explicit
    # ownership mapping. Thousands of stream rows themselves are never sent to UI.
    result['providers'].update(row['id'] for row in source['providers'])
    return result


def same_identity(old, current):
    return bool(old.get('identity') and old['identity'] == current.get('identity'))


def build_report(root, source, current, token):
    required = required_ids(root, source)
    report = {'token': token, 'entities': {}, 'targets': {}, 'legacy_inventory': not (Path(root) / INVENTORY_FILE).exists()}
    for kind in KINDS[:-1]:
        old_by_id = {row['id']: row for row in source[kind]}
        candidates = current[kind]
        by_id = {row['id']: row for row in candidates}
        entries = []
        for identity in sorted(required[kind]):
            old = old_by_id.get(identity, {'id': identity, 'name': f'{kind.replace("_", " ")} #{identity}', 'identity': None})
            same = by_id.get(identity)
            exact = [row for row in candidates if same_identity(old, row)]
            suggested = same['id'] if same and same_identity(old, same) else exact[0]['id'] if len(exact) == 1 else None
            verified = suggested is not None
            if suggested is None and old['name']:
                names = [row for row in candidates if row['name'].casefold() == old['name'].casefold()]
                if len(names) == 1:
                    suggested = names[0]['id']
            status = 'verified' if verified and suggested == identity else 'moved' if verified else 'review' if suggested is not None else 'unresolved'
            entries.append({'id': identity, 'name': old['name'], 'suggested': suggested, 'status': status})
        report['entities'][kind] = entries
        report['targets'][kind] = [{'id': row['id'], 'name': row['name']} for row in candidates]
    report['history_note'] = 'History is kept only for verified channel and stream identities. Unverified measurements are removed. Monitoring snapshots are omitted when identifiers change.'
    return report


def validate_mappings(root, source, current, mappings):
    required = required_ids(root, source)
    if not isinstance(mappings, dict) or set(mappings) != set(KINDS[:-1]):
        raise ValueError('Provide every restore mapping category')
    result = {}
    for kind in KINDS[:-1]:
        values = mappings[kind]
        if not isinstance(values, dict) or set(values) != {str(value) for value in required[kind]}:
            raise ValueError('Resolve every saved assignment, or explicitly skip it')
        available = {row['id'] for row in current[kind]}
        result[kind] = {}
        for old, target in values.items():
            if target is not None and (type(target) is not int or target not in available):
                raise ValueError('A selected Dispatcharr target no longer exists')
            result[kind][int(old)] = target
        selected = [target for target in result[kind].values() if target is not None]
        if kind != 'providers' and len(selected) != len(set(selected)):
            raise ValueError('Assign each target channel or group only once')
    return result


def history_maps(source, current, mappings):
    targets = {kind: {row['id']: row for row in current[kind]} for kind in KINDS}
    trusted_channels = {}
    for old in source['channels']:
        if old['id'] in mappings['channels']:
            target = mappings['channels'][old['id']]
        else:
            matches = [row['id'] for row in current['channels'] if same_identity(old, row)]
            target = matches[0] if len(matches) == 1 else None
        if target in targets['channels'] and same_identity(old, targets['channels'][target]):
            trusted_channels[old['id']] = target
    # A duplicate URL is ambiguous. Require both source URL and confirmed owner.
    stream_targets = {}
    for row in current['streams']:
        stream_targets.setdefault((row.get('identity'), row.get('provider_id')), []).append(row['id'])
    streams = {}
    for row in source['streams']:
        owner = row.get('provider_id')
        target_owner = mappings['providers'].get(owner) if owner is not None else None
        if owner is not None and target_owner is None:
            continue
        candidates = stream_targets.get((row.get('identity'), target_owner), []) if row.get('identity') else []
        if row['id'] in candidates:
            streams[row['id']] = row['id']
        elif len(candidates) == 1:
            streams[row['id']] = candidates[0]
    return trusted_channels, streams


def mapping_context(root, source, current, mappings):
    root = Path(root)
    mappings = validate_mappings(root, source, current, mappings)
    channels, streams = history_maps(source, current, mappings)
    mappings['streams'] = streams
    source_by_id = {kind: {row['id']: row for row in source[kind]} for kind in KINDS}
    target_by_id = {kind: {row['id']: row for row in current[kind]} for kind in KINDS}
    mappings['channel_uuids'] = {row['uuid']: target_by_id['channels'][target].get('uuid') or None
                               for row in source['channels'] if row.get('uuid')
                               and (target := mappings['channels'].get(row['id'])) in target_by_id['channels']}
    # Legacy UUID filters still prove identity when that UUID exists uniquely
    # on the destination. A skipped known source assignment must stay skipped.
    current_uuids = {}
    for row in current['channels']:
        if row.get('uuid'):
            current_uuids[row['uuid']] = current_uuids.get(row['uuid'], 0) + 1
    for row in source['channels']:
        if row.get('uuid') and row['id'] in mappings['channels']:
            mappings['channel_uuids'].setdefault(row['uuid'], None)
    with closing(sqlite3.connect(root / 'streamflow.db')) as db:
        def preserve_uuid(kind, value):
            if kind == 'channel_uuids' and value not in mappings['channel_uuids']:
                mappings['channel_uuids'][value] = value if current_uuids.get(value) == 1 else None
            return value
        for origin, key, value in configuration_records(db, root):
            visit(value, str(key).removesuffix('.json'), preserve_uuid)
    foreign = not (root / INVENTORY_FILE).exists() or any(target != old or (kind in ('providers','channels') and not same_identity(source_by_id[kind].get(old, {}), target_by_id[kind].get(target, {})))
                  for kind in KINDS[:-1] for old, target in mappings[kind].items())
    # Monitoring snapshots embed stream IDs as well as channel/provider IDs.
    # Keep them only when every saved stream still has its verified original ID.
    foreign = foreign or any(streams.get(row['id']) != row['id'] for row in source['streams'])
    return mappings, channels, streams, foreign, source_by_id


def preview_mappings(root, source, current, mappings):
    mappings, channels, streams, foreign, source_by_id = mapping_context(root, source, current, mappings)
    counts = {}
    with closing(sqlite3.connect(Path(root) / 'streamflow.db')) as db:
        # Validate scopes before offering confirmation; this path never writes.
        rewrites = list(configuration_rewrites(db, Path(root), mappings))
        disabled_scopes = sum(disable or (isinstance(value, dict) and value.get('enabled') is False)
                              for origin, key, value, disable in rewrites)
        for table in ('stream_telemetry', 'playback_observations'):
            fields = 'channel_id,stream_id' + (',source_fingerprint' if table == 'playback_observations' else '')
            rows = db.execute(f'SELECT {fields} FROM {table}').fetchall()
            kept = sum(row[0] in channels and row[1] in streams and
                       (table != 'playback_observations' or row[2] == source_by_id['streams'].get(row[1], {}).get('identity')) for row in rows)
            counts[table] = {'kept': kept, 'removed': len(rows) - kept}
    return {'foreign': foreign, 'history': counts, 'skipped_assignments': sum(target is None for kind in KINDS[:-1] for target in mappings[kind].values()),
            'monitoring_history_kept': not foreign, 'disabled_configurations': disabled_scopes}


def apply_mappings(root, source, current, mappings):
    root = Path(root)
    mappings, channels, streams, foreign, source_by_id = mapping_context(root, source, current, mappings)
    counts = {}
    with closing(sqlite3.connect(root / 'streamflow.db')) as db:
        for table in ('stream_telemetry', 'playback_observations'):
            rows = db.execute(f'SELECT id,channel_id,stream_id FROM {table}').fetchall()
            kept = 0
            for identity, channel, stream in rows:
                trusted = channel in channels and stream in streams
                if table == 'playback_observations':
                    observed = db.execute('SELECT source_fingerprint FROM playback_observations WHERE id=?', (identity,)).fetchone()[0]
                    trusted = trusted and observed == source_by_id['streams'].get(stream, {}).get('identity')
                if trusted:
                    db.execute(f'UPDATE {table} SET channel_id=?,stream_id=? WHERE id=?', (channels[channel], streams[stream], identity))
                    if table == 'stream_telemetry':
                        old_provider = db.execute('SELECT provider_id FROM stream_telemetry WHERE id=?', (identity,)).fetchone()[0]
                        db.execute('UPDATE stream_telemetry SET provider_id=? WHERE id=?', (mappings['providers'].get(old_provider), identity))
                    kept += 1
                else:
                    db.execute(f'DELETE FROM {table} WHERE id=?', (identity,))
            counts[table] = {'kept': kept, 'removed': len(rows) - kept}
        for identity, channel in db.execute('SELECT id,channel_id FROM channel_health').fetchall():
            if channel in channels:
                db.execute('UPDATE channel_health SET channel_id=? WHERE id=?', (channels[channel], identity))
            else:
                db.execute('DELETE FROM channel_health WHERE id=?', (identity,))
        if foreign:
            # Preserve aggregate Analytics totals; embedded old IDs cannot safely
            # be interpreted against the new server's live objects.
            db.execute('UPDATE runs SET raw_details=NULL,raw_subentries=NULL,job_subject_ref=NULL')
        rewrite_configuration(db, root, mappings, foreign=foreign)
        db.execute('INSERT OR REPLACE INTO system_settings VALUES (?,?)', ('backup_restore_review_summary', json.dumps(counts)))
        db.commit()
    if foreign:
        (root / 'monitoring_history.json').unlink(missing_ok=True)
    return {'foreign': foreign, 'history': counts}
