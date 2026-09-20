import re
from pathlib import Path


ADMIN_DIR = Path("frontend/admin")
INDEX = ADMIN_DIR / "index.html"


def test_admin_scripts_are_loaded_once_and_workspace_is_consolidated():
    html = INDEX.read_text(encoding="utf-8-sig")
    scripts = re.findall(r'<script\s+src="([^"]+)"', html)
    normalized = [script.split("?", 1)[0] for script in scripts]

    assert len(scripts) == len(set(scripts))
    assert not any("pilot" in script for script in scripts)
    assert "/static/admin/company-control-center.js" in normalized
    assert "/static/admin/privacy-boundaries.js" in normalized
    assert normalized.index("/static/admin/privacy-boundaries.js") > normalized.index(
        "/static/admin/company-control-center.js"
    )
    assert normalized[-1] == "/static/admin/privacy-boundaries.js"
    assert "/static/admin/customer-operations.js" not in normalized
    assert "/static/admin/human-chat.js" not in normalized
    assert not any("company-control-center-runtime" in script for script in scripts)
    assert not any("employee-workspace" in script for script in scripts)
    assert not (ADMIN_DIR / "company-control-center-runtime.js").exists()
    assert not (ADMIN_DIR / "employee-workspace.js").exists()


def test_legacy_pilot_files_are_removed():
    assert not (ADMIN_DIR / "pilot.js").exists()
    assert not (ADMIN_DIR / "pilot_upgrade.js").exists()


def test_obsolete_admin_workspace_files_are_removed():
    obsolete = {
        "business.js",
        "company_workspace.js",
        "company_workspace_automation.js",
        "company_workspace_plans.js",
        "customer-operations.js",
        "human-chat.js",
        "operations.js",
        "services.js",
        "solutions.js",
    }
    for filename in obsolete:
        assert not (ADMIN_DIR / filename).exists(), filename


def test_ai_employee_profile_does_not_duplicate_company_or_fixed_operations():
    legacy = (ADMIN_DIR / "simple-company.js").read_text(encoding="utf-8-sig")
    profile = (ADMIN_DIR / "employee-capabilities.js").read_text(encoding="utf-8-sig")
    workspace = (ADMIN_DIR / "company-control-center.js").read_text(encoding="utf-8-sig")

    assert "simple-business-name" not in legacy
    assert "simple-business-type" not in legacy
    assert "simple-booking-system" not in legacy
    assert "simple-order-system" not in legacy
    assert "Booking System" not in profile
    assert "Xvond Orders" not in profile
    assert "Information → Knowledge → Actions → Channels → Conversations" in workspace


def test_company_profile_uses_canonical_catalog_and_service_billing():
    workspace = (ADMIN_DIR / "company-control-center.js").read_text(encoding="utf-8-sig")
    assert "p.catalog" in workspace
    assert "cp-type" in workspace and "<select" in workspace
    assert "/admin/service-billing/companies/" in workspace
    assert "/admin/billing/" not in workspace


def test_admin_shell_does_not_expose_legacy_agent_factory():
    app = (ADMIN_DIR / "app.js").read_text(encoding="utf-8-sig")
    index = INDEX.read_text(encoding="utf-8-sig")
    assert "agent-factory" not in app
    assert "openCreateAgentFromTemplate" not in app
    assert "/static/admin/services.js" not in index
    assert "/static/admin/business.js" not in index


def test_admin_privacy_boundary_keeps_customer_content_out_of_operator_ui():
    privacy = (ADMIN_DIR / "privacy-boundaries.js").read_text(encoding="utf-8-sig")

    assert "company_profile_ready" in privacy
    assert "Real Customer Operations" in privacy
    assert "requests: []" in privacy
    assert "conversations: []" in privacy
    assert "handoffs: []" in privacy
    assert "openHumanTakeover" in privacy

    # The privacy-aware loaders must not request tenant customer payloads.
    assert "/admin/agent-actions/companies/${companyId}/requests" not in privacy
    assert "/admin/operations/companies/${companyId}/conversations" not in privacy
    assert "/admin/handoff/companies/${companyId}/sessions" not in privacy

    # Admin keeps a privacy-safe technical reconciliation console.
    assert "/admin/operations/companies/${companyId}/external-unresolved" in privacy
    assert "renderPrivacySafeOperations" in privacy
    assert "reconcilePrivacySafeOperation" in privacy
    assert "Customer payloads remain inside the tenant workspace" in privacy


def test_admin_privacy_loader_keeps_company_open_bounded_and_lazy():
    privacy = (ADMIN_DIR / "privacy-boundaries.js").read_text(encoding="utf-8-sig")

    assert "loadPrivacyAwareAgentMetadata" in privacy
    assert "hydratePrivacyAwareWorkspaceTab" in privacy
    assert "loadedTabs: new Set(['overview'])" in privacy
    assert "viewController.abort()" in privacy
    assert "await hydrateWorkspaceTab(xvondWorkspace.tab)" in privacy


def test_admin_polish_contains_only_operator_safe_attention_data():
    polish = (ADMIN_DIR / "control-center-polish.js").read_text(encoding="utf-8-sig")
    assert "xvondCustomerOps" not in polish
    assert "renderCustomersTab" not in polish
    assert "renderNotificationsTab" not in polish
    assert "renderConversationsTab" not in polish
    assert "openHumanConversation" not in polish
    assert "Customer content is intentionally excluded" in polish
    assert "external operation" in polish


def test_admin_workspace_uses_canonical_company_lifecycle_transition():
    privacy = (ADMIN_DIR / "privacy-boundaries.js").read_text(encoding="utf-8-sig")
    assert "/admin/companies/${xvondWorkspace.companyId}/status" in privacy
    assert "toggleCanonicalWorkspaceCompany" in privacy
    canonical_section = privacy.split("toggleCanonicalWorkspaceCompany", 1)[1].split(
        "renderCompanyControlCenter", 1
    )[0]
    assert "/admin/production/" not in canonical_section


def test_admin_action_editor_preserves_intentional_state_mutations():
    privacy = (ADMIN_DIR / "privacy-boundaries.js").read_text(encoding="utf-8-sig")

    assert "renderAgentActionsAfterStateMutation" in privacy
    assert "skipCollectAfterMutation" in privacy
    assert "addCustomAgentAction = function" in privacy
    assert "removeAgentAction = function" in privacy
    assert "applySuggestedAgentActionTemplate = function" in privacy
