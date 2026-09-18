# Xvond Project Checkpoint

Last refreshed: 2026-09-18 (Asia/Muscat)

## Product identity and canonical architecture

Xvond is an AI Employee platform. The customer-facing product idea is:

**Build your employee.**

The same AI Employee can serve multiple communication surfaces and execute authorized operational work. Channels do not own separate persona or business truth.

Canonical runtime:

`Customer / Trigger -> Xvond Core -> AI Employee + Knowledge + Rules -> authorized Action -> Workflow Engine -> execution target -> Xvond result -> Customer`

Responsibilities are deliberately separated:

- **Xvond Core** is the product/control plane and AI decision layer.
- **AI Employee** owns identity, instructions, knowledge access, permissions and behavior.
- **Channels** such as Website and WhatsApp are communication surfaces attached to the same employee.
- **Workflow Engine** (self-hosted n8n) is the side-effect execution plane for operational actions.
- Xvond Core validates scope, permissions, required fields, confirmation, entitlement and execution state.
- AI providers never call the Workflow Engine directly.
- Workflow Engine does not access the Xvond application database directly.
- Third-party execution credentials belong to the workflow/integration plane, not to prompts or action payloads.
- External actions use stable request identity, idempotency and fail-closed results.

## Two intentional delivery models

Xvond has two different delivery models. They must not be collapsed into one lifecycle.

### Xvond Managed

Managed delivery is operator-led and keeps the strict Company Workspace / Delivery Readiness flow.

Typical sequence:

1. Create Company.
2. Complete Company Profile / Business Information.
3. Configure commercial entitlement.
4. Create AI Employee in Draft.
5. Configure identity/behavior.
6. Attach real business Knowledge.
7. Configure Actions only when operational side effects are needed.
8. Configure the intended customer Channels.
9. Complete non-customer-facing validation.
10. Activate Company and move the intended employee through Go Live.
11. Activate only the intended live channel.
12. Run controlled real external acceptance before commercial handover.

Managed readiness intentionally remains strict.

### Self-Service — Build your employee

Self-Service is Job-Brief-driven. The Job Brief is the source of truth for what the employee needs.

Canonical sequence:

1. Customer signs up into a Self-Service workspace.
2. Customer describes the job in an open-ended Job Brief.
3. Customer chooses an AI Employee service plan.
4. Xvond builds/compiles the employee specification.
5. Xvond provisions generated capabilities/actions/automation contracts.
6. Customer supplies only the inputs and communication channels actually required by the current employee contract.
7. Readiness evaluates subscription, provider route, compiled requirements, provisioned execution and required channel setup.
8. Customer launches the employee atomically.
9. Customer may deactivate, revise the Job Brief, rebuild and relaunch.
10. Recurring/background work runs through the production automation scheduler when the employee specification requires it.

Important Self-Service rules:

- A personal/background employee may legitimately require **zero communication channels**.
- Customer-facing channel slots are derived from the current Job Brief plus compiled channel requirements.
- Stale configured channels from an older Job Brief do not override the current employee contract.
- Revising a Job Brief while Draft deactivates no-longer-requested communication channels but preserves reusable connection configuration/credentials.
- Customer Website/WhatsApp setup is rejected at the API layer if that channel is outside the current Self-Service employee contract.
- A live employee must be deactivated before changing its Website/WhatsApp connection.
- Real non-Mock AI routing is required in production even for a channel-less employee.
- Knowledge requirements resolve only from enabled, tenant-owned knowledge actually attached to the employee.
- Managed delivery rules are not reused as blanket Self-Service blockers.

## Readiness model

Xvond distinguishes three different facts:

1. **Code ready** — repository CI passes.
2. **Production ready** — the reviewed release is actually deployed and API, background workers, database, Redis, routing, Workflow Engine where required, migrations and backup checks pass.
3. **Service ready** — the exact sold customer path has passed real end-to-end acceptance with its external providers and channels.

These states are intentionally different. Passing CI never proves Meta, DNS, Nginx, an AI-provider key, a customer CRM/POS/calendar, a Workflow integration route, off-site storage or a real customer channel is live.

## Current Core platform

Implemented platform foundations include:

- tenant-scoped Companies and Users
- JWT/session revocation and role-based access
- Managed and Self-Service onboarding separation
- public open-ended employee builder
- Company Profile / Business Information
- AI Employees and provider routing
- Job Brief compile/provision/revise/rebuild lifecycle
- Knowledge
- generic Actions / Action Requests
- Website and WhatsApp customer setup
- Voice runtime/provisioning foundations
- Connected Apps / Integrations metadata
- Customer Portal and unified Inbox
- Usage and provider-cost tracking
- canonical service subscriptions/entitlements/limits
- Self-Service plan selection and pending-payment state
- recurring automation scheduler
- audit/runtime observability
- production and delivery readiness checks
- PostgreSQL/Redis production Compose
- Workflow Engine/n8n with separate PostgreSQL
- local and encrypted off-site backup/restore tooling
- repeatable production release tooling

