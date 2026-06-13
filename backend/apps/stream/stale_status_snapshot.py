"""UI-safe stale-status summaries for StreamFlow run snapshots."""

from collections import Counter
from typing import Any, Dict, List, Optional


ACTIVE_EXTERNAL_STATUSES = {"fetching", "parsing", "processing", "queued", "running"}
EXTERNAL_CHECK_KEYS = ("celery", "redis", "postgres")


def external_message_class(message: Any) -> str:
    """Classify Dispatcharr status text without exposing the raw message."""
    text = str(message or "").strip().lower()
    if not text:
        return "none"
    if "processing completed" in text or "completed in" in text or "refresh completed" in text:
        return "completed"
    if any(token in text for token in ("error", "failed", "exception", "traceback", "can't ", "cannot ")):
        return "error"
    return "other"


def external_m3u_account_risk(account: Dict[str, Any]) -> Dict[str, Any]:
    """Return a UI-safe M3U account status summary and stale-risk classification."""
    status = str(account.get("status") or "unknown").strip().lower() or "unknown"
    message_class = external_message_class(account.get("last_message"))
    active_status = status in ACTIVE_EXTERNAL_STATUSES
    stale_suspected = False
    conflict = None

    if active_status and message_class == "completed":
        stale_suspected = True
        conflict = "active_status_with_completed_message"
    elif active_status and message_class == "error":
        stale_suspected = True
        conflict = "active_status_with_error_message"
    elif status == "success" and message_class == "error":
        stale_suspected = True
        conflict = "success_status_with_error_message"
    elif status == "error" and message_class == "completed":
        stale_suspected = True
        conflict = "error_status_with_completed_message"

    return {
        "account_id": account.get("id"),
        "account_name": account.get("name") or (f"Account {account.get('id')}" if account.get("id") is not None else "Account"),
        "status": status,
        "message_class": message_class,
        "updated_at": account.get("updated_at"),
        "active_status": active_status,
        "stale_status_suspected": stale_suspected,
        "conflict": conflict,
    }


def _unknown_external_checks() -> Dict[str, str]:
    return {key: "unknown" for key in EXTERNAL_CHECK_KEYS}


def build_dispatcharr_stale_snapshot(
    *,
    network_ready: Optional[bool] = None,
    accounts: Optional[List[Dict[str, Any]]] = None,
    diagnostics: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Build a compact, sanitized stale-status summary for persisted history."""
    summary = {
        "status": "unknown",
        "read_only": True,
        "stale_status_suspected": False,
        "m3u_accounts_available": False,
        "m3u_accounts_total": 0,
        "m3u_accounts_active": 0,
        "m3u_status_counts": {},
        "stale_suspected_count": 0,
        "external_checks": _unknown_external_checks(),
        "actions": {
            "dispatcharr_mutated": False,
            "dispatcharr_restart_attempted": False,
            "repair_requires_operator_approval": True,
        },
    }

    if isinstance(diagnostics, dict):
        summary["status"] = str(diagnostics.get("status") or "unknown")
        summary["read_only"] = diagnostics.get("read_only") is not False
        summary["stale_status_suspected"] = bool(diagnostics.get("stale_status_suspected"))

        m3u_accounts = diagnostics.get("m3u_accounts") if isinstance(diagnostics.get("m3u_accounts"), dict) else {}
        summary["m3u_accounts_available"] = bool(m3u_accounts.get("available"))
        summary["m3u_accounts_total"] = int(m3u_accounts.get("total") or 0)
        summary["m3u_accounts_active"] = int(m3u_accounts.get("active") or 0)
        status_counts = m3u_accounts.get("status_counts") if isinstance(m3u_accounts.get("status_counts"), dict) else {}
        summary["m3u_status_counts"] = dict(sorted((str(key), int(value or 0)) for key, value in status_counts.items()))
        summary["stale_suspected_count"] = int(m3u_accounts.get("stale_suspected_count") or 0)

        external_checks = diagnostics.get("external_checks") if isinstance(diagnostics.get("external_checks"), dict) else {}
        summary["external_checks"] = {
            key: str((external_checks.get(key) or {}).get("status") or "unknown")
            if isinstance(external_checks.get(key), dict)
            else "unknown"
            for key in EXTERNAL_CHECK_KEYS
        }

        actions = diagnostics.get("actions") if isinstance(diagnostics.get("actions"), dict) else {}
        summary["actions"] = {
            "dispatcharr_mutated": bool(actions.get("dispatcharr_mutated")),
            "dispatcharr_restart_attempted": bool(actions.get("dispatcharr_restart_attempted")),
            "repair_requires_operator_approval": actions.get("repair_requires_operator_approval") is not False,
        }
        return summary

    if network_ready is False:
        summary["status"] = "insufficient_evidence"
        return summary

    if not isinstance(accounts, list):
        return summary

    status_counts: Counter = Counter()
    active_count = 0
    stale_count = 0
    for account in accounts:
        if not isinstance(account, dict):
            continue
        if account.get("is_active") is False:
            continue
        active_count += 1
        risk = external_m3u_account_risk(account)
        status_counts[risk["status"]] += 1
        if risk["stale_status_suspected"]:
            stale_count += 1

    summary.update({
        "status": "stale_risk" if stale_count else "ok",
        "stale_status_suspected": bool(stale_count),
        "m3u_accounts_available": True,
        "m3u_accounts_total": len(accounts),
        "m3u_accounts_active": active_count,
        "m3u_status_counts": dict(sorted(status_counts.items())),
        "stale_suspected_count": stale_count,
    })
    return summary


def build_stale_warnings(*, dispatcharr_stale: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """Return compact warning records suitable for History and Changelog."""
    warnings: List[Dict[str, Any]] = []
    if isinstance(dispatcharr_stale, dict) and dispatcharr_stale.get("stale_status_suspected"):
        warnings.append({
            "type": "dispatcharr_status_risk",
            "label": "Dispatcharr Status Risk",
            "status": dispatcharr_stale.get("status") or "stale_risk",
            "count": int(dispatcharr_stale.get("stale_suspected_count") or 0),
            "read_only": dispatcharr_stale.get("read_only") is not False,
            "repair_requires_operator_approval": (dispatcharr_stale.get("actions") or {}).get(
                "repair_requires_operator_approval",
                True,
            ) is not False,
        })
    return warnings
