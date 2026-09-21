from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.app.api import customer_management
from backend.app.api.admin_ai_employee_knowledge import _protected_title
from backend.app.api.customer_agents import router as customer_agents_router
from backend.app.core.dependencies import require_customer_user
from backend.app.models.user import User


ROOT = Path(__file__).resolve().parents[1]
CUSTOMER_MANAGEMENT = ROOT / "backend" / "app" / "api" / "customer_management.py"
CUSTOMER_AGENTS = ROOT / "backend" / "app" / "api" / "customer_agents.py"
MANAGER_KNOWLEDGE_UI = ROOT / "frontend" / "customer" / "manager-knowledge-controls.js"
PORTAL_HTML = ROOT / "frontend" / "customer" / "index.html"
MAIN_APP = ROOT / "backend" / "app" / "main.py"


def _user(role: str, company_id: int = 7) -> User:
    return User(
        id=100,
        company_id=company_id,
        email=f"{role}@example.com",
        full_name=role.title(),
        password_hash="test",
        role=role,
        active=True,
        token_version=0,
    )


def _customer_test_app(user: User) -> FastAPI:
    app = FastAPI()
    app.include_router(customer_agents_router)
    app.dependency_overrides[require_customer_user] = lambda: user
    return app


def test_staff_is_forbidden_from_customer_business_information():
    client = TestClient(_customer_test_app(_user("employee")))
    response = client.get("/customer/agents/manage/business-information")
    assert response.status_code == 403
    assert response.json()["detail"] == "Company management access required"


def test_manager_route_is_real_and_uses_company_from_authenticated_session(monkeypatch):
    captured = {}

    def fake_get_company_profile(company_id, current_admin):
        captured["company_id"] = company_id
        captured["role"] = current_admin.role
        return {"company_id": company_id, "company_name": "Tenant Seven", "catalog": {}}

    monkeypatch.setattr(customer_management, "get_company_profile", fake_get_company_profile)
    client = TestClient(_customer_test_app(_user("manager", company_id=7)))
    response = client.get("/customer/agents/manage/business-information")
    assert response.status_code == 200
    assert response.json()["company_id"] == 7
    assert captured == {"company_id": 7, "role": "manager"}


def test_manager_knowledge_route_passes_session_company_and_requested_agent_only(monkeypatch):
    captured = {}

    def fake_list_employee_knowledge(company_id, agent_id, current_admin):
        captured.update(company_id=company_id, agent_id=agent_id, role=current_admin.role)
        return {"items": []}

    monkeypatch.setattr(customer_management, "list_employee_knowledge", fake_list_employee_knowledge)
    client = TestClient(_customer_test_app(_user("manager", company_id=22)))
    response = client.get("/customer/agents/manage/91/knowledge")
    assert response.status_code == 200
    assert captured == {"company_id": 22, "agent_id": 91, "role": "manager"}


def test_canonical_business_knowledge_title_remains_protected():
    assert _protected_title("Business Information") is True
    assert _protected_title("Business Profile") is True
    assert _protected_title("Menu") is False


def test_customer_management_is_manager_only_and_company_scoped_from_session():
    source = CUSTOMER_MANAGEMENT.read_text(encoding="utf-8")
    assert source.count("Depends(require_customer_manager)") >= 9
    assert "return user.company_id" in source
    assert "company_id=_company_id(current_user)" in source
    assert 'router = APIRouter(prefix="/manage"' in source


def test_customer_business_information_reuses_canonical_company_profile_sync():
    source = CUSTOMER_MANAGEMENT.read_text(encoding="utf-8")
    assert "get_company_profile" in source
    assert "update_company_profile" in source


def test_customer_knowledge_reuses_existing_secure_knowledge_pipeline():
    source = CUSTOMER_MANAGEMENT.read_text(encoding="utf-8")
    assert "create_employee_knowledge" in source
    assert "update_employee_knowledge" in source
    assert "toggle_employee_knowledge" in source
    assert "delete_employee_knowledge" in source
    assert "ingest_website_knowledge" in source
    assert "upload_pdf_knowledge" in source


def test_customer_management_router_is_nested_under_registered_customer_agents_router():
    source = CUSTOMER_AGENTS.read_text(encoding="utf-8")
    assert "from backend.app.api.customer_management import router as customer_management_router" in source
    assert "router.include_router(customer_management_router)" in source


def test_manager_ui_exposes_safe_business_and_knowledge_controls():
    source = MANAGER_KNOWLEDGE_UI.read_text(encoding="utf-8")
    assert 'managerTabButton("Behavior", "behavior")' in source
    assert 'managerTabButton("Business Information", "business")' in source
    assert 'managerTabButton("Knowledge", "knowledge")' in source
    assert "/customer/agents/manage/business-information" in source
    assert "businessTypeOptions" in source
    assert "catalog?.business_types" in source
    assert "collectWorkingHours" in source
    assert "linesToStructuredList" in source
    assert 'credentials: "same-origin"' in source
    assert "/knowledge/url" in source
    assert "/knowledge/pdf" in source
    assert "provider" not in source.lower()
    assert "model" not in source.lower()


def test_manager_knowledge_ui_loads_after_base_manager_controls_before_session_start():
    html = PORTAL_HTML.read_text(encoding="utf-8")
    base = html.index("/static/customer/manager-controls.js")
    knowledge = html.index("/static/customer/manager-knowledge-controls.js")
    session = html.index("/static/customer/session-security.js")
    assert base < knowledge < session
    assert "manager-knowledge-controls.js?v=20260921-employee-control1" in html


def test_customer_portal_entrypoint_version_is_bumped_for_new_manager_ui():
    source = MAIN_APP.read_text(encoding="utf-8")
    assert 'CUSTOMER_PORTAL_VERSION = "20260917-selfservice1"' in source
