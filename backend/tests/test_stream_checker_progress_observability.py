from unittest.mock import patch

from apps.stream.concurrent_stream_limiter import AccountStreamLimiter
from apps.stream.stream_checker_components import StreamCheckerProgress


class FakeDB:
    def __init__(self):
        self.settings = {}

    def set_system_setting(self, key, value):
        self.settings[key] = value

    def get_system_setting(self, key, default=None):
        return self.settings.get(key, default)


def test_progress_update_builds_provider_progress_counters():
    fake_db = FakeDB()

    with patch('apps.database.manager.get_db_manager', return_value=fake_db):
        progress = StreamCheckerProgress()
        progress.update(
            channel_id=10,
            channel_name='Test Channel',
            current=1,
            total=7,
            status='analyzing',
            streams_detail=[
                {
                    'id': 1,
                    'name': 'A1',
                    'm3u_account': 'Provider A',
                    'm3u_account_id': 5,
                    'status': 'checking',
                },
                {
                    'id': 2,
                    'name': 'A2',
                    'm3u_account': 'Provider A',
                    'm3u_account_id': 5,
                    'status': 'waiting_provider_limit',
                    'reason_detail': 'checking_capacity',
                },
                {
                    'id': 3,
                    'name': 'A3',
                    'm3u_account': 'Provider A',
                    'm3u_account_id': 5,
                    'status': 'provider_limit_wait_timeout',
                    'reason_detail': 'active_viewers',
                },
                {
                    'id': 7,
                    'name': 'A4',
                    'm3u_account': 'Provider A',
                    'm3u_account_id': 5,
                    'status': 'viewer_preempted',
                    'reason_detail': 'viewer_preempted',
                },
                {'id': 4, 'name': 'B1', 'm3u_account': 'Provider B', 'status': 'completed'},
                {'id': 5, 'name': 'B2', 'm3u_account': 'Provider B', 'status': 'pending'},
                {'id': 6, 'name': 'B3', 'm3u_account': 'Provider B', 'status': 'dead'},
            ],
            provider_profile_slots={
                '5': [
                    {
                        'id': 50,
                        'name': 'Primary',
                        'limit': 1,
                        'unlimited': False,
                        'active_viewers': 0,
                        'checking': 1,
                        'used': 1,
                        'available': 0,
                        'full': True,
                    },
                    {
                        'id': 51,
                        'name': 'Sibling',
                        'limit': 1,
                        'unlimited': False,
                        'active_viewers': 0,
                        'checking': 0,
                        'used': 0,
                        'available': 1,
                        'full': False,
                    },
                ],
            },
        )

    saved = fake_db.settings['stream_checker_progress']
    provider_a = next(item for item in saved['provider_progress'] if item['name'] == 'Provider A')
    provider_b = next(item for item in saved['provider_progress'] if item['name'] == 'Provider B')

    assert provider_a['checking'] == 1
    assert provider_a['waiting'] == 1
    assert provider_a['skipped'] == 2
    assert provider_a['finished'] == 2
    assert provider_a['state'] == 'waiting_provider_limit'
    assert provider_a['wait_reason_counts'] == {
        'active_viewers': 1,
        'checking_capacity': 1,
        'viewer_preempted': 1,
    }
    assert provider_a['dominant_wait_reason'] == 'active_viewers'
    assert provider_a['account_id'] == 5
    assert provider_a['capacity_explanation'] == {
        'state': 'viewer_protected',
        'message': 'Real viewer capacity is protected before StreamFlow probes use the slot.',
        'operator_action': 'wait_for_viewer_capacity',
        'primary_reason': 'active_viewers',
        'wait_reason_counts': {
            'active_viewers': 1,
            'checking_capacity': 1,
            'viewer_preempted': 1,
        },
        'capacity_sources': [
            'real_viewers',
            'provider_profile',
            'streamflow_workers',
            'profile_limit',
        ],
        'has_free_profile_slot': True,
        'has_full_profile_slot': True,
        'has_real_viewer_usage': True,
        'has_shadow_watcher_usage': False,
        'has_streamflow_worker_usage': True,
        'has_teamarr_preflight_usage': False,
        'has_quality_check_usage': False,
        'profile_slot_summary': {
            'total': 2,
            'limited': 2,
            'unlimited': 0,
            'full': 1,
            'open': 1,
            'with_real_viewers': 0,
            'with_shadow_watchers': 0,
            'with_streamflow_workers': 1,
            'with_teamarr_preflight': 0,
            'with_quality_checks': 0,
        },
    }
    assert provider_a['profile_slots'] == [
        {
            'id': 50,
            'name': 'Primary',
            'limit': 1,
            'unlimited': False,
            'active_viewers': 0,
            'checking': 1,
            'used': 1,
            'available': 0,
            'full': True,
        },
        {
            'id': 51,
            'name': 'Sibling',
            'limit': 1,
            'unlimited': False,
            'active_viewers': 0,
            'checking': 0,
            'used': 0,
            'available': 1,
            'full': False,
        },
    ]

    assert provider_b['pending'] == 1
    assert provider_b['completed'] == 1
    assert provider_b['failed'] == 1
    assert provider_b['finished'] == 2

    assert saved['provider_summary'] == {
        'total_providers': 2,
        'active_providers': 1,
        'waiting_providers': 1,
        'checking_streams': 1,
        'waiting_streams': 1,
        'pending_streams': 1,
        'completed_streams': 1,
        'skipped_streams': 2,
        'failed_streams': 1,
        'profile_slots_total': 2,
        'profile_slots_full': 1,
        'profile_slots_open': 1,
        'profile_slots_with_real_viewers': 0,
        'profile_slots_with_shadow_watchers': 0,
        'profile_slots_with_streamflow_workers': 1,
        'profile_slots_with_teamarr_preflight': 0,
        'profile_slots_with_quality_checks': 0,
    }


