#!/usr/bin/env python3
"""
Tests for Automation Periods functionality
"""

import sys
import os
import tempfile
import json
from pathlib import Path
from unittest.mock import patch
from flask import Flask

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from apps.automation.automation_config_manager import AutomationConfigManager, CONFIG_DIR, AUTOMATION_CONFIG_FILE
from apps.api.automation_handlers import get_group_configuration_summary_response


def test_automation_periods_creation():
    """Test creating automation periods"""
    print("Test 1: Creating automation periods")
    
    # Use temporary directory for testing
    with tempfile.TemporaryDirectory() as tmpdir:
        # Override config paths
        import apps.automation.automation_config_manager
        original_config_dir = automation_config_manager.CONFIG_DIR
        original_config_file = automation_config_manager.AUTOMATION_CONFIG_FILE
        automation_config_manager.CONFIG_DIR = Path(tmpdir)
        automation_config_manager.AUTOMATION_CONFIG_FILE = Path(tmpdir) / 'test_automation_config.json'
        
        try:
            # Create manager
            manager = AutomationConfigManager()
            
            # Create a profile first (periods require profiles)
            profile_data = {
                "name": "Test Profile",
                "description": "Test profile for periods",
                "m3u_update": {"enabled": True, "playlists": []},
                "stream_matching": {"enabled": True, "playlists": []},
                "stream_checking": {"enabled": True}
            }
            profile_id = manager.create_profile(profile_data)
            assert profile_id is not None, "Failed to create profile"
            print(f"✓ Created profile: {profile_id}")
            
            # Create an automation period (no profile_id - profiles are per-channel now)
            period_data = {
                "name": "Test Period",
                "schedule": {"type": "interval", "value": 60}
            }
            period_id = manager.create_period(period_data)
            assert period_id is not None, "Failed to create period"
            print(f"✓ Created period: {period_id}")
            
            # Verify period was created
            period = manager.get_period(period_id)
            assert period is not None, "Period not found"
            assert period['name'] == "Test Period", "Period name mismatch"
            assert period['schedule']['type'] == "interval", "Schedule type mismatch"
            assert period['schedule']['value'] == 60, "Schedule value mismatch"
            assert 'profile_id' not in period, "Period should not have profile_id (profiles are per-channel now)"
            print("✓ Period details verified")
            
            # List all periods
            all_periods = manager.get_all_periods()
            assert len(all_periods) == 1, f"Expected 1 period, got {len(all_periods)}"
            print(f"✓ Listed periods: {len(all_periods)}")
            
            print("✅ Test 1 passed\n")
            
        finally:
            # Restore original config
            automation_config_manager.CONFIG_DIR = original_config_dir
            automation_config_manager.AUTOMATION_CONFIG_FILE = original_config_file


def test_automation_periods_channel_assignment():
    """Test assigning periods to channels"""
    print("Test 2: Assigning periods to channels")
    
    with tempfile.TemporaryDirectory() as tmpdir:
        import apps.automation.automation_config_manager
        original_config_dir = automation_config_manager.CONFIG_DIR
        original_config_file = automation_config_manager.AUTOMATION_CONFIG_FILE
        automation_config_manager.CONFIG_DIR = Path(tmpdir)
        automation_config_manager.AUTOMATION_CONFIG_FILE = Path(tmpdir) / 'test_automation_config.json'
        
        try:
            manager = AutomationConfigManager()
            
            # Create profile and period (no profile_id in period)
            profile_id = manager.create_profile({"name": "Test Profile"})
            period_id = manager.create_period({
                "name": "Test Period",
                "schedule": {"type": "interval", "value": 30}
            })
            print(f"✓ Created period: {period_id}")
            
            # Assign period to channels WITH profile_id (new model)
            channel_ids = [1, 2, 3]
            result = manager.assign_period_to_channels(period_id, channel_ids, profile_id, replace=False)
            assert result is True, "Failed to assign period to channels"
            print(f"✓ Assigned period with profile to {len(channel_ids)} channels")
            
            # Verify assignments (now returns dict of period_id -> profile_id)
            for cid in channel_ids:
                period_assignments = manager.get_channel_periods(cid)
                assert isinstance(period_assignments, dict), f"Expected dict for channel {cid}"
                assert period_id in period_assignments, f"Period not assigned to channel {cid}"
                assert period_assignments[period_id] == profile_id, f"Profile mismatch for channel {cid}"
            print("✓ Verified all channel assignments with correct profiles")
            
            # Get channels for period
            assigned_channels = manager.get_period_channels(period_id)
            assert set(assigned_channels) == set(channel_ids), "Channel list mismatch"
            print(f"✓ Period has {len(assigned_channels)} channels assigned")
            
            # Remove period from one channel
            result = manager.remove_period_from_channels(period_id, [1])
            assert result is True, "Failed to remove period from channel"
            
            remaining_channels = manager.get_period_channels(period_id)
            assert 1 not in remaining_channels, "Channel 1 still has period"
            assert set(remaining_channels) == {2, 3}, "Remaining channels mismatch"
            print("✓ Removed period from channel 1")
            
            print("✅ Test 2 passed\n")
            
        finally:
            automation_config_manager.CONFIG_DIR = original_config_dir
            automation_config_manager.AUTOMATION_CONFIG_FILE = original_config_file


