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
                {'id': 1, 'name': 'A1', 'm3u_account': 'Provider A', 'status': 'checking'},
                {
                    'id': 2,
                    'name': 'A2',
                    'm3u_account': 'Provider A',
                    'status': 'waiting_provider_limit',
                    'reason_detail': 'checking_capacity',
                },
                {
                    'id': 3,
                    'name': 'A3',
                    'm3u_account': 'Provider A',
                    'status': 'provider_limit_wait_timeout',
                    'reason_detail': 'active_viewers',
                },
                {
                    'id': 7,
                    'name': 'A4',
                    'm3u_account': 'Provider A',
                    'status': 'viewer_preempted',
                    'reason_detail': 'viewer_preempted',
                },
                {'id': 4, 'name': 'B1', 'm3u_account': 'Provider B', 'status': 'completed'},
                {'id': 5, 'name': 'B2', 'm3u_account': 'Provider B', 'status': 'pending'},
                {'id': 6, 'name': 'B3', 'm3u_account': 'Provider B', 'status': 'dead'},
            ],
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
    }
