"""
Additional test cases for max_loop_duration schema validation.
Add these to backend/tests/test_api_schemas.py alongside the existing
test_automation_profile_schema_* tests.
"""

# --- max_loop_duration validation ---

def test_automation_profile_schema_accepts_valid_max_loop_duration():
    """max_loop_duration within range is stored as-is."""
    parsed = AutomationProfileCreateSchema.from_payload(
        {
            "name": "Sports",
            "stream_checking": {
                "enabled": True,
                "max_loop_duration": 120,
            },
        }
    )
    assert parsed.profile_data["stream_checking"]["max_loop_duration"] == 120


def test_automation_profile_schema_clamps_max_loop_duration_above_max():
    """Values above 240 are clamped to 240."""
    parsed = AutomationProfileUpdateSchema.from_payload(
        {
            "stream_checking": {
                "max_loop_duration": 999,
            },
        }
    )
    assert parsed.profile_data["stream_checking"]["max_loop_duration"] == 240


def test_automation_profile_schema_clamps_max_loop_duration_below_min():
    """Values below 10 are clamped to 10."""
    parsed = AutomationProfileUpdateSchema.from_payload(
        {
            "stream_checking": {
                "max_loop_duration": 0,
            },
        }
    )
    assert parsed.profile_data["stream_checking"]["max_loop_duration"] == 10


def test_automation_profile_schema_clamps_max_loop_duration_negative():
    """Negative values are clamped to 10 (the minimum)."""
    parsed = AutomationProfileUpdateSchema.from_payload(
        {
            "stream_checking": {
                "max_loop_duration": -50,
            },
        }
    )
    assert parsed.profile_data["stream_checking"]["max_loop_duration"] == 10


def test_automation_profile_schema_rejects_non_numeric_max_loop_duration():
    """Non-numeric max_loop_duration raises ValidationError with clear message."""
    with pytest.raises(ValidationError) as exc:
        AutomationProfileUpdateSchema.from_payload(
            {
                "stream_checking": {
                    "max_loop_duration": "fast",
                },
            }
        )
    assert "stream_checking.max_loop_duration" in str(exc.value)
    assert "integer" in str(exc.value)


def test_automation_profile_schema_accepts_max_loop_duration_at_boundaries():
    """Boundary values 10 and 240 are accepted without clamping."""
    for boundary in (10, 240):
        parsed = AutomationProfileUpdateSchema.from_payload(
            {
                "stream_checking": {
                    "max_loop_duration": boundary,
                },
            }
        )
        assert parsed.profile_data["stream_checking"]["max_loop_duration"] == boundary


def test_automation_profile_schema_passes_through_without_max_loop_duration():
    """Profiles without max_loop_duration are accepted unchanged (migration case)."""
    parsed = AutomationProfileCreateSchema.from_payload(
        {
            "name": "Legacy Profile",
            "stream_checking": {
                "enabled": True,
                "loop_check_enabled": True,
            },
        }
    )
    # Field absent — not injected by schema, backend uses default of 120
    assert "max_loop_duration" not in parsed.profile_data["stream_checking"]
