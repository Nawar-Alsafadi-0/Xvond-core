# Xvond Workflow Engine Runbook

This directory contains the self-hosted workflow runtime used as Xvond's external execution plane.

## Architecture

- Xvond Core is the control plane.
- The workflow engine executes external business actions.
- The workflow engine has its own PostgreSQL database.
- The workflow engine does not read or write the Xvond application database directly.
- Xvond communicates with the workflow engine only through the gateway webhook contract.
- The vendor name is infrastructure-only and should not be exposed in customer or admin product UI.

## Required environment values

Set these in `.env` before enabling the workflow profile:

- `WORKFLOW_ENGINE_VERSION`
- `WORKFLOW_DB_USER`
- `WORKFLOW_DB_PASSWORD`
- `WORKFLOW_DB_NAME`
- `WORKFLOW_ENCRYPTION_KEY`
- `WORKFLOW_HOST`
- `WORKFLOW_PUBLIC_URL`
- `WORKFLOW_PORT`
- `WORKFLOW_TIMEZONE`
- `N8N_SHARED_SECRET`
- `N8N_ENABLED=true`
- `N8N_WEBHOOK_URL=http://workflow-engine:5678/webhook/xvond-actions`

`N8N_SHARED_SECRET` must be identical in Xvond Core and the workflow-engine container.

## Safe startup

The workflow services are isolated behind the Docker Compose `workflow` profile. Normal Xvond startup is unchanged until that profile is explicitly enabled.

Run:

```sh
sh scripts/workflow_engine_up.sh
```

Equivalent command:

```sh
docker compose -f docker-compose.production.yml --profile workflow up -d workflow-postgres workflow-engine
```

## Master workflow

Import and activate the Xvond-owned gateway/provider workflows:

- `ops/n8n/xvond-actions.workflow.json` for outbound actions and managed-channel sends.
- `ops/n8n/xvond-channel-inbound.workflow.json` for normalized inbound communication-channel messages.
- `ops/n8n/xvond-telegram-provider.workflow.json` for Telegram Bot API inbound/outbound transport.
- `ops/n8n/xvond-meta-messaging-provider.workflow.json` for Instagram DM and Facebook Messenger transport.
- `ops/n8n/xvond-slack-provider.workflow.json` for Slack Events API inbound and Web API outbound transport.

The first supported action is intentionally non-destructive: `health_check`.

Expected request contract:

```json
{
  "request_id": "stable-request-id",
  "company_id": 1,
  "agent_id": 1,
  "conversation_id": null,
  "action": "health_check",
  "data": {}
}
```

Required headers:

- `X-Xvond-N8N-Secret`
- `X-Xvond-Request-ID`

Expected response contract:

```json
{
  "success": true,
  "request_id": "stable-request-id",
  "action": "health_check",
  "data": {"status": "ok"},
  "error": null
}
```

## Canonical business-action contracts

The authoritative action catalog is `ops/n8n/action-contracts.json`.

Current contracts include:

- `booking.check_availability`
- `booking.execute`
- `booking.cancel`
- `send_email.execute`
- `crm.upsert_contact`
- `crm.create_lead`
- `pos.create_order`
- `custom_api.execute`
- `notification.send`
- `channel.send`

Every side-effecting action must carry a stable `idempotency_key`. Xvond generates and persists that identity before dispatch. The workflow must reuse it when calling the third-party provider and must not invent a new request identity on retry.

Provider credentials are intentionally not stored in Git. Attach real credentials only inside the workflow engine when configuring the target provider. Switching providers must not require a Xvond Core code change as long as the workflow preserves the canonical request/response contract.

A successful side-effect response must only be returned after the external provider confirms success:

```json
{
  "success": true,
  "request_id": "same-stable-request-id",
  "action": "booking.execute",
  "data": {"booking_id": "external-id"},
  "error": null
}
```

Failures must return `success: false` and an error message without pretending the business operation succeeded.

## Managed communication channels

