from types import SimpleNamespace

from backend.app.modules.ai_agent.self_service_policy import (
    channel_limit_from_plan,
    communication_channels,
    evaluate_readiness,
    interaction_mode,
)


def _spec(*, scope="personal", requirements=None):
    return {
        "scope": scope,
        "requirements": list(requirements or []),
        "delivery": {"provisioning_version": 1},
    }


def test_personal_employee_can_launch_with_zero_channels():
    state = evaluate_readiness(
        subscribed=True,
        channel_limit=1,
        requested_channels=[],
        enabled_channels=[],
        compiled_spec=_spec(scope="personal"),
        provisioned=True,
    )
    assert state["ready"] is True
    assert state["mode"] == "personal"
    assert state["channels_required"] is False
    assert state["channel_slots_used"] == 0


def test_background_employee_can_launch_with_zero_channels():
    spec = _spec(
        scope="personal",
        requirements=[
            {
                "key": "daily_monitor",
                "kind": "automation",
                "status": "available",
                "primitives": ["scheduler"],
            }
        ],
    )
    state = evaluate_readiness(
        subscribed=True,
        channel_limit=1,
        requested_channels=[],
        enabled_channels=[],
        compiled_spec=spec,
        provisioned=True,
    )
    assert state["ready"] is True
    assert state["mode"] == "background"
    assert state["channels_required"] is False


def test_customer_facing_employee_requires_connected_channel():
    spec = _spec(
        scope="business",
        requirements=[
            {
                "key": "whatsapp",
                "kind": "channel",
                "status": "connection_required",
            }
        ],
    )
    state = evaluate_readiness(
        subscribed=True,
        channel_limit=1,
        requested_channels=["whatsapp"],
        enabled_channels=[],
        compiled_spec=spec,
        provisioned=True,
    )
    assert state["ready"] is False
    assert state["mode"] == "customer_facing"
    assert state["missing_channels"] == ["whatsapp"]
    assert any("Connect and activate whatsapp" in item for item in state["blockers"])


def test_active_channel_resolves_channel_connection_requirement():
    spec = _spec(
        scope="business",
        requirements=[
            {
                "key": "whatsapp",
                "kind": "channel",
                "status": "connection_required",
            }
        ],
    )
    state = evaluate_readiness(
        subscribed=True,
        channel_limit=1,
        requested_channels=["whatsapp"],
        enabled_channels=["whatsapp"],
        compiled_spec=spec,
        provisioned=True,
    )
    assert state["ready"] is True
    assert state["channel_slots_used"] == 1
    assert state["missing_channels"] == []


def test_email_and_instagram_publishing_are_not_channel_slots():
    assert communication_channels(["email", "instagram", "whatsapp"]) == ["whatsapp"]
    spec = _spec(
        requirements=[
            {
                "key": "email_read",
                "kind": "integration",
                "status": "connection_required",
            },
            {
                "key": "instagram_publish",
                "kind": "integration",
                "status": "connection_required",
            },
        ]
    )
    state = evaluate_readiness(
        subscribed=True,
        channel_limit=1,
        requested_channels=["email", "instagram"],
        enabled_channels=[],
        compiled_spec=spec,
        provisioned=True,
    )
    assert state["channel_slots_used"] == 0
    assert state["channels_required"] is False
    assert state["ready"] is False
    assert any("email_read: setup required" == item for item in state["blockers"])
    assert any("instagram_publish: setup required" == item for item in state["blockers"])


def test_plan_channel_slots_are_enforced():
    state = evaluate_readiness(
        subscribed=True,
        channel_limit=1,
        requested_channels=["xvond", "whatsapp"],
        enabled_channels=["whatsapp"],
        compiled_spec=_spec(scope="business"),
        provisioned=True,
    )
    assert state["channel_slots_used"] == 2
    assert state["ready"] is False
    assert any("plan limit (1)" in item for item in state["blockers"])


def test_enabled_channel_consumes_slot_even_if_not_in_job_brief():
    state = evaluate_readiness(
        subscribed=True,
        channel_limit=1,
        requested_channels=[],
        enabled_channels=["whatsapp"],
        compiled_spec=_spec(scope="business"),
        provisioned=True,
    )
    assert state["channel_slots_used"] == 1
    assert state["mode"] == "customer_facing"
    assert state["ready"] is True


def test_subscription_is_always_required_for_live_self_service_employee():
    state = evaluate_readiness(
        subscribed=False,
        channel_limit=None,
        requested_channels=[],
        enabled_channels=[],
        compiled_spec=_spec(),
        provisioned=True,
    )
    assert state["ready"] is False
    assert "Active AI Employee subscription required" in state["blockers"]


def test_channels_per_employee_limit_overrides_legacy_channels_limit():
    plan = SimpleNamespace(limits={"channels": 3, "channels_per_employee": 2})
    assert channel_limit_from_plan(plan) == 2


def test_interaction_mode_does_not_treat_integrations_as_channels():
    spec = _spec(
        scope="personal",
        requirements=[
            {"key": "email_read", "kind": "integration", "status": "connection_required"}
        ],
    )
    assert interaction_mode(spec, ["email"]) == "personal"
