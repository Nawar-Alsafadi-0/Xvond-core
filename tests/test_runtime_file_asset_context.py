from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from backend.app.core.database.base import Base
from backend.app.models.company import Company
from backend.app.modules.ai_agent.models import AIAgent
from backend.app.modules.files.models import EmployeeFileAsset
from backend.app.modules.tools.executor import (
    _employee_file_asset_context,
    _runtime_description,
)


def test_runtime_file_asset_context_is_employee_scoped_and_metadata_only():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine, autoflush=False) as db:
        db.add(Company(id=7, name="Self Service"))
        db.add_all([
            AIAgent(
                id=21,
                company_id=7,
                name="Uploader",
                system_prompt="Upload files",
                provider="mock",
                model="mock",
                enabled=True,
            ),
            AIAgent(
                id=22,
                company_id=7,
                name="Other",
                system_prompt="Other",
                provider="mock",
                model="mock",
                enabled=True,
            ),
        ])
        db.add_all([
            EmployeeFileAsset(
                id=51,
                company_id=7,
                agent_id=21,
                filename="invoice.pdf",
                content_type="application/pdf",
                size_bytes=7,
                sha256="a" * 64,
                content=b"PDFDATA",
                enabled=True,
            ),
            EmployeeFileAsset(
                id=52,
                company_id=7,
                agent_id=22,
                filename="other.pdf",
                content_type="application/pdf",
                size_bytes=5,
                sha256="b" * 64,
                content=b"OTHER",
                enabled=True,
            ),
            EmployeeFileAsset(
                id=53,
                company_id=7,
                agent_id=21,
                filename="disabled.pdf",
                content_type="application/pdf",
                size_bytes=8,
                sha256="c" * 64,
                content=b"DISABLED",
                enabled=False,
            ),
        ])
        db.commit()

        config = {
            "actions": {
                "upload_invoice": {
                    "enabled": True,
                    "label": "Upload invoice",
                    "description": "Upload invoice to provider",
                    "module": "tools",
                    "confirmation_required": False,
                    "fields": [
                        {
                            "key": "file",
                            "label": "Invoice file",
                            "required": True,
                            "type": "file",
                        }
                    ],
                    "destination": {"type": "integration", "integration_id": 11},
                }
            }
        }

        assets = _employee_file_asset_context(db, 7, 21, config)

    engine.dispose()

    assert assets == [
        {
            "id": 51,
            "filename": "invoice.pdf",
            "content_type": "application/pdf",
        }
    ]


def test_runtime_description_exposes_exact_asset_id_without_file_content():
    tool = SimpleNamespace(
        name="action_request",
        description="Run configured action.",
    )
    config = {
        "actions": {
            "upload_invoice": {
                "enabled": True,
                "label": "Upload invoice",
                "description": "Upload invoice to provider",
                "module": "tools",
                "confirmation_required": False,
                "fields": [
                    {
                        "key": "file",
                        "label": "Invoice file",
                        "required": True,
                        "type": "file",
                    }
                ],
                "destination": {"type": "integration", "integration_id": 11},
            }
        }
    }

    description = _runtime_description(
        tool,
        config,
        file_assets=[
            {
                "id": 51,
                "filename": "invoice.pdf",
                "content_type": "application/pdf",
            }
        ],
    )

    assert "asset_id=51" in description
    assert "invoice.pdf" in description
    assert "application/pdf" in description
    assert "Never invent an asset id" in description
    assert "PDFDATA" not in description


def test_runtime_description_blocks_file_action_when_no_asset_exists():
    tool = SimpleNamespace(
        name="action_request",
        description="Run configured action.",
    )
    config = {
        "actions": {
            "upload_invoice": {
                "enabled": True,
                "fields": [
                    {
                        "key": "file",
                        "label": "Invoice file",
                        "required": True,
                        "type": "file",
                    }
                ],
                "destination": {"type": "integration", "integration_id": 11},
            }
        }
    }

    description = _runtime_description(tool, config, file_assets=[])

    assert "currently has no enabled file assets" in description
    assert "do not execute that file action" in description