def test_shared_route_alias_rows_count_capacity_once():
    rows = [
        {
            'id': 1,
            'name': 'A1',
            'm3u_account': 'Provider A',
            'm3u_account_id': 5,
            'status': 'checking',
        },
    ]
    shared_route_slots = [
        {
            'id': 50,
            'name': 'Shared route representative',
            'limit': 3,
            'unlimited': False,
            'active_viewers': 2,
            'real_viewers': 1,
            'shadow_watchers': 1,
            'checking': 1,
            'teamarr_preflight': 1,
            'quality_checks': 1,
            'used': 3,
            'available': 0,
            'full': True,
            'shared_route': True,
            'capacity_counted': True,
        },
        {
            'id': 51,
            'name': 'Shared route alias',
            'limit': 3,
            'unlimited': False,
            'active_viewers': 2,
            'real_viewers': 1,
            'shadow_watchers': 1,
            'checking': 1,
            'teamarr_preflight': 1,
            'quality_checks': 1,
            'used': 3,
            'available': 0,
            'full': True,
            'shared_route': True,
            'capacity_counted': False,
        },
    ]

    provider_progress = StreamCheckerProgress._build_provider_progress(
        rows,
        provider_profile_slots={'5': shared_route_slots},
    )

    assert provider_progress[0]['profile_slots'] == shared_route_slots
    assert provider_progress[0]['capacity_explanation']['profile_slot_summary'] == {
        'total': 1,
        'limited': 1,
        'unlimited': 0,
        'full': 1,
        'open': 0,
        'with_real_viewers': 1,
        'with_shadow_watchers': 1,
        'with_streamflow_workers': 1,
        'with_teamarr_preflight': 1,
        'with_quality_checks': 1,
    }
    assert StreamCheckerProgress._build_provider_summary(provider_progress) == {
        'total_providers': 1,
        'active_providers': 1,
        'waiting_providers': 0,
        'checking_streams': 1,
        'waiting_streams': 0,
        'pending_streams': 0,
        'completed_streams': 0,
        'skipped_streams': 0,
        'failed_streams': 0,
        'profile_slots_total': 1,
        'profile_slots_full': 1,
        'profile_slots_open': 0,
        'profile_slots_with_real_viewers': 1,
        'profile_slots_with_shadow_watchers': 1,
        'profile_slots_with_streamflow_workers': 1,
        'profile_slots_with_teamarr_preflight': 1,
        'profile_slots_with_quality_checks': 1,
    }


