from pathlib import Path


INDEX = Path("frontend/admin/index.html").read_text(encoding="utf-8")
HEALTH = Path("frontend/admin/operations-health-dashboard.js").read_text(encoding="utf-8")
OPERATIONS = Path("backend/app/api/admin_operations.py").read_text(encoding="utf-8")


def test_admin_dashboard_loads_operations_health_layer():
    assert "/static/admin/operations-health-dashboard.js" in INDEX
    assert "/admin/operations/backups/status" in HEALTH
    assert "/admin/dashboard/summary" in HEALTH


def test_operations_health_surfaces_backup_and_delivery_attention():
    assert "Backup Health" in HEALTH
    assert "Local:" in HEALTH
    assert "Offsite:" in HEALTH
    assert "WhatsApp Deliveries to Review" in HEALTH
    assert "unresolved_whatsapp_deliveries" in HEALTH
    assert "Managed Channel Deliveries to Review" in HEALTH
    assert "unresolved_managed_channel_deliveries" in HEALTH


def test_backup_status_endpoint_does_not_expose_repository_secrets():
    assert '@router.get("/backups/status")' in OPERATIONS
    block = OPERATIONS.split('@router.get("/backups/status")', 1)[1].split('@router.get(', 1)[0]
    assert '"status": overall' in block
    assert '"local": local' in block
    assert '"offsite": offsite' in block
    assert '"repository"' not in block
    assert '"password"' not in block
