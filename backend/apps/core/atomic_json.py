"""Crash-safe JSON persistence helpers for runtime configuration and state."""

from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any, Callable, Optional


_path_locks_guard = threading.Lock()
_path_locks: dict[str, threading.RLock] = {}


def _path_lock(path: Path) -> threading.RLock:
    key = str(path.resolve())
    with _path_locks_guard:
        return _path_locks.setdefault(key, threading.RLock())


def last_known_good_path(path: Path) -> Path:
    return path.with_name(f"{path.name}.last-good")


def _write_bytes_atomic(path: Path, payload: bytes, *, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Optional[Path] = None
    try:
        descriptor, temp_name = tempfile.mkstemp(
            dir=str(path.parent),
            prefix=f".{path.name}.",
            suffix=".tmp",
        )
        temp_path = Path(temp_name)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_path, mode)
        os.replace(temp_path, path)
        temp_path = None
        try:
            directory_fd = os.open(str(path.parent), os.O_RDONLY)
        except (AttributeError, OSError):
            directory_fd = None
        if directory_fd is not None:
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def atomic_write_json(
    path: Path,
    payload: Any,
    *,
    mode: int = 0o600,
    indent: int = 2,
    sort_keys: bool = False,
    backup: bool = True,
) -> None:
    """Serialize JSON completely before atomically replacing the destination."""
    path = Path(path)
    encoded = (
        json.dumps(payload, indent=indent, sort_keys=sort_keys, ensure_ascii=True)
        + "\n"
    ).encode("utf-8")
    with _path_lock(path):
        if backup and path.exists():
            try:
                current = path.read_bytes()
                json.loads(current.decode("utf-8"))
                _write_bytes_atomic(last_known_good_path(path), current, mode=mode)
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                pass
        _write_bytes_atomic(path, encoded, mode=mode)
        if backup and not last_known_good_path(path).exists():
            _write_bytes_atomic(last_known_good_path(path), encoded, mode=mode)


def load_json_with_backup(
    path: Path,
    *,
    default: Any = None,
    validator: Optional[Callable[[Any], bool]] = None,
) -> Any:
    """Load the primary JSON document, falling back to its last-known-good copy."""
    path = Path(path)
    for candidate in (path, last_known_good_path(path)):
        try:
            value = json.loads(candidate.read_text(encoding="utf-8"))
            if validator is None or validator(value):
                return value
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
    return default
