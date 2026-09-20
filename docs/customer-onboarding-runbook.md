# Customer Onboarding Runbook

This runbook is the default path for taking a signed Xvond customer from handoff to production. Deviations require an explicit technical/operations note.

## 1. Sales handoff

Record the agreed business outcome, service family, package, expected channels, languages, expected volume, required integrations, human-handoff owner and launch target. Do not use a business type as a substitute for the customer's actual services or policies.

Exit condition: the solution scope is specific enough that Operations can identify the required employee, knowledge, tools and channels.

## 2. Create tenant and owner

Create the Company through Xvond Admin. The company starts in `onboarding`; production runtime is off. Create the customer owner account and confirm the owner can access the Customer Portal.

Exit condition: tenant isolation is established and the customer owner can sign in without enabling production AI traffic.

## 3. Assign commercial entitlements

Assign canonical `ServicePlan` / `ServiceSubscription` records for every sold service. Do not create or rely on legacy generic subscriptions.

Exit condition: readiness and usage limits see exactly the services included in the signed scope.

## 4. Capture business truth

Customer/Operations completes Company Profile and Business Information: legal/display name where relevant, business type, services/products, working hours, policies, locations, contact paths and any facts the AI employee is allowed to state.

Exit condition: required business facts are stored canonically. Missing services remain missing; Xvond never invents them.

## 5. Create AI employee

Prefer an approved Agent Template when the role matches a standard Xvond delivery pattern. Customize only the fields required by the customer's actual operation.

Configure role, behavior, supported languages/dialect, response style, capabilities, customer controls and real provider/model routing. The employee remains Draft/off until Delivery Readiness passes.

Exit condition: one canonical AI employee represents the role across all assigned channels.

## 6. Add knowledge

Attach approved text, URLs and/or documents to the employee. Confirm tenant and employee ownership. Validate retrieval against representative customer questions, including fallback behavior when embeddings are unavailable.

Exit condition: expected answers are grounded in saved business facts/knowledge and unsupported facts are not invented.

## 7. Configure tools and integrations

Assign only the tools required for the sold workflow. Validate integration secrets through Xvond Admin and perform a non-destructive test where possible. External workflows must honor Xvond request IDs as idempotency keys.

For any employee with enabled business actions, the canonical Workflow Engine health action must succeed before employee Go Live. A configured URL/secret alone is not proof that the workflow is active.

For a channel-free background employee, run its provisioned workflow at least once through the real automation runtime. The market launch gate must see a finished successful run belonging to that exact employee; a compiled graph or enabled schedule alone is not execution evidence.

Exit condition: every enabled tool has a real execution path, safe error behavior and an owner for unresolved operations.

## 8. Configure channels

Connect the purchased channels to the employee. A channel never gets an independent AI persona.

For WhatsApp Coexistence, verify Embedded Signup ownership, phone/token validity, WABA app subscription and the required `messages` and `smb_message_echoes` webhook fields. These facts establish usable Meta transport. A real Business App echo is separate evidence that automatic human takeover has worked in practice and is verified during the controlled live-channel acceptance step below.

For Website, validate widget origin/configuration. Voice and other channels must expose only capabilities actually supported.

Exit condition: every intended channel belongs to the correct Company and AI Employee, its source identity is stable and its configuration can pass its pre-activation checks.

## 9. Move to testing

Operations sets Company lifecycle to `testing`. Customer Portal remains accessible and production customer runtime remains off.

Run all validation that does not require accepting real public channel traffic: representative FAQ/service questions, unsupported questions, Knowledge retrieval, provider timeout/failure behavior, action preparation/confirmation rules, integration configuration, duplicate/idempotency tests, Admin/Customer access boundaries and channel configuration checks.

Do not pretend a real Meta/Vapi event was tested while the production route is disabled.

Exit condition: no open P0/P1 defect applies to the customer's sold path and all pre-live checks are green.

## 10. Business action acceptance

For booking/order/lead/custom workflows, validate the canonical Workflow Engine health path and at least one representative action path against the intended execution target. For mutating actions, use a controlled test record and validate the relevant failure/retry path.

Verify duplicate inbound or repeated action dispatch cannot execute a business side effect twice. Unknown external outcomes go to reconciliation rather than blind retry.

