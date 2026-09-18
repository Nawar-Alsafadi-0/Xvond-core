from pathlib import Path


READINESS = Path("backend/app/api/admin_delivery_readiness.py").read_text(encoding="utf-8")
MAIN = Path("backend/app/main.py").read_text(encoding="utf-8")


def test_delivery_readiness_route_is_registered():
    assert 'prefix="/admin/delivery-readiness"' in READINESS
    assert '@router.get("/companies/{company_id}/agents/{agent_id}")' in READINESS
    assert "from backend.app.api.admin_delivery_readiness import router as admin_delivery_readiness_router" in MAIN
    assert "admin_delivery_readiness_router," in MAIN


def test_conversational_employee_does_not_require_actions_or_workflow_engine():
    assert '"requested": False' in READINESS
    assert '"ready": True' in READINESS
    assert '"requires_workflow_engine": False' in READINESS
    assert '"mode": "conversational_and_operational" if actions["requested"] else "conversational"' in READINESS


def test_operational_employee_requires_workflow_and_configured_integrations():
    assert '"requires_workflow_engine": bool(enabled_actions)' in READINESS
    assert "Workflow Engine is not ready for enabled business actions or managed customer channels" in READINESS
    assert "managed_workflow_count" in READINESS
    assert "N8N_CHANNEL_ADAPTER" in READINESS
    assert "Connected App #" in READINESS
    assert "N8N_SHARED_SECRET" in READINESS


def test_action_gate_rejects_empty_or_legacy_business_execution_state():
    assert 'LEGACY_BUSINESS_TOOL_NAMES = frozenset({"booking", "order", "lead"})' in READINESS
    assert "Legacy business tools are still enabled" in READINESS
    assert "Business Actions are enabled, but no customer operation is enabled and runtime-ready" in READINESS
    assert '"legacy_business_tools": legacy_business_tools' in READINESS
    assert '"ready": bool(not issues and (assignment is None or enabled_actions))' in READINESS


def test_readiness_separates_setup_live_and_customer_accepted_state():
    assert '"setup_ready": setup_ready' in READINESS
    assert '"ready_for_customer": ready_for_customer' in READINESS
    assert 'and channels["customer_ready"]' in READINESS
    assert '"customer_ready_channels": channels["customer_ready"]' in READINESS
    assert '"acceptance_pending_channels": channels["acceptance_pending_count"]' in READINESS
    assert '"lifecycle": "live" if agent.enabled else "draft"' in READINESS
    assert 'blockers.insert(0, "AI employee is in draft mode")' in READINESS
    assert '"company_active": bool(company.active)' in READINESS


def test_draft_can_be_setup_before_real_channel_acceptance():
    assert "def _channel_state" in READINESS
    assert "_channel_customer_accepted" in READINESS
    assert '"configured": bool(configured)' in READINESS
    assert '"live": bool(live)' in READINESS
    assert '"customer_ready": fully_customer_ready' in READINESS
    assert 'if not channels["configured"]' in READINESS
    assert 'setup_blockers.append("Connect and configure at least one customer channel")' in READINESS
    assert 'elif not channels["live"]' in READINESS
    assert 'blockers.append("Activate at least one connected customer channel")' in READINESS
    assert 'elif not channels["customer_ready"]' in READINESS
    assert 'blockers.append("Complete live channel acceptance before customer handover")' in READINESS
    assert "whatsapp_connection_state" in READINESS
    assert "verify_remote=True" in READINESS
    assert "whatsapp_meta_onboarding_complete" not in READINESS


def test_go_live_is_guarded_by_setup_company_state_and_plan_capacity():
    assert '@router.post("/companies/{company_id}/agents/{agent_id}/go-live")' in READINESS
    assert 'if not state["payload"]["setup_ready"]' in READINESS
    assert "Activate the company before the AI employee goes live" in READINESS
    assert "limits_service.check_agent_limit(db, company_id)" in READINESS
    assert "agent.enabled = True" in READINESS
    assert '@router.post("/companies/{company_id}/agents/{agent_id}/deactivate")' in READINESS


def test_operational_go_live_requires_real_workflow_health_before_enable():
    assert "def _assert_workflow_runtime_ready" in READINESS
    assert 'action="health_check"' in READINESS
    assert '"source": "delivery_readiness_go_live"' in READINESS
    assert '"workflow_required": workflow_required' in READINESS
    assert 'if state["payload"]["workflow_required"]' in READINESS
    health_gate = READINESS.index("_assert_workflow_runtime_ready(company_id, agent_id)")
    enable = READINESS.index("agent.enabled = True")
    assert health_gate < enable
    assert "Workflow Engine did not confirm the canonical action workflow" in READINESS


def test_readiness_checks_customer_delivery_basics():
    for value in (
        "company_active",
        "employee_enabled",
        "profile",
        "knowledge",
        "channels",
        "live_channels",
        "customer_ready_channels",
        "actions",
        "workflow_engine",
        "connected_apps",
        "ready_for_customer",
        "setup_ready",
        "workflow_required",
    ):
        assert value in READINESS
