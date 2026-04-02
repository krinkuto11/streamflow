"""Setup wizard API handler functions extracted from web_api."""

import threading
from typing import Any, Callable

from flask import jsonify

from apps.core.logging_config import setup_logging

logger = setup_logging(__name__)


def get_setup_wizard_status_response(
    *,
    test_mode: bool,
    get_automation_config_manager: Callable[[], Any],
    get_dispatcharr_config: Callable[[], Any],
    get_udi_manager: Callable[[], Any],
):
    """Handle setup wizard status retrieval."""
    try:
        from apps.database.manager import get_db_manager

        manager = get_automation_config_manager()
        db = get_db_manager()

        automation_config_exists = False
        try:
            manager.get_global_settings()
            automation_config_exists = True
        except Exception:
            automation_config_exists = False

        regex_global_settings = db.get_system_setting("channel_regex_global_settings", None)
        regex_configs = db.get_all_channel_regex_configs()

        status = {
            "automation_config_exists": automation_config_exists,
            "regex_config_exists": regex_global_settings is not None,
            "has_patterns": bool(regex_configs),
            "has_channels": False,
            "dispatcharr_connection": False,
        }

        if test_mode:
            status["dispatcharr_connection"] = True
            status["has_channels"] = True
        else:
            dispatcharr_config = get_dispatcharr_config()
            if dispatcharr_config.is_configured():
                try:
                    udi = get_udi_manager()
                    status["dispatcharr_connection"] = udi.fetcher.test_connection()
                    if status["dispatcharr_connection"]:
                        if not udi.is_initialized():
                            threading.Thread(
                                target=udi.initialize,
                                kwargs={"force_refresh": False},
                                daemon=True,
                            ).start()
                        status["has_channels"] = bool(getattr(udi, "_channels_cache", []))
                except Exception as exc:
                    logger.warning(f"Error checking Dispatcharr connection: {exc}")

        status["setup_complete"] = get_dispatcharr_config().is_configured() or test_mode
        return jsonify(status)
    except Exception as exc:
        logger.error(f"Error getting setup wizard status: {exc}")
        return jsonify({"error": "Internal Server Error"}), 500


def ensure_wizard_config_response(*, get_automation_config_manager: Callable[[], Any]):
    """Handle setup wizard SQL defaults creation."""
    try:
        from apps.database.manager import get_db_manager

        manager = get_automation_config_manager()
        db = get_db_manager()

        automation_defaults = {
            "regular_automation_enabled": False,
            "validate_existing_streams": False,
            "playlist_update_interval_minutes": {"type": "interval", "value": 5},
            "channel_assignments": {},
            "group_assignments": {},
            "channel_period_assignments": {},
        }
        for key, value in automation_defaults.items():
            if db.get_system_setting(key, None) is None:
                db.set_system_setting(key, value)

        manager.get_global_settings()

        if db.get_system_setting("channel_regex_global_settings", None) is None:
            db.set_system_setting(
                "channel_regex_global_settings",
                {
                    "case_sensitive": False,
                    "require_exact_match": False,
                },
            )

        return jsonify({"message": "Configuration defaults ensured in SQL"})
    except Exception as exc:
        logger.error(f"Error ensuring wizard config: {exc}")
        return jsonify({"error": "Internal Server Error"}), 500


def create_sample_patterns_response():
    """Handle creation of sample regex patterns for setup completion."""
    try:
        from apps.database.manager import get_db_manager

        db = get_db_manager()

        patterns = {
            "patterns": {
                "1": {
                    "name": "News Channels",
                    "regex_patterns": [
                        {"pattern": ".*News.*", "priority": 0},
                        {"pattern": ".*CNN.*", "priority": 1},
                        {"pattern": ".*BBC.*", "priority": 2},
                    ],
                    "enabled": True,
                },
                "2": {
                    "name": "Sports Channels",
                    "regex_patterns": [
                        {"pattern": ".*Sport.*", "priority": 0},
                        {"pattern": ".*ESPN.*", "priority": 1},
                        {"pattern": ".*Fox Sports.*", "priority": 2},
                    ],
                    "enabled": True,
                },
            },
            "global_settings": {
                "case_sensitive": False,
                "require_exact_match": False,
            },
        }

        imported, errors = db.import_channel_regex_configs_from_json(patterns, merge=False)
        db.set_system_setting("channel_regex_global_settings", patterns["global_settings"])

        if errors:
            return (
                jsonify(
                    {
                        "message": "Sample patterns created with warnings",
                        "imported": imported,
                        "warnings": errors,
                    }
                ),
                200,
            )

        return jsonify({"message": "Sample patterns created successfully", "imported": imported})
    except Exception as exc:
        logger.error(f"Error creating sample patterns: {exc}")
        return jsonify({"error": "Internal Server Error"}), 500
