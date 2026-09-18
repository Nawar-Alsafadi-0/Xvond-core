from pathlib import Path


APP = Path("frontend/admin/app.js").read_text(encoding="utf-8")
EMPLOYEE = Path("frontend/admin/employee-capabilities.js").read_text(encoding="utf-8")
GUIDE = Path("frontend/admin/ai-employee-setup-guide.js").read_text(encoding="utf-8")
PROFILE_API = Path("backend/app/api/admin_ai_employee_profile.py").read_text(encoding="utf-8")
READINESS_API = Path("backend/app/api/admin_delivery_readiness.py").read_text(encoding="utf-8")
MAIN = Path("backend/app/main.py").read_text(encoding="utf-8")


def test_customer_delivery_flow_has_company_creation_entrypoint():
    assert 'api("/admin/companies",{method:"POST"' in APP
    assert "await openCompany(data.company.id)" in APP


def test_customer_delivery_flow_has_canonical_employee_creation_in_draft():
    assert "openAddAIEmployee(companyId)" in APP
    assert 'api(`/admin/ai-employee-profile/companies/${simpleCompanyId}`' in EMPLOYEE
    assert '@router.post("/companies/{company_id}")' in PROFILE_API
    assert 'for module_name in ("ai_agent", "knowledge", "tools")' in PROFILE_API
    assert "enabled=False" in PROFILE_API
    assert '"lifecycle": "draft"' in PROFILE_API


def test_customer_delivery_flow_requires_knowledge_and_configured_channel_before_setup_ready():
    assert 'setup_blockers.append("Attach at least one enabled knowledge source")' in READINESS_API
    assert 'setup_blockers.append("Connect and configure at least one customer channel")' in READINESS_API
    assert "openKnowledgeManager" in GUIDE
    assert "switchWorkspaceTab('channels')" in GUIDE


def test_customer_delivery_flow_separates_live_transport_from_customer_handover():
    assert 'elif not channels["live"]' in READINESS_API
    assert 'blockers.append("Activate at least one connected customer channel")' in READINESS_API
    assert 'elif not channels["customer_ready"]' in READINESS_API
    assert 'blockers.append("Complete live channel acceptance before customer handover")' in READINESS_API
    assert 'and channels["customer_ready"]' in READINESS_API
    assert '"live_channels": channels["live"]' in READINESS_API
    assert '"customer_ready_channels": channels["customer_ready"]' in READINESS_API


def test_customer_delivery_flow_requires_company_activation_before_employee_go_live():
    assert '"company_active": bool(company.active)' in READINESS_API
    assert "Activate the company before the AI employee goes live" in READINESS_API


def test_customer_delivery_flow_supports_reply_only_and_operational_employees():
    assert '"requires_workflow_engine": bool(enabled_actions)' in READINESS_API
    assert '"mode": "conversational_and_operational" if actions["requested"] else "conversational"' in READINESS_API
    assert "actionsOptional=s.enabledActions===0" in GUIDE


def test_customer_delivery_flow_checks_execution_dependencies_only_when_needed():
    assert 'destination.get("type") == "integration"' in READINESS_API
    assert "required_integration_ids" in READINESS_API
    assert "Workflow Engine is not ready for enabled business actions or managed customer channels" in READINESS_API
    assert "managed_workflow_count" in READINESS_API
    assert "Connected App #" in READINESS_API


def test_customer_delivery_flow_has_internal_test_chat_before_go_live():
    assert "openAgentTestChat(companyId,agentId)" in APP
    assert '/admin/companies/${companyId}/agents/${agentId}/test-chat' in APP
    assert "Test Employee" in GUIDE
    assert "Go Live" in GUIDE


def test_delivery_readiness_is_registered_in_application():
    assert "admin_delivery_readiness_router" in MAIN
    assert "admin_delivery_readiness_router," in MAIN
    assert 'prefix="/admin/delivery-readiness"' in READINESS_API
    assert "/admin/delivery-readiness/companies/${companyId}/agents/${agentId}" in GUIDE


def test_delivery_contract_separates_draft_setup_from_live_customer_verdict():
    assert '"setup_ready": setup_ready' in READINESS_API
    assert '"ready_for_customer": ready_for_customer' in READINESS_API
    assert '@router.post("/companies/{company_id}/agents/{agent_id}/go-live")' in READINESS_API
    assert "Ready to go live" in GUIDE
    assert "Live for customer" in GUIDE
    assert "Deactivate" in GUIDE