def test_unusable_profile_route_stays_visible_without_capacity_summary():
    rows = [
        {
            'id': 1,
            'name': 'A1',
            'm3u_account': 'Provider A',
            'm3u_account_id': 5,
            'status': 'pending',
        },
    ]
    unusable_slot = {
        'id': 52,
        'name': 'Unusable profile route',
        'limit': 4,
        'unlimited': False,
        'active_viewers': 0,
        'real_viewers': 0,
        'shadow_watchers': 0,
        'checking': 0,
        'used': 0,
        'available': 4,
        'full': False,
        'capacity_counted': False,
        'route_usable': False,
    }

    provider_progress = StreamCheckerProgress._build_provider_progress(
        rows,
        provider_profile_slots={'5': [unusable_slot]},
    )

    assert provider_progress[0]['profile_slots'] == [unusable_slot]
    assert provider_progress[0]['capacity_explanation']['profile_slot_summary'] == {
        'total': 0,
        'limited': 0,
        'unlimited': 0,
        'full': 0,
        'open': 0,
        'with_real_viewers': 0,
        'with_shadow_watchers': 0,
        'with_streamflow_workers': 0,
        'with_teamarr_preflight': 0,
        'with_quality_checks': 0,
    }
    summary = StreamCheckerProgress._build_provider_summary(provider_progress)
    assert summary['profile_slots_total'] == 0
    assert summary['profile_slots_full'] == 0
    assert summary['profile_slots_open'] == 0

    inconsistent_marker = dict(unusable_slot, capacity_counted=True)
    defensive_explanation = StreamCheckerProgress._build_capacity_explanation(
        {'waiting': 0, 'checking': 0, 'skipped': 0},
        profile_slots=[inconsistent_marker],
    )
    assert defensive_explanation['profile_slot_summary']['total'] == 0
    assert defensive_explanation['profile_slot_summary']['open'] == 0


def test_limiter_shared_route_snapshot_counts_once_in_progress_summary():
    profiles = [
        {
            'id': 20,
            'name': 'Default alias B',
            'max_streams': 1,
            'is_active': True,
            'is_default': True,
        },
        {
            'id': 10,
            'name': 'Default alias A',
            'max_streams': 1,
            'is_active': True,
        },
    ]

    class FakeUDI:
        def get_m3u_account_by_id(self, account_id):
            return {'id': 1, 'profiles': profiles} if account_id == 1 else None

        def get_active_streams_count_per_profile(self, _account_id):
            return {}

    limiter = AccountStreamLimiter(udi_manager=FakeUDI())
    limiter.set_account_limit(1, 1, profiles=profiles)
    snapshot = limiter.get_profile_slot_snapshot(1)

    assert len(snapshot) == 2
    assert [item['id'] for item in snapshot if item['capacity_counted']] == [10]
    provider_progress = StreamCheckerProgress._build_provider_progress(
        [
            {
                'id': 1,
                'name': 'A1',
                'm3u_account': 'Provider A',
                'm3u_account_id': 1,
                'status': 'pending',
            },
        ],
        provider_profile_slots={'1': snapshot},
    )

    assert len(provider_progress[0]['profile_slots']) == 2
    assert provider_progress[0]['capacity_explanation']['profile_slot_summary'] == {
        'total': 1,
        'limited': 1,
        'unlimited': 0,
        'full': 0,
        'open': 1,
        'with_real_viewers': 0,
        'with_shadow_watchers': 0,
        'with_streamflow_workers': 0,
        'with_teamarr_preflight': 0,
        'with_quality_checks': 0,
    }
    summary = StreamCheckerProgress._build_provider_summary(provider_progress)
    assert summary['profile_slots_total'] == 1
    assert summary['profile_slots_open'] == 1


