from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEPENDENCIES = (ROOT / "backend/app/core/dependencies.py").read_text(encoding="utf-8")
ADMIN = (ROOT / "backend/app/api/admin.py").read_text(encoding="utf-8")
DASHBOARD = (ROOT / "backend/app/api/admin_dashboard.py").read_text(encoding="utf-8")
COMPANY_VIEW = (ROOT / "backend/app/api/admin_company_view.py").read_text(encoding="utf-8")
OPERATIONS = (ROOT / "backend/app/api/admin_operations.py").read_text(encoding="utf-8")


def test_support_has_distinct_read_only_operator_dependency():
    assert 'XVOND_OPERATOR_ROLES = {' in DEPENDENCIES
    assert '"support"' in DEPENDENCIES
    assert 'def require_xvond_operator' in DEPENDENCIES
    operator_block = DEPENDENCIES.split('def require_xvond_operator', 1)[1].split('def require_xvond_admin', 1)[0]
    assert 'XVOND_OPERATOR_ROLES' in operator_block
    admin_block = DEPENDENCIES.split('def require_xvond_admin', 1)[1].split('def require_super_admin', 1)[0]
    assert '"support"' not in admin_block


def test_support_can_read_operational_control_plane():
    assert 'def summary(current_admin: User = Depends(require_xvond_operator))' in DASHBOARD
    assert 'def company_full_view(company_id: int, current_admin: User = Depends(require_xvond_operator))' in COMPANY_VIEW
    assert 'def list_companies(' in ADMIN
    list_block = ADMIN.split('def list_companies(', 1)[1].split('finally:', 1)[0]
    assert 'current_admin: User = Depends(require_xvond_operator)' in list_block
    assert 'def backup_status(current_admin: User = Depends(require_xvond_operator))' in OPERATIONS
    assert 'def company_usage(company_id: int, current_admin: User = Depends(require_xvond_operator))' in OPERATIONS
    assert 'def whatsapp_worker_status(current_admin: User = Depends(require_xvond_operator))' in OPERATIONS


def test_support_company_view_minimizes_tenant_identity_data():
    assert 'support_view = current_admin.role == "support"' in COMPANY_VIEW
    assert 'user_payload = [] if support_view else [' in COMPANY_VIEW
    assert '"user_count": len(users)' in COMPANY_VIEW


def test_support_cannot_mutate_production_or_reconcile_incidents():
    assert 'def create_company(data: CompanyCreate, current_admin: User = Depends(require_xvond_admin))' in ADMIN
    assert 'current_admin: User = Depends(require_xvond_admin),' in ADMIN
    retry_block = OPERATIONS.split('def retry_whatsapp_delivery', 1)[1].split('@router.patch("/requests/{request_id}/reconcile")', 1)[0]
    reconcile_block = OPERATIONS.split('def reconcile_external_operation', 1)[1].split('@router.get("/workers/whatsapp")', 1)[0]
    dead_retry_block = OPERATIONS.split('def retry_whatsapp_dead_jobs', 1)[1]
    assert 'Depends(require_xvond_admin)' in retry_block
    assert 'Depends(require_xvond_admin)' in reconcile_block
    assert 'Depends(require_xvond_admin)' in dead_retry_block
