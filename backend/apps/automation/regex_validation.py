"""Regex validation helpers for channel matching."""

from typing import Any, Iterable, List, Tuple


def is_dangerous_regex(pattern: str) -> bool:
    """Return True if the regex pattern contains nested quantifiers (ReDoS risk)."""
    inside_parens = False
    has_inner_quantifier = False
    for i, char in enumerate(pattern):
        if i > 0 and pattern[i - 1] == "\\":
            continue
        if char == "(":
            inside_parens = True
            has_inner_quantifier = False
        elif char == ")":
            if inside_parens and has_inner_quantifier:
                if i + 1 < len(pattern) and pattern[i + 1] in "+*":
                    return True
            inside_parens = False
        elif inside_parens and char in "+*":
            has_inner_quantifier = True
    return False


def validate_regex_patterns(patterns: Iterable[Any]) -> Tuple[bool, List[str]]:
    """Validate regex payload values and return a tuple of (is_valid, errors)."""
    errors: List[str] = []
    for index, pattern in enumerate(patterns):
        if isinstance(pattern, dict):
            regex_pattern = pattern.get("pattern", "")
        else:
            regex_pattern = pattern

        if not isinstance(regex_pattern, str) or not regex_pattern.strip():
            errors.append(f"Pattern at index {index} must be a non-empty string")
            continue

        if is_dangerous_regex(regex_pattern):
            errors.append(f"Pattern at index {index} contains dangerous nested quantifiers")

    return len(errors) == 0, errors
