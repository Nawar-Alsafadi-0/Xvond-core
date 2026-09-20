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
function fail(message){ console.error(message); process.exit(1); }

let routes = {};
let channelRoutes = {};
try {
  routes = JSON.parse(process.env.XVOND_MICROSOFT_TEAMS_ROUTES_JSON || '{}');
  channelRoutes = JSON.parse(process.env.XVOND_CHANNEL_ROUTES_JSON || '{}');
} catch (_error) {
  fail('Microsoft Teams validation failed: route registry JSON is invalid');
}
const routeKey = `${companyId}:${connectionKey}`;
const route = routes[routeKey];
const gatewayRoute = channelRoutes[routeKey];
if (!route) fail(`Microsoft Teams validation failed: missing route ${routeKey}`);
if (!gatewayRoute) fail(`Microsoft Teams validation failed: missing Xvond channel route ${routeKey}`);

const appId = String(route.microsoft_app_id || '').trim();
const appPassword = String(route.microsoft_app_password || '');
const providerSecret = String(route.provider_secret || '');
const tenantId = String(route.microsoft_tenant_id || 'botframework.com').trim();
const tenantGuid = /^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$/;
const serviceUrl = String(route.service_url || '').trim().replace(/\/$/,'');
if (!/^[0-9a-fA-F-]{36}$/.test(appId)) fail('Microsoft Teams validation failed: microsoft_app_id format is invalid');
if (tenantId !== 'botframework.com' && !tenantGuid.test(tenantId)) fail('Microsoft Teams validation failed: microsoft_tenant_id must be a tenant GUID when set');
if (appPassword.length < 16 || providerSecret.length < 32) fail('Microsoft Teams validation failed: credentials are missing or too short');
let parsed;
try { parsed = new URL(serviceUrl); } catch (_error) { fail('Microsoft Teams validation failed: service_url is invalid'); }
if (parsed.protocol !== 'https:') fail('Microsoft Teams validation failed: service_url must use HTTPS');
if (String(gatewayRoute.type || '') !== 'webhook') fail('Microsoft Teams validation failed: Xvond channel route must use webhook type');
const providerUrl = String(gatewayRoute.url || '');
if (!providerUrl.startsWith('https://') || !providerUrl.includes('/webhook/xvond-microsoft-teams-provider')) {
  fail('Microsoft Teams validation failed: Xvond channel route must target xvond-microsoft-teams-provider');
}
if (String(gatewayRoute.secret || '') !== providerSecret) {
  fail('Microsoft Teams validation failed: provider secret mismatch between route registries');
}

const form = new URLSearchParams();
form.set('grant_type','client_credentials');
form.set('client_id',appId);
form.set('client_secret',appPassword);
form.set('scope','https://api.botframework.com/.default');
const tokenUrl = 'https://login.microsoftonline.com/' + encodeURIComponent(tenantId) + '/oauth2/v2.0/token';
fetch(tokenUrl,{
  method:'POST',
  headers:{'Content-Type':'application/x-www-form-urlencoded'},
  body:form.toString(),
}).then(async response => {
  const payload = await response.json().catch(()=>null);
  if (!response.ok || !payload?.access_token) fail('Microsoft Teams validation failed: Bot Framework OAuth credentials were rejected');
  console.log(JSON.stringify({
    success:true,
    company_id:companyId,
    agent_id:Number(route.agent_id || 0),
    channel_id:Number(route.channel_id || 0),
    connection_key:connectionKey,
    service_url:serviceUrl,
    oauth_tenant_mode:tenantId === 'botframework.com' ? 'multi_tenant' : 'single_tenant',
    inbound_path:'/webhook/xvond-microsoft-teams-inbound',
    provider_path:'/webhook/xvond-microsoft-teams-provider'
  }, null, 2));
}).catch(error => fail('Microsoft Teams validation failed: ' + String(error && error.message || 'request failed')));
NODE
