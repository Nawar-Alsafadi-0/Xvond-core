#!/bin/sh
set -eu

COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.production.yml}"

if [ "$#" -ne 2 ]; then
    echo "Usage: $0 <company_id> <connection_key>" >&2
    exit 2
fi

company_id="$1"
connection_key="$2"

case "$company_id" in
    ''|*[!0-9]*) echo "company_id must be numeric" >&2; exit 2 ;;
esac

docker compose -f "$COMPOSE_FILE" --profile workflow exec -T workflow-engine     node - "$company_id" "$connection_key" <<'NODE'
const companyId = Number(process.argv[2] || 0);
const connectionKey = String(process.argv[3] || '').trim();

function fail(message) {
  console.error(message);
  process.exit(1);
}

let telegramRoutes = {};
let channelRoutes = {};
try {
  telegramRoutes = JSON.parse(process.env.XVOND_TELEGRAM_ROUTES_JSON || '{}');
  channelRoutes = JSON.parse(process.env.XVOND_CHANNEL_ROUTES_JSON || '{}');
} catch (_error) {
  fail('Telegram provisioning failed: route registry JSON is invalid');
}

const routeKey = `${companyId}:${connectionKey}`;
const route = telegramRoutes[routeKey];
const gatewayRoute = channelRoutes[routeKey];
if (!route) fail(`Telegram provisioning failed: missing XVOND_TELEGRAM_ROUTES_JSON route ${routeKey}`);
if (!gatewayRoute) fail(`Telegram provisioning failed: missing XVOND_CHANNEL_ROUTES_JSON route ${routeKey}`);

const botToken = String(route.bot_token || '').trim();
const webhookSecret = String(route.webhook_secret || '').trim();
const providerSecret = String(route.provider_secret || '').trim();
const publicBase = String(process.env.N8N_WEBHOOK_URL || '').replace(/\/+$/, '');

if (!botToken) fail('Telegram provisioning failed: bot_token is missing');
if (!webhookSecret || !/^[A-Za-z0-9_-]{1,256}$/.test(webhookSecret)) {
  fail('Telegram provisioning failed: webhook_secret must match Telegram secret_token rules');
}
if (!providerSecret) fail('Telegram provisioning failed: provider_secret is missing');
if (!publicBase.startsWith('https://')) {
  fail('Telegram provisioning failed: workflow public URL must use HTTPS');
}
if (String(gatewayRoute.type || '') !== 'webhook') {
  fail('Telegram provisioning failed: Xvond channel route must use webhook type');
}
const providerUrl = String(gatewayRoute.url || '');
if (!providerUrl.startsWith('https://') || !providerUrl.includes('/webhook/xvond-telegram-provider')) {
  fail('Telegram provisioning failed: Xvond channel route must target xvond-telegram-provider');
}
if (String(gatewayRoute.secret || '') !== providerSecret) {
  fail('Telegram provisioning failed: provider secret mismatch between route registries');
}

const inboundUrl = new URL(publicBase + '/webhook/xvond-telegram-inbound');
inboundUrl.searchParams.set('company_id', String(companyId));
inboundUrl.searchParams.set('connection_key', connectionKey);

async function telegram(method, body) {
  const response = await fetch(`https://api.telegram.org/bot${botToken}/${method}`, {
    method: 'POST',
    headers: {'content-type': 'application/json'},
    body: JSON.stringify(body || {}),
  });
  const payload = await response.json().catch(() => null);
  if (!response.ok || !payload || payload.ok !== true) {
    const description = payload && payload.description ? payload.description : `HTTP ${response.status}`;
    throw new Error(`${method} failed: ${description}`);
  }
  return payload.result;
}

(async () => {
  await telegram('setWebhook', {
    url: inboundUrl.toString(),
    secret_token: webhookSecret,
    allowed_updates: ['message', 'edited_message'],
    drop_pending_updates: false,
  });

  const info = await telegram('getWebhookInfo', {});
  if (String(info.url || '') !== inboundUrl.toString()) {
    throw new Error('Telegram webhook verification failed: provider returned a different URL');
  }

  console.log(JSON.stringify({
    success: true,
    company_id: companyId,
    agent_id: Number(route.agent_id || 0),
    channel_id: Number(route.channel_id || 0),
    connection_key: connectionKey,
    webhook_url: info.url,
    pending_update_count: Number(info.pending_update_count || 0),
    last_error_date: info.last_error_date || null,
    last_error_message: info.last_error_message || null,
    allowed_updates: info.allowed_updates || [],
  }, null, 2));
})().catch(error => fail(`Telegram provisioning failed: ${String(error && error.message || 'unknown')}`));
NODE
