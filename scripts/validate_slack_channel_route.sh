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

let slackRoutes = {};
let channelRoutes = {};
try {
  slackRoutes = JSON.parse(process.env.XVOND_SLACK_ROUTES_JSON || '{}');
  channelRoutes = JSON.parse(process.env.XVOND_CHANNEL_ROUTES_JSON || '{}');
} catch (_error) {
  fail('Slack validation failed: route registry JSON is invalid');
}

const routeKey = `${companyId}:${connectionKey}`;
const route = slackRoutes[routeKey];
const gatewayRoute = channelRoutes[routeKey];
if (!route) fail(`Slack validation failed: missing route ${routeKey}`);
if (!gatewayRoute) fail(`Slack validation failed: missing Xvond channel route ${routeKey}`);

const botToken = String(route.bot_token || '').trim();
const signingSecret = String(route.signing_secret || '').trim();
const providerSecret = String(route.provider_secret || '').trim();
const configuredTeamId = String(route.team_id || '').trim();

if (!botToken || !signingSecret || !providerSecret) {
  fail('Slack validation failed: bot_token/signing_secret/provider_secret are required');
}
if (String(gatewayRoute.type || '') !== 'webhook') {
  fail('Slack validation failed: Xvond channel route must use webhook type');
}
const providerUrl = String(gatewayRoute.url || '');
if (!providerUrl.startsWith('https://') || !providerUrl.includes('/webhook/xvond-slack-provider')) {
  fail('Slack validation failed: Xvond channel route must target xvond-slack-provider');
}
if (String(gatewayRoute.secret || '') !== providerSecret) {
  fail('Slack validation failed: provider secret mismatch between route registries');
}

(async () => {
  const response = await fetch('https://slack.com/api/auth.test', {
    method: 'POST',
    headers: {authorization: `Bearer ${botToken}`},
  });
  const payload = await response.json().catch(() => null);
  if (!response.ok || !payload || payload.ok !== true) {
    const providerMessage = payload?.error || `HTTP ${response.status}`;
    throw new Error(`provider identity check failed: ${providerMessage}`);
  }
  if (configuredTeamId && String(payload.team_id || '') !== configuredTeamId) {
    throw new Error('provider identity check failed: configured team_id does not match Slack');
  }

  console.log(JSON.stringify({
    success: true,
    company_id: companyId,
    agent_id: Number(route.agent_id || 0),
    channel_id: Number(route.channel_id || 0),
    connection_key: connectionKey,
    team_id: String(payload.team_id || ''),
    bot_user_id: String(payload.user_id || ''),
    inbound_path: '/webhook/xvond-slack-inbound',
    provider_path: '/webhook/xvond-slack-provider',
  }, null, 2));
})().catch(error => fail(`Slack validation failed: ${String(error && error.message || 'unknown')}`));
NODE
