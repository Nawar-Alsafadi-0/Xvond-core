#!/bin/sh
set -eu

COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.production.yml}"
ACCEPTANCE_COMPANY_ID="${ACCEPTANCE_COMPANY_ID:-}"
ACCEPTANCE_AGENT_ID="${ACCEPTANCE_AGENT_ID:-}"
ACCEPTANCE_LIVE_AI="${ACCEPTANCE_LIVE_AI:-false}"
ACCEPTANCE_REQUIRE_LIVE="${ACCEPTANCE_REQUIRE_LIVE:-false}"
GENERALIZATION_ACCEPTANCE="${GENERALIZATION_ACCEPTANCE:-false}"
MARKET_ACCEPTANCE_MODE="${MARKET_ACCEPTANCE_MODE:-}"
MARKET_ACCEPTANCE_CHANNELS="${MARKET_ACCEPTANCE_CHANNELS:-}"
MARKET_ACCEPTANCE_REQUIRE_AUTOMATION_RUN="${MARKET_ACCEPTANCE_REQUIRE_AUTOMATION_RUN:-false}"
MARKET_ACCEPTANCE_REQUIRE_ONLINE_BILLING="${MARKET_ACCEPTANCE_REQUIRE_ONLINE_BILLING:-false}"
MARKET_ACCEPTANCE_REQUIRE_PAYMENT_EVIDENCE="${MARKET_ACCEPTANCE_REQUIRE_PAYMENT_EVIDENCE:-false}"
deployment_started_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

compose() {
    docker compose -f "$COMPOSE_FILE" "$@"
}

wait_healthy() {
    container="$1"
    attempts="${2:-60}"
    count=0
    while [ "$count" -lt "$attempts" ]; do
        status="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$container" 2>/dev/null || true)"
        if [ "$status" = "healthy" ] || [ "$status" = "running" ]; then
            return 0
        fi
        if [ "$status" = "unhealthy" ] || [ "$status" = "exited" ] || [ "$status" = "dead" ]; then
            echo "Container $container entered unsafe state: $status" >&2
            docker logs --tail 100 "$container" >&2 || true
            return 1
        fi
        count=$((count + 1))
        sleep 2
    done
    echo "Container $container did not become healthy" >&2
    docker logs --tail 100 "$container" >&2 || true
    return 1
}

wait_whatsapp_worker_lease() {
    attempts="${1:-30}"
    count=0
    while [ "$count" -lt "$attempts" ]; do
        if compose exec -T whatsapp-worker python -c "from backend.app.modules.channels.whatsapp_queue import whatsapp_job_queue; status=whatsapp_job_queue.stats(); raise SystemExit(0 if status.get('configured') and status.get('worker_active') else 1)" >/dev/null 2>&1; then
            return 0
        fi
        count=$((count + 1))
        sleep 1
    done
    echo "WhatsApp worker did not acquire its Redis lease" >&2
    docker logs --tail 100 xvond-whatsapp-worker >&2 || true
    return 1
}

wait_scheduler_heartbeat() {
    attempts="${1:-30}"
    count=0
    while [ "$count" -lt "$attempts" ]; do
        if compose exec -T automation-scheduler python -c "from backend.app.modules.automation.scheduler_health import automation_scheduler_health; status=automation_scheduler_health.status(); raise SystemExit(0 if status.get('configured') and status.get('active') else 1)" >/dev/null 2>&1; then
            return 0
        fi
        count=$((count + 1))
        sleep 1
    done
    echo "Automation scheduler did not publish a healthy heartbeat" >&2
    docker logs --tail 100 xvond-automation-scheduler >&2 || true
    return 1
}

