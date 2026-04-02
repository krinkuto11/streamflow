#!/usr/bin/env python3
"""
Tests for Automation Events Scheduler
"""

import sys
import os
import tempfile
from pathlib import Path
from datetime import datetime, timedelta

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_calculate_upcoming_events():
    """Test calculating upcoming automation events"""
    print("Test: Calculate upcoming events")
    
    with tempfile.TemporaryDirectory() as tmpdir:
        # Override config paths
        import apps.automation.automation_config_manager as automation_config_manager
        import apps.automation.automation_events_scheduler as automation_events_scheduler
        
        original_config_dir = automation_config_manager.CONFIG_DIR
        original_config_file = automation_config_manager.AUTOMATION_CONFIG_FILE
        original_events_cache = automation_events_scheduler.EVENTS_CACHE_FILE
        
        automation_config_manager.CONFIG_DIR = Path(tmpdir)
        automation_config_manager.AUTOMATION_CONFIG_FILE = Path(tmpdir) / 'test_automation_config.json'
        automation_events_scheduler.CONFIG_DIR = Path(tmpdir)
        automation_events_scheduler.EVENTS_CACHE_FILE = Path(tmpdir) / 'test_events_cache.json'
        
        try:
            from apps.automation.automation_config_manager import AutomationConfigManager
            from apps.automation.automation_events_scheduler import AutomationEventsScheduler
            
            # Create manager and scheduler
            manager = AutomationConfigManager()
            scheduler = AutomationEventsScheduler()
            
            # Create profile and periods
            profile_id = manager.create_profile({"name": "Test Profile"})
            assert profile_id is not None, "Failed to create profile"
            print(f"✓ Created profile: {profile_id}")
            
            # Create period with interval schedule (no profile_id in period)
            period1_id = manager.create_period({
                "name": "Hourly Period",
                "schedule": {"type": "interval", "value": 60}
            })
            assert period1_id is not None, "Failed to create period 1"
            print(f"✓ Created interval period: {period1_id}")
            
            # Assign period to channels WITH profile_id
            manager.assign_period_to_channels(period1_id, [1, 2, 3], profile_id)
            print("✓ Assigned period to 3 channels")
            
            # Calculate upcoming events for 24 hours
            events = scheduler.calculate_upcoming_events(hours_ahead=24, max_events=100)
            assert len(events) > 0, "Should have calculated some events"
            assert len(events) <= 24, f"Should have at most 24 events for hourly schedule, got {len(events)}"
            print(f"✓ Calculated {len(events)} events")
            
            # Verify event structure (profile_name changed to profile_display)
            first_event = events[0]
            assert 'time' in first_event, "Event should have time field"
            assert 'period_id' in first_event, "Event should have period_id"
            assert 'period_name' in first_event, "Event should have period_name"
            assert 'profile_display' in first_event, "Event should have profile_display"
            assert 'channel_count' in first_event, "Event should have channel_count"
            assert first_event['channel_count'] == 3, "Channel count should be 3"
            assert first_event['period_name'] == "Hourly Period", "Period name mismatch"
            print("✓ Event structure verified")
            
            # Verify events are sorted by time
            for i in range(len(events) - 1):
                assert events[i]['time'] <= events[i+1]['time'], "Events should be sorted by time"
            print("✓ Events are sorted by time")
            
            print("✅ Test passed\n")
            
        finally:
            # Restore original config
            automation_config_manager.CONFIG_DIR = original_config_dir
            automation_config_manager.AUTOMATION_CONFIG_FILE = original_config_file
            automation_events_scheduler.CONFIG_DIR = original_config_dir
            automation_events_scheduler.EVENTS_CACHE_FILE = original_events_cache


def test_event_caching():
    """Test event caching functionality"""
    print("Test: Event caching")
    
    with tempfile.TemporaryDirectory() as tmpdir:
        import apps.automation.automation_config_manager as automation_config_manager
        import apps.automation.automation_events_scheduler as automation_events_scheduler
        
        original_config_dir = automation_config_manager.CONFIG_DIR
        original_config_file = automation_config_manager.AUTOMATION_CONFIG_FILE
        original_events_cache = automation_events_scheduler.EVENTS_CACHE_FILE
        
        automation_config_manager.CONFIG_DIR = Path(tmpdir)
        automation_config_manager.AUTOMATION_CONFIG_FILE = Path(tmpdir) / 'test_automation_config.json'
        automation_events_scheduler.CONFIG_DIR = Path(tmpdir)
        automation_events_scheduler.EVENTS_CACHE_FILE = Path(tmpdir) / 'test_events_cache.json'
        
        try:
            from apps.automation.automation_config_manager import AutomationConfigManager
            from apps.automation.automation_events_scheduler import AutomationEventsScheduler
            
            manager = AutomationConfigManager()
            scheduler = AutomationEventsScheduler()
            
            # Create profile and period (no profile_id in period)
            profile_id = manager.create_profile({"name": "Test Profile"})
            period_id = manager.create_period({
                "name": "Test Period",
                "schedule": {"type": "interval", "value": 30}
            })
            manager.assign_period_to_channels(period_id, [1], profile_id)
            print("✓ Created test data")
            
            # Get cached events (should calculate)
            result1 = scheduler.get_cached_events()
            assert 'events' in result1, "Result should have events"
            assert 'cached_at' in result1, "Result should have cached_at"
            assert 'from_cache' in result1, "Result should have from_cache"
            assert result1['from_cache'] is False, "First call should not be from cache"
            print("✓ First call calculated events")
            
            # Get cached events again (should use cache)
            result2 = scheduler.get_cached_events()
            assert result2['from_cache'] is True, "Second call should be from cache"
            assert len(result1['events']) == len(result2['events']), "Cache should return same events"
            print("✓ Second call used cache")
            
            # Force refresh
            result3 = scheduler.get_cached_events(force_refresh=True)
            assert result3['from_cache'] is False, "Force refresh should recalculate"
            print("✓ Force refresh recalculated")
            
            # Invalidate cache
            scheduler.invalidate_cache()
            result4 = scheduler.get_cached_events()
            assert result4['from_cache'] is False, "After invalidation should recalculate"
            print("✓ Cache invalidation works")
            
            print("✅ Test passed\n")
            
        finally:
            automation_config_manager.CONFIG_DIR = original_config_dir
            automation_config_manager.AUTOMATION_CONFIG_FILE = original_config_file
            automation_events_scheduler.CONFIG_DIR = original_config_dir
            automation_events_scheduler.EVENTS_CACHE_FILE = original_events_cache


