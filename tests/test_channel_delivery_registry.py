from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from types import SimpleNamespace

from backend.app.main import app  # noqa: F401 - register metadata
from backend.app.core.database.base import Base
from backend.app.modules.ai_agent.employee_builder import (
    blueprint_readiness,
    build_employee_blueprint,
)
from backend.app.modules.ai_agent.self_service_policy import (
    configured_channel_types,
    self_service_spec_view,
)
from backend.app.modules.channels.catalog import (
    CHANNEL_RUNTIME_LIVE,
    CHANNEL_SETUP_MANAGED,
    N8N_CHANNEL_ADAPTER,
    CHANNEL_SETUP_SELF_SERVICE,
    canonical_channel_type,
    get_channel_capability,
    list_customer_channel_capabilities,
)
from backend.app.modules.channels.delivery import reconcile_managed_channel_requests
from backend.app.modules.channels.models import AgentChannel
from backend.app.api.admin_channels import _activation_blockers
from backend.app.api.customer_inbox import LIVE_INBOX_CHANNELS
from backend.app.api.public_employee_builder import public_employee_builder_channels


def test_channel_registry_covers_replit_style_employee_surfaces():
    items = {item["type"]: item for item in list_customer_channel_capabilities()}

    expected = {
        "xvond",
        "website",
        "whatsapp",
        "voice",
        "telegram",
        "instagram",
        "messenger",
        "email",
        "sms",
        "slack",
        "teams",
        "custom",
    }
    assert expected.issubset(items)

    assert items["website"]["setup_mode"] == CHANNEL_SETUP_SELF_SERVICE
    assert items["website"]["runtime_state"] == CHANNEL_RUNTIME_LIVE
    assert items["whatsapp"]["setup_mode"] == CHANNEL_SETUP_SELF_SERVICE
    assert items["voice"]["setup_mode"] == CHANNEL_SETUP_MANAGED
    assert items["voice"]["runtime_state"] == CHANNEL_RUNTIME_LIVE

    for key in expected - {"xvond", "website", "whatsapp", "voice"}:
        assert items[key]["setup_mode"] == CHANNEL_SETUP_MANAGED
        assert items[key]["runtime_state"] == CHANNEL_RUNTIME_LIVE
        assert items[key]["runtime_adapter"] == N8N_CHANNEL_ADAPTER


def test_channel_aliases_are_canonical_and_do_not_create_parallel_channel_types():
    assert canonical_channel_type("instagram_dm") == "instagram"
    assert canonical_channel_type("facebook_messenger") == "messenger"
    assert canonical_channel_type("microsoft teams") == "teams"
    assert canonical_channel_type("phone") == "voice"
    assert canonical_channel_type("web chat") == "website"


def test_public_channel_catalog_exposes_delivery_truth_without_configs_or_secrets():
    payload = public_employee_builder_channels()
    items = {item["type"]: item for item in payload["channels"]}

    assert items["website"]["availability"] == "self_service_live"
    assert items["voice"]["availability"] == "xvond_managed_live"
    assert items["telegram"]["availability"] == "xvond_managed_live"
    assert items["telegram"]["packaged_provider"] is True
    assert items["instagram"]["availability"] == "xvond_managed_live"
    assert items["instagram"]["packaged_provider"] is True
    assert items["messenger"]["availability"] == "xvond_managed_live"
    assert items["messenger"]["packaged_provider"] is True
    assert items["slack"]["availability"] == "xvond_managed_live"
    assert items["slack"]["packaged_provider"] is True
    assert items["custom"]["availability"] == "xvond_managed_live"
    assert items["custom"]["packaged_provider"] is True
    assert items["sms"]["availability"] == "xvond_managed_live"
    assert items["sms"]["packaged_provider"] is True
    assert items["xvond"]["availability"] == "built_in"

    for item in payload["channels"]:
        assert "config_fields" not in item
        assert "access_token" not in str(item)
        assert "bot_token" not in str(item)


def test_open_ended_builder_detects_managed_and_self_service_channels():
    blueprint = build_employee_blueprint(
        "بدي موظف يرد على واتساب ورسائل انستغرام، يتصل هاتفيًا، "
        "ويتابع Telegram وSlack ويرد على العملاء بالإيميل وSMS"
    )

    for key in ("whatsapp", "instagram", "voice", "telegram", "slack", "email", "sms"):
        assert key in blueprint.channels

    readiness = blueprint_readiness(blueprint)
    assert readiness["channels"]["whatsapp"] == "connect_required"
    assert readiness["channels"]["voice"] == "xvond_managed_setup"
    assert readiness["channels"]["instagram"] == "xvond_managed_setup"
    assert readiness["channels"]["telegram"] == "xvond_managed_setup"


