#!/bin/sh
set -eu

COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.production.yml}"

required_vars="WORKFLOW_ENGINE_VERSION WORKFLOW_DB_PASSWORD WORKFLOW_ENCRYPTION_KEY WORKFLOW_PUBLIC_URL N8N_SHARED_SECRET"
for var in $required_vars; do
  eval "value=\${$var:-}"
  if [ -z "$value" ]; then
    echo "Missing required environment variable: $var" >&2
    exit 1
  fi
done

case "$WORKFLOW_PUBLIC_URL" in
  https://*) ;;
  *)
    echo "WORKFLOW_PUBLIC_URL must use https://" >&2
    exit 1
    ;;
esac

docker compose -f "$COMPOSE_FILE" --profile workflow config >/dev/null
docker compose -f "$COMPOSE_FILE" --profile workflow pull workflow-postgres workflow-engine
docker compose -f "$COMPOSE_FILE" --profile workflow up -d workflow-postgres workflow-engine

docker compose -f "$COMPOSE_FILE" --profile workflow ps workflow-postgres workflow-engine

echo "Workflow engine containers started. Import and activate ops/n8n/xvond-actions.workflow.json and ops/n8n/xvond-channel-inbound.workflow.json after HTTPS/reverse-proxy setup is verified."