probe_workflow_contract() {
    docker exec xvond-workflow-engine node -e '
const url = "http://127.0.0.1:5678/webhook/xvond-actions";
const secret = String(process.env.N8N_SHARED_SECRET || "");
if (!secret) { console.error("Workflow contract probe failed: shared secret missing"); process.exit(1); }
const requestId = `release-${Date.now()}`;
fetch(url, {
  method: "POST",
  headers: {
    "content-type": "application/json",
    "x-xvond-n8n-secret": secret,
    "x-xvond-request-id": requestId,
  },
  body: JSON.stringify({request_id: requestId, company_id: 1, agent_id: 1, conversation_id: null, action: "health_check", data: {source: "production_release"}}),
}).then(async response => {
  const text = await response.text();
  if (!response.ok) throw new Error(`http_${response.status}:${text.slice(0, 200)}`);
  if (!text.trim()) throw new Error("empty_contract_response");
  let result;
  try { result = JSON.parse(text); }
  catch (_error) { throw new Error(`invalid_json_response:${text.slice(0, 200)}`); }
  if (!result || result.success !== true || !result.data || String(result.data.status || "").toLowerCase() !== "ok") {
    throw new Error(`invalid_contract_response:${text.slice(0, 200)}`);
  }
}).catch(error => {
  console.error(`Workflow contract probe failed: ${String(error && error.message || "unknown")}`);
  process.exit(1);
});'
}

probe_workflow_to_app_health() {
    docker exec xvond-workflow-engine node -e '
fetch("http://app:8000/health/ready")
  .then(async response => {
    const text = await response.text();
    if (!response.ok) throw new Error(`http_${response.status}:${text.slice(0, 200)}`);
    let result;
    try { result = JSON.parse(text); }
    catch (_error) { throw new Error(`invalid_json_response:${text.slice(0, 200)}`); }
    if (!result || result.status !== "healthy") {
      throw new Error(`api_not_ready:${text.slice(0, 200)}`);
    }
  })
  .catch(error => {
    console.error(`Workflow-to-API probe failed: ${String(error && error.message || "unknown")}`);
    process.exit(1);
  });'
}

env_value() {
    key="$1"
    awk -F= -v wanted="$key" '
        /^[[:space:]]*#/ || !/=/{next}
        {
            current=$1
            gsub(/^[[:space:]]+|[[:space:]]+$/, "", current)
            if (current == wanted) {
                value=substr($0, index($0, "=") + 1)
                gsub(/^[[:space:]]+|[[:space:]]+$/, "", value)
                print value
                exit
            }
        }
    ' .env
}

is_placeholder_value() {
    upper="$(printf '%s' "$1" | tr '[:lower:]' '[:upper:]')"
    case "$upper" in
        *GENERATE_*|*CHANGE_TO_*|*URL_ENCODED_PASSWORD*|*REPLACE_ME*|*YOUR_SECRET*|*YOUR_PASSWORD*|*EXAMPLE_SECRET*) return 0 ;;
        *) return 1 ;;
    esac
}

require_real_env() {
    key="$1"
    value="$(env_value "$key")"
    if [ -z "$value" ]; then
        echo "Refusing production deploy: required value is missing for $key" >&2
        exit 1
    fi
    if is_placeholder_value "$value"; then
        echo "Refusing production deploy: placeholder value remains for $key" >&2
        exit 1
    fi
    if [ "$key" = "SUPERADMIN_EMAIL" ] && [ "$(printf '%s' "$value" | tr '[:upper:]' '[:lower:]')" = "admin@example.com" ]; then
        echo "Refusing production deploy: placeholder value remains for $key" >&2
        exit 1
    fi
}

parse_bool_env() {
    key="$1"
    value="$(env_value "$key" | tr '[:upper:]' '[:lower:]')"
    case "$value" in
        1|true|yes|on) printf 'true\n' ;;
        0|false|no|off|'') printf 'false\n' ;;
        *)
            echo "Refusing production deploy: invalid boolean value for $key" >&2
            exit 1
            ;;
    esac
}

case "$(git status --porcelain 2>/dev/null || true)" in
    "") ;;
    *) echo "Refusing production deploy from a dirty Git working tree" >&2; exit 1 ;;
esac

required_release_branch="${DEPLOY_RELEASE_BRANCH:-main}"
current_branch="$(git symbolic-ref --quiet --short HEAD 2>/dev/null || true)"
if [ "$current_branch" != "$required_release_branch" ]; then
    echo "Refusing production deploy: current branch '${current_branch:-detached}' is not required release branch '$required_release_branch'" >&2
    exit 1
