"""Single-channel playlist scope and post-refresh observation helpers."""

import logging
import threading
import time
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger("apps.stream.stream_checker_service")


def _wait_for_udi_stream_count_stabilise(
    udi,
    pre_count: int,
    timeout: int = 60,
    poll_interval: int = 5,
    abort_event: Optional[threading.Event] = None,
) -> bool:
    """Poll UDI stream count after triggering a Dispatcharr playlist refresh.

    Dispatcharr processes M3U playlists asynchronously. The refresh API call
    returns as soon as the job is *enqueued*, not when it completes. Immediately
    syncing the UDI cache after the call often returns pre-refresh data.

    This helper polls the UDI stream count until it changes from pre_count
    (indicating Dispatcharr has finished processing) or until timeout elapses.
    It reuses the same poll-and-confirm pattern used in the startup sequence.

    Args:
        udi: Initialised UDI manager instance.
        pre_count: Stream count captured before refresh_m3u_playlists() was called.
        timeout: Maximum seconds to wait before giving up (default 60).
        poll_interval: Seconds between each poll attempt (default 5).

    Returns:
        True  — stream count changed; refresh appears to have taken effect.
        False — timed out with no change; downstream steps proceed on
                potentially stale data (logged as a warning).
    """
    elapsed = 0
    while elapsed < timeout:
        if abort_event is not None:
            if abort_event.wait(poll_interval):
                logger.info("Post-refresh UDI wait aborted")
                return False
        else:
            time.sleep(poll_interval)
        elapsed += poll_interval
        try:
            current_count = udi.get_stream_count()
            if current_count != pre_count:
                logger.info(
                    f"UDI stream count changed after playlist refresh: "
                    f"{pre_count} → {current_count} ({elapsed}s elapsed)"
                )
                return True
        except Exception as _e:
            logger.warning(
                f"Error polling UDI stream count during post-refresh wait: {_e}"
            )
    logger.warning(
        f"UDI stream count unchanged after {timeout}s (still {pre_count} streams). "
        "Proceeding with potentially stale data. "
        "Consider setting post_refresh_delay_seconds in config if this recurs."
    )
    return False


def _coerce_m3u_account_id(value: Any) -> Optional[int]:
    try:
        if value is None or isinstance(value, bool):
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _dedupe_m3u_account_ids(values: List[Any]) -> List[int]:
    account_ids: List[int] = []
    seen: Set[int] = set()
    for value in values:
        account_id = _coerce_m3u_account_id(value)
        if account_id is None or account_id in seen:
            continue
        seen.add(account_id)
        account_ids.append(account_id)
    return account_ids


def _is_active_non_custom_m3u_account(account: Dict[str, Any]) -> bool:
    name = str(account.get("name") or "").strip().lower()
    raw_id = str(account.get("id") or "").strip().lower()
    if name == "custom" or raw_id == "custom":
        return False

    for key in ("is_active", "active", "enabled"):
        if key in account:
            return bool(account.get(key))
    return True


def _sort_m3u_account_ids(values: Set[int]) -> List[int]:
    return sorted(values, key=lambda value: (str(type(value)), value))


def _resolve_single_channel_m3u_refresh_scope(
    *,
    profile: Dict[str, Any],
    channel_account_ids: Set[int],
    udi: Any,
) -> Tuple[List[int], str]:
    """Resolve the provider-fetch scope for a single-channel check.

    V6 semantics:
      - explicit profile m3u_update.playlists: refresh exactly those accounts
      - empty profile playlist list: refresh every active, non-custom account

    If account discovery is unavailable, fall back to the current channel
    accounts to preserve the pre-V6 behavior instead of silently doing nothing.
    """
    m3u_update = profile.get("m3u_update") if isinstance(profile, dict) else {}
    if not isinstance(m3u_update, dict):
        m3u_update = {}

    explicit_playlist_ids = _dedupe_m3u_account_ids(m3u_update.get("playlists") or [])
    if explicit_playlist_ids:
        return explicit_playlist_ids, "profile_playlists"

    all_accounts = []
    try:
        get_accounts = getattr(udi, "get_m3u_accounts", None)
        if callable(get_accounts):
            fetched_accounts = get_accounts()
            if isinstance(fetched_accounts, list):
                all_accounts = fetched_accounts
    except Exception as exc:
        logger.warning("Could not resolve active M3U accounts for single-channel refresh: %s", exc)

    active_account_ids = _dedupe_m3u_account_ids(
        [
            account.get("id")
            for account in all_accounts
            if isinstance(account, dict) and _is_active_non_custom_m3u_account(account)
        ]
    )
    if active_account_ids:
        return active_account_ids, "all_active_non_custom"

    fallback_ids = _sort_m3u_account_ids(channel_account_ids)
    if fallback_ids:
        logger.warning(
            "Falling back to channel-attached M3U accounts for single-channel refresh "
            "because active account discovery returned no usable accounts"
        )
        return fallback_ids, "channel_accounts_fallback"

    return [], "none"

