from apps.core.secret_sources import external_secret_source, read_external_secret


def test_secret_file_takes_precedence_over_environment(tmp_path, monkeypatch):
    secret_file = tmp_path / "secret"
    secret_file.write_text("from-file\n", encoding="utf-8")
    monkeypatch.setenv("TEST_SECRET", "from-env")
    monkeypatch.setenv("TEST_SECRET_FILE", str(secret_file))

    assert read_external_secret("TEST_SECRET", "TEST_SECRET_FILE") == "from-file"
    assert external_secret_source("TEST_SECRET", "TEST_SECRET_FILE") == "file"


def test_missing_secret_file_does_not_fall_back_to_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_SECRET", "from-env")
    monkeypatch.setenv("TEST_SECRET_FILE", str(tmp_path / "missing"))

    assert read_external_secret("TEST_SECRET", "TEST_SECRET_FILE") is None
    assert external_secret_source("TEST_SECRET", "TEST_SECRET_FILE") == "file"
