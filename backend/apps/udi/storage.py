"""UDI storage compatibility facade.

Runtime UDI data is owned by the in-memory manager and refreshed from
Dispatcharr. This file keeps the old direct storage API usable for legacy
tests and helper scripts without reintroducing startup persistence.
"""

from pathlib import Path
from typing import Any, Dict, List, Optional

from apps.core.atomic_json import atomic_write_json, load_json_with_backup
from apps.core.logging_config import setup_logging

logger = setup_logging(__name__)
CONFIG_DIR = Path("/app/data")


class UDIStorage:
    """Small local-file facade kept for legacy callers and unit tests."""

    storage_dir: Optional[Path] = None

    def __init__(self, storage_dir: Optional[Path] = None):
        self.storage_dir = Path(storage_dir) if storage_dir else None
        if self.storage_dir:
            self.storage_dir.mkdir(parents=True, exist_ok=True)
        logger.debug("UDIStorage compatibility facade initialized")

    def _path(self, entity: str) -> Optional[Path]:
        if not self.storage_dir:
            return None
        return self.storage_dir / f"{entity}.json"

    def _load_json(self, entity: str) -> List[Dict[str, Any]]:
        path = self._path(entity)
        if not path:
            return []
        try:
            data = load_json_with_backup(
                path,
                default=[],
                validator=lambda value: isinstance(value, list),
            )
            return data if isinstance(data, list) else []
        except Exception as exc:
            logger.warning("Failed to load UDIStorage %s data: %s", entity, exc)
            return []

    def _save_json(self, entity: str, rows: List[Dict[str, Any]]) -> bool:
        path = self._path(entity)
        if not path:
            return True
        try:
            atomic_write_json(path, rows)
            return True
        except Exception as exc:
            logger.warning("Failed to save UDIStorage %s data: %s", entity, exc)
            return False

    def save_channels(self, channels: List[Dict[str, Any]]) -> bool:
        return self._save_json("channels", channels or [])

    def load_channels(self) -> List[Dict[str, Any]]:
        return self._load_json("channels")

    def save_streams(self, streams: List[Dict[str, Any]]) -> bool:
        return self._save_json("streams", streams or [])

    def load_streams(self) -> List[Dict[str, Any]]:
        return self._load_json("streams")

    def get_channel_by_id(self, channel_id: int) -> Optional[Dict[str, Any]]:
        for channel in self.load_channels():
            if channel.get("id") == channel_id:
                return channel
        return None

    def update_channel(self, channel_id: int, channel_data: Dict[str, Any]) -> bool:
        channels = self.load_channels()
        for index, channel in enumerate(channels):
            if channel.get("id") == channel_id:
                channels[index] = channel_data
                return self.save_channels(channels)
        return False

    def clear_all(self) -> bool:
        if not self.storage_dir:
            return True
        ok = True
        paths = {
            *self.storage_dir.glob("*.json"),
            *self.storage_dir.glob("*.json.last-good"),
        }
        for path in paths:
            try:
                path.unlink()
            except Exception as exc:
                logger.warning("Failed to remove UDIStorage file %s: %s", path, exc)
                ok = False
        return ok

    def is_initialized(self) -> bool:
        return bool(self.load_channels())

    # ------------------------------------------------------------------
    # Match-profile pass-throughs (MatchProfilesManager owns its own DB)
    # ------------------------------------------------------------------

    def load_match_profiles(self) -> List[Dict[str, Any]]:
        try:
            from apps.automation.match_profiles_manager import get_match_profiles_manager

            return get_match_profiles_manager().get_all_profiles()
        except Exception:
            return []

    def save_match_profiles(self, profiles: List[Dict[str, Any]]) -> bool:
        return True

    def get_match_profile(self, profile_id: int) -> Optional[Dict[str, Any]]:
        try:
            from apps.automation.match_profiles_manager import get_match_profiles_manager

            return get_match_profiles_manager().get_profile(str(profile_id))
        except Exception:
            return None

    def update_match_profile(self, profile_id: int, profile_data: Dict[str, Any]) -> bool:
        try:
            from apps.automation.match_profiles_manager import get_match_profiles_manager

            return get_match_profiles_manager().update_profile(str(profile_id), profile_data)
        except Exception:
            return False

    def delete_match_profile(self, profile_id: int) -> bool:
        try:
            from apps.automation.match_profiles_manager import get_match_profiles_manager

            return get_match_profiles_manager().delete_profile(str(profile_id))
        except Exception:
            return False
