from datetime import UTC, datetime
from types import SimpleNamespace

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
from backend.app.modules.automation.schedule import (
    latest_due_slot,
    normalize_schedule_config,
    schedule_slot_key,
)
from backend.app.modules.automation import scheduler
from backend.app.modules.automation import runtime as automation_runtime_module
from backend.app.modules.automation.execution_graph import graph_has_side_effect
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


def test_graph_nested_approval_is_blocked_before_execution(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine, autoflush=False) as db:
        db.add(Company(id=1, name="Nested Approval", active=True))
        db.add(
            AIAgent(
                id=1,
                company_id=1,
                name="Nested worker",
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
                                "id": "each",
                                "type": "foreach",
                                "depends_on": [],
                                "params": {
                                    "items": [{"id": 1}],
                                    "graph": {
                                        "version": 1,
                                        "nodes": [
                                            {
                                                "id": "send",
                                                "type": "action",
                                                "depends_on": [],
                                                "params": {
                                                    "action_type": "send_one",
                                                    "arguments": {
                                                        "id": "$item.id"
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
                state={"_xvond_execution_key": "nested-approval-test"},
                run_id=1,
                step_index=0,
            )
        except ValueError as exc:
            assert "inside foreach" in str(exc)
        else:
            raise AssertionError("nested approval must fail closed")

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