def test_provider_progress_counts_bitrate_rechecks_and_terminal_incomplete_rows():
    rows = [
        {
            'id': 1,
            'name': 'Rechecking',
            'm3u_account': 'Provider A',
            'm3u_account_id': 5,
            'status': 'rechecking_bitrate',
        },
        {
            'id': 2,
            'name': 'Incomplete',
            'm3u_account': 'Provider A',
            'm3u_account_id': 5,
            'status': 'incomplete_bitrate',
        },
    ]

    provider = StreamCheckerProgress._build_provider_progress(rows)[0]
    assert provider['checking'] == 1
    assert provider['incomplete'] == 1
    assert provider['finished'] == 1
    assert provider['state'] == 'checking'

    terminal = StreamCheckerProgress._build_provider_progress(rows[1:])[0]
    assert terminal['checking'] == 0
    assert terminal['incomplete'] == 1
    assert terminal['finished'] == 1
    assert terminal['state'] == 'complete'


def test_progress_update_persists_run_quality_and_capacity_context():
    fake_db = FakeDB()

    with patch('apps.database.manager.get_db_manager', return_value=fake_db):
        progress = StreamCheckerProgress()
        progress.update(
            channel_id=10,
            channel_name='Test Channel',
            current=1,
            total=1,
            status='analyzing',
            run_mode='automation_quality_check',
            run_profile_id='profile-a',
            run_profile_name='Prime',
            run_profile_source='schedule',
            quality_profile_id='quality-a',
            quality_profile_name='Strict Quality',
            quality_profile_source='forced',
            capacity_profile_name='Provider account profiles',
            capacity_profile_source='m3u_account_profiles',
        )

    saved = fake_db.settings['stream_checker_progress']

    assert saved['run_mode'] == 'automation_quality_check'
    assert saved['run_profile_id'] == 'profile-a'
    assert saved['run_profile_name'] == 'Prime'
    assert saved['run_profile_source'] == 'schedule'
    assert saved['quality_profile_id'] == 'quality-a'
    assert saved['quality_profile_name'] == 'Strict Quality'
    assert saved['quality_profile_source'] == 'forced'
    assert saved['capacity_profile_name'] == 'Provider account profiles'
    assert saved['capacity_profile_source'] == 'm3u_account_profiles'


def test_capacity_explanation_marks_viewer_preemption_as_retry_later():
    explanation = StreamCheckerProgress._build_capacity_explanation(
        {
            'checking': 0,
            'waiting': 0,
            'skipped': 1,
        },
        dominant_wait_reason='viewer_preempted',
        profile_slots=[
            {
                'id': 10,
                'name': 'Main',
                'limit': 1,
                'unlimited': False,
                'active_viewers': 1,
                'checking': 0,
                'used': 1,
                'available': 0,
                'full': True,
            },
            {
                'id': 11,
                'name': 'Backup',
                'limit': 2,
                'unlimited': False,
                'active_viewers': 0,
                'checking': 0,
                'used': 0,
                'available': 2,
                'full': False,
            },
        ],
    )

    assert explanation['state'] == 'viewer_preempted'
    assert explanation['operator_action'] == 'retry_later'
    assert explanation['capacity_sources'] == [
        'real_viewers',
        'provider_profile',
        'profile_limit',
    ]
    assert explanation['has_free_profile_slot'] is True
    assert explanation['has_real_viewer_usage'] is True
    assert explanation['profile_slot_summary'] == {
        'total': 2,
        'limited': 2,
        'unlimited': 0,
        'full': 1,
        'open': 1,
        'with_real_viewers': 1,
        'with_shadow_watchers': 0,
        'with_streamflow_workers': 0,
        'with_teamarr_preflight': 0,
        'with_quality_checks': 0,
    }


