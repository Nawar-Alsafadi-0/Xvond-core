#!/bin/sh
set -eu

COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.production.yml}"
ACTION_WORKFLOW_FILE="${ACTION_WORKFLOW_FILE:-ops/n8n/xvond-actions.workflow.json}"
ACTION_WORKFLOW_ID="${ACTION_WORKFLOW_ID:-77dbf1b8-241b-44ec-b0f8-a16fe415490a}"
CHANNEL_WORKFLOW_FILE="${CHANNEL_WORKFLOW_FILE:-ops/n8n/xvond-channel-inbound.workflow.json}"
CHANNEL_WORKFLOW_ID="${CHANNEL_WORKFLOW_ID:-xvond-channel-inbound-v1}"
TELEGRAM_WORKFLOW_FILE="${TELEGRAM_WORKFLOW_FILE:-ops/n8n/xvond-telegram-provider.workflow.json}"
TELEGRAM_WORKFLOW_ID="${TELEGRAM_WORKFLOW_ID:-xvond-telegram-provider-v1}"
META_WORKFLOW_FILE="${META_WORKFLOW_FILE:-ops/n8n/xvond-meta-messaging-provider.workflow.json}"
META_WORKFLOW_ID="${META_WORKFLOW_ID:-xvond-meta-messaging-provider-v1}"
SLACK_WORKFLOW_FILE="${SLACK_WORKFLOW_FILE:-ops/n8n/xvond-slack-provider.workflow.json}"
SLACK_WORKFLOW_ID="${SLACK_WORKFLOW_ID:-xvond-slack-provider-v1}"
CUSTOM_CHANNEL_WORKFLOW_FILE="${CUSTOM_CHANNEL_WORKFLOW_FILE:-ops/n8n/xvond-custom-channel-provider.workflow.json}"
CUSTOM_CHANNEL_WORKFLOW_ID="${CUSTOM_CHANNEL_WORKFLOW_ID:-xvond-custom-channel-provider-v1}"
TWILIO_SMS_WORKFLOW_FILE="${TWILIO_SMS_WORKFLOW_FILE:-ops/n8n/xvond-twilio-sms-provider.workflow.json}"
TWILIO_SMS_WORKFLOW_ID="${TWILIO_SMS_WORKFLOW_ID:-xvond-twilio-sms-provider-v1}"
MAILGUN_EMAIL_WORKFLOW_FILE="${MAILGUN_EMAIL_WORKFLOW_FILE:-ops/n8n/xvond-mailgun-email-provider.workflow.json}"
MAILGUN_EMAIL_WORKFLOW_ID="${MAILGUN_EMAIL_WORKFLOW_ID:-xvond-mailgun-email-provider-v1}"

for file in "$ACTION_WORKFLOW_FILE" "$CHANNEL_WORKFLOW_FILE" "$TELEGRAM_WORKFLOW_FILE" "$META_WORKFLOW_FILE" "$SLACK_WORKFLOW_FILE" "$CUSTOM_CHANNEL_WORKFLOW_FILE" "$TWILIO_SMS_WORKFLOW_FILE" "$MAILGUN_EMAIL_WORKFLOW_FILE"; do
    if [ ! -f "$file" ]; then
        echo "Workflow sync failed: $file not found" >&2
        exit 1
    fi
done

compose_workflow() {
    docker compose -f "$COMPOSE_FILE" --profile workflow "$@"
}

initialize_workflow_registry() {
    compose_workflow up -d workflow-postgres
    compose_workflow exec -T workflow-postgres sh -c 'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"' < ops/n8n/idempotency.sql
    compose_workflow up -d --no-deps workflow-registry
}


probe_action_gateway() {
    docker exec xvond-workflow-engine node -e '
const secret = String(process.env.N8N_SHARED_SECRET || "");
const requestId = `sync-action-${Date.now()}`;
fetch("http://127.0.0.1:5678/webhook/xvond-actions", {
  method: "POST",
  headers: {
    "content-type": "application/json",
    "x-xvond-n8n-secret": secret,
    "x-xvond-request-id": requestId,
  },
  body: JSON.stringify({
    request_id: requestId,
    company_id: 1,
    agent_id: 1,
    conversation_id: null,
    action: "health_check",
    data: {source: "workflow_sync"}
  }),
}).then(async response => {
  const text = await response.text();
  if (!response.ok) {
    console.error(`http_${response.status}:${text.slice(0, 300)}`);
    process.exit(2);
  }
  let result;
  try { result = JSON.parse(text); }
  catch (_error) {
    console.error(`invalid_json_response:${text.slice(0, 300)}`);
    process.exit(2);
  }
  if (!result || result.request_id !== requestId || result.success !== true ||
      !result.data || String(result.data.status || "").toLowerCase() !== "ok") {
    console.error(`invalid_action_gateway_response:${text.slice(0, 300)}`);
    process.exit(2);
  }
  process.exit(0);
}).catch(error => {
  console.error(`fetch_failed:${String(error && error.message || "unknown")}`);
  process.exit(2);
});'
}

