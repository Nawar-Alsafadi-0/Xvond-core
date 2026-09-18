# Xvond Managed Channel Adapter Contract

This contract is for communication channels delivered through Xvond Managed using the self-hosted Workflow Engine.

The customer never interacts with n8n. Xvond Core owns the AI Employee, conversation history, permissions, knowledge, handoff state and readiness. n8n only normalizes provider traffic and performs provider-specific delivery.

## Supported managed surfaces

The generic bridge can be used for:

- Telegram
- Instagram DM
- Facebook Messenger
- Email
- SMS
- Slack
- Microsoft Teams
- Custom/API communication channels

A catalog entry means Xvond has a runtime bridge for the surface. It does **not** mean the provider route is provisioned or externally accepted for a customer.

## Route identity

Every managed channel has one Xvond route key:

```
<company_id>:<agent_channel_id>
```

Xvond Admin shows this as **Route** on the managed channel request card.

The self-hosted Workflow Engine reads `XVOND_WORKFLOW_CHANNELS_JSON`. Example:

```json
{
  "12:44": {
    "type": "webhook",
    "channel_type": "instagram",
    "url": "https://workflow.example.com/webhook/xvond-instagram-12-44",
    "secret": "provider-adapter-secret"
  }
}
```

Provider credentials belong to the provider-specific n8n workflow/credential store, not this repository and not the AI Employee prompt.

## Outbound provider adapter

The central `Xvond Managed Channels Gateway` sends a normalized POST request to the route URL.

Headers include:

- `X-Xvond-Channel-Secret`
- `X-Xvond-Request-ID`
- `Idempotency-Key`

Body:

```json
{
  "request_id": "stable request id",
  "company_id": 12,
  "agent_id": 8,
  "channel_id": 44,
  "channel_type": "instagram",
  "external_contact_id": "provider user/thread id",
  "message": "Reply text",
  "idempotency_key": "stable delivery identity"
}
```

The provider workflow must deduplicate mutating delivery by `idempotency_key` and return:

```json
{
  "success": true,
  "provider_message_id": "provider message id"
}
```

On failure it should return a bounded provider-safe code and no credentials.

## Inbound provider adapter

A provider-specific n8n trigger/webhook must normalize the provider event and POST it to:

```
POST /internal/channels/inbound
X-Xvond-N8N-Secret: <N8N_SHARED_SECRET>
```

Body:

```json
{
  "channel_id": 44,
  "external_contact_id": "provider user/thread id",
  "external_message_id": "provider event/message id",
  "message": "Customer message"
}
```

Xvond returns one of:

- `reply_ready` — send `response.content` back through the provider.
- `duplicate` — this provider message was already processed; reuse the returned response instead of running the employee again.
- `human_active` — the conversation is under human control; do not send an AI reply.

The external message ID must be stable for provider retries. Xvond binds it to a unique persisted message source key.

## Delivery confirmation

After a real provider delivery of an AI or human reply, the provider adapter should POST:

```
POST /internal/channels/delivery-confirmed
X-Xvond-N8N-Secret: <N8N_SHARED_SECRET>
```

Body:

```json
{
  "channel_id": 44,
  "conversation_id": 991,
  "response_message_id": 7712,
  "provider_message_id": "provider-message-abc"
}
```

This records system-owned customer roundtrip evidence on the Xvond channel.

## Admin provisioning sequence

1. The Job Brief or Managed delivery creates the disabled channel request.
2. Xvond staff creates/configures the provider-specific n8n adapter and adds its route to `XVOND_WORKFLOW_CHANNELS_JSON`.
3. In Xvond Admin, open the company -> Channels.
4. The card shows the route key.
5. Click **Verify Xvond Route**.
6. Xvond marks `provisioning_state=connected` only after the central workflow gateway confirms the route exists.
7. Complete the normal Company/Employee readiness requirements.
8. Activate the channel from Xvond Admin.
9. Run a real provider-specific inbound -> AI reply -> provider delivery -> human takeover/return-to-AI acceptance.
10. Only then call that customer/channel service-ready.

## Production workflow sync

`scripts/sync_workflow_engine.sh` publishes and activates both source-controlled Xvond gateway workflows:

- `ops/n8n/xvond-actions.workflow.json`
- `ops/n8n/xvond-channels.workflow.json`

Provider-specific channel workflows remain customer/provider provisioning artifacts and are not permitted to access the Xvond application database directly.