def test_capacity_explanation_marks_provider_account_timeout_without_profile_slots():
    explanation = StreamCheckerProgress._build_capacity_explanation(
        {
            'checking': 0,
            'waiting': 0,
            'skipped': 1,
        },
        dominant_wait_reason='provider_capacity',
    )

    assert explanation['state'] == 'capacity_timeout'
    assert explanation['message'] == 'Provider account capacity did not free up before the wait timeout.'
    assert explanation['operator_action'] == 'review_capacity_or_retry'
    assert explanation['capacity_sources'] == ['provider_account']
    assert explanation['has_free_profile_slot'] is False
    assert explanation['has_full_profile_slot'] is False
    assert explanation['profile_slot_summary'] == {
        'total': 0,
        'limited': 0,
        'unlimited': 0,
        'full': 0,
        'open': 0,
        'with_real_viewers': 0,
        'with_shadow_watchers': 0,
        'with_streamflow_workers': 0,
        'with_teamarr_preflight': 0,
        'with_quality_checks': 0,
    }


def test_capacity_explanation_keeps_shadow_watcher_out_of_real_viewers():
    explanation = StreamCheckerProgress._build_capacity_explanation(
        {
            'checking': 0,
            'waiting': 1,
            'skipped': 0,
        },
        dominant_wait_reason='shadow_watchers',
        profile_slots=[
            {
                'id': 10,
                'name': 'Shadow Only',
                'limit': 1,
                'unlimited': False,
                'active_viewers': 1,
                'real_viewers': 0,
                'shadow_watchers': 1,
                'checking': 0,
                'used': 1,
                'available': 0,
                'full': True,
            },
        ],
    )

    assert explanation['state'] == 'shadow_watcher_capacity'
    assert explanation['operator_action'] == 'wait_for_shadow_watcher'
    assert explanation['capacity_sources'] == [
        'shadow_watchers',
        'provider_profile',
        'profile_limit',
    ]
    assert explanation['has_real_viewer_usage'] is False
    assert explanation['has_shadow_watcher_usage'] is True
    assert explanation['profile_slot_summary']['with_real_viewers'] == 0
    assert explanation['profile_slot_summary']['with_shadow_watchers'] == 1


def test_capacity_explanation_surfaces_teamarr_and_quality_slot_context():
    explanation = StreamCheckerProgress._build_capacity_explanation(
        {
            'checking': 2,
            'waiting': 0,
            'skipped': 0,
        },
        dominant_wait_reason='checking_capacity',
        profile_slots=[
            {
                'id': 20,
                'name': 'Teamarr',
                'limit': 1,
                'unlimited': False,
                'active_viewers': 0,
                'real_viewers': 0,
                'shadow_watchers': 0,
                'checking': 1,
                'teamarr_preflight': 1,
                'used': 1,
                'available': 0,
                'full': True,
            },
            {
                'id': 21,
                'name': 'Quality',
                'limit': 1,
                'unlimited': False,
                'active_viewers': 0,
                'real_viewers': 0,
                'shadow_watchers': 0,
                'checking': 1,
                'quality_checks': 1,
                'used': 1,
                'available': 0,
                'full': True,
            },
        ],
    )

    assert explanation['has_teamarr_preflight_usage'] is True
    assert explanation['has_quality_check_usage'] is True
    assert explanation['capacity_sources'] == [
        'streamflow_workers',
        'provider_profile',
        'teamarr_preflight',
        'quality_checks',
        'profile_limit',
    ]
    assert explanation['profile_slot_summary']['with_streamflow_workers'] == 2
    assert explanation['profile_slot_summary']['with_teamarr_preflight'] == 1
    assert explanation['profile_slot_summary']['with_quality_checks'] == 1
