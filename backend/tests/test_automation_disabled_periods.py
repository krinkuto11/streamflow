from apps.automation.automation_config_manager import AutomationConfigManager


def _manager_with_periods(periods, profiles=None):
    manager = object.__new__(AutomationConfigManager)
    profiles = profiles or {"profile-1": {"id": "profile-1", "name": "Profile 1"}}
    period_assignments = {period_id: "profile-1" for period_id in periods}

    manager.get_effective_channel_periods = lambda channel_id, group_id=None: period_assignments
    manager.get_period = lambda period_id: periods.get(period_id)
    manager.get_profile = lambda profile_id: profiles.get(profile_id)
    return manager


def test_disabled_period_is_not_active_for_channel():
    manager = _manager_with_periods(
        {
            "period-1": {
                "id": "period-1",
                "name": "Disabled Full Check",
                "enabled": False,
            }
        }
    )

    assert manager.get_active_periods_for_channel(123) == []
    assert manager.get_effective_configuration(123) is None


def test_enabled_period_still_resolves_when_disabled_period_is_present():
    manager = _manager_with_periods(
        {
            "period-1": {
                "id": "period-1",
                "name": "Disabled Full Check",
                "enabled": False,
            },
            "period-2": {
                "id": "period-2",
                "name": "Enabled Full Check",
                "enabled": True,
            },
        }
    )

    active_periods = manager.get_active_periods_for_channel(123)

    assert [period["id"] for period in active_periods] == ["period-2"]
    config = manager.get_effective_configuration(123)
    assert config is not None
    assert config["period_id"] == "period-2"
    assert config["profile"]["id"] == "profile-1"
