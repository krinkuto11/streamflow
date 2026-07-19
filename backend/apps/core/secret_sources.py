"""Helpers for Docker/Unraid-managed secret values."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional


def read_external_secret(env_name: str, file_env_name: str) -> Optional[str]:
    """Read a secret file first, then a direct environment value."""
    file_path = (os.getenv(file_env_name) or "").strip()
    if file_path:
        try:
            value = Path(file_path).read_text(encoding="utf-8").strip()
        except OSError:
            return None
        return value or None

    value = os.getenv(env_name)
    if value is None:
        return None
    return value.strip() or None


def external_secret_source(env_name: str, file_env_name: str) -> Optional[str]:
    if (os.getenv(file_env_name) or "").strip():
        return "file"
    if (os.getenv(env_name) or "").strip():
        return "environment"
    return None
