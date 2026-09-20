from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from fastapi import HTTPException

from backend.app.main import app  # noqa: F401 - register model metadata
from backend.app.api.admin_automation import validate_workflow
from backend.app.core.database.base import Base
from backend.app.models.company import Company
from backend.app.modules.ai_agent.models import AIAgent
from backend.app.modules.ai_agent.factory_models import AgentConfig
from backend.app.modules.automation.models import AutomationRun, AutomationWorkflow
from backend.app.modules.customer_ops.models import NotificationEvent
from backend.app.modules.automation.schedule import (
    latest_due_slot,
    next_schedule_slot,
    normalize_schedule_config,
    schedule_slot_key,
)
from backend.app.modules.automation import scheduler
from backend.app.modules.automation import runtime as automation_runtime_module
from backend.app.modules.automation.execution_graph import (
    graph_contract_errors,
    graph_has_side_effect,
)
from backend.app.modules.automation.webhook_auth import (
    automation_webhook_key,
    verify_automation_webhook_key,
)
from backend.app.modules.automation import event_dispatch as event_dispatch_module
from backend.app.modules.tools.models import AgentToolAssignment
from backend.app.modules.tools.business_models import ActionRequest


def test_interval_schedule_uses_latest_slot_without_catchup_storm():
    created = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)
    now = datetime(2026, 9, 18, 12, 17, tzinfo=UTC)
    slot = latest_due_slot(
        {"kind": "interval", "every_minutes": 5},
        now=now,
        created_at=created,
    )
    assert slot == datetime(2026, 9, 18, 12, 15, tzinfo=UTC)


def test_daily_schedule_uses_workspace_timezone():
    schedule = normalize_schedule_config(
        {"kind": "daily", "hour": 8, "minute": 0},
        default_timezone="Asia/Muscat",
    )
    slot = latest_due_slot(
        schedule,
        now=datetime(2026, 9, 18, 4, 5, tzinfo=UTC),
        created_at=datetime(2026, 9, 17, 0, 0, tzinfo=UTC),
    )
    assert schedule["timezone"] == "Asia/Muscat"
    assert slot == datetime(2026, 9, 18, 4, 0, tzinfo=UTC)
    assert schedule_slot_key(slot) == "2026-09-18T04:00:00Z"


def test_weekly_schedule_selects_latest_requested_weekday():
    slot = latest_due_slot(
        {
            "kind": "weekly",
            "weekdays": [0, 4],
            "hour": 9,
            "minute": 30,
            "timezone": "UTC",
        },
        now=datetime(2026, 9, 18, 10, 0, tzinfo=UTC),
        created_at=datetime(2026, 9, 1, 0, 0, tzinfo=UTC),
    )
    assert slot == datetime(2026, 9, 18, 9, 30, tzinfo=UTC)


