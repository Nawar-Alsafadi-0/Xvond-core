"""Compile/provision regressions against real persisted employee/action records."""
from copy import deepcopy
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
from backend.app.modules.ai_agent.employee_capability_builder import build_managed_action_config
from backend.app.modules.ai_agent.employee_compiler import normalize_compiled_spec
from backend.app.modules.ai_agent.factory_models import AgentConfig
from backend.app.modules.ai_agent.models import AIAgent, AIUsage
from backend.app.modules.automation.models import AutomationWorkflow
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


def _cache(factory, spec):
    with factory() as db:
        config = db.query(AgentConfig).filter_by(agent_id=1).one()
        settings = deepcopy(config.settings)
        settings["employee_builder"].update({"compiled_spec": spec, "compiled_at": "original-time"})
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
    ("automatic", True, False), ("ask_before", True, True), ("never", False, True),
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
    _cache(factory, normalize_compiled_spec(payload, job_brief=BRIEF))
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
    _cache(factory, spec)
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
        assert workflow.steps == [{
            "type": "scheduled_action",
            "agent_id": 1,
            "action_type": KEY,
        }]

    # Cached compilation repairs/reuses the same generated workflow instead of
    # creating duplicates or paying for another compiler call.
    api.compile_employee(1, USER)
    with factory() as db:
        assert db.query(AutomationWorkflow).count() == 1
    assert calls == []


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

    _cache(factory, normalize_compiled_spec(payload, job_brief=brief))
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

    _cache(factory, normalize_compiled_spec(payload, job_brief=brief))
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
        db.add_all([
            AutomationWorkflow(
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
            ),
            AutomationWorkflow(
                company_id=1,
                name="Manual schedule",
                trigger_type="schedule",
                trigger_config={"schedule": {"kind": "interval", "every_minutes": 60}},
                steps=[],
                enabled=False,
            ),
        ])
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

        workflows = db.query(AutomationWorkflow).all()
        assert [item.name for item in workflows] == ["Manual schedule"]


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