probe_channel_gateway() {
    docker exec xvond-workflow-engine node -e '
const secret = String(process.env.N8N_SHARED_SECRET || "");
fetch("http://127.0.0.1:5678/webhook/xvond-channel-inbound", {
  method: "POST",
  headers: {
    "content-type": "application/json",
    "x-xvond-n8n-secret": secret,
  },
  // Deliberately incomplete: this proves the webhook is registered without
  // invoking Xvond Core or any provider route.
  body: JSON.stringify({external_message_id: "workflow-sync-probe"}),
}).then(async response => {
  const text = await response.text();
  if (!response.ok) {
    console.error(`http_${response.status}:${text.slice(0, 300)}`);
    process.exit(2);
  }
  let result;
  try { result = JSON.parse(text); }
  catch (_error) {
    console.error(`invalid_json_response:${text.slice(0, 300)}`);
    process.exit(2);
  }
  if (!result || result.success !== false || String(result.code || "") !== "invalid_contract") {
    console.error(`invalid_channel_gateway_response:${text.slice(0, 300)}`);
    process.exit(2);
  }
  process.exit(0);
}).catch(error => {
  console.error(`fetch_failed:${String(error && error.message || "unknown")}`);
  process.exit(2);
});'
}


probe_telegram_gateway() {
    docker exec xvond-workflow-engine node -e '
fetch("http://127.0.0.1:5678/webhook/xvond-telegram-provider", {
  method: "POST",
  headers: {"content-type": "application/json"},
  body: JSON.stringify({action: "health_probe"}),
}).then(async response => {
  const text = await response.text();
  if (!response.ok) {
    console.error(`http_${response.status}:${text.slice(0, 300)}`);
    process.exit(2);
  }
  let result;
  try { result = JSON.parse(text); }
  catch (_error) {
    console.error(`invalid_json_response:${text.slice(0, 300)}`);
    process.exit(2);
  }
  if (!result || result.success !== false) {
    console.error(`invalid_telegram_gateway_response:${text.slice(0, 300)}`);
    process.exit(2);
  }
  process.exit(0);
}).catch(error => {
  console.error(`fetch_failed:${String(error && error.message || "unknown")}`);
  process.exit(2);
});'
}


probe_meta_gateway() {
    docker exec xvond-workflow-engine node -e '
fetch("http://127.0.0.1:5678/webhook/xvond-meta-messaging-provider", {
  method: "POST",
  headers: {"content-type": "application/json"},
  body: JSON.stringify({action: "health_probe"}),
}).then(async response => {
  const text = await response.text();
  if (!response.ok) {
    console.error(`http_${response.status}:${text.slice(0, 300)}`);
    process.exit(2);
  }
  let result;
  try { result = JSON.parse(text); }
  catch (_error) {
    console.error(`invalid_json_response:${text.slice(0, 300)}`);
    process.exit(2);
  }
  if (!result || result.success !== false) {
    console.error(`invalid_meta_gateway_response:${text.slice(0, 300)}`);
    process.exit(2);
  }
  process.exit(0);
}).catch(error => {
  console.error(`fetch_failed:${String(error && error.message || "unknown")}`);
  process.exit(2);
});'
}

probe_slack_gateway() {
    docker exec xvond-workflow-engine node -e '
fetch("http://127.0.0.1:5678/webhook/xvond-slack-provider", {
  method: "POST",
  headers: {"content-type": "application/json"},
  body: JSON.stringify({action: "health_probe"}),
}).then(async response => {
  const text = await response.text();
  if (!response.ok) {
    console.error(`http_${response.status}:${text.slice(0, 300)}`);
    process.exit(2);
  }
  let result;
  try { result = JSON.parse(text); }
  catch (_error) {
    console.error(`invalid_json_response:${text.slice(0, 300)}`);
    process.exit(2);
  }
  if (!result || result.success !== false) {
    console.error(`invalid_slack_gateway_response:${text.slice(0, 300)}`);
    process.exit(2);
  }
  process.exit(0);
}).catch(error => {
  console.error(`fetch_failed:${String(error && error.message || "unknown")}`);
  process.exit(2);
});'
}


probe_custom_channel_gateway() {
    docker exec xvond-workflow-engine node -e '
fetch("http://127.0.0.1:5678/webhook/xvond-custom-channel-provider", {
  method: "POST",
  headers: {"content-type": "application/json"},
  body: JSON.stringify({action: "health_probe"}),
}).then(async response => {
  const text = await response.text();
  if (!response.ok) {
    console.error(`http_${response.status}:${text.slice(0, 300)}`);
    process.exit(2);
  }
  let result;
  try { result = JSON.parse(text); }
  catch (_error) {
    console.error(`invalid_json_response:${text.slice(0, 300)}`);
    process.exit(2);
  }
  if (!result || result.success !== false) {
    console.error(`invalid_custom_channel_gateway_response:${text.slice(0, 300)}`);
    process.exit(2);
  }
  process.exit(0);
}).catch(error => {
  console.error(`fetch_failed:${String(error && error.message || "unknown")}`);
  process.exit(2);
});'
}


