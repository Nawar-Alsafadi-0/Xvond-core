from pathlib import Path


ADMIN_APP = Path("frontend/admin/app.js").read_text(encoding="utf-8")
ADMIN_INDEX = Path("frontend/admin/index.html").read_text(encoding="utf-8")
LIFECYCLE_UI = Path("frontend/admin/company-lifecycle-controls.js").read_text(encoding="utf-8")
COMPANY_VIEW = Path("backend/app/api/admin_company_view.py").read_text(encoding="utf-8")


def test_company_list_separates_lifecycle_from_runtime():
    assert "<th>Lifecycle</th>" in ADMIN_INDEX
    assert "<th>AI Runtime</th>" in ADMIN_INDEX
    assert "company.lifecycle_status" in ADMIN_APP
    assert "company.active?'Running':'Stopped'" in ADMIN_APP


def test_company_workspace_exposes_canonical_lifecycle_controls():
    assert "/admin/companies/${companyId}/lifecycle" in LIFECYCLE_UI
    assert "onboarding" in LIFECYCLE_UI
    assert "testing" in LIFECYCLE_UI
    assert "live" in LIFECYCLE_UI
    assert "paused" in LIFECYCLE_UI
    assert "suspended" in LIFECYCLE_UI
    assert "cancelled" in LIFECYCLE_UI
    assert "archived" in LIFECYCLE_UI
    assert "Emergency Stop" in LIFECYCLE_UI
    assert "/admin/companies/${companyId}/status" in LIFECYCLE_UI


def test_company_view_exposes_lifecycle_source_of_truth():
    assert '"lifecycle_status": company.lifecycle_status' in COMPANY_VIEW
    assert '"lifecycle_updated_at": company.lifecycle_updated_at' in COMPANY_VIEW


def test_lifecycle_controls_are_loaded_after_company_control_center():
    company_script = ADMIN_INDEX.index("company-control-center.js")
    lifecycle_script = ADMIN_INDEX.index("company-lifecycle-controls.js")
    assert company_script < lifecycle_script


def test_lifecycle_mutation_observer_does_not_rewrite_its_own_panel_forever():
    guard = "if(hero.querySelector('[data-xvond-lifecycle-controls]'))return;"
    observer = "new MutationObserver(()=>xvondInjectLifecycleControls())"

    assert observer in LIFECYCLE_UI
    assert guard in LIFECYCLE_UI
    assert LIFECYCLE_UI.index(guard) < LIFECYCLE_UI.index("panel.innerHTML=")
