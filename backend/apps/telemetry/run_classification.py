"""Run/job classification helpers for telemetry history.

The raw ``run_type`` values are kept for backwards compatibility.  V6 adds a
stable operator-facing category/outcome layer so UI and API consumers can show
full runs, single checks, provider refreshes, Shadow decisions and future job
types without guessing from free-form action names.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, Optional


FULL_RUN_ACTIONS = {"automation_run", "global_check", "playlist_update_match"}
SINGLE_CHANNEL_ACTIONS = {"single_channel_check"}
PROVIDER_REFRESH_ACTIONS = {"playlist_refresh", "m3u_refresh", "provider_refresh"}
STREAM_MATCHING_ACTIONS = {"streams_assigned", "stream_validation"}
TEAMARR_ACTION_PREFIXES = ("teamarr_", "epg_", "scheduled_event_")
SHADOW_ACTION_PREFIXES = ("shadow_", "watcher_", "switch_")


def _first_present(details: Dict[str, Any], keys: Iterable[str]) -> Any:
    for key in keys:
        value = details.get(key)
        if value not in (None, ""):
            return value
    return None


def classify_job_category(action: str, details: Optional[Dict[str, Any]] = None) -> str:
    """Return the stable V6 job category for a telemetry action."""
    action_key = (action or "").strip().lower()
    details = details or {}

    if action_key in FULL_RUN_ACTIONS:
        return "full_run"
    if action_key in SINGLE_CHANNEL_ACTIONS:
        return "single_channel"
    if action_key in PROVIDER_REFRESH_ACTIONS:
        return "provider_refresh"
    if action_key in STREAM_MATCHING_ACTIONS:
        return "stream_matching"
    if action_key.startswith(SHADOW_ACTION_PREFIXES) or details.get("shadow_monitor"):
        return "shadow_monitor"
    if action_key.startswith(TEAMARR_ACTION_PREFIXES) or details.get("event_identity"):
        return "event_check"
    if "manual" in action_key:
        return "manual_probe"
    return "system"


def classify_job_outcome(action: str, details: Optional[Dict[str, Any]] = None) -> str:
    """Normalize assorted success/state fields into a small outcome vocabulary."""
    details = details or {}
    raw = _first_present(
        details,
        (
            "job_outcome",
            "outcome",
            "run_state",
            "state",
            "status",
            "result",
        ),
    )
    raw_value = str(raw).strip().lower() if raw is not None else ""

    if raw_value in {"completed_degraded", "degraded", "partial", "stale_ok"}:
        return "completed_degraded"
    if raw_value in {"completed", "complete", "success", "successful", "ok", "settled"}:
        return "completed"
    if raw_value in {"skipped", "skip", "not_due", "no_due_period"}:
        return "skipped"
    if raw_value in {"aborted", "abort", "cancelled", "canceled", "stopped"}:
        return "aborted"
    if raw_value in {"failed", "failure", "error", "fatal"}:
        return "failed"

    if details.get("failed_refresh_requests") or details.get("degraded_count"):
        return "completed_degraded"
    if details.get("success") is False:
        return "failed"
    if details.get("success") is True:
        return "completed"
    if details.get("error") or details.get("last_error"):
        return "failed"

    # Existing changelog entries are emitted after useful work has happened.
    if action:
        return "completed"
    return "unknown"


def classify_job_subject(details: Optional[Dict[str, Any]] = None) -> str:
    """Return a non-private subject reference for filtering and grouping."""
    details = details or {}
    channel_id = _first_present(details, ("channel_id", "dispatcharr_channel_id"))
    if channel_id is not None:
        return f"channel:{channel_id}"

    stream_id = _first_present(details, ("stream_id", "dispatcharr_stream_id"))
    if stream_id is not None:
        return f"stream:{stream_id}"

    provider_id = _first_present(details, ("provider_id", "m3u_account_id", "account_id"))
    if provider_id is not None:
        return f"provider:{provider_id}"

    event_identity = _first_present(details, ("event_identity", "identity"))
    if event_identity is not None:
        return "event"

    return "global"


def classify_job_correlation_id(action: str, details: Optional[Dict[str, Any]] = None) -> Optional[str]:
    """Extract a stable correlation ID when the producer already has one."""
    details = details or {}
    value = _first_present(
        details,
        (
            "job_correlation_id",
            "correlation_id",
            "run_id",
            "automation_run_id",
            "event_identity",
            "identity",
        ),
    )
    if value is None:
        return None
    return str(value)


def classify_run_metadata(
    action: str,
    details: Optional[Dict[str, Any]] = None,
    subentries: Optional[Any] = None,
) -> Dict[str, Optional[str]]:
    """Return all normalized V6 job-history fields for a run row."""
    return {
        "job_category": classify_job_category(action, details),
        "job_outcome": classify_job_outcome(action, details),
        "job_subject_ref": classify_job_subject(details),
        "job_correlation_id": classify_job_correlation_id(action, details),
    }
