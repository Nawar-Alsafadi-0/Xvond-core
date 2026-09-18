from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from backend.app.main import app  # noqa: F401 - register model metadata
from backend.app.api import customer_action_requests as api
from backend.app.core.database.base import Base
from backend.app.models.company import Company
from backend.app.modules.ai_agent.models import AIAgent
from backend.app.modules.audit.models import AuditLog
from backend.app.modules.tools.business_models import ActionRequest


USER = SimpleNamespace(id=10, company_id=1)
OTHER_USER = SimpleNamespace(id=20, company_id=2)


@pytest.fixture
def database(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = lambda: Session(engine, autoflush=False)
    monkeypatch.setattr(api, "SessionLocal", factory)

    with factory() as db:
        db.add_all(
            [
                Company(id=1, name="Owner", active=True),
                Company(id=2, name="Other", active=True),
            ]
        )
        db.flush()
        db.add_all(
            [
                AIAgent(
                    id=1,
                    company_id=1,
                    name="Employee",
                    provider="mock",
                    model="mock",
                    system_prompt="prompt",
                    enabled=True,
                ),
                AIAgent(
                    id=2,
                    company_id=2,
                    name="Other employee",
                    provider="mock",
                    model="mock",
                    system_prompt="prompt",
                    enabled=True,
                ),
            ]
        )
        db.flush()
        db.add_all(
            [
                ActionRequest(
                    id=11,
                    company_id=1,
                    agent_id=1,
                    conversation_id=None,
                    action_type="email_send",
                    summary="Send follow-up",
                    status="external_failed",
                    details={
                        "to": "customer@example.com",
                        "subject": "Follow-up",
                        "_xvond_execution": {
                            "state": "external_failed",
                            "operation": "execute",
                            "error": "provider timeout",
                            "key": "secret-internal-key",
                        },
                    },
                ),
                ActionRequest(
                    id=12,
                    company_id=2,
                    agent_id=2,
                    conversation_id=None,
                    action_type="email_send",
                    summary="Other tenant",
                    status="external_failed",
                    details={
                        "to": "other@example.com",
                        "_xvond_execution": {"state": "external_failed"},
                    },
                ),
            ]
        )
        db.commit()

    yield factory
    engine.dispose()


def test_customer_unresolved_operations_are_tenant_scoped_and_hide_internal_metadata(database):
    result = api.unresolved_external_requests(current_user=USER)

    assert result["count"] == 1
    item = result["requests"][0]
    assert item["id"] == 11
    assert item["details"] == {
        "to": "customer@example.com",
        "subject": "Follow-up",
    }
    assert item["external_execution"]["state"] == "external_failed"
    assert item["external_execution"]["reconciliation_required"] is True
    assert "key" not in item["external_execution"]


def test_customer_can_confirm_external_operation_executed(database):
    result = api.reconcile_external_request(
        11,
        api.ReconcileExternalOperation(
            outcome="executed",
            note="Confirmed in provider dashboard",
        ),
        USER,
    )

    assert result["status"] == "confirmed"
    assert result["external_execution"]["state"] == "confirmed"
    assert result["external_execution"]["reconciliation_required"] is False

    factory = database
    with factory() as db:
        row = db.get(ActionRequest, 11)
        assert row.status == "confirmed"
        assert row.details["_xvond_reconciliation"]["outcome"] == "executed"
        audit = db.query(AuditLog).filter(
            AuditLog.company_id == 1,
            AuditLog.action == "customer.external_operation_reconciled",
        ).one()
        assert audit.resource_id == "11"


def test_customer_not_executed_returns_request_to_safe_new_state(database):
    result = api.reconcile_external_request(
        11,
        api.ReconcileExternalOperation(outcome="not_executed"),
        USER,
    )

    assert result["status"] == "new"
    assert result["external_execution"]["state"] == "reconciled_not_executed"


def test_customer_cannot_reconcile_another_tenants_operation(database):
    with pytest.raises(HTTPException) as exc:
        api.reconcile_external_request(
            12,
            api.ReconcileExternalOperation(outcome="executed"),
            USER,
        )
    assert exc.value.status_code == 404


def test_normal_status_edit_cannot_bypass_unresolved_external_reconciliation(database):
    with pytest.raises(HTTPException) as exc:
        api.update_status(
            11,
            api.StatusUpdate(status="completed"),
            USER,
        )
    assert exc.value.status_code == 409
    assert "reconciled" in str(exc.value.detail).lower()