def test_automation_profile_can_be_assigned_to_multiple_groups():
    """Test assigning one automation profile to multiple groups."""
    with tempfile.TemporaryDirectory() as tmpdir:
        import apps.automation.automation_config_manager
        original_config_dir = automation_config_manager.CONFIG_DIR
        original_config_file = automation_config_manager.AUTOMATION_CONFIG_FILE
        automation_config_manager.CONFIG_DIR = Path(tmpdir)
        automation_config_manager.AUTOMATION_CONFIG_FILE = Path(tmpdir) / 'test_automation_config.json'

        try:
            manager = AutomationConfigManager()
            profile_id = manager.create_profile({"name": "Group Profile"})

            assert manager.assign_profile_to_groups([11, 12, 13], profile_id) is True
            assert manager.get_all_group_assignments() == {
                "11": profile_id,
                "12": profile_id,
                "13": profile_id,
            }

            assert manager.assign_profile_to_groups([12, 13], None) is True
            assert manager.get_all_group_assignments() == {"11": profile_id}
        finally:
            automation_config_manager.CONFIG_DIR = original_config_dir
            automation_config_manager.AUTOMATION_CONFIG_FILE = original_config_file


def test_group_configuration_summary_returns_all_group_settings():
    """Test the group summary endpoint returns profile, period, EPG, and matching settings."""
    with tempfile.TemporaryDirectory() as tmpdir:
        import apps.automation.automation_config_manager
        original_config_dir = automation_config_manager.CONFIG_DIR
        original_config_file = automation_config_manager.AUTOMATION_CONFIG_FILE
        automation_config_manager.CONFIG_DIR = Path(tmpdir)
        automation_config_manager.AUTOMATION_CONFIG_FILE = Path(tmpdir) / 'test_automation_config.json'

        class MatcherStub:
            def reload_patterns(self):
                return None

            def _get_group_patterns(self):
                return {
                    "42": {
                        "name": "Sports",
                        "enabled": True,
                        "match_by_tvg_id": True,
                        "regex_patterns": [{"pattern": ".*Sports.*", "m3u_accounts": None}],
                    }
                }

        try:
            manager = AutomationConfigManager()
            profile_id = manager.create_profile({"name": "Event Profile"})
            epg_profile_id = manager.create_profile({"name": "EPG Profile"})
            period_id = manager.create_period({
                "name": "Prime Time",
                "schedule": {"type": "interval", "value": 30}
            })
            assert manager.assign_profile_to_groups([42], profile_id) is True
            assert manager.assign_epg_scheduled_profile_to_group(42, epg_profile_id) is True
            assert manager.assign_period_to_groups(period_id, [42], profile_id) is True

            app = Flask(__name__)
            with app.app_context():
                response, status_code = get_group_configuration_summary_response(
                    get_automation_config_manager=lambda: manager,
                    get_regex_matcher=lambda: MatcherStub(),
                )

            assert status_code == 200
            payload = response.get_json()
            assert payload["42"]["profile_id"] == profile_id
            assert payload["42"]["epg_profile_id"] == epg_profile_id
            assert payload["42"]["periods"][0]["id"] == period_id
            assert payload["42"]["periods"][0]["profile_name"] == "Event Profile"
            assert payload["42"]["matching"]["match_by_tvg_id"] is True
        finally:
            automation_config_manager.CONFIG_DIR = original_config_dir
            automation_config_manager.AUTOMATION_CONFIG_FILE = original_config_file


