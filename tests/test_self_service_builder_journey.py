from types import SimpleNamespace

from backend.app.api.customer_employee_builder import _self_service_builder_journey


def stage(journey, stage_id):
    return next(item for item in journey["stages"] if item["id"] == stage_id)


def test_builder_journey_starts_with_plan_before_paid_build():
    journey = _self_service_builder_journey(
        agent=SimpleNamespace(enabled=False),
        has_entitlement=False,
        compiled_spec=None,
        state={
            "subscription": {"active": False, "status": None},
            "ready": False,
        },
    )

    assert stage(journey, "brief")["status"] == "complete"
    assert stage(journey, "plan")["status"] == "action_required"
    assert stage(journey, "build")["status"] == "blocked"
    assert stage(journey, "setup")["status"] == "blocked"
    assert stage(journey, "launch")["status"] == "blocked"
    assert [item["type"] for item in journey["next_actions"]] == ["choose_plan"]


def test_builder_journey_surfaces_only_required_customer_setup_actions():
    spec = {
        "requirements": [
            {
                "key": "knowledge",
                "kind": "knowledge",
                "status": "customer_input_required",
                "purpose": "Use the company reference material",
            },
            {
                "key": "website",
                "kind": "channel",
                "status": "connection_required",
            },
        ],
        "delivery": {"provisioning_version": 1},
    }
    journey = _self_service_builder_journey(
        agent=SimpleNamespace(enabled=False),
        has_entitlement=True,
        compiled_spec=spec,
        state={
            "subscription": {
                "active": True,
                "status": "active",
                "plan_name": "Starter",
            },
            "missing_channels": ["website"],
            "resolved_requirements": [],
            "provider_ready": True,
            "ready": False,
        },
    )

    assert stage(journey, "plan")["status"] == "complete"
    assert stage(journey, "build")["status"] == "complete"
    setup = stage(journey, "setup")
    assert setup["status"] == "action_required"
    assert {item["type"] for item in setup["actions"]} == {
        "setup_website",
        "manage_knowledge",
    }
    assert [item["type"] for item in journey["next_actions"]] == [
        item["type"] for item in setup["actions"]
    ]


def test_builder_journey_marks_external_adapter_gap_as_xvond_waiting():
    spec = {
        "requirements": [
            {
                "key": "email_send",
                "kind": "integration",
                "status": "connection_required",
                "self_service_connection_status": "xvond_adapter_required",
            }
        ],
        "delivery": {"provisioning_version": 1},
    }
    journey = _self_service_builder_journey(
        agent=SimpleNamespace(enabled=False),
        has_entitlement=True,
        compiled_spec=spec,
        state={
            "subscription": {"active": True, "status": "active"},
            "missing_channels": [],
            "resolved_requirements": [],
            "provider_ready": True,
            "ready": False,
        },
    )

    setup = stage(journey, "setup")
    assert setup["status"] == "waiting"
    assert "Xvond connection adapter" in setup["detail"]
    assert journey["next_actions"] == []


def test_builder_journey_unlocks_launch_only_from_runtime_readiness():
    spec = {
        "requirements": [],
        "delivery": {"provisioning_version": 1},
    }
    journey = _self_service_builder_journey(
        agent=SimpleNamespace(enabled=False),
        has_entitlement=True,
        compiled_spec=spec,
        state={
            "subscription": {"active": True, "status": "active"},
            "missing_channels": [],
            "resolved_requirements": [],
            "provider_ready": True,
            "ready": True,
        },
        builder={
            "compiled_at": "2026-09-18T10:00:00Z",
            "last_tested_compiled_at": "2026-09-18T10:00:00Z",
        },
    )

    assert stage(journey, "setup")["status"] == "complete"
    assert stage(journey, "test")["status"] == "complete"
    assert stage(journey, "launch")["status"] == "action_required"
    assert [item["type"] for item in journey["next_actions"]] == ["launch_employee"]

    live = _self_service_builder_journey(
        agent=SimpleNamespace(enabled=True),
        has_entitlement=True,
        compiled_spec=spec,
        state={
            "subscription": {"active": True, "status": "active"},
            "missing_channels": [],
            "resolved_requirements": [],
            "provider_ready": True,
            "ready": True,
        },
        builder={
            "compiled_at": "2026-09-18T10:00:00Z",
            "last_tested_compiled_at": "2026-09-18T10:00:00Z",
        },
    )
    assert stage(live, "launch")["status"] == "complete"
    assert live["live"] is True