Exit condition: the exact sold operational path is known to execute or fail safely, and a customer-facing retry cannot duplicate a side effect.

## 11. Pre-live production acceptance

Run the version-controlled `scripts/production_acceptance.py` check for the target Company/AI Employee. Confirm database, migration head, Redis, real AI route, service entitlements, setup readiness, WhatsApp worker health where configured, Workflow Engine health for operational employees, unresolved delivery/action attention and backup freshness.

Optionally include the live-AI probe to verify a real provider route without creating a customer conversation.

Also verify the public `/admin-ui`, `/customer-ui` and `/health/ready` routes reach Core through the intended production reverse proxy.

Exit condition: the pre-live acceptance report is green or every non-green item has an explicit approved exception that does not affect the sold path.

## 12. Controlled production activation

External providers such as Meta and Vapi cannot prove real inbound/outbound delivery while the production route is disabled. Therefore final channel acceptance uses a controlled activation window rather than weakening the `testing` lifecycle.

In this order:

1. Set Company lifecycle to `live` only after the pre-live gate passes.
2. Use Delivery Readiness to Go Live the intended AI Employee. Operational employees perform a real Workflow Engine health check before enablement.
3. Activate only the channel being accepted.
4. Immediately execute the real-channel acceptance script with a designated test customer/number/account.

Do not treat this technical activation as customer handover. If the real-channel acceptance fails, deactivate the affected channel/employee or use the Company emergency stop immediately, return the lifecycle to a non-live state and resolve the failure before customer handover.

Exit condition: only the minimum intended runtime is active and the team is actively performing final acceptance.

## 13. Real channel and handoff acceptance

For each sold channel, test the actual provider path rather than only an internal chat endpoint.

For WhatsApp Coexistence, prove all of the following on the deployed image:

- one real customer inbound message reaches the intended Company/AI Employee;
- AI sends exactly one reply to the same WhatsApp conversation;
- the conversation/source identity remains stable;
- a reply sent from the native WhatsApp Business App produces a real `smb_message_echoes` event;
- the echo is mirrored to the Inbox and establishes `coexistence_ready` evidence;
- human takeover suppresses subsequent AI replies;
- a Customer Portal operator can claim/reply on the same conversation;
- explicit Return to AI resumes automation;
- replaying the same webhook does not duplicate messages, actions or delivery.

For Website, prove the public widget origin, visitor continuity, handoff/reply and Return to AI on the real site.

For Voice, prove a real provider phone call, authenticated callback, response latency/behavior and any sold action/handoff behavior.

For a channel-free background employee, prove one successful provisioned automation run on the deployed image and verify its stored output. Use `--require-automation-run` in the market launch gate; production deployment also applies its cutover start time so an older success cannot authorize a new release. The automation requirement may be combined with channel requirements for a hybrid employee.

Exit condition: every sold live channel has a real end-to-end acceptance result. A configured credential is not acceptance evidence.

## 14. Customer handover

Only after controlled activation and real-channel acceptance succeed should Operations declare the service launched to the customer.

Record launch timestamp, package, active employee IDs, channels, support owner, acceptance evidence and known non-blocking limitations. Confirm the customer owner can access the Portal and knows the human-handoff/support process.

Exit condition: the commercial launch record matches the runtime that was actually accepted.

## 15. First-day monitoring

Run production acceptance again with `--require-live`. Review inbound processing, AI failures/latency, business actions, handoffs, outbound delivery statuses, queue/dead jobs and usage. Check that customer-facing conversation counts match the Inbox and no test/unclassified traffic appears in live views.

For any `unknown` external action or delivery result, reconcile before retrying. Never resend a potentially accepted side effect blindly.

## 16. Ongoing service

Operations owns lifecycle changes, subscription changes, incident triage and renewal coordination. Customer manages approved business facts/knowledge and its own team within allowed permissions. Technical owns defects, releases, migrations, platform reliability, backups and provider/integration escalation.

### Pause

Use `paused` when the relationship remains active but runtime should stop temporarily. Preserve employee configuration so service can resume after checks.

### Suspend

Use `suspended` for a commercial/security/operational block. Portal and runtime access are blocked until Xvond explicitly resolves the condition.

### Cancel / Archive

`cancelled` terminates service. `archived` is the controlled historical state after retention/export obligations are addressed. Neither is a temporary runtime switch.
