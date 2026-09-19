"""Compile/provision regressions against real persisted employee/action records."""
from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from backend.app.main import app  # noqa: F401 - register model metadata
from backend.app.api import customer_employee_builder as api
from backend.app.api.admin_delivery_readiness import _action_state, _delivery_state
from backend.app.core.config_secrets import reveal_config
from backend.app.core.database.base import Base
from backend.app.models.company import Company
from backend.app.models.company_module import CompanyModule
from backend.app.models.company_profile import CompanyProfile
from backend.app.modules.ai_agent.employee_capability_builder import (
    build_managed_action_config,
    provision_compiled_capabilities,
)
from backend.app.modules.ai_agent.employee_compiler import normalize_compiled_spec
from backend.app.modules.ai_agent.factory_models import AgentConfig
from backend.app.modules.ai_agent.models import AIAgent, AIUsage
from backend.app.modules.automation.models import AutomationRun, AutomationWorkflow
from backend.app.modules.channels.models import AgentChannel
from backend.app.modules.tools.business_models import ActionRequest
from backend.app.modules.tools.models import AgentToolAssignment
from backend.app.modules.tools.executor import ToolExecutor
from backend.app.modules.tools.workflow_action_request import WorkflowActionRequestTool


BRIEF = "Monitor the specialist platform daily and ask before sending a notification."
KEY = "specialist_platform_monitor"
PAYLOAD = {
    "role": "Monitoring employee",
    "summary": "Monitor a specialist platform.",
    "tasks": [{"name": "Monitor", "description": BRIEF, "trigger": "daily"}],
    "requirements": [
        {"key": KEY, "kind": "custom", "purpose": "Monitor the specialist platform",
         "primitives": ["browser_web", "scheduler", "workflow_engine"]},
        {"key": "email_send", "kind": "integration", "purpose": "Send notifications"},
        {"key": "knowledge", "kind": "knowledge", "purpose": "Owner's rules"},
    ],
    "permissions": [{"action": "send notifications", "mode": "ask_before"}],
}
USER = SimpleNamespace(company_id=1)