def test_builder_journey_exposes_declared_setup_fields_and_never_plain_credentials():
    fields_spec = {
        "requirements": [
            {
                "key": "workspace_context",
                "kind": "custom",
                "status": "customer_input_required",
                "purpose": "Know the target workspace",
                "customer_inputs": ["workspace_id", "timezone"],
                "customer_input_labels": {
                    "workspace_id": "Workspace",
                    "timezone": "Timezone",
                },
                "customer_input_purposes": {
                    "workspace_id": "Choose the workspace this employee should use",
                },
            }
        ],
        "delivery": {"provisioning_version": 1},
    }
    journey = _self_service_builder_journey(
        agent=SimpleNamespace(enabled=False),
        has_entitlement=True,
        compiled_spec=fields_spec,
        state={
            "subscription": {"active": True, "status": "active"},
            "missing_channels": [],
            "resolved_requirements": [],
            "provider_ready": True,
            "ready": False,
        },
    )
    action = stage(journey, "setup")["actions"][0]
    assert action["type"] == "provide_input"
    assert action["fields"] == [
        {
            "key": "workspace_id",
            "label": "Workspace",
            "detail": "Choose the workspace this employee should use",
        },
        {"key": "timezone", "label": "Timezone", "detail": None},
    ]

    sensitive_spec = {
        "requirements": [
            {
                "key": "crm_access_token",
                "kind": "custom",
                "status": "customer_input_required",
                "purpose": "Connect CRM",
                "customer_inputs": ["access_token"],
            }
        ],
        "delivery": {"provisioning_version": 1},
    }
    journey = _self_service_builder_journey(
        agent=SimpleNamespace(enabled=False),
        has_entitlement=True,
        compiled_spec=sensitive_spec,
        state={
            "subscription": {"active": True, "status": "active"},
            "missing_channels": [],
            "resolved_requirements": [],
            "provider_ready": True,
            "ready": False,
        },
    )
    setup = stage(journey, "setup")
    assert setup["status"] == "waiting"
    assert setup["actions"] == []
    assert "protected connection" in setup["detail"]


def test_builder_journey_requires_setup_then_current_build_preview_before_launch():
    spec = {
        "requirements": [],
        "delivery": {"provisioning_version": 1},
    }
    journey = _self_service_builder_journey(
        agent=SimpleNamespace(enabled=False),
        has_entitlement=True,
        compiled_spec=spec,
        state={
            "subscription": {"active": True, "status": "active"},
            "missing_channels": [],
            "resolved_requirements": [],
            "provider_ready": True,
            "ready": True,
        },
        builder={"compiled_at": "build-2"},
    )

    assert stage(journey, "setup")["status"] == "complete"
    assert stage(journey, "test")["status"] == "action_required"
    assert stage(journey, "launch")["status"] == "blocked"
    assert [item["type"] for item in journey["next_actions"]] == ["test_employee"]


def test_builder_journey_turns_external_requirement_into_connect_system_action():
    spec = {
        "requirements": [
            {
                "key": "booking",
                "kind": "integration",
                "status": "connection_required",
                "purpose": "Use the existing booking system",
            }
        ],
        "delivery": {"provisioning_version": 1},
    }
    journey = _self_service_builder_journey(
        agent=SimpleNamespace(enabled=False),
        has_entitlement=True,
        compiled_spec=spec,
        state={
            "subscription": {"active": True, "status": "active"},
            "missing_channels": [],
            "resolved_requirements": [],
            "provider_ready": True,
            "ready": False,
        },
        builder={"compiled_at": "build-1"},
    )

    setup = stage(journey, "setup")
    assert setup["status"] == "action_required"
    action = next(item for item in setup["actions"] if item["type"] == "connect_system")
    assert action["key"] == "booking"
