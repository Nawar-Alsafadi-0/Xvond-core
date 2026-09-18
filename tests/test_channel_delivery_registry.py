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
    CHANNEL_RUNTIME_ADAPTER_REQUIRED,
    CHANNEL_RUNTIME_LIVE,
    CHANNEL_SETUP_MANAGED,
    CHANNEL_SETUP_SELF_SERVICE,
    canonical_channel_type,
    get_channel_capability,
    list_customer_channel_capabilities,
)
from backend.app.modules.channels.delivery import reconcile_managed_channel_requests
from backend.app.modules.channels.models import AgentChannel
from backend.app.api.admin_channels import _activation_blockers
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
        assert items[key]["runtime_state"] == CHANNEL_RUNTIME_ADAPTER_REQUIRED


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
    assert items["instagram"]["availability"] == "xvond_managed_adapter"
    assert items["xvond"]["availability"] == "built_in"

    for item in payload["channels"]:
        assert "config_fields" not in item
        assert "access_token" not in str(item)
        assert "bot_token" not in str(item)


def test_open_ended_builder_detects_managed_and_self_service_channels():
    blueprint = build_employee_blueprint(
        "بدي موظف يرد على واتساب وانستغرام، يتصل هاتفيًا، "
        "ويتابع Telegram وSlack ويرد بالإيميل وSMS"
    )

    for key in ("whatsapp", "instagram", "voice", "telegram", "slack", "email", "sms"):
        assert key in blueprint.channels

    readiness = blueprint_readiness(blueprint)
    assert readiness["channels"]["whatsapp"] == "connect_required"
    assert readiness["channels"]["voice"] == "xvond_managed_setup"
    assert readiness["channels"]["instagram"] == "xvond_adapter_required"
    assert readiness["channels"]["telegram"] == "xvond_adapter_required"


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
    assert rows["telegram"]["self_service_connection_status"] == "xvond_adapter_required"
    assert rows["instagram_dm"]["channel_delivery"]["type"] == "instagram"
    assert rows["email_send"]["self_service_connection_status"] == "xvond_adapter_required"


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


def test_only_real_managed_runtime_can_be_prepared_before_launch():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
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
            config={"bot_token": "token", "provisioning_state": "connected"},
        )
        db.add_all([voice, telegram])
        db.commit()

        prepared = configured_channel_types(db, company_id=1, agent_id=1)
        assert "voice" in prepared
        assert "telegram" not in prepared
    engine.dispose()


def test_non_live_channel_capability_cannot_be_mistaken_for_runtime():
    telegram = get_channel_capability("telegram")
    instagram = get_channel_capability("instagram")
    assert telegram["runtime_adapter"] is None
    assert instagram["runtime_adapter"] is None
    assert telegram["runtime_state"] == CHANNEL_RUNTIME_ADAPTER_REQUIRED
    assert instagram["runtime_state"] == CHANNEL_RUNTIME_ADAPTER_REQUIRED


def test_adapter_required_channel_cannot_be_activated_by_admin_config():
    channel = SimpleNamespace(channel_type="telegram")
    blockers = _activation_blockers(None, channel)
    assert blockers
    assert "runtime adapter is not available yet" in blockers[0]


def test_custom_channel_is_registered_but_not_claimed_live_without_gateway():
    custom = get_channel_capability("custom")
    assert custom["setup_mode"] == CHANNEL_SETUP_MANAGED
    assert custom["runtime_state"] == CHANNEL_RUNTIME_ADAPTER_REQUIRED
    assert custom["runtime_adapter"] is None
