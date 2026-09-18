from pathlib import Path


SOURCE = Path("scripts/deploy_production.sh").read_text(encoding="utf-8")
COMPOSE = Path("docker-compose.production.yml").read_text(encoding="utf-8")
WORKFLOW_SYNC = Path("scripts/sync_workflow_engine.sh").read_text(encoding="utf-8")


def test_release_refuses_dirty_tree_and_validates_compose():
    assert "git status --porcelain" in SOURCE
    assert "Refusing production deploy from a dirty Git working tree" in SOURCE
    assert "compose config" in SOURCE


def test_release_requires_canonical_release_branch_by_default():
    dirty_check = SOURCE.index("git status --porcelain")
    branch_check = SOURCE.index('required_release_branch="${DEPLOY_RELEASE_BRANCH:-main}"')
    env_check = SOURCE.index('if [ ! -f .env ]')
    assert dirty_check < branch_check < env_check
    assert "git symbolic-ref --quiet --short HEAD" in SOURCE
    assert "Refusing production deploy: current branch" in SOURCE


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


def test_release_stops_workers_before_app_and_recreates_same_image_afterwards():
    stop_workers = SOURCE.index("compose stop whatsapp-worker automation-scheduler")
    recreate_app = SOURCE.index("--force-recreate app")
    recreate_workers = SOURCE.index("--force-recreate whatsapp-worker automation-scheduler")
    worker_ready = SOURCE.index("wait_healthy xvond-whatsapp-worker")
    scheduler_ready = SOURCE.index("wait_healthy xvond-automation-scheduler")
    worker_lease = SOURCE.index("wait_whatsapp_worker_lease", worker_ready)
    scheduler_heartbeat = SOURCE.index("wait_scheduler_heartbeat", scheduler_ready)
    scheduler_image = SOURCE.index("scheduler_image=")
    image_check = SOURCE.index(
        'if [ -z "$app_image" ] || [ "$app_image" != "$worker_image" ] || [ "$app_image" != "$scheduler_image" ]'
    )
    assert (
        stop_workers
        < recreate_app
        < recreate_workers
        < worker_ready
        < scheduler_ready
        < worker_lease
        < scheduler_heartbeat
        < scheduler_image
        < image_check
    )
    assert "API, WhatsApp worker and automation scheduler are not running the same image" in SOURCE


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


def test_release_rechecks_workflow_can_reach_new_api_after_cutover():
    recreate_app = SOURCE.index("--force-recreate app")
    app_ready = SOURCE.index("wait_healthy xvond-core", recreate_app)
    post_cutover_probe = SOURCE.index("probe_workflow_to_app_health", app_ready)
    recreate_workers = SOURCE.index(
        "--force-recreate whatsapp-worker automation-scheduler",
        post_cutover_probe,
    )
    assert recreate_app < app_ready < post_cutover_probe < recreate_workers
    assert 'fetch("http://app:8000/health/ready")' in SOURCE
    assert "Workflow-to-API probe failed" in SOURCE


def test_workflow_sync_publishes_and_sets_active_before_restart():
    helper = WORKFLOW_SYNC.index("sync_one_workflow()")
    publish = WORKFLOW_SYNC.index('publish:workflow --id="$workflow_id"', helper)
    activate = WORKFLOW_SYNC.index(
        'update:workflow --id="$workflow_id" --active=true',
        helper,
    )
    action_sync = WORKFLOW_SYNC.index(
        'sync_one_workflow "$ACTION_WORKFLOW_FILE" "$ACTION_WORKFLOW_ID"'
    )
    channel_sync = WORKFLOW_SYNC.index(
        'sync_one_workflow "$CHANNEL_WORKFLOW_FILE" "$CHANNEL_WORKFLOW_ID"'
    )
    telegram_sync = WORKFLOW_SYNC.index(
        'sync_one_workflow "$TELEGRAM_WORKFLOW_FILE" "$TELEGRAM_WORKFLOW_ID"'
    )
    meta_sync = WORKFLOW_SYNC.index(
        'sync_one_workflow "$META_WORKFLOW_FILE" "$META_WORKFLOW_ID"'
    )
    restart = WORKFLOW_SYNC.index("up -d --no-deps workflow-engine")
    assert helper < publish < activate < action_sync < channel_sync < telegram_sync < meta_sync < restart


def test_workflow_sync_retries_transient_runtime_startup_failures():
    assert 'attempts="${1:-90}"' in WORKFLOW_SYNC
    assert 'process.exit(2)' in WORKFLOW_SYNC
    assert 'sleep 1' in WORKFLOW_SYNC
    assert 'invalid_action_gateway_response' in WORKFLOW_SYNC
    assert 'invalid_channel_gateway_response' in WORKFLOW_SYNC
    assert 'invalid_telegram_gateway_response' in WORKFLOW_SYNC
    assert "probe_telegram_gateway" in WORKFLOW_SYNC
    assert 'invalid_meta_gateway_response' in WORKFLOW_SYNC
    assert "probe_meta_gateway" in WORKFLOW_SYNC
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


