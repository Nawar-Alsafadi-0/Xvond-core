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

let twilioRoutes = {};
let channelRoutes = {};
try {
  twilioRoutes = JSON.parse(process.env.XVOND_TWILIO_SMS_ROUTES_JSON || '{}');
  channelRoutes = JSON.parse(process.env.XVOND_CHANNEL_ROUTES_JSON || '{}');
} catch (_error) {
  fail('Twilio SMS validation failed: route registry JSON is invalid');
}

const routeKey = `${companyId}:${connectionKey}`;
const route = twilioRoutes[routeKey];
const gatewayRoute = channelRoutes[routeKey];
if (!route) fail(`Twilio SMS validation failed: missing route ${routeKey}`);
if (!gatewayRoute) fail(`Twilio SMS validation failed: missing Xvond channel route ${routeKey}`);

const accountSid = String(route.account_sid || '').trim();
const authToken = String(route.auth_token || '');
const providerSecret = String(route.provider_secret || '');
const inboundUrl = String(route.inbound_url || '').trim();
const fromNumber = String(route.from_number || '').trim();
const messagingServiceSid = String(route.messaging_service_sid || '').trim();

if (!/^AC[a-fA-F0-9]{32}$/.test(accountSid)) {
  fail('Twilio SMS validation failed: account_sid format is invalid');
}
if (authToken.length < 16 || providerSecret.length < 32) {
  fail('Twilio SMS validation failed: auth_token/provider_secret are missing or too short');
}
if ((!fromNumber && !messagingServiceSid) || (fromNumber && messagingServiceSid)) {
  fail('Twilio SMS validation failed: configure exactly one sender: from_number or messaging_service_sid');
}
if (messagingServiceSid && !/^MG[a-fA-F0-9]{32}$/.test(messagingServiceSid)) {
  fail('Twilio SMS validation failed: messaging_service_sid format is invalid');
}
let parsed;
try { parsed = new URL(inboundUrl); }
catch (_error) { fail('Twilio SMS validation failed: inbound_url is invalid'); }
if (
  parsed.protocol !== 'https:'
  || !parsed.pathname.endsWith('/webhook/xvond-twilio-sms-inbound')
  || parsed.searchParams.get('company_id') !== String(companyId)
  || parsed.searchParams.get('connection_key') !== connectionKey
) {
  fail('Twilio SMS validation failed: inbound_url must be the exact signed Xvond Twilio webhook URL');
}
if (String(gatewayRoute.type || '') !== 'webhook') {
  fail('Twilio SMS validation failed: Xvond channel route must use webhook type');
}
const providerUrl = String(gatewayRoute.url || '');
if (!providerUrl.startsWith('https://') || !providerUrl.includes('/webhook/xvond-twilio-sms-provider')) {
  fail('Twilio SMS validation failed: Xvond channel route must target xvond-twilio-sms-provider');
}
if (String(gatewayRoute.secret || '') !== providerSecret) {
  fail('Twilio SMS validation failed: provider secret mismatch between route registries');
}

console.log(JSON.stringify({
  success: true,
  company_id: companyId,
  agent_id: Number(route.agent_id || 0),
  channel_id: Number(route.channel_id || 0),
  connection_key: connectionKey,
  sender_mode: messagingServiceSid ? 'messaging_service' : 'phone_number',
  inbound_path: '/webhook/xvond-twilio-sms-inbound',
  provider_path: '/webhook/xvond-twilio-sms-provider',
}, null, 2));
NODE
