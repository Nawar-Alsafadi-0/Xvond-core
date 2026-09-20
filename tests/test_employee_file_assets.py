import asyncio
from io import BytesIO
from types import SimpleNamespace

from fastapi import UploadFile
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from starlette.datastructures import Headers

from backend.app.api import customer_employee_builder as api
from backend.app.core.database.base import Base
from backend.app.models.company import Company
from backend.app.modules.ai_agent.models import AIAgent
from backend.app.modules.files.models import EmployeeFileAsset
from backend.app.modules.integrations.models import CompanyIntegration
from backend.app.modules.tools import action_request as action_runtime


def database_factory():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    return engine, lambda: Session(engine, autoflush=False)


def seed_company_and_agents(factory):
    with factory() as db:
        db.add(
            Company(
                id=7,
                name="Self Service",
                active=False,
                lifecycle_status="onboarding",
                onboarding_source="self_service",
            )
        )
        db.add(
            AIAgent(
                id=21,
                company_id=7,
                name="File employee",
                system_prompt="Use files",
                provider="mock",
                model="mock",
                enabled=False,
            )
        )
        db.add(
            AIAgent(
                id=22,
                company_id=7,
                name="Other employee",
                system_prompt="Other",
                provider="mock",
                model="mock",
                enabled=False,
            )
        )
        db.commit()


def test_self_service_file_upload_is_bounded_owned_and_deduplicated(monkeypatch):
    engine, factory = database_factory()
    seed_company_and_agents(factory)
    monkeypatch.setattr(api, "SessionLocal", factory)
    user = SimpleNamespace(company_id=7)

    def upload():
        return UploadFile(
            file=BytesIO(b"hello file"),
            filename="../report.txt",
            headers=Headers({"content-type": "text/plain"}),
        )

    first = asyncio.run(api.upload_employee_file_asset(21, upload(), user))
    second = asyncio.run(api.upload_employee_file_asset(21, upload(), user))

    assert first["status"] == "uploaded"
    assert first["filename"] == "report.txt"
    assert first["size_bytes"] == len(b"hello file")
    assert second["status"] == "existing"
    assert second["id"] == first["id"]

    with factory() as db:
        row = db.get(EmployeeFileAsset, first["id"])
        assert row.company_id == 7
        assert row.agent_id == 21
        assert row.content == b"hello file"
        assert row.enabled is True

    engine.dispose()


def test_binary_multipart_resolves_only_same_employee_owned_asset(monkeypatch):
    engine, factory = database_factory()
    seed_company_and_agents(factory)
    with factory() as db:
        db.add(
            EmployeeFileAsset(
                id=51,
                company_id=7,
                agent_id=21,
                filename="document.pdf",
                content_type="application/pdf",
                size_bytes=7,
                sha256="a" * 64,
                content=b"PDFDATA",
                enabled=True,
            )
        )
        db.add(
            CompanyIntegration(
                id=11,
                company_id=7,
                integration_type="custom_api",
                name="Upload API",
                config={
                    "base_url": "https://api.example.com",
                    "auth_type": "none",
                    "_xvond_validation": {
                        "validated": True,
                        "validated_at": "2026-09-20T00:00:00Z",
                    },
                },
                enabled=True,
            )
        )
        db.commit()

    captured = {}
    monkeypatch.setattr(action_runtime, "validate_public_http_url", lambda url: url)

    def fake_request(**kwargs):
        captured.update(kwargs)
        return {"status_code": 200, "response": '{"ok":true}', "truncated": False}

    monkeypatch.setattr(action_runtime, "safe_http_request", fake_request)
    operation = {
        "method": "POST",
        "endpoint": "/upload",
        "input_mode": "multipart",
        "required_form_fields": ["file", "label"],
        "form_fields": [
            {
                "key": "file",
                "required": True,
                "type": "string",
                "format": "binary",
            },
            {"key": "label", "required": True, "type": "string"},
        ],
    }

    with factory() as db:
        result = action_runtime._integration_call(
            db,
            {"company_id": 7, "agent_id": 21},
            "upload_document",
            {
                "destination": {
                    "type": "integration",
                    "integration_id": 11,
                    "operations": {"execute": operation},
                }
            },
            {"details": {"file": 51, "label": "invoice", "extra": "ignored"}},
            "execute",
            idempotency_key="file-upload-1",
        )

    assert result.success is True
    assert captured["multipart_data"] == {"label": "invoice"}
    assert captured["multipart_files"] == {
        "file": ("document.pdf", b"PDFDATA", "application/pdf")
    }

    with factory() as db:
        wrong_employee = action_runtime._integration_call(
            db,
            {"company_id": 7, "agent_id": 22},
            "upload_document",
            {
                "destination": {
                    "type": "integration",
                    "integration_id": 11,
                    "operations": {"execute": operation},
                }
            },
            {"details": {"file": 51, "label": "invoice"}},
            "execute",
            idempotency_key="file-upload-2",
        )

    assert wrong_employee.success is False
    assert "was not found" in wrong_employee.error

    engine.dispose()


def test_safe_http_request_builds_scalar_and_binary_multipart(monkeypatch):
    from backend.app.core import http_security

    captured = {}

    class Response:
        status_code = 200
        headers = {}

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def iter_bytes(self):
            yield b"{}"

    class Client:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def stream(self, method, url, **kwargs):
            captured.update(kwargs)
            return Response()

    monkeypatch.setattr(http_security, "validate_public_http_url", lambda url: url)
    monkeypatch.setattr(http_security.httpx, "Client", Client)

    http_security.safe_http_request(
        "https://api.example.com/upload",
        method="POST",
        multipart_data={"label": "invoice"},
        multipart_files={
            "file": ("document.pdf", b"PDFDATA", "application/pdf"),
        },
    )

    assert captured["data"] is None
    assert captured["json"] is None
    assert captured["files"] == {
        "label": (None, "invoice"),
        "file": ("document.pdf", b"PDFDATA", "application/pdf"),
    }
