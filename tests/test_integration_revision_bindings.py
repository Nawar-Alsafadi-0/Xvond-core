import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from backend.app.main import app  # noqa: F401 - register model metadata
from backend.app.api import customer_management as management
from backend.app.core.database.base import Base
from backend.app.models.company import Company
from backend.app.modules.ai_agent.factory_models import AgentConfig
from backend.app.modules.ai_agent.models import AIAgent


def _engine():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    return engine


def test_pending_revision_binding_invalidates_only_pending_preview():
    engine = _engine()
    with Session(engine, autoflush=False) as db:
        db.add(Company(id=1, name="Owner", active=True))
        db.add(
            AIAgent(
                id=1,
                company_id=1,
                name="Employee",
                provider="mock",
                model="mock",
                system_prompt="live",
                enabled=True,
            )
        )
        db.flush()
        db.add(
            AgentConfig(
                agent_id=1,
                agent_type="employee",
                capabilities={},
                settings={
                    "employee_builder": {
                        "compiled_at": "live-build",
                        "last_tested_at": "live-test",
                        "last_tested_compiled_at": "live-build",
                        "compiled_spec": {
                            "requirements": [],
                        },
                        "pending_revision": {
                            "compiled_at": "pending-build",
                            "last_tested_at": "pending-test",
                            "last_tested_compiled_at": "pending-build",
                            "status": "tested",
                            "compiled_spec": {
                                "requirements": [
                                    {
                                        "key": "email_send",
                                        "integration_id": 7,
                                    }
                                ]
                            },
                        },
                    }
                },
            )
        )
        db.commit()

        assert management._integration_bound(
            db,
            company_id=1,
            integration_id=7,
        ) is True

        changed = management._invalidate_bound_integration_previews(
            db,
            company_id=1,
            integration_id=7,
        )
        assert changed == 1
        db.flush()

        builder = db.query(AgentConfig).filter_by(agent_id=1).one().settings[
            "employee_builder"
        ]
        assert builder["last_tested_at"] == "live-test"
        assert builder["last_tested_compiled_at"] == "live-build"
        pending = builder["pending_revision"]
        assert "last_tested_at" not in pending
        assert "last_tested_compiled_at" not in pending
        assert pending["status"] == "built"

    engine.dispose()


def test_live_binding_still_blocks_connection_mutation():
    engine = _engine()
    with Session(engine, autoflush=False) as db:
        db.add(Company(id=1, name="Owner", active=True))
        db.add(
            AIAgent(
                id=1,
                company_id=1,
                name="Employee",
                provider="mock",
                model="mock",
                system_prompt="live",
                enabled=True,
            )
        )
        db.flush()
        db.add(
            AgentConfig(
                agent_id=1,
                agent_type="employee",
                capabilities={},
                settings={
                    "employee_builder": {
                        "compiled_spec": {
                            "requirements": [
                                {
                                    "key": "email_send",
                                    "integration_id": 7,
                                }
                            ]
                        }
                    }
                },
            )
        )
        db.commit()

        with pytest.raises(HTTPException) as exc:
            management._invalidate_bound_integration_previews(
                db,
                company_id=1,
                integration_id=7,
            )
        assert exc.value.status_code == 409
        assert "live employee" in str(exc.value.detail).lower()

    engine.dispose()
