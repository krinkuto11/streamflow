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