def test_scheduler_executes_once_per_slot_and_passes_stable_execution_key(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = lambda: Session(engine, autoflush=False)
    monkeypatch.setattr(scheduler, "SessionLocal", factory)

    with factory() as db:
        db.add(
            Company(
                id=1,
                name="Self Service",
                active=True,
                lifecycle_status="live",
                onboarding_source="self_service",
            )
        )
        db.add(
            AIAgent(
                id=1,
                company_id=1,
                name="Monitor",
                system_prompt="monitor",
                provider="mock",
                model="mock",
                enabled=True,
            )
        )
        workflow = AutomationWorkflow(
            id=1,
            company_id=1,
            name="Hourly monitor",
            trigger_type="schedule",
            trigger_config={
                "_xvond_source": "self_service_employee",
                "_xvond_agent_id": 1,
                "_xvond_requirement_key": "price_monitor",
                "schedule": {"kind": "interval", "every_minutes": 60},
                "input_data": {"threshold": 100},
            },
            steps=[{"type": "scheduled_action", "agent_id": 1, "action_type": "price_monitor"}],
            enabled=True,
            created_at=datetime(2026, 9, 18, 10, 0),
        )
        db.add(workflow)
        db.commit()

    captured = []

    def fake_execute(*, db, company_id, workflow, input_data):
        captured.append(dict(input_data))
        run = AutomationRun(
            company_id=company_id,
            workflow_id=workflow.id,
            status="success",
            input_data=dict(input_data),
            output_data={},
            finished_at=datetime(2026, 9, 18, 12, 0),
        )
        db.add(run)
        db.commit()
        db.refresh(run)
        return run

    monkeypatch.setattr(scheduler.automation_runtime, "execute", fake_execute)
    now = datetime(2026, 9, 18, 12, 5, tzinfo=UTC)

    first = scheduler.run_due_workflow(1, now=now)
    second = scheduler.run_due_workflow(1, now=now)

    assert first["status"] == "success"
    assert second["status"] == "already_recorded"
    assert captured[0]["_xvond_schedule_slot"] == "2026-09-18T12:00:00Z"
    assert captured[0]["_xvond_execution_key"] == (
        "automation:1:1:2026-09-18T12:00:00Z"
    )
    engine.dispose()


def test_scheduler_does_not_run_when_generated_employee_is_paused(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = lambda: Session(engine, autoflush=False)
    monkeypatch.setattr(scheduler, "SessionLocal", factory)

    with factory() as db:
        db.add(
            Company(
                id=1,
                name="Self Service",
                active=True,
                lifecycle_status="live",
                onboarding_source="self_service",
            )
        )
        db.add(
            AIAgent(
                id=1,
                company_id=1,
                name="Monitor",
                system_prompt="monitor",
                provider="mock",
                model="mock",
                enabled=False,
            )
        )
        db.add(
            AutomationWorkflow(
                id=1,
                company_id=1,
                name="Monitor",
                trigger_type="schedule",
                trigger_config={
                    "_xvond_source": "self_service_employee",
                    "_xvond_agent_id": 1,
                    "schedule": {"kind": "interval", "every_minutes": 60},
                },
                steps=[],
                enabled=True,
                created_at=datetime(2026, 9, 18, 10, 0),
            )
        )
        db.commit()

    result = scheduler.run_due_workflow(
        1,
        now=datetime(2026, 9, 18, 12, 5, tzinfo=UTC),
    )
    assert result["status"] == "employee_not_live"
    engine.dispose()



def test_schedule_validation_allows_only_idempotent_execution_path():
    trigger = validate_workflow(
        "schedule",
        [{"type": "scheduled_action", "agent_id": 1, "action_type": "monitor"}],
        {"schedule": {"kind": "interval", "every_minutes": 15}},
    )
    assert trigger == "schedule"

    with __import__("pytest").raises(HTTPException) as exc:
        validate_workflow(
            "schedule",
            [{"type": "tool", "agent_id": 1, "tool_name": "action_request"}],
            {"schedule": {"kind": "interval", "every_minutes": 15}},
        )
    assert exc.value.status_code == 400
    assert "not production-safe yet" in str(exc.value.detail)



def test_scheduled_connected_action_uses_stable_execution_key_and_ai_output(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine, autoflush=False) as db:
        db.add(
            Company(
                id=1,
                name="Self Service",
                active=True,
                lifecycle_status="live",
                onboarding_source="self_service",
            )
        )
        db.add(
            AIAgent(
                id=1,
                company_id=1,
                name="Content employee",
                system_prompt="Create social content.",
                provider="mock",
                model="mock",
                enabled=True,
            )
        )
        db.flush()
        db.add(
            AgentToolAssignment(
                agent_id=1,
                tool_name="action_request",
                enabled=True,
                config={
                    "actions": {
                        "instagram_publish": {
                            "enabled": True,
                            "confirmation_required": False,
                            "description": "Publish generated Instagram content",
                            "module": "tools",
                            "destination": {
                                "type": "integration",
                                "integration_id": 44,
                                "operations": {
                                    "execute": {"method": "POST", "endpoint": "/publish"}
                                },
                            },
                            "availability": {"mode": "none"},
                        }
                    }
                },
            )
        )
        db.commit()

        captured = {}

        def fake_integration_call(
            db_arg,
            context,
            action_type,
            action,
            payload,
            operation,
            *,
            idempotency_key=None,
        ):
            captured.update(
                {
                    "context": context,
                    "action_type": action_type,
                    "payload": payload,
                    "operation": operation,
                    "idempotency_key": idempotency_key,
                }
            )
            return SimpleNamespace(success=True, data={"published": True}, error=None)

        monkeypatch.setattr(
            automation_runtime_module,
            "_integration_call",
            fake_integration_call,
        )

        result = automation_runtime_module.automation_runtime.execute_step(
            db,
            1,
            {
                "type": "scheduled_action",
                "agent_id": 1,
                "action_type": "instagram_publish",
            },
            {
                "_xvond_execution_key": "automation:1:9:2026-09-19T08:00:00Z",
                "ai_response": "Final generated post",
                "conversation_id": 123,
                "campaign": "launch",
            },
            run_id=9,
            step_index=1,
        )

    assert captured["action_type"] == "instagram_publish"
    assert captured["operation"] == "execute"
    assert captured["idempotency_key"] == (
        "automation:1:9:2026-09-19T08:00:00Z:1"
    )
    assert captured["payload"]["details"]["ai_response"] == "Final generated post"
    assert captured["payload"]["details"]["campaign"] == "launch"
    assert "conversation_id" not in captured["payload"]["details"]
    assert result["scheduled_action_result"]["runtime"] == "connected_integration"
    engine.dispose()



def test_schedule_validation_accepts_general_graph_unit():
    trigger = validate_workflow(
        "schedule",
        [
            {
                "type": "graph",
                "agent_id": 1,
                "graph": {
                    "version": 1,
                    "nodes": [
                        {
                            "id": "draft",
                            "type": "ai",
                            "depends_on": [],
                            "params": {"prompt": "Create content"},
                        },
                        {
                            "id": "publish",
                            "type": "action",
                            "depends_on": ["draft"],
                            "params": {
                                "action_type": "publish",
                                "arguments": {
                                    "body": "$nodes.draft.ai_response"
                                },
                            },
                        },
                    ],
                },
            }
        ],
        {"schedule": {"kind": "interval", "every_minutes": 15}},
    )
    assert trigger == "schedule"


def test_graph_runtime_resolves_node_outputs_into_later_action(monkeypatch):
    captured = {}

    def fake_step(db, company_id, step, state, *, run_id, step_index):
        if step["type"] == "ai":
            return {"ai_response": "Generated result"}
        if step["type"] == "scheduled_action":
            captured["arguments"] = dict(step.get("arguments") or {})
            captured["action_type"] = step.get("action_type")
            return {"done": True}
        raise AssertionError(step["type"])

    runtime = automation_runtime_module.AutomationRuntime()
    original = runtime.execute_step

    def dispatch(db, company_id, step, state, *, run_id, step_index):
        if step.get("type") == "graph":
            return original(
                db,
                company_id,
                step,
                state,
                run_id=run_id,
                step_index=step_index,
            )
        return fake_step(
            db,
            company_id,
            step,
            state,
            run_id=run_id,
            step_index=step_index,
        )

    monkeypatch.setattr(runtime, "execute_step", dispatch)

    result = runtime.execute_step(
        db=object(),
        company_id=1,
        step={
            "type": "graph",
            "agent_id": 77,
            "graph": {
                "version": 1,
                "nodes": [
                    {
                        "id": "draft",
                        "type": "ai",
                        "depends_on": [],
                        "params": {"prompt": "Create the result"},
                    },
                    {
                        "id": "act",
                        "type": "action",
                        "depends_on": ["draft"],
                        "params": {
                            "action_type": "custom_action",
                            "arguments": {
                                "body": "$nodes.draft.ai_response",
                            },
                        },
                    },
                ],
            },
        },
        state={"_xvond_execution_key": "graph-test"},
        run_id=1,
        step_index=0,
    )

    assert captured["action_type"] == "custom_action"
    assert captured["arguments"]["body"] == "Generated result"
    assert result["graph_outputs"]["draft"]["ai_response"] == "Generated result"
    assert result["graph_outputs"]["act"]["done"] is True



def test_graph_runtime_supports_condition_gates(monkeypatch):
    calls = []

    runtime = automation_runtime_module.AutomationRuntime()
    original = runtime.execute_step

    def dispatch(db, company_id, step, state, *, run_id, step_index):
        if step.get("type") == "graph":
            return original(
                db,
                company_id,
                step,
                state,
                run_id=run_id,
                step_index=step_index,
            )
        if step.get("type") == "scheduled_action":
            calls.append(step)
            return {"executed": True}
        raise AssertionError(step.get("type"))

    monkeypatch.setattr(runtime, "execute_step", dispatch)

    result = runtime.execute_step(
        db=object(),
        company_id=1,
        step={
            "type": "graph",
            "agent_id": 1,
            "graph": {
                "version": 1,
                "nodes": [
                    {
                        "id": "check",
                        "type": "condition",
                        "depends_on": [],
                        "params": {
                            "left": "$input.score",
                            "operator": "gte",
                            "right": 80,
                        },
                    },
                    {
                        "id": "act",
                        "type": "action",
                        "depends_on": ["check"],
                        "when": "$nodes.check.matched",
                        "params": {
                            "action_type": "send_report",
                            "arguments": {"score": "$input.score"},
                        },
                    },
                ],
            },
        },
        state={
            "_xvond_execution_key": "graph-condition-test",
            "score": 75,
        },
        run_id=1,
        step_index=0,
    )

    assert result["graph_outputs"]["check"]["matched"] is False
    assert result["graph_outputs"]["act"]["skipped"] is True
    assert calls == []



def test_graph_runtime_supports_bounded_foreach_with_item_references(monkeypatch):
    seen = []

    runtime = automation_runtime_module.AutomationRuntime()
    original = runtime.execute_step

    def dispatch(db, company_id, step, state, *, run_id, step_index):
        if step.get("type") == "graph":
            return original(
                db,
                company_id,
                step,
                state,
                run_id=run_id,
                step_index=step_index,
            )
        if step.get("type") == "scheduled_action":
            seen.append(dict(step.get("arguments") or {}))
            return {"executed": True}
        raise AssertionError(step.get("type"))

    monkeypatch.setattr(runtime, "execute_step", dispatch)

    result = runtime.execute_step(
        db=object(),
        company_id=1,
        step={
            "type": "graph",
            "agent_id": 1,
            "graph": {
                "version": 1,
                "nodes": [
                    {
                        "id": "each",
                        "type": "foreach",
                        "depends_on": [],
                        "params": {
                            "items": "$input.contacts",
                            "graph": {
                                "version": 1,
                                "nodes": [
                                    {
                                        "id": "send",
                                        "type": "action",
                                        "depends_on": [],
                                        "params": {
                                            "action_type": "contact_action",
                                            "arguments": {
                                                "email": "$item.email",
                                                "position": "$index",
                                            },
                                        },
                                    }
                                ],
                            },
                        },
                    }
                ],
            },
        },
        state={
            "_xvond_execution_key": "foreach-test",
            "contacts": [
                {"email": "a@example.com"},
                {"email": "b@example.com"},
            ],
        },
        run_id=1,
        step_index=0,
    )

    assert seen == [
        {"email": "a@example.com", "position": 0},
        {"email": "b@example.com", "position": 1},
    ]
    assert result["graph_outputs"]["each"]["count"] == 2


def test_graph_runtime_rejects_unbounded_foreach():
    runtime = automation_runtime_module.AutomationRuntime()

    try:
        runtime.execute_step(
            db=object(),
            company_id=1,
            step={
                "type": "graph",
                "agent_id": 1,
                "graph": {
                    "version": 1,
                    "nodes": [
                        {
                            "id": "each",
                            "type": "foreach",
                            "depends_on": [],
                            "params": {
                                "items": list(range(101)),
                                "graph": {
                                    "version": 1,
                                    "nodes": [
                                        {
                                            "id": "noop",
                                            "type": "notify",
                                            "depends_on": [],
                                            "params": {"message": "done"},
                                        }
                                    ],
                                },
                            },
                        }
                    ],
                },
            },
            state={"_xvond_execution_key": "foreach-limit"},
            run_id=1,
            step_index=0,
        )
    except ValueError as exc:
        assert "exceeds 100 items" in str(exc)
    else:
        raise AssertionError("foreach over 100 items must fail closed")



def test_webhook_trigger_accepts_general_graph_but_rejects_raw_tool():
    trigger = validate_workflow(
        "webhook",
        [
            {
                "type": "graph",
                "agent_id": 1,
                "graph": {
                    "version": 1,
                    "nodes": [
                        {
                            "id": "notify",
                            "type": "notify",
                            "depends_on": [],
                            "params": {"message": "received"},
                        }
                    ],
                },
            }
        ],
        {},
    )
    assert trigger == "webhook"

    with __import__("pytest").raises(HTTPException) as exc:
        validate_workflow(
            "webhook",
            [{"type": "tool", "agent_id": 1, "tool_name": "action_request"}],
            {},
        )
    assert exc.value.status_code == 400
    assert "not production-safe yet" in str(exc.value.detail)


def test_automation_webhook_key_is_stable_and_scoped(monkeypatch):
    import backend.app.modules.automation.webhook_auth as webhook_auth

    monkeypatch.setattr(
        webhook_auth.settings,
        "JWT_SECRET",
        "unit-test-secret-" * 4,
    )
    first = automation_webhook_key(workflow_id=10, company_id=20)
    second = automation_webhook_key(workflow_id=10, company_id=20)
    other = automation_webhook_key(workflow_id=11, company_id=20)

    assert first == second
    assert first != other
    assert verify_automation_webhook_key(
        first,
        workflow_id=10,
        company_id=20,
    ) is True
    assert verify_automation_webhook_key(
        other,
        workflow_id=10,
        company_id=20,
    ) is False



def test_internal_event_dispatch_runs_matching_graph_once(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = lambda: Session(engine, autoflush=False)
    monkeypatch.setattr(event_dispatch_module, "SessionLocal", factory)

    with factory() as db:
        db.add(
            Company(
                id=1,
                name="Self Service",
                active=True,
                lifecycle_status="live",
                onboarding_source="self_service",
            )
        )
        db.add(
            AutomationWorkflow(
                id=1,
                company_id=1,
                name="Booking follow-up",
                trigger_type="event",
                trigger_config={"event_name": "booking.created"},
                steps=[
                    {
                        "type": "graph",
                        "agent_id": 1,
                        "graph": {
                            "version": 1,
                            "nodes": [
                                {
                                    "id": "notify",
                                    "type": "notify",
                                    "depends_on": [],
                                    "params": {
                                        "message": "$input.customer_name",
                                    },
                                }
                            ],
                        },
                    }
                ],
                enabled=True,
            )
        )
        db.commit()

    captured = []

    def fake_execute(*, db, company_id, workflow, input_data):
        captured.append(dict(input_data))
        run = AutomationRun(
            company_id=company_id,
            workflow_id=workflow.id,
            status="success",
            input_data=dict(input_data),
            output_data={},
            finished_at=datetime(2026, 9, 18, 12, 0),
        )
        db.add(run)
        db.commit()
        db.refresh(run)
        return run

    monkeypatch.setattr(
        event_dispatch_module.automation_runtime,
        "execute",
        fake_execute,
    )

    result = event_dispatch_module.dispatch_automation_event(
        company_id=1,
        event_name="booking.created",
        event_id="evt-123",
        payload={"customer_name": "Nawar"},
    )

    assert result["matched"] == 1
    assert result["runs"][0]["status"] == "success"
    assert captured[0]["customer_name"] == "Nawar"
    assert captured[0]["_xvond_event_name"] == "booking.created"
    assert captured[0]["_xvond_execution_key"].endswith(":evt-123")

    second = event_dispatch_module.dispatch_automation_event(
        company_id=1,
        event_name="booking.created",
        event_id="evt-123",
        payload={"customer_name": "Nawar"},
    )
    assert second["matched"] == 1
    assert second["runs"][0]["status"] == "already_recorded"
    assert len(captured) == 1
    engine.dispose()



def test_graph_runtime_supports_filter_select_and_aggregate():
    runtime = automation_runtime_module.AutomationRuntime()

    result = runtime.execute_step(
        db=object(),
        company_id=1,
        step={
            "type": "graph",
            "agent_id": 1,
            "graph": {
                "version": 1,
                "nodes": [
                    {
                        "id": "qualified",
                        "type": "filter",
                        "depends_on": [],
                        "params": {
                            "items": "$input.leads",
                            "path": "score",
                            "operator": "gte",
                            "value": 80,
                        },
                    },
                    {
                        "id": "public_fields",
                        "type": "select",
                        "depends_on": ["qualified"],
                        "params": {
                            "items": "$nodes.qualified.items",
                            "fields": ["name", "score"],
                        },
                    },
                    {
                        "id": "average_score",
                        "type": "aggregate",
                        "depends_on": ["qualified"],
                        "params": {
                            "items": "$nodes.qualified.items",
                            "operation": "avg",
                            "path": "score",
                        },
                    },
                ],
            },
        },
        state={
            "_xvond_execution_key": "transform-test",
            "leads": [
                {"name": "A", "score": 95, "secret": "x"},
                {"name": "B", "score": 70, "secret": "y"},
                {"name": "C", "score": 85, "secret": "z"},
            ],
        },
        run_id=1,
        step_index=0,
    )

    outputs = result["graph_outputs"]
    assert outputs["qualified"]["count"] == 2
    assert outputs["public_fields"]["items"] == [
        {"name": "A", "score": 95},
        {"name": "C", "score": 85},
    ]
    assert outputs["average_score"]["value"] == 90.0


def test_graph_runtime_filter_handles_missing_fields_without_failing():
    runtime = automation_runtime_module.AutomationRuntime()

    result = runtime.execute_step(
        db=object(),
        company_id=1,
        step={
            "type": "graph",
            "graph": {
                "version": 1,
                "nodes": [
                    {
                        "id": "filtered",
                        "type": "filter",
                        "depends_on": [],
                        "params": {
                            "items": "$input.items",
                            "path": "nested.value",
                            "operator": "eq",
                            "value": "yes",
                        },
                    }
                ],
            },
        },
        state={
            "items": [
                {"nested": {"value": "yes"}},
                {"nested": {}},
                {"other": 1},
            ]
        },
        run_id=1,
        step_index=0,
    )

    assert result["graph_outputs"]["filtered"]["items"] == [
        {"nested": {"value": "yes"}}
    ]



def test_graph_runtime_supports_safe_public_web_fetch(monkeypatch):
    runtime = automation_runtime_module.AutomationRuntime()
    captured = {}

    def fake_request(**kwargs):
        captured.update(kwargs)
        return {
            "status_code": 200,
            "response": "<html><body>Competitor offer</body></html>",
            "truncated": False,
        }

    monkeypatch.setattr(
        automation_runtime_module,
        "safe_http_request",
        fake_request,
    )

    result = runtime.execute_step(
        db=object(),
        company_id=1,
        step={
            "type": "graph",
            "graph": {
                "version": 1,
                "nodes": [
                    {
                        "id": "page",
                        "type": "web_fetch",
                        "depends_on": [],
                        "params": {"url": "https://example.com/offers"},
                    }
                ],
            },
        },
        state={"_xvond_execution_key": "web-fetch-test"},
        run_id=1,
        step_index=0,
    )

    assert captured["method"] == "GET"
    assert captured["url"] == "https://example.com/offers"
    assert captured["max_response_bytes"] == 500_000
    assert result["graph_outputs"]["page"]["content"].endswith("</html>")
    assert result["graph_outputs"]["page"]["truncated"] is False



def test_graph_runtime_persists_reads_and_deletes_agent_state():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine, autoflush=False) as db:
        db.add(
            Company(
                id=1,
                name="State Company",
                active=True,
                lifecycle_status="live",
                onboarding_source="self_service",
            )
        )
        db.add(
            AIAgent(
                id=1,
                company_id=1,
                name="Stateful worker",
                system_prompt="Persist compact operational state.",
                provider="mock",
                model="mock",
                enabled=True,
            )
        )
        db.flush()
        db.add(
            AgentConfig(
                agent_id=1,
                agent_type="employee",
                settings={},
                capabilities={},
                customer_controls={},
            )
        )
        db.commit()

        runtime = automation_runtime_module.AutomationRuntime()

        write_result = runtime.execute_step(
            db=db,
            company_id=1,
            step={
                "type": "graph",
                "agent_id": 1,
                "graph": {
                    "version": 1,
                    "nodes": [
                        {
                            "id": "save",
                            "type": "state_write",
                            "depends_on": [],
                            "params": {
                                "namespace": "monitor",
                                "key": "last_processed_id",
                                "value": "abc-123",
                            },
                        },
                        {
                            "id": "read",
                            "type": "state_read",
                            "depends_on": ["save"],
                            "params": {
                                "namespace": "monitor",
                                "key": "last_processed_id",
                            },
                        },
                    ],
                },
            },
            state={"_xvond_execution_key": "state-write-read"},
            run_id=1,
            step_index=0,
        )

        assert write_result["graph_outputs"]["save"]["written"] is True
        assert write_result["graph_outputs"]["read"]["value"] == "abc-123"

        db.commit()

    with Session(engine, autoflush=False) as db:
        runtime = automation_runtime_module.AutomationRuntime()
        read_result = runtime.execute_step(
            db=db,
            company_id=1,
            step={
                "type": "graph",
                "agent_id": 1,
                "graph": {
                    "version": 1,
                    "nodes": [
                        {
                            "id": "read",
                            "type": "state_read",
                            "depends_on": [],
                            "params": {
                                "namespace": "monitor",
                                "key": "last_processed_id",
                            },
                        },
                        {
                            "id": "delete",
                            "type": "state_delete",
                            "depends_on": ["read"],
                            "params": {
                                "namespace": "monitor",
                                "key": "last_processed_id",
                            },
                        },
                    ],
                },
            },
            state={"_xvond_execution_key": "state-read-delete"},
            run_id=2,
            step_index=0,
        )

        assert read_result["graph_outputs"]["read"]["value"] == "abc-123"
        assert read_result["graph_outputs"]["delete"]["deleted"] is True
        db.commit()

    with Session(engine, autoflush=False) as db:
        config = db.query(AgentConfig).filter_by(agent_id=1).one()
        root = (config.settings or {}).get("_xvond_runtime_state") or {}
        assert "monitor" not in root

    engine.dispose()


def test_agent_state_rejects_large_values():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine, autoflush=False) as db:
        db.add(Company(id=1, name="State Company", active=True))
        db.add(
            AIAgent(
                id=1,
                company_id=1,
                name="Stateful worker",
                system_prompt="State",
                provider="mock",
                model="mock",
                enabled=True,
            )
        )
        db.flush()
        db.add(
            AgentConfig(
                agent_id=1,
                agent_type="employee",
                settings={},
                capabilities={},
                customer_controls={},
            )
        )
        db.commit()

        runtime = automation_runtime_module.AutomationRuntime()
        try:
            runtime.execute_step(
                db=db,
                company_id=1,
                step={
                    "type": "graph",
                    "agent_id": 1,
                    "graph": {
                        "version": 1,
                        "nodes": [
                            {
                                "id": "save",
                                "type": "state_write",
                                "depends_on": [],
                                "params": {
                                    "namespace": "memory",
                                    "key": "too_large",
                                    "value": "x" * 70000,
                                },
                            }
                        ],
                    },
                },
                state={},
                run_id=1,
                step_index=0,
            )
        except ValueError as exc:
            assert "64 KB" in str(exc)
        else:
            raise AssertionError("oversized state value must be rejected")

    engine.dispose()



def test_graph_approval_resumes_same_run_without_replaying_prior_nodes(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    calls = {"fetch": 0, "action": 0}

    monkeypatch.setattr(
        automation_runtime_module.service_limits,
        "record",
        lambda *args, **kwargs: None,
    )

    def fake_request(**kwargs):
        calls["fetch"] += 1
        return {
            "status_code": 200,
            "response": '{"value": 42}',
            "truncated": False,
        }

    def fake_capability(*args, **kwargs):
        calls["action"] += 1
        return {"ok": True, "details": kwargs.get("details") or {}}

    monkeypatch.setattr(
        automation_runtime_module,
        "safe_http_request",
        fake_request,
    )
    monkeypatch.setattr(
        automation_runtime_module,
        "execute_generic_capability",
        fake_capability,
    )

    with Session(engine, autoflush=False) as db:
        db.add(
            Company(
                id=1,
                name="Approval Company",
                active=True,
                lifecycle_status="live",
                onboarding_source="self_service",
            )
        )
        db.add(
            AIAgent(
                id=1,
                company_id=1,
                name="Approval worker",
                system_prompt="Ask before consequential actions.",
                provider="mock",
                model="mock",
                enabled=True,
            )
        )
        db.flush()
        db.add(
            AgentConfig(
                agent_id=1,
                agent_type="employee",
                settings={},
                capabilities={},
                customer_controls={},
            )
        )
        db.add(
            AgentToolAssignment(
                agent_id=1,
                tool_name="action_request",
                enabled=True,
                config={
                    "actions": {
                        "send_report": {
                            "enabled": True,
                            "confirmation_required": True,
                            "label": "Send report",
                            "description": "Send the generated report",
                            "destination": {
                                "type": "xvond_internal",
                                "adapter": "generic_capability",
                                "execution_plan": [
                                    {
                                        "id": "notify",
                                        "op": "notify",
                                        "title": "Report",
                                        "message": "Report sent.",
                                    }
                                ],
                            },
                            "availability": {"mode": "none"},
                        }
                    }
                },
            )
        )
        db.flush()
        workflow = AutomationWorkflow(
            id=1,
            company_id=1,
            name="Approval graph",
            trigger_type="manual",
            trigger_config={
                "_xvond_source": "self_service_employee",
                "_xvond_agent_id": 1,
                "_xvond_graph_trigger": True,
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
                                "params": {
                                    "url": "https://example.com/data",
                                },
                            },
                            {
                                "id": "send",
                                "type": "action",
                                "depends_on": ["fetch"],
                                "params": {
                                    "action_type": "send_report",
                                    "arguments": {
                                        "payload": "$nodes.fetch.result",
                                    },
                                },
                            },
                        ],
                    },
                }
            ],
            enabled=True,
        )
        db.add(workflow)
        db.commit()

        runtime = automation_runtime_module.AutomationRuntime()
        waiting = runtime.execute(
            db=db,
            company_id=1,
            workflow=workflow,
            input_data={},
        )

        assert waiting.status == "waiting_approval"
        assert calls["fetch"] == 1
        assert calls["action"] == 0
        request = db.query(ActionRequest).one()
        assert request.status == "awaiting_confirmation"
        assert waiting.output_data["approval"]["node_id"] == "send"
        assert waiting.output_data["approval"]["request_id"] == request.id

        request.status = "approved"
        db.commit()

        resumed = runtime.resume_approval(
            db,
            company_id=1,
            workflow=workflow,
            run=waiting,
            request=request,
        )

        assert resumed.id == waiting.id
        assert resumed.status == "success"
        assert calls["fetch"] == 1
        assert calls["action"] == 1
        assert (
            resumed.output_data["steps"][-1]["result"]["graph_outputs"]["send"]
            ["scheduled_action_result"]["ok"]
            is True
        )

    engine.dispose()


