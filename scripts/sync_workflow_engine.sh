#!/bin/sh
set -eu

COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.production.yml}"
ACTION_WORKFLOW_FILE="${ACTION_WORKFLOW_FILE:-ops/n8n/xvond-actions.workflow.json}"
ACTION_WORKFLOW_ID="${ACTION_WORKFLOW_ID:-77dbf1b8-241b-44ec-b0f8-a16fe415490a}"
CHANNEL_WORKFLOW_FILE="${CHANNEL_WORKFLOW_FILE:-ops/n8n/xvond-channels.workflow.json}"
CHANNEL_WORKFLOW_ID="${CHANNEL_WORKFLOW_ID:-1ac59a11-41a9-48c0-a67d-9462b5cddc0e}"

for file in "$ACTION_WORKFLOW_FILE" "$CHANNEL_WORKFLOW_FILE"; do
    if [ ! -f "$file" ]; then
        echo "Workflow sync failed: $file not found" >&2
        exit 1
    fi
done

compose_workflow() {
    docker compose -f "$COMPOSE_FILE" --profile workflow "$@"
}

probe_inside_engine() {
    path="$1"
    script="$2"
    docker exec xvond-workflow-engine node -e "
const secret = String(process.env.N8N_SHARED_SECRET || '');
const requestId = 'sync-' + Date.now();
fetch('http://127.0.0.1:5678/webhook/$path', {
  method: 'POST',
  headers: {
    'content-type': 'application/json',
    'x-xvond-n8n-secret': secret,
    'x-xvond-request-id': requestId,
  },
  body: JSON.stringify($script),
}).then(async response => {
  const text = await response.text();
  if (!response.ok) {
    console.error('http_' + response.status + ':' + text.slice(0, 300));
    process.exit(2);
  }
  let result;
  try { result = JSON.parse(text); }
  catch (_error) {
    console.error('invalid_json_response:' + text.slice(0, 300));
    process.exit(2);
  }
  if (!result || result.request_id !== requestId) {
    console.error('invalid_contract_response:' + text.slice(0, 300));
    process.exit(2);
  }
  if ('$path' === 'xvond-actions') {
    if (result.success !== true || !result.data || String(result.data.status || '').toLowerCase() !== 'ok') {
      console.error('invalid_action_gateway_response:' + text.slice(0, 300));
      process.exit(2);
    }
  } else {
    if (result.action !== 'channel.check') {
      console.error('invalid_channel_gateway_response:' + text.slice(0, 300));
      process.exit(2);
    }
    const code = String(result.error_code || '');
    if (result.success !== true && code !== 'provider_not_configured') {
      console.error('unexpected_channel_gateway_response:' + text.slice(0, 300));
      process.exit(2);
    }
  }
  process.exit(0);
}).catch(error => {
  console.error('fetch_failed:' + String(error && error.message || 'unknown'));
  process.exit(2);
});"
}

wait_for_runtime_webhooks() {
    attempts="${1:-90}"
    count=0
    last_error=""
    while [ "$count" -lt "$attempts" ]; do
        if action_output="$(probe_inside_engine             "xvond-actions"             "{request_id: requestId, company_id: 1, agent_id: 1, conversation_id: null, action: 'health_check', data: {source: 'workflow_sync'}}" 2>&1)"             && channel_output="$(probe_inside_engine             "xvond-channels"             "{request_id: requestId, company_id: 1, agent_id: 1, action: 'channel.check', data: {channel_id: 1, channel_type: 'custom'}}" 2>&1)"; then
            return 0
        else
            code="$?"
            last_error="${action_output:-}
${channel_output:-}"
            if [ "$code" -ne 2 ]; then
                printf '%b\n' "$last_error" >&2
                return "$code"
            fi
        fi
        count=$((count + 1))
        sleep 1
    done
    echo "Workflow runtime webhooks did not become ready after restart" >&2
    if [ -n "$last_error" ]; then
        printf 'Last runtime probe error:\n%b\n' "$last_error" >&2
    fi
    docker logs --tail 100 xvond-workflow-engine >&2 || true
    return 1
}

sync_one_workflow() {
    file="$1"
    workflow_id="$2"
    import_name="$(basename "$file")"

    compose_workflow run --rm         -v "$PWD/ops/n8n:/import:ro"         workflow-engine         import:workflow --input="/import/$import_name"

    compose_workflow run --rm         workflow-engine         publish:workflow --id="$workflow_id"

    compose_workflow run --rm         workflow-engine         update:workflow --id="$workflow_id" --active=true
}

# Stop runtime before importing so database state and registered webhooks move
# together. Stable source-controlled workflow IDs make repeated deploys replace
# the same Xvond gateways instead of creating duplicate workflows.
compose_workflow stop workflow-engine >/dev/null 2>&1 || true

sync_one_workflow "$ACTION_WORKFLOW_FILE" "$ACTION_WORKFLOW_ID"
sync_one_workflow "$CHANNEL_WORKFLOW_FILE" "$CHANNEL_WORKFLOW_ID"

compose_workflow up -d --no-deps workflow-engine
wait_for_runtime_webhooks

echo "Workflow engine synced from Git: $ACTION_WORKFLOW_ID, $CHANNEL_WORKFLOW_ID"
