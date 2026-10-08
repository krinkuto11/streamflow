"""Explicit Dispatcharr references; automation profile/period IDs stay local."""

import json
from apps.backups.inventory import identifier, KINDS

FIELDS = {
    'providers': {'m3u_accounts', 'm3u_account_ids', 'enabled_m3u_accounts', 'm3u_account', 'm3u_account_id', 'provider_id', 'provider_ids'},
    'channels': {'channel_id', 'dispatcharr_channel_id', 'target_channel_id', 'start_channel_id', 'channel_ids', 'included_channel_ids', 'excluded_channel_ids', 'pending_channel_ids', 'completed_channel_ids'},
    'groups': {'group_id', 'channel_group_id', 'group_ids', 'channel_group_ids', 'included_group_ids', 'excluded_group_ids'},
    'channel_profiles': {'channel_profile_id', 'channel_profile_ids', 'selected_channel_profile_ids'},
    'streams': {'stream_id', 'stream_ids'},
    'channel_uuids': {'included_channel_uuids', 'excluded_channel_uuids'},
}
KEYED = {
    'channels': {'channel_assignments', 'channel_period_assignments', 'channel_epg_scheduled_assignments', 'channel_settings', 'channels'},
    'groups': {'group_assignments', 'group_period_assignments', 'group_epg_scheduled_assignments', 'group_settings', 'groups'},
}
RUNTIME_KEYS = {'automation_state', 'last_run', 'last_automation_run', 'teamarr_preflight_attempt_state',
                'teamarr_preflight_checked_state', 'shadow_blank_monitor_safety_state'}


def visit(value, key, callback, depth=0):
    if depth > 60:
        raise ValueError('Configuration nesting exceeds the remapping limit')
    kind = next((k for k, fields in FIELDS.items() if key in fields), None)
    if key == 'order':
        kind = 'channels'
    if kind:
        if isinstance(value, list):
            return [mapped for item in value if (mapped := callback(kind, item)) is not None]
        return callback(kind, value)
    keyed_kind = next((k for k, fields in KEYED.items() if key in fields), None)
    if keyed_kind and isinstance(value, dict) and all(identifier(item) is not None for item in value):
        result = {}
        for identity, item in value.items():
            mapped = callback(keyed_kind, identity)
            if mapped is not None:
                rewritten = visit(item, '', callback, depth + 1)
                if rewritten is not None:
                    result[str(mapped)] = rewritten
        return result
    if key == 'group_regex_patterns' and isinstance(value, dict):
        return {k: visit(v, 'groups' if k == 'patterns' else k, callback, depth + 1) for k, v in value.items()}
    if isinstance(value, dict):
        # Explicit provider restrictions must never become an unrestricted empty list.
        for field in ('m3u_accounts', 'm3u_account_ids'):
            if isinstance(value.get(field), list) and value[field] and not visit(value[field], field, callback, depth + 1):
                return None
        if identifier(value.get('channel_id')) is not None and callback('channels', value['channel_id']) is None:
            return None
        scopes = ('channel_ids', 'channel_group_ids', 'included_channel_ids', 'included_channel_uuids')
        original_scopes = [field for field in scopes if isinstance(value.get(field), list) and value[field]]
        if original_scopes and not any(visit(value[field], field, callback, depth + 1) for field in original_scopes):
            # A skipped restricted rule/monitor must never turn into "all".
            if 'enabled' in value:
                value = {**value, 'enabled': False}
            else:
                return None
        return {k: visit(v, k, callback, depth + 1) for k, v in value.items()}
    if isinstance(value, list):
        return [mapped for item in value if (mapped := visit(item, '', callback, depth + 1)) is not None]
    return value


