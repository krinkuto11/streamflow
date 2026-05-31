"""Legacy channel/group settings facade used by older tests and scripts.

V2 resolves channel behaviour through automation profiles. This module keeps
the previous explicit channel/group setting API available as a small JSON-backed
compatibility layer.
"""

import json
import os
from pathlib import Path
from typing import Dict, Optional


DEFAULT_MODE = "enabled"


class ChannelSettingsManager:
    def __init__(self, config_file: Optional[Path] = None):
        config_dir = Path(os.environ.get("CONFIG_DIR", "/app/data"))
        self.config_file = Path(config_file) if config_file else config_dir / "channel_settings.json"
        self._settings = self._load()

    def _load(self) -> Dict:
        if not self.config_file.exists():
            return {"channels": {}, "groups": {}}
        try:
            data = json.loads(self.config_file.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return {"channels": {}, "groups": {}}
            data.setdefault("channels", {})
            data.setdefault("groups", {})
            return data
        except Exception:
            return {"channels": {}, "groups": {}}

    def _save(self) -> bool:
        self.config_file.parent.mkdir(parents=True, exist_ok=True)
        self.config_file.write_text(json.dumps(self._settings, indent=2), encoding="utf-8")
        return True

    def get_group_settings(self, group_id: int) -> Dict[str, str]:
        stored = self._settings.get("groups", {}).get(str(group_id), {})
        return {
            "matching_mode": stored.get("matching_mode", DEFAULT_MODE),
            "checking_mode": stored.get("checking_mode", DEFAULT_MODE),
        }

    def set_group_settings(
        self,
        group_id: int,
        *,
        matching_mode: Optional[str] = None,
        checking_mode: Optional[str] = None,
    ) -> bool:
        groups = self._settings.setdefault("groups", {})
        settings = groups.setdefault(str(group_id), {})
        if matching_mode is not None:
            settings["matching_mode"] = matching_mode
        if checking_mode is not None:
            settings["checking_mode"] = checking_mode
        return self._save()

    def get_all_group_settings(self) -> Dict[int, Dict[str, str]]:
        return {int(group_id): self.get_group_settings(int(group_id)) for group_id in self._settings.get("groups", {})}

    def is_group_matching_enabled(self, group_id: int) -> bool:
        return self.get_group_settings(group_id)["matching_mode"] != "disabled"

    def is_group_checking_enabled(self, group_id: int) -> bool:
        return self.get_group_settings(group_id)["checking_mode"] != "disabled"

    def is_channel_enabled_by_group(self, group_id: Optional[int], mode: str = "matching") -> bool:
        if group_id is None:
            return True
        key = "checking_mode" if mode == "checking" else "matching_mode"
        return self.get_group_settings(group_id).get(key, DEFAULT_MODE) != "disabled"

    def get_channel_settings(self, channel_id: int) -> Dict[str, str]:
        return dict(self._settings.get("channels", {}).get(str(channel_id), {}))

    def set_channel_settings(
        self,
        channel_id: int,
        *,
        matching_mode: Optional[str] = None,
        checking_mode: Optional[str] = None,
    ) -> bool:
        channels = self._settings.setdefault("channels", {})
        settings = channels.setdefault(str(channel_id), {})
        if matching_mode is not None:
            settings["matching_mode"] = matching_mode
        if checking_mode is not None:
            settings["checking_mode"] = checking_mode
        return self._save()

    def get_channel_effective_settings(self, channel_id: int, channel_group_id: Optional[int] = None) -> Dict[str, object]:
        channel_settings = self.get_channel_settings(channel_id)
        group_settings = self.get_group_settings(channel_group_id) if channel_group_id is not None else {}

        effective = {}
        for key in ("matching_mode", "checking_mode"):
            source_key = key.replace("_mode", "_mode_source")
            explicit_key = "has_explicit_" + key.replace("_mode", "")
            if key in channel_settings:
                effective[key] = channel_settings[key]
                effective[source_key] = "channel"
                effective[explicit_key] = True
            elif key in group_settings:
                effective[key] = group_settings[key]
                effective[source_key] = "group"
                effective[explicit_key] = False
            else:
                effective[key] = DEFAULT_MODE
                effective[source_key] = "default"
                effective[explicit_key] = False

        return effective

    def is_matching_enabled(self, channel_id: int, channel_group_id: Optional[int] = None) -> bool:
        return self.get_channel_effective_settings(channel_id, channel_group_id)["matching_mode"] != "disabled"

    def is_checking_enabled(self, channel_id: int, channel_group_id: Optional[int] = None) -> bool:
        return self.get_channel_effective_settings(channel_id, channel_group_id)["checking_mode"] != "disabled"


_channel_settings_manager: Optional[ChannelSettingsManager] = None


def get_channel_settings_manager() -> ChannelSettingsManager:
    global _channel_settings_manager
    if _channel_settings_manager is None:
        _channel_settings_manager = ChannelSettingsManager()
    return _channel_settings_manager
