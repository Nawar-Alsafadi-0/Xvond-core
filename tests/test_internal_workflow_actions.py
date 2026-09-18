from pathlib import Path


INTERNAL = Path("backend/app/api/internal_workflow_actions.py").read_text(encoding="utf-8")
MAIN = Path("backend/app/main.py").read_text(encoding="utf-8")
WORKFLOW = Path("ops/n8n/xvond-actions.workflow.json").read_text(encoding="utf-8")
ENV = Path(".env.example").read_text(encoding="utf-8")


def test_internal_workflow_route_is_registered():
    assert "internal_workflow_actions_router" in MAIN
    assert "internal_workflow_actions_router," in MAIN
    assert 'prefix="/internal/workflow"' in INTERNAL
    assert '@router.post("/xvond-internal")' in INTERNAL


def test_internal_workflow_requires_shared_secret():
    assert "N8N_SHARED_SECRET" in INTERNAL
    assert "Unauthorized workflow request" in INTERNAL
    assert "x_xvond_n8n_secret" in INTERNAL


def test_native_execution_is_idempotent_and_persisted():
    assert "_xvond_native_execution" in INTERNAL
    assert "idempotency_key" in INTERNAL
    assert 'current.get("idempotency_key") == idempotency_key' in INTERNAL
    assert '"already_executed": True' in INTERNAL
    assert '"state": "confirmed"' in INTERNAL


def test_native_execution_rechecks_xvond_schedule():
    assert "_internal_slots" in INTERNAL
    assert 'operation == "check_availability"' in INTERNAL
    assert 'availability.get("mode") or "none"' in INTERNAL
    assert "Requested time is no longer available" in INTERNAL


def test_internal_adapter_accepts_only_xvond_internal_destination():
    assert 'get("type") or "") != "xvond_internal"' in INTERNAL
    assert "Action is not routed to Xvond Internal" in INTERNAL



def test_internal_workflow_routes_generic_capability_runtime():
    assert "generic_capability_readiness" in INTERNAL
    assert "execute_generic_capability" in INTERNAL
    assert 'adapter == "generic_capability"' in INTERNAL
    assert '"runtime": "generic_capability"' in INTERNAL


def test_generic_runtime_cancel_does_not_claim_compensating_action():
    assert "Generic capability cancellation requires an explicit compensating plan" in INTERNAL


def test_master_workflow_routes_xvond_internal_to_private_callback():
    assert "Execute Xvond Internal" in WORKFLOW
    assert "XVOND_INTERNAL_ACTION_URL" in WORKFLOW
    assert "X-Xvond-N8N-Secret" in WORKFLOW
    assert "destinationType === 'xvond_internal'" in WORKFLOW
    assert "provider_not_configured" in WORKFLOW
    assert "XVOND_INTERNAL_ACTION_URL=http://app:8000/internal/workflow/xvond-internal" in ENV