Telegram, Instagram DM, Facebook Messenger, Email, SMS, Slack, Microsoft Teams and Custom/API channels use one Xvond-managed channel contract instead of separate Core adapters. The provider workflow normalizes inbound events to `ops/n8n/channel-adapter.contract.json`; Xvond Core owns conversation continuity, AI execution, knowledge, tools and handoff state.

Set these workflow-engine environment values:

- `XVOND_INTERNAL_CHANNEL_URL=http://app:8000/internal/channels/message`
- `XVOND_CHANNEL_ROUTES_JSON` with keys in the form `company_id:connection_key`

A channel route points to a provider-specific webhook owned by the workflow engine. Provider OAuth/API credentials stay in that provider workflow or its credential store. Do not store those provider credentials in Xvond Core. A managed channel must remain disabled until its Xvond channel config contains a non-secret `connection_key`, `provisioning_state=connected`, and the shared Xvond workflow gateway is enabled.

Inbound provider workflows must supply a stable provider message ID as `external_message_id`. Xvond uses it for deduplication, so provider retries do not create duplicate customer turns. Outbound `channel.send` calls must honor the provided `idempotency_key` before performing a side effect.


### Telegram provider

Telegram is the first concrete provider binding for the universal channel contract.

Configure both registries with the same tenant-scoped key `company_id:connection_key`:

- `XVOND_CHANNEL_ROUTES_JSON` points the generic channel gateway to `https://<workflow-host>/webhook/xvond-telegram-provider` and carries only the Xvond provider-route secret.
- `XVOND_TELEGRAM_ROUTES_JSON` carries the workflow-only Telegram settings: `company_id`, `agent_id`, `channel_id`, `bot_token`, `webhook_secret`, and the matching `provider_secret`.

Never copy the Telegram bot token into Xvond Core channel config.

After the workflow is deployed, provision the Telegram webhook from inside the workflow container:

```sh
sh scripts/provision_telegram_channel.sh <company_id> <connection_key>
```

The provisioning command validates both route registries, calls Telegram `setWebhook` with the route's secret token, then verifies the installed URL with `getWebhookInfo`. Only after that succeeds should the Xvond channel config be marked `provisioning_state=connected`.

Telegram inbound `update_id` is used as the stable external message identity. Telegram `sendMessage` must return a provider `message_id`; Xvond will not mark delivery accepted without it.


### Meta Messaging provider — Instagram DM and Messenger

Instagram DM and Facebook Messenger share the Xvond Meta Messaging provider workflow while remaining separate employee channels.

Configure:

- `XVOND_META_MESSAGING_VERIFY_TOKEN` as the Meta webhook verification token.
- `XVOND_META_MESSAGING_ROUTES_JSON` with tenant-scoped keys `company_id:connection_key`.
- `XVOND_CHANNEL_ROUTES_JSON` with the same keys pointing to `https://<workflow-host>/webhook/xvond-meta-messaging-provider`.

Each Meta route contains the Xvond company/agent/channel ids, `channel_type` (`instagram` or `messenger`), the provider sender/account id, Graph version, provider access token, Meta app secret and the matching Xvond provider-route secret. These credentials remain in the workflow plane.

The POST webhook keeps the provider raw request body and validates `X-Hub-Signature-256` with HMAC SHA-256 before parsing customer messages. Invalid signatures fail closed with HTTP 403. Message echoes and unsupported/non-text events are acknowledged without entering Xvond.

Inbound provider `message.mid` becomes Xvond's stable external message identity. Outbound delivery uses the appropriate Graph endpoint for the channel and Xvond accepts success only when Meta returns `message_id`.

Validate a configured route before marking its Xvond channel connected:

```sh
sh scripts/validate_meta_messaging_route.sh <company_id> <connection_key>
```

The validation command checks both route registries and confirms that the configured sender/account id is reachable with the stored provider token. It never prints the provider credentials.

Provider-side setup remains an external acceptance gate. For Messenger, the Meta app/Page must have the messaging permissions needed for the intended Page. For Instagram, the professional account/app must have the messaging permission required by the Instagram Messages API. Meta webhook subscription, app review/access level where required, customer message eligibility and one real inbound/outbound round trip must all pass before that customer channel is called service-ready.

