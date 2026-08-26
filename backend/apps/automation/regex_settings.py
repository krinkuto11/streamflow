"""Shared defaults and helpers for channel regex matching settings."""

from copy import deepcopy
from typing import Any, Dict, Optional


DEFAULT_CHANNEL_REGEX_GLOBAL_SETTINGS: Dict[str, Any] = {
    "case_sensitive": True,
    "require_exact_match": False,
}


def default_channel_regex_global_settings() -> Dict[str, Any]:
    return deepcopy(DEFAULT_CHANNEL_REGEX_GLOBAL_SETTINGS)


def normalize_channel_regex_global_settings(
    settings: Optional[Dict[str, Any]] = None,
    *,
    base: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    normalized = default_channel_regex_global_settings()
    if isinstance(base, dict):
        normalized.update({
            key: bool(base[key])
            for key in normalized
            if key in base
        })
    if isinstance(settings, dict):
        normalized.update({
            key: bool(settings[key])
            for key in normalized
            if key in settings
        })
    return normalized