def test_period_channel_profiles_do_not_initialize_udi_for_explicit_assignments():
    """Explicit period channel assignments should not trigger UDI auto-init."""
    with tempfile.TemporaryDirectory() as tmpdir:
        import apps.automation.automation_config_manager
        original_config_dir = automation_config_manager.CONFIG_DIR
        original_config_file = automation_config_manager.AUTOMATION_CONFIG_FILE
        automation_config_manager.CONFIG_DIR = Path(tmpdir)
        automation_config_manager.AUTOMATION_CONFIG_FILE = Path(tmpdir) / 'test_automation_config.json'

        class UninitializedUdi:
            def is_initialized(self):
                return False

            def get_channels(self):
                raise AssertionError("period profile lookup must not initialize UDI")

            def get_channels_by_group(self, group_id):
                raise AssertionError("group lookup requires initialized UDI cache")

        try:
            manager = AutomationConfigManager()
            profile_id = manager.create_profile({"name": "Test Profile"})
            period_id = manager.create_period({
                "name": "Test Period",
                "schedule": {"type": "interval", "value": 30}
            })
            assert manager.assign_period_to_channels(period_id, [1, 2], profile_id) is True

            with patch('apps.udi.get_udi_manager', return_value=UninitializedUdi()):
                effective = manager.get_effective_period_channel_profiles(period_id)

            assert effective == {1: profile_id, 2: profile_id}
        finally:
            automation_config_manager.CONFIG_DIR = original_config_dir
            automation_config_manager.AUTOMATION_CONFIG_FILE = original_config_file


def test_automation_periods_update_delete():
    """Test updating and deleting periods"""
    print("Test 3: Updating and deleting periods")
    
    with tempfile.TemporaryDirectory() as tmpdir:
        import apps.automation.automation_config_manager
        original_config_dir = automation_config_manager.CONFIG_DIR
        original_config_file = automation_config_manager.AUTOMATION_CONFIG_FILE
        automation_config_manager.CONFIG_DIR = Path(tmpdir)
        automation_config_manager.AUTOMATION_CONFIG_FILE = Path(tmpdir) / 'test_automation_config.json'
        
        try:
            manager = AutomationConfigManager()
            
            # Create profile and period (no profile_id in period)
            profile_id = manager.create_profile({"name": "Test Profile"})
            period_id = manager.create_period({
                "name": "Original Name",
                "schedule": {"type": "interval", "value": 45}
            })
            print(f"✓ Created period: {period_id}")
            
            # Update period (no profile_id to update)
            result = manager.update_period(period_id, {
                "name": "Updated Name",
                "schedule": {"type": "cron", "value": "*/30 * * * *"}
            })
            assert result is True, "Failed to update period"
            
            # Verify update
            period = manager.get_period(period_id)
            assert period['name'] == "Updated Name", "Name not updated"
            assert period['schedule']['type'] == "cron", "Schedule type not updated"
            assert period['schedule']['value'] == "*/30 * * * *", "Schedule value not updated"
            print("✓ Period updated successfully")
            
            # Assign to channels (with profile_id)
            manager.assign_period_to_channels(period_id, [1, 2], profile_id)
            print("✓ Assigned period to channels")
            
            # Delete period
            result = manager.delete_period(period_id)
            assert result is True, "Failed to delete period"
            
            # Verify deletion
            period = manager.get_period(period_id)
            assert period is None, "Period still exists after deletion"
            
            # Verify channel assignments were removed
            periods_ch1 = manager.get_channel_periods(1)
            periods_ch2 = manager.get_channel_periods(2)
            assert period_id not in periods_ch1, "Period still assigned to channel 1"
            assert period_id not in periods_ch2, "Period still assigned to channel 2"
            print("✓ Period deleted and assignments cleaned up")
            
            print("✅ Test 3 passed\n")
            
        finally:
            automation_config_manager.CONFIG_DIR = original_config_dir
            automation_config_manager.AUTOMATION_CONFIG_FILE = original_config_file


def test_effective_configuration():
    """Test get_effective_configuration method"""
    print("Test 4: Testing effective configuration")
    
    with tempfile.TemporaryDirectory() as tmpdir:
        import apps.automation.automation_config_manager
        original_config_dir = automation_config_manager.CONFIG_DIR
        original_config_file = automation_config_manager.AUTOMATION_CONFIG_FILE
        automation_config_manager.CONFIG_DIR = Path(tmpdir)
        automation_config_manager.AUTOMATION_CONFIG_FILE = Path(tmpdir) / 'test_automation_config.json'
        
        try:
            manager = AutomationConfigManager()
            
            # Create profile and period (no profile_id in period)
            profile_id = manager.create_profile({
                "name": "Test Profile",
                "stream_matching": {"enabled": True}
            })
            period_id = manager.create_period({
                "name": "Test Period",
                "schedule": {"type": "interval", "value": 60}
            })
            
            # Channel without period - should return None
            config = manager.get_effective_configuration(100)
            assert config is None, "Expected None for channel without periods"
            print("✓ Returns None for channel without periods")
            
            # Assign period to channel (with profile_id)
            manager.assign_period_to_channels(period_id, [100], profile_id)
            
            # Channel with period - should return configuration
            config = manager.get_effective_configuration(100)
            assert config is not None, "Expected configuration for channel with period"
            assert config['source'] == 'period', "Source should be 'period'"
            assert config['period_id'] == period_id, "Period ID mismatch"
            assert config['profile'] is not None, "Profile should be included"
            assert config['profile']['id'] == profile_id, "Profile ID mismatch"
            print("✓ Returns correct configuration for channel with period")
            
            print("✅ Test 4 passed\n")
            
        finally:
            automation_config_manager.CONFIG_DIR = original_config_dir
            automation_config_manager.AUTOMATION_CONFIG_FILE = original_config_file


