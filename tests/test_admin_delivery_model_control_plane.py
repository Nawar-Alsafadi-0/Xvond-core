from pathlib import Path


ADMIN_API = Path("backend/app/api/admin.py").read_text(encoding="utf-8")
COMPANY_VIEW = Path("backend/app/api/admin_company_view.py").read_text(encoding="utf-8")
SELF_SERVICE_API = Path("backend/app/api/customer_employee_builder.py").read_text(encoding="utf-8")
ADMIN_INDEX = Path("frontend/admin/index.html").read_text(encoding="utf-8")
ADMIN_DIRECTORY = Path("frontend/admin/company-onboarding-source.js").read_text(encoding="utf-8")
CONTROL_CENTER = Path("frontend/admin/company-control-center.js").read_text(encoding="utf-8")
POLISH = Path("frontend/admin/control-center-polish.js").read_text(encoding="utf-8")
MANAGED_ONBOARDING = Path("frontend/admin/company-onboarding-workflow.js").read_text(encoding="utf-8")
MANAGED_GO_LIVE = Path("frontend/admin/employee-go-live-controls.js").read_text(encoding="utf-8")
LIFECYCLE_UI = Path("frontend/admin/company-lifecycle-controls.js").read_text(encoding="utf-8")


def test_admin_has_first_class_managed_and_self_service_directories():
    assert "Managed Delivery" in ADMIN_INDEX
    assert "Self-Service" in ADMIN_INDEX
    assert 'id="company-search"' in ADMIN_INDEX
    assert 'id="company-lifecycle-filter"' in ADMIN_INDEX
    assert "showCompanyDirectory('managed'" in ADMIN_INDEX
    assert "showCompanyDirectory('self_service'" in ADMIN_INDEX


def test_company_directory_scales_server_side_without_breaking_legacy_callers():
    assert "source: str | None = None" in ADMIN_API
    assert "lifecycle: str | None = None" in ADMIN_API
    assert "search: str | None = None" in ADMIN_API
    assert "limit: int | None = None" in ADMIN_API
    assert "offset: int = 0" in ADMIN_API
    assert '"total": total' in ADMIN_API
    assert "new URLSearchParams()" in ADMIN_DIRECTORY
    assert 'params.set("limit"' in ADMIN_DIRECTORY
    assert 'params.set("offset"' in ADMIN_DIRECTORY


def test_company_workspace_exposes_delivery_source_and_canonical_self_service_readiness():
    assert '"onboarding_source": company.onboarding_source' in COMPANY_VIEW
    assert '"creation_source": (' in COMPANY_VIEW
    assert '"self_service_readiness": self_service_states.get(item.id)' in COMPANY_VIEW
    assert "self_service_readiness(" in COMPANY_VIEW
    assert "Self-Service Workspace" in CONTROL_CENTER
    assert "Managed Delivery Workspace" in CONTROL_CENTER
    assert "Self-Service Readiness" in POLISH


def test_admin_workspace_degrades_gracefully_when_noncritical_sections_fail():
    assert "const loadIssues=[]" in CONTROL_CENTER
    assert "view=await api(`/admin/company-view/${companyId}`,{signal:viewController.signal})" in CONTROL_CENTER
    assert "wsOptional(`/admin/channels/companies/${companyId}`" in CONTROL_CENTER
    assert "Admin data partially unavailable" in POLISH
    assert "workspace remains usable" in POLISH


def test_managed_delivery_gates_do_not_render_for_self_service_workspaces():
    guard = "onboarding_source||'managed')==='self_service'"
    assert guard in MANAGED_ONBOARDING
    assert guard in MANAGED_GO_LIVE
    assert "Self-Service Workspace State" in LIFECYCLE_UI
    assert "Customer launch uses the self-service readiness policy" in LIFECYCLE_UI


def test_self_service_launch_uses_canonical_company_lifecycle_value():
    assert 'company.lifecycle_status = "live"' in SELF_SERVICE_API
    assert 'company.lifecycle_status = "active"' not in SELF_SERVICE_API
