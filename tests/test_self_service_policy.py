from types import SimpleNamespace

from backend.app.modules.ai_agent.self_service_policy import (
    channel_limit_from_plan,
    communication_channels,
    evaluate_readiness,
    interaction_mode,
    self_service_spec_view,
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


def test_email_and_instagram_actions_remain_integrations_while_channels_are_explicit():
    assert communication_channels(["email", "instagram", "whatsapp"]) == [
        "email",
        "instagram",
        "whatsapp",
    ]
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
        requested_channels=[],
        enabled_channels=[],
        compiled_spec=spec,
        provisioned=True,
    )
    assert state["channel_slots_used"] == 0
    assert state["channels_required"] is False
    assert state["ready"] is False
    assert "email_read: setup required" in state["blockers"]
    assert "instagram_publish: setup required" in state["blockers"]

    channel_state = evaluate_readiness(
        subscribed=True,
        channel_limit=2,
        requested_channels=["email", "instagram"],
        enabled_channels=[],
        compiled_spec=_spec(scope="business"),
        provisioned=True,
    )
    assert channel_state["channel_slots_used"] == 2
    assert channel_state["channels_required"] is True
    assert channel_state["missing_channels"] == ["email", "instagram"]


def test_plan_channel_slots_exclude_built_in_xvond_workspace():
    state = evaluate_readiness(
        subscribed=True,
        channel_limit=1,
        requested_channels=["xvond", "whatsapp"],
        enabled_channels=["whatsapp"],
        compiled_spec=_spec(scope="business"),
        provisioned=True,
    )
    assert state["channel_slots_used"] == 1
    assert state["billed_slot_channels"] == ["whatsapp"]
    assert state["ready"] is True


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


def test_interaction_mode_distinguishes_email_integration_from_email_channel():
    spec = _spec(
        scope="personal",
        requirements=[
            {"key": "email_read", "kind": "integration", "status": "connection_required"}
        ],
    )
    assert interaction_mode(spec, []) == "personal"
    assert interaction_mode(spec, ["email"]) == "hybrid"


def test_self_service_spec_view_annotates_cached_connection_truth_without_recompile():
    cached = _spec(
        scope="business",
        requirements=[
            {
                "key": "whatsapp",
                "kind": "channel",
                "status": "connection_required",
            },
            {
                "key": "email_send",
                "kind": "integration",
                "status": "connection_required",
            },
            {
                "key": "voice",
                "kind": "channel",
                "status": "connection_required",
            },
        ],
    )

    rendered = self_service_spec_view(cached)
    rows = {item["key"]: item for item in rendered["requirements"]}

    assert rows["whatsapp"]["self_service_connection_status"] == "self_service_available"
    assert rows["email_send"]["self_service_connection_status"] == "self_service_integration_available"
    assert rows["voice"]["self_service_connection_status"] == "xvond_managed_available"
    assert rows["voice"]["channel_delivery"]["setup_mode"] == "managed"
    assert rows["voice"]["channel_delivery"]["runtime_state"] == "live"
    assert "self_service_connection_status" not in cached["requirements"][0]


def test_direct_self_service_channel_keeps_normal_connect_blocker():
    spec = _spec(
        scope="business",
        requirements=[
            {
                "key": "website",
                "kind": "channel",
                "status": "connection_required",
            }
        ],
    )
    state = evaluate_readiness(
        subscribed=True,
        channel_limit=1,
        requested_channels=["website"],
        enabled_channels=[],
        compiled_spec=spec,
        provisioned=True,
    )
    assert "website: setup required" in state["blockers"]
    assert not any("adapter required" in item for item in state["blockers"])



def test_readiness_checks_every_employee_routine_trigger():
    spec = _spec(scope="personal")
    spec["delivery"]["graph_trigger"] = {
        "routine_id": "morning",
        "routine_name": "Morning routine",
        "status": "ready",
        "workflow_id": 10,
        "trigger_type": "schedule",
    }
    spec["delivery"]["graph_triggers"] = [
        {
            "routine_id": "morning",
            "routine_name": "Morning routine",
            "status": "ready",
            "workflow_id": 10,
            "trigger_type": "schedule",
        },
        {
            "routine_id": "incoming_event",
            "routine_name": "Incoming event",
            "status": "disabled",
            "workflow_id": 11,
            "trigger_type": "event",
        },
    ]

    state = evaluate_readiness(
        subscribed=True,
        channel_limit=None,
        requested_channels=[],
        enabled_channels=[],
        compiled_spec=spec,
        provisioned=True,
    )

    assert state["ready"] is False
    assert "Incoming event: generated workflow is disabled" in state["blockers"]
    assert not any("Morning routine" in item for item in state["blockers"])


def test_readiness_keeps_legacy_single_graph_trigger_compatible():
    spec = _spec(scope="personal")
    spec["delivery"]["graph_trigger"] = {
        "status": "ready",
        "workflow_id": 10,
        "trigger_type": "manual",
    }

    state = evaluate_readiness(
        subscribed=True,
        channel_limit=None,
        requested_channels=[],
        enabled_channels=[],
        compiled_spec=spec,
        provisioned=True,
    )

    assert state["ready"] is True
