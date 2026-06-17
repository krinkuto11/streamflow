from flask import Flask

from apps.api.regex_handlers import (
    get_regex_global_settings_response,
    update_regex_global_settings_response,
)


class FakeDb:
    def __init__(self, initial=None):
        self.value = initial

    def get_system_setting(self, _key, default=None):
        return self.value if self.value is not None else default

    def set_system_setting(self, _key, value):
        self.value = value
        return True


class FakeMatcher:
    def __init__(self):
        self.reloads = 0

    def reload_patterns(self):
        self.reloads += 1


def test_regex_global_settings_default_case_sensitive(monkeypatch):
    app = Flask(__name__)
    db = FakeDb()

    monkeypatch.setattr("apps.database.manager.get_db_manager", lambda: db)

    with app.app_context():
        response = get_regex_global_settings_response()

    data = response.get_json()
    assert data == {"case_sensitive": True, "require_exact_match": False}


def test_regex_global_settings_update_preserves_unsubmitted_keys(monkeypatch):
    app = Flask(__name__)
    db = FakeDb({"case_sensitive": False, "require_exact_match": False})
    matcher = FakeMatcher()

    monkeypatch.setattr("apps.database.manager.get_db_manager", lambda: db)

    with app.app_context():
        response = update_regex_global_settings_response(
            payload={"case_sensitive": True},
            get_regex_matcher=lambda: matcher,
        )

    data = response.get_json()
    assert data["settings"] == {"case_sensitive": True, "require_exact_match": False}
    assert db.value == {"case_sensitive": True, "require_exact_match": False}
    assert matcher.reloads == 1


def test_regex_global_settings_rejects_unknown_keys(monkeypatch):
    app = Flask(__name__)
    db = FakeDb()

    monkeypatch.setattr("apps.database.manager.get_db_manager", lambda: db)

    with app.app_context():
        response, status = update_regex_global_settings_response(
            payload={"exactly": True},
            get_regex_matcher=lambda: FakeMatcher(),
        )

    assert status == 400
    assert response.get_json()["keys"] == ["exactly"]