def test_release_reports_scheduler_image_for_operator_verification():
    assert "Automation scheduler image:" in SOURCE
    assert "xvond-automation-scheduler" in SOURCE


def test_release_requires_scheduler_heartbeat_before_completion():
    assert "Automation scheduler did not publish a healthy heartbeat" in SOURCE
    assert "automation_scheduler_health.status()" in SOURCE
    scheduler_service = COMPOSE.split("  automation-scheduler:", 1)[1].split("\n  postgres:", 1)[0]
    assert "REDIS_URL: redis://redis:6379/0" in scheduler_service
    assert "redis:" in scheduler_service
    assert "condition: service_healthy" in scheduler_service


def test_release_requires_whatsapp_worker_lease_before_completion():
    assert "WhatsApp worker did not acquire its Redis lease" in SOURCE
    assert "whatsapp_job_queue.stats()" in SOURCE
    worker_ready = SOURCE.index("wait_healthy xvond-whatsapp-worker")
    worker_lease = SOURCE.index("wait_whatsapp_worker_lease", worker_ready)
    scheduler_image = SOURCE.index("scheduler_image=")
    assert worker_ready < worker_lease < scheduler_image


def test_release_requires_public_https_origin_after_local_health():
    local_health = SOURCE.index("http://127.0.0.1:8000/health/ready")
    public_probe = SOURCE.index(
        'python3 scripts/public_origin_probe.py --base-url "$public_base_url"'
    )
    acceptance = SOURCE.index("scripts/production_acceptance.py")
    assert local_health < public_probe < acceptance
    assert 'public_base_url="$(env_value PUBLIC_BASE_URL)"' in SOURCE
    assert "Public Core origin:" in SOURCE


def test_public_origin_probe_uses_host_network_not_app_container():
    assert 'python3 scripts/public_origin_probe.py --base-url "$public_base_url"' in SOURCE
    assert 'compose exec -T app python scripts/public_origin_probe.py' not in SOURCE


def test_telegram_provider_workflow_is_source_controlled_and_synced():
    assert 'TELEGRAM_WORKFLOW_FILE="${TELEGRAM_WORKFLOW_FILE:-ops/n8n/xvond-telegram-provider.workflow.json}"' in WORKFLOW_SYNC
    assert 'TELEGRAM_WORKFLOW_ID="${TELEGRAM_WORKFLOW_ID:-xvond-telegram-provider-v1}"' in WORKFLOW_SYNC
    assert "XVOND_TELEGRAM_ROUTES_JSON" in COMPOSE


def test_meta_messaging_provider_workflow_is_source_controlled_and_synced():
    assert 'META_WORKFLOW_FILE="${META_WORKFLOW_FILE:-ops/n8n/xvond-meta-messaging-provider.workflow.json}"' in WORKFLOW_SYNC
    assert 'META_WORKFLOW_ID="${META_WORKFLOW_ID:-xvond-meta-messaging-provider-v1}"' in WORKFLOW_SYNC
    assert "XVOND_META_MESSAGING_ROUTES_JSON" in COMPOSE
    assert "XVOND_META_MESSAGING_VERIFY_TOKEN" in COMPOSE
    assert "NODE_FUNCTION_ALLOW_BUILTIN: crypto" in COMPOSE


def test_release_can_run_fail_closed_market_launch_gate_after_cutover():
    public_probe = SOURCE.index(
        'python3 scripts/public_origin_probe.py --base-url "$public_base_url"'
    )
    market_gate = SOURCE.index("python scripts/market_launch_gate.py")
    assert public_probe < market_gate
    assert 'MARKET_ACCEPTANCE_MODE="${MARKET_ACCEPTANCE_MODE:-}"' in SOURCE
    assert 'MARKET_ACCEPTANCE_CHANNELS="${MARKET_ACCEPTANCE_CHANNELS:-}"' in SOURCE
    assert "MARKET_ACCEPTANCE_REQUIRE_ONLINE_BILLING" in SOURCE
    assert "MARKET_ACCEPTANCE_REQUIRE_PAYMENT_EVIDENCE" in SOURCE
    assert "--require-channel" in SOURCE
    assert "--require-online-billing" in SOURCE
    assert "--require-payment-evidence" in SOURCE
    assert "requires ACCEPTANCE_COMPANY_ID and ACCEPTANCE_AGENT_ID" in SOURCE
