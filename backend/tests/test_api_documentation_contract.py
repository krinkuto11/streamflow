import re
from pathlib import Path

from apps.api.web_api import app


REPO_ROOT = Path(__file__).resolve().parents[2]
TABLE_ROUTE = re.compile(r"^\|\s*(GET|POST|PUT|PATCH|DELETE)\s*\|\s*`([^`]+)`\s*\|")


def _documented_routes():
    routes = set()
    for line in (REPO_ROOT / "docs" / "API.md").read_text(encoding="utf-8").splitlines():
        match = TABLE_ROUTE.match(line)
        if match:
            routes.add((match.group(1), match.group(2)))
    return routes


def _registered_routes():
    return {
        (method, rule.rule)
        for rule in app.url_map.iter_rules()
        for method in rule.methods
        if method not in {"HEAD", "OPTIONS"}
    }


def test_every_documented_api_method_and_path_is_registered():
    documented = _documented_routes()
    missing = documented - _registered_routes()

    assert documented
    assert not missing, f"API.md contains unregistered routes: {sorted(missing)}"
