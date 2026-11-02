"""Provider diversity must not leave usable channel slots empty."""
from collections import Counter
from copy import deepcopy

import pytest

from apps.automation.automation_config_manager import AutomationConfigManager
from apps.stream.stream_checker_service import StreamCheckerService


PROVIDERS = {sid: 10 if sid <= 8 else 20 if sid <= 12 else 30
             for sid in range(1, 17)}


@pytest.mark.parametrize('limit,cap,ids,expected', [
    (8, 3, list(range(1, 17)), [1, 2, 3, 9, 10, 11, 13, 14]),
    (8, 3, list(range(1, 13)), [1, 2, 3, 4, 5, 9, 10, 11]),
    (8, 3, list(range(1, 9)), list(range(1, 9))),
    (8, 3, [1, 2, 3, 9, 10], [1, 2, 3, 9, 10]),
    (8, 0, list(range(1, 17)), list(range(1, 9))),
    (0, 3, list(range(1, 17)), list(range(1, 17))),
    (4, 9, list(range(1, 17)), [1, 2, 3, 4]),
    (4, 1, [1, 1, 2, 9, 9, 13, 14], [1, 2, 9, 13]),
])
def test_provider_selection_fills_in_existing_rank_order(limit, cap, ids, expected):
    result = StreamCheckerService._limit_write_back_stream_ids(
        ids, limit, set(), provider_stream_limit=cap, stream_provider_ids=PROVIDERS,
    )
    assert result == expected


@pytest.mark.parametrize('protected,limit,expected', [
    ({7, 8}, 4, [7, 8, 9, 13]),
    ({6, 7, 8}, 2, [6, 7, 8]),
    ({16}, 4, [1, 2, 9, 16]),
])
def test_active_viewers_reserve_slots_and_provider_quota(protected, limit, expected):
    result = StreamCheckerService._limit_write_back_stream_ids(
        list(range(1, 17)), limit, protected,
        provider_stream_limit=1, stream_provider_ids=PROVIDERS,
    )
    assert result == expected
    assert protected <= set(result)


def test_unknown_sources_can_fill_but_do_not_invent_provider_diversity():
    result = StreamCheckerService._limit_write_back_stream_ids(
        [1, 2, 3, 4, 5, 6], 4, set(), provider_stream_limit=1,
        stream_provider_ids={1: None, 2: 'custom', 3: '010', 4: 10, 5: '20', 6: 20},
    )
    assert result == [1, 2, 3, 5]


def test_single_provider_backfill_still_truncates_to_the_best_eight():
    assert StreamCheckerService._limit_write_back_stream_ids(
        list(range(1, 11)), 8, set(), provider_stream_limit=3,
        stream_provider_ids={sid: 10 for sid in range(1, 11)},
    ) == list(range(1, 9))


@pytest.mark.parametrize('cap', [None, 'invalid', -2, float('inf')])
def test_invalid_provider_caps_keep_existing_global_limit_behavior(cap):
    assert StreamCheckerService._limit_write_back_stream_ids(
        list(range(1, 17)), 8, set(), provider_stream_limit=cap,
        stream_provider_ids=PROVIDERS,
    ) == list(range(1, 9))


def test_profile_selection_excludes_dead_candidates_before_filling():
    service = StreamCheckerService.__new__(StreamCheckerService)
    streams = [{'id': sid, 'm3u_account_id': provider} for sid, provider in PROVIDERS.items()]
    ranked = [{'stream_id': sid, 'score': 1 - sid / 20} for sid in range(1, 17)]
    original = deepcopy(ranked)
    selected = service._select_profile_streams(
        ranked, streams, list(range(1, 17)), set(), 8, 3, {9, 10, 11, 12},
    )
    assert [item['stream_id'] for item in selected] == [1, 2, 3, 4, 5, 13, 14, 15]
    assert ranked == original


def test_profile_selection_accounts_for_protected_viewer_before_truncating():
    service = StreamCheckerService.__new__(StreamCheckerService)
    streams = [{'id': sid, 'm3u_account_id': provider} for sid, provider in PROVIDERS.items()]
    ranked = [{'stream_id': sid} for sid in range(1, 17) if sid not in {7, 8}]
    selected = service._select_profile_streams(
        ranked, streams, list(range(1, 17)), {7, 8}, 4, 1, set(),
    )
    assert [item['stream_id'] for item in selected] == [9, 13]


def test_provider_identity_uses_account_ids_instead_of_names_or_profile_ids():
    service = StreamCheckerService.__new__(StreamCheckerService)
    streams = [
        {'id': 1, 'm3u_account_id': '10', 'm3u_account': 'Same name', 'profile_id': 101},
        {'id': 2, 'm3u_account_id': 10, 'm3u_account': 'Other name', 'profile_id': 102},
        {'id': 3, 'm3u_account': '20', 'profile_id': 101},
        {'id': 4, 'm3u_account_id': 30, 'm3u_account': 'Same name'},
    ]
    selected = service._select_profile_streams(
        [{'stream_id': sid} for sid in [1, 2, 3, 4]], streams, [1, 2, 3, 4],
        set(), 3, 1, set(),
    )
    assert [item['stream_id'] for item in selected] == [1, 3, 4]


def test_soft_cap_preserves_existing_quality_and_playlist_ranking():
    # This order represents the already evaluated priority/quality sort keys;
    # diversification may select lower-ranked providers, but never reorder them.
    ranked_ids = [13, 14, 9, 10, 1, 2, 3, 4]
    result = StreamCheckerService._limit_write_back_stream_ids(
        ranked_ids, 5, set(), provider_stream_limit=1, stream_provider_ids=PROVIDERS,
    )
    assert result == [13, 14, 9, 10, 1]
    assert Counter(PROVIDERS[sid] for sid in result) == {30: 2, 20: 2, 10: 1}


def test_provider_cap_round_trips_in_sql_profile_without_schema_migration():
    manager = AutomationConfigManager()
    payload = {'name': 'Provider cap', 'stream_checking': {
        'enabled': True, 'stream_limit': 8, 'provider_stream_limit': '3',
    }}
    original = deepcopy(payload)
    profile_id = manager.create_profile(payload)
    assert profile_id
    profile = manager.get_profile(profile_id)
    assert profile['stream_checking']['provider_stream_limit'] == 3
    assert profile['stream_checking']['stream_limit'] == 8
    assert payload == original
    assert manager.update_profile(profile_id, {'stream_checking': {
        **profile['stream_checking'], 'provider_stream_limit': 2,
    }})
    assert manager.get_profile(profile_id)['stream_checking']['provider_stream_limit'] == 2


@pytest.mark.parametrize('cap,expected', [('invalid', 0), (-3, 0), (None, 0), ('4', 4)])
def test_profile_normalizes_provider_cap(cap, expected):
    manager = AutomationConfigManager()
    profile_id = manager.create_profile({'name': 'Normalize', 'stream_checking': {
        'stream_limit': 8, 'provider_stream_limit': cap,
    }})
    assert manager.get_profile(profile_id)['stream_checking']['provider_stream_limit'] == expected


def test_old_profile_defaults_to_disabled_provider_cap():
    from apps.database.connection import get_session
    from apps.database.models import AutomationProfile

    session = get_session()
    row = AutomationProfile(name='Legacy', extra_settings={'stream_checking': {'stream_limit': 8}})
    session.add(row)
    session.commit()
    profile_id = str(row.id)
    session.close()
    manager = AutomationConfigManager()
    checking = manager.get_profile(profile_id)['stream_checking']
    assert checking['provider_stream_limit'] == 0
    assert checking['stream_limit'] == 8