## AI runtime

Implemented provider adapters:

- OpenAI
- Anthropic / Claude
- Google / Gemini
- xAI / Grok
- Mock for development/testing only

Do not advertise a provider as live merely because its adapter exists. It must be configured and pass the intended production acceptance path.

Routing supports company default/fallback selection, eligible-provider ranking, quality limits, priority, reliability/latency/cost signals and first-request failover.

Business facts must come from current Knowledge or successful action results. The runtime must never claim a booking, order, quotation, cancellation, payment or other action succeeded unless the corresponding action reports success.

PII protection is enabled by default in production before content is sent to external AI providers. Protected values are restored locally when required for tool execution and customer-visible output.

Known non-blocking future hardening: once a provider-specific multi-round tool continuation has started, continuation remains on that provider. Cross-provider continuation-state translation is not implemented.

## Workflow Engine and business actions

The customer business-action runtime is intentionally generic.

Xvond sends safe routing/action metadata to the Workflow Engine and strips credential-like fields from Core action configuration. External provider credentials belong to the workflow plane.

Destination model:

- `xvond_internal` — Workflow Engine calls the private Xvond internal execution endpoint.
- `integration` — Workflow Engine resolves a tenant/integration route from its execution registry and invokes the external system.
- unsupported/unconfigured routes fail closed.

The private callback is protected by the shared Xvond/Workflow secret and supports the safe action contract with idempotency receipts.

Configured integration types such as CRM, POS, ERP, calendar, webhook, email and custom API are contracts/routing metadata until their real provider bindings and credentials exist in the workflow plane.

**Email/Instagram must not currently be described as service-ready customer connectors merely because a contract or catalog entry exists.**

Production release verifies Workflow Engine health when enabled and, after API cutover, verifies that n8n can reach the new API instance.

## Channel truth

### Website

Implemented:

- Self-Service Website setup
- domain normalization
- stable widget identity
- public widget script
- widget key authentication
- signed visitor tokens
- origin validation against the customer domain
- unified-conversation source tagging
- human-assistance behavior
- Draft preparation followed by atomic launch activation

The customer portal only presents Website setup when Website belongs to the current Self-Service employee contract.

A real public-domain smoke test remains mandatory before a sold Website channel is service-ready.

### WhatsApp

Implemented:

- Meta Cloud API webhook verification/signature validation
- phone-number routing across tenants
- inbound deduplication
- Redis worker queue/lease
- outbound delivery/status handling
- human handoff
- Meta Embedded Signup
- WhatsApp Business App Coexistence support
- WABA/phone ownership verification
- app subscription checks
- encrypted Meta/customer secrets
- customer-side connection flow
- live-edit guard and post-Meta-window race guard
- Self-Service Job-Brief channel-contract enforcement

WhatsApp exposes two separate truths:

- `connected` — the Meta transport is usable and required app/WABA/webhook setup is present.
- `coexistence_ready` — a real `smb_message_echoes` event has been observed, proving native WhatsApp Business App human takeover in practice.

Required Meta webhook fields for the intended coexistence path include:

- `messages`
- `smb_message_echoes`

A correctly subscribed coexistence connection may serve AI traffic before the first native human reply. That first real WhatsApp Business App reply supplies echo evidence and moves that conversation under human control.

A live customer acceptance remains mandatory before WhatsApp is called service-ready: customer inbound, AI outbound, native human reply/echo, AI suppression while human owns the conversation, portal handoff/reply, explicit Return to AI and duplicate webhook replay.

### Other communication surfaces

Voice has runtime/provisioning foundations but is not service-ready until a real provider/phone/call path passes end-to-end acceptance.

Self-Service direct live channel support currently centers on **Website and WhatsApp**. Other compiled connection requirements must remain truthfully marked as needing an Xvond/provider adapter rather than being shown as magically connectable.

## Automation scheduler

Recurring/background Self-Service employees use the production automation scheduler.

Release/runtime truth:

- scheduler runs the same reviewed application image as the API and WhatsApp worker
- scheduler publishes a TTL heartbeat to Redis
- production deploy waits for that heartbeat
- production acceptance fails closed when scheduler heartbeat is missing
- schedule execution remains bounded by the generated safe automation/runtime contract

The scheduler is not arbitrary-code execution.

## Billing truth

