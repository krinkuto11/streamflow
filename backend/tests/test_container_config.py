from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_DIR = REPO_ROOT / "backend"


def test_dockerfile_uses_the_direct_entrypoint_without_bundled_services():
    content = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert "supervisor" not in content.lower()
    assert "redis-server" not in content.lower()
    assert 'ENTRYPOINT ["/app/entrypoint.sh"]' in content
    assert "gosu" in content
    assert "ENV PUID=99" in content
    assert "ENV PGID=100" in content
    assert "ENV STREAMFLOW_RUN_AS_ROOT=false" in content


def test_dockerfile_installs_the_hashed_production_lock():
    content = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert "COPY backend/requirements.lock ." in content
    assert "pip install --no-cache-dir --require-hashes -r requirements.lock" in content
    assert "COPY backend/requirements.txt ." not in content


def test_production_lock_overrides_base_setuptools_with_hashed_pin():
    requirements = (BACKEND_DIR / "requirements.txt").read_text(encoding="utf-8")
    lock = (BACKEND_DIR / "requirements.lock").read_text(encoding="utf-8")
    test_lock = (BACKEND_DIR / "requirements-test.lock").read_text(encoding="utf-8")

    assert "setuptools==83.0.0" in requirements
    for compiled_lock in (lock, test_lock):
        assert "setuptools==83.0.0" in compiled_lock
        assert (
            "sha256:025bccbbf0fa05b6192bc64ae1e7b16e001fd6d6d4d5de03c97b1c1ade523bef"
            in compiled_lock
        )
        assert (
            "sha256:29b23c360f22f414dc7336bb39178cc7bcbf6021ed2733cde173f09dba19abb3"
            in compiled_lock
        )


def test_test_workflow_installs_locks_and_audits_dependencies():
    content = (REPO_ROOT / ".github" / "workflows" / "tests.yml").read_text(encoding="utf-8")

    assert content.count("pip install --require-hashes -r backend/requirements-test.lock") == 2
    assert "pip-audit -r backend/requirements.lock" in content
    assert "npm audit --audit-level=high" in content


def test_compose_does_not_restore_removed_sidecar_services():
    content = (REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8")

    assert "redis:" not in content
    assert "redis-server" not in content.lower()
    assert "celery-worker:" not in content


def test_entrypoint_execs_the_api_as_pid_one():
    content = (BACKEND_DIR / "entrypoint.sh").read_text(encoding="utf-8")

    assert "supervisord" not in content
    assert "exec python3 apps/api/web_api.py" in content
    assert 'exec gosu "$PUID:$PGID" "$0" "$@"' in content
    assert 'chown -R "$PUID:$PGID" csv logs "$CONFIG_DIR"' in content
    assert 'find "$CONFIG_DIR" -maxdepth 1 -type f -exec chmod 600 {} +' in content


def test_compose_exposes_unraid_compatible_runtime_identity_defaults():
    content = (REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8")

    assert "PUID=99" in content
    assert "PGID=100" in content


def test_removed_supervisor_config_stays_removed():
    assert not (BACKEND_DIR / "supervisord.conf").exists()
