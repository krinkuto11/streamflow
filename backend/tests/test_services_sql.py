import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

# Add backend to path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from apps.database.connection import get_session, init_db
from apps.database.models import SystemSetting
from apps.automation.automated_stream_manager import RegexChannelMatcher, AutomatedStreamManager
from apps.automation.scheduling_service import DEFAULT_UDI_REFRESH_INTERVAL_MINUTES, SchedulingService

class TestServicesSQL(unittest.TestCase):
    
    @classmethod
    def setUpClass(cls):
        # Use a temporary test database
        os.environ['DATABASE_URL'] = 'sqlite:///:memory:'
        init_db()
        
    def setUp(self):
        # Clear SystemSetting table before each test
        session = get_session()
        session.query(SystemSetting).delete()
        session.commit()
        session.close()

    def test_regex_channel_matcher_sql(self):
        matcher = RegexChannelMatcher()
        
        # Test Save
        test_patterns = {
            "patterns": {
                "1": {"name": "CNN", "regex_patterns": [{"pattern": ".*CNN.*"}], "enabled": True}
            }
        }
        matcher._save_patterns(test_patterns)
        
        # Verify in DB
        session = get_session()
        setting = session.query(SystemSetting).filter(SystemSetting.key == 'channel_regex_config').first()
        self.assertIsNotNone(setting)
        self.assertEqual(setting.value['patterns']['1']['name'], 'CNN')
        session.close()
        
        # Test Load
        loaded = matcher._load_patterns()
        self.assertEqual(loaded['patterns']['1']['name'], 'CNN')

    def test_automated_stream_manager_state_sql(self):
        manager = AutomatedStreamManager()
        
        # Test Save State
        now = datetime.now()
        manager.period_last_run = {"period_1": now}
        manager._scheduler_retry_state = {
            "period_1": {
                "attempt": 1,
                "max_attempts": 3,
                "next_retry_at": (now + timedelta(minutes=5)).isoformat(),
                "pending_channel_ids": [101],
                "exhausted": False,
            },
        }
        manager._save_state()
        
        # Verify in DB
        session = get_session()
        setting = session.query(SystemSetting).filter(SystemSetting.key == 'automation_state').first()
        self.assertIsNotNone(setting)
        self.assertIn("period_1", setting.value['period_last_run'])
        self.assertEqual(
            setting.value['scheduler_retry_state']['period_1']['pending_channel_ids'],
            [101],
        )
        session.close()
        
        # Test Load State
        loaded_runs = manager._load_state()
        self.assertIn("period_1", loaded_runs)
        self.assertTrue(isinstance(loaded_runs["period_1"], datetime))
        # Account for microsecond truncation in string conversion if any, but should be close
        self.assertEqual(loaded_runs["period_1"].isoformat(), now.isoformat())
        self.assertEqual(
            manager._scheduler_retry_state["period_1"]["next_retry_at"],
            (now + timedelta(minutes=5)).isoformat(),
        )

    def test_scheduling_service_sql(self):
        service = SchedulingService()
        
        # 1. Test Config Save/Load
        service._config = {'epg_refresh_interval_minutes': 30, 'enabled': False}
        service._save_config()
        
        # Verify in DB
        session = get_session()
        setting = session.query(SystemSetting).filter(SystemSetting.key == 'scheduling_config').first()
        self.assertIsNotNone(setting)
        self.assertEqual(setting.value['epg_refresh_interval_minutes'], 30)
        session.close()
        
        # Test Load
        loaded_config = service._load_config()
        self.assertEqual(loaded_config['epg_refresh_interval_minutes'], 30)

        # 2. Test Scheduled Events Save/Load
        test_event = {
            'id': 'test-uuid-1',
            'channel_id': 101,
            'program_title': 'Test Program',
            'check_time': datetime.now(timezone.utc).isoformat()
        }
        service._scheduled_events = [test_event]
        service._save_scheduled_events()
        
        # Test Load
        loaded_events = service._load_scheduled_events()
        self.assertEqual(len(loaded_events), 1)
        self.assertEqual(loaded_events[0]['id'], 'test-uuid-1')

    def test_scheduling_service_defaults_udi_refresh_to_240_minutes(self):
        service = SchedulingService()

        self.assertEqual(
            service.get_udi_refresh_schedule(),
            {'type': 'interval', 'value': DEFAULT_UDI_REFRESH_INTERVAL_MINUTES}
        )

    def test_scheduling_service_migrates_missing_udi_refresh_default(self):
        session = get_session()
        setting = SystemSetting(
            key='scheduling_config',
            value={'epg_schedule': {'type': 'interval', 'value': 60}, 'enabled': True}
        )
        session.add(setting)
        session.commit()
        session.close()

        service = SchedulingService()

        self.assertEqual(
            service.get_udi_refresh_schedule(),
            {'type': 'interval', 'value': DEFAULT_UDI_REFRESH_INTERVAL_MINUTES}
        )
        session = get_session()
        setting = session.query(SystemSetting).filter(SystemSetting.key == 'scheduling_config').first()
        self.assertEqual(
            setting.value['udi_refresh_schedule'],
            {'type': 'interval', 'value': DEFAULT_UDI_REFRESH_INTERVAL_MINUTES}
        )
        session.close()

    def test_scheduling_service_preserves_disabled_udi_refresh(self):
        session = get_session()
        setting = SystemSetting(
            key='scheduling_config',
            value={
                'epg_schedule': {'type': 'interval', 'value': 60},
                'udi_refresh_schedule': None,
                'enabled': True
            }
        )
        session.add(setting)
        session.commit()
        session.close()

        service = SchedulingService()

        self.assertIsNone(service.get_udi_refresh_schedule())

if __name__ == '__main__':
    unittest.main()
