from datetime import UTC, datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from backend.app.core.database.base import Base
from backend.app.models.company import Company
from backend.app.modules.automation import event_dispatch as event_dispatch_module
from backend.app.modules.automation import event_outbox as event_outbox_module
from backend.app.modules.automation.event_outbox import enqueue_automation_event
from backend.app.modules.automation.models import AutomationEventOutbox


def _factory(engine):
    return lambda: Session(engine, autoflush=False)


def test_enqueue_automation_event_is_durable_and_idempotent():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine, autoflush=False) as db:
        db.add(Company(id=1, name="Outbox Company", active=True))
        db.commit()

        first = enqueue_automation_event(
            db,
            company_id=1,
            event_name="booking.created",
            event_id="booking:100",
            source_type="action_request",
            source_id=100,
            payload={"request_id": 100},
        )
        second = enqueue_automation_event(
            db,
            company_id=1,
            event_name="booking.created",
            event_id="booking:100",
            source_type="action_request",
            source_id=100,
            payload={"request_id": 100},
        )
        db.commit()

        assert first.id == second.id
        assert db.query(AutomationEventOutbox).count() == 1
        row = db.query(AutomationEventOutbox).one()
        assert row.status == "pending"
        assert row.attempt_count == 0
        assert row.payload["request_id"] == 100

    engine.dispose()


def test_pending_outbox_event_dispatches_and_records_result(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = _factory(engine)
    monkeypatch.setattr(event_outbox_module, "SessionLocal", factory)

    with factory() as db:
        db.add(Company(id=1, name="Outbox Company", active=True))
        db.commit()
        item = enqueue_automation_event(
            db,
            company_id=1,
            event_name="lead.created",
            event_id="lead:1",
            source_type="action_request",
            source_id=1,
            payload={"lead_id": 1},
        )
        outbox_id = item.id
        db.commit()

    captured = []

    def fake_dispatch(**kwargs):
        captured.append(kwargs)
        return {
            "event": kwargs["event_name"],
            "matched": 1,
            "runs": [{"workflow_id": 7, "status": "success"}],
        }

    monkeypatch.setattr(
        event_dispatch_module,
        "dispatch_automation_event",
        fake_dispatch,
    )

    summary = event_outbox_module.dispatch_pending_automation_events_once()

    assert summary["checked"] == 1
    assert summary["dispatched"] == 1
    assert captured[0]["event_id"] == "lead:1"
    assert captured[0]["payload"]["lead_id"] == 1

    with factory() as db:
        row = db.query(AutomationEventOutbox).filter_by(id=outbox_id).one()
        assert row.status == "dispatched"
        assert row.attempt_count == 1
        assert row.dispatched_at is not None
        assert row.result["matched"] == 1

    engine.dispose()


def test_failed_outbox_dispatch_remains_retryable(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = _factory(engine)
    monkeypatch.setattr(event_outbox_module, "SessionLocal", factory)

    with factory() as db:
        db.add(Company(id=1, name="Outbox Company", active=True))
        db.commit()
        item = enqueue_automation_event(
            db,
            company_id=1,
            event_name="order.created",
            event_id="order:1",
            source_type="action_request",
            source_id=1,
            payload={"order_id": 1},
        )
        outbox_id = item.id
        db.commit()

    def fail_dispatch(**kwargs):
        raise RuntimeError("temporary event transport failure")

    monkeypatch.setattr(
        event_dispatch_module,
        "dispatch_automation_event",
        fail_dispatch,
    )

    result = event_outbox_module.dispatch_outbox_event(outbox_id)
    assert result["status"] == "pending"
    assert result["attempt_count"] == 1

    with factory() as db:
        row = db.query(AutomationEventOutbox).filter_by(id=outbox_id).one()
        assert row.status == "pending"
        assert row.last_error == "temporary event transport failure"
        assert row.available_at > datetime.now(UTC).replace(tzinfo=None)

    engine.dispose()