### Slack provider

Slack now has a source-controlled binding on the shared Managed Channel Gateway.

Configure both registries with the same tenant-scoped key `company_id:connection_key`:

- `XVOND_CHANNEL_ROUTES_JSON` points the generic channel gateway to `https://<workflow-host>/webhook/xvond-slack-provider` and carries only the Xvond provider-route secret.
- `XVOND_SLACK_ROUTES_JSON` carries workflow-only Slack settings: `company_id`, `agent_id`, `channel_id`, `bot_token`, `signing_secret`, optional `team_id`, and the matching `provider_secret`.

The Slack Events API Request URL is:

`https://<workflow-host>/webhook/xvond-slack-inbound?company_id=<id>&connection_key=<key>`

Inbound requests are verified from the raw body using Slack's `v0:<timestamp>:<raw_body>` HMAC-SHA256 signature and are rejected when the request timestamp is older than five minutes. URL verification challenges are answered by the same webhook. Bot-authored events and unsupported message subtypes are ignored.

Inbound `event_id` is used as the stable external message identity. The Slack conversation/channel id is used as the external contact id so replies return to the same Slack conversation. Outbound delivery uses `chat.postMessage`; Xvond accepts success only when Slack returns a message `ts`, which becomes the provider message id.

Validate the route before marking the Xvond channel connected:

```sh
sh scripts/validate_slack_channel_route.sh <company_id> <connection_key>
```

The validator checks both route registries and calls Slack `auth.test` with the workflow-only bot token. It never prints the bot token, signing secret or provider secret.

Slack remains subject to real provider acceptance: install the app into the intended workspace, grant the event/message scopes needed for the sold path, configure the Events API Request URL, and prove one real inbound/outbound round trip before calling that customer channel service-ready.


## Booking adapter

The provider-neutral booking contract is `ops/n8n/booking-adapter.contract.json`.

The workflow database schema for side-effect idempotency is `ops/n8n/idempotency.sql`.

For `booking.execute` and `booking.cancel`, use this order exactly:

1. Validate the canonical Xvond payload.
2. Atomically claim `idempotency_key` in the workflow database before calling the provider.
3. If the key already exists with `completed`, return the stored prior result without calling the provider again.
4. If the key already exists with `processing` or `ambiguous`, do not blindly repeat the provider side effect. Reconcile with the provider first.
5. Call the configured booking/calendar provider using credentials stored only in the workflow engine.
6. After confirmed provider success, persist `completed`, the provider reference, and the result payload.
7. Only then return `success: true` to Xvond.
8. For a clear provider rejection, persist `failed` and return `success: false`.
9. For a timeout or unknown provider outcome after the request may have been accepted, persist `ambiguous`; never auto-retry the side effect until reconciliation proves it did not execute.

`booking.check_availability` is read-only and does not require a side-effect claim, but it must still return only provider-confirmed availability.

The first provider binding can be Google Calendar, Microsoft 365 Calendar, a salon/clinic booking API, or another calendar system. The provider binding must preserve the same Xvond booking contract so Xvond Core does not change when providers change.

## Production promotion checklist

1. Keep the workflow engine bound to localhost and expose it only through the reverse proxy.
2. Use HTTPS for the public editor/webhook origin.
3. Use unique strong values for the workflow database password, encryption key, and shared secret.
4. Never reuse the Xvond application database credentials.
5. Activate the imported workflow only after the secret and webhook endpoint are configured.
6. Verify `health_check` end-to-end before enabling any business action.
7. For every mutating action, use the Xvond request identity as the workflow idempotency key.
8. Store third-party OAuth/API credentials in the workflow engine when that engine performs the external operation.
9. Do not give workflows direct database access to Xvond Core.
10. Run the workflow engine security audit after initial setup and after material configuration changes.

## Rollback

To stop the workflow runtime without affecting Xvond Core:

```sh
docker compose -f docker-compose.production.yml --profile workflow stop workflow-engine workflow-postgres
```

Set `N8N_ENABLED=false` in Xvond and restart the app. The preserved legacy implementation remains in the repository, but it is not the registered runtime path.
