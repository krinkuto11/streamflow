"""
UDI storage stub — the SQL persistence layer has been removed.

The UDI now operates as a pure in-memory cache populated on every startup via
the Dispatcharr API.  Because Streamflow cannot function without a live
Dispatcharr connection, the warm-start benefit of SQLite persistence did not
justify the complexity cost.

This stub is kept so existing imports (tests, match_profiles_manager) do not
break immediately.  Match-profile methods delegate to MatchProfilesManager
which talks to the real DB directly and is unaffected.
"""

from pathlib import Path
from typing import Any, Dict, List, Optional

from apps.core.logging_config import setup_logging

logger = setup_logging(__name__)


class UDIStorage:
    """No-op stub.  All persistence methods are removed; UDI is pure memory."""

    storage_dir: Optional[Path] = None

    def __init__(self, storage_dir: Optional[Path] = None):
        logger.debug("UDIStorage is a no-op stub — UDI runs in pure memory")

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
