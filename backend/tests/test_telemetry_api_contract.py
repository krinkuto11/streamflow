from types import SimpleNamespace

from flask import Flask

from apps.telemetry import telemetry_api


class _Query:
    def __init__(self, rows):
        self.rows = rows

    def join(self, *_args, **_kwargs):
        return self

    def filter(self, *_args, **_kwargs):
        return self

    def group_by(self, *_args, **_kwargs):
        return self

    def order_by(self, *_args, **_kwargs):
        return self

    def all(self):
        return self.rows


class _Session:
    def __init__(self, query_rows):
        self.query_rows = iter(query_rows)

    def query(self, *_args, **_kwargs):
        return _Query(next(self.query_rows))

    def close(self):
        return None


def _client():
    app = Flask(__name__)
    app.register_blueprint(telemetry_api.telemetry_bp, url_prefix="/api/telemetry")
    return app.test_client()


def test_requested_days_is_bounded_to_retained_history():
    app = Flask(__name__)
    with app.test_request_context("/?days=30"):
        assert telemetry_api._requested_days() == 7
    with app.test_request_context("/?days=0"):
        assert telemetry_api._requested_days() == 1
    with app.test_request_context("/?days=invalid"):
        assert telemetry_api._requested_days() == 7


def test_provider_payload_has_correct_availability_name_and_legacy_alias(monkeypatch):
    provider = SimpleNamespace(
        provider_id=4,
        total_streams=10,
        dead_streams=2,
        avg_bitrate_kbps=4500,
        avg_fps=50,
        avg_quality_score=0.8,
        avg_res_height=1080,
    )
    monkeypatch.setattr(
        telemetry_api,
        "get_session",
        lambda: _Session([[provider], []]),
    )
    monkeypatch.setattr(
        "apps.udi.get_udi_manager",
        lambda: SimpleNamespace(get_m3u_accounts=lambda: [{"id": 4, "name": "Provider 4"}]),
    )

    response = _client().get("/api/telemetry/providers?days=30")

    assert response.status_code == 200
    item = response.get_json()["data"][0]
    assert item["availability_percentage"] == 80
    assert item["availability_pecentage"] == 80
