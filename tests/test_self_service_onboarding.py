from inspect import signature
from pathlib import Path

from backend.app.api.customer_employee_builder import SELF_SERVICE_FREE_TEST_MESSAGES
from backend.app.models.company import Company
from backend.app.modules.ai_agent.employee_builder import build_employee_blueprint
from backend.app.modules.ai_agent.factory import AgentFactory


ROOT = Path(__file__).resolve().parents[1]


def test_self_service_build_preview_is_not_blocked_by_subscription():
    # Interactive free-test messages remain disabled; the initial compiler build itself is free.
    assert SELF_SERVICE_FREE_TEST_MESSAGES == 0
    api = (ROOT / "backend" / "app" / "api" / "customer_employee_builder.py").read_text(encoding="utf-8")
    assert "if not is_self_service_company(company):" in api


def test_company_source_defaults_to_managed_for_existing_manual_flow():
    column = Company.__table__.c.onboarding_source
    assert column.default.arg == "managed"
    assert column.nullable is False


def test_agent_factory_keeps_capacity_enforcement_by_default():
    parameter = signature(AgentFactory.create_custom_agent).parameters["enforce_capacity"]
    assert parameter.default is True


def test_open_ended_brief_is_preserved_even_when_internal_hints_match():
    description = (
        "راقب لي كل يوم صفحة مورد محدد، وقارن التغييرات مع ملاحظاتي، "
        "وخبرني فقط إذا صار تغيير مهم حسب الشروط التي أعطيك إياها"
    )
    blueprint = build_employee_blueprint(description)
    assert blueprint.description == description
    assert blueprint.capabilities


def test_public_builder_is_open_ended_and_creates_directly_without_preview():
    html = (ROOT / "frontend" / "public" / "employee-builder.html").read_text(
        encoding="utf-8"
    )
    assert "/public/employee-builder/preview" not in html
    assert "preview-btn" not in html
    assert "review-card" not in html
    assert "/customer/employee-builder/create" in html
    assert "/customer/employee-builder/employees" in html
    assert "Agent جديد" in html
    assert "selectedAgentId" in html
    assert "/auth/login" in html
    assert "/auth/signup" in html
    assert "sessionStorage" in html
    assert "ابنِ موظفي" in html
    assert "شو بدك موظفك يعمل؟" in html
    assert "/customer/employee-builder/${agentId}/compile" in html
    assert "مثل Replit بس للموظفين والـAgents" in html
    assert "Build status" in html
    assert 'id="employee-name"' not in html
    assert 'name="channel"' not in html
    assert 'name="capability"' not in html
    assert "خدمة العملاء" not in html
    assert "المبيعات" not in html


def test_customer_portal_treats_job_brief_as_source_of_truth():
    source = (
        ROOT / "frontend" / "customer" / "employee-builder.js"
    ).read_text(encoding="utf-8")
    assert 'href="/build"' in source
    assert "Build on Xvond.com" in source
    assert "Xvond Workspace" in source
    assert "شو طلبت من الموظف" in source
    assert "ما منعرض إعدادات عامة ما إلها علاقة بالوظيفة" in source
    assert "عدّل موظفك بالكلام" in source
    assert "Xvond builds this" in source
    assert "BUILD PROGRESS" in source
    assert "data-builder-action" in source
    assert "setup_website" in source
    assert "setup_whatsapp" in source
    assert "manage_knowledge" in source
    assert "Capabilities" not in source
    assert "/customer/employee-builder/create" not in source
    assert "/customer/employee-builder/preview" not in source
    assert "/customer/employee-builder/${agentId}/test" in source
    assert "Preview & Test" in source
    assert "employee-refine-instruction" in source
    assert "Version history" in source


def test_xvond_com_nginx_exposes_public_builder_without_replacing_landing():
    nginx = (ROOT / "ops" / "nginx" / "core-locations.conf").read_text(
        encoding="utf-8"
    )
    assert "Keep the existing landing" in nginx
    assert "|build" in nginx
    assert "public/" in nginx


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


def test_self_service_builder_is_a_first_class_portal_route():
    portal = (ROOT / "backend" / "app" / "api" / "customer_portal.py").read_text(
        encoding="utf-8"
    )
    app = (ROOT / "frontend" / "customer" / "app.js").read_text(
        encoding="utf-8"
    )
    session = (ROOT / "frontend" / "customer" / "session-security.js").read_text(
        encoding="utf-8"
    )

    assert '"id": "employee-builder"' in portal
    assert '"loader": "employee-builder"' in portal
    assert "is_self_service_workspace = company.onboarding_source == \"self_service\"" in portal
    assert "if is_self_service_workspace:" in portal
    assert "async function openInitialPortalPage()" in app
    assert 'requestedPage' in app
    assert 'selfServiceDraft' in app
    draft_block = app.split("const selfServiceDraft =", 1)[1].split(");", 1)[0]
    assert "summary?.agents" not in draft_block
    assert '"employee-builder"' in app
    assert "await openInitialPortalPage()" in session