`ServicePlan` / `ServiceSubscription` are the canonical service entitlement and limit system. They are not a payment gateway, invoice ledger or accounting platform.

Self-Service plan flow currently behaves truthfully:

- a free plan can activate entitlement immediately
- selecting a paid plan creates/keeps `pending_payment`
- `pending_payment` does **not** grant entitlement
- repeating the same pending paid selection is idempotent
- an active subscription is not silently replaced by a customer plan change
- when Xvond/Admin activates a pending paid subscription, its billing period starts from activation time

**There is no real online payment checkout/provider in Xvond Core yet.** Paid-plan selection must not be described as completed payment.

## Security and privacy

- secure password hashing and password policy
- issuer/audience/expiry/token-version session revocation
- HttpOnly SameSite browser sessions in Admin/Customer Portal
- Secure cookies in production
- bearer-token support for non-browser API clients
- credential-free public CORS
- public Website requests require origin + widget/visitor security
- encrypted channel/integration/config secrets
- SSRF controls for outbound HTTP
- tenant-scoped customer data
- Xvond Admin is an infrastructure/configuration control plane, not a cross-tenant operator inbox for customer-content payloads

Enterprise governance claims such as formal data-residency commitments, DPA coverage, retention guarantees or subprocessor promises must not be sold unless separately implemented and contractually established.

## Production public routing

`PUBLIC_BASE_URL` is the canonical public Xvond Core origin.

Production validation requires it to be a clean HTTPS origin without credentials, path, query or fragment.

The repository Nginx installer:

- reads `PUBLIC_BASE_URL` from the process environment or repository `.env`
- targets the exact matching `server_name` token
- does not mistake a sibling/subdomain vhost for the requested host
- verifies the Core include inside the target server block
- backs up the active vhost
- validates with `nginx -t`
- reloads Nginx
- restores the prior vhost if validation/reload fails

Canonical server release sequence starts with:

```bash
python3 scripts/install_nginx_core_routes.py
./scripts/deploy_production.sh
```

## CI and production release gate

GitHub CI checks:

1. dependency installation and `pip check`
2. Python compilation
3. fresh PostgreSQL migration chain
4. frontend JavaScript syntax
5. shell syntax
6. Meta Embedded Signup tests
7. production Compose validation
8. full pytest suite
9. production container build

Production deploy additionally:

- refuses a dirty Git tree
- requires the canonical release branch (`main` by default)
- validates required production environment values and rejects placeholders
- validates production Compose
- starts/verifies PostgreSQL and Redis
- takes a fresh local PostgreSQL backup before application replacement
- builds one reviewed application image
- verifies Workflow Engine configuration/contract when enabled
- stops old WhatsApp/scheduler processes before API cutover
- recreates the API and waits for readiness
- verifies n8n -> new API reachability after cutover
- recreates WhatsApp worker and automation scheduler
- requires a real WhatsApp Redis worker lease
- requires a scheduler Redis heartbeat
- requires API, WhatsApp worker and scheduler to use the same image ID
- supports customer-specific production acceptance after cutover

## External validation boundary

Repository CI cannot truthfully prove:

- the current server has pulled/deployed the reviewed `main`
- production `.env` and live secrets are correct
- the real HTTPS/DNS/Nginx route is serving the released Core
- Meta customer Embedded Signup/Coexistence works on a real customer number
- real WhatsApp inbound/outbound/human-handoff works
- a real Website widget works on a customer domain
- a real Voice call works
- the intended production AI providers accept live requests
- a real CRM/POS/ERP/calendar/email/Instagram/API target executes correctly
- the chosen off-site backup repository restores correctly

No live provider, Meta, Workflow Engine, customer-integration or payment secret belongs in Git.

## Current release status at this checkpoint

Repository state through the Self-Service channel-contract work is **code validated**, but this checkpoint does **not** claim that the reviewed release has been deployed to the production server.

Highest-priority remaining external/product work:

1. Deploy the reviewed `main` release to the server using the canonical Nginx + deploy flow.
2. Run real production end-to-end acceptance for Website, WhatsApp/Meta, Workflow Engine and the intended AI provider.
3. Implement a real payment provider/checkout/webhook/idempotency path before calling paid Self-Service checkout automated.
4. Implement and externally validate real Email/Instagram provider adapters before selling them as live connectors.

## Branch model

- `main` = canonical release branch
- `staging` = integration mirror kept aligned through normal merges
- `feat/*`, `fix/*`, `docs/*` = temporary change branches

Do not force-reset staging merely to make commit graphs look identical. A staging compare may be ahead because of historical sync merge commits; the important release condition is that staging is not missing validated main code.
