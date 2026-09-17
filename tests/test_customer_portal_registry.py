from pathlib import Path

from backend.app.modules.ai_agent.models import AIConversation
from backend.app.modules.solutions.portal import build_customer_portal_navigation


ROOT = Path(__file__).resolve().parents[1]


def _ids(items):
    return [item["id"] for item in items]


def test_ai_agents_portal_is_capability_aware():
    basic = build_customer_portal_navigation(["ai_agents"], [])
    assert _ids(basic) == [
        "dashboard",
        "business-profile",
        "employee-builder",
        "agents",
        "chat",
        "usage",
        "conversations",
        "customers",
        "business-analytics",
        "notifications",
        "account",
        "billing",
    ]
    assert next(item for item in basic if item["id"] == "business-profile")["group"] == "Company"
    assert next(item for item in basic if item["id"] == "employee-builder")["group"] == "AI Workforce"
    assert next(item for item in basic if item["id"] == "employee-builder")["loader"] == "employee-builder"
    assert next(item for item in basic if item["id"] == "agents")["group"] == "AI Workforce"
    assert next(item for item in basic if item["id"] == "conversations")["group"] == "Customer Operations"
    assert next(item for item in basic if item["id"] == "conversations")["label"] == "Inbox"
    assert next(item for item in basic if item["id"] == "customers")["loader"] == "customers"
    assert next(item for item in basic if item["id"] == "business-analytics")["loader"] == "customer-analytics"
    assert next(item for item in basic if item["id"] == "notifications")["loader"] == "customer-notifications"
    assert next(item for item in basic if item["id"] == "account")["group"] == "Account"

    quotation = build_customer_portal_navigation(
        ["ai_agents"],
        ["quotation"],
    )
    assert _ids(quotation) == [
        "dashboard",
        "business-profile",
        "employee-builder",
        "agents",
        "chat",
        "usage",
        "conversations",
        "customers",
        "requests-quotation",
        "business-analytics",
        "notifications",
        "account",
        "billing",
    ]
    quote_page = next(item for item in quotation if item["id"] == "requests-quotation")
    assert quote_page["capability_module"] == "quotation"
    assert quote_page["label"] == "Quotation Requests"
    assert quote_page["group"] == "Customer Operations"


def test_multiple_capabilities_create_separate_operation_pages():
    navigation = build_customer_portal_navigation(
        ["ai_agents"],
        ["quotation", "booking", "orders", "lead_management", "customer_support"],
    )
    ids = _ids(navigation)
    assert "employee-builder" in ids
    assert "requests-quotation" in ids
    assert "requests-booking" in ids
    assert "requests-orders" in ids
    assert "requests-leads" in ids
    assert "requests-support" in ids
    assert "business" not in ids
    for item in navigation:
        if item["id"].startswith("requests-"):
            assert item["group"] == "Customer Operations"


def test_portal_separates_active_services_and_keeps_account_core():
    navigation = build_customer_portal_navigation(
        ["automation", "analytics", "integrations"],
        [],
    )
    ids = _ids(navigation)
    assert ids == [
        "dashboard",
        "service-automation",
        "service-analytics",
        "integrations",
        "account",
        "billing",
    ]
    assert "employee-builder" not in ids
    assert "agents" not in ids
    assert "customers" not in ids
    assert "business-profile" not in ids


def test_conversations_have_generic_channel_source_fields():
    columns = AIConversation.__table__.c
    assert "channel_id" in columns
    assert "channel_type" in columns
    assert "external_contact_id" in columns


def test_customer_ui_renders_backend_navigation_and_unified_inbox():
    html = (ROOT / "frontend" / "customer" / "index.html").read_text(encoding="utf-8")
    js = (ROOT / "frontend" / "customer" / "app.js").read_text(encoding="utf-8")
    enhancements = (
        ROOT / "frontend" / "customer" / "portal-enhancements.js"
    ).read_text(encoding="utf-8")
    operations = (
        ROOT / "frontend" / "customer" / "customer-operations.js"
    ).read_text(encoding="utf-8")
    information_architecture = (
        ROOT / "frontend" / "customer" / "portal-information-architecture.js"
    ).read_text(encoding="utf-8")
    employee_builder = (
        ROOT / "frontend" / "customer" / "employee-builder.js"
    ).read_text(encoding="utf-8")
    api_source = (
        ROOT / "backend" / "app" / "api" / "customer_portal.py"
    ).read_text(encoding="utf-8")
    inbox_source = (
        ROOT / "backend" / "app" / "api" / "customer_inbox.py"
    ).read_text(encoding="utf-8")
    operations_api = (
        ROOT / "backend" / "app" / "api" / "customer_operations.py"
    ).read_text(encoding="utf-8")

    assert 'id="portal-nav"' in html
    assert 'id="page-business-profile"' in html
    assert 'id="customer-business-profile-content"' in html
    assert 'id="page-account"' in html
    assert 'id="page-billing"' in html
    assert "/static/customer/portal-enhancements.js" in html
    assert "/static/customer/customer-operations.js" in html
    assert "/static/customer/portal-information-architecture.js" in html
    assert "/static/customer/employee-builder.js" in html
    assert "portalOverview?.portal?.navigation" in js
    assert "renderPortalNavigation" in js
    assert "renderBilling" in js
    assert "renderBusinessProfilePage" in information_architecture
    assert "Business Information" in information_architecture
    assert "openBusinessProfileFromEmployee" in information_architecture
    assert "loadEmployeeBuilder" in employee_builder
    assert "/customer/employee-builder/preview" in employee_builder
    assert "/customer/employee-builder/create" in employee_builder
    assert "/customer/inbox" in enhancements
    assert "conversation-channel" in enhancements
    assert "capability_module" in enhancements
    assert "module=${encodeURIComponent(moduleName)}" in enhancements
    assert 'router = APIRouter(prefix="/customer/inbox"' in inbox_source
    assert 'prefix="/customer/operations"' in operations_api
    assert "/customer/operations/customers" in operations
    assert "/customer/operations/notifications" in operations
    assert "/customer/operations/analytics" in operations
    assert '"online_payments_enabled": False' in api_source