def test_multiple_periods():
    """Test events from multiple periods"""
    print("Test: Multiple periods")
    
    with tempfile.TemporaryDirectory() as tmpdir:
        import apps.automation.automation_config_manager as automation_config_manager
        import apps.automation.automation_events_scheduler as automation_events_scheduler
        
        original_config_dir = automation_config_manager.CONFIG_DIR
        original_config_file = automation_config_manager.AUTOMATION_CONFIG_FILE
        original_events_cache = automation_events_scheduler.EVENTS_CACHE_FILE
        
        automation_config_manager.CONFIG_DIR = Path(tmpdir)
        automation_config_manager.AUTOMATION_CONFIG_FILE = Path(tmpdir) / 'test_automation_config.json'
        automation_events_scheduler.CONFIG_DIR = Path(tmpdir)
        automation_events_scheduler.EVENTS_CACHE_FILE = Path(tmpdir) / 'test_events_cache.json'
        
        try:
            from apps.automation.automation_config_manager import AutomationConfigManager
            from apps.automation.automation_events_scheduler import AutomationEventsScheduler
            import apps.automation.automation_config_manager as automation_config_manager
            
            # Reset the singleton to ensure it uses our test config
            automation_config_manager._automation_config_manager = None
            
            manager = AutomationConfigManager()
            scheduler = AutomationEventsScheduler()
            
            # Create profiles
            profile_id = manager.create_profile({"name": "Test Profile"})
            
            # Create multiple periods with different schedules (no profile_id in periods)
            period1_id = manager.create_period({
                "name": "Period 1",
                "schedule": {"type": "interval", "value": 60}
            })
            period2_id = manager.create_period({
                "name": "Period 2",
                "schedule": {"type": "interval", "value": 120}
            })
            
            manager.assign_period_to_channels(period1_id, [1, 2], profile_id)
            manager.assign_period_to_channels(period2_id, [3, 4, 5], profile_id)
            print("✓ Created 2 periods with different schedules")
            
            # Create a NEW scheduler to ensure it loads the latest config
            scheduler = AutomationEventsScheduler()
            
            # Verify periods were created by reloading config
            all_periods = manager.get_all_periods()
            assert len(all_periods) == 2, f"Should have 2 periods, got {len(all_periods)}"
            
            # Debug: Check assignments
            for period in all_periods:
                pid = period['id']
                channels = manager.get_period_channels(pid)
                print(f"Debug: Period {period['name']} has {len(channels)} channels: {channels}")
            
            # Calculate events using the scheduler (which handles the new data model)
            events = scheduler.calculate_upcoming_events(hours_ahead=24, max_events=100)
            print(f"✓ Generated {len(events)} total events")
            
            # Should have events from both periods
            period1_events = [e for e in events if e['period_id'] == period1_id]
            period2_events = [e for e in events if e['period_id'] == period2_id]
            
            print(f"Debug: Period 1 events: {len(period1_events)}, Period 2 events: {len(period2_events)}")
            
            assert len(period1_events) > 0, "Should have events from period 1"
            assert len(period2_events) > 0, "Should have events from period 2"
            assert len(period1_events) > len(period2_events), "Period 1 (60min) should have more events than Period 2 (120min)"
            print(f"✓ Period 1: {len(period1_events)} events, Period 2: {len(period2_events)} events")
            
            # Verify channel counts
            for event in period1_events:
                assert event['channel_count'] == 2, "Period 1 should have 2 channels"
            for event in period2_events:
                assert event['channel_count'] == 3, "Period 2 should have 3 channels"
            print("✓ Channel counts correct for each period")
            
            print("✅ Test passed\n")
            
        finally:
            automation_config_manager.CONFIG_DIR = original_config_dir
            automation_config_manager.AUTOMATION_CONFIG_FILE = original_config_file
            automation_events_scheduler.CONFIG_DIR = original_config_dir
            automation_events_scheduler.EVENTS_CACHE_FILE = original_events_cache


if __name__ == '__main__':
    print("=" * 60)
    print("Testing Automation Events Scheduler")
    print("=" * 60 + "\n")
    
    try:
        test_calculate_upcoming_events()
        test_event_caching()
        test_multiple_periods()
        
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
