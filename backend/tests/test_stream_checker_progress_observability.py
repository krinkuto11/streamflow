from unittest.mock import patch

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
        'has_streamflow_worker_usage': True,
        'profile_slot_summary': {
            'total': 2,
            'limited': 2,
            'unlimited': 0,
            'full': 1,
            'open': 1,
            'with_real_viewers': 0,
            'with_streamflow_workers': 1,
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
        'profile_slots_with_streamflow_workers': 1,
    }


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
        'with_streamflow_workers': 0,
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
        'with_streamflow_workers': 0,
    }
