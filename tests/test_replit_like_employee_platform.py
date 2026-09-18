from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MANAGEMENT = (ROOT / "backend" / "app" / "api" / "customer_management.py").read_text(encoding="utf-8")
BUILDER = (ROOT / "backend" / "app" / "api" / "customer_employee_builder.py").read_text(encoding="utf-8")
BUILDER_UI = (ROOT / "frontend" / "customer" / "employee-builder.js").read_text(encoding="utf-8")
PORTAL = (ROOT / "frontend" / "customer" / "app.js").read_text(encoding="utf-8")
POLICY = (ROOT / "backend" / "app" / "modules" / "ai_agent" / "self_service_policy.py").read_text(encoding="utf-8")
WORKFLOW_TOOL = (ROOT / "backend" / "app" / "modules" / "tools" / "workflow_action_request.py").read_text(encoding="utf-8")


def test_customer_connected_systems_are_company_scoped_and_secret_safe():
    assert '@router.post("/integrations")' in MANAGEMENT
    assert '@router.patch("/integrations/{integration_id}")' in MANAGEMENT
    assert '@router.delete("/integrations/{integration_id}")' in MANAGEMENT
    assert "CompanyIntegration.company_id == company_id" in MANAGEMENT
    assert "public_config(item.config)" in MANAGEMENT
    assert "configured_secret_fields(item.config)" in MANAGEMENT
    assert "merge_config(item.config, payload.config)" in MANAGEMENT
    assert "reveal_config(merged)" in MANAGEMENT


def test_managed_customer_cannot_take_over_self_service_connection_setup():
    assert "Connected systems for Xvond Managed customers are configured by Xvond" in MANAGEMENT
    assert "_self_service_company(db, current_user)" in MANAGEMENT


def test_bound_connected_system_cannot_be_disabled_or_deleted_blindly():
    assert "def _integration_bound" in MANAGEMENT
    assert "This connected system is used by an AI employee" in MANAGEMENT
    assert 'destination.get("integration_id")' in MANAGEMENT


def test_external_requirement_has_customer_connect_and_bind_path():
    assert 'return "self_service_integration_available"' in POLICY
    assert '"connect_system"' in BUILDER
    assert '@router.post("/{agent_id}/connections/{requirement_key}")' in BUILDER
    assert "integration_operations" in BUILDER
    assert "provision_compiled_capabilities(" in BUILDER
    assert "data-bind-integration" in BUILDER_UI
    assert "/connections/${encodeURIComponent(key)}" in BUILDER_UI
    assert "/manage/integrations" in BUILDER_UI


def test_connected_systems_are_real_execution_not_read_only_metadata():
    assert '{"xvond_internal", "integration"}' in WORKFLOW_TOOL
    assert "Connected Systems" in PORTAL
    assert "/manage/integrations/catalog" in PORTAL
    assert "/manage/integrations" in PORTAL


def test_builder_has_replit_style_refinement_history_and_build_scoped_preview():
    assert '@router.post("/{agent_id}/refine")' in BUILDER
    assert '@router.get("/{agent_id}/versions")' in BUILDER
    assert '@router.post("/{agent_id}/rollback")' in BUILDER
    assert "OWNER REFINEMENT" in BUILDER
    assert "last_tested_compiled_at" in BUILDER
    assert "Test the current employee build before launch" in BUILDER
    assert "BUILDER_HISTORY_LIMIT = 20" in BUILDER
    assert "_snapshot_builder_version" in BUILDER
    assert "refineEmployee" in BUILDER_UI
    assert "data-rollback-version" in BUILDER_UI


def test_dynamic_setup_data_reprovisions_instead_of_only_being_saved():
    setup = BUILDER.split('def save_self_service_setup_answer(', 1)[1]
    assert 'item["status"] = next_status' in setup
    assert "provision_compiled_capabilities(" in setup
    assert 'builder["delivery"] = delivery' in setup
    assert "build_compiled_employee_system_prompt" in setup