@pytest.fixture
def database(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = lambda: Session(engine, autoflush=False)
    monkeypatch.setattr(api, "SessionLocal", factory)
    monkeypatch.setattr(api.service_limits, "entitlement", lambda *args: True)
    monkeypatch.setattr(api.limits_service, "check_token_limit", lambda *args: None)
    monkeypatch.setattr(api, "runtime_selections", lambda *args, **kwargs: [
        SimpleNamespace(provider="mock", model="mock")
    ])
    calls = []

    def generate(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(text=json.dumps(PAYLOAD), input_tokens=10, output_tokens=20,
                               total_tokens=30, cost=0)

    monkeypatch.setattr(api.ai_engine, "generate", generate)
    monkeypatch.setattr(
        "backend.app.modules.tools.workflow_action_request.n8n_gateway.execute",
        lambda **kwargs: pytest.fail("Compilation/testing must not execute workflows"),
    )
    with factory() as db:
        db.add_all([Company(id=1, name="Owner", active=True), Company(id=2, name="Other", active=True)])
        db.flush()
        db.add_all([
            AIAgent(id=1, company_id=1, name="Employee", description=BRIEF, system_prompt="original",
                    provider="mock", model="mock", enabled=False),
            AIAgent(id=2, company_id=2, name="Other employee", system_prompt="other",
                    provider="mock", model="mock", enabled=False),
        ])
        db.flush()
        db.add(AgentConfig(agent_id=1, agent_type="employee", settings={
            "unrelated": "preserved", "employee_builder": {
                "source_description": BRIEF, "compiled_spec": None, "requested_channels": [],
            },
        }))
        db.add(CompanyModule(company_id=1, module_name="tools", enabled=True))
        db.commit()
    yield factory, calls
    engine.dispose()


def _builder(db):
    return db.query(AgentConfig).filter_by(agent_id=1).one().settings["employee_builder"]


def _assignment(db):
    return db.query(AgentToolAssignment).filter_by(agent_id=1, tool_name="action_request").one()


def _cache(factory, spec, owner_permissions=None):
    with factory() as db:
        config = db.query(AgentConfig).filter_by(agent_id=1).one()
        settings = deepcopy(config.settings)
        settings["employee_builder"].update(
            {
                "compiled_spec": spec,
                "compiled_at": "original-time",
                "owner_permissions": dict(owner_permissions or {}),
            }
        )
        config.settings = settings
        db.commit()


def test_compile_provisions_and_atomically_stores_spec_action_contract_and_delivery(database):
    factory, calls = database
    result = api.compile_employee(1, USER)
    with factory() as db:
        builder = _builder(db)
        assert builder["compiled_spec"] == result["spec"]
        assert builder["delivery"] == result["spec"]["delivery"]
        spec = builder["compiled_spec"]
        assert spec["job_brief"] == BRIEF
        assert spec["requirements"][0]["status"] == "xvond_managed"
        assert spec["requirements"][0]["provisioned"] is True
        assert spec["requirements"][0]["execution_status"] == "setup_required"
        assert spec["ready_requirements"] == []
        assert spec["build_required"] == []
        assert builder["missing_information"] == ["email_send", "knowledge"]
        assert spec["unsupported_requirements"] == spec["delivery"]["unsupported"] == []
        assert not {"unsupported", "custom_required"} & {r["status"] for r in spec["requirements"]}
        plan = spec["delivery"]["action_plan"][KEY]
        assert plan["tool_name"] == "action_request"
        assert plan["operations"] == [f"{KEY}.{op}" for op in ("check_availability", "execute", "cancel")]
        assert plan["status"] == "contract_provisioned"
        assignment = _assignment(db)
        assert set(assignment.config["actions"]) == {KEY}
        action = assignment.config["actions"][KEY]
        assert action["destination"]["type"] == "xvond_internal"
        assert action["destination"]["adapter"] == "generic_capability"
        assert action["destination"]["execution_plan"] == []
        assert action["destination"]["allowed_hosts"] == []
        assert action["destination"]["capability_key"] == KEY
        assert action["confirmation_required"] is True
        assert action["destination"]["job_brief"] == BRIEF
        agent = db.get(AIAgent, 1)
        assert agent.enabled is False
        assert f"{KEY}: xvond_managed" in agent.system_prompt
        assert "not that its execution adapter is ready" in agent.system_prompt
        assert db.query(ActionRequest).count() == 0
        assert db.query(AIUsage).count() == 1
        assert db.query(AgentConfig).one().settings["unrelated"] == "preserved"
    assert len(calls) == 1
    assert calls[0]["tools"] is None


def test_repeated_compile_preserves_operator_config_disabled_switches_and_usage(database):
    factory, calls = database
    api.compile_employee(1, USER)
    with factory() as db:
        assignment = _assignment(db)
        config = deepcopy(assignment.config)
        config["approval_required"] = True
        config["actions"][KEY]["enabled"] = False
        config["actions"][KEY]["fields"] = [{"key": "target", "required": True}]
        config["actions"]["operator_action"] = {"enabled": True, "destination": {"type": "xvond_internal"}}
        assignment.config = config
        assignment.enabled = False
        compiled_at = _builder(db)["compiled_at"]
        db.commit()
    api.compile_employee(1, USER)
    with factory() as db:
        assert _assignment(db).config == config
        assert _assignment(db).enabled is False
        assert db.query(AgentToolAssignment).count() == 1
        assert db.query(AIUsage).count() == 1
        assert _builder(db)["compiled_at"] == compiled_at
        assert _builder(db)["delivery"]["action_plan"][KEY]["execution_status"] == "disabled"
    assert len(calls) == 1


@pytest.mark.parametrize("legacy_status", ["xvond_build", "custom_required", "unsupported"])
def test_cached_specs_are_provisioned_without_paid_recompilation(database, legacy_status):
    factory, calls = database
    spec = normalize_compiled_spec(PAYLOAD, job_brief=BRIEF)
    spec["requirements"][0]["status"] = legacy_status
    spec["unsupported_requirements"] = [KEY]
    _cache(factory, spec)
    result = api.compile_employee(1, USER)
    assert result["spec"]["requirements"][0]["status"] == "xvond_managed"
    assert result["spec"]["unsupported_requirements"] == []
    with factory() as db:
        assert KEY in _assignment(db).config["actions"]
        assert _builder(db)["compiled_at"] == "original-time"
        assert db.query(AIUsage).count() == 0
    assert calls == []


def test_cached_managed_spec_repairs_missing_contract_without_recompiling(database):
    factory, calls = database
    api.compile_employee(1, USER)
    with factory() as db:
        db.delete(_assignment(db))
        db.commit()
        blockers = _delivery_state(db, 1, 1)["payload"]["setup_blockers"]
        assert any("restore missing action contracts" in item for item in blockers)
    api.compile_employee(1, USER)
    with factory() as db:
        assert KEY in _assignment(db).config["actions"]
    assert len(calls) == 1


def test_existing_manual_contract_and_encrypted_settings_are_preserved(database):
    factory, _ = database
    manual = {"enabled": False, "confirmation_required": True, "destination": {"type": "integration", "integration_id": 99}}
    with factory() as db:
        db.add(AgentToolAssignment(agent_id=1, tool_name="action_request", enabled=False,
                                  config={"api_token": "test-only-secret", "actions": {KEY: manual}}))
        db.commit()
    result = api.compile_employee(1, USER)
    with factory() as db:
        assignment = _assignment(db)
        assert reveal_config(assignment.config)["api_token"] == "test-only-secret"
        assert assignment.config["api_token"] != "test-only-secret"
        assert assignment.config["actions"][KEY] == manual
        assert assignment.enabled is False
    assert result["spec"]["delivery"]["action_plan"][KEY]["source"] == "existing"
    assert result["spec"]["delivery"]["action_plan"][KEY]["execution_status"] == "disabled"


@pytest.mark.parametrize("cached", [False, True])
def test_test_endpoint_provisions_before_draft_reply_and_never_executes_tools(database, monkeypatch, cached):
    factory, calls = database
    if cached:
        _cache(factory, normalize_compiled_spec(PAYLOAD, job_brief=BRIEF))
    generate = api.ai_engine.generate

    def reply(**kwargs):
        if kwargs["system_prompt"] != api.COMPILER_SYSTEM_PROMPT:
            assert f"{KEY}: xvond_managed" in kwargs["system_prompt"]
            assert kwargs["tools"] is None
        return generate(**kwargs)

    monkeypatch.setattr(api.ai_engine, "generate", reply)
    result = api.test_draft_employee(1, api.EmployeeBuilderTestRequest(message="What can you do?"), USER)
    assert result["tools_used"] is result["channels_used"] is False
    with factory() as db:
        assert KEY in _assignment(db).config["actions"]
        assert _builder(db)["delivery"]["action_plan"][KEY]["status"] == "contract_provisioned"
        assert db.query(ActionRequest).count() == 0
        assert db.get(AIAgent, 1).enabled is False
    assert len(calls) == (1 if cached else 2)


def test_provision_failure_rolls_back_contracts_spec_prompt_and_usage(database, monkeypatch):
    factory, _ = database
    provision = api.provision_compiled_capabilities

    def fail(db, **kwargs):
        provision(db, **kwargs)
        db.flush()
        raise RuntimeError("provision interrupted")

    monkeypatch.setattr(api, "provision_compiled_capabilities", fail)
    with pytest.raises(RuntimeError, match="provision interrupted"):
        api.compile_employee(1, USER)
    with factory() as db:
        assert _builder(db)["compiled_spec"] is None
        assert db.query(AgentToolAssignment).count() == 0
        assert db.query(AIUsage).count() == 0
        assert db.get(AIAgent, 1).system_prompt == "original"


def test_cross_tenant_compile_cannot_provision_another_employee(database):
    factory, calls = database
    with pytest.raises(HTTPException) as exc:
        api.compile_employee(2, USER)
    assert exc.value.status_code == 404
    with factory() as db:
        assert db.query(AgentToolAssignment).count() == 0
    assert calls == []


def test_external_connections_and_customer_inputs_only_create_no_actions(database):
    factory, calls = database
    spec = normalize_compiled_spec({"requirements": PAYLOAD["requirements"][1:]}, job_brief=BRIEF)
    _cache(factory, spec)
    result = api.compile_employee(1, USER)
    assert result["spec"]["setup_required"] == ["email_send", "knowledge"]
    assert result["spec"]["delivery"]["action_plan"] == {}
    with factory() as db:
        assert db.query(AgentToolAssignment).count() == 0
        assert db.query(ActionRequest).count() == 0
    assert calls == []


def test_unprovisioned_spec_and_missing_adapter_block_go_live(database):
    factory, _ = database
    with factory() as db:
        blockers = _delivery_state(db, 1, 1)["payload"]["setup_blockers"]
        assert any("provision its action contracts" in item for item in blockers)
    api.compile_employee(1, USER)
    with factory() as db:
        state = _action_state(db, 1, 1)
        assert state["ready"] is False
        assert any("generated execution plan" in item for item in state["issues"])
        assert _delivery_state(db, 1, 1)["payload"]["setup_ready"] is False
        assert "action_request" not in {item["name"] for item in ToolExecutor().get_agent_tools(db, 1)}


def test_runtime_ready_generated_plan_is_exposed_to_employee(database):
    factory, _ = database
    payload = deepcopy(PAYLOAD)
    payload["requirements"][0]["execution_plan"] = [
        {"id": "fetch", "op": "http_get_json", "url_field": "url"},
        {"id": "value", "op": "extract", "source": "fetch", "path": "value"},
        {"id": "matched", "op": "compare", "source": "value", "operator": "gte", "value_field": "threshold"},
        {"id": "notify", "op": "notify", "when": "matched", "title": "Monitor alert", "message": "Condition matched."},
    ]
    brief = "Monitor https://prices.example.com daily and notify me when the threshold matches."
    _cache(factory, normalize_compiled_spec(payload, job_brief=brief))

    result = api.compile_employee(1, USER)
    requirement = result["spec"]["requirements"][0]
    assert requirement["execution_status"] == "ready"

    with factory() as db:
        assignment = _assignment(db)
        action = reveal_config(assignment.config)["actions"][KEY]
        destination = action["destination"]
        assert destination["adapter"] == "generic_capability"
        assert destination["allowed_hosts"] == ["prices.example.com"]
        assert _action_state(db, 1, 1)["ready"] is True
        assert "action_request" in {item["name"] for item in ToolExecutor().get_agent_tools(db, 1)}


@pytest.mark.parametrize("mode,enabled,confirmation", [
    ("automatic", True, False), ("ask_before", True, True), ("never", True, True),
])
def test_generated_contract_respects_exact_permission(mode, enabled, confirmation):
    action = build_managed_action_config(requirement=PAYLOAD["requirements"][0], spec={
        "permissions": [{"action": KEY, "mode": mode}],
    })
    assert action["enabled"] is enabled
    assert action["confirmation_required"] is confirmation


def test_shared_words_do_not_grant_automatic_permission():
    action = build_managed_action_config(requirement={"key": "delete", "purpose": "Delete the messages"}, spec={
        "permissions": [{"action": "Read the messages", "mode": "automatic"}],
    })
    assert action["confirmation_required"] is True


def test_customer_prerequisites_override_build_defaults():
    spec = normalize_compiled_spec({"requirements": [
        {"key": "web_research", "requires_connection": True},
        {"key": "private_rules", "customer_inputs": ["Provide the rules"]},
    ]}, job_brief=BRIEF)
    assert [item["status"] for item in spec["requirements"]] == ["connection_required", "customer_input_required"]
    assert spec["build_required"] == []


@pytest.mark.parametrize("key", ["راقب_الأسعار", "monitor.prices", "_private_monitor"])
def test_novel_keys_produce_stable_generic_workflow_identifiers(key):
    import re

    payload = {"requirements": [{"key": key, "purpose": "Monitor prices"}]}
    spec = normalize_compiled_spec(payload, job_brief=BRIEF)
    requirement = spec["requirements"][0]
    assert re.fullmatch(r"[a-z0-9][a-z0-9_\-]{0,127}", requirement["key"])
    assert requirement["purpose"] == "Monitor prices"
    assert normalize_compiled_spec(payload, job_brief=BRIEF) == spec


def test_stored_contract_reaches_generic_runtime_and_fails_closed_without_plan(database, monkeypatch):
    factory, _ = database
    payload = deepcopy(PAYLOAD)
    payload["permissions"] = [{"action": KEY, "mode": "automatic"}]
    _cache(
        factory,
        normalize_compiled_spec(payload, job_brief=BRIEF),
        owner_permissions={KEY: "automatic"},
    )
    api.compile_employee(1, USER)
    captured = []

    def dispatch(**kwargs):
        captured.append(kwargs)
        return {
            "success": False,
            "request_id": kwargs.get("request_id"),
            "action": kwargs["action"],
            "data": {"runtime": "generic_capability"},
            "error": "execution_plan_required",
        }

    monkeypatch.setattr("backend.app.modules.tools.workflow_action_request.n8n_gateway.execute", dispatch)
    with factory() as db:
        result = WorkflowActionRequestTool().execute(
            {"operation": "prepare", "action_type": KEY, "details": {"target": "example.test"}},
            {"db": db, "company_id": 1, "agent_id": 1, "config": reveal_config(_assignment(db).config)},
        )
        assert result.success is False
        assert result.error == "execution_plan_required"
        assert result.data["runtime"] == "generic_capability"
        assert db.query(ActionRequest).one().status == "external_failed"
    assert captured[0]["action"] == f"{KEY}.execute"
    assert captured[0]["data"]["idempotency_key"]
    destination = captured[0]["data"]["action_config"]["destination"]
    assert destination["type"] == "xvond_internal"
    assert destination["adapter"] == "generic_capability"
    assert destination["capability_key"] == KEY



def _scheduled_payload(*, permission_mode="automatic", schedule=None):
    schedule = deepcopy(schedule) if schedule is not None else {
        "kind": "interval",
        "every_minutes": 60,
        "source_text": "every 60 minutes",
    }
    source_text = str(schedule.get("source_text") or "every 60 minutes")
    schedule["source_text"] = source_text
    brief = (
        "Monitor https://prices.example.com "
        + source_text
        + " automatically and notify me when the value reaches 100."
    )
    payload = {
        "role": "Price monitor",
        "scope": "personal",
        "summary": "Monitor a price endpoint.",
        "tasks": [{"name": "Monitor", "description": brief, "trigger": "recurring"}],
        "requirements": [{
            "key": KEY,
            "kind": "custom",
            "purpose": "Monitor price",
            "primitives": ["http_api", "scheduler", "workflow_engine"],
            "schedule": schedule,
            "runtime_inputs": {
                "url": "https://prices.example.com",
                "threshold": 100,
            },
            "execution_plan": [
                {"id": "fetch", "op": "http_get_json", "url_field": "url"},
                {"id": "value", "op": "extract", "source": "fetch", "path": "value"},
                {
                    "id": "matched",
                    "op": "compare",
                    "source": "value",
                    "operator": "gte",
                    "value_field": "threshold",
                },
                {
                    "id": "notify",
                    "op": "notify",
                    "when": "matched",
                    "title": "Price monitor",
                    "message": "Target reached.",
                },
            ],
        }],
        "permissions": [{"action": "Monitor price", "mode": permission_mode}],
        "setup_questions": [],
    }
    return brief, payload


def test_self_service_recurring_capability_provisions_one_real_schedule_workflow(database):
    factory, calls = database
    brief, payload = _scheduled_payload()
    with factory() as db:
        company = db.get(Company, 1)
        company.onboarding_source = "self_service"
        agent = db.get(AIAgent, 1)
        agent.description = brief
        config = db.query(AgentConfig).filter_by(agent_id=1).one()
        settings = deepcopy(config.settings)
        settings["employee_builder"]["source_description"] = brief
        config.settings = settings
        db.commit()

    spec = normalize_compiled_spec(payload, job_brief=brief)
    _cache(factory, spec, owner_permissions={KEY: "automatic"})
    result = api.compile_employee(1, USER)

    requirement = result["spec"]["requirements"][0]
    assert requirement["execution_status"] == "ready"
    assert requirement["schedule_status"] == "ready"
    assert requirement["schedule_workflow_id"]
    assert result["spec"]["delivery"]["automation_plan"][KEY]["status"] == "ready"

    with factory() as db:
        rows = db.query(AutomationWorkflow).all()
        assert len(rows) == 1
        workflow = rows[0]
        assert workflow.enabled is True
        assert workflow.trigger_type == "schedule"
        assert workflow.trigger_config["_xvond_source"] == "self_service_employee"
        assert workflow.trigger_config["_xvond_agent_id"] == 1
        assert workflow.trigger_config["_xvond_requirement_key"] == KEY
        assert workflow.trigger_config["schedule"] == {
            "kind": "interval",
            "every_minutes": 60,
        }
        assert workflow.trigger_config["input_data"] == {
            "url": "https://prices.example.com",
            "threshold": 100,
        }
        assert len(workflow.steps) == 1
        graph_step = workflow.steps[0]
        assert graph_step["type"] == "graph"
        assert graph_step["agent_id"] == 1
        assert [node["type"] for node in graph_step["graph"]["nodes"]] == ["action"]
        assert graph_step["graph"]["nodes"][0]["params"]["action_type"] == KEY

    # Cached compilation repairs/reuses the same generated workflow instead of
    # creating duplicates or paying for another compiler call.
    api.compile_employee(1, USER)
    with factory() as db:
        assert db.query(AutomationWorkflow).count() == 1
    assert calls == []


def test_self_service_content_generation_schedule_builds_ai_then_action(database):
    factory, _ = database
    brief, payload = _scheduled_payload()
    payload["requirements"][0]["purpose"] = "Create and publish the daily social post"
    payload["requirements"][0]["primitives"] = [
        "content_generation",
        "scheduler",
        "workflow_engine",
    ]
    payload["requirements"][0]["execution_plan"] = [
        {
            "id": "notify",
            "op": "notify",
            "title": "Content ready",
            "message": "Scheduled content was generated.",
        }
    ]
    payload["permissions"] = [
        {"action": "Create and publish the daily social post", "mode": "automatic"}
    ]

    with factory() as db:
        company = db.get(Company, 1)
        company.onboarding_source = "self_service"
        db.commit()

    _cache(
        factory,
        normalize_compiled_spec(payload, job_brief=brief),
        owner_permissions={KEY: "automatic"},
    )
    result = api.compile_employee(1, USER)
    requirement = result["spec"]["requirements"][0]

    assert requirement["execution_status"] == "ready"
    assert requirement["schedule_status"] == "ready"

    with factory() as db:
        workflow = db.query(AutomationWorkflow).one()
        assert len(workflow.steps) == 1
        graph_step = workflow.steps[0]
        assert graph_step["type"] == "graph"
        assert graph_step["agent_id"] == 1
        nodes = graph_step["graph"]["nodes"]
        assert [node["type"] for node in nodes] == ["ai", "action"]
        assert "Create and publish the daily social post" in nodes[0]["params"]["prompt"]
        assert nodes[1]["params"]["action_type"] == KEY
        assert nodes[1]["params"]["arguments"]["caption"] == "$nodes.generate_content.ai_response"


def test_self_service_graph_schedule_owns_the_full_pipeline(database):
    factory, _ = database
    brief = "Every 60 minutes generate a summary and save the result automatically."
    payload = {
        "role": "Scheduled graph worker",
        "scope": "business",
        "requirements": [
            {
                "key": KEY,
                "kind": "custom",
                "purpose": "Save the generated summary",
                "primitives": [
                    "content_generation",
                    "scheduler",
                    "workflow_engine",
                ],
                "schedule": {
                    "kind": "interval",
                    "every_minutes": 60,
                    "source_text": "Every 60 minutes",
                },
                "execution_plan": [
                    {
                        "id": "notify",
                        "op": "notify",
                        "title": "Saved",
                        "message": "Summary saved.",
                    }
                ],
            }
        ],
        "permissions": [
            {"action": "Save the generated summary", "mode": "automatic"}
        ],
        "execution_graph": {
            "version": 1,
            "trigger": {
                "type": "schedule",
                "schedule": {
                    "kind": "interval",
                    "every_minutes": 60,
                },
            },
            "nodes": [
                {
                    "id": "draft",
                    "type": "ai",
                    "depends_on": [],
                    "params": {"prompt": "Generate the scheduled summary."},
                },
                {
                    "id": "save",
                    "type": "action",
                    "depends_on": ["draft"],
                    "params": {
                        "action_type": KEY,
                        "arguments": {
                            "body": "$nodes.draft.ai_response"
                        },
                    },
                },
            ],
        },
    }

    with factory() as db:
        company = db.get(Company, 1)
        company.onboarding_source = "self_service"
        db.commit()

    _cache(factory, normalize_compiled_spec(payload, job_brief=brief))
    result = api.compile_employee(1, USER)

    trigger = result["spec"]["delivery"]["graph_trigger"]
    assert trigger["status"] == "ready"
    assert trigger["trigger_type"] == "schedule"
    assert trigger["workflow_id"]

    requirement = result["spec"]["requirements"][0]
    assert requirement.get("schedule_workflow_id") is None

    with factory() as db:
        workflows = db.query(AutomationWorkflow).all()
        assert len(workflows) == 1
        workflow = workflows[0]
        assert workflow.trigger_type == "schedule"
        assert workflow.trigger_config["schedule"] == {
            "kind": "interval",
            "every_minutes": 60,
        }
        assert workflow.trigger_config["_xvond_graph_trigger"] is True
        assert workflow.steps[0]["type"] == "graph"
        assert [node["id"] for node in workflow.steps[0]["graph"]["nodes"]] == [
            "draft",
            "save",
        ]


def test_self_service_media_generation_schedule_builds_ai_media_then_action(database):
    factory, _ = database
    brief, payload = _scheduled_payload()
    payload["requirements"][0]["purpose"] = "Create and publish the daily Instagram post"
    payload["requirements"][0]["primitives"] = [
        "content_generation",
        "media_generation",
        "scheduler",
        "workflow_engine",
    ]
    payload["requirements"][0]["execution_plan"] = [
        {
            "id": "notify",
            "op": "notify",
            "title": "Content ready",
            "message": "Scheduled content was generated.",
        }
    ]
    payload["permissions"] = [
        {"action": "Create and publish the daily Instagram post", "mode": "automatic"}
    ]

    with factory() as db:
        company = db.get(Company, 1)
        company.onboarding_source = "self_service"
        db.commit()

    _cache(
        factory,
        normalize_compiled_spec(payload, job_brief=brief),
        owner_permissions={KEY: "automatic"},
    )
    result = api.compile_employee(1, USER)
    requirement = result["spec"]["requirements"][0]

    assert requirement["execution_status"] == "ready"
    assert requirement["schedule_status"] == "ready"

    with factory() as db:
        workflow = db.query(AutomationWorkflow).one()
        assert len(workflow.steps) == 1
        graph_step = workflow.steps[0]
        assert graph_step["type"] == "graph"
        nodes = graph_step["graph"]["nodes"]
        assert [node["type"] for node in nodes] == ["ai", "media", "action"]
        assert nodes[1]["params"]["size"] == "1024x1024"
        assert nodes[2]["params"]["action_type"] == KEY
        assert nodes[2]["params"]["arguments"]["media_url"] == "$nodes.generate_media.media_url"


def test_self_service_manual_graph_is_provisioned_for_dashboard_worker(database):
    factory, _ = database
    brief = "Give me an internal dashboard worker that summarizes the supplied data when I run it."
    payload = {
        "role": "Dashboard worker",
        "scope": "personal",
        "requirements": [],
        "permissions": [],
        "execution_graph": {
            "version": 1,
            "trigger": {"type": "manual"},
            "nodes": [
                {
                    "id": "summarize",
                    "type": "ai",
                    "depends_on": [],
                    "params": {
                        "prompt": "Summarize the supplied data for the owner."
                    },
                }
            ],
        },
    }

    with factory() as db:
        company = db.get(Company, 1)
        company.onboarding_source = "self_service"
        db.commit()

    _cache(factory, normalize_compiled_spec(payload, job_brief=brief))
    result = api.compile_employee(1, USER)

    trigger = result["spec"]["delivery"]["graph_trigger"]
    assert trigger["status"] == "ready"
    assert trigger["trigger_type"] == "manual"
    assert trigger["workflow_id"]

    with factory() as db:
        workflow = db.get(AutomationWorkflow, trigger["workflow_id"])
        assert workflow.trigger_type == "manual"
        assert workflow.enabled is True
        assert workflow.steps[0]["type"] == "graph"
        assert workflow.steps[0]["graph"]["nodes"][0]["type"] == "ai"


def test_self_service_direct_approval_graph_is_provisioned(database):
    factory, _ = database
    brief = "When I run this employee, prepare the report and ask me before sending it."
    payload = {
        "role": "Approval worker",
        "scope": "business",
        "requirements": [
            {
                "key": KEY,
                "kind": "custom",
                "purpose": "Send the report",
                "primitives": ["workflow_engine"],
                "execution_plan": [
                    {
                        "id": "notify",
                        "op": "notify",
                        "title": "Report",
                        "message": "Report ready.",
                    }
                ],
            }
        ],
        "permissions": [{"action": "Send the report", "mode": "ask_before"}],
        "execution_graph": {
            "version": 1,
            "trigger": {"type": "manual"},
            "nodes": [
                {
                    "id": "send",
                    "type": "action",
                    "depends_on": [],
                    "params": {
                        "action_type": KEY,
                        "arguments": {"report": "ready"},
                    },
                }
            ],
        },
    }

    with factory() as db:
        company = db.get(Company, 1)
        company.onboarding_source = "self_service"
        db.commit()

    _cache(factory, normalize_compiled_spec(payload, job_brief=brief))
    result = api.compile_employee(1, USER)

    trigger = result["spec"]["delivery"]["graph_trigger"]
    assert trigger["status"] == "ready"
    assert trigger["trigger_type"] == "manual"
    assert trigger["workflow_id"]


def test_self_service_nested_approval_graph_is_provisioned(database):
    factory, _ = database
    brief = "For every lead, ask me before sending the message."
    payload = {
        "role": "Approval worker",
        "scope": "business",
        "requirements": [
            {
                "key": KEY,
                "kind": "custom",
                "purpose": "Send lead message",
                "primitives": ["workflow_engine"],
                "execution_plan": [
                    {
                        "id": "notify",
                        "op": "notify",
                        "title": "Lead",
                        "message": "Lead message ready.",
                    }
                ],
            }
        ],
        "permissions": [{"action": "Send lead message", "mode": "ask_before"}],
        "execution_graph": {
            "version": 1,
            "trigger": {"type": "manual"},
            "nodes": [
                {
                    "id": "each",
                    "type": "foreach",
                    "depends_on": [],
                    "params": {
                        "items": "$input.leads",
                        "graph": {
                            "version": 1,
                            "nodes": [
                                {
                                    "id": "send",
                                    "type": "action",
                                    "depends_on": [],
                                    "params": {
                                        "action_type": KEY,
                                        "arguments": {"lead": "$item"},
                                    },
                                }
                            ],
                        },
                    },
                }
            ],
        },
    }

    with factory() as db:
        company = db.get(Company, 1)
        company.onboarding_source = "self_service"
        db.commit()

    _cache(factory, normalize_compiled_spec(payload, job_brief=brief))
    result = api.compile_employee(1, USER)

    trigger = result["spec"]["delivery"]["graph_trigger"]
    assert trigger["status"] == "ready"
    assert trigger["workflow_id"]


def test_self_service_webhook_graph_is_provisioned_when_actions_are_ready(database):
    factory, _ = database
    brief = "When my external system sends an event, notify me automatically."
    purpose = "Notify me from the external event"
    payload = {
        "role": "Event worker",
        "scope": "business",
        "requirements": [
            {
                "key": KEY,
                "kind": "custom",
                "purpose": purpose,
                "primitives": ["workflow_engine"],
                "execution_plan": [
                    {
                        "id": "notify",
                        "op": "notify",
                        "title": "External event",
                        "message": "The external event arrived.",
                    }
                ],
            }
        ],
        "permissions": [{"action": purpose, "mode": "automatic"}],
        "execution_graph": {
            "version": 1,
            "trigger": {"type": "webhook"},
            "nodes": [
                {
                    "id": "act",
                    "type": "action",
                    "depends_on": [],
                    "params": {"action_type": KEY, "arguments": {}},
                }
            ],
        },
    }

    with factory() as db:
        company = db.get(Company, 1)
        company.onboarding_source = "self_service"
        db.commit()

    _cache(factory, normalize_compiled_spec(payload, job_brief=brief))
    result = api.compile_employee(1, USER)

    trigger = result["spec"]["delivery"]["graph_trigger"]
    assert trigger["status"] == "ready"
    assert trigger["trigger_type"] == "webhook"
    assert trigger["workflow_id"]

    with factory() as db:
        workflow = db.get(AutomationWorkflow, trigger["workflow_id"])
        assert workflow.trigger_type == "webhook"
        assert workflow.enabled is True
        assert workflow.trigger_config["_xvond_graph_trigger"] is True
        assert workflow.steps[0]["type"] == "graph"
        assert workflow.steps[0]["graph"]["nodes"][0]["params"]["action_type"] == KEY


def test_self_service_schedule_requires_explicit_automatic_permission(database):
    factory, _ = database
    brief, payload = _scheduled_payload(permission_mode="ask_before")
    with factory() as db:
        company = db.get(Company, 1)
        company.onboarding_source = "self_service"
        db.commit()

    _cache(factory, normalize_compiled_spec(payload, job_brief=brief))
    result = api.compile_employee(1, USER)
    requirement = result["spec"]["requirements"][0]

    assert requirement["execution_status"] == "setup_required"
    assert requirement["schedule_status"] == "approval_required"
    with factory() as db:
        assert db.query(AutomationWorkflow).count() == 0


def test_self_service_daily_schedule_inherits_workspace_timezone(database):
    factory, _ = database
    brief, payload = _scheduled_payload(
        schedule={"kind": "daily", "hour": 8, "minute": 15, "source_text": "every day at 8:15"},
    )
    with factory() as db:
        company = db.get(Company, 1)
        company.onboarding_source = "self_service"
        db.add(CompanyProfile(company_id=1, timezone="Asia/Muscat"))
        db.commit()

    _cache(
        factory,
        normalize_compiled_spec(payload, job_brief=brief),
        owner_permissions={KEY: "automatic"},
    )
    result = api.compile_employee(1, USER)
    requirement = result["spec"]["requirements"][0]

    assert requirement["execution_status"] == "ready"
    assert requirement["schedule_status"] == "ready"
    with factory() as db:
        workflow = db.query(AutomationWorkflow).one()
        assert workflow.trigger_config["schedule"] == {
            "kind": "daily",
            "hour": 8,
            "minute": 15,
            "timezone": "Asia/Muscat",
        }


def test_self_service_schedule_blocks_when_required_runtime_input_is_missing(database):
    factory, _ = database
    brief, payload = _scheduled_payload()
    payload["requirements"][0]["runtime_inputs"].pop("threshold")
    with factory() as db:
        company = db.get(Company, 1)
        company.onboarding_source = "self_service"
        db.commit()

    _cache(
        factory,
        normalize_compiled_spec(payload, job_brief=brief),
        owner_permissions={KEY: "automatic"},
    )
    result = api.compile_employee(1, USER)
    requirement = result["spec"]["requirements"][0]

    assert requirement["execution_status"] == "setup_required"
    assert requirement["schedule_status"] == "runtime_inputs_required"
    assert requirement["schedule_missing_inputs"] == ["threshold"]
    with factory() as db:
        assert db.query(AutomationWorkflow).count() == 0


def test_self_service_job_brief_revision_clears_only_generated_build_artifacts(database):
    factory, calls = database
    with factory() as db:
        company = db.get(Company, 1)
        company.onboarding_source = "self_service"
        db.commit()

    api.compile_employee(1, USER)

    with factory() as db:
        assignment = _assignment(db)
        assignment_config = reveal_config(assignment.config)
        assignment_config["actions"]["operator_action"] = {
            "enabled": True,
            "confirmation_required": True,
            "destination": {"type": "integration", "integration_id": 99},
        }
        assignment.config = assignment_config

        config = db.query(AgentConfig).filter_by(agent_id=1).one()
        config.capabilities = {"customer_support": True}
        db.add_all([
            AgentToolAssignment(
                agent_id=1,
                tool_name="human_handoff",
                config={},
                enabled=True,
            ),
            AgentToolAssignment(
                agent_id=1,
                tool_name="webhook",
                config={"url": "https://operator.example.com/hook"},
                enabled=True,
            ),
            AgentChannel(
                company_id=1,
                agent_id=1,
                channel_type="whatsapp",
                config={"phone_number_id": "old-phone"},
                enabled=True,
            ),
        ])

        generated = AutomationWorkflow(
            company_id=1,
            name="Generated old schedule",
            trigger_type="schedule",
            trigger_config={
                "_xvond_source": "self_service_employee",
                "_xvond_agent_id": 1,
                "_xvond_requirement_key": KEY,
            },
            steps=[],
            enabled=False,
        )
        manual = AutomationWorkflow(
            company_id=1,
            name="Manual schedule",
            trigger_type="schedule",
            trigger_config={"schedule": {"kind": "interval", "every_minutes": 60}},
            steps=[],
            enabled=False,
        )
        db.add_all([generated, manual])
        db.flush()
        db.add(
            AutomationRun(
                company_id=1,
                workflow_id=generated.id,
                status="completed",
                input_data={},
                output_data={"historical": True},
            )
        )
        db.commit()

    revised = "Create content drafts for my social posts and organize the writing work."
    result = api.revise_self_service_job_brief(
        1,
        api.EmployeeBuilderReviseRequest(description=revised),
        USER,
    )

    assert result["status"] == "updated"
    assert result["compiled"] is False
    assert result["job_brief"] == revised
    assert result["deactivated_channels"] == ["whatsapp"]
    assert calls and len(calls) == 1

    with factory() as db:
        builder = _builder(db)
        assert builder["source_description"] == revised
        assert builder["job_brief"] == revised
        assert builder["compiled_spec"] is None
        assert "delivery" not in builder
        assert "compiled_at" not in builder

        agent = db.get(AIAgent, 1)
        assert agent.description == revised
        config = db.query(AgentConfig).filter_by(agent_id=1).one()
        assert config.capabilities == {"content": True}

        actions = reveal_config(_assignment(db).config)["actions"]
        assert set(actions) == {"operator_action"}
        assert actions["operator_action"]["destination"]["integration_id"] == 99

        handoff = db.query(AgentToolAssignment).filter_by(
            agent_id=1, tool_name="human_handoff"
        ).one()
        assert handoff.enabled is False
        handoff_config = reveal_config(handoff.config)
        assert handoff_config["_xvond_source"] == "employee_builder"
        assert handoff_config["_xvond_retired_by_revision"] is True

        manual_webhook = db.query(AgentToolAssignment).filter_by(
            agent_id=1, tool_name="webhook"
        ).one()
        assert manual_webhook.enabled is True
        assert reveal_config(manual_webhook.config)["url"] == "https://operator.example.com/hook"

        whatsapp = db.query(AgentChannel).filter_by(
            company_id=1, agent_id=1, channel_type="whatsapp"
        ).one()
        assert whatsapp.enabled is False
        assert reveal_config(whatsapp.config)["phone_number_id"] == "old-phone"

        workflows = {item.name: item for item in db.query(AutomationWorkflow).all()}
        assert set(workflows) == {"Generated old schedule", "Manual schedule"}
        retired = workflows["Generated old schedule"]
        assert retired.enabled is False
        assert retired.trigger_config["_xvond_source"] == "self_service_employee_retired"
        assert retired.trigger_config["_xvond_retired_at"]
        assert db.query(AutomationRun).filter_by(workflow_id=retired.id).count() == 1
        assert workflows["Manual schedule"].trigger_config == {
            "schedule": {"kind": "interval", "every_minutes": 60}
        }


def test_job_brief_revision_is_self_service_only(database):
    factory, calls = database

    with pytest.raises(HTTPException) as exc:
        api.revise_self_service_job_brief(
            1,
            api.EmployeeBuilderReviseRequest(description="Create content drafts for my social posts."),
            USER,
        )

    assert exc.value.status_code == 409
    assert "Managed employees" in str(exc.value.detail)
    with factory() as db:
        assert _builder(db)["source_description"] == BRIEF
    assert calls == []


def test_live_self_service_employee_must_be_deactivated_before_job_brief_revision(database):
    factory, calls = database
    with factory() as db:
        company = db.get(Company, 1)
        company.onboarding_source = "self_service"
        db.get(AIAgent, 1).enabled = True
        db.commit()

    with pytest.raises(HTTPException) as exc:
        api.revise_self_service_job_brief(
            1,
            api.EmployeeBuilderReviseRequest(description="Create content drafts for my social posts."),
            USER,
        )

    assert exc.value.status_code == 409
    assert "Deactivate" in str(exc.value.detail)
    with factory() as db:
        assert _builder(db)["source_description"] == BRIEF
    assert calls == []


def test_job_brief_revision_keeps_still_requested_channel_active(database):
    factory, calls = database
    with factory() as db:
        company = db.get(Company, 1)
        company.onboarding_source = "self_service"
        db.add(
            AgentChannel(
                company_id=1,
                agent_id=1,
                channel_type="whatsapp",
                config={"phone_number_id": "keep-phone"},
                enabled=True,
            )
        )
        db.commit()

    revised = "Reply to customers on WhatsApp and help with their questions."
    result = api.revise_self_service_job_brief(
        1,
        api.EmployeeBuilderReviseRequest(description=revised),
        USER,
    )

    assert result["requested_channels"] == ["whatsapp"]
    assert result["deactivated_channels"] == []
    with factory() as db:
        whatsapp = db.query(AgentChannel).filter_by(
            company_id=1, agent_id=1, channel_type="whatsapp"
        ).one()
        assert whatsapp.enabled is True
        assert reveal_config(whatsapp.config)["phone_number_id"] == "keep-phone"
    assert calls == []



def test_live_refinement_stages_and_tests_without_mutating_live_employee(database, monkeypatch):
    factory, calls = database
    monkeypatch.setattr(api, "_has_ai_agents_entitlement", lambda *args, **kwargs: True)

    with factory() as db:
        company = db.get(Company, 1)
        company.onboarding_source = "self_service"
        company.lifecycle_status = "live"
        agent = db.get(AIAgent, 1)
        agent.enabled = False
        db.commit()

    api.compile_employee(1, USER)

    with factory() as db:
        agent = db.get(AIAgent, 1)
        live_prompt = agent.system_prompt
        live_description = agent.description
        builder = _builder(db)
        live_compiled_at = builder["compiled_at"]
        live_spec = deepcopy(builder["compiled_spec"])
        agent.enabled = True
        db.commit()

    result = api.refine_self_service_employee(
        1,
        api.EmployeeBuilderRefineRequest(
            instruction="Make the employee more concise and keep the live version running."
        ),
        USER,
    )

    assert result["status"] == "revision_staged_and_built"
    assert result["live_employee_unchanged"] is True
    with factory() as db:
        agent = db.get(AIAgent, 1)
        builder = _builder(db)
        pending = builder["pending_revision"]
        assert agent.enabled is True
        assert agent.description == live_description
        assert agent.system_prompt == live_prompt
        assert builder["compiled_at"] == live_compiled_at
        assert builder["compiled_spec"] == live_spec
        assert pending["base_compiled_at"] == live_compiled_at
        assert pending["status"] == "built"
        assert "OWNER REFINEMENT" in pending["source_description"]
        assert "more concise" in pending["source_description"]
        assert isinstance(pending["compiled_spec"], dict)

    tested = api.test_draft_employee(
        1,
        api.EmployeeBuilderTestRequest(message="How would you answer a customer?"),
        USER,
    )
    assert tested["test_target"] == "pending_revision"
    assert tested["lifecycle"] == "live"

    with factory() as db:
        agent = db.get(AIAgent, 1)
        builder = _builder(db)
        pending = builder["pending_revision"]
        assert agent.description == live_description
        assert agent.system_prompt == live_prompt
        assert builder["compiled_at"] == live_compiled_at
        assert pending["status"] == "tested"
        assert pending["last_tested_compiled_at"] == pending["compiled_at"]

    assert len(calls) == 3


def test_tested_pending_revision_applies_atomically_without_deactivation(database, monkeypatch):
    factory, _ = database
    monkeypatch.setattr(
        api,
        "self_service_readiness",
        lambda *args, **kwargs: {
            "ready": True,
            "blockers": [],
            "slot_channels": [],
            "missing_channels": [],
        },
    )

    old_spec = normalize_compiled_spec(
        {
            "role": "Old employee",
            "summary": "Old behavior",
            "requirements": [],
            "permissions": [],
            "execution_graph": {"version": 1, "trigger": {"type": "manual"}, "nodes": []},
        },
        job_brief="Old behavior",
    )
    new_spec = normalize_compiled_spec(
        {
            "role": "New employee",
            "summary": "New behavior",
            "requirements": [],
            "permissions": [],
            "execution_graph": {"version": 1, "trigger": {"type": "manual"}, "nodes": []},
        },
        job_brief="New behavior",
    )

    with factory() as db:
        company = db.get(Company, 1)
        company.onboarding_source = "self_service"
        company.lifecycle_status = "live"
        company.active = True
        agent = db.get(AIAgent, 1)
        agent.enabled = True
        agent.description = "Old behavior"
        config = db.query(AgentConfig).filter_by(agent_id=1).one()
        settings = deepcopy(config.settings)
        settings["employee_builder"] = {
            "source_description": "Old behavior",
            "job_brief": "Old behavior",
            "requested_channels": [],
            "permissions": {},
            "setup_answers": {},
            "compiled_spec": old_spec,
            "compiled_at": "old-build",
            "last_tested_at": "old-test",
            "last_tested_compiled_at": "old-build",
            "pending_revision": {
                "version": 1,
                "status": "tested",
                "source_description": "New behavior",
                "job_brief": "New behavior",
                "audience": "business",
                "requested_channels": [],
                "permissions": {},
                "capabilities": {},
                "setup_answers": {},
                "base_compiled_at": "old-build",
                "compiled_spec": new_spec,
                "compiled_at": "new-build",
                "compiler_provider": "mock",
                "compiler_model": "mock",
                "last_tested_at": "new-test",
                "last_tested_compiled_at": "new-build",
            },
        }
        config.settings = settings
        db.add(
            AutomationWorkflow(
                company_id=1,
                name="Old generated event",
                trigger_type="event",
                trigger_config={
                    "_xvond_source": "self_service_employee",
                    "_xvond_agent_id": 1,
                    "_xvond_graph_trigger": True,
                    "event_name": "old.event",
                },
                steps=[],
                enabled=True,
            )
        )
        db.commit()

    applied = api.apply_pending_live_revision(1, USER)
    assert applied["status"] == "revision_applied"
    assert applied["live_employee_replaced_atomically"] is True

    with factory() as db:
        agent = db.get(AIAgent, 1)
        config = db.query(AgentConfig).filter_by(agent_id=1).one()
        builder = config.settings["employee_builder"]
        assert agent.enabled is True
        assert agent.description == "New behavior"
        assert builder["source_description"] == "New behavior"
        assert builder["compiled_at"] == "new-build"
        assert "pending_revision" not in builder
        assert builder["versions"][-1]["source_description"] == "Old behavior"
        retired = db.query(AutomationWorkflow).filter_by(name="Old generated event").one()
        assert retired.enabled is False
        assert retired.trigger_config["_xvond_source"] == "self_service_employee_retired"



def test_live_pending_revision_can_be_built_after_entitlement(database, monkeypatch):
    factory, calls = database
    monkeypatch.setattr(api, "_has_ai_agents_entitlement", lambda *args, **kwargs: True)

    with factory() as db:
        company = db.get(Company, 1)
        company.onboarding_source = "self_service"
        company.lifecycle_status = "live"
        agent = db.get(AIAgent, 1)
        agent.enabled = True
        live_description = agent.description
        live_prompt = agent.system_prompt
        config = db.query(AgentConfig).filter_by(agent_id=1).one()
        settings = deepcopy(config.settings)
        builder = deepcopy(settings["employee_builder"])
        builder["compiled_at"] = "live-build"
        builder["pending_revision"] = {
            "version": 1,
            "status": "draft",
            "source_description": "Draft a concise weekly market summary.",
            "job_brief": "Draft a concise weekly market summary.",
            "requested_channels": [],
            "setup_answers": {},
            "base_compiled_at": "live-build",
            "created_at": "2026-09-19T00:00:00Z",
            "compiled_spec": None,
        }
        settings["employee_builder"] = builder
        config.settings = settings
        db.commit()

    result = api.build_pending_live_revision(1, USER)
    assert result["status"] == "pending_revision_built"
    assert result["live_employee_unchanged"] is True

    with factory() as db:
        agent = db.get(AIAgent, 1)
        pending = _builder(db)["pending_revision"]
        assert agent.enabled is True
        assert agent.description == live_description
        assert agent.system_prompt == live_prompt
        assert pending["status"] == "built"
        assert isinstance(pending["compiled_spec"], dict)
        assert pending["compiled_at"]
        assert "last_tested_compiled_at" not in pending

    assert len(calls) == 1


def test_live_rollback_stages_previous_version_without_touching_live_runtime(database):
    factory, calls = database
    previous_spec = normalize_compiled_spec(
        {
            "role": "Previous employee",
            "summary": "Previous behavior",
            "requirements": [],
            "permissions": [],
            "execution_graph": {
                "version": 1,
                "trigger": {"type": "manual"},
                "nodes": [],
            },
        },
        job_brief="Previous behavior",
    )

    with factory() as db:
        company = db.get(Company, 1)
        company.onboarding_source = "self_service"
        company.lifecycle_status = "live"
        agent = db.get(AIAgent, 1)
        agent.enabled = True
        agent.description = "Current behavior"
        agent.system_prompt = "current live prompt"
        config = db.query(AgentConfig).filter_by(agent_id=1).one()
        settings = deepcopy(config.settings)
        settings["employee_builder"] = {
            "source_description": "Current behavior",
            "job_brief": "Current behavior",
            "compiled_at": "current-build",
            "compiled_spec": normalize_compiled_spec(
                {
                    "role": "Current employee",
                    "summary": "Current behavior",
                    "requirements": [],
                    "permissions": [],
                    "execution_graph": {
                        "version": 1,
                        "trigger": {"type": "manual"},
                        "nodes": [],
                    },
                },
                job_brief="Current behavior",
            ),
            "requested_channels": [],
            "permissions": {},
            "setup_answers": {},
            "versions": [
                {
                    "id": "previous-v1",
                    "created_at": "2026-09-18T00:00:00Z",
                    "reason": "previous",
                    "source_description": "Previous behavior",
                    "compiled_spec": previous_spec,
                    "compiled_at": "previous-build",
                    "setup_answers": {},
                    "requested_channels": [],
                    "audience": "business",
                    "permissions": {},
                    "capabilities": {},
                }
            ],
        }
        config.settings = settings
        db.commit()

    result = api.rollback_self_service_employee(
        1,
        api.EmployeeBuilderRollbackRequest(version_id="previous-v1"),
        USER,
    )
    assert result["status"] == "rollback_staged"
    assert result["live_employee_unchanged"] is True
    assert result["compiled"] is True

    with factory() as db:
        agent = db.get(AIAgent, 1)
        builder = _builder(db)
        pending = builder["pending_revision"]
        assert agent.enabled is True
        assert agent.description == "Current behavior"
        assert agent.system_prompt == "current live prompt"
        assert builder["source_description"] == "Current behavior"
        assert builder["compiled_at"] == "current-build"
        assert pending["source_description"] == "Previous behavior"
        assert pending["source_version_id"] == "previous-v1"
        assert pending["base_compiled_at"] == "current-build"
        assert pending["status"] == "built"
        assert "last_tested_compiled_at" not in pending

    assert calls == []


def _seed_owner_permission_contract(factory, *, live: bool):
    with factory() as db:
        company = db.query(Company).filter_by(id=1).one()
        company.onboarding_source = "self_service"
        company.lifecycle_status = "live" if live else "paused"
        company.active = True

        agent = db.query(AIAgent).filter_by(id=1).one()
        agent.enabled = live

        config = db.query(AgentConfig).filter_by(agent_id=1).one()
        settings = deepcopy(config.settings or {})
        builder = dict(settings.get("employee_builder") or {})
        builder.update(
            {
                "compiled_at": "2026-09-19T00:00:00Z",
                "last_tested_at": "2026-09-19T00:01:00Z",
                "last_tested_compiled_at": "2026-09-19T00:00:00Z",
                "owner_permissions": {},
                "compiled_spec": {
                    "version": 1,
                    "job_brief": "Monitor and send a report.",
                    "role": "Report worker",
                    "scope": "business",
                    "summary": "Monitor and send a report.",
                    "tasks": [],
                    "requirements": [
                        {
                            "key": KEY,
                            "kind": "custom",
                            "purpose": "Monitor the specialist platform",
                            "status": "xvond_managed",
                            "delivery_mode": "compose",
                            "primitives": ["workflow_engine"],
                            "runtime_inputs": {},
                            "execution_plan": [
                                {
                                    "id": "notify",
                                    "op": "notify",
                                    "title": "Monitor",
                                    "message": "Done.",
                                }
                            ],
                            "customer_inputs": [],
                            "requires_connection": False,
                            "fulfillment_mode": "xvond_internal",
                            "known_to_xvond": False,
                        }
                    ],
                    "permissions": [
                        {
                            "action": KEY,
                            "mode": "ask_before",
                            "suggested_mode": "automatic",
                            "source": "compiler_suggestion",
                        }
                    ],
                    "execution_graph": {
                        "version": 1,
                        "trigger": {"type": "manual"},
                        "nodes": [],
                    },
                    "setup_questions": [],
                    "ready_requirements": [],
                    "build_required": [],
                    "setup_required": [],
                    "unsupported_requirements": [],
                    "delivery": {
                        "provisioning_version": 1,
                        "action_plan": {
                            KEY: {
                                "tool_name": "action_request",
                                "action_type": KEY,
                                "execution_status": "ready",
                            }
                        },
                        "automation_plan": {},
                        "graph_trigger": {
                            "status": "not_required",
                            "workflow_id": None,
                            "trigger_type": "manual",
                        },
                        "managed_capabilities": [KEY],
                        "connection_required": [],
                        "customer_input_required": [],
                        "unsupported": [],
                    },
                },
            }
        )
        settings["employee_builder"] = builder
        config.settings = settings

        assignment = (
            db.query(AgentToolAssignment)
            .filter_by(agent_id=1, tool_name="action_request")
            .first()
        )
        if assignment is None:
            assignment = AgentToolAssignment(
                agent_id=1,
                tool_name="action_request",
                enabled=True,
                config={},
            )
            db.add(assignment)
            db.flush()
        assignment.config = {
            "actions": {
                KEY: {
                    "enabled": True,
                    "confirmation_required": True,
                    "xvond_generated": True,
                    "_xvond_permission_mode": "ask_before",
                    "module": "tools",
                    "destination": {
                        "type": "xvond_internal",
                        "adapter": "generic_capability",
                        "capability_key": KEY,
                        "execution_plan": [
                            {
                                "id": "notify",
                                "op": "notify",
                                "title": "Monitor",
                                "message": "Done.",
                            }
                        ],
                        "allowed_hosts": [],
                    },
                    "availability": {"mode": "none"},
                }
            }
        }
        db.commit()


def test_live_employee_cannot_escalate_owner_permission_to_automatic(database):
    factory, _ = database
    _seed_owner_permission_contract(factory, live=True)

    with pytest.raises(HTTPException) as exc:
        api.set_self_service_permission(
            1,
            KEY,
            api.EmployeeBuilderPermissionRequest(mode="automatic"),
            current_user=USER,
        )

    assert exc.value.status_code == 409
    assert exc.value.detail["requires_deactivation"] is True

    with factory() as db:
        config = db.query(AgentConfig).filter_by(agent_id=1).one()
        builder = config.settings["employee_builder"]
        assert builder.get("owner_permissions") == {}
        action = reveal_config(_assignment(db).config)["actions"][KEY]
        assert action["confirmation_required"] is True


def test_paused_employee_owner_grant_becomes_authoritative_runtime_policy(database, monkeypatch):
    factory, _ = database
    monkeypatch.setattr(api, "self_service_readiness", lambda *args, **kwargs: {})
    _seed_owner_permission_contract(factory, live=False)

    result = api.set_self_service_permission(
        1,
        KEY,
        api.EmployeeBuilderPermissionRequest(mode="automatic"),
        current_user=USER,
    )

    assert result["status"] == "saved"
    assert result["mode"] == "automatic"
    permission = next(
        item
        for item in result["compiled_spec"]["permissions"]
        if item["action"] == KEY
    )
    assert permission["mode"] == "automatic"
    assert permission["source"] == "owner"

    with factory() as db:
        config = db.query(AgentConfig).filter_by(agent_id=1).one()
        builder = config.settings["employee_builder"]
        assert builder["owner_permissions"][KEY] == "automatic"
        assert "last_tested_at" not in builder
        assert "last_tested_compiled_at" not in builder
        action = reveal_config(_assignment(db).config)["actions"][KEY]
        assert action["confirmation_required"] is False
        assert action["_xvond_permission_mode"] == "automatic"



def test_multi_routine_provisioning_keeps_same_trigger_routines_independent(database):
    factory, _ = database
    spec = {
        "role": "Multi-routine employee",
        "scope": "personal",
        "requirements": [],
        "permissions": [],
        "execution_routines": [
            {
                "id": "morning_summary",
                "name": "Morning summary",
                "graph": {
                    "version": 1,
                    "trigger": {
                        "type": "schedule",
                        "schedule": {
                            "kind": "daily",
                            "hour": 8,
                            "minute": 0,
                            "timezone": "UTC",
                        },
                    },
                    "nodes": [
                        {
                            "id": "morning_done",
                            "type": "notify",
                            "depends_on": [],
                            "params": {"message": "Morning routine finished."},
                        }
                    ],
                },
            },
            {
                "id": "evening_summary",
                "name": "Evening summary",
                "graph": {
                    "version": 1,
                    "trigger": {
                        "type": "schedule",
                        "schedule": {
                            "kind": "daily",
                            "hour": 18,
                            "minute": 0,
                            "timezone": "UTC",
                        },
                    },
                    "nodes": [
                        {
                            "id": "evening_done",
                            "type": "notify",
                            "depends_on": [],
                            "params": {"message": "Evening routine finished."},
                        }
                    ],
                },
            },
        ],
    }

    with factory() as db:
        company = db.get(Company, 1)
        company.onboarding_source = "self_service"
        prepared, delivery = provision_compiled_capabilities(
            db,
            agent_id=1,
            spec=spec,
        )
        db.commit()

        triggers = delivery["graph_triggers"]
        assert [item["routine_id"] for item in triggers] == [
            "morning_summary",
            "evening_summary",
        ]
        assert all(item["status"] == "ready" for item in triggers)
        assert delivery["graph_trigger"]["routine_id"] == "morning_summary"
        assert prepared["delivery"]["provisioning_version"] == 1

        workflows = (
            db.query(AutomationWorkflow)
            .filter(AutomationWorkflow.company_id == 1)
            .order_by(AutomationWorkflow.id.asc())
            .all()
        )
        assert len(workflows) == 2
        assert [row.trigger_config["_xvond_routine_id"] for row in workflows] == [
            "morning_summary",
            "evening_summary",
        ]
        assert workflows[0].trigger_config["schedule"]["hour"] == 8
        assert workflows[1].trigger_config["schedule"]["hour"] == 18
        assert workflows[0].steps[0]["graph"]["nodes"][0]["id"] == "morning_done"
        assert workflows[1].steps[0]["graph"]["nodes"][0]["id"] == "evening_done"


def test_legacy_execution_graph_still_provisions_as_primary_routine(database):
    factory, _ = database
    spec = {
        "role": "Legacy employee",
        "scope": "personal",
        "requirements": [],
        "permissions": [],
        "execution_graph": {
            "version": 1,
            "trigger": {"type": "manual"},
            "nodes": [
                {
                    "id": "done",
                    "type": "notify",
                    "depends_on": [],
                    "params": {"message": "Done."},
                }
            ],
        },
    }

    with factory() as db:
        company = db.get(Company, 1)
        company.onboarding_source = "self_service"
        prepared, delivery = provision_compiled_capabilities(
            db,
            agent_id=1,
            spec=spec,
        )
        db.commit()

        assert len(delivery["graph_triggers"]) == 1
        assert delivery["graph_triggers"][0]["routine_id"] == "primary"
        assert delivery["graph_trigger"]["routine_id"] == "primary"
        assert delivery["graph_trigger"]["trigger_type"] == "manual"
        workflow = db.get(
            AutomationWorkflow,
            delivery["graph_trigger"]["workflow_id"],
        )
        assert workflow.trigger_config["_xvond_routine_id"] == "primary"
        assert prepared["execution_graph"]["nodes"][0]["id"] == "done"



def test_manual_run_requires_routine_id_when_employee_has_multiple_manual_routines(
    database,
    monkeypatch,
):
    factory, _ = database
    with factory() as db:
        company = db.get(Company, 1)
        company.onboarding_source = "self_service"
        agent = db.get(AIAgent, 1)
        agent.enabled = True
        db.add_all(
            [
                AutomationWorkflow(
                    id=101,
                    company_id=1,
                    name="First manual routine",
                    trigger_type="manual",
                    trigger_config={
                        "_xvond_source": "self_service_employee",
                        "_xvond_agent_id": 1,
                        "_xvond_graph_trigger": True,
                        "_xvond_routine_id": "first",
                        "_xvond_routine_name": "First",
                    },
                    steps=[{"type": "graph", "agent_id": 1, "graph": {"version": 1, "nodes": []}}],
                    enabled=True,
                ),
                AutomationWorkflow(
                    id=102,
                    company_id=1,
                    name="Second manual routine",
                    trigger_type="manual",
                    trigger_config={
                        "_xvond_source": "self_service_employee",
                        "_xvond_agent_id": 1,
                        "_xvond_graph_trigger": True,
                        "_xvond_routine_id": "second",
                        "_xvond_routine_name": "Second",
                    },
                    steps=[{"type": "graph", "agent_id": 1, "graph": {"version": 1, "nodes": []}}],
                    enabled=True,
                ),
            ]
        )
        db.commit()

    executed = []

    def fake_execute(*, db, company_id, workflow, input_data):
        executed.append(workflow.id)
        return SimpleNamespace(
            id=900 + workflow.id,
            workflow_id=workflow.id,
            status="success",
            output_data={"ok": True},
            error_message=None,
            created_at=None,
            finished_at=None,
        )

    monkeypatch.setattr(api.automation_runtime, "execute", fake_execute)

    with pytest.raises(HTTPException) as exc:
        api.customer_employee_run_graph(
            1,
            api.EmployeeBuilderGraphRunRequest(input_data={}),
            USER,
        )
    assert exc.value.status_code == 409
    assert "multiple manual routines" in str(exc.value.detail)

    result = api.customer_employee_run_graph(
        1,
        api.EmployeeBuilderGraphRunRequest(
            input_data={"source": "owner"},
            routine_id="second",
        ),
        USER,
    )

    assert executed == [102]
    assert result["workflow_id"] == 102
    assert result["routine_id"] == "second"
    assert result["routine_name"] == "Second"


def test_webhook_config_selects_requested_routine(database, monkeypatch):
    factory, _ = database
    with factory() as db:
        company = db.get(Company, 1)
        company.onboarding_source = "self_service"
        config = db.query(AgentConfig).filter_by(agent_id=1).one()
        settings_value = deepcopy(config.settings)
        builder = dict(settings_value.get("employee_builder") or {})
        builder["compiled_spec"] = {
            "role": "Webhook employee",
            "requirements": [],
            "delivery": {
                "provisioning_version": 1,
                "graph_trigger": {
                    "routine_id": "alpha",
                    "routine_name": "Alpha",
                    "status": "ready",
                    "workflow_id": 201,
                    "trigger_type": "webhook",
                },
                "graph_triggers": [
                    {
                        "routine_id": "alpha",
                        "routine_name": "Alpha",
                        "status": "ready",
                        "workflow_id": 201,
                        "trigger_type": "webhook",
                    },
                    {
                        "routine_id": "beta",
                        "routine_name": "Beta",
                        "status": "ready",
                        "workflow_id": 202,
                        "trigger_type": "webhook",
                    },
                ],
            },
        }
        settings_value["employee_builder"] = builder
        config.settings = settings_value
        db.add_all(
            [
                AutomationWorkflow(
                    id=201,
                    company_id=1,
                    name="Alpha",
                    trigger_type="webhook",
                    trigger_config={
                        "_xvond_source": "self_service_employee",
                        "_xvond_agent_id": 1,
                        "_xvond_graph_trigger": True,
                        "_xvond_routine_id": "alpha",
                    },
                    steps=[],
                    enabled=True,
                ),
                AutomationWorkflow(
                    id=202,
                    company_id=1,
                    name="Beta",
                    trigger_type="webhook",
                    trigger_config={
                        "_xvond_source": "self_service_employee",
                        "_xvond_agent_id": 1,
                        "_xvond_graph_trigger": True,
                        "_xvond_routine_id": "beta",
                    },
                    steps=[],
                    enabled=True,
                ),
            ]
        )
        db.commit()

    monkeypatch.setattr(api.settings, "PUBLIC_BASE_URL", "https://xvond.test")
    monkeypatch.setattr(
        api,
        "automation_webhook_key",
        lambda *, workflow_id, company_id: f"key-{company_id}-{workflow_id}",
    )

    with pytest.raises(HTTPException) as exc:
        api.customer_employee_webhook(1, USER)
    assert exc.value.status_code == 409
    assert "multiple webhook routines" in str(exc.value.detail)

    result = api.customer_employee_webhook(1, USER, "beta")

    assert result["workflow_id"] == 202
    assert result["routine_id"] == "beta"
    assert result["routine_name"] == "Beta"
    assert result["url"] == "https://xvond.test/webhooks/automation/202"
    assert result["key"] == "key-1-202"



def test_customer_can_pause_and_resume_one_employee_routine(database):
    factory, _ = database
    with factory() as db:
        company = db.get(Company, 1)
        company.onboarding_source = "self_service"
        config = db.query(AgentConfig).filter_by(agent_id=1).one()
        settings_value = deepcopy(config.settings)
        builder = dict(settings_value.get("employee_builder") or {})
        builder["compiled_spec"] = {
            "role": "Routine worker",
            "requirements": [],
            "delivery": {
                "provisioning_version": 1,
                "graph_trigger": {
                    "routine_id": "morning",
                    "routine_name": "Morning",
                    "status": "ready",
                    "workflow_id": 301,
                    "trigger_type": "schedule",
                },
                "graph_triggers": [
                    {
                        "routine_id": "morning",
                        "routine_name": "Morning",
                        "status": "ready",
                        "workflow_id": 301,
                        "trigger_type": "schedule",
                    },
                    {
                        "routine_id": "events",
                        "routine_name": "Events",
                        "status": "ready",
                        "workflow_id": 302,
                        "trigger_type": "event",
                    },
                ],
            },
        }
        builder["delivery"] = deepcopy(builder["compiled_spec"]["delivery"])
        settings_value["employee_builder"] = builder
        config.settings = settings_value
        db.add_all(
            [
                AutomationWorkflow(
                    id=301,
                    company_id=1,
                    name="Morning",
                    trigger_type="schedule",
                    trigger_config={
                        "_xvond_source": "self_service_employee",
                        "_xvond_agent_id": 1,
                        "_xvond_graph_trigger": True,
                        "_xvond_routine_id": "morning",
                        "_xvond_routine_name": "Morning",
                        "schedule": {
                            "kind": "daily",
                            "hour": 8,
                            "minute": 0,
                            "timezone": "UTC",
                        },
                    },
                    steps=[],
                    enabled=True,
                ),
                AutomationWorkflow(
                    id=302,
                    company_id=1,
                    name="Events",
                    trigger_type="event",
                    trigger_config={
                        "_xvond_source": "self_service_employee",
                        "_xvond_agent_id": 1,
                        "_xvond_graph_trigger": True,
                        "_xvond_routine_id": "events",
                        "_xvond_routine_name": "Events",
                        "event_name": "lead.created",
                    },
                    steps=[],
                    enabled=True,
                ),
            ]
        )
        db.commit()

    paused = api.customer_employee_set_routine_state(
        1,
        "morning",
        api.EmployeeBuilderRoutineStateRequest(enabled=False),
        USER,
    )
    assert paused["status"] == "paused"
    assert paused["enabled"] is False

    with factory() as db:
        workflow = db.get(AutomationWorkflow, 301)
        assert workflow.enabled is False
        assert workflow.trigger_config["_xvond_paused_at"]
        config = db.query(AgentConfig).filter_by(agent_id=1).one()
        delivery = config.settings["employee_builder"]["compiled_spec"]["delivery"]
        assert delivery["graph_trigger"]["status"] == "disabled"
        statuses = {
            item["routine_id"]: item["status"]
            for item in delivery["graph_triggers"]
        }
        assert statuses == {"morning": "disabled", "events": "ready"}
        assert config.settings["employee_builder"]["delivery"]["graph_trigger"]["status"] == "disabled"

    resumed = api.customer_employee_set_routine_state(
        1,
        "morning",
        api.EmployeeBuilderRoutineStateRequest(enabled=True),
        USER,
    )
    assert resumed["status"] == "resumed"
    assert resumed["enabled"] is True

    with factory() as db:
        workflow = db.get(AutomationWorkflow, 301)
        assert workflow.enabled is True
        assert "_xvond_paused_at" not in workflow.trigger_config
        assert workflow.trigger_config["_xvond_resumed_at"]
        config = db.query(AgentConfig).filter_by(agent_id=1).one()
        delivery = config.settings["employee_builder"]["compiled_spec"]["delivery"]
        assert delivery["graph_trigger"]["status"] == "ready"
        statuses = {
            item["routine_id"]: item["status"]
            for item in delivery["graph_triggers"]
        }
        assert statuses == {"morning": "ready", "events": "ready"}


def test_customer_routine_list_is_scoped_to_current_employee(database):
    factory, _ = database
    with factory() as db:
        company = db.get(Company, 1)
        company.onboarding_source = "self_service"
        db.add(
            AutomationWorkflow(
                id=401,
                company_id=1,
                name="Primary",
                trigger_type="manual",
                trigger_config={
                    "_xvond_source": "self_service_employee",
                    "_xvond_agent_id": 1,
                    "_xvond_graph_trigger": True,
                    "_xvond_routine_id": "primary",
                    "_xvond_routine_name": "Primary",
                },
                steps=[],
                enabled=False,
            )
        )
        db.add(
            AutomationWorkflow(
                id=402,
                company_id=1,
                name="Unrelated",
                trigger_type="manual",
                trigger_config={
                    "_xvond_source": "other_source",
                    "_xvond_agent_id": 1,
                    "_xvond_graph_trigger": True,
                },
                steps=[],
                enabled=True,
            )
        )
        db.commit()

    result = api.customer_employee_routines(1, USER)

    assert len(result["routines"]) == 1
    assert result["routines"][0]["routine_id"] == "primary"
    assert result["routines"][0]["enabled"] is False



def test_multi_routine_runtime_inputs_are_isolated_by_requirement_scope(database):
    factory, _ = database
    spec = {
        "version": 10,
        "role": "Scoped monitor",
        "scope": "personal",
        "requirements": [
            {
                "key": "morning_source",
                "kind": "custom",
                "status": "available",
                "runtime_inputs": {
                    "target": "morning-only",
                    "morning_secret_name": "morning",
                },
            },
            {
                "key": "evening_source",
                "kind": "custom",
                "status": "available",
                "runtime_inputs": {
                    "target": "evening-only",
                    "evening_secret_name": "evening",
                },
            },
        ],
        "permissions": [],
        "execution_routines": [
            {
                "id": "morning",
                "name": "Morning routine",
                "requirement_keys": ["morning_source"],
                "graph": {
                    "version": 1,
                    "trigger": {
                        "type": "schedule",
                        "schedule": {
                            "kind": "daily",
                            "hour": 8,
                            "minute": 0,
                            "timezone": "UTC",
                        },
                    },
                    "nodes": [
                        {
                            "id": "done",
                            "type": "notify",
                            "depends_on": [],
                            "params": {"message": "Morning complete"},
                        }
                    ],
                },
            },
            {
                "id": "evening",
                "name": "Evening routine",
                "requirement_keys": ["evening_source"],
                "graph": {
                    "version": 1,
                    "trigger": {
                        "type": "schedule",
                        "schedule": {
                            "kind": "daily",
                            "hour": 18,
                            "minute": 0,
                            "timezone": "UTC",
                        },
                    },
                    "nodes": [
                        {
                            "id": "done",
                            "type": "notify",
                            "depends_on": [],
                            "params": {"message": "Evening complete"},
                        }
                    ],
                },
            },
        ],
    }

    with factory() as db:
        company = db.get(Company, 1)
        company.onboarding_source = "self_service"
        prepared, delivery = provision_compiled_capabilities(
            db,
            agent_id=1,
            spec=spec,
        )
        db.commit()

        rows = (
            db.query(AutomationWorkflow)
            .filter(
                AutomationWorkflow.company_id == 1,
                AutomationWorkflow.trigger_type == "schedule",
            )
            .order_by(AutomationWorkflow.id.asc())
            .all()
        )

        assert len(rows) == 2
        by_routine = {
            row.trigger_config["_xvond_routine_id"]: row
            for row in rows
        }
        assert by_routine["morning"].trigger_config["_xvond_requirement_keys"] == [
            "morning_source"
        ]
        assert by_routine["evening"].trigger_config["_xvond_requirement_keys"] == [
            "evening_source"
        ]
        assert by_routine["morning"].trigger_config["input_data"] == {
            "target": "morning-only",
            "morning_secret_name": "morning",
        }
        assert by_routine["morning"].trigger_config["_xvond_runtime_inputs"] == {
            "target": "morning-only",
            "morning_secret_name": "morning",
        }
        assert by_routine["evening"].trigger_config["input_data"] == {
            "target": "evening-only",
            "evening_secret_name": "evening",
        }
        assert by_routine["evening"].trigger_config["_xvond_runtime_inputs"] == {
            "target": "evening-only",
            "evening_secret_name": "evening",
        }
        assert delivery["graph_triggers"][0]["requirement_keys"] == [
            "morning_source"
        ]
        assert delivery["graph_triggers"][1]["requirement_keys"] == [
            "evening_source"
        ]
        assert prepared["execution_routines"][0]["requirement_keys"] == [
            "morning_source"
        ]


def test_pre_v10_routine_without_scope_keeps_shared_runtime_inputs(database):
    factory, _ = database
    spec = {
        "version": 9,
        "role": "Legacy scoped employee",
        "scope": "personal",
        "requirements": [
            {
                "key": "legacy_one",
                "kind": "custom",
                "status": "available",
                "runtime_inputs": {"one": 1},
            },
            {
                "key": "legacy_two",
                "kind": "custom",
                "status": "available",
                "runtime_inputs": {"two": 2},
            },
        ],
        "permissions": [],
        "execution_routines": [
            {
                "id": "legacy",
                "name": "Legacy",
                "graph": {
                    "version": 1,
                    "trigger": {
                        "type": "schedule",
                        "schedule": {
                            "kind": "daily",
                            "hour": 9,
                            "minute": 0,
                            "timezone": "UTC",
                        },
                    },
                    "nodes": [
                        {
                            "id": "done",
                            "type": "notify",
                            "depends_on": [],
                            "params": {"message": "Done"},
                        }
                    ],
                },
            }
        ],
    }

    with factory() as db:
        company = db.get(Company, 1)
        company.onboarding_source = "self_service"
        _, delivery = provision_compiled_capabilities(
            db,
            agent_id=1,
            spec=spec,
        )
        db.commit()

        workflow = db.get(
            AutomationWorkflow,
            delivery["graph_triggers"][0]["workflow_id"],
        )
        assert workflow.trigger_config["input_data"] == {"one": 1, "two": 2}



def test_v10_explicit_empty_routine_scope_does_not_inherit_shared_inputs(database):
    factory, _ = database
    spec = {
        "version": 10,
        "role": "Explicitly unscoped routine",
        "scope": "personal",
        "requirements": [
            {
                "key": "other_requirement",
                "kind": "custom",
                "status": "available",
                "runtime_inputs": {"should_not_leak": "secret-value"},
            }
        ],
        "permissions": [],
        "execution_routines": [
            {
                "id": "manual_only",
                "name": "Manual only",
                "requirement_keys": [],
                "graph": {
                    "version": 1,
                    "trigger": {
                        "type": "schedule",
                        "schedule": {
                            "kind": "daily",
                            "hour": 10,
                            "minute": 0,
                            "timezone": "UTC",
                        },
                    },
                    "nodes": [
                        {
                            "id": "done",
                            "type": "notify",
                            "depends_on": [],
                            "params": {"message": "Done"},
                        }
                    ],
                },
            }
        ],
    }

    with factory() as db:
        company = db.get(Company, 1)
        company.onboarding_source = "self_service"
        _, delivery = provision_compiled_capabilities(
            db,
            agent_id=1,
            spec=spec,
        )
        db.commit()

        workflow = db.get(
            AutomationWorkflow,
            delivery["graph_triggers"][0]["workflow_id"],
        )
        assert workflow.trigger_config["input_data"] == {}
        assert workflow.trigger_config["_xvond_runtime_inputs"] == {}
        assert workflow.trigger_config["_xvond_requirement_keys"] == []



def test_routine_runtime_input_conflict_blocks_workflow_provisioning(database):
    factory, _ = database
    spec = {
        "version": 10,
        "role": "Conflicted routine",
        "scope": "personal",
        "requirements": [
            {
                "key": "source_one",
                "kind": "custom",
                "status": "available",
                "runtime_inputs": {"target": "A"},
            },
            {
                "key": "source_two",
                "kind": "custom",
                "status": "available",
                "runtime_inputs": {"target": "B"},
            },
        ],
        "permissions": [],
        "execution_routines": [
            {
                "id": "conflicted",
                "name": "Conflicted routine",
                "requirement_keys": ["source_one", "source_two"],
                "graph": {
                    "version": 1,
                    "trigger": {
                        "type": "schedule",
                        "schedule": {
                            "kind": "daily",
                            "hour": 8,
                            "minute": 0,
                            "timezone": "UTC",
                        },
                    },
                    "nodes": [
                        {
                            "id": "done",
                            "type": "notify",
                            "depends_on": [],
                            "params": {"message": "Done"},
                        }
                    ],
                },
            }
        ],
    }

    with factory() as db:
        company = db.get(Company, 1)
        company.onboarding_source = "self_service"
        _, delivery = provision_compiled_capabilities(
            db,
            agent_id=1,
            spec=spec,
        )
        db.commit()

        trigger = delivery["graph_triggers"][0]
        assert trigger["status"] == "runtime_input_conflict"
        assert trigger["runtime_input_conflicts"] == ["target"]
        assert trigger["workflow_id"] is None
        assert (
            db.query(AutomationWorkflow)
            .filter(AutomationWorkflow.company_id == 1)
            .count()
            == 0
        )



def test_routine_preview_records_build_scoped_evidence_without_live_execution(
    database,
    monkeypatch,
):
    factory, _ = database
    monkeypatch.setattr(api, "_has_ai_agents_entitlement", lambda *args, **kwargs: True)

    spec = normalize_compiled_spec(
        {
            "role": "Background worker",
            "scope": "personal",
            "requirements": [],
            "permissions": [],
            "execution_routines": [
                {
                    "id": "monitor",
                    "name": "Monitor",
                    "requirement_keys": [],
                    "graph": {
                        "version": 1,
                        "trigger": {"type": "manual"},
                        "nodes": [
                            {
                                "id": "done",
                                "type": "notify",
                                "depends_on": [],
                                "params": {"message": "Done"},
                            }
                        ],
                    },
                }
            ],
        },
        job_brief="Run the monitor manually.",
    )

    with factory() as db:
        company = db.get(Company, 1)
        company.onboarding_source = "self_service"
        config = db.query(AgentConfig).filter_by(agent_id=1).one()
        settings = deepcopy(config.settings)
        settings["employee_builder"] = {
            "source_description": "Run the monitor manually.",
            "job_brief": "Run the monitor manually.",
            "compiled_spec": spec,
            "compiled_at": "build-1",
            "requested_channels": [],
            "setup_answers": {},
            "owner_permissions": {},
        }
        config.settings = settings
        db.commit()

    captured = {}

    def fake_preview(db, company_id, step, state, *, run_id, step_index):
        captured.update(
            {
                "company_id": company_id,
                "step": deepcopy(step),
                "state": deepcopy(state),
                "run_id": run_id,
                "step_index": step_index,
            }
        )
        return {
            "graph_outputs": {
                "done": {
                    "preview": True,
                    "notification": {"persisted": False},
                }
            }
        }

    monkeypatch.setattr(api.automation_runtime, "execute_step", fake_preview)

    result = api.preview_employee_routine(
        1,
        api.EmployeeBuilderRoutinePreviewRequest(
            routine_id="monitor",
            input_data={"sample": 123},
        ),
        USER,
    )

    assert result["status"] == "previewed"
    assert result["routine_id"] == "monitor"
    assert result["current_build_tested"] is True
    assert result["safety"]["business_actions_executed"] is False
    assert captured["run_id"] == 0
    assert captured["state"]["_xvond_preview"] is True
    assert captured["state"]["sample"] == 123

    with factory() as db:
        builder = _builder(db)
        assert builder["last_tested_compiled_at"] == "build-1"
        assert builder["routine_preview_evidence"]["monitor"]["compiled_at"] == "build-1"
        assert db.query(AutomationRun).count() == 0
        assert db.query(ActionRequest).count() == 0


def test_chat_preview_does_not_unlock_build_that_has_execution_routines(database, monkeypatch):
    factory, calls = database
    monkeypatch.setattr(api, "_has_ai_agents_entitlement", lambda *args, **kwargs: True)

    spec = normalize_compiled_spec(
        {
            "role": "Background worker",
            "scope": "personal",
            "requirements": [],
            "permissions": [],
            "execution_routines": [
                {
                    "id": "background",
                    "name": "Background",
                    "requirement_keys": [],
                    "graph": {
                        "version": 1,
                        "trigger": {"type": "manual"},
                        "nodes": [
                            {
                                "id": "done",
                                "type": "notify",
                                "depends_on": [],
                                "params": {"message": "Done"},
                            }
                        ],
                    },
                }
            ],
        },
        job_brief="Run background work manually.",
    )
    _cache(factory, spec)

    result = api.test_draft_employee(
        1,
        api.EmployeeBuilderTestRequest(message="What do you do?"),
        USER,
    )

    assert result["tools_used"] is False
    with factory() as db:
        builder = _builder(db)
        assert builder["chat_tested_compiled_at"] == builder["compiled_at"]
        assert builder.get("last_tested_compiled_at") is None
        assert builder.get("routine_preview_evidence") in (None, {})
    assert len(calls) == 1



def test_routine_inventory_exposes_live_operational_state(database):
    factory, _ = database
    with factory() as db:
        company = db.get(Company, 1)
        company.onboarding_source = "self_service"
        company.lifecycle_status = "live"
        company.active = True
        agent = db.get(AIAgent, 1)
        agent.enabled = True

        scheduled = AutomationWorkflow(
            id=501,
            company_id=1,
            name="Scheduled monitor",
            trigger_type="schedule",
            trigger_config={
                "_xvond_source": "self_service_employee",
                "_xvond_agent_id": 1,
                "_xvond_graph_trigger": True,
                "_xvond_routine_id": "monitor",
                "_xvond_routine_name": "Monitor",
                "schedule": {"kind": "interval", "every_minutes": 60},
            },
            steps=[],
            enabled=True,
        )
        event = AutomationWorkflow(
            id=502,
            company_id=1,
            name="Event follow-up",
            trigger_type="event",
            trigger_config={
                "_xvond_source": "self_service_employee",
                "_xvond_agent_id": 1,
                "_xvond_graph_trigger": True,
                "_xvond_routine_id": "follow_up",
                "_xvond_routine_name": "Follow up",
                "event_name": "lead.created",
            },
            steps=[],
            enabled=True,
        )
        failed = AutomationWorkflow(
            id=503,
            company_id=1,
            name="Manual report",
            trigger_type="manual",
            trigger_config={
                "_xvond_source": "self_service_employee",
                "_xvond_agent_id": 1,
                "_xvond_graph_trigger": True,
                "_xvond_routine_id": "report",
                "_xvond_routine_name": "Report",
            },
            steps=[],
            enabled=True,
        )
        paused = AutomationWorkflow(
            id=504,
            company_id=1,
            name="Paused routine",
            trigger_type="manual",
            trigger_config={
                "_xvond_source": "self_service_employee",
                "_xvond_agent_id": 1,
                "_xvond_graph_trigger": True,
                "_xvond_routine_id": "paused",
                "_xvond_routine_name": "Paused",
                "_xvond_paused_at": "2026-09-19T10:00:00Z",
            },
            steps=[],
            enabled=False,
        )
        db.add_all([scheduled, event, failed, paused])
        db.flush()
        db.add_all(
            [
                AutomationRun(
                    company_id=1,
                    workflow_id=501,
                    status="success",
                    input_data={},
                    output_data={},
                    finished_at=datetime(2026, 9, 19, 10, 0),
                ),
                AutomationRun(
                    company_id=1,
                    workflow_id=502,
                    status="waiting_event",
                    input_data={},
                    output_data={},
                    resume_event_name="payment.confirmed",
                ),
                AutomationRun(
                    company_id=1,
                    workflow_id=503,
                    status="failed",
                    input_data={},
                    output_data={},
                    error_message="provider timeout",
                    finished_at=datetime(2026, 9, 19, 10, 5),
                ),
            ]
        )
        db.commit()

    result = api.customer_employee_routines(1, USER)
    routines = {item["routine_id"]: item for item in result["routines"]}

    assert routines["monitor"]["operational_state"] == "healthy"
    assert routines["monitor"]["last_run"]["status"] == "success"
    assert routines["monitor"]["next_scheduled_at"] is not None

    assert routines["follow_up"]["operational_state"] == "waiting_event"
    assert routines["follow_up"]["waiting"] == {
        "type": "event",
        "event_name": "payment.confirmed",
    }

    assert routines["report"]["operational_state"] == "needs_attention"
    assert routines["report"]["last_run"]["error_message"] == "provider timeout"

    assert routines["paused"]["operational_state"] == "paused"
    assert routines["paused"]["next_scheduled_at"] is None



def test_routine_observability_exposes_health_duration_and_failed_step(database):
    factory, _ = database
    with factory() as db:
        company = db.get(Company, 1)
        company.onboarding_source = "self_service"
        company.lifecycle_status = "live"
        company.active = True
        agent = db.get(AIAgent, 1)
        agent.enabled = True

        workflow = AutomationWorkflow(
            id=601,
            company_id=1,
            name="Observed routine",
            trigger_type="manual",
            trigger_config={
                "_xvond_source": "self_service_employee",
                "_xvond_agent_id": 1,
                "_xvond_graph_trigger": True,
                "_xvond_routine_id": "observed",
                "_xvond_routine_name": "Observed",
            },
            steps=[],
            enabled=True,
        )
        db.add(workflow)
        db.flush()
        db.add_all(
            [
                AutomationRun(
                    company_id=1,
                    workflow_id=601,
                    status="success",
                    input_data={},
                    output_data={},
                    created_at=datetime(2026, 9, 19, 9, 0),
                    finished_at=datetime(2026, 9, 19, 9, 0, 2),
                ),
                AutomationRun(
                    company_id=1,
                    workflow_id=601,
                    status="failed",
                    input_data={},
                    output_data={
                        "trace": {
                            "spans": [
                                {
                                    "step_index": 0,
                                    "step_type": "graph",
                                    "phase": "execute",
                                    "status": "failed",
                                    "node_id": "fetch_price",
                                    "duration_ms": 125.5,
                                    "error": "upstream timeout",
                                }
                            ]
                        }
                    },
                    error_message="upstream timeout",
                    created_at=datetime(2026, 9, 19, 10, 0),
                    finished_at=datetime(2026, 9, 19, 10, 0, 3),
                ),
                AutomationRun(
                    company_id=1,
                    workflow_id=601,
                    status="failed",
                    input_data={},
                    output_data={
                        "trace": {
                            "spans": [
                                {
                                    "step_index": 1,
                                    "step_type": "graph",
                                    "phase": "resume",
                                    "status": "failed",
                                    "node_id": None,
                                    "duration_ms": 87.25,
                                    "error": "Execution graph node notify_owner (notify) failed: notification provider unavailable",
                                }
                            ]
                        }
                    },
                    error_message="Execution graph node notify_owner (notify) failed: notification provider unavailable",
                    created_at=datetime(2026, 9, 19, 11, 0),
                    finished_at=datetime(2026, 9, 19, 11, 0, 4),
                ),
            ]
        )
        db.commit()

    result = api.customer_employee_routines(1, USER)
    observed = next(
        item for item in result["routines"]
        if item["routine_id"] == "observed"
    )

    assert observed["operational_state"] == "needs_attention"
    assert observed["last_run"]["status"] == "failed"
    assert observed["last_run"]["duration_ms"] == 4000
    assert observed["health"] == {
        "window_size": 3,
        "success_count": 1,
        "failure_count": 2,
        "rejected_count": 0,
        "success_rate_percent": 33.3,
        "consecutive_failures": 2,
        "last_success_at": "2026-09-19T09:00:02Z",
        "last_failure_at": "2026-09-19T11:00:04Z",
    }
    assert observed["failure"] == {
        "step_index": 1,
        "step_type": "graph",
        "node_id": "notify_owner",
        "phase": "resume",
        "error": "Execution graph node notify_owner (notify) failed: notification provider unavailable",
        "duration_ms": 87.25,
    }
    assert observed["retry"]["safe"] is False
    assert "no durable node checkpoint" in observed["retry"]["reason"]

def test_retry_state_exposes_safe_durable_checkpoint():
    run = SimpleNamespace(
        id=77,
        status="failed",
        output_data={
            "retry_attempts": 2,
            "retry_checkpoint": {
                "safe": True,
                "reason": None,
                "failed_node_id": "fetch_price",
                "failed_node_type": "http_get_json",
                "failed_node_scope": "daily/fetch_price",
            },
        },
    )

    retry = api._run_retry_state(run)

    assert retry == {
        "safe": True,
        "run_id": 77,
        "reason": (
            "Retry will resume from the last durable node checkpoint without "
            "replaying completed nodes."
        ),
        "node_id": "fetch_price",
        "node_type": "http_get_json",
        "node_scope": "daily/fetch_price",
        "attempts": 2,
    }


def test_customer_retry_requires_latest_failed_safe_run(database, monkeypatch):
    factory, _ = database
    captured = {}

    with factory() as db:
        company = db.get(Company, 1)
        company.onboarding_source = "self_service"
        company.lifecycle_status = "live"
        company.active = True
        agent = db.get(AIAgent, 1)
        agent.enabled = True

        workflow = AutomationWorkflow(
            id=701,
            company_id=1,
            name="Retryable routine",
            trigger_type="manual",
            trigger_config={
                "_xvond_source": "self_service_employee",
                "_xvond_agent_id": 1,
                "_xvond_graph_trigger": True,
                "_xvond_routine_id": "retryable",
                "_xvond_routine_name": "Retryable",
            },
            steps=[
                {
                    "type": "graph",
                    "agent_id": 1,
                    "graph": {
                        "version": 1,
                        "nodes": [
                            {
                                "id": "fetch",
                                "type": "http_get_json",
                                "depends_on": [],
                                "params": {"url": "https://example.com/data"},
                            }
                        ],
                    },
                }
            ],
            enabled=True,
        )
        db.add(workflow)
        db.flush()
        failed = AutomationRun(
            company_id=1,
            workflow_id=workflow.id,
            status="failed",
            input_data={"_xvond_execution_key": "api-retry"},
            output_data={
                "retry_checkpoint": {
                    "version": 1,
                    "safe": True,
                    "reason": None,
                    "failed_node_id": "fetch",
                    "failed_node_type": "http_get_json",
                    "failed_node_scope": "fetch",
                }
            },
            error_message="temporary failure",
            finished_at=datetime(2026, 9, 19, 12, 0),
        )
        db.add(failed)
        db.commit()
        failed_id = failed.id

    def fake_retry(db, *, company_id, workflow, run):
        captured.update(
            {
                "company_id": company_id,
                "workflow_id": workflow.id,
                "run_id": run.id,
            }
        )
        run.status = "success"
        run.error_message = None
        run.finished_at = datetime(2026, 9, 19, 12, 1)
        run.output_data = {
            **dict(run.output_data or {}),
            "retry_attempts": 1,
            "retry": {"status": "succeeded", "attempt": 1},
        }
        db.commit()
        db.refresh(run)
        return run

    monkeypatch.setattr(api.automation_runtime, "retry_failed", fake_retry)

    result = api.customer_employee_retry_routine(
        1,
        "retryable",
        api.EmployeeBuilderRoutineRetryRequest(run_id=failed_id),
        USER,
    )

    assert captured == {
        "company_id": 1,
        "workflow_id": 701,
        "run_id": failed_id,
    }
    assert result["status"] == "success"
    assert result["run_id"] == failed_id

    with factory() as db:
        newer = AutomationRun(
            company_id=1,
            workflow_id=701,
            status="failed",
            input_data={},
            output_data={},
            error_message="newer failure",
        )
        db.add(newer)
        db.commit()

    with pytest.raises(HTTPException) as exc:
        api.customer_employee_retry_routine(
            1,
            "retryable",
            api.EmployeeBuilderRoutineRetryRequest(run_id=failed_id),
            USER,
        )

    assert exc.value.status_code == 409
    assert exc.value.detail["message"] == "A newer routine run exists. Refresh before retrying."

def test_routine_observability_marks_automatic_retry_as_recovering(database):
    factory, _ = database
    with factory() as db:
        company = db.get(Company, 1)
        company.onboarding_source = "self_service"
        company.lifecycle_status = "live"
        company.active = True
        agent = db.get(AIAgent, 1)
        agent.enabled = True

        workflow = AutomationWorkflow(
            id=801,
            company_id=1,
            name="Recovering routine",
            trigger_type="schedule",
            trigger_config={
                "_xvond_source": "self_service_employee",
                "_xvond_agent_id": 1,
                "_xvond_graph_trigger": True,
                "_xvond_routine_id": "recovering",
                "_xvond_routine_name": "Recovering",
                "schedule": {"kind": "interval", "every_minutes": 5},
            },
            steps=[],
            enabled=True,
        )
        db.add(workflow)
        db.flush()
        run = AutomationRun(
            company_id=1,
            workflow_id=workflow.id,
            status="waiting_retry",
            input_data={"_xvond_execution_key": "recovering-run"},
            output_data={
                "trace": {
                    "spans": [
                        {
                            "step_index": 0,
                            "step_type": "graph",
                            "phase": "execute",
                            "status": "failed",
                            "node_id": "fetch_price",
                            "duration_ms": 45.0,
                            "error": "temporary upstream failure",
                        }
                    ]
                },
                "retry": {
                    "status": "scheduled",
                    "attempt": 1,
                    "max_attempts": 2,
                },
            },
            error_message="temporary upstream failure",
            resume_at=datetime(2026, 9, 19, 13, 0),
        )
        db.add(run)
        db.commit()

    result = api.customer_employee_routines(1, USER)
    recovering = next(
        item for item in result["routines"]
        if item["routine_id"] == "recovering"
    )

    assert recovering["operational_state"] == "recovering"
    assert recovering["last_run"]["status"] == "waiting_retry"
    assert recovering["waiting"] == {
        "type": "retry",
        "resume_at": "2026-09-19T13:00:00Z",
        "attempt": 1,
        "max_attempts": 2,
        "last_error": "temporary upstream failure",
    }
    assert recovering["failure"]["node_id"] == "fetch_price"
    assert recovering["failure"]["error"] == "temporary upstream failure"
    assert recovering["retry"] is None