def test_graph_nested_approval_resumes_each_item_without_replay(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    calls = {"fetch": 0, "actions": []}

    monkeypatch.setattr(
        automation_runtime_module.service_limits,
        "record",
        lambda *args, **kwargs: None,
    )

    def fake_request(**kwargs):
        calls["fetch"] += 1
        return {
            "status_code": 200,
            "response": '{"ready":true}',
            "truncated": False,
        }

    def fake_capability(*args, **kwargs):
        details = kwargs.get("details") or {}
        calls["actions"].append(details.get("id"))
        return {"ok": True, "id": details.get("id")}

    monkeypatch.setattr(
        automation_runtime_module,
        "safe_http_request",
        fake_request,
    )
    monkeypatch.setattr(
        automation_runtime_module,
        "execute_generic_capability",
        fake_capability,
    )

    with Session(engine, autoflush=False) as db:
        db.add(
            Company(
                id=1,
                name="Nested Approval",
                active=True,
                lifecycle_status="live",
                onboarding_source="self_service",
            )
        )
        db.add(
            AIAgent(
                id=1,
                company_id=1,
                name="Nested worker",
                system_prompt="Ask before each send.",
                provider="mock",
                model="mock",
                enabled=True,
            )
        )
        db.flush()
        db.add(
            AgentConfig(
                agent_id=1,
                agent_type="employee",
                settings={},
                capabilities={},
                customer_controls={},
            )
        )
        db.add(
            AgentToolAssignment(
                agent_id=1,
                tool_name="action_request",
                enabled=True,
                config={
                    "actions": {
                        "send_one": {
                            "enabled": True,
                            "confirmation_required": True,
                            "label": "Send one",
                            "destination": {
                                "type": "xvond_internal",
                                "adapter": "generic_capability",
                                "execution_plan": [
                                    {
                                        "id": "notify",
                                        "op": "notify",
                                        "title": "Sent",
                                        "message": "Sent.",
                                    }
                                ],
                            },
                            "availability": {"mode": "none"},
                        }
                    }
                },
            )
        )
        workflow = AutomationWorkflow(
            id=1,
            company_id=1,
            name="Nested approval graph",
            trigger_type="manual",
            trigger_config={
                "_xvond_source": "self_service_employee",
                "_xvond_agent_id": 1,
                "_xvond_graph_trigger": True,
            },
            steps=[
                {
                    "type": "graph",
                    "agent_id": 1,
                    "graph": {
                        "version": 1,
                        "nodes": [
                            {
                                "id": "each",
                                "type": "foreach",
                                "depends_on": [],
                                "params": {
                                    "items": [
                                        {
                                            "id": 1,
                                            "url": "https://example.com/1",
                                        },
                                        {
                                            "id": 2,
                                            "url": "https://example.com/2",
                                        },
                                    ],
                                    "graph": {
                                        "version": 1,
                                        "nodes": [
                                            {
                                                "id": "fetch",
                                                "type": "http_get_json",
                                                "depends_on": [],
                                                "params": {"url": "$item.url"},
                                            },
                                            {
                                                "id": "send",
                                                "type": "action",
                                                "depends_on": ["fetch"],
                                                "params": {
                                                    "action_type": "send_one",
                                                    "arguments": {
                                                        "id": "$item.id",
                                                        "payload": "$nodes.fetch.result",
                                                    },
                                                },
                                            },
                                        ],
                                    },
                                },
                            }
                        ],
                    },
                }
            ],
            enabled=True,
        )
        db.add(workflow)
        db.commit()

        runtime = automation_runtime_module.AutomationRuntime()
        waiting_first = runtime.execute(
            db=db,
            company_id=1,
            workflow=workflow,
            input_data={},
        )

        assert waiting_first.status == "waiting_approval"
        assert calls == {"fetch": 1, "actions": []}
        request_one = (
            db.query(ActionRequest)
            .filter(ActionRequest.status == "awaiting_confirmation")
            .one()
        )
        meta_one = request_one.details["_xvond_automation"]
        assert meta_one["approval_scope"] == "each[0]/send"
        first_foreach_checkpoint = waiting_first.output_data["approval"]["graph_resume"]["foreach"]
        assert first_foreach_checkpoint["loop_index"] == 0
        assert first_foreach_checkpoint["items_fingerprint"]
        assert waiting_first.output_data["approval"]["workflow_fingerprint"]

        request_one.status = "approved"
        db.commit()
        waiting_second = runtime.resume_approval(
            db,
            company_id=1,
            workflow=workflow,
            run=waiting_first,
            request=request_one,
        )

        assert waiting_second.id == waiting_first.id
        assert waiting_second.status == "waiting_approval"
        assert calls == {"fetch": 2, "actions": [1]}
        request_two = (
            db.query(ActionRequest)
            .filter(ActionRequest.status == "awaiting_confirmation")
            .one()
        )
        assert request_two.id != request_one.id
        meta_two = request_two.details["_xvond_automation"]
        assert meta_two["approval_scope"] == "each[1]/send"
        checkpoint = waiting_second.output_data["approval"]["graph_resume"]["foreach"]
        assert checkpoint["loop_index"] == 1
        assert len(checkpoint["completed_results"]) == 1

        request_two.status = "approved"
        db.commit()
        finished = runtime.resume_approval(
            db,
            company_id=1,
            workflow=workflow,
            run=waiting_second,
            request=request_two,
        )

        assert finished.id == waiting_first.id
        assert finished.status == "success"
        assert calls == {"fetch": 2, "actions": [1, 2]}
        each = finished.output_data["steps"][-1]["result"]["graph_outputs"]["each"]
        assert each["count"] == 2
        assert len(each["items"]) == 2

    engine.dispose()



def test_graph_ai_node_consumes_resolved_context_without_special_case(monkeypatch):
    captured = {}

    def fake_chat(**kwargs):
        captured.update(kwargs)
        return {
            "response": {"content": "summary"},
            "conversation_id": 99,
        }

    monkeypatch.setattr(
        automation_runtime_module.agent_runtime,
        "chat",
        fake_chat,
    )

    runtime = automation_runtime_module.AutomationRuntime()
    result = runtime.execute_step(
        db=object(),
        company_id=1,
        step={
            "type": "graph",
            "agent_id": 7,
            "graph": {
                "version": 1,
                "nodes": [
                    {
                        "id": "data",
                        "type": "transform",
                        "depends_on": [],
                        "params": {
                            "values": {
                                "items": [
                                    {"name": "A", "score": 91},
                                    {"name": "B", "score": 84},
                                ]
                            }
                        },
                    },
                    {
                        "id": "summarize",
                        "type": "ai",
                        "depends_on": ["data"],
                        "params": {
                            "prompt": "Summarize the qualified leads.",
                            "context": "$nodes.data.items",
                        },
                    },
                ],
            },
        },
        state={"_xvond_execution_key": "ai-context-test"},
        run_id=1,
        step_index=0,
    )

    assert captured["agent_id"] == 7
    assert "Summarize the qualified leads." in captured["message"]
    assert '"name": "A"' in captured["message"]
    assert '"score": 84' in captured["message"]
    assert result["graph_outputs"]["summarize"]["ai_response"] == "summary"


def test_ai_step_context_is_bounded_to_runtime_message_limit(monkeypatch):
    captured = {}

    def fake_chat(**kwargs):
        captured.update(kwargs)
        return {
            "response": {"content": "ok"},
            "conversation_id": 1,
        }

    monkeypatch.setattr(
        automation_runtime_module.agent_runtime,
        "chat",
        fake_chat,
    )

    runtime = automation_runtime_module.AutomationRuntime()
    runtime.execute_step(
        db=object(),
        company_id=1,
        step={
            "type": "ai",
            "agent_id": 1,
            "prompt": "Analyze this page.",
            "context": "x" * 20000,
        },
        state={},
        run_id=1,
        step_index=0,
    )

    assert len(captured["message"]) <= 12000
    assert "context truncated by Xvond" in captured["message"]


def test_graph_media_node_consumes_explicit_resolved_context(monkeypatch):
    captured = {}

    def fake_generate_image_asset(*, prompt, model=None, size="1024x1024"):
        captured.update({"prompt": prompt, "model": model, "size": size})
        return {
            "media_url": "https://api.xvond.test/media/generated/context.jpg?x=1",
            "content_type": "image/jpeg",
            "bytes": 100,
            "model": "image-test",
        }

    monkeypatch.setattr(
        automation_runtime_module,
        "generate_image_asset",
        fake_generate_image_asset,
    )

    runtime = automation_runtime_module.AutomationRuntime()
    result = runtime.execute_step(
        db=object(),
        company_id=1,
        step={
            "type": "graph",
            "agent_id": 1,
            "graph": {
                "version": 1,
                "nodes": [
                    {
                        "id": "brief",
                        "type": "transform",
                        "depends_on": [],
                        "params": {
                            "values": {
                                "creative": "Minimal clinic launch visual"
                            }
                        },
                    },
                    {
                        "id": "image",
                        "type": "media",
                        "depends_on": ["brief"],
                        "params": {
                            "prompt": "Create the campaign visual.",
                            "context": "$nodes.brief.creative",
                            "size": "1024x1024",
                        },
                    },
                ],
            },
        },
        state={"ai_response": "legacy fallback must not win"},
        run_id=1,
        step_index=0,
    )

    assert "Create the campaign visual." in captured["prompt"]
    assert "Minimal clinic launch visual" in captured["prompt"]
    assert "legacy fallback must not win" not in captured["prompt"]
    assert result["graph_outputs"]["image"]["media_url"].startswith(
        "https://api.xvond.test/"
    )



def test_graph_browser_read_only_runs_without_approval(monkeypatch):
    captured = {}

    def fake_browser(**kwargs):
        captured.update(kwargs)
        return {
            "url": "https://example.com",
            "title": "Example",
            "actions": [{"index": 0, "op": "extract_text", "value": "Hello"}],
        }

    monkeypatch.setattr(
        automation_runtime_module,
        "run_browser_task",
        fake_browser,
    )

    runtime = automation_runtime_module.AutomationRuntime()
    result = runtime.execute_step(
        db=object(),
        company_id=1,
        step={
            "type": "graph",
            "agent_id": 7,
            "graph": {
                "version": 1,
                "nodes": [
                    {
                        "id": "page",
                        "type": "browser",
                        "depends_on": [],
                        "params": {
                            "url": "https://example.com",
                            "actions": [
                                {
                                    "op": "extract_text",
                                    "selector": "body",
                                }
                            ],
                        },
                    }
                ],
            },
        },
        state={"_xvond_execution_key": "browser-read-only"},
        run_id=1,
        step_index=0,
    )

    assert captured["start_url"] == "https://example.com"
    assert captured["allow_interactions"] is False
    assert result["graph_outputs"]["page"]["title"] == "Example"


def test_interactive_browser_pauses_and_resumes_same_run(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    calls = {"fetch": 0, "browser": 0}

    monkeypatch.setattr(
        automation_runtime_module.service_limits,
        "record",
        lambda *args, **kwargs: None,
    )

    def fake_request(**kwargs):
        calls["fetch"] += 1
        return {
            "status_code": 200,
            "response": '{"target":"https://example.com/form"}',
            "truncated": False,
        }

    def fake_browser(**kwargs):
        calls["browser"] += 1
        assert kwargs["allow_interactions"] is True
        return {
            "url": kwargs["start_url"],
            "title": "Form",
            "actions": [{"index": 0, "op": "click", "clicked": True}],
        }

    monkeypatch.setattr(
        automation_runtime_module,
        "safe_http_request",
        fake_request,
    )
    monkeypatch.setattr(
        automation_runtime_module,
        "run_browser_task",
        fake_browser,
    )

    with Session(engine, autoflush=False) as db:
        db.add(
            Company(
                id=1,
                name="Browser Approval",
                active=True,
                lifecycle_status="live",
                onboarding_source="self_service",
            )
        )
        db.add(
            AIAgent(
                id=1,
                company_id=1,
                name="Browser worker",
                system_prompt="Use browser only when required.",
                provider="mock",
                model="mock",
                enabled=True,
            )
        )
        db.flush()
        db.add(
            AgentConfig(
                agent_id=1,
                agent_type="employee",
                settings={},
                capabilities={},
                customer_controls={},
            )
        )
        workflow = AutomationWorkflow(
            id=1,
            company_id=1,
            name="Browser graph",
            trigger_type="manual",
            trigger_config={
                "_xvond_source": "self_service_employee",
                "_xvond_agent_id": 1,
                "_xvond_graph_trigger": True,
            },
            steps=[
                {
                    "type": "graph",
                    "agent_id": 1,
                    "graph": {
                        "version": 1,
                        "nodes": [
                            {
                                "id": "discover",
                                "type": "http_get_json",
                                "depends_on": [],
                                "params": {"url": "https://example.com/config"},
                            },
                            {
                                "id": "interact",
                                "type": "browser",
                                "depends_on": ["discover"],
                                "params": {
                                    "url": "$nodes.discover.result.target",
                                    "actions": [
                                        {
                                            "op": "click",
                                            "selector": "#submit",
                                        }
                                    ],
                                },
                            },
                        ],
                    },
                }
            ],
            enabled=True,
        )
        db.add(workflow)
        db.commit()

        runtime = automation_runtime_module.AutomationRuntime()
        waiting = runtime.execute(
            db=db,
            company_id=1,
            workflow=workflow,
            input_data={},
        )

        assert waiting.status == "waiting_approval"
        assert calls["fetch"] == 1
        assert calls["browser"] == 0

        request = (
            db.query(ActionRequest)
            .filter(ActionRequest.action_type == "browser_interaction")
            .one()
        )
        assert request.status == "awaiting_confirmation"
        assert waiting.output_data["approval"]["node_id"] == "interact"

        request.status = "approved"
        db.commit()

        resumed = runtime.resume_approval(
            db,
            company_id=1,
            workflow=workflow,
            run=waiting,
            request=request,
        )

        assert resumed.status == "success"
        assert resumed.id == waiting.id
        assert calls["fetch"] == 1
        assert calls["browser"] == 1
        assert (
            resumed.output_data["steps"][-1]["result"]["graph_outputs"]["interact"]["title"]
            == "Form"
        )

    engine.dispose()


def test_browser_approval_cannot_authorize_different_node(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    monkeypatch.setattr(
        automation_runtime_module,
        "run_browser_task",
        lambda **kwargs: {
            "url": kwargs["start_url"],
            "title": "Unsafe",
            "actions": [],
        },
    )

    with Session(engine, autoflush=False) as db:
        db.add(Company(id=1, name="Scoped Browser", active=True))
        db.add(
            AIAgent(
                id=1,
                company_id=1,
                name="Scoped worker",
                system_prompt="Scope approvals.",
                provider="mock",
                model="mock",
                enabled=True,
            )
        )
        db.flush()
        db.add(
            AgentConfig(
                agent_id=1,
                agent_type="employee",
                settings={},
                capabilities={},
                customer_controls={},
            )
        )
        request = ActionRequest(
            company_id=1,
            agent_id=1,
            conversation_id=None,
            action_type="browser_interaction",
            details={
                "_xvond_automation": {
                    "run_id": 1,
                    "workflow_id": 1,
                    "workflow_step_index": 0,
                    "node_id": "other_node",
                }
            },
            summary="Other browser action",
            status="approved",
        )
        db.add(request)
        db.commit()

        runtime = automation_runtime_module.AutomationRuntime()
        try:
            runtime.execute_step(
                db=db,
                company_id=1,
                step={
                    "type": "graph",
                    "agent_id": 1,
                    "graph": {
                        "version": 1,
                        "nodes": [
                            {
                                "id": "current_node",
                                "type": "browser",
                                "depends_on": [],
                                "params": {
                                    "url": "https://example.com",
                                    "actions": [
                                        {"op": "click", "selector": "#go"}
                                    ],
                                },
                            }
                        ],
                    },
                },
                state={
                    "_xvond_execution_key": "browser-scope-test",
                    "_xvond_approved_request_id": request.id,
                },
                run_id=1,
                step_index=0,
            )
        except automation_runtime_module.AutomationApprovalRequired as exc:
            assert exc.node_id == "current_node"
        else:
            raise AssertionError("approval from another node must not authorize browser")

    engine.dispose()



def test_graph_side_effect_detection_includes_interactive_browser_and_nested_graphs():
    read_only = {
        "version": 1,
        "nodes": [
            {
                "id": "read",
                "type": "browser",
                "params": {
                    "url": "https://example.com",
                    "actions": [{"op": "extract_text", "selector": "body"}],
                },
            }
        ],
    }
    interactive = {
        "version": 1,
        "nodes": [
            {
                "id": "click",
                "type": "browser",
                "params": {
                    "url": "https://example.com",
                    "actions": [{"op": "click", "text": "Continue"}],
                },
            }
        ],
    }
    nested = {
        "version": 1,
        "nodes": [
            {
                "id": "each",
                "type": "foreach",
                "params": {
                    "items": [1],
                    "graph": interactive,
                },
            }
        ],
    }

    assert graph_has_side_effect(read_only) is False
    assert graph_has_side_effect(interactive) is True
    assert graph_has_side_effect(nested) is True



def test_automation_trace_records_step_lifecycle(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(
        automation_runtime_module.service_limits,
        "record",
        lambda *args, **kwargs: None,
    )

    with Session(engine, autoflush=False) as db:
        db.add(Company(id=1, name="Trace Company", active=True))
        db.add(
            AutomationWorkflow(
                id=1,
                company_id=1,
                name="Trace workflow",
                trigger_type="manual",
                trigger_config={},
                steps=[
                    {
                        "type": "transform",
                        "label": "Prepare",
                        "values": {"prepared": True},
                    }
                ],
                enabled=True,
            )
        )
        db.commit()
        workflow = db.query(AutomationWorkflow).filter_by(id=1).one()

        run = automation_runtime_module.AutomationRuntime().execute(
            db=db,
            company_id=1,
            workflow=workflow,
            input_data={"source": "test"},
        )

        trace = run.output_data["trace"]
        assert trace["version"] == 1
        assert trace["status"] == "success"
        assert trace["trigger_type"] == "manual"
        assert trace["trace_id"].startswith("xvond_trace_")
        assert trace["finished_at"]
        assert len(trace["spans"]) == 1
        span = trace["spans"][0]
        assert span["step_index"] == 0
        assert span["step_type"] == "transform"
        assert span["label"] == "Prepare"
        assert span["status"] == "success"
        assert span["phase"] == "execute"
        assert span["duration_ms"] >= 0

    engine.dispose()



def test_approval_resume_rejects_changed_workflow_checkpoint(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(
        automation_runtime_module.service_limits,
        "record",
        lambda *args, **kwargs: None,
    )

    with Session(engine, autoflush=False) as db:
        db.add(Company(id=1, name="Checkpoint Company", active=True))
        db.add(
            AIAgent(
                id=1,
                company_id=1,
                name="Checkpoint worker",
                system_prompt="Ask first.",
                provider="mock",
                model="mock",
                enabled=True,
            )
        )
        db.flush()
        db.add(
            AgentToolAssignment(
                agent_id=1,
                tool_name="action_request",
                enabled=True,
                config={
                    "actions": {
                        "send": {
                            "enabled": True,
                            "confirmation_required": True,
                            "destination": {
                                "type": "xvond_internal",
                                "adapter": "generic_capability",
                                "execution_plan": [
                                    {
                                        "id": "notify",
                                        "op": "notify",
                                        "title": "Send",
                                        "message": "Sent.",
                                    }
                                ],
                            },
                            "availability": {"mode": "none"},
                        }
                    }
                },
            )
        )
        workflow = AutomationWorkflow(
            id=1,
            company_id=1,
            name="Checkpoint workflow",
            trigger_type="manual",
            trigger_config={},
            steps=[
                {
                    "type": "graph",
                    "agent_id": 1,
                    "graph": {
                        "version": 1,
                        "nodes": [
                            {
                                "id": "send",
                                "type": "action",
                                "depends_on": [],
                                "params": {
                                    "action_type": "send",
                                    "arguments": {"value": "original"},
                                },
                            }
                        ],
                    },
                }
            ],
            enabled=True,
        )
        db.add(workflow)
        db.commit()

        runtime = automation_runtime_module.AutomationRuntime()
        waiting = runtime.execute(
            db=db,
            company_id=1,
            workflow=workflow,
            input_data={},
        )
        request = db.query(ActionRequest).one()
        request.status = "approved"
        workflow.steps = [
            {
                "type": "graph",
                "agent_id": 1,
                "graph": {
                    "version": 1,
                    "nodes": [
                        {
                            "id": "send",
                            "type": "action",
                            "depends_on": [],
                            "params": {
                                "action_type": "send",
                                "arguments": {"value": "changed"},
                            },
                        }
                    ],
                },
            }
        ]
        db.flush()

        try:
            runtime.resume_approval(
                db,
                company_id=1,
                workflow=workflow,
                run=waiting,
                request=request,
            )
        except ValueError as exc:
            assert "workflow changed" in str(exc).lower()
        else:
            raise AssertionError("changed workflow must invalidate approval checkpoint")

    engine.dispose()


def test_owner_never_permission_skips_scheduled_action_without_side_effect(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine, autoflush=False) as db:
        db.add(Company(id=1, name="Denied Action", active=True))
        db.add(
            AIAgent(
                id=1,
                company_id=1,
                name="Denied worker",
                system_prompt="Do not execute denied actions.",
                provider="mock",
                model="mock",
                enabled=True,
            )
        )
        db.flush()
        db.add(
            AgentToolAssignment(
                agent_id=1,
                tool_name="action_request",
                enabled=True,
                config={
                    "actions": {
                        "send_report": {
                            "enabled": True,
                            "confirmation_required": True,
                            "_xvond_permission_mode": "never",
                            "xvond_generated": True,
                            "destination": {
                                "type": "xvond_internal",
                                "adapter": "generic_capability",
                                "execution_plan": [
                                    {
                                        "id": "notify",
                                        "op": "notify",
                                        "title": "Report",
                                        "message": "Sent.",
                                    }
                                ],
                            },
                        }
                    }
                },
            )
        )
        db.commit()

        called = {"count": 0}

        def forbidden(*args, **kwargs):
            called["count"] += 1
            raise AssertionError("owner-denied action must not execute")

        monkeypatch.setattr(
            automation_runtime_module,
            "execute_generic_capability",
            forbidden,
        )

        result = automation_runtime_module.AutomationRuntime().execute_step(
            db=db,
            company_id=1,
            step={
                "type": "scheduled_action",
                "agent_id": 1,
                "action_type": "send_report",
            },
            state={"_xvond_execution_key": "denied-action-test"},
            run_id=1,
            step_index=0,
        )

        assert called["count"] == 0
        assert result["scheduled_action_result"] == {
            "skipped": True,
            "reason": "owner_permission_never",
            "action_type": "send_report",
        }

    engine.dispose()



def test_monthly_schedule_uses_generic_day_of_month_and_clamps_short_months():
    schedule = normalize_schedule_config(
        {
            "kind": "monthly",
            "day_of_month": 31,
            "hour": 9,
            "minute": 0,
            "timezone": "UTC",
        }
    )
    slot = latest_due_slot(
        schedule,
        now=datetime(2026, 2, 28, 10, 0, tzinfo=UTC),
        created_at=datetime(2026, 1, 1, 0, 0, tzinfo=UTC),
    )

    assert schedule["day_of_month"] == 31
    assert slot == datetime(2026, 2, 28, 9, 0, tzinfo=UTC)


def test_one_time_schedule_is_due_only_after_target_time():
    schedule = normalize_schedule_config(
        {
            "kind": "once",
            "at": "2026-10-01T09:00:00",
            "timezone": "Asia/Muscat",
        }
    )

    before = latest_due_slot(
        schedule,
        now=datetime(2026, 10, 1, 4, 59, tzinfo=UTC),
        created_at=datetime(2026, 9, 20, 0, 0, tzinfo=UTC),
    )
    due = latest_due_slot(
        schedule,
        now=datetime(2026, 10, 1, 5, 1, tzinfo=UTC),
        created_at=datetime(2026, 9, 20, 0, 0, tzinfo=UTC),
    )

    assert schedule["at"] == "2026-10-01T05:00:00Z"
    assert before is None
    assert due == datetime(2026, 10, 1, 5, 0, tzinfo=UTC)



def test_wait_graph_contract_is_generic_and_bounded():
    assert graph_contract_errors(
        {
            "version": 1,
            "nodes": [
                {
                    "id": "pause",
                    "type": "wait",
                    "depends_on": [],
                    "params": {"duration": 2, "unit": "days"},
                }
            ],
        }
    ) == []

    errors = graph_contract_errors(
        {
            "version": 1,
            "nodes": [
                {
                    "id": "pause",
                    "type": "wait",
                    "depends_on": [],
                    "params": {"duration": 500, "unit": "weeks"},
                }
            ],
        }
    )
    assert any("at most one year" in item for item in errors)

    ambiguous_until = graph_contract_errors(
        {
            "version": 1,
            "nodes": [
                {
                    "id": "pause",
                    "type": "wait",
                    "depends_on": [],
                    "params": {"until": "2026-10-01T09:00:00"},
                }
            ],
        }
    )
    assert any("timezone offset" in item for item in ambiguous_until)


def test_graph_wait_resumes_same_run_without_replaying_prior_nodes(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    calls = {"fetch": 0}

    monkeypatch.setattr(
        automation_runtime_module.service_limits,
        "record",
        lambda *args, **kwargs: None,
    )

    def fake_request(**kwargs):
        calls["fetch"] += 1
        return {
            "status_code": 200,
            "response": '{"value":42}',
            "truncated": False,
        }

    monkeypatch.setattr(
        automation_runtime_module,
        "safe_http_request",
        fake_request,
    )

    with Session(engine, autoflush=False) as db:
        db.add(
            Company(
                id=1,
                name="Durable Wait",
                active=True,
                lifecycle_status="live",
                onboarding_source="self_service",
            )
        )
        db.add(
            AIAgent(
                id=1,
                company_id=1,
                name="Long-running worker",
                system_prompt="Run durable work.",
                provider="mock",
                model="mock",
                enabled=True,
            )
        )
        db.flush()
        db.add(
            AgentConfig(
                agent_id=1,
                agent_type="employee",
                settings={},
                capabilities={},
                customer_controls={},
            )
        )
        workflow = AutomationWorkflow(
            id=1,
            company_id=1,
            name="Durable wait graph",
            trigger_type="manual",
            trigger_config={"_xvond_agent_id": 1},
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
                            },
                            {
                                "id": "pause",
                                "type": "wait",
                                "depends_on": ["fetch"],
                                "params": {"duration": 5, "unit": "minutes"},
                            },
                            {
                                "id": "after_wait",
                                "type": "notify",
                                "depends_on": ["pause"],
                                "params": {"message": "continued"},
                            },
                        ],
                    },
                }
            ],
            enabled=True,
        )
        db.add(workflow)
        db.commit()

        runtime = automation_runtime_module.AutomationRuntime()
        waiting = runtime.execute(
            db=db,
            company_id=1,
            workflow=workflow,
            input_data={"_xvond_execution_key": "durable-wait-test"},
        )

        assert waiting.status == "waiting_time"
        assert waiting.resume_at is not None
        assert waiting.finished_at is None
        assert calls["fetch"] == 1
        assert waiting.output_data["wait"]["node_id"] == "pause"

        resumed = runtime.resume_wait(
            db,
            company_id=1,
            workflow=workflow,
            run=waiting,
            now=waiting.resume_at,
        )

        assert resumed.id == waiting.id
        assert resumed.status == "success"
        assert resumed.resume_at is None
        assert calls["fetch"] == 1
        outputs = resumed.output_data["steps"][-1]["result"]["graph_outputs"]
        assert outputs["pause"]["resumed"] is True
        assert outputs["after_wait"]["notification"]["message"] == "continued"

    engine.dispose()


def test_scheduler_resumes_due_durable_wait(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = lambda: Session(engine, autoflush=False)

    monkeypatch.setattr(scheduler, "SessionLocal", factory)
    monkeypatch.setattr(
        automation_runtime_module.service_limits,
        "record",
        lambda *args, **kwargs: None,
    )

    with factory() as db:
        db.add(
            Company(
                id=1,
                name="Due Wait",
                active=True,
                lifecycle_status="live",
                onboarding_source="self_service",
            )
        )
        db.add(
            AIAgent(
                id=1,
                company_id=1,
                name="Delayed worker",
                system_prompt="Continue later.",
                provider="mock",
                model="mock",
                enabled=True,
            )
        )
        db.flush()
        db.add(
            AgentConfig(
                agent_id=1,
                agent_type="employee",
                settings={},
                capabilities={},
                customer_controls={},
            )
        )
        workflow = AutomationWorkflow(
            id=1,
            company_id=1,
            name="Delayed workflow",
            trigger_type="manual",
            trigger_config={"_xvond_agent_id": 1},
            steps=[
                {
                    "type": "graph",
                    "agent_id": 1,
                    "graph": {
                        "version": 1,
                        "nodes": [
                            {
                                "id": "pause",
                                "type": "wait",
                                "depends_on": [],
                                "params": {"duration": 1, "unit": "minutes"},
                            },
                            {
                                "id": "done",
                                "type": "notify",
                                "depends_on": ["pause"],
                                "params": {"message": "done"},
                            },
                        ],
                    },
                }
            ],
            enabled=True,
        )
        db.add(workflow)
        db.commit()

        waiting = automation_runtime_module.automation_runtime.execute(
            db=db,
            company_id=1,
            workflow=workflow,
            input_data={"_xvond_execution_key": "scheduler-wait-test"},
        )
        run_id = waiting.id
        due_at = waiting.resume_at

    result = scheduler.run_due_waiting_run(run_id, now=due_at)

    assert result["run_id"] == run_id
    assert result["status"] == "success"

    with factory() as db:
        stored = db.query(AutomationRun).filter(AutomationRun.id == run_id).one()
        assert stored.status == "success"
        assert stored.resume_at is None

    engine.dispose()


def test_nested_foreach_wait_resumes_without_replaying_completed_work(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    calls = {"fetch": 0}

    monkeypatch.setattr(
        automation_runtime_module.service_limits,
        "record",
        lambda *args, **kwargs: None,
    )

    def fake_request(**kwargs):
        calls["fetch"] += 1
        return {
            "status_code": 200,
            "response": '{"ok":true}',
            "truncated": False,
        }

    monkeypatch.setattr(
        automation_runtime_module,
        "safe_http_request",
        fake_request,
    )

    with Session(engine, autoflush=False) as db:
        db.add(
            Company(
                id=1,
                name="Nested Wait",
                active=True,
                lifecycle_status="live",
                onboarding_source="self_service",
            )
        )
        db.add(
            AIAgent(
                id=1,
                company_id=1,
                name="Batch worker",
                system_prompt="Process items with durable pauses.",
                provider="mock",
                model="mock",
                enabled=True,
            )
        )
        db.flush()
        db.add(
            AgentConfig(
                agent_id=1,
                agent_type="employee",
                settings={},
                capabilities={},
                customer_controls={},
            )
        )
        workflow = AutomationWorkflow(
            id=1,
            company_id=1,
            name="Nested wait graph",
            trigger_type="manual",
            trigger_config={"_xvond_agent_id": 1},
            steps=[
                {
                    "type": "graph",
                    "agent_id": 1,
                    "graph": {
                        "version": 1,
                        "nodes": [
                            {
                                "id": "each",
                                "type": "foreach",
                                "depends_on": [],
                                "params": {
                                    "items": [{"id": 1}, {"id": 2}],
                                    "graph": {
                                        "version": 1,
                                        "nodes": [
                                            {
                                                "id": "fetch",
                                                "type": "http_get_json",
                                                "depends_on": [],
                                                "params": {
                                                    "url": "https://example.com/data"
                                                },
                                            },
                                            {
                                                "id": "pause",
                                                "type": "wait",
                                                "depends_on": ["fetch"],
                                                "params": {
                                                    "duration": 1,
                                                    "unit": "minutes",
                                                },
                                            },
                                            {
                                                "id": "done",
                                                "type": "transform",
                                                "depends_on": ["pause"],
                                                "params": {
                                                    "values": {"item_id": "$item.id"}
                                                },
                                            },
                                        ],
                                    },
                                },
                            }
                        ],
                    },
                }
            ],
            enabled=True,
        )
        db.add(workflow)
        db.commit()

        runtime = automation_runtime_module.AutomationRuntime()
        first = runtime.execute(
            db=db,
            company_id=1,
            workflow=workflow,
            input_data={"_xvond_execution_key": "nested-wait-test"},
        )
        assert first.status == "waiting_time"
        assert calls["fetch"] == 1

        second = runtime.resume_wait(
            db,
            company_id=1,
            workflow=workflow,
            run=first,
            now=first.resume_at,
        )
        assert second.status == "waiting_time"
        assert calls["fetch"] == 2

        finished = runtime.resume_wait(
            db,
            company_id=1,
            workflow=workflow,
            run=second,
            now=second.resume_at,
        )
        assert finished.status == "success"
        assert calls["fetch"] == 2
        each = finished.output_data["steps"][-1]["result"]["graph_outputs"]["each"]
        assert each["count"] == 2

    engine.dispose()



def test_await_event_contract_supports_generic_correlation():
    assert graph_contract_errors(
        {
            "version": 1,
            "nodes": [
                {
                    "id": "wait_for_result",
                    "type": "await_event",
                    "depends_on": [],
                    "params": {
                        "event": "external.result.ready",
                        "match": {"job_id": "$input.job_id"},
                    },
                }
            ],
        }
    ) == []

    errors = graph_contract_errors(
        {
            "version": 1,
            "nodes": [
                {
                    "id": "bad_wait",
                    "type": "await_event",
                    "depends_on": [],
                    "params": {"event": ""},
                }
            ],
        }
    )
    assert any("valid event name" in item for item in errors)


def test_correlated_event_resumes_same_run_without_replaying_prior_work(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = lambda: Session(engine, autoflush=False)
    calls = {"fetch": 0}

    monkeypatch.setattr(event_dispatch_module, "SessionLocal", factory)
    monkeypatch.setattr(
        automation_runtime_module.service_limits,
        "record",
        lambda *args, **kwargs: None,
    )

    def fake_request(**kwargs):
        calls["fetch"] += 1
        return {
            "status_code": 200,
            "response": '{"submitted":true}',
            "truncated": False,
        }

    monkeypatch.setattr(
        automation_runtime_module,
        "safe_http_request",
        fake_request,
    )

    with factory() as db:
        db.add(
            Company(
                id=1,
                name="Event Wait",
                active=True,
                lifecycle_status="live",
                onboarding_source="self_service",
            )
        )
        db.add(
            AIAgent(
                id=1,
                company_id=1,
                name="Event-driven worker",
                system_prompt="Continue after a correlated event.",
                provider="mock",
                model="mock",
                enabled=True,
            )
        )
        db.flush()
        db.add(
            AgentConfig(
                agent_id=1,
                agent_type="employee",
                settings={},
                capabilities={},
                customer_controls={},
            )
        )
        workflow = AutomationWorkflow(
            id=1,
            company_id=1,
            name="Correlated event continuation",
            trigger_type="manual",
            trigger_config={"_xvond_agent_id": 1},
            steps=[
                {
                    "type": "graph",
                    "agent_id": 1,
                    "graph": {
                        "version": 1,
                        "nodes": [
                            {
                                "id": "submit",
                                "type": "http_get_json",
                                "depends_on": [],
                                "params": {"url": "https://example.com/start"},
                            },
                            {
                                "id": "await_result",
                                "type": "await_event",
                                "depends_on": ["submit"],
                                "params": {
                                    "event": "external.result.ready",
                                    "match": {"job_id": "$input.job_id"},
                                },
                            },
                            {
                                "id": "final",
                                "type": "transform",
                                "depends_on": ["await_result"],
                                "params": {
                                    "values": {
                                        "status": "$nodes.await_result.payload.status"
                                    }
                                },
                            },
                        ],
                    },
                }
            ],
            enabled=True,
        )
        db.add(workflow)
        db.commit()

        waiting = automation_runtime_module.automation_runtime.execute(
            db=db,
            company_id=1,
            workflow=workflow,
            input_data={
                "_xvond_execution_key": "event-wait-test",
                "job_id": "job-123",
            },
        )
        run_id = waiting.id
        assert waiting.status == "waiting_event"
        assert waiting.resume_event_name == "external.result.ready"
        assert waiting.output_data["event_wait"]["match"] == {"job_id": "job-123"}
        assert calls["fetch"] == 1

    unrelated = event_dispatch_module.dispatch_automation_event(
        company_id=1,
        event_name="external.result.ready",
        event_id="evt-unrelated",
        payload={"job_id": "job-999", "status": "done"},
    )
    assert unrelated["resumed_waits"] == []

    with factory() as db:
        still_waiting = db.query(AutomationRun).filter(AutomationRun.id == run_id).one()
        assert still_waiting.status == "waiting_event"

    matched = event_dispatch_module.dispatch_automation_event(
        company_id=1,
        event_name="external.result.ready",
        event_id="evt-matched",
        payload={"job_id": "job-123", "status": "done"},
    )

    assert matched["resumed_waits"][0]["run_id"] == run_id
    assert matched["resumed_waits"][0]["status"] == "success"
    assert calls["fetch"] == 1

    with factory() as db:
        finished = db.query(AutomationRun).filter(AutomationRun.id == run_id).one()
        assert finished.status == "success"
        assert finished.resume_event_name is None
        outputs = finished.output_data["steps"][-1]["result"]["graph_outputs"]
        assert outputs["await_result"]["event_id"] == "evt-matched"
        assert outputs["await_result"]["payload"]["status"] == "done"
        assert outputs["final"]["status"] == "done"

    engine.dispose()



def test_graph_notify_persists_owner_visible_event_idempotently():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine, autoflush=False) as db:
        db.add(Company(id=1, name="Notify Co", active=True, lifecycle_status="live"))
        db.commit()

        runtime = automation_runtime_module.AutomationRuntime()
        step = {
            "type": "graph",
            "agent_id": 77,
            "graph": {
                "version": 1,
                "nodes": [
                    {
                        "id": "owner_update",
                        "type": "notify",
                        "depends_on": [],
                        "params": {
                            "title": "Employee finished",
                            "message": "The requested work is ready.",
                            "severity": "info",
                        },
                    }
                ],
            },
        }
        state = {"_xvond_execution_key": "notify-idempotency-key"}

        first = runtime.execute_step(db, 1, step, dict(state), run_id=10, step_index=0)
        second = runtime.execute_step(db, 1, step, dict(state), run_id=10, step_index=0)
        db.commit()

        rows = db.query(NotificationEvent).filter_by(company_id=1).all()
        assert len(rows) == 1
        assert rows[0].event_type == "employee_update"
        assert rows[0].title == "Employee finished"
        assert rows[0].message == "The requested work is ready."
        assert rows[0].payload["agent_id"] == 77
        assert rows[0].payload["automation_run_id"] == 10
        assert first["graph_outputs"]["owner_update"]["notification"]["duplicate"] is False
        assert second["graph_outputs"]["owner_update"]["notification"]["duplicate"] is True
        assert (
            first["graph_outputs"]["owner_update"]["notification"]["event_id"]
            == second["graph_outputs"]["owner_update"]["notification"]["event_id"]
        )

    engine.dispose()


def test_nested_foreach_notifications_are_scoped_per_item():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine, autoflush=False) as db:
        db.add(Company(id=1, name="Batch Notify", active=True, lifecycle_status="live"))
        db.commit()

        runtime = automation_runtime_module.AutomationRuntime()
        result = runtime.execute_step(
            db,
            1,
            {
                "type": "graph",
                "agent_id": 9,
                "graph": {
                    "version": 1,
                    "nodes": [
                        {
                            "id": "each",
                            "type": "foreach",
                            "depends_on": [],
                            "params": {
                                "items": [{"id": 1}, {"id": 2}],
                                "graph": {
                                    "version": 1,
                                    "nodes": [
                                        {
                                            "id": "done",
                                            "type": "notify",
                                            "depends_on": [],
                                            "params": {"message": "Item processed"},
                                        }
                                    ],
                                },
                            },
                        }
                    ],
                },
            },
            {"_xvond_execution_key": "batch-notify"},
            run_id=11,
            step_index=0,
        )
        db.commit()

        rows = (
            db.query(NotificationEvent)
            .filter_by(company_id=1, event_type="employee_update")
            .order_by(NotificationEvent.id.asc())
            .all()
        )
        assert len(rows) == 2
        assert rows[0].event_key != rows[1].event_key
        assert rows[0].payload["node_scope"] == "each[0]/done"
        assert rows[1].payload["node_scope"] == "each[1]/done"
        assert result["graph_outputs"]["each"]["count"] == 2

    engine.dispose()



def test_resumed_interval_routine_waits_for_next_slot_instead_of_catching_up(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = lambda: Session(engine, autoflush=False)
    monkeypatch.setattr(scheduler, "SessionLocal", factory)

    with factory() as db:
        db.add(
            Company(
                id=1,
                name="Self Service",
                active=True,
                lifecycle_status="live",
                onboarding_source="self_service",
            )
        )
        db.add(
            AIAgent(
                id=1,
                company_id=1,
                name="Monitor",
                system_prompt="monitor",
                provider="mock",
                model="mock",
                enabled=True,
            )
        )
        db.add(
            AutomationWorkflow(
                id=1,
                company_id=1,
                name="Hourly routine",
                trigger_type="schedule",
                trigger_config={
                    "_xvond_source": "self_service_employee",
                    "_xvond_agent_id": 1,
                    "_xvond_graph_trigger": True,
                    "_xvond_routine_id": "hourly",
                    "_xvond_resumed_at": "2026-09-18T12:05:00Z",
                    "schedule": {"kind": "interval", "every_minutes": 60},
                },
                steps=[],
                enabled=True,
                created_at=datetime(2026, 9, 18, 10, 0),
            )
        )
        db.commit()

    called = []

    def fake_execute(*, db, company_id, workflow, input_data):
        called.append(dict(input_data))
        run = AutomationRun(
            company_id=company_id,
            workflow_id=workflow.id,
            status="success",
            input_data=dict(input_data),
            output_data={},
            finished_at=datetime(2026, 9, 18, 13, 5),
        )
        db.add(run)
        db.commit()
        db.refresh(run)
        return run

    monkeypatch.setattr(scheduler.automation_runtime, "execute", fake_execute)

    immediately_after_resume = scheduler.run_due_workflow(
        1,
        now=datetime(2026, 9, 18, 12, 10, tzinfo=UTC),
    )
    next_slot = scheduler.run_due_workflow(
        1,
        now=datetime(2026, 9, 18, 13, 6, tzinfo=UTC),
    )

    assert immediately_after_resume["status"] == "not_due"
    assert next_slot["status"] == "success"
    assert called[0]["_xvond_schedule_slot"] == "2026-09-18T13:05:00Z"
    engine.dispose()



def test_automation_runtime_merges_routine_defaults_for_all_triggers(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(
        automation_runtime_module.service_limits,
        "record",
        lambda *args, **kwargs: None,
    )

    with Session(engine, autoflush=False) as db:
        db.add(Company(id=1, name="Defaults Company", active=True))
        workflow = AutomationWorkflow(
            id=1,
            company_id=1,
            name="Scoped defaults",
            trigger_type="manual",
            trigger_config={
                "_xvond_source": "self_service_employee",
                "_xvond_runtime_inputs": {
                    "target": "compiled-default",
                    "fixed": "from-routine",
                },
            },
            steps=[],
            enabled=True,
        )
        db.add(workflow)
        db.commit()

        run = automation_runtime_module.AutomationRuntime().execute(
            db=db,
            company_id=1,
            workflow=workflow,
            input_data={
                "target": "caller-override",
                "dynamic": "runtime",
            },
        )

        assert run.input_data["target"] == "caller-override"
        assert run.input_data["fixed"] == "from-routine"
        assert run.input_data["dynamic"] == "runtime"
        assert run.input_data["_xvond_execution_key"].startswith(
            "automation:1:1:run:"
        )

    engine.dispose()



def test_graph_preview_simulates_side_effects_without_persisting_or_waiting(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    monkeypatch.setattr(
        automation_runtime_module,
        "run_browser_task",
        lambda **kwargs: pytest.fail("interactive browser must not run in preview"),
    )
    monkeypatch.setattr(
        automation_runtime_module,
        "generate_image_asset",
        lambda **kwargs: pytest.fail("media generation must not run in preview"),
    )
    monkeypatch.setattr(
        automation_runtime_module,
        "write_agent_state",
        lambda *args, **kwargs: pytest.fail("state write must not run in preview"),
    )
    monkeypatch.setattr(
        automation_runtime_module,
        "delete_agent_state",
        lambda *args, **kwargs: pytest.fail("state delete must not run in preview"),
    )

    with Session(engine, autoflush=False) as db:
        db.add(Company(id=1, name="Preview Co", active=False))
        db.add(
            AIAgent(
                id=1,
                company_id=1,
                name="Preview worker",
                system_prompt="preview",
                provider="mock",
                model="mock",
                enabled=False,
            )
        )
        db.flush()
        db.add(
            AgentConfig(
                agent_id=1,
                agent_type="employee",
                settings={},
                capabilities={},
                customer_controls={},
            )
        )
        db.commit()

        graph = {
            "version": 1,
            "trigger": {"type": "manual"},
            "nodes": [
                {
                    "id": "think",
                    "type": "ai",
                    "depends_on": [],
                    "params": {"prompt": "Analyze the input."},
                },
                {
                    "id": "act",
                    "type": "action",
                    "depends_on": ["think"],
                    "params": {
                        "action_type": "send_report",
                        "arguments": {"text": "$nodes.think.ai_response"},
                    },
                },
                {
                    "id": "write",
                    "type": "state_write",
                    "depends_on": ["act"],
                    "params": {
                        "namespace": "preview",
                        "key": "last",
                        "value": "$nodes.act.scheduled_action_result.action_type",
                    },
                },
                {
                    "id": "notify",
                    "type": "notify",
                    "depends_on": ["write"],
                    "params": {"title": "Done", "message": "Preview finished."},
                },
                {
                    "id": "pause",
                    "type": "wait",
                    "depends_on": ["notify"],
                    "params": {"duration": 1, "unit": "hours"},
                },
                {
                    "id": "event",
                    "type": "await_event",
                    "depends_on": ["pause"],
                    "params": {"event": "external.ready", "match": {"id": "$input.id"}},
                },
                {
                    "id": "browser",
                    "type": "browser",
                    "depends_on": ["event"],
                    "params": {
                        "url": "https://example.com",
                        "actions": [{"op": "click", "text": "Continue"}],
                    },
                },
                {
                    "id": "image",
                    "type": "media",
                    "depends_on": ["browser"],
                    "params": {"prompt": "Create a preview image."},
                },
                {
                    "id": "delete",
                    "type": "state_delete",
                    "depends_on": ["image"],
                    "params": {
                        "namespace": "preview",
                        "key": "last",
                    },
                },
            ],
        }

        result = automation_runtime_module.AutomationRuntime().execute_step(
            db,
            1,
            {"type": "graph", "agent_id": 1, "graph": graph},
            {
                "id": "abc",
                "_xvond_preview": True,
                "_xvond_execution_key": "preview:1",
                "_xvond_preview_event_payloads": {
                    "event": {"id": "abc", "status": "ready"}
                },
            },
            run_id=0,
            step_index=0,
        )

        outputs = result["graph_outputs"]
        assert outputs["think"]["simulated"] is True
        assert outputs["act"]["scheduled_action_result"]["would_execute"] is True
        assert outputs["write"]["written"] is False
        assert outputs["notify"]["notification"]["persisted"] is False
        assert outputs["pause"]["would_wait"] is True
        assert outputs["event"]["would_wait"] is True
        assert outputs["event"]["payload"]["status"] == "ready"
        assert outputs["browser"]["browser"]["would_interact"] is True
        assert outputs["image"]["media_url"].startswith("preview://media/")
        assert outputs["delete"]["deleted"] is False

        assert db.query(ActionRequest).count() == 0
        assert db.query(NotificationEvent).count() == 0
        config = db.query(AgentConfig).filter_by(agent_id=1).one()
        assert not (config.settings or {}).get("_xvond_runtime_state")

    engine.dispose()


def test_graph_preview_allows_explicit_simulated_node_outputs():
    runtime = automation_runtime_module.AutomationRuntime()
    result = runtime.execute_step(
        db=object(),
        company_id=1,
        step={
            "type": "graph",
            "agent_id": 1,
            "graph": {
                "version": 1,
                "nodes": [
                    {
                        "id": "external",
                        "type": "http_get_json",
                        "depends_on": [],
                        "params": {"url": "https://example.com/data"},
                    },
                    {
                        "id": "check",
                        "type": "condition",
                        "depends_on": ["external"],
                        "params": {
                            "left": "$nodes.external.result.price",
                            "operator": "lt",
                            "right": 10,
                        },
                    },
                ],
            },
        },
        state={
            "_xvond_preview": True,
            "_xvond_preview_outputs": {
                "external": {"result": {"price": 7}}
            },
        },
        run_id=0,
        step_index=0,
    )

    assert result["graph_outputs"]["external"]["preview_override"] is True
    assert result["graph_outputs"]["check"]["matched"] is True



def test_graph_preview_can_use_stateless_real_ai_executor():
    runtime = automation_runtime_module.AutomationRuntime()
    captured = {}

    def preview_ai_executor(*, prompt, context, node_scope):
        captured.update(
            {
                "prompt": prompt,
                "context": context,
                "node_scope": node_scope,
            }
        )
        return {
            "ai_response": "preview answer",
            "usage": {"total_tokens": 12},
        }

    result = runtime.execute_step(
        db=object(),
        company_id=1,
        step={
            "type": "graph",
            "agent_id": 1,
            "graph": {
                "version": 1,
                "nodes": [
                    {
                        "id": "think",
                        "type": "ai",
                        "depends_on": [],
                        "params": {
                            "prompt": "Analyze this.",
                            "context": {"value": 42},
                        },
                    }
                ],
            },
        },
        state={
            "_xvond_preview": True,
            "_xvond_preview_ai_executor": preview_ai_executor,
        },
        run_id=0,
        step_index=0,
    )

    output = result["graph_outputs"]["think"]
    assert output["preview"] is True
    assert output["simulated"] is False
    assert output["ai_response"] == "preview answer"
    assert captured == {
        "prompt": "Analyze this.",
        "context": {"value": 42},
        "node_scope": "think",
    }



def test_next_interval_schedule_slot_is_strictly_future():
    slot = next_schedule_slot(
        {"kind": "interval", "every_minutes": 15},
        after=datetime(2026, 9, 18, 12, 30, tzinfo=UTC),
        created_at=datetime(2026, 9, 18, 12, 0, tzinfo=UTC),
    )
    assert slot == datetime(2026, 9, 18, 12, 45, tzinfo=UTC)


def test_next_daily_schedule_slot_uses_local_timezone():
    slot = next_schedule_slot(
        {"kind": "daily", "hour": 8, "minute": 0, "timezone": "Asia/Muscat"},
        after=datetime(2026, 9, 18, 4, 0, tzinfo=UTC),
        created_at=datetime(2026, 9, 1, 0, 0, tzinfo=UTC),
    )
    assert slot == datetime(2026, 9, 19, 4, 0, tzinfo=UTC)


def test_next_weekly_schedule_slot_selects_nearest_requested_day():
    slot = next_schedule_slot(
        {
            "kind": "weekly",
            "weekdays": [0, 4],
            "hour": 9,
            "minute": 30,
            "timezone": "UTC",
        },
        after=datetime(2026, 9, 18, 10, 0, tzinfo=UTC),
        created_at=datetime(2026, 9, 1, 0, 0, tzinfo=UTC),
    )
    assert slot == datetime(2026, 9, 21, 9, 30, tzinfo=UTC)


def test_next_monthly_schedule_clamps_short_month_and_moves_forward():
    slot = next_schedule_slot(
        {
            "kind": "monthly",
            "day_of_month": 31,
            "hour": 9,
            "minute": 0,
            "timezone": "UTC",
        },
        after=datetime(2026, 1, 31, 10, 0, tzinfo=UTC),
        created_at=datetime(2026, 1, 1, 0, 0, tzinfo=UTC),
    )
    assert slot == datetime(2026, 2, 28, 9, 0, tzinfo=UTC)


def test_next_one_time_schedule_disappears_after_execution_time():
    before = next_schedule_slot(
        {"kind": "once", "at": "2026-10-01T05:00:00Z"},
        after=datetime(2026, 10, 1, 4, 59, tzinfo=UTC),
        created_at=datetime(2026, 9, 1, 0, 0, tzinfo=UTC),
    )
    after = next_schedule_slot(
        {"kind": "once", "at": "2026-10-01T05:00:00Z"},
        after=datetime(2026, 10, 1, 5, 0, tzinfo=UTC),
        created_at=datetime(2026, 9, 1, 0, 0, tzinfo=UTC),
    )
    assert before == datetime(2026, 10, 1, 5, 0, tzinfo=UTC)
    assert after is None



def test_foreach_graph_actions_use_stable_per_item_idempotency_keys(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    calls = []

    def fake_capability(*args, **kwargs):
        calls.append(
            {
                "details": dict(kwargs.get("details") or {}),
                "idempotency_key": kwargs["idempotency_key"],
            }
        )
        return {"ok": True}

    monkeypatch.setattr(
        automation_runtime_module,
        "execute_generic_capability",
        fake_capability,
    )

    with Session(engine, autoflush=False) as db:
        db.add(Company(id=1, name="Foreach Idempotency", active=True))
        db.add(
            AIAgent(
                id=1,
                company_id=1,
                name="Worker",
                system_prompt="work",
                provider="mock",
                model="mock",
                enabled=True,
            )
        )
        db.flush()
        db.add(
            AgentToolAssignment(
                agent_id=1,
                tool_name="action_request",
                enabled=True,
                config={
                    "actions": {
                        "send_item": {
                            "enabled": True,
                            "confirmation_required": False,
                            "_xvond_permission_mode": "automatic",
                            "destination": {
                                "type": "xvond_internal",
                                "adapter": "generic_capability",
                            },
                        }
                    }
                },
            )
        )
        db.commit()

        graph = {
            "version": 1,
            "nodes": [
                {
                    "id": "each",
                    "type": "foreach",
                    "depends_on": [],
                    "params": {
                        "items": [{"id": 1}, {"id": 2}],
                        "graph": {
                            "version": 1,
                            "nodes": [
                                {
                                    "id": "send",
                                    "type": "action",
                                    "depends_on": [],
                                    "params": {
                                        "action_type": "send_item",
                                        "arguments": {"id": "$item.id"},
                                    },
                                }
                            ],
                        },
                    },
                }
            ],
        }

        runtime = automation_runtime_module.AutomationRuntime()
        for _ in range(2):
            result = runtime.execute_step(
                db,
                1,
                {"type": "graph", "agent_id": 1, "graph": graph},
                {"_xvond_execution_key": "stable-foreach-retry"},
                run_id=1,
                step_index=0,
            )
            assert result["graph_outputs"]["each"]["count"] == 2

    assert len(calls) == 4
    first_run_keys = [item["idempotency_key"] for item in calls[:2]]
    second_run_keys = [item["idempotency_key"] for item in calls[2:]]

    assert first_run_keys[0] != first_run_keys[1]
    assert first_run_keys == second_run_keys
    assert all(
        key.startswith("stable-foreach-retry:") and ":graph:" in key
        for key in first_run_keys
    )
    engine.dispose()

def test_failed_graph_retry_skips_completed_action_and_reuses_same_run(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    action_calls = []
    fetch_calls = {"count": 0}

    monkeypatch.setattr(
        automation_runtime_module.service_limits,
        "record",
        lambda *args, **kwargs: None,
    )

    def fake_capability(*args, **kwargs):
        action_calls.append(
            {
                "details": dict(kwargs.get("details") or {}),
                "idempotency_key": kwargs["idempotency_key"],
            }
        )
        return {"ok": True}

    def fake_request(**kwargs):
        fetch_calls["count"] += 1
        if fetch_calls["count"] == 1:
            raise ValueError("temporary upstream failure")
        return {
            "status_code": 200,
            "response": '{"value":42}',
            "truncated": False,
        }

    monkeypatch.setattr(
        automation_runtime_module,
        "execute_generic_capability",
        fake_capability,
    )
    monkeypatch.setattr(
        automation_runtime_module,
        "safe_http_request",
        fake_request,
    )

    with Session(engine, autoflush=False) as db:
        db.add(Company(id=1, name="Retry Company", active=True))
        db.add(
            AIAgent(
                id=1,
                company_id=1,
                name="Retry Worker",
                system_prompt="work",
                provider="mock",
                model="mock",
                enabled=True,
            )
        )
        db.flush()
        db.add(
            AgentToolAssignment(
                agent_id=1,
                tool_name="action_request",
                enabled=True,
                config={
                    "actions": {
                        "send": {
                            "enabled": True,
                            "confirmation_required": False,
                            "_xvond_permission_mode": "automatic",
                            "destination": {
                                "type": "xvond_internal",
                                "adapter": "generic_capability",
                            },
                        }
                    }
                },
            )
        )
        workflow = AutomationWorkflow(
            id=1,
            company_id=1,
            name="Retry graph",
            trigger_type="manual",
            trigger_config={},
            steps=[
                {
                    "type": "graph",
                    "agent_id": 1,
                    "graph": {
                        "version": 1,
                        "nodes": [
                            {
                                "id": "send",
                                "type": "action",
                                "depends_on": [],
                                "params": {
                                    "action_type": "send",
                                    "arguments": {"value": "once"},
                                },
                            },
                            {
                                "id": "fetch",
                                "type": "http_get_json",
                                "depends_on": ["send"],
                                "params": {"url": "https://example.com/data"},
                            },
                        ],
                    },
                }
            ],
            enabled=True,
        )
        db.add(workflow)
        db.commit()

        runtime = automation_runtime_module.AutomationRuntime()
        with pytest.raises(ValueError, match="temporary upstream failure"):
            runtime.execute(
                db=db,
                company_id=1,
                workflow=workflow,
                input_data={"_xvond_execution_key": "safe-retry-test"},
            )

        failed = db.query(AutomationRun).one()
        original_run_id = failed.id
        assert failed.status == "failed"
        checkpoint = failed.output_data["retry_checkpoint"]
        assert checkpoint["safe"] is True
        assert checkpoint["failed_node_id"] == "fetch"
        assert checkpoint["graph_resume"]["node_id"] == "fetch"
        assert "send" in checkpoint["graph_resume"]["node_outputs"]
        assert len(action_calls) == 1
        assert fetch_calls["count"] == 1

        retried = runtime.retry_failed(
            db,
            company_id=1,
            workflow=workflow,
            run=failed,
        )

        assert retried.id == original_run_id
        assert retried.status == "success"
        assert retried.output_data["retry"]["status"] == "succeeded"
        assert retried.output_data["retry_attempts"] == 1
        assert len(action_calls) == 1
        assert fetch_calls["count"] == 2
        outputs = retried.output_data["steps"][-1]["result"]["graph_outputs"]
        assert outputs["send"]["scheduled_action_result"]["ok"] is True
        assert outputs["fetch"]["result"]["value"] == 42

    engine.dispose()


def test_failed_foreach_retry_resumes_failed_item_without_replaying_prior_items(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    action_items = []
    ai_messages = []

    monkeypatch.setattr(
        automation_runtime_module.service_limits,
        "record",
        lambda *args, **kwargs: None,
    )

    def fake_capability(*args, **kwargs):
        details = dict(kwargs.get("details") or {})
        action_items.append(details.get("id"))
        return {"ok": True, "id": details.get("id")}

    def fake_chat(**kwargs):
        message = str(kwargs.get("message") or "")
        ai_messages.append(message)
        if message == "2" and ai_messages.count("2") == 1:
            raise ValueError("temporary AI failure")
        return {
            "response": {"content": f"ok-{message}"},
            "conversation_id": 1,
        }

    monkeypatch.setattr(
        automation_runtime_module,
        "execute_generic_capability",
        fake_capability,
    )
    monkeypatch.setattr(
        automation_runtime_module.agent_runtime,
        "chat",
        fake_chat,
    )

    with Session(engine, autoflush=False) as db:
        db.add(Company(id=1, name="Foreach Retry", active=True))
        db.add(
            AIAgent(
                id=1,
                company_id=1,
                name="Loop Worker",
                system_prompt="work",
                provider="mock",
                model="mock",
                enabled=True,
            )
        )
        db.flush()
        db.add(
            AgentToolAssignment(
                agent_id=1,
                tool_name="action_request",
                enabled=True,
                config={
                    "actions": {
                        "send_item": {
                            "enabled": True,
                            "confirmation_required": False,
                            "_xvond_permission_mode": "automatic",
                            "destination": {
                                "type": "xvond_internal",
                                "adapter": "generic_capability",
                            },
                        }
                    }
                },
            )
        )
        workflow = AutomationWorkflow(
            id=1,
            company_id=1,
            name="Foreach retry graph",
            trigger_type="manual",
            trigger_config={},
            steps=[
                {
                    "type": "graph",
                    "agent_id": 1,
                    "graph": {
                        "version": 1,
                        "nodes": [
                            {
                                "id": "each",
                                "type": "foreach",
                                "depends_on": [],
                                "params": {
                                    "items": [{"id": 1}, {"id": 2}],
                                    "graph": {
                                        "version": 1,
                                        "nodes": [
                                            {
                                                "id": "send",
                                                "type": "action",
                                                "depends_on": [],
                                                "params": {
                                                    "action_type": "send_item",
                                                    "arguments": {"id": "$item.id"},
                                                },
                                            },
                                            {
                                                "id": "judge",
                                                "type": "ai",
                                                "depends_on": ["send"],
                                                "params": {"prompt": "$item.id"},
                                            },
                                        ],
                                    },
                                },
                            }
                        ],
                    },
                }
            ],
            enabled=True,
        )
        db.add(workflow)
        db.commit()

        runtime = automation_runtime_module.AutomationRuntime()
        with pytest.raises(ValueError, match="temporary AI failure"):
            runtime.execute(
                db=db,
                company_id=1,
                workflow=workflow,
                input_data={"_xvond_execution_key": "foreach-safe-retry"},
            )

        failed = db.query(AutomationRun).one()
        checkpoint = failed.output_data["retry_checkpoint"]
        graph_resume = checkpoint["graph_resume"]
        assert checkpoint["safe"] is True
        assert checkpoint["failed_node_id"] == "judge"
        assert checkpoint["failed_node_scope"] == "each[1]/judge"
        assert graph_resume["node_id"] == "each"
        assert graph_resume["foreach"]["loop_index"] == 1
        assert len(graph_resume["foreach"]["completed_results"]) == 1
        assert graph_resume["foreach"]["child_resume"]["node_id"] == "judge"
        assert "send" in graph_resume["foreach"]["child_resume"]["node_outputs"]
        assert action_items == [1, 2]
        assert ai_messages == ["1", "2"]

        retried = runtime.retry_failed(
            db,
            company_id=1,
            workflow=workflow,
            run=failed,
        )

        assert retried.status == "success"
        assert action_items == [1, 2]
        assert ai_messages == ["1", "2", "2"]
        outputs = retried.output_data["steps"][-1]["result"]["graph_outputs"]
        assert outputs["each"]["count"] == 2

    engine.dispose()


def test_failed_external_action_retry_is_blocked_when_outcome_is_uncertain(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    external_calls = {"count": 0}

    monkeypatch.setattr(
        automation_runtime_module.service_limits,
        "record",
        lambda *args, **kwargs: None,
    )

    def fake_integration(*args, **kwargs):
        external_calls["count"] += 1
        return SimpleNamespace(
            success=False,
            error="external outcome is unknown",
            data={"reconciliation_required": True},
        )

    monkeypatch.setattr(
        automation_runtime_module,
        "_integration_call",
        fake_integration,
    )

    with Session(engine, autoflush=False) as db:
        db.add(Company(id=1, name="Unsafe Retry", active=True))
        db.add(
            AIAgent(
                id=1,
                company_id=1,
                name="External Worker",
                system_prompt="work",
                provider="mock",
                model="mock",
                enabled=True,
            )
        )
        db.flush()
        db.add(
            AgentToolAssignment(
                agent_id=1,
                tool_name="action_request",
                enabled=True,
                config={
                    "actions": {
                        "publish": {
                            "enabled": True,
                            "confirmation_required": False,
                            "_xvond_permission_mode": "automatic",
                            "destination": {
                                "type": "integration",
                                "integration_id": 999,
                            },
                        }
                    }
                },
            )
        )
        workflow = AutomationWorkflow(
            id=1,
            company_id=1,
            name="Unsafe external action",
            trigger_type="manual",
            trigger_config={},
            steps=[
                {
                    "type": "graph",
                    "agent_id": 1,
                    "graph": {
                        "version": 1,
                        "nodes": [
                            {
                                "id": "publish",
                                "type": "action",
                                "depends_on": [],
                                "params": {
                                    "action_type": "publish",
                                    "arguments": {"post": "hello"},
                                },
                            }
                        ],
                    },
                }
            ],
            enabled=True,
        )
        db.add(workflow)
        db.commit()

        runtime = automation_runtime_module.AutomationRuntime()
        with pytest.raises(ValueError, match="external outcome is unknown"):
            runtime.execute(
                db=db,
                company_id=1,
                workflow=workflow,
                input_data={"_xvond_execution_key": "unsafe-external-retry"},
            )

        failed = db.query(AutomationRun).one()
        checkpoint = failed.output_data["retry_checkpoint"]
        assert checkpoint["safe"] is False
        assert checkpoint["failed_node_id"] == "publish"
        assert "external integration" in checkpoint["reason"]
        assert external_calls["count"] == 1

        with pytest.raises(ValueError, match="external integration"):
            runtime.retry_failed(
                db,
                company_id=1,
                workflow=workflow,
                run=failed,
            )

        assert external_calls["count"] == 1

    engine.dispose()

def test_safe_background_failure_is_retried_automatically_on_same_run(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = lambda: Session(engine, autoflush=False)
    calls = {"fetch": 0}

    monkeypatch.setattr(scheduler, "SessionLocal", factory)
    monkeypatch.setattr(
        automation_runtime_module.service_limits,
        "record",
        lambda *args, **kwargs: None,
    )

    def fake_request(**kwargs):
        calls["fetch"] += 1
        if calls["fetch"] == 1:
            raise ValueError("temporary upstream failure")
        return {
            "status_code": 200,
            "response": '{"value":42}',
            "truncated": False,
        }

    monkeypatch.setattr(
        automation_runtime_module,
        "safe_http_request",
        fake_request,
    )

    with factory() as db:
        db.add(
            Company(
                id=1,
                name="Automatic Recovery",
                active=True,
                lifecycle_status="live",
                onboarding_source="self_service",
            )
        )
        db.add(
            AIAgent(
                id=1,
                company_id=1,
                name="Background Worker",
                system_prompt="work",
                provider="mock",
                model="mock",
                enabled=True,
            )
        )
        workflow = AutomationWorkflow(
            id=1,
            company_id=1,
            name="Safe background routine",
            trigger_type="schedule",
            trigger_config={
                "_xvond_source": "self_service_employee",
                "_xvond_agent_id": 1,
                "_xvond_graph_trigger": True,
                "_xvond_routine_id": "monitor",
                "schedule": {"kind": "interval", "every_minutes": 5},
            },
            steps=[
                {
                    "type": "graph",
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
        db.commit()

        with pytest.raises(ValueError, match="temporary upstream failure"):
            automation_runtime_module.AutomationRuntime().execute(
                db=db,
                company_id=1,
                workflow=workflow,
                input_data={"_xvond_execution_key": "automatic-retry"},
            )

        run = db.query(AutomationRun).one()
        original_run_id = run.id
        assert run.status == "waiting_retry"
        assert run.resume_at is not None
        assert run.finished_at is None
        assert run.output_data["retry"]["status"] == "scheduled"
        assert run.output_data["retry"]["attempt"] == 1
        assert run.output_data["retry"]["max_attempts"] == 2
        due_at = run.resume_at

    result = scheduler.run_due_retry_run(original_run_id, now=due_at)

    assert result["run_id"] == original_run_id
    assert result["status"] == "success"
    assert calls["fetch"] == 2
    with factory() as db:
        run = db.get(AutomationRun, original_run_id)
        assert run.status == "success"
        assert run.resume_at is None
        assert run.output_data["retry_attempts"] == 1
        assert run.output_data["retry"]["status"] == "succeeded"

    engine.dispose()


def test_automatic_safe_retry_stops_after_bounded_attempts(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = lambda: Session(engine, autoflush=False)
    calls = {"fetch": 0}

    monkeypatch.setattr(scheduler, "SessionLocal", factory)
    monkeypatch.setattr(
        automation_runtime_module.service_limits,
        "record",
        lambda *args, **kwargs: None,
    )

    def always_fail(**kwargs):
        calls["fetch"] += 1
        raise ValueError("upstream remains unavailable")

    monkeypatch.setattr(
        automation_runtime_module,
        "safe_http_request",
        always_fail,
    )

    with factory() as db:
        db.add(
            Company(
                id=1,
                name="Bounded Recovery",
                active=True,
                lifecycle_status="live",
                onboarding_source="self_service",
            )
        )
        db.add(
            AIAgent(
                id=1,
                company_id=1,
                name="Background Worker",
                system_prompt="work",
                provider="mock",
                model="mock",
                enabled=True,
            )
        )
        workflow = AutomationWorkflow(
            id=1,
            company_id=1,
            name="Bounded background routine",
            trigger_type="schedule",
            trigger_config={
                "_xvond_source": "self_service_employee",
                "_xvond_agent_id": 1,
                "_xvond_graph_trigger": True,
                "_xvond_routine_id": "monitor",
                "schedule": {"kind": "interval", "every_minutes": 5},
            },
            steps=[
                {
                    "type": "graph",
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
        db.commit()

        with pytest.raises(ValueError, match="upstream remains unavailable"):
            automation_runtime_module.AutomationRuntime().execute(
                db=db,
                company_id=1,
                workflow=workflow,
                input_data={"_xvond_execution_key": "bounded-auto-retry"},
            )
        run = db.query(AutomationRun).one()
        run_id = run.id
        first_due = run.resume_at
        assert run.status == "waiting_retry"
        assert run.output_data["retry"]["attempt"] == 1

    first_retry = scheduler.run_due_retry_run(run_id, now=first_due)
    assert first_retry["status"] == "waiting_retry"
    with factory() as db:
        run = db.get(AutomationRun, run_id)
        second_due = run.resume_at
        assert run.output_data["retry_attempts"] == 1
        assert run.output_data["retry"]["attempt"] == 2
        assert second_due is not None
        assert second_due > first_due

    second_retry = scheduler.run_due_retry_run(run_id, now=second_due)
    assert second_retry["status"] == "failed"
    assert calls["fetch"] == 3
    with factory() as db:
        run = db.get(AutomationRun, run_id)
        assert run.status == "failed"
        assert run.resume_at is None
        assert run.finished_at is not None
        assert run.output_data["retry_attempts"] == 2
        assert run.output_data["retry"]["status"] == "failed"

    engine.dispose()

def test_graph_runtime_repeat_passes_previous_result_and_stops(monkeypatch):
    seen = []
    runtime = automation_runtime_module.AutomationRuntime()
    original = runtime.execute_step

    def dispatch(db, company_id, step, state, *, run_id, step_index):
        if step.get("type") == "graph":
            return original(
                db, company_id, step, state,
                run_id=run_id, step_index=step_index,
            )
        if step.get("type") == "scheduled_action":
            cursor = (step.get("arguments") or {}).get("cursor")
            seen.append(cursor)
            next_cursor = None if cursor == 2 else cursor + 1
            return {"cursor": cursor, "next": next_cursor}
        raise AssertionError(step.get("type"))

    monkeypatch.setattr(runtime, "execute_step", dispatch)

    result = runtime.execute_step(
        db=object(),
        company_id=1,
        step={
            "type": "graph",
            "agent_id": 1,
            "graph": {
                "version": 1,
                "nodes": [{
                    "id": "pages",
                    "type": "repeat",
                    "params": {
                        "max_iterations": 5,
                        "initial": {"next": 0},
                        "until": {
                            "path": "next",
                            "operator": "eq",
                            "value": None,
                        },
                        "graph": {
                            "version": 1,
                            "nodes": [{
                                "id": "fetch",
                                "type": "action",
                                "params": {
                                    "action_type": "list_records",
                                    "arguments": {"cursor": "$previous.next"},
                                },
                            }],
                        },
                    },
                }],
            },
        },
        state={"_xvond_execution_key": "repeat-pagination"},
        run_id=1,
        step_index=0,
    )

    repeat = result["graph_outputs"]["pages"]
    assert seen == [0, 1, 2]
    assert repeat["count"] == 3
    assert repeat["stopped"] is True
    assert repeat["limit_reached"] is False
    assert repeat["last"]["next"] is None


def test_graph_runtime_repeat_reports_limit_reached(monkeypatch):
    runtime = automation_runtime_module.AutomationRuntime()
    original = runtime.execute_step

    def dispatch(db, company_id, step, state, *, run_id, step_index):
        if step.get("type") == "graph":
            return original(
                db, company_id, step, state,
                run_id=run_id, step_index=step_index,
            )
        if step.get("type") == "scheduled_action":
            return {"done": False}
        raise AssertionError(step.get("type"))

    monkeypatch.setattr(runtime, "execute_step", dispatch)
    result = runtime.execute_step(
        db=object(),
        company_id=1,
        step={
            "type": "graph",
            "agent_id": 1,
            "graph": {
                "version": 1,
                "nodes": [{
                    "id": "bounded",
                    "type": "repeat",
                    "params": {
                        "max_iterations": 2,
                        "until": {
                            "path": "done",
                            "operator": "eq",
                            "value": True,
                        },
                        "graph": {
                            "version": 1,
                            "nodes": [{
                                "id": "work",
                                "type": "action",
                                "params": {
                                    "action_type": "bounded_work",
                                    "arguments": {"iteration": "$index"},
                                },
                            }],
                        },
                    },
                }],
            },
        },
        state={"_xvond_execution_key": "repeat-limit"},
        run_id=1,
        step_index=0,
    )
    repeat = result["graph_outputs"]["bounded"]
    assert repeat["count"] == 2
    assert repeat["stopped"] is False
    assert repeat["limit_reached"] is True

def test_nested_repeat_wait_resumes_without_replaying_completed_iterations(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    calls = {"fetch": 0}

    monkeypatch.setattr(
        automation_runtime_module.service_limits,
        "record",
        lambda *args, **kwargs: None,
    )

    def fake_request(**kwargs):
        calls["fetch"] += 1
        return {
            "status_code": 200,
            "response": '{"ok":true}',
            "truncated": False,
        }

    monkeypatch.setattr(automation_runtime_module, "safe_http_request", fake_request)

    with Session(engine, autoflush=False) as db:
        db.add(Company(
            id=1,
            name="Repeat Wait",
            active=True,
            lifecycle_status="live",
            onboarding_source="self_service",
        ))
        db.add(AIAgent(
            id=1,
            company_id=1,
            name="Paged worker",
            system_prompt="Process pages with durable pauses.",
            provider="mock",
            model="mock",
            enabled=True,
        ))
        db.flush()
        db.add(AgentConfig(
            agent_id=1,
            agent_type="employee",
            settings={},
            capabilities={},
            customer_controls={},
        ))
        workflow = AutomationWorkflow(
            id=1,
            company_id=1,
            name="Repeat wait graph",
            trigger_type="manual",
            trigger_config={"_xvond_agent_id": 1},
            steps=[{
                "type": "graph",
                "agent_id": 1,
                "graph": {
                    "version": 1,
                    "nodes": [{
                        "id": "pages",
                        "type": "repeat",
                        "params": {
                            "max_iterations": 5,
                            "until": {
                                "path": "done",
                                "operator": "eq",
                                "value": 1,
                            },
                            "graph": {
                                "version": 1,
                                "nodes": [
                                    {
                                        "id": "fetch",
                                        "type": "http_get_json",
                                        "params": {"url": "https://example.com/data"},
                                    },
                                    {
                                        "id": "pause",
                                        "type": "wait",
                                        "depends_on": ["fetch"],
                                        "params": {"duration": 1, "unit": "minutes"},
                                    },
                                    {
                                        "id": "done",
                                        "type": "transform",
                                        "depends_on": ["pause"],
                                        "params": {"values": {"done": "$index"}},
                                    },
                                ],
                            },
                        },
                    }],
                },
            }],
            enabled=True,
        )
        db.add(workflow)
        db.commit()

        runtime = automation_runtime_module.AutomationRuntime()
        first = runtime.execute(
            db=db,
            company_id=1,
            workflow=workflow,
            input_data={"_xvond_execution_key": "repeat-wait-test"},
        )
        assert first.status == "waiting_time"
        assert calls["fetch"] == 1

        second = runtime.resume_wait(
            db,
            company_id=1,
            workflow=workflow,
            run=first,
            now=first.resume_at,
        )
        assert second.status == "waiting_time"
        assert calls["fetch"] == 2

        finished = runtime.resume_wait(
            db,
            company_id=1,
            workflow=workflow,
            run=second,
            now=second.resume_at,
        )
        assert finished.status == "success"
        assert calls["fetch"] == 2
        pages = finished.output_data["steps"][-1]["result"]["graph_outputs"]["pages"]
        assert pages["count"] == 2
        assert pages["stopped"] is True
        assert pages["limit_reached"] is False

    engine.dispose()