def test_multiple_periods_per_channel():
    """Test assigning multiple periods to a single channel"""
    print("Test 5: Multiple periods per channel")
    
    with tempfile.TemporaryDirectory() as tmpdir:
        import apps.automation.automation_config_manager
        original_config_dir = automation_config_manager.CONFIG_DIR
        original_config_file = automation_config_manager.AUTOMATION_CONFIG_FILE
        automation_config_manager.CONFIG_DIR = Path(tmpdir)
        automation_config_manager.AUTOMATION_CONFIG_FILE = Path(tmpdir) / 'test_automation_config.json'
        
        try:
            manager = AutomationConfigManager()
            
            # Create profiles (we'll use different profiles for each period)
            profile1_id = manager.create_profile({"name": "Profile 1"})
            profile2_id = manager.create_profile({"name": "Profile 2"})
            profile3_id = manager.create_profile({"name": "Profile 3"})
            
            # Create multiple periods (no profile_id in periods)
            period1_id = manager.create_period({
                "name": "Period 1",
                "schedule": {"type": "interval", "value": 30}
            })
            period2_id = manager.create_period({
                "name": "Period 2",
                "schedule": {"type": "interval", "value": 60}
            })
            period3_id = manager.create_period({
                "name": "Period 3",
                "schedule": {"type": "cron", "value": "0 * * * *"}
            })
            print(f"✓ Created 3 periods")
            
            # Assign all periods to same channel WITH DIFFERENT PROFILES (new feature!)
            channel_id = 42
            manager.assign_period_to_channels(period1_id, [channel_id], profile1_id)
            manager.assign_period_to_channels(period2_id, [channel_id], profile2_id)
            manager.assign_period_to_channels(period3_id, [channel_id], profile3_id)
            
            # Verify all periods are assigned (now returns dict)
            channel_period_assignments = manager.get_channel_periods(channel_id)
            assert isinstance(channel_period_assignments, dict), "Should return dict"
            assert len(channel_period_assignments) == 3, f"Expected 3 periods, got {len(channel_period_assignments)}"
            assert set(channel_period_assignments.keys()) == {period1_id, period2_id, period3_id}, "Period IDs mismatch"
            assert channel_period_assignments[period1_id] == profile1_id, "Profile 1 mismatch"
            assert channel_period_assignments[period2_id] == profile2_id, "Profile 2 mismatch"
            assert channel_period_assignments[period3_id] == profile3_id, "Profile 3 mismatch"
            print(f"✓ Channel has {len(channel_period_assignments)} periods with different profiles assigned")
            
            # Get active periods (should return all since we don't have real time checking)
            active_periods = manager.get_active_periods_for_channel(channel_id)
            assert len(active_periods) > 0, "Should have at least one active period"
            print(f"✓ Found {len(active_periods)} active periods")
            
            # Effective configuration should return the first period
            config = manager.get_effective_configuration(channel_id)
            assert config is not None, "Should have effective configuration"
            assert config['period_id'] in [period1_id, period2_id, period3_id], "Period ID should be one of assigned"
            print("✓ Effective configuration uses first active period")
            
            print("✅ Test 5 passed\n")
            
        finally:
            automation_config_manager.CONFIG_DIR = original_config_dir
            automation_config_manager.AUTOMATION_CONFIG_FILE = original_config_file


if __name__ == '__main__':
    print("=" * 60)
    print("Testing Automation Periods Functionality")
    print("=" * 60 + "\n")
    
    try:
        test_automation_periods_creation()
        test_automation_periods_channel_assignment()
        test_automation_periods_update_delete()
        test_effective_configuration()
        test_multiple_periods_per_channel()
        
        print("=" * 60)
        print("✅ ALL TESTS PASSED")
        print("=" * 60)
        
    except AssertionError as e:
        print(f"\n❌ TEST FAILED: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"\n❌ UNEXPECTED ERROR: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
