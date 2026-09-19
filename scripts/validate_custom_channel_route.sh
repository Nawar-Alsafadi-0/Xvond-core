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

docker compose -f "$COMPOSE_FILE" --profile workflow exec -T workflow-engine \
    node - "$company_id" "$connection_key" <<'NODE'
const companyId = Number(process.argv[2] || 0);
const connectionKey = String(process.argv[3] || '').trim();

function fail(message) {
  console.error(message);
  process.exit(1);
}

let customRoutes = {};
let channelRoutes = {};
try {
  customRoutes = JSON.parse(process.env.XVOND_CUSTOM_CHANNEL_ROUTES_JSON || '{}');
  channelRoutes = JSON.parse(process.env.XVOND_CHANNEL_ROUTES_JSON || '{}');
} catch (_error) {
  fail('Custom channel validation failed: route registry JSON is invalid');
}

const routeKey = `${companyId}:${connectionKey}`;
const route = customRoutes[routeKey];
const gatewayRoute = channelRoutes[routeKey];
if (!route) fail(`Custom channel validation failed: missing route ${routeKey}`);
if (!gatewayRoute) fail(`Custom channel validation failed: missing Xvond channel route ${routeKey}`);

const inboundSecret = String(route.inbound_secret || '').trim();
const outboundSecret = String(route.outbound_secret || '').trim();
const providerSecret = String(route.provider_secret || '').trim();
const outboundUrl = String(route.outbound_url || '').trim();

if (!inboundSecret || !outboundSecret || !providerSecret || !outboundUrl) {
  fail('Custom channel validation failed: inbound_secret/outbound_secret/provider_secret/outbound_url are required');
}
if (inboundSecret.length < 32 || outboundSecret.length < 32 || providerSecret.length < 32) {
  fail('Custom channel validation failed: route secrets must be at least 32 characters');
}
let parsed;
try {
  parsed = new URL(outboundUrl);
} catch (_error) {
  fail('Custom channel validation failed: outbound_url is invalid');
}
if (parsed.protocol !== 'https:' || parsed.username || parsed.password) {
  fail('Custom channel validation failed: outbound_url must be credential-free HTTPS');
}
if (['localhost','127.0.0.1','0.0.0.0','::1'].includes(parsed.hostname.toLowerCase())) {
  fail('Custom channel validation failed: outbound_url may not target localhost');
}
if (String(gatewayRoute.type || '') !== 'webhook') {
  fail('Custom channel validation failed: Xvond channel route must use webhook type');
}
const providerUrl = String(gatewayRoute.url || '');
if (!providerUrl.startsWith('https://') || !providerUrl.includes('/webhook/xvond-custom-channel-provider')) {
  fail('Custom channel validation failed: Xvond channel route must target xvond-custom-channel-provider');
}
if (String(gatewayRoute.secret || '') !== providerSecret) {
  fail('Custom channel validation failed: provider secret mismatch between route registries');
}

console.log(JSON.stringify({
  success: true,
  company_id: companyId,
  agent_id: Number(route.agent_id || 0),
  channel_id: Number(route.channel_id || 0),
  connection_key: connectionKey,
  outbound_origin: parsed.origin,
  inbound_path: '/webhook/xvond-custom-channel-inbound',
  provider_path: '/webhook/xvond-custom-channel-provider',
}, null, 2));
NODE
