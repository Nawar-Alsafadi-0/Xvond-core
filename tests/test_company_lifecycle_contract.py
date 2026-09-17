import inspect
from types import SimpleNamespace

from backend.app.api import admin, auth
from backend.app.core import company_lifecycle
from backend.app.core import dependencies


def test_customer_portal_access_is_not_coupled_to_runtime_active():
    onboarding = SimpleNamespace(lifecycle_status="onboarding", active=False)
    testing = SimpleNamespace(lifecycle_status="testing", active=False)
    paused = SimpleNamespace(lifecycle_status="paused", active=False)
    suspended = SimpleNamespace(lifecycle_status="suspended", active=False)
    cancelled = SimpleNamespace(lifecycle_status="cancelled", active=False)
    archived = SimpleNamespace(lifecycle_status="archived", active=False)

    assert company_lifecycle.portal_access_allowed(onboarding) is True
    assert company_lifecycle.portal_access_allowed(testing) is True
    assert company_lifecycle.portal_access_allowed(paused) is True
    assert company_lifecycle.portal_access_allowed(suspended) is False
    assert company_lifecycle.portal_access_allowed(cancelled) is False
    assert company_lifecycle.portal_access_allowed(archived) is False


def test_login_and_session_dependencies_use_lifecycle_portal_gate():
    login_source = inspect.getsource(auth.login)
    dependency_source = inspect.getsource(dependencies.get_current_user)
    assert "portal_access_allowed(company)" in login_source
    assert "portal_access_allowed(company)" in dependency_source
    assert "not company.active" not in login_source
    assert "not company.active" not in dependency_source


def test_company_creation_starts_onboarding_with_runtime_off():
    source = inspect.getsource(admin.create_company)
    assert "Company(" in source
    assert "active=False" in source
    assert 'lifecycle_status="onboarding"' in source
    assert 'onboarding_source="managed"' in source
    assert '"initial_state": "onboarding"' in source
    assert '"runtime_active": False' in source


def test_go_live_is_readiness_gated_but_pause_preserves_agent_configuration():
    lifecycle_source = inspect.getsource(company_lifecycle.set_company_lifecycle)
    emergency_source = inspect.getsource(company_lifecycle.deactivate_company)
    assert 'normalized == "live"' in lifecycle_source
    assert 'if not readiness["ready"]' in lifecycle_source
    assert 'normalized in {"onboarding", "testing", "paused", "suspended", "cancelled", "archived"}' in lifecycle_source
    assert ".update({AIAgent.enabled: False}" not in lifecycle_source
    assert ".update({AIAgent.enabled: False}" in emergency_source


def test_admin_exposes_commercial_lifecycle_separately_from_emergency_switch():
    module_source = inspect.getsource(admin)
    assert '@router.patch("/companies/{company_id}/lifecycle")' in module_source
    assert '@router.patch("/companies/{company_id}/status")' in module_source
    assert 'action="company.lifecycle_changed"' in module_source
