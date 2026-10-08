"""Shared persisted probe-report fields."""

VISUAL_PROBE_REPORT_FIELDS = (
    "visual_probe_ran",
    "visual_probe_completed",
    "visual_probe_incomplete",
    "visual_probe_incomplete_reason",
    "visual_probe_requested_duration_seconds",
    "visual_probe_minimum_duration_seconds",
    "visual_probe_duration_seconds",
    "visual_probe_duration_adjusted",
    "visual_probe_duration_adjustment_reason",
    "visual_probe_elapsed_time",
)

BITRATE_RECHECK_REPORT_FIELDS = (
    "measurement_incomplete",
    "measurement_incomplete_reason",
    "measurement_incomplete_context",
    "bitrate_recheck_required",
    "bitrate_recheck_attempted",
    "bitrate_recheck_outcome",
)

