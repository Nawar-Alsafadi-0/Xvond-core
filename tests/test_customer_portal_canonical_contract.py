from pathlib import Path

from backend.app.api import admin_ai_employee_knowledge, admin_company_profile
from backend.app.api import customer_management


ROOT = Path(__file__).resolve().parents[1]


def test_customer_management_reuses_canonical_admin_business_profile_functions():
    assert customer_management.get_company_profile is admin_company_profile.get_company_profile
    assert customer_management.update_company_profile is admin_company_profile.update_company_profile


def test_customer_management_reuses_canonical_admin_knowledge_functions():
    assert customer_management.list_employee_knowledge is admin_ai_employee_knowledge.list_employee_knowledge
    assert customer_management.get_employee_knowledge is admin_ai_employee_knowledge.get_employee_knowledge
    assert customer_management.create_employee_knowledge is admin_ai_employee_knowledge.create_employee_knowledge
    assert customer_management.update_employee_knowledge is admin_ai_employee_knowledge.update_employee_knowledge
    assert customer_management.toggle_employee_knowledge is admin_ai_employee_knowledge.toggle_employee_knowledge
    assert customer_management.delete_employee_knowledge is admin_ai_employee_knowledge.delete_employee_knowledge
    assert customer_management.ingest_website_knowledge is admin_ai_employee_knowledge.ingest_website_knowledge


def test_customer_portal_loads_professional_and_canonical_assets_last():
    html = (ROOT / "frontend/customer/index.html").read_text(encoding="utf-8")
    assert "portal-pro.css?v=20260905-4" in html
    assert "manager-knowledge-controls.js?v=20260905-3" in html
    assert "portal-canonical.js?v=20260905-4" in html
    assert html.index("session-security.js?v=20260918-journey1") < html.index("portal-canonical.js?v=20260905-4")


def test_canonical_portal_layer_does_not_store_bearer_tokens():
    script = (ROOT / "frontend/customer/portal-canonical.js").read_text(encoding="utf-8")
    assert "localStorage.setItem" not in script
    assert "Authorization" not in script
    assert 'api("/customer/overview")' in script
    assert 'api(`/customer/agents/${customerManagedAgentId}`)' in script

def test_customer_portal_enhanced_navigation_loads_channels():
    script = (ROOT / "frontend/customer/portal-enhancements.js").read_text(encoding="utf-8")
    html = (ROOT / "frontend/customer/index.html").read_text(encoding="utf-8")
    assert 'loader === "channels"' in script
    assert "renderXvondChannelCenter()" in script
    assert "portal-enhancements.js?v=20260921-channels1" in html

