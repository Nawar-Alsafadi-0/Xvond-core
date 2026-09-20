from pathlib import Path


MANAGED = Path("backend/app/api/admin_delivery_readiness.py").read_text(encoding="utf-8")
SELF_SERVICE = Path("backend/app/api/customer_employee_builder.py").read_text(encoding="utf-8")
POLICY = Path("backend/app/modules/ai_agent/self_service_policy.py").read_text(encoding="utf-8")
RUNTIME = Path("backend/app/modules/tools/generic_capability_runtime.py").read_text(encoding="utf-8")
PUBLIC_BUILDER = Path("frontend/public/employee-builder.html").read_text(encoding="utf-8")
PORTAL = Path("frontend/customer/employee-builder.js").read_text(encoding="utf-8")


def test_managed_delivery_contract_remains_separate_and_strict():
    assert "self_service_policy" not in MANAGED
    assert "Attach at least one enabled knowledge source" in MANAGED
    assert "Connect and configure at least one customer channel" in MANAGED
    assert "Complete live channel acceptance before customer handover" in MANAGED
    assert "limits_service.check_agent_limit(db, company_id)" in MANAGED


def test_self_service_has_its_own_launch_flow_and_cannot_launch_managed_employee():
    assert '@router.post("/{agent_id}/launch")' in SELF_SERVICE
    assert "launch_self_service_employee" in SELF_SERVICE
    assert "self_service_readiness" in SELF_SERVICE
    assert "Xvond Managed employees must use the managed delivery flow" in SELF_SERVICE
    assert '"delivery_mode": (' in SELF_SERVICE


def test_self_service_allows_zero_channels_when_job_does_not_need_conversation():
    assert "channels_required" in POLICY
    assert "interaction_mode" in POLICY
    assert 'name="channel"' not in PUBLIC_BUILDER
    assert "قنوات المحادثة" not in PUBLIC_BUILDER


def test_self_service_ui_lets_xvond_infer_channels_and_integrations():
    assert "القدرات والقنوات والـIntegrations" in PUBLIC_BUILDER
    assert 'name="channel"' not in PUBLIC_BUILDER
    assert 'id="employee-name"' not in PUBLIC_BUILDER
    assert 'instagram: "إنستغرام"' not in PUBLIC_BUILDER
    assert 'email: "الإيميل"' not in PUBLIC_BUILDER
    assert "Channel slots" not in PORTAL
    assert "Subscription required" not in PORTAL


def test_self_service_background_runtime_is_subscription_gated():
    assert "assert_self_service_runtime_subscription" in RUNTIME
    assert "service_limits.entitlement" in POLICY
    assert "if not is_self_service_company(company)" in POLICY


def test_self_service_connection_ui_does_not_fake_missing_adapters():
    assert "self_service_connection_status" in POLICY
    assert "Xvond connection adapter required" in POLICY
    assert "Xvond connection adapter required" in PORTAL
    assert "self_service_spec_view" in SELF_SERVICE
