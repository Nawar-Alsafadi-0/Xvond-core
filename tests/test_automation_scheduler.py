from datetime import UTC, datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from backend.app.main import app  # noqa: F401 - register model metadata
from backend.app.core.database.base import Base
from backend.app.models.company import Company
from backend.app.modules.ai_agent.models import AIAgent
from backend.app.modules.automation.models import AutomationRun, AutomationWorkflow
from backend.app.modules.automation.schedule import (
    latest_due_slot,
    normalize_schedule_config,
    schedule_slot_key,
)
from backend.app.modules.automation import scheduler


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
