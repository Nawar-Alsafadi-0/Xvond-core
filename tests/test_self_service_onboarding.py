from inspect import signature
from pathlib import Path

from backend.app.api.public_employee_builder import (
    PublicEmployeePreviewRequest,
    preview_employee,
)
from backend.app.models.company import Company
from backend.app.modules.ai_agent.factory import AgentFactory


ROOT = Path(__file__).resolve().parents[1]


def test_public_preview_is_rule_based_and_free():
    result = preview_employee(
        PublicEmployeePreviewRequest(
            description="بدي موظف يرد على العملاء ويتابع المبيعات على واتساب"
        )
    )
    assert result["lifecycle"] == "preview"
    assert result["ai_usage"] is False
    assert "customer_support" in result["blueprint"]["capabilities"]
    assert "whatsapp" in result["blueprint"]["channels"]


def test_company_source_defaults_to_managed_for_existing_manual_flow():
    column = Company.__table__.c.onboarding_source
    assert column.default.arg == "managed"
    assert column.nullable is False


def test_agent_factory_keeps_capacity_enforcement_by_default():
    parameter = signature(AgentFactory.create_custom_agent).parameters["enforce_capacity"]
    assert parameter.default is True


def test_public_builder_gates_creation_not_preview():
    html = (ROOT / "frontend" / "public" / "employee-builder.html").read_text(
        encoding="utf-8"
    )
    assert "/public/employee-builder/preview" in html
    assert "/customer/employee-builder/create" in html
    assert "/auth/login" in html
    assert "/auth/signup" in html
    assert "sessionStorage" in html
    assert "ai_usage" not in html  # UI never calls an AI endpoint for preview.


def test_signup_and_admin_source_separation_are_explicit():
    auth_source = (ROOT / "backend" / "app" / "api" / "auth.py").read_text(
        encoding="utf-8"
    )
    admin_source = (ROOT / "backend" / "app" / "api" / "admin.py").read_text(
        encoding="utf-8"
    )
    admin_html = (ROOT / "frontend" / "admin" / "index.html").read_text(
        encoding="utf-8"
    )
    admin_js = (
        ROOT / "frontend" / "admin" / "company-onboarding-source.js"
    ).read_text(encoding="utf-8")

    assert 'onboarding_source="self_service"' in auth_source
    assert 'onboarding_source="managed"' in admin_source
    assert 'id="company-source-filter"' in admin_html
    assert "Managed by Xvond" in admin_html
    assert "Self-service" in admin_html
    assert "self_service" in admin_js


def test_self_service_portal_visibility_does_not_fake_paid_entitlement():
    source = (ROOT / "backend" / "app" / "api" / "customer_portal.py").read_text(
        encoding="utf-8"
    )
    assert "has_self_service_employee" in source
    assert "portal_service_codes" in source
    assert '"active_services": active_service_codes' in source