fi

if [ ! -f .env ]; then
    echo "Refusing production deploy: .env is missing" >&2
    exit 1
fi

# Core production secrets are mandatory on every release. Optional feature
# secrets are validated only when that feature is enabled, so disabled profiles
# may safely retain template placeholders without weakening active services.
for key in \
    DATABASE_URL \
    DATABASE_URL_DOCKER \
    POSTGRES_PASSWORD \
    JWT_SECRET \
    CONFIG_ENCRYPTION_KEY \
    SUPERADMIN_EMAIL \
    SUPERADMIN_PASSWORD \
    PUBLIC_BASE_URL
do
    require_real_env "$key"
done

public_base_url="$(env_value PUBLIC_BASE_URL)"

workflow_enabled="$(parse_bool_env N8N_ENABLED)"
if [ "$workflow_enabled" = "true" ]; then
    for key in \
        N8N_WEBHOOK_URL \
        N8N_SHARED_SECRET \
        WORKFLOW_ENGINE_VERSION \
        WORKFLOW_DB_PASSWORD \
        WORKFLOW_ENCRYPTION_KEY \
        WORKFLOW_PUBLIC_URL
    do
        require_real_env "$key"
    done
fi

release_sha="$(git rev-parse HEAD)"
release_short="$(git rev-parse --short HEAD)"
echo "Deploying Xvond release $release_short"

compose config >/dev/null
compose up -d postgres redis
wait_healthy xvond-postgres
wait_healthy xvond-redis
compose run --rm --no-deps --entrypoint /bin/sh postgres-backup /opt/xvond/scripts/backup_postgres.sh
compose build app

# Parse the built application's effective settings as a second source of truth.
# It must agree with the feature-aware host preflight before any live cutover.
runtime_workflow_enabled="$(compose run --rm --no-deps --entrypoint python app -c "from backend.app.core.config.settings import settings; print('true' if settings.N8N_ENABLED else 'false')" | tr -d '\r\n')"
if [ "$runtime_workflow_enabled" != "$workflow_enabled" ]; then
    echo "Refusing production deploy: workflow enablement differs between .env preflight and application settings" >&2
    exit 1
fi

if [ "$workflow_enabled" = "true" ]; then
    docker compose -f "$COMPOSE_FILE" --profile workflow up -d workflow-postgres
    wait_healthy xvond-workflow-postgres
    COMPOSE_FILE="$COMPOSE_FILE" sh scripts/sync_workflow_engine.sh
    wait_healthy xvond-workflow-engine
    probe_workflow_contract
fi

compose stop whatsapp-worker automation-scheduler >/dev/null 2>&1 || true
compose up -d --no-deps --force-recreate app
wait_healthy xvond-core
if [ "$workflow_enabled" = "true" ]; then
    probe_workflow_to_app_health
fi
compose up -d --no-deps --force-recreate whatsapp-worker automation-scheduler
wait_healthy xvond-whatsapp-worker
wait_healthy xvond-automation-scheduler
wait_whatsapp_worker_lease
wait_scheduler_heartbeat

app_image="$(docker inspect --format '{{.Image}}' xvond-core)"
worker_image="$(docker inspect --format '{{.Image}}' xvond-whatsapp-worker)"
scheduler_image="$(docker inspect --format '{{.Image}}' xvond-automation-scheduler)"
if [ -z "$app_image" ] || [ "$app_image" != "$worker_image" ] || [ "$app_image" != "$scheduler_image" ]; then
    echo "Release rejected: API, WhatsApp worker and automation scheduler are not running the same image" >&2
    echo "app=$app_image worker=$worker_image scheduler=$scheduler_image" >&2
    exit 1
fi

compose up -d postgres-backup
compose exec -T app python -c "import json, urllib.request; data=json.load(urllib.request.urlopen('http://127.0.0.1:8000/health/ready', timeout=5)); assert data.get('status') == 'healthy', data"
python3 scripts/public_origin_probe.py --base-url "$public_base_url"

