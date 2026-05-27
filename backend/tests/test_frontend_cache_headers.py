from pathlib import Path

from flask import Flask

from apps.api.meta_handlers import root_response, serve_frontend_response


def _static_dir(tmp_path: Path) -> Path:
    static_dir = tmp_path / "static"
    static_dir.mkdir()
    (static_dir / "index.html").write_text("<html><body>ok</body></html>", encoding="utf-8")
    return static_dir


def test_root_frontend_shell_disables_browser_cache(tmp_path):
    app = Flask(__name__)
    static_dir = _static_dir(tmp_path)

    with app.test_request_context("/"):
        response = root_response(static_folder=static_dir)

    assert "no-store" in response.headers["Cache-Control"]
    assert response.headers["Pragma"] == "no-cache"
    assert response.headers["Expires"] == "0"


def test_spa_fallback_shell_disables_browser_cache(tmp_path):
    app = Flask(__name__)
    static_dir = _static_dir(tmp_path)

    with app.test_request_context("/dashboard"):
        response = serve_frontend_response(static_folder=static_dir, path="dashboard")

    assert "no-store" in response.headers["Cache-Control"]
    assert response.headers["Pragma"] == "no-cache"
    assert response.headers["Expires"] == "0"
