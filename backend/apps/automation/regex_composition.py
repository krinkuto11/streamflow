"""Compose channel regex alternatives without leaking global inline flags."""

import re
from typing import Sequence


_GLOBAL_FLAGS = re.compile(r"\(\?([aiLmsux]+)\)")
_VERBOSE_WHITESPACE = " \t\n\r\v\f"


def _group_alternative(pattern: str) -> str:
    # Global flags cannot appear inside the non-capturing group used for an OR
    # alternative. Move only the leading flag declarations into that group's
    # scope; never replace flag-like text in the actual expression.
    cursor = 0
    flags = ""
    while cursor < len(pattern):
        declaration = _GLOBAL_FLAGS.match(pattern, cursor)
        if declaration:
            flags += declaration.group(1)
            cursor = declaration.end()
            continue
        if pattern.startswith("(?#", cursor):
            end = pattern.find(")", cursor + 3)
            if end >= 0:
                cursor = end + 1
                continue
        if "x" in flags:
            if pattern[cursor] in _VERBOSE_WHITESPACE:
                cursor += 1
                continue
            if pattern[cursor] == "#":
                end = pattern.find("\n", cursor)
                cursor = len(pattern) if end < 0 else end + 1
                continue
        break

    if not flags:
        return f"(?:{pattern})"

    scoped_flags = "".join(dict.fromkeys(flags))
    expression = pattern[cursor:]
    # An end-of-line comment in verbose mode must not consume our closing
    # parentheses or a following alternative. This newline is ignored by x.
    if "x" in flags:
        expression += "\n"
    return f"(?{scoped_flags}:{expression})"


def combine_regex_patterns(patterns: Sequence[str]) -> str:
    """Keep single patterns intact and scope flags per multi-pattern branch.

    The existing outer capture and branch order are retained for compatibility.
    Patterns without global inline flags produce exactly the legacy output.
    """
    if len(patterns) == 1:
        return patterns[0]
    return "(" + "|".join(_group_alternative(pattern) for pattern in patterns) + ")"
