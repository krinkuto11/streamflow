import json
import os
import threading

import pytest

from apps.core.atomic_json import (
    atomic_write_json,
    last_known_good_path,
    load_json_with_backup,
)


def test_atomic_write_creates_valid_primary_and_last_known_good(tmp_path):
    path = tmp_path / "config.json"

    atomic_write_json(path, {"enabled": True}, sort_keys=True)

    assert json.loads(path.read_text(encoding="utf-8")) == {"enabled": True}
    assert json.loads(last_known_good_path(path).read_text(encoding="utf-8")) == {
        "enabled": True,
    }
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600


def test_failed_replace_preserves_existing_document_and_cleans_temp(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    atomic_write_json(path, {"version": 1})
    original_replace = os.replace

    def fail_primary_replace(source, destination):
        if str(destination) == str(path):
            raise OSError("simulated interrupted replace")
        return original_replace(source, destination)

    monkeypatch.setattr("apps.core.atomic_json.os.replace", fail_primary_replace)

    with pytest.raises(OSError, match="interrupted replace"):
        atomic_write_json(path, {"version": 2})

    assert json.loads(path.read_text(encoding="utf-8")) == {"version": 1}
    assert not list(tmp_path.glob(".config.json.*.tmp"))


def test_load_uses_last_known_good_when_primary_is_corrupt(tmp_path):
    path = tmp_path / "config.json"
    atomic_write_json(path, {"version": 1})
    atomic_write_json(path, {"version": 2})
    path.write_text("{broken", encoding="utf-8")

    assert load_json_with_backup(path) == {"version": 1}


def test_concurrent_writes_leave_one_complete_document(tmp_path):
    path = tmp_path / "state.json"
    threads = [
        threading.Thread(target=atomic_write_json, args=(path, {"writer": index}))
        for index in range(12)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert json.loads(path.read_text(encoding="utf-8"))["writer"] in range(12)
    assert load_json_with_backup(path)["writer"] in range(12)
