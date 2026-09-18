from pathlib import Path
import time

from backend.app.api import admin_operations


def test_backup_marker_reports_healthy_stale_and_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("BACKUP_STATUS_DIR", str(tmp_path))
    now = int(time.time())

    marker = tmp_path / "local_success_epoch"
    marker.write_text(str(now - 60), encoding="utf-8")
    healthy = admin_operations._backup_marker(
        "local_success_epoch", expected=True, stale_after=3600
    )
    assert healthy["status"] == "healthy"
    assert healthy["age_seconds"] >= 0

    marker.write_text(str(now - 7200), encoding="utf-8")
    stale = admin_operations._backup_marker(
        "local_success_epoch", expected=True, stale_after=3600
    )
    assert stale["status"] == "stale"

    marker.unlink()
    missing = admin_operations._backup_marker(
        "local_success_epoch", expected=True, stale_after=3600
    )
    assert missing["status"] == "missing"

    optional = admin_operations._backup_marker(
        "offsite_success_epoch", expected=False, stale_after=3600
    )
    assert optional["status"] == "not_configured"
    assert optional["expected"] is False


def test_backup_scripts_publish_status_only_after_success():
    root = Path(__file__).resolve().parents[1]
    local = (root / "scripts" / "backup_postgres.sh").read_text(encoding="utf-8")
    offsite = (root / "scripts" / "offsite_backup_loop.sh").read_text(encoding="utf-8")

    assert local.index("pg_dump") < local.index("pg_restore --list") < local.index("sha256sum") < local.index("local_success_epoch")
    assert offsite.index("restic backup") < offsite.index("restic check") < offsite.index("offsite_success_epoch")


def test_backup_health_never_returns_repository_or_password(monkeypatch, tmp_path):
    monkeypatch.setenv("BACKUP_STATUS_DIR", str(tmp_path))
    monkeypatch.setenv("RESTIC_REPOSITORY", "s3:https://secret.example/private")
    monkeypatch.setenv("RESTIC_PASSWORD", "VERY-SECRET")
    result = admin_operations.backup_status(current_admin=object())
    rendered = str(result)
    assert "secret.example" not in rendered
    assert "VERY-SECRET" not in rendered
    assert result["offsite"]["expected"] is True
