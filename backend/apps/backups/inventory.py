"""Secret-free identity snapshots and authoritative read-only Dispatcharr fetches."""

import hashlib
import json
import re
from concurrent.futures import ThreadPoolExecutor

KINDS = ('providers', 'channels', 'groups', 'channel_profiles', 'streams')
INVENTORY_FILE = 'restore_inventory.json'
REVIEW_KEY = 'backup_restore_review_pending'


def fingerprint(value):
    return hashlib.sha256(str(value).encode()).hexdigest() if value else None


def identifier(value):
    if isinstance(value, dict):
        value = value.get('id')
    if type(value) is int and value > 0:
        return value
    if isinstance(value, str) and value.isdecimal() and int(value) > 0:
        return int(value)
    return None


def normalize_inventory(*, providers, channels, groups, channel_profiles, streams):
    result = {'format_version': 1}
    for kind, rows in zip(KINDS, (providers, channels, groups, channel_profiles, streams)):
        result[kind] = []
        for row in rows:
            identity = identifier(row.get('id'))
            if identity is None:
                raise ValueError('Dispatcharr inventory contains an invalid identifier')
            # Never include URLs or credentials in list/preview payloads.
            item = {'id': identity, 'name': str(row.get('name') or '')[:512]}
            if kind == 'providers':
                address = row.get('server_url') or row.get('file_path')
                item['identity'] = fingerprint(address)
            elif kind == 'channels':
                item.update(identity=fingerprint(row.get('uuid')), uuid=str(row.get('uuid') or '')[:100],
                            tvg_id=str(row.get('tvg_id') or '')[:100])
            elif kind == 'streams':
                item.update(identity=fingerprint(row.get('url')), provider_id=identifier(row.get('m3u_account_id') or row.get('m3u_account')))
            else:
                item['identity'] = None  # Names are suggestions, never identity proof.
            result[kind].append(item)
    validate_inventory(result)
    return result


def validate_inventory(value):
    if not isinstance(value, dict) or set(value) != {'format_version', *KINDS} or value['format_version'] != 1:
        raise ValueError('Invalid Dispatcharr identity snapshot')
    for kind in KINDS:
        rows = value[kind]
        if not isinstance(rows, list) or len(rows) > 200000:
            raise ValueError('Dispatcharr identity snapshot exceeds its limit')
        seen = set()
        for item in rows:
            if not isinstance(item, dict) or set(item) - {'id','name','identity','uuid','tvg_id','provider_id'}:
                raise ValueError('Invalid Dispatcharr identity record')
            identity = item.get('id')
            if type(identity) is not int or identity < 1 or identity in seen:
                raise ValueError('Invalid or duplicate Dispatcharr identity')
            seen.add(identity)
            if not isinstance(item.get('name'), str) or len(item['name']) > 512:
                raise ValueError('Invalid Dispatcharr identity label')
            if item.get('identity') is not None and (not isinstance(item['identity'], str) or not re.fullmatch('[a-f0-9]{64}', item['identity'])):
                raise ValueError('Invalid Dispatcharr fingerprint')
            for field in ('uuid', 'tvg_id'):
                if field in item and (not isinstance(item[field], str) or len(item[field]) > 100):
                    raise ValueError('Invalid Dispatcharr channel identity')
            if 'provider_id' in item and item['provider_id'] is not None and (type(item['provider_id']) is not int or item['provider_id'] < 1):
                raise ValueError('Invalid Dispatcharr provider identity')
    return value


def inventory_token(value, connection_identity=''):
    canonical = {**value, **{kind: sorted(value[kind], key=lambda row: row['id']) for kind in KINDS}}
    return fingerprint(json.dumps(canonical, sort_keys=True, separators=(',', ':')) + connection_identity)


def capture_inventory():
    from apps.udi.manager import get_udi_manager
    udi = get_udi_manager()
    with udi._lock:
        if not udi._initialized:
            return None
        return normalize_inventory(providers=udi._m3u_accounts_cache, channels=udi._channels_cache,
                                   groups=udi._channel_groups_cache, channel_profiles=udi._channel_profiles_cache,
                                   streams=udi._streams_cache)


def fetch_inventory():
    from apps.udi.fetcher import UDIFetcher
    from apps.config.dispatcharr_config import get_dispatcharr_config
    if not get_dispatcharr_config().is_configured():
        raise ValueError('Configure the target Dispatcharr connection first')

    class StrictFetcher(UDIFetcher):
        def _fetch_url(self, url):
            result = super()._fetch_url(url)
            if result is None:
                raise ValueError('Dispatcharr inventory could not be fetched completely; automation remains paused')
            if isinstance(result, dict) and (not isinstance(result.get('results'), list)
                    or type(result.get('count')) is not int or result['count'] < 0):
                raise ValueError('Dispatcharr returned invalid paginated inventory')
            return result

    fetcher = StrictFetcher()
    with ThreadPoolExecutor(max_workers=5) as pool:
        jobs = {
            'channels': pool.submit(fetcher.fetch_channels),
            'streams': pool.submit(fetcher.fetch_streams),
            'providers': pool.submit(fetcher._fetch_url, fetcher.base_url + '/api/m3u/accounts/'),
            'groups': pool.submit(fetcher._fetch_url, fetcher.base_url + '/api/channels/groups/'),
            'channel_profiles': pool.submit(fetcher._fetch_url, fetcher.base_url + '/api/channels/profiles/'),
        }
        values = {key: job.result() for key, job in jobs.items()}
    for key in ('channels', 'streams'):
        value = values[key]
        if value.expected_count is not None and len(value) != value.expected_count:
            raise ValueError('Dispatcharr returned incomplete inventory; retry the comparison')
        values[key] = value.items
    if any(not isinstance(value, list) for value in values.values()):
        raise ValueError('Dispatcharr returned invalid inventory')
    return normalize_inventory(**values)
