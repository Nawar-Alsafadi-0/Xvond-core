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

let metaRoutes = {};
let channelRoutes = {};
try {
  metaRoutes = JSON.parse(process.env.XVOND_META_MESSAGING_ROUTES_JSON || '{}');
  channelRoutes = JSON.parse(process.env.XVOND_CHANNEL_ROUTES_JSON || '{}');
} catch (_error) {
  fail('Meta Messaging validation failed: route registry JSON is invalid');
}

const routeKey = `${companyId}:${connectionKey}`;
const route = metaRoutes[routeKey];
const gatewayRoute = channelRoutes[routeKey];
if (!route) fail(`Meta Messaging validation failed: missing route ${routeKey}`);
if (!gatewayRoute) fail(`Meta Messaging validation failed: missing Xvond channel route ${routeKey}`);

const channelType = String(route.channel_type || '').trim().toLowerCase();
if (!['instagram','messenger'].includes(channelType)) {
  fail('Meta Messaging validation failed: channel_type must be instagram or messenger');
}

const senderId = String(route.sender_id || '').trim();
const accessToken = String(route.access_token || '').trim();
const appSecret = String(route.app_secret || '').trim();
const providerSecret = String(route.provider_secret || '').trim();
const graphVersion = String(route.graph_version || 'v26.0').trim();

if (!senderId || !accessToken || !appSecret || !providerSecret) {
  fail('Meta Messaging validation failed: sender_id/access_token/app_secret/provider_secret are required');
}
if (String(gatewayRoute.type || '') !== 'webhook') {
  fail('Meta Messaging validation failed: Xvond channel route must use webhook type');
}
const providerUrl = String(gatewayRoute.url || '');
if (!providerUrl.startsWith('https://') || !providerUrl.includes('/webhook/xvond-meta-messaging-provider')) {
  fail('Meta Messaging validation failed: Xvond channel route must target xvond-meta-messaging-provider');
}
if (String(gatewayRoute.secret || '') !== providerSecret) {
  fail('Meta Messaging validation failed: provider secret mismatch between route registries');
}

const base = channelType === 'instagram'
  ? 'https://graph.instagram.com'
  : 'https://graph.facebook.com';

(async () => {
  const url = `${base}/${graphVersion}/${encodeURIComponent(senderId)}?fields=id`;
  const response = await fetch(url, {
    headers: {authorization: `Bearer ${accessToken}`},
  });
  const payload = await response.json().catch(() => null);
  if (!response.ok || !payload || String(payload.id || '') !== senderId) {
    const providerMessage = payload?.error?.message || `HTTP ${response.status}`;
    throw new Error(`provider identity check failed: ${providerMessage}`);
  }

  console.log(JSON.stringify({
    success: true,
    company_id: companyId,
    agent_id: Number(route.agent_id || 0),
    channel_id: Number(route.channel_id || 0),
    connection_key: connectionKey,
    channel_type: channelType,
    sender_id: senderId,
    graph_version: graphVersion,
    webhook_path: '/webhook/xvond-meta-messaging',
    provider_path: '/webhook/xvond-meta-messaging-provider',
  }, null, 2));
})().catch(error => fail(`Meta Messaging validation failed: ${String(error && error.message || 'unknown')}`));
NODE
