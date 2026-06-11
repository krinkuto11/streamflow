"""StreamFlow-owned Dispatcharr channel visibility automation."""

from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional

from apps.core.logging_config import setup_logging

logger = setup_logging(__name__)

STATE_KEY = "streamflow_channel_visibility_state"
DEFAULT_VISIBILITY_CONFIG = {
    "enabled": False,
    "hide_on_no_regex": False,
    "hide_on_no_streams": False,
    "hide_on_all_failed": False,
    "unhide_on_recovered": True,
}


class ChannelVisibilityAutomation:
    """Manage only StreamFlow-owned hide/unhide decisions."""

    def __init__(
        self,
        *,
        db_provider: Optional[Callable[[], Any]] = None,
        patch_request: Optional[Callable[[str, Dict[str, Any]], Any]] = None,
        base_url_provider: Optional[Callable[[], str]] = None,
        udi_provider: Optional[Callable[[], Any]] = None,
        clock: Optional[Callable[[], datetime]] = None,
    ):
        if db_provider is None:
            from apps.database.manager import get_db_manager

            db_provider = get_db_manager
        if patch_request is None:
            from apps.core.api_utils import patch_request as api_patch_request

            patch_request = api_patch_request
        if base_url_provider is None:
            from apps.core.api_utils import _get_base_url

            base_url_provider = _get_base_url
        if udi_provider is None:
            from apps.udi import get_udi_manager

            udi_provider = get_udi_manager

        self.db_provider = db_provider
        self.patch_request = patch_request
        self.base_url_provider = base_url_provider
        self.udi_provider = udi_provider
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    @staticmethod
    def merge_config(config: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        merged = dict(DEFAULT_VISIBILITY_CONFIG)
        if isinstance(config, dict):
            merged.update(config)
        return merged

    def handle_no_regex(
        self,
        channel: Dict[str, Any],
        *,
        config: Optional[Dict[str, Any]] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        merged = self.merge_config(config)
        if not merged.get("enabled") or not merged.get("hide_on_no_regex"):
            return self._skipped(channel, "disabled", "no_regex")
        return self.hide_channel(channel, reason="no_regex", details=details)

    def handle_quality_result(
        self,
        channel: Dict[str, Any],
        *,
        good_streams_count: int,
        dead_streams_count: int,
        revived_streams_count: int = 0,
        config: Optional[Dict[str, Any]] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        merged = self.merge_config(config)
        if not merged.get("enabled"):
            return self._skipped(channel, "disabled", "quality_result")

        details = dict(details or {})
        total_streams = details.get("total_streams")
        try:
            total_streams = int(total_streams)
        except (TypeError, ValueError):
            total_streams = None

        if total_streams == 0 and merged.get("hide_on_no_streams"):
            return self.hide_channel(channel, reason="no_streams", details=details)

        if total_streams and total_streams > 0 and merged.get("unhide_on_recovered"):
            state_entry = self._state_entry_for_channel(channel)
            if state_entry and state_entry.get("reason") == "no_streams":
                return self.unhide_channel(channel, reason="streams_recovered", details=details)

        if (good_streams_count > 0 or revived_streams_count > 0) and merged.get("unhide_on_recovered"):
            return self.unhide_channel(channel, reason="recovered", details=details)
        if good_streams_count <= 0 and dead_streams_count > 0 and merged.get("hide_on_all_failed"):
            return self.hide_channel(channel, reason="all_failed", details=details)
        return self._skipped(channel, "no_visibility_change", "quality_result")

    def hide_channel(
        self,
        channel: Dict[str, Any],
        *,
        reason: str,
        details: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        channel_id = self._channel_id(channel)
        if channel_id is None:
            return self._skipped(channel, "missing_channel_id", reason)

        state = self._load_state()
        state_entry = state.get(str(channel_id))
        if self._is_hidden(channel):
            if state_entry:
                state[str(channel_id)] = self._state_entry(channel, reason, details)
                self._save_state(state)
                return self._result(channel, "hidden_already_managed", reason, changed=False, details=details)
            return self._result(channel, "manual_hidden_preserved", reason, changed=False, details=details)

        patch_result = self._patch_hidden(channel, True)
        if not patch_result.get("success"):
            return self._result(
                channel,
                "patch_failed",
                reason,
                changed=False,
                details={**(details or {}), "error": patch_result.get("error")},
            )

        state[str(channel_id)] = self._state_entry(channel, reason, details)
        self._save_state(state)
        return self._result(channel, "hidden", reason, changed=True, details=details)

    def unhide_channel(
        self,
        channel: Dict[str, Any],
        *,
        reason: str,
        details: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        channel_id = self._channel_id(channel)
        if channel_id is None:
            return self._skipped(channel, "missing_channel_id", reason)

        state = self._load_state()
        state_entry = state.get(str(channel_id))
        if not state_entry:
            if self._is_hidden(channel):
                return self._result(channel, "manual_hidden_preserved", reason, changed=False, details=details)
            return self._result(channel, "visible_unmanaged", reason, changed=False, details=details)

        if not self._is_hidden(channel):
            state.pop(str(channel_id), None)
            self._save_state(state)
            return self._result(channel, "state_cleared_visible", reason, changed=False, details=details)

        patch_result = self._patch_hidden(channel, False)
        if not patch_result.get("success"):
            return self._result(
                channel,
                "patch_failed",
                reason,
                changed=False,
                details={**(details or {}), "error": patch_result.get("error")},
            )

        state.pop(str(channel_id), None)
        self._save_state(state)
        return self._result(channel, "unhidden", reason, changed=True, details=details)

    def _load_state(self) -> Dict[str, Dict[str, Any]]:
        try:
            state = self.db_provider().get_system_setting(STATE_KEY, {}) or {}
            return state if isinstance(state, dict) else {}
        except Exception as exc:
            logger.warning("Could not load StreamFlow channel visibility state: %s", exc)
            return {}

    def _state_entry_for_channel(self, channel: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        channel_id = self._channel_id(channel)
        if channel_id is None:
            return None
        return self._load_state().get(str(channel_id))

    def _save_state(self, state: Dict[str, Dict[str, Any]]) -> None:
        self.db_provider().set_system_setting(STATE_KEY, state)

    def _state_entry(
        self,
        channel: Dict[str, Any],
        reason: str,
        details: Optional[Dict[str, Any]],
    ) -> Dict[str, Any]:
        return {
            "hidden_by": "streamflow",
            "reason": reason,
            "channel_id": self._channel_id(channel),
            "channel_ref": self._channel_ref(channel),
            "hidden_at": self.clock().isoformat(),
            "previous_hidden_from_output": bool(channel.get("hidden_from_output", False)),
            "details": dict(details or {}),
        }

    def _patch_hidden(self, channel: Dict[str, Any], hidden: bool) -> Dict[str, Any]:
        channel_id = self._channel_id(channel)
        try:
            base_url = str(self.base_url_provider() or "").rstrip("/")
            if not base_url:
                return {"success": False, "error": "missing_base_url"}
            response = self.patch_request(
                f"{base_url}/api/channels/channels/{channel_id}/",
                {"hidden_from_output": bool(hidden)},
            )
            status_code = getattr(response, "status_code", 204)
            if status_code not in (200, 204):
                return {"success": False, "error": f"unexpected_status_{status_code}"}
            self._update_udi_channel(channel, hidden)
            return {"success": True}
        except Exception as exc:
            logger.warning("Channel visibility patch failed for channel %s: %s", channel_id, exc)
            return {"success": False, "error": "patch_exception"}

    def _update_udi_channel(self, channel: Dict[str, Any], hidden: bool) -> None:
        try:
            udi = self.udi_provider()
            if not udi or not hasattr(udi, "update_channel"):
                return
            updated = dict(channel)
            updated["hidden_from_output"] = bool(hidden)
            udi.update_channel(self._channel_id(channel), updated)
        except Exception as exc:
            logger.debug("Could not update UDI channel visibility cache: %s", exc)

    @staticmethod
    def _channel_id(channel: Dict[str, Any]) -> Optional[int]:
        try:
            value = channel.get("id")
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _is_hidden(channel: Dict[str, Any]) -> bool:
        return bool(channel.get("hidden_from_output", False))

    def _channel_ref(self, channel: Dict[str, Any]) -> str:
        channel_id = self._channel_id(channel)
        return f"channel-{channel_id}" if channel_id is not None else "channel-unknown"

    def _skipped(self, channel: Dict[str, Any], action: str, reason: str) -> Dict[str, Any]:
        return self._result(channel, action, reason, changed=False, details={})

    def _result(
        self,
        channel: Dict[str, Any],
        action: str,
        reason: str,
        *,
        changed: bool,
        details: Optional[Dict[str, Any]],
    ) -> Dict[str, Any]:
        return {
            "action": action,
            "changed": bool(changed),
            "channel_id": self._channel_id(channel),
            "channel_ref": self._channel_ref(channel),
            "reason": reason,
            "hidden_from_output": action in {"hidden", "hidden_already_managed", "manual_hidden_preserved"},
            "details": dict(details or {}),
        }
