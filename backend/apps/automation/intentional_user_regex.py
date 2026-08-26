"""Intentional execution helper for user-configured channel regex patterns."""

import re
from typing import Optional


def search_user_regex(
    pattern: str,
    value: str,
    *,
    case_sensitive: bool = True,
) -> Optional[re.Match[str]]:
    """Run a user-configured regex after callers have applied safety validation."""
    flags = 0 if case_sensitive else re.IGNORECASE
    return re.compile(pattern, flags).search(value)