def test_compiler_cached_channel_view_uses_registry_delivery_truth():
    spec = {
        "scope": "business",
        "requirements": [
            {"key": "voice", "kind": "channel", "status": "connection_required"},
            {"key": "telegram", "kind": "channel", "status": "connection_required"},
            {"key": "instagram_dm", "kind": "channel", "status": "connection_required"},
            {"key": "email_send", "kind": "integration", "status": "connection_required"},
        ],
        "delivery": {"provisioning_version": 1},
    }
    rendered = self_service_spec_view(spec)
    rows = {item["key"]: item for item in rendered["requirements"]}

    assert rows["voice"]["self_service_connection_status"] == "xvond_managed_available"
    assert rows["voice"]["channel_delivery"]["runtime_state"] == "live"
    assert rows["telegram"]["self_service_connection_status"] == "xvond_managed_available"
    assert rows["telegram"]["channel_delivery"]["runtime_adapter"] == "xvond_managed"
    assert "n8n" not in str(rendered).lower()
    assert rows["instagram_dm"]["self_service_connection_status"] == "xvond_managed_available"
    assert rows["instagram_dm"]["channel_delivery"]["type"] == "instagram"
    assert rows["email_send"]["self_service_connection_status"] == "self_service_integration_available"


def test_managed_channel_requests_are_durable_and_cancel_with_job_contract():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine, autoflush=False) as db:
        first = reconcile_managed_channel_requests(
            db,
            company_id=11,
            agent_id=22,
            desired_channel_types=["voice", "telegram", "instagram", "website"],
        )
        assert first["desired"] == ["instagram", "telegram", "voice"]
        assert set(first["requested"]) == {"instagram", "telegram", "voice"}

        rows = db.query(AgentChannel).order_by(AgentChannel.channel_type).all()
        assert [row.channel_type for row in rows] == ["instagram", "telegram", "voice"]
        assert all(row.enabled is False for row in rows)

        second = reconcile_managed_channel_requests(
            db,
            company_id=11,
            agent_id=22,
            desired_channel_types=["voice"],
            request_source="job_brief_revision",
        )
        assert set(second["cancelled"]) == {"instagram", "telegram"}

        states = {
            row.channel_type: row.config.get("provisioning_state")
            for row in db.query(AgentChannel).all()
        }
        assert states["voice"] == "requested"
        assert states["instagram"] == "cancelled"
        assert states["telegram"] == "cancelled"
    engine.dispose()


def test_only_real_managed_runtime_can_be_prepared_before_launch(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(
        "backend.app.modules.ai_agent.self_service_policy.n8n_gateway.configured",
        lambda: True,
    )
    with Session(engine, autoflush=False) as db:
        voice = AgentChannel(
            company_id=1,
            agent_id=1,
            channel_type="voice",
            enabled=False,
            config={
                "provider": "vapi",
                "phone_number": "+96800000000",
                "llm_api_key": "llm-key",
                "vapi_llm_credential_id": "cred-1",
                "vapi_assistant_id": "assistant-1",
                "vapi_phone_number_id": "phone-1",
                "provisioning_state": "connected",
            },
        )
        telegram = AgentChannel(
            company_id=1,
            agent_id=1,
            channel_type="telegram",
            enabled=False,
            config={
                "connection_key": "telegram-main",
                "provisioning_state": "connected",
            },
        )
        db.add_all([voice, telegram])
        db.commit()

        prepared = configured_channel_types(db, company_id=1, agent_id=1)
        assert "voice" in prepared
        assert "telegram" in prepared
    engine.dispose()

def test_managed_channels_share_one_xvond_runtime_adapter_but_provider_packaging_is_truthful():
    for key in ("telegram", "instagram", "messenger", "email", "sms", "slack", "teams", "custom"):
        capability = get_channel_capability(key)
        assert capability["runtime_adapter"] == N8N_CHANNEL_ADAPTER
        assert capability["runtime_state"] == CHANNEL_RUNTIME_LIVE
        assert capability["setup_mode"] == CHANNEL_SETUP_MANAGED

    for key in ("telegram", "instagram", "messenger", "email", "slack", "sms", "custom"):
        assert get_channel_capability(key)["packaged_provider"] is True
    assert get_channel_capability("teams")["packaged_provider"] is False


def test_managed_gateway_channel_requires_connected_provisioning():
    from backend.app.modules.channels.catalog import validate_channel_config

    try:
        validate_channel_config("telegram", {"connection_key": "telegram-main"})
    except ValueError as exc:
        assert "provisioning_state" in str(exc)
    else:
        raise AssertionError("Managed channel must not validate before provisioning is connected")


def test_customer_inbox_only_counts_channels_with_real_runtime():
    assert "website" in LIVE_INBOX_CHANNELS
    assert "whatsapp" in LIVE_INBOX_CHANNELS
    assert "voice" in LIVE_INBOX_CHANNELS
    assert "instagram" in LIVE_INBOX_CHANNELS
    assert "telegram" in LIVE_INBOX_CHANNELS
    assert "email" in LIVE_INBOX_CHANNELS