def configuration_records(db, root):
    from apps.backups.archive import config_files, read_json
    for key, raw in db.execute('SELECT key,value FROM system_settings'):
        if key in RUNTIME_KEYS or key.startswith('backup_') or key == 'dispatcharr_config':
            continue
        yield ('setting', key, json.loads(raw))
    for table in ('automation_profiles', 'automation_periods'):
        for identity, raw in db.execute(f'SELECT id,extra_settings FROM {table}'):
            yield (table, identity, json.loads(raw) if raw else {})
    for identity, raw in db.execute('SELECT id,variables FROM match_profile_steps'):
        yield ('match_profile_steps', identity, json.loads(raw) if raw else {})
    for path in config_files(root):
        if path.stem not in RUNTIME_KEYS:
            yield ('file', path.name, read_json(path))


def referenced_ids(db, root):
    result = {kind: set() for kind in KINDS}
    def collect(kind, value):
        if kind == 'channel_uuids':
            return value
        identity = identifier(value)
        if identity is not None:
            result[kind].add(identity)
        return value
    for origin, key, value in configuration_records(db, root):
        visit(value, str(key).removesuffix('.json'), collect)
    for identity, providers in db.execute('SELECT channel_id,m3u_accounts FROM channel_regex_patterns'):
        collect('channels', identity)
        for provider in json.loads(providers) if providers else []:
            collect('providers', provider)
    for identity, in db.execute('SELECT channel_id FROM channel_regex_configs'):
        collect('channels', identity)
    return result


def rewrite_configuration(db, root, mappings, *, foreign):
    from apps.core.atomic_json import atomic_write_json
    def mapped(kind, value):
        if kind == 'channel_uuids':
            return mappings[kind].get(value)
        identity = identifier(value)
        if identity is None:
            return value
        target = mappings[kind].get(identity)
        return str(target) if isinstance(value, str) and target is not None else target
    for origin, key, value in list(configuration_records(db, root)):
        rewritten = visit(value, str(key).removesuffix('.json'), mapped)
        if origin == 'setting':
            if key == 'enabled_m3u_accounts' and value and not rewritten:
                raise ValueError('Select at least one target for the enabled-provider filter; dropping all would broaden automation')
            db.execute('UPDATE system_settings SET value=? WHERE key=?', (json.dumps(rewritten), key))
        elif origin == 'file':
            atomic_write_json(root / key, rewritten, backup=False)
        else:
            field = 'variables' if origin == 'match_profile_steps' else 'extra_settings'
            db.execute(f'UPDATE {origin} SET {field}=? WHERE id=?', (json.dumps(rewritten), key))
    configs = db.execute('SELECT channel_id,name,enabled,match_by_tvg_id FROM channel_regex_configs').fetchall()
    patterns = db.execute('SELECT id,channel_id,pattern,m3u_accounts,step_order FROM channel_regex_patterns').fetchall()
    db.execute('DELETE FROM channel_regex_patterns'); db.execute('DELETE FROM channel_regex_configs')
    for identity, name, enabled, tvg in configs:
        target = mappings['channels'].get(identifier(identity))
        if target is not None:
            db.execute('INSERT INTO channel_regex_configs VALUES (?,?,?,?)', (str(target), name, enabled, tvg))
    for identity, channel, pattern, raw, order in patterns:
        target = mappings['channels'].get(identifier(channel))
        providers = json.loads(raw) if raw else None
        rewritten = [mappings['providers'][p] for p in providers if mappings['providers'].get(p) is not None] if providers else providers
        if target is not None and not (providers and not rewritten):
            db.execute('INSERT INTO channel_regex_patterns VALUES (?,?,?,?,?)', (identity, str(target), pattern, json.dumps(rewritten) if rewritten is not None else None, order))
    if foreign:
        for key in RUNTIME_KEYS:
            db.execute('DELETE FROM system_settings WHERE key=?', (key,))
            (root / (key + '.json')).unlink(missing_ok=True)
        # Old in-memory inventories/relations may have stale FK generations.
        for table in ('channel_streams','group_accounts','monitoring_sessions','dead_streams','channels','streams','m3u_account_profiles','m3u_accounts','channel_groups','logos'):
            db.execute(f'DELETE FROM {table}')