if [ -n "$ACCEPTANCE_COMPANY_ID" ]; then
    set -- python scripts/production_acceptance.py --company-id "$ACCEPTANCE_COMPANY_ID"
    if [ -n "$ACCEPTANCE_AGENT_ID" ]; then set -- "$@" --agent-id "$ACCEPTANCE_AGENT_ID"; fi
    if [ "$ACCEPTANCE_LIVE_AI" = "true" ]; then set -- "$@" --live-ai; fi
    if [ "$ACCEPTANCE_REQUIRE_LIVE" = "true" ]; then set -- "$@" --require-live; fi
    compose exec -T app "$@"
fi

if [ "$GENERALIZATION_ACCEPTANCE" = "true" ]; then
    if [ -z "$ACCEPTANCE_COMPANY_ID" ] || [ -z "$ACCEPTANCE_AGENT_ID" ]; then
        echo "Generalization gate requires ACCEPTANCE_COMPANY_ID and ACCEPTANCE_AGENT_ID" >&2
        exit 1
    fi
    compose exec -T app python -m scripts.generalization_acceptance \
        --company-id "$ACCEPTANCE_COMPANY_ID" \
        --agent-id "$ACCEPTANCE_AGENT_ID"
fi

if [ -n "$MARKET_ACCEPTANCE_MODE" ]; then
    case "$MARKET_ACCEPTANCE_MODE" in
        managed|self_service) ;;
        *)
            echo "Market launch gate rejected: MARKET_ACCEPTANCE_MODE must be managed or self_service" >&2
            exit 1
            ;;
    esac
    if [ -z "$ACCEPTANCE_COMPANY_ID" ] || [ -z "$ACCEPTANCE_AGENT_ID" ]; then
        echo "Market launch gate requires ACCEPTANCE_COMPANY_ID and ACCEPTANCE_AGENT_ID" >&2
        exit 1
    fi
    if [ -z "$MARKET_ACCEPTANCE_CHANNELS" ] && [ "$MARKET_ACCEPTANCE_REQUIRE_AUTOMATION_RUN" != "true" ]; then
        echo "Market launch gate requires MARKET_ACCEPTANCE_CHANNELS or MARKET_ACCEPTANCE_REQUIRE_AUTOMATION_RUN=true" >&2
        exit 1
    fi

    set -- python -m scripts.market_launch_gate \
        --company-id "$ACCEPTANCE_COMPANY_ID" \
        --agent-id "$ACCEPTANCE_AGENT_ID" \
        --launch-mode "$MARKET_ACCEPTANCE_MODE"

    old_ifs="$IFS"
    IFS=','
    for channel in $MARKET_ACCEPTANCE_CHANNELS; do
        clean_channel="$(printf '%s' "$channel" | tr -d '[:space:]')"
        if [ -n "$clean_channel" ]; then
            set -- "$@" --require-channel "$clean_channel"
        fi
    done
    IFS="$old_ifs"

    if [ "$MARKET_ACCEPTANCE_REQUIRE_ONLINE_BILLING" = "true" ]; then
        set -- "$@" --require-online-billing
    fi
    if [ "$MARKET_ACCEPTANCE_REQUIRE_PAYMENT_EVIDENCE" = "true" ]; then
        set -- "$@" --require-payment-evidence
    fi
    if [ "$MARKET_ACCEPTANCE_REQUIRE_AUTOMATION_RUN" = "true" ]; then
        set -- "$@" --require-automation-run --automation-run-after "$deployment_started_at"
    fi
    compose exec -T app "$@"
fi

printf 'Xvond release complete: %s\n' "$release_sha"
printf 'API image: %s\n' "$app_image"
printf 'WhatsApp worker image: %s\n' "$worker_image"
printf 'Automation scheduler image: %s\n' "$scheduler_image"
printf 'Workflow engine enabled: %s\n' "$workflow_enabled"
printf 'Public Core origin: %s\n' "$public_base_url"
