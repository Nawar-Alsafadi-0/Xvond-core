from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BACKUP = (ROOT / "scripts" / "backup_postgres.sh").read_text()
RESTORE = (ROOT / "scripts" / "restore_postgres.sh").read_text()
OFFSITE_BACKUP = (ROOT / "scripts" / "offsite_backup_loop.sh").read_text()
OFFSITE_RESTORE = (ROOT / "scripts" / "offsite_restore_latest.sh").read_text()
PRODUCTION_COMPOSE = (ROOT / "docker-compose.production.yml").read_text()


def test_backup_is_private_atomic_readable_and_checksummed():
    assert "umask 077" in BACKUP
    assert ".partial" in BACKUP
    verify = BACKUP.index('pg_restore --list "$partial_path"')
    promote = BACKUP.index('mv "$partial_path" "$final_path"')
    checksum = BACKUP.index('sha256sum "$final_path"')
    assert verify < promote < checksum


def test_backup_has_retention_policy():
    assert "BACKUP_RETENTION_DAYS" in BACKUP
    assert "-mtime" in BACKUP
    assert "-delete" in BACKUP


def test_restore_requires_explicit_confirmation():
    assert "CONFIRM_RESTORE" in RESTORE
    assert "RESTORE_XVOND_DATABASE" in RESTORE


def test_restore_verifies_checksum_before_running():
    checksum_position = RESTORE.index('sha256sum --check')
    restore_position = RESTORE.index('pg_restore')
    assert checksum_position < restore_position


def test_restore_is_transactional_and_stops_on_error():
    assert "--single-transaction" in RESTORE
    assert "--exit-on-error" in RESTORE


def test_offsite_backup_requires_encrypted_restic_repository_credentials():
    assert "RESTIC_REPOSITORY is required" in OFFSITE_BACKUP
    assert "RESTIC_PASSWORD is required" in OFFSITE_BACKUP
    assert "restic backup /backups" in OFFSITE_BACKUP
    assert "restic forget" in OFFSITE_BACKUP
    assert "restic check" in OFFSITE_BACKUP


def test_offsite_restore_downloads_latest_snapshot_and_verifies_dump_checksum():
    restore_position = OFFSITE_RESTORE.index("restic restore latest")
    checksum_position = OFFSITE_RESTORE.index("sha256sum -c")
    assert restore_position < checksum_position
    assert "xvond_*.dump" in OFFSITE_RESTORE


def test_production_compose_pins_restic_and_keeps_offsite_backup_opt_in():
    assert "restic/restic:0.19.1" in PRODUCTION_COMPOSE
    assert 'profiles: ["offsite-backup"]' in PRODUCTION_COMPOSE
    assert 'profiles: ["offsite-verify"]' in PRODUCTION_COMPOSE
    assert "xvond_backups:/backups:ro" in PRODUCTION_COMPOSE
