# Employee compilation and adaptive capability provisioning

Xvond treats the Job Brief as the source of truth. The Self-Service compiler turns
that brief into a structured employee specification, then provisions each
requirement through the safest real execution path available.

## Fulfillment decision

A requirement can be fulfilled in three ways:

- **Xvond native** — when Xvond can provide the capability directly, no external
  product is forced on the customer. Current native business capabilities include
  booking/reservations, leads, orders, quotations and support records.
- **Customer connected system** — when the owner explicitly wants an existing
  CRM/POS/ERP/booking/custom API, the protected Xvond connection is bound to that
  requirement and the action uses the hardened Core integration adapter.
- **Managed/packaged workflow** — provider/channel and other packaged Xvond
  workflows can execute through the workflow engine without exposing provider
  credentials to the customer or the employee prompt.

The compiler may also create a bounded generic Xvond capability for work that can
be represented safely by the supported runtime primitives.

## Dynamic setup is part of the build

Missing owner data is represented as structured dynamic inputs. Saving an answer
is not the end of the flow: Xvond advances the requirement back into its build
stage, re-runs capability provisioning, refreshes the employee system prompt and
recalculates readiness.

For internal booking, Xvond uses known facts from Smart Intake and asks only for
facts that are still missing, such as working days, opening/closing time,
appointment duration or capacity. The resulting booking action uses Xvond's
database-backed availability and request records with concurrency protection.

## Generic capability runtime

The generic runtime is intentionally bounded rather than arbitrary code
execution. A generated execution plan can currently use:

- `http_get_json` — HTTPS GET against an explicitly approved host grounded in
  the Job Brief.
- `extract` — read a value from returned structured data.
- `compare` — lt/lte/gt/gte/eq/neq comparisons.
- `notify` — create an idempotent Xvond notification.
- scheduled execution when the compiled requirement includes a valid schedule and
  automatic permission.

HTTP execution fails closed: credentials in URLs, local/private/reserved targets,
unapproved hosts, redirects and oversized responses are rejected. A requirement
with no executable plan remains a readiness blocker; Xvond must never present a
stored contract as a completed real capability.

## Existing systems and secrets

Self-Service owners can create company-scoped Connected Systems. Secret fields are
encrypted at rest and never returned to the browser. A connection must be bound
to a specific employee requirement before it affects execution. Generic API
operation paths are relative to the validated connection base URL, and mutating
requests receive stable Xvond idempotency headers.

Xvond Managed customers keep the operator-owned integration path. Customer-side
integration mutation endpoints are intentionally restricted to Self-Service
workspaces.

## Preview, revisions and versions

After required setup is complete, the owner tests the current build in Preview &
Test. The preview calls the employee with tools disabled, so it cannot send on
live channels or execute business actions. Successful preview evidence is tied to
the current `compiled_at` build; a rebuild, connection change or rollback
invalidates old evidence.

Owners can refine the same draft employee with short natural-language
instructions. Xvond stores bounded version history and can rollback a draft,
re-provisioning generated runtime artifacts, Builder-owned tools and channel
requirements. A restored build must be preview-tested again before launch.

## Readiness contract

`xvond_managed` or a persisted action contract does not by itself mean that the
employee is launch-ready. Readiness requires the exact runtime path for every
requirement to be provisioned. External channels and provider bindings have their
own connection/acceptance gates, and unresolved delivery or execution state
remains fail-closed.

The production Market Launch Gate is the final authority for a real customer path;
code readiness alone is not service readiness.
