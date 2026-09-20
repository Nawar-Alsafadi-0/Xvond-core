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
function fail(message) { console.error(message); process.exit(1); }

let routes = {};
let channelRoutes = {};
try {
  routes = JSON.parse(process.env.XVOND_MAILGUN_EMAIL_ROUTES_JSON || '{}');
  channelRoutes = JSON.parse(process.env.XVOND_CHANNEL_ROUTES_JSON || '{}');
} catch (_error) {
  fail('Mailgun email validation failed: route registry JSON is invalid');
}
const routeKey = `${companyId}:${connectionKey}`;
const route = routes[routeKey];
const gatewayRoute = channelRoutes[routeKey];
if (!route) fail(`Mailgun email validation failed: missing route ${routeKey}`);
if (!gatewayRoute) fail(`Mailgun email validation failed: missing Xvond channel route ${routeKey}`);

const domain = String(route.domain || '').trim();
const apiKey = String(route.api_key || '');
const signingKey = String(route.webhook_signing_key || '');
const providerSecret = String(route.provider_secret || '');
const fromAddress = String(route.from_address || route.recipient || '').trim();
const recipient = String(route.recipient || '').trim();
const region = String(route.region || 'us').trim().toLowerCase();

if (!domain || !domain.includes('.')) fail('Mailgun email validation failed: domain is invalid');
if (apiKey.length < 16 || signingKey.length < 16 || providerSecret.length < 32) {
  fail('Mailgun email validation failed: provider credentials are missing or too short');
}
if (!fromAddress.includes('@') || (recipient && !recipient.includes('@'))) {
  fail('Mailgun email validation failed: email address configuration is invalid');
}
if (!['us','eu'].includes(region)) fail('Mailgun email validation failed: region must be us or eu');
if (String(gatewayRoute.type || '') !== 'webhook') {
  fail('Mailgun email validation failed: Xvond channel route must use webhook type');
}
const providerUrl = String(gatewayRoute.url || '');
if (!providerUrl.startsWith('https://') || !providerUrl.includes('/webhook/xvond-mailgun-email-provider')) {
  fail('Mailgun email validation failed: Xvond channel route must target xvond-mailgun-email-provider');
}
if (String(gatewayRoute.secret || '') !== providerSecret) {
  fail('Mailgun email validation failed: provider secret mismatch between route registries');
}

const host = region === 'eu' ? 'https://api.eu.mailgun.net' : 'https://api.mailgun.net';
const basic = Buffer.from('api:' + apiKey).toString('base64');
fetch(host + '/v3/domains/' + encodeURIComponent(domain), {
  headers: {Authorization: 'Basic ' + basic},
}).then(async response => {
  if (!response.ok) fail('Mailgun email validation failed: Mailgun domain credentials were rejected');
  console.log(JSON.stringify({
    success:true,
    company_id:companyId,
    agent_id:Number(route.agent_id || 0),
    channel_id:Number(route.channel_id || 0),
    connection_key:connectionKey,
    domain,
    region,
    inbound_path:'/webhook/xvond-mailgun-email-inbound',
    provider_path:'/webhook/xvond-mailgun-email-provider',
  }, null, 2));
}).catch(error => fail('Mailgun email validation failed: ' + String(error && error.message || 'request failed')));
NODE
