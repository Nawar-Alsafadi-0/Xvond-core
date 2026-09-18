from pathlib import Path


SOURCE = Path("scripts/deploy_production.sh").read_text(encoding="utf-8")
COMPOSE = Path("docker-compose.production.yml").read_text(encoding="utf-8")
WORKFLOW_SYNC = Path("scripts/sync_workflow_engine.sh").read_text(encoding="utf-8")


def test_release_refuses_dirty_tree_and_validates_compose():
    assert "git status --porcelain" in SOURCE
    assert "Refusing production deploy from a dirty Git working tree" in SOURCE
    assert "compose config" in SOURCE


def test_release_rejects_missing_or_placeholder_environment_before_compose():
    env_check = SOURCE.index('if [ ! -f .env ]')
    required_core = SOURCE.index('for key in \\\n    DATABASE_URL')
    require_real_env_call = SOURCE.index('    require_real_env "$key"', required_core)
    compose_config = SOURCE.index("compose config >/dev/null")
    assert env_check < required_core < require_real_env_call < compose_config
    assert "placeholder value remains for $key" in SOURCE
    for marker in (
        "GENERATE_",
        "CHANGE_TO_",
        "URL_ENCODED_PASSWORD",
        "REPLACE_ME",
        "YOUR_SECRET",
        "YOUR_PASSWORD",
        "EXAMPLE_SECRET",
        "admin@example.com",
    ):
        assert marker in SOURCE


def test_release_validates_workflow_secrets_only_when_enabled():
    workflow_flag = SOURCE.index('workflow_enabled="$(parse_bool_env N8N_ENABLED)"')
    workflow_gate = SOURCE.index('if [ "$workflow_enabled" = "true" ]; then', workflow_flag)
    workflow_secret = SOURCE.index("N8N_SHARED_SECRET", workflow_gate)
    workflow_db_secret = SOURCE.index("WORKFLOW_DB_PASSWORD", workflow_gate)
    workflow_encrypt_secret = SOURCE.index("WORKFLOW_ENCRYPTION_KEY", workflow_gate)
    compose_config = SOURCE.index("compose config >/dev/null")
    assert workflow_flag < workflow_gate < workflow_secret < workflow_db_secret < workflow_encrypt_secret < compose_config


def test_release_takes_backup_before_recreating_application():
    backup = SOURCE.index("backup_postgres.sh")
    build = SOURCE.index("compose build app")
    recreate = SOURCE.index("--force-recreate app")
    assert backup < build < recreate


def test_release_stops_worker_before_app_and_recreates_same_image_afterwards():
    stop_worker = SOURCE.index("compose stop whatsapp-worker")
    recreate_app = SOURCE.index("--force-recreate app")
    recreate_worker = SOURCE.index("--force-recreate whatsapp-worker")
    image_check = SOURCE.index('if [ -z "$app_image" ] || [ "$app_image" != "$worker_image" ]')
    assert stop_worker < recreate_app < recreate_worker < image_check


def test_release_preflights_workflow_before_runtime_cutover_when_required():
    build = SOURCE.index("compose build app")
    workflow_setting = SOURCE.index("settings.N8N_ENABLED")
    workflow_db_start = SOURCE.index('--profile workflow up -d workflow-postgres')
    workflow_db_ready = SOURCE.index("wait_healthy xvond-workflow-postgres")
    workflow_sync = SOURCE.index('COMPOSE_FILE="$COMPOSE_FILE" sh scripts/sync_workflow_engine.sh')
    workflow_ready = SOURCE.index("wait_healthy xvond-workflow-engine")
    workflow_probe_call = SOURCE.index("    probe_workflow_contract", workflow_ready)
    stop_worker = SOURCE.index("compose stop whatsapp-worker")
    recreate_app = SOURCE.index("--force-recreate app")
    acceptance = SOURCE.index("scripts/production_acceptance.py")
    assert (
        build
        < workflow_setting
        < workflow_db_start
        < workflow_db_ready
        < workflow_sync
        < workflow_ready
        < workflow_probe_call
        < stop_worker
        < recreate_app
        < acceptance
    )
    assert "Workflow contract probe failed" in SOURCE
    assert 'action: "health_check"' in SOURCE


def test_workflow_sync_publishes_and_sets_active_before_restart():
    publish = WORKFLOW_SYNC.index('publish:workflow --id="$WORKFLOW_ID"')
    activate = WORKFLOW_SYNC.index('update:workflow --id="$WORKFLOW_ID" --active=true')
    restart = WORKFLOW_SYNC.index("up -d --no-deps workflow-engine")
    assert publish < activate < restart


def test_workflow_sync_retries_transient_runtime_startup_failures():
    assert 'attempts="${1:-90}"' in WORKFLOW_SYNC
    assert 'process.exit(2)' in WORKFLOW_SYNC
    assert 'sleep 1' in WORKFLOW_SYNC
    assert 'invalid_contract_response' in WORKFLOW_SYNC
    assert 'Last runtime probe error:' in WORKFLOW_SYNC


def test_workflow_engine_has_real_http_healthcheck_before_release_continues():
    workflow = COMPOSE.split("  workflow-engine:", 1)[1].split("\nvolumes:", 1)[0]
    assert "healthcheck:" in workflow
    assert "http://127.0.0.1:5678/healthz" in workflow
    assert '"node"' in workflow
    assert "start_period: 30s" in workflow


def test_release_has_mandatory_health_and_optional_customer_acceptance():
    assert "/health/ready" in SOURCE
    assert "ACCEPTANCE_COMPANY_ID" in SOURCE
    assert "scripts/production_acceptance.py" in SOURCE
    assert '"$@" --agent-id' in SOURCE
    assert '"$@" --live-ai' in SOURCE
    assert '"$@" --require-live' in SOURCE