probe_mailgun_email_gateway() {
    docker exec xvond-workflow-engine node -e '
fetch("http://127.0.0.1:5678/webhook/xvond-mailgun-email-provider", {
  method: "POST",
  headers: {"content-type": "application/json"},
  body: JSON.stringify({action: "health_probe"}),
}).then(async response => {
  const text = await response.text();
  if (!response.ok) {
    console.error(`http_${response.status}:${text.slice(0, 300)}`);
    process.exit(2);
  }
  let result;
  try { result = JSON.parse(text); }
  catch (_error) {
    console.error(`invalid_json_response:${text.slice(0, 300)}`);
    process.exit(2);
  }
  if (!result || result.success !== false) {
    console.error(`invalid_mailgun_email_gateway_response:${text.slice(0, 300)}`);
    process.exit(2);
  }
  process.exit(0);
}).catch(error => {
  console.error(`fetch_failed:${String(error && error.message || "unknown")}`);
  process.exit(2);
});'
}


probe_twilio_sms_gateway() {
    docker exec xvond-workflow-engine node -e '
fetch("http://127.0.0.1:5678/webhook/xvond-twilio-sms-provider", {
  method: "POST",
  headers: {"content-type": "application/json"},
  body: JSON.stringify({action: "health_probe"}),
}).then(async response => {
  const text = await response.text();
  if (!response.ok) {
    console.error(`http_${response.status}:${text.slice(0, 300)}`);
    process.exit(2);
  }
  let result;
  try { result = JSON.parse(text); }
  catch (_error) {
    console.error(`invalid_json_response:${text.slice(0, 300)}`);
    process.exit(2);
  }
  if (!result || result.success !== false) {
    console.error(`invalid_twilio_sms_gateway_response:${text.slice(0, 300)}`);
    process.exit(2);
  }
  process.exit(0);
}).catch(error => {
  console.error(`fetch_failed:${String(error && error.message || "unknown")}`);
  process.exit(2);
});'
}


wait_for_runtime_webhooks() {
    attempts="${1:-90}"
    count=0
    last_error=""
    while [ "$count" -lt "$attempts" ]; do
        if action_output="$(probe_action_gateway 2>&1)" &&
           channel_output="$(probe_channel_gateway 2>&1)" &&
           telegram_output="$(probe_telegram_gateway 2>&1)" &&
           meta_output="$(probe_meta_gateway 2>&1)" &&
           slack_output="$(probe_slack_gateway 2>&1)" &&
           custom_output="$(probe_custom_channel_gateway 2>&1)" &&
           twilio_output="$(probe_twilio_sms_gateway 2>&1)" &&
           mailgun_output="$(probe_mailgun_email_gateway 2>&1)"; then
            return 0
        else
            code="$?"
            last_error="${action_output:-}
${channel_output:-}
${telegram_output:-}
${meta_output:-}
${slack_output:-}
${custom_output:-}
${twilio_output:-}
${mailgun_output:-}"
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

    compose_workflow run --rm \
        -v "$PWD/ops/n8n:/import:ro" \
        workflow-engine \
        import:workflow --input="/import/$import_name"

    compose_workflow run --rm \
        workflow-engine \
        publish:workflow --id="$workflow_id"

    compose_workflow run --rm \
        workflow-engine \
        update:workflow --id="$workflow_id" --active=true
}

initialize_workflow_registry

# Stop runtime while source-controlled workflow state is replaced so database
# state and registered webhooks cannot drift during deployment.
compose_workflow stop workflow-engine >/dev/null 2>&1 || true

sync_one_workflow "$ACTION_WORKFLOW_FILE" "$ACTION_WORKFLOW_ID"
sync_one_workflow "$CHANNEL_WORKFLOW_FILE" "$CHANNEL_WORKFLOW_ID"
sync_one_workflow "$TELEGRAM_WORKFLOW_FILE" "$TELEGRAM_WORKFLOW_ID"
sync_one_workflow "$META_WORKFLOW_FILE" "$META_WORKFLOW_ID"
sync_one_workflow "$SLACK_WORKFLOW_FILE" "$SLACK_WORKFLOW_ID"
sync_one_workflow "$CUSTOM_CHANNEL_WORKFLOW_FILE" "$CUSTOM_CHANNEL_WORKFLOW_ID"
sync_one_workflow "$TWILIO_SMS_WORKFLOW_FILE" "$TWILIO_SMS_WORKFLOW_ID"
sync_one_workflow "$MAILGUN_EMAIL_WORKFLOW_FILE" "$MAILGUN_EMAIL_WORKFLOW_ID"

compose_workflow up -d --no-deps workflow-engine
wait_for_runtime_webhooks

echo "Workflow engine synced from Git: $ACTION_WORKFLOW_ID, $CHANNEL_WORKFLOW_ID, $TELEGRAM_WORKFLOW_ID, $META_WORKFLOW_ID, $SLACK_WORKFLOW_ID, $CUSTOM_CHANNEL_WORKFLOW_ID, $TWILIO_SMS_WORKFLOW_ID, $MAILGUN_EMAIL_WORKFLOW_ID"
