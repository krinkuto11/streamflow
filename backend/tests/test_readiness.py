from unittest.mock import Mock

from flask import Flask
from sqlalchemy import create_engine, text

from apps.api.meta_handlers import readiness_check_response


def make_engine(*, complete_schema=True):
    engine = create_engine("sqlite://")
    table_names = (
        ("channels", "streams", "system_settings", "monitoring_sessions")
        if complete_schema
        else ("channels",)
    )
    with engine.begin() as connection:
        for table_name in table_names:
            connection.execute(text(f'CREATE TABLE "{table_name}" (id INTEGER)'))
    return engine


def fake_udi(*, network_ready=True, pending=False):
    udi = Mock()
    udi.is_initialized.return_value = True
    udi.is_network_ready.return_value = network_ready
    udi.is_initialization_pending.return_value = pending
    udi.get_init_progress.return_value = {
        "status": "completed" if network_ready else "in_progress",
        "percentage": 100 if network_ready else 60,
        "message": "Initialization complete" if network_ready else "Fetching data",
    }
    udi.get_status.return_value = {
        "data_counts": {"channels": 2, "streams": 4, "m3u_accounts": 1}
    }
    return udi


def call_readiness(*, engine=None, configured=True, udi=None, services=None):
    app = Flask(__name__)
    config = Mock()
    config.is_configured.return_value = configured
    with app.app_context():
        response, status = readiness_check_response(
            get_engine=lambda: engine or make_engine(),
            get_dispatcharr_config=lambda: config,
            get_udi_manager=lambda: udi or fake_udi(),
            get_required_services_status=lambda: services or {
                "monitoring": {"required": True, "ready": True, "state": "running"},
                "shadow": {"required": False, "ready": True, "state": "disabled"},
            },
        )
        return response.get_json(), status


def test_readiness_is_ready_only_after_all_required_checks_pass():
    payload, status = call_readiness()

    assert status == 200
    assert payload["status"] == "ready"
    assert payload["ready"] is True
    assert payload["checks"]["database"]["schema_ready"] is True
    assert payload["checks"]["udi"]["network_ready"] is True
    assert payload["checks"]["services"]["ready"] is True


def test_readiness_reports_missing_schema_without_exposing_exception_details():
    payload, status = call_readiness(engine=make_engine(complete_schema=False))

    assert status == 503
    assert payload["ready"] is False
    assert payload["checks"]["database"]["missing_tables"] == [
        "monitoring_sessions",
        "streams",
        "system_settings",
    ]


def test_readiness_waits_for_dispatcharr_and_live_udi_cache():
    payload, status = call_readiness(configured=False, udi=fake_udi(network_ready=False, pending=True))

    assert status == 503
    assert payload["checks"]["dispatcharr_config"]["reason"] == "setup_required"
    assert payload["checks"]["udi"]["initialization_pending"] is True
    assert payload["initialization"]["percentage"] == 60


def test_readiness_ignores_disabled_optional_service_but_rejects_stopped_required_service():
    optional_payload, optional_status = call_readiness(services={
        "shadow": {"required": False, "ready": True, "state": "disabled"},
    })
    required_payload, required_status = call_readiness(services={
        "monitoring": {"required": True, "ready": False, "state": "stopped"},
    })

    assert optional_status == 200
    assert optional_payload["ready"] is True
    assert required_status == 503
    assert required_payload["checks"]["services"]["ready"] is False
